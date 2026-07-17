"""Collision-obstacle observation for the opt-in PX4/Gazebo route runtime.

This module consumes explicit simulator application facts and read-only pose or
contact observations. It cannot approve, dispatch, mutate a task, activate a
gate, or claim delivery or physical execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import math
from pathlib import Path
import time
from typing import Any
import xml.etree.ElementTree as ET

from src.runtime.px4_gazebo_route.observation import (
    point_to_segment_distance_m,
    xy_pairs_match,
)


def _observed_at_text(value: datetime | None) -> str:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc).isoformat()


def collision_obstacle_sdf_placement_readback(
    world_sdf_path: str,
) -> dict[str, Any]:
    if not world_sdf_path:
        return {"observed": False, "error": "world_sdf_path_missing"}
    path = Path(world_sdf_path)
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"observed": False, "error": str(exc)[-500:]}
    model = None
    for candidate in root.iter("model"):
        if candidate.attrib.get("name") == "mission_designer_collision_obstacle":
            model = candidate
            break
    if model is None:
        return {"observed": False, "error": "collision_obstacle_model_missing"}
    pose_xy = None
    pose_text = (model.findtext("pose") or "").strip()
    if pose_text:
        try:
            parts = [float(part) for part in pose_text.split()]
            if len(parts) >= 2 and math.isfinite(parts[0]) and math.isfinite(parts[1]):
                pose_xy = [parts[0], parts[1]]
        except ValueError:
            pose_xy = None
    waypoints: list[list[float]] = []
    for waypoint in model.iter("waypoint"):
        text = (waypoint.text or "").strip()
        try:
            parts = [float(part) for part in text.split()]
        except ValueError:
            continue
        if len(parts) >= 2 and math.isfinite(parts[0]) and math.isfinite(parts[1]):
            waypoints.append([parts[0], parts[1]])
    return {
        "observed": pose_xy is not None and len(waypoints) >= 2,
        "pose_start_xy_m": pose_xy,
        "waypoint_start_xy_m": waypoints[0] if waypoints else None,
        "waypoint_end_xy_m": waypoints[1] if len(waypoints) >= 2 else None,
        "waypoint_count": len(waypoints),
    }


def run_collision_obstacle_evidence(
    *,
    requested: bool,
    obstacle_profile: Mapping[str, Any] | None,
    spawn_application: Mapping[str, Any],
    spawn_application_verified: bool,
    spawn_source_fail_reasons: Sequence[str],
    fallback_motion_spec: Mapping[str, Any],
    route_start_xy_m: tuple[float, float],
    route_dropoff_xy_m: tuple[float, float],
    pose_sample: Callable[[], Mapping[str, float]],
    contact_observation: Callable[[], Mapping[str, Any]],
    configured_contact_topic: str,
    sleep: Callable[[float], None] = time.sleep,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    requested = bool(requested)
    obstacle_profile = dict(obstacle_profile or {})
    obstacle = (
        (obstacle_profile.get("obstacles") or [{}])[0]
        if isinstance(obstacle_profile.get("obstacles"), list) and obstacle_profile.get("obstacles")
        else {}
    )
    spawn_application = dict(spawn_application)
    spawn_observed = spawn_application.get("observed") or {}
    simulator_condition_applied = bool(spawn_application_verified)
    spawn_source_fail_reasons = list(spawn_source_fail_reasons)
    unsupported_reasons: list[str] = []
    if not requested:
        observation_status = "not_requested"
        observed: dict[str, Any] = {}
    elif not simulator_condition_applied:
        observation_status = "collision_obstacle_not_materialized"
        unsupported_reasons.append("gazebo_route_corridor_obstacle_spawn_not_applied")
        unsupported_reasons.extend(spawn_source_fail_reasons)
        observed = {
            "source": "gazebo_route_corridor_obstacle_spawn_application",
            "spawn_application_ref": spawn_application.get("application_id", ""),
            "source_condition_application_verified": False,
            "world_sdf_hash_match": bool(spawn_observed.get("world_sdf_hash_match")),
            "observed": False,
            "simulator_condition_applied": False,
            "collision_geometry_observed": False,
            "route_blocking_observed": False,
            "incident_observed": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    elif not obstacle.get("collision_enabled"):
        observation_status = "collision_obstacle_not_materialized"
        unsupported_reasons.append("collision_geometry_not_materialized")
        observed = {
            "source": "gazebo_route_corridor_obstacle_spawn_application",
            "spawn_application_ref": spawn_application.get("application_id", ""),
            "source_condition_application_verified": False,
            "world_sdf_hash_match": bool(spawn_observed.get("world_sdf_hash_match")),
            "observed": False,
            "simulator_condition_applied": simulator_condition_applied,
            "collision_geometry_observed": False,
            "route_blocking_observed": False,
            "incident_observed": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    else:
        try:
            first = pose_sample()
            sleep(2.0)
            second = pose_sample()
            xy_samples = [
                [float(first["x"]), float(first["y"])],
                [float(second["x"]), float(second["y"])],
            ]
            fallback_motion = dict(fallback_motion_spec)
            configured_start_xy = obstacle.get("start_xy_m") or fallback_motion["start_xy_m"]
            configured_end_xy = obstacle.get("end_xy_m") or fallback_motion["end_xy_m"]
            configured_xy_samples = [
                [
                    float(configured_start_xy[0]),
                    float(configured_start_xy[1]),
                ],
                [
                    float(configured_end_xy[0]),
                    float(configured_end_xy[1]),
                ],
            ]
            sdf_readback = collision_obstacle_sdf_placement_readback(
                str((spawn_application.get("applied") or {}).get("world_sdf_path", ""))
            )
            sdf_placement_matches_configured = (
                sdf_readback.get("observed") is True
                and xy_pairs_match(
                    sdf_readback.get("pose_start_xy_m"),
                    configured_xy_samples[0],
                )
                and xy_pairs_match(
                    sdf_readback.get("waypoint_start_xy_m"),
                    configured_xy_samples[0],
                )
                and xy_pairs_match(
                    sdf_readback.get("waypoint_end_xy_m"),
                    configured_xy_samples[1],
                )
            )
            if not sdf_placement_matches_configured:
                raise RuntimeError("collision_obstacle_sdf_placement_not_source_bound")
            runtime_route_distances = [
                point_to_segment_distance_m(
                    (sample[0], sample[1]),
                    route_start_xy_m,
                    route_dropoff_xy_m,
                )
                for sample in xy_samples
            ]
            configured_route_distances = [
                point_to_segment_distance_m(
                    (sample[0], sample[1]),
                    route_start_xy_m,
                    route_dropoff_xy_m,
                )
                for sample in configured_xy_samples
            ]
            dropoff_distances = [
                math.hypot(
                    sample[0] - route_dropoff_xy_m[0],
                    sample[1] - route_dropoff_xy_m[1],
                )
                for sample in xy_samples
            ]
            displacement_xy_m = math.hypot(
                float(second["x"]) - float(first["x"]),
                float(second["y"]) - float(first["y"]),
            )
            contact_observation = dict(contact_observation())
            observation_status = "collision_obstacle_evidence_observed"
            observed = {
                "source": (
                    "gazebo_route_corridor_obstacle_spawn_application_and_gz_topic_pose_info"
                ),
                "spawn_application_ref": spawn_application.get("application_id", ""),
                "source_condition_application_verified": True,
                "world_sdf_hash_match": True,
                "simulator_condition_applied": simulator_condition_applied,
                "topic": "/world/default/pose/info",
                "entity_name": "mission_designer_collision_obstacle",
                "sample_count": 2,
                "first_pose_xyz_m": [first["x"], first["y"], first["z"]],
                "second_pose_xyz_m": [second["x"], second["y"], second["z"]],
                "displacement_xy_m": displacement_xy_m,
                "collision_geometry_observed": True,
                "trajectory_follower_observed": bool(
                    obstacle.get("trajectory_follower_plugin_enabled")
                ),
                "pose_observed": True,
                "actor_xy_samples_m": xy_samples,
                "configured_xy_samples_m": configured_xy_samples,
                "sdf_placement_readback_observed": bool(sdf_readback.get("observed")),
                "sdf_pose_start_xy_m": sdf_readback.get("pose_start_xy_m"),
                "sdf_waypoint_start_xy_m": sdf_readback.get("waypoint_start_xy_m"),
                "sdf_waypoint_end_xy_m": sdf_readback.get("waypoint_end_xy_m"),
                "sdf_waypoint_count": sdf_readback.get("waypoint_count"),
                "sdf_placement_matches_configured": sdf_placement_matches_configured,
                "route_start_xy_m": list(route_start_xy_m),
                "route_dropoff_xy_m": list(route_dropoff_xy_m),
                "runtime_min_distance_to_route_m": min(runtime_route_distances),
                "configured_min_distance_to_route_m": min(configured_route_distances),
                "min_distance_to_route_m": min(
                    runtime_route_distances + configured_route_distances
                ),
                "min_distance_to_dropoff_m": min(dropoff_distances),
                "contact_topic": configured_contact_topic,
                "contact_topic_runtime": contact_observation.get("topic"),
                "contact_topic_candidates": contact_observation.get("candidate_topics", []),
                "contact_topic_observed": bool(contact_observation.get("contact_topic_observed")),
                "contact_topic_advertised": bool(contact_observation.get("topic_advertised")),
                "contact_event_observed": bool(contact_observation.get("contact_event_observed")),
                "contact_observation_source": contact_observation.get("source"),
                "contact_sample_returncode": contact_observation.get("contact_sample_returncode"),
                "route_blocking_candidate": False,
                "route_blocking_observed": False,
                "incident_observed": False,
                "traffic_conflict_verified": False,
                "task_status_mutated": False,
                "delivery_completion_claimed": False,
            }
        except Exception as exc:
            observation_status = "collision_obstacle_pose_not_observed"
            unsupported_reasons.append("collision_obstacle_pose_not_observed")
            observed = {
                "source": (
                    "gazebo_route_corridor_obstacle_spawn_application_and_gz_topic_pose_info"
                ),
                "spawn_application_ref": spawn_application.get("application_id", ""),
                "source_condition_application_verified": True,
                "world_sdf_hash_match": True,
                "simulator_condition_applied": simulator_condition_applied,
                "topic": "/world/default/pose/info",
                "entity_name": "mission_designer_collision_obstacle",
                "observed": False,
                "error": str(exc)[-500:],
                "collision_geometry_observed": True,
                "route_blocking_observed": False,
                "incident_observed": False,
                "traffic_conflict_verified": False,
                "task_status_mutated": False,
                "delivery_completion_claimed": False,
            }
    return {
        "collision_obstacle_evidence": {
            "schema_version": "collision_obstacle_evidence.v1",
            "evidence_id": (
                "collision_obstacle_evidence:mission_designer_collision_enabled_obstacle"
            ),
            "condition_kind": "collision_enabled_moving_obstacle",
            "observation_status": observation_status,
            "collision_obstacle_ref": (
                "collision_obstacle_profile:mission_designer_collision_obstacle"
            ),
            "gazebo_route_corridor_obstacle_spawn_application_ref": (
                spawn_application.get("application_id", "")
            ),
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "simulator_only": True,
            "collision_enabled": requested,
            "sensor_visible_claimed": False,
            "route_blocking_enabled": False,
            "incident_claimed": False,
            "traffic_conflict_verifier": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": _observed_at_text(observed_at),
        }
    }


def project_route_blocking_candidate(
    *,
    requested: bool,
    collision_evidence: Mapping[str, Any] | None,
    spawn_application: Mapping[str, Any],
    spawn_application_verified: bool,
    spawn_source_fail_reasons: Sequence[str],
    candidate_threshold_m: float = 1.25,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    collision_evidence = dict(collision_evidence or {})
    collision_observed = collision_evidence.get("observed") or {}
    spawn_application = dict(spawn_application)
    spawn_source_fail_reasons = list(spawn_source_fail_reasons)
    requested = bool(requested)
    unsupported_reasons: list[str] = []
    if not requested:
        observation_status = "not_requested"
        observed: dict[str, Any] = {}
    elif collision_evidence.get("observation_status") != "collision_obstacle_evidence_observed":
        observation_status = "route_blocking_candidate_not_observed"
        unsupported_reasons.append("collision_obstacle_evidence_missing")
        observed = {
            "source": "collision_obstacle_evidence",
            "observed": False,
            "route_blocking_candidate": False,
            "route_blocking_verified": False,
            "incident_report_created": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    elif not spawn_application_verified:
        observation_status = "route_blocking_candidate_not_observed"
        unsupported_reasons.extend(spawn_source_fail_reasons)
        observed = {
            "source": "gazebo_route_corridor_obstacle_spawn_application",
            "observed": False,
            "source_condition_application_ref": spawn_application.get("application_id", ""),
            "source_condition_application_verified": False,
            "world_sdf_hash_match": bool(
                (spawn_application.get("observed") or {}).get("world_sdf_hash_match")
            ),
            "simulator_condition_applied": False,
            "route_blocking_candidate": False,
            "route_blocking_verified": False,
            "incident_report_created": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    else:
        min_distance = collision_observed.get("min_distance_to_route_m")
        route_blocking_candidate = (
            isinstance(min_distance, (int, float)) and float(min_distance) <= candidate_threshold_m
        )
        observation_status = (
            "route_blocking_candidate_observed"
            if route_blocking_candidate
            else "route_clear_candidate_observed"
        )
        observed = {
            "source": "collision_obstacle_evidence",
            "gazebo_route_corridor_obstacle_spawn_application_ref": (
                collision_evidence.get("gazebo_route_corridor_obstacle_spawn_application_ref", "")
            ),
            "source_condition_application_ref": spawn_application.get("application_id", ""),
            "source_condition_application_verified": True,
            "world_sdf_hash_match": bool(
                (spawn_application.get("observed") or {}).get("world_sdf_hash_match")
            ),
            "simulator_condition_applied": bool(
                collision_observed.get("simulator_condition_applied")
            ),
            "observed": True,
            "candidate_threshold_m": candidate_threshold_m,
            "min_distance_to_route_m": min_distance,
            "min_distance_to_dropoff_m": collision_observed.get("min_distance_to_dropoff_m"),
            "collision_geometry_observed": bool(
                collision_observed.get("collision_geometry_observed")
            ),
            "contact_topic_observed": bool(collision_observed.get("contact_topic_observed")),
            "route_blocking_candidate": route_blocking_candidate,
            "route_blocking_verified": False,
            "operator_review_required": route_blocking_candidate,
            "incident_report_created": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    return {
        "route_blocking_candidate_evidence": {
            "schema_version": "route_blocking_candidate_evidence.v1",
            "evidence_id": (
                "route_blocking_candidate_evidence:mission_designer_collision_obstacle"
            ),
            "condition_kind": "route_blocking_candidate_from_collision_obstacle",
            "observation_status": observation_status,
            "collision_obstacle_evidence_ref": (
                "collision_obstacle_evidence:mission_designer_collision_enabled_obstacle"
            ),
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "candidate_only": True,
            "route_blocking_verifier": False,
            "incident_report_created": False,
            "traffic_conflict_verifier": False,
            "task_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": _observed_at_text(observed_at),
        }
    }


__all__ = [
    "collision_obstacle_sdf_placement_readback",
    "project_route_blocking_candidate",
    "run_collision_obstacle_evidence",
]
