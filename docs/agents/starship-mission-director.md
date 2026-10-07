# Mission-wide Starship decision envelope

This opt-in development slice connects mission decisions across deployment,
return and booster recovery. Numerical tools belong to MissionOS. They are
neither replaced by language models nor treated as the project's adversary.
Normal monitoring starts before a deployment interlock. Unmanaged simulation
and narrow supervision scenarios retain their separate approval formats.

## Authority and contract

The new `sixdof_managed_*` scenarios require a new displayed plan, `/approve`,
then `/run`. Approval binds `mission_envelope`, source/profile hashes, one of
five synthetic conditions and a same-start fixed-timeline comparison. Within
this envelope **MissionOS decides**; routine operations require no further
human command. Internally the model returns a decision candidate, the worker
independently checks it, the sequencer/controller applies it, and the verifier
checks subsequent observations. A provider answer is never approval or execution.

| Decision point | Approved choices |
|---|---|
| Deployment start, without an alarm | Continue, bounded hold, stop remaining releases, collect mechanism status |
| Deployment monitoring after the first attempt | Same four choices |
| After a mechanism diagnostic | Continue or stop |
| Once during an explicit hold | Continue, collect mechanism status, or stop |
| Return selection before the fixed deorbit time | Fixed terminal guidance or the existing retained-payload guidance |
| Early booster return | Decide capture or divert, subject to independent arrival qualification |

Limits: six assessments per flight, at most six Jev and six conditional
DeepSeek calls in live mode, one mechanism-status request, total hold budget
30 simulated seconds, at most 75 simulated seconds per response. Initial
assessment expires before the first planned release slot (about 45 seconds after
orbit insertion in this profile, about 44.5 seconds after the request). A pending model never
freezes the plant; integration is paced while awaiting the host broker.
The scheduled return cannot be delayed. Release feasibility, fuel floor and
qualified capture authority remain independent dispatch conditions. A notice
cannot change these limits. The configured 28 t release fuel floor is a
development-profile constraint, not a certified state-dependent return reserve.
The grant also approves an initial nominal deployment plan, fixed return
guidance and diversion. Initial deployment cannot start while its decision is
pending; the model or the preapproved fallback resolves before the first slot.
Orbital dynamics continue. Later monitoring runs alongside the authorized sequence.
The stored verifier checks observation release inventories against independently
validated separation events and rejects release before the start decision.

Out-of-scope, expired and invalid decisions record a reason and apply the
preapproved fallback: continue the approved nominal release plan only when the
current sequencer is running, no notice is unresolved and persistent fuel,
mechanism and return-time constraints hold; otherwise inhibit further releases.
A temporary perigee/pressure/rate gate keeps the sequence queued: the executor
independently skips that release slot and reevaluates at the next slot. Keep the initially
approved fixed return policy instead of forcing retained-payload guidance.
Divert remains the booster fallback. Never restart a held, inhibited or stopped
sequence through fallback. An explicit hold schedules one reassessment five
seconds after dispatch. The request must bind the actual hold's 30-second expiry;
its deadline cannot extend that expiry or the immutable return deadline. Only
a valid checked decision can resume the held sequence. A status request may use
the existing one-observation budget and diagnostic point before expiry. Hold
expiry disables remaining releases, including while an answer is pending.
There is no second hold or repeated reassessment budget. An escalation request can
be recorded, but this first slice does not yet accept a new human grant during
flight. It uses the preapproved no-response behavior. This limitation must not
be described as a working asynchronous human handoff.

`hold_used_s` reserves the entire 30-second allowance when a hold is selected;
it is not the measured duration of waiting. Actual waiting comes from the
hold and resume/expiry event times. The extra reassessment is omitted when no
hold is selected, so normal runs do not incur an extra provider call.
Hold is rejected unless more than `maximum_hold_s + decision_expiry_s` remains
before return (105 seconds here), reserving time for the return decision.
This bound has no additional integration-step or post-dispatch observation
margin. In a last-window hold followed by two timeouts, return fallback can
occur on the deorbit step and lack a later orbital observation. A first
deployment assessment created at/after return can likewise have a nonpositive
deadline and raise `invalid_decision_deadline`. Those late schedules are not
qualified. Accepted runs use the profile's early deployment window; do not
generalize their verification to return-adjacent holds or late release starts.
Requests expose only presently eligible choices. A used mechanism-status
budget or a remaining diagnostic window of at most two seconds removes
`collect_status`; dispatch checks these limits again against fresh state.
Collecting status preserves an inhibited/skipped state. A clear status report
alone cannot unlatch an interlock. The verifier checks release-free pause
intervals, absence of release after stop/inhibit/expiry, and an actual resume
event with a later running state (or a separately observed new interlock).

