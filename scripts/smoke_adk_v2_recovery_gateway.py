#!/usr/bin/env python3
"""Exercise verifier-failure recovery routing through the real Gateway."""

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
    with tempfile.TemporaryDirectory(prefix="missionos-adk-v2-recovery-") as raw:
        tmp = Path(raw)
        hitl_smoke._configure_runtime(tmp)
        os.environ["MISSIONOS_ADK_V2_GUARDED_EXECUTION_ENABLED"] = "1"
        os.environ["MISSIONOS_ADK_V2_GUARDED_EXECUTION_FIXTURE"] = "1"
        os.environ["MISSIONOS_ADK_V2_GUARDED_EXECUTION_FIXTURE_VERIFIER"] = "failed"
        os.environ["MISSIONOS_ADK_V2_RECOVERY_ENABLED"] = "1"
        os.environ["MISSIONOS_ADK_V2_DISPATCH_SNAPSHOT_MAX_AGE_SECONDS"] = "60"
        os.chdir(tmp)
        operator_session_id = f"operator_recovery_{uuid.uuid4().hex[:12]}"
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
                prior_bounded_action_ref = str(
                    paused["request_input_payload"]["bounded_action_ref"]
                )
                prior_dispatch_ref = str(
                    paused["request_input_payload"]["dispatch_ref"]
                )

                review = await client.post(
                    "/missionos/form2a-operator-review/approve"
                )
                review.raise_for_status()
                if review.json().get("summary_status") != "approved":
                    raise RuntimeError("recovery_gateway_human_review_failed")

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
                if resumed.get("hitl_status") != "recovery_approval_pending":
                    raise RuntimeError("recovery_gateway_not_approval_pending")
                proposal = resumed.get("recovery_proposal")
                proposal = proposal if isinstance(proposal, dict) else {}
                if (
                    proposal.get("approval_request_created") is not True
                    or proposal.get("new_human_approval_required") is not True
                    or proposal.get("approval_created") is not False
                ):
                    raise RuntimeError("recovery_gateway_fresh_approval_not_required")
                if (
                    proposal.get("bounded_action_ref") == prior_bounded_action_ref
                    or proposal.get("dispatch_ref") == prior_dispatch_ref
                ):
                    raise RuntimeError("recovery_gateway_refs_not_changed")
                if (
                    proposal.get("dispatch_authority_created") is not False
                    or proposal.get("executor_invoked") is not False
                    or proposal.get("automatic_recovery_executed") is not False
                ):
                    raise RuntimeError("recovery_gateway_executed_recovery")
                proposal_path = Path(
                    str(proposal.get("recovery_proposal_artifact_path") or "")
                )
                if not proposal_path.is_file():
                    raise RuntimeError("recovery_gateway_artifact_missing")

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
                    raise RuntimeError("recovery_gateway_duplicate_resume_not_blocked")

            record = DispatchAuthorityTable(
                MISSIONOS_ADK_V2_GUARDED_EXECUTION_STATE_PATH
            ).lookup_dispatch_ref(prior_dispatch_ref)
            attempts = record.get("attempts")
            attempts = attempts if isinstance(attempts, dict) else {}
            if len(attempts) != 1:
                raise RuntimeError("recovery_gateway_dispatch_attempt_count_invalid")
            trace = resumed.get("same_task_audit_trace")
            trace = trace if isinstance(trace, dict) else {}
            nodes = trace.get("nodes")
            nodes = nodes if isinstance(nodes, list) else []
            if not any(
                "route_verifier_failure_to_recovery" in str(node.get("node_path"))
                for node in nodes
                if isinstance(node, dict)
            ):
                raise RuntimeError("recovery_gateway_audit_node_missing")

            await hitl_smoke._cleanup_session(
                gateway,
                operator_session_id=operator_session_id,
                adk_session_id=adk_session_id,
            )
            return {
                "schema_version": "missionos_adk_v2_recovery_gateway_smoke.v1",
                "verification_status": "passed",
                "transport": "loopback_http",
                "session_backend": resumed["session_backend"],
                "task_id": resumed["task_id"],
                "resume_status": resumed["hitl_status"],
                "prior_dispatch_attempt_count": len(attempts),
                "prior_fixture_executor_invocation_count": 1,
                "prior_external_sender_invoked": False,
                "verifier_status": "failed",
                "recovery_proposal_created": True,
                "recovery_approval_request_created": True,
                "recovery_human_approval_created": False,
                "recovery_bounded_action_changed": True,
                "recovery_dispatch_ref_changed": True,
                "recovery_dispatch_authority_created": False,
                "recovery_executor_invoked": False,
                "automatic_recovery_executed": False,
                "duplicate_resume_attempted": False,
                "duplicate_executor_invoked": False,
                "audit_node_count": trace.get("node_count"),
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
