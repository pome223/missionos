"""MissionOS real-hardware arm/disarm proposal planner bridge.

This planner lets a real Google ADK LlmAgent (or a subprocess command override
used only as an explicit dev/test escape hatch) *propose* a props-removed
real-hardware arm/disarm bench response. It only ever emits a guarded
``missionos_llm_response_proposal.v1`` whose ``response_kind`` is the single
real-hardware kind ``operator_gated_real_hardware_arm_disarm``.

It does NOT approve, dispatch, execute, register dispatch authority, or make
the proposal artifact count as runtime or AI-agent progress. The downstream
operator approval, dispatch token consumption, and deterministic executor
(``invoke_missionos_real_hardware_dispatch_runtime``) remain the only path to
physical actuation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import uuid
from typing import Any, Mapping

from src.runtime.missionos_llm_schemas import (
    LLM_INVOCATION_EVIDENCE_SCHEMA_VERSION,
    LLM_RESPONSE_PROPOSAL_SCHEMA_VERSION,
    MissionOSLLMSchemaValidationError,
    validate_llm_response_proposal,
)


REAL_HARDWARE_ARM_DISARM_PLANNER_RESULT_SCHEMA_VERSION = (
    "missionos_real_hardware_arm_disarm_planner_result.v1"
)
REAL_HARDWARE_ARM_DISARM_PLANNER_GUARDRAIL_SCHEMA_VERSION = (
    "missionos_real_hardware_arm_disarm_planner_guardrail.v1"
)
REAL_HARDWARE_ARM_DISARM_PLANNER_PROMPT_SCHEMA_VERSION = (
    "missionos_real_hardware_arm_disarm_planner_prompt.v1"
)

REAL_HARDWARE_ARM_DISARM_RESPONSE_KIND = "operator_gated_real_hardware_arm_disarm"

REAL_HARDWARE_ARM_DISARM_PLANNER_ADK_ENABLED_ENV = (
    "MISSIONOS_REAL_HARDWARE_ARM_DISARM_PLANNER_ADK_ENABLED"
)
REAL_HARDWARE_ARM_DISARM_PLANNER_COMMAND_ENV = (
    "MISSIONOS_REAL_HARDWARE_ARM_DISARM_PLANNER_COMMAND"
)
REAL_HARDWARE_ARM_DISARM_PLANNER_ALLOW_OVERRIDE_ENV = (
    "MISSIONOS_ALLOW_REAL_HARDWARE_ARM_DISARM_PLANNER_COMMAND_OVERRIDE"
)
REAL_HARDWARE_ARM_DISARM_PLANNER_MODEL_ENV = (
    "MISSIONOS_REAL_HARDWARE_ARM_DISARM_PLANNER_MODEL_ID"
)
REAL_HARDWARE_ARM_DISARM_PLANNER_TIMEOUT_ENV = (
    "MISSIONOS_REAL_HARDWARE_ARM_DISARM_PLANNER_TIMEOUT_SECONDS"
)
DEFAULT_REAL_HARDWARE_ARM_DISARM_PLANNER_TIMEOUT_SECONDS = 60

# The ADK output may not carry authority/approval/progress claims. These are
# the same forbidden keys the generic response planner rejects.
RAW_RESPONSE_FORBIDDEN_AUTHORITY_KEYS = frozenset(
    {
        "approved",
        "approval_granted",
        "operator_approved",
        "dispatch_authority_created",
        "gate_passed",
        "llm_judgment_in_gate",
        "progress_counted",
        "goal_640_progress_counted",
        "ai_agent_progress_counted",
        "drone_physics_affected",
        "dispatch_executed",
        "dispatch_executed_in_runtime",
        "automatic_dispatch_executed",
        "physical_execution_invoked",
        "hardware_target_allowed",
        "delivery_completion_claimed",
    }
)

_PLANNER_INSTRUCTION = (
    "You are the MissionOS real-hardware arm/disarm bench proposer. "
    "The drone has its propellers physically removed; this is a bench smoke "
    "of arm then disarm only, with no flight and no thrust. "
    "Return only one JSON object. Do not wrap it in markdown. "
    "You may explain the situation and propose response_kind, parameters, "
    "rationale, expected_outcome, uncertainty, and approval_request. "
    "The response_kind must be exactly "
    "'operator_gated_real_hardware_arm_disarm'. The parameters object must be "
    "empty: this bench takes no numeric parameters. "
    "You must not approve, create dispatch authority, claim gate passage, "
    "claim physical execution, or claim progress."
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_json_from_stdout(stdout: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _read_json_from_model_response(response_text: str) -> dict[str, Any] | None:
    candidate = response_text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def build_real_hardware_arm_disarm_prompt(
    *,
    bench_context: Mapping[str, Any] | None = None,
    operator_instruction: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic prompt payload for the arm/disarm proposer."""

    context = dict(bench_context or {})
    instruction = dict(operator_instruction or {})
    return {
        "schema_version": REAL_HARDWARE_ARM_DISARM_PLANNER_PROMPT_SCHEMA_VERSION,
        "task": "propose_real_hardware_arm_disarm_bench",
        "role_contract": {
            "llm_judgment_in_gate": False,
            "operator_approval_required": True,
            "rules_constrain_only": True,
            "artifact_is_not_progress": True,
            "physical_execution_is_downstream_executor_only": True,
        },
        "allowed_response_kinds": [REAL_HARDWARE_ARM_DISARM_RESPONSE_KIND],
        "parameter_contract": {
            "allowed_parameter_keys": [],
            "parameters_must_be_empty": True,
        },
        "bench": {
            "props_removed": bool(context.get("props_removed", True)),
            "backend_target": "px4_real_hardware",
            "sequence": ["arm", "disarm"],
            "no_flight": True,
            "serial_device": context.get("serial_device"),
        },
        "operator_instruction": {
            "text": str(instruction.get("text") or "")[:2000],
            "source": str(instruction.get("source") or "missionos_operator"),
        },
        "required_output_fields": [
            "response_kind",
            "parameters",
            "rationale",
            "expected_outcome",
            "uncertainty",
            "approval_request",
        ],
        "strict_output_contract": (
            "Return exactly one JSON object. response_kind must be "
            "'operator_gated_real_hardware_arm_disarm'. parameters must be an "
            "empty object. Do not include approval flags, gate verdicts, "
            "dispatch authority, physical execution claims, or progress claims."
        ),
    }


