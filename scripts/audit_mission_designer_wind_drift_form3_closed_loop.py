#!/usr/bin/env python3
"""Audit wind drift with two response/action/re-observation cycles.

This is stricter than the wind drift recovery closed-loop audit: it requires a
same-run cycle 1 bounded RTL response and a cycle 2 bounded LAND response with
an action-outcome observation after each response.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from scripts.audit_mission_designer_wind_drift_recovery_closed_loop import (
    DEFAULT_DRIFT_THRESHOLD_M,
    DEFAULT_WIND_DIRECTION_DEG,
    DEFAULT_WIND_MPS,
    UNSAFE_AUTHORITY_KEYS,
    _as_float,
    _nested_true_keys,
    _read_json,
    _source_refs_observed,
    _summary_path,
    _wind_application_source_bound,
    _wind_drift_observed,
    _write_json,
)
from scripts.diagnose_mission_designer_wind_form3_partial_run import (
    build_diagnostic,
)
from scripts.mission_designer_form3_envelope_source import (
    MISSION_DESIGNER_FORM3_MISSION_CONTRACT_REF,
    MISSION_DESIGNER_FORM3_SOURCE_BACKEND_TYPE,
    MISSION_DESIGNER_FORM3_TASK_GRAPH_REF,
    build_form3_backend_context,
    build_wind_parameterized_sdf_delta_proof,
    parameter_observation,
)


SCHEMA_VERSION = "mission_designer_wind_drift_form3_closed_loop_audit.v1"
RTL_RESPONSE_REF = "mission_response_candidate:wind_drift_bounded_rtl"
LAND_RESPONSE_REF = "mission_response_candidate:wind_rtl_state_bounded_land"
POLICY_GATE_REF = "policy_gate_result:wind_rtl_state_bounded_land"
OPERATOR_DECISION_REF = "operator_decision:wind_rtl_state_bounded_land"
AI_ASSESSMENT_REF = "ai_mission_situation_assessment:wind_rtl_state_bounded_land"
MISSION_OS_LOOP_SCHEMA_VERSION = "mission_os_recovery_runtime_bridge.v1"
RTL_DECISION_REF = "mission_os_recovery_decision:wind_drift_bounded_rtl"
RTL_ACTION_REQUEST_REF = "mission_os_backend_action_request:wind_drift_bounded_rtl"
RTL_ACTION_RECEIPT_REF = "mission_os_backend_action_receipt:wind_drift_bounded_rtl"
RTL_OUTCOME_REF = "mission_os_recovery_outcome_observation:wind_drift_bounded_rtl"
LAND_DECISION_REF = "mission_os_recovery_decision:wind_rtl_state_bounded_land"
LAND_ACTION_REQUEST_REF = "mission_os_backend_action_request:wind_rtl_state_bounded_land"
LAND_ACTION_RECEIPT_REF = "mission_os_backend_action_receipt:wind_rtl_state_bounded_land"
LAND_OUTCOME_REF = "mission_os_recovery_outcome_observation:wind_rtl_state_bounded_land"


def _latest_partial_run_dir(artifact_root: Path) -> Path | None:
    if not artifact_root.exists():
        return None
    candidates = [
        path
        for path in artifact_root.rglob("pose_samples.jsonl")
        if path.parent.is_dir()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime).parent


def _build_partial_run_artifact(
    run_dir: Path,
    *,
    audit_dir: Path,
    run_mode: str,
    smoke_error: str | None = None,
) -> dict[str, Any]:
    artifact = build_diagnostic(run_dir)
    artifact["audit_dir"] = str(audit_dir)
    artifact["run_mode"] = run_mode
    if smoke_error:
        artifact["smoke_error_digest"] = smoke_error[-2000:]
    return artifact


def _rtl_recovery_observed(summary: dict[str, Any]) -> bool:
    return (
        summary.get("recovery_action_taken") == "rtl"
        and summary.get("recovery_completed") is True
        and summary.get("recovery_state_observed") is True
        and summary.get("recovery_state_label") == "return_to_launch_state_observed"
        and summary.get("recovery_completion_basis")
        in ("state_observed_after_dispatch_timeout", "ack_observed_and_state_observed")
        and summary.get("recovery_dispatch_status") in ("accepted", "timeout")
        and str(summary.get("recovery_dispatch_ref", "")).startswith(
            "px4_gazebo_emergency_command_dispatch_result:"
        )
        and str(summary.get("recovery_completion_ref", "")).startswith(
            "px4_gazebo_route_recovery_completion:"
        )
    )


def _post_recovery_land_observed(summary: dict[str, Any]) -> bool:
    pose_z = _as_float(summary.get("post_recovery_pose_z_m"))
    return (
        summary.get("post_recovery_action_taken") == "land"
        and summary.get("post_recovery_completed") is True
        and summary.get("post_recovery_state_observed") is True
        and summary.get("post_recovery_completion_basis")
        in ("state_observed_after_dispatch_timeout", "ack_observed_and_state_observed")
        and summary.get("post_recovery_dispatch_status") in ("accepted", "timeout")
        and pose_z is not None
        and pose_z <= 0.15
        and str(summary.get("post_recovery_dispatch_ref", "")).startswith(
            "px4_gazebo_emergency_command_dispatch_result:"
        )
        and str(summary.get("post_recovery_completion_ref", "")).startswith(
            "px4_gazebo_route_recovery_completion:"
        )
    )


def _build_ai_assessment(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "ai_mission_situation_assessment.v1",
        "assessment_id": AI_ASSESSMENT_REF,
        "cycle_index": 2,
        "assessment_generated_by": "deterministic_rule",
        "ai_model_invoked": False,
        "assessment_authority_scope": "mission_evidence_only",
        "assessment_is_gate_input": True,
        "assessment_is_gate_verdict": False,
        "assessment_created_dispatch_authority": False,
        "source_observation_ref": summary.get("recovery_completion_ref"),
        "mission_state_interpretation": "rtl_state_observed_after_wind_drift",
        "assessment_summary": (
            "Wind drift triggered a bounded RTL response and RTL state was "
            "observed. A second bounded LAND response is appropriate as a "
            "same-run safety recovery action, but the assessment itself is not "
            "a gate verdict or dispatch authority."
        ),
        "mission_response_candidate": "bounded_land",
        "candidate_confidence": "medium",
        "uncertainty_reasons": [
            "wind_drift_exceeded_threshold",
            "route_stream_terminated_before_recovery_dispatch",
            "rtl_state_observed_not_delivery_completion",
        ],
        "operator_question": (
            "RTL state was observed after wind drift. Approve a bounded LAND "
            "recovery action and verify landing outcome?"
        ),
        "operator_review_required": True,
        "automatic_dispatch_suppressed": False,
        "ai_judgment_is_gate_verdict": False,
        "ai_judgment_created_dispatch_authority": False,
        "llm_gate_judge_used": False,
        "created_dispatch_authority": False,
    }


def _build_policy_gate_result(assessment: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "policy_gate_result.v1",
        "gate_id": "policy_gate:wind_rtl_state_bounded_land",
        "cycle_index": 2,
        "input_response_candidate_ref": LAND_RESPONSE_REF,
        "ai_situation_assessment_ref": AI_ASSESSMENT_REF,
        "ai_assessment_required": True,
        "ai_proposal_confidence": assessment["candidate_confidence"],
        "gate_status": "pass",
        "gate_decision_basis": [
            "source_bound_wind_drift_observed",
            "return_to_launch_state_observed",
            "operator_approval_required",
            "bounded_land_allowlisted",
        ],
        "operator_review_required": True,
        "automatic_dispatch_allowed": False,
        "operator_approved_dispatch_allowed": True,
        "bounded_action_dispatch_allowed": True,
        "allowed_actions": ["bounded_land"],
        "forbidden_actions": [
            "delivery_completion_claim",
            "approval_free_dispatch",
            "hardware_execution",
            "physical_execution",
        ],
        "ai_judgment_used_for_gate": False,
        "ai_judgment_is_gate_verdict": False,
        "ai_judgment_created_dispatch_authority": False,
        "llm_gate_judge_used": False,
        "authority_flags": {
            "delivery_completion_claimed": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def _build_operator_decision_record(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "operator_decision_record.v1",
        "decision_id": OPERATOR_DECISION_REF,
        "cycle_index": 2,
        "advisory_ref": LAND_RESPONSE_REF,
        "ai_situation_assessment_ref": AI_ASSESSMENT_REF,
        "operator_question": (
            "RTL state was observed after wind drift. Approve bounded LAND?"
        ),
        "operator_question_options": ["approve_bounded_land", "rtl", "abort"],
        "selected_option": "approve_bounded_land",
        "selection_status": "operator_approved",
        "selected_bounded_action": "land",
        "created_dispatch_authority": False,
        "bounded_action_ref": "bounded_action:wind_rtl_state_land",
        "dispatch_ref": summary.get("post_recovery_dispatch_ref"),
        "action_outcome_observation_ref": summary.get(
            "post_recovery_completion_ref"
        ),
    }


def _runtime_decision_ref(cycle_index: int) -> str:
    return RTL_DECISION_REF if cycle_index == 1 else LAND_DECISION_REF


def _runtime_action_request_ref(cycle_index: int) -> str:
    return RTL_ACTION_REQUEST_REF if cycle_index == 1 else LAND_ACTION_REQUEST_REF


def _runtime_action_receipt_ref(cycle_index: int) -> str:
    return RTL_ACTION_RECEIPT_REF if cycle_index == 1 else LAND_ACTION_RECEIPT_REF


def _runtime_outcome_ref(cycle_index: int) -> str:
    return RTL_OUTCOME_REF if cycle_index == 1 else LAND_OUTCOME_REF


def _requested_payload_kg(summary: dict[str, Any]) -> float | None:
    requested = (summary.get("vehicle_condition_profile") or {}).get("requested") or {}
    return _as_float(requested.get("payload_mass_kg"))


def _battery_warning_state(summary: dict[str, Any]) -> str:
    observed = (summary.get("observed_battery_condition_evidence") or {}).get(
        "observed"
    ) or {}
    warning_state = observed.get("battery_warning_state") or observed.get(
        "warning_state"
    )
    if warning_state:
        return str(warning_state)
    return "nominal_or_unknown"


def _telemetry_continuity(summary: dict[str, Any]) -> str:
    if summary.get("observer_dropout_active") is True:
        return "observer_dropout_active"
    if summary.get("telemetry_observer_dropout_active") is True:
        return "observer_dropout_active"
    if summary.get("actual_px4_gazebo_horizontal_smoke_observed") is True:
        return "sufficient_for_recovery_audit"
    return "unknown"


def _route_blocking_active(summary: dict[str, Any]) -> bool:
    operational_requested = (
        (summary.get("operational_condition_profile") or {}).get("requested") or {}
    )
    return bool(
        operational_requested.get("route_blocking_enabled")
        or operational_requested.get("collision_obstacle")
        or summary.get("route_blocking_observed") is True
        or summary.get("obstacle_route_blocking_observed") is True
    )


def _payload_margin_risk(summary: dict[str, Any]) -> str:
    payload_kg = _requested_payload_kg(summary)
    if payload_kg is None:
        return "unknown_or_not_active"
    if payload_kg > 5.0:
        return "contract_envelope_violation"
    if payload_kg > 1.25:
        return "payload_margin_risk_possible"
    return "not_active"


def _compound_assessment_inputs(
    summary: dict[str, Any],
    *,
    cycle_index: int,
    selected_bounded_action: str,
) -> dict[str, Any]:
    wind_requested = (
        (summary.get("environment_condition_profile") or {}).get("requested") or {}
    )
    wind_observed = (
        (summary.get("observed_environment_evidence") or {}).get("observed") or {}
    )
    wind_speed = _as_float(
        wind_observed.get("wind_mean_mps")
        if wind_observed.get("wind_mean_mps") is not None
        else wind_requested.get("wind_mean_mps")
    )
    wind_direction = _as_float(
        wind_observed.get("wind_direction_deg")
        if wind_observed.get("wind_direction_deg") is not None
        else wind_requested.get("wind_direction_deg")
    )
    wind_drift = None
    if summary.get("deviation_samples"):
        wind_drift = _as_float(
            (summary.get("deviation_samples") or [{}])[0].get("deviation_xy_m")
        )
    route_blocking_active = _route_blocking_active(summary)
    battery_warning_state = _battery_warning_state(summary)
    payload_margin_risk = _payload_margin_risk(summary)
    telemetry_continuity = _telemetry_continuity(summary)
    dropoff_verified = summary.get("dropoff_verified") is True
    delivery_completion_claimed = summary.get("delivery_completion_claimed") is True
    conflicting_risks = []
    if route_blocking_active:
        conflicting_risks.append("route_blocking_active")
    if battery_warning_state not in ("nominal", "nominal_or_unknown", "unknown"):
        conflicting_risks.append("battery_warning_state_active")
    if payload_margin_risk in (
        "contract_envelope_violation",
        "payload_margin_risk_possible",
    ):
        conflicting_risks.append("payload_margin_risk_active")
    if telemetry_continuity == "observer_dropout_active":
        conflicting_risks.append("telemetry_observer_dropout_active")
    if delivery_completion_claimed:
        conflicting_risks.append("delivery_completion_already_claimed")
    return {
        "primary_trigger": "wind_drift_exceeded_threshold",
        "assessment_mode": "compound_mission_state_assessment",
        "cycle_index": cycle_index,
        "wind": {
            "drift_above_threshold": True,
            "wind_speed_mps": wind_speed,
            "wind_direction_deg": wind_direction,
            "wind_drift_deviation_xy_m": wind_drift,
            "source_bound_application_observed": _wind_application_source_bound(
                summary,
                expected_wind_mps=wind_speed or DEFAULT_WIND_MPS,
                expected_direction_deg=wind_direction or DEFAULT_WIND_DIRECTION_DEG,
            )
            if wind_speed is not None and wind_direction is not None
            else False,
        },
        "battery": {
            "battery_warning_state": battery_warning_state,
            "px4_battery_warning_state_affected": False,
        },
        "payload": {
            "payload_kg": _requested_payload_kg(summary),
            "payload_feasibility_advisory_active": payload_margin_risk
            in ("contract_envelope_violation", "payload_margin_risk_possible"),
            "payload_margin_risk": payload_margin_risk,
        },
        "route": {
            "route_blocked": route_blocking_active,
            "dropoff_verified": dropoff_verified,
            "delivery_completion_claimed": delivery_completion_claimed,
        },
        "telemetry": {
            "telemetry_continuity": telemetry_continuity,
            "observer_dropout_active": telemetry_continuity
            == "observer_dropout_active",
        },
        "recovery_state": {
            "cycle1_recovery_action_taken": summary.get("recovery_action_taken"),
            "cycle1_recovery_state_label": summary.get("recovery_state_label"),
            "selected_bounded_action": selected_bounded_action,
        },
        "authority": {
            "operator_review_required": True,
            "automatic_dispatch_allowed": False,
            "bounded_action_dispatch_allowed": True,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "conflicting_risks": conflicting_risks,
        "conflict_policy": (
            "operator_review_required_or_form0b_readiness_when_conflict_active"
        ),
        "mission_state_interpretation": (
            "wind_drift_recovery_required_no_conflicting_blocker_detected"
            if not conflicting_risks
            else "wind_drift_recovery_requires_operator_review_for_compound_risk"
        ),
    }


def _build_mission_os_recovery_decision(
    *,
    cycle_index: int,
    observation_ref: str | None,
    response_ref: str,
    selected_bounded_action: str,
    policy_gate_ref: str | None,
    operator_decision_ref: str | None,
    ai_situation_assessment_ref: str | None,
    assessment_inputs: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "mission_os_recovery_decision.v1",
        "decision_id": _runtime_decision_ref(cycle_index),
        "cycle_index": cycle_index,
        "decision_loop_driver": "scripted_mission_os_runtime_bridge",
        "full_gateway_runtime_loop": False,
        "source_observation_ref": observation_ref,
        "mission_response_candidate_ref": response_ref,
        "primary_trigger": "wind_drift_exceeded_threshold",
        "assessment_inputs": assessment_inputs,
        "mission_state_interpretation": assessment_inputs[
            "mission_state_interpretation"
        ],
        "selected_bounded_action": selected_bounded_action,
        "policy_gate_ref": policy_gate_ref,
        "operator_decision_ref": operator_decision_ref,
        "ai_situation_assessment_ref": ai_situation_assessment_ref,
        "decision_authority_scope": "bounded_sitl_recovery_only",
        "operator_approval_required": True,
        "automatic_dispatch_allowed": False,
        "operator_approved_dispatch_allowed": True,
        "ai_judgment_is_gate_verdict": False,
        "ai_judgment_created_dispatch_authority": False,
        "llm_gate_judge_used": False,
        "created_dispatch_authority": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def _build_mission_os_backend_action_request(
    *,
    cycle_index: int,
    decision_ref: str,
    bounded_action: str,
    expected_dispatch_ref: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": "mission_os_backend_action_request.v1",
        "request_id": _runtime_action_request_ref(cycle_index),
        "cycle_index": cycle_index,
        "decision_ref": decision_ref,
        "backend_target": "px4_gazebo_sitl",
        "bounded_action": bounded_action,
        "expected_dispatch_ref": expected_dispatch_ref,
        "allowlisted_action": True,
        "operator_approved": True,
        "automatic_dispatch_allowed": False,
        "dispatch_authority_created": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def _build_mission_os_backend_action_receipt(
    *,
    cycle_index: int,
    request_ref: str,
    dispatch_ref: str | None,
    dispatch_status: str | None,
) -> dict[str, Any]:
    dispatch_observed = str(dispatch_ref or "").startswith(
        "px4_gazebo_emergency_command_dispatch_result:"
    )
    return {
        "schema_version": "mission_os_backend_action_receipt.v1",
        "receipt_id": _runtime_action_receipt_ref(cycle_index),
        "cycle_index": cycle_index,
        "action_request_ref": request_ref,
        "dispatch_ref": dispatch_ref,
        "dispatch_status": dispatch_status,
        "dispatch_observed": dispatch_observed,
        "backend_target": "px4_gazebo_sitl",
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def _build_mission_os_recovery_outcome_observation(
    *,
    cycle_index: int,
    action_receipt_ref: str,
    outcome_observation_ref: str | None,
    outcome_observed: bool,
    state_label: str | None,
    pose_z_m: float | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "mission_os_recovery_outcome_observation.v1",
        "observation_id": _runtime_outcome_ref(cycle_index),
        "cycle_index": cycle_index,
        "action_receipt_ref": action_receipt_ref,
        "outcome_observation_ref": outcome_observation_ref,
        "outcome_observed": outcome_observed,
        "state_label": state_label,
        "pose_z_m": pose_z_m,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def _build_mission_os_runtime_bridge(
    summary: dict[str, Any],
    *,
    checks: dict[str, bool],
    form3_observed: bool,
) -> dict[str, Any]:
    supervisor_loop = summary.get("mission_os_supervisor_recovery_loop")
    if isinstance(supervisor_loop, dict):
        cycles = supervisor_loop.get("cycles") or []
        decisions = [cycle.get("decision") for cycle in cycles if cycle.get("decision")]
        action_requests = [
            cycle.get("action_request")
            for cycle in cycles
            if cycle.get("action_request")
        ]
        action_receipts = [
            cycle.get("action_receipt")
            for cycle in cycles
            if cycle.get("action_receipt")
        ]
        outcome_observations = [
            cycle.get("outcome_observation")
            for cycle in cycles
            if cycle.get("outcome_observation")
        ]
        return {
            "schema_version": MISSION_OS_LOOP_SCHEMA_VERSION,
            "loop_id": "mission_os_recovery_runtime_bridge:wind_drift_form3",
            "decision_loop_driver": "mission_os_supervisor",
            "supervisor_scope": supervisor_loop.get("supervisor_scope"),
            "full_gateway_runtime_loop": False,
            "runtime_claim_scope": (
                "mission_os_supervisor_owned_wind_form3_sitl_recovery_loop"
            ),
            "primary_trigger": supervisor_loop.get("primary_trigger"),
            "assessment_mode": supervisor_loop.get("assessment_mode"),
            "cycle_count": 2 if form3_observed else 1,
            "same_session_decision_action_outcome_chain_observed": form3_observed,
            "supervisor_loop_claim_supported": bool(
                supervisor_loop.get("supervisor_loop_claim_supported")
                and form3_observed
            ),
            "conflicting_risks": supervisor_loop.get("conflicting_risks") or [],
            "mission_os_recovery_decisions": decisions,
            "mission_os_backend_action_requests": action_requests,
            "mission_os_backend_action_receipts": action_receipts,
            "mission_os_recovery_outcome_observations": outcome_observations,
            "cycles": [
                {
                    "cycle_index": cycle.get("cycle_index"),
                    "decision_ref": (cycle.get("decision") or {}).get("decision_id"),
                    "action_request_ref": (cycle.get("action_request") or {}).get(
                        "request_id"
                    ),
                    "action_receipt_ref": (cycle.get("action_receipt") or {}).get(
                        "receipt_id"
                    ),
                    "outcome_observation_ref": (
                        cycle.get("outcome_observation") or {}
                    ).get("observation_id"),
                    "qualifies_as_runtime_bridge_cycle": bool(
                        (cycle.get("outcome_observation") or {}).get(
                            "outcome_observed"
                        )
                    ),
                }
                for cycle in cycles
            ],
            "authority_boundary": supervisor_loop.get("authority_boundary")
            or {
                "ai_judgment_is_gate_verdict": False,
                "ai_judgment_created_dispatch_authority": False,
                "llm_gate_judge_used": False,
                "dispatch_authority_created": False,
                "delivery_completion_claimed": False,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            },
        }
    cycle1_assessment_inputs = _compound_assessment_inputs(
        summary,
        cycle_index=1,
        selected_bounded_action="rtl",
    )
    cycle2_assessment_inputs = _compound_assessment_inputs(
        summary,
        cycle_index=2,
        selected_bounded_action="land",
    )
    cycle1_decision = _build_mission_os_recovery_decision(
        cycle_index=1,
        observation_ref="route_deviation_observation:wind_drift",
        response_ref=RTL_RESPONSE_REF,
        selected_bounded_action="rtl",
        policy_gate_ref="policy_gate_result:wind_drift_bounded_rtl",
        operator_decision_ref="operator_decision:wind_drift_bounded_rtl",
        ai_situation_assessment_ref=None,
        assessment_inputs=cycle1_assessment_inputs,
    )
    cycle2_decision = _build_mission_os_recovery_decision(
        cycle_index=2,
        observation_ref=summary.get("recovery_completion_ref"),
        response_ref=LAND_RESPONSE_REF,
        selected_bounded_action="land",
        policy_gate_ref=POLICY_GATE_REF,
        operator_decision_ref=OPERATOR_DECISION_REF,
        ai_situation_assessment_ref=AI_ASSESSMENT_REF,
        assessment_inputs=cycle2_assessment_inputs,
    )
    cycle1_request = _build_mission_os_backend_action_request(
        cycle_index=1,
        decision_ref=cycle1_decision["decision_id"],
        bounded_action="rtl",
        expected_dispatch_ref=summary.get("recovery_dispatch_ref"),
    )
    cycle2_request = _build_mission_os_backend_action_request(
        cycle_index=2,
        decision_ref=cycle2_decision["decision_id"],
        bounded_action="land",
        expected_dispatch_ref=summary.get("post_recovery_dispatch_ref"),
    )
    cycle1_receipt = _build_mission_os_backend_action_receipt(
        cycle_index=1,
        request_ref=cycle1_request["request_id"],
        dispatch_ref=summary.get("recovery_dispatch_ref"),
        dispatch_status=summary.get("recovery_dispatch_status"),
    )
    cycle2_receipt = _build_mission_os_backend_action_receipt(
        cycle_index=2,
        request_ref=cycle2_request["request_id"],
        dispatch_ref=summary.get("post_recovery_dispatch_ref"),
        dispatch_status=summary.get("post_recovery_dispatch_status"),
    )
    cycle1_outcome = _build_mission_os_recovery_outcome_observation(
        cycle_index=1,
        action_receipt_ref=cycle1_receipt["receipt_id"],
        outcome_observation_ref=summary.get("recovery_completion_ref"),
        outcome_observed=checks["cycle1_bounded_rtl_action_outcome_observed"],
        state_label=summary.get("recovery_state_label"),
    )
    cycle2_outcome = _build_mission_os_recovery_outcome_observation(
        cycle_index=2,
        action_receipt_ref=cycle2_receipt["receipt_id"],
        outcome_observation_ref=summary.get("post_recovery_completion_ref"),
        outcome_observed=checks["cycle2_bounded_land_action_outcome_observed"],
        state_label=summary.get("post_recovery_state_label"),
        pose_z_m=_as_float(summary.get("post_recovery_pose_z_m")),
    )
    decisions = [cycle1_decision, cycle2_decision]
    action_requests = [cycle1_request, cycle2_request]
    action_receipts = [cycle1_receipt, cycle2_receipt]
    outcome_observations = [cycle1_outcome, cycle2_outcome]
    return {
        "schema_version": MISSION_OS_LOOP_SCHEMA_VERSION,
        "loop_id": "mission_os_recovery_runtime_bridge:wind_drift_form3",
        "decision_loop_driver": "scripted_mission_os_runtime_bridge",
        "full_gateway_runtime_loop": False,
        "runtime_claim_scope": (
            "same_session_decision_to_backend_action_bridge_not_full_gateway_loop"
        ),
        "primary_trigger": "wind_drift_exceeded_threshold",
        "assessment_mode": "compound_mission_state_assessment",
        "cycle_count": 2 if form3_observed else 1,
        "same_session_decision_action_outcome_chain_observed": form3_observed,
        "mission_os_recovery_decisions": decisions,
        "mission_os_backend_action_requests": action_requests,
        "mission_os_backend_action_receipts": action_receipts,
        "mission_os_recovery_outcome_observations": outcome_observations,
        "cycles": [
            {
                "cycle_index": 1,
                "decision_ref": cycle1_decision["decision_id"],
                "action_request_ref": cycle1_request["request_id"],
                "action_receipt_ref": cycle1_receipt["receipt_id"],
                "outcome_observation_ref": cycle1_outcome["observation_id"],
                "qualifies_as_runtime_bridge_cycle": checks[
                    "cycle1_bounded_rtl_action_outcome_observed"
                ],
            },
            {
                "cycle_index": 2,
                "decision_ref": cycle2_decision["decision_id"],
                "action_request_ref": cycle2_request["request_id"],
                "action_receipt_ref": cycle2_receipt["receipt_id"],
                "outcome_observation_ref": cycle2_outcome["observation_id"],
                "qualifies_as_runtime_bridge_cycle": checks[
                    "cycle2_bounded_land_action_outcome_observed"
                ],
            },
        ],
        "authority_boundary": {
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
            "llm_gate_judge_used": False,
            "dispatch_authority_created": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def _summarize_form3(
    run_dir: Path,
    *,
    expected_wind_mps: float,
    expected_direction_deg: float,
    drift_threshold_m: float,
) -> dict[str, Any]:
    summary = _read_json(_summary_path(run_dir))
    unsafe_flags = sorted(set(_nested_true_keys(summary, set(UNSAFE_AUTHORITY_KEYS))))
    ai_assessment = _build_ai_assessment(summary)
    policy_gate = _build_policy_gate_result(ai_assessment)
    operator_decision = _build_operator_decision_record(summary)
    source_refs = {
        "wind_application": "simulator_condition_application:mission_designer_wind_gust",
        "route_deviation_abort": summary.get("deviation_abort_ref"),
        "cycle1_recovery_dispatch": summary.get("recovery_dispatch_ref"),
        "cycle1_recovery_completion": summary.get("recovery_completion_ref"),
        "cycle2_recovery_dispatch": summary.get("post_recovery_dispatch_ref"),
        "cycle2_recovery_completion": summary.get("post_recovery_completion_ref"),
    }
    backend_context = build_form3_backend_context(
        summary,
        applicator_chain_refs=[str(source_refs["wind_application"])],
        verifier_version=SCHEMA_VERSION,
        audit_script_version="scripts/audit_mission_designer_wind_drift_form3_closed_loop.py",
    )
    parameter_observations = [
        parameter_observation(
            parameter="wind_speed_mps",
            value=expected_wind_mps,
            unit="m/s",
            source_ref=str(source_refs["wind_application"]),
        ),
        parameter_observation(
            parameter="wind_direction_deg",
            value=expected_direction_deg,
            unit="deg",
            source_ref=str(source_refs["wind_application"]),
        ),
        parameter_observation(
            parameter="wind_drift_threshold_m",
            value=drift_threshold_m,
            unit="m",
            source_ref="route_deviation_threshold:wind_drift_form3",
        ),
    ]
    parameterized_sdf_delta_proof = build_wind_parameterized_sdf_delta_proof(
        summary,
        wind_speed_mps=expected_wind_mps,
        wind_direction_deg=expected_direction_deg,
        drift_threshold_m=drift_threshold_m,
        source_ref=str(source_refs["wind_application"]),
    )
    supervisor_loop = summary.get("mission_os_supervisor_recovery_loop")
    supervisor_loop_present = isinstance(supervisor_loop, dict)
    supervisor_conflicting_risks = (
        supervisor_loop.get("conflicting_risks") if supervisor_loop_present else []
    ) or []
    checks = {
        "horizontal_route_smoke_observed": summary.get(
            "actual_px4_gazebo_horizontal_smoke_observed"
        )
        is True,
        "wind_application_source_bound": _wind_application_source_bound(
            summary,
            expected_wind_mps=expected_wind_mps,
            expected_direction_deg=expected_direction_deg,
        ),
        "wind_drift_observed": _wind_drift_observed(
            summary, drift_threshold_m=drift_threshold_m
        ),
        "cycle1_bounded_rtl_action_outcome_observed": _rtl_recovery_observed(
            summary
        ),
        "cycle2_bounded_land_action_outcome_observed": (
            _post_recovery_land_observed(summary)
        ),
        "source_refs_observed": _source_refs_observed(summary),
        "post_recovery_refs_observed": str(
            summary.get("post_recovery_dispatch_ref", "")
        ).startswith("px4_gazebo_emergency_command_dispatch_result:")
        and str(summary.get("post_recovery_completion_ref", "")).startswith(
            "px4_gazebo_route_recovery_completion:"
        ),
        "dropoff_not_claimed": summary.get("dropoff_region_reached") is False
        and summary.get("dropoff_verified") is False
        and summary.get("delivery_completion_claimed") is False,
        "top_level_hardware_physical_false": summary.get("hardware_target_allowed")
        is False
        and summary.get("physical_execution_invoked") is False,
        "unsafe_authority_flags_absent": not unsafe_flags,
        "mission_os_supervisor_loop_observed_if_requested": (
            summary.get("decision_loop_driver") != "mission_os_supervisor"
            or (
                supervisor_loop_present
                and supervisor_loop.get("decision_loop_driver")
                == "mission_os_supervisor"
                and supervisor_loop.get("supervisor_scope") == "wind_form3_sitl_only"
                and supervisor_loop.get("full_gateway_runtime_loop") is False
                and supervisor_loop.get("supervisor_loop_claim_supported") is True
            )
        ),
        "mission_os_supervisor_conflicting_risks_absent": (
            not supervisor_conflicting_risks
        ),
    }
    missing = [name for name, passed in checks.items() if not passed]
    form3_observed = not missing
    mission_os_runtime_bridge = _build_mission_os_runtime_bridge(
        summary,
        checks=checks,
        form3_observed=form3_observed,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_id": (
            "mission_designer_wind_drift_form3_closed_loop_audit:"
            "mission_designer_wind_speed"
        ),
        "condition_kind": "source_bound_wind_drift_form3_closed_loop",
        "causal_form": "Form 3" if form3_observed else "Form 0b",
        "audit_status": "form3_observed" if form3_observed else "unsupported",
        "form3_claim_supported": form3_observed,
        "progress_counted": form3_observed,
        "source_bound": form3_observed,
        "mission_contract_ref": MISSION_DESIGNER_FORM3_MISSION_CONTRACT_REF,
        "task_graph_ref": MISSION_DESIGNER_FORM3_TASK_GRAPH_REF,
        "source_backend_type": MISSION_DESIGNER_FORM3_SOURCE_BACKEND_TYPE,
        "backend_context": backend_context,
        "parameter_observations": parameter_observations,
        "parameterized_sdf_delta_proof": parameterized_sdf_delta_proof,
        "cycle_count": 2 if form3_observed else 1,
        "artifact_dir": str(run_dir),
        "checks": checks,
        "unsupported_reasons": [
            f"{name}_not_observed"
            for name in missing
            if name != "unsafe_authority_flags_absent"
        ]
        + (["source_run_forbidden_authority_flags_observed"] if unsafe_flags else []),
        "cycles": [
            {
                "cycle_index": 1,
                "observation_ref": "route_deviation_observation:wind_drift",
                "response_ref": RTL_RESPONSE_REF,
                "bounded_action_ref": summary.get("recovery_dispatch_ref"),
                "re_observation_ref": summary.get("recovery_completion_ref"),
                "mission_response_kind": "action",
                "form2_subtype": "Form 2a",
                "qualifies_as_form3_cycle": checks[
                    "cycle1_bounded_rtl_action_outcome_observed"
                ],
            },
            {
                "cycle_index": 2,
                "observation_ref": summary.get("recovery_completion_ref"),
                "ai_situation_assessment_ref": AI_ASSESSMENT_REF,
                "policy_gate_ref": POLICY_GATE_REF,
                "operator_decision_ref": OPERATOR_DECISION_REF,
                "response_ref": LAND_RESPONSE_REF,
                "bounded_action_ref": summary.get("post_recovery_dispatch_ref"),
                "re_observation_ref": summary.get("post_recovery_completion_ref"),
                "mission_response_kind": "action",
                "form2_subtype": "Form 2a",
                "qualifies_as_form3_cycle": checks[
                    "cycle2_bounded_land_action_outcome_observed"
                ],
            },
        ],
        "cycle2_ai_situation_assessment": ai_assessment,
        "cycle2_policy_gate_result": policy_gate,
        "cycle2_operator_decision_record": operator_decision,
        "mission_os_runtime_decision_loop": mission_os_runtime_bridge,
        "observed": {
            "wind_drift_deviation_xy_m": (
                summary.get("deviation_samples", [{}])[0].get("deviation_xy_m")
                if summary.get("deviation_samples")
                else None
            ),
            "recovery_action_taken": summary.get("recovery_action_taken"),
            "recovery_state_label": summary.get("recovery_state_label"),
            "post_recovery_action_taken": summary.get("post_recovery_action_taken"),
            "post_recovery_pose_z_m": summary.get("post_recovery_pose_z_m"),
            "final_status": summary.get("final_status"),
            "task_status": summary.get("task_status"),
            "unsafe_authority_flags_observed": unsafe_flags,
        },
        "source_refs": source_refs,
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _run_horizontal_route_smoke(
    *,
    wind_mps: float,
    wind_direction_deg: float,
    drift_threshold_m: float,
    artifact_root: Path,
    mission_os_supervisor_recovery_loop: bool = False,
) -> Path:
    env = os.environ.copy()
    env.update(
        {
            "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE": "1",
            "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT": str(artifact_root),
            "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS": str(wind_mps),
            "MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG": str(wind_direction_deg),
        }
    )
    env.pop("MISSION_DESIGNER_REALISM_WIND_GUST_MPS", None)
    env.pop("MISSION_DESIGNER_REALISM_WIND_VARIANCE", None)
    command = [
        sys.executable,
        "scripts/smoke_px4_gazebo_horizontal_route_delivery.py",
        "--on-deviation-action",
        "rtl",
        "--post-recovery-action",
        "land",
        "--max-pose-deviation-xy-m",
        str(drift_threshold_m),
    ]
    if mission_os_supervisor_recovery_loop:
        command.append("--mission-os-supervisor-recovery-loop")
    result = subprocess.run(
        command,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=420,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "horizontal route wind drift Form 3 smoke failed: "
            f"rc={result.returncode}\n"
            f"stdout_tail={result.stdout[-2000:]}\n"
            f"stderr_tail={result.stderr[-2000:]}"
        )
    try:
        summary = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "horizontal route smoke did not emit JSON summary: "
            f"{result.stdout[-2000:]}"
        ) from exc
    run_dir = Path(summary["artifact_dir"])
    if not run_dir.exists():
        raise FileNotFoundError(f"reported artifact_dir does not exist: {run_dir}")
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit strict wind-drift Form 3 closed-loop behavior."
    )
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--wind-mps", type=float, default=DEFAULT_WIND_MPS)
    parser.add_argument(
        "--wind-direction-deg", type=float, default=DEFAULT_WIND_DIRECTION_DEG
    )
    parser.add_argument(
        "--drift-threshold-m", type=float, default=DEFAULT_DRIFT_THRESHOLD_M
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    parser.add_argument(
        "--mission-os-supervisor-recovery-loop",
        action="store_true",
        help=(
            "When executing a new run, drive wind RTL -> LAND recovery through "
            "the scoped Mission OS supervisor loop instead of the scripted "
            "bridge."
        ),
    )
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_dir = args.output_dir / f"wind_drift_form3_closed_loop_{stamp}"
    audit_dir.mkdir(parents=True, exist_ok=False)
    run_mode = "existing_run" if args.run_dir else "executed_run"
    try:
        run_dir = args.run_dir or _run_horizontal_route_smoke(
            wind_mps=args.wind_mps,
            wind_direction_deg=args.wind_direction_deg,
            drift_threshold_m=args.drift_threshold_m,
            artifact_root=audit_dir / "runs" / "wind_drift_form3",
            mission_os_supervisor_recovery_loop=(
                args.mission_os_supervisor_recovery_loop
            ),
        )
        artifact = _summarize_form3(
            run_dir,
            expected_wind_mps=args.wind_mps,
            expected_direction_deg=args.wind_direction_deg,
            drift_threshold_m=args.drift_threshold_m,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        partial_run_dir = args.run_dir or _latest_partial_run_dir(
            audit_dir / "runs" / "wind_drift_form3"
        )
        if partial_run_dir is None:
            raise
        artifact = _build_partial_run_artifact(
            partial_run_dir,
            audit_dir=audit_dir,
            run_mode=run_mode,
            smoke_error=str(exc),
        )
    artifact["audit_dir"] = str(audit_dir)
    artifact["run_mode"] = run_mode
    output_path = audit_dir / "mission_designer_wind_drift_form3_closed_loop.json"
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact["form3_claim_supported"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
