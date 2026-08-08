from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types


from src.intelligence import missionos_adk_v2_shadow_graph as shadow_graph
from src.intelligence import missionos_agent_runtime as agent_runtime


class StaticGraphLlm(BaseLlm):
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


def _invocation(
    agent_name: str,
    output: dict[str, Any],
    *,
    passed: bool = True,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "agent_name": agent_name,
        "validated_output": output if passed else {},
        "guardrail_result": {
            "guardrail_passed": passed,
            "blocking_reasons": list(reasons or []),
            "validated_output": output if passed else {},
        },
        "workflow_execution_mode": "adk_v2_graph_shadow",
        "adk_v2_graph_invoked": True,
        "progress_counted": False,
    }


def _primary_result() -> dict[str, Any]:
    return {
        "schema_version": "missionos_agent_runtime_result.v1",
        "runtime_status": "proposal_guardrail_passed",
        "blocking_reasons": [],
        "proposal": {
            "intent": "status",
            "specialist_agent": "missionos_situation_judge_agent",
            "requires_human_approval": True,
            "safety_critic_output": {
                "boundary_status": "needs_human_approval",
            },
        },
        "agent_invocations": [{}, {}, {}],
        "progress_counted": False,
    }


def _shadow_result() -> dict[str, Any]:
    return {
        "schema_version": shadow_graph.MISSIONOS_ADK_V2_SHADOW_RESULT_SCHEMA_VERSION,
        "graph_runtime_status": "proposal_guardrail_passed",
        "blocking_reasons": [],
        "proposal": {
            "intent": "status",
            "specialist_agent": "missionos_situation_judge_agent",
            "requires_human_approval": True,
            "safety_critic_output": {
                "boundary_status": "needs_human_approval",
            },
        },
        "agent_invocations": [{}, {}, {}],
        "workflow_name": shadow_graph.MISSIONOS_ADK_V2_SHADOW_WORKFLOW_NAME,
        "graph_node_sequence": [
            "normalize_shadow_input",
            "invoke_shadow_chief",
            "invoke_shadow_specialist",
            "invoke_shadow_safety_critic",
            "finalize_shadow_proposal",
        ],
        "session_backend": "in_memory_shadow_only",
        "retry_policy": "disabled",
        "measurement_only": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "executor_invoked": False,
        "physical_execution_invoked": False,
        "outcome_observed": False,
        "progress_counted": False,
    }