def _raw_response_forbidden_authority_reasons(
    raw_response: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    for key in sorted(RAW_RESPONSE_FORBIDDEN_AUTHORITY_KEYS):
        if key in raw_response:
            reasons.append(f"raw_llm_output_forbidden_authority_key:{key}")
    return reasons


def guard_real_hardware_arm_disarm_proposal(
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Reject unsafe proposal shapes without deciding whether the idea is good."""

    blocking_reasons: list[str] = []
    checks = {
        "schema_valid": False,
        "response_kind_is_real_hardware_arm_disarm": False,
        "parameters_empty": False,
        "llm_judgment_not_in_gate": False,
        "artifact_not_progress": False,
        "operator_approval_not_self_granted": False,
    }
    validated: dict[str, Any] | None = None
    try:
        validated = validate_llm_response_proposal(proposal)
        checks["schema_valid"] = True
    except MissionOSLLMSchemaValidationError as exc:
        blocking_reasons.append(str(exc))

    if validated is not None:
        response_kind = str(validated.get("response_kind") or "")
        checks["response_kind_is_real_hardware_arm_disarm"] = (
            response_kind == REAL_HARDWARE_ARM_DISARM_RESPONSE_KIND
        )
        if not checks["response_kind_is_real_hardware_arm_disarm"]:
            blocking_reasons.append("response_kind_not_real_hardware_arm_disarm")
        parameters = validated.get("parameters")
        checks["parameters_empty"] = isinstance(parameters, Mapping) and not parameters
        if not checks["parameters_empty"]:
            blocking_reasons.append("parameters_must_be_empty_for_arm_disarm")
        checks["llm_judgment_not_in_gate"] = (
            validated.get("llm_judgment_in_gate") is False
        )
        checks["artifact_not_progress"] = (
            validated.get("progress_counted") is False
            and validated.get("goal_640_progress_counted") is False
            and validated.get("ai_agent_progress_counted") is False
            and validated.get("drone_physics_affected") is False
        )
        checks["operator_approval_not_self_granted"] = (
            validated.get("operator_approved") is False
            and validated.get("dispatch_authority_created") is False
        )

    return {
        "schema_version": REAL_HARDWARE_ARM_DISARM_PLANNER_GUARDRAIL_SCHEMA_VERSION,
        "guardrail_passed": not blocking_reasons,
        "checks": checks,
        "blocking_reasons": blocking_reasons,
    }


def run_real_hardware_arm_disarm_planner(
    *,
    artifact_root: Path | str,
    artifact_relative,
    bench_context: Mapping[str, Any] | None = None,
    operator_instruction: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the configured ADK/Gemini proposer and persist a guarded proposal.

    If ADK/Gemini is not explicitly enabled, a command override can be used for
    tests/dev only when both the command and the explicit override env are
    present. Neither path approves, dispatches, or executes anything.
    """

    prompt = build_real_hardware_arm_disarm_prompt(
        bench_context=bench_context,
        operator_instruction=operator_instruction,
    )
    prompt_text = json.dumps(prompt, sort_keys=True)

    if os.environ.get(REAL_HARDWARE_ARM_DISARM_PLANNER_ADK_ENABLED_ENV) == "1":
        return _run_adk_gemini_planner(
            prompt_text=prompt_text,
            artifact_root=artifact_root,
            artifact_relative=artifact_relative,
        )

    command_text = os.environ.get(
        REAL_HARDWARE_ARM_DISARM_PLANNER_COMMAND_ENV, ""
    ).strip()
    if command_text:
        if (
            os.environ.get(REAL_HARDWARE_ARM_DISARM_PLANNER_ALLOW_OVERRIDE_ENV)
            != "1"
        ):
            return _planner_result(
                status="blocked",
                blocking_reasons=[
                    f"{REAL_HARDWARE_ARM_DISARM_PLANNER_ALLOW_OVERRIDE_ENV}_required"
                ],
            )
        return _run_command_override_planner(
            command_text=command_text,
            prompt_text=prompt_text,
            artifact_root=artifact_root,
            artifact_relative=artifact_relative,
        )

    return _planner_result(
        status="not_configured",
        blocking_reasons=[
            f"{REAL_HARDWARE_ARM_DISARM_PLANNER_ADK_ENABLED_ENV}_not_enabled",
            f"{REAL_HARDWARE_ARM_DISARM_PLANNER_COMMAND_ENV}_not_configured",
        ],
    )


def _run_adk_gemini_planner(
    *,
    prompt_text: str,
    artifact_root: Path | str,
    artifact_relative,
) -> dict[str, Any]:
    from src.agents.model_config import llm_provider_label

    agent_name = "missionos_real_hardware_arm_disarm_planner"
    model_id = _planner_model_id()
    started_at = datetime.now(timezone.utc)
    try:
        response_text = _invoke_adk_gemini_response_text(
            prompt_text=prompt_text,
            model_id=model_id,
            timeout_seconds=_planner_timeout_seconds(),
        )
    except Exception as exc:  # pragma: no cover - live service failure shape varies.
        return _planner_result(
            status="blocked",
            blocking_reasons=[
                f"google_adk_gemini_invocation_failed:{type(exc).__name__}"
            ],
        )
    completed_at = datetime.now(timezone.utc)
    raw_response = _read_json_from_model_response(response_text)
    if raw_response is None:
        return _planner_result(
            status="blocked",
            blocking_reasons=["google_adk_gemini_response_not_json_object"],
            stdout_sha256=_sha256_text(response_text),
        )
    invocation_evidence = _llm_invocation_evidence(
        provider=llm_provider_label(agent_name),
        invocation_kind=llm_provider_label(agent_name),
        model_id=model_id,
        prompt_text=prompt_text,
        response_text=response_text,
        started_at=started_at,
        completed_at=completed_at,
        exit_code=0,
        command_argv=None,
        process_pid=None,
        stderr_text="",
    )
    return _persist_guarded_planner_response(
        raw_response=raw_response,
        invocation_evidence=invocation_evidence,
        generated_at=completed_at.isoformat(),
        artifact_root=artifact_root,
        artifact_relative=artifact_relative,
    )


def _run_command_override_planner(
    *,
    command_text: str,
    prompt_text: str,
    artifact_root: Path | str,
    artifact_relative,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    command_argv = shlex.split(command_text)
    try:
        process = subprocess.Popen(
            command_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate(
            input=prompt_text,
            timeout=_planner_timeout_seconds(),
        )
        exit_code = int(process.returncode)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _planner_result(
            status="blocked",
            blocking_reasons=[
                f"real_hardware_arm_disarm_planner_invocation_failed:{type(exc).__name__}"
            ],
        )
    completed_at = datetime.now(timezone.utc)
    if exit_code != 0:
        return _planner_result(
            status="blocked",
            blocking_reasons=["real_hardware_arm_disarm_planner_exit_code_nonzero"],
            invocation_exit_code=exit_code,
            stderr_sha256=_sha256_text(stderr),
        )

    raw_response = _read_json_from_stdout(stdout)
    if raw_response is None:
        return _planner_result(
            status="blocked",
            blocking_reasons=["real_hardware_arm_disarm_planner_stdout_not_json_object"],
            invocation_exit_code=exit_code,
            stdout_sha256=_sha256_text(stdout),
        )

    invocation_evidence = _llm_invocation_evidence(
        provider="command_override",
        invocation_kind="subprocess",
        model_id=os.environ.get(
            REAL_HARDWARE_ARM_DISARM_PLANNER_MODEL_ENV,
            "command_override_planner",
        ),
        prompt_text=prompt_text,
        response_text=stdout,
        started_at=started_at,
        completed_at=completed_at,
        exit_code=exit_code,
        command_argv=command_argv,
        process_pid=int(process.pid),
        stderr_text=stderr,
    )
    return _persist_guarded_planner_response(
        raw_response=raw_response,
        invocation_evidence=invocation_evidence,
        generated_at=completed_at.isoformat(),
        artifact_root=artifact_root,
        artifact_relative=artifact_relative,
    )


def _persist_guarded_planner_response(
    *,
    raw_response: Mapping[str, Any],
    invocation_evidence: Mapping[str, Any],
    generated_at: str,
    artifact_root: Path | str,
    artifact_relative,
) -> dict[str, Any]:
    forbidden_reasons = _raw_response_forbidden_authority_reasons(raw_response)
    if forbidden_reasons:
        guardrail = {
            "schema_version": (
                REAL_HARDWARE_ARM_DISARM_PLANNER_GUARDRAIL_SCHEMA_VERSION
            ),
            "guardrail_passed": False,
            "checks": {"raw_response_forbidden_authority_keys_absent": False},
            "blocking_reasons": forbidden_reasons,
        }
        return _planner_result(
            status="guardrail_blocked",
            blocking_reasons=forbidden_reasons,
            guardrail=guardrail,
        )
    proposal_id = f"missionos_llm_response_proposal_{uuid.uuid4().hex[:12]}"
    proposal = {
        "schema_version": LLM_RESPONSE_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "generated_at": generated_at,
        "response_kind": raw_response.get("response_kind"),
        "parameters": raw_response.get("parameters") or {},
        "rationale": raw_response.get("rationale"),
        "expected_outcome": raw_response.get("expected_outcome"),
        "uncertainty": raw_response.get("uncertainty"),
        "approval_request": raw_response.get("approval_request"),
        "llm_invocation_evidence": dict(invocation_evidence),
        "llm_judgment_in_gate": False,
        "progress_counted": False,
        "goal_640_progress_counted": False,
        "ai_agent_progress_counted": False,
        "drone_physics_affected": False,
        "dispatch_authority_created": False,
        "operator_approved": False,
    }
    guardrail = guard_real_hardware_arm_disarm_proposal(proposal)
    if guardrail.get("guardrail_passed") is not True:
        return _planner_result(
            status="guardrail_blocked",
            blocking_reasons=list(guardrail.get("blocking_reasons") or []),
            guardrail=guardrail,
            proposal=proposal,
        )

    root = Path(artifact_root)
    proposal_dir = root / "missionos_llm_response_proposal" / proposal_id
    proposal_dir.mkdir(parents=True, exist_ok=True)
    proposal_path = proposal_dir / "missionos_llm_response_proposal.json"
    proposal_path.write_text(
        json.dumps(proposal, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return _planner_result(
        status="proposal_guardrail_passed",
        proposal=proposal,
        proposal_ref=f"missionos_llm_response_proposal:{proposal_id}",
        proposal_artifact_path=artifact_relative(proposal_path),
        guardrail=guardrail,
    )


def _llm_invocation_evidence(
    *,
    provider: str,
    invocation_kind: str,
    model_id: str,
    prompt_text: str,
    response_text: str,
    started_at: datetime,
    completed_at: datetime,
    exit_code: int,
    command_argv: list[str] | None,
    process_pid: int | None,
    stderr_text: str,
) -> dict[str, Any]:
    evidence = {
        "schema_version": LLM_INVOCATION_EVIDENCE_SCHEMA_VERSION,
        "provider": provider,
        "model_id": model_id,
        "prompt_sha256": _sha256_text(prompt_text),
        "response_sha256": _sha256_text(response_text),
        "temperature": 0.0,
        "seed": None,
        "replay_n_runs": 1,
        "replay_agreement_ratio": 1.0,
        "llm_judgment_in_gate": False,
        "invocation_kind": invocation_kind,
        "command_argv_sha256": _sha256_json({"command_argv": command_argv or []}),
        "process_pid": process_pid,
        "invocation_started_at": started_at.isoformat(),
        "invocation_completed_at": completed_at.isoformat(),
        "invocation_exit_code": exit_code,
        "invocation_stdout_sha256": _sha256_text(response_text),
        "invocation_stderr_sha256": _sha256_text(stderr_text),
    }
    if command_argv is not None:
        evidence["command_argv"] = list(command_argv)
    return evidence


def _planner_model_id() -> str:
    from src.agents.model_config import agent_model_label

    env_model = os.environ.get(REAL_HARDWARE_ARM_DISARM_PLANNER_MODEL_ENV, "").strip()
    try:
        from src.config.settings import get_settings

        fallback = str(get_settings().agent_model)
    except Exception:
        fallback = "gemini-3.1-flash-lite-preview"
    return agent_model_label(
        env_model or fallback,
        agent_name="missionos_real_hardware_arm_disarm_planner",
    )


def _configure_google_adk_environment() -> None:
    from src.agents.model_config import google_llm_backend_enabled

    if not google_llm_backend_enabled("missionos_real_hardware_arm_disarm_planner"):
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


def _invoke_adk_gemini_response_text(
    *,
    prompt_text: str,
    model_id: str,
    timeout_seconds: int,
) -> str:
    return asyncio.run(
        asyncio.wait_for(
            _invoke_adk_gemini_response_text_async(
                prompt_text=prompt_text,
                model_id=model_id,
            ),
            timeout=timeout_seconds,
        )
    )


async def _invoke_adk_gemini_response_text_async(
    *,
    prompt_text: str,
    model_id: str,
) -> str:
    _configure_google_adk_environment()
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.genai import types

    from src.runtime.session_service import create_session_service
    from src.agents.model_config import resolve_agent_model

    agent = LlmAgent(
        name="missionos_real_hardware_arm_disarm_planner",
        model=resolve_agent_model(
            model_id,
            agent_name="missionos_real_hardware_arm_disarm_planner",
        ),
        instruction=_PLANNER_INSTRUCTION,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.0,
            responseMimeType="application/json",
        ),
    )
    app_name = "missionos_real_hardware_arm_disarm_planner"
    user_id = "missionos_operator"
    session_service = create_session_service()
    session = await session_service.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(
        agent=agent,
        app_name=app_name,
        session_service=session_service,
    )
    content = types.Content(
        role="user",
        parts=[
            types.Part(
                text=(
                    "Given this props-removed real-hardware bench context, "
                    "propose one bounded arm/disarm response as JSON only:\n"
                    f"{prompt_text}"
                )
            )
        ],
    )
    response_text_parts: list[str] = []
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
                response_text_parts.append(text)
    return "".join(response_text_parts).strip()


def _planner_timeout_seconds() -> int:
    value = os.environ.get(REAL_HARDWARE_ARM_DISARM_PLANNER_TIMEOUT_ENV)
    try:
        parsed = (
            int(value)
            if value is not None
            else DEFAULT_REAL_HARDWARE_ARM_DISARM_PLANNER_TIMEOUT_SECONDS
        )
    except ValueError:
        return DEFAULT_REAL_HARDWARE_ARM_DISARM_PLANNER_TIMEOUT_SECONDS
    return max(1, parsed)


def _planner_result(
    *,
    status: str,
    blocking_reasons: list[str] | None = None,
    proposal: Mapping[str, Any] | None = None,
    proposal_ref: str = "",
    proposal_artifact_path: str = "",
    guardrail: Mapping[str, Any] | None = None,
    invocation_exit_code: int | None = None,
    stdout_sha256: str = "",
    stderr_sha256: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": REAL_HARDWARE_ARM_DISARM_PLANNER_RESULT_SCHEMA_VERSION,
        "planner_status": status,
        "blocking_reasons": list(blocking_reasons or []),
        "proposal": dict(proposal or {}),
        "proposal_ref": proposal_ref,
        "proposal_artifact_path": proposal_artifact_path,
        "guardrail": dict(guardrail or {}),
        "invocation_exit_code": invocation_exit_code,
        "stdout_sha256": stdout_sha256,
        "stderr_sha256": stderr_sha256,
        "progress_counted": False,
        "goal_640_progress_counted": False,
        "ai_agent_progress_counted": False,
        "drone_physics_affected": False,
        "llm_judgment_in_gate": False,
    }


__all__ = [
    "REAL_HARDWARE_ARM_DISARM_PLANNER_ADK_ENABLED_ENV",
    "REAL_HARDWARE_ARM_DISARM_PLANNER_ALLOW_OVERRIDE_ENV",
    "REAL_HARDWARE_ARM_DISARM_PLANNER_COMMAND_ENV",
    "REAL_HARDWARE_ARM_DISARM_PLANNER_GUARDRAIL_SCHEMA_VERSION",
    "REAL_HARDWARE_ARM_DISARM_PLANNER_RESULT_SCHEMA_VERSION",
    "REAL_HARDWARE_ARM_DISARM_RESPONSE_KIND",
    "build_real_hardware_arm_disarm_prompt",
    "guard_real_hardware_arm_disarm_proposal",
    "run_real_hardware_arm_disarm_planner",
]