The current contract is `mission_envelope.v4`, with `director_request.v3`.
Historical v2/v3 approvals cannot acquire the reassessment or increased call
budget: create and approve a new plan. The fallback is a
bounded simulator operating policy, not a certified safe mode. Plant faults,
valid decisions to suspend deployment, and insufficient fuel can still lead to
the unsuccessful returns below. Do not claim that continuing is always safe.

## Observations and tools

The strict provider boundary contains simulation time, phase, release tracker,
ACK, sequencer, executor hold-expiry timer, quantized fuel gauge, return deadline, tower status, four tool
fields and a bounded operations notice. It excludes scenario name, future fault
schedule, true inertia/mass, physical force histories and terminal outcomes.
The release tracker and mechanism status are explicit synthetic sensors. Fuel
has reproducible bounded synthetic noise and 100 kg quantization; the release
check subtracts the 100 kg error bound. This is not a calibrated sensor model.
The development GNC still uses ideal plant state for its local controller.
The hold timer is command-ledger state, not hidden physical truth or a fault
schedule. `response_invalid` marks a missing/non-string action; it does not set
`escalation_requested`. A string outside the approved choices records a
system-generated referral, not proof that the model explicitly asked a human.
The verifier replays both flags. Human notification remains unimplemented.
`retained_payload_present` is an inventory observation, not a return-feasibility
certificate. It replaces the misleading name `retained_payload_possible`.

`collect_status` requests a separate actuator/latch-status channel and holds
deployment for at least two simulated seconds. The simulator supplies a later
mechanism report, and MissionOS reassesses. It is not a hardware camera or a
measured SpaceX mechanism. Tiny attitude/inertia identification is not a
prerequisite for this mission-management implementation.

Jev directly chooses among bounded decisions or requests DeepSeek reasoning.
Neither API is called in fixture mode. Fixtures exercise governance/execution
and form a transparent scripted-policy comparator; they are not AI evidence.
Provider keys remain in the Gateway. The simulation subprocess receives a
credential-free environment. Requests and replies bind run/request, observation
and envelope hashes. Fresh dispatch-time observations are checked again.

## Comparison and completion gate

Five synthetic conditions are frozen: normal; accepted release without physical
separation; propellant loss; unavailable tower; a payload-operations suspension
notice. Each runs a fixed timeline and MissionOS from the same initial state,
profile and fault sequence. Both share physical release constraints and the
same unqualified-capture restriction. The fixed timeline does not interpret
the notice or switch terminal return guidance. This is not a claim that a
well-designed conventional flight manager could not do both.

Record validity (`verification.passed`) is separate from
`comparison.comparison_accepted`. The completion gate is:

- Normal: exactly equal recorded terminal metrics, no hold/diagnostic/stop.
- Abnormal: orbit result and scheduled return time preserved; measured contact
  speed no higher than baseline by more than 0.01 m/s; deployment count no
  lower, except for the explicit suspension notice, where at most one release
  is allowed. The booster destination remains the approved divert site.
- All five conditions must pass; a model call or a dispatch alone is insufficient.
- Report fuel remainder, contact speed, deployment count, actual diversion
  arrival and every rejection separately. A low contact speed is not landing
  success. Failed acceptance remains failed, without tuning thresholds afterward.

This initial gate covers those selected outcomes, not general noninferiority
or safety: fuel/contact tradeoffs, noise, unseen notices, latency and controller
qualification remain unevaluated. Human comparison/workload is **unmeasured**;
zero in-flight commands in an automated test is not a measured labor saving.
Use a real per-step human comparator before claiming that saving. Numerical
policies can also investigate; this implementation does not claim otherwise.

## Physical and timing limits

The ship and launch-derived booster are integrated sequentially with independent
branch clocks, as in the existing harness. Their states are never reset. A model
does not receive other-branch future results. This is not a real-time concurrent
flight scheduler. The booster uses the declared synthetic 30 km east target and
finite control; changing the target does not establish reaching it. Current
launch-derived capture qualification is absent, so independent Rules prevent
capture and MissionOS selects divert. The new slice does not connect the
predictive catch solver or claim a launch-derived catch.

## Runtime checks

```sh
python scripts/start_starship_gateway.py --fixture-planner \
  --state-dir output/managed-fixture-state --port 18920
python scripts/smoke_starship_chat_gateway.py --port 18920 \
  --scenario sixdof_managed_normal --output-dir output/managed-normal
```

Repeat with `sixdof_managed_release_fault`, `sixdof_managed_fuel_shortage`,
`sixdof_managed_tower_unavailable`, and `sixdof_managed_operations_notice`.
Fresh output directories preserve unsuccessful attempts. The loopback smoke
uses a test operator, not authenticated human identity.

