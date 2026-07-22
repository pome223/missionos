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
        "max_wind_speed_mps": 6.0,
        "wind_uncertainty_floor_mps": 1.0,
    }


def _telemetry(
    *,
    wind_mps: float = 1.0,
    sample_index: int = 30,
    elapsed_seconds: float = 30.0,
) -> dict:
    return {
        "sample_index": sample_index,
        "elapsed_seconds": elapsed_seconds,
        "position": {
            "local_x_m": 0.0,
            "local_y_m": 0.0,
            "altitude_above_home_m": 30.0,
        },
        "wind": {"speed_mps": wind_mps},
        "telemetry": {"stale": False},
        "obstacle": {
            "conflict_assessment": {
                "local_avoidance_required": True,
                "nearest_obstacle": {
                    "obstacle_name": "missionos_route_obstacle_50pct",
                },
            }
        },
        "battery": {
            "endurance_projection": {
                "projected_insufficient_for_route": False,
            }
        },
    }


def _runtime_snapshot(telemetry: dict) -> dict:
    position = telemetry.get("position") or {}
    wind = telemetry.get("wind") or {}
    state = telemetry.get("telemetry") or {}
    return {
        "sample_index": telemetry.get("sample_index"),
        "elapsed_seconds": telemetry.get("elapsed_seconds"),
        "local_x_m": position.get("local_x_m"),
        "local_y_m": position.get("local_y_m"),
        "altitude_above_home_m": position.get("altitude_above_home_m"),
        "wind_speed_mps": wind.get("speed_mps"),
        "heartbeat_observed": state.get("stale") is not True,
        "landed": False,
    }


def _dispatch_artifacts(
    proposal: dict,
    telemetry: dict,
    *,
    window_samples: list[dict] | None = None,
) -> dict:
    bridge = {"telemetry_snapshot": telemetry}
    if window_samples is not None:
        bridge["recovery_window_samples"] = window_samples
    return {
        "missionos_runtime_recovery_last_proposal": proposal,
        "missionos_auto_mission_runtime_snapshot": _runtime_snapshot(
            telemetry
        ),
        "missionos_runtime_recovery_agent_live_bridge": bridge,
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
                "source_obstacle_name": "missionos_route_obstacle_50pct",
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
            "source_obstacle_name": "missionos_route_obstacle_50pct",
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
        "source_obstacle_name": "missionos_route_obstacle_50pct",
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
        artifacts=_dispatch_artifacts(
            proposal,
            _telemetry(wind_mps=1.0),
        ),
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=now,
    )
    blocked = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts=_dispatch_artifacts(
            proposal,
            _telemetry(wind_mps=7.0),
        ),
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=now,
    )
    stale_bridge = _telemetry(
        wind_mps=5.0,
        sample_index=190,
        elapsed_seconds=190.0,
    )
    newer_runtime = _runtime_snapshot(
        _telemetry(
            wind_mps=7.0,
            sample_index=200,
            elapsed_seconds=200.0,
        )
    )
    newer_runtime_result = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts={
            "missionos_runtime_recovery_last_proposal": proposal,
            "missionos_auto_mission_runtime_snapshot": newer_runtime,
            "missionos_runtime_recovery_agent_live_bridge": {
                "telemetry_snapshot": stale_bridge,
            },
        },
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=now,
    )
    regressed_cursor_result = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts={
            "missionos_runtime_recovery_last_proposal": proposal,
            "missionos_auto_mission_runtime_snapshot": {
                **newer_runtime,
                "elapsed_seconds": 180.0,
            },
            "missionos_runtime_recovery_agent_live_bridge": {
                "telemetry_snapshot": stale_bridge,
            },
        },
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=now,
    )

    assert valid["validation_status"] == "valid"
    assert valid["dispatch_reachability_verification"]["verification_status"] == "verified"
    assert blocked["validation_status"] == "blocked"
    assert blocked["dispatch_reachability_verification"]["verification_status"] == (
        "verified"
    )
    assert "runtime_recovery_dispatch_current_wind_above_limit" in blocked[
        "reasons"
    ]
    assert newer_runtime_result["validation_status"] == "blocked"
    assert newer_runtime_result["telemetry_arbitration"][
        "selected_source"
    ] == "missionos_auto_mission_runtime_snapshot"
    assert "runtime_recovery_dispatch_current_wind_above_limit" in (
        newer_runtime_result["reasons"]
    )
    assert regressed_cursor_result["validation_status"] == "blocked"
    assert "telemetry_arbitration_cursor_regression" in (
        regressed_cursor_result["reasons"]
    )
    assert "runtime_recovery_telemetry_arbitration_unverified" in (
        regressed_cursor_result["reasons"]
    )


