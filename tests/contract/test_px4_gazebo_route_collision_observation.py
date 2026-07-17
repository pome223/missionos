from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import collision_observation, world


FIXED_TIME = datetime(2026, 7, 16, 16, 0, tzinfo=timezone.utc)
CONTACT_TOPIC = "/mission_designer/collision_obstacle/contacts"


def _assert_non_authoritative(artifact: dict[str, Any]) -> None:
    assert artifact.get("physical_execution_invoked") is False
    assert artifact.get("delivery_completion_claimed") is False
    assert artifact.get("task_status_mutated", False) is False
    assert artifact.get("gate_status_mutated", False) is False
    assert artifact.get("auto_gate", False) is False


def _motion() -> dict[str, Any]:
    return {
        "start_xy_m": [1.0, 0.2],
        "end_xy_m": [3.0, 0.2],
    }


def _world_path(tmp_path: Path) -> Path:
    path = tmp_path / "default.sdf"
    path.write_text(
        '<sdf version="1.9"><world name="default">'
        + world.collision_obstacle_world_sdf_patch(
            motion=_motion(),
            contact_topic=CONTACT_TOPIC,
        )
        + "</world></sdf>"
    )
    return path


def _spawn_application(path: Path) -> dict[str, Any]:
    return {
        "application_id": "fixture:collision-spawn",
        "applied": {"world_sdf_path": str(path)},
        "observed": {"world_sdf_hash_match": True},
    }


def _obstacle_profile() -> dict[str, Any]:
    return {
        "obstacles": [
            {
                "collision_enabled": True,
                "start_xy_m": [1.0, 0.2],
                "end_xy_m": [3.0, 0.2],
                "trajectory_follower_plugin_enabled": True,
            }
        ]
    }


def test_legacy_entrypoint_delegates_collision_observation_projection() -> None:
    assert (
        route_entrypoint._run_collision_obstacle_evidence
        is collision_observation.run_collision_obstacle_evidence
    )
    assert (
        route_entrypoint._project_route_blocking_candidate
        is collision_observation.project_route_blocking_candidate
    )


def test_not_requested_invokes_no_pose_or_contact_observer() -> None:
    calls: list[str] = []
    evidence = collision_observation.run_collision_obstacle_evidence(
        requested=False,
        obstacle_profile=None,
        spawn_application={},
        spawn_application_verified=False,
        spawn_source_fail_reasons=[],
        fallback_motion_spec=_motion(),
        route_start_xy_m=(0.0, 0.0),
        route_dropoff_xy_m=(4.0, 0.0),
        pose_sample=lambda: calls.append("pose") or {},
        contact_observation=lambda: calls.append("contact") or {},
        configured_contact_topic=CONTACT_TOPIC,
        sleep=lambda _seconds: calls.append("sleep"),
        observed_at=FIXED_TIME,
    )["collision_obstacle_evidence"]
    candidate = collision_observation.project_route_blocking_candidate(
        requested=False,
        collision_evidence=evidence,
        spawn_application={},
        spawn_application_verified=False,
        spawn_source_fail_reasons=[],
        observed_at=FIXED_TIME,
    )["route_blocking_candidate_evidence"]

    assert calls == []
    assert evidence["observation_status"] == "not_requested"
    assert candidate["observation_status"] == "not_requested"
    _assert_non_authoritative(evidence)
    _assert_non_authoritative(candidate)


def test_unverified_spawn_fails_before_runtime_observation() -> None:
    calls: list[str] = []
    evidence = collision_observation.run_collision_obstacle_evidence(
        requested=True,
        obstacle_profile=_obstacle_profile(),
        spawn_application={"application_id": "fixture:unverified"},
        spawn_application_verified=False,
        spawn_source_fail_reasons=["world_sdf_hash_mismatch"],
        fallback_motion_spec=_motion(),
        route_start_xy_m=(0.0, 0.0),
        route_dropoff_xy_m=(4.0, 0.0),
        pose_sample=lambda: calls.append("pose") or {},
        contact_observation=lambda: calls.append("contact") or {},
        configured_contact_topic=CONTACT_TOPIC,
        sleep=lambda _seconds: calls.append("sleep"),
        observed_at=FIXED_TIME,
    )["collision_obstacle_evidence"]

    assert calls == []
    assert evidence["observation_status"] == "collision_obstacle_not_materialized"
    assert "world_sdf_hash_mismatch" in evidence["unsupported_reasons"]
    assert evidence["observed"]["source_condition_application_verified"] is False
    _assert_non_authoritative(evidence)


def test_source_bound_collision_observation_projects_candidate_only(
    tmp_path: Path,
) -> None:
    path = _world_path(tmp_path)
    pose_samples = iter(
        [
            {"x": 1.0, "y": 0.2, "z": 0.5},
            {"x": 1.4, "y": 0.2, "z": 0.5},
        ]
    )
    evidence = collision_observation.run_collision_obstacle_evidence(
        requested=True,
        obstacle_profile=_obstacle_profile(),
        spawn_application=_spawn_application(path),
        spawn_application_verified=True,
        spawn_source_fail_reasons=[],
        fallback_motion_spec=_motion(),
        route_start_xy_m=(0.0, 0.0),
        route_dropoff_xy_m=(4.0, 0.0),
        pose_sample=lambda: next(pose_samples),
        contact_observation=lambda: {
            "topic": CONTACT_TOPIC,
            "candidate_topics": [CONTACT_TOPIC],
            "contact_topic_observed": True,
            "topic_advertised": True,
            "contact_event_observed": True,
            "source": "fixture-contact-observer",
            "contact_sample_returncode": 0,
        },
        configured_contact_topic=CONTACT_TOPIC,
        sleep=lambda _seconds: None,
        observed_at=FIXED_TIME,
    )["collision_obstacle_evidence"]
    candidate = collision_observation.project_route_blocking_candidate(
        requested=True,
        collision_evidence=evidence,
        spawn_application=_spawn_application(path),
        spawn_application_verified=True,
        spawn_source_fail_reasons=[],
        observed_at=FIXED_TIME,
    )["route_blocking_candidate_evidence"]

    assert evidence["observation_status"] == "collision_obstacle_evidence_observed"
    assert evidence["observed"]["sdf_placement_matches_configured"] is True
    assert evidence["observed"]["contact_event_observed"] is True
    assert evidence["observed"]["min_distance_to_route_m"] == 0.2
    assert evidence["observed"]["route_blocking_observed"] is False
    assert candidate["observation_status"] == "route_blocking_candidate_observed"
    assert candidate["observed"]["route_blocking_candidate"] is True
    assert candidate["observed"]["route_blocking_verified"] is False
    assert candidate["candidate_only"] is True
    assert candidate["route_blocking_verifier"] is False
    _assert_non_authoritative(evidence)
    _assert_non_authoritative(candidate)


def test_sdf_readback_rejects_missing_source() -> None:
    result = collision_observation.collision_obstacle_sdf_placement_readback("")
    assert result == {"observed": False, "error": "world_sdf_path_missing"}
