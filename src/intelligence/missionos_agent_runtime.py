"""MissionOS ADK agent runtime.

Gateway owns HTTP/session/artifact/approval/dispatch boundaries.  This module
owns the MissionOS intelligence layer: actual ADK agent invocations plus
deterministic guardrails over their JSON outputs.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.agents.model_config import (
    agent_model_label,
    configure_google_vertex_location,
    deepseek_llm_backend_enabled,
    google_llm_backend_enabled,
    llm_provider_label,
    local_llm_backend_enabled,
)
from src.gateway.missionos_capabilities import (
    MISSIONOS_OPERATOR_FACING_ROUTE,
    all_capability_descriptors_for_prompt,
    build_missionos_capability_registry_summary,
)
from src.intelligence.missionos_adk_v2_shadow_graph import (
    MISSIONOS_ADK_V2_GRAPH_PRIMARY_ENV,
    MISSIONOS_ADK_V2_GRAPH_ROLLBACK_ENV,
    MISSIONOS_ADK_V2_GRAPH_SHADOW_ENV,
    validate_adk_v2_graph_rollout_env,
)
from src.runtime.px4_gazebo_route.action_feasibility import (
    SUPPORTED_FEASIBILITY_ACTIONS,
)
from src.runtime.px4_gazebo_route.core_action_feasibility_adapter import (
    build_runtime_recovery_hazard_state,
    verify_runtime_recovery_action_candidates,
    verify_runtime_recovery_action_feasibility,
)
from src.runtime.px4_gazebo_route.recovery_intent_compiler import (
    build_runtime_recovery_intent,
    compile_runtime_recovery_intent,
    verify_runtime_recovery_reachability,
)

MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION = "missionos_agent_runtime_result.v1"
MISSIONOS_AGENT_INVOCATION_EVIDENCE_SCHEMA_VERSION = "missionos_agent_invocation_evidence.v1"
MISSIONOS_AGENT_GUARDRAIL_SCHEMA_VERSION = "missionos_agent_guardrail.v1"
MISSIONOS_MONITORING_OBSERVATION_SCHEMA_VERSION = "missionos_monitoring_observation.v1"

MISSIONOS_AGENT_RUNTIME_ADK_ENABLED_ENV = "MISSIONOS_AGENT_RUNTIME_ADK_ENABLED"
MISSIONOS_AGENT_RUNTIME_MODEL_ENV = "MISSIONOS_AGENT_RUNTIME_MODEL_ID"
MISSIONOS_AGENT_RUNTIME_TIMEOUT_ENV = "MISSIONOS_AGENT_RUNTIME_TIMEOUT_SECONDS"

DEFAULT_TIMEOUT_SECONDS = 45
ARTIFACT_ROOT = Path("output/mission_designer_behavior_delta_audits")
_ENV_SWITCH_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class _RuntimeRecoveryADKResponse(BaseModel):
    """Structured no-tool response for non-parameterized recovery actions."""

    model_config = ConfigDict(extra="forbid")

    intent: Literal["runtime_recovery"]
    operator_instruction: str = Field(min_length=1)
    selected_bounded_action: Literal[
        "continue",
        "hold",
        "return_to_launch",
        "land",
        "operator_review",
    ]
    proposed_parameters: dict[str, Any]
    trigger_level: Literal["none", "advisory", "immediate"]
    trigger_reasons: list[str]
    telemetry_assessment: dict[str, Any]
    rationale: str = Field(min_length=1)
    expected_outcome: str = Field(min_length=1)
    requires_human_approval: bool
    uncertainty: str


def _runtime_recovery_output_schema() -> type[_RuntimeRecoveryADKResponse] | None:
    """DeepSeek currently rejects ADK response_format; guards still parse JSON."""

    if deepseek_llm_backend_enabled("missionos_runtime_recovery_agent"):
        return None
    return _RuntimeRecoveryADKResponse

MISSIONOS_AGENT_ALLOWED_INTENTS = frozenset(
    {
        "status",
        "approve",
        "reject",
        "revision",
        "execute",
        "repair",
        "plan",
        "mission_designer_plan",
        "runtime_recovery",
    }
)

MISSIONOS_AGENT_FORBIDDEN_KEYS = frozenset(
    {
        "approved",
        "approval_granted",
        "operator_approved",
        "dispatch_authority_created",
        "progress_counted",
        "goal_640_progress_counted",
        "ai_agent_progress_counted",
        "dispatch_executed",
        "dispatch_executed_in_runtime",
        "automatic_dispatch_executed",
        "physical_execution_invoked",
        "delivery_completion_claimed",
        "hardware_target_allowed",
        "bypass_gate",
        "llm_judgment_in_gate",
        "gate_status_mutated",
        "gate_passed",
        "mission_upload_performed",
        "px4_mission_upload_performed",
    }
)

# Stage A deterministic floor.  The Chief Agent proposes the intent, but this
# map still chooses the specialist invocation while ADK transfer/workflow
# delegation is introduced incrementally and safely.
_CHIEF_TO_SPECIALIST = {
    "status": "missionos_situation_judge_agent",
    "plan": "missionos_response_planner_agent",
    "revision": "missionos_response_planner_agent",
    "runtime_recovery": "missionos_runtime_recovery_agent",
    "mission_designer_plan": "missionos_flight_scenario_designer_agent",
    "repair": "missionos_repair_planner_agent",
}

MISSIONOS_SAFETY_CRITIC_AGENT_NAME = "missionos_safety_critic_agent"
MISSIONOS_SAFETY_CRITIC_PASS_STATUSES = frozenset(
    {
        "safe",
        "needs_human_approval",
        "operator_review_required",
    }
)
MISSIONOS_SAFETY_CRITIC_RECOGNIZED_STATUSES = MISSIONOS_SAFETY_CRITIC_PASS_STATUSES | frozenset(
    {"blocked"}
)

MISSIONOS_MONITORING_OBSERVATION_SEVERITIES = frozenset(
    {
        "info",
        "advisory",
        "warning",
        "critical",
    }
)

MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION = "missionos_runtime_recovery_agent_result.v1"
MISSIONOS_RUNTIME_RECOVERY_ASSESSMENT_SCHEMA_VERSION = "missionos_runtime_recovery_assessment.v1"
MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_SCHEMA_VERSION = (
    "missionos_runtime_recovery_planner_tool_result.v1"
)
MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_NAME = "missionos_plan_bounded_recovery_maneuver"
_PARAMETERIZED_RUNTIME_RECOVERY_ACTIONS = frozenset(
    {
        "adjust_altitude",
        "reroute",
        "avoid_obstacle",
    }
)

MISSIONOS_RUNTIME_RECOVERY_ACTIONS = frozenset(
    {
        "continue",
        "hold",
        "return_to_launch",
        "land",
        "adjust_altitude",
        "adjust_speed",
        "reroute",
        "avoid_obstacle",
        "operator_review",
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_text(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        )
    )


def _model_id(agent_name: str | None = None) -> str:
    env_model = os.environ.get(MISSIONOS_AGENT_RUNTIME_MODEL_ENV, "").strip()
    return agent_model_label(env_model or None, agent_name=agent_name)


def _monitoring_observation_payloads(
    monitoring_observations: list[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for index, observation in enumerate(monitoring_observations or []):
        if not isinstance(observation, Mapping):
            continue
        severity = str(observation.get("severity") or "advisory").strip().lower()
        if severity not in MISSIONOS_MONITORING_OBSERVATION_SEVERITIES:
            severity = "advisory"
        suggested_intent = str(observation.get("suggested_intent") or "").strip()
        if suggested_intent not in MISSIONOS_AGENT_ALLOWED_INTENTS:
            suggested_intent = ""
        evidence_refs = [
            str(ref)[:500]
            for ref in observation.get("evidence_refs") or []
            if isinstance(ref, str) and ref.strip()
        ][:10]
        payloads.append(
            {
                "schema_version": MISSIONOS_MONITORING_OBSERVATION_SCHEMA_VERSION,
                "observation_id": str(
                    observation.get("observation_id")
                    or observation.get("id")
                    or f"monitoring_observation:{index + 1}"
                )[:200],
                "source": str(observation.get("source") or "missionos_event_monitor")[:200],
                "observed_at": str(observation.get("observed_at") or "")[:100],
                "observation_type": str(observation.get("observation_type") or "runtime_snapshot")[
                    :200
                ],
                "severity": severity,
                "summary": str(observation.get("summary") or "")[:2000],
                "suggested_intent": suggested_intent,
                "evidence_refs": evidence_refs,
                "authority_status": "observation_only",
                "approval_request_created": False,
                "dispatch_authority_created": False,
                "progress_counted": False,
            }
        )
    return payloads[:5]


def _timeout_seconds() -> int:
    value = os.environ.get(MISSIONOS_AGENT_RUNTIME_TIMEOUT_ENV)
    try:
        parsed = int(value) if value is not None else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return max(1, parsed)


def _configure_google_adk_environment(agent_name: str | None = None) -> None:
    if not google_llm_backend_enabled(agent_name):
        return
    try:
        from src.config.settings import get_settings

        settings = get_settings()
    except Exception:
        return
    api_key = str(getattr(settings, "google_api_key", "") or "").strip()
    if api_key and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = api_key
    if not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
        use_vertex = bool(getattr(settings, "google_genai_use_vertexai", False))
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true" if use_vertex else "false"
    configure_google_vertex_location(
        _model_id(agent_name),
        agent_name=agent_name,
    )


def _adk_llm_credentials_available(agent_name: str | None = None) -> bool:
    if local_llm_backend_enabled(agent_name):
        return True
    if deepseek_llm_backend_enabled(agent_name):
        return bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())
    if not google_llm_backend_enabled(agent_name):
        return False
    _configure_google_adk_environment(agent_name)
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower()
    if use_vertex in {"1", "true", "yes"}:
        return True
    return bool(os.environ.get("GOOGLE_API_KEY", "").strip())


def _google_adk_credentials_available(agent_name: str | None = None) -> bool:
    """Compatibility alias for the provider-neutral ADK credential check."""
    return _adk_llm_credentials_available(agent_name)


def _llm_credentials_blocking_reason(agent_name: str | None = None) -> str:
    if deepseek_llm_backend_enabled(agent_name):
        return "DEEPSEEK_API_KEY_not_configured"
    if google_llm_backend_enabled(agent_name):
        return "GOOGLE_API_KEY_not_configured"
    return "llm_backend_credentials_not_configured"


def _read_json_object(response_text: str) -> dict[str, Any] | None:
    text = response_text.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _scan_forbidden_keys(obj: Any, *, _depth: int = 0) -> list[str]:
    if _depth > 20:
        return []
    found: list[str] = []
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            key_text = str(key)
            if key_text in MISSIONOS_AGENT_FORBIDDEN_KEYS:
                found.append(key_text)
            found.extend(_scan_forbidden_keys(value, _depth=_depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_scan_forbidden_keys(item, _depth=_depth + 1))
    return found


def guard_missionos_agent_output(
    raw_output: Any,
    *,
    validate_intent: bool = True,
) -> dict[str, Any]:
    """Deterministic guardrail over agent JSON output.

    validate_intent:
      True  (default) — used for the ROOT agent.  The intent field must be in
            MISSIONOS_AGENT_ALLOWED_INTENTS because it drives the routing
            decision to a specialist.
      False — used for SPECIALIST agents.  The routing is already determined
            by the Chief agent; the specialist's intent field is a result label
            and is NOT subject to the routing allowed-set.  Forbidden-key and
            type checks still apply.
    """
    blocking_reasons: list[str] = []
    if not isinstance(raw_output, Mapping):
        return {
            "schema_version": MISSIONOS_AGENT_GUARDRAIL_SCHEMA_VERSION,
            "guardrail_passed": False,
            "blocking_reasons": ["agent_output_not_dict"],
            "validated_output": {},
        }

    for key in dict.fromkeys(_scan_forbidden_keys(raw_output)):
        blocking_reasons.append(f"forbidden_key_present:{key}")

    intent = raw_output.get("intent")
    if validate_intent and intent is not None and intent not in MISSIONOS_AGENT_ALLOWED_INTENTS:
        blocking_reasons.append(f"intent_not_in_allowed_set:{intent!r}")

    operator_instruction = raw_output.get("operator_instruction")
    if operator_instruction is not None and not isinstance(operator_instruction, str):
        blocking_reasons.append("operator_instruction_must_be_string")

    requires_human_approval = raw_output.get("requires_human_approval")
    if requires_human_approval is not None and not isinstance(requires_human_approval, bool):
        blocking_reasons.append("requires_human_approval_must_be_bool")

    if blocking_reasons:
        return {
            "schema_version": MISSIONOS_AGENT_GUARDRAIL_SCHEMA_VERSION,
            "guardrail_passed": False,
            "blocking_reasons": blocking_reasons,
            "validated_output": {},
        }

    validated = dict(raw_output)
    if isinstance(validated.get("operator_instruction"), str):
        validated["operator_instruction"] = str(validated["operator_instruction"])[:2000]
    return {
        "schema_version": MISSIONOS_AGENT_GUARDRAIL_SCHEMA_VERSION,
        "guardrail_passed": True,
        "blocking_reasons": [],
        "validated_output": validated,
    }


async def _invoke_adk_agent_text_async(
    *,
    agent_name: str,
    model_id: str,
    prompt_text: str,
) -> str:
    _configure_google_adk_environment(agent_name)
    from google.adk.runners import Runner
    from google.genai import types

    from src.agents.missionos_agents import build_missionos_agent
    from src.runtime.session_service import create_session_service

    agent = build_missionos_agent(agent_name, model_id=model_id)
    app_name = "missionos_agent_runtime"
    user_id = "missionos_operator"
    session_service = create_session_service()
    session = await session_service.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)
    content = types.Content(role="user", parts=[types.Part(text=prompt_text)])
    response_parts: list[str] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=content,
    ):
        if not event.is_final_response() or not event.content:
            continue
        for part in event.content.parts or []:
            text = getattr(part, "text", None)
            if text:
                response_parts.append(text)
    return "".join(response_parts).strip()


async def _invoke_adk_agent_text_node_async(
    *,
    workflow_ctx: Any,
    agent_name: str,
    model_id: str,
    prompt_text: str,
    run_id: str,
    timeout_seconds: int,
) -> str:
    """Invoke a MissionOS Agent as an ADK v2 dynamic workflow child."""

    _configure_google_adk_environment(agent_name)
    from google.genai import types

    from src.agents.missionos_agents import build_missionos_agent

    safe_run_id = re.sub(r"[^0-9A-Za-z_-]+", "-", run_id).strip("-")
    output_key = f"temp:adk_v2_agent_output:{safe_run_id or agent_name}"
    agent = build_missionos_agent(agent_name, model_id=model_id).model_copy(
        update={
            "output_key": output_key,
            "rerun_on_resume": True,
            "timeout": float(timeout_seconds),
        }
    )
    content = types.Content(role="user", parts=[types.Part(text=prompt_text)])
    await workflow_ctx.run_node(
        agent,
        content,
        run_id=run_id,
    )
    return str(workflow_ctx.state.get(output_key) or "").strip()


def _runtime_recovery_agent_output_from_planner_result(
    planner_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Build proposal-only agent output from a guarded planner result.

    The Runtime Recovery Agent still judges which planner action to request.
    Once the deterministic planner and shared guardrail have constrained that
    request, this payload is suitable as the final ADK FunctionTool response.
    It intentionally contains no approval, dispatch, execution, or verifier
    authority.
    """

    candidate_value = planner_result.get("recommended_candidate")
    candidate = candidate_value if isinstance(candidate_value, Mapping) else {}
    assessment_value = planner_result.get("recovery_guardrail_assessment")
    assessment = assessment_value if isinstance(assessment_value, Mapping) else {}
    recovery_intent_value = assessment.get("recovery_intent")
    recovery_intent = (
        recovery_intent_value
        if isinstance(recovery_intent_value, Mapping)
        else {}
    )
    selected_action = str(candidate.get("selected_bounded_action") or "operator_review").strip()
    proposed_parameters_value = candidate.get("proposed_parameters")
    proposed_parameters = (
        dict(proposed_parameters_value) if isinstance(proposed_parameters_value, Mapping) else {}
    )
    trigger_reasons = [
        str(item)
        for item in (
            assessment.get("observed_risk_reasons")
            or assessment.get("agent_trigger_reasons")
            or ["runtime_recovery_planner_candidate"]
        )
        if str(item).strip()
    ]
    source_refs = [str(item) for item in candidate.get("source_refs") or [] if str(item).strip()]
    rationale = str(candidate.get("rationale") or "").strip()
    if not rationale:
        rationale = (
            "The bounded deterministic recovery candidate requires operator "
            "review before any dispatch."
        )
    guardrail_passed = str(assessment.get("assessment_status") or "") == "proposal_guardrail_passed"
    return {
        "intent": "runtime_recovery",
        "operator_instruction": (
            f"Review the bounded {selected_action} recovery proposal before any dispatch."
        ),
        "selected_bounded_action": selected_action,
        "proposed_parameters": proposed_parameters,
        "strategy": str(recovery_intent.get("strategy") or ""),
        "intent_constraints": dict(
            recovery_intent.get("intent_constraints")
            if isinstance(recovery_intent.get("intent_constraints"), Mapping)
            else {}
        ),
        "trigger_level": str(assessment.get("trigger_level") or "advisory"),
        "trigger_reasons": trigger_reasons,
        "telemetry_assessment": {
            "source": "runtime_recovery_planner_guardrail",
            "observed_risk_reasons": trigger_reasons,
            "source_refs": source_refs,
        },
        "rationale": rationale,
        "expected_outcome": ("A human reviews the bounded proposal before executor dispatch."),
        "requires_human_approval": True,
        "uncertainty": ("" if guardrail_passed else "planner_guardrail_requires_operator_review"),
    }


