#!/usr/bin/env python3
"""Audit obstacle alternate-route recovery with strict two-cycle Form 3.

Cycle 1 consumes a source-bound route-blocking obstacle and requires the
operator-approved alternate route to reach the alternate waypoint. Cycle 2 then
consumes that alternate-waypoint observation, records assessment / policy /
operator artifacts, dispatches a separate bounded LAND action, and verifies the
LAND action outcome.

The alternate mission upload may contain a LAND item, but that belongs to the
cycle 1 dispatch chain. Strict Form 3 requires a distinct cycle 2 dispatch and
action-outcome observation after the alternate waypoint has been observed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from scripts.audit_mission_designer_obstacle_alternate_route_closed_loop import (
    ALTERNATE_ROUTE_DISPATCH_REF,
    DEFAULT_PROGRESS_THRESHOLD_M,
    DEFAULT_WAYPOINT_THRESHOLD_M,
    OBSTACLE_PLACEMENT_ENV_BY_ARG,
    UNSAFE_AUTHORITY_KEYS,
    _default_obstacle_placement,
    _nested_true_keys,
    _obstacle_placement_from_args,
    _read_json,
    _run_horizontal_route_smoke_with_env,
    _summarize_closed_loop,
    _summary_artifact,
    _summary_path,
    _write_json,
)
from scripts.diagnose_mission_designer_obstacle_form3_partial_run import (
    build_diagnostic,
)
from scripts.mission_designer_form3_envelope_source import (
    MISSION_DESIGNER_FORM3_MISSION_CONTRACT_REF,
    MISSION_DESIGNER_FORM3_SOURCE_BACKEND_TYPE,
    MISSION_DESIGNER_FORM3_TASK_GRAPH_REF,
    build_form3_backend_context,
    parameter_observation,
)


SCHEMA_VERSION = "mission_designer_obstacle_true_form3_closed_loop_audit.v1"
ALT_ROUTE_RESPONSE_REF = "mission_response_candidate:obstacle_route_blocking_alternate_route"
LAND_RESPONSE_REF = "mission_response_candidate:obstacle_alternate_waypoint_bounded_land"
POLICY_GATE_REF = "policy_gate_result:obstacle_alternate_waypoint_bounded_land"
OPERATOR_DECISION_REF = "operator_decision:obstacle_alternate_waypoint_bounded_land"
AI_ASSESSMENT_REF = "ai_mission_situation_assessment:obstacle_alternate_waypoint_bounded_land"
POST_ALTERNATE_LAND_ACTION_REF = "bounded_action:obstacle_alternate_waypoint_land"
ROUTE_BLOCKING_VERIFICATION_REF = (
    "route_blocking_verification:mission_designer_collision_obstacle"
)
OBSTACLE_SUPERVISOR_PRIMARY_TRIGGER = "route_blocking_obstacle_verified"


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


def _top_level_false_or_absent(summary: dict[str, Any], key: str) -> bool:
    return summary.get(key) is not True


def _cycle2_post_alternate_land_observed(summary: dict[str, Any]) -> bool:
    dispatch = _summary_artifact(summary, "alternate_landing_command_dispatch")
    behavior = _summary_artifact(summary, "alternate_landing_behavior_observation")
    outcome = _summary_artifact(summary, "alternate_landing_outcome")
    final_pose = behavior.get("final_pose_xyz_m") or []
    final_z = None
    if isinstance(final_pose, list) and len(final_pose) >= 3:
        try:
            final_z = float(final_pose[2])
        except (TypeError, ValueError):
            final_z = None
    return (
        dispatch.get("schema_version") == "alternate_landing_command_dispatch.v1"
        and dispatch.get("dispatch_id")
        == "alternate_landing_command_dispatch:mission_designer_route_blocking"
        and str(dispatch.get("emergency_dispatch_ref", "")).startswith(
            "px4_gazebo_emergency_command_dispatch_result:"
        )
        and str(dispatch.get("approval_ref", "")).startswith(
            "px4_gazebo_emergency_command_approval:"
        )
        and dispatch.get("command_name") == "MAV_CMD_NAV_LAND"
        and int(dispatch.get("command_id", -1)) == 21
        and dispatch.get("mavlink_dispatch_performed") is True
        and dispatch.get("bounded_allowlist_enforced") is True
        and dispatch.get("approval_free_dispatch_allowed") is False
        and dispatch.get("px4_route_changed") is False
        and dispatch.get("rth_commanded") is False
        and dispatch.get("hardware_target_allowed") is False
        and dispatch.get("physical_execution_invoked") is False
        and dispatch.get("delivery_completion_claimed") is False
        and behavior.get("schema_version")
        == "alternate_landing_behavior_observation.v1"
        and behavior.get("observation_id")
        == "alternate_landing_behavior_observation:mission_designer_route_blocking"
        and behavior.get("alternate_landing_behavior_observed") is True
        and behavior.get("land_commanded") is True
        and behavior.get("landing_observed") is True
        and final_z is not None
        and final_z <= 0.15
        and behavior.get("px4_route_changed") is False
        and behavior.get("task_status_mutated") is False
        and behavior.get("gate_status_mutated") is False
        and behavior.get("hardware_target_allowed") is False
        and behavior.get("physical_execution_invoked") is False
        and behavior.get("delivery_completion_claimed") is False
        and outcome.get("schema_version") == "alternate_landing_outcome.v1"
        and outcome.get("outcome_status") == "alternate_landing_behavior_observed"
        and outcome.get("alternate_landing_behavior_observed") is True
        and outcome.get("delivery_completion_claimed") is False
        and outcome.get("hardware_target_allowed") is False
        and outcome.get("physical_execution_invoked") is False
    )


def _cycle2_dispatch_chain_distinct(summary: dict[str, Any]) -> bool:
    cycle1_dispatch_artifact = _summary_artifact(
        summary, "alternate_route_command_dispatch"
    )
    route_evidence = _summary_artifact(summary, "alternate_route_execution_evidence")
    route_observed = route_evidence.get("observed")
    route_observed = route_observed if isinstance(route_observed, dict) else {}
    cycle2_dispatch_artifact = _summary_artifact(
        summary, "alternate_landing_command_dispatch"
    )
    cycle1_dispatch = (
        cycle1_dispatch_artifact.get("dispatch_id")
        or route_observed.get("alternate_route_command_dispatch_ref")
    )
    cycle2_dispatch = cycle2_dispatch_artifact.get("dispatch_id")
    cycle2_emergency_dispatch = cycle2_dispatch_artifact.get("emergency_dispatch_ref")
    return (
        cycle1_dispatch == ALTERNATE_ROUTE_DISPATCH_REF
        and cycle2_dispatch
        == "alternate_landing_command_dispatch:mission_designer_route_blocking"
        and cycle2_dispatch != cycle1_dispatch
        and isinstance(cycle2_emergency_dispatch, str)
        and cycle2_emergency_dispatch.startswith(
            "px4_gazebo_emergency_command_dispatch_result:"
        )
    )


def _build_ai_assessment(summary: dict[str, Any]) -> dict[str, Any]:
    route_evidence = _summary_artifact(summary, "alternate_route_execution_evidence")
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
        "source_observation_ref": route_evidence.get("evidence_id"),
        "mission_state_interpretation": (
            "alternate_waypoint_reached_original_dropoff_unverified"
        ),
        "assessment_summary": (
            "A source-bound obstacle blocked the route and the operator-approved "
            "alternate route reached its alternate waypoint. The original "
            "dropoff remains unverified, so a separate operator-approved "
            "bounded LAND action is the selected cycle 2 response. This "
            "assessment is mission evidence only, not gate authority."
        ),
        "mission_response_candidate": "bounded_land",
        "candidate_confidence": "medium",
        "uncertainty_reasons": [
            "original_dropoff_unverified",
            "alternate_waypoint_reached_is_not_delivery_completion",
            "post_alternate_action_requires_operator_approval",
        ],
        "operator_question": (
            "Alternate waypoint was reached but original dropoff remains "
            "unverified. Approve a separate bounded LAND action and verify "
            "landing outcome?"
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
        "gate_id": "policy_gate:obstacle_alternate_waypoint_bounded_land",
        "cycle_index": 2,
        "input_response_candidate_ref": LAND_RESPONSE_REF,
        "ai_situation_assessment_ref": AI_ASSESSMENT_REF,
        "ai_assessment_required": True,
        "ai_proposal_confidence": assessment["candidate_confidence"],
        "gate_status": "pass",
        "gate_decision_basis": [
            "source_bound_obstacle_route_blocking_observed",
            "alternate_waypoint_reached_observed",
            "original_dropoff_unverified",
            "operator_approval_required",
            "bounded_land_allowlisted",
            "cycle2_dispatch_chain_distinct_from_cycle1",
        ],
        "operator_review_required": True,
        "automatic_dispatch_allowed": False,
        "operator_approved_dispatch_allowed": True,
        "bounded_action_dispatch_allowed": True,
        "allowed_actions": ["bounded_land"],
        "forbidden_actions": [
            "delivery_completion_claim",
            "approval_free_dispatch",
            "reuse_cycle1_dispatch_as_cycle2_evidence",
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
    dispatch = _summary_artifact(summary, "alternate_landing_command_dispatch")
    behavior = _summary_artifact(summary, "alternate_landing_behavior_observation")
    return {
        "schema_version": "operator_decision_record.v1",
        "decision_id": OPERATOR_DECISION_REF,
        "cycle_index": 2,
        "advisory_ref": LAND_RESPONSE_REF,
        "ai_situation_assessment_ref": AI_ASSESSMENT_REF,
        "operator_question": (
            "Alternate waypoint was reached but original dropoff is unverified. "
            "Approve separate bounded LAND?"
        ),
        "operator_question_options": [
            "approve_bounded_land",
            "hold",
            "mission_abort",
            "request_new_bounded_route",
        ],
        "selected_option": "approve_bounded_land",
        "selection_status": "operator_approved",
        "selected_bounded_action": "land",
        "created_dispatch_authority": False,
        "bounded_action_ref": POST_ALTERNATE_LAND_ACTION_REF,
        "dispatch_ref": dispatch.get("emergency_dispatch_ref"),
        "action_outcome_observation_ref": behavior.get("observation_id"),
    }


def _cycle_ref_chain_supported(
    cycle: dict[str, Any],
    *,
    cycle_index: int,
    expected_source_observation_ref: str,
    expected_response_ref: str,
    expected_primary_trigger: str,
    expected_action: str,
    expected_dispatch_ref: str,
    expected_approval_ref: str,
    expected_outcome_ref: str,
) -> bool:
    if not isinstance(cycle, dict):
        return False
    decision = cycle.get("decision")
    request = cycle.get("action_request")
    receipt = cycle.get("action_receipt")
    outcome = cycle.get("outcome_observation")
    if not all(
        isinstance(artifact, dict)
        for artifact in (decision, request, receipt, outcome)
    ):
        return False
    decision_id = decision.get("decision_id")
    request_id = request.get("request_id")
    receipt_id = receipt.get("receipt_id")
    outcome_id = outcome.get("observation_id")
    return (
        cycle.get("cycle_index") == cycle_index
        and cycle.get("decision_ref") == decision_id
        and cycle.get("action_request_ref") == request_id
        and cycle.get("action_receipt_ref") == receipt_id
        and cycle.get("outcome_observation_ref") == outcome_id
        and decision.get("schema_version") == "mission_os_recovery_decision.v1"
        and decision.get("cycle_index") == cycle_index
        and decision.get("decision_loop_driver") == "mission_os_supervisor"
        and decision.get("supervisor_scope") == "obstacle_form3_sitl_only"
        and decision.get("full_gateway_runtime_loop") is False
        and decision.get("source_observation_ref") == expected_source_observation_ref
        and decision.get("mission_response_candidate_ref") == expected_response_ref
        and decision.get("primary_trigger") == expected_primary_trigger
        and decision.get("selected_bounded_action") == expected_action
        and decision.get("operator_approval_required") is True
        and decision.get("automatic_dispatch_allowed") is False
        and decision.get("operator_approved_dispatch_allowed") is True
        and decision.get("ai_judgment_is_gate_verdict") is False
        and decision.get("ai_judgment_created_dispatch_authority") is False
        and decision.get("llm_gate_judge_used") is False
        and decision.get("created_dispatch_authority") is False
        and decision.get("delivery_completion_claimed") is False
        and decision.get("hardware_target_allowed") is False
        and decision.get("physical_execution_invoked") is False
        and request.get("schema_version") == "mission_os_backend_action_request.v1"
        and request.get("cycle_index") == cycle_index
        and request.get("decision_ref") == decision_id
        and request.get("backend_target") == "px4_gazebo_sitl"
        and request.get("bounded_action") == expected_action
        and request.get("expected_dispatch_ref") == expected_dispatch_ref
        and request.get("approval_ref") == expected_approval_ref
        and request.get("allowlisted_action") is True
        and request.get("operator_approved") is True
        and request.get("automatic_dispatch_allowed") is False
        and request.get("dispatch_authority_created") is False
        and request.get("hardware_target_allowed") is False
        and request.get("physical_execution_invoked") is False
        and receipt.get("schema_version") == "mission_os_backend_action_receipt.v1"
        and receipt.get("cycle_index") == cycle_index
        and receipt.get("action_request_ref") == request_id
        and receipt.get("dispatch_ref") == expected_dispatch_ref
        and receipt.get("dispatch_observed") is True
        and receipt.get("backend_target") == "px4_gazebo_sitl"
        and receipt.get("hardware_target_allowed") is False
        and receipt.get("physical_execution_invoked") is False
        and outcome.get("schema_version")
        == "mission_os_recovery_outcome_observation.v1"
        and outcome.get("cycle_index") == cycle_index
        and outcome.get("action_receipt_ref") == receipt_id
        and outcome.get("outcome_observation_ref") == expected_outcome_ref
        and outcome.get("outcome_observed") is True
        and outcome.get("delivery_completion_claimed") is False
        and outcome.get("hardware_target_allowed") is False
        and outcome.get("physical_execution_invoked") is False
    )


def _supervisor_authority_boundary_safe(supervisor_loop: dict[str, Any]) -> bool:
    authority = supervisor_loop.get("authority_boundary") or {}
    if not isinstance(authority, dict):
        return False
    return (
        authority.get("ai_judgment_is_gate_verdict") is False
        and authority.get("ai_judgment_created_dispatch_authority") is False
        and authority.get("llm_gate_judge_used") is False
        and authority.get("dispatch_authority_created") is False
        and authority.get("delivery_completion_claimed") is False
        and authority.get("hardware_target_allowed") is False
        and authority.get("physical_execution_invoked") is False
    )


def _supervisor_loop_supported(
    supervisor_loop: dict[str, Any],
    *,
    summary: dict[str, Any],
) -> bool:
    route_evidence = _summary_artifact(summary, "alternate_route_execution_evidence")
    route_dispatch = _summary_artifact(summary, "alternate_route_command_dispatch")
    landing_dispatch = _summary_artifact(summary, "alternate_landing_command_dispatch")
    landing_behavior = _summary_artifact(
        summary, "alternate_landing_behavior_observation"
    )
    cycles = supervisor_loop.get("cycles") or []
    if not isinstance(cycles, list) or len(cycles) != 2:
        return False
    if not all(isinstance(cycle, dict) for cycle in cycles):
        return False
    cycles_by_index = {cycle.get("cycle_index"): cycle for cycle in cycles}
    cycle1 = cycles_by_index.get(1) or {}
    cycle2 = cycles_by_index.get(2) or {}
    cycle1_dispatch_ref = route_dispatch.get("dispatch_id") or ALTERNATE_ROUTE_DISPATCH_REF
    cycle1_outcome_ref = (
        route_evidence.get("evidence_id")
        or "alternate_route_execution_evidence:mission_designer_route_blocking"
    )
    cycle2_dispatch_ref = landing_dispatch.get("emergency_dispatch_ref")
    cycle1_approval_ref = route_dispatch.get("approval_ref")
    cycle2_approval_ref = landing_dispatch.get("approval_ref")
    cycle2_outcome_ref = landing_behavior.get("observation_id")
    return (
        supervisor_loop.get("schema_version") == "mission_os_supervisor_recovery_loop.v1"
        and supervisor_loop.get("decision_loop_driver") == "mission_os_supervisor"
        and supervisor_loop.get("supervisor_scope") == "obstacle_form3_sitl_only"
        and supervisor_loop.get("full_gateway_runtime_loop") is False
        and supervisor_loop.get("primary_trigger") == OBSTACLE_SUPERVISOR_PRIMARY_TRIGGER
        and supervisor_loop.get("cycle_count") == 2
        and supervisor_loop.get("supervisor_loop_claim_supported") is True
        and _supervisor_authority_boundary_safe(supervisor_loop)
        and supervisor_loop.get("cycle1_supervisor_decision_observed") is True
        and supervisor_loop.get("cycle1_backend_action_request_observed") is True
        and supervisor_loop.get("cycle1_backend_action_receipt_observed") is True
        and supervisor_loop.get("cycle1_outcome_observation_observed") is True
        and supervisor_loop.get("cycle2_supervisor_decision_observed") is True
        and supervisor_loop.get("cycle2_backend_action_request_observed") is True
        and supervisor_loop.get("cycle2_backend_action_receipt_observed") is True
        and supervisor_loop.get("cycle2_outcome_observation_observed") is True
        and _cycle_ref_chain_supported(
            cycle1,
            cycle_index=1,
            expected_source_observation_ref=ROUTE_BLOCKING_VERIFICATION_REF,
            expected_response_ref=ALT_ROUTE_RESPONSE_REF,
            expected_primary_trigger=OBSTACLE_SUPERVISOR_PRIMARY_TRIGGER,
            expected_action="alternate_route",
            expected_dispatch_ref=cycle1_dispatch_ref,
            expected_approval_ref=cycle1_approval_ref,
            expected_outcome_ref=cycle1_outcome_ref,
        )
        and _cycle_ref_chain_supported(
            cycle2,
            cycle_index=2,
            expected_source_observation_ref=cycle1_outcome_ref,
            expected_response_ref=LAND_RESPONSE_REF,
            expected_primary_trigger=OBSTACLE_SUPERVISOR_PRIMARY_TRIGGER,
            expected_action="land",
            expected_dispatch_ref=cycle2_dispatch_ref,
            expected_approval_ref=cycle2_approval_ref,
            expected_outcome_ref=cycle2_outcome_ref,
        )
    )


def _summarize_form3(
    run_dir: Path,
    *,
    progress_threshold_m: float,
    waypoint_threshold_m: float,
    expected_obstacle_placement: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    summary = _read_json(_summary_path(run_dir))
    closed_loop = _summarize_closed_loop(
        run_dir,
        progress_threshold_m=progress_threshold_m,
        waypoint_threshold_m=waypoint_threshold_m,
        expected_obstacle_placement=expected_obstacle_placement,
    )
    unsafe_flags = sorted(set(_nested_true_keys(summary, set(UNSAFE_AUTHORITY_KEYS))))
    ai_assessment = _build_ai_assessment(summary)
    policy_gate = _build_policy_gate_result(ai_assessment)
    operator_decision = _build_operator_decision_record(summary)
    supervisor_loop = summary.get("mission_os_supervisor_recovery_loop")
    supervisor_loop_artifact_present = supervisor_loop is not None
    supervisor_loop_present = isinstance(supervisor_loop, dict)
    supervisor_loop_requested_or_present = (
        summary.get("decision_loop_driver") == "mission_os_supervisor"
        or supervisor_loop_artifact_present
    )
    supervisor_conflicting_risks = (
        supervisor_loop.get("conflicting_risks") if supervisor_loop_present else []
    ) or []
    placement = expected_obstacle_placement or _default_obstacle_placement()
    source_refs = {
        "obstacle_closed_loop_audit": closed_loop.get("audit_id"),
        "obstacle_application": closed_loop.get("source_refs", {}).get(
            "obstacle_application"
        ),
        "route_blocking_verification": closed_loop.get("source_refs", {}).get(
            "route_blocking_verification"
        ),
        "cycle1_alternate_route_dispatch": ALTERNATE_ROUTE_DISPATCH_REF,
        "cycle1_alternate_route_outcome": (
            _summary_artifact(summary, "alternate_route_execution_evidence").get(
                "evidence_id"
            )
        ),
        "cycle2_post_alternate_land_dispatch": (
            _summary_artifact(summary, "alternate_landing_command_dispatch").get(
                "emergency_dispatch_ref"
            )
        ),
        "cycle2_post_alternate_land_outcome": (
            _summary_artifact(summary, "alternate_landing_behavior_observation").get(
                "observation_id"
            )
        ),
    }
    obstacle_application_ref = str(source_refs["obstacle_application"] or "")
    backend_context = build_form3_backend_context(
        summary,
        applicator_chain_refs=[obstacle_application_ref],
        verifier_version=SCHEMA_VERSION,
        audit_script_version="scripts/audit_mission_designer_obstacle_true_form3_closed_loop.py",
    )
    parameter_observations = [
        parameter_observation(
            parameter="obstacle_start_x_m",
            value=placement["start_xy_m"][0],
            unit="m",
            source_ref=obstacle_application_ref,
        ),
        parameter_observation(
            parameter="obstacle_start_y_m",
            value=placement["start_xy_m"][1],
            unit="m",
            source_ref=obstacle_application_ref,
        ),
        parameter_observation(
            parameter="obstacle_end_x_m",
            value=placement["end_xy_m"][0],
            unit="m",
            source_ref=obstacle_application_ref,
        ),
        parameter_observation(
            parameter="obstacle_end_y_m",
            value=placement["end_xy_m"][1],
            unit="m",
            source_ref=obstacle_application_ref,
        ),
        parameter_observation(
            parameter="alternate_waypoint_threshold_m",
            value=waypoint_threshold_m,
            unit="m",
            source_ref="route_observation_threshold:obstacle_true_form3",
        ),
    ]
    checks = {
        "cycle1_alternate_route_action_outcome_observed": closed_loop.get(
            "closed_loop_observed"
        )
        is True,
        "cycle2_observation_consumes_cycle1_outcome": (
            _summary_artifact(summary, "alternate_route_execution_evidence").get(
                "alternate_waypoint_reached_observed"
            )
            is True
        ),
        "cycle2_response_selected": operator_decision["selected_option"]
        == "approve_bounded_land",
        "cycle2_dispatch_chain_distinct_from_cycle1": _cycle2_dispatch_chain_distinct(
            summary
        ),
        "cycle2_bounded_land_action_outcome_observed": (
            _cycle2_post_alternate_land_observed(summary)
        ),
        "policy_gate_result_artifact_observed": (
            policy_gate["schema_version"] == "policy_gate_result.v1"
            and policy_gate["gate_status"] == "pass"
            and policy_gate["automatic_dispatch_allowed"] is False
            and policy_gate["operator_approved_dispatch_allowed"] is True
            and policy_gate["bounded_action_dispatch_allowed"] is True
        ),
        "operator_decision_record_artifact_observed": (
            operator_decision["schema_version"] == "operator_decision_record.v1"
            and operator_decision["selection_status"] == "operator_approved"
            and operator_decision["bounded_action_ref"]
            == POST_ALTERNATE_LAND_ACTION_REF
            and bool(operator_decision["dispatch_ref"])
            and bool(operator_decision["action_outcome_observation_ref"])
        ),
        "dropoff_not_claimed": summary.get("dropoff_region_reached") is False
        and _top_level_false_or_absent(summary, "dropoff_verified")
        and _top_level_false_or_absent(summary, "delivery_completion_claimed"),
        "top_level_hardware_physical_false": summary.get("hardware_target_allowed")
        is False
        and summary.get("physical_execution_invoked") is False,
        "unsafe_authority_flags_absent": not unsafe_flags,
        "mission_os_supervisor_top_level_scope_observed": (
            not supervisor_loop_requested_or_present
            or (
                summary.get("decision_loop_driver") == "mission_os_supervisor"
                and summary.get("supervisor_scope") == "obstacle_form3_sitl_only"
                and summary.get("full_gateway_runtime_loop") is False
                and summary.get("primary_trigger") == OBSTACLE_SUPERVISOR_PRIMARY_TRIGGER
            )
        ),
        "mission_os_supervisor_loop_observed_if_requested": (
            not supervisor_loop_requested_or_present
            or (
                supervisor_loop_present
                and _supervisor_loop_supported(supervisor_loop, summary=summary)
            )
        ),
        "mission_os_supervisor_conflicting_risks_absent": (
            not supervisor_conflicting_risks
        ),
    }
    missing = [name for name, passed in checks.items() if not passed]
    form3_observed = not missing
    alternate_route_evidence = _summary_artifact(
        summary, "alternate_route_execution_evidence"
    )
    alternate_landing_behavior = _summary_artifact(
        summary, "alternate_landing_behavior_observation"
    )
    alternate_landing_dispatch = _summary_artifact(
        summary, "alternate_landing_command_dispatch"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_id": (
            "mission_designer_obstacle_true_form3_closed_loop_audit:"
            "mission_designer_collision_obstacle"
        ),
        "condition_kind": "source_bound_obstacle_true_form3_closed_loop",
        "causal_form": "Form 3" if form3_observed else "Form 0b",
        "audit_status": "form3_observed" if form3_observed else "unsupported",
        "form3_claim_supported": form3_observed,
        "form3_candidate": not form3_observed and closed_loop.get("closed_loop_observed") is True,
        "progress_counted": form3_observed,
        "source_bound": form3_observed,
        "mission_contract_ref": MISSION_DESIGNER_FORM3_MISSION_CONTRACT_REF,
        "task_graph_ref": MISSION_DESIGNER_FORM3_TASK_GRAPH_REF,
        "source_backend_type": MISSION_DESIGNER_FORM3_SOURCE_BACKEND_TYPE,
        "backend_context": backend_context,
        "parameter_observations": parameter_observations,
        "cycle_count": 2 if form3_observed else 1,
        "candidate_cycle_count": 2,
        "artifact_dir": str(run_dir),
        "checks": checks,
        "unsupported_reasons": [
            f"{name}_not_observed"
            for name in missing
            if name != "unsafe_authority_flags_absent"
        ]
        + (["source_run_forbidden_authority_flags_observed"] if unsafe_flags else []),
        "missing_form3_requirements": [
            name
            for name in (
                "cycle2_dispatch_chain_distinct_from_cycle1",
                "cycle2_bounded_land_action_outcome_observed",
                "policy_gate_result_artifact_observed",
                "operator_decision_record_artifact_observed",
            )
            if not checks[name]
        ],
        "strict_form3_check_items": [
            "cycle2_observation_consumes_cycle1_outcome",
            "cycle2_response_selected",
            "cycle2_response_triggers_bounded_action",
            "cycle2_reobservation_is_action_outcome",
            "policy_gate_and_operator_decision_are_artifacts",
            "cycle2_dispatch_chain_distinct_from_cycle1",
            "decision_actor_is_mission_os_supervisor_when_claimed",
        ],
        "cycles": [
            {
                "cycle_index": 1,
                "observation_ref": (
                    "route_blocking_verification:"
                    "mission_designer_collision_obstacle"
                ),
                "response_ref": ALT_ROUTE_RESPONSE_REF,
                "bounded_action_ref": ALTERNATE_ROUTE_DISPATCH_REF,
                "dispatch_ref": ALTERNATE_ROUTE_DISPATCH_REF,
                "re_observation_ref": alternate_route_evidence.get("evidence_id"),
                "mission_response_kind": "action",
                "form2_subtype": "Form 2a",
                "qualifies_as_form3_cycle": checks[
                    "cycle1_alternate_route_action_outcome_observed"
                ],
            },
            {
                "cycle_index": 2,
                "observation_ref": alternate_route_evidence.get("evidence_id"),
                "ai_situation_assessment_ref": AI_ASSESSMENT_REF,
                "policy_gate_ref": POLICY_GATE_REF,
                "operator_decision_ref": OPERATOR_DECISION_REF,
                "response_ref": LAND_RESPONSE_REF,
                "bounded_action_ref": POST_ALTERNATE_LAND_ACTION_REF,
                "dispatch_ref": alternate_landing_dispatch.get(
                    "emergency_dispatch_ref"
                ),
                "re_observation_ref": alternate_landing_behavior.get(
                    "observation_id"
                ),
                "mission_response_kind": "action",
                "form2_subtype": "Form 2a",
                "qualifies_as_form3_cycle": checks[
                    "cycle2_bounded_land_action_outcome_observed"
                ]
                and checks["cycle2_dispatch_chain_distinct_from_cycle1"],
            },
        ],
        "cycle2_ai_situation_assessment": ai_assessment,
        "cycle2_policy_gate_result": policy_gate,
        "cycle2_operator_decision_record": operator_decision,
        "mission_os_runtime_decision_loop": supervisor_loop if supervisor_loop_present else {},
        "observed": {
            "alternate_route_execution_observed": alternate_route_evidence.get(
                "alternate_route_execution_observed"
            ),
            "alternate_waypoint_reached_observed": alternate_route_evidence.get(
                "alternate_waypoint_reached_observed"
            ),
            "cycle1_dispatch_ref": ALTERNATE_ROUTE_DISPATCH_REF,
            "cycle2_dispatch_ref": alternate_landing_dispatch.get(
                "emergency_dispatch_ref"
            ),
            "cycle2_behavior_observation_ref": alternate_landing_behavior.get(
                "observation_id"
            ),
            "cycle2_landing_observed": alternate_landing_behavior.get(
                "landing_observed"
            ),
            "final_status": summary.get("final_status"),
            "task_status": summary.get("task_status"),
            "unsafe_authority_flags_observed": unsafe_flags,
        },
        "source_refs": source_refs,
        "cycle_boundary_notes": [
            "alternate_mission_upload_contains_land_item_but_is_cycle1_context",
            "cycle2_requires_separate_post_alternate_land_dispatch",
            "cycle2_dispatch_ref_must_not_equal_cycle1_dispatch_ref",
        ],
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit strict obstacle true Form 3 closed-loop behavior."
    )
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--progress-threshold-m", type=float, default=DEFAULT_PROGRESS_THRESHOLD_M
    )
    parser.add_argument(
        "--waypoint-threshold-m", type=float, default=DEFAULT_WAYPOINT_THRESHOLD_M
    )
    for arg_name, _env_name in OBSTACLE_PLACEMENT_ENV_BY_ARG:
        parser.add_argument(f"--{arg_name.replace('_', '-')}", type=float)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    parser.add_argument(
        "--mission-os-supervisor-recovery-loop",
        action="store_true",
        help=(
            "When executing a new run, drive obstacle alternate-route -> LAND "
            "recovery through the scoped Mission OS supervisor loop."
        ),
    )
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_dir = args.output_dir / f"obstacle_true_form3_closed_loop_{stamp}"
    audit_dir.mkdir(parents=True, exist_ok=False)
    run_mode = "existing_run" if args.run_dir else "executed_run"
    env_overrides, expected_obstacle_placement = _obstacle_placement_from_args(args)
    try:
        run_dir = args.run_dir or _run_horizontal_route_smoke_with_env(
            artifact_root=audit_dir / "runs" / "obstacle_true_form3",
            env_overrides=env_overrides,
            extra_args=(
                ["--mission-os-supervisor-obstacle-loop"]
                if args.mission_os_supervisor_recovery_loop
                else None
            ),
        )
        artifact = _summarize_form3(
            run_dir,
            progress_threshold_m=args.progress_threshold_m,
            waypoint_threshold_m=args.waypoint_threshold_m,
            expected_obstacle_placement=expected_obstacle_placement,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        partial_run_dir = args.run_dir or _latest_partial_run_dir(
            audit_dir / "runs" / "obstacle_true_form3"
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
    artifact["obstacle_placement_env_overrides"] = env_overrides
    output_path = audit_dir / "mission_designer_obstacle_true_form3_closed_loop.json"
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact["form3_claim_supported"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
