from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from src.runtime.nav2_core_action_feasibility_adapter import (
    NAV2_CORE_ADAPTER_ID,
    evaluate_nav2_recovery_candidates_through_core,
    nav2_recovery_policy,
    verify_nav2_core_action_candidate,
)


pytestmark = pytest.mark.contract
EVALUATED_AT = "2026-07-24T03:00:00+00:00"


def _path_sha256(points: list[dict]) -> str:
    material = [
        [round(float(point["x_m"]), 6), round(float(point["y_m"]), 6)]
        for point in points
    ]
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":")).encode()
    ).hexdigest()


def _obstacle() -> dict:
    return {
        "runtime_obstacle_x_m": 0.0,
        "runtime_obstacle_y_m": 0.0,
        "runtime_obstacle_z_m": 0.25,
        "runtime_obstacle_collision_size_x_m": 0.4,
        "runtime_obstacle_collision_size_y_m": 0.4,
        "runtime_obstacle_size_z_m": 0.5,
        "runtime_obstacle_frame_id": "map",
        "runtime_obstacle_scene_ref": "fixture_box",
        "runtime_obstacle_geometry_source": "fixture_sdf_collision",
        "runtime_obstacle_source": "fixture_costmap",
    }


def _robot_envelope() -> dict:
    return {
        "radius_m": 0.19,
        "z_min_m": -0.01,
        "z_max_m": 0.14,
        "frame_id": "base_footprint",
        "geometry_source": "fixture_robot_collision_envelope",
    }


def _candidate(
    *,
    points: list[dict] | None = None,
    frame_id: str = "map",
) -> dict:
    points = points or [
        {"x_m": -1.0, "y_m": 1.0, "frame_id": frame_id},
        {"x_m": 1.0, "y_m": 1.0, "frame_id": frame_id},
    ]
    return {
        "candidate_id": "bypass_north",
        "action": "avoid_obstacle",
        "x_m": 1.0,
        "y_m": 1.0,
        "yaw_rad": 0.0,
        "max_speed_mps": 0.2,
        "path_valid": True,
        "planner_status": "succeeded",
        "path_frame_id": frame_id,
        "path_points": points,
        "path_length_m": 2.0,
        "path_goal_error_m": 0.0,
        "path_goal_tolerance_m": 0.1,
        "path_sha256": _path_sha256(points),
        "target_cost": 0,
        "maximum_path_cost": 20,
        "local_current_cost": 0,
        "local_target_cost": 0,
        "local_maximum_path_cost": 40,
    }


def _evaluation(
    candidate: dict | None = None,
    *,
    stamp_ns: int = 2_000_000_000,
    age_s: float | None = 0.1,
) -> dict:
    candidate = candidate or _candidate()
    return {
        "schema_version": "missionos_nav2_recovery_candidate_evaluation.v1",
        "evaluation_status": "validated",
        "selected_candidate": candidate,
        "candidate_evaluations": [candidate],
        "global_costmap_snapshot_hash": "a" * 64,
        "global_costmap_frame_id": "map",
        "global_costmap_stamp_ns": stamp_ns,
        "global_costmap_age_s": age_s,
        "local_costmap_snapshot_hash": "b" * 64,
        "local_costmap_frame_id": "odom",
        "local_costmap_stamp_ns": stamp_ns,
        "local_costmap_age_s": age_s,
        "local_frame_transform_verified": True,
        "dispatch_request_sent": False,
        "dispatch_authority_created": False,
        "command_ack_observed": False,
        "observation_captured_at": EVALUATED_AT,
    }