def test_compound_dispatch_revalidates_latest_safe_window() -> None:
    now = datetime.now(timezone.utc)
    intent, compilation, reachability = _intent_and_compilation()
    parameters = gateway_server._bounded_operator_recovery_parameters(
        recovery_action="avoid_obstacle",
        body={"recovery_parameters": dict(compilation["compiled_parameters"])},
    )
    safe_window = {
        "verification_status": "verified_safe",
        "safe_window_observed": True,
        "minimum_window_s": 30.0,
        "maximum_sample_gap_s": 15.0,
    }
    proposal = {
        "schema_version": "missionos_runtime_recovery_proposal_evidence.v2",
        "proposal_id": "proposal_compound_safe_window",
        "proposal_status": "awaiting_operator_approval",
        "source_obstacle_name": "missionos_route_obstacle_50pct",
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(minutes=2)).isoformat(),
        "origin_position": {"local_x_m": 0.0, "local_y_m": 0.0},
        "max_origin_drift_m": 30.0,
        "recovery_intent": intent,
        "intent_compilation": compilation,
        "reachability_verification": reachability,
        "compound_hazard_transition": {
            "transition_status": "wind_safe_window_observed",
            "source_obstacle_name": "missionos_route_obstacle_50pct",
            "wind_safe_window": safe_window,
        },
        "runtime_recovery_agent_result": {
            "assessment": {
                "recovery_planner_tool_candidate": {
                    "selected_bounded_action": "avoid_obstacle",
                    "proposed_parameters": parameters,
                }
            }
        },
    }

    def _sample(elapsed_s: float, wind_mps: float) -> dict:
        return {
            **_telemetry(wind_mps=wind_mps),
            "elapsed_seconds": elapsed_s,
            "sample_index": int(elapsed_s),
        }

    safe_samples = [_sample(value, 5.0) for value in (0.0, 10.0, 20.0, 30.0)]
    valid = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts=_dispatch_artifacts(
            proposal,
            safe_samples[-1],
            window_samples=safe_samples,
        ),
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=now,
    )
    raised_samples = [*safe_samples, _sample(31.0, 7.0)]
    blocked = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts=_dispatch_artifacts(
            proposal,
            raised_samples[-1],
            window_samples=raised_samples,
        ),
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=now,
    )
    tail_mismatch_telemetry = _sample(31.0, 5.0)
    tail_mismatch = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts=_dispatch_artifacts(
            proposal,
            tail_mismatch_telemetry,
            window_samples=safe_samples,
        ),
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=now,
    )

    assert valid["validation_status"] == "valid"
    assert valid["dispatch_safe_window_revalidation"]["safe_window_observed"] is True
    assert blocked["validation_status"] == "blocked"
    assert "runtime_recovery_dispatch_current_wind_above_limit" in blocked[
        "reasons"
    ]
    assert "runtime_recovery_dispatch_safe_window_unverified" in blocked[
        "reasons"
    ]
    assert tail_mismatch["validation_status"] == "blocked"
    assert "runtime_recovery_dispatch_safe_window_tail_mismatch" in (
        tail_mismatch["reasons"]
    )


def test_dispatch_rejects_changed_or_inactive_obstacle_binding() -> None:
    now = datetime.now(timezone.utc)
    intent, compilation, reachability = _intent_and_compilation()
    parameters = gateway_server._bounded_operator_recovery_parameters(
        recovery_action="avoid_obstacle",
        body={"recovery_parameters": dict(compilation["compiled_parameters"])},
    )
    proposal = {
        "schema_version": "missionos_runtime_recovery_proposal_evidence.v2",
        "proposal_id": "proposal_obstacle_binding",
        "proposal_status": "awaiting_operator_approval",
        "source_obstacle_name": "missionos_route_obstacle_50pct",
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
    changed = _telemetry()
    changed["obstacle"]["conflict_assessment"]["nearest_obstacle"][
        "obstacle_name"
    ] = "missionos_route_obstacle_75pct"
    changed_result = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts=_dispatch_artifacts(proposal, changed),
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=now,
    )
    inactive = _telemetry()
    inactive["obstacle"]["conflict_assessment"][
        "local_avoidance_required"
    ] = False
    inactive_result = gateway_server._runtime_recovery_proposal_revalidation(
        artifacts=_dispatch_artifacts(proposal, inactive),
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=now,
    )

    assert changed_result["validation_status"] == "blocked"
    assert "runtime_recovery_dispatch_obstacle_source_mismatch" in changed_result[
        "reasons"
    ]
    assert inactive_result["validation_status"] == "blocked"
    assert "runtime_recovery_dispatch_local_avoidance_not_required" in (
        inactive_result["reasons"]
    )


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
        "source_obstacle_name": "missionos_route_obstacle_50pct",
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
        artifacts=_dispatch_artifacts(proposal, _telemetry()),
        recovery_action="avoid_obstacle",
        recovery_parameters=parameters,
        now=now,
    )

    assert result["validation_status"] == "blocked"
    assert "runtime_recovery_intent_compilation_chain_mismatch" in result[
        "reasons"
    ]
