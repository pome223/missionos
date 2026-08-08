from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.memory import InMemoryMemoryService
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import node
from google.genai import types

import src.control_loop.root_workflow as root_workflow
from src.control_loop.callbacks import repair_callback
from src.runtime.state_keys import StateKeys


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


def _node_paths(events: list[Any]) -> list[str]:
    paths: list[str] = []
    for event in events:
        node_info = getattr(event, "node_info", None)
        path = str(getattr(node_info, "path", "") or "")
        if path:
            paths.append(path)
    return paths


def _install_pass_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    @node(name="planner", rerun_on_resume=True)
    def planner(ctx: Any, node_input: Any) -> dict[str, bool]:
        ctx.state[StateKeys.PLAN_APPROVED] = {
            "plan_id": "plan_fixture_v2",
            "steps": [],
        }
        ctx.state[StateKeys.APPROVAL_STATUS] = "policy_approved"
        return {"planned": True}

    @node(name="executor", rerun_on_resume=True)
    def executor(ctx: Any, node_input: Any) -> dict[str, bool]:
        ctx.state[StateKeys.TEMP_EXECUTOR_OUTPUTS] = {"status": "ok"}
        return {"executed": True}

    @node(name="verifier", rerun_on_resume=True)
    def verifier(ctx: Any, node_input: Any) -> dict[str, bool]:
        ctx.state[StateKeys.VERIFY_LAST_REPORT] = {
            "report_id": "report_fixture_v2",
            "status": "pass",
            "overall_score": 1.0,
            "summary": "fixture verification passed",
        }
        return {"verified": True}

    monkeypatch.setattr(root_workflow, "planner_with_policy", planner)
    monkeypatch.setattr(root_workflow, "executor_with_tools", executor)
    monkeypatch.setattr(root_workflow, "verifier_with_hooks", verifier)


