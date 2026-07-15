"""Pure supervisor audit-artifact builders for the PX4 route runtime.

These helpers assess supplied observations and compose audit records. They do
not read process environment, mint approval or dispatch authority, execute a
backend action, mutate task state, or claim delivery or physical execution.
"""

from __future__ import annotations

from typing import Any, Mapping


MULTI_CONDITION_SUPERVISOR_SCOPE = "wind_obstacle_payload_form3_sitl"
WIND_SUPERVISOR_SCOPE = "wind_form3_sitl_only"


def wind_supervisor_assessment_inputs(
    *,
    selected_bounded_action: str,
    deviation_samples: list[dict[str, Any]],
    wind_requested_profile: Mapping[str, Any],
    route_blocking_verification_summary: Mapping[str, Any],
    vehicle_realism_summary: Mapping[str, Any],
    battery_realism_summary: Mapping[str, Any],
    telemetry_realism_summary: Mapping[str, Any],
    supervisor_scope: str = WIND_SUPERVISOR_SCOPE,
    recovery_state_label: str | None = None,
) -> dict[str, Any]:
    wind_profile = wind_requested_profile.get("requested", {})
    deviation_xy = None
    if deviation_samples:
        deviation_xy = deviation_samples[0].get("deviation_xy_m")
    multi_condition = supervisor_scope == MULTI_CONDITION_SUPERVISOR_SCOPE
    route_blocking = route_blocking_verification_summary.get(
        "route_blocking_verification",
        {},
    )
    route_blocking_observed = route_blocking.get("observed") or {}
    route_blocking_active = bool(
        route_blocking.get("verification_status")
        in {"verified", "route_blocking_verified", "blocked"}
        or route_blocking_observed.get("route_blocked") is True
        or route_blocking_observed.get("route_blocking_observed") is True
    )
    payload_application = vehicle_realism_summary.get(
        "payload_simulator_condition_application",
        {},
    )
    payload_advisory = vehicle_realism_summary.get(
        "payload_feasibility_advisory",
        {},
    )
    payload_advisory_active = bool(payload_advisory)
    battery_evidence = battery_realism_summary.get(
        "observed_battery_condition_evidence",
        {},
    )
    battery_observed = battery_evidence.get("observed") or {}
    battery_warning = battery_observed.get("observed_warning")
    battery_warning_active = False
    if battery_warning is not None:
        try:
            battery_warning_active = int(battery_warning) > 0
        except (TypeError, ValueError):
            battery_warning_active = True
    telemetry_freshness = telemetry_realism_summary.get(
        "telemetry_freshness_report",
        {},
    )
    telemetry_gap_count = int(telemetry_freshness.get("gap_count") or 0)
    observer_dropout_active = (
        telemetry_freshness.get("freshness_status") == "gap_observed"
        and telemetry_gap_count > 0
    )
    conflicting_risks = []
    if route_blocking_active:
        conflicting_risks.append("route_blocking_active")
    if payload_advisory_active:
        conflicting_risks.append("payload_feasibility_advisory_active")
    if battery_warning_active:
        conflicting_risks.append("battery_warning_active")
    if observer_dropout_active:
        conflicting_risks.append("telemetry_observer_dropout_active")
    secondary_risks = (
        [
            {
                "condition": "route_blocking",
                "risk_state": (
                    "route_blocking_active" if route_blocking_active else "not_active"
                ),
                "silent_continuation_allowed": not route_blocking_active,
                "source_ref": route_blocking.get("verification_id"),
            },
            {
                "condition": "payload_feasibility",
                "risk_state": (
                    "payload_feasibility_advisory_active"
                    if payload_advisory_active
                    else "not_active"
                ),
                "silent_continuation_allowed": not payload_advisory_active,
                "source_ref": payload_application.get("application_id"),
            },
            {
                "condition": "battery_warning",
                "risk_state": (
                    f"warning_{battery_warning}"
                    if battery_warning_active
                    else "nominal_or_unknown"
                ),
                "silent_continuation_allowed": not battery_warning_active,
                "source_ref": battery_evidence.get("evidence_id"),
            },
            {
                "condition": "telemetry_continuity",
                "risk_state": (
                    "observer_dropout_active"
                    if observer_dropout_active
                    else "sufficient_for_recovery_audit"
                ),
                "silent_continuation_allowed": not observer_dropout_active,
                "source_ref": telemetry_freshness.get("report_id"),
            },
        ]
        if multi_condition
        else []
    )
    return {
        "primary_trigger": "wind_drift_exceeded_threshold",
        "assessment_mode": "compound_mission_state_assessment",
        "supervisor_scope": supervisor_scope,
        "condition_priority": [
            "authority_boundary",
            "route_blocking",
            "payload_feasibility",
            "battery_warning",
            "telemetry_continuity",
            "wind_drift",
        ],
        "secondary_risks": secondary_risks,
        "wind": {
            "drift_above_threshold": True,
            "wind_speed_mps": wind_profile.get("wind_mean_mps"),
            "wind_direction_deg": wind_profile.get("wind_direction_deg"),
            "wind_drift_deviation_xy_m": deviation_xy,
            "primary_trigger": True,
        },
        "obstacle": {
            "route_blocking_observed": route_blocking_active,
            "route_blocking_verification_ref": route_blocking.get("verification_id"),
            "verification_status": route_blocking.get("verification_status"),
            "condition_checked": multi_condition,
        },
        "battery": {
            "battery_warning_state": (
                f"warning_{battery_warning}"
                if battery_warning_active
                else "nominal_or_unknown"
            ),
            "battery_evidence_ref": battery_evidence.get("evidence_id"),
            "px4_battery_warning_state_affected": battery_warning_active,
            "condition_checked": True,
        },
        "payload": {
            "payload_feasibility_advisory_active": payload_advisory_active,
            "payload_condition_application_ref": payload_application.get(
                "application_id"
            ),
            "payload_margin_risk": (
                "payload_feasibility_advisory_active"
                if payload_advisory_active
                else "unknown_or_not_active"
            ),
            "condition_checked": multi_condition,
        },
        "route": {
            "route_blocked": route_blocking_active,
            "dropoff_verified": False,
            "delivery_completion_claimed": False,
        },
        "telemetry": {
            "telemetry_continuity": (
                "observer_dropout_active"
                if observer_dropout_active
                else "sufficient_for_recovery_audit"
            ),
            "telemetry_freshness_ref": telemetry_freshness.get("report_id"),
            "observer_dropout_active": observer_dropout_active,
        },
        "recovery_state": {
            "cycle1_recovery_state_label": recovery_state_label,
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
            "wind_drift_recovery_operator_review_required_due_to_conflicting_risks"
            if conflicting_risks
            else "wind_drift_recovery_required_no_conflicting_blocker_detected"
        ),
    }