def test_adk_v2_shadow_graph_runs_chief_specialist_and_critic_without_authority(
    monkeypatch,
) -> None:
    outputs = {
        "missionos_chief_agent": {
            "intent": "status",
            "operator_instruction": "Check current mission status.",
            "requires_human_approval": False,
        },
        "missionos_situation_judge_agent": {
            "intent": "status",
            "operator_instruction": "Hold for operator review.",
            "requires_human_approval": True,
        },
        "missionos_safety_critic_agent": {
            "intent": "status",
            "boundary_status": "needs_human_approval",
            "requires_human_approval": True,
        },
    }
    calls: list[dict[str, Any]] = []

    async def fake_run_agent_once_async(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        name = kwargs["agent_name"]
        return _invocation(name, outputs[name])

    monkeypatch.setattr(
        agent_runtime,
        "_run_agent_once_async",
        fake_run_agent_once_async,
    )

    result = shadow_graph.run_missionos_conversation_shadow_graph(
        utterance="What is the mission status?",
        missionos_state={"task_id": "task_shadow_fixture"},
    )

    assert result["graph_runtime_status"] == "proposal_guardrail_passed"
    assert result["proposal"]["intent"] == "status"
    assert result["proposal"]["specialist_agent"] == ("missionos_situation_judge_agent")
    assert result["proposal"]["safety_critic_output"]["boundary_status"] == ("needs_human_approval")
    assert result["graph_node_sequence"] == [
        "normalize_shadow_input",
        "invoke_shadow_chief",
        "invoke_shadow_specialist",
        "invoke_shadow_safety_critic",
        "finalize_shadow_proposal",
    ]
    assert result["session_backend"] == "in_memory_shadow_only"
    assert result["retry_policy"] == "disabled"
    assert result["measurement_only"] is True
    assert result["approval_created"] is False
    assert result["dispatch_authority_created"] is False
    assert result["executor_invoked"] is False
    assert result["physical_execution_invoked"] is False
    assert result["outcome_observed"] is False
    assert result["progress_counted"] is False
    assert [call["agent_name"] for call in calls] == [
        "missionos_chief_agent",
        "missionos_situation_judge_agent",
        "missionos_safety_critic_agent",
    ]
    assert all(call["workflow_execution_mode"] == "adk_v2_graph_shadow" for call in calls)


def test_adk_v2_shadow_agents_are_dynamic_children_without_nested_runners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agents import missionos_agents

    responses = {
        "missionos_chief_agent": {
            "intent": "status",
            "operator_instruction": "Inspect current status.",
            "requires_human_approval": False,
        },
        "missionos_situation_judge_agent": {
            "intent": "status",
            "operator_instruction": "Current evidence is advisory only.",
            "requires_human_approval": False,
        },
        "missionos_safety_critic_agent": {
            "boundary_status": "safe",
            "operator_instruction": "No authority is created.",
            "requires_human_approval": False,
        },
    }

    def build_fixture_agent(
        agent_name: str,
        *,
        model_id: str | None = None,
    ) -> LlmAgent:
        return LlmAgent(
            name=agent_name,
            model=StaticGraphLlm(
                model=f"static-{agent_name}",
                response_text=json.dumps(responses[agent_name]),
            ),
            instruction="Return the configured JSON object.",
        )

    async def fail_standalone_runner(**kwargs: Any) -> str:
        raise AssertionError("shadow graph invoked the standalone nested Runner")

    monkeypatch.setattr(missionos_agents, "build_missionos_agent", build_fixture_agent)
    monkeypatch.setattr(
        agent_runtime,
        "_invoke_adk_agent_text_async",
        fail_standalone_runner,
    )
    monkeypatch.setattr(
        agent_runtime,
        "_persist_invocation_evidence",
        lambda evidence: f"fixture/{evidence['agent_name']}.json",
    )

    result = shadow_graph.run_missionos_conversation_shadow_graph(
        utterance="What is the mission status?",
        missionos_state={},
    )

    assert result["graph_runtime_status"] == "proposal_guardrail_passed"
    assert len(result["agent_invocations"]) == 3
    assert all(
        invocation["agent_node_execution"] == "ctx.run_node"
        and invocation["workflow_child_node"] is True
        and invocation["standalone_runner_invoked"] is False
        and invocation["nested_runner_invoked"] is False
        for invocation in result["agent_invocations"]
    )
    paths = result["workflow_node_paths"]
    assert any("/missionos_chief_agent@chief-agent" in path for path in paths)
    assert any(
        "/missionos_situation_judge_agent@specialist-agent" in path for path in paths
    )
    assert any(
        "/missionos_safety_critic_agent@safety-critic-agent" in path for path in paths
    )
    assert result["approval_created"] is False
    assert result["dispatch_authority_created"] is False


def test_adk_v2_shadow_graph_stops_agent_calls_after_specialist_guardrail_block(
    monkeypatch,
) -> None:
    calls: list[str] = []

    async def fake_run_agent_once_async(**kwargs: Any) -> dict[str, Any]:
        name = kwargs["agent_name"]
        calls.append(name)
        if name == "missionos_chief_agent":
            return _invocation(
                name,
                {
                    "intent": "status",
                    "operator_instruction": "Check status.",
                    "requires_human_approval": False,
                },
            )
        return _invocation(
            name,
            {},
            passed=False,
            reasons=["specialist_output_forbidden_key:approved"],
        )

    monkeypatch.setattr(
        agent_runtime,
        "_run_agent_once_async",
        fake_run_agent_once_async,
    )

    result = shadow_graph.run_missionos_conversation_shadow_graph(
        utterance="What is the mission status?",
        missionos_state={},
    )

    assert result["graph_runtime_status"] == "guardrail_blocked"
    assert result["blocking_reasons"] == ["specialist_output_forbidden_key:approved"]
    assert result["proposal"] == {}
    assert calls == [
        "missionos_chief_agent",
        "missionos_situation_judge_agent",
    ]
    assert result["approval_created"] is False
    assert result["dispatch_authority_created"] is False
    assert result["executor_invoked"] is False


def test_shadow_comparison_is_narrow_and_measurement_only() -> None:
    comparison = shadow_graph.build_missionos_conversation_shadow_comparison(
        primary_result=_primary_result(),
        shadow_result=_shadow_result(),
        input_payload={"utterance": "status", "missionos_state": {}},
    )

    assert comparison["comparison_scope"] == "conversation_proposal_path_only"
    assert comparison["chief_stage_compared"] is True
    assert comparison["specialist_stage_compared"] is True
    assert comparison["safety_critic_stage_compared"] is True
    assert comparison["control_loop_compared"] is False
    assert comparison["agreement"] is True
    assert comparison["field_comparisons"]["intent"]["agreement"] is True
    assert comparison["measurement_only"] is True
    assert comparison["approval_created"] is False
    assert comparison["dispatch_authority_created"] is False
    assert comparison["executor_invoked"] is False
    assert comparison["physical_execution_invoked"] is False
    assert comparison["outcome_observed"] is False
    assert comparison["progress_counted"] is False


def test_runtime_does_not_invoke_shadow_graph_without_explicit_flag(
    monkeypatch,
) -> None:
    primary = _primary_result()
    monkeypatch.delenv(shadow_graph.MISSIONOS_ADK_V2_GRAPH_SHADOW_ENV, raising=False)
    monkeypatch.setattr(
        agent_runtime,
        "_run_missionos_agent_runtime_sequential",
        lambda **_kwargs: primary,
    )

    def fail_if_called(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("shadow graph must be opt-in")

    monkeypatch.setattr(
        shadow_graph,
        "run_missionos_conversation_shadow_graph",
        fail_if_called,
    )

    result = agent_runtime.run_missionos_agent_runtime(
        utterance="status",
        missionos_state={},
    )

    assert result is primary
    assert "adk_v2_shadow_comparison" not in result


def test_runtime_attaches_shadow_measurement_without_changing_primary_proposal(
    monkeypatch,
) -> None:
    primary = _primary_result()
    shadow = _shadow_result()
    monkeypatch.setenv(shadow_graph.MISSIONOS_ADK_V2_GRAPH_SHADOW_ENV, "1")
    monkeypatch.setattr(
        agent_runtime,
        "_run_missionos_agent_runtime_sequential",
        lambda **_kwargs: primary,
    )
    monkeypatch.setattr(
        shadow_graph,
        "run_missionos_conversation_shadow_graph",
        lambda **_kwargs: shadow,
    )

    result = agent_runtime.run_missionos_agent_runtime(
        utterance="status",
        missionos_state={"task_id": "task_shadow_fixture"},
    )

    assert result["runtime_status"] == primary["runtime_status"]
    assert result["proposal"] == primary["proposal"]
    assert result["adk_v2_shadow_result"] == shadow
    comparison = result["adk_v2_shadow_comparison"]
    assert comparison["agreement"] is True
    assert comparison["comparison_scope"] == "conversation_proposal_path_only"
    assert comparison["control_loop_compared"] is False
    assert comparison["dispatch_authority_created"] is False
    assert comparison["executor_invoked"] is False


def test_runtime_shadow_failure_is_fail_open_for_primary_path(monkeypatch) -> None:
    primary = _primary_result()
    monkeypatch.setenv(shadow_graph.MISSIONOS_ADK_V2_GRAPH_SHADOW_ENV, "1")
    monkeypatch.setattr(
        agent_runtime,
        "_run_missionos_agent_runtime_sequential",
        lambda **_kwargs: primary,
    )

    def fail_shadow(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("fixture shadow failure")

    monkeypatch.setattr(
        shadow_graph,
        "run_missionos_conversation_shadow_graph",
        fail_shadow,
    )

    result = agent_runtime.run_missionos_agent_runtime(
        utterance="status",
        missionos_state={},
    )

    assert result["runtime_status"] == "proposal_guardrail_passed"
    assert result["proposal"] == primary["proposal"]
    assert result["adk_v2_shadow_result"] == {}
    comparison = result["adk_v2_shadow_comparison"]
    assert comparison["shadow_runtime_status"] == "error"
    assert comparison["blocking_reasons"] == ["adk_v2_shadow_graph_failed:RuntimeError"]
    assert comparison["agreement"] is None
    assert comparison["dispatch_authority_created"] is False
    assert comparison["executor_invoked"] is False


def test_async_agent_invocation_marks_graph_execution_mode(monkeypatch) -> None:
    async def fake_invoke(**_kwargs: Any) -> str:
        return (
            '{"intent":"status","operator_instruction":"Check status",'
            '"requires_human_approval":false}'
        )

    monkeypatch.setattr(agent_runtime, "_invoke_adk_agent_text_async", fake_invoke)
    monkeypatch.setattr(
        agent_runtime,
        "_persist_invocation_evidence",
        lambda _evidence: "fixture/invocation.json",
    )

    evidence = asyncio.run(
        agent_runtime._run_agent_once_async(
            agent_name="missionos_chief_agent",
            agent_role="MissionOS chief coordinator agent",
            prompt_payload={"human_utterance": "status"},
            workflow_execution_mode="adk_v2_graph_shadow",
        )
    )

    assert evidence["guardrail_result"]["guardrail_passed"] is True
    assert evidence["workflow_execution_mode"] == "adk_v2_graph_shadow"
    assert evidence["adk_v2_graph_invoked"] is True
    assert evidence["progress_counted"] is False


@pytest.mark.parametrize(
    ("response_text", "expected_passed", "expected_blocking_reasons"),
    [
        (
            (
                '{"intent":"status","operator_instruction":"Check status",'
                '"requires_human_approval":false}'
            ),
            True,
            [],
        ),
        (
            (
                '{"intent":"status","operator_instruction":"Check status",'
                '"requires_human_approval":false,"approved":true}'
            ),
            False,
            ["forbidden_key_present:approved"],
        ),
    ],
    ids=["guardrail_passed", "forbidden_authority_key"],
)
def test_standalone_and_workflow_child_invocations_share_guardrail_contract(
    monkeypatch,
    response_text: str,
    expected_passed: bool,
    expected_blocking_reasons: list[str],
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_standalone_invoke(**kwargs: Any) -> str:
        calls.append(("standalone_runner", kwargs))
        return response_text

    async def fake_workflow_child_invoke(**kwargs: Any) -> str:
        calls.append(("ctx.run_node", kwargs))
        return response_text

    monkeypatch.setattr(
        agent_runtime,
        "_invoke_adk_agent_text_async",
        fake_standalone_invoke,
    )
    monkeypatch.setattr(
        agent_runtime,
        "_invoke_adk_agent_text_node_async",
        fake_workflow_child_invoke,
    )
    monkeypatch.setattr(
        agent_runtime,
        "_persist_invocation_evidence",
        lambda _evidence: "fixture/invocation.json",
    )
    common_kwargs = {
        "agent_name": "missionos_chief_agent",
        "agent_role": "MissionOS chief coordinator agent",
        "prompt_payload": {"human_utterance": "status"},
    }

    standalone = asyncio.run(
        agent_runtime._run_agent_once_async(
            **common_kwargs,
            workflow_execution_mode="sequential_runner",
        )
    )
    workflow_child = asyncio.run(
        agent_runtime._run_agent_once_async(
            **common_kwargs,
            workflow_execution_mode="adk_v2_graph_primary",
            workflow_ctx=object(),
            workflow_run_id="chief-agent",
        )
    )

    assert standalone["guardrail_result"] == workflow_child["guardrail_result"]
    assert standalone["guardrail_result"]["guardrail_passed"] is expected_passed
    assert standalone["guardrail_result"]["blocking_reasons"] == (
        expected_blocking_reasons
    )
    assert standalone["validated_output"] == workflow_child["validated_output"]
    assert standalone["prompt_sha256"] == workflow_child["prompt_sha256"]
    assert standalone["response_sha256"] == workflow_child["response_sha256"]
    assert standalone["provider"] == workflow_child["provider"]
    assert standalone["agent_node_execution"] == "standalone_runner"
    assert standalone["standalone_runner_invoked"] is True
    assert standalone["workflow_child_node"] is False
    assert workflow_child["agent_node_execution"] == "ctx.run_node"
    assert workflow_child["standalone_runner_invoked"] is False
    assert workflow_child["workflow_child_node"] is True
    assert [kind for kind, _kwargs in calls] == [
        "standalone_runner",
        "ctx.run_node",
    ]
    assert calls[0][1]["prompt_text"] == calls[1][1]["prompt_text"]
    assert calls[0][1]["model_id"] == calls[1][1]["model_id"]
