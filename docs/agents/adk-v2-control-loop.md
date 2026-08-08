# ADK v2 Dynamic ControlLoop Contract

This contract covers the `ControlLoop` used by the legacy-agent Gateway
profile. It does not describe the separate MissionOS conversation proposal,
canonical HITL, guarded-execution, or bounded-recovery workflows.

## Runtime Shape

`ControlLoop.run()` creates one ADK v2 `Workflow` per invocation:

```text
START
-> orchestrate_control_loop (dynamic node, rerun_on_resume=true)
   -> planner (LlmAgent child)
   -> executor (LlmAgent child)
   -> prepare_control_loop_verification (function child)
   -> verifier (LlmAgent child)
   -> promote_control_loop_memories (function child, only after pass)
```

The dynamic node uses `ctx.run_node()` for every child. Repair remains a
bounded Python loop inside the dynamic node, which is the ADK v2 dynamic
workflow form for iterative routing. The implementation must not create a
second `Runner` for each Planner, Executor, or Verifier invocation.

The Workflow name is `missionos_control_loop_v2`. The terminal output uses
`missionos_adk_v2_control_loop_result.v1` and is adapted back to the existing
`ExecutionResult` Python contract so Gateway callers do not gain a second
result shape.

## Resume Rules

Resume behavior follows the node's authority and side-effect boundary:

- Planner judgment must be recomputed from current state after an ADK resume.
- A completed Executor node sets `rerun_on_resume=false`; its guarded tools must
  not be reinvoked merely because orchestration resumed. An incomplete or
  ambiguous tool outcome still requires its own receipt/idempotency handling.
- Verifier output must be fresh after resume; cached verification is not
  dispatch-time or outcome truth.
- Completed memory promotion also sets `rerun_on_resume=false` so a terminal
  orchestration replay does not duplicate publication side effects.

The existing ControlLoop human-approval behavior is unchanged. A
`needs_human` state terminates the current Workflow before Executor. The human
decision is still recorded through `ControlLoop.resolve_human_approval()` and
a later `run()` invocation may reuse only the canonical approved plan in
MissionOS session state. ADK node completion does not create that approval.

## Authority Floor

The Workflow owns scheduling only. It does not independently create any of:

- human approval
- dispatch authority
- executor or backend receipts
- observed effects
- verifier causality
- delivery completion
- physical execution

Result metadata therefore fixes:

```text
node_completion_is_external_execution=false
node_completion_counts_progress=false
```

The normal MissionOS approval state, guarded tools, dispatch receipts, backend
evidence, and verifier report remain authoritative for their respective facts.

## Event Compatibility

ADK v2 events include `node_info.path`. ControlLoop event lookup supports both
the former `event.author == agent_name` shape and an agent name embedded in the
v2 node path. This is required when collecting Executor tool responses inside
a Workflow invocation.

## Verification

Run the deterministic runtime boundary smoke:

```bash
PYTHONPATH=. .venv/bin/python scripts/smoke_adk_v2_control_loop.py
```

The smoke uses fixture-only Agent nodes, invokes no external executor, and must
show Planner, Executor, and Verifier beneath
`missionos_control_loop_v2/orchestrate_control_loop` in `node_info.path`.

Run the contract tests:

```bash
.venv/bin/python -m pytest -q \
  tests/contract/test_control_loop_adk_v2_workflow.py
```

One contract test uses real ADK `LlmAgent` nodes with deterministic in-process
models. This proves callback, output-key, session-state, and node-path behavior
without making a model-network request.

## Scope Limit

This migration does not mean every MissionOS Agent route is graph-orchestrated.
Standalone dialogue, response, repair, hardware, perception, and specialist
Runner integrations require separate migration slices and inventories.