def _verify(
    *,
    evaluation: dict | None = None,
    candidate: dict | None = None,
    obstacle: dict | None = None,
    policy: dict | None = None,
    previous_hazard_state: dict | None = None,
) -> dict:
    candidate = candidate or _candidate()
    evaluation = evaluation or _evaluation(candidate)
    return verify_nav2_core_action_candidate(
        evaluation=evaluation,
        candidate_evaluation=candidate,
        obstacle=obstacle or _obstacle(),
        robot_collision_envelope=_robot_envelope(),
        active_policy=policy or nav2_recovery_policy(),
        evaluated_at=EVALUATED_AT,
        previous_hazard_state=previous_hazard_state,
    )


def _result(artifact: dict) -> dict:
    return artifact["action_feasibility"]


def test_verified_nav2_candidate_uses_core_without_creating_authority() -> None:
    artifact = _verify()
    result = _result(artifact)

    assert artifact["adapter_id"] == NAV2_CORE_ADAPTER_ID
    assert result["status"] == "verified_feasible"
    assert result["blocked_reasons"] == ()
    assert result["unverified_reasons"] == ()
    assert artifact["llm_invoked"] is False
    assert artifact["approval_created"] is False
    assert artifact["dispatch_authority_created"] is False
    assert artifact["dispatch_request_sent"] is False
    assert artifact["physical_execution_invoked"] is False
    assert artifact["completion_claimed"] is False


@pytest.mark.parametrize(
    ("mutation", "required_reason"),
    [
        (
            lambda obstacle, evaluation, candidate: obstacle.pop(
                "runtime_obstacle_size_z_m"
            ),
            "nav2_obstacle_geometry_unverified",
        ),
        (
            lambda obstacle, evaluation, candidate: candidate.update(
                {
                    "path_frame_id": "odom",
                    "path_points": [
                        {**point, "frame_id": "odom"}
                        for point in candidate["path_points"]
                    ],
                }
            ),
            "nav2_path_obstacle_frame_mismatch",
        ),
        (
            lambda obstacle, evaluation, candidate: evaluation.update(
                {
                    "global_costmap_age_s": 4.0,
                    "local_costmap_age_s": 4.0,
                }
            ),
            "nav2_dynamic_observation_stale",
        ),
    ],
)
def test_missing_geometry_frame_mismatch_and_stale_observation_fail_closed(
    mutation,
    required_reason: str,
) -> None:
    obstacle = _obstacle()
    candidate = _candidate()
    evaluation = _evaluation(candidate)
    mutation(obstacle, evaluation, candidate)
    if candidate.get("path_points"):
        candidate["path_sha256"] = _path_sha256(candidate["path_points"])

    result = _result(
        _verify(
            evaluation=evaluation,
            candidate=candidate,
            obstacle=obstacle,
        )
    )

    assert result["status"] == "unverified"
    assert required_reason in result["unverified_reasons"]


def test_collision_path_is_deterministically_blocked() -> None:
    points = [
        {"x_m": -1.0, "y_m": 0.0, "frame_id": "map"},
        {"x_m": 1.0, "y_m": 0.0, "frame_id": "map"},
    ]
    candidate = _candidate(points=points)
    evaluation = _evaluation(candidate)

    result = _result(_verify(evaluation=evaluation, candidate=candidate))

    assert result["status"] == "blocked"
    assert "nav2_candidate_collision_envelope_intersection" in result[
        "blocked_reasons"
    ]


def test_path_endpoint_must_match_the_candidate_target() -> None:
    candidate = _candidate()
    candidate["x_m"] = 2.0

    result = _result(
        _verify(
            evaluation=_evaluation(candidate),
            candidate=candidate,
        )
    )

    assert result["status"] == "blocked"
    assert "nav2_candidate_goal_error_mismatch" in result["blocked_reasons"]


def test_avoidance_candidate_cannot_verify_without_motion() -> None:
    points = [
        {"x_m": -1.0, "y_m": 1.0, "frame_id": "map"},
        {"x_m": -1.0, "y_m": 1.0, "frame_id": "map"},
    ]
    candidate = _candidate(points=points)
    candidate.update(
        {
            "x_m": -1.0,
            "y_m": 1.0,
            "path_length_m": 0.0,
        }
    )

    result = _result(
        _verify(
            evaluation=_evaluation(candidate),
            candidate=candidate,
        )
    )

    assert result["status"] == "blocked"
    assert "nav2_candidate_no_motion" in result["blocked_reasons"]


