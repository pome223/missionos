#!/usr/bin/env python3
"""Regenerate the sealed, publication-safe Nav2 feasibility corpus."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.runtime.nav2_action_feasibility_corpus import (
    NAV2_CORPUS_CASE_SCHEMA,
    NAV2_CORPUS_SCHEMA,
    seal_nav2_corpus_case,
)
from src.runtime.nav2_core_action_feasibility_adapter import (
    nav2_recovery_policy,
)


ROOT = (
    Path(__file__).parents[1]
    / "tests"
    / "golden"
    / "action_feasibility"
    / "nav2_v1"
)
CASES = ROOT / "cases"


def _sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _path_sha256(points: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(
            [
                [round(float(point["x_m"]), 6), round(float(point["y_m"]), 6)]
                for point in points
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _candidate(*, frame_id: str = "map") -> dict[str, Any]:
    points = [
        {"x_m": -1.0, "y_m": 1.0, "frame_id": frame_id},
        {"x_m": 1.0, "y_m": 1.0, "frame_id": frame_id},
    ]
    return {
        "candidate_id": "nav2-verified-bypass",
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


def _evaluation(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "missionos_nav2_recovery_candidate_evaluation.v1",
        "evaluation_status": "validated",
        "selected_candidate": candidate,
        "candidate_evaluations": [candidate],
        "global_costmap_snapshot_hash": "a" * 64,
        "global_costmap_frame_id": "map",
        "global_costmap_stamp_ns": 2_000_000_000,
        "global_costmap_age_s": 0.1,
        "local_costmap_snapshot_hash": "b" * 64,
        "local_costmap_frame_id": "odom",
        "local_costmap_stamp_ns": 2_000_000_000,
        "local_costmap_age_s": 0.1,
        "local_frame_transform_verified": True,
        "dispatch_request_sent": False,
        "dispatch_authority_created": False,
        "command_ack_observed": False,
        "observation_captured_at": "2026-07-24T03:00:00+00:00",
    }


def _obstacle() -> dict[str, Any]:
    return {
        "runtime_obstacle_x_m": 0.0,
        "runtime_obstacle_y_m": 0.0,
        "runtime_obstacle_z_m": 0.25,
        "runtime_obstacle_collision_size_x_m": 0.4,
        "runtime_obstacle_collision_size_y_m": 0.4,
        "runtime_obstacle_size_z_m": 0.5,
        "runtime_obstacle_frame_id": "map",
        "runtime_obstacle_scene_ref": "sealed_fixture_box",
        "runtime_obstacle_geometry_source": "sealed_fixture_sdf_collision",
        "runtime_obstacle_source": "sealed_fixture_costmap",
    }


def _envelope() -> dict[str, Any]:
    return {
        "radius_m": 0.19,
        "z_min_m": -0.01,
        "z_max_m": 0.14,
        "frame_id": "base_footprint",
        "geometry_source": "sealed_fixture_robot_collision_envelope",
    }


def _authority_chain(positive: bool) -> dict[str, Any]:
    if positive:
        return {
            "proposal": {
                "artifact_ref": "proposal:nav2-positive",
                "status": "created",
                "approval_created": False,
                "dispatch_authority_created": False,
            },
            "human_approval": {
                "artifact_ref": "approval:nav2-positive",
                "status": "approved",
                "human_approval_performed": True,
            },
            "dispatch_revalidation": {
                "artifact_ref": "revalidation:nav2-positive",
                "status": "valid",
            },
            "dispatch_authority": {
                "artifact_ref": "authority:nav2-positive",
                "created": True,
            },
            "runner_ack": {
                "artifact_ref": "ack:nav2-positive",
                "observed": True,
                "ack_is_execution_effect": False,
            },
            "observed_effect": {
                "artifact_ref": "effect:nav2-positive",
                "target_reached": True,
                "resume_status": "resumed_route",
            },
            "completion": {
                "artifact_ref": "completion:nav2-positive",
                "sim_route_terminal_observed": True,
                "mission_completion_claimed": True,
                "delivery_completion_claimed": False,
                "physical_execution_invoked": False,
            },
        }
    return {
        "proposal": {
            "artifact_ref": "proposal:nav2-refusal",
            "status": "rejected_before_proposal",
            "approval_created": False,
            "dispatch_authority_created": False,
        },
        "human_approval": {
            "artifact_ref": "approval:nav2-refusal",
            "status": "not_created",
            "human_approval_performed": False,
        },
        "dispatch_revalidation": {
            "artifact_ref": "revalidation:nav2-refusal",
            "status": "blocked",
        },
        "dispatch_authority": {
            "artifact_ref": "authority:nav2-refusal",
            "created": False,
        },
        "runner_ack": {
            "artifact_ref": "ack:nav2-refusal",
            "observed": False,
            "ack_is_execution_effect": False,
        },
        "observed_effect": {
            "artifact_ref": "effect:nav2-refusal",
            "target_reached": False,
            "resume_status": "not_attempted",
        },
        "completion": {
            "artifact_ref": "completion:nav2-refusal",
            "sim_route_terminal_observed": False,
            "mission_completion_claimed": False,
            "delivery_completion_claimed": False,
            "physical_execution_invoked": False,
        },
    }


def _case(
    *,
    case_id: str,
    scenario_class: str,
    candidate: dict[str, Any],
    evaluation: dict[str, Any],
    obstacle: dict[str, Any],
    expected_status: str,
    required_reason: str = "",
) -> dict[str, Any]:
    return seal_nav2_corpus_case(
        {
            "schema_version": NAV2_CORPUS_CASE_SCHEMA,
            "case_id": case_id,
            "scenario_class": scenario_class,
            "truth_boundary": {
                "case_is_replay_fixture": True,
                "runtime_invoked_by_this_replay": False,
                "source_contract_evidence_available": True,
            },
            "evaluated_at": "2026-07-24T03:00:00+00:00",
            "evaluation": evaluation,
            "candidate": candidate,
            "obstacle": obstacle,
            "robot_collision_envelope": _envelope(),
            "active_policy": nav2_recovery_policy(),
            "expected": {
                "status": expected_status,
                "required_reason": required_reason,
            },
            "authority_chain": _authority_chain(
                scenario_class == "positive"
            ),
        }
    )


def main() -> None:
    positive_candidate = _candidate()
    positive = _case(
        case_id="nav2-positive-verified-bypass",
        scenario_class="positive",
        candidate=positive_candidate,
        evaluation=_evaluation(positive_candidate),
        obstacle=_obstacle(),
        expected_status="verified_feasible",
    )

    missing_geometry_obstacle = _obstacle()
    missing_geometry_obstacle.pop("runtime_obstacle_size_z_m")
    missing_candidate = _candidate()
    missing_geometry = _case(
        case_id="nav2-refusal-missing-obstacle-geometry",
        scenario_class="refusal",
        candidate=missing_candidate,
        evaluation=_evaluation(missing_candidate),
        obstacle=missing_geometry_obstacle,
        expected_status="unverified",
        required_reason="nav2_obstacle_geometry_unverified",
    )

    frame_candidate = _candidate(frame_id="odom")
    frame_mismatch = _case(
        case_id="nav2-refusal-frame-mismatch",
        scenario_class="refusal",
        candidate=frame_candidate,
        evaluation=_evaluation(frame_candidate),
        obstacle=_obstacle(),
        expected_status="unverified",
        required_reason="nav2_path_obstacle_frame_mismatch",
    )

    stale_candidate = _candidate()
    stale_evaluation = _evaluation(stale_candidate)
    stale_evaluation["global_costmap_age_s"] = 4.0
    stale_evaluation["local_costmap_age_s"] = 4.0
    stale = _case(
        case_id="nav2-refusal-stale-dynamic-observation",
        scenario_class="refusal",
        candidate=stale_candidate,
        evaluation=stale_evaluation,
        obstacle=_obstacle(),
        expected_status="unverified",
        required_reason="nav2_dynamic_observation_stale",
    )

    collision_candidate = _candidate()
    collision_points = [
        {"x_m": -1.0, "y_m": 0.0, "frame_id": "map"},
        {"x_m": 1.0, "y_m": 0.0, "frame_id": "map"},
    ]
    collision_candidate.update(
        {
            "y_m": 0.0,
            "path_points": collision_points,
            "path_sha256": _path_sha256(collision_points),
        }
    )
    collision = _case(
        case_id="nav2-refusal-collision-path",
        scenario_class="refusal",
        candidate=collision_candidate,
        evaluation=_evaluation(collision_candidate),
        obstacle=_obstacle(),
        expected_status="blocked",
        required_reason="nav2_candidate_collision_envelope_intersection",
    )

    cases = [
        positive,
        collision,
        missing_geometry,
        frame_mismatch,
        stale,
    ]
    CASES.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for case in cases:
        relative = Path("cases") / f"{case['case_id']}.json"
        (ROOT / relative).write_text(
            json.dumps(case, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        entries.append({"path": str(relative), "sha256": _sha256(case)})
    material = {
        "schema_version": NAV2_CORPUS_SCHEMA,
        "corpus_id": "nav2_v1",
        "case_count": len(entries),
        "cases": entries,
        "claim_boundary": (
            "Replay verifies artifact contracts only; it does not invoke "
            "Nav2, a simulator, an LLM, approval, dispatch, or execution."
        ),
    }
    manifest = {**material, "manifest_sha256": _sha256(material)}
    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
