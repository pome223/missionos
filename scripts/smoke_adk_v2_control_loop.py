#!/usr/bin/env python3
"""Deterministic runtime smoke for the ADK v2 ControlLoop boundary."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from google.adk.memory import InMemoryMemoryService
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import node

import src.control_loop.root_workflow as root_workflow
from src.runtime.state_keys import StateKeys


@node(name="planner", rerun_on_resume=True)
def fixture_planner(ctx: Any, node_input: Any) -> dict[str, bool]:
    ctx.state[StateKeys.PLAN_APPROVED] = {
        "plan_id": "plan_adk_v2_control_loop_smoke",
        "steps": [],
    }
    ctx.state[StateKeys.APPROVAL_STATUS] = "policy_approved"
    return {"planned": True}


@node(name="executor", rerun_on_resume=True)
def fixture_executor(ctx: Any, node_input: Any) -> dict[str, bool]:
    ctx.state[StateKeys.TEMP_EXECUTOR_OUTPUTS] = {
        "status": "fixture_only",
        "external_execution_invoked": False,
    }
    return {"executed_fixture": True}


@node(name="verifier", rerun_on_resume=True)
def fixture_verifier(ctx: Any, node_input: Any) -> dict[str, bool]:
    ctx.state[StateKeys.VERIFY_LAST_REPORT] = {
        "report_id": "report_adk_v2_control_loop_smoke",
        "status": "pass",
        "overall_score": 1.0,
        "summary": "ADK v2 fixture workflow completed",
    }
    return {"verified": True}


async def main() -> None:
    root_workflow.planner_with_policy = fixture_planner
    root_workflow.executor_with_tools = fixture_executor
    root_workflow.verifier_with_hooks = fixture_verifier

    session_service = InMemorySessionService()
    loop = root_workflow.ControlLoop(
        session_service=session_service,
        memory_service=InMemoryMemoryService(),
    )

    async def prepare_verification_state(**kwargs: Any) -> dict[str, Any]:
        ctx = kwargs["workflow_ctx"]
        verification_inputs = {
            "artifact_refs": [],
            "fixture_only": True,
            "external_execution_invoked": False,
        }
        ctx.state[StateKeys.TEMP_VERIFICATION_INPUTS] = verification_inputs
        return verification_inputs

    async def promote_memories(**kwargs: Any) -> list[str]:
        return []

    loop._prepare_verification_state = prepare_verification_state  # type: ignore[method-assign]
    loop._promote_memories = promote_memories  # type: ignore[method-assign]
    result = await loop.run(
        goal="run the deterministic ADK v2 control-loop smoke",
        user_id="smoke_operator",
        session_id="smoke_adk_v2_control_loop",
    )
    session = await session_service.get_session(
        app_name=root_workflow._APP_NAME,
        user_id="smoke_operator",
        session_id="smoke_adk_v2_control_loop",
    )
    if session is None:
        raise RuntimeError("control_loop_smoke_session_missing")
    node_paths = [
        str(getattr(getattr(event, "node_info", None), "path", "") or "")
        for event in session.events
    ]
    required_children = (
        "/planner@planner-attempt-0",
        "/executor@executor-attempt-0",
        "/verifier@verifier-attempt-0",
    )
    if not result.success or not all(
        any(required in path for path in node_paths) for required in required_children
    ):
        raise RuntimeError("control_loop_agents_not_executed_as_v2_children")

    print(
        json.dumps(
            {
                "schema_version": "missionos_adk_v2_control_loop_smoke.v1",
                "status": "passed",
                "workflow_name": result.metadata.get("adk_workflow_name"),
                "workflow_engine": result.metadata.get("adk_workflow_engine"),
                "agent_nodes_as_dynamic_children": True,
                "fixture_only": True,
                "approval_created": False,
                "dispatch_authority_created": False,
                "external_execution_invoked": False,
                "physical_execution_invoked": False,
                "node_completion_counts_progress": False,
                "node_paths": [path for path in node_paths if path],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
