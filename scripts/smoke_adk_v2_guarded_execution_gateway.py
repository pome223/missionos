#!/usr/bin/env python3
"""Exercise ADK v2 guarded fixture execution through the real Gateway."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import uuid

import httpx
import uvicorn

from scripts import smoke_adk_v2_hitl_gateway as hitl_smoke


async def _run() -> dict[str, Any]:
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(
        prefix="missionos-adk-v2-guarded-gateway-"
    ) as raw:
        tmp = Path(raw)
        hitl_smoke._configure_runtime(tmp)
        os.environ["MISSIONOS_ADK_V2_GUARDED_EXECUTION_ENABLED"] = "1"
        os.environ["MISSIONOS_ADK_V2_GUARDED_EXECUTION_FIXTURE"] = "1"
        os.environ["MISSIONOS_ADK_V2_DISPATCH_SNAPSHOT_MAX_AGE_SECONDS"] = "60"
        os.chdir(tmp)
        operator_session_id = f"operator_guarded_{uuid.uuid4().hex[:12]}"
        adk_session_id = ""
        server_task: asyncio.Task[None] | None = None
        server: uvicorn.Server | None = None
        try:
            approval_ref = hitl_smoke._write_form2a_fixture(
                Path("output/mission_designer_behavior_delta_audits")
            )
            from src.config.settings import reset_settings
            from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
            from src.gateway.server import create_missionos_gateway
            from src.intelligence.missionos_adk_v2_guarded_execution import (
                MISSIONOS_ADK_V2_GUARDED_EXECUTION_STATE_PATH,
                build_form2a_guarded_execution_handler,
            )
            from src.runtime.task_store import reset_task_store

            reset_settings()
            reset_task_store()
            gateway = create_missionos_gateway()
            port = hitl_smoke._free_port()
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
            await hitl_smoke._wait_for_health(base_url)
            async with httpx.AsyncClient(base_url=base_url, timeout=20.0) as client:
                start = await client.post(
                    "/missionos/adk-v2/hitl/form2a-approval/start",
                    json={"operator_session_id": operator_session_id},
                )
                start.raise_for_status()
                paused = start.json()
                adk_session_id = str(paused["adk_session_id"])
                dispatch_ref = str(paused["request_input_payload"]["dispatch_ref"])

                yes = await client.post(
                    "/missionos/adk-v2/hitl/form2a-approval/resume",
                    json={
                        "operator_session_id": operator_session_id,
                        "adk_session_id": adk_session_id,
                        "approval_ref": "yes",
                    },
                )
                if yes.status_code != 409 or yes.json().get("resume_attempted") is not False:
                    raise RuntimeError("guarded_gateway_yes_not_blocked_before_resume")

                review = await client.post(
                    "/missionos/form2a-operator-review/approve"
                )
                review.raise_for_status()
                if review.json().get("summary_status") != "approved":
                    raise RuntimeError("guarded_gateway_human_review_failed")

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
                if resumed.get("hitl_status") != "guarded_execution_completed":
                    raise RuntimeError("guarded_gateway_execution_not_completed")
                if resumed.get("dispatch_authority_created") is not True:
                    raise RuntimeError("guarded_gateway_dispatch_authority_missing")
                if resumed.get("executor_invoked") is not True:
                    raise RuntimeError("guarded_gateway_fixture_executor_not_invoked")
                if resumed.get("physical_execution_invoked") is not False:
                    raise RuntimeError("guarded_gateway_physical_execution_claimed")
                trace = resumed.get("same_task_audit_trace")
                trace = trace if isinstance(trace, dict) else {}
                trace_refs = trace.get("artifact_refs")
                trace_refs = trace_refs if isinstance(trace_refs, dict) else {}
                if not trace.get("task_id") or trace_refs.get("dispatch_ref") != dispatch_ref:
                    raise RuntimeError("guarded_gateway_same_task_trace_invalid")

                duplicate = await client.post(
                    "/missionos/adk-v2/hitl/form2a-approval/resume",
                    json={
                        "operator_session_id": operator_session_id,
                        "adk_session_id": adk_session_id,
                        "approval_ref": approval_ref,
                    },
                )
                duplicate_result = duplicate.json()
                if (
                    duplicate.status_code != 409
                    or duplicate_result.get("resume_attempted") is not False
                    or duplicate_result.get("executor_invoked") is not False
                ):
                    raise RuntimeError("guarded_gateway_duplicate_resume_not_blocked")

            handler = build_form2a_guarded_execution_handler()
            replay = await handler(resumed)
            if replay.get("guarded_execution_status") != "receipt_replayed":
                raise RuntimeError("guarded_gateway_receipt_not_replayed")
            if replay.get("executor_invoked") is not False:
                raise RuntimeError("guarded_gateway_receipt_replay_reinvoked_executor")
            record = DispatchAuthorityTable(
                MISSIONOS_ADK_V2_GUARDED_EXECUTION_STATE_PATH
            ).lookup_dispatch_ref(dispatch_ref)
            attempts = record.get("attempts")
            attempts = attempts if isinstance(attempts, dict) else {}
            if len(attempts) != 1 or not record.get("receipt"):
                raise RuntimeError("guarded_gateway_idempotency_record_invalid")

            await hitl_smoke._cleanup_session(
                gateway,
                operator_session_id=operator_session_id,
                adk_session_id=adk_session_id,
            )
            return {
                "schema_version": "missionos_adk_v2_guarded_gateway_smoke.v1",
                "verification_status": "passed",
                "transport": "loopback_http",
                "session_backend": resumed["session_backend"],
                "human_review_status": "approved",
                "resume_status": resumed["hitl_status"],
                "canonical_approval_validated": True,
                "dispatch_authority_created": True,
                "dispatch_attempt_count": len(attempts),
                "executor_invocation_count": 1,
                "fixture_execution_boundary_invoked": True,
                "external_sender_invoked": False,
                "duplicate_resume_attempted": duplicate_result["resume_attempted"],
                "duplicate_executor_invoked": duplicate_result["executor_invoked"],
                "receipt_replayed": True,
                "same_task_audit_trace": True,
                "audit_task_id": trace["task_id"],
                "audit_node_count": trace["node_count"],
                "automatic_redispatch_performed": False,
                "ack_observed": False,
                "outcome_observed": False,
                "verifier_passed": False,
                "completion_claimed": False,
                "physical_execution_invoked": False,
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
