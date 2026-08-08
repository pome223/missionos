from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import json
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types


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


class BrowserFailureLlm(BaseLlm):
    call_count: int = 0

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        self.call_count += 1
        if self.call_count == 1:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                id="browser-call",
                                name="browser_navigate",
                                args={"url": "https://example.test"},
                            )
                        )
                    ],
                ),
                partial=False,
            )
            return
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text="browser result received")],
            ),
            partial=False,
        )


def _static_agent(name: str, response_text: str) -> LlmAgent:
    return LlmAgent(
        name=name,
        model=StaticLlm(model=f"static-{name}", response_text=response_text),
        instruction="Return the configured fixture response.",
    )


def _route_response(
    *,
    target: str,
    specialist: str | None = None,
    handoff_mode: str = "direct",
) -> str:
    return json.dumps(
        {
            "target": target,
            "specialist": specialist,
            "handoff_mode": handoff_mode,
            "reason": "fixture route",
            "confidence": 0.99,
            "dynamic_agent": {
                "instruction": "",
                "mcp_servers": [],
                "mode": "run",
            },
        }
    )


@pytest.fixture
def legacy_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from src.config.settings import reset_settings
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setenv("GATEWAY_LEGACY_AGENT_ROUTES_ENABLED", "1")
    reset_settings()
    reset_task_store()
    audit._audit_logger = None

    from src.gateway.server import create_legacy_agent_gateway

    gateway = create_legacy_agent_gateway()
    yield gateway
    reset_task_store()
    reset_settings()
    audit._audit_logger = None


async def _run_workflow(
    gateway: Any,
    *,
    message: str = "hello",
):
    session = await gateway._get_or_create_gateway_session(
        user_id="operator_fixture",
        session_id="gateway_v2_fixture",
    )
    return await gateway._run_gateway_conversation_workflow(
        session_id=session.id,
        user_id="operator_fixture",
        message=message,
        route_message=message,
        source="test",
        request_id="request_fixture",
        emit_grounding_tool_events=False,
    )


def test_gateway_root_route_runs_router_and_root_as_v2_children(
    legacy_gateway: Any,
) -> None:
    legacy_gateway.gateway_routing_agent = _static_agent(
        "routing_agent",
        _route_response(target="root_agent"),
    )
    legacy_gateway.gateway_root_agent = _static_agent(
        "boiled_claw",
        "root fixture response",
    )
    legacy_gateway.gateway_specialist_agents = {}

    result = asyncio.run(_run_workflow(legacy_gateway))

    assert result.response_kind == "agent_message"
    assert result.response_text == "root fixture response"
    assert result.workflow_name == "missionos_legacy_gateway_conversation_v2"
    assert result.workflow_engine == "v2_dynamic"
    assert any("/routing_agent@routing-agent" in path for path in result.node_paths)
    assert any("/boiled_claw@root-agent" in path for path in result.node_paths)
    assert result.approval_created is False
    assert result.dispatch_authority_created is False
    assert result.external_execution_invoked is False
    assert result.physical_execution_invoked is False
    assert result.node_completion_counts_progress is False
    assert not hasattr(legacy_gateway, "runner")
    assert not hasattr(legacy_gateway, "routing_runner")
    assert not hasattr(legacy_gateway, "specialist_runners")


def test_gateway_resume_rechecks_route_without_replaying_agent_tools(
    legacy_gateway: Any,
) -> None:
    from src.gateway.server import SpecialistPrepassResult

    legacy_gateway.gateway_routing_agent = _static_agent(
        "routing_agent",
        _route_response(target="root_agent"),
    )
    legacy_gateway.gateway_root_agent = _static_agent(
        "boiled_claw",
        "root fixture response",
    )
    legacy_gateway.gateway_specialist_agents = {
        "file_manager": _static_agent("file_manager", "specialist fixture")
    }

    router = legacy_gateway._gateway_router_node(output_key="temp:router")
    specialist = legacy_gateway._gateway_specialist_node(
        specialist_name="file_manager",
        output_key="temp:specialist",
        result=SpecialistPrepassResult(),
        message="inspect a file",
    )
    root = legacy_gateway._gateway_root_node(
        output_key="temp:root",
        runtime_context="fixture context",
    )

    assert router.rerun_on_resume is True
    assert specialist is not None
    assert specialist.rerun_on_resume is False
    assert root.rerun_on_resume is False


