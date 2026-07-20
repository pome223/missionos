from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.gateway import server as gateway_server
from src.intelligence import missionos_agent_runtime
from src.runtime.px4_gazebo_route.recovery_intent_compiler import (
    build_runtime_recovery_intent,
    compile_runtime_recovery_intent,
    recovery_artifact_hash_matches,
    verify_runtime_recovery_outcome,
    verify_runtime_recovery_reachability,
)


pytestmark = pytest.mark.contract


def _policy() -> dict:
    return {
        "policy_ref": "test_intent_compiler_policy",
        "max_recovery_duration_s": 75.0,
        "max_recovery_horizontal_speed_mps": 10.0,
        "max_recovery_vertical_speed_mps": 3.0,
        "max_reroute_target_abs_m": 5000.0,
        "reachability_duration_margin_factor": 1.25,
        "reachability_setup_seconds": 5.0,
        "wind_uncertainty_floor_mps": 1.0,
    }


def _telemetry(*, wind_mps: float = 1.0) -> dict:
    return {
        "position": {
            "local_x_m": 0.0,
            "local_y_m": 0.0,
            "altitude_above_home_m": 30.0,
        },
        "wind": {"speed_mps": wind_mps},
        "telemetry": {"stale": False},
        "battery": {
            "endurance_projection": {
                "projected_insufficient_for_route": False,
            }
        },
    }


def _intent_and_compilation(*, wind_mps: float = 1.0) -> tuple[dict, dict, dict]:
    intent = build_runtime_recovery_intent(
        agent_output={
            "strategy": "local_avoidance",
            "selected_bounded_action": "avoid_obstacle",
            "proposed_parameters": {
                "target_x_m": 80.0,
                "target_y_m": 30.0,
                "target_altitude_m": 35.0,
            },
            "intent_constraints": {
                "avoidance_side": "left",
                "minimum_clearance_m": 30.0,
                "maximum_duration_s": 75.0,
            },
            "requires_human_approval": True,
        },
        observed_at="2026-07-19T00:00:00+00:00",
        decision_signature="decision_signature_test",
    )
    candidate = {
        "selected_bounded_action": "avoid_obstacle",
        "proposed_parameters": {
            "target_x_m": 80.0,
            "target_y_m": 30.0,
            "target_altitude_m": 35.0,
        },
        "basis": {
            "avoidance_side": "left",
            "minimum_lateral_clearance_m": 32.0,
        },
        "source_refs": ["telemetry.obstacle", "telemetry.route"],
    }
    compilation = compile_runtime_recovery_intent(
        intent=intent,
        candidate=candidate,
        recovery_policy=_policy(),
    )
    reachability = verify_runtime_recovery_reachability(
        compilation=compilation,
        telemetry_snapshot=_telemetry(wind_mps=wind_mps),
        recovery_policy=_policy(),
    )
    return intent, compilation, reachability


def test_compiler_preserves_llm_strategy_action_and_constraints() -> None:
    intent, compilation, reachability = _intent_and_compilation()

    assert intent["intent_status"] == "valid"
    assert intent["strategy"] == "local_avoidance"
    assert intent["dispatch_authority_created"] is False
    assert recovery_artifact_hash_matches(intent, id_prefix="recovery_intent")
    assert compilation["compilation_status"] == "compiled"
    assert compilation["meaning_preserved"] is True
    assert compilation["compiled_action"] == "avoid_obstacle"
    assert recovery_artifact_hash_matches(
        compilation,
        id_prefix="recovery_compilation",
    )
    assert reachability["verification_status"] == "verified"
    assert reachability["reachability_verified"] is True
    assert reachability["dispatch_authority_created"] is False


def test_compiler_returns_infeasible_instead_of_changing_avoidance_side() -> None:
    intent, _, _ = _intent_and_compilation()
    compilation = compile_runtime_recovery_intent(
        intent=intent,
        candidate={
            "selected_bounded_action": "avoid_obstacle",
            "proposed_parameters": intent["requested_parameters"],
            "basis": {
                "avoidance_side": "right",
                "minimum_lateral_clearance_m": 32.0,
            },
        },
        recovery_policy=_policy(),
    )

    assert compilation["compilation_status"] == "infeasible"
    assert compilation["meaning_preserved"] is False
    assert compilation["compiled_action"] == ""
    assert "recovery_compiler_cannot_preserve_avoidance_side" in compilation[
        "blocking_reasons"
    ]
    assert compilation["dispatch_authority_created"] is False