def test_hold_candidate_requires_fresh_observation_but_no_motion_path() -> None:
    hold = {
        "candidate_id": "hold-current-pose",
        "action": "hold",
        "path_valid": False,
    }
    fresh = _result(
        _verify(evaluation=_evaluation(hold), candidate=hold)
    )
    stale = _result(
        _verify(
            evaluation=_evaluation(hold, age_s=4.0),
            candidate=hold,
        )
    )

    assert fresh["status"] == "verified_feasible"
    assert fresh["extension_verdicts"][0]["measurements"][
        "motion_command_required"
    ] is False
    assert stale["status"] == "unverified"
    assert "nav2_dynamic_observation_stale" in stale[
        "unverified_reasons"
    ]


def test_dispatch_revalidation_blocks_cursor_regression_and_policy_drift() -> None:
    initial = _verify(evaluation=_evaluation(stamp_ns=3_000_000_000))
    previous = initial["hazard_state"]

    regression = _verify(
        evaluation=_evaluation(stamp_ns=2_000_000_000),
        previous_hazard_state=previous,
    )
    changed_policy = nav2_recovery_policy(
        {"minimum_surface_clearance_m": 0.2}
    )
    drift = _verify(
        evaluation=_evaluation(stamp_ns=4_000_000_000),
        policy=changed_policy,
        previous_hazard_state=previous,
    )

    assert _result(regression)["status"] == "blocked"
    assert "cursor_regression" in _result(regression)["blocked_reasons"]
    assert _result(drift)["status"] == "blocked"
    assert "nav2_policy_binding_drift" in _result(drift)["blocked_reasons"]


def test_cursor_is_incomparable_when_same_stamp_content_changes() -> None:
    initial = _verify(evaluation=_evaluation(stamp_ns=3_000_000_000))
    current_evaluation = _evaluation(stamp_ns=3_000_000_000)
    current_evaluation["global_costmap_snapshot_hash"] = "c" * 64
    current_evaluation["local_costmap_stamp_ns"] = 4_000_000_000

    result = _result(
        _verify(
            evaluation=current_evaluation,
            previous_hazard_state=initial["hazard_state"],
        )
    )

    assert result["status"] == "unverified"
    assert "cursor_incomparable" in result["unverified_reasons"]


def test_blocked_or_unverified_candidate_cannot_remain_selected() -> None:
    collision_points = [
        {"x_m": -1.0, "y_m": 0.0, "frame_id": "map"},
        {"x_m": 1.0, "y_m": 0.0, "frame_id": "map"},
    ]
    blocked = _candidate(points=collision_points)
    unverified = deepcopy(_candidate())
    unverified["candidate_id"] = "missing_geometry"
    unverified.pop("path_points")
    unverified.pop("path_sha256")
    evaluation = _evaluation(blocked)
    evaluation["candidate_evaluations"] = [blocked, unverified]

    verdict = evaluate_nav2_recovery_candidates_through_core(
        evaluation=evaluation,
        obstacle=_obstacle(),
        robot_collision_envelope=_robot_envelope(),
        active_policy=nav2_recovery_policy(),
        evaluated_at=EVALUATED_AT,
    )

    statuses = {
        item["candidate_id"]: item["core_action_feasibility_status"]
        for item in verdict["candidate_evaluations"]
    }
    assert statuses == {
        "bypass_north": "blocked",
        "missing_geometry": "unverified",
    }
    assert verdict["evaluation_status"] == "blocked"
    assert verdict["selected_candidate"] is None
    assert verdict["dispatch_authority_created"] is False
    assert verdict["dispatch_request_sent"] is False