def test_gateway_preflight_specialist_and_root_share_one_v2_workflow(
    legacy_gateway: Any,
) -> None:
    legacy_gateway.gateway_routing_agent = _static_agent(
        "routing_agent",
        _route_response(
            target="specialist",
            specialist="file_manager",
            handoff_mode="preflight_then_root",
        ),
    )
    legacy_gateway.gateway_specialist_agents = {
        "file_manager": _static_agent(
            "file_manager",
            "specialist fixture evidence",
        )
    }
    legacy_gateway.gateway_root_agent = _static_agent(
        "boiled_claw",
        "root synthesized fixture response",
    )

    result = asyncio.run(_run_workflow(legacy_gateway, message="inspect a file"))

    assert result.response_kind == "agent_message"
    assert result.response_text == "root synthesized fixture response"
    assert result.specialist_prepass.text == "specialist fixture evidence"
    assert any("/routing_agent@routing-agent" in path for path in result.node_paths)
    assert any("/file_manager@specialist-agent" in path for path in result.node_paths)
    assert any("/boiled_claw@root-agent" in path for path in result.node_paths)


def test_gateway_direct_specialist_stops_before_root(
    legacy_gateway: Any,
) -> None:
    legacy_gateway.gateway_routing_agent = _static_agent(
        "routing_agent",
        _route_response(
            target="specialist",
            specialist="system_operator",
        ),
    )
    legacy_gateway.gateway_specialist_agents = {
        "system_operator": _static_agent(
            "system_operator",
            "direct specialist fixture response",
        )
    }
    legacy_gateway.gateway_root_agent = _static_agent(
        "boiled_claw",
        "root must not run",
    )

    result = asyncio.run(_run_workflow(legacy_gateway, message="inspect the system"))

    assert result.response_kind == "specialist"
    assert result.response_text == "direct specialist fixture response"
    assert any("/system_operator@specialist-agent" in path for path in result.node_paths)
    assert not any("/boiled_claw@root-agent" in path for path in result.node_paths)


def test_gateway_browser_infrastructure_failure_blocks_root(
    legacy_gateway: Any,
) -> None:
    def browser_navigate(url: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "Playwright is not installed in this execution environment",
            "url": url,
        }

    legacy_gateway.gateway_routing_agent = _static_agent(
        "routing_agent",
        _route_response(
            target="specialist",
            specialist="browser_automator",
            handoff_mode="preflight_then_root",
        ),
    )
    legacy_gateway.gateway_specialist_agents = {
        "browser_automator": LlmAgent(
            name="browser_automator",
            model=BrowserFailureLlm(model="browser-failure-fixture"),
            instruction="Call browser_navigate and then report its result.",
            tools=[browser_navigate],
        )
    }
    legacy_gateway.gateway_root_agent = _static_agent(
        "boiled_claw",
        "root must not run after infrastructure failure",
    )

    result = asyncio.run(_run_workflow(legacy_gateway, message="open a web page"))

    assert result.response_kind == "error"
    assert result.ok is False
    assert result.specialist_prepass.infrastructure_blocked is True
    assert "Playwright is not installed" in result.response_text
    assert not any("/boiled_claw@root-agent" in path for path in result.node_paths)


def test_gateway_control_loop_route_does_not_create_execution_authority(
    legacy_gateway: Any,
) -> None:
    legacy_gateway.gateway_routing_agent = _static_agent(
        "routing_agent",
        _route_response(target="control_loop"),
    )
    legacy_gateway.gateway_root_agent = _static_agent(
        "boiled_claw",
        "root must not run",
    )
    legacy_gateway.gateway_specialist_agents = {}

    result = asyncio.run(_run_workflow(legacy_gateway, message="run a complex task"))

    assert result.decision.target == "control_loop"
    assert result.response_kind == "control_loop"
    assert not any("/boiled_claw@root-agent" in path for path in result.node_paths)
    assert result.approval_created is False
    assert result.dispatch_authority_created is False
    assert result.external_execution_invoked is False
    assert result.node_completion_counts_progress is False


def test_gateway_route_only_entrypoint_runs_router_as_v2_child(
    legacy_gateway: Any,
) -> None:
    legacy_gateway.gateway_routing_agent = _static_agent(
        "routing_agent",
        _route_response(
            target="specialist",
            specialist="memory_keeper",
        ),
    )

    async def scenario():
        decision = await legacy_gateway._select_route_for_message(
            session_id="cron_requester_fixture",
            user_id="operator_fixture",
            message="remember this fixture",
            source="cron",
        )
        sessions = await legacy_gateway.routing_session_service.list_sessions(
            app_name="boiled-claw-router",
            user_id="operator_fixture",
        )
        stored_session = await legacy_gateway.routing_session_service.get_session(
            app_name="boiled-claw-router",
            user_id="operator_fixture",
            session_id=sessions.sessions[-1].id,
        )
        return decision, stored_session

    decision, session = asyncio.run(scenario())
    assert session is not None
    paths = [
        str(getattr(getattr(event, "node_info", None), "path", "") or "")
        for event in session.events
    ]

    assert decision.target == "specialist"
    assert decision.specialist == "memory_keeper"
    assert any(
        "missionos_legacy_gateway_router_v2@1/"
        "orchestrate_gateway_route@1/routing_agent@routing-agent" in path
        for path in paths
    ), paths