For live inference, keep `--fixture-planner` and add
`--enable-live-mission-director --project PROJECT_ID` to the launcher.
This separately opts into only the displayed per-plan provider budget.
Stop the live Gateway after the bounded run. Never publish keys, raw session
databases, local paths or unsanitized run directories.

For the production-boundary response-error fixture, use the same keyless launcher
and `--scenario sixdof_managed_invalid_response`. The displayed plan binds
`invalid_deployment_monitor`, and the worker injects an invalid action into a
scripted reply. Live mode rejects this scenario. This is not new model inference.
The standalone CLI also accepts fixture-only `--response-fault` values for
invalid replies or missing replies at a declared decision point. The verifier
requires that the configured point was exercised, checks the fallback against
fresh observations, and retains the original normal-outcome comparison gate.
The verifier recomputes the start deadline from the orbit-cutoff event and
profile, rejects premature timeout labels, and enforces a strictly positive
request window. `MISSIONOS_STARSHIP_FULL_FALLBACK_TEST=1` opts into four full-flight
pytest regressions; they are skipped in ordinary fixture CI.

The current managed verdict records a canonical JSON study digest, expected
and observed run IDs, and fingerprints of the named verifier source files.
Canonical digests are distinct from byte hashes in the artifact manifest.
Invalid studies have no accepted canonical digest. Historical verdicts lack
these fields. These identifiers bind the result to its input; they are not a
signature or proof that the fingerprinted files executed. Signed worker
receipts and artifact manifests remain the runtime evidence boundary.
For failed verdicts, retain the original study byte hash/manifest and supply
the expected run ID: a standalone failed verdict with null canonical digest
and no identified run is not independently bound to an invalid input.

## Optional offshore-entry contract

Current v4 hold and entry verification, with fresh output directories:

```sh
python scripts/start_starship_gateway.py --fixture-planner \
  --state-dir output/hold-entry-state --port 18935
python scripts/smoke_starship_chat_gateway.py --port 18935 \
  --scenario sixdof_managed_hold --output-dir output/hold-entry-http
python scripts/smoke_starship_chat_gateway.py --port 18935 \
  --scenario sixdof_managed_splashdown --output-dir output/entry-http
python scripts/run_starship_managed_mission.py --approve-simulation --case normal \
  --response-fault hold_deployment_monitor --run-id hold-monitor --output-dir output/hold-monitor
python scripts/run_starship_managed_mission.py --approve-simulation --case normal \
  --response-fault timeout_deployment_reassessment --run-id hold-timeout --output-dir output/hold-timeout
```

[The v4 summary](../assets/starship-hold-reassessment-20261007/summary.json)
binds each stored study/verdict and its source snapshot. Final HTTP hold and
entry checks exercise consumed approval, the key-free separate worker, later
observations, signed receipt/artifact hashes and cross-session rejection.
The final summary covers fresh post-review initial hold, post-release hold,
timeout, interlock-diagnostic and offshore-entry runs under the final source.
Pre-review snapshots remain historical local records. A separate batch whose
source changed during execution is excluded and its errors are preserved.
There are no new model calls.

`sixdof_managed_splashdown` requires `mission_envelope.v4` with the exact
`starship-splashdown-goal.json` object. It shares the bounded hold reassessment and
uses `capture/splashdown` at booster selection; capture dispatch is rejected
because that controller is not connected/qualified for this trial. Fallback
selects the preapproved offshore goal. Other current managed scenarios keep their
capture/divert behavior. Old source-bound approvals do not acquire this goal.

The historical v3 nominal entry and sensitivities retain their original source
snapshots. New hold-path checks do not requalify those results. The
`sixdof_managed_hold` fixture injects a valid initial hold, then uses the normal
fixture policy at reassessment; live mode rejects this injection. It tests
execution and later state, not the quality of an AI decision. An unnecessary
hold still fails the normal no-intervention comparator even if all payloads
are eventually released.

The original divert site's geographic declaration is a location reference.
The distinct water-entry goal binds its SHA-256 and its own approved area,
speed, attitude, rate and fuel limits. It does not relabel the old failed
surface-contact result. Both same-start comparison branches use the same
water-entry local controller; unchanged outcomes do not show model value.

Common six-DOF/contact verification runs first. The separate splashdown verifier
recomputes contact-point Earth-relative speed and normal/tangential components,
target distance and final pose/rate/reserve, and checks every producer flag.
Record validity and `controlled_water_entry_envelope_met` are separate outputs.
The surface is a sea-level ellipsoid proxy: water response and real clearance
remain explicitly false.

