# Claim Semantics

Use explicit claim language. Do not collapse separate facts. This is the field
dictionary for the claim boundary; the enforcement code is
`src/runtime/runtime_claim_evidence.py` and
`src/runtime/hardware_adapter_contract.py`.

## Canonical Split

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

## Common Fields

Each field is a boolean unless noted. A field being present in an artifact does
not make it runtime-true; see "Two-Phase Artifact vs Runtime" below.

- `proposal_created`: an AI or planner produced a candidate response.
- `approval_recorded` / `operator_approved`: a human approval or rejection was
  recorded.
- `dispatch_authority_created`: a gate created authority to send a bounded
  action. Pinned `Literal[False]` on preflight and dispatch-candidate models in
  `hardware_adapter_contract.py`; authority is created later, not at preflight.
- `dispatch_request_sent`: an executor attempted to send a command.
- `command_ack_observed`: a runtime or transport acknowledgement was observed.
  ACK status itself is `HardwareAckStatus` ∈ `not_requested | accepted |
  rejected | timeout` (`hardware_adapter_contract.py`).
- `runtime_progress_observed`: the system observed runtime movement or state
  change beyond ACK.
- `landing_observed`: landing evidence was observed.
- `completion_claimed`: the adapter/verifier claims the bounded action
  completed. Its scope is qualified by `completion_scope` (below); it is not a
  delivery claim by itself.
- `delivery_completion_claimed`: the verifier claims delivery completion. This
  is the generic authority key (it appears in `AUTHORITY_RUNTIME_CLAIM_KEYS`).
  See "Delivery Completion Variants" for the scope-qualified names.
- `physical_execution_invoked`: a physical-world execution path was invoked.
  Pinned `Literal[False]` on read-only, preflight, dispatch-candidate, sim
  sidecar, and loopback models; it may only become true on an opt-in real
  serial/hardware path.

## `completion_scope`

`completion_scope` is a `Literal` on `HardwareAdapterEvidence`
(`hardware_adapter_contract.py`). Allowed values, weakest to strongest:

- `none` — no completion claimed.
- `loopback_action` — an in-process loopback action completed. Stays
  `physical_execution_invoked=false`.
- `sim_action` — a simulator action completed (the TurtleBot3, Unitree/MuJoCo,
  and Isaac sim slices use this).
- `adapter_action` — a bounded hardware-adapter action completed. The current
  sim slices must **not** use this: `contracts.md` requires sim completion to be
  `sim_action`, not `adapter_action`.
- `mission` — mission-level completion. Not claimed in public snapshots.

## Delivery Completion Variants

The code carries scope-qualified delivery flags. They are not interchangeable;
pick the one that matches the boundary you are changing.

- `delivery_completion_claimed` — generic authority key used by the LLM planners
  and the two-phase validator. Default `False`.
- `mission_delivery_completion_claimed` — mission-outcome level. Pinned
  `Literal[False]` on the TurtleBot3 telemetry sidecar
  (`src/runtime/turtlebot3_telemetry_sidecar.py`); also read in
  `src/runtime/mission_episode_review.py` and
  `src/intelligence/turtlebot3_recovery_planner.py`.
- `payload_delivery_completion_claimed` — payload-drop level
  (`src/intelligence/turtlebot3_recovery_planner.py`).
- `source_mission_delivery_completion_claimed` — provenance copy of the
  mission-level flag carried through `src/runtime/mission_episode_review.py`.

All four are `False` (or pinned `Literal[False]`) in public snapshots. Public
docs and CLI/map output should show the mission/payload variants, not only the
generic one, when reporting a simulator run's boundary.

## Two-Phase Artifact vs Runtime

Artifact truth is not runtime truth. `runtime_claim_evidence.py` splits every
authority claim into two phases:

- Legacy `foo=true` is preserved as `foo_in_artifact=true`.
- Runtime promotion to `foo_in_runtime=true` requires a valid
  `runtime_invocation_evidence` payload (`runtime_invocation_evidence.v1`)
  attached; otherwise `normalize_runtime_claims` forces `foo_in_runtime=false`
  and raises `..._requires_runtime_invocation_evidence`.

`AUTHORITY_RUNTIME_CLAIM_KEYS` (the keys this applies to) includes
`dispatch_executed`, `dispatch_authority_created`, `operator_approved`,
`route_invocation_observed`, `verified_dispatch_execution`,
`delivery_completion_claimed`, and `llm_judgment_used_in_gate`, among others.

Runtime invocation evidence must declare an `invocation_kind`:

- Any runtime claim: `subprocess | docker_exec | mavlink | gz_topic |
  http_loopback` (`ALLOWED_RUNTIME_INVOCATION_KINDS`).
- `dispatch_executed_in_runtime` specifically: `subprocess | docker_exec |
  mavlink | gz_topic` (`DISPATCH_RUNTIME_INVOCATION_KINDS`) — an
  `http_loopback` is not strong enough to promote a dispatch claim.

Invariant: an artifact-only claim cannot count progress. If `progress_counted`
is true while there are artifact claims and no runtime claims,
`normalize_runtime_claims` raises
`artifact_only_runtime_claim_cannot_count_progress`.

## Artifact Truth Versus Runtime Truth

Stored artifacts prove that MissionOS wrote a record. They do not by themselves
prove that runtime execution happened. Use runtime evidence — HTTP loopback
calls, subprocess execution, simulator telemetry, MAVLink ACK/readback, or
hardware readback — before claiming runtime execution, and attach it as
`runtime_invocation_evidence` so the two-phase validator can promote the claim.
