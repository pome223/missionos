# PX4 Recovery Intent, Compiler, Guard, and Outcome Verifier

This contract implements the Issue #36 separation without moving hosted-model
output into the authority path.

## Authority split

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

The implementation uses four immutable evidence artifacts before and after the
existing human approval boundary.

## Artifacts

### `missionos_runtime_recovery_intent.v1`

This is the normalized Runtime Recovery Agent judgment. Required fields include:

- `strategy`: `monitor`, `global_reroute`, `local_avoidance`, `hold`, or
  `rtl_or_land`
- `selected_action`
- `intent_constraints`
- `requested_parameters`
- `recovery_intent_id` and `recovery_intent_sha256`

Supported semantic constraints are direction, minimum clearance, destination
kind, maximum duration/speed, and altitude bounds. Unsupported constraints or a
strategy/action mismatch make the intent invalid. The artifact always keeps
approval, dispatch, runtime execution, and progress false.

The Runtime Recovery FunctionTool accepts `strategy`, `avoidance_side`,
`minimum_clearance_m`, `maximum_duration_s`, `maximum_speed_mps`,
`destination_kind`, and altitude-envelope arguments in addition to the selected
action. These values are recorded as model intent before deterministic planning.
Omitted constraints are not invented later. Unsupported or internally
inconsistent values make the intent invalid.

### `missionos_runtime_recovery_intent_compilation.v1`

The compiler binds one intent to one deterministic candidate. It records:

- source intent id/hash
- requested and compiled action
- requested and compiled parameters
- intent constraints and candidate basis
- policy snapshot used for later revalidation
- `meaning_preserved`
- compilation id/hash and blocking reasons

The compiler may add source-backed safety metadata. It must not change action,
requested coordinates, avoidance side, destination meaning, minimum clearance,
or altitude bounds. If preservation is impossible it emits
`compilation_status=infeasible`, no compiled action, and no authority.

### `missionos_runtime_recovery_reachability_verification.v1`

This pre-dispatch evidence is a conservative support check, not a future-arrival
claim. It records current-to-target distance, configured speed bounds, observed
wind, uncertainty, estimated duration, conservative upper duration, available
duration, geofence checks, battery-envelope checks, and reasons.

For a parameterized Recovery proposal, `verification_status=verified` is
required before it can proceed to human approval and must be recomputed from
fresh telemetry before dispatch. Strong wind, stale telemetry, missing geometry,
an expired duration envelope, or an insufficient battery envelope fails closed.

### `missionos_runtime_recovery_outcome_verification.v1`

This is post-execution evidence. It keeps these facts distinct:

- dispatch authority observed
- command ACK observed
- executor effect observed
- target reached
- resume-safety verification
- AUTO resume status

`ack_is_execution_effect` is always false. Parameterized Recovery success needs
`target_reached=true`. `resumed_auto_mission` additionally needs a verified
resume-safety artifact with `resume_auto_authorized=true`.

## Proposal compatibility

New proposals use `missionos_runtime_recovery_proposal_evidence.v2` and copy the
intent, compilation, and reachability artifacts plus their ids and hashes.
Dispatch-time revalidation recomputes artifact hashes, verifies the
intent-to-compilation-to-reachability id/hash chain, and recomputes reachability
from the current task telemetry. Any mismatch blocks authority minting.

Legacy v1 proposals remain readable for stored-history compatibility. They do
not retroactively acquire v2 compiler or reachability claims.

## Compound wind and obstacle transition

Wind and a source-backed obstacle may be observed at the same time. If wind is
above the Recovery policy limit, the Runtime Recovery Agent must keep the
aircraft in the already observed safety HOLD and emit `operator_review`; it must
not compile or dispatch an obstacle maneuver through an unsafe wind envelope.

A later obstacle rejudgment is allowed only when
`missionos_runtime_recovery_wind_safe_window.v1` verifies all of the following:

- the same source-backed obstacle still requires local avoidance
- the safety HOLD is observed and stable
- every sample in the configured window is fresh and contains wind telemetry
- the window spans the configured minimum duration without an excessive gap
- the maximum observed wind remains at or below the Recovery policy limit

The default minimum window is 30 seconds. Missing, stale, sparse, or
above-limit observations fail closed. Sustained above-limit wind therefore
remains in HOLD/operator review.

When the window verifies, the runtime records
`missionos_runtime_recovery_compound_hazard_transition.v1`, supersedes or
retains the stale history of the old wind-blocked proposal, and opens exactly
one new hosted-model judgment epoch for the remaining obstacle. This transition
does not create approval, dispatch authority, progress, or physical-execution
evidence. Any resulting obstacle proposal still requires fresh human approval
and dispatch-time revalidation.

## Required invariants

- A proposal is not approval.
- Compilation is not authority.
- Reachability verification is not target arrival.
- ACK is not effect.
- Target not reached is not Recovery success.
- AUTO cannot resume without target and resume-safety verification.
- Changed action, parameters, constraints, or approved envelope creates a new
  proposal/checkpoint and fresh human approval.
- Infeasible intent returns to the agent/operator; the compiler does not select
  a different strategy.
- All artifacts keep delivery completion and physical execution unclaimed.

## Runtime verification

Changes to this boundary require both:

1. contract tests for hash binding, meaning preservation, conservative
   reachability, ACK/effect separation, and dispatch-time recomputation
2. a production-boundary smoke using a real TaskStore and Gateway revalidation,
   or an opt-in PX4/Gazebo SITL run when executor behavior changes

The deterministic compiler and verifier do not require a repeated hosted-model
call when an existing E2E judgment artifact is sufficient for the changed
boundary.