## Operating references

[NASA cFS Stored Command](https://software.nasa.gov/software/GSC-16009-1)
supports onboard absolute/relative command sequences. NASA's
[Initialization / Safe Mode guidance](https://swehb.nasa.gov/spaces/SWEHBVC/pages/85426332/9.10%2BInitialization%2B-%2BSafe%2BMode)
describes mission-phase-dependent safing and timely critical activity completion.
These inform separating a rejected reasoning response from an actual plant
fault. Neither source prescribes this payload-continuation policy, identifies
SpaceX's fallback logic, or certifies this simulator.

## Response-failure regression, 2026-10-07

The [v2 summary](../assets/starship-mission-decisions-20261007/fallback-summary.json)
binds four same-start full-flight fixture comparisons to source/profile hashes.
All four passed record verification and exact normal-outcome equality:
`invalid_deployment_monitor`, `invalid_return_selection`,
`timeout_deployment_start`, `timeout_deployment_monitor`. Each retains 26
releases, 4.546834477 m/s contact, the original return time and fuel remainder.
The Gateway monitoring-error scenario additionally passed fresh approval,
worker-signature, HTTP artifact-hash, replay and cross-session checks.

Reproduce it with the keyless launcher and `sixdof_managed_invalid_response`,
or the standalone CLI with `--case normal --response-fault FAULT` and a fresh
output directory. All are opt-in synthetic response fixtures, not model
performance results. The original five-condition gate was not rerun under v2;
the historical negative live record is unchanged. Exact equality in these
nominal fixtures checks preservation of the same approved controls, not general
robustness or model value. Their source snapshots precede the review fixes;
a separate post-review Gateway record covers the final worker logic.

A combined release fault and invalid return-selection reply was also run after
review. Both baseline and fallback impacted at 238.74 m/s, with all 26 payloads
retained. This is worse than the old v1 retained-guidance contact at 3.22 m/s in
that one condition. v2 keeps the initially approved fixed plan instead of
automatically selecting an unqualified adaptive controller. This tradeoff is
not a safe-return claim and remains a blocker for general fault recovery.

Late deployment/diagnostic requests can still obstruct return-selection
bookkeeping outside the current profile's early decision times. That preexisting
unqualified scheduling edge is not fixed or exercised by these regressions.

## Recorded development results, 2026-10-06

The [public summary](../assets/starship-mission-decisions-20261006/evidence-summary.json)
stores source/profile and artifact digests with selected observations and invocation
metadata. It excludes raw sessions, credentials and workstation paths. Its five
fixture pairs and two live pairs are frozen snapshots preceding the final
pending-start and observation-inventory checks; those records are not rewritten
as final-head live inference. A separate final-head keyless Gateway smoke covers
the added boundary.

| Mode / condition | Fixed timeline | Managed | Comparison |
|---|---|---|---|
| Scripted / normal | 26 releases; 4.55 m/s contact | Identical | Pass |
| Scripted / release fault | 0 releases; 238.74 m/s impact | 0 releases; 3.22 m/s contact | Pass |
| Scripted / tower unavailable | 26 releases; 4.55 m/s contact | Identical | Pass |
| Scripted / suspension notice | 26 releases; 4.55 m/s contact | 1 release; 619.78 m/s impact | Fail |
| Scripted / fuel loss | No contact before time limit | Same | Fail |
| Live / normal | 26 releases; 4.55 m/s contact | 1 release; 619.78 m/s impact | Fail |
| Live / release fault | 0 releases; 238.74 m/s impact | 0 releases; 3.22 m/s contact | Pass |

All seven stored pairs passed record verification. This is separate from the
failed completion gate. All booster branches selected/used the declared divert
site and failed its arrival envelope. There is no launch-derived catch or
qualified recovery result. The scheduled return time remained unchanged.

Live Jev: nine attempts, eight validated inference responses; one normal-monitor
response was `invalid_response`. Its cause is not established from hashed
response metadata. The consequent `stop_deployment` was a fallback, not an
accepted Jev command. Five valid Jev decisions in the release-fault run included
collecting a distinct mechanism report and selecting return guidance. Conditional
DeepSeek was never routed. Do not present this as an observed DeepSeek flight.

The release-fault improvement also occurs with the scripted policy. Neither
model superiority nor human labor savings are established. The no-response
fallback and both return choices are development mechanisms without a safe-return
guarantee; passing a scope check does not certify feasibility or safety.

Failed tuple-representation and disk-full attempts were preserved separately.
The full-suite rerun was interrupted by disk exhaustion; use targeted tests and
CI separately, without calling that run green. Claude Opus 5.5/high launched but
reported an expired login before reviewing; no completed Claude review exists.
