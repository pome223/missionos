#!/usr/bin/env python3
"""Deterministic runtime smoke for the default ADK v2 proposal Workflow."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import json
import os
from pathlib import Path
import sys

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))


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


def main() -> None:
    from src.agents import missionos_agents
    from src.intelligence import missionos_adk_v2_shadow_graph as proposal_graph
    from src.intelligence import missionos_agent_runtime as runtime

    responses = {
        "missionos_chief_agent": {
            "intent": "status",
            "operator_instruction": "Inspect current status.",
            "requires_human_approval": False,
        },
        "missionos_situation_judge_agent": {
            "intent": "status",
            "operator_instruction": "Evidence remains advisory.",
            "requires_human_approval": False,
        },
        "missionos_safety_critic_agent": {
            "boundary_status": "safe",
            "operator_instruction": "No execution authority is created.",
            "requires_human_approval": False,
        },
    }

    def build_fixture_agent(
        agent_name: str,
        *,
        model_id: str | None = None,
    ) -> LlmAgent:
        del model_id
        return LlmAgent(
            name=agent_name,
            model=StaticLlm(
                model=f"static-{agent_name}",
                response_text=json.dumps(responses[agent_name]),
            ),
            instruction="Return the configured JSON object.",
        )

    os.environ[runtime.MISSIONOS_AGENT_RUNTIME_ADK_ENABLED_ENV] = "1"
    os.environ.pop(proposal_graph.MISSIONOS_ADK_V2_GRAPH_PRIMARY_ENV, None)
    os.environ.pop(proposal_graph.MISSIONOS_ADK_V2_GRAPH_ROLLBACK_ENV, None)
    os.environ.pop(proposal_graph.MISSIONOS_ADK_V2_GRAPH_SHADOW_ENV, None)
    runtime._adk_llm_credentials_available = lambda _name=None: True
    runtime._persist_invocation_evidence = (
        lambda evidence: f"fixture/{evidence['agent_name']}.json"
    )
    missionos_agents.build_missionos_agent = build_fixture_agent

    result = runtime.run_missionos_agent_runtime(
        utterance="What is the mission status?",
        missionos_state={"task_id": "task_adk_v2_default_smoke"},
    )
    graph_result = dict(result.get("adk_v2_graph_result") or {})
    node_paths = list(graph_result.get("workflow_node_paths") or [])
    required_paths = (
        "/missionos_chief_agent@chief-agent",
        "/missionos_situation_judge_agent@specialist-agent",
        "/missionos_safety_critic_agent@safety-critic-agent",
    )
    if result.get("workflow_execution_mode") != "adk_v2_graph_primary":
        raise RuntimeError("adk_v2_proposal_workflow_not_default")
    if not all(any(required in path for path in node_paths) for required in required_paths):
        raise RuntimeError("proposal_agents_not_executed_as_v2_children")
    if any(
        result.get(field) is not False
        for field in (
            "approval_created",
            "dispatch_authority_created",
            "executor_invoked",
            "physical_execution_invoked",
            "outcome_observed",
            "progress_counted",
        )
    ):
        raise RuntimeError("default_proposal_workflow_created_authority")

    print(
        json.dumps(
            {
                "schema_version": "missionos_adk_v2_default_proposal_smoke.v1",
                "status": "passed",
                "workflow_execution_mode": result.get("workflow_execution_mode"),
                "workflow_name": graph_result.get("workflow_name"),
                "agent_nodes_as_dynamic_children": True,
                "fixture_only": True,
                "approval_created": result.get("approval_created"),
                "dispatch_authority_created": result.get(
                    "dispatch_authority_created"
                ),
                "executor_invoked": result.get("executor_invoked"),
                "physical_execution_invoked": result.get(
                    "physical_execution_invoked"
                ),
                "outcome_observed": result.get("outcome_observed"),
                "progress_counted": result.get("progress_counted"),
                "node_paths": node_paths,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
