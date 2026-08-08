"""ADK v2 graph for the MissionOS conversation/proposal path.

The same proposal-only graph can run as a measurement-only shadow or as the
primary proposal generator. Neither mode can approve, create dispatch
authority, invoke an executor, claim an observed effect, or count mission
progress.
"""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import os
from typing import Any, Mapping


MISSIONOS_ADK_V2_GRAPH_SHADOW_ENV = "MISSIONOS_ADK_V2_GRAPH_SHADOW"
MISSIONOS_ADK_V2_GRAPH_PRIMARY_ENV = "MISSIONOS_ADK_V2_GRAPH_PRIMARY"
MISSIONOS_ADK_V2_GRAPH_ROLLBACK_ENV = "MISSIONOS_ADK_V2_GRAPH_ROLLBACK"
MISSIONOS_ADK_V2_SHADOW_RESULT_SCHEMA_VERSION = "missionos_adk_v2_conversation_shadow_result.v1"
MISSIONOS_ADK_V2_PROPOSAL_RESULT_SCHEMA_VERSION = (
    "missionos_adk_v2_conversation_proposal_graph_result.v1"
)
MISSIONOS_ADK_V2_SHADOW_COMPARISON_SCHEMA_VERSION = (
    "missionos_adk_v2_conversation_shadow_comparison.v1"
)
MISSIONOS_ADK_V2_SHADOW_WORKFLOW_NAME = "missionos_conversation_proposal_shadow_v2"
MISSIONOS_ADK_V2_PRIMARY_WORKFLOW_NAME = "missionos_conversation_proposal_primary_v2"
_ENV_SWITCH_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_ENV_SWITCH_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def adk_v2_graph_switch_enabled(env_name: str, *, default: bool) -> bool:
    """Parse one rollout switch without silently accepting operator typos."""

    raw_value = os.environ.get(env_name)
    if raw_value is None or not raw_value.strip():
        return default
    normalized = raw_value.strip().lower()
    if normalized in _ENV_SWITCH_TRUE_VALUES:
        return True
    if normalized in _ENV_SWITCH_FALSE_VALUES:
        return False
    raise RuntimeError(f"invalid_adk_v2_graph_switch:{env_name}:{raw_value!r}")


def validate_adk_v2_graph_rollout_env() -> dict[str, bool]:
    """Validate every proposal-graph rollout control during Gateway startup."""

    return {
        "primary": adk_v2_graph_switch_enabled(
            MISSIONOS_ADK_V2_GRAPH_PRIMARY_ENV,
            default=True,
        ),
        "rollback": adk_v2_graph_switch_enabled(
            MISSIONOS_ADK_V2_GRAPH_ROLLBACK_ENV,
            default=False,
        ),
        "shadow": adk_v2_graph_switch_enabled(
            MISSIONOS_ADK_V2_GRAPH_SHADOW_ENV,
            default=False,
        ),
    }


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _content_text(value: Any) -> str:
    parts = getattr(value, "parts", None) or []
    return "".join(
        str(getattr(part, "text", "") or "") for part in parts if getattr(part, "text", None)
    )


def _guardrail(invocation: Mapping[str, Any]) -> Mapping[str, Any]:
    value = invocation.get("guardrail_result")
    return value if isinstance(value, Mapping) else {}


def _validated_output(invocation: Mapping[str, Any]) -> Mapping[str, Any]:
    value = invocation.get("validated_output")
    return value if isinstance(value, Mapping) else {}


def _blocked_state(
    state: Mapping[str, Any],
    *,
    reasons: list[str],
) -> dict[str, Any]:
    blocked = dict(state)
    blocked["graph_runtime_status"] = "guardrail_blocked"
    blocked["blocking_reasons"] = reasons
    return blocked


def _authority_floor(*, measurement_only: bool = True) -> dict[str, bool]:
    return {
        "proposal_only": True,
        "measurement_only": measurement_only,
        "approval_created": False,
        "dispatch_authority_created": False,
        "executor_invoked": False,
        "physical_execution_invoked": False,
        "outcome_observed": False,
        "progress_counted": False,
    }