def _finalize_runtime_recovery_tool_response(
    *,
    planner_result: Mapping[str, Any],
    tool_context: Any,
) -> dict[str, Any]:
    """End the ADK turn after one guarded recovery FunctionTool call."""

    if tool_context is not None:
        tool_context.actions.skip_summarization = True
    return _runtime_recovery_agent_output_from_planner_result(planner_result)


def _runtime_recovery_planner_tool_required(
    *,
    requested_action: str,
    planner_preview: Mapping[str, Any],
) -> bool:
    """Attach the maneuver tool only when a parameterized action can use it."""

    if requested_action in _PARAMETERIZED_RUNTIME_RECOVERY_ACTIONS:
        return True
    candidates = planner_preview.get("candidates")
    candidates = candidates if isinstance(candidates, list) else []
    return any(
        isinstance(candidate, Mapping)
        and candidate.get("selected_bounded_action")
        in _PARAMETERIZED_RUNTIME_RECOVERY_ACTIONS
        for candidate in candidates
    )


async def _invoke_runtime_recovery_agent_text_with_tools_async(
    *,
    model_id: str,
    prompt_text: str,
    telemetry_snapshot: Mapping[str, Any],
    mission_context: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> dict[str, Any]:
    _configure_google_adk_environment("missionos_runtime_recovery_agent")
    from google.adk.runners import Runner
    from google.adk.tools import FunctionTool
    from google.genai import types

    from src.agents.missionos_agents import build_missionos_runtime_recovery_agent
    from src.runtime.session_service import create_session_service

    captured: dict[str, Any] = {
        "tool_arguments": [],
        "tool_results": [],
    }

    def missionos_plan_bounded_recovery_maneuver(
        recovery_action: str = "",
        reason: str = "",
        strategy: str = "",
        avoidance_side: str = "",
        minimum_clearance_m: float | None = None,
        maximum_duration_s: float | None = None,
        maximum_speed_mps: float | None = None,
        destination_kind: str = "",
        target_altitude_min_m: float | None = None,
        target_altitude_max_m: float | None = None,
        tool_context: Any = None,
    ) -> dict[str, Any]:
        """Compute bounded recovery proposal parameters without authority.

        Args:
            recovery_action: One of adjust_altitude, reroute, avoid_obstacle, or
                empty when asking for the best available bounded candidate.
            reason: Concise reason the Runtime Recovery Agent is considering the
                maneuver.
            strategy: Optional monitor, global_reroute, local_avoidance, hold,
                or rtl_or_land strategy selected by the Runtime Recovery Agent.
                It must match the selected action's compiler mapping:
                adjust_altitude/adjust_speed/avoid_obstacle use local_avoidance,
                reroute uses global_reroute, hold uses hold, and
                return_to_launch/land use rtl_or_land.
            avoidance_side: Optional left or right semantic constraint.
            minimum_clearance_m: Optional minimum lateral clearance constraint.
                Supply it only when the selected candidate basis explicitly
                contains minimum_lateral_clearance_m or
                required_lateral_clearance_m; otherwise omit it.
            maximum_duration_s: Optional maximum execution-duration envelope.
            maximum_speed_mps: Optional maximum horizontal-speed envelope.
            destination_kind: Optional original_route or alternate_dropoff intent.
            target_altitude_min_m: Optional lower altitude-envelope bound.
            target_altitude_max_m: Optional upper altitude-envelope bound.

        Returns:
            Bounded proposal-only target_altitude_m and/or local NED target_x_m
            and target_y_m candidates. This tool never approves, dispatches,
            executes, verifies, or counts progress.
        """

        intent_constraints = {
            key: value
            for key, value in {
                "avoidance_side": str(avoidance_side or "").strip(),
                "minimum_clearance_m": minimum_clearance_m,
                "maximum_duration_s": maximum_duration_s,
                "maximum_speed_mps": maximum_speed_mps,
                "destination_kind": str(destination_kind or "").strip(),
                "target_altitude_min_m": target_altitude_min_m,
                "target_altitude_max_m": target_altitude_max_m,
            }.items()
            if value not in {None, ""}
        }
        arguments = {
            "recovery_action": str(recovery_action or ""),
            "reason": str(reason or "")[:500],
            "strategy": str(strategy or "").strip(),
            "intent_constraints": intent_constraints,
        }
        planner_result = plan_runtime_recovery_maneuver(
            telemetry_snapshot=telemetry_snapshot,
            mission_context=mission_context,
            recovery_policy=recovery_policy,
            requested_action=arguments["recovery_action"],
            request_reason=arguments["reason"],
        )
        guarded_result = guard_runtime_recovery_planner_result(
            planner_result=planner_result,
            telemetry_snapshot=telemetry_snapshot,
            recovery_policy=recovery_policy,
            agent_intent={
                "strategy": arguments["strategy"],
                "intent_constraints": intent_constraints,
                "rationale": arguments["reason"],
            },
        )
        captured["tool_arguments"].append(arguments)
        captured["tool_results"].append(dict(guarded_result))
        return _finalize_runtime_recovery_tool_response(
            planner_result=guarded_result,
            tool_context=tool_context,
        )

    operator_request = mission_context.get("operator_recovery_request")
    operator_request = (
        dict(operator_request) if isinstance(operator_request, Mapping) else {}
    )
    requested_action = str(operator_request.get("requested_action") or "").strip()
    planner_preview = plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry_snapshot,
        mission_context=mission_context,
        recovery_policy=recovery_policy,
        requested_action="",
        request_reason="planner_tool_attachment_preview",
    )
    planner_tool_required = _runtime_recovery_planner_tool_required(
        requested_action=requested_action,
        planner_preview=planner_preview,
    )
    agent = build_missionos_runtime_recovery_agent(
        model_id=model_id,
        tools=(
            [FunctionTool(missionos_plan_bounded_recovery_maneuver)]
            if planner_tool_required
            else []
        ),
    )
    output_schema = _runtime_recovery_output_schema()
    if not planner_tool_required and output_schema is not None:
        agent = agent.model_copy(update={"output_schema": output_schema})
    app_name = "missionos_runtime_recovery_function_tool"
    user_id = "missionos_operator"
    session_service = create_session_service()
    session = await session_service.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)
    content = types.Content(role="user", parts=[types.Part(text=prompt_text)])
    response_parts: list[str] = []
    response_source = "llm_final_response"
    function_calls: list[dict[str, Any]] = []
    function_responses: list[dict[str, Any]] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=content,
    ):
        if not event.content:
            continue
        for part in event.content.parts or []:
            text = getattr(part, "text", None)
            if text and event.is_final_response():
                response_parts.append(text)
            function_call = getattr(part, "function_call", None)
            if function_call:
                function_calls.append(
                    {
                        "name": str(getattr(function_call, "name", "") or ""),
                        "args": dict(getattr(function_call, "args", None) or {}),
                    }
                )
            function_response = getattr(part, "function_response", None)
            if function_response:
                function_response_payload = getattr(function_response, "response", None)
                function_responses.append(
                    {
                        "name": str(getattr(function_response, "name", "") or ""),
                        "response_present": bool(function_response_payload),
                    }
                )
                if event.is_final_response() and isinstance(function_response_payload, Mapping):
                    response_parts.append(
                        json.dumps(dict(function_response_payload), ensure_ascii=False)
                    )
                    response_source = "function_tool_result_skip_summarization"
    return {
        "response_text": "".join(response_parts).strip(),
        "response_source": response_source,
        "function_calls": function_calls,
        "function_responses": function_responses,
        "function_tool_called": bool(captured["tool_results"]),
        "planner_tool_attached": planner_tool_required,
        "tool_arguments": list(captured["tool_arguments"]),
        "function_tool_results": list(captured["tool_results"]),
    }


