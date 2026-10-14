# Concurrent ascent and deployment prefix

`starship_m3_prefix.run_fixture(profile, case, output)` is an explicit local
simulation entry point. Registered cases are `normal`, `operations_notice` and
`release_fault`. It does not run when imported. The default booster controller
remains `fixed_v1`; the rejected coast controller is not enabled.

The outer prefix authority contains the canonical fixture M2 authority and the
ascent authority, with separate canonical SHA-256 identities. The v2 authority
also binds a separate canonical fixture booster grant with the registered
`SplashdownGoal`. A body router sends deployment requests to the unchanged M2
actor and only `booster_selection` to the booster actor. Its simulator-facing
envelope carries the exact same goal passed to `simulate_concurrent`; this
activates the existing hull-clearance water-entry control, not new dynamics or
increased actuator authority. Ship records stay in `mission_director`; booster
records are saved separately in `booster_mission_director`. Both request
namespaces bind the actual run ID. The outer scope
intersects these grants: embedding M2 return authority does not permit this
runner to forecast or dispatch Ship return. `approval_binding` records fixture
configuration, not an authenticated human approval. Model calls and forecasts
are zero. All completion claims remain false.

The actual coordinator invokes the ascent observer before each Ship step and
passes its permission to the real release sequencer. Withholding is latched and
does not grant engine, attitude or abort-trajectory control. The independent
engine-health interlock is recorded separately from provider decisions, including
when the provider budget is exhausted. These are synthetic telemetry channels,
not a validated navigation or engine-health estimator.

Ship stops at the existing M2 checkpoint, T+1000.7 seconds. The separated booster
also has this absolute ceiling; a branch still flying then is marked unfinished
and its exact integrated state is retained. It is never integrated farther and
then presented as simultaneous with Ship. Early booster contact remains its own
outcome, never evidence of successful recovery. Satellites keep the existing
postprocessed orbit-only propagation and are outside the cooperative scheduler.

The handoff keeps the **inner M2** authority identity and exact final state,
inventory generation and synthetic confirmed-separation counter. The study also
binds the **outer M3** authority. Continuation compares the complete separately
approved prefix grant and run ID, not only the unchanged inner M2 hash. Default
M2 contracts and handoff validation remain unchanged. Historical v1 prefix
records remain verifiable; `splashdown=False` is only the explicit legacy prefix
path and is not accepted as the new M3 composition grant. No predicted state
replaces an executed state.
An unclosed or unconfirmed deployment produces `handoff: null` with its reason;
it does not become permission to continue. In particular, a Director `continue`
that is overridden by the ascent latch may remain unconfirmed in the current
M2 tracker. That path needs explicit integration work before continuation.

`starship_m3_prefix_verifier.verify` checks saved records without running the
producer, tracker, supervisor or physics: execution-order clock and step bounds,
exact separation identity, bound ascent requests, source telemetry witnesses,
actual sequencer effect, strictly later confirmation, observed inventory and
exact handoff state. For v2 it additionally checks the booster grant, one
body-specific decision, command/site identity and a strictly later recorded
booster tick. It then runs the common six-DOF trajectory/contact record validator
before `verify_splashdown`. `booster_splashdown.passed` means record consistency;
`controlled_water_entry_envelope_met` can still be false. Neither field proves
water dynamics, survival or real recovery. The source manifest hashes active imported module files,
including an explicitly frozen source overlay. The verifier validates manifest
shape, not its equality to an external Git commit. It is record-integrity
evidence, not independent dynamics validation. It does not certify a booster
recovery, Ship return, real flight, or completion of M3.

## E2E / Runtime Verification

Unit tests exercise corruption, fixture wiring and the cooperative scheduler
with physical steps stubbed. They are not numerical-flight verification.

For a separately authorized numerical run, freeze the source and budget first,
then call the real `run_fixture` entry point once with the registered profile.
Persist `authority.json`, `approval-binding.json`, `source-sha256.json`,
`prefix-run.json`, `ascent.json`, `ascent-witnesses.json`,
`observation-trace.json`, `handoff.json` when available, `study.json`,
`verification.json` and `summary.json`. A raised coordinator exception carries
its partial execution-order trace, saved as `partial-shared-clock.json`.
Record the exact invocation, wall limit, result and remaining limitations in the
PR. Do not rerun an unsuccessful bounded flight automatically. A prefix that
passes these checks is still a component result, not M3 completion.
