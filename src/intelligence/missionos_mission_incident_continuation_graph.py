"""Post-approval continuation for a frozen Mission Incident judgment.

This graph never reruns Recovery or Mission Assurance.  It accepts only the
hash-bound output of the judgment graph, observes individual human approval or
checks an explicitly injected human-approved policy, revalidates the action,
invokes one executor boundary, verifies the returned
receipt, and creates the next observation as distinct facts.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from src.intelligence.missionos_mission_incident_graph import (
    MISSION_INCIDENT_GRAPH_NODE_SEQUENCE,
    MISSION_INCIDENT_GRAPH_SCHEMA_VERSION,
    MISSION_INCIDENT_GRAPH_WORKFLOW_NAME,
)

MISSION_INCIDENT_CONTINUATION_SCHEMA_VERSION = (
    "missionos_adk_v2_mission_incident_continuation_result.v1"
)
MISSION_INCIDENT_CONTINUATION_WORKFLOW_NAME = (
    "missionos_mission_incident_continuation_v1"
)
MISSION_INCIDENT_CONTINUATION_NODE_SEQUENCE = (
    "bind_frozen_mission_incident_judgment",
    "observe_explicit_operator_approval",
    "revalidate_dispatch_time_action_feasibility",
    "invoke_recovery_executor_boundary",
    "verify_recovery_execution_evidence",
    "observe_next_mission_situation",
    "finalize_mission_incident_continuation",
)

ContinuationHandler = Callable[
    [Mapping[str, Any]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


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


def _incident_graph_hash_payload(graph: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in graph.items()
        if key
        not in {
            "mission_incident_graph_id",
            "mission_incident_graph_sha256",
            "workflow_node_paths",
        }
    }


def _frozen_graph_reasons(graph: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    expected_hash = str(graph.get("mission_incident_graph_sha256") or "")
    observed_hash = _canonical_sha256(_incident_graph_hash_payload(graph))
    if graph.get("schema_version") != MISSION_INCIDENT_GRAPH_SCHEMA_VERSION:
        reasons.append("frozen_mission_incident_graph_schema_invalid")
    if graph.get("workflow_name") != MISSION_INCIDENT_GRAPH_WORKFLOW_NAME:
        reasons.append("frozen_mission_incident_graph_workflow_invalid")
    if graph.get("graph_runtime_status") != "proposal_guardrail_passed":
        reasons.append("frozen_mission_incident_graph_not_accepted")
    if graph.get("decision_status") != "awaiting_operator_approval":
        reasons.append("frozen_mission_incident_graph_not_awaiting_approval")
    if graph.get("alignment_status") != "accepted":
        reasons.append("frozen_mission_incident_graph_alignment_not_accepted")
    if graph.get("operator_approval_required") is not True:
        reasons.append("frozen_mission_incident_graph_approval_not_required")
    if tuple(graph.get("graph_node_sequence") or ()) != (
        MISSION_INCIDENT_GRAPH_NODE_SEQUENCE
    ):
        reasons.append("frozen_mission_incident_graph_node_sequence_invalid")
    if not expected_hash or expected_hash != observed_hash:
        reasons.append("frozen_mission_incident_graph_hash_mismatch")
    expected_id = f"mission_incident_graph_{observed_hash[:12]}"
    if str(graph.get("mission_incident_graph_id") or "") != expected_id:
        reasons.append("frozen_mission_incident_graph_id_mismatch")
    if not str(graph.get("recovery_proposed_action") or ""):
        reasons.append("frozen_mission_incident_graph_recovery_action_missing")
    if graph.get("recovery_judgment_available_before_mission_assurance") is not True:
        reasons.append("frozen_recovery_judgment_not_observed")
    if graph.get("mission_assurance_agent_invoked") is not True:
        reasons.append("frozen_mission_assurance_judgment_not_observed")
    for field in (
        "approval_created",
        "dispatch_authority_created",
        "dispatch_request_sent",
        "executor_invoked",
        "command_ack_observed",
        "effect_observed",
        "physical_execution_invoked",
        "progress_counted",
        "delivery_completion_claimed",
    ):
        if graph.get(field) is not False:
            reasons.append(f"frozen_mission_incident_graph_{field}_must_be_false")
    return list(dict.fromkeys(reasons))


async def _resolve(
    handler: ContinuationHandler,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    result = handler(state)
    if inspect.isawaitable(result):
        result = await result
    return dict(result) if isinstance(result, Mapping) else {}


async def _run_mission_incident_continuation_async(
    *,
    frozen_mission_incident_graph: Mapping[str, Any],
    continuation_request: Mapping[str, Any],
    action_revalidation_handler: ContinuationHandler,
    executor_handler: ContinuationHandler,
    verifier_handler: ContinuationHandler,
    observation_handler: ContinuationHandler,
    policy_authorization_handler: ContinuationHandler | None = None,
) -> dict[str, Any]:
    from google.adk import Workflow
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.workflow import node
    from google.genai import types

    names = dict(
        zip(
            (
                "bind",
                "approval",
                "revalidation",
                "executor",
                "verifier",
                "observe",
                "finalize",
            ),
            MISSION_INCIDENT_CONTINUATION_NODE_SEQUENCE,
            strict=True,
        )
    )

    @node(name=names["bind"], rerun_on_resume=False)
    def bind_frozen_mission_incident_judgment(node_input: Any) -> dict[str, Any]:
        try:
            payload = json.loads(_content_text(node_input))
        except (TypeError, json.JSONDecodeError):
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        graph = _mapping(payload.get("frozen_mission_incident_graph"))
        request = _mapping(payload.get("continuation_request"))
        reasons = _frozen_graph_reasons(graph)
        expected_action = str(graph.get("recovery_proposed_action") or "")
        if str(request.get("recovery_action") or "") != expected_action:
            reasons.append("continuation_recovery_action_mismatch")
        return {
            "continuation_runtime_status": (
                "running" if not reasons else "blocked"
            ),
            "blocking_reasons": list(dict.fromkeys(reasons)),
            "frozen_mission_incident_graph": graph,
            "continuation_request": request,
            "frozen_mission_incident_graph_id": graph.get(
                "mission_incident_graph_id"
            ),
            "frozen_mission_incident_graph_sha256": graph.get(
                "mission_incident_graph_sha256"
            ),
            "recovery_action": expected_action,
            "continuation_node_sequence": [names["bind"]],
            "recovery_agent_rerun": False,
            "mission_assurance_agent_rerun": False,
        }

    @node(name=names["approval"], rerun_on_resume=False)
    def observe_explicit_operator_approval(
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(node_input)
        state["continuation_node_sequence"] = [
            *list(state.get("continuation_node_sequence") or []),
            names["approval"],
        ]
        if state.get("continuation_runtime_status") != "running":
            return state
        request = _mapping(state.get("continuation_request"))
        reasons: list[str] = []
        if request.get("explicit_recovery_dispatch_approval") is not True:
            if policy_authorization_handler is None:
                reasons.append("explicit_recovery_dispatch_approval_required")
            else:
                # Trusted Python composition, never a request-supplied verdict.
                state["policy_authorization_pending"] = True
        if not str(request.get("task_id") or ""):
            reasons.append("continuation_task_id_missing")
        if not str(request.get("proposal_id") or ""):
            reasons.append("continuation_proposal_id_missing")
        state["human_approval"] = {
            "approval_status": (
                "blocked" if reasons else "not_observed"
                if state.get("policy_authorization_pending") else "observed"
            ),
            "task_id": request.get("task_id"),
            "proposal_id": request.get("proposal_id"),
            "recovery_action": request.get("recovery_action"),
            "recovery_parameters": _mapping(request.get("recovery_parameters")),
            "explicit_recovery_dispatch_approval": (
                request.get("explicit_recovery_dispatch_approval") is True
            ),
            "human_approval_observed": (
                not reasons and request.get("explicit_recovery_dispatch_approval") is True
            ),
            "approval_created_by_graph": False,
            "dispatch_authority_created": False,
        }
        if reasons:
            state["continuation_runtime_status"] = "blocked"
            state["blocking_reasons"] = reasons
        return state

    @node(name=names["revalidation"], rerun_on_resume=False)
    async def revalidate_dispatch_time_action_feasibility(
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(node_input)
        state["continuation_node_sequence"] = [
            *list(state.get("continuation_node_sequence") or []),
            names["revalidation"],
        ]
        if state.get("continuation_runtime_status") != "running":
            # Invalid legacy or mutated proposals still receive the same
            # read-only revalidation diagnostics, but can never reach the
            # executor node.
            revalidation = await _resolve(action_revalidation_handler, state)
            state["action_revalidation"] = revalidation
            state["blocking_reasons"] = list(
                dict.fromkeys(
                    [
                        *list(state.get("blocking_reasons") or []),
                        *list(
                            revalidation.get("reasons")
                            or revalidation.get("blocking_reasons")
                            or []
                        ),
                    ]
                )
            )
            return state
        revalidation = await _resolve(action_revalidation_handler, state)
        state["action_revalidation"] = revalidation
        request = _mapping(state.get("continuation_request"))
        valid = bool(
            revalidation.get("validation_status") == "valid"
            and str(revalidation.get("proposal_id") or "")
            == str(request.get("proposal_id") or "")
        )
        if not valid:
            state["continuation_runtime_status"] = "blocked"
            state["blocking_reasons"] = list(
                revalidation.get("reasons")
                or revalidation.get("blocking_reasons")
                or ["dispatch_time_action_revalidation_blocked"]
            )
        return state

    @node(name=names["executor"], rerun_on_resume=False)
    async def invoke_recovery_executor_boundary(
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(node_input)
        state["continuation_node_sequence"] = [
            *list(state.get("continuation_node_sequence") or []),
            names["executor"],
        ]
        if state.get("continuation_runtime_status") != "running":
            return state
        if policy_authorization_handler is not None:
            policy_state = {**state, "policy_check_phase": "before_executor"}
            policy_result = await _resolve(policy_authorization_handler, policy_state)
            state["policy_authorization"] = policy_result
            policy_required = (
                state.get("policy_authorization_pending")
                or policy_result.get("mode") not in {"human", "shadow"}
            )
            if policy_required and policy_result.get("policy_authorized") is not True:
                state["continuation_runtime_status"] = "blocked"
                state["blocking_reasons"] = list(
                    policy_result.get("blocking_reasons")
                    or ["individual_human_approval_required_by_policy_mode"]
                )
                return state
        execution = await _resolve(executor_handler, state)
        state["execution"] = execution
        if execution.get("executor_invoked") is True:
            state["continuation_runtime_status"] = "executor_returned"
            state["blocking_reasons"] = list(
                dict.fromkeys(
                    [
                        *list(state.get("blocking_reasons") or []),
                        *list(execution.get("blocking_reasons") or []),
                    ]
                )
            )
        else:
            state["continuation_runtime_status"] = "blocked"
            state["blocking_reasons"] = list(
                execution.get("blocking_reasons")
                or ["recovery_executor_not_invoked"]
            )
        return state

    @node(name=names["verifier"], rerun_on_resume=False)
    async def verify_recovery_execution_evidence(
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(node_input)
        state["continuation_node_sequence"] = [
            *list(state.get("continuation_node_sequence") or []),
            names["verifier"],
        ]
        if state.get("continuation_runtime_status") != "executor_returned":
            return state
        verification = await _resolve(verifier_handler, state)
        state["verification"] = verification
        state["continuation_runtime_status"] = "verifier_returned"
        return state

    @node(name=names["observe"], rerun_on_resume=False)
    async def observe_next_mission_situation(
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(node_input)
        state["continuation_node_sequence"] = [
            *list(state.get("continuation_node_sequence") or []),
            names["observe"],
        ]
        if state.get("continuation_runtime_status") != "verifier_returned":
            return state
        observation = await _resolve(observation_handler, state)
        state["next_mission_situation"] = observation
        execution = _mapping(state.get("execution"))
        state["continuation_runtime_status"] = (
            "completed"
            if execution.get("dispatch_request_sent") is True
            else "completed_without_dispatch"
        )
        return state

    @node(name=names["finalize"], rerun_on_resume=False)
    def finalize_mission_incident_continuation(
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(node_input)
        sequence = [
            *list(state.get("continuation_node_sequence") or []),
            names["finalize"],
        ]
        approval = _mapping(state.get("human_approval"))
        execution = _mapping(state.get("execution"))
        verification = _mapping(state.get("verification"))
        next_situation = _mapping(state.get("next_mission_situation"))
        result = {
            "schema_version": MISSION_INCIDENT_CONTINUATION_SCHEMA_VERSION,
            "workflow_name": MISSION_INCIDENT_CONTINUATION_WORKFLOW_NAME,
            "continuation_runtime_status": str(
                state.get("continuation_runtime_status") or "blocked"
            ),
            "blocking_reasons": list(state.get("blocking_reasons") or []),
            "continuation_node_sequence": sequence,
            "frozen_mission_incident_graph_id": state.get(
                "frozen_mission_incident_graph_id"
            ),
            "frozen_mission_incident_graph_sha256": state.get(
                "frozen_mission_incident_graph_sha256"
            ),
            "recovery_action": state.get("recovery_action"),
            "recovery_agent_rerun": False,
            "mission_assurance_agent_rerun": False,
            "human_approval": approval,
            "policy_authorization": _mapping(state.get("policy_authorization")),
            "authorization_source": (
                "individual_human_approval" if approval.get("human_approval_observed")
                else "human_approved_policy"
                if _mapping(state.get("policy_authorization")).get("policy_authorized")
                else "none"
            ),
            "action_revalidation": _mapping(state.get("action_revalidation")),
            "execution": execution,
            "verification": verification,
            "next_mission_situation": next_situation,
            "human_approval_observed": approval.get("human_approval_observed")
            is True,
            "dispatch_authority_created": execution.get(
                "dispatch_authority_created"
            )
            is True,
            "dispatch_request_sent": execution.get("dispatch_request_sent")
            is True,
            "executor_invoked": execution.get("executor_invoked") is True,
            "command_ack_observed": bool(
                execution.get("command_ack_observed") is True
                or verification.get("command_ack_observed") is True
            ),
            "effect_observed": verification.get("effect_observed") is True,
            "verifier_status": str(
                verification.get("verifier_status") or "not_started"
            ),
            "next_mission_situation_created": next_situation.get(
                "next_mission_situation_created"
            )
            is True,
            "physical_execution_invoked": execution.get(
                "physical_execution_invoked"
            )
            is True,
            "progress_counted": verification.get("progress_counted") is True,
            "delivery_completion_claimed": verification.get(
                "delivery_completion_claimed"
            )
            is True,
        }
        digest = _canonical_sha256(result)
        return {
            **result,
            "continuation_graph_sha256": digest,
            "continuation_graph_id": (
                f"mission_incident_continuation_{digest[:12]}"
            ),
        }

    workflow = Workflow(
        name=MISSION_INCIDENT_CONTINUATION_WORKFLOW_NAME,
        description=(
            "Resume a frozen Mission Incident judgment through human approval, "
            "fresh Rules revalidation, Executor, Verifier, and re-observation."
        ),
        # ADK v2 requires a Workflow parent to be rerunnable so it can schedule
        # child nodes. This continuation has no resume entrypoint, and every
        # child that binds authority or invokes a side effect is non-rerunnable.
        rerun_on_resume=True,
        edges=[
            (
                "START",
                bind_frozen_mission_incident_judgment,
                observe_explicit_operator_approval,
                revalidate_dispatch_time_action_feasibility,
                invoke_recovery_executor_boundary,
                verify_recovery_execution_evidence,
                observe_next_mission_situation,
                finalize_mission_incident_continuation,
            )
        ],
    )
    service = InMemorySessionService()
    app_name = "missionos_mission_incident_continuation"
    user_id = "missionos_operator"
    session = await service.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(agent=workflow, app_name=app_name, session_service=service)
    content = types.Content(
        role="user",
        parts=[
            types.Part(
                text=json.dumps(
                    {
                        "frozen_mission_incident_graph": dict(
                            frozen_mission_incident_graph
                        ),
                        "continuation_request": dict(continuation_request),
                    },
                    sort_keys=True,
                    default=str,
                )
            )
        ],
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
    if (
        final_output.get("schema_version")
        != MISSION_INCIDENT_CONTINUATION_SCHEMA_VERSION
    ):
        raise RuntimeError("mission_incident_continuation_final_output_missing")
    final_output["workflow_node_paths"] = workflow_node_paths
    return final_output


def run_missionos_mission_incident_continuation_graph(
    *,
    frozen_mission_incident_graph: Mapping[str, Any],
    continuation_request: Mapping[str, Any],
    action_revalidation_handler: ContinuationHandler,
    executor_handler: ContinuationHandler,
    verifier_handler: ContinuationHandler,
    observation_handler: ContinuationHandler,
    policy_authorization_handler: ContinuationHandler | None = None,
) -> dict[str, Any]:
    """Run the post-approval graph without rerunning either LLM judgment."""

    return asyncio.run(
        _run_mission_incident_continuation_async(
            frozen_mission_incident_graph=frozen_mission_incident_graph,
            continuation_request=continuation_request,
            action_revalidation_handler=action_revalidation_handler,
            executor_handler=executor_handler,
            verifier_handler=verifier_handler,
            observation_handler=observation_handler,
            policy_authorization_handler=policy_authorization_handler,
        )
    )


__all__ = [
    "MISSION_INCIDENT_CONTINUATION_NODE_SEQUENCE",
    "MISSION_INCIDENT_CONTINUATION_SCHEMA_VERSION",
    "MISSION_INCIDENT_CONTINUATION_WORKFLOW_NAME",
    "run_missionos_mission_incident_continuation_graph",
]
