"""MissionOS ADK agent runtime.

Gateway owns HTTP/session/artifact/approval/dispatch boundaries.  This module
owns the MissionOS intelligence layer: actual ADK agent invocations plus
deterministic guardrails over their JSON outputs.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping

from src.agents.model_config import (
    agent_model_label,
    google_llm_backend_enabled,
    llm_provider_label,
    local_llm_backend_enabled,
)
from src.gateway.missionos_capabilities import (
    MISSIONOS_OPERATOR_FACING_ROUTE,
    all_capability_descriptors_for_prompt,
    build_missionos_capability_registry_summary,
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

MISSIONOS_AGENT_ALLOWED_INTENTS = frozenset({
    "status",
    "approve",
    "reject",
    "revision",
    "execute",
    "repair",
    "plan",
    "mission_designer_plan",
    "runtime_recovery",
})

MISSIONOS_AGENT_FORBIDDEN_KEYS = frozenset({
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
})

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
MISSIONOS_SAFETY_CRITIC_PASS_STATUSES = frozenset({
    "safe",
    "needs_human_approval",
    "operator_review_required",
})
MISSIONOS_SAFETY_CRITIC_RECOGNIZED_STATUSES = (
    MISSIONOS_SAFETY_CRITIC_PASS_STATUSES | frozenset({"blocked"})
)

MISSIONOS_MONITORING_OBSERVATION_SEVERITIES = frozenset({
    "info",
    "advisory",
    "warning",
    "critical",
})

MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION = (
    "missionos_runtime_recovery_agent_result.v1"
)
MISSIONOS_RUNTIME_RECOVERY_ASSESSMENT_SCHEMA_VERSION = (
    "missionos_runtime_recovery_assessment.v1"
)
MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_SCHEMA_VERSION = (
    "missionos_runtime_recovery_planner_tool_result.v1"
)
MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_NAME = (
    "missionos_plan_bounded_recovery_maneuver"
)
_PARAMETERIZED_RUNTIME_RECOVERY_ACTIONS = frozenset({
    "adjust_altitude",
    "reroute",
    "avoid_obstacle",
})

MISSIONOS_RUNTIME_RECOVERY_ACTIONS = frozenset({
    "continue",
    "hold",
    "return_to_launch",
    "land",
    "adjust_altitude",
    "adjust_speed",
    "reroute",
    "avoid_obstacle",
    "operator_review",
})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


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
        payloads.append({
            "schema_version": MISSIONOS_MONITORING_OBSERVATION_SCHEMA_VERSION,
            "observation_id": str(
                observation.get("observation_id")
                or observation.get("id")
                or f"monitoring_observation:{index + 1}"
            )[:200],
            "source": str(observation.get("source") or "missionos_event_monitor")[:200],
            "observed_at": str(observation.get("observed_at") or "")[:100],
            "observation_type": str(
                observation.get("observation_type") or "runtime_snapshot"
            )[:200],
            "severity": severity,
            "summary": str(observation.get("summary") or "")[:2000],
            "suggested_intent": suggested_intent,
            "evidence_refs": evidence_refs,
            "authority_status": "observation_only",
            "approval_request_created": False,
            "dispatch_authority_created": False,
            "progress_counted": False,
        })
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


def _google_adk_credentials_available(agent_name: str | None = None) -> bool:
    if local_llm_backend_enabled(agent_name):
        return True
    if not google_llm_backend_enabled(agent_name):
        return False
    _configure_google_adk_environment(agent_name)
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower()
    if use_vertex in {"1", "true", "yes"}:
        return True
    return bool(os.environ.get("GOOGLE_API_KEY", "").strip())


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


def _invoke_adk_agent_text(
    *,
    agent_name: str,
    model_id: str,
    prompt_text: str,
    timeout_seconds: int,
) -> str:
    return asyncio.run(
        asyncio.wait_for(
            _invoke_adk_agent_text_async(
                agent_name=agent_name,
                model_id=model_id,
                prompt_text=prompt_text,
            ),
            timeout=timeout_seconds,
        )
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
    ) -> dict[str, Any]:
        """Compute bounded recovery proposal parameters without authority.

        Args:
            recovery_action: One of adjust_altitude, reroute, avoid_obstacle, or
                empty when asking for the best available bounded candidate.
            reason: Concise reason the Runtime Recovery Agent is considering the
                maneuver.

        Returns:
            Bounded proposal-only target_altitude_m and/or local NED target_x_m
            and target_y_m candidates. This tool never approves, dispatches,
            executes, verifies, or counts progress.
        """

        arguments = {
            "recovery_action": str(recovery_action or ""),
            "reason": str(reason or "")[:500],
        }
        result = plan_runtime_recovery_maneuver(
            telemetry_snapshot=telemetry_snapshot,
            mission_context=mission_context,
            recovery_policy=recovery_policy,
            requested_action=arguments["recovery_action"],
            request_reason=arguments["reason"],
        )
        captured["tool_arguments"].append(arguments)
        captured["tool_results"].append(dict(result))
        return dict(result)

    agent = build_missionos_runtime_recovery_agent(
        model_id=model_id,
        tools=[FunctionTool(missionos_plan_bounded_recovery_maneuver)],
    )
    app_name = "missionos_runtime_recovery_function_tool"
    user_id = "missionos_operator"
    session_service = create_session_service()
    session = await session_service.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)
    content = types.Content(role="user", parts=[types.Part(text=prompt_text)])
    response_parts: list[str] = []
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
                function_calls.append({
                    "name": str(getattr(function_call, "name", "") or ""),
                    "args": dict(getattr(function_call, "args", None) or {}),
                })
            function_response = getattr(part, "function_response", None)
            if function_response:
                function_responses.append({
                    "name": str(getattr(function_response, "name", "") or ""),
                    "response_present": bool(
                        getattr(function_response, "response", None)
                    ),
                })
    return {
        "response_text": "".join(response_parts).strip(),
        "function_calls": function_calls,
        "function_responses": function_responses,
        "function_tool_called": bool(captured["tool_results"]),
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


def _run_agent_once(
    *,
    agent_name: str,
    agent_role: str,
    prompt_payload: Mapping[str, Any],
    validate_intent: bool = True,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    model_id = _model_id(agent_name)
    prompt_text = json.dumps(dict(prompt_payload), ensure_ascii=False, sort_keys=True)
    started_at = _utc_now()
    try:
        response_text = _invoke_adk_agent_text(
            agent_name=agent_name,
            model_id=model_id,
            prompt_text=prompt_text,
            timeout_seconds=timeout_seconds or _timeout_seconds(),
        )
        invocation_error = ""
    except Exception as exc:  # pragma: no cover - live service failure shape varies.
        response_text = ""
        invocation_error = (
            f"{llm_provider_label(agent_name)}_agent_invocation_failed:"
            f"{type(exc).__name__}"
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
        "validated_output": guardrail.get("validated_output") or {},
        "guardrail_result": guardrail,
        "progress_counted": False,
        "llm_judgment_in_gate": False,
    }
    evidence["artifact_path"] = _persist_invocation_evidence(evidence)
    return evidence


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
    prompt_text = json.dumps(dict(prompt_payload), ensure_ascii=False, sort_keys=True)
    started_at = _utc_now()
    function_calls: list[dict[str, Any]] = []
    function_responses: list[dict[str, Any]] = []
    function_tool_results: list[dict[str, Any]] = []
    tool_arguments: list[dict[str, Any]] = []
    function_tool_called = False
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
        function_calls = [
            dict(item)
            for item in invocation.get("function_calls", [])
            if isinstance(item, Mapping)
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
            dict(item)
            for item in invocation.get("tool_arguments", [])
            if isinstance(item, Mapping)
        ]
        function_tool_called = bool(invocation.get("function_tool_called"))
        invocation_error = ""
    except Exception as exc:  # pragma: no cover - live service failure shape varies.
        response_text = ""
        invocation_error = (
            f"{llm_provider_label(agent_name)}_agent_invocation_failed:"
            f"{type(exc).__name__}"
        )
    completed_at = _utc_now()
    raw_output = _read_json_object(response_text) if response_text else None
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
        "invocation_kind": "google_adk_function_tool_call",
        "model_id": model_id,
        "prompt_sha256": _sha256_text(prompt_text),
        "response_sha256": _sha256_text(response_text),
        "invocation_started_at": started_at.isoformat(),
        "invocation_completed_at": completed_at.isoformat(),
        "function_tool_name": MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_NAME,
        "function_calls": function_calls,
        "function_responses": function_responses,
        "function_tool_called": function_tool_called,
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
            "coordination_pattern": (
                "chief_intent_router_with_deterministic_specialist_floor"
            ),
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
        "monitoring_observations": _monitoring_observation_payloads(
            monitoring_observations
        ),
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
        "monitoring_observations": _monitoring_observation_payloads(
            monitoring_observations
        ),
        "human_utterance": utterance[:2000],
    }


def _runtime_recovery_prompt_payload(
    *,
    telemetry_snapshot: Mapping[str, Any],
    mission_context: Mapping[str, Any] | None,
    recovery_policy: Mapping[str, Any] | None,
) -> dict[str, Any]:
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
                    "propose adjust_altitude, adjust_speed, reroute, or "
                    "avoid_obstacle only when supplied telemetry, policy, or tool "
                    "output include enough bounded parameters"
                ),
                (
                    "propose continue, hold, return_to_launch, land, "
                    "adjust_altitude, adjust_speed, reroute, avoid_obstacle, or "
                    "operator_review"
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
            "agents_must_not_output": sorted(MISSIONOS_AGENT_FORBIDDEN_KEYS),
        },
        "telemetry_snapshot": dict(telemetry_snapshot),
        "mission_context": dict(mission_context or {}),
        "recovery_policy": dict(recovery_policy or {}),
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
            to_point = _runtime_recovery_point_xy({
                "x_m": leg.get("to_x_m"),
                "local_x_m": leg.get("target_x_m"),
                "north_m": leg.get("to_north_m"),
                "n_m": leg.get("target_n_m"),
                "y_m": leg.get("to_y_m"),
                "local_y_m": leg.get("target_y_m"),
                "east_m": leg.get("to_east_m"),
                "e_m": leg.get("target_e_m"),
            })
            from_point = _runtime_recovery_point_xy({
                "x_m": leg.get("from_x_m"),
                "local_x_m": leg.get("source_x_m"),
                "north_m": leg.get("from_north_m"),
                "n_m": leg.get("source_n_m"),
                "y_m": leg.get("from_y_m"),
                "local_y_m": leg.get("source_y_m"),
                "east_m": leg.get("from_east_m"),
                "e_m": leg.get("source_e_m"),
            })
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
    requested_step_m = (
        requested_delta_m if requested_delta_m is not None else requested_climb_m
    )
    if requested_step_m is None and operator_request.get("requested_action") == "adjust_altitude":
        requested_step_m = _first_float(
            recovery_policy.get("operator_requested_altitude_step_m")
        ) or 10.0
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
            "terrain_clearance_m": round(clearance_m, 3)
            if clearance_m is not None
            else None,
            "terrain_clearance_target_m": round(target_clearance_m, 3),
            "terrain_clearance_margin_m": round(margin_m, 3)
            if margin_m is not None
            else None,
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
        _first_float(position.get("local_x_m"), telemetry_snapshot.get("local_x_m"))
        or 0.0
    )
    current_y_m = (
        _first_float(position.get("local_y_m"), telemetry_snapshot.get("local_y_m"))
        or 0.0
    )
    current_altitude_m = _first_float(
        position.get("altitude_above_home_m"),
        telemetry_snapshot.get("altitude_above_home_m"),
    ) or 0.0
    primary = obstacle_points[0]
    obstacle_x_m = float(primary["x_m"])
    obstacle_y_m = float(primary["y_m"])
    distance_m = max(
        math.hypot(obstacle_x_m - current_x_m, obstacle_y_m - current_y_m),
        1e-6,
    )
    unit_x, unit_y, route_source_ref = _runtime_recovery_route_vector(
        telemetry_snapshot=telemetry_snapshot,
        mission_context=mission_context,
        current_x_m=current_x_m,
        current_y_m=current_y_m,
        obstacle_x_m=obstacle_x_m,
        obstacle_y_m=obstacle_y_m,
    )
    # Choose a deterministic left-hand lateral offset relative to the current
    # route-to-obstacle vector. The operator still approves before execution.
    perp_x = -unit_y
    perp_y = unit_x
    obstacle_radius_m = max(
        _first_float(primary.get("size_x_m")) or 0.0,
        _first_float(primary.get("size_y_m")) or 0.0,
    ) / 2.0
    lateral_clearance_m = max(
        _first_float(recovery_policy.get("obstacle_lateral_clearance_m")) or 30.0,
        obstacle_radius_m + (_first_float(recovery_policy.get("obstacle_buffer_m")) or 20.0),
    )
    forward_m = _clamp(
        distance_m * 0.2,
        minimum=_first_float(recovery_policy.get("obstacle_min_forward_m")) or 30.0,
        maximum=_first_float(recovery_policy.get("obstacle_max_forward_m")) or 120.0,
    )
    target_x_m = current_x_m + unit_x * forward_m + perp_x * lateral_clearance_m
    target_y_m = current_y_m + unit_y * forward_m + perp_y * lateral_clearance_m
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
    target_clearance_m = _first_float(
        terrain.get("terrain_clearance_target_m"),
        terrain.get("target_clearance_m"),
        recovery_policy.get("min_terrain_clearance_m"),
    ) or 30.0
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
        },
        "source_refs": [
            str(primary.get("source_ref") or "telemetry_snapshot.obstacle"),
            "telemetry_snapshot.position",
            route_source_ref,
            "recovery_policy.max_reroute_target_abs_m",
        ],
        "basis": {
            "current_x_m": round(current_x_m, 3),
            "current_y_m": round(current_y_m, 3),
            "obstacle_x_m": round(obstacle_x_m, 3),
            "obstacle_y_m": round(obstacle_y_m, 3),
            "obstacle_name": primary.get("name"),
            "distance_to_obstacle_m": round(distance_m, 3),
            "forward_m": round(forward_m, 3),
            "lateral_clearance_m": round(lateral_clearance_m, 3),
            "route_vector_source_ref": route_source_ref,
        },
        "rationale": (
            "source-backed obstacle/building risk is present; offset laterally "
            "from the current route-to-obstacle vector and climb to a bounded "
            "avoidance altitude"
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
    if requested_reroute_candidate is not None:
        candidates.append(requested_reroute_candidate)
    if avoidance_candidate is not None:
        candidates.append(avoidance_candidate)
    if altitude_candidate is not None:
        candidates.append(altitude_candidate)

    if requested in _PARAMETERIZED_RUNTIME_RECOVERY_ACTIONS:
        ranked = [
            candidate
            for candidate in candidates
            if candidate.get("selected_bounded_action") == requested
        ]
    else:
        ranked = candidates
    recommended = ranked[0] if ranked else None
    return {
        "schema_version": MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_SCHEMA_VERSION,
        "tool_name": MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_NAME,
        "tool_status": "computed" if recommended else "insufficient_context",
        "requested_action": requested,
        "request_reason": str(request_reason or "")[:500],
        "recommended_candidate": dict(recommended) if recommended else {},
        "candidates": [dict(candidate) for candidate in candidates],
        "candidate_actions": [
            str(candidate.get("selected_bounded_action") or "")
            for candidate in candidates
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


def _recommended_recovery_tool_action(
    planner_tool_results: list[Mapping[str, Any]],
) -> str:
    for result in planner_tool_results:
        recommended = result.get("recommended_candidate")
        if not isinstance(recommended, Mapping):
            continue
        selected = str(recommended.get("selected_bounded_action") or "").strip()
        if selected:
            return selected
    return ""


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
    battery_threshold = _float_or_none(
        recovery_policy.get("battery_return_threshold_percent")
    )
    if battery_threshold is None:
        battery_threshold = 20.0
    battery_warning = str(
        battery.get("warning")
        or battery.get("battery_warning")
        or telemetry_snapshot.get("battery_warning")
        or ""
    ).strip().lower()
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
        terrain.get("terrain_clearance_m")
        or telemetry_snapshot.get("terrain_clearance_m")
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
    terrain_clearance_grace = _float_or_none(
        terrain.get("terrain_clearance_grace_m")
        or telemetry_snapshot.get("terrain_clearance_grace_m")
    ) or 0.0
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
    if (
        wind_limit is not None
        and wind_speed is not None
        and wind_speed > wind_limit
    ):
        reasons.append("wind_above_recovery_limit")

    route_deviation = _float_or_none(
        route.get("deviation_xy_m")
        or route.get("wind_drift_deviation_xy_m")
        or telemetry_snapshot.get("route_deviation_xy_m")
    )
    route_limit = _float_or_none(recovery_policy.get("max_route_deviation_xy_m"))
    if (
        route_limit is not None
        and route_deviation is not None
        and route_deviation > route_limit
    ):
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
    if _boolish(
        telemetry.get("dropout") or telemetry_snapshot.get("telemetry_dropout")
    ):
        reasons.append("telemetry_dropout")
    if _boolish(
        obstacle.get("obstacle_detected")
        or obstacle.get("building_risk_detected")
        or obstacle.get("landing_zone_blocked")
        or telemetry_snapshot.get("obstacle_detected")
        or telemetry_snapshot.get("building_risk_detected")
    ):
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
        agent_output.get("selected_bounded_action")
        or agent_output.get("response_kind")
        or ""
    ).strip()
    if selected_action not in MISSIONOS_RUNTIME_RECOVERY_ACTIONS:
        blocking_reasons.append(
            f"unsupported_recovery_action:{selected_action or '<missing>'}"
        )

    trigger_level = str(agent_output.get("trigger_level") or "").strip()
    if trigger_level not in {"none", "advisory", "immediate"}:
        blocking_reasons.append("trigger_level_not_supported")

    preauthorized_actions = recovery_policy.get("preauthorized_actions")
    if isinstance(preauthorized_actions, str):
        preauthorized = {preauthorized_actions}
    else:
        preauthorized = {
            str(item)
            for item in (preauthorized_actions or ())
            if str(item).strip()
        }
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
    operator_approval_required = bool(
        agent_output.get("requires_human_approval", True)
    )
    proposed_parameters = agent_output.get("proposed_parameters")
    proposed_parameters = (
        dict(proposed_parameters) if isinstance(proposed_parameters, Mapping) else {}
    )
    matching_tool_candidate: dict[str, Any] | None = None
    if selected_action in _PARAMETERIZED_RUNTIME_RECOVERY_ACTIONS:
        if require_parameter_tool_call and not parameter_tool_called:
            blocking_reasons.append(
                "parameterized_recovery_requires_runtime_recovery_planner_tool_call"
            )
        if parameter_tool_called:
            recommended_tool_action = _recommended_recovery_tool_action(
                list(planner_tool_results or [])
            )
            if (
                require_parameter_tool_call
                and recommended_tool_action
                and recommended_tool_action != selected_action
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

    if selected_action == "continue" and observed_reasons:
        blocking_reasons.append("continue_not_allowed_with_active_runtime_risk")
    if (
        selected_action == "return_to_launch"
        and "battery_projected_insufficient_for_return_home" in observed_reasons
    ):
        blocking_reasons.append(
            "return_to_launch_not_allowed_when_projected_battery_insufficient_for_return_home"
        )
    if (
        selected_action == "avoid_obstacle"
        and "obstacle_or_building_risk" not in observed_reasons
    ):
        blocking_reasons.append(
            "avoid_obstacle_requires_source_backed_obstacle_or_building_risk"
        )
    if selected_action == "adjust_altitude" and _float_or_none(
        proposed_parameters.get("target_altitude_m")
        if "target_altitude_m" in proposed_parameters
        else proposed_parameters.get("altitude_m")
    ) is None:
        blocking_reasons.append("adjust_altitude_requires_target_altitude_m")
    if selected_action == "adjust_speed" and _float_or_none(
        proposed_parameters.get("target_speed_mps")
        if "target_speed_mps" in proposed_parameters
        else proposed_parameters.get("speed_mps")
    ) is None:
        blocking_reasons.append("adjust_speed_requires_target_speed_mps")
    if selected_action in {"reroute", "avoid_obstacle"} and (
        _first_float(proposed_parameters.get("target_x_m"), proposed_parameters.get("x_m"))
        is None
        or _first_float(
            proposed_parameters.get("target_y_m"),
            proposed_parameters.get("y_m"),
        )
        is None
    ):
        blocking_reasons.append(f"{selected_action}_requires_target_x_m_and_target_y_m")
    if high_impact and not (action_preapproved or operator_approval_required):
        blocking_reasons.append(
            "high_impact_recovery_requires_preapproval_or_human_review"
        )

    if blocking_reasons:
        selected_action = "operator_review"
        trigger_level = "advisory"

    return {
        "schema_version": MISSIONOS_RUNTIME_RECOVERY_ASSESSMENT_SCHEMA_VERSION,
        "assessment_status": (
            "blocked" if blocking_reasons else "proposal_guardrail_passed"
        ),
        "selected_bounded_action": selected_action,
        "proposed_parameters": proposed_parameters,
        "trigger_level": trigger_level or "advisory",
        "agent_trigger_reasons": list(agent_output.get("trigger_reasons") or []),
        "observed_risk_reasons": observed_reasons,
        "blocking_reasons": blocking_reasons,
        "recovery_planner_tool_called": bool(parameter_tool_called),
        "recovery_planner_tool_candidate": dict(matching_tool_candidate or {}),
        "proposed_parameters_source": (
            "runtime_recovery_planner_function_tool"
            if matching_tool_candidate is not None
            else "agent_output"
        ),
        "action_preapproved_by_policy": action_preapproved,
        "preauthorized_policy_ref": str(
            recovery_policy.get("policy_ref")
            or recovery_policy.get("recovery_policy_ref")
            or ""
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
    assessment = _validate_runtime_recovery_output(
        agent_output={
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
    if not _google_adk_credentials_available("missionos_runtime_recovery_agent"):
        return {
            "schema_version": MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION,
            "runtime_status": "not_configured",
            "blocking_reasons": ["GOOGLE_API_KEY_not_configured"],
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
    monitoring_payloads = _monitoring_observation_payloads(monitoring_observations)
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
    if not _google_adk_credentials_available("missionos_chief_agent"):
        return {
            "schema_version": MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION,
            "runtime_status": "not_configured",
            "blocking_reasons": ["GOOGLE_API_KEY_not_configured"],
            "proposal": {},
            "agent_invocations": [],
            "monitoring_observations": monitoring_payloads,
            "progress_counted": False,
        }

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
    chief_guardrail = (
        chief_guardrail_value
        if isinstance(chief_guardrail_value, Mapping)
        else {}
    )
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
    chief_output = (
        chief_output_value
        if isinstance(chief_output_value, Mapping)
        else {}
    )
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
    boundary_status = str(
        safety_critic_output.get("boundary_status") or ""
    ).strip()
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
        "safety_critic_agent": (
            MISSIONOS_SAFETY_CRITIC_AGENT_NAME if safety_critic_output else ""
        ),
        "safety_critic_output": dict(safety_critic_output),
        "operator_facing_route": MISSIONOS_OPERATOR_FACING_ROUTE,
        "internal_capability_registry": build_missionos_capability_registry_summary(),
        "coordination_pattern": (
            "chief_intent_router_with_specialist_pipeline_and_safety_critic"
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
        "schema_version": MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION,
        "runtime_status": "proposal_guardrail_passed",
        "blocking_reasons": [],
        "proposal": proposal,
        "agent_invocations": invocations,
        "operator_facing_route": MISSIONOS_OPERATOR_FACING_ROUTE,
        "internal_capability_registry": build_missionos_capability_registry_summary(),
        "coordination_pattern": (
            "chief_intent_router_with_specialist_pipeline_and_safety_critic"
        ),
        "monitoring_observations": monitoring_payloads,
        "progress_counted": False,
    }


__all__ = [
    "MISSIONOS_AGENT_RUNTIME_ADK_ENABLED_ENV",
    "MISSIONOS_AGENT_RUNTIME_MODEL_ENV",
    "MISSIONOS_AGENT_RUNTIME_RESULT_SCHEMA_VERSION",
    "MISSIONOS_OPERATOR_FACING_ROUTE",
    "MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION",
    "guard_missionos_agent_output",
    "guard_runtime_recovery_planner_result",
    "run_missionos_agent_runtime",
    "run_missionos_runtime_recovery_agent",
]
