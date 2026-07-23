# Action Feasibility Engine contract

This contract describes the deterministic multi-hazard verification boundary
used by PX4 recovery proposals. It is a maintainer implementation contract, not
a claim that the current simulation models are physically calibrated.

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

The engine only constrains a proposed action. It never creates approval,
dispatch authority, physical execution, progress, or completion.

## Hazard State

`missionos_runtime_recovery_hazard_state.v1` normalizes source-backed facts from
one telemetry cursor:

- battery remaining and return margin
- wind speed, gust, and control margin
- terrain clearance and clearance margin
- obstacle identity, local-conflict status, time to conflict, source-backed
  3D bounds, frame, and required clearance
- temperature and explicit battery/motor derating model
- payload, position, altitude, home distance, and landing-zone safety
- same-task bounded-OFFBOARD performance observations and their conservative
  speed envelope

Each observed fact carries its unit, source references, observation time,
telemetry cursor, and frame where applicable. Derived facts retain their model
reference. The state also binds an immutable policy snapshot and SHA-256 digest.

The state is `unverified` when the cursor is incomplete or regresses, telemetry
is stale or dropped out, the policy reference is absent, or the expected policy
digest differs. Missing evidence is not converted into a safe default.

## Action Feasibility

`missionos_runtime_recovery_action_feasibility.v1` evaluates one of:

- `avoid_obstacle`
- `reroute`
- `adjust_altitude`
- `return_to_launch`
- `land`

The result is tri-state:

- `verified_feasible`: every required fact, model, and constraint is present
  and passes
- `blocked`: source-backed evidence proves at least one constraint fails
- `unverified`: required evidence, model metadata, geometry, or policy material
  is missing, stale, inconsistent, or incomparable

The common verifier checks wind control margin, terrain clearance, motor
derating, maneuver duration, time to conflict, obstacle identity, landing-zone
safety, and projected battery reserve. For `avoid_obstacle`, every segment in
the candidate recovery path is compared deterministically with the selected
obstacle AABB in the same frame. Collision, clearance-boundary equality, and a
path that does not finish beyond the obstacle are blocked. Missing bounds,
path geometry, or a frame mismatch is `unverified`. Battery and temperature calculations
require explicit model identifiers, versions, source references, and
uncertainty. The built-in coefficients are labeled
`simulation_only_not_physical_calibration`.

Payload request evidence and applied simulator mass are separate facts. If a
route requests a payload, the requested mass must remain present in Hazard
State, the materialized SDF application mass must be present, and the two values must
match within the serialization tolerance. A missing applied value, a
configured-but-unverified application, or a requested/applied mismatch is
`unverified`; the energy model must not silently substitute its no-payload
multiplier. A scenario with no payload request may still use the explicit
no-payload default.

`avoid_obstacle` and `reroute` do not use AUTO cruise speed or
`max_horizontal_speed - wind_speed` as proof of executable ground speed. When
the active policy requires an OFFBOARD performance envelope, the verifier
requires a same-task bounded maneuver observation containing sample count,
duration, horizontal displacement, and source references. It applies the
policy uncertainty fraction to the observed average speed and uses the lower
of that result, the policy maximum, and the wind-control margin. Missing,
insufficient, or non-positive performance evidence is `unverified`; it is not
replaced by a nominal speed.

The first observation is bootstrapped through the explicit
`missionos calibrate-offboard` boundary. Calibration is SITL-only, is never an
LLM-selectable recovery action, and requires a separate human confirmation.
It is accepted only while telemetry is fresh and synchronized, no local
obstacle conflict is active, no verified same-task envelope already exists,
and the target remains inside the calibration distance and altitude limits.
The runner uses a tighter 2 m horizontal target tolerance than normal recovery
and persists a successful observation as
`missionos_runtime_recovery_performance_evidence.v1`. A later Safety HOLD does
not overwrite that evidence.

When the active runner has source-backed observations for the complete
preauthorized Safety HOLD sequence (request, command ACK, observed HOLD, and
`held_awaiting_operator_recovery_approval`), progress along the original
colliding route has stopped. In that state only, the original-route
time-to-conflict value is no longer treated as a running maneuver deadline.
The result records `suspended_by_observed_safety_hold`; action geometry,
obstacle identity, reachability, and every other hazard remain mandatory and
are revalidated at dispatch. A missing TTC without that complete HOLD evidence
is `unverified`.

The active SITL policy keeps the normal maneuver-duration bound at 75 seconds.
Only source-backed `avoid_obstacle` candidates observed under the complete
Safety HOLD contract may use the separate 150-second avoidance bound. This
does not apply to `reroute`, calibration, altitude, speed, RTL, or LAND.
Observed performance, battery reserve, wind margin, terrain clearance,
obstacle geometry, policy binding, dispatch-time telemetry, and human approval
remain mandatory. The active runner uses the same action-specific bound.

Only `verified_feasible` candidates may remain selectable when
`action_feasibility_required=true`. A hosted model choosing a `blocked` or
`unverified` candidate is converted to `operator_review`; this is rejection of
the proposed bounded action, not an alternate approval.

The hosted judge receives `action_judgment_context`, including every candidate,
its `blocked` or `unverified` reasons, and the separately filtered selectable
set. The model may compare compound conditions and explain why HOLD, LAND, RTL,
or operator review is safer. It cannot change `eligible_for_selection`, upgrade
feasibility, or create approval or dispatch authority.

## Proposal and dispatch binding

A proposal with complete hazard and feasibility evidence uses
`missionos_runtime_recovery_proposal_evidence.v3`. It binds:

- hazard-state id and digest
- action-feasibility id and digest
- recovery intent, compilation, and reachability evidence
- telemetry cursor and policy digest

At operator-approved dispatch time, Gateway:

1. validates all stored artifact hashes and their chain;
2. arbitrates the latest telemetry using the existing live-bridge/runtime
   snapshot contract;
3. resolves the currently active policy independently of the proposal;
4. rejects an unknown policy or any digest drift from the proposal-bound
   policy, requiring a new proposal and approval;
5. rebuilds Hazard State using the current active policy;
6. rejects cursor regression, model drift, stale or incomplete facts;
7. recomputes the selected action and its full 3D path clearance against the
   latest hazards and the latest source-bound performance envelope;
8. queues the runner request only when the result remains
   `verified_feasible`.

When the current policy has `action_feasibility_required=true`, pending legacy
v2 proposals are invalidated at dispatch and must be regenerated as v3. This
closes the deployment window in which an already-approved v2 artifact could
otherwise bypass the new engine.

## Verification and evidence boundary

Contract tests must cover all five actions and table-driven combinations of
battery, wind, terrain, obstacle, temperature, and timing conditions. Negative
tests must include missing facts, stale/dropout telemetry, cursor regression,
unknown models, active-policy drift, obstacle identity changes, collision
paths, exact clearance boundaries, missing geometry, frame mismatches, pending
v2 migration, missing or insufficient OFFBOARD performance samples,
optimistic nominal-speed substitution, and dispatch-time hazard deterioration.
Payload negatives must include requested mass with missing SDF application,
requested/applied mass mismatch, and configured-but-unverified application.
Tests must also prove that calibration cannot suppress a later obstacle epoch,
that its observation survives Safety HOLD replacement, and that the 150-second
bound applies only to obstacle avoidance.

A fixture Gateway smoke proves serialization and fail-closed queue behavior.
An opt-in PX4/Gazebo run is still required to claim live simulator behavior.
Fixture or contract evidence must not be described as physical execution or as
proof that the simulation-only energy/derating coefficients match hardware.
