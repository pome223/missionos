# Return replanning milestone 1

## Observable completion

A launch releases its payloads. A fresh notice makes the planned recovery area
unavailable. MissionOS uses numerical tools to evaluate a bounded postponement,
continues monitoring during the wait, and updates its return plan from new
observations. Independent checks precede execution and later observations must
confirm the changed return and the original Ship contact limits.

Normal flight must retain its outcome without unnecessary intervention. A
missing model response must lead to a checked preapproved continuation, never
an unchecked return. These are three separate end-to-end cases. A model call,
record verification, or return candidate alone does not complete this milestone.

The wider goal remains flight-wide AI control and eventually repeated flights.
Numerical estimators are the AI's tools. Script parity is not an AI-value gate.
New authentication, a new control-room UI, fleet scheduling and tower catch are
outside this first milestone; existing inspection/report surfaces can show it.

## Operational basis and exclusions

[NASA's Crew Dragon return description](https://www.nasa.gov/reference/nasa-spacex-crew-launch-return-operations/)
describes repeated weather/recovery assessments and postponement or alternate
sites. This is an operational analogy, not Starship flight software, a Starship
endurance claim, or permission to copy Dragon's times and thresholds.

The existing fuel-shortage case has about 22.4 t left, below the 28 t reserve.
Waiting cannot make that case pass an unchanged reserve requirement. It remains
unresolved, not the successful postponement scenario.

The current surrogate lacks power, thermal endurance and propellant boil-off
models. They cannot be treated as infinite resources. Initial timing probes
measure only the effect of a short changed coast on the existing dynamics.
Before online postponement admission, explicit bounded assumptions or models
for relevant waiting resources, uncertainty and expiry are required.

## First gate: two full-flight timing probes

Freeze production source at `caf547f26ef53529f20d6490a1a4a04f2e457bff`.
Change only `guidance.coast_before_return_s`: nominal and nominal +60 s. Both
launch from the same physical initial state and release all 26 rigid payloads.
Use the existing opt-in development qualification path and
`trimmed_state_terminal_v4`. Do not change forces, gains, geometry, tolerances,
integration intervals or the existing qualification certificate.

Budget: two CPU flights, 900 wall seconds per flight, no automatic retry,
no hosted inference. Use the recorded NumPy 2.4.6 / SciPy 1.17.1 backend.

```sh
python scripts/probe_starship_return_delay.py --approve-simulation \
  --delay-s 0 --output-dir output/return-delay/nominal
python scripts/probe_starship_return_delay.py --approve-simulation \
  --delay-s 60 --output-dir output/return-delay/delay60
```

Output directories must not exist. Preserve inputs, source copies, full study,
verifiers, final contact coordinates/time, and the result even on failure.
Process exit 0 means source and record checks passed, **not** successful return.
The separately reported `contact_limits_met` requires orbit, 26 releases,
contact speed <=5 m/s, tilt <=5 degrees, rate <=0.02 rad/s and fuel >=28 t.

Compare the complete physical sample prefix before the nominal return request
and all payload-release records. Measure the contact-location/time shift rather
than assuming the delayed flight reaches the original recovery area. Neither
probe certifies an area, water entry, waiting endurance or an online decision.

If this first gate fails, diagnose the saved samples and identify the control,
trajectory or runtime limitation. Do not turn it into an unlimited parameter
search or hide the failure behind successful log validation.

## Work after the first gate

1. Provide a resumable prediction state with finite actuator/controller history.
   An offline launch replay is not a rollout from a current observed state.
2. Evaluate bounded return-time candidates from observations/estimates, without
   giving the policy future faults or plant truth. Include fuel, duration,
   contact footprint, uncertainty, resource bounds and validity limits.
3. Separate reproducibility of the six-DOF forecast from independent constraint
   checks and uncertainty/numerical-error assessment. Repeating the same model
   does not validate the model or detect its shared implementation errors.
4. Connect Jev's tool requests, wait, new observation and revised plan to the
   existing authority/execution boundary. DeepSeek is conditional on actual
   need; invocation count is not the acceptance criterion.
5. Advance the plant clock while tools/models are pending; bind results to plan
   revision and state, then recheck at dispatch. Reject stale results and
   out-of-scope changes. The old 39-trajectory matching envelope stays intact.
6. Run normal, changed-recovery-condition and missing-response E2Es; distinguish
   fixtures from actual inference and preserve unresolved/failing outcomes.

The two-flight gate passed after re-reading the saved JSON artifacts. Both
original processes reported invalid JSON because the first probe version passed
in-memory tuples to the verifiers. No dynamics were repeated. Original verdicts
and executed source hashes remain in the audit. Contact shifted by about 278 km;
no recovery area or wait-resource admission was demonstrated.

Current implementation and evidence status are recorded in the
[timing-probe report](../examples/starship-return-replanning.md). Completion of
the two-flight gate does not close M1.

## Second gate: saved-state return predictions

The next tool maps an absolute deorbit-request time to a predicted contact
position/time, contact speed/attitude/rate and propellant. It does not yet choose
or approve a recovery area. Freeze the area/time availability requirements
before selecting a candidate; do not put an area under a computed endpoint and
then count it as independently demonstrated target guidance.

For the first reproduction test, take the last saved `orbital_coast` sample at
or before the nominal return request minus 90 seconds, after all 26 releases.
Preserve position, velocity, attitude, angular rate, fuel and every finite engine
and flap state. Both forecasts use this identical origin and the nominal
profile. Vary only the requested return time, nominal versus +60 seconds.
The full-flight records are compared **after** the forecast; no future sample
is an input to its dynamics or control.

The original PR #127 screen accepted only orbital-coast origins, retained counts
0–26, a return request within 180 seconds of the origin, and a positive prediction
horizon of at most 4,000 seconds beyond the origin that includes that request.
Those remain the default bounds without an explicit opportunity-window input.
Those are software bounds, not a qualified flight envelope. Only the two tested
zero-retained candidates are covered by this screen. Entry/terminal restarts,
pending releases and persistent mission-director state are unsupported.

PR #127 reused the plant, actuator control, trim preparation and terminal budget,
but copied the return phase machine. Its saved artifacts retain the original
15-file source binding. The implementation below removes that copy; historical
artifacts are not relabelled as executions of the shared controller. Dispatch
through a forecast remains unimplemented.

Use the fixed backend from `spaceflight-qualified`. Two CPU forecasts, each
limited to 900 wall seconds; no search, hosted model or new full flight:

```sh
python scripts/study_starship_return_candidates.py --approve-simulation \
  --nominal output/return-delay/nominal --delayed output/return-delay/delay60 \
  --delay-s 0 --output-dir output/return-candidates/nominal
python scripts/study_starship_return_candidates.py --approve-simulation \
  --nominal output/return-delay/nominal --delayed output/return-delay/delay60 \
  --delay-s 60 --output-dir output/return-candidates/delay60
```

The caller enforces wall-time limits. Each command requires a fresh directory
and preserves source copies, input/backend hashes, prediction, reference suffix
and checks. The shared `write_verified_input` function serializes strict JSON,
creates a new artifact and reads it back before verification. The full-flight
timing probe uses the same boundary. No verifier is relaxed to accept tuples.

Independent record checks compare physical state and finite actuators at shared
sample times, phase-event times, final state and contact receipt exactly; sample
coverage must be at least 95% and is reported. They also check time advancement,
fuel monotonicity and material-point speed relative to the rotating surface.
Exit 0 means reproduction checks and source binding passed; contact limits are
reported separately. Neither result means recovery-area or flight admission.

This input is explicitly saved **plant state**, not a sensor-state estimate.
It must not be given to Jev/DeepSeek as if it were available telemetry. Subsequent
work must propagate observation uncertainty, model waiting resources and expiry,
advance the plant during computation and check the current state before dispatch.
Matching the original simulation cannot establish model accuracy or robustness.

### What the displacement does and does not establish

The measured 278.45 km displacement applies to this origin and 60-second change.
It does not establish that every delay or orbit has that displacement. Current
bank selection minimizes aerodynamic trim/control problems; it is not feedback
on a desired contact latitude/longitude. Aerodynamic lateral motion is possible,
but no specified cross-range target is qualified here.

The osculating two-body period at the recorded return state is approximately
90.85 minutes, during which the modeled Earth rotates about 22.77 degrees.
One additional orbit therefore does not imply revisiting the same recovery area.
This screen admits no one-orbit waiting scenario and no waiting-endurance claim.

## Decisions before operational integration (review of PR #127)

These decisions govern the shared implementation below; they are **not flight
authority**. The default 180-second input bound and 4,000-second prediction
horizon remain for the original interface. In the tested origin, nominal return
is 90 seconds ahead: that bound
permits at most 90 seconds of postponement beyond the original plan, not 180.
The two-point displacement measurement cannot establish an 800 km reachable
corridor or justify linear extrapolation to later orbits.

### One return controller for execution and prediction

Extract the return phase transitions and command generation into a shared,
stateful component called by both the flight loop and the forecast. Remove the
mirrored phase machine before MissionOS can dispatch a predicted return. Keep
the plant integration/contact path shared too; do not duplicate its sampling,
event ordering or finite-actuator behavior in a new wrapper.

The restart contract must preserve phase, planned request time, return-policy
status, trim preparation/attempt count/next attempt, transported reference frame
quaternion/time/bridging state, landing-frame state and prior terminal budget.
Vehicle position/velocity/attitude/rate/fuel and all actuator states remain
separate plant state. A forecast copies its input context and cannot mutate the
executor's context. Scope remains post-deployment Ship return; existing fixed
and legacy return policies must keep their behavior.

The predictor answers what this Executor would do. Independence belongs to
constraint/result checks: recovery geometry and time, fuel/resources, authority,
freshness, and explicit observation/model/numerical uncertainty. An uncertainty
sweep is empirical coverage, not a proof that every possible state is safe.

Acceptance for extraction: preserve sampled commands, physical states, event
ordering and outcomes against frozen records, including normal, retained-load,
terminal-engine-fault and unresolved-return paths. Add restart/context-isolation
checks. Merely importing the new function from both callers is insufficient.
The mission file is qualification-bound: extraction invalidates the current
certificate. Add the new dependency to source binding and requalify before
enabling the changed runtime. The current builder requires fresh evidence for
all 39 cases; never replace old source hashes to label old runs as new execution.
Begin with a bounded representative check before spending on the full census.

### M1 covers a later orbital opportunity

Keep same-pass adjustments as regression/engineering probes. M1's operational
target includes evaluating a return opportunity on the following orbit, with a
first development search ceiling of **6,000 seconds beyond the original planned
return request**. This covers roughly one additional modeled orbit, not a claim
of arbitrary hours/days of loiter. A fixed original deadline prevents a moving
six-thousand-second window from permitting unlimited postponement.

NASA's [Dragon return procedure](https://www.nasa.gov/reference/nasa-spacex-crew-launch-return-operations/)
includes an approximately 48-hour backup opportunity after a wave-off. This
supports considering later opportunities, not copying that endurance or timing
to Starship. The first M1 scope does not reproduce such a 48-hour operation.
Neither a following orbit nor the 6,000-second ceiling guarantees an approved
recovery site is reachable. Waiting for weather to clear within 60 seconds must
not be invented to fit the existing tool.

Before the scenario, freeze recovery-area geometry, supported time intervals,
recovery-resource assumptions, and waiting-resource bounds. Recheck actual
availability from later observations; a forecast recovery time is not a future
fact. Power/thermal/propellant bounds must be explicit modeled assumptions or
supported data with expiry, never free/infinite waiting. If the later opportunity
is unreachable, exceeds resources, or remains uncertain, preserve that result
and leave the successful-replanning milestone incomplete.

For the **first later-orbit technical screen**, plan at most four fixed 6DOF
forecasts from the same origin: original request, +60 seconds, +one osculating
period, and +one period+60 seconds. Compute and freeze the period from the origin;
do not tune candidate times or recovery areas after seeing terminal outcomes.
Ceilings: two CPU workers, 900 wall seconds per forecast, 1,800 seconds for the
whole batch, no hosted model calls, no automatic retry. Include rejected/failed
candidates in that budget. The bounded probe below implements this fixed candidate set. The extended
horizon includes both coast and descent; raising only `180` is insufficient.

Measure later-orbit runtime before fixing the live tool budget; 48 seconds was
measured only for the short coast. Live computation must finish before a fixed
decision deadline with time left for revalidation and execution. Its budget
must count uncertainty checks and repeated requests, advance the flight clock,
and use a checked preapproved continuation on expiry. Do not freeze the flight
or remove uncertainty checks just to fit a candidate count.


## Shared implementation and bounded opportunity probe

`starship_ship_return.ReturnController` owns coast-to-return transitions,
entry trim history, reference-frame history and terminal decisions. Both
`starship_sixdof_mission.simulate` and `starship_return_prediction.forecast`
call its `update` and `command`. Sampling triggers and bracketed contact stepping use shared helpers.
The flight wrapper retains its existing opt-in integration scale; the predictor
uses scale 1. The controller invokes the
same existing actuator/GNC functions; it does not add forces or alter gates.

The JSON context schema `missionos.ship_return_controller.v1` preserves all
controller fields, including the exact reference quaternion (no second
normalization on restoration). `from_dict` rejects missing/extra fields,
non-finite JSON, mismatched policy and invalid reference frames. Restoring this
history grants no authority. Flight admission still happens in the mission loop
before `start`; forecast results cannot dispatch. Unit checks exercise all five
phases, context isolation, commands and finite stepping after round-trip.
The public forecast interface still starts from post-deployment coast only;
restoring a controller alone does not restore pending MissionOS transactions.

The new module is bound by the plan source hash, execution manifest, qualification
builder and independent feasibility verifier. A source change invalidates the
old certificate. The regression audit compares fresh full flights against the
previous certificate's exact study hashes, then compares every run field except
`development_source_sha256`. It includes samples and commands, event order,
separated satellites, booster execution, return history and final state.

```sh
python scripts/run_starship_return_qualification.py --approve-simulation \
  --retained 0 --wind-trim --output-dir output/shared-return/census/retained0
# Repeat for retained 1..26 and +/-1000 kg at 0,14,15,16,25,26 (39 total).
python scripts/build_starship_return_qualification.py \
  --input-dir output/shared-return/census --output output/shared-return/qualification.json
python scripts/verify_starship_return_refactor.py \
  --previous-dir output/previous-census --current-dir output/shared-return/census \
  --previous-certificate output/previous-qualification.json \
  --previous-certificate-sha256 <reviewed-previous-certificate-sha256> \
  --output output/shared-return/regression.json
```

Review the newly built certificate before installing it at the registered path.
Do not edit its hashes to bypass missing executions. The fixed backend, contact
limits and 39 coast-matching corridors remain unchanged in meaning; none cover
following-orbit returns.

`probe_starship_return_opportunities.py` requires a current-certificate-bound
nominal census run. It selects the same complete coast snapshot 90 seconds
before the original return request, computes its osculating period, and writes
all four candidate times before executing one. Each invocation uses a fresh
directory. Run only the four specified slots, at most two concurrently, with
external timeouts of 900 seconds each and 1,800 seconds for the complete batch.
Do not launch another slot after a failure without diagnosing and recording it.

```sh
python scripts/probe_starship_return_opportunities.py --approve-simulation \
  --reference-dir output/shared-return/census/retained0 --candidate nominal \
  --output-dir output/shared-return/opportunities/nominal
# Other fixed slots: delay60, next_orbit, next_orbit60. No search or retries.
```

The explicit `missionos.ship_return_opportunity_window.v1` input anchors a
6,000-second delay ceiling to the original scheduled request. The origin must
precede that anchor by at most 180 seconds. Its prediction horizon is at most
10,000 seconds from the origin, including descent; the shorter default interface
is unchanged. This is an offline compute bound, not delegated authority.
Inputs, complete predictions, captured source, backend and terminal outcome are
preserved. Nominal reproduction uses the saved full flight only after prediction.
The other slots report the unchanged four contact limits, positions and times.

A contact pass is not an available recovery opportunity: no region or availability
window is approved here, and waiting power/thermal/boil-off are not modeled.
Do not place a recovery region around the resulting point afterwards and call
that a mission success. All inputs remain explicit development plant states,
not uncertainty-bounded onboard estimates; model calls and runtime admission
remain zero. These limitations must accompany any reported later-orbit result.


### Shared-controller requalification result

All 39 freshly executed flights pass the unchanged contact gate and exactly
match their corresponding previous source-bound flight records. The comparison
covers **97,703 samples**, including command/controller fields, as well as every
run field other than the intentionally changed source-hash map. Maximum contact
speed is 4.691169 m/s, maximum contact tilt 2.754979 degrees, and minimum remaining
propellant 45,231.488 kg. No retries or parameter changes were used.

The installed certificate binds 16 core files, including the shared controller.
Removing only source/study identities from the old and new certificates produces
identical data: corridor coordinates/times, interpolation tolerances, limits,
backend and empirical margins have not expanded. Historical evidence remains
under its original identity. The [compact regression audit](../assets/starship-return-replanning-m1/shared-controller-regression.json)
records both study hashes for each case and the checks applied to every case.


The terminal-engine-loss CLI also preserves the previous 22.734699 m/s impact,
including the domain-violation event and post-trigger integration. Both branches
of the fixture fuel-shortage comparison preserve `return_inhibited_unresolved`;
that remains an unresolved return, not a contact pass. Their physical samples,
commands, final states/outcomes and event times match the saved previous runs.
Current independent record/return/admission checkers pass. Changed certificate
identities in admission receipts are excluded from the historical physics
comparison and checked separately against the current certificate.


### Fixed following-orbit screen result

All four fixed candidates pass the unchanged four modeled contact limits. The
period frozen from the common origin is 5,451.309975 s; requested return times are
T+2,280.7, 2,340.7, 7,732.009975 and 7,792.009975 s. Request times are lower bounds;
the first actual control tick at or after each time executes the transition.

| Request delay | Contact speed | Tilt | Propellant | Contact latitude / longitude | CPU process wall time |
|---|---:|---:|---:|---|---:|
| Original time | 4.60826 m/s | 2.37486° | 62.39431 t | 4.88280° / 165.27819° | 45.44 s |
| +60 s | 4.61028 m/s | 2.38119° | 62.39420 t | 6.01916° / 167.51984° | 45.59 s |
| +5,451.31 s (one period) | 4.63322 m/s | 2.42069° | 62.42976 t | 5.35468° / 143.03096° | 84.15 s |
| +5,511.31 s | 4.63249 m/s | 2.41880° | 62.49358 t | 7.28258° / 146.88590° | 85.31 s |

The complete batch took 267.29 wall seconds with **one forecast
worker**, within the two-worker ceiling; Starship tests overlapped part of it.
No timeouts, retries or model calls occurred. The nominal forecast exactly
matches the new full-flight suffix. Both short candidates also match final
state/contact/event times against the historical PR #127 predictions; coverage
is 1,378/1,380 and 1,397/1,400 samples respectively. The shared controller records
additional activation/previous-budget witnesses; the 95% coverage threshold
was not changed.

This is saved-state prediction under a model, not a new dispatched flight or a
qualified recovery opportunity. Following-orbit contact is near longitude 143
or 147 degrees, versus 165 or 168 degrees for the short candidates. Recovery
geometry/availability, observation uncertainty and waiting power/thermal/boil-off
remain unchecked. The existing certificate cannot admit the later candidates.
The [compact screen](../assets/starship-return-replanning-m1/opportunity-screen.json)
preserves source hashes, candidate identities, actual transition times and these
false claim flags. M1's repeated AI decisions and three operational E2Es remain
unfinished.

## Integrated M1 actor (following PR #128)

The registered catalog entries are `sixdof_m1_normal`, `sixdof_m1_replan` and
`sixdof_m1_timeout`. Each uses the existing chat plan → `/approve` → `/run`
boundary. Approval binds the complete operations contract, code/profile hashes,
call and compute budgets, immutable original deadline and fallback. The host
alone holds provider credentials. The separate worker rechecks the signed
single-use grant and records a signed artifact receipt. Existing test-operator
approval does not authenticate a real operator. This integration does not add
in-flight human notification/approval delivery.

`starship_replanning_executor` runs launch and all 26 releases with the existing
production simulator, then continues its exact position, velocity, attitude,
rates, propellant and finite actuators after T+1000.7 s. It calls the shared
`ReturnController` and plant/contact stepper. It never replaces the plant with
a forecast. The initial launch/deployment controller remains deterministic;
M1 adds repeated post-deployment return supervision, not AI engine control or
continuous LLM inference throughout ascent. Booster recovery remains outside M1.

The two opportunity times are frozen from the first synthetic navigation
estimate: the original cutoff-plus-coast schedule and one osculating period
later. Neither monitoring nor repeated predictions can move the original
schedule +6,000 s deadline. A normal return still requires numerical checks;
`keep_plan` does not bypass them. Jev chooses whether to request evaluation,
wait, refresh the published recovery status and select a return. DeepSeek is
called only if Jev actually requests deeper reasoning, with a separate cap.

### Observation and uncertainty scope

Only the sensor adapter reads the plant for navigation/gauge telemetry.
Position, velocity, attitude, angular rate and fuel are quantized before the
prediction boundary. Perfect actuator position/health telemetry is an explicit
model assumption. The model receives the operational notice, budgets, predicted
contact quantities and constraint rejections, not the scenario name, future
notice, plant mass, inertia or hidden fault schedule. This is a synthetic sensor
adapter, not a flight-qualified navigation filter. Existing low-level GNC still
has the simulator's mass/inertia information; M1 does not fix that limitation.

Each tool request evaluates the estimated center and two fixed joint
perturbations: position components (±2,∓2,±1) m, velocity (±0.002,∓0.002,±0.001)
m/s, ±0.01° attitude, ±0.00001 rad/s per body-rate component and ±100 kg fuel.
All three must pass. A fourth forecast halves both powered and coast
integration/control intervals from the estimated center and must pass the same
contact, area and time checks. This is **sampled uncertainty and grid sensitivity,
not coverage of an entire uncertainty box, numerical convergence or a safety
proof**. A 20 km footprint and 30 s arrival padding apply to each of the four
forecasts as additional declared engineering margins; they do not bound the
coarse/fine discrepancy. Numerical step refinement is reported separately; reproducing the Executor is not independent physics
validation. The old 39-trajectory certificate and its tolerances are unchanged.

### Fixed study areas and finite waiting resources

`examples/spaceflight/starship-m1-operations.json` freezes two 125 km radius
circles around (5° N, 165° E) and (5° N, 143° E). These are **engineering targets
calibrated using the previous opportunity screen**, not independently chosen
real recovery sites. They are fixed before the M1 scenarios and new runs and
are not repositioned after a failed candidate. Inclusion demonstrates operation
within these declared synthetic areas, not arbitrary target guidance, surveyed
ocean clearance, weather validity, recovery-vessel reachability or cross-range
capability. No physical safe-ocean claim follows from a latitude/longitude test.

The notice sequence distinguishes current information from predicted recovery.
Normal operation has an available primary area. The changed case withdraws it;
the western team's availability remains unknown until a later published notice.
The AI does not see that later message in advance. The revised notice supplies
an availability interval, which is rechecked at dispatch and against actual
contact time/location. This is a recovery-asset scheduling scenario, not a claim
that weather clears within minutes.

Waiting resources are finite **accounting bounds**, not full subsystem models:
30 kWh initially, 4 kW draw, minimum 12 kWh; temperature upper bound starting
295 K and rising at 0.001 K/s, maximum 310 K; propellant-loss bound 0.02 kg/s,
deducted from predicted and measured terminal fuel margins. The resource clock
starts at the original return time and cannot reset. These assumptions expire
at the fixed deadline. Heat flow, actual vent dynamics, battery faults, slosh
and long-duration Starship endurance remain unmodeled.

### Async work, revisions and independent checks

A pending model/tool job advances the finite plant by at least the elapsed wall
time, with measured start/end/elapsed values in the record. Outside pending work
the numerical simulation can run faster than real time. Maximums: 12 Jev calls,
2 conditional DeepSeek calls, 12 forecasts, 2 forecast workers, 180 s per forecast,
600 s per batch, 35 s per decision, 4 plan revisions and 1,800 s per worker run.
No automatic retry is performed. A tool or model timeout cannot manufacture an
admission. Unanswered reassessment follows the preapproved numerical evaluation
and current checks; if no return is admitted, the result remains unresolved.

Request IDs, request hash, authority hash and revision bind a response. Candidate
selection checks the authority, all four contact results, resource margins,
current area availability and the current measured state against the forecast's
coast samples. The same independent scalar checker runs immediately before
return dispatch; it rejects expired notices, missed times and state drift.
The verifier repeats these checks from saved records, checks the commanded
return time, later activation event, actual contact geometry/time and unchanged
original contact limits. `verified` is record validity; unresolved operation or
an impact must never become a successful contact merely through verification.

The three HTTP E2Es and compact outcome evidence determine completion. Merely
registering the actor or passing unit tests does not close M1. For the normal
case compare the physical contact and timing with the original timeline. For
the changed case require an actual Jev decision, a later revised plan and actual
contact in its checked area/time. For the timeout case require a recorded
missing response, checked fallback and the final observed outcome.


### Review corrections and completion gate

The first integrated normal flight was preserved as unresolved: numerical
forecast results still contained NumPy scalars after being written to JSON, so
the strict in-process constraint checker rejected otherwise finite contact
results. The tool now returns the reloaded JSON value. A regression test injects
actual NumPy scalar values across that process boundary. The verifier is not
relaxed. This run is not counted as a successful normal flight.

The actor uses only the current notice's published expiry and a bounded periodic
monitor interval; the future scenario update time is not an actor trigger.
`keep_plan` requests only the mandatory nominal check, `evaluate_returns` adds
alternatives, and a requested ground-status refresh consumes a control step and
records the delivered observation. Waiting continues monitoring. In the timeout
case, all decisions after service loss remain unanswered; the following choice
is genuinely a checked fallback, not a later fixture response mislabeled as one.

Record validity remains separate from `case_accepted`. The latter additionally
requires nominal return for the normal case, a model-attributed following-orbit
selection and contact for the changed case, and persistent missing responses
plus a checked fallback for the timeout case. All require actual contact within
the independent limits and the selected fixed area/time. The HTTP smoke asserts
this case gate. The existing independent six-DOF sample and rigid-body contact
kinematics checks are also applied; a controller-supplied speed alone cannot
satisfy the gate.

A development replan attempt also remained unresolved after the live model kept
waiting despite a feasible future candidate. That failed flight is retained.
The action descriptions now explain that selecting an opportunity **books a
future burn**, while waiting consumes a finite opportunity; they do not assert
that the original opportunity is unavailable. The later runs are development
validation after that correction, not a held-out evaluation of model quality.

Completion attribution follows the revision actually dispatched. An earlier AI
selection that was withheld and replaced by fallback cannot count as executed
AI replanning. Exhausting the model-call budget creates its own request identity,
explicit `budget_exhausted` record and later observation, with no provider call;
the fallback cannot borrow the preceding model response. The timeout validator
requires missing responses from the registered update stage onward, including
later monitoring, rather than trusting only individual injection flags.

A requested recovery-status refresh retrieves the currently published coordinator
message. It may return the same message, and records `information_changed`.
It is not a second independent sensor or guaranteed new information. The later
published update is what changes recovery availability in this scenario.
Report single-choice calls separately from calls with multiple available actions;
invoking a model alone does not establish a meaningful tradeoff.

For this M1 Gateway route, record-valid but case-unsatisfied results have status
`unresolved`, retain signed/hash-checked artifacts for inspection, and are not
reported as `verified` success. Record-invalid results remain `failed`. This
does not change the older routes' definition of record verification.


### Numerical sensitivity discovered before final acceptance

Halving powered/coast integration and control intervals shifted contact by
77.291 km (nominal) and 76.572 km (later orbit), with arrival shifts 10.510 s and
10.415 s. Contact speeds remained 4.597/4.595 m/s. The original 20 km padding
cannot be presented as a bound on this discrepancy. No contact limit, target
center/radius or old certificate tolerance was enlarged. Instead, **every online
candidate now includes the half-step center forecast**, and all four forecasts
must fit the unchanged area and time windows with the same padding. This raises
tool use from three to four forecasts per opportunity while keeping the original
12-forecast flight cap and two-worker cap. Three opportunities/refreshes exhaust
that budget. A half-step failure rejects the candidate; it is not averaged away.

This is a finite-resolution development acceptance criterion. Two grids and
three observation points do not prove asymptotic convergence or bound all
intermediate trajectories. The measured location sensitivity remains a material
limitation for future real-site guidance or tighter recovery regions.


### M1 execution evidence

The final flight-source snapshot is recorded in
[`summary.json`](../assets/starship-m1-operations/summary.json), including hashes
of all 62 bound source/configuration inputs. Three fresh loopback Gateway runs
passed their registered case gates: normal (3 live Jev calls), recovery update
(10 live Jev calls), persistent outage (fixture responses before loss; none
afterward). DeepSeek calls: zero. All released 26 payloads, used no human
inflight commands, and met contact and fixed area/time conditions. Normal
final state/contact exactly matched the original timeline. Updated/outage cases
returned at T+7731.450 s and contacted at T+10378.626705 s, approximately
(5.340555° N, 143.003176° E), at 4.613326 m/s, 2.396269°, 0.00302825 rad/s,
with 62.433932 t remaining before the declared loss accounting bound.

The tests exercise `/approve` and `/run` over real HTTP, one-time authority,
ambiguous/unapproved commands, changed hashes, session isolation, credential-free
simulator processes, signed worker receipts and hash-checked artifact retrieval.
Approvals use a **test operator**, not authenticated human identity. A normal
flight uses four forecasts; changed/outage flights use twelve, including fresh
forecasts after the recovery notice changes. All computation/model waits advance
the plant, and the same independent checks gate AI and fallback dispatch.

An earlier normal flight failed on the JSON boundary, and an earlier live
replan stayed unresolved after excessive waiting. Both are preserved and listed
as development failures. The final cases are development validation of the
implemented scenario, not a held-out benchmark or an assurance of general
mission reliability. M1 is complete at this bounded simulation milestone;
fleet operations, real recovery safety, authentication/escalation and booster
recovery remain outside it.