def _install_fixture_services(
    loop: root_workflow.ControlLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def prepare_verification_state(**kwargs: Any) -> dict[str, Any]:
        ctx = kwargs["workflow_ctx"]
        verification_inputs = {"artifact_refs": [], "fixture": True}
        ctx.state[StateKeys.TEMP_VERIFICATION_INPUTS] = verification_inputs
        return verification_inputs

    async def promote_memories(**kwargs: Any) -> list[str]:
        return []

    monkeypatch.setattr(loop, "_prepare_verification_state", prepare_verification_state)
    monkeypatch.setattr(loop, "_promote_memories", promote_memories)


def test_control_loop_runs_agents_as_adk_v2_dynamic_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_pass_nodes(monkeypatch)
    session_service = InMemorySessionService()
    loop = root_workflow.ControlLoop(
        session_service=session_service,
        memory_service=InMemoryMemoryService(),
    )
    _install_fixture_services(loop, monkeypatch)

    async def scenario():
        result = await loop.run(
            goal="exercise the v2 control loop",
            user_id="operator_fixture",
            session_id="control_loop_fixture",
        )
        session = await session_service.get_session(
            app_name=root_workflow._APP_NAME,
            user_id="operator_fixture",
            session_id="control_loop_fixture",
        )
        return result, session

    result, session = asyncio.run(scenario())

    assert result.success is True
    assert result.plan_id == "plan_fixture_v2"
    assert result.verification_report_id == "report_fixture_v2"
    assert result.metadata["adk_workflow_name"] == "missionos_control_loop_v2"
    assert result.metadata["adk_workflow_engine"] == "v2_dynamic"
    assert result.metadata["node_completion_is_external_execution"] is False
    assert result.metadata["node_completion_counts_progress"] is False

    assert session is not None
    paths = _node_paths(session.events)
    dynamic_parent = "missionos_control_loop_v2@1/orchestrate_control_loop@1"
    assert any(f"{dynamic_parent}/planner@planner-attempt-0" in p for p in paths)
    assert any(f"{dynamic_parent}/executor@executor-attempt-0" in p for p in paths)
    assert any(f"{dynamic_parent}/verifier@verifier-attempt-0" in p for p in paths)
    assert root_workflow._latest_agent_invocation_id(session.events, "executor")


def test_control_loop_v2_stops_before_executor_for_human_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @node(name="fixture_approval_planner", rerun_on_resume=True)
    def planner(ctx: Any, node_input: Any) -> dict[str, bool]:
        ctx.state[StateKeys.PLAN_APPROVED] = {
            "plan_id": "plan_needs_human",
            "steps": [],
        }
        ctx.state[StateKeys.APPROVAL_STATUS] = "needs_human"
        ctx.state[StateKeys.APPROVAL_REQUEST] = {"request_id": "approval_fixture"}
        return {"planned": True}

    @node(name="fixture_must_not_execute", rerun_on_resume=True)
    def must_not_execute(ctx: Any, node_input: Any) -> dict[str, bool]:
        raise AssertionError("executor or verifier ran before human approval")

    monkeypatch.setattr(root_workflow, "planner_with_policy", planner)
    monkeypatch.setattr(root_workflow, "executor_with_tools", must_not_execute)
    monkeypatch.setattr(root_workflow, "verifier_with_hooks", must_not_execute)
    session_service = InMemorySessionService()
    loop = root_workflow.ControlLoop(
        session_service=session_service,
        memory_service=InMemoryMemoryService(),
    )

    result = asyncio.run(
        loop.run(
            goal="require approval",
            user_id="operator_fixture",
            session_id="approval_fixture",
        )
    )

    assert result.success is False
    assert result.metadata["needs_human"] is True
    assert result.metadata["approval_request"] == {"request_id": "approval_fixture"}
    assert result.metadata["adk_workflow_node_sequence"] == [
        "orchestrate_control_loop",
        "planner[0]",
    ]


def test_control_loop_v2_reenters_only_after_missionos_human_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner_calls: list[str] = []

    @node(name="planner", rerun_on_resume=True)
    def planner(ctx: Any, node_input: Any) -> dict[str, bool]:
        planner_calls.append("planner")
        ctx.state[StateKeys.PLAN_APPROVED] = {
            "plan_id": "plan_approved_on_second_run",
            "steps": [],
        }
        ctx.state[StateKeys.APPROVAL_STATUS] = "needs_human"
        ctx.state[StateKeys.APPROVAL_REQUEST] = {"request_id": "approval_second_run"}
        return {"planned": True}

    @node(name="executor", rerun_on_resume=True)
    def executor(ctx: Any, node_input: Any) -> dict[str, bool]:
        ctx.state[StateKeys.TEMP_EXECUTOR_OUTPUTS] = {"status": "ok"}
        return {"executed": True}

    @node(name="verifier", rerun_on_resume=True)
    def verifier(ctx: Any, node_input: Any) -> dict[str, bool]:
        ctx.state[StateKeys.VERIFY_LAST_REPORT] = {
            "report_id": "report_after_human_approval",
            "status": "pass",
            "overall_score": 1.0,
            "summary": "executed only after MissionOS approval",
        }
        return {"verified": True}

    monkeypatch.setattr(root_workflow, "planner_with_policy", planner)
    monkeypatch.setattr(root_workflow, "executor_with_tools", executor)
    monkeypatch.setattr(root_workflow, "verifier_with_hooks", verifier)
    session_service = InMemorySessionService()
    loop = root_workflow.ControlLoop(
        session_service=session_service,
        memory_service=InMemoryMemoryService(),
    )
    _install_fixture_services(loop, monkeypatch)

    async def scenario():
        pending = await loop.run(
            goal="execute after approval",
            user_id="operator_fixture",
            session_id="approval_reentry_fixture",
        )
        approved = await loop.resolve_human_approval(
            user_id="operator_fixture",
            session_id="approval_reentry_fixture",
            approved=True,
            request_id="approval_second_run",
        )
        completed = await loop.run(
            goal="execute after approval",
            user_id="operator_fixture",
            session_id="approval_reentry_fixture",
        )
        return pending, approved, completed

    pending, approved, completed = asyncio.run(scenario())

    assert pending.metadata["needs_human"] is True
    assert approved is True
    assert completed.success is True
    assert completed.verification_report_id == "report_after_human_approval"
    assert planner_calls == ["planner"]
    assert completed.metadata["adk_workflow_node_sequence"] == [
        "orchestrate_control_loop",
        "executor[0]",
        "verification_prep[0]",
        "verifier[0]",
        "memory_promotion",
    ]


def test_control_loop_resume_rechecks_judgment_without_replaying_executor() -> None:
    assert root_workflow.planner_with_policy.rerun_on_resume is True
    assert root_workflow.executor_with_tools.rerun_on_resume is False
    assert root_workflow.verifier_with_hooks.rerun_on_resume is True


def test_control_loop_runs_llm_agents_as_v2_nodes_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner_text = json.dumps(
        {
            "plan_id": "plan_llm_node_fixture",
            "goal": "fixture LLM workflow",
            "risk_level": "low",
            "required_capabilities": [],
            "steps": [],
            "success_criteria": [],
        }
    )
    executor_text = json.dumps({"status": "ok", "artifacts": []})
    verifier_text = json.dumps(
        {
            "report_id": "report_llm_node_fixture",
            "status": "pass",
            "overall_score": 1.0,
            "summary": "LLM Agent nodes completed under the v2 workflow",
            "criterion_results": [],
            "repair_actions": [],
        }
    )
    monkeypatch.setattr(
        root_workflow,
        "planner_with_policy",
        root_workflow.planner_with_policy.model_copy(
            update={
                "model": StaticLlm(
                    model="static-planner",
                    response_text=planner_text,
                )
            }
        ),
    )
    monkeypatch.setattr(
        root_workflow,
        "executor_with_tools",
        root_workflow.executor_with_tools.model_copy(
            update={
                "model": StaticLlm(
                    model="static-executor",
                    response_text=executor_text,
                ),
                "tools": [],
            }
        ),
    )
    monkeypatch.setattr(
        root_workflow,
        "verifier_with_hooks",
        root_workflow.verifier_with_hooks.model_copy(
            update={
                "model": StaticLlm(
                    model="static-verifier",
                    response_text=verifier_text,
                ),
                "after_agent_callback": repair_callback,
            }
        ),
    )
    session_service = InMemorySessionService()
    loop = root_workflow.ControlLoop(
        session_service=session_service,
        memory_service=InMemoryMemoryService(),
    )

    async def promote_memories(**kwargs: Any) -> list[str]:
        return []

    monkeypatch.setattr(loop, "_promote_memories", promote_memories)

    async def scenario():
        result = await loop.run(
            goal="fixture LLM workflow",
            user_id="llm_fixture_operator",
            session_id="llm_agent_nodes_fixture",
        )
        session = await session_service.get_session(
            app_name=root_workflow._APP_NAME,
            user_id="llm_fixture_operator",
            session_id="llm_agent_nodes_fixture",
        )
        return result, session

    result, session = asyncio.run(scenario())

    assert result.success is True
    assert result.plan_id == "plan_llm_node_fixture"
    assert session is not None
    paths = _node_paths(session.events)
    assert any("/planner@planner-attempt-0" in path for path in paths)
    assert any("/executor@executor-attempt-0" in path for path in paths)
    assert any("/verifier@verifier-attempt-0" in path for path in paths)
