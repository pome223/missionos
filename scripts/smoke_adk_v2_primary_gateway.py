#!/usr/bin/env python3
"""Verify default ADK v2 proposal primary through a real loopback Gateway.

This opt-in smoke uses the configured hosted/local model backend. It performs
proposal routing only and must not create approval, dispatch, execution,
observed-effect, or progress authority.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import socket
import tempfile
from typing import Any

import httpx
import uvicorn


OPT_IN_ENV = "RUN_MISSIONOS_ADK_V2_PRIMARY_GATEWAY_SMOKE"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_health(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
        for _ in range(100):
            with suppress(httpx.HTTPError):
                response = await client.get("/health")
                if response.status_code == 200:
                    return
            await asyncio.sleep(0.05)
    raise TimeoutError(f"Gateway did not become healthy: {base_url}")


def _configure_temp_paths(tmp: Path) -> None:
    os.environ["TASK_STORE_DB_PATH"] = str(tmp / "tasks.db")
    os.environ["MEMORY_DB_PATH"] = str(tmp / "memory.db")
    os.environ["AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["COMPUTER_TRAJECTORY_DB_PATH"] = str(tmp / "computer.db")
    os.environ["PHYSICAL_AI_VALIDATION_DB_PATH"] = str(tmp / "physical.db")


async def _run() -> dict[str, Any]:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run the primary Gateway smoke")

    # Unset the mode controls to prove that v2 primary is the default. Model
    # backend and credentials remain caller-owned.
    for key in (
        "MISSIONOS_ADK_V2_GRAPH_PRIMARY",
        "MISSIONOS_ADK_V2_GRAPH_ROLLBACK",
        "MISSIONOS_ADK_V2_GRAPH_SHADOW",
    ):
        os.environ.pop(key, None)
    os.environ["MISSIONOS_AGENT_RUNTIME_ADK_ENABLED"] = "1"

    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="missionos-adk-v2-primary-gateway-") as raw:
        tmp = Path(raw)
        _configure_temp_paths(tmp)
        os.chdir(tmp)
        server: uvicorn.Server | None = None
        server_task: asyncio.Task[None] | None = None
        try:
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
            async with httpx.AsyncClient(base_url=base_url, timeout=180.0) as client:
                response = await client.post(
                    "/missionos/autonomy-conversation/run",
                    json={
                        "text": (
                            "Report the current mission status as a proposal only. "
                            "Do not approve, dispatch, execute, or count progress."
                        ),
                    },
                )
                response.raise_for_status()
                payload = response.json()

            runtime = payload.get("missionos_agent_runtime")
            runtime = runtime if isinstance(runtime, dict) else {}
            graph = runtime.get("adk_v2_graph_result")
            graph = graph if isinstance(graph, dict) else {}
            invocations = runtime.get("agent_invocations")
            invocations = invocations if isinstance(invocations, list) else []
            authority_fields = (
                "approval_created",
                "dispatch_authority_created",
                "executor_invoked",
                "physical_execution_invoked",
                "outcome_observed",
                "progress_counted",
            )
            if runtime.get("runtime_status") != "proposal_guardrail_passed":
                raise RuntimeError(
                    "primary_gateway_proposal_blocked:"
                    + ",".join(str(item) for item in runtime.get("blocking_reasons") or [])
                )
            if runtime.get("workflow_execution_mode") != "adk_v2_graph_primary":
                raise RuntimeError("primary_gateway_did_not_use_v2_graph")
            if len(invocations) != 3 or not all(
                item.get("agent_node_execution") == "ctx.run_node"
                and item.get("workflow_child_node") is True
                and item.get("standalone_runner_invoked") is False
                for item in invocations
                if isinstance(item, dict)
            ):
                raise RuntimeError("primary_gateway_agent_children_invalid")
            if any(runtime.get(field) is not False for field in authority_fields):
                raise RuntimeError("primary_gateway_crossed_authority_floor")

            return {
                "schema_version": "missionos_adk_v2_primary_gateway_smoke.v1",
                "status": "passed",
                "transport": "loopback_http",
                "http_status": response.status_code,
                "workflow_name": graph.get("workflow_name"),
                "workflow_execution_mode": runtime.get("workflow_execution_mode"),
                "agent_sequence": [
                    str(item.get("agent_name") or "")
                    for item in invocations
                    if isinstance(item, dict)
                ],
                "agent_nodes_as_dynamic_children": True,
                "node_paths": list(graph.get("workflow_node_paths") or []),
                "model_backend": os.getenv("MISSIONOS_LLM_BACKEND", "deepseek"),
                **{field: False for field in authority_fields},
            }
        finally:
            if server is not None:
                server.should_exit = True
            if server_task is not None:
                await asyncio.wait_for(server_task, timeout=10.0)
            os.chdir(original_cwd)


def main() -> int:
    print(json.dumps(asyncio.run(_run()), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
