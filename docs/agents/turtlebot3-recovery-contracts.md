# TurtleBot3 Recovery Contract Boundaries

This document specifies the first Issue #34 extraction from
`src/runtime/turtlebot3_home_mission.py`. The extracted module is
`src/runtime/turtlebot3_recovery_contracts.py`. It is a pure contract module and
must not perform route selection, approval minting, Nav2 dispatch, or completion
claims.

## Artifact chain

New Recovery checkpoints include `recovery_contract_bundle` with these nested
artifacts:

- `missionos_turtlebot3_recovery_intent.v1`: binds `selected_action`, requested
  parameters, proposal identity, and classification identity.
- `missionos_turtlebot3_recovery_intent_compilation.v1`: binds the same action
  and parameters as compiled output and records `compiler_changed_meaning=false`.
- `missionos_turtlebot3_recovery_predispatch_verification.v1`: projects existing
  path and costmap evidence. It does not perform a new live evaluation.
- `missionos_turtlebot3_recovery_contract_bundle.v1`: hashes the nested chain.

Every artifact has a canonical SHA-256 and a digest-derived ID. The checkpoint
hash covers the bundle. Resume validation checks both the checkpoint hash and
the nested chain.

All pre-dispatch artifacts must keep these fields false:

```text
approval_created
dispatch_authority_created
physical_execution_invoked
progress_counted
```

The existing operator-approval artifact remains the only Recovery dispatch
authority. A bundle is not approval.

## Candidate evidence

For `avoid_obstacle`, pre-dispatch verification is `verified` only when the
checkpoint already contains the required path and costmap hashes. A checkpoint
may still record an `unverified` deterministic candidate while it waits for the
existing live evaluation and operator workflow. The contract projection must
not invent missing costmap evidence.

For `reroute` and `return_home`, candidate path evidence is `not_required` by
this artifact. Their existing runtime guards remain authoritative.

## Outcome verification

Each closed-loop Recovery cycle records
`missionos_turtlebot3_recovery_outcome_verification.v1`. The verifier separates:

- fresh authority bound to checkpoint ID, hash, action, and parameters;
- dispatch request sent;
- command ACK observed;
- executor effect observed;
- complete goal sequence observed;
- requested-side and obstacle-clearance evidence;
- route-resume authorization.

`command_ack_observed=true` never implies executor effect. Recovery success
requires fresh authority, dispatch, executor effect, goal completion, and all
required geometry observations. Route resume additionally requires the
approved action semantics to allow it.

The outcome artifact always keeps `delivery_completion_claimed=false` and
`physical_execution_invoked=false` for this simulator boundary.

## Compatibility

Stored legacy checkpoints without `recovery_contract_bundle` remain readable.
They still pass through the pre-existing checkpoint, approval, candidate, and
resume guards. New checkpoints must contain a valid bundle; changing their
action, parameters, or candidate binding invalidates the chain.

## Runtime smoke

The Docker smoke can exercise the fresh-approval boundary by combining:

```text
MISSIONOS_CHAT_TURTLEBOT3_DYNAMIC_OBSTACLE_RECOVERY_SMOKE=1
MISSIONOS_CHAT_TURTLEBOT3_HUMAN_APPROVAL_DEMO_SMOKE=1
MISSIONOS_TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL=1
```

The human-approval-demo flag selects the scenario only. It is not approval
authority. The smoke reads the pending checkpoint and calls the production
Gateway recovery-dispatch route with its exact ID, hash, action, and parameters.
The Gateway performs the same validation and creates the same fresh approval as
the operator CLI path.

## Remaining Issue #34 work

This extraction intentionally leaves execution orchestration in the existing
runtime. Nav2 dispatch and source-backed re-observation now live in
`src/runtime/turtlebot3_nav2_execution.py`. CLI companion presentation and task
correlation live in `missionos_cli.chat_companions` and
`missionos_cli.operate_view`.

Gateway context recognition, the read-only Recovery decision projection, task
creation/update, and display-only live-telemetry binding live in
`src/gateway/turtlebot3_task_lifecycle.py`. The module receives the Gateway's
TaskStore explicitly. It cannot create an approval or bounded action and cannot
dispatch Nav2. Explicit operator approval, checkpoint CAS, synchronous dispatch,
and recovery outcome finalization remain on the existing Gateway authority path.

The maintained production-boundary runner now lives in
`src/runtime/turtlebot3_chat_e2e_runner.py`. The historical
`scripts/smoke_missionos_chat_turtlebot3_home_mission.py` path is an eight-line
compatibility entrypoint, so existing Docker and operator commands keep working.
The runner still calls the production Gateway and exact Recovery checkpoint
route; it does not manufacture approval in a fixture.

The final Issue #34 gate is a same-task chat/operate/watch/map runtime check
through the opt-in TurtleBot3 simulator boundary.
