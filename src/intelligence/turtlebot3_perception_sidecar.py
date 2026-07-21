"""Gemini-backed VLM sidecar producing perception claim payloads (issue #31).

Takes a camera frame, hashes it, and asks Gemini (via ADK) or an operator-
provided command to classify it into one of ``PerceptionClaimKind``. Output
is a source-bound claim plus a normalized horizontal target center that
``build_perception_claim_from_camera_observation`` in
``src/runtime/perception_claim.py`` consumes.

The sidecar never emits ``corroborated_by`` — even if a raw model response
includes one, it is dropped before the result is returned. Corroboration is
computed downstream from a same-window LaserScan candidate and source-bound
runtime invocation evidence, never from anything a vision pipeline claims
about itself; see ``perception_claim.py`` for why.

Mirrors ``turtlebot3_recovery_planner.py``'s two backend paths so it shares
the same tested, opt-in posture: ADK/Gemini (live, uncovered by the fast
test suite — same as every other Gemini-backed capability here) and an
operator-provided command override (subprocess, base64-encoded image on
stdin, exercised by the fast test suite via a fixture script).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, get_args

from src.runtime.perception_claim import PerceptionClaimKind

TURTLEBOT3_PERCEPTION_SIDECAR_RESULT_SCHEMA_VERSION = (
    "missionos_turtlebot3_perception_sidecar_result.v1"
)

TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV = (
    "MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND"
)
TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV = (
    "MISSIONOS_ALLOW_TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_OVERRIDE"
)
TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV = (
    "MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED"
)
TURTLEBOT3_PERCEPTION_SIDECAR_TIMEOUT_ENV = (
    "MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_TIMEOUT_SECONDS"
)
TURTLEBOT3_PERCEPTION_SIDECAR_MODEL_ENV = (
    "MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_MODEL_ID"
)
DEFAULT_TURTLEBOT3_PERCEPTION_SIDECAR_TIMEOUT_SECONDS = 60

_AGENT_NAME = "missionos_turtlebot3_perception_sidecar_agent"
# Derived from PerceptionClaimKind (perception_claim.py) so the sidecar's
# allowed vocabulary can never drift from the schema that consumes it.
_ALLOWED_CLAIM_KINDS = frozenset(get_args(PerceptionClaimKind))
_ALLOWED_HORIZONTAL_SECTORS = frozenset({"left", "center", "right", "unknown"})


def _sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


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


def build_turtlebot3_perception_sidecar_prompt(
    *,
    image_sha256: str,
    include_image_base64: str | None = None,
) -> dict[str, Any]:
    """Build the source-bound JSON prompt for Gemini/command-override.

    ``include_image_base64`` is only set for the command-override (subprocess
    stdin) path; the ADK path attaches the image as a native inline_data
    Part instead of embedding it in this JSON prompt.
    """

    prompt: dict[str, Any] = {
        "schema_version": "missionos_turtlebot3_perception_sidecar_prompt.v1",
        "task": "classify_camera_frame_for_recovery_perception_claim",
        "image_sha256": image_sha256,
        "image_mime_type": "image/png",
        "allowed_claim_kinds": sorted(_ALLOWED_CLAIM_KINDS),
        "required_output_fields": [
            "claim_kind",
            "confidence",
            "horizontal_sector",
            "target_center_x_normalized",
        ],
        "strict_output_contract": (
            "Return exactly one JSON object with claim_kind (one of "
            "allowed_claim_kinds), confidence (a float between 0.0 and 1.0), "
            "and horizontal_sector (left, center, right, or unknown), plus "
            "target_center_x_normalized (0.0 at the left image edge, 1.0 at "
            "the right edge, or null when no obstacle target is visible). Do not "
            "include corroborated_by, source_frame_ref, "
            "approval, dispatch, or any execution-authority field — those "
            "are computed downstream by MissionOS, never by this sidecar."
        ),
    }
    if include_image_base64 is not None:
        prompt["image_base64"] = include_image_base64
    return prompt


def _sidecar_result(
    *,
    status: str,
    blocking_reasons: list[str] | None = None,
    claim: dict[str, Any] | None = None,
    invocation_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": TURTLEBOT3_PERCEPTION_SIDECAR_RESULT_SCHEMA_VERSION,
        "sidecar_status": status,
        "blocking_reasons": list(blocking_reasons or []),
        "camera_observation": dict(claim or {}),
        "llm_invocation_evidence": dict(invocation_evidence or {}),
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _validate_and_strip_claim(
    raw_output: dict[str, Any],
    *,
    source_frame_ref: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    claim_kind = str(raw_output.get("claim_kind") or "")
    if claim_kind not in _ALLOWED_CLAIM_KINDS:
        reasons.append("claim_kind_not_allowed")
    confidence = raw_output.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        reasons.append("confidence_not_numeric")
    elif not (0.0 <= float(confidence) <= 1.0):
        reasons.append("confidence_out_of_range")
    horizontal_sector = str(raw_output.get("horizontal_sector") or "unknown")
    if horizontal_sector not in _ALLOWED_HORIZONTAL_SECTORS:
        reasons.append("horizontal_sector_not_allowed")
    raw_target_center = raw_output.get("target_center_x_normalized")
    if raw_target_center is None:
        target_center_x_normalized: float | None = None
    elif isinstance(raw_target_center, bool) or not isinstance(
        raw_target_center, (int, float)
    ):
        target_center_x_normalized = None
        reasons.append("target_center_x_normalized_not_numeric_or_null")
    elif not (0.0 <= float(raw_target_center) <= 1.0):
        target_center_x_normalized = None
        reasons.append("target_center_x_normalized_out_of_range")
    else:
        target_center_x_normalized = float(raw_target_center)
    if claim_kind != "path_clear" and target_center_x_normalized is None:
        reasons.append("obstacle_claim_target_center_required")
    if reasons:
        return None, reasons
    # Only evidence fields pass through. A raw
    # response cannot inject corroborated_by or any other field — those
    # are computed downstream, never taken from the vision pipeline.
    return (
        {
            "claim_kind": claim_kind,
            "source_frame_ref": source_frame_ref,
            "confidence": float(confidence),
            "horizontal_sector": horizontal_sector,
            "target_center_x_normalized": target_center_x_normalized,
        },
        [],
    )


def run_turtlebot3_perception_sidecar(
    *,
    image_path: str | Path,
) -> dict[str, Any]:
    """Classify a camera frame into a camera_observation payload, or block."""

    image_bytes = Path(image_path).read_bytes()
    image_sha256 = _sha256_bytes(image_bytes)
    source_frame_ref = f"sha256:{image_sha256}"

    if os.environ.get(TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV, "") == "1":
        return _run_adk_sidecar(
            image_bytes=image_bytes,
            image_sha256=image_sha256,
            source_frame_ref=source_frame_ref,
        )

    command_text = os.environ.get(
        TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV, ""
    ).strip()
    if command_text:
        if (
            os.environ.get(TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV)
            != "1"
        ):
            return _sidecar_result(
                status="blocked",
                blocking_reasons=[
                    f"{TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV}_required"
                ],
            )
        return _run_command_override_sidecar(
            command_text=command_text,
            image_bytes=image_bytes,
            image_sha256=image_sha256,
            source_frame_ref=source_frame_ref,
        )

    return _sidecar_result(
        status="not_configured",
        blocking_reasons=[
            f"{TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV}_not_enabled",
            f"{TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV}_not_configured",
        ],
    )


def _sidecar_timeout_seconds() -> int:
    raw = os.environ.get(TURTLEBOT3_PERCEPTION_SIDECAR_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_TURTLEBOT3_PERCEPTION_SIDECAR_TIMEOUT_SECONDS
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_TURTLEBOT3_PERCEPTION_SIDECAR_TIMEOUT_SECONDS


def _sidecar_model_id() -> str:
    from src.agents.model_config import agent_model_label

    env_model = os.environ.get(TURTLEBOT3_PERCEPTION_SIDECAR_MODEL_ENV, "").strip()
    try:
        from src.config.settings import get_settings

        fallback = str(get_settings().agent_model)
    except Exception:
        fallback = "gemini-3.1-flash-lite-preview"
    return agent_model_label(env_model or fallback, agent_name=_AGENT_NAME)


def _runtime_invocation_evidence(
    *,
    invocation_kind: str,
    invocation_target: str,
    provider: str,
    model_id: str,
    image_sha256: str,
    prompt_text: str,
    stdout: str,
    stderr: str,
    started_at: datetime,
    completed_at: datetime,
    exit_code: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "runtime_invocation_evidence.v1",
        "invocation_kind": invocation_kind,
        "invocation_target": invocation_target,
        "provider": provider,
        "model_id": model_id,
        "input_image_sha256": image_sha256,
        "prompt_sha256": _sha256_text(prompt_text),
        "invocation_started_at": started_at.isoformat(),
        "invocation_completed_at": completed_at.isoformat(),
        "invocation_stdout_sha256": _sha256_text(stdout),
        "invocation_stderr_sha256": _sha256_text(stderr),
        "invocation_stdout_preimage": stdout,
        "invocation_stderr_preimage": stderr,
        "invocation_exit_code": exit_code,
        "physical_execution_invoked": False,
    }
    payload.update(extra or {})
    ref_payload = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    payload["invocation_ref"] = f"vlm_invocation:{_sha256_text(ref_payload)[:16]}"
    return payload


def _run_command_override_sidecar(
    *,
    command_text: str,
    image_bytes: bytes,
    image_sha256: str,
    source_frame_ref: str,
) -> dict[str, Any]:
    prompt = build_turtlebot3_perception_sidecar_prompt(
        image_sha256=image_sha256,
        include_image_base64=base64.b64encode(image_bytes).decode("ascii"),
    )
    prompt_text = json.dumps(prompt, sort_keys=True)
    command_argv = shlex.split(command_text)
    started_at = datetime.now(timezone.utc)
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
            timeout=_sidecar_timeout_seconds(),
        )
        exit_code = int(process.returncode)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _sidecar_result(
            status="blocked",
            blocking_reasons=[
                f"turtlebot3_perception_sidecar_invocation_failed:"
                f"{type(exc).__name__}"
            ],
        )
    completed_at = datetime.now(timezone.utc)
    if exit_code != 0:
        return _sidecar_result(
            status="blocked",
            blocking_reasons=["turtlebot3_perception_sidecar_exit_code_nonzero"],
        )
    raw_output = _read_json_object(stdout)
    if raw_output is None:
        return _sidecar_result(
            status="blocked",
            blocking_reasons=[
                "turtlebot3_perception_sidecar_stdout_not_json_object"
            ],
        )
    claim, reasons = _validate_and_strip_claim(
        raw_output, source_frame_ref=source_frame_ref
    )
    invocation_evidence = _runtime_invocation_evidence(
        invocation_kind="subprocess",
        invocation_target="turtlebot3_perception_sidecar_command_override",
        provider="command_override",
        model_id="operator_provided_command",
        image_sha256=image_sha256,
        prompt_text=prompt_text,
        stdout=stdout,
        stderr=stderr,
        started_at=started_at,
        completed_at=completed_at,
        exit_code=exit_code,
        extra={"command_argv": list(command_argv)},
    )
    if reasons:
        return _sidecar_result(
            status="blocked",
            blocking_reasons=reasons,
            invocation_evidence=invocation_evidence,
        )
    return _sidecar_result(
        status="classified",
        claim=claim,
        invocation_evidence=invocation_evidence,
    )


def _run_adk_sidecar(
    *,
    image_bytes: bytes,
    image_sha256: str,
    source_frame_ref: str,
) -> dict[str, Any]:
    model_id = _sidecar_model_id()
    started_at = datetime.now(timezone.utc)
    try:
        response_text = asyncio.run(
            asyncio.wait_for(
                _invoke_adk_perception_response_async(
                    image_bytes=image_bytes,
                    image_sha256=image_sha256,
                    model_id=model_id,
                ),
                timeout=_sidecar_timeout_seconds(),
            )
        )
    except Exception as exc:  # pragma: no cover - live backend failure shape varies.
        return _sidecar_result(
            status="blocked",
            blocking_reasons=[
                f"adk_perception_sidecar_invocation_failed:{type(exc).__name__}"
            ],
        )
    completed_at = datetime.now(timezone.utc)
    raw_output = _read_json_object(response_text)
    prompt_text = json.dumps(
        build_turtlebot3_perception_sidecar_prompt(image_sha256=image_sha256),
        sort_keys=True,
    )
    invocation_evidence = _runtime_invocation_evidence(
        invocation_kind="llm_api",
        invocation_target=f"google_adk:{model_id}",
        provider="google_adk",
        model_id=model_id,
        image_sha256=image_sha256,
        prompt_text=prompt_text,
        stdout=response_text,
        stderr="",
        started_at=started_at,
        completed_at=completed_at,
        exit_code=0,
    )
    if raw_output is None:
        return _sidecar_result(
            status="blocked",
            blocking_reasons=["adk_perception_sidecar_response_not_json_object"],
            invocation_evidence=invocation_evidence,
        )
    claim, reasons = _validate_and_strip_claim(
        raw_output, source_frame_ref=source_frame_ref
    )
    if reasons:
        return _sidecar_result(
            status="blocked",
            blocking_reasons=reasons,
            invocation_evidence=invocation_evidence,
        )
    return _sidecar_result(
        status="classified",
        claim=claim,
        invocation_evidence=invocation_evidence,
    )


async def _invoke_adk_perception_response_async(
    *,
    image_bytes: bytes,
    image_sha256: str,
    model_id: str,
) -> str:
    from src.intelligence.turtlebot3_recovery_planner import (
        _configure_google_adk_environment,
    )

    _configure_google_adk_environment()
    from src.agents.model_config import configure_google_vertex_location

    configure_google_vertex_location(model_id, agent_name=_AGENT_NAME)
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.genai import types

    from src.agents.model_config import resolve_agent_model
    from src.runtime.session_service import create_session_service

    instruction = (
        "You are the MissionOS TurtleBot3 perception sidecar. You are shown "
        "one camera frame. Return exactly one JSON object and no markdown, "
        "with claim_kind (one of the values listed in the prompt's "
        "allowed_claim_kinds), confidence (0.0-1.0), horizontal_sector "
        "(left, center, right, or unknown), and target_center_x_normalized "
        "(0.0 at the left edge, 1.0 at the right edge, or null only when no "
        "obstacle target is visible). Do not include "
        "corroborated_by, source_frame_ref, approval, dispatch, or any "
        "execution-authority field; those are computed downstream, never "
        "by you."
    )
    agent = LlmAgent(
        name="missionos_turtlebot3_perception_sidecar",
        model=resolve_agent_model(model_id, agent_name=_AGENT_NAME),
        instruction=instruction,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.0,
            responseMimeType="application/json",
        ),
    )
    app_name = "missionos_turtlebot3_perception_sidecar"
    user_id = "missionos_operator"
    session_service = create_session_service()
    session = await session_service.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)
    prompt = build_turtlebot3_perception_sidecar_prompt(image_sha256=image_sha256)
    content = types.Content(
        role="user",
        parts=[
            types.Part(text=json.dumps(prompt, sort_keys=True)),
            types.Part(inline_data={"mime_type": "image/png", "data": image_bytes}),
        ],
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


__all__ = [
    "DEFAULT_TURTLEBOT3_PERCEPTION_SIDECAR_TIMEOUT_SECONDS",
    "TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED_ENV",
    "TURTLEBOT3_PERCEPTION_SIDECAR_ALLOW_OVERRIDE_ENV",
    "TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_ENV",
    "TURTLEBOT3_PERCEPTION_SIDECAR_MODEL_ENV",
    "TURTLEBOT3_PERCEPTION_SIDECAR_RESULT_SCHEMA_VERSION",
    "TURTLEBOT3_PERCEPTION_SIDECAR_TIMEOUT_ENV",
    "build_turtlebot3_perception_sidecar_prompt",
    "run_turtlebot3_perception_sidecar",
]