def test_guard_preserves_agent_constraints_or_returns_operator_review() -> None:
    planner_result = {
        "tool_status": "computed",
        "recommended_candidate": {
            "selected_bounded_action": "avoid_obstacle",
            "proposed_parameters": {
                "target_x_m": 80.0,
                "target_y_m": 30.0,
                "target_altitude_m": 35.0,
            },
            "basis": {
                "avoidance_side": "left",
                "minimum_lateral_clearance_m": 32.0,
            },
        },
    }
    guarded = missionos_agent_runtime.guard_runtime_recovery_planner_result(
        planner_result=planner_result,
        telemetry_snapshot={
            **_telemetry(),
            "obstacle": {"obstacle_detected": True},
        },
        recovery_policy=_policy(),
        agent_intent={
            "strategy": "local_avoidance",
            "intent_constraints": {
                "avoidance_side": "right",
                "minimum_clearance_m": 30.0,
            },
        },
    )

    assessment = guarded["recovery_guardrail_assessment"]
    assert guarded["recommended_candidate"]["selected_bounded_action"] == (
        "operator_review"
    )
    assert assessment["recovery_intent"]["strategy"] == "local_avoidance"
    assert assessment["recovery_intent"]["intent_constraints"] == {
        "avoidance_side": "right",
        "minimum_clearance_m": 30.0,
    }
    assert "recovery_compiler_cannot_preserve_avoidance_side" in assessment[
        "blocking_reasons"
    ]


def test_intent_rejects_invalid_constraint_values() -> None:
    intent = build_runtime_recovery_intent(
        agent_output={
            "strategy": "local_avoidance",
            "selected_bounded_action": "avoid_obstacle",
            "intent_constraints": {
                "avoidance_side": "upwind-ish",
                "maximum_duration_s": -1,
            },
        }
    )

    assert intent["intent_status"] == "invalid"
    assert "recovery_intent_avoidance_side_not_supported" in intent[
        "blocking_reasons"
    ]
    assert "recovery_intent_constraint_not_positive:maximum_duration_s" in intent[
        "blocking_reasons"
    ]


def test_strong_wind_keeps_reachability_unverified() -> None:
    _, _, reachability = _intent_and_compilation(wind_mps=12.0)

    assert reachability["verification_status"] == "unverified"
    assert reachability["reachability_verified"] is False
    assert "recovery_reachability_control_margin_not_positive" in reachability[
        "blocking_reasons"
    ]
    assert reachability["upper_bound_duration_s"] is None


def test_outcome_verifier_does_not_treat_ack_as_effect_or_success() -> None:
    verification = verify_runtime_recovery_outcome(
        action="avoid_obstacle",
        recovery_observation={
            "command_ack_observed": True,
            "assist_attempted": False,
            "target_reached": False,
            "resume_status": "not_resumed",
        },
        dispatch_authority_created=True,
    )

    assert verification["verification_status"] == "failed"
    assert verification["ack_is_execution_effect"] is False
    assert verification["recovery_success_verified"] is False
    assert "recovery_outcome_executor_effect_not_observed" in verification[
        "blocking_reasons"
    ]
    assert "recovery_outcome_target_not_reached" in verification[
        "blocking_reasons"
    ]


def test_outcome_verifier_requires_target_and_verified_auto_resume() -> None:
    verification = verify_runtime_recovery_outcome(
        action="avoid_obstacle",
        recovery_observation={
            "command_ack_observed": True,
            "assist_attempted": True,
            "target_reached": True,
            "resume_status": "resumed_auto_mission",
            "resume_safety_verification": {
                "verification_status": "verified",
                "resume_auto_authorized": True,
            },
        },
        dispatch_authority_created=True,
    )

    assert verification["verification_status"] == "verified"
    assert verification["recovery_success_verified"] is True
    assert verification["delivery_completion_claimed"] is False


