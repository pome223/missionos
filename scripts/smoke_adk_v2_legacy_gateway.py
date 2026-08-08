#!/usr/bin/env python3
"""Loopback HTTP smoke for the ADK v2 legacy Gateway conversation graph."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import json
import os
from pathlib import Path
import socket
import tempfile

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types
import httpx
import uvicorn


class StaticLlm(BaseLlm):
    response_text: str

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=self.response_text)],
            ),
            partial=False,
        )


def _agent(name: str, response_text: str) -> LlmAgent:
    return LlmAgent(
        name=name,
        model=StaticLlm(model=f"static-{name}", response_text=response_text),
        instruction="Return the configured deterministic fixture response.",
    )


def _loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _wait_until_started(server: uvicorn.Server) -> None:
    for _ in range(100):
        if server.started:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("legacy_gateway_loopback_start_timeout")


async def _run_smoke() -> None:
    with tempfile.TemporaryDirectory(prefix="missionos-adk-v2-gateway-") as temp_dir:
        runtime_dir = Path(temp_dir)
        os.environ["TASK_STORE_DB_PATH"] = str(runtime_dir / "tasks.db")
        os.environ["MEMORY_DB_PATH"] = str(runtime_dir / "memory.db")
        os.environ["AUDIT_LOG_PATH"] = str(runtime_dir / "audit.log")
        os.environ["GATEWAY_LEGACY_AGENT_ROUTES_ENABLED"] = "1"
        os.environ.pop("GATEWAY_API_KEY", None)
        os.environ.pop("REDIS_URL", None)

        from src.config.settings import reset_settings
        from src.gateway.server import (
            _ADK_V2_GATEWAY_CONVERSATION_RESULT_SCHEMA_VERSION,
            create_legacy_agent_gateway,
        )
        from src.runtime.task_store import reset_task_store
        from src.security import audit

        reset_settings()
        reset_task_store()
        audit._audit_logger = None
        gateway = create_legacy_agent_gateway()
        gateway.gateway_routing_agent = _agent(
            "routing_agent",
            json.dumps(
                {
                    "target": "root_agent",
                    "specialist": None,
                    "handoff_mode": "direct",
                    "reason": "deterministic smoke route",
                    "confidence": 0.99,
                    "dynamic_agent": {
                        "instruction": "",
                        "mcp_servers": [],
                        "mode": "run",
                    },
                }
            ),
        )
        gateway.gateway_root_agent = _agent(
            "boiled_claw",
            "ADK v2 legacy Gateway smoke response",
        )
        gateway.gateway_specialist_agents = {}

        port = _loopback_port()
        server = uvicorn.Server(
            uvicorn.Config(
                gateway.app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                access_log=False,
            )
        )
        server_task = asyncio.create_task(server.serve())
        try:
            await _wait_until_started(server)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                timeout=10.0,
            ) as client:
                response = await client.post(
                    "/agent/run",
                    json={
                        "user_id": "smoke_operator",
                        "session_id": "smoke_adk_v2_legacy_gateway",
                        "message": "hello from the deterministic Gateway smoke",
                    },
                )
                response.raise_for_status()
                payload = response.json()

            session = await gateway.session_service.get_session(
                app_name="boiled-claw",
                user_id="smoke_operator",
                session_id="smoke_adk_v2_legacy_gateway",
            )
            if session is None:
                raise RuntimeError("legacy_gateway_smoke_session_missing")
            node_paths = [
                str(getattr(getattr(event, "node_info", None), "path", "") or "")
                for event in session.events
            ]
            terminal_outputs = [
                dict(event.output)
                for event in session.events
                if isinstance(event.output, dict)
                and event.output.get("schema_version")
                == _ADK_V2_GATEWAY_CONVERSATION_RESULT_SCHEMA_VERSION
            ]
            terminal = terminal_outputs[-1] if terminal_outputs else {}
            required_paths = (
                "/routing_agent@routing-agent",
                "/boiled_claw@root-agent",
            )
            if (
                payload.get("response") != "ADK v2 legacy Gateway smoke response"
                or not all(
                    any(required in path for path in node_paths) for required in required_paths
                )
                or not terminal
            ):
                raise RuntimeError("legacy_gateway_agents_not_executed_as_v2_children")

            print(
                json.dumps(
                    {
                        "schema_version": "missionos_adk_v2_legacy_gateway_smoke.v1",
                        "status": "passed",
                        "http_status": response.status_code,
                        "response_type": payload.get("type"),
                        "workflow_name": terminal.get("workflow_name"),
                        "workflow_engine": terminal.get("workflow_engine"),
                        "agent_nodes_as_dynamic_children": True,
                        "fixture_only": True,
                        "approval_created": terminal.get("approval_created"),
                        "dispatch_authority_created": terminal.get("dispatch_authority_created"),
                        "external_execution_invoked": terminal.get("external_execution_invoked"),
                        "physical_execution_invoked": terminal.get("physical_execution_invoked"),
                        "node_completion_counts_progress": terminal.get(
                            "node_completion_counts_progress"
                        ),
                        "node_paths": [path for path in node_paths if path],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        finally:
            server.should_exit = True
            await server_task
            reset_task_store()
            reset_settings()
            audit._audit_logger = None


if __name__ == "__main__":
    asyncio.run(_run_smoke())
