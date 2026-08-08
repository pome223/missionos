#!/usr/bin/env python3
"""Exercise canonical ADK v2 HITL through a real loopback Gateway.

The smoke uses a public-safe Form 2A fixture and a real Redis backend. It
records human approval through the existing MissionOS HTTP route, then resumes
the ADK checkpoint with the exact canonical approval_ref. It invokes no
executor, simulator, or hardware.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
from typing import Any
import uuid

import httpx
import uvicorn


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_form2a_fixture(root: Path) -> str:
    from src.gateway.missionos_knowledge_sharing import (
        FORM2A_OPERATOR_APPROVAL_TOKEN_SCHEMA_VERSION,
        FORM2A_RESPONSE_SELECTION_SCHEMA_VERSION,
    )

    fixture_id = uuid.uuid4().hex[:12]
    source_path = root / "fixture_source" / "form1.json"
    selection_path = (
        root
        / f"missionos_form2a_response_selection_fixture_{fixture_id}"
        / "missionos_form2a_response_selection.json"
    )
    token_path = (
        root
        / f"missionos_form2a_operator_approval_token_fixture_{fixture_id}"
        / "missionos_form2a_operator_approval_token.json"
    )
    _write_json(
        source_path,
        {
            "schema_version": "missionos_adk_v2_hitl_gateway_source_fixture.v1",
            "fixture_only": True,
            "physical_execution_invoked": False,
            "progress_counted": False,
        },
    )
    approval_ref = f"missionos_form2a_operator_approval_token:{fixture_id}"
    selection_ref = f"missionos_form2a_response_selection:{fixture_id}"
    bounded_action_ref = f"missionos_bounded_action:{fixture_id}"
    dispatch_ref = f"missionos_pending_dispatch:{fixture_id}"
    _write_json(
        selection_path,
        {
            "schema_version": FORM2A_RESPONSE_SELECTION_SCHEMA_VERSION,
            "response_selection_id": fixture_id,
            "selection_status": "selected",
            "input_form1_artifact_path": source_path.as_posix(),
            "input_form1_artifact_sha256": _sha256(source_path),
            "source_check": {"source_supported": True},
            "form2a_response_selected_in_artifact": True,
            "form2a_response_selected_in_runtime": False,
            "mission_response_kind": "action",
            "selected_response_kind": "operator_gated_fixture_action",
            "operator_approval_required": True,
            "operator_approval_token_issued_in_artifact": True,
            "operator_approval_token_consumed_in_runtime": False,
            "approval_ref": approval_ref,
            "approval_artifact_path": token_path.as_posix(),
            "bounded_action_ref": bounded_action_ref,
            "dispatch_ref": dispatch_ref,
            "dispatch_executed_in_runtime": False,
            "automatic_dispatch_executed": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        },
    )
    _write_json(
        token_path,
        {
            "schema_version": FORM2A_OPERATOR_APPROVAL_TOKEN_SCHEMA_VERSION,
            "approval_token_id": fixture_id,
            "approval_token_status": "issued_unconsumed",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
            "response_selection_ref": selection_ref,
            "response_selection_artifact_path": selection_path.as_posix(),
            "operator_approval_token_issued_in_artifact": True,
            "operator_approval_token_consumed_in_runtime": False,
            "approval_ref": approval_ref,
            "bounded_action_ref": bounded_action_ref,
            "dispatch_ref": dispatch_ref,
            "automatic_dispatch_executed": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        },
    )
    return approval_ref


def _configure_runtime(tmp: Path) -> None:
    if not os.environ.get("REDIS_URL", "").strip():
        raise RuntimeError("adk_v2_hitl_gateway_smoke_requires_REDIS_URL")
    os.environ["MISSIONOS_ADK_V2_HITL_ENABLED"] = "1"
    os.environ["MISSIONOS_LLM_BACKEND"] = "off"
    os.environ["TASK_STORE_DB_PATH"] = str(tmp / "tasks.db")
    os.environ["MEMORY_DB_PATH"] = str(tmp / "memory.db")
    os.environ["AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["COMPUTER_TRAJECTORY_DB_PATH"] = str(
        tmp / "computer_trajectories.db"
    )
    os.environ["PHYSICAL_AI_VALIDATION_DB_PATH"] = str(
        tmp / "physical_ai_validation.db"
    )


async def _wait_for_health(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
        for _ in range(100):
            with suppress(httpx.HTTPError):
                response = await client.get("/health")
                if response.status_code == 200:
                    return
            await asyncio.sleep(0.05)
    raise TimeoutError(f"Gateway did not become healthy: {base_url}")


def _assert_authority_floor(result: dict[str, Any]) -> None:
    for field in (
        "approval_created",
        "dispatch_authority_created",
        "executor_invoked",
        "physical_execution_invoked",
        "outcome_observed",
        "progress_counted",
    ):
        if result.get(field) is not False:
            raise RuntimeError(f"adk_v2_hitl_authority_floor_failed:{field}")


async def _cleanup_session(
    gateway: Any,
    *,
    operator_session_id: str,
    adk_session_id: str,
) -> None:
    from src.intelligence.missionos_adk_v2_hitl import (
        MISSIONOS_ADK_V2_HITL_APP_NAME,
    )
    from src.runtime.session_service import create_session_service

    service = create_session_service(gateway.settings, require_redis=True)
    try:
        await service.delete_session(
            app_name=MISSIONOS_ADK_V2_HITL_APP_NAME,
            user_id=operator_session_id,
            session_id=adk_session_id,
        )
    finally:
        close = getattr(service._client, "aclose", None)
        if callable(close):
            await close()


async def _run() -> dict[str, Any]:
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="missionos-adk-v2-hitl-gateway-") as raw:
        tmp = Path(raw)
        _configure_runtime(tmp)
        os.chdir(tmp)
        operator_session_id = f"operator_gateway_{uuid.uuid4().hex[:12]}"
        adk_session_id = ""
        server_task: asyncio.Task[None] | None = None
        server: uvicorn.Server | None = None
        try:
            approval_ref = _write_form2a_fixture(
                Path("output/mission_designer_behavior_delta_audits")
            )
            from src.config.settings import reset_settings
            from src.gateway.server import create_missionos_gateway
            from src.runtime.task_store import reset_task_store

            reset_settings()
            reset_task_store()
            gateway = create_missionos_gateway()
            port = _free_port()
            base_url = f"http://127.0.0.1:{port}"
            server = uvicorn.Server(
                uvicorn.Config(
                    gateway.app,
                    host="127.0.0.1",
                    port=port,
                    log_level="error",
                    lifespan="on",
                )
            )
            server_task = asyncio.create_task(server.serve())
            await _wait_for_health(base_url)
            async with httpx.AsyncClient(base_url=base_url, timeout=20.0) as client:
                start = await client.post(
                    "/missionos/adk-v2/hitl/form2a-approval/start",
                    json={"operator_session_id": operator_session_id},
                )
                start.raise_for_status()
                paused = start.json()
                adk_session_id = str(paused["adk_session_id"])
                if paused.get("hitl_status") != "awaiting_canonical_approval":
                    raise RuntimeError("adk_v2_hitl_gateway_pause_failed")
                _assert_authority_floor(paused)

                yes = await client.post(
                    "/missionos/adk-v2/hitl/form2a-approval/resume",
                    json={
                        "operator_session_id": operator_session_id,
                        "adk_session_id": adk_session_id,
                        "approval_ref": "yes",
                    },
                )
                yes_result = yes.json()
                if yes.status_code != 409 or yes_result.get("resume_attempted") is not False:
                    raise RuntimeError("adk_v2_hitl_gateway_yes_not_blocked")
                _assert_authority_floor(yes_result)

                review = await client.post(
                    "/missionos/form2a-operator-review/approve"
                )
                review.raise_for_status()
                if review.json().get("summary_status") != "approved":
                    raise RuntimeError("adk_v2_hitl_gateway_human_review_failed")

                resume = await client.post(
                    "/missionos/adk-v2/hitl/form2a-approval/resume",
                    json={
                        "operator_session_id": operator_session_id,
                        "adk_session_id": adk_session_id,
                        "approval_ref": approval_ref,
                    },
                )
                resume.raise_for_status()
                resumed = resume.json()
                if resumed.get("canonical_approval_validated") is not True:
                    raise RuntimeError("adk_v2_hitl_gateway_resume_failed")
                _assert_authority_floor(resumed)

            await _cleanup_session(
                gateway,
                operator_session_id=operator_session_id,
                adk_session_id=adk_session_id,
            )
            return {
                "schema_version": "missionos_adk_v2_hitl_gateway_smoke.v1",
                "verification_status": "passed",
                "transport": "loopback_http",
                "session_backend": resumed["session_backend"],
                "pause_status": paused["hitl_status"],
                "yes_status": yes_result["hitl_status"],
                "yes_resume_attempted": yes_result["resume_attempted"],
                "human_review_status": "approved",
                "resume_status": resumed["hitl_status"],
                "canonical_approval_validated": True,
                "approval_created_by_adk": False,
                "approval_token_consumed": False,
                "dispatch_authority_created": False,
                "executor_invoked": False,
                "physical_execution_invoked": False,
                "outcome_observed": False,
                "progress_counted": False,
                "redis_session_cleaned_up": True,
            }
        finally:
            if server is not None:
                server.should_exit = True
            if server_task is not None:
                await asyncio.wait_for(server_task, timeout=10.0)
            os.chdir(original_cwd)


def main() -> int:
    print(json.dumps(asyncio.run(_run()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
