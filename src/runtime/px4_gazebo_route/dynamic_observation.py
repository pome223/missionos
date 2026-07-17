"""Dynamic actor observation for the opt-in PX4/Gazebo route runtime.

The functions in this module accept explicit simulator observations and project
bounded artifacts. They cannot approve, dispatch, mutate a task, or claim
delivery or physical execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import time
from typing import Any
import xml.etree.ElementTree as ET

from src.runtime.px4_gazebo_route.observation import point_to_segment_distance_m


def _observed_at_text(value: datetime | None) -> str:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc).isoformat()


def run_moving_actor_waypoint_motion_application(
    *,
    requested: bool,
    motion_spec: Mapping[str, Any],
    trajectory_definition_sha256: str,
    operational_realism_summary: Mapping[str, Any] | None,
    pose_sample: Callable[[], Mapping[str, float]],
    sleep: Callable[[float], None] = time.sleep,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    requested = bool(requested)
    spec = dict(motion_spec)
    trajectory_sha256 = trajectory_definition_sha256
    operational_application = (operational_realism_summary or {}).get(
        "operational_application",
        {},
    )
    operational_applied = operational_application.get("applied") or {}
    unsupported_reasons: list[str] = []
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    application_status = "not_requested"
    if requested:
        world_path_text = str(operational_applied.get("world_sdf_path") or "")
        world_path = Path(world_path_text) if world_path_text else None
        if operational_application.get("application_status") != "applied_with_approximations":
            unsupported_reasons.append("moving_actor_operational_application_not_applied")
        if world_path is None or not world_path.exists():
            unsupported_reasons.append("moving_actor_world_sdf_missing")
        if unsupported_reasons:
            application_status = "unsupported"
            observed = {
                "source": "moving_actor_operational_application",
                "observed": False,
                "moving_actor_present": False,
                "moving_actor_trajectory_materialized": False,
                "moving_actor_pose_stream_observed": False,
                "moving_actor_velocity_readback_observed": False,
                "telemetry_publisher_state_mutated": False,
                "mission_upload_path_mutated": False,
                "mission_progress_mutated": False,
                "incident_verified": False,
                "route_blocking_verified": False,
                "traffic_conflict_verified": False,
                "collision_obstacle_observed": False,
                "task_status_mutated": False,
                "gate_status_mutated": False,
                "delivery_completion_claimed": False,
            }
        else:
            assert world_path is not None
            world_text = world_path.read_text(encoding="utf-8")
            world_sha256 = hashlib.sha256(world_text.encode("utf-8")).hexdigest()
            expected_world_sha256 = str(operational_applied.get("world_sdf_sha256") or "")
            world_sdf_hash_match = bool(
                expected_world_sha256 and world_sha256 == expected_world_sha256
            )
            actor_present = False
            visual_present = False
            trajectory_follower_present = False
            geometry_present = False
            waypoints: list[list[float]] = []
            try:
                root = ET.fromstring(world_text)
                for model in root.iter("model"):
                    if model.attrib.get("name") != spec["actor_id"]:
                        continue
                    actor_present = True
                    visual_present = model.find(".//visual") is not None
                    geometry_present = model.find(".//visual/geometry") is not None
                    trajectory_follower_present = (
                        model.find(".//plugin[@name='gz::sim::systems::TrajectoryFollower']")
                        is not None
                    )
                    for waypoint in model.findall(".//waypoints/waypoint"):
                        parts = (waypoint.text or "").split()
                        if len(parts) >= 2:
                            waypoints.append([float(parts[0]), float(parts[1])])
            except Exception as exc:
                unsupported_reasons.append("moving_actor_world_sdf_parse_failed")
                observed = {"error": str(exc)[-500:]}
            expected_waypoints = [spec["start_xy_m"], spec["end_xy_m"]]
            trajectory_materialized = (
                actor_present
                and visual_present
                and geometry_present
                and trajectory_follower_present
                and len(waypoints) >= 2
                and all(
                    math.isclose(
                        float(waypoints[index][axis]),
                        float(expected_waypoints[index][axis]),
                        abs_tol=1e-6,
                    )
                    for index in range(2)
                    for axis in range(2)
                )
            )
            if not world_sdf_hash_match:
                unsupported_reasons.append("moving_actor_world_sdf_hash_mismatch")
            if not trajectory_materialized:
                unsupported_reasons.append("moving_actor_trajectory_not_materialized")
            if unsupported_reasons:
                application_status = "unsupported"
                observed = {
                    **observed,
                    "source": "gazebo_world_sdf",
                    "observed": False,
                    "moving_actor_present": actor_present,
                    "moving_actor_trajectory_materialized": trajectory_materialized,
                    "moving_actor_pose_stream_observed": False,
                    "moving_actor_velocity_readback_observed": False,
                    "world_sdf_sha256": world_sha256,
                    "expected_world_sdf_sha256": expected_world_sha256,
                    "world_sdf_hash_match": world_sdf_hash_match,
                    "waypoints_observed_xy_m": waypoints,
                    "telemetry_publisher_state_mutated": False,
                    "mission_upload_path_mutated": False,
                    "mission_progress_mutated": False,
                    "incident_verified": False,
                    "route_blocking_verified": False,
                    "traffic_conflict_verified": False,
                    "collision_obstacle_observed": False,
                    "task_status_mutated": False,
                    "gate_status_mutated": False,
                    "delivery_completion_claimed": False,
                }
            else:
                sample_interval_seconds = 2.0
                try:
                    first = pose_sample()
                    sleep(sample_interval_seconds)
                    second = pose_sample()
                    displacement_xy_m = math.hypot(
                        float(second["x"]) - float(first["x"]),
                        float(second["y"]) - float(first["y"]),
                    )
                    observed_velocity_mps = displacement_xy_m / sample_interval_seconds
                    nominal_velocity_mps = float(spec["nominal_profile_velocity_mps"])
                    motion_observed = displacement_xy_m >= 0.25
                    if not motion_observed:
                        unsupported_reasons.append("moving_actor_pose_stream_not_observed")
                    application_status = (
                        "applied_with_approximations" if motion_observed else "unsupported"
                    )
                    applied = {
                        "method": "gazebo_world_sdf_waypoint_motion_actor",
                        "target": "gazebo_world_sdf",
                        "world_sdf_path": str(world_path),
                        "world_sdf_sha256": world_sha256,
                        "trajectory_definition_sha256": trajectory_sha256,
                        "source": "mission_designer_moving_actor_marker",
                        "actor_id": spec["actor_id"],
                        "frame": spec["frame"],
                        "mode": spec["mode"],
                        "start_xy_m": spec["start_xy_m"],
                        "end_xy_m": spec["end_xy_m"],
                        "nominal_profile_velocity_mps": nominal_velocity_mps,
                        "velocity_target_materialized": False,
                        "sample_interval_seconds": sample_interval_seconds,
                        "applied_at": _observed_at_text(observed_at),
                    }
                    observed = {
                        "source": "gz_topic_pose_info_read_only",
                        "topic": "/world/default/pose/info",
                        "observed": motion_observed,
                        "moving_actor_present": actor_present,
                        "moving_actor_trajectory_materialized": trajectory_materialized,
                        "moving_actor_pose_stream_observed": bool(displacement_xy_m >= 0.25),
                        "moving_actor_velocity_readback_observed": motion_observed,
                        "world_sdf_sha256": world_sha256,
                        "expected_world_sdf_sha256": expected_world_sha256,
                        "world_sdf_hash_match": world_sdf_hash_match,
                        "trajectory_definition_sha256": trajectory_sha256,
                        "waypoints_observed_xy_m": waypoints,
                        "first_pose_xyz_m": [first["x"], first["y"], first["z"]],
                        "second_pose_xyz_m": [second["x"], second["y"], second["z"]],
                        "sample_interval_seconds": sample_interval_seconds,
                        "displacement_xy_m": displacement_xy_m,
                        "nominal_profile_velocity_mps": nominal_velocity_mps,
                        "velocity_target_materialized": False,
                        "observed_velocity_mps": observed_velocity_mps,
                        "velocity_formula": "xy_displacement_m / sample_interval_seconds",
                        "telemetry_publisher_state_mutated": False,
                        "mission_upload_path_mutated": False,
                        "mission_progress_mutated": False,
                        "incident_verified": False,
                        "route_blocking_verified": False,
                        "traffic_conflict_verified": False,
                        "collision_obstacle_observed": False,
                        "task_status_mutated": False,
                        "gate_status_mutated": False,
                        "delivery_completion_claimed": False,
                    }
                except Exception as exc:
                    application_status = "unsupported"
                    unsupported_reasons.append("moving_actor_pose_stream_not_observed")
                    observed = {
                        "source": "gz_topic_pose_info_read_only",
                        "topic": "/world/default/pose/info",
                        "observed": False,
                        "moving_actor_present": actor_present,
                        "moving_actor_trajectory_materialized": trajectory_materialized,
                        "moving_actor_pose_stream_observed": False,
                        "moving_actor_velocity_readback_observed": False,
                        "error": str(exc)[-500:],
                        "world_sdf_sha256": world_sha256,
                        "expected_world_sdf_sha256": expected_world_sha256,
                        "world_sdf_hash_match": world_sdf_hash_match,
                        "trajectory_definition_sha256": trajectory_sha256,
                        "telemetry_publisher_state_mutated": False,
                        "mission_upload_path_mutated": False,
                        "mission_progress_mutated": False,
                        "incident_verified": False,
                        "route_blocking_verified": False,
                        "traffic_conflict_verified": False,
                        "collision_obstacle_observed": False,
                        "task_status_mutated": False,
                        "gate_status_mutated": False,
                        "delivery_completion_claimed": False,
                    }
    return {
        "moving_actor_waypoint_motion_application": {
            "schema_version": "moving_actor_waypoint_motion_application.v1",
            "application_id": (
                "moving_actor_waypoint_motion_application:mission_designer_moving_actor_marker"
            ),
            "condition_kind": "moving_actor_linear_waypoint_motion",
            "application_status": application_status,
            "requested": {
                "requested_present": requested,
                "mode": spec["mode"],
                "actor_id": spec["actor_id"],
                "frame": spec["frame"],
                "start_xy_m": spec["start_xy_m"],
                "end_xy_m": spec["end_xy_m"],
                "nominal_profile_velocity_mps": spec["nominal_profile_velocity_mps"],
            },
            "applied": applied,
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "approximation_reasons": (
                ["moving_actor_velocity_target_not_materialized_as_guaranteed_runtime_speed"]
                if application_status == "applied_with_approximations"
                else []
            ),
            "simulator_only": True,
            "verifier": False,
            "candidate": False,
            "approval_chain": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": _observed_at_text(observed_at),
        }
    }


def observe_moving_actor_pose(
    *,
    requested: bool,
    pose_sample: Callable[[], Mapping[str, float]],
    sleep: Callable[[float], None] = time.sleep,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    requested = bool(requested)
    observed: dict[str, Any] = {}
    observation_status = "not_requested"
    unsupported_reasons: list[str] = []
    if requested:
        try:
            first = pose_sample()
            sleep(2.0)
            second = pose_sample()
            displacement_xy_m = math.hypot(
                float(second["x"]) - float(first["x"]),
                float(second["y"]) - float(first["y"]),
            )
            z_range_m = abs(float(second["z"]) - float(first["z"]))
            marker_altitude_reasonable = max(abs(float(first["z"])), abs(float(second["z"]))) <= 5.0
            pose_motion_observed = displacement_xy_m >= 0.25
            observation_status = (
                "pose_motion_observed"
                if pose_motion_observed and marker_altitude_reasonable
                else (
                    "pose_motion_observed_unbounded_altitude"
                    if pose_motion_observed
                    else "pose_sample_observed_without_motion"
                )
            )
            observed = {
                "source": "gz_topic_pose_info_read_only",
                "topic": "/world/default/pose/info",
                "entity_name": "mission_designer_moving_actor_marker",
                "sample_count": 2,
                "first_pose_xyz_m": [first["x"], first["y"], first["z"]],
                "second_pose_xyz_m": [second["x"], second["y"], second["z"]],
                "displacement_xy_m": displacement_xy_m,
                "z_range_m": z_range_m,
                "marker_altitude_reasonable": marker_altitude_reasonable,
                "pose_motion_observed": pose_motion_observed,
                "read_only_observer": True,
                "collision_observed": False,
                "sensor_evidence_observed": False,
                "incident_observed": False,
                "route_blocking_observed": False,
                "task_status_mutated": False,
                "delivery_completion_claimed": False,
            }
        except Exception as exc:
            observation_status = "pose_not_observed"
            unsupported_reasons.append("moving_actor_pose_not_observed")
            observed = {
                "source": "gz_topic_pose_info_read_only",
                "topic": "/world/default/pose/info",
                "entity_name": "mission_designer_moving_actor_marker",
                "observed": False,
                "error": str(exc)[-500:],
                "read_only_observer": True,
                "collision_observed": False,
                "sensor_evidence_observed": False,
                "incident_observed": False,
                "route_blocking_observed": False,
                "task_status_mutated": False,
                "delivery_completion_claimed": False,
            }
    return {
        "moving_actor_pose_observation": {
            "schema_version": "moving_actor_pose_observation.v1",
            "observation_id": (
                "moving_actor_pose_observation:mission_designer_moving_visual_marker"
            ),
            "condition_kind": "moving_visual_actor_marker",
            "observation_status": observation_status,
            "requested_condition_ref": (
                "operational_condition_profile:mission_designer_operational_markers"
            ),
            "dynamic_actor_ref": ("dynamic_actor_profile:mission_designer_moving_visual_marker"),
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "observer_only": True,
            "simulator_only": True,
            "collision_enabled": False,
            "sensor_visible_claimed": False,
            "incident_claimed": False,
            "route_blocking_enabled": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": _observed_at_text(observed_at),
        }
    }


def project_moving_actor_proximity(
    *,
    requested: bool,
    pose_observation: Mapping[str, Any] | None,
    route_start_xy_m: tuple[float, float],
    route_dropoff_xy_m: tuple[float, float],
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    requested = bool(requested)
    pose_observation = dict(pose_observation or {})
    observed_pose = pose_observation.get("observed") or {}
    pose_samples = [
        observed_pose.get("first_pose_xyz_m"),
        observed_pose.get("second_pose_xyz_m"),
    ]
    actor_xy_samples = [
        [float(sample[0]), float(sample[1])]
        for sample in pose_samples
        if isinstance(sample, list) and len(sample) >= 2
    ]
    advisory_near_route_threshold_m = 2.0
    advisory_near_dropoff_threshold_m = 3.0
    unsupported_reasons: list[str] = []
    if not requested:
        observation_status = "not_requested"
        observed: dict[str, Any] = {}
    elif not actor_xy_samples:
        observation_status = "proximity_not_observed"
        unsupported_reasons.append("moving_actor_pose_samples_missing")
        observed = {
            "source": "moving_actor_pose_observation",
            "observed": False,
            "actor_sample_count": 0,
            "route_blocking_observed": False,
            "incident_observed": False,
            "delivery_completion_claimed": False,
        }
    else:
        route_distances = [
            point_to_segment_distance_m(
                (sample[0], sample[1]),
                route_start_xy_m,
                route_dropoff_xy_m,
            )
            for sample in actor_xy_samples
        ]
        dropoff_distances = [
            math.hypot(
                sample[0] - route_dropoff_xy_m[0],
                sample[1] - route_dropoff_xy_m[1],
            )
            for sample in actor_xy_samples
        ]
        min_distance_to_route_m = min(route_distances)
        min_distance_to_dropoff_m = min(dropoff_distances)
        advisory_status = (
            "near_route_advisory"
            if min_distance_to_route_m <= advisory_near_route_threshold_m
            else (
                "near_dropoff_advisory"
                if min_distance_to_dropoff_m <= advisory_near_dropoff_threshold_m
                else "clear_advisory"
            )
        )
        observation_status = "proximity_observed"
        observed = {
            "source": "moving_actor_pose_observation",
            "observed": True,
            "actor_sample_count": len(actor_xy_samples),
            "actor_xy_samples_m": actor_xy_samples,
            "route_start_xy_m": list(route_start_xy_m),
            "route_dropoff_xy_m": list(route_dropoff_xy_m),
            "min_distance_to_route_m": min_distance_to_route_m,
            "min_distance_to_dropoff_m": min_distance_to_dropoff_m,
            "advisory_status": advisory_status,
            "advisory_near_route_threshold_m": advisory_near_route_threshold_m,
            "advisory_near_dropoff_threshold_m": advisory_near_dropoff_threshold_m,
            "advisory_only": True,
            "route_blocking_observed": False,
            "incident_observed": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    return {
        "moving_actor_proximity_evidence": {
            "schema_version": "moving_actor_proximity_evidence.v1",
            "evidence_id": (
                "moving_actor_proximity_evidence:mission_designer_moving_visual_marker"
            ),
            "condition_kind": "moving_visual_actor_marker",
            "observation_status": observation_status,
            "pose_observation_ref": (
                "moving_actor_pose_observation:mission_designer_moving_visual_marker"
            ),
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "observer_only": True,
            "simulator_only": True,
            "route_blocking_enabled": False,
            "incident_claimed": False,
            "traffic_conflict_verifier": False,
            "advisory_only": True,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": _observed_at_text(observed_at),
        }
    }


__all__ = [
    "observe_moving_actor_pose",
    "project_moving_actor_proximity",
    "run_moving_actor_waypoint_motion_application",
]
