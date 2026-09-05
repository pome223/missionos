"""Read-only projections of Recovery and Mission Assurance task evidence."""

from __future__ import annotations

from typing import Any


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def mission_assurance_projection(artifacts: dict[str, Any]) -> dict[str, Any]:
    """Return one source-backed two-Agent projection without creating authority."""

    e2e = _mapping(artifacts.get("missionos_mission_assurance_gateway_px4_e2e"))
    summary = _mapping(artifacts.get("missionos_mission_assurance_px4_horizontal_summary"))
    incident_graph = _mapping(artifacts.get("missionos_mission_incident_graph"))
    if not incident_graph:
        incident_graph = _mapping(_mapping(artifacts.get("turtlebot3_recovery_checkpoint")).get("missionos_mission_incident_graph"))
    guard = _mapping(summary.get("mission_assurance_live_guard"))
    if not guard:
        guard = _mapping(e2e.get("mission_assurance_live_guard"))
    recovery = _mapping(guard.get("runtime_recovery_agent_proposal"))
    if not recovery and incident_graph:
        recovery_result = _mapping(incident_graph.get("recovery_result"))
        recovery_invocations = recovery_result.get("agent_invocations")
        recovery_invocations = (
            recovery_invocations if isinstance(recovery_invocations, list) else []
        )
        recovery_invocation = next(
            (
                _mapping(item)
                for item in recovery_invocations
                if _mapping(item).get("agent_name")
                == "missionos_runtime_recovery_agent"
                or _mapping(item).get("agent_role") == "recovery"
            ),
            {},
        )
        recovery = {
            "selected_bounded_action": incident_graph.get(
                "recovery_proposed_action"
            ),
            "runtime_status": recovery_result.get("runtime_status"),
            "model_invocation_evidence": recovery_invocation,
        }
    assurance_evaluation = _mapping(guard.get("mission_assurance_evaluation"))
    assurance = _mapping(assurance_evaluation.get("proposal"))
    if not assurance and incident_graph:
        assurance = _mapping(incident_graph.get("mission_assurance_proposal"))
    original = _mapping(guard.get("original_action_feasibility"))
    if not original and incident_graph:
        original = _mapping(incident_graph.get("source_action_feasibility"))
    current = _mapping(guard.get("current_action_feasibility"))
    revalidation = _mapping(guard.get("action_revalidation"))
    approval_request = _mapping(guard.get("operator_recovery_approval_request"))

    present = bool(e2e or guard or incident_graph)
    if not present:
        return {}
    sequence = _first(
        e2e.get("decision_sequence"),
        guard.get("decision_sequence"),
        incident_graph.get("decision_sequence"),
        [],
    )
    if not isinstance(sequence, list):
        sequence = []
    return {
        "schema_version": "missionos_cli_mission_assurance_projection.v1",
        "present": True,
        "e2e_status": e2e.get("e2e_status"),
        "decision_sequence": [str(item) for item in sequence],
        "recovery_agent_invoked": _first(
            e2e.get("runtime_recovery_agent_invoked"),
            guard.get("runtime_recovery_agent_invoked"),
            incident_graph.get("recovery_agent_invoked"),
        ),
        "recovery_agent_before_assurance": _first(
            e2e.get("recovery_agent_invoked_before_mission_assurance"),
            guard.get("recovery_agent_invoked_before_mission_assurance"),
            incident_graph.get("recovery_agent_invoked_before_mission_assurance"),
        ),
        "recovery_model_id": _first(
            e2e.get("recovery_agent_model_id"),
            _mapping(recovery.get("model_invocation_evidence")).get("model_id"),
        ),
        "recovery_proposed_action": _first(
            e2e.get("recovery_proposed_action"),
            recovery.get("selected_bounded_action"),
        ),
        "recovery_proposal_status": recovery.get("runtime_status"),
        "mission_assurance_agent_invoked": _first(
            e2e.get("mission_assurance_agent_invoked"),
            guard.get("mission_assurance_agent_invoked"),
            incident_graph.get("mission_assurance_agent_invoked"),
        ),
        "mission_assurance_model_id": _first(
            e2e.get("mission_assurance_agent_model_id"),
            _mapping(assurance.get("model_invocation_evidence")).get("model_id"),
        ),
        "mission_assurance_response": _first(
            e2e.get("proposed_response_kind"),
            assurance.get("proposed_response_kind"),
        ),
        "mission_assurance_judgment_status": assurance.get("judgment_status"),
        "original_feasibility": _first(
            e2e.get("original_feasibility_status"),
            e2e.get("original_action_feasibility_status"),
            original.get("feasibility_status"),
        ),
        "current_feasibility": _first(
            e2e.get("current_action_feasibility_status"),
            current.get("feasibility_status"),
        ),
        "revalidation_status": _first(
            e2e.get("action_revalidation_status"),
            revalidation.get("revalidation_status"),
        ),
        "guard_status": _first(
            e2e.get("guard_status"),
            guard.get("guard_status"),
            incident_graph.get("decision_status"),
        ),
        "dispatch_prevented_by_mission_assurance": _first(
            e2e.get("dispatch_prevented_by_mission_assurance"),
            guard.get("dispatch_prevented_by_mission_assurance"),
            incident_graph.get("dispatch_prevented_by_mission_assurance"),
            False,
        ),
        "suppression_source": _first(
            e2e.get("suppression_source"),
            guard.get("suppression_source"),
            incident_graph.get("suppression_source"),
        ),
        "suppression_reason": _first(
            e2e.get("suppression_reason"), guard.get("suppression_reason")
        ),
        "post_suppression_reobservation_observed": _first(
            e2e.get("post_suppression_reobservation_observed"),
            bool(_mapping(guard.get("post_suppression_observation"))),
        ),
        "agent_disagreement_observed": _first(
            e2e.get("agent_disagreement_observed"),
            guard.get("agent_disagreement_observed"),
            False,
        ),
        "agent_disagreement_kind": _first(
            e2e.get("agent_disagreement_kind"),
            guard.get("agent_disagreement_kind"),
        ),
        "agent_disagreement_resolution": _first(
            e2e.get("agent_disagreement_resolution"),
            guard.get("agent_disagreement_resolution"),
        ),
        "selected_action": _first(
            e2e.get("selected_recovery_action"),
            guard.get("selected_recovery_action"),
        ),
        "proposed_action_awaiting_approval": _first(
            e2e.get("proposed_recovery_action"),
            guard.get("proposed_recovery_action"),
            approval_request.get("recovery_action"),
            (
                incident_graph.get("recovery_proposed_action")
                if incident_graph.get("decision_status")
                == "awaiting_operator_approval"
                else None
            ),
        ),
        "assurance_requested_action": _first(
            e2e.get("assurance_requested_action"),
            guard.get("assurance_requested_action"),
        ),
        "recovery_no_action_response": _first(
            e2e.get("recovery_no_action_response"),
            guard.get("recovery_no_action_response"),
        ),
        "fresh_operator_approval_required": (
            e2e.get("fresh_operator_approval_required") is True
            or approval_request.get("requires_new_human_approval") is True
            or incident_graph.get("operator_approval_required") is True
        ),
        "recovery_approval_status": _first(
            e2e.get("recovery_approval_status"),
            approval_request.get("request_status"),
        ),
        "human_approval_consumed": e2e.get("human_execution_approval_consumed"),
        "route_execution_approval_consumed": e2e.get(
            "human_execution_approval_consumed"
        ),
        "recovery_approval_recorded": _first(
            e2e.get("recovery_approval_recorded"),
            approval_request.get("approval_recorded"),
            False,
        ),
        "runtime_state_observed": _first(
            e2e.get("runtime_state_observed"), summary.get("recovery_state_observed")
        ),
        "runtime_state_label": _first(
            e2e.get("runtime_state_label"), summary.get("recovery_state_label")
        ),
        "command_ack_observed": _first(
            e2e.get("command_ack_observed"),
            summary.get("recovery_command_ack_observed"),
        ),
        "final_status": _first(e2e.get("final_status"), summary.get("final_status")),
        "agent_created_approval": _first(e2e.get("agent_created_approval"), False),
        "agent_created_dispatch_authority": _first(
            e2e.get("agent_created_dispatch_authority"), False
        ),
        "physical_execution_invoked": _first(
            e2e.get("physical_execution_invoked"),
            guard.get("physical_execution_invoked"),
            False,
        ),
        "source_artifact": (
            "missionos_mission_assurance_gateway_px4_e2e"
            if e2e
            else "missionos_mission_incident_graph"
            if incident_graph
            else "missionos_mission_assurance_px4_horizontal_summary.mission_assurance_live_guard"
        ),
    }