def test_v2_dispatch_revalidation_recomputes_reachability() -> None:
    now = datetime.now(timezone.utc)
    intent, compilation, reachability = _intent_and_compilation()
    parameters = gateway_server._bounded_operator_recovery_parameters(
        recovery_action="avoid_obstacle",
        body={
            "recovery_parameters": dict(compilation["compiled_parameters"])
        },
    )
    proposal = {
        "schema_version": "missionos_runtime_recovery_proposal_evidence.v2",
        "proposal_id": "proposal_compiled_recovery",
        "proposal_status": "awaiting_operator_approval",
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(minutes=2)).isoformat(),
        "origin_position": {"local_x_m": 0.0, "local_y_m": 0.0},
        "max_origin_drift_m": 30.0,
        "recovery_intent": intent,
        "intent_compilation": compilation,
        "reachability_verification": reachability,
        "runtime_recovery_agent_result": {
            "assessment": {
                "recovery_planner_tool_candidate": {
                    "selected_bounded_action": "avoid_obstacle",
                    "proposed_parameters": parameters,
                }
            }
        },
    }
    valid = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts={
            "missionos_runtime_recovery_last_proposal": proposal,
            "missionos_runtime_recovery_agent_live_bridge": {
                "telemetry_snapshot": _telemetry(wind_mps=1.0)
            },
        },
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=now,
    )
    blocked = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts={
            "missionos_runtime_recovery_last_proposal": proposal,
            "missionos_runtime_recovery_agent_live_bridge": {
                "telemetry_snapshot": _telemetry(wind_mps=12.0)
            },
        },
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=now,
    )

    assert valid["validation_status"] == "valid"
    assert valid["dispatch_reachability_verification"]["verification_status"] == "verified"
    assert blocked["validation_status"] == "blocked"
    assert "runtime_recovery_dispatch_reachability_unverified" in blocked["reasons"]


def test_avoid_obstacle_parameters_preserve_bound_source_obstacle_name() -> None:
    parameters = gateway_server._bounded_operator_recovery_parameters(
        recovery_action="avoid_obstacle",
        body={
            "recovery_parameters": {
                "target_x_m": 1.0,
                "target_y_m": 320.0,
                "target_altitude_m": 45.0,
                "source_obstacle_name": "missionos_route_obstacle_50pct",
            }
        },
    )

    assert parameters["source_obstacle_name"] == ("missionos_route_obstacle_50pct")


def test_v2_dispatch_revalidation_rejects_mixed_artifact_chain() -> None:
    now = datetime.now(timezone.utc)
    intent, compilation, reachability = _intent_and_compilation()
    other_intent = build_runtime_recovery_intent(
        agent_output={
            "strategy": "local_avoidance",
            "selected_bounded_action": "avoid_obstacle",
            "proposed_parameters": intent["requested_parameters"],
            "intent_constraints": {
                "avoidance_side": "left",
                "minimum_clearance_m": 31.0,
            },
        }
    )
    parameters = dict(compilation["compiled_parameters"])
    proposal = {
        "schema_version": "missionos_runtime_recovery_proposal_evidence.v2",
        "proposal_id": "proposal_mixed_chain",
        "proposal_status": "awaiting_operator_approval",
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(minutes=2)).isoformat(),
        "origin_position": {"local_x_m": 0.0, "local_y_m": 0.0},
        "max_origin_drift_m": 30.0,
        "recovery_intent": other_intent,
        "intent_compilation": compilation,
        "reachability_verification": reachability,
        "runtime_recovery_agent_result": {
            "assessment": {
                "recovery_planner_tool_candidate": {
                    "selected_bounded_action": "avoid_obstacle",
                    "proposed_parameters": parameters,
                }
            }
        },
    }
    result = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts={
            "missionos_runtime_recovery_last_proposal": proposal,
            "missionos_runtime_recovery_agent_live_bridge": {
                "telemetry_snapshot": _telemetry()
            },
        },
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=now,
    )

    assert result["validation_status"] == "blocked"
    assert "runtime_recovery_intent_compilation_chain_mismatch" in result[
        "reasons"
    ]