async def _run_missionos_conversation_graph_async(
    *,
    utterance: str,
    missionos_state: Mapping[str, Any],
    mission_designer_context: Mapping[str, Any] | None = None,
    coordinate_route: Mapping[str, Any] | None = None,
    conversation_history: list[dict[str, str]] | None = None,
    monitoring_observations: list[Mapping[str, Any]] | None = None,
    route_hint: str = "",
    timeout_seconds: int | None = None,
    execution_mode: str,
) -> dict[str, Any]:
    """Run one proposal-only ADK v2 graph evaluation."""

    if execution_mode not in {"primary", "shadow"}:
        raise ValueError(f"unsupported_adk_v2_graph_execution_mode:{execution_mode}")
    is_shadow = execution_mode == "shadow"
    workflow_execution_mode = (
        "adk_v2_graph_shadow" if is_shadow else "adk_v2_graph_primary"
    )
    result_schema_version = (
        MISSIONOS_ADK_V2_SHADOW_RESULT_SCHEMA_VERSION
        if is_shadow
        else MISSIONOS_ADK_V2_PROPOSAL_RESULT_SCHEMA_VERSION
    )
    workflow_name = (
        MISSIONOS_ADK_V2_SHADOW_WORKFLOW_NAME
        if is_shadow
        else MISSIONOS_ADK_V2_PRIMARY_WORKFLOW_NAME
    )
    session_backend = "in_memory_shadow_only" if is_shadow else "in_memory_proposal_only"
    node_names = {
        "normalize": "normalize_shadow_input" if is_shadow else "normalize_proposal_input",
        "chief": "invoke_shadow_chief" if is_shadow else "invoke_proposal_chief",
        "specialist": (
            "invoke_shadow_specialist" if is_shadow else "invoke_proposal_specialist"
        ),
        "safety_critic": (
            "invoke_shadow_safety_critic"
            if is_shadow
            else "invoke_proposal_safety_critic"
        ),
        "finalize": (
            "finalize_shadow_proposal" if is_shadow else "finalize_primary_proposal"
        ),
    }

    from google.adk import Workflow
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.workflow import node
    from google.genai import types

    from src.intelligence import missionos_agent_runtime as runtime

    @node(name=node_names["normalize"], rerun_on_resume=False)
    async def normalize_shadow_input(node_input: Any) -> dict[str, Any]:
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
            "agent_invocations": [],
            "graph_node_sequence": [node_names["normalize"]],
        }

    @node(name=node_names["chief"], rerun_on_resume=True)
    async def invoke_shadow_chief(
        ctx: Any,
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(node_input)
        payload = state.get("input") if isinstance(state.get("input"), Mapping) else {}
        monitoring_payloads = runtime._monitoring_observation_payloads(
            payload.get("monitoring_observations")
            if isinstance(payload.get("monitoring_observations"), list)
            else []
        )
        invocation = await runtime._run_agent_once_async(
            agent_name="missionos_chief_agent",
            agent_role="MissionOS chief coordinator agent",
            prompt_payload=runtime._root_prompt_payload(
                utterance=str(payload.get("utterance") or ""),
                missionos_state=(
                    payload.get("missionos_state")
                    if isinstance(payload.get("missionos_state"), Mapping)
                    else {}
                ),
                mission_designer_context=(
                    payload.get("mission_designer_context")
                    if isinstance(payload.get("mission_designer_context"), Mapping)
                    else {}
                ),
                coordinate_route=(
                    payload.get("coordinate_route")
                    if isinstance(payload.get("coordinate_route"), Mapping)
                    else {}
                ),
                conversation_history=(
                    payload.get("conversation_history")
                    if isinstance(payload.get("conversation_history"), list)
                    else []
                ),
                monitoring_observations=monitoring_payloads,
                route_hint=str(payload.get("route_hint") or ""),
            ),
            timeout_seconds=timeout_seconds,
            workflow_execution_mode=workflow_execution_mode,
            workflow_ctx=ctx,
            workflow_run_id="chief-agent",
        )
        state["agent_invocations"] = [invocation]
        state["chief_output"] = dict(_validated_output(invocation))
        state["graph_node_sequence"] = [
            *list(state.get("graph_node_sequence") or []),
            node_names["chief"],
        ]
        guardrail = _guardrail(invocation)
        if guardrail.get("guardrail_passed") is not True:
            return _blocked_state(
                state,
                reasons=list(guardrail.get("blocking_reasons") or []),
            )
        return state

    @node(name=node_names["specialist"], rerun_on_resume=True)
    async def invoke_shadow_specialist(
        ctx: Any,
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(node_input)
        state["graph_node_sequence"] = [
            *list(state.get("graph_node_sequence") or []),
            node_names["specialist"],
        ]
        if state.get("graph_runtime_status") != "running":
            return state
        payload = state.get("input") if isinstance(state.get("input"), Mapping) else {}
        chief_output = (
            state.get("chief_output") if isinstance(state.get("chief_output"), Mapping) else {}
        )
        intent = str(chief_output.get("intent") or "plan")
        specialist_name = runtime._CHIEF_TO_SPECIALIST.get(intent)
        state["specialist_name"] = specialist_name or ""
        state["specialist_output"] = {}
        if not specialist_name:
            return state
        monitoring_payloads = runtime._monitoring_observation_payloads(
            payload.get("monitoring_observations")
            if isinstance(payload.get("monitoring_observations"), list)
            else []
        )
        invocation = await runtime._run_agent_once_async(
            agent_name=specialist_name,
            agent_role=specialist_name.replace("_", " "),
            validate_intent=False,
            prompt_payload=runtime._specialist_prompt_payload(
                utterance=str(payload.get("utterance") or ""),
                root_output=chief_output,
                missionos_state=(
                    payload.get("missionos_state")
                    if isinstance(payload.get("missionos_state"), Mapping)
                    else {}
                ),
                mission_designer_context=(
                    payload.get("mission_designer_context")
                    if isinstance(payload.get("mission_designer_context"), Mapping)
                    else {}
                ),
                coordinate_route=(
                    payload.get("coordinate_route")
                    if isinstance(payload.get("coordinate_route"), Mapping)
                    else {}
                ),
                conversation_history=(
                    payload.get("conversation_history")
                    if isinstance(payload.get("conversation_history"), list)
                    else []
                ),
                monitoring_observations=monitoring_payloads,
                route_hint=str(payload.get("route_hint") or ""),
            ),
            timeout_seconds=timeout_seconds,
            workflow_execution_mode=workflow_execution_mode,
            workflow_ctx=ctx,
            workflow_run_id="specialist-agent",
        )
        state["agent_invocations"] = [
            *list(state.get("agent_invocations") or []),
            invocation,
        ]
        state["specialist_output"] = dict(_validated_output(invocation))
        guardrail = _guardrail(invocation)
        if guardrail.get("guardrail_passed") is not True:
            return _blocked_state(
                state,
                reasons=list(guardrail.get("blocking_reasons") or []),
            )
        return state

    @node(name=node_names["safety_critic"], rerun_on_resume=True)
    async def invoke_shadow_safety_critic(
        ctx: Any,
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(node_input)
        state["graph_node_sequence"] = [
            *list(state.get("graph_node_sequence") or []),
            node_names["safety_critic"],
        ]
        if state.get("graph_runtime_status") != "running":
            return state
        payload = state.get("input") if isinstance(state.get("input"), Mapping) else {}
        chief_output = (
            state.get("chief_output") if isinstance(state.get("chief_output"), Mapping) else {}
        )
        specialist_output = (
            state.get("specialist_output")
            if isinstance(state.get("specialist_output"), Mapping)
            else {}
        )
        monitoring_payloads = runtime._monitoring_observation_payloads(
            payload.get("monitoring_observations")
            if isinstance(payload.get("monitoring_observations"), list)
            else []
        )
        invocation = await runtime._run_agent_once_async(
            agent_name=runtime.MISSIONOS_SAFETY_CRITIC_AGENT_NAME,
            agent_role="MissionOS safety and boundary critic agent",
            validate_intent=False,
            prompt_payload=runtime._safety_critic_prompt_payload(
                utterance=str(payload.get("utterance") or ""),
                chief_output=chief_output,
                specialist_name=str(state.get("specialist_name") or ""),
                specialist_output=specialist_output,
                missionos_state=(
                    payload.get("missionos_state")
                    if isinstance(payload.get("missionos_state"), Mapping)
                    else {}
                ),
                mission_designer_context=(
                    payload.get("mission_designer_context")
                    if isinstance(payload.get("mission_designer_context"), Mapping)
                    else {}
                ),
                coordinate_route=(
                    payload.get("coordinate_route")
                    if isinstance(payload.get("coordinate_route"), Mapping)
                    else {}
                ),
                monitoring_observations=monitoring_payloads,
                route_hint=str(payload.get("route_hint") or ""),
            ),
            timeout_seconds=timeout_seconds,
            workflow_execution_mode=workflow_execution_mode,
            workflow_ctx=ctx,
            workflow_run_id="safety-critic-agent",
        )
        state["agent_invocations"] = [
            *list(state.get("agent_invocations") or []),
            invocation,
        ]
        state["safety_critic_output"] = dict(_validated_output(invocation))
        guardrail = _guardrail(invocation)
        if guardrail.get("guardrail_passed") is not True:
            return _blocked_state(
                state,
                reasons=list(guardrail.get("blocking_reasons") or []),
            )
        boundary_status = str(state["safety_critic_output"].get("boundary_status") or "").strip()
        if not boundary_status:
            return _blocked_state(
                state,
                reasons=["safety_critic_boundary_status_missing"],
            )
        if boundary_status not in runtime.MISSIONOS_SAFETY_CRITIC_PASS_STATUSES:
            return _blocked_state(
                state,
                reasons=[f"safety_critic_boundary_status:{boundary_status}"],
            )
        return state

    @node(name=node_names["finalize"], rerun_on_resume=False)
    async def finalize_shadow_proposal(node_input: Mapping[str, Any]) -> dict[str, Any]:
        state = dict(node_input)
        node_sequence = [
            *list(state.get("graph_node_sequence") or []),
            node_names["finalize"],
        ]
        if state.get("graph_runtime_status") != "running":
            return {
                "schema_version": result_schema_version,
                "graph_runtime_status": str(
                    state.get("graph_runtime_status") or "guardrail_blocked"
                ),
                "blocking_reasons": list(state.get("blocking_reasons") or []),
                "proposal": {},
                "agent_invocations": list(state.get("agent_invocations") or []),
                "graph_node_sequence": node_sequence,
                "workflow_name": workflow_name,
                "workflow_execution_mode": workflow_execution_mode,
                "session_backend": session_backend,
                "retry_policy": "disabled",
                **_authority_floor(measurement_only=is_shadow),
            }
        payload = state.get("input") if isinstance(state.get("input"), Mapping) else {}
        chief_output = (
            state.get("chief_output") if isinstance(state.get("chief_output"), Mapping) else {}
        )
        specialist_output = (
            state.get("specialist_output")
            if isinstance(state.get("specialist_output"), Mapping)
            else {}
        )
        safety_critic_output = (
            state.get("safety_critic_output")
            if isinstance(state.get("safety_critic_output"), Mapping)
            else {}
        )
        specialist_name = str(state.get("specialist_name") or "")
        monitoring_payloads = runtime._monitoring_observation_payloads(
            payload.get("monitoring_observations")
            if isinstance(payload.get("monitoring_observations"), list)
            else []
        )
        proposal = {
            "intent": str(chief_output.get("intent") or "plan"),
            "specialist_intent": str(specialist_output.get("intent") or ""),
            "operator_instruction": str(
                specialist_output.get("operator_instruction")
                or chief_output.get("operator_instruction")
                or payload.get("utterance")
                or ""
            )[:2000],
            "specialist_agent": specialist_name or str(chief_output.get("specialist_agent") or ""),
            "chief_agent_output": dict(chief_output),
            "root_agent_output": dict(chief_output),
            "specialist_agent_output": dict(specialist_output),
            "safety_critic_agent": (
                runtime.MISSIONOS_SAFETY_CRITIC_AGENT_NAME if safety_critic_output else ""
            ),
            "safety_critic_output": dict(safety_critic_output),
            "operator_facing_route": runtime.MISSIONOS_OPERATOR_FACING_ROUTE,
            "internal_capability_registry": (
                runtime.build_missionos_capability_registry_summary()
            ),
            "coordination_pattern": (
                f"{workflow_execution_mode}_chief_specialist_safety_critic"
            ),
            "routing_floor": "deterministic_chief_to_specialist_allowlist",
            "ambient_monitoring_model": "event_driven_chief_invocation",
            "monitoring_observations": monitoring_payloads,
            "requires_human_approval": bool(
                specialist_output.get("requires_human_approval")
                if "requires_human_approval" in specialist_output
                else chief_output.get("requires_human_approval", False)
            ),
        }
        return {
            "schema_version": result_schema_version,
            "graph_runtime_status": "proposal_guardrail_passed",
            "blocking_reasons": [],
            "proposal": proposal,
            "agent_invocations": list(state.get("agent_invocations") or []),
            "graph_node_sequence": node_sequence,
            "workflow_name": workflow_name,
            "workflow_execution_mode": workflow_execution_mode,
            "session_backend": session_backend,
            "retry_policy": "disabled",
            **_authority_floor(measurement_only=is_shadow),
        }

    workflow = Workflow(
        name=workflow_name,
        description=(
            "Proposal-only ADK v2 graph for MissionOS conversation decisions; "
            f"execution mode is {execution_mode}."
        ),
        # The graph parent and judgment nodes are re-enterable because the
        # latter schedule LlmAgent children through ctx.run_node(). Pure
        # normalize/finalize nodes remain reusable.
        rerun_on_resume=True,
        edges=[
            (
                "START",
                normalize_shadow_input,
                invoke_shadow_chief,
                invoke_shadow_specialist,
                invoke_shadow_safety_critic,
                finalize_shadow_proposal,
            )
        ],
    )
    graph_input = {
        "utterance": utterance,
        "missionos_state": dict(missionos_state),
        "mission_designer_context": dict(mission_designer_context or {}),
        "coordinate_route": dict(coordinate_route or {}),
        "conversation_history": list(conversation_history or [])[-10:],
        "monitoring_observations": [
            dict(item) for item in (monitoring_observations or []) if isinstance(item, Mapping)
        ],
        "route_hint": route_hint,
    }
    session_service = InMemorySessionService()
    app_name = f"missionos_adk_v2_{execution_mode}"
    user_id = f"missionos_{execution_mode}_operator"
    session = await session_service.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(agent=workflow, app_name=app_name, session_service=session_service)
    content = types.Content(
        role="user",
        parts=[
            types.Part(
                text=json.dumps(
                    graph_input,
                    ensure_ascii=False,
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
    if final_output.get("schema_version") != result_schema_version:
        raise RuntimeError(f"adk_v2_{execution_mode}_graph_final_output_missing")
    final_output["workflow_node_paths"] = workflow_node_paths
    return final_output


async def run_missionos_conversation_shadow_graph_async(**kwargs: Any) -> dict[str, Any]:
    return await _run_missionos_conversation_graph_async(
        execution_mode="shadow",
        **kwargs,
    )


def run_missionos_conversation_shadow_graph(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_missionos_conversation_shadow_graph_async(**kwargs))


async def run_missionos_conversation_proposal_graph_async(**kwargs: Any) -> dict[str, Any]:
    return await _run_missionos_conversation_graph_async(
        execution_mode="primary",
        **kwargs,
    )


def run_missionos_conversation_proposal_graph(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_missionos_conversation_proposal_graph_async(**kwargs))


def build_missionos_conversation_shadow_comparison(
    *,
    primary_result: Mapping[str, Any],
    shadow_result: Mapping[str, Any],
    input_payload: Mapping[str, Any],
) -> dict[str, Any]:
    primary_proposal_value = primary_result.get("proposal")
    primary_proposal = primary_proposal_value if isinstance(primary_proposal_value, Mapping) else {}
    shadow_proposal_value = shadow_result.get("proposal")
    shadow_proposal = shadow_proposal_value if isinstance(shadow_proposal_value, Mapping) else {}
    primary_critic_value = primary_proposal.get("safety_critic_output")
    primary_critic = primary_critic_value if isinstance(primary_critic_value, Mapping) else {}
    shadow_critic_value = shadow_proposal.get("safety_critic_output")
    shadow_critic = shadow_critic_value if isinstance(shadow_critic_value, Mapping) else {}
    field_comparisons = {
        "runtime_status": {
            "primary": str(primary_result.get("runtime_status") or ""),
            "shadow": str(shadow_result.get("graph_runtime_status") or ""),
        },
        "intent": {
            "primary": str(primary_proposal.get("intent") or ""),
            "shadow": str(shadow_proposal.get("intent") or ""),
        },
        "specialist_agent": {
            "primary": str(primary_proposal.get("specialist_agent") or ""),
            "shadow": str(shadow_proposal.get("specialist_agent") or ""),
        },
        "requires_human_approval": {
            "primary": bool(primary_proposal.get("requires_human_approval", False)),
            "shadow": bool(shadow_proposal.get("requires_human_approval", False)),
        },
        "safety_boundary_status": {
            "primary": str(primary_critic.get("boundary_status") or ""),
            "shadow": str(shadow_critic.get("boundary_status") or ""),
        },
    }
    for comparison in field_comparisons.values():
        comparison["agreement"] = comparison["primary"] == comparison["shadow"]
    statuses_passed = (
        primary_result.get("runtime_status") == "proposal_guardrail_passed"
        and shadow_result.get("graph_runtime_status") == "proposal_guardrail_passed"
    )
    return {
        "schema_version": MISSIONOS_ADK_V2_SHADOW_COMPARISON_SCHEMA_VERSION,
        "comparison_scope": "conversation_proposal_path_only",
        "chief_stage_compared": True,
        "specialist_stage_compared": bool(
            primary_proposal.get("specialist_agent") or shadow_proposal.get("specialist_agent")
        ),
        "safety_critic_stage_compared": True,
        "control_loop_compared": False,
        "same_input_sha256": _json_sha256(input_payload),
        "field_comparisons": field_comparisons,
        "agreement": (
            all(item["agreement"] for item in field_comparisons.values())
            if statuses_passed
            else None
        ),
        "primary_agent_invocation_count": len(primary_result.get("agent_invocations") or []),
        "shadow_agent_invocation_count": len(shadow_result.get("agent_invocations") or []),
        "shadow_workflow_name": str(shadow_result.get("workflow_name") or ""),
        "shadow_graph_node_sequence": list(shadow_result.get("graph_node_sequence") or []),
        "shadow_session_backend": str(shadow_result.get("session_backend") or ""),
        "shadow_retry_policy": str(shadow_result.get("retry_policy") or ""),
        "shadow_blocking_reasons": list(shadow_result.get("blocking_reasons") or []),
        **_authority_floor(),
    }


def build_shadow_runtime_error_comparison(exc: BaseException) -> dict[str, Any]:
    return {
        "schema_version": MISSIONOS_ADK_V2_SHADOW_COMPARISON_SCHEMA_VERSION,
        "comparison_scope": "conversation_proposal_path_only",
        "shadow_runtime_status": "error",
        "blocking_reasons": [f"adk_v2_shadow_graph_failed:{type(exc).__name__}"],
        "agreement": None,
        "control_loop_compared": False,
        **_authority_floor(),
    }


__all__ = [
    "MISSIONOS_ADK_V2_GRAPH_PRIMARY_ENV",
    "MISSIONOS_ADK_V2_GRAPH_ROLLBACK_ENV",
    "MISSIONOS_ADK_V2_GRAPH_SHADOW_ENV",
    "MISSIONOS_ADK_V2_PRIMARY_WORKFLOW_NAME",
    "MISSIONOS_ADK_V2_PROPOSAL_RESULT_SCHEMA_VERSION",
    "MISSIONOS_ADK_V2_SHADOW_COMPARISON_SCHEMA_VERSION",
    "MISSIONOS_ADK_V2_SHADOW_RESULT_SCHEMA_VERSION",
    "adk_v2_graph_switch_enabled",
    "build_missionos_conversation_shadow_comparison",
    "build_shadow_runtime_error_comparison",
    "run_missionos_conversation_proposal_graph",
    "run_missionos_conversation_proposal_graph_async",
    "run_missionos_conversation_shadow_graph",
    "run_missionos_conversation_shadow_graph_async",
    "validate_adk_v2_graph_rollout_env",
]
