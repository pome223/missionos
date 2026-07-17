from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import dynamic_observation, world


FIXED_TIME = datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc)


def _assert_non_authoritative(artifact: dict[str, Any]) -> None:
    assert artifact.get("physical_execution_invoked") is False
    assert artifact.get("delivery_completion_claimed") is False
    assert artifact.get("task_status_mutated", False) is False
    assert artifact.get("gate_status_mutated", False) is False
    assert artifact.get("auto_gate", False) is False


def test_legacy_entrypoint_delegates_dynamic_actor_projection() -> None:
    assert (
        route_entrypoint._run_moving_actor_waypoint_motion_application
        is dynamic_observation.run_moving_actor_waypoint_motion_application
    )
    assert (
        route_entrypoint._observe_moving_actor_pose is dynamic_observation.observe_moving_actor_pose
    )
    assert (
        route_entrypoint._project_moving_actor_proximity
        is dynamic_observation.project_moving_actor_proximity
    )


def test_not_requested_invokes_no_pose_observer() -> None:
    calls: list[str] = []
    spec = world.moving_actor_waypoint_motion_spec()

    application = dynamic_observation.run_moving_actor_waypoint_motion_application(
        requested=False,
        motion_spec=spec,
        trajectory_definition_sha256=(world.moving_actor_waypoint_trajectory_definition_sha256()),
        operational_realism_summary=None,
        pose_sample=lambda: calls.append("application") or {},
        sleep=lambda _seconds: calls.append("sleep"),
        observed_at=FIXED_TIME,
    )["moving_actor_waypoint_motion_application"]
    observation = dynamic_observation.observe_moving_actor_pose(
        requested=False,
        pose_sample=lambda: calls.append("observation") or {},
        sleep=lambda _seconds: calls.append("sleep"),
        observed_at=FIXED_TIME,
    )["moving_actor_pose_observation"]
    proximity = dynamic_observation.project_moving_actor_proximity(
        requested=False,
        pose_observation=observation,
        route_start_xy_m=(0.0, 0.0),
        route_dropoff_xy_m=(4.0, 0.0),
        observed_at=FIXED_TIME,
    )["moving_actor_proximity_evidence"]

    assert calls == []
    assert application["application_status"] == "not_requested"
    assert observation["observation_status"] == "not_requested"
    assert proximity["observation_status"] == "not_requested"
    _assert_non_authoritative(application)
    _assert_non_authoritative(observation)
    _assert_non_authoritative(proximity)


def test_world_bound_motion_observation_and_proximity_remain_advisory(
    tmp_path: Path,
) -> None:
    world_path = tmp_path / "default.sdf"
    world_text = (
        '<sdf version="1.9"><world name="default">'
        + world.moving_actor_world_sdf_patch()
        + "</world></sdf>"
    )
    world_path.write_text(world_text)
    world_sha256 = hashlib.sha256(world_text.encode()).hexdigest()
    samples = iter(
        [
            {"x": 1.2, "y": -0.7, "z": 0.25},
            {"x": 1.8, "y": -0.7, "z": 0.25},
        ]
    )
    application_result = dynamic_observation.run_moving_actor_waypoint_motion_application(
        requested=True,
        motion_spec=world.moving_actor_waypoint_motion_spec(),
        trajectory_definition_sha256=(world.moving_actor_waypoint_trajectory_definition_sha256()),
        operational_realism_summary={
            "operational_application": {
                "application_status": "applied_with_approximations",
                "applied": {
                    "world_sdf_path": str(world_path),
                    "world_sdf_sha256": world_sha256,
                },
            }
        },
        pose_sample=lambda: next(samples),
        sleep=lambda _seconds: None,
        observed_at=FIXED_TIME,
    )
    application = application_result["moving_actor_waypoint_motion_application"]
    assert application["application_status"] == "applied_with_approximations"
    assert application["observed"]["moving_actor_pose_stream_observed"] is True
    assert application["observed"]["observed_velocity_mps"] == pytest.approx(0.3)

    observation_samples = iter(
        [
            {"x": 1.2, "y": 0.4, "z": 0.25},
            {"x": 1.8, "y": 0.4, "z": 0.25},
        ]
    )
    observation = dynamic_observation.observe_moving_actor_pose(
        requested=True,
        pose_sample=lambda: next(observation_samples),
        sleep=lambda _seconds: None,
        observed_at=FIXED_TIME,
    )["moving_actor_pose_observation"]
    proximity = dynamic_observation.project_moving_actor_proximity(
        requested=True,
        pose_observation=observation,
        route_start_xy_m=(0.0, 0.0),
        route_dropoff_xy_m=(4.0, 0.0),
        observed_at=FIXED_TIME,
    )["moving_actor_proximity_evidence"]

    assert observation["observation_status"] == "pose_motion_observed"
    assert proximity["observation_status"] == "proximity_observed"
    assert proximity["observed"]["advisory_status"] == "near_route_advisory"
    assert proximity["observed"]["advisory_only"] is True
    assert proximity["observed"]["route_blocking_observed"] is False
    _assert_non_authoritative(application)
    _assert_non_authoritative(observation)
    _assert_non_authoritative(proximity)


def test_world_hash_mismatch_fails_before_pose_observation(tmp_path: Path) -> None:
    world_path = tmp_path / "default.sdf"
    world_path.write_text(
        '<sdf version="1.9"><world name="default">'
        + world.moving_actor_world_sdf_patch()
        + "</world></sdf>"
    )
    calls: list[str] = []

    application = dynamic_observation.run_moving_actor_waypoint_motion_application(
        requested=True,
        motion_spec=world.moving_actor_waypoint_motion_spec(),
        trajectory_definition_sha256=(world.moving_actor_waypoint_trajectory_definition_sha256()),
        operational_realism_summary={
            "operational_application": {
                "application_status": "applied_with_approximations",
                "applied": {
                    "world_sdf_path": str(world_path),
                    "world_sdf_sha256": "not-the-observed-hash",
                },
            }
        },
        pose_sample=lambda: calls.append("pose") or {},
        sleep=lambda _seconds: calls.append("sleep"),
        observed_at=FIXED_TIME,
    )["moving_actor_waypoint_motion_application"]

    assert calls == []
    assert application["application_status"] == "unsupported"
    assert "moving_actor_world_sdf_hash_mismatch" in application["unsupported_reasons"]
    assert application["observed"]["world_sdf_hash_match"] is False
    _assert_non_authoritative(application)