def _invoke_runtime_recovery_agent_text_with_tools(
    *,
    model_id: str,
    prompt_text: str,
    telemetry_snapshot: Mapping[str, Any],
    mission_context: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    return asyncio.run(
        asyncio.wait_for(
            _invoke_runtime_recovery_agent_text_with_tools_async(
                model_id=model_id,
                prompt_text=prompt_text,
                telemetry_snapshot=telemetry_snapshot,
                mission_context=mission_context,
                recovery_policy=recovery_policy,
            ),
            timeout=timeout_seconds,
        )
    )


def _artifact_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _persist_invocation_evidence(evidence: Mapping[str, Any]) -> str:
    root = ARTIFACT_ROOT / "missionos_agent_runtime"
    root.mkdir(parents=True, exist_ok=True)
    started = str(evidence.get("invocation_started_at") or "")
    safe_started = re.sub(r"[^0-9A-Za-z]+", "", started)[:16] or "unknown"
    agent_name = re.sub(r"[^0-9A-Za-z_]+", "_", str(evidence.get("agent_name") or "agent"))
    digest = str(evidence.get("response_sha256") or evidence.get("prompt_sha256") or "")[:12]
    path = root / f"{safe_started}_{agent_name}_{digest}.v1.json"
    path.write_text(json.dumps(dict(evidence), indent=2, sort_keys=True), encoding="utf-8")
    return _artifact_relative(path)


async def _run_agent_once_async(
    *,
    agent_name: str,
    agent_role: str,
    prompt_payload: Mapping[str, Any],
    validate_intent: bool = True,
    timeout_seconds: int | None = None,
    workflow_execution_mode: str = "sequential_runner",
    workflow_ctx: Any | None = None,
    workflow_run_id: str = "",
) -> dict[str, Any]:
    model_id = _model_id(agent_name)
    prompt_text = json.dumps(dict(prompt_payload), ensure_ascii=False, sort_keys=True)
    started_at = _utc_now()
    try:
        resolved_timeout = timeout_seconds or _timeout_seconds()
        if workflow_ctx is not None:
            response_text = await _invoke_adk_agent_text_node_async(
                workflow_ctx=workflow_ctx,
                agent_name=agent_name,
                model_id=model_id,
                prompt_text=prompt_text,
                run_id=workflow_run_id or f"{agent_name}-node",
                timeout_seconds=resolved_timeout,
            )
        else:
            response_text = await asyncio.wait_for(
                _invoke_adk_agent_text_async(
                    agent_name=agent_name,
                    model_id=model_id,
                    prompt_text=prompt_text,
                ),
                timeout=resolved_timeout,
            )
        invocation_error = ""
    except Exception as exc:  # pragma: no cover - live service failure shape varies.
        response_text = ""
        invocation_error = (
            f"{llm_provider_label(agent_name)}_agent_invocation_failed:{type(exc).__name__}"
        )
    completed_at = _utc_now()
    raw_output = _read_json_object(response_text) if response_text else None
    guardrail = guard_missionos_agent_output(raw_output, validate_intent=validate_intent)
    if invocation_error:
        guardrail = {
            "schema_version": MISSIONOS_AGENT_GUARDRAIL_SCHEMA_VERSION,
            "guardrail_passed": False,
            "blocking_reasons": [invocation_error],
            "validated_output": {},
        }
    elif raw_output is None:
        guardrail = {
            "schema_version": MISSIONOS_AGENT_GUARDRAIL_SCHEMA_VERSION,
            "guardrail_passed": False,
            "blocking_reasons": [
                f"{llm_provider_label(agent_name)}_agent_response_not_json_object"
            ],
            "validated_output": {},
        }
    evidence = {
        "schema_version": MISSIONOS_AGENT_INVOCATION_EVIDENCE_SCHEMA_VERSION,
        "agent_name": agent_name,
        "agent_role": agent_role,
        "provider": llm_provider_label(agent_name),
        "invocation_kind": llm_provider_label(agent_name),
        "model_id": model_id,
        "prompt_sha256": _sha256_text(prompt_text),
        "response_sha256": _sha256_text(response_text),
        "invocation_started_at": started_at.isoformat(),
        "invocation_completed_at": completed_at.isoformat(),
        "workflow_execution_mode": workflow_execution_mode,
        "adk_v2_graph_invoked": workflow_execution_mode.startswith("adk_v2_graph_"),
        "agent_node_execution": (
            "ctx.run_node" if workflow_ctx is not None else "standalone_runner"
        ),
        "workflow_child_node": workflow_ctx is not None,
        "standalone_runner_invoked": workflow_ctx is None,
        "nested_runner_invoked": False,
        "validated_output": guardrail.get("validated_output") or {},
        "guardrail_result": guardrail,
        "progress_counted": False,
        "llm_judgment_in_gate": False,
    }
    evidence["artifact_path"] = _persist_invocation_evidence(evidence)
    return evidence


def _run_agent_once(
    *,
    agent_name: str,
    agent_role: str,
    prompt_payload: Mapping[str, Any],
    validate_intent: bool = True,
    timeout_seconds: int | None = None,
    workflow_execution_mode: str = "sequential_runner",
) -> dict[str, Any]:
    return asyncio.run(
        _run_agent_once_async(
            agent_name=agent_name,
            agent_role=agent_role,
            prompt_payload=prompt_payload,
            validate_intent=validate_intent,
            timeout_seconds=timeout_seconds,
            workflow_execution_mode=workflow_execution_mode,
        )
    )


def _run_runtime_recovery_agent_once(
    *,
    prompt_payload: Mapping[str, Any],
    telemetry_snapshot: Mapping[str, Any],
    mission_context: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    agent_name = "missionos_runtime_recovery_agent"
    model_id = _model_id(agent_name)
    operator_request = mission_context.get("operator_recovery_request")
    operator_request = (
        dict(operator_request) if isinstance(operator_request, Mapping) else {}
    )
    requested_action = str(operator_request.get("requested_action") or "").strip()
    model_prompt_payload = dict(prompt_payload)
    if requested_action and requested_action not in _PARAMETERIZED_RUNTIME_RECOVERY_ACTIONS:
        model_prompt_payload = {
            "schema_version": "missionos_runtime_recovery_agent_prompt.v1",
            "task": "judge_non_parameterized_recovery_proposal",
            "authority_boundary": (
                "proposal only; human approval, rules, execution, and verification "
                "remain outside this Agent"
            ),
            "allowed_actions": [
                "continue",
                "hold",
                "return_to_launch",
                "land",
                "operator_review",
            ],
            "requested_action": requested_action,
            "judgment_instruction": (
                "Judge the requested recovery action against the observed mission "
                "facts. When return_to_launch is requested and no observed fact "
                "establishes that RTL is blocked, propose return_to_launch. Do not "
                "replace it with hold or operator_review merely because later "
                "Action Feasibility and human approval are still required."
            ),
            "telemetry_snapshot": {
                key: telemetry_snapshot.get(key)
                for key in (
                    "source",
                    "observed_at",
                    "sample_index",
                    "elapsed_seconds",
                    "telemetry",
                    "position",
                    "battery",
                    "wind",
                    "terrain",
                )
                if telemetry_snapshot.get(key) is not None
            },
            "mission_context": {
                key: mission_context.get(key)
                for key in (
                    "task_id",
                    "mission_phase",
                    "route_plan_id",
                    "route_deviation",
                )
                if mission_context.get(key) is not None
            },
            "recovery_policy": {
                key: recovery_policy.get(key)
                for key in (
                    "policy_ref",
                    "base_policy_ref",
                    "execution_scope",
                    "battery_return_threshold_percent",
                    "min_terrain_clearance_m",
                    "max_recovery_horizontal_speed_mps",
                    "max_recovery_duration_s",
                )
                if recovery_policy.get(key) is not None
            },
            "output_contract": {
                "selected_action_must_equal_requested_or_be_safer": True,
                "return_to_launch_parameters_must_be_empty": True,
                "requires_human_approval": True,
            },
        }
    prompt_text = json.dumps(model_prompt_payload, ensure_ascii=False, sort_keys=True)
    started_at = _utc_now()
    function_calls: list[dict[str, Any]] = []
    function_responses: list[dict[str, Any]] = []
    function_tool_results: list[dict[str, Any]] = []
    tool_arguments: list[dict[str, Any]] = []
    function_tool_called = False
    planner_tool_attached = False
    response_source = "llm_final_response"
    try:
        invocation = _invoke_runtime_recovery_agent_text_with_tools(
            model_id=model_id,
            prompt_text=prompt_text,
            telemetry_snapshot=telemetry_snapshot,
            mission_context=mission_context,
            recovery_policy=recovery_policy,
            timeout_seconds=timeout_seconds or _timeout_seconds(),
        )
        response_text = str(invocation.get("response_text") or "")
        response_source = str(invocation.get("response_source") or "llm_final_response")
        function_calls = [
            dict(item) for item in invocation.get("function_calls", []) if isinstance(item, Mapping)
        ]
        function_responses = [
            dict(item)
            for item in invocation.get("function_responses", [])
            if isinstance(item, Mapping)
        ]
        function_tool_results = [
            dict(item)
            for item in invocation.get("function_tool_results", [])
            if isinstance(item, Mapping)
        ]
        tool_arguments = [
            dict(item) for item in invocation.get("tool_arguments", []) if isinstance(item, Mapping)
        ]
        function_tool_called = bool(invocation.get("function_tool_called"))
        planner_tool_attached = bool(invocation.get("planner_tool_attached"))
        invocation_error = ""
    except Exception as exc:  # pragma: no cover - live service failure shape varies.
        response_text = ""
        invocation_error = (
            f"{llm_provider_label(agent_name)}_agent_invocation_failed:{type(exc).__name__}"
        )
    completed_at = _utc_now()
    if response_source == "function_tool_result_skip_summarization" and function_tool_results:
        # ADK intentionally emits no final prose/JSON after this FunctionTool
        # asks to skip summarization. The hosted judgment is the recorded tool
        # call; the concrete parameters are the guarded deterministic result.
        raw_output = _runtime_recovery_agent_output_from_planner_result(function_tool_results[-1])
        validated_output_source = "hosted_function_tool_call_and_guarded_result"
    else:
        raw_output = _read_json_object(response_text) if response_text else None
        validated_output_source = "llm_final_response"
    guardrail = guard_missionos_agent_output(raw_output, validate_intent=False)
    if invocation_error:
        guardrail = {
            "schema_version": MISSIONOS_AGENT_GUARDRAIL_SCHEMA_VERSION,
            "guardrail_passed": False,
            "blocking_reasons": [invocation_error],
            "validated_output": {},
        }
    elif raw_output is None:
        guardrail = {
            "schema_version": MISSIONOS_AGENT_GUARDRAIL_SCHEMA_VERSION,
            "guardrail_passed": False,
            "blocking_reasons": [
                f"{llm_provider_label(agent_name)}_agent_response_not_json_object"
            ],
            "validated_output": {},
        }
    evidence = {
        "schema_version": MISSIONOS_AGENT_INVOCATION_EVIDENCE_SCHEMA_VERSION,
        "agent_name": "missionos_runtime_recovery_agent",
        "agent_role": "MissionOS runtime recovery agent",
        "provider": llm_provider_label(agent_name),
        "invocation_kind": (
            "google_adk_function_tool_call"
            if planner_tool_attached
            else "google_adk_recovery_judgment"
        ),
        "model_id": model_id,
        "prompt_sha256": _sha256_text(prompt_text),
        "response_sha256": _sha256_text(response_text),
        "function_calls_sha256": _sha256_json(function_calls),
        "function_tool_results_sha256": _sha256_json(function_tool_results),
        "response_source": response_source,
        "validated_output_source": validated_output_source,
        "invocation_started_at": started_at.isoformat(),
        "invocation_completed_at": completed_at.isoformat(),
        "function_tool_name": MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_NAME,
        "function_calls": function_calls,
        "function_responses": function_responses,
        "function_tool_called": function_tool_called,
        "planner_tool_attached": planner_tool_attached,
        "tool_arguments": tool_arguments,
        "function_tool_results": function_tool_results,
        "validated_output": guardrail.get("validated_output") or {},
        "guardrail_result": guardrail,
        "progress_counted": False,
        "llm_judgment_in_gate": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
    }
    evidence["artifact_path"] = _persist_invocation_evidence(evidence)
    return evidence


def _root_prompt_payload(
    *,
    utterance: str,
    missionos_state: Mapping[str, Any],
    mission_designer_context: Mapping[str, Any] | None,
    coordinate_route: Mapping[str, Any] | None,
    conversation_history: list[dict[str, str]] | None,
    monitoring_observations: list[Mapping[str, Any]] | None = None,
    route_hint: str = "",
) -> dict[str, Any]:
    monitoring_payloads = _monitoring_observation_payloads(monitoring_observations)
    return {
        "schema_version": "missionos_chief_agent_runtime_prompt.v1",
        "role_contract": {
            "agent_layer": "missionos_intelligence",
            "operator_facing_agent": "missionos_chief_agent",
            "coordination_pattern": ("chief_intent_router_with_deterministic_specialist_floor"),
            "operator_facing_route": MISSIONOS_OPERATOR_FACING_ROUTE,
            "internal_capabilities": all_capability_descriptors_for_prompt(),
            "deterministic_routing_floor": "_CHIEF_TO_SPECIALIST",
            "safety_critic_agent": MISSIONOS_SAFETY_CRITIC_AGENT_NAME,
            "ambient_monitoring_model": (
                "event-driven Chief invocation; no continuous LLM while-loop"
            ),
            "monitoring_observation_contract": {
                "schema_version": MISSIONOS_MONITORING_OBSERVATION_SCHEMA_VERSION,
                "authority_status": "observation_only",
                "may_inform": [
                    "situation_summary",
                    "monitoring_focus",
                    "specialist selection",
                    "approval request proposal",
                ],
                "must_not_create": [
                    "approval",
                    "dispatch authority",
                    "execution",
                    "progress claim",
                ],
            },
            "gateway_owns": [
                "session binding",
                "source-bound context lookup",
                "deterministic guardrails",
                "human approval records",
                "artifact persistence",
                "execution route",
                "verifier result persistence",
            ],
            "agents_must_not_output": sorted(MISSIONOS_AGENT_FORBIDDEN_KEYS),
        },
        "route_hint": route_hint,
        "missionos_current_state": dict(missionos_state),
        "mission_designer_context": dict(mission_designer_context or {}),
        "coordinate_route": dict(coordinate_route or {}),
        "conversation_history": list(conversation_history or [])[-10:],
        "monitoring_observations": monitoring_payloads,
        "human_utterance": utterance[:2000],
    }


def _safety_critic_prompt_payload(
    *,
    utterance: str,
    chief_output: Mapping[str, Any],
    specialist_name: str,
    specialist_output: Mapping[str, Any],
    missionos_state: Mapping[str, Any],
    mission_designer_context: Mapping[str, Any] | None,
    coordinate_route: Mapping[str, Any] | None,
    monitoring_observations: list[Mapping[str, Any]] | None = None,
    route_hint: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": "missionos_safety_critic_prompt.v1",
        "route_hint": route_hint,
        "operator_facing_route": MISSIONOS_OPERATOR_FACING_ROUTE,
        "internal_capabilities": all_capability_descriptors_for_prompt(),
        "role_contract": {
            "critic_layer": "missionos_llm_boundary_review",
            "critic_may": [
                "review proposal boundaries",
                "identify missing evidence",
                "identify required human approval",
                "recommend Gateway checks",
            ],
            "critic_must_not": sorted(MISSIONOS_AGENT_FORBIDDEN_KEYS),
            "gateway_safety_kernel_remains_authoritative": True,
        },
        "human_utterance": utterance[:2000],
        "chief_agent_output": dict(chief_output),
        "specialist_agent": specialist_name,
        "specialist_agent_output": dict(specialist_output),
        "missionos_current_state": dict(missionos_state),
        "mission_designer_context": dict(mission_designer_context or {}),
        "coordinate_route": dict(coordinate_route or {}),
        "monitoring_observations": _monitoring_observation_payloads(monitoring_observations),
    }


def _specialist_prompt_payload(
    *,
    utterance: str,
    root_output: Mapping[str, Any],
    missionos_state: Mapping[str, Any],
    mission_designer_context: Mapping[str, Any] | None,
    coordinate_route: Mapping[str, Any] | None,
    conversation_history: list[dict[str, str]] | None,
    monitoring_observations: list[Mapping[str, Any]] | None = None,
    route_hint: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": "missionos_specialist_agent_prompt.v1",
        "route_hint": route_hint,
        "internal_capabilities": all_capability_descriptors_for_prompt(),
        "chief_agent_output": dict(root_output),
        "root_agent_output": dict(root_output),
        "missionos_current_state": dict(missionos_state),
        "mission_designer_context": dict(mission_designer_context or {}),
        "coordinate_route": dict(coordinate_route or {}),
        "conversation_history": list(conversation_history or [])[-10:],
        "monitoring_observations": _monitoring_observation_payloads(monitoring_observations),
        "human_utterance": utterance[:2000],
    }


def _runtime_recovery_prompt_payload(
    *,
    telemetry_snapshot: Mapping[str, Any],
    mission_context: Mapping[str, Any] | None,
    recovery_policy: Mapping[str, Any] | None,
) -> dict[str, Any]:
    policy = dict(recovery_policy or {})
    context = dict(mission_context or {})
    planner_preview = plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry_snapshot,
        mission_context=context,
        recovery_policy=policy,
        requested_action="",
        request_reason="pre_llm_compound_judgment_preview",
    )
    return {
        "schema_version": "missionos_runtime_recovery_agent_prompt.v1",
        "role_contract": {
            "agent_layer": "missionos_runtime_recovery_intelligence",
            "agent_may": [
                "monitor telemetry",
                "judge unsafe or uncertain mission state",
                (
                    "compare battery consumption rate against remaining route "
                    "distance and reserve margin"
                ),
                (
                    "compare every action_judgment_context candidate and its "
                    "blocking or unverified reasons; never treat LLM reasoning "
                    "as a feasibility upgrade"
                ),
                "compare terrain clearance against the planned source-backed terrain profile",
                "compare route cross-track deviation against wind-drift recovery limits",
                "compare obstacle or building-risk facts when supplied by a source-backed runtime",
                (
                    "propose return_to_launch when drift is recoverable or land "
                    "when drift makes route recovery unsafe"
                ),
                (
                    "call the runtime recovery maneuver planner FunctionTool before "
                    "proposing adjust_altitude, reroute, or avoid_obstacle"
                ),
                (
                    "when mission_context.operator_recovery_request.requested_action "
                    "matches a verified_selectable_candidate, call the planner for "
                    "that action and preserve it as a proposal-only judgment"
                ),
                (
                    "propose adjust_altitude, adjust_speed, reroute, or "
                    "avoid_obstacle only when supplied telemetry, policy, or tool "
                    "output include enough bounded parameters"
                ),
                (
                    "propose continue, hold, return_to_launch, land, "
                    "adjust_altitude, adjust_speed, reroute, avoid_obstacle, or "
                    "operator_review"
                ),
                (
                    "choose a mission strategy: monitor, global_reroute, "
                    "local_avoidance, hold, or rtl_or_land"
                ),
                (
                    "express optional intent_constraints for direction, minimum "
                    "clearance, destination meaning, duration, speed, or altitude "
                    "bounds; the compiler may not silently change them"
                ),
                (
                    "preserve the compiler strategy mapping exactly: "
                    "adjust_altitude, adjust_speed, and avoid_obstacle use "
                    "local_avoidance; reroute uses global_reroute; hold uses "
                    "hold; return_to_launch and land use rtl_or_land"
                ),
                (
                    "omit optional intent constraints unless the selected "
                    "candidate fields explicitly demonstrate that the compiler "
                    "can preserve them; do not derive minimum_clearance_m from "
                    "alternate-dropoff horizontal-clearance metadata"
                ),
                (
                    "prioritize a source-backed local route conflict when "
                    "conflict_assessment.local_avoidance_required is true; do "
                    "not replace it with a terrain action when the explicit "
                    "terrain_clearance_below_minimum judgment is false"
                ),
                "explain uncertainty",
            ],
            "function_tools": [
                {
                    "name": MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_NAME,
                    "purpose": (
                        "compute bounded tool-derived target_altitude_m and local "
                        "NED target_x_m/target_y_m candidates from source-backed "
                        "telemetry, obstacle, route, operator request, and policy facts"
                    ),
                    "must_use_before_actions": [
                        "adjust_altitude",
                        "reroute",
                        "avoid_obstacle",
                    ],
                    "copy_tool_proposed_parameters_exactly": True,
                }
            ],
            "gateway_owns": [
                "preauthorized recovery policy validation",
                "deterministic action allowlist",
                "human approval records",
                "backend dispatch request and receipt",
                "verifier outcome observation",
            ],
            "compiler_contract": {
                "compiler_may": [
                    "convert intent and constraints into a bounded executor candidate",
                    "add safety metadata already supported by source-backed facts",
                ],
                "compiler_must_not": [
                    "change strategy",
                    "change selected action",
                    "change direction or destination meaning",
                    "weaken minimum clearance or other intent constraints",
                ],
                "infeasible_constraints_return_to_agent": True,
            },
            "agents_must_not_output": sorted(MISSIONOS_AGENT_FORBIDDEN_KEYS),
        },
        "telemetry_snapshot": dict(telemetry_snapshot),
        "mission_context": context,
        "recovery_policy": policy,
        "action_judgment_context": {
            "judgment_candidates": list(
                planner_preview.get("judgment_candidates") or []
            ),
            "verified_selectable_candidates": list(
                planner_preview.get("candidates") or []
            ),
            "hazard_state": dict(
                planner_preview.get("hazard_state") or {}
            ),
            "action_feasibility": dict(
                planner_preview.get("action_feasibility") or {}
            ),
            "llm_may_explain_unverified_candidates": True,
            "llm_may_upgrade_feasibility": False,
            "approval_created": False,
            "dispatch_authority_created": False,
        },
    }


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _first_float(*values: Any) -> float | None:
    for value in values:
        parsed = _float_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _clamp(value: float, *, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _runtime_recovery_obstacle_points(
    telemetry_snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    obstacle = telemetry_snapshot.get("obstacle")
    obstacle = obstacle if isinstance(obstacle, Mapping) else {}
    manifest = obstacle.get("obstacle_manifest")
    manifest = manifest if isinstance(manifest, Mapping) else {}
    records: list[dict[str, Any]] = []

    def add_record(source_ref: str, record: Mapping[str, Any]) -> None:
        x_m = _first_float(record.get("x_m"), record.get("local_x_m"), record.get("x"))
        y_m = _first_float(record.get("y_m"), record.get("local_y_m"), record.get("y"))
        if x_m is None or y_m is None:
            return
        records.append(
            {
                "name": str(record.get("name") or f"obstacle_{len(records)}"),
                "kind": str(record.get("kind") or "obstacle"),
                "source": str(record.get("source") or source_ref),
                "source_ref": source_ref,
                "x_m": x_m,
                "y_m": y_m,
                "size_x_m": _first_float(record.get("size_x_m")),
                "size_y_m": _first_float(record.get("size_y_m")),
                "size_z_m": _first_float(record.get("size_z_m")),
            }
        )

    obstacles = manifest.get("obstacles")
    if isinstance(obstacles, list):
        for item in obstacles:
            if isinstance(item, Mapping):
                add_record("obstacle.obstacle_manifest", item)
    if not records:
        x_m = _first_float(manifest.get("dropoff_local_x_m"), obstacle.get("dropoff_local_x_m"))
        y_m = _first_float(manifest.get("dropoff_local_y_m"), obstacle.get("dropoff_local_y_m"))
        if x_m is not None and y_m is not None:
            records.append(
                {
                    "name": "landing_zone_blocked",
                    "kind": "landing_zone_risk",
                    "source": "obstacle_manifest.dropoff_local",
                    "source_ref": "obstacle.obstacle_manifest",
                    "x_m": x_m,
                    "y_m": y_m,
                    "size_x_m": None,
                    "size_y_m": None,
                    "size_z_m": None,
                }
            )
    return records


def _runtime_recovery_selected_obstacle_point(
    telemetry_snapshot: Mapping[str, Any],
    obstacle_points: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind the proposal to the active local conflict, not manifest order."""

    obstacle = telemetry_snapshot.get("obstacle")
    obstacle = obstacle if isinstance(obstacle, Mapping) else {}
    conflict = obstacle.get("conflict_assessment")
    conflict = conflict if isinstance(conflict, Mapping) else {}
    selected = conflict.get("nearest_obstacle")
    selected = selected if isinstance(selected, Mapping) else {}
    selected_index = selected.get("obstacle_index")
    if isinstance(selected_index, int) and 0 <= selected_index < len(obstacle_points):
        return obstacle_points[selected_index]
    selected_name = str(selected.get("obstacle_name") or "")
    if selected_name:
        for point in obstacle_points:
            if str(point.get("name") or "") == selected_name:
                return point
    return obstacle_points[0]


def _runtime_recovery_point_xy(record: Mapping[str, Any]) -> tuple[float, float] | None:
    x_m = _first_float(
        record.get("x_m"),
        record.get("local_x_m"),
        record.get("north_m"),
        record.get("n_m"),
        record.get("x"),
        record.get("n"),
    )
    y_m = _first_float(
        record.get("y_m"),
        record.get("local_y_m"),
        record.get("east_m"),
        record.get("e_m"),
        record.get("y"),
        record.get("e"),
    )
    if x_m is None or y_m is None:
        return None
    return x_m, y_m


def _runtime_recovery_unit_vector(
    dx: float,
    dy: float,
) -> tuple[float, float] | None:
    distance_m = math.hypot(dx, dy)
    if distance_m < 1e-6:
        return None
    return dx / distance_m, dy / distance_m


def _runtime_recovery_route_vector(
    *,
    telemetry_snapshot: Mapping[str, Any],
    mission_context: Mapping[str, Any],
    current_x_m: float,
    current_y_m: float,
    obstacle_x_m: float,
    obstacle_y_m: float,
) -> tuple[float, float, str]:
    route = telemetry_snapshot.get("route")
    context_route = mission_context.get("route")
    original_route = mission_context.get("original_route")
    route_sources = [
        ("telemetry_snapshot.route", route),
        ("mission_context.route", context_route),
        ("mission_context.original_route", original_route),
        ("mission_context", mission_context),
    ]

    def leg_vector(source_ref: str, source: Mapping[str, Any]) -> tuple[float, float, str] | None:
        for key in ("active_leg", "current_leg", "original_active_leg"):
            leg = source.get(key)
            if not isinstance(leg, Mapping):
                continue
            to_point = _runtime_recovery_point_xy(
                {
                    "x_m": leg.get("to_x_m"),
                    "local_x_m": leg.get("target_x_m"),
                    "north_m": leg.get("to_north_m"),
                    "n_m": leg.get("target_n_m"),
                    "y_m": leg.get("to_y_m"),
                    "local_y_m": leg.get("target_y_m"),
                    "east_m": leg.get("to_east_m"),
                    "e_m": leg.get("target_e_m"),
                }
            )
            from_point = _runtime_recovery_point_xy(
                {
                    "x_m": leg.get("from_x_m"),
                    "local_x_m": leg.get("source_x_m"),
                    "north_m": leg.get("from_north_m"),
                    "n_m": leg.get("source_n_m"),
                    "y_m": leg.get("from_y_m"),
                    "local_y_m": leg.get("source_y_m"),
                    "east_m": leg.get("from_east_m"),
                    "e_m": leg.get("source_e_m"),
                }
            )
            if to_point is None:
                continue
            if from_point is not None:
                vector = _runtime_recovery_unit_vector(
                    to_point[0] - from_point[0],
                    to_point[1] - from_point[1],
                )
            else:
                vector = _runtime_recovery_unit_vector(
                    to_point[0] - current_x_m,
                    to_point[1] - current_y_m,
                )
            if vector is not None:
                return vector[0], vector[1], f"{source_ref}.{key}"
        return None

    def points_vector(
        source_ref: str,
        source: Mapping[str, Any],
    ) -> tuple[float, float, str] | None:
        for key in (
            "planned_points",
            "planned_route_points",
            "original_route_points",
            "mission_waypoints",
            "waypoints",
        ):
            raw_points = source.get(key)
            if not isinstance(raw_points, list):
                continue
            points = [
                point
                for item in raw_points
                if isinstance(item, Mapping)
                for point in [_runtime_recovery_point_xy(item)]
                if point is not None
            ]
            if len(points) < 2:
                continue
            nearest_index = min(
                range(len(points)),
                key=lambda index: math.hypot(
                    points[index][0] - current_x_m,
                    points[index][1] - current_y_m,
                ),
            )
            if nearest_index < len(points) - 1:
                start = points[nearest_index]
                end = points[nearest_index + 1]
            else:
                start = points[nearest_index - 1]
                end = points[nearest_index]
            vector = _runtime_recovery_unit_vector(end[0] - start[0], end[1] - start[1])
            if vector is not None:
                return vector[0], vector[1], f"{source_ref}.{key}"
        return None

    for source_ref, source in route_sources:
        if not isinstance(source, Mapping):
            continue
        vector = leg_vector(source_ref, source) or points_vector(source_ref, source)
        if vector is not None:
            return vector

    fallback = _runtime_recovery_unit_vector(
        obstacle_x_m - current_x_m,
        obstacle_y_m - current_y_m,
    )
    if fallback is None:
        return 1.0, 0.0, "fallback.default_forward_vector"
    return fallback[0], fallback[1], "fallback.current_position_to_obstacle"


def _runtime_recovery_obstacle_conflict_assessment(
    *,
    telemetry_snapshot: Mapping[str, Any],
    mission_context: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    primary: Mapping[str, Any],
    current_x_m: float,
    current_y_m: float,
) -> dict[str, Any]:
    """Measure whether a source-backed obstacle needs local avoidance now.

    A real obstacle at the destination may still be hundreds of metres away.
    Presence alone must not compile into an immediate local detour. These are
    deterministic geometry/time facts for the LLM and authority guard; they do
    not choose a strategic action or create dispatch authority.
    """

    obstacle = telemetry_snapshot.get("obstacle")
    obstacle = obstacle if isinstance(obstacle, Mapping) else {}
    supplied = obstacle.get("conflict_assessment")
    supplied = supplied if isinstance(supplied, Mapping) else {}
    if isinstance(supplied.get("local_avoidance_required"), bool):
        return dict(supplied)

    obstacle_x_m = float(primary["x_m"])
    obstacle_y_m = float(primary["y_m"])
    unit_x, unit_y, route_source_ref = _runtime_recovery_route_vector(
        telemetry_snapshot=telemetry_snapshot,
        mission_context=mission_context,
        current_x_m=current_x_m,
        current_y_m=current_y_m,
        obstacle_x_m=obstacle_x_m,
        obstacle_y_m=obstacle_y_m,
    )
    relative_x_m = obstacle_x_m - current_x_m
    relative_y_m = obstacle_y_m - current_y_m
    distance_m = math.hypot(relative_x_m, relative_y_m)
    along_track_m = relative_x_m * unit_x + relative_y_m * unit_y
    cross_track_m = abs(relative_x_m * unit_y - relative_y_m * unit_x)
    obstacle_radius_m = (
        max(
            _first_float(primary.get("size_x_m")) or 0.0,
            _first_float(primary.get("size_y_m")) or 0.0,
        )
        / 2.0
    )
    buffer_m = _first_float(recovery_policy.get("obstacle_buffer_m")) or 20.0
    corridor_half_width_m = max(
        _first_float(recovery_policy.get("obstacle_route_corridor_half_width_m")) or 30.0,
        obstacle_radius_m + buffer_m,
    )
    lookahead_m = _first_float(recovery_policy.get("obstacle_local_lookahead_m")) or 150.0
    time_limit_s = _first_float(recovery_policy.get("obstacle_local_time_to_conflict_s")) or 30.0
    route = telemetry_snapshot.get("route")
    route = route if isinstance(route, Mapping) else {}
    position = telemetry_snapshot.get("position")
    position = position if isinstance(position, Mapping) else {}
    ground_speed_mps = _first_float(
        position.get("ground_speed_mps"),
        route.get("ground_speed_mps"),
        telemetry_snapshot.get("ground_speed_mps"),
    )
    time_to_conflict_s = (
        max(0.0, along_track_m - obstacle_radius_m) / ground_speed_mps
        if ground_speed_mps is not None and ground_speed_mps > 0.1
        else None
    )
    ahead = along_track_m >= -obstacle_radius_m
    route_intersects = cross_track_m <= corridor_half_width_m
    local_avoidance_required = bool(
        ahead
        and route_intersects
        and distance_m <= lookahead_m
        and (time_to_conflict_s is None or time_to_conflict_s <= time_limit_s)
    )
    return {
        "assessment_status": "computed",
        "local_avoidance_required": local_avoidance_required,
        "conflict_class": (
            "local_route_conflict"
            if local_avoidance_required
            else "distant_or_non_intersecting_obstacle"
        ),
        "distance_to_obstacle_m": round(distance_m, 3),
        "along_track_to_obstacle_m": round(along_track_m, 3),
        "cross_track_to_obstacle_m": round(cross_track_m, 3),
        "route_corridor_intersects": route_intersects,
        "obstacle_ahead": ahead,
        "lookahead_m": round(lookahead_m, 3),
        "time_to_conflict_s": (
            round(time_to_conflict_s, 3) if time_to_conflict_s is not None else None
        ),
        "time_to_conflict_limit_s": round(time_limit_s, 3),
        "ground_speed_mps": (round(ground_speed_mps, 3) if ground_speed_mps is not None else None),
        "route_vector_source_ref": route_source_ref,
    }


def _runtime_recovery_altitude_candidate(
    *,
    telemetry_snapshot: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    operator_request: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    operator_request = operator_request if isinstance(operator_request, Mapping) else {}
    terrain = telemetry_snapshot.get("terrain")
    terrain = terrain if isinstance(terrain, Mapping) else {}
    position = telemetry_snapshot.get("position")
    position = position if isinstance(position, Mapping) else {}
    current_altitude_m = _first_float(
        position.get("altitude_above_home_m"),
        telemetry_snapshot.get("altitude_above_home_m"),
    )
    clearance_m = _first_float(
        terrain.get("terrain_clearance_m"),
        terrain.get("clearance_m"),
        telemetry_snapshot.get("terrain_clearance_m"),
    )
    target_clearance_m = _first_float(
        terrain.get("terrain_clearance_target_m"),
        terrain.get("target_clearance_m"),
        telemetry_snapshot.get("terrain_clearance_target_m"),
        recovery_policy.get("min_terrain_clearance_m"),
    )
    margin_m = _first_float(
        terrain.get("terrain_clearance_margin_m"),
        terrain.get("clearance_margin_m"),
        telemetry_snapshot.get("terrain_clearance_margin_m"),
    )
    below_minimum = _boolish(
        terrain.get("terrain_clearance_below_minimum")
        or telemetry_snapshot.get("terrain_clearance_below_minimum")
    )
    requested_target_altitude_m = _first_float(
        operator_request.get("target_altitude_m"),
        operator_request.get("altitude_m"),
    )
    max_altitude_m = _first_float(recovery_policy.get("max_adjust_altitude_m")) or 500.0
    minimum_step_m = _first_float(recovery_policy.get("min_altitude_adjustment_step_m")) or 2.0
    if requested_target_altitude_m is not None:
        target_altitude_m = _clamp(
            requested_target_altitude_m,
            minimum=0.5,
            maximum=max_altitude_m,
        )
        return {
            "selected_bounded_action": "adjust_altitude",
            "proposed_parameters": {
                "target_altitude_m": round(target_altitude_m, 3),
            },
            "source_refs": [
                "mission_context.operator_recovery_request",
                "recovery_policy.max_adjust_altitude_m",
            ],
            "basis": {
                "current_altitude_m": round(current_altitude_m, 3)
                if current_altitude_m is not None
                else None,
                "requested_target_altitude_m": round(requested_target_altitude_m, 3),
                "max_adjust_altitude_m": round(max_altitude_m, 3),
            },
            "rationale": (
                "operator requested an altitude change; target altitude is bounded "
                "by recovery policy and remains proposal-only"
            ),
        }
    requested_delta_m = _first_float(operator_request.get("altitude_delta_m"))
    requested_climb_m = _first_float(
        operator_request.get("climb_m"),
        operator_request.get("step_m"),
    )
    requested_step_m = requested_delta_m if requested_delta_m is not None else requested_climb_m
    if requested_step_m is None and operator_request.get("requested_action") == "adjust_altitude":
        requested_step_m = (
            _first_float(recovery_policy.get("operator_requested_altitude_step_m")) or 10.0
        )
    if requested_step_m is not None and current_altitude_m is not None:
        if requested_delta_m is not None:
            adjustment_m = requested_step_m
            if 0 < abs(adjustment_m) < minimum_step_m:
                adjustment_m = minimum_step_m if adjustment_m > 0 else -minimum_step_m
            target_altitude_m = _clamp(
                current_altitude_m + adjustment_m,
                minimum=0.5,
                maximum=max_altitude_m,
            )
            rationale = (
                "operator requested a signed altitude delta; propose a bounded "
                "target altitude without changing approval or execution authority"
            )
            basis_step_key = "requested_delta_m"
        else:
            adjustment_m = max(minimum_step_m, requested_step_m)
            target_altitude_m = _clamp(
                current_altitude_m + adjustment_m,
                minimum=max(0.5, current_altitude_m),
                maximum=max_altitude_m,
            )
            rationale = (
                "operator requested a climb without an exact altitude; propose a "
                "bounded step above current altitude"
            )
            basis_step_key = "requested_step_m"
        return {
            "selected_bounded_action": "adjust_altitude",
            "proposed_parameters": {
                "target_altitude_m": round(target_altitude_m, 3),
            },
            "source_refs": [
                "mission_context.operator_recovery_request",
                "telemetry_snapshot.position",
                "recovery_policy.max_adjust_altitude_m",
            ],
            "basis": {
                "current_altitude_m": round(current_altitude_m, 3),
                basis_step_key: round(requested_step_m, 3),
                "adjustment_m": round(adjustment_m, 3),
            },
            "rationale": rationale,
        }
    if current_altitude_m is None or target_clearance_m is None:
        return None
    if not below_minimum and (margin_m is None or margin_m >= 0):
        return None

    buffer_m = _first_float(recovery_policy.get("altitude_adjustment_buffer_m")) or 5.0
    if clearance_m is not None:
        deficit_m = max(0.0, target_clearance_m - clearance_m)
    elif margin_m is not None:
        deficit_m = max(0.0, -margin_m)
    else:
        deficit_m = buffer_m
    climb_m = max(minimum_step_m, deficit_m + buffer_m)
    target_altitude_m = _clamp(
        current_altitude_m + climb_m,
        minimum=max(0.0, current_altitude_m),
        maximum=max_altitude_m,
    )
    return {
        "selected_bounded_action": "adjust_altitude",
        "proposed_parameters": {
            "target_altitude_m": round(target_altitude_m, 3),
        },
        "source_refs": [
            "telemetry_snapshot.terrain",
            "telemetry_snapshot.position",
            "recovery_policy.min_terrain_clearance_m",
        ],
        "basis": {
            "current_altitude_m": round(current_altitude_m, 3),
            "terrain_clearance_m": round(clearance_m, 3) if clearance_m is not None else None,
            "terrain_clearance_target_m": round(target_clearance_m, 3),
            "terrain_clearance_margin_m": round(margin_m, 3) if margin_m is not None else None,
            "buffer_m": round(buffer_m, 3),
            "climb_m": round(climb_m, 3),
        },
        "rationale": (
            "terrain clearance is below or inside the minimum margin; climb to "
            "restore clearance buffer"
        ),
    }


def _runtime_recovery_avoidance_candidate(
    *,
    telemetry_snapshot: Mapping[str, Any],
    mission_context: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> dict[str, Any] | None:
    recovery = telemetry_snapshot.get("recovery")
    recovery = recovery if isinstance(recovery, Mapping) else {}
    resume_verification = recovery.get("resume_safety_verification")
    resume_verification = resume_verification if isinstance(resume_verification, Mapping) else {}
    if (
        resume_verification.get("verification_status") == "failed"
        and resume_verification.get("original_dropoff_available") is False
    ):
        # A short local avoid cannot repair an occupied terminal.  Keep the
        # previous failed observation visible and let the alternate-dropoff
        # reroute candidate become a fresh, separately approved proposal.
        return None
    obstacle = telemetry_snapshot.get("obstacle")
    obstacle = obstacle if isinstance(obstacle, Mapping) else {}
    source_backed = _boolish(
        obstacle.get("obstacle_detected")
        or obstacle.get("building_risk_detected")
        or obstacle.get("landing_zone_blocked")
        or telemetry_snapshot.get("obstacle_detected")
        or telemetry_snapshot.get("building_risk_detected")
    )
    if not source_backed:
        return None
    obstacle_points = _runtime_recovery_obstacle_points(telemetry_snapshot)
    if not obstacle_points:
        return None
    position = telemetry_snapshot.get("position")
    position = position if isinstance(position, Mapping) else {}
    current_x_m = (
        _first_float(position.get("local_x_m"), telemetry_snapshot.get("local_x_m")) or 0.0
    )
    current_y_m = (
        _first_float(position.get("local_y_m"), telemetry_snapshot.get("local_y_m")) or 0.0
    )
    current_altitude_m = (
        _first_float(
            position.get("altitude_above_home_m"),
            telemetry_snapshot.get("altitude_above_home_m"),
        )
        or 0.0
    )
    primary = _runtime_recovery_selected_obstacle_point(
        telemetry_snapshot,
        obstacle_points,
    )
    obstacle_x_m = float(primary["x_m"])
    obstacle_y_m = float(primary["y_m"])
    distance_m = max(
        math.hypot(obstacle_x_m - current_x_m, obstacle_y_m - current_y_m),
        1e-6,
    )
    conflict_assessment = _runtime_recovery_obstacle_conflict_assessment(
        telemetry_snapshot=telemetry_snapshot,
        mission_context=mission_context,
        recovery_policy=recovery_policy,
        primary=primary,
        current_x_m=current_x_m,
        current_y_m=current_y_m,
    )
    if conflict_assessment.get("local_avoidance_required") is not True:
        return None
    unit_x, unit_y, route_source_ref = _runtime_recovery_route_vector(
        telemetry_snapshot=telemetry_snapshot,
        mission_context=mission_context,
        current_x_m=current_x_m,
        current_y_m=current_y_m,
        obstacle_x_m=obstacle_x_m,
        obstacle_y_m=obstacle_y_m,
    )
    # Compile the LLM's lateral-avoidance intent into one bounded point beyond
    # the obstacle.  The old compiler placed the point a short distance ahead
    # of the *current aircraft*, which made the vehicle leave the route far
    # before the obstacle and then allowed AUTO to steer back through it.  A
    # target beyond the obstacle makes the approved OFFBOARD leg pass along the
    # side of the collision box; AUTO may rejoin only after that leg succeeds.
    # The operator still approves the exact compiled target before execution.
    perp_x = -unit_y
    perp_y = unit_x
    obstacle_half_x_m = (_first_float(primary.get("size_x_m")) or 0.0) / 2.0
    obstacle_half_y_m = (_first_float(primary.get("size_y_m")) or 0.0) / 2.0
    obstacle_half_along_route_m = abs(unit_x) * obstacle_half_x_m + abs(unit_y) * obstacle_half_y_m
    obstacle_half_lateral_m = abs(perp_x) * obstacle_half_x_m + abs(perp_y) * obstacle_half_y_m
    obstacle_buffer_m = _first_float(recovery_policy.get("obstacle_buffer_m")) or 20.0
    required_lateral_clearance_m = max(
        _first_float(recovery_policy.get("obstacle_lateral_clearance_m")) or 30.0,
        obstacle_half_lateral_m + obstacle_buffer_m,
    )
    along_track_to_obstacle_m = (obstacle_x_m - current_x_m) * unit_x + (
        obstacle_y_m - current_y_m
    ) * unit_y
    if along_track_to_obstacle_m <= 1.0:
        return None
    pass_distance_m = max(
        obstacle_half_along_route_m
        + max(
            obstacle_buffer_m,
            _first_float(
                recovery_policy.get("obstacle_minimum_clearance_m")
            )
            or 0.0,
        )
        + 2.0,
        _first_float(recovery_policy.get("obstacle_min_pass_distance_m")) or 30.0,
    )
    target_along_track_m = along_track_to_obstacle_m + pass_distance_m
    expanded_half_along_route_m = obstacle_half_along_route_m + obstacle_buffer_m
    clearance_entry_along_track_m = along_track_to_obstacle_m - expanded_half_along_route_m
    if clearance_entry_along_track_m <= 1.0:
        # A direct diagonal bypass cannot prove clearance when the aircraft is
        # already inside the obstacle's expanded near face.  A separately
        # proposed retreat/escape is required instead.
        return None
    clearance_entry_fraction_on_recovery_leg = _clamp(
        clearance_entry_along_track_m / target_along_track_m,
        minimum=0.01,
        maximum=0.99,
    )
    # The lateral offset grows linearly along the direct OFFBOARD leg.  Scale
    # the final offset so that, when the leg is abreast of the obstacle, it is
    # already outside the expanded footprint at its *near face*.  Checking
    # only at the obstacle centre lets the diagonal cut through a near corner.
    # Two metres avoid a boundary-equality pass being treated as clearance.
    target_lateral_offset_m = (
        required_lateral_clearance_m + 2.0
    ) / clearance_entry_fraction_on_recovery_leg
    target_x_m = current_x_m + unit_x * target_along_track_m + perp_x * target_lateral_offset_m
    target_y_m = current_y_m + unit_y * target_along_track_m + perp_y * target_lateral_offset_m
    max_abs_m = _first_float(recovery_policy.get("max_reroute_target_abs_m")) or 5000.0
    target_x_m = _clamp(target_x_m, minimum=-max_abs_m, maximum=max_abs_m)
    target_y_m = _clamp(target_y_m, minimum=-max_abs_m, maximum=max_abs_m)

    altitude_candidate = _runtime_recovery_altitude_candidate(
        telemetry_snapshot=telemetry_snapshot,
        recovery_policy=recovery_policy,
        operator_request={},
    )
    terrain = telemetry_snapshot.get("terrain")
    terrain = terrain if isinstance(terrain, Mapping) else {}
    target_clearance_m = (
        _first_float(
            terrain.get("terrain_clearance_target_m"),
            terrain.get("target_clearance_m"),
            recovery_policy.get("min_terrain_clearance_m"),
        )
        or 30.0
    )
    avoidance_climb_m = _first_float(recovery_policy.get("obstacle_avoidance_climb_m")) or 15.0
    max_altitude_m = _first_float(recovery_policy.get("max_adjust_altitude_m")) or 500.0
    altitude_m = max(
        current_altitude_m,
        target_clearance_m + avoidance_climb_m,
        _first_float(
            (altitude_candidate or {}).get("proposed_parameters", {}).get("target_altitude_m")
            if altitude_candidate
            else None
        )
        or 0.0,
    )
    altitude_m = _clamp(altitude_m, minimum=0.0, maximum=max_altitude_m)
    return {
        "selected_bounded_action": "avoid_obstacle",
        "proposed_parameters": {
            "target_x_m": round(target_x_m, 3),
            "target_y_m": round(target_y_m, 3),
            "target_altitude_m": round(altitude_m, 3),
            "source_obstacle_name": str(primary.get("name") or ""),
        },
        "source_refs": [
            str(primary.get("source_ref") or "telemetry_snapshot.obstacle"),
            "telemetry_snapshot.position",
            route_source_ref,
            "recovery_policy.max_reroute_target_abs_m",
        ],
        "recovery_path": {
            "frame_id": "local_ned_xy_altitude_up",
            "waypoints": [
                {
                    "x_m": round(target_x_m, 3),
                    "y_m": round(target_y_m, 3),
                    "z_m": round(altitude_m, 3),
                }
            ],
            "source_refs": [
                "telemetry_snapshot.position",
                str(primary.get("source_ref") or "telemetry_snapshot.obstacle"),
                "recovery_policy.obstacle_minimum_clearance_m",
            ],
        },
        "basis": {
            "current_x_m": round(current_x_m, 3),
            "current_y_m": round(current_y_m, 3),
            "obstacle_x_m": round(obstacle_x_m, 3),
            "obstacle_y_m": round(obstacle_y_m, 3),
            "obstacle_name": primary.get("name"),
            "distance_to_obstacle_m": round(distance_m, 3),
            "along_track_to_obstacle_m": round(along_track_to_obstacle_m, 3),
            "obstacle_half_along_route_m": round(obstacle_half_along_route_m, 3),
            "obstacle_half_lateral_m": round(obstacle_half_lateral_m, 3),
            "obstacle_buffer_m": round(obstacle_buffer_m, 3),
            "expanded_half_along_route_m": round(expanded_half_along_route_m, 3),
            "clearance_entry_along_track_m": round(clearance_entry_along_track_m, 3),
            "clearance_entry_fraction_on_recovery_leg": round(
                clearance_entry_fraction_on_recovery_leg, 6
            ),
            "pass_distance_after_obstacle_m": round(pass_distance_m, 3),
            "required_lateral_clearance_m": round(required_lateral_clearance_m, 3),
            "minimum_lateral_clearance_m": round(
                required_lateral_clearance_m + 2.0, 3
            ),
            "avoidance_side": "left",
            "target_lateral_offset_m": round(target_lateral_offset_m, 3),
            "target_is_beyond_obstacle": True,
            "route_vector_source_ref": route_source_ref,
            "conflict_assessment": conflict_assessment,
        },
        "rationale": (
            "source-backed local route conflict is present; pass beside the "
            "expanded collision footprint, reach a bounded point beyond the "
            "obstacle, and only then permit the original route to rejoin"
        ),
    }


def _runtime_recovery_alternate_dropoff_candidate(
    *,
    telemetry_snapshot: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> dict[str, Any] | None:
    recovery = telemetry_snapshot.get("recovery")
    recovery = recovery if isinstance(recovery, Mapping) else {}
    resume_verification = recovery.get("resume_safety_verification")
    resume_verification = resume_verification if isinstance(resume_verification, Mapping) else {}
    obstacle = telemetry_snapshot.get("obstacle")
    obstacle = obstacle if isinstance(obstacle, Mapping) else {}
    manifest = obstacle.get("obstacle_manifest")
    manifest = manifest if isinstance(manifest, Mapping) else {}
    raw_candidate = resume_verification.get("alternate_dropoff_candidate")
    source_ref = "telemetry_snapshot.recovery.resume_safety_verification"
    if not isinstance(raw_candidate, Mapping):
        raw_candidate = manifest.get("alternate_dropoff_candidate")
        source_ref = "telemetry_snapshot.obstacle.obstacle_manifest"
    if not isinstance(raw_candidate, Mapping):
        return None
    original_dropoff_available = resume_verification.get(
        "original_dropoff_available",
        manifest.get("original_dropoff_available"),
    )
    if original_dropoff_available is not False:
        return None
    action = str(raw_candidate.get("selected_bounded_action") or "").strip()
    parameters = raw_candidate.get("proposed_parameters")
    parameters = parameters if isinstance(parameters, Mapping) else {}
    target_x_m = _first_float(parameters.get("target_x_m"))
    target_y_m = _first_float(parameters.get("target_y_m"))
    target_altitude_m = _first_float(parameters.get("target_altitude_m"))
    if action != "reroute" or target_x_m is None or target_y_m is None:
        return None
    max_abs_m = _first_float(recovery_policy.get("max_reroute_target_abs_m")) or 5000.0
    if abs(target_x_m) > max_abs_m or abs(target_y_m) > max_abs_m:
        return None
    proposed_parameters: dict[str, Any] = {
        "target_x_m": round(target_x_m, 3),
        "target_y_m": round(target_y_m, 3),
        "alternate_dropoff": True,
        "resume_original_route": False,
    }
    if target_altitude_m is not None:
        max_altitude_m = _first_float(recovery_policy.get("max_adjust_altitude_m")) or 500.0
        proposed_parameters["target_altitude_m"] = round(
            _clamp(target_altitude_m, minimum=0.5, maximum=max_altitude_m),
            3,
        )
    source_obstacle_name = str(parameters.get("source_obstacle_name") or "").strip()
    if source_obstacle_name:
        proposed_parameters["source_obstacle_name"] = source_obstacle_name
    return {
        "selected_bounded_action": "reroute",
        "proposed_parameters": proposed_parameters,
        "source_refs": [
            source_ref,
            "telemetry_snapshot.obstacle.obstacle_manifest",
            "recovery_policy.max_reroute_target_abs_m",
        ],
        "basis": {
            **dict(raw_candidate.get("basis") or {}),
            "previous_resume_verification_status": resume_verification.get("verification_status"),
            "original_dropoff_available": resume_verification.get(
                "original_dropoff_available",
                manifest.get("original_dropoff_available"),
            ),
        },
        "rationale": (
            "the collision-backed original dropoff is unavailable after the "
            "previous bounded recovery; propose the manifest-bound alternate "
            "hover point without resuming the original route"
        ),
    }


def _runtime_recovery_requested_reroute_candidate(
    *,
    telemetry_snapshot: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    operator_request: Mapping[str, Any],
) -> dict[str, Any] | None:
    target_x_m = _first_float(operator_request.get("target_x_m"), operator_request.get("x_m"))
    target_y_m = _first_float(operator_request.get("target_y_m"), operator_request.get("y_m"))
    position = telemetry_snapshot.get("position")
    position = position if isinstance(position, Mapping) else {}
    current_x_m = _first_float(position.get("local_x_m"), telemetry_snapshot.get("local_x_m"))
    current_y_m = _first_float(position.get("local_y_m"), telemetry_snapshot.get("local_y_m"))
    source_refs = [
        "mission_context.operator_recovery_request",
        "recovery_policy.max_reroute_target_abs_m",
    ]
    if target_x_m is None or target_y_m is None:
        if operator_request.get("requested_action") != "reroute":
            return None
        if current_x_m is None or current_y_m is None:
            return None
        route = telemetry_snapshot.get("route")
        route = route if isinstance(route, Mapping) else {}
        active_leg = route.get("active_leg")
        active_leg = active_leg if isinstance(active_leg, Mapping) else {}
        unit: tuple[float, float] | None = None
        from_x = _first_float(active_leg.get("from_x_m"), active_leg.get("start_x_m"))
        from_y = _first_float(active_leg.get("from_y_m"), active_leg.get("start_y_m"))
        to_x = _first_float(active_leg.get("to_x_m"), active_leg.get("end_x_m"))
        to_y = _first_float(active_leg.get("to_y_m"), active_leg.get("end_y_m"))
        if None not in (from_x, from_y, to_x, to_y):
            unit = _runtime_recovery_unit_vector(to_x - from_x, to_y - from_y)
            source_refs.append("telemetry_snapshot.route.active_leg")
        if unit is None:
            unit = (1.0, 0.0)
            source_refs.append("fallback.default_forward_vector")
        forward_m = _first_float(recovery_policy.get("operator_reroute_forward_m")) or 80.0
        lateral_m = _first_float(recovery_policy.get("operator_reroute_lateral_m")) or 30.0
        target_x_m = current_x_m + unit[0] * forward_m - unit[1] * lateral_m
        target_y_m = current_y_m + unit[1] * forward_m + unit[0] * lateral_m
        source_refs.append("telemetry_snapshot.position")
    max_abs_m = _first_float(recovery_policy.get("max_reroute_target_abs_m")) or 5000.0
    target_x_m = _clamp(target_x_m, minimum=-max_abs_m, maximum=max_abs_m)
    target_y_m = _clamp(target_y_m, minimum=-max_abs_m, maximum=max_abs_m)
    proposed_parameters = {
        "target_x_m": round(target_x_m, 3),
        "target_y_m": round(target_y_m, 3),
    }
    requested_altitude_m = _first_float(
        operator_request.get("target_altitude_m"),
        operator_request.get("altitude_m"),
    )
    if requested_altitude_m is not None:
        max_altitude_m = _first_float(recovery_policy.get("max_adjust_altitude_m")) or 500.0
        proposed_parameters["target_altitude_m"] = round(
            _clamp(requested_altitude_m, minimum=0.5, maximum=max_altitude_m),
            3,
        )
    return {
        "selected_bounded_action": "reroute",
        "proposed_parameters": proposed_parameters,
        "source_refs": source_refs,
        "basis": {
            "current_x_m": current_x_m,
            "current_y_m": current_y_m,
            "requested_target_x_m": round(target_x_m, 3),
            "requested_target_y_m": round(target_y_m, 3),
            "max_reroute_target_abs_m": round(max_abs_m, 3),
        },
        "rationale": (
            "operator requested a local reroute target; target is bounded by "
            "recovery policy and remains proposal-only"
        ),
    }


def plan_runtime_recovery_maneuver(
    *,
    telemetry_snapshot: Mapping[str, Any],
    mission_context: Mapping[str, Any] | None = None,
    recovery_policy: Mapping[str, Any] | None = None,
    requested_action: str = "",
    request_reason: str = "",
) -> dict[str, Any]:
    """Deterministically compute bounded recovery proposal parameters.

    This is the implementation behind the Runtime Recovery Agent FunctionTool.
    It returns candidate parameters only; it never approves, dispatches, executes,
    verifies, or counts progress.
    """

    policy = dict(recovery_policy or {})
    context = dict(mission_context or {})
    operator_request = context.get("operator_recovery_request")
    operator_request = operator_request if isinstance(operator_request, Mapping) else {}
    requested = str(requested_action or "").strip()
    candidates: list[dict[str, Any]] = []
    altitude_candidate = _runtime_recovery_altitude_candidate(
        telemetry_snapshot=telemetry_snapshot,
        recovery_policy=policy,
        operator_request=operator_request,
    )
    alternate_dropoff_candidate = _runtime_recovery_alternate_dropoff_candidate(
        telemetry_snapshot=telemetry_snapshot,
        recovery_policy=policy,
    )
    requested_reroute_candidate = _runtime_recovery_requested_reroute_candidate(
        telemetry_snapshot=telemetry_snapshot,
        recovery_policy=policy,
        operator_request=operator_request,
    )
    avoidance_candidate = _runtime_recovery_avoidance_candidate(
        telemetry_snapshot=telemetry_snapshot,
        mission_context=context,
        recovery_policy=policy,
    )
    if alternate_dropoff_candidate is not None:
        candidates.append(alternate_dropoff_candidate)
    if requested_reroute_candidate is not None:
        candidates.append(requested_reroute_candidate)
    if avoidance_candidate is not None:
        candidates.append(avoidance_candidate)
    if altitude_candidate is not None:
        candidates.append(altitude_candidate)

    hazard_state = build_runtime_recovery_hazard_state(
        telemetry_snapshot=telemetry_snapshot,
        recovery_policy=policy,
        observed_at=str(telemetry_snapshot.get("observed_at") or ""),
    )
    action_feasibility = verify_runtime_recovery_action_candidates(
        candidates=candidates,
        hazard_state=hazard_state,
        recovery_policy=policy,
    )
    unfiltered_candidates = [dict(candidate) for candidate in candidates]
    feasibility_evaluations = [
        dict(item)
        for item in action_feasibility.get("evaluations") or []
        if isinstance(item, Mapping)
    ]
    judgment_candidates = [
        {
            "candidate": dict(candidate),
            "feasibility_status": (
                feasibility_evaluations[index].get("feasibility_status")
                if index < len(feasibility_evaluations)
                else "unverified"
            ),
            "blocking_reasons": (
                list(
                    feasibility_evaluations[index].get(
                        "blocking_reasons"
                    )
                    or []
                )
                if index < len(feasibility_evaluations)
                else ["action_feasibility_evaluation_missing"]
            ),
            "unverified_reasons": (
                list(
                    feasibility_evaluations[index].get(
                        "unverified_reasons"
                    )
                    or []
                )
                if index < len(feasibility_evaluations)
                else ["action_feasibility_evaluation_missing"]
            ),
            "eligible_for_selection": bool(
                index < len(feasibility_evaluations)
                and feasibility_evaluations[index].get(
                    "feasibility_status"
                )
                == "verified_feasible"
            ),
            "dispatch_authority_created": False,
        }
        for index, candidate in enumerate(unfiltered_candidates)
    ]
    if policy.get("action_feasibility_required") is True:
        candidates = [
            dict(candidate)
            for candidate in action_feasibility.get(
                "verified_feasible_candidates"
            )
            or []
            if isinstance(candidate, Mapping)
        ]
    obstacle = telemetry_snapshot.get("obstacle")
    obstacle = obstacle if isinstance(obstacle, Mapping) else {}
    conflict = obstacle.get("conflict_assessment")
    conflict = conflict if isinstance(conflict, Mapping) else {}
    if conflict.get("local_avoidance_required") is True:
        # A local collision does not become an altitude/reroute proposal merely
        # because the actual avoidance candidate is unverified.  Keep all
        # candidates in judgment_candidates for the LLM explanation, while the
        # deterministic selectable set remains scoped to the primary hazard.
        # A manifest-bound alternate dropoff is the terminal-conflict recovery,
        # not an arbitrary reroute: it must remain selectable when the previous
        # resume verification proved that the original dropoff is unavailable.
        candidates = [
            candidate
            for candidate in candidates
            if candidate.get("selected_bounded_action") == "avoid_obstacle"
            or (
                candidate.get("selected_bounded_action") == "reroute"
                and isinstance(candidate.get("proposed_parameters"), Mapping)
                and candidate["proposed_parameters"].get("alternate_dropoff") is True
                and candidate["proposed_parameters"].get("resume_original_route") is False
            )
        ]

    if requested in _PARAMETERIZED_RUNTIME_RECOVERY_ACTIONS:
        ranked = [
            candidate
            for candidate in candidates
            if candidate.get("selected_bounded_action") == requested
        ]
        requested_action_matched = bool(ranked)
    else:
        ranked = candidates
        requested_action_matched = not requested
    recommended = ranked[0] if ranked else None
    if (
        recommended is None
        and requested in _PARAMETERIZED_RUNTIME_RECOVERY_ACTIONS
        and not requested_action_matched
    ):
        selection_basis = "requested_action_not_compilable"
    elif recommended is None:
        selection_basis = "no_candidate"
    elif requested in _PARAMETERIZED_RUNTIME_RECOVERY_ACTIONS:
        selection_basis = (
            "requested_action" if requested_action_matched else "requested_action_not_compilable"
        )
    else:
        selection_basis = "best_available_candidate"
    return {
        "schema_version": MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_SCHEMA_VERSION,
        "tool_name": MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_NAME,
        "tool_status": "computed" if recommended else "insufficient_context",
        "requested_action": requested,
        "requested_action_matched": requested_action_matched,
        "selection_basis": selection_basis,
        "request_reason": str(request_reason or "")[:500],
        "recommended_candidate": dict(recommended) if recommended else {},
        "candidates": [dict(candidate) for candidate in candidates],
        "unfiltered_candidates": unfiltered_candidates,
        # The hosted judge sees every deterministic candidate and why it is
        # blocked or unverified.  Only ``candidates`` above is selectable;
        # explanatory LLM reasoning cannot upgrade an unverified envelope.
        "judgment_candidates": judgment_candidates,
        "hazard_state": hazard_state,
        "action_feasibility": action_feasibility,
        "candidate_actions": [
            str(candidate.get("selected_bounded_action") or "") for candidate in candidates
        ],
        "dispatch_authority_created": False,
        "operator_approval_required": True,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _candidate_parameters_match(
    proposed_parameters: Mapping[str, Any],
    candidate_parameters: Mapping[str, Any],
    *,
    tolerance: float = 0.05,
) -> bool:
    for key, expected in candidate_parameters.items():
        if isinstance(expected, bool):
            # Boolean safety metadata belongs to the matched deterministic
            # compiler output; it is restored below and is not a coordinate
            # the LLM may invent or modify.
            continue
        expected_number = _float_or_none(expected)
        if expected_number is None:
            continue
        actual_number = _first_float(
            proposed_parameters.get(key),
            proposed_parameters.get(key.removeprefix("target_")),
        )
        if actual_number is None or abs(actual_number - expected_number) > tolerance:
            return False
    return True


def _matching_recovery_tool_candidate(
    *,
    selected_action: str,
    proposed_parameters: Mapping[str, Any],
    planner_tool_results: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    for result in planner_tool_results:
        recommended = result.get("recommended_candidate")
        if isinstance(recommended, Mapping):
            candidates = [recommended]
        else:
            candidates = result.get("candidates")
            if not isinstance(candidates, list):
                candidates = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            if candidate.get("selected_bounded_action") != selected_action:
                continue
            candidate_parameters = candidate.get("proposed_parameters")
            if not isinstance(candidate_parameters, Mapping):
                continue
            if _candidate_parameters_match(proposed_parameters, candidate_parameters):
                return dict(candidate)
    return None


def _recommended_recovery_tool_actions(
    planner_tool_results: list[Mapping[str, Any]],
) -> set[str]:
    selected_actions: set[str] = set()
    for result in planner_tool_results:
        recommended = result.get("recommended_candidate")
        if not isinstance(recommended, Mapping):
            continue
        selected = str(recommended.get("selected_bounded_action") or "").strip()
        if selected:
            selected_actions.add(selected)
    return selected_actions


def _telemetry_risk_reasons(
    telemetry_snapshot: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    battery = telemetry_snapshot.get("battery")
    battery = battery if isinstance(battery, Mapping) else {}
    endurance = battery.get("endurance_projection")
    endurance = endurance if isinstance(endurance, Mapping) else {}
    return_home = battery.get("return_home_projection")
    return_home = return_home if isinstance(return_home, Mapping) else {}
    wind = telemetry_snapshot.get("wind")
    wind = wind if isinstance(wind, Mapping) else {}
    telemetry = telemetry_snapshot.get("telemetry")
    telemetry = telemetry if isinstance(telemetry, Mapping) else {}
    route = telemetry_snapshot.get("route")
    route = route if isinstance(route, Mapping) else {}
    terrain = telemetry_snapshot.get("terrain")
    terrain = terrain if isinstance(terrain, Mapping) else {}
    obstacle = telemetry_snapshot.get("obstacle")
    obstacle = obstacle if isinstance(obstacle, Mapping) else {}
    recovery = telemetry_snapshot.get("recovery")
    recovery = recovery if isinstance(recovery, Mapping) else {}

    battery_remaining = _float_or_none(
        battery.get("remaining_percent")
        or battery.get("battery_remaining_percent")
        or telemetry_snapshot.get("battery_remaining_percent")
    )
    battery_threshold = _float_or_none(recovery_policy.get("battery_return_threshold_percent"))
    if battery_threshold is None:
        battery_threshold = 20.0
    battery_warning = (
        str(
            battery.get("warning")
            or battery.get("battery_warning")
            or telemetry_snapshot.get("battery_warning")
            or ""
        )
        .strip()
        .lower()
    )
    if battery_remaining is not None and battery_remaining <= battery_threshold:
        reasons.append("battery_insufficient")
    if battery_warning in {"low", "critical", "emergency", "failed"}:
        reasons.append(f"battery_warning_{battery_warning}")
    if _boolish(
        endurance.get("projected_insufficient_for_route")
        or telemetry_snapshot.get("battery_projected_insufficient_for_route")
    ):
        reasons.append("battery_projected_insufficient_for_route")
    if _boolish(
        return_home.get("projected_insufficient_for_return_home")
        or telemetry_snapshot.get("battery_projected_insufficient_for_return_home")
    ):
        reasons.append("battery_projected_insufficient_for_return_home")

    terrain_clearance = _float_or_none(
        terrain.get("terrain_clearance_m") or telemetry_snapshot.get("terrain_clearance_m")
    )
    terrain_clearance_target = _float_or_none(
        terrain.get("terrain_clearance_target_m")
        or telemetry_snapshot.get("terrain_clearance_target_m")
        or recovery_policy.get("min_terrain_clearance_m")
    )
    # Respect the same clearance grace the terrain projection uses, so the risk
    # side and the projection side agree. Without grace a tiny terrain-following
    # error (e.g. 29.2 m vs a 30 m target) would be flagged as a hard breach even
    # though the projection reports it as within grace (below_minimum=false).
    terrain_clearance_grace = (
        _float_or_none(
            terrain.get("terrain_clearance_grace_m")
            or telemetry_snapshot.get("terrain_clearance_grace_m")
        )
        or 0.0
    )
    if _boolish(
        terrain.get("terrain_clearance_below_minimum")
        or telemetry_snapshot.get("terrain_clearance_below_minimum")
    ):
        reasons.append("terrain_clearance_below_minimum")
    elif (
        terrain_clearance is not None
        and terrain_clearance_target is not None
        and terrain_clearance < (terrain_clearance_target - terrain_clearance_grace)
    ):
        reasons.append("terrain_clearance_below_minimum")

    wind_speed = _float_or_none(
        wind.get("speed_mps")
        or wind.get("observed_speed_mps")
        or telemetry_snapshot.get("wind_speed_mps")
    )
    wind_limit = _float_or_none(recovery_policy.get("max_wind_speed_mps"))
    if wind_limit is not None and wind_speed is not None and wind_speed > wind_limit:
        reasons.append("wind_above_recovery_limit")

    route_deviation = _float_or_none(
        route.get("deviation_xy_m")
        or route.get("wind_drift_deviation_xy_m")
        or telemetry_snapshot.get("route_deviation_xy_m")
    )
    route_limit = _float_or_none(recovery_policy.get("max_route_deviation_xy_m"))
    if route_limit is not None and route_deviation is not None and route_deviation > route_limit:
        reasons.append("route_deviation_above_limit")
    route_emergency_limit = _float_or_none(
        recovery_policy.get("emergency_landing_route_deviation_xy_m")
    )
    if (
        route_emergency_limit is not None
        and route_deviation is not None
        and route_deviation > route_emergency_limit
    ):
        reasons.append("route_deviation_emergency_landing_candidate")

    if _boolish(telemetry.get("stale") or telemetry_snapshot.get("telemetry_stale")):
        reasons.append("telemetry_stale")
    if _boolish(telemetry.get("dropout") or telemetry_snapshot.get("telemetry_dropout")):
        reasons.append("telemetry_dropout")
    obstacle_source_backed = _boolish(
        obstacle.get("obstacle_detected")
        or obstacle.get("building_risk_detected")
        or obstacle.get("landing_zone_blocked")
        or telemetry_snapshot.get("obstacle_detected")
        or telemetry_snapshot.get("building_risk_detected")
    )
    conflict_assessment = obstacle.get("conflict_assessment")
    conflict_assessment = conflict_assessment if isinstance(conflict_assessment, Mapping) else {}
    local_avoidance_required = conflict_assessment.get("local_avoidance_required")
    if obstacle_source_backed and local_avoidance_required is not False:
        reasons.append("obstacle_or_building_risk")
    if _boolish(
        recovery.get("telemetry_stale")
        or recovery.get("recovery_telemetry_stale")
        or telemetry_snapshot.get("recovery_telemetry_stale")
    ):
        reasons.append("recovery_telemetry_stale")
    if _boolish(
        recovery.get("observation_lost")
        or recovery.get("recovery_observation_lost")
        or telemetry_snapshot.get("recovery_observation_lost")
    ):
        reasons.append("recovery_observation_lost")
    if (
        recovery.get("final_landing_safe") is False
        or telemetry_snapshot.get("final_landing_safe") is False
    ) and _boolish(
        recovery.get("command_ack_observed")
        or recovery.get("recovery_command_ack_observed")
        or telemetry_snapshot.get("recovery_command_ack_observed")
    ):
        reasons.append("recovery_final_landing_not_observed")
    if _boolish(
        recovery.get("stalled")
        or recovery.get("recovery_stalled")
        or telemetry_snapshot.get("recovery_stalled")
    ):
        reasons.append("recovery_return_stalled")
    incomplete_reason = str(
        recovery.get("recovery_incomplete_reason")
        or telemetry_snapshot.get("recovery_incomplete_reason")
        or ""
    ).strip()
    if incomplete_reason:
        reasons.append(incomplete_reason)
    return list(dict.fromkeys(reasons))


def _validate_runtime_recovery_output(
    *,
    agent_output: Mapping[str, Any],
    telemetry_snapshot: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    planner_tool_results: list[Mapping[str, Any]] | None = None,
    require_parameter_tool_call: bool = False,
    parameter_tool_called: bool = False,
) -> dict[str, Any]:
    blocking_reasons: list[str] = []
    selected_action = str(
        agent_output.get("selected_bounded_action") or agent_output.get("response_kind") or ""
    ).strip()
    if selected_action not in MISSIONOS_RUNTIME_RECOVERY_ACTIONS:
        blocking_reasons.append(f"unsupported_recovery_action:{selected_action or '<missing>'}")

    trigger_level = str(agent_output.get("trigger_level") or "").strip()
    if trigger_level not in {"none", "advisory", "immediate"}:
        blocking_reasons.append("trigger_level_not_supported")

    preauthorized_actions = recovery_policy.get("preauthorized_actions")
    if isinstance(preauthorized_actions, str):
        preauthorized = {preauthorized_actions}
    else:
        preauthorized = {str(item) for item in (preauthorized_actions or ()) if str(item).strip()}
    observed_reasons = _telemetry_risk_reasons(telemetry_snapshot, recovery_policy)
    high_impact = selected_action in {
        "return_to_launch",
        "land",
        "adjust_altitude",
        "adjust_speed",
        "reroute",
        "avoid_obstacle",
    }
    action_preapproved = selected_action in preauthorized
    operator_approval_required = bool(agent_output.get("requires_human_approval", True))
    proposed_parameters = agent_output.get("proposed_parameters")
    proposed_parameters = (
        dict(proposed_parameters) if isinstance(proposed_parameters, Mapping) else {}
    )
    matching_tool_candidate: dict[str, Any] | None = None
    if (
        selected_action in _PARAMETERIZED_RUNTIME_RECOVERY_ACTIONS
        and require_parameter_tool_call
    ):
        if require_parameter_tool_call and not parameter_tool_called:
            blocking_reasons.append(
                "parameterized_recovery_requires_runtime_recovery_planner_tool_call"
            )
        if parameter_tool_called:
            recommended_tool_actions = _recommended_recovery_tool_actions(
                list(planner_tool_results or [])
            )
            if (
                require_parameter_tool_call
                and recommended_tool_actions
                and selected_action not in recommended_tool_actions
            ):
                blocking_reasons.append(
                    "parameterized_recovery_action_must_match_runtime_recovery_"
                    "planner_recommendation"
                )
            matching_tool_candidate = _matching_recovery_tool_candidate(
                selected_action=selected_action,
                proposed_parameters=proposed_parameters,
                planner_tool_results=list(planner_tool_results or []),
            )
            if require_parameter_tool_call and matching_tool_candidate is None:
                blocking_reasons.append(
                    "parameterized_recovery_parameters_must_match_runtime_recovery_"
                    "planner_tool_candidate"
                )
            elif matching_tool_candidate is not None:
                compiled_parameters = matching_tool_candidate.get("proposed_parameters")
                if isinstance(compiled_parameters, Mapping):
                    # The tool is the deterministic compiler for the LLM's
                    # selected intent.  Preserve all verifier-relevant flags
                    # from the matched candidate instead of trusting the model
                    # to repeat non-coordinate metadata verbatim.
                    proposed_parameters = dict(compiled_parameters)

    if selected_action == "continue" and observed_reasons:
        blocking_reasons.append("continue_not_allowed_with_active_runtime_risk")
    if (
        selected_action == "return_to_launch"
        and "battery_projected_insufficient_for_return_home" in observed_reasons
    ):
        blocking_reasons.append(
            "return_to_launch_not_allowed_when_projected_battery_insufficient_for_return_home"
        )
    if selected_action == "avoid_obstacle" and "obstacle_or_building_risk" not in observed_reasons:
        blocking_reasons.append("avoid_obstacle_requires_source_backed_obstacle_or_building_risk")
    obstacle = telemetry_snapshot.get("obstacle")
    obstacle = obstacle if isinstance(obstacle, Mapping) else {}
    conflict = obstacle.get("conflict_assessment")
    conflict = conflict if isinstance(conflict, Mapping) else {}
    terrain = telemetry_snapshot.get("terrain")
    terrain = terrain if isinstance(terrain, Mapping) else {}
    if (
        selected_action == "adjust_altitude"
        and conflict.get("local_avoidance_required") is True
        and terrain.get("terrain_clearance_below_minimum") is False
    ):
        blocking_reasons.append(
            "adjust_altitude_not_supported_for_local_route_conflict_without_"
            "altitude_clearance_evidence"
        )
    if (
        selected_action == "adjust_altitude"
        and _float_or_none(
            proposed_parameters.get("target_altitude_m")
            if "target_altitude_m" in proposed_parameters
            else proposed_parameters.get("altitude_m")
        )
        is None
    ):
        blocking_reasons.append("adjust_altitude_requires_target_altitude_m")
    if (
        selected_action == "adjust_speed"
        and _float_or_none(
            proposed_parameters.get("target_speed_mps")
            if "target_speed_mps" in proposed_parameters
            else proposed_parameters.get("speed_mps")
        )
        is None
    ):
        blocking_reasons.append("adjust_speed_requires_target_speed_mps")
    if selected_action in {"reroute", "avoid_obstacle"} and (
        _first_float(proposed_parameters.get("target_x_m"), proposed_parameters.get("x_m")) is None
        or _first_float(
            proposed_parameters.get("target_y_m"),
            proposed_parameters.get("y_m"),
        )
        is None
    ):
        blocking_reasons.append(f"{selected_action}_requires_target_x_m_and_target_y_m")
    if high_impact and not (action_preapproved or operator_approval_required):
        blocking_reasons.append("high_impact_recovery_requires_preapproval_or_human_review")

    recovery_intent = build_runtime_recovery_intent(agent_output=agent_output)
    intent_compilation = compile_runtime_recovery_intent(
        intent=recovery_intent,
        candidate=matching_tool_candidate or {},
        recovery_policy=recovery_policy,
    )
    reachability_verification = verify_runtime_recovery_reachability(
        compilation=intent_compilation,
        telemetry_snapshot=telemetry_snapshot,
        recovery_policy=recovery_policy,
    )
    hazard_state = build_runtime_recovery_hazard_state(
        telemetry_snapshot=telemetry_snapshot,
        recovery_policy=recovery_policy,
        observed_at=str(telemetry_snapshot.get("observed_at") or ""),
    )
    selected_action_feasibility: dict[str, Any] = {}
    if selected_action in SUPPORTED_FEASIBILITY_ACTIONS:
        selected_action_feasibility = (
            verify_runtime_recovery_action_feasibility(
                candidate=matching_tool_candidate
                or {
                    "selected_bounded_action": selected_action,
                    "proposed_parameters": proposed_parameters,
                    "source_refs": ["runtime_recovery_agent_output"],
                },
                hazard_state=hazard_state,
                recovery_policy=recovery_policy,
            )
        )
        if (
            recovery_policy.get("action_feasibility_required") is True
            and selected_action_feasibility.get("feasibility_status")
            != "verified_feasible"
        ):
            blocking_reasons.extend(
                str(item)
                for item in selected_action_feasibility.get(
                    "blocking_reasons"
                )
                or []
            )
            blocking_reasons.extend(
                str(item)
                for item in selected_action_feasibility.get(
                    "unverified_reasons"
                )
                or []
            )
            blocking_reasons.append(
                "runtime_recovery_action_not_verified_feasible"
            )
    if (
        selected_action in _PARAMETERIZED_RUNTIME_RECOVERY_ACTIONS
        and require_parameter_tool_call
    ):
        if intent_compilation.get("compilation_status") != "compiled":
            blocking_reasons.extend(
                str(item)
                for item in intent_compilation.get("blocking_reasons") or []
            )
            blocking_reasons.append("parameterized_recovery_intent_not_compilable")
        if reachability_verification.get("verification_status") != "verified":
            blocking_reasons.extend(
                str(item)
                for item in reachability_verification.get("blocking_reasons") or []
            )
            blocking_reasons.append("parameterized_recovery_reachability_unverified")

    blocking_reasons = list(dict.fromkeys(blocking_reasons))

    if blocking_reasons:
        selected_action = "operator_review"
        trigger_level = "advisory"

    return {
        "schema_version": MISSIONOS_RUNTIME_RECOVERY_ASSESSMENT_SCHEMA_VERSION,
        "assessment_status": ("blocked" if blocking_reasons else "proposal_guardrail_passed"),
        "selected_bounded_action": selected_action,
        "proposed_parameters": proposed_parameters,
        "trigger_level": trigger_level or "advisory",
        "agent_trigger_reasons": list(agent_output.get("trigger_reasons") or []),
        "observed_risk_reasons": observed_reasons,
        "blocking_reasons": blocking_reasons,
        "recovery_planner_tool_called": bool(parameter_tool_called),
        "recovery_planner_tool_candidate": dict(matching_tool_candidate or {}),
        "recovery_intent": recovery_intent,
        "intent_compilation": intent_compilation,
        "reachability_verification": reachability_verification,
        "hazard_state": hazard_state,
        "action_feasibility": selected_action_feasibility,
        "proposed_parameters_source": (
            "runtime_recovery_planner_function_tool"
            if matching_tool_candidate is not None
            else "agent_output"
        ),
        "action_preapproved_by_policy": action_preapproved,
        "preauthorized_policy_ref": str(
            recovery_policy.get("policy_ref") or recovery_policy.get("recovery_policy_ref") or ""
        ),
        "backend_action_request_allowed": False,
        "dispatch_authority_created": False,
        "operator_approval_required": operator_approval_required,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def guard_runtime_recovery_planner_result(
    *,
    planner_result: Mapping[str, Any],
    telemetry_snapshot: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    agent_intent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply the runtime-recovery guardrail to a deterministic planner result.

    The ADK Runtime Recovery Agent path validates its final proposal against the
    FunctionTool candidate.  Task-based proposal endpoints may call the same
    deterministic planner without ADK credentials, so they still need the same
    recovery guardrail before exposing a candidate to the operator.
    """

    guarded = dict(planner_result)
    candidate_value = guarded.get("recommended_candidate")
    candidate = candidate_value if isinstance(candidate_value, Mapping) else {}
    if not candidate:
        guarded.setdefault("guardrail_status", "skipped_no_candidate")
        guarded.setdefault("recovery_guardrail_assessment", {})
        return guarded

    selected_action = str(candidate.get("selected_bounded_action") or "").strip()
    proposed_parameters = candidate.get("proposed_parameters")
    proposed_parameters = (
        dict(proposed_parameters) if isinstance(proposed_parameters, Mapping) else {}
    )
    parameterized = selected_action in _PARAMETERIZED_RUNTIME_RECOVERY_ACTIONS
    intent_fields = dict(agent_intent or {})
    assessment = _validate_runtime_recovery_output(
        agent_output={
            **intent_fields,
            "selected_bounded_action": selected_action,
            "trigger_level": "advisory",
            "requires_human_approval": True,
            "proposed_parameters": proposed_parameters,
            "trigger_reasons": ["runtime_recovery_planner_candidate"],
        },
        telemetry_snapshot=telemetry_snapshot,
        recovery_policy=recovery_policy,
        planner_tool_results=[guarded],
        require_parameter_tool_call=parameterized,
        parameter_tool_called=parameterized,
    )
    guarded["recovery_guardrail_assessment"] = assessment
    guarded["requested_recovery_intent"] = {
        key: value
        for key, value in intent_fields.items()
        if key in {"strategy", "intent_constraints", "rationale"}
    }
    guarded["guardrail_status"] = assessment["assessment_status"]
    if assessment["assessment_status"] != "proposal_guardrail_passed":
        guarded["unguarded_recommended_candidate"] = dict(candidate)
        guarded["recommended_candidate"] = {
            "selected_bounded_action": "operator_review",
            "proposed_parameters": {},
            "source_refs": ["runtime_recovery_planner_guardrail"],
            "basis": {
                "blocking_reasons": list(assessment.get("blocking_reasons") or []),
            },
            "rationale": (
                "planner candidate did not pass the shared runtime recovery "
                "guardrail; operator review is required"
            ),
        }
        guarded["tool_status"] = "guardrail_blocked"
        candidate_actions = [
            str(item) for item in guarded.get("candidate_actions") or [] if str(item)
        ]
        if "operator_review" not in candidate_actions:
            candidate_actions.append("operator_review")
        guarded["candidate_actions"] = candidate_actions
    return guarded


def run_missionos_runtime_recovery_agent(
    *,
    telemetry_snapshot: Mapping[str, Any],
    mission_context: Mapping[str, Any] | None = None,
    recovery_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if os.environ.get(MISSIONOS_AGENT_RUNTIME_ADK_ENABLED_ENV, "").strip() != "1":
        return {
            "schema_version": MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION,
            "runtime_status": "not_configured",
            "blocking_reasons": [f"{MISSIONOS_AGENT_RUNTIME_ADK_ENABLED_ENV}_not_enabled"],
            "assessment": {},
            "agent_invocations": [],
            "progress_counted": False,
        }
    if not _adk_llm_credentials_available("missionos_runtime_recovery_agent"):
        return {
            "schema_version": MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION,
            "runtime_status": "not_configured",
            "blocking_reasons": [
                _llm_credentials_blocking_reason("missionos_runtime_recovery_agent")
            ],
            "assessment": {},
            "agent_invocations": [],
            "progress_counted": False,
        }

    policy = dict(recovery_policy or {})
    prompt_payload = _runtime_recovery_prompt_payload(
        telemetry_snapshot=telemetry_snapshot,
        mission_context=mission_context,
        recovery_policy=policy,
    )
    invocation = _run_runtime_recovery_agent_once(
        prompt_payload=prompt_payload,
        telemetry_snapshot=telemetry_snapshot,
        mission_context=dict(mission_context or {}),
        recovery_policy=policy,
    )
    guardrail = (
        invocation.get("guardrail_result")
        if isinstance(invocation.get("guardrail_result"), Mapping)
        else {}
    )
    if guardrail.get("guardrail_passed") is not True:
        return {
            "schema_version": MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION,
            "runtime_status": "guardrail_blocked",
            "blocking_reasons": list(guardrail.get("blocking_reasons") or []),
            "assessment": {},
            "agent_invocations": [invocation],
            "progress_counted": False,
        }

    agent_output = (
        invocation.get("validated_output")
        if isinstance(invocation.get("validated_output"), Mapping)
        else {}
    )
    planner_tool_results = [
        dict(item)
        for item in invocation.get("function_tool_results", [])
        if isinstance(item, Mapping)
    ]
    assessment = _validate_runtime_recovery_output(
        agent_output=agent_output,
        telemetry_snapshot=telemetry_snapshot,
        recovery_policy=policy,
        planner_tool_results=planner_tool_results,
        require_parameter_tool_call=True,
        parameter_tool_called=bool(invocation.get("function_tool_called")),
    )
    return {
        "schema_version": MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION,
        "runtime_status": assessment["assessment_status"],
        "blocking_reasons": list(assessment.get("blocking_reasons") or []),
        "assessment": assessment,
        "agent_output": dict(agent_output),
        "agent_invocations": [invocation],
        "progress_counted": False,
    }


def run_missionos_runtime_recovery_agent_pipeline(
    *,
    telemetry_snapshot: Mapping[str, Any],
    mission_context: Mapping[str, Any] | None = None,
    recovery_policy: Mapping[str, Any] | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run one visible Chief -> Recovery -> Critic judgment epoch.

    This is an intelligence pipeline only.  It creates no approval, dispatch
    authority, executor ACK, observed effect, verifier result, or progress
    claim.  The existing runtime supervisor remains responsible for binding a
    separately approved proposal to executor and observation records.
    """

    context = dict(mission_context or {})
    policy = dict(recovery_policy or {})
    if os.environ.get(MISSIONOS_AGENT_RUNTIME_ADK_ENABLED_ENV, "").strip() != "1":
        return {
            "schema_version": MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION,
            "runtime_status": "not_configured",
            "blocking_reasons": [f"{MISSIONOS_AGENT_RUNTIME_ADK_ENABLED_ENV}_not_enabled"],
            "assessment": {},
            "agent_invocations": [],
            "agent_pipeline": {
                "schema_version": "missionos_agent_judgment_pipeline.v1",
                "pipeline_status": "not_configured",
                "decision_source": "unavailable",
                "task_id": str(context.get("task_id") or ""),
                "stages": [],
            },
            "progress_counted": False,
        }
    for agent_name in (
        "missionos_chief_agent",
        "missionos_runtime_recovery_agent",
        MISSIONOS_SAFETY_CRITIC_AGENT_NAME,
    ):
        if not _adk_llm_credentials_available(agent_name):
            return {
                "schema_version": MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION,
                "runtime_status": "not_configured",
                "blocking_reasons": [_llm_credentials_blocking_reason(agent_name)],
                "assessment": {},
                "agent_invocations": [],
                "agent_pipeline": {
                    "schema_version": "missionos_agent_judgment_pipeline.v1",
                    "pipeline_status": "not_configured",
                    "decision_source": "unavailable",
                    "task_id": str(context.get("task_id") or ""),
                    "stages": [],
                },
                "progress_counted": False,
            }

    utterance = (
        "Assess the current runtime telemetry and select the bounded runtime "
        "recovery specialist. This is a proposal-only judgment epoch."
    )
    missionos_state = {
        "task_id": str(context.get("task_id") or ""),
        "mission_phase": str(context.get("mission_phase") or "live_runtime"),
        "authority_status": "proposal_only",
        "telemetry_snapshot": dict(telemetry_snapshot),
        "recovery_policy": policy,
    }
    invocations: list[dict[str, Any]] = []
    chief_invocation = _run_agent_once(
        agent_name="missionos_chief_agent",
        agent_role="MissionOS chief coordinator agent",
        prompt_payload=_root_prompt_payload(
            utterance=utterance,
            missionos_state=missionos_state,
            mission_designer_context=None,
            coordinate_route=None,
            conversation_history=None,
            route_hint="runtime_recovery",
        ),
        timeout_seconds=timeout_seconds,
    )
    invocations.append(chief_invocation)
    chief_guardrail = chief_invocation.get("guardrail_result")
    chief_guardrail = chief_guardrail if isinstance(chief_guardrail, Mapping) else {}
    chief_output = chief_invocation.get("validated_output")
    chief_output = dict(chief_output) if isinstance(chief_output, Mapping) else {}
    if chief_guardrail.get("guardrail_passed") is not True:
        return _blocked_runtime_recovery_pipeline_result(
            context=context,
            invocations=invocations,
            blocking_reasons=list(chief_guardrail.get("blocking_reasons") or []),
            failed_stage="chief",
        )
    if chief_output.get("intent") != "runtime_recovery":
        return _blocked_runtime_recovery_pipeline_result(
            context=context,
            invocations=invocations,
            blocking_reasons=[
                "chief_intent_not_runtime_recovery:"
                + str(chief_output.get("intent") or "missing")
            ],
            failed_stage="chief",
        )

    recovery_prompt = _runtime_recovery_prompt_payload(
        telemetry_snapshot=telemetry_snapshot,
        mission_context=context,
        recovery_policy=policy,
    )
    specialist_invocation = _run_runtime_recovery_agent_once(
        prompt_payload={
            **recovery_prompt,
            "chief_agent_output": chief_output,
        },
        telemetry_snapshot=telemetry_snapshot,
        mission_context=context,
        recovery_policy=policy,
        timeout_seconds=timeout_seconds,
    )
    invocations.append(specialist_invocation)
    specialist_guardrail = specialist_invocation.get("guardrail_result")
    specialist_guardrail = (
        specialist_guardrail if isinstance(specialist_guardrail, Mapping) else {}
    )
    if specialist_guardrail.get("guardrail_passed") is not True:
        return _blocked_runtime_recovery_pipeline_result(
            context=context,
            invocations=invocations,
            blocking_reasons=list(specialist_guardrail.get("blocking_reasons") or []),
            failed_stage="specialist",
        )
    specialist_output = specialist_invocation.get("validated_output")
    specialist_output = (
        dict(specialist_output) if isinstance(specialist_output, Mapping) else {}
    )
    planner_tool_results = [
        dict(item)
        for item in specialist_invocation.get("function_tool_results", [])
        if isinstance(item, Mapping)
    ]
    assessment = _validate_runtime_recovery_output(
        agent_output=specialist_output,
        telemetry_snapshot=telemetry_snapshot,
        recovery_policy=policy,
        planner_tool_results=planner_tool_results,
        require_parameter_tool_call=True,
        parameter_tool_called=bool(specialist_invocation.get("function_tool_called")),
    )
    if assessment.get("assessment_status") != "proposal_guardrail_passed":
        return _blocked_runtime_recovery_pipeline_result(
            context=context,
            invocations=invocations,
            blocking_reasons=list(assessment.get("blocking_reasons") or []),
            failed_stage="specialist_guardrail",
        )

    critic_invocation = _run_agent_once(
        agent_name=MISSIONOS_SAFETY_CRITIC_AGENT_NAME,
        agent_role="MissionOS safety and boundary critic agent",
        validate_intent=False,
        prompt_payload=_safety_critic_prompt_payload(
            utterance=utterance,
            chief_output=chief_output,
            specialist_name="missionos_runtime_recovery_agent",
            specialist_output=specialist_output,
            missionos_state=missionos_state,
            mission_designer_context=None,
            coordinate_route=None,
            route_hint="runtime_recovery",
        ),
        timeout_seconds=timeout_seconds,
    )
    invocations.append(critic_invocation)
    critic_guardrail = critic_invocation.get("guardrail_result")
    critic_guardrail = critic_guardrail if isinstance(critic_guardrail, Mapping) else {}
    critic_output = critic_invocation.get("validated_output")
    critic_output = dict(critic_output) if isinstance(critic_output, Mapping) else {}
    if critic_guardrail.get("guardrail_passed") is not True:
        return _blocked_runtime_recovery_pipeline_result(
            context=context,
            invocations=invocations,
            blocking_reasons=list(critic_guardrail.get("blocking_reasons") or []),
            failed_stage="safety_critic",
        )
    boundary_status = str(critic_output.get("boundary_status") or "")
    if boundary_status not in MISSIONOS_SAFETY_CRITIC_PASS_STATUSES:
        return _blocked_runtime_recovery_pipeline_result(
            context=context,
            invocations=invocations,
            blocking_reasons=[
                "safety_critic_boundary_status:" + (boundary_status or "missing")
            ],
            failed_stage="safety_critic",
        )

    return {
        "schema_version": MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION,
        "runtime_status": "proposal_guardrail_passed",
        "blocking_reasons": [],
        "assessment": assessment,
        "agent_output": specialist_output,
        "agent_invocations": invocations,
        "agent_pipeline": {
            "schema_version": "missionos_agent_judgment_pipeline.v1",
            "pipeline_status": "proposal_guardrail_passed",
            "decision_source": "llm",
            "task_id": str(context.get("task_id") or ""),
            "chief_agent_output": chief_output,
            "specialist_agent": "missionos_runtime_recovery_agent",
            "specialist_agent_output": specialist_output,
            "safety_critic_agent_output": critic_output,
            "requires_human_approval": True,
            "dispatch_authority_created": False,
            "executor_ack_observed": "unknown",
            "effect_observed": "unknown",
            "verifier_status": "not_started",
            "stages": ["chief", "specialist", "safety_critic"],
        },
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _blocked_runtime_recovery_pipeline_result(
    *,
    context: Mapping[str, Any],
    invocations: list[dict[str, Any]],
    blocking_reasons: list[str],
    failed_stage: str,
) -> dict[str, Any]:
    reasons = [str(reason) for reason in blocking_reasons if str(reason)]
    if not reasons:
        reasons = [f"{failed_stage}_guardrail_blocked"]
    return {
        "schema_version": MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION,
        "runtime_status": "guardrail_blocked",
        "blocking_reasons": reasons,
        "assessment": {},
        "agent_invocations": invocations,
        "agent_pipeline": {
            "schema_version": "missionos_agent_judgment_pipeline.v1",
            "pipeline_status": "guardrail_blocked",
            "decision_source": "unavailable",
            "task_id": str(context.get("task_id") or ""),
            "failed_stage": failed_stage,
            "blocking_reasons": reasons,
            "stages": [
                str(item.get("agent_name") or "")
                for item in invocations
                if isinstance(item, Mapping)
            ],
        },
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _missionos_agent_runtime_configuration_failure(
    *,
    monitoring_payloads: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if os.environ.get(MISSIONOS_AGENT_RUNTIME_ADK_ENABLED_ENV, "").strip() != "1":
        return {
            "schema_version": MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION,
            "runtime_status": "not_configured",
            "blocking_reasons": [f"{MISSIONOS_AGENT_RUNTIME_ADK_ENABLED_ENV}_not_enabled"],
            "proposal": {},
            "agent_invocations": [],
            "monitoring_observations": monitoring_payloads,
            "progress_counted": False,
        }
    if not _adk_llm_credentials_available("missionos_chief_agent"):
        return {
            "schema_version": MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION,
            "runtime_status": "not_configured",
            "blocking_reasons": [
                _llm_credentials_blocking_reason("missionos_chief_agent")
            ],
            "proposal": {},
            "agent_invocations": [],
            "monitoring_observations": monitoring_payloads,
            "progress_counted": False,
        }
    return None


def _run_missionos_agent_runtime_sequential(
    *,
    utterance: str,
    missionos_state: Mapping[str, Any],
    mission_designer_context: Mapping[str, Any] | None = None,
    coordinate_route: Mapping[str, Any] | None = None,
    conversation_history: list[dict[str, str]] | None = None,
    monitoring_observations: list[Mapping[str, Any]] | None = None,
    route_hint: str = "",
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    monitoring_payloads = _monitoring_observation_payloads(monitoring_observations)
    configuration_failure = _missionos_agent_runtime_configuration_failure(
        monitoring_payloads=monitoring_payloads,
    )
    if configuration_failure is not None:
        return configuration_failure

    invocations: list[dict[str, Any]] = []
    chief_invocation = _run_agent_once(
        agent_name="missionos_chief_agent",
        agent_role="MissionOS chief coordinator agent",
        prompt_payload=_root_prompt_payload(
            utterance=utterance,
            missionos_state=missionos_state,
            mission_designer_context=mission_designer_context,
            coordinate_route=coordinate_route,
            conversation_history=conversation_history,
            monitoring_observations=monitoring_payloads,
            route_hint=route_hint,
        ),
        timeout_seconds=timeout_seconds,
    )
    invocations.append(chief_invocation)
    chief_guardrail_value = chief_invocation.get("guardrail_result")
    chief_guardrail = chief_guardrail_value if isinstance(chief_guardrail_value, Mapping) else {}
    if chief_guardrail.get("guardrail_passed") is not True:
        return {
            "schema_version": MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION,
            "runtime_status": "guardrail_blocked",
            "blocking_reasons": list(chief_guardrail.get("blocking_reasons") or []),
            "proposal": {},
            "agent_invocations": invocations,
            "monitoring_observations": monitoring_payloads,
            "progress_counted": False,
        }

    chief_output_value = chief_invocation.get("validated_output")
    chief_output = chief_output_value if isinstance(chief_output_value, Mapping) else {}
    intent = str(chief_output.get("intent") or "plan")
    specialist_name = _CHIEF_TO_SPECIALIST.get(intent)
    specialist_output: dict[str, Any] = {}
    safety_critic_output: dict[str, Any] = {}
    if specialist_name:
        specialist_invocation = _run_agent_once(
            agent_name=specialist_name,
            agent_role=specialist_name.replace("_", " "),
            # Specialist agent's intent is a result label, not a routing decision.
            # Routing was already fixed by the Chief agent.  Only forbidden-key
            # and type-safety checks apply; do not validate against the routing
            # allowed-intent set.
            validate_intent=False,
            prompt_payload=_specialist_prompt_payload(
                utterance=utterance,
                root_output=chief_output,
                missionos_state=missionos_state,
                mission_designer_context=mission_designer_context,
                coordinate_route=coordinate_route,
                conversation_history=conversation_history,
                monitoring_observations=monitoring_payloads,
                route_hint=route_hint,
            ),
            timeout_seconds=timeout_seconds,
        )
        invocations.append(specialist_invocation)
        specialist_guardrail = (
            specialist_invocation.get("guardrail_result")
            if isinstance(specialist_invocation.get("guardrail_result"), Mapping)
            else {}
        )
        if specialist_guardrail.get("guardrail_passed") is not True:
            return {
                "schema_version": MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION,
                "runtime_status": "guardrail_blocked",
                "blocking_reasons": list(specialist_guardrail.get("blocking_reasons") or []),
                "proposal": {},
                "agent_invocations": invocations,
                "monitoring_observations": monitoring_payloads,
                "progress_counted": False,
            }
        specialist_output = (
            specialist_invocation.get("validated_output")
            if isinstance(specialist_invocation.get("validated_output"), Mapping)
            else {}
        )
    safety_critic_invocation = _run_agent_once(
        agent_name=MISSIONOS_SAFETY_CRITIC_AGENT_NAME,
        agent_role="MissionOS safety and boundary critic agent",
        validate_intent=False,
        prompt_payload=_safety_critic_prompt_payload(
            utterance=utterance,
            chief_output=chief_output,
            specialist_name=specialist_name or "",
            specialist_output=specialist_output,
            missionos_state=missionos_state,
            mission_designer_context=mission_designer_context,
            coordinate_route=coordinate_route,
            monitoring_observations=monitoring_payloads,
            route_hint=route_hint,
        ),
        timeout_seconds=timeout_seconds,
    )
    invocations.append(safety_critic_invocation)
    safety_critic_guardrail = (
        safety_critic_invocation.get("guardrail_result")
        if isinstance(safety_critic_invocation.get("guardrail_result"), Mapping)
        else {}
    )
    if safety_critic_guardrail.get("guardrail_passed") is not True:
        return {
            "schema_version": MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION,
            "runtime_status": "guardrail_blocked",
            "blocking_reasons": list(safety_critic_guardrail.get("blocking_reasons") or []),
            "proposal": {},
            "agent_invocations": invocations,
            "monitoring_observations": monitoring_payloads,
            "progress_counted": False,
        }
    safety_critic_output = (
        safety_critic_invocation.get("validated_output")
        if isinstance(safety_critic_invocation.get("validated_output"), Mapping)
        else {}
    )
    boundary_status = str(safety_critic_output.get("boundary_status") or "").strip()
    if not boundary_status:
        return {
            "schema_version": MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION,
            "runtime_status": "guardrail_blocked",
            "blocking_reasons": ["safety_critic_boundary_status_missing"],
            "proposal": {},
            "agent_invocations": invocations,
            "monitoring_observations": monitoring_payloads,
            "progress_counted": False,
        }
    if boundary_status not in MISSIONOS_SAFETY_CRITIC_RECOGNIZED_STATUSES:
        return {
            "schema_version": MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION,
            "runtime_status": "guardrail_blocked",
            "blocking_reasons": [f"safety_critic_boundary_status:{boundary_status}"],
            "proposal": {},
            "agent_invocations": invocations,
            "monitoring_observations": monitoring_payloads,
            "progress_counted": False,
        }
    if boundary_status not in MISSIONOS_SAFETY_CRITIC_PASS_STATUSES:
        return {
            "schema_version": MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION,
            "runtime_status": "guardrail_blocked",
            "blocking_reasons": [f"safety_critic_boundary_status:{boundary_status}"],
            "proposal": {},
            "agent_invocations": invocations,
            "monitoring_observations": monitoring_payloads,
            "progress_counted": False,
        }

    proposal = {
        # intent is the Chief agent's routing decision, not the specialist's result
        # label.  The specialist may output a finer-grained intent (e.g. "replan",
        # "hold") that is useful as metadata but must not override the routing intent
        # that server.py uses to select the next gateway action.
        "intent": str(chief_output.get("intent") or "plan"),
        "specialist_intent": str(specialist_output.get("intent") or ""),
        "operator_instruction": str(
            specialist_output.get("operator_instruction")
            or chief_output.get("operator_instruction")
            or utterance
        )[:2000],
        "specialist_agent": specialist_name or str(chief_output.get("specialist_agent") or ""),
        "chief_agent_output": dict(chief_output),
        # Keep the legacy key for downstream callers that still expect the old
        # root-agent shape while the public conversation entrypoint moves to
        # the Chief/coordinator pattern.
        "root_agent_output": dict(chief_output),
        "specialist_agent_output": dict(specialist_output),
        "safety_critic_agent": (MISSIONOS_SAFETY_CRITIC_AGENT_NAME if safety_critic_output else ""),
        "safety_critic_output": dict(safety_critic_output),
        "operator_facing_route": MISSIONOS_OPERATOR_FACING_ROUTE,
        "internal_capability_registry": build_missionos_capability_registry_summary(),
        "coordination_pattern": ("chief_intent_router_with_specialist_pipeline_and_safety_critic"),
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
        "schema_version": MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION,
        "runtime_status": "proposal_guardrail_passed",
        "blocking_reasons": [],
        "proposal": proposal,
        "agent_invocations": invocations,
        "operator_facing_route": MISSIONOS_OPERATOR_FACING_ROUTE,
        "internal_capability_registry": build_missionos_capability_registry_summary(),
        "coordination_pattern": ("chief_intent_router_with_specialist_pipeline_and_safety_critic"),
        "monitoring_observations": monitoring_payloads,
        "progress_counted": False,
    }


def _missionos_adk_v2_primary_runtime_error(
    exc: BaseException,
    *,
    monitoring_payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION,
        "runtime_status": "guardrail_blocked",
        "blocking_reasons": [f"adk_v2_primary_graph_failed:{type(exc).__name__}"],
        "proposal": {},
        "agent_invocations": [],
        "monitoring_observations": monitoring_payloads,
        "workflow_execution_mode": "adk_v2_graph_primary",
        "adk_v2_graph_primary": True,
        "adk_v2_graph_result": {},
        "proposal_only": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "executor_invoked": False,
        "physical_execution_invoked": False,
        "outcome_observed": False,
        "progress_counted": False,
    }


def _missionos_adk_v2_primary_runtime_result(
    graph_result: Mapping[str, Any],
    *,
    monitoring_payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    graph_status = str(graph_result.get("graph_runtime_status") or "")
    if graph_status not in {"proposal_guardrail_passed", "guardrail_blocked"}:
        graph_status = "guardrail_blocked"
        blocking_reasons = ["adk_v2_primary_graph_runtime_status_invalid"]
        proposal: dict[str, Any] = {}
    else:
        blocking_reasons = list(graph_result.get("blocking_reasons") or [])
        proposal_value = graph_result.get("proposal")
        proposal = dict(proposal_value) if isinstance(proposal_value, Mapping) else {}
        if graph_status != "proposal_guardrail_passed":
            proposal = {}
    invocations = [
        dict(item)
        for item in graph_result.get("agent_invocations") or []
        if isinstance(item, Mapping)
    ]
    return {
        "schema_version": MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION,
        "runtime_status": graph_status,
        "blocking_reasons": blocking_reasons,
        "proposal": proposal,
        "agent_invocations": invocations,
        "operator_facing_route": MISSIONOS_OPERATOR_FACING_ROUTE,
        "internal_capability_registry": build_missionos_capability_registry_summary(),
        "coordination_pattern": "adk_v2_graph_primary_chief_specialist_safety_critic",
        "monitoring_observations": monitoring_payloads,
        "workflow_execution_mode": "adk_v2_graph_primary",
        "adk_v2_graph_primary": True,
        "adk_v2_graph_result": dict(graph_result),
        "proposal_only": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "executor_invoked": False,
        "physical_execution_invoked": False,
        "outcome_observed": False,
        "progress_counted": False,
    }


def _run_missionos_agent_runtime_adk_v2_primary(
    *,
    utterance: str,
    missionos_state: Mapping[str, Any],
    mission_designer_context: Mapping[str, Any] | None = None,
    coordinate_route: Mapping[str, Any] | None = None,
    conversation_history: list[dict[str, str]] | None = None,
    monitoring_observations: list[Mapping[str, Any]] | None = None,
    route_hint: str = "",
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    monitoring_payloads = _monitoring_observation_payloads(monitoring_observations)
    configuration_failure = _missionos_agent_runtime_configuration_failure(
        monitoring_payloads=monitoring_payloads,
    )
    if configuration_failure is not None:
        return configuration_failure

    from src.intelligence.missionos_adk_v2_shadow_graph import (
        run_missionos_conversation_proposal_graph,
    )

    try:
        graph_result = run_missionos_conversation_proposal_graph(
            utterance=utterance,
            missionos_state=missionos_state,
            mission_designer_context=mission_designer_context,
            coordinate_route=coordinate_route,
            conversation_history=conversation_history,
            monitoring_observations=monitoring_observations,
            route_hint=route_hint,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:  # pragma: no cover - live model/runtime failures vary.
        return _missionos_adk_v2_primary_runtime_error(
            exc,
            monitoring_payloads=monitoring_payloads,
        )
    return _missionos_adk_v2_primary_runtime_result(
        graph_result,
        monitoring_payloads=monitoring_payloads,
    )


def run_missionos_agent_runtime(
    *,
    utterance: str,
    missionos_state: Mapping[str, Any],
    mission_designer_context: Mapping[str, Any] | None = None,
    coordinate_route: Mapping[str, Any] | None = None,
    conversation_history: list[dict[str, str]] | None = None,
    monitoring_observations: list[Mapping[str, Any]] | None = None,
    route_hint: str = "",
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run the configured MissionOS conversation proposal path.

    The proposal-only ADK v2 graph is the default engine. Setting
    ``MISSIONOS_ADK_V2_GRAPH_PRIMARY=0`` explicitly retains the sequential
    engine for bounded shadow comparisons only.
    ``MISSIONOS_ADK_V2_GRAPH_ROLLBACK=1`` always restores the sequential path
    and suppresses both primary and shadow graph execution.
    """

    call_kwargs = {
        "utterance": utterance,
        "missionos_state": missionos_state,
        "mission_designer_context": mission_designer_context,
        "coordinate_route": coordinate_route,
        "conversation_history": conversation_history,
        "monitoring_observations": monitoring_observations,
        "route_hint": route_hint,
        "timeout_seconds": timeout_seconds,
    }
    rollout = validate_adk_v2_graph_rollout_env()
    if rollout["rollback"]:
        rollback_result = _run_missionos_agent_runtime_sequential(**call_kwargs)
        rollback_evidence = dict(rollback_result)
        rollback_evidence["workflow_execution_mode"] = "sequential_rollback"
        rollback_evidence["adk_v2_graph_primary"] = False
        rollback_evidence["adk_v2_graph_rollback"] = True
        return rollback_evidence

    if rollout["primary"]:
        return _run_missionos_agent_runtime_adk_v2_primary(**call_kwargs)

    primary_result = _run_missionos_agent_runtime_sequential(**call_kwargs)
    if not rollout["shadow"]:
        return primary_result
    if primary_result.get("runtime_status") == "not_configured":
        return primary_result

    from src.intelligence.missionos_adk_v2_shadow_graph import (
        build_missionos_conversation_shadow_comparison,
        build_shadow_runtime_error_comparison,
        run_missionos_conversation_shadow_graph,
    )

    shadow_input = {
        "utterance": utterance,
        "missionos_state": dict(missionos_state),
        "mission_designer_context": dict(mission_designer_context or {}),
        "coordinate_route": dict(coordinate_route or {}),
        "conversation_history": list(conversation_history or [])[-10:],
        "monitoring_observations": [
            dict(item)
            for item in (monitoring_observations or [])
            if isinstance(item, Mapping)
        ],
        "route_hint": route_hint,
    }
    enriched_result = dict(primary_result)
    try:
        shadow_result = run_missionos_conversation_shadow_graph(
            utterance=utterance,
            missionos_state=missionos_state,
            mission_designer_context=mission_designer_context,
            coordinate_route=coordinate_route,
            conversation_history=conversation_history,
            monitoring_observations=monitoring_observations,
            route_hint=route_hint,
            timeout_seconds=timeout_seconds,
        )
        enriched_result["adk_v2_shadow_result"] = shadow_result
        enriched_result["adk_v2_shadow_comparison"] = (
            build_missionos_conversation_shadow_comparison(
                primary_result=primary_result,
                shadow_result=shadow_result,
                input_payload=shadow_input,
            )
        )
    except Exception as exc:  # pragma: no cover - live model/runtime failures vary.
        enriched_result["adk_v2_shadow_result"] = {}
        enriched_result["adk_v2_shadow_comparison"] = (
            build_shadow_runtime_error_comparison(exc)
        )
    return enriched_result


__all__ = [
    "MISSIONOS_ADK_V2_GRAPH_PRIMARY_ENV",
    "MISSIONOS_ADK_V2_GRAPH_ROLLBACK_ENV",
    "MISSIONOS_ADK_V2_GRAPH_SHADOW_ENV",
    "MISSIONOS_AGENT_RUNTIME_ADK_ENABLED_ENV",
    "MISSIONOS_AGENT_RUNTIME_MODEL_ENV",
    "MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION",
    "MISSIONOS_OPERATOR_FACING_ROUTE",
    "MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION",
    "guard_missionos_agent_output",
    "guard_runtime_recovery_planner_result",
    "run_missionos_agent_runtime",
    "run_missionos_runtime_recovery_agent",
    "run_missionos_runtime_recovery_agent_pipeline",
]
