#!/usr/bin/env python3
"""Exercise the real ADK v2 shadow Workflow with fixture agent outputs."""

from __future__ import annotations

import json
import os
from typing import Any

from src.intelligence import missionos_adk_v2_shadow_graph as shadow_graph
from src.intelligence import missionos_agent_runtime as agent_runtime


def _invocation(agent_name: str, output: dict[str, Any]) -> dict[str, Any]:
    guardrail = {
        "guardrail_passed": True,
        "blocking_reasons": [],
        "validated_output": output,
    }
    return {
        "agent_name": agent_name,
        "validated_output": output,
        "guardrail_result": guardrail,
        "workflow_execution_mode": "adk_v2_graph_shadow",
        "adk_v2_graph_invoked": True,
        "progress_counted": False,
    }


def main() -> int:
    outputs = {
        "missionos_chief_agent": {
            "intent": "status",
            "operator_instruction": "Inspect fixture mission state.",
            "requires_human_approval": False,
        },
        "missionos_situation_judge_agent": {
            "intent": "status",
            "operator_instruction": "Present the fixture status to the operator.",
            "requires_human_approval": True,
        },
        "missionos_safety_critic_agent": {
            "intent": "status",
            "boundary_status": "needs_human_approval",
            "requires_human_approval": True,
        },
    }

    async def fixture_agent(**kwargs: Any) -> dict[str, Any]:
        name = str(kwargs["agent_name"])
        return _invocation(name, outputs[name])

    primary = {
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
    original_agent = agent_runtime._run_agent_once_async
    original_sequential = agent_runtime._run_missionos_agent_runtime_sequential
    original_flag = os.environ.get(shadow_graph.MISSIONOS_ADK_V2_GRAPH_SHADOW_ENV)
    try:
        agent_runtime._run_agent_once_async = fixture_agent
        agent_runtime._run_missionos_agent_runtime_sequential = lambda **_kwargs: primary
        os.environ[shadow_graph.MISSIONOS_ADK_V2_GRAPH_SHADOW_ENV] = "1"
        result = agent_runtime.run_missionos_agent_runtime(
            utterance="Report fixture mission status.",
            missionos_state={"task_id": "task:adk-v2-shadow-smoke"},
        )
    finally:
        agent_runtime._run_agent_once_async = original_agent
        agent_runtime._run_missionos_agent_runtime_sequential = original_sequential
        if original_flag is None:
            os.environ.pop(shadow_graph.MISSIONOS_ADK_V2_GRAPH_SHADOW_ENV, None)
        else:
            os.environ[shadow_graph.MISSIONOS_ADK_V2_GRAPH_SHADOW_ENV] = original_flag

    shadow = result.get("adk_v2_shadow_result")
    shadow = shadow if isinstance(shadow, dict) else {}
    comparison = result.get("adk_v2_shadow_comparison")
    comparison = comparison if isinstance(comparison, dict) else {}
    expected_nodes = [
        "normalize_shadow_input",
        "invoke_shadow_chief",
        "invoke_shadow_specialist",
        "invoke_shadow_safety_critic",
        "finalize_shadow_proposal",
    ]
    if result.get("proposal") != primary["proposal"]:
        raise RuntimeError("shadow graph changed the authoritative proposal")
    if shadow.get("graph_node_sequence") != expected_nodes:
        raise RuntimeError("shadow graph node sequence mismatch")
    authority_fields = (
        "approval_created",
        "dispatch_authority_created",
        "executor_invoked",
        "physical_execution_invoked",
        "outcome_observed",
        "progress_counted",
    )
    if any(shadow.get(field) is not False for field in authority_fields):
        raise RuntimeError("shadow graph crossed the authority floor")

    print(
        json.dumps(
            {
                "schema_version": "missionos_adk_v2_shadow_graph_smoke.v1",
                "verification_status": "passed",
                "workflow_name": shadow.get("workflow_name"),
                "graph_node_sequence": expected_nodes,
                "comparison_scope": comparison.get("comparison_scope"),
                "agreement": comparison.get("agreement"),
                "primary_proposal_unchanged": True,
                "measurement_only": shadow.get("measurement_only") is True,
                **{field: False for field in authority_fields},
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