def build_wind_supervisor_cycle(
    *,
    cycle_index: int,
    observation_ref: str,
    response_ref: str,
    selected_bounded_action: str,
    assessment_inputs: Mapping[str, Any],
    dispatch_ref: str | None,
    dispatch_status: str | None,
    approval_ref: str | None,
    outcome_ref: str | None = None,
    outcome_observed: bool = False,
    recovery_state_label: str | None = None,
    pose_z_m: float | None = None,
    supervisor_scope: str = WIND_SUPERVISOR_SCOPE,
) -> dict[str, Any]:
    decision_id = (
        "mission_os_recovery_decision:wind_drift_supervisor_bounded_rtl"
        if cycle_index == 1
        else "mission_os_recovery_decision:wind_rtl_state_supervisor_bounded_land"
    )
    request_id = (
        "mission_os_backend_action_request:wind_drift_supervisor_bounded_rtl"
        if cycle_index == 1
        else "mission_os_backend_action_request:wind_rtl_state_supervisor_bounded_land"
    )
    receipt_id = (
        "mission_os_backend_action_receipt:wind_drift_supervisor_bounded_rtl"
        if cycle_index == 1
        else "mission_os_backend_action_receipt:wind_rtl_state_supervisor_bounded_land"
    )
    outcome_id = (
        "mission_os_recovery_outcome_observation:wind_drift_supervisor_bounded_rtl"
        if cycle_index == 1
        else "mission_os_recovery_outcome_observation:wind_rtl_state_supervisor_bounded_land"
    )
    return {
        "cycle_index": cycle_index,
        "decision_ref": decision_id,
        "action_request_ref": request_id,
        "action_receipt_ref": receipt_id,
        "outcome_observation_ref": outcome_id,
        "decision": {
            "schema_version": "mission_os_recovery_decision.v1",
            "decision_id": decision_id,
            "cycle_index": cycle_index,
            "decision_loop_driver": "mission_os_supervisor",
            "supervisor_scope": supervisor_scope,
            "full_gateway_runtime_loop": False,
            "source_observation_ref": observation_ref,
            "mission_response_candidate_ref": response_ref,
            "primary_trigger": "wind_drift_exceeded_threshold",
            "assessment_inputs": dict(assessment_inputs),
            "mission_state_interpretation": assessment_inputs[
                "mission_state_interpretation"
            ],
            "selected_bounded_action": selected_bounded_action,
            "operator_approval_required": True,
            "automatic_dispatch_allowed": False,
            "operator_approved_dispatch_allowed": approval_ref is not None,
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
            "llm_gate_judge_used": False,
            "created_dispatch_authority": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "action_request": {
            "schema_version": "mission_os_backend_action_request.v1",
            "request_id": request_id,
            "cycle_index": cycle_index,
            "decision_ref": decision_id,
            "backend_target": "px4_gazebo_sitl",
            "bounded_action": selected_bounded_action,
            "expected_dispatch_ref": dispatch_ref,
            "approval_ref": approval_ref,
            "allowlisted_action": True,
            "operator_approved": approval_ref is not None,
            "automatic_dispatch_allowed": False,
            "dispatch_authority_created": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "action_receipt": {
            "schema_version": "mission_os_backend_action_receipt.v1",
            "receipt_id": receipt_id,
            "cycle_index": cycle_index,
            "action_request_ref": request_id,
            "dispatch_ref": dispatch_ref,
            "dispatch_status": dispatch_status,
            "dispatch_observed": str(dispatch_ref or "").startswith(
                "px4_gazebo_emergency_command_dispatch_result:"
            ),
            "backend_target": "px4_gazebo_sitl",
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "outcome_observation": {
            "schema_version": "mission_os_recovery_outcome_observation.v1",
            "observation_id": outcome_id,
            "cycle_index": cycle_index,
            "action_receipt_ref": receipt_id,
            "outcome_observation_ref": outcome_ref,
            "outcome_observed": outcome_observed,
            "state_label": recovery_state_label,
            "pose_z_m": pose_z_m,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def build_wind_supervisor_loop(
    *,
    cycle1: Mapping[str, Any],
    cycle2: Mapping[str, Any],
    cycle1_outcome_observed: bool,
    cycle2_outcome_observed: bool,
    supervisor_scope: str = WIND_SUPERVISOR_SCOPE,
) -> dict[str, Any]:
    cycles = [dict(cycle1), dict(cycle2)]
    loop_conflicting_risks = sorted(
        {
            risk
            for cycle in cycles
            for risk in (
                (cycle.get("decision") or {})
                .get("assessment_inputs", {})
                .get("conflicting_risks", [])
            )
            if isinstance(risk, str) and risk
        }
    )
    supervisor_loop_claim_supported = bool(
        cycle1_outcome_observed
        and cycle2_outcome_observed
        and not loop_conflicting_risks
    )
    return {
        "schema_version": "mission_os_supervisor_recovery_loop.v1",
        "decision_loop_driver": "mission_os_supervisor",
        "supervisor_scope": supervisor_scope,
        "full_gateway_runtime_loop": False,
        "primary_trigger": "wind_drift_exceeded_threshold",
        "assessment_mode": "compound_mission_state_assessment",
        "secondary_risks": sorted(
            {
                risk["condition"]
                for cycle in cycles
                for risk in (
                    (cycle.get("decision") or {})
                    .get("assessment_inputs", {})
                    .get("secondary_risks", [])
                )
                if isinstance(risk, dict)
            }
        ),
        "cycle_count": 2 if supervisor_loop_claim_supported else 1,
        "observed_cycle_count": (
            2 if cycle1_outcome_observed and cycle2_outcome_observed else 1
        ),
        "supervisor_loop_claim_supported": supervisor_loop_claim_supported,
        "conflicting_risks": loop_conflicting_risks,
        "cycles": cycles,
        "cycle1_supervisor_decision_observed": True,
        "cycle1_backend_action_request_observed": True,
        "cycle1_backend_action_receipt_observed": bool(
            cycle1.get("action_receipt", {}).get("dispatch_ref")
        ),
        "cycle1_outcome_observation_observed": cycle1_outcome_observed,
        "cycle2_supervisor_decision_observed": True,
        "cycle2_backend_action_request_observed": True,
        "cycle2_backend_action_receipt_observed": bool(
            cycle2.get("action_receipt", {}).get("dispatch_ref")
        ),
        "cycle2_outcome_observation_observed": cycle2_outcome_observed,
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


def obstacle_supervisor_assessment_inputs(
    *,
    selected_bounded_action: str,
    route_blocking_verification_summary: Mapping[str, Any],
    alternate_mission_upload_summary: Mapping[str, Any],
    battery_realism_summary: Mapping[str, Any],
    telemetry_realism_summary: Mapping[str, Any],
    cycle1_state_label: str | None = None,
) -> dict[str, Any]:
    route_blocking = route_blocking_verification_summary.get(
        "route_blocking_verification",
        {},
    )
    route_blocking_observed = route_blocking.get("observed") or {}
    alternate_route = alternate_mission_upload_summary.get(
        "alternate_route_execution_evidence",
        {},
    )
    alternate_route_observed = alternate_route.get("observed") or {}
    battery_evidence = battery_realism_summary.get(
        "observed_battery_condition_evidence",
        {},
    )
    battery_observed = battery_evidence.get("observed") or {}
    battery_warning = battery_observed.get("observed_warning")
    battery_warning_active = False
    if battery_warning is not None:
        try:
            battery_warning_active = int(battery_warning) > 0
        except (TypeError, ValueError):
            battery_warning_active = True
    telemetry_freshness = telemetry_realism_summary.get(
        "telemetry_freshness_report",
        {},
    )
    telemetry_gap_count = int(telemetry_freshness.get("gap_count") or 0)
    observer_dropout_active = (
        telemetry_freshness.get("freshness_status") == "gap_observed"
        and telemetry_gap_count > 0
    )
    conflicting_risks = []
    if battery_warning_active:
        conflicting_risks.append("battery_warning_active")
    if observer_dropout_active:
        conflicting_risks.append("telemetry_observer_dropout_active")
    mission_state_interpretation = (
        "obstacle_supervisor_operator_review_required_due_to_conflicting_risks"
        if conflicting_risks
        else "obstacle_alternate_route_completed_no_conflicting_blocker_detected"
    )
    return {
        "primary_trigger": "route_blocking_obstacle_verified",
        "assessment_mode": "compound_mission_state_assessment",
        "obstacle": {
            "route_blocked": bool(
                route_blocking_observed.get("route_blocking_verified")
            ),
            "verification_ref": (
                "route_blocking_verification:mission_designer_collision_obstacle"
            ),
        },
        "alternate_route": {
            "alternate_route_execution_observed": alternate_route.get(
                "alternate_route_execution_observed"
            ),
            "alternate_waypoint_reached_observed": alternate_route.get(
                "alternate_waypoint_reached_observed"
            ),
            "cycle1_state_label": cycle1_state_label,
            "final_distance_to_alternate_waypoint_m": alternate_route_observed.get(
                "final_distance_to_alternate_waypoint_m"
            ),
        },
        "battery": {
            "battery_warning_state": (
                f"warning_{battery_warning}"
                if battery_warning_active
                else "nominal_or_unknown"
            ),
            "px4_battery_warning_state_affected": battery_warning_active,
        },
        "payload": {
            "payload_feasibility_advisory_active": False,
            "payload_margin_risk": "unknown_or_not_active",
        },
        "route": {
            "dropoff_verified": False,
            "delivery_completion_claimed": False,
            "original_dropoff_unverified": True,
        },
        "telemetry": {
            "telemetry_continuity": (
                "observer_dropout_active"
                if observer_dropout_active
                else "sufficient_for_recovery_audit"
            ),
            "observer_dropout_active": observer_dropout_active,
            "gap_count": telemetry_gap_count,
        },
        "recovery_state": {"selected_bounded_action": selected_bounded_action},
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
        "mission_state_interpretation": mission_state_interpretation,
    }


def build_obstacle_supervisor_cycle(
    *,
    cycle_index: int,
    observation_ref: str,
    response_ref: str,
    selected_bounded_action: str,
    assessment_inputs: Mapping[str, Any],
    dispatch_ref: str | None,
    dispatch_status: str | None,
    approval_ref: str | None,
    outcome_ref: str | None = None,
    outcome_observed: bool = False,
    cycle1_state_label: str | None = None,
    pose_z_m: float | None = None,
) -> dict[str, Any]:
    decision_id = (
        "mission_os_recovery_decision:obstacle_supervisor_alternate_route"
        if cycle_index == 1
        else "mission_os_recovery_decision:obstacle_alternate_waypoint_supervisor_bounded_land"
    )
    request_id = (
        "mission_os_backend_action_request:obstacle_supervisor_alternate_route"
        if cycle_index == 1
        else "mission_os_backend_action_request:obstacle_alternate_waypoint_supervisor_bounded_land"
    )
    receipt_id = (
        "mission_os_backend_action_receipt:obstacle_supervisor_alternate_route"
        if cycle_index == 1
        else "mission_os_backend_action_receipt:obstacle_alternate_waypoint_supervisor_bounded_land"
    )
    outcome_id = (
        "mission_os_recovery_outcome_observation:obstacle_supervisor_alternate_route"
        if cycle_index == 1
        else "mission_os_recovery_outcome_observation:obstacle_alternate_waypoint_supervisor_bounded_land"
    )
    dispatch_observed = (
        bool(dispatch_ref)
        if cycle_index == 1
        else str(dispatch_ref or "").startswith(
            "px4_gazebo_emergency_command_dispatch_result:"
        )
    )
    return {
        "cycle_index": cycle_index,
        "decision_ref": decision_id,
        "action_request_ref": request_id,
        "action_receipt_ref": receipt_id,
        "outcome_observation_ref": outcome_id,
        "decision": {
            "schema_version": "mission_os_recovery_decision.v1",
            "decision_id": decision_id,
            "cycle_index": cycle_index,
            "decision_loop_driver": "mission_os_supervisor",
            "supervisor_scope": "obstacle_form3_sitl_only",
            "full_gateway_runtime_loop": False,
            "source_observation_ref": observation_ref,
            "mission_response_candidate_ref": response_ref,
            "primary_trigger": "route_blocking_obstacle_verified",
            "assessment_inputs": dict(assessment_inputs),
            "mission_state_interpretation": assessment_inputs[
                "mission_state_interpretation"
            ],
            "selected_bounded_action": selected_bounded_action,
            "operator_approval_required": True,
            "automatic_dispatch_allowed": False,
            "operator_approved_dispatch_allowed": approval_ref is not None,
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
            "llm_gate_judge_used": False,
            "created_dispatch_authority": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "action_request": {
            "schema_version": "mission_os_backend_action_request.v1",
            "request_id": request_id,
            "cycle_index": cycle_index,
            "decision_ref": decision_id,
            "backend_target": "px4_gazebo_sitl",
            "bounded_action": selected_bounded_action,
            "expected_dispatch_ref": dispatch_ref,
            "approval_ref": approval_ref,
            "allowlisted_action": True,
            "operator_approved": approval_ref is not None,
            "automatic_dispatch_allowed": False,
            "dispatch_authority_created": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "action_receipt": {
            "schema_version": "mission_os_backend_action_receipt.v1",
            "receipt_id": receipt_id,
            "cycle_index": cycle_index,
            "action_request_ref": request_id,
            "dispatch_ref": dispatch_ref,
            "dispatch_status": dispatch_status,
            "dispatch_observed": dispatch_observed,
            "backend_target": "px4_gazebo_sitl",
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "outcome_observation": {
            "schema_version": "mission_os_recovery_outcome_observation.v1",
            "observation_id": outcome_id,
            "cycle_index": cycle_index,
            "action_receipt_ref": receipt_id,
            "outcome_observation_ref": outcome_ref,
            "outcome_observed": outcome_observed,
            "state_label": cycle1_state_label,
            "pose_z_m": pose_z_m,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def build_obstacle_supervisor_loop(
    *,
    cycle1: Mapping[str, Any],
    cycle2: Mapping[str, Any],
    cycle1_outcome_observed: bool,
    cycle2_outcome_observed: bool,
) -> dict[str, Any]:
    cycles = [dict(cycle1), dict(cycle2)]
    conflicting_risks = sorted(
        {
            risk
            for cycle in cycles
            for risk in (
                (cycle.get("decision") or {})
                .get("assessment_inputs", {})
                .get("conflicting_risks", [])
            )
        }
    )
    supervisor_loop_claim_supported = bool(
        cycle1_outcome_observed and cycle2_outcome_observed and not conflicting_risks
    )
    return {
        "schema_version": "mission_os_supervisor_recovery_loop.v1",
        "decision_loop_driver": "mission_os_supervisor",
        "supervisor_scope": "obstacle_form3_sitl_only",
        "full_gateway_runtime_loop": False,
        "primary_trigger": "route_blocking_obstacle_verified",
        "assessment_mode": "compound_mission_state_assessment",
        "cycle_count": 2 if supervisor_loop_claim_supported else 1,
        "supervisor_loop_claim_supported": supervisor_loop_claim_supported,
        "conflicting_risks": conflicting_risks,
        "cycles": cycles,
        "cycle1_supervisor_decision_observed": True,
        "cycle1_backend_action_request_observed": True,
        "cycle1_backend_action_receipt_observed": bool(
            cycle1.get("action_receipt", {}).get("dispatch_ref")
        ),
        "cycle1_outcome_observation_observed": cycle1_outcome_observed,
        "cycle2_supervisor_decision_observed": True,
        "cycle2_backend_action_request_observed": True,
        "cycle2_backend_action_receipt_observed": bool(
            cycle2.get("action_receipt", {}).get("dispatch_ref")
        ),
        "cycle2_outcome_observation_observed": cycle2_outcome_observed,
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


__all__ = [
    "MULTI_CONDITION_SUPERVISOR_SCOPE",
    "WIND_SUPERVISOR_SCOPE",
    "build_obstacle_supervisor_cycle",
    "build_obstacle_supervisor_loop",
    "build_wind_supervisor_cycle",
    "build_wind_supervisor_loop",
    "obstacle_supervisor_assessment_inputs",
    "wind_supervisor_assessment_inputs",
]
