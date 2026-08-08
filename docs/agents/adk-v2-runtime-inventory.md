# ADK v2 Runtime Inventory

This inventory defines the completion boundary for the MissionOS ADK v2
migration. A `Runner` is an ADK application/session entrypoint; it is not by
itself evidence of legacy orchestration.

## Classification Rule

- `workflow_root`: the Runner receives an ADK v2 `Workflow`. All multi-stage
  control flow is expressed by Workflow nodes and dynamic `ctx.run_node()`
  children.
- `single_agent_root`: the Runner receives one standalone ADK Agent. There is
  no local multi-stage Python orchestration to convert. Wrapping this in a
  one-node Workflow would not add graph semantics.

`tests/contract/test_adk_v2_runner_inventory.py` AST-scans every Python module
under `src/`. CI fails when a Runner is added, removed, or moved to a different
scope without updating this contract and reviewing its classification.

## Workflow Roots

| Runtime boundary | Workflow purpose |
| --- | --- |
| `ControlLoop.run` | Planner, Executor, Verifier, and bounded Repair loop |
| `GatewayServer._run_gateway_conversation_workflow` | routing, optional specialist, and root synthesis |
| `GatewayServer._select_route_for_message` | route-only child Agent execution |
| `start_missionos_canonical_approval_hitl` | canonical approval RequestInput and guarded continuation |
| `resume_missionos_canonical_approval_hitl` | checkpoint restore, fresh validation, guarded continuation, and bounded recovery routing |
| `_run_missionos_conversation_graph_async` | Chief, deterministic specialist selection, specialist, and Safety Critic proposal graph |

## Standalone Agent Roots

| Runtime boundary | Single Agent responsibility |
| --- | --- |
| `_run_cli` | interactive root Agent session |
| `_run_channels` | channel root Agent session |
| `_invoke_adk_gemini_async` | dialogue-router JSON judgment |
| `_invoke_adk_gemini_repair_text_async` | one repair-plan judgment |
| `_invoke_adk_gemini_response_text_async` | one Form 2A response-plan judgment |
| `_invoke_adk_agent_text_async` | legacy sequential rollback/shadow comparison Agent invocation |
| `_invoke_runtime_recovery_agent_text_with_tools_async` | one Runtime Recovery Agent with a bounded FunctionTool |
| `_invoke_chief_route_function_tool_async` | one Chief route-planning Agent with a bounded FunctionTool |
| `_invoke_adk_gemini_response_text_async` in the arm/disarm planner | one props-removed bench proposal judgment |
| `_invoke_adk_perception_response_async` | one TurtleBot perception judgment |
| `_invoke_adk_response_text_async` | one TurtleBot recovery judgment |
| `SubagentManager._worker_loop` | one explicitly requested background Agent job |

## Completion Boundary

The runtime migration is complete when all of these remain true:

1. The production dependency is pinned to ADK v2 (`>=2.5.0,<3.0.0`).
2. Every production Runner is classified by the AST contract above.
3. Every multi-stage Agent chain uses a `Workflow`; no production path manually
   appends ADK session events or implements a custom `_run_async_impl` loop.
4. The conversation proposal Workflow is the default. The sequential
   Chief/specialist/Safety Critic code remains only for explicit rollback or
   bounded shadow comparison.
5. Approval, dispatch, execution, receipt, observed effect, verifier result,
   recovery success, and progress remain MissionOS facts outside Agent output.
6. HITL, guarded execution, and recovery remain opt-in because adopting ADK v2
   does not authorize side effects.

This does not mean every Agent must be rewritten as a Workflow. In ADK v2, a
standalone Agent is already a valid root node. Graph structure is required when
MissionOS composes multiple decisions or execution stages.
