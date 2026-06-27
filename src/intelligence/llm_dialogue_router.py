"""MissionOS LLM Dialogue Router.

Interprets human operator utterances in the context of current MissionOS state
and conversation history, then proposes a bounded routing intent.

The router does not approve actions, create dispatch authority, execute runtime
paths, or make progress claims. It outputs a routing proposal that is validated
by a deterministic guardrail before use.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
from typing import Any

LLM_DIALOGUE_ROUTER_RESULT_SCHEMA_VERSION = "missionos_llm_dialogue_router_result.v1"
LLM_DIALOGUE_ROUTER_PROPOSAL_SCHEMA_VERSION = "missionos_llm_dialogue_router_proposal.v1"
LLM_DIALOGUE_ROUTER_GUARDRAIL_SCHEMA_VERSION = "missionos_llm_dialogue_router_guardrail.v1"

LLM_DIALOGUE_ROUTER_MODEL_ENV = "MISSIONOS_LLM_DIALOGUE_ROUTER_MODEL_ID"
LLM_DIALOGUE_ROUTER_COMMAND_ENV = "MISSIONOS_LLM_DIALOGUE_ROUTER_COMMAND"
LLM_DIALOGUE_ROUTER_COMMAND_ALLOW_ENV = "MISSIONOS_ALLOW_LLM_DIALOGUE_ROUTER_COMMAND_OVERRIDE"
LLM_DIALOGUE_ROUTER_TIMEOUT_ENV = "MISSIONOS_LLM_DIALOGUE_ROUTER_TIMEOUT_SECONDS"
LLM_DIALOGUE_ROUTER_ADK_ENABLED_ENV = "MISSIONOS_LLM_DIALOGUE_ROUTER_ADK_ENABLED"

DEFAULT_TIMEOUT_SECONDS = 30

DIALOGUE_ROUTER_ALLOWED_INTENTS = frozenset({
    "status",
    "approve",
    "reject",
    "revision",
    "execute",
    "repair",
    "plan",
    "mission_designer_plan",
})

DIALOGUE_ROUTER_FORBIDDEN_KEYS = frozenset({
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
})

_SYSTEM_INSTRUCTION = (
    "You are the MissionOS Dialogue Router. "
    "Your task is to interpret a human operator utterance in the context of the current "
    "MissionOS state and conversation history, then output a bounded routing proposal as JSON. "
    "Output exactly one JSON object with these fields: "
    "intent (string, one of: status, approve, reject, revision, execute, repair, plan, mission_designer_plan), "
    "operator_instruction (string, enriched instruction for the downstream planner, max 2000 chars), "
    "reason (string, brief explanation of why this intent was selected), "
    "requires_human_approval (boolean). "
    "You must not include approved, approval_granted, operator_approved, "
    "dispatch_authority_created, progress_counted, goal_640_progress_counted, "
    "ai_agent_progress_counted, dispatch_executed, automatic_dispatch_executed, "
    "physical_execution_invoked, delivery_completion_claimed, hardware_target_allowed, "
    "bypass_gate, llm_judgment_in_gate, or gate_status_mutated in your output. "
    "Use mission_designer_plan when the human asks to design or fly a drone mission, "
    "sets scenario conditions such as wind, terrain, payload, battery, waypoints, or asks for PX4/Gazebo flight planning. "
    "Use plan for source-bound MissionOS Form 2a response selection from existing Form 1 evidence. "
    "You do not approve, dispatch, count progress, or execute. You only route."
)


def _model_id() -> str:
    from src.agents.model_config import agent_model_label

    env_model = os.environ.get(LLM_DIALOGUE_ROUTER_MODEL_ENV, "").strip()
    try:
        from src.config.settings import get_settings
        fallback = str(get_settings().agent_model)
    except Exception:
        fallback = "gemini-2.5-flash-preview-04-17"
    return agent_model_label(
        env_model or fallback,
        agent_name="missionos_dialogue_router_agent",
    )


def _timeout_seconds() -> int:
    value = os.environ.get(LLM_DIALOGUE_ROUTER_TIMEOUT_ENV)
    try:
        parsed = int(value) if value is not None else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return max(1, parsed)


def _configure_google_adk_environment() -> None:
    from src.agents.model_config import google_llm_backend_enabled

    if not google_llm_backend_enabled("missionos_dialogue_router_agent"):
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


def build_dialogue_router_prompt(
    utterance: str,
    missionos_state: dict[str, Any],
    conversation_history: list[dict[str, str]] | None = None,
) -> str:
    """Build the JSON prompt string for the LLM Dialogue Router."""
    payload = {
        "schema_version": "missionos_llm_dialogue_router_prompt.v1",
        "role_contract": {
            "llm_role": "dialogue_router_only",
            "llm_outputs": [
                "intent",
                "operator_instruction",
                "reason",
                "requires_human_approval",
            ],
            "llm_must_not_output": sorted(DIALOGUE_ROUTER_FORBIDDEN_KEYS),
            "llm_does_not": [
                "approve actions",
                "create dispatch authority",
                "claim gate passage",
                "dispatch",
                "count progress",
                "execute physical actions",
            ],
        },
        "allowed_intents": sorted(DIALOGUE_ROUTER_ALLOWED_INTENTS),
        "missionos_current_state": missionos_state,
        "conversation_history": list(conversation_history or [])[-10:],
        "human_utterance": utterance[:2000],
    }
    return json.dumps(payload, ensure_ascii=False)


def _unique_json_text_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        stripped = candidate.strip()
        if not stripped or stripped in seen:
            return
        seen.add(stripped)
        candidates.append(stripped)

    add(text)
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL):
        add(match.group(1))

    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            current = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    add(text[start : index + 1])
                    break
    return candidates


def _load_dialogue_router_json_object(response_text: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    parse_errors: list[str] = []
    strict_text = response_text.strip()
    for index, candidate in enumerate(_unique_json_text_candidates(response_text)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            if index == 0:
                parse_errors.append(f"strict_json_decode_failed:{exc.msg}")
            continue
        if not isinstance(parsed, dict):
            parse_errors.append(f"json_value_not_object:{type(parsed).__name__}")
            continue
        strategy = "strict_json" if candidate == strict_text else "salvaged_json_object"
        return parsed, {
            "strategy": strategy,
            "salvaged": strategy != "strict_json",
            "errors": parse_errors[:3],
        }
    return None, {
        "strategy": "failed",
        "salvaged": False,
        "errors": parse_errors[:3] or ["json_object_not_found"],
    }


def _scan_forbidden_keys_recursive(
    obj: Any,
    forbidden_keys: frozenset[str],
    _depth: int = 0,
) -> list[str]:
    """Recursively scan obj for forbidden keys at any nesting depth."""
    if _depth > 20:
        return []
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in forbidden_keys:
                found.append(key)
            found.extend(_scan_forbidden_keys_recursive(value, forbidden_keys, _depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_scan_forbidden_keys_recursive(item, forbidden_keys, _depth + 1))
    return found


def guard_llm_dialogue_router_proposal(raw_output: Any) -> dict[str, Any]:
    """Validate LLM dialogue router output. Returns a guardrail result dict."""
    blocking_reasons: list[str] = []

    if not isinstance(raw_output, dict):
        return {
            "schema_version": LLM_DIALOGUE_ROUTER_GUARDRAIL_SCHEMA_VERSION,
            "guardrail_passed": False,
            "blocking_reasons": ["llm_output_not_dict"],
            "validated_proposal": None,
        }

    found_forbidden = _scan_forbidden_keys_recursive(raw_output, DIALOGUE_ROUTER_FORBIDDEN_KEYS)
    for key in dict.fromkeys(found_forbidden):
        blocking_reasons.append(f"forbidden_key_present:{key}")

    intent = raw_output.get("intent")
    if intent not in DIALOGUE_ROUTER_ALLOWED_INTENTS:
        blocking_reasons.append(f"intent_not_in_allowed_set:{intent!r}")

    op_instruction = raw_output.get("operator_instruction")
    if not isinstance(op_instruction, str):
        blocking_reasons.append("operator_instruction_must_be_string")

    reason = raw_output.get("reason")
    if not isinstance(reason, str):
        blocking_reasons.append("reason_must_be_string")

    requires_approval = raw_output.get("requires_human_approval")
    if not isinstance(requires_approval, bool):
        blocking_reasons.append("requires_human_approval_must_be_bool")

    if blocking_reasons:
        return {
            "schema_version": LLM_DIALOGUE_ROUTER_GUARDRAIL_SCHEMA_VERSION,
            "guardrail_passed": False,
            "blocking_reasons": blocking_reasons,
            "validated_proposal": None,
        }

    return {
        "schema_version": LLM_DIALOGUE_ROUTER_GUARDRAIL_SCHEMA_VERSION,
        "guardrail_passed": True,
        "blocking_reasons": [],
        "validated_proposal": {
            "schema_version": LLM_DIALOGUE_ROUTER_PROPOSAL_SCHEMA_VERSION,
            "intent": str(intent),
            "operator_instruction": str(op_instruction)[:2000],
            "reason": str(reason)[:500],
            "requires_human_approval": bool(requires_approval),
        },
    }


async def _invoke_adk_gemini_async(*, prompt_text: str, model_id: str) -> str:
    _configure_google_adk_environment()
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.genai import types

    from src.runtime.session_service import create_session_service
    from src.agents.model_config import resolve_agent_model

    agent = LlmAgent(
        name="missionos_dialogue_router",
        model=resolve_agent_model(model_id, agent_name="missionos_dialogue_router_agent"),
        instruction=_SYSTEM_INSTRUCTION,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.0,
            responseMimeType="application/json",
        ),
    )
    app_name = "missionos_llm_dialogue_router"
    user_id = "missionos_operator"
    session_service = create_session_service()
    session = await session_service.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)
    content = types.Content(
        role="user",
        parts=[types.Part(text=prompt_text)],
    )
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


def run_llm_dialogue_router(
    utterance: str,
    missionos_state: dict[str, Any],
    conversation_history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Run the LLM Dialogue Router and return a guardrail-validated routing proposal.

    Returns router_status one of:
      "proposal_guardrail_passed" — use proposal["intent"] and proposal["operator_instruction"]
      "guardrail_blocked"         — LLM output was invalid; return error to human (Case B)
      "not_configured"            — no LLM backend; caller should fall back to keyword router
    """
    prompt_text = build_dialogue_router_prompt(utterance, missionos_state, conversation_history)
    timeout_seconds = _timeout_seconds()

    raw_output: dict[str, Any] | None = None
    router_error: str | None = None
    json_parse: dict[str, Any] | None = None

    adk_enabled = os.environ.get(LLM_DIALOGUE_ROUTER_ADK_ENABLED_ENV, "").strip() == "1"
    if adk_enabled:
        try:
            response_text = asyncio.run(
                asyncio.wait_for(
                    _invoke_adk_gemini_async(
                        prompt_text=prompt_text,
                        model_id=_model_id(),
                    ),
                    timeout=timeout_seconds,
                )
            )
            raw_output, json_parse = _load_dialogue_router_json_object(response_text)
            if raw_output is None:
                reason = ",".join(str(item) for item in (json_parse or {}).get("errors") or [])
                router_error = f"adk_error:dialogue_router_json_unparseable:{reason}"
        except Exception as exc:
            router_error = f"adk_error:{type(exc).__name__}:{exc}"

    if raw_output is None:
        command_override = os.environ.get(LLM_DIALOGUE_ROUTER_COMMAND_ENV, "").strip()
        allow_override = os.environ.get(LLM_DIALOGUE_ROUTER_COMMAND_ALLOW_ENV, "").strip() == "1"
        if command_override and allow_override:
            try:
                proc = subprocess.run(
                    shlex.split(command_override),
                    input=prompt_text.encode(),
                    capture_output=True,
                    timeout=timeout_seconds,
                )
                if proc.returncode == 0:
                    raw_output, json_parse = _load_dialogue_router_json_object(
                        proc.stdout.decode()
                    )
                    if raw_output is None:
                        reason = ",".join(
                            str(item) for item in (json_parse or {}).get("errors") or []
                        )
                        router_error = f"command_error:dialogue_router_json_unparseable:{reason}"
                else:
                    router_error = f"command_error:exit_{proc.returncode}"
            except Exception as exc:
                router_error = f"command_error:{type(exc).__name__}:{exc}"

    if raw_output is None:
        return {
            "schema_version": LLM_DIALOGUE_ROUTER_RESULT_SCHEMA_VERSION,
            "router_status": "not_configured",
            "blocking_reasons": [router_error or "no_llm_backend_available"],
            "proposal": None,
            "guardrail": None,
            "json_parse": json_parse,
            "progress_counted": False,
        }

    guardrail = guard_llm_dialogue_router_proposal(raw_output)

    if not guardrail["guardrail_passed"]:
        return {
            "schema_version": LLM_DIALOGUE_ROUTER_RESULT_SCHEMA_VERSION,
            "router_status": "guardrail_blocked",
            "blocking_reasons": guardrail["blocking_reasons"],
            "proposal": None,
            "guardrail": guardrail,
            "json_parse": json_parse,
            "progress_counted": False,
        }

    return {
        "schema_version": LLM_DIALOGUE_ROUTER_RESULT_SCHEMA_VERSION,
        "router_status": "proposal_guardrail_passed",
        "blocking_reasons": [],
        "proposal": guardrail["validated_proposal"],
        "guardrail": guardrail,
        "json_parse": json_parse,
        "progress_counted": False,
    }
