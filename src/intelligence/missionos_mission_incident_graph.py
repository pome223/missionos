"""One ADK v2 graph for Recovery and Mission Assurance judgment.

The graph owns sequencing only.  Recovery and Mission Assurance remain
proposal-only LLM nodes; Rules materialize source Action Feasibility between
them.  Human approval, dispatch, execution, effect observation, and verifier
truth remain outside Agent output and are represented only as downstream
checkpoints.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from src.intelligence.mission_assurance_agent import (
    MissionAssuranceAgent,
    MissionSituation,
    configured_mission_assurance_agent,
)

MISSION_INCIDENT_GRAPH_SCHEMA_VERSION = (
    "missionos_adk_v2_mission_incident_graph_result.v1"
)
MISSION_INCIDENT_GRAPH_WORKFLOW_NAME = "missionos_mission_incident_v2"
MISSION_INCIDENT_GRAPH_NODE_SEQUENCE = (
    "observe_mission_incident",
    "invoke_runtime_recovery_agent",
    "materialize_source_action_feasibility",
    "invoke_mission_assurance_agent",
    "resolve_mission_incident_checkpoint",
    "finalize_mission_incident",
)

_NO_ACTION_RECOVERY_RESPONSES = {
    "continue": "continue",
    "hold": "hold",
    "operator_review": "operator_escalation",
}
_ACTION_RESPONSE_ALIGNMENT = {
    "adjust_altitude": "replan",
    "adjust_speed": "replan",
    "avoid_obstacle": "replan",
    "reroute": "replan",
    "return_to_launch": "return",
    "return_home": "return",
    "land": "abort",
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _content_text(value: Any) -> str:
    parts = getattr(value, "parts", None) or []
    return "".join(
        str(getattr(part, "text", "") or "")
        for part in parts
        if getattr(part, "text", None)
    )


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _recovery_action(result: Mapping[str, Any]) -> str:
    assessment = _mapping(result.get("assessment"))
    return str(assessment.get("selected_bounded_action") or "").strip()


def _source_feasibility(result: Mapping[str, Any]) -> dict[str, Any]:
    assessment = _mapping(result.get("assessment"))
    feasibility = _mapping(assessment.get("action_feasibility"))
    if feasibility:
        return feasibility
    candidate = _mapping(assessment.get("recovery_planner_tool_candidate"))
    return _mapping(candidate.get("action_feasibility"))


def _hosted_recovery_agent_invoked(result: Mapping[str, Any]) -> bool:
    return any(
        (
            str(item.get("agent_name") or "") == "missionos_runtime_recovery_agent"
            or (item.get("agent_role") == "recovery" and bool(item.get("provider")))
        )
        and str(item.get("provider") or "") != "deterministic"
        and str(item.get("invocation_kind") or "")
        != "deterministic_guardrail_fallback"
        for item in result.get("agent_invocations") or ()
        if isinstance(item, Mapping)
    )


def _recovery_judgment_binding(
    *,
    mission_context: Mapping[str, Any],
    recovery_result: Mapping[str, Any],
) -> dict[str, Any]:
    raw = _mapping(mission_context.get("recovery_judgment_binding"))
    source_graph = _mapping(raw.get("source_mission_incident_graph"))
    source_digest_payload = {
        key: value
        for key, value in source_graph.items()
        if key
        not in {
            "mission_incident_graph_id",
            "mission_incident_graph_sha256",
            "workflow_node_paths",
        }
    }
    expected_digest = str(source_graph.get("mission_incident_graph_sha256") or "")
    inherited_binding = _mapping(source_graph.get("recovery_judgment_binding"))
    inherited_binding_digest = str(
        inherited_binding.get("recovery_judgment_binding_sha256") or ""
    )
    inherited_binding_payload = {
        key: value
        for key, value in inherited_binding.items()
        if key
        not in {
            "recovery_judgment_binding_id",
            "recovery_judgment_binding_sha256",
        }
    }
    inherited_source_verified = bool(
        source_graph.get("recovery_judgment_inherited") is True
        and inherited_binding.get("schema_version")
        == "missionos_recovery_judgment_binding.v1"
        and inherited_binding.get("binding_status") == "verified"
        and inherited_binding_digest
        and inherited_binding_digest
        == _canonical_sha256(inherited_binding_payload)
    )
    reasons: list[str] = []
    if raw.get("binding_mode") != "deterministic_recompile_of_prior_judgment":
        reasons.append("recovery_judgment_binding_mode_invalid")
    if not str(raw.get("source_proposal_id") or ""):
        reasons.append("recovery_judgment_source_proposal_missing")
    if (
        source_graph.get("schema_version")
        != MISSION_INCIDENT_GRAPH_SCHEMA_VERSION
    ):
        reasons.append("recovery_judgment_source_graph_invalid")
    if (
        not expected_digest
        or expected_digest != _canonical_sha256(source_digest_payload)
    ):
        reasons.append("recovery_judgment_source_graph_hash_mismatch")
    if source_graph.get("graph_runtime_status") != "proposal_guardrail_passed":
        reasons.append("recovery_judgment_source_graph_not_accepted")
    if (
        source_graph.get("recovery_agent_invoked") is not True
        and not inherited_source_verified
    ):
        reasons.append("recovery_judgment_source_agent_not_observed")
    if source_graph.get("mission_assurance_agent_invoked") is not True:
        reasons.append("recovery_judgment_source_assurance_not_observed")
    current_action = _recovery_action(recovery_result)
    if str(source_graph.get("recovery_proposed_action") or "") != current_action:
        reasons.append("recovery_judgment_recompiled_action_mismatch")
    source_situation = _mapping(source_graph.get("mission_situation"))
    evidence = {
        "schema_version": "missionos_recovery_judgment_binding.v1",
        "binding_mode": "deterministic_recompile_of_prior_judgment",
        "binding_status": "blocked" if reasons else "verified",
        "source_proposal_id": str(raw.get("source_proposal_id") or ""),
        "source_mission_incident_graph_id": str(
            source_graph.get("mission_incident_graph_id") or ""
        ),
        "source_mission_incident_graph_sha256": expected_digest,
        "source_mission_situation_id": str(
            source_situation.get("situation_id") or ""
        ),
        "source_recovery_action": str(
            source_graph.get("recovery_proposed_action") or ""
        ),
        "source_recovery_judgment_inherited": inherited_source_verified,
        "recompiled_recovery_action": current_action,
        "fresh_mission_assurance_required": True,
        "blocking_reasons": reasons,
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    digest = _canonical_sha256(evidence)
    return {
        **evidence,
        "recovery_judgment_binding_sha256": digest,
        "recovery_judgment_binding_id": f"recovery_judgment_binding_{digest[:12]}",
    }


def _build_situation(
    *,
    telemetry_snapshot: Mapping[str, Any],
    mission_context: Mapping[str, Any],
    recovery_result: Mapping[str, Any],
    source_feasibility: Mapping[str, Any],
) -> MissionSituation:
    action = _recovery_action(recovery_result)
    configured_response_kinds = tuple(
        str(item).strip()
        for item in mission_context.get("allowed_response_kinds") or ()
        if str(item).strip()
    )
    allowed_response_kinds = configured_response_kinds or tuple(
        sorted(
            {
                "continue",
                "hold",
                "operator_escalation",
                "replan",
                "return",
                *({"abort"} if action == "land" else set()),
            }
        )
    )
    source = {
        "telemetry_snapshot": dict(telemetry_snapshot),
        "mission_context": dict(mission_context),
        "recovery_action": action,
        "recovery_result_sha256": _canonical_sha256(recovery_result),
        "source_action_feasibility": dict(source_feasibility),
    }
    input_digest = _canonical_sha256(source)
    observed_at = str(
        telemetry_snapshot.get("observed_at")
        or mission_context.get("observed_at")
        or datetime.now(timezone.utc).isoformat()
    )
    return MissionSituation(
        situation_id=f"mission_situation_{input_digest[:12]}",
        observed_at=observed_at,
        mission_contract={
            "objective": "preserve the declared mission within active constraints",
            "mission_context": _mapping(mission_context.get("mission_contract")),
        },
        progress={
            "mission_phase": mission_context.get("mission_phase"),
            "task_id": mission_context.get("task_id"),
            "delivery_completion_claimed": False,
            "mission_context": _mapping(mission_context.get("progress")),
        },
        observations={
            "runtime_telemetry": dict(telemetry_snapshot),
            "runtime_recovery_agent_result": dict(recovery_result),
            "source_action_feasibility": dict(source_feasibility),
            "mission_context": _mapping(mission_context.get("observations")),
        },
        constraints={
            "recovery_proposal_is_not_approval": True,
            "source_action_feasibility_required_before_action_acceptance": True,
            "mission_assurance_must_not_modify_recovery_action": True,
            "mission_context": _mapping(mission_context.get("constraints")),
        },
        uncertainty={
            "unverified_is_not_dispatchable": True,
            "mission_context": _mapping(mission_context.get("uncertainty")),
        },
        source_refs=(
            f"runtime_recovery_agent:{mission_context.get('task_id') or 'unbound'}",
            *tuple(str(item) for item in mission_context.get("source_refs") or ()),
        ),
        source_schema_version=str(
            recovery_result.get("schema_version")
            or "missionos_runtime_recovery_agent_result.v1"
        ),
        input_digest=input_digest,
        execution_scope=str(
            mission_context.get("execution_scope") or "simulator"
        ),
        allowed_response_kinds=allowed_response_kinds,
    )


def _authority_floor() -> dict[str, Any]:
    return {
        "proposal_only": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "dispatch_request_sent": False,
        "executor_invoked": False,
        "command_ack_observed": False,
        "effect_observed": False,
        "verifier_status": "not_started",
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
        "progress_counted": False,
    }


async def _run_mission_incident_graph_async(
    *,
    telemetry_snapshot: Mapping[str, Any],
    mission_context: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    recovery_runner: Callable[..., Mapping[str, Any]],
    mission_assurance_agent: MissionAssuranceAgent,
    mission_assurance_timeout_seconds: float | None,
) -> dict[str, Any]:
    from google.adk import Workflow
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.workflow import node
    from google.genai import types

    node_names = dict(
        zip(
            (
                "observe",
                "recovery",
                "feasibility",
                "assurance",
                "checkpoint",
                "finalize",
            ),
            MISSION_INCIDENT_GRAPH_NODE_SEQUENCE,
            strict=True,
        )
    )

    @node(name=node_names["observe"], rerun_on_resume=False)
    async def observe_mission_incident(node_input: Any) -> dict[str, Any]:
        try:
            payload = json.loads(_content_text(node_input))
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "graph_runtime_status": "running",
            "blocking_reasons": [],
            "input": payload,
            "graph_node_sequence": [node_names["observe"]],
        }

    @node(name=node_names["recovery"], rerun_on_resume=True)
    async def invoke_runtime_recovery_agent(
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(node_input)
        state["graph_node_sequence"] = [
            *list(state.get("graph_node_sequence") or []),
            node_names["recovery"],
        ]
        payload = _mapping(state.get("input"))
        result = await asyncio.to_thread(
            recovery_runner,
            telemetry_snapshot=_mapping(payload.get("telemetry_snapshot")),
            mission_context=_mapping(payload.get("mission_context")),
            recovery_policy=_mapping(payload.get("recovery_policy")),
        )
        state["recovery_result"] = _mapping(result)
        if state["recovery_result"].get("runtime_status") != "proposal_guardrail_passed":
            state["graph_runtime_status"] = "guardrail_blocked"
            state["blocking_reasons"] = list(
                state["recovery_result"].get("blocking_reasons") or []
            ) or ["runtime_recovery_agent_proposal_not_accepted"]
            return state
        hosted_invocation = _hosted_recovery_agent_invoked(
            state["recovery_result"]
        )
        state["recovery_agent_invoked"] = hosted_invocation
        if not hosted_invocation:
            binding = _recovery_judgment_binding(
                mission_context=_mapping(payload.get("mission_context")),
                recovery_result=state["recovery_result"],
            )
            state["recovery_judgment_binding"] = binding
            if binding.get("binding_status") != "verified":
                state["graph_runtime_status"] = "guardrail_blocked"
                state["blocking_reasons"] = list(
                    binding.get("blocking_reasons") or []
                ) or ["recovery_agent_inference_or_binding_required"]
        return state

    @node(name=node_names["feasibility"], rerun_on_resume=True)
    async def materialize_source_action_feasibility(
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(node_input)
        state["graph_node_sequence"] = [
            *list(state.get("graph_node_sequence") or []),
            node_names["feasibility"],
        ]
        if state.get("graph_runtime_status") != "running":
            return state
        recovery_result = _mapping(state.get("recovery_result"))
        action = _recovery_action(recovery_result)
        feasibility = _source_feasibility(recovery_result)
        state["recovery_action"] = action
        state["source_action_feasibility"] = feasibility
        if not action:
            state["graph_runtime_status"] = "guardrail_blocked"
            state["blocking_reasons"] = ["runtime_recovery_agent_action_missing"]
        elif action not in _NO_ACTION_RECOVERY_RESPONSES and (
            feasibility.get("feasibility_status") != "verified_feasible"
            or str(feasibility.get("action") or "") != action
        ):
            state["graph_runtime_status"] = "guardrail_blocked"
            state["blocking_reasons"] = [
                "source_action_feasibility_not_verified_for_recovery_action"
            ]
        return state

    @node(name=node_names["assurance"], rerun_on_resume=True)
    async def invoke_mission_assurance_agent(
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(node_input)
        state["graph_node_sequence"] = [
            *list(state.get("graph_node_sequence") or []),
            node_names["assurance"],
        ]
        if state.get("graph_runtime_status") != "running":
            return state
        payload = _mapping(state.get("input"))
        mission_context = _mapping(payload.get("mission_context"))
        if state.get("recovery_judgment_binding"):
            mission_context["recovery_judgment_binding"] = _mapping(
                state.get("recovery_judgment_binding")
            )
        situation = _build_situation(
            telemetry_snapshot=_mapping(payload.get("telemetry_snapshot")),
            mission_context=mission_context,
            recovery_result=_mapping(state.get("recovery_result")),
            source_feasibility=_mapping(state.get("source_action_feasibility")),
        )
        evaluation = asyncio.to_thread(
            mission_assurance_agent.evaluate,
            situation,
        )
        proposal = (
            await asyncio.wait_for(
                evaluation,
                timeout=mission_assurance_timeout_seconds,
            )
            if mission_assurance_timeout_seconds is not None
            else await evaluation
        )
        state["mission_situation"] = situation.to_dict()
        state["mission_assurance_proposal"] = proposal.to_dict()
        if proposal.judgment_status != "proposal_guardrail_passed":
            state["graph_runtime_status"] = "guardrail_blocked"
            state["blocking_reasons"] = list(proposal.blocking_reasons) or [
                "mission_assurance_agent_judgment_not_accepted"
            ]
        return state

    @node(name=node_names["checkpoint"], rerun_on_resume=True)
    async def resolve_mission_incident_checkpoint(
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(node_input)
        state["graph_node_sequence"] = [
            *list(state.get("graph_node_sequence") or []),
            node_names["checkpoint"],
        ]
        if state.get("graph_runtime_status") != "running":
            state["decision_status"] = "operator_escalation"
            state["alignment_status"] = "not_evaluated"
            return state
        action = str(state.get("recovery_action") or "")
        proposal = _mapping(state.get("mission_assurance_proposal"))
        response = str(proposal.get("proposed_response_kind") or "")
        expected = _NO_ACTION_RECOVERY_RESPONSES.get(action)
        actionable = expected is None
        if actionable:
            expected = _ACTION_RESPONSE_ALIGNMENT.get(action)
        state["expected_mission_response_kind"] = expected
        if not expected:
            state["graph_runtime_status"] = "guardrail_blocked"
            state["decision_status"] = "operator_escalation"
            state["alignment_status"] = "unsupported_recovery_action"
            state["blocking_reasons"] = [
                "recovery_action_has_no_mission_response_alignment"
            ]
        elif response == expected:
            state["alignment_status"] = "accepted"
            state["decision_status"] = (
                "awaiting_operator_approval" if actionable else "no_dispatch"
            )
        elif actionable and response in {
            "continue",
            "hold",
            "operator_escalation",
        }:
            state["alignment_status"] = "suppressed_by_mission_assurance"
            state["decision_status"] = "no_dispatch"
        elif action == "continue" and response in {
            "hold",
            "operator_escalation",
        }:
            state["alignment_status"] = (
                "mission_continuation_suppressed_by_mission_assurance"
            )
            state["decision_status"] = "no_dispatch"
        else:
            state["alignment_status"] = "agent_disagreement"
            state["decision_status"] = "operator_escalation"
        if state.get("graph_runtime_status") == "running":
            state["graph_runtime_status"] = "proposal_guardrail_passed"
        return state

    @node(name=node_names["finalize"], rerun_on_resume=False)
    async def finalize_mission_incident(
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(node_input)
        sequence = [
            *list(state.get("graph_node_sequence") or []),
            node_names["finalize"],
        ]
        proposal = _mapping(state.get("mission_assurance_proposal"))
        decision_status = str(
            state.get("decision_status") or "operator_escalation"
        )
        alignment_status = str(
            state.get("alignment_status") or "not_evaluated"
        )
        recovery_action = str(state.get("recovery_action") or "")
        response_kind = str(proposal.get("proposed_response_kind") or "")
        action_dispatch_suppressed = (
            alignment_status == "suppressed_by_mission_assurance"
        )
        mission_continuation_suppressed = (
            alignment_status
            == "mission_continuation_suppressed_by_mission_assurance"
        )
        terminal_boundary = (
            "operator_recovery_approval_boundary"
            if decision_status == "awaiting_operator_approval"
            else "next_mission_situation"
            if decision_status == "no_dispatch"
            else "operator_escalation_boundary"
        )
        result = {
            "schema_version": MISSION_INCIDENT_GRAPH_SCHEMA_VERSION,
            "workflow_name": MISSION_INCIDENT_GRAPH_WORKFLOW_NAME,
            "graph_runtime_status": str(
                state.get("graph_runtime_status") or "guardrail_blocked"
            ),
            "decision_status": decision_status,
            "alignment_status": alignment_status,
            "blocking_reasons": list(state.get("blocking_reasons") or []),
            "graph_node_sequence": sequence,
            "decision_sequence": [
                "runtime_recovery_agent",
                "source_action_feasibility",
                "mission_assurance_agent",
                terminal_boundary,
            ],
            "declared_next_sequence": [
                "operator_recovery_approval_boundary",
                "dispatch_time_action_feasibility_revalidation",
                "executor",
                "verifier",
                "next_mission_situation",
            ],
            "recovery_result": _mapping(state.get("recovery_result")),
            "recovery_judgment_binding": _mapping(
                state.get("recovery_judgment_binding")
            ),
            "recovery_proposed_action": recovery_action,
            "source_action_feasibility": _mapping(
                state.get("source_action_feasibility")
            ),
            "mission_situation": _mapping(state.get("mission_situation")),
            "mission_assurance_proposal": proposal,
            "mission_assurance_response_kind": response_kind,
            "expected_mission_response_kind": state.get(
                "expected_mission_response_kind"
            ),
            "recovery_agent_invoked": state.get("recovery_agent_invoked") is True,
            "recovery_judgment_inherited": (
                _mapping(state.get("recovery_judgment_binding")).get(
                    "binding_status"
                )
                == "verified"
            ),
            "recovery_judgment_available_before_mission_assurance": bool(
                state.get("recovery_agent_invoked") is True
                or _mapping(state.get("recovery_judgment_binding")).get(
                    "binding_status"
                )
                == "verified"
            ),
            "mission_assurance_agent_invoked": (
                proposal.get("model_inference_invoked") is True
            ),
            "recovery_agent_invoked_before_mission_assurance": bool(
                state.get("recovery_agent_invoked") is True and proposal
            ),
            "operator_approval_required": (
                state.get("decision_status") == "awaiting_operator_approval"
            ),
            "dispatch_prevented_by_mission_assurance": (
                action_dispatch_suppressed
            ),
            "mission_continuation_prevented_by_mission_assurance": (
                mission_continuation_suppressed
            ),
            "suppression_source": (
                "mission_assurance_agent"
                if action_dispatch_suppressed or mission_continuation_suppressed
                else None
            ),
            "suppression_reason": (
                f"mission_assurance_{response_kind}_suppressed_feasible_"
                "recovery_proposal"
                if action_dispatch_suppressed
                else f"mission_assurance_{response_kind}_prevented_mission_"
                "continuation"
                if mission_continuation_suppressed
                else None
            ),
            "suppressed_recovery_action": (
                recovery_action if action_dispatch_suppressed else None
            ),
            "suppressed_recovery_response": (
                recovery_action if mission_continuation_suppressed else None
            ),
            **_authority_floor(),
        }
        digest = _canonical_sha256(result)
        return {
            **result,
            "mission_incident_graph_sha256": digest,
            "mission_incident_graph_id": f"mission_incident_graph_{digest[:12]}",
        }

    workflow = Workflow(
        name=MISSION_INCIDENT_GRAPH_WORKFLOW_NAME,
        description=(
            "Recovery, source Rules feasibility, Mission Assurance, and the "
            "downstream human/runtime checkpoint in one ADK v2 graph."
        ),
        rerun_on_resume=True,
        edges=[
            (
                "START",
                observe_mission_incident,
                invoke_runtime_recovery_agent,
                materialize_source_action_feasibility,
                invoke_mission_assurance_agent,
                resolve_mission_incident_checkpoint,
                finalize_mission_incident,
            )
        ],
    )
    session_service = InMemorySessionService()
    app_name = "missionos_mission_incident"
    user_id = "missionos_operator"
    session = await session_service.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(agent=workflow, app_name=app_name, session_service=session_service)
    graph_input = {
        "telemetry_snapshot": dict(telemetry_snapshot),
        "mission_context": dict(mission_context),
        "recovery_policy": dict(recovery_policy),
    }
    content = types.Content(
        role="user",
        parts=[types.Part(text=json.dumps(graph_input, sort_keys=True, default=str))],
    )
    final_output: dict[str, Any] = {}
    workflow_node_paths: list[str] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=content,
    ):
        node_info = getattr(event, "node_info", None)
        node_path = str(getattr(node_info, "path", "") or "")
        if node_path and node_path not in workflow_node_paths:
            workflow_node_paths.append(node_path)
        if isinstance(event.output, Mapping):
            final_output = dict(event.output)
    if final_output.get("schema_version") != MISSION_INCIDENT_GRAPH_SCHEMA_VERSION:
        raise RuntimeError("mission_incident_graph_final_output_missing")
    final_output["workflow_node_paths"] = workflow_node_paths
    return final_output


def run_missionos_mission_incident_graph(
    *,
    telemetry_snapshot: Mapping[str, Any],
    mission_context: Mapping[str, Any] | None,
    recovery_policy: Mapping[str, Any] | None,
    recovery_runner: Callable[..., Mapping[str, Any]],
    mission_assurance_agent: MissionAssuranceAgent | None = None,
    mission_assurance_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run the shared proposal graph without creating downstream authority."""

    return asyncio.run(
        _run_mission_incident_graph_async(
            telemetry_snapshot=telemetry_snapshot,
            mission_context=dict(mission_context or {}),
            recovery_policy=dict(recovery_policy or {}),
            recovery_runner=recovery_runner,
            mission_assurance_agent=(
                mission_assurance_agent or configured_mission_assurance_agent()
            ),
            mission_assurance_timeout_seconds=(
                mission_assurance_timeout_seconds
            ),
        )
    )


__all__ = [
    "MISSION_INCIDENT_GRAPH_NODE_SEQUENCE",
    "MISSION_INCIDENT_GRAPH_SCHEMA_VERSION",
    "MISSION_INCIDENT_GRAPH_WORKFLOW_NAME",
    "run_missionos_mission_incident_graph",
]
