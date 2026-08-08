# ADK v2 Legacy Gateway Conversation Contract

This contract covers the explicit `legacy_agent` Gateway profile. It does not
change the production MissionOS conversation proposal graph or give ADK any
MissionOS approval, dispatch, execution, or verifier authority.

## Runtime Shape

The legacy Gateway conversation path uses one ADK v2 dynamic Workflow per
request:

```text
START
-> orchestrate_gateway_conversation (rerun_on_resume=true)
   -> routing_agent (single_turn sub-branch)
   -> optional specialist (single_turn sub-branch)
   -> optional boiled_claw root_agent (chat main branch)
```

The Workflow name is `missionos_legacy_gateway_conversation_v2`. Router,
specialist, and root Agent calls use `ctx.run_node()`. The former long-lived
root Runner, routing Runner, and per-specialist Runner map are removed from
`GatewayServer`.

Router and specialist inputs run in ADK sub-branches so their private prompts
and intermediate responses do not become ordinary root conversation turns.
The root Agent remains in `chat` mode on the main branch so existing ADK
conversation history and supported sub-agent collaboration remain available.
The current routing and grounding context is injected into that turn's root
instruction. A unique temporary state key captures the root's final text
because ADK chat-mode child nodes do not return text directly from
`ctx.run_node()`.

## Gateway-Owned Branches

The Workflow may return either `control_loop` or `dynamic_agent` as a routing
decision, but it does not invoke either boundary. `GatewayServer` receives the
decision and performs the existing deterministic handoff afterward. The graph
therefore cannot create execution authority merely by selecting a route.

Direct specialist mode terminates after the specialist node. In
`preflight_then_root` mode, specialist evidence is supplied to the root node.
Browser or GUI infrastructure failure remains fail-closed and prevents root
synthesis from implying that the requested browser operation occurred.

Cron route selection uses the separate
`missionos_legacy_gateway_router_v2` Workflow. Its routing Agent is likewise a
real dynamic child rather than the root Agent of a dedicated routing Runner.

## Authority Floor

Every terminal conversation result fixes:

```text
approval_created=false
dispatch_authority_created=false
external_execution_invoked=false
physical_execution_invoked=false
node_completion_counts_progress=false
```

Tool results remain tool results. A tool response, Agent response, route
selection, node path, or Workflow completion is not human approval, dispatch
authority, execution evidence, an observed effect, verifier passage, delivery
completion, or progress.

## Resume and Failure Rules

The orchestrator and routing judgment set `rerun_on_resume=true` so a resumed
route uses current context. Completed specialist and root nodes set
`rerun_on_resume=false` because either may have invoked tools; orchestration
resume must not replay those side effects. This Workflow is presently a normal
one-request Gateway conversation path; it does not add automatic retry.

Router model failure falls back to the existing deterministic heuristic and
is audit-logged. A direct specialist failure propagates as an error. A
preflight specialist exception may fall back to root synthesis, but a proven
browser/GUI infrastructure failure remains blocked instead of being presented
as a successful browser operation.

## Verification

Run the real loopback HTTP boundary smoke:

```bash
PYTHONPATH=. .venv/bin/python scripts/smoke_adk_v2_legacy_gateway.py
```

The smoke starts the legacy Gateway with Uvicorn on a loopback port, sends
`POST /agent/run` with an HTTP client, and verifies the router and root
`node_info.path` entries. It uses deterministic in-process models and invokes
no external model, executor, simulator, hardware, or physical action.

Run the focused contracts:

```bash
PYTHONPATH=packages/missionos-cli/src:. .venv/bin/python -m pytest -q \
  tests/contract/test_gateway_adk_v2_conversation_workflow.py
```

The contracts cover root routing, direct specialist routing, specialist
preflight plus root synthesis, browser-infrastructure fail-closed behavior,
and the control-loop routing authority floor.

## Remaining Migration Boundary

Standalone dialogue, response, repair, hardware, perception, recovery, CLI,
and dynamic-subagent `LlmAgent + Runner` integrations remain separate runtime
entrypoints. A direct Runner around one ADK v2 Agent node is not automatically
a legacy orchestration loop. Each remaining callsite must be classified as
either a valid standalone entrypoint or a multi-stage path that needs a
Workflow.
