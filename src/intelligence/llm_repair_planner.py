"""MissionOS LLM Repair Planner bridge.

The repair planner reads blocked or failed MissionOS evidence and proposes the
next bounded repair attempt. It does not approve actions, create dispatch
authority, execute runtime paths, or make progress claims.
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
    LLM_REPAIR_PROPOSAL_SCHEMA_VERSION,
    MissionOSLLMSchemaValidationError,
    validate_llm_repair_proposal,
)


LLM_REPAIR_PLANNER_RESULT_SCHEMA_VERSION = "missionos_llm_repair_planner_result.v1"
LLM_REPAIR_PLANNER_GUARDRAIL_SCHEMA_VERSION = "missionos_llm_repair_planner_guardrail.v1"

LLM_REPAIR_PLANNER_COMMAND_ENV = "MISSIONOS_LLM_REPAIR_PLANNER_COMMAND"
LLM_REPAIR_PLANNER_ALLOW_OVERRIDE_ENV = "MISSIONOS_ALLOW_LLM_REPAIR_PLANNER_COMMAND_OVERRIDE"
LLM_REPAIR_PLANNER_ADK_ENABLED_ENV = "MISSIONOS_LLM_REPAIR_PLANNER_ADK_ENABLED"
LLM_REPAIR_PLANNER_TIMEOUT_ENV = "MISSIONOS_LLM_REPAIR_PLANNER_TIMEOUT_SECONDS"
LLM_REPAIR_PLANNER_MODEL_ENV = "MISSIONOS_LLM_REPAIR_PLANNER_MODEL_ID"
DEFAULT_LLM_REPAIR_PLANNER_TIMEOUT_SECONDS = 60

REPO_ROOT = Path(__file__).resolve().parents[2]

ALLOWED_REPAIR_TARGETS = frozenset(
    {
        "payload_recovery_action_runtime_recheck",
        "real_smoke_dispatch_and_post_action_trajectory_reobservation",
        "adjust_form2a_response_parameters",
        "collect_more_runtime_evidence",
        "no_repair_required",
    }
)

ALLOWED_REPAIR_ACTION_TYPES = frozenset(
    {
        "rerun_runtime_verification",
        "adjust_response_parameters",
        "collect_more_evidence",
        "inspect_blocking_reason",
        "no_op",
    }
)

UI_ACTIONABLE_REPAIR_PARAMETERS = frozenset(
    {
        "payload_weight_kg",
        "takeoff_latitude",
        "takeoff_longitude",
        "dropoff_latitude",
        "dropoff_longitude",
    }
)

UNSUPPORTED_AUTO_RETRY_PARAMETERS = frozenset(
    {
        "wind_speed_mps",
        "wind_direction_deg",
        "mission_upload_timeout_seconds",
        "mission_upload_timeout_threshold",
        "px4_mission_upload_timeout_seconds",
        "px4_endpoint_connectivity",
        "gazebo_startup_delay",
    }
)

RAW_REPAIR_FORBIDDEN_AUTHORITY_KEYS = frozenset(
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


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_json_object(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
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


def build_repair_planner_prompt(
    *,
    evidence_artifact: Mapping[str, Any],
    evidence_artifact_path: str,
    evidence_artifact_sha256: str,
) -> dict[str, Any]:
    """Build a deterministic prompt payload for MissionOS repair planning."""

    return {
        "schema_version": "missionos_llm_repair_planner_prompt.v1",
        "task": "propose_missionos_repair",
        "role_contract": {
            "llm_judgment_in_gate": False,
            "operator_approval_required_for_execution": True,
            "rules_constrain_only": True,
            "artifact_is_not_progress": True,
        },
        "allowed_repair_targets": sorted(ALLOWED_REPAIR_TARGETS),
        "allowed_repair_action_types": sorted(ALLOWED_REPAIR_ACTION_TYPES),
        "ui_actionable_repair_parameters": sorted(UI_ACTIONABLE_REPAIR_PARAMETERS),
        "unsupported_auto_retry_parameters": sorted(UNSUPPORTED_AUTO_RETRY_PARAMETERS),
        "input_evidence": {
            "artifact_path": evidence_artifact_path,
            "artifact_sha256": evidence_artifact_sha256,
            "schema_version": evidence_artifact.get("schema_version"),
            "summary_status": evidence_artifact.get("summary_status")
            or evidence_artifact.get("action_status")
            or evidence_artifact.get("audit_status"),
            "selected_response_kind": evidence_artifact.get("selected_response_kind"),
            "blocking_reasons": list(evidence_artifact.get("blocking_reasons") or []),
            "next_required_applicator": evidence_artifact.get("next_required_applicator"),
            "ai_agent_progress_counted": evidence_artifact.get("ai_agent_progress_counted"),
            "goal_640_progress_counted": evidence_artifact.get("goal_640_progress_counted"),
        },
        "evidence_artifact": dict(evidence_artifact),
        "required_output_fields": [
            "repair_target",
            "repair_actions",
            "rationale",
            "expected_outcome",
            "uncertainty",
            "next_verification",
            "proposed_operator_instruction",
            "proposed_parameters",
        ],
        "output_example": {
            "repair_target": "payload_recovery_action_runtime_recheck",
            "repair_actions": [
                {
                    "action_type": "rerun_runtime_verification",
                    "description": "Rerun the scoped runtime verification only after confirming the input evidence hash still matches.",
                }
            ],
            "rationale": "The latest evidence needs a source-bound runtime recheck.",
            "expected_outcome": "A supported runtime artifact or an honest blocked reason.",
            "uncertainty": "SITL stability and source freshness may still block the run.",
            "next_verification": "Call the relevant Gateway verification endpoint and inspect persisted evidence.",
            "proposed_operator_instruction": "Retry the same approved route with payload 0.5kg after inspecting the takeoff failure receipt.",
            "proposed_parameters": {
                "payload_weight_kg": 0.5
            },
        },
        "strict_output_contract": (
            "Return exactly one JSON object. Propose repair planning only. "
            "repair_actions must be a non-empty array of objects, and every "
            "object must include action_type and description strings. "
            "proposed_operator_instruction must be the exact natural-language "
            "operator instruction for MissionOS to plan next if a human approves "
            "this repair proposal. proposed_parameters must be a JSON object of "
            "only the parameters the repair planner intentionally changes. "
            "Only payload_weight_kg and route coordinate changes are currently "
            "UI-actionable Mission Designer retry parameters. Wind is execution "
            "environment state, not a repair knob from this chat. Mission upload "
            "timeouts, endpoint connectivity, and Gazebo startup delay are "
            "engineering/runtime readiness issues, not operator-approved mission "
            "repair parameters. When evidence includes payload_weight or "
            "payload_margin_risk with takeoff/climb or upload-blocked symptoms, "
            "prefer a bounded payload_weight_kg reduction for the same route. "
            "If the best repair is an unsupported engineering action, say so in "
            "rationale and leave proposed_parameters empty so the UI will not "
            "expose an approval button for a fake action. "
            "Do not approve, create dispatch authority, claim gate passage, "
            "dispatch, physical execution, delivery completion, or progress."
        ),
    }


def guard_llm_repair_proposal(proposal: Mapping[str, Any]) -> dict[str, Any]:
    blocking_reasons: list[str] = []
    checks = {
        "schema_valid": False,
        "repair_target_allowed": False,
        "repair_actions_allowed": False,
        "llm_judgment_not_in_gate": False,
        "artifact_not_progress": False,
        "authority_not_self_granted": False,
        "proposed_parameters_supported": False,
    }
    validated: dict[str, Any] | None = None
    try:
        validated = validate_llm_repair_proposal(proposal)
        checks["schema_valid"] = True
    except MissionOSLLMSchemaValidationError as exc:
        blocking_reasons.append(str(exc))

    if validated is not None:
        repair_target = str(validated.get("repair_target") or "")
        checks["repair_target_allowed"] = repair_target in ALLOWED_REPAIR_TARGETS
        if not checks["repair_target_allowed"]:
            blocking_reasons.append("repair_target_not_allowed")
        actions = validated.get("repair_actions")
        action_types = [
            str(action.get("action_type") or "")
            for action in actions
            if isinstance(action, Mapping)
        ] if isinstance(actions, list) else []
        checks["repair_actions_allowed"] = bool(action_types) and all(
            action_type in ALLOWED_REPAIR_ACTION_TYPES for action_type in action_types
        )
        if not checks["repair_actions_allowed"]:
            blocking_reasons.append("repair_action_type_not_allowed")
        checks["llm_judgment_not_in_gate"] = (
            validated.get("llm_judgment_in_gate") is False
        )
        checks["artifact_not_progress"] = (
            validated.get("progress_counted") is False
            and validated.get("goal_640_progress_counted") is False
            and validated.get("ai_agent_progress_counted") is False
            and validated.get("drone_physics_affected") is False
            and validated.get("physical_execution_invoked") is False
        )
        checks["authority_not_self_granted"] = (
            validated.get("operator_approved") is False
            and validated.get("dispatch_authority_created") is False
        )
        proposed_parameters = validated.get("proposed_parameters") or {}
        parameter_keys = (
            {str(key) for key in proposed_parameters.keys()}
            if isinstance(proposed_parameters, Mapping)
            else set()
        )
        unsupported_keys = sorted(parameter_keys & UNSUPPORTED_AUTO_RETRY_PARAMETERS)
        if unsupported_keys:
            blocking_reasons.extend(
                f"unsupported_operator_repair_parameter:{key}"
                for key in unsupported_keys
            )
        unknown_keys = sorted(
            key
            for key in parameter_keys
            if key not in UI_ACTIONABLE_REPAIR_PARAMETERS
            and key not in UNSUPPORTED_AUTO_RETRY_PARAMETERS
        )
        if unknown_keys:
            blocking_reasons.extend(
                f"unknown_operator_repair_parameter:{key}"
                for key in unknown_keys
            )
        checks["proposed_parameters_supported"] = not unsupported_keys and not unknown_keys

    return {
        "schema_version": LLM_REPAIR_PLANNER_GUARDRAIL_SCHEMA_VERSION,
        "guardrail_passed": not blocking_reasons,
        "checks": checks,
        "blocking_reasons": blocking_reasons,
        "allowed_repair_targets": sorted(ALLOWED_REPAIR_TARGETS),
        "allowed_repair_action_types": sorted(ALLOWED_REPAIR_ACTION_TYPES),
        "ui_actionable_repair_parameters": sorted(UI_ACTIONABLE_REPAIR_PARAMETERS),
        "unsupported_auto_retry_parameters": sorted(UNSUPPORTED_AUTO_RETRY_PARAMETERS),
    }


def run_llm_repair_planner(
    *,
    evidence_artifact: Mapping[str, Any],
    evidence_artifact_path: str,
    evidence_artifact_sha256: str,
    artifact_root: Path | str,
    artifact_relative,
) -> dict[str, Any]:
    prompt = build_repair_planner_prompt(
        evidence_artifact=evidence_artifact,
        evidence_artifact_path=evidence_artifact_path,
        evidence_artifact_sha256=evidence_artifact_sha256,
    )
    prompt_text = json.dumps(prompt, sort_keys=True)

    if os.environ.get(LLM_REPAIR_PLANNER_ADK_ENABLED_ENV) == "1":
        return _run_adk_gemini_repair_planner(
            prompt_text=prompt_text,
            artifact_root=artifact_root,
            artifact_relative=artifact_relative,
            evidence_artifact_path=evidence_artifact_path,
            evidence_artifact_sha256=evidence_artifact_sha256,
        )

    command_text = os.environ.get(LLM_REPAIR_PLANNER_COMMAND_ENV, "").strip()
    if command_text:
        if os.environ.get(LLM_REPAIR_PLANNER_ALLOW_OVERRIDE_ENV) != "1":
            return _planner_result(
                status="blocked",
                blocking_reasons=[f"{LLM_REPAIR_PLANNER_ALLOW_OVERRIDE_ENV}_required"],
            )
        return _run_command_override_repair_planner(
            command_text=command_text,
            prompt_text=prompt_text,
            artifact_root=artifact_root,
            artifact_relative=artifact_relative,
            evidence_artifact_path=evidence_artifact_path,
            evidence_artifact_sha256=evidence_artifact_sha256,
        )

    return _planner_result(
        status="not_configured",
        blocking_reasons=[
            f"{LLM_REPAIR_PLANNER_ADK_ENABLED_ENV}_not_enabled",
            f"{LLM_REPAIR_PLANNER_COMMAND_ENV}_not_configured",
        ],
    )


def _run_adk_gemini_repair_planner(
    *,
    prompt_text: str,
    artifact_root: Path | str,
    artifact_relative,
    evidence_artifact_path: str,
    evidence_artifact_sha256: str,
) -> dict[str, Any]:
    from src.agents.model_config import llm_provider_label

    agent_name = "missionos_repair_planner_agent"
    model_id = _repair_model_id()
    started_at = datetime.now(timezone.utc)
    try:
        response_text = _invoke_adk_gemini_repair_text(
            prompt_text=prompt_text,
            model_id=model_id,
            timeout_seconds=_repair_timeout_seconds(),
        )
    except Exception as exc:  # pragma: no cover - live service failure shape varies.
        return _planner_result(
            status="blocked",
            blocking_reasons=[
                f"google_adk_gemini_repair_invocation_failed:{type(exc).__name__}"
            ],
        )
    completed_at = datetime.now(timezone.utc)
    raw_response = _read_json_object(response_text)
    if raw_response is None:
        return _planner_result(
            status="blocked",
            blocking_reasons=["google_adk_gemini_repair_response_not_json_object"],
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
    return _persist_guarded_repair_response(
        raw_response=raw_response,
        invocation_evidence=invocation_evidence,
        generated_at=completed_at.isoformat(),
        artifact_root=artifact_root,
        artifact_relative=artifact_relative,
        evidence_artifact_path=evidence_artifact_path,
        evidence_artifact_sha256=evidence_artifact_sha256,
    )


def _run_command_override_repair_planner(
    *,
    command_text: str,
    prompt_text: str,
    artifact_root: Path | str,
    artifact_relative,
    evidence_artifact_path: str,
    evidence_artifact_sha256: str,
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
            timeout=_repair_timeout_seconds(),
        )
        exit_code = int(process.returncode)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _planner_result(
            status="blocked",
            blocking_reasons=[
                f"llm_repair_planner_invocation_failed:{type(exc).__name__}"
            ],
        )
    completed_at = datetime.now(timezone.utc)
    if exit_code != 0:
        return _planner_result(
            status="blocked",
            blocking_reasons=["llm_repair_planner_exit_code_nonzero"],
            invocation_exit_code=exit_code,
            stderr_sha256=_sha256_text(stderr),
        )
    raw_response = _read_json_object(stdout)
    if raw_response is None:
        return _planner_result(
            status="blocked",
            blocking_reasons=["llm_repair_planner_stdout_not_json_object"],
            invocation_exit_code=exit_code,
            stdout_sha256=_sha256_text(stdout),
        )
    invocation_evidence = _llm_invocation_evidence(
        provider="command_override",
        invocation_kind="subprocess",
        model_id=os.environ.get(
            LLM_REPAIR_PLANNER_MODEL_ENV,
            "command_override_repair_planner",
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
    return _persist_guarded_repair_response(
        raw_response=raw_response,
        invocation_evidence=invocation_evidence,
        generated_at=completed_at.isoformat(),
        artifact_root=artifact_root,
        artifact_relative=artifact_relative,
        evidence_artifact_path=evidence_artifact_path,
        evidence_artifact_sha256=evidence_artifact_sha256,
    )


def _persist_guarded_repair_response(
    *,
    raw_response: Mapping[str, Any],
    invocation_evidence: Mapping[str, Any],
    generated_at: str,
    artifact_root: Path | str,
    artifact_relative,
    evidence_artifact_path: str,
    evidence_artifact_sha256: str,
) -> dict[str, Any]:
    forbidden_reasons = _raw_response_forbidden_authority_reasons(raw_response)
    if forbidden_reasons:
        guardrail = {
            "schema_version": LLM_REPAIR_PLANNER_GUARDRAIL_SCHEMA_VERSION,
            "guardrail_passed": False,
            "checks": {"raw_response_forbidden_authority_keys_absent": False},
            "blocking_reasons": forbidden_reasons,
        }
        return _planner_result(
            status="guardrail_blocked",
            blocking_reasons=forbidden_reasons,
            guardrail=guardrail,
        )
    proposal_id = f"missionos_llm_repair_proposal_{uuid.uuid4().hex[:12]}"
    proposal = {
        "schema_version": LLM_REPAIR_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "generated_at": generated_at,
        "input_evidence_artifact_path": evidence_artifact_path,
        "input_evidence_artifact_sha256": evidence_artifact_sha256,
        "repair_target": raw_response.get("repair_target"),
        "repair_actions": raw_response.get("repair_actions") or [],
        "rationale": raw_response.get("rationale"),
        "expected_outcome": raw_response.get("expected_outcome"),
        "uncertainty": raw_response.get("uncertainty"),
        "next_verification": raw_response.get("next_verification"),
        "proposed_operator_instruction": raw_response.get("proposed_operator_instruction"),
        "proposed_parameters": raw_response.get("proposed_parameters") or {},
        "llm_invocation_evidence": dict(invocation_evidence),
        "llm_judgment_in_gate": False,
        "progress_counted": False,
        "goal_640_progress_counted": False,
        "ai_agent_progress_counted": False,
        "drone_physics_affected": False,
        "physical_execution_invoked": False,
        "dispatch_authority_created": False,
        "operator_approved": False,
        "operator_approval_required": True,
    }
    guardrail = guard_llm_repair_proposal(proposal)
    if guardrail.get("guardrail_passed") is not True:
        return _planner_result(
            status="guardrail_blocked",
            blocking_reasons=list(guardrail.get("blocking_reasons") or []),
            guardrail=guardrail,
            proposal=proposal,
        )
    root = Path(artifact_root)
    proposal_dir = root / "missionos_llm_repair_proposal" / proposal_id
    proposal_dir.mkdir(parents=True, exist_ok=True)
    proposal_path = proposal_dir / "missionos_llm_repair_proposal.json"
    proposal_path.write_text(
        json.dumps(proposal, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return _planner_result(
        status="proposal_guardrail_passed",
        proposal=proposal,
        proposal_ref=f"missionos_llm_repair_proposal:{proposal_id}",
        proposal_artifact_path=artifact_relative(proposal_path),
        guardrail=guardrail,
    )


def _raw_response_forbidden_authority_reasons(raw_response: Mapping[str, Any]) -> list[str]:
    return [
        f"raw_llm_output_forbidden_authority_key:{key}"
        for key in sorted(RAW_REPAIR_FORBIDDEN_AUTHORITY_KEYS)
        if key in raw_response
    ]


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


def _repair_model_id() -> str:
    from src.agents.model_config import agent_model_label

    env_model = os.environ.get(LLM_REPAIR_PLANNER_MODEL_ENV, "").strip()
    try:
        from src.config.settings import get_settings

        fallback = str(get_settings().agent_model)
    except Exception:
        fallback = "gemini-3.1-flash-lite-preview"
    return agent_model_label(
        env_model or fallback,
        agent_name="missionos_repair_planner_agent",
    )


def _configure_google_adk_environment() -> None:
    from src.agents.model_config import google_llm_backend_enabled

    if not google_llm_backend_enabled("missionos_repair_planner_agent"):
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


def _invoke_adk_gemini_repair_text(
    *,
    prompt_text: str,
    model_id: str,
    timeout_seconds: int,
) -> str:
    return asyncio.run(
        asyncio.wait_for(
            _invoke_adk_gemini_repair_text_async(
                prompt_text=prompt_text,
                model_id=model_id,
            ),
            timeout=timeout_seconds,
        )
    )


async def _invoke_adk_gemini_repair_text_async(*, prompt_text: str, model_id: str) -> str:
    _configure_google_adk_environment()
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.genai import types

    from src.runtime.session_service import create_session_service

    instruction = (
        "You are the MissionOS Repair Planner. Return only one JSON object. "
        "You may diagnose the blocked evidence and propose repair_target, "
        "repair_actions, rationale, expected_outcome, uncertainty, and "
        "next_verification. You must also provide proposed_operator_instruction "
        "and proposed_parameters. repair_actions must be an array of objects with "
        "action_type and description string fields. You must not approve, create dispatch authority, "
        "claim gate passage, dispatch, physical execution, delivery completion, "
        "or progress. Only payload_weight_kg and route coordinates are actionable "
        "operator repair parameters. Do not propose wind changes or mission upload "
        "timeout changes as an operator-approved retry; describe them only as "
        "diagnostic or engineering readiness issues when relevant."
    )
    from src.agents.model_config import resolve_agent_model

    agent = LlmAgent(
        name="missionos_repair_planner",
        model=resolve_agent_model(
            model_id,
            agent_name="missionos_repair_planner_agent",
        ),
        instruction=instruction,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.0,
            responseMimeType="application/json",
        ),
    )
    app_name = "missionos_llm_repair_planner"
    user_id = "missionos_operator"
    session_service = create_session_service()
    session = await session_service.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)
    content = types.Content(
        role="user",
        parts=[
            types.Part(
                text=(
                    "Given this MissionOS blocked or completed evidence, propose "
                    "one bounded repair plan as JSON only:\n"
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


def _repair_timeout_seconds() -> int:
    value = os.environ.get(LLM_REPAIR_PLANNER_TIMEOUT_ENV)
    try:
        parsed = int(value) if value is not None else DEFAULT_LLM_REPAIR_PLANNER_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_LLM_REPAIR_PLANNER_TIMEOUT_SECONDS
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
        "schema_version": LLM_REPAIR_PLANNER_RESULT_SCHEMA_VERSION,
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
        "physical_execution_invoked": False,
        "dispatch_authority_created": False,
        "operator_approval_required": True,
        "llm_judgment_in_gate": False,
    }


__all__ = [
    "ALLOWED_REPAIR_ACTION_TYPES",
    "ALLOWED_REPAIR_TARGETS",
    "LLM_REPAIR_PLANNER_ADK_ENABLED_ENV",
    "LLM_REPAIR_PLANNER_ALLOW_OVERRIDE_ENV",
    "LLM_REPAIR_PLANNER_COMMAND_ENV",
    "LLM_REPAIR_PLANNER_GUARDRAIL_SCHEMA_VERSION",
    "LLM_REPAIR_PLANNER_RESULT_SCHEMA_VERSION",
    "build_repair_planner_prompt",
    "guard_llm_repair_proposal",
    "run_llm_repair_planner",
]
