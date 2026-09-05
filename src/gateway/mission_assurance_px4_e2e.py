"""Gateway-owned evidence for the Mission Assurance CLI to PX4 live loop."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

MISSION_ASSURANCE_GATEWAY_PX4_E2E_SCHEMA_VERSION = (
    "missionos_mission_assurance_gateway_px4_e2e.v1"
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _unique_reasons(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def build_mission_assurance_gateway_px4_e2e(
    *,
    task_id: str,
    client_surface: str,
    mission_assurance_requested: bool,
    execution_approval: Mapping[str, Any],
    live_summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove the complete HTTP-owned loop without transferring authority."""

    summary = dict(live_summary)
    approval = dict(execution_approval)
    guard = _mapping(summary.get("mission_assurance_live_guard"))
    evaluation = _mapping(guard.get("mission_assurance_evaluation"))
    proposal = _mapping(evaluation.get("proposal"))
    model_evidence = _mapping(proposal.get("model_invocation_evidence"))
    recovery_proposal = _mapping(guard.get("runtime_recovery_agent_proposal"))
    recovery_model_evidence = _mapping(
        recovery_proposal.get("model_invocation_evidence")
    )
    original_feasibility = _mapping(guard.get("original_action_feasibility"))
    current_feasibility = _mapping(guard.get("current_action_feasibility"))
    recovery_reported_feasibility = _mapping(
        guard.get("recovery_proposed_action_feasibility")
    )
    revalidation = _mapping(guard.get("action_revalidation"))
    compilation = _mapping(guard.get("response_compilation"))
    response_kind = str(proposal.get("proposed_response_kind") or "")
    recovery_response = str(
        recovery_proposal.get("selected_bounded_action") or ""
    )
    recovery_proposed_dispatch = recovery_response not in {
        "continue",
        "hold",
        "operator_review",
    }
    source_feasibility = (
        original_feasibility
        if recovery_proposed_dispatch
        else recovery_reported_feasibility
    )
    reasons: list[str] = []

    def require(condition: bool, reason: str) -> None:
        if not condition:
            reasons.append(reason)

    require(client_surface == "missionos_cli", "missionos_cli_entrypoint_not_observed")
    require(
        mission_assurance_requested,
        "mission_assurance_on_deviation_not_requested",
    )
    require(
        approval.get("mission_assurance_on_deviation_approved") is True,
        "mission_assurance_execution_approval_binding_missing",
    )
    require(
        approval.get("missionos_client_surface") == "missionos_cli",
        "mission_assurance_execution_approval_client_surface_mismatch",
    )
    require(
        approval.get("operator_approved") is True,
        "human_execution_approval_not_observed",
    )
    require(
        approval.get("approval_status") == "consumed_in_runtime"
        and approval.get("consumed_in_runtime") is True,
        "human_execution_approval_not_consumed",
    )
    require(
        summary.get("mission_designer_task_id") == task_id,
        "gateway_task_binding_mismatch",
    )
    require(
        summary.get("same_gateway_execution_run_observed") is True,
        "same_gateway_execution_run_not_observed",
    )
    require(
        summary.get("actual_px4_gazebo_horizontal_smoke_observed") is True,
        "actual_px4_gazebo_sitl_not_observed",
    )
    require(
        summary.get("actual_sitl_flight_evidence_observed") is True,
        "actual_sitl_flight_evidence_not_observed",
    )
    require(
        summary.get("decision_loop_driver")
        == "runtime_recovery_agent_then_mission_assurance_agent",
        "two_agent_decision_order_not_observed",
    )
    require(
        summary.get("runtime_recovery_agent_invoked") is True
        and guard.get("runtime_recovery_agent_invoked") is True,
        "runtime_recovery_agent_not_invoked",
    )
    require(
        guard.get("recovery_agent_invoked_before_mission_assurance") is True,
        "recovery_agent_not_observed_before_mission_assurance",
    )
    require(
        recovery_proposal.get("model_inference_invoked") is True,
        "runtime_recovery_agent_model_inference_not_observed",
    )
    require(
        recovery_proposal.get("runtime_status") == "proposal_guardrail_passed",
        "runtime_recovery_agent_proposal_not_accepted",
    )
    require(
        summary.get("mission_assurance_agent_invoked") is True,
        "mission_assurance_agent_not_invoked",
    )
    require(
        proposal.get("model_inference_invoked") is True,
        "mission_assurance_model_inference_not_observed",
    )
    require(
        proposal.get("judgment_status") == "proposal_guardrail_passed",
        "mission_assurance_model_judgment_not_accepted",
    )
    disagreement_observed = guard.get("agent_disagreement_observed") is True
    if disagreement_observed:
        require(
            recovery_response in {"continue", "hold", "operator_review"},
            "agent_disagreement_recovery_no_action_response_missing",
        )
        require(
            response_kind in {"return", "replan"},
            "agent_disagreement_assurance_action_missing",
        )
        require(
            guard.get("guard_status") == "operator_escalation"
            and guard.get("agent_disagreement_resolution")
            == "operator_escalation",
            "agent_disagreement_not_escalated",
        )
        require(
            guard.get("selected_recovery_action") is None
            and not _mapping(guard.get("operator_recovery_approval_request"))
            and guard.get("dispatch_request_sent") is False,
            "agent_disagreement_created_recovery_authority",
        )
        require(
            summary.get("task_status") == "blocked",
            "agent_disagreement_task_not_blocked",
        )
        e2e_disposition = "agent_disagreement_operator_escalation"
    elif response_kind == "return":
        require(
            recovery_response == "return_to_launch",
            "runtime_recovery_agent_rtl_not_proposed",
        )
        require(
            original_feasibility.get("feasibility_status") == "verified_feasible",
            "mission_assurance_original_feasibility_not_verified",
        )
        approval_request = _mapping(
            guard.get("operator_recovery_approval_request")
        )
        require(
            guard.get("guard_status") == "awaiting_operator_approval",
            "mission_assurance_fresh_operator_approval_not_requested",
        )
        require(
            approval_request.get("request_status")
            == "awaiting_operator_approval"
            and approval_request.get("requires_new_human_approval") is True,
            "mission_assurance_recovery_approval_request_missing",
        )
        require(
            guard.get("selected_recovery_action") is None
            and guard.get("dispatch_request_sent") is False,
            "mission_assurance_return_dispatched_without_fresh_approval",
        )
        e2e_disposition = "return_awaiting_operator_approval"
    elif response_kind == "hold":
        if recovery_proposed_dispatch:
            require(
                source_feasibility.get("feasibility_status") == "verified_feasible",
                "source_action_feasibility_not_verified_before_assurance",
            )
        require(
            compilation.get("compile_status") == "no_action_required",
            "mission_assurance_hold_not_compiled_as_no_action",
        )
        require(
            guard.get("guard_status") in {"blocked", "no_dispatch"},
            "mission_assurance_hold_did_not_prevent_dispatch",
        )
        if recovery_proposed_dispatch:
            require(
                guard.get("dispatch_prevented_by_mission_assurance") is True,
                "mission_assurance_hold_dispatch_prevention_not_observed",
            )
            require(
                guard.get("suppression_source") == "mission_assurance_agent",
                "mission_assurance_hold_suppression_source_not_observed",
            )
            require(
                bool(_mapping(guard.get("post_suppression_observation"))),
                "mission_assurance_hold_reobservation_not_observed",
            )
        require(
            guard.get("selected_recovery_action") is None,
            "mission_assurance_hold_selected_recovery_action",
        )
        require(
            summary.get("recovery_command_ack_observed") is not True,
            "mission_assurance_hold_recovery_ack_observed",
        )
        e2e_disposition = (
            "hold_prevented_recovery_dispatch"
            if recovery_proposed_dispatch
            else "hold_no_recovery_dispatch"
        )
    elif response_kind in {"continue", "operator_escalation"}:
        expected_recovery_response = {
            "continue": "continue",
            "operator_escalation": "operator_review",
        }[response_kind]
        require(
            recovery_response == expected_recovery_response,
            "mission_assurance_no_dispatch_response_mismatch",
        )
        require(
            compilation.get("compile_status") == "no_action_required",
            "mission_assurance_response_not_compiled_as_no_action",
        )
        require(
            guard.get("guard_status") == "no_dispatch",
            "mission_assurance_no_dispatch_not_accepted",
        )
        require(
            guard.get("selected_recovery_action") is None,
            "mission_assurance_no_dispatch_selected_recovery_action",
        )
        require(
            summary.get("recovery_command_ack_observed") is not True,
            "mission_assurance_no_dispatch_recovery_ack_observed",
        )
        if response_kind == "continue":
            continue_execution = _mapping(
                summary.get("mission_assurance_continue_execution")
            )
            require(
                summary.get("mission_assurance_continue_execution_invoked")
                is True,
                "mission_assurance_continue_execution_not_invoked",
            )
            require(
                continue_execution.get("existing_route_approval_consumed")
                is True,
                "mission_assurance_continue_route_approval_not_consumed",
            )
            require(
                continue_execution.get("offboard_mode_switch_ack_observed")
                is True
                and continue_execution.get(
                    "offboard_mode_switch_ack_result_code"
                )
                == 0,
                "mission_assurance_continue_offboard_ack_not_observed",
            )
            require(
                int(continue_execution.get("setpoint_frames_sent") or 0) > 0,
                "mission_assurance_continue_setpoint_stream_not_observed",
            )
            require(
                summary.get("mission_assurance_continue_effect_observed")
                is True,
                "mission_assurance_continue_effect_not_observed",
            )
            require(
                summary.get("mission_assurance_continue_route_completion_observed")
                is True,
                "mission_assurance_continue_route_completion_not_observed",
            )
            require(
                summary.get(
                    "mission_assurance_continue_dropoff_approach_observed"
                )
                is True,
                "mission_assurance_continue_dropoff_approach_not_observed",
            )
            require(
                summary.get("task_status") == "completed"
                and summary.get("final_status") == "completed",
                "mission_assurance_continue_task_not_completed",
            )
            require(
                summary.get("dropoff_region_reached") is True,
                "mission_assurance_continue_dropoff_not_observed",
            )
            require(
                summary.get("payload_release_observed") is True,
                "mission_assurance_continue_payload_release_not_observed",
            )
            require(
                summary.get("delivery_completion_claimed") is True,
                "mission_assurance_continue_delivery_completion_not_observed",
            )
            e2e_disposition = "continue_completed_route_delivery"
        else:
            require(
                summary.get("task_status") == "blocked",
                "mission_assurance_no_dispatch_task_not_blocked",
            )
            e2e_disposition = "operator_escalation_no_recovery_dispatch"
    else:
        require(False, "mission_assurance_e2e_response_not_supported")
        e2e_disposition = "unsupported"
    if response_kind != "continue":
        require(
            summary.get("delivery_completion_claimed") is False,
            "mission_assurance_e2e_must_not_claim_delivery",
        )
    require(
        summary.get("hardware_target_allowed") is False
        and summary.get("physical_execution_invoked") is False,
        "mission_assurance_e2e_scope_invalid",
    )
    require(
        proposal.get("approval_recorded") is False
        and proposal.get("dispatch_authority_created") is False
        and proposal.get("dispatch_request_sent") is False
        and proposal.get("physical_execution_invoked") is False,
        "mission_assurance_proposal_authority_boundary_invalid",
    )
    require(
        recovery_proposal.get("approval_recorded") is False
        and recovery_proposal.get("dispatch_authority_created") is False
        and recovery_proposal.get("dispatch_request_sent") is False
        and recovery_proposal.get("physical_execution_invoked") is False,
        "runtime_recovery_agent_proposal_authority_boundary_invalid",
    )
    require(
        guard.get("approval_recorded") is False
        and guard.get("dispatch_authority_created") is False
        and guard.get("dispatch_request_sent") is False
        and guard.get("physical_execution_invoked") is False,
        "mission_assurance_guard_authority_boundary_invalid",
    )
    blocking_reasons = _unique_reasons(reasons)
    completed = not blocking_reasons
    identity_payload = {
        "task_id": task_id,
        "approval_id": approval.get("approval_id"),
        "proposal_id": proposal.get("proposal_id"),
        "recovery_proposal_ref": recovery_proposal.get("proposal_ref"),
        "artifact_dir": summary.get("artifact_dir"),
    }
    digest = sha256(
        json.dumps(identity_payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": MISSION_ASSURANCE_GATEWAY_PX4_E2E_SCHEMA_VERSION,
        "e2e_id": f"mission_assurance_gateway_px4_e2e_{digest[:12]}",
        "task_id": task_id,
        "client_surface": client_surface,
        "cli_entrypoint_observed": client_surface == "missionos_cli",
        "gateway_http_route_observed": True,
        "same_task_id_observed": summary.get("mission_designer_task_id") == task_id,
        "same_gateway_execution_run_observed": summary.get(
            "same_gateway_execution_run_observed"
        )
        is True,
        "mission_assurance_on_deviation_requested": mission_assurance_requested,
        "mission_assurance_on_deviation_approved": approval.get(
            "mission_assurance_on_deviation_approved"
        )
        is True,
        "execution_approval_id": approval.get("approval_id"),
        "human_execution_approval_observed": approval.get("operator_approved")
        is True,
        "human_execution_approval_consumed": approval.get("consumed_in_runtime")
        is True,
        "mission_assurance_agent_invoked": summary.get(
            "mission_assurance_agent_invoked"
        )
        is True,
        "runtime_recovery_agent_invoked": summary.get(
            "runtime_recovery_agent_invoked"
        )
        is True,
        "recovery_agent_invoked_before_mission_assurance": guard.get(
            "recovery_agent_invoked_before_mission_assurance"
        )
        is True,
        "decision_sequence": list(guard.get("decision_sequence") or []),
        "recovery_agent_model_id": recovery_model_evidence.get("model_id"),
        "recovery_proposed_action": recovery_proposal.get(
            "selected_bounded_action"
        ),
        "model_inference_invoked": proposal.get("model_inference_invoked") is True,
        "model_id": model_evidence.get("model_id"),
        "mission_assurance_agent_model_id": model_evidence.get("model_id"),
        "proposed_response_kind": proposal.get("proposed_response_kind"),
        "e2e_disposition": e2e_disposition,
        "judgment_status": proposal.get("judgment_status"),
        "original_feasibility_status": original_feasibility.get(
            "feasibility_status"
        ),
        "recovery_proposed_action_feasibility_status": source_feasibility.get(
            "feasibility_status"
        ),
        "dispatch_prevented_by_mission_assurance": guard.get(
            "dispatch_prevented_by_mission_assurance"
        )
        is True,
        "suppression_source": guard.get("suppression_source"),
        "suppression_reason": guard.get("suppression_reason"),
        "post_suppression_reobservation_observed": bool(
            _mapping(guard.get("post_suppression_observation"))
        ),
        "agent_disagreement_observed": disagreement_observed,
        "agent_disagreement_kind": guard.get("agent_disagreement_kind"),
        "agent_disagreement_resolution": guard.get(
            "agent_disagreement_resolution"
        ),
        "assurance_requested_action": guard.get("assurance_requested_action"),
        "recovery_no_action_response": guard.get("recovery_no_action_response"),
        "current_feasibility_status": current_feasibility.get(
            "feasibility_status"
        ),
        "revalidation_status": revalidation.get("revalidation_status"),
        "guard_status": guard.get("guard_status"),
        "selected_recovery_action": guard.get("selected_recovery_action"),
        "proposed_recovery_action": guard.get("proposed_recovery_action"),
        "fresh_operator_approval_required": (
            _mapping(guard.get("operator_recovery_approval_request")).get(
                "requires_new_human_approval"
            )
            is True
        ),
        "recovery_approval_status": _mapping(
            guard.get("operator_recovery_approval_request")
        ).get("request_status"),
        "mission_assurance_continue_execution_invoked": summary.get(
            "mission_assurance_continue_execution_invoked"
        )
        is True,
        "mission_assurance_continue_effect_observed": summary.get(
            "mission_assurance_continue_effect_observed"
        )
        is True,
        "mission_assurance_continue_execution": _mapping(
            summary.get("mission_assurance_continue_execution")
        ),
        "mission_assurance_continue_route_completion_observed": summary.get(
            "mission_assurance_continue_route_completion_observed"
        )
        is True,
        "mission_assurance_continue_dropoff_approach_observed": summary.get(
            "mission_assurance_continue_dropoff_approach_observed"
        )
        is True,
        "simulator_execution_observed": summary.get(
            "actual_px4_gazebo_horizontal_smoke_observed"
        )
        is True,
        "command_ack_observed": summary.get("recovery_command_ack_observed")
        is True,
        "runtime_state_observed": summary.get("recovery_state_observed") is True,
        "runtime_state_label": summary.get("recovery_state_label"),
        "final_status": summary.get("final_status"),
        "full_gateway_runtime_loop": completed,
        "e2e_status": "completed" if completed else "blocked",
        "blocking_reasons": blocking_reasons,
        "agent_created_approval": False,
        "agent_created_dispatch_authority": False,
        "e2e_artifact_creates_dispatch_authority": False,
        "llm_judgment_in_gate": False,
        "gateway_autonomous_runtime_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": summary.get(
            "delivery_completion_claimed"
        )
        is True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


__all__ = [
    "MISSION_ASSURANCE_GATEWAY_PX4_E2E_SCHEMA_VERSION",
    "build_mission_assurance_gateway_px4_e2e",
]
