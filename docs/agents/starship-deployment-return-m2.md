# M2: carry deployment decisions into checked return planning

This milestone is in progress, not completed implementation or qualification.
M1 evidence remains valid within its registered development conditions.

## First implementation boundary

The opt-in `scripts/run_starship_m2_handoff.py` connects existing fixture
deployment decisions to **one forecast** from the resulting observed inventory
and state. It does not select or dispatch a Ship return. The normal M1 Gateway route
and its fully-deployed assumption are unchanged; this is not a new Gateway
grant or evidence of live M2 control.

- The deployment tracker observes the existing synthetic confirmed-separation
  counter, not command acknowledgements. This channel is perfect in the current
  simulator; it is not a demonstrated estimator for uncertain real inventory.
- Each separation is associated with an observation interval. Exact release
  event times are separately checked against that interval in the saved plant
  log; the policy cannot claim it sensed the exact event time.
- The handoff retains the complete execution state, including actuators, and
  creates a separate quantized navigation origin for prediction. Prediction
  results never replace the execution state.
- All inventory must be released, or a later observation must confirm the
  sequence is skipped/inhibited. `held` is resumable and never means complete.
  The last deployment observation can be at most 2.001 s older than the state.
- A later observation confirms membership in the command's compatible state
  set. A running `continue` or an inhibited `collect_status` need not change
  that state; this observation alone is not proof of a new physical effect or
  a completed mechanism diagnostic.
- A deployment command changes the generation before the plant applies it.
  Resume therefore invalidates issued handoffs even before inventory changes.
  A new closed, confirmed observation is needed to freeze another handoff.
- Authority, profile, generation, observed inventory, complete state and
  observation history are hash-bound to the forecast. `require_current` checks
  deployment provenance only; it does not replace fresh physical-state,
  resource, area or deadline admission at future selection/dispatch boundaries.
  In this offline boundary smoke the producer has already stopped before
  prediction. Invalidation during a concurrently running flight remains a
  future M2 selection/dispatch test, separate from tracker regression coverage.
- The independent verifier reads stored JSON bytes and checks counter/event
  agreement, sequencer transitions against core events, Director observation
  copies, declared fixture case/fault, state continuity, vehicle mass/centroid,
  forecast input and file hash. It does not use the tracker to recompute those
  claims or certify physics. These mutually checked fixture records are not
  signed worker receipts or proof against coordinated record rewriting.

Runtime command (fresh directory required):

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/run_starship_m2_handoff.py \
  --approve-simulation --case operations_notice \
  --output-dir output/m2-handoff-operations-notice
```

The fixed budget is one launch/deployment producer to T+1000.7 s and one
center-only 6DOF return forecast (180 s wall deadline), no model calls and no
automatic retries. The producer also retains the existing separately integrated
booster branch; this is not concurrent booster control or recovery validation.
Every completed stage and any exception remain in the output directory.

One center forecast is not a four-trial M1 admission set. Wiring this handoff
into an approved M2 actor, refreshing all candidate checks and reservations,
phase-specific model deadlines, and observing actual return execution are still
required. No live M2 flight or mission-completion claim follows from this slice.

### Bounded runtime evidence

The [CLI evidence](../assets/starship-m2-handoff/boundary-smoke.json) preserves
both attempts. The first stopped before handoff/forecast because the inherited
booster branch lacked `ReturnSites`. After repair, the user authorized exactly
one additional trial; no automatic retry or model call was made.

The repaired CLI completed on source `1ecc01dd` in 97.7 s:

- One confirmed release, 25 retained; the sequence was observed `skipped` at
  T+1000.45 s. The handoff preserves the complete T+1000.70 s execution state
  and uses a separate quantized 25-retained origin for the forecast.
- The independent stored-record verifier passed. All 75 saved source hashes
  match that commit. The Ship observation trace is byte-identical to the first
  attempt, so the wiring repair did not change that prefix.
- One forecast used the original T+2280.70 s return request. Its predicted
  contact is T+4953.48 s, 3.5142 m/s, 0.5015 degrees, 45.812 t remaining
  propellant, at 5.8959 N / 167.4149 E. This is neither a dispatched Ship return
  nor an admitted recovery-area/uncertainty result. The existing booster branch
  does not demonstrate recovery.

Latest boundary tests: 105 passed. The related set passed 212 tests (4 skipped)
before the final two record-integrity checks; counts overlap. No four-trial
candidate admission, live M2 actor, or executed Ship return was added here.

## Goal

Within a single preflight-approved mission scope, MissionOS observes a change
while deployment is active, decides whether to continue, hold/resume or reduce
release, carries the observed remaining inventory and actual time/state into
return planning, chooses an admitted plan, executes it and checks later results.
A numerical tool belongs to MissionOS. Agreement with Rules or a fallback is
not an AI-capability score. Keep AI action, regression, safety-net behavior,
record validity and mission outcome separate.

## Verified starting facts

- The 6DOF profile releases every 15 s, with the first slot 45 s after the orbital
  gate (`starship-sixdof-profile.json`:394,460). These are simulator settings,
  not a claim about SpaceX's operations.
- Existing Director decisions expire after 75 s at most. Start is additionally
  clipped before the first release; return time and active hold expiry also
  bound decisions. Hold is 30 s maximum; reassessment starts after 5 s.
- Existing Jev transport permits 30 s. A short-slot actor deadline does not make
  Jev respond faster. M2 must derive provider/cancellation time from remaining
  phase time and discard late/stale responses.
- M1's 150 s actor / 120 s DeepSeek window is explicitly post-deployment coast
  only. It requires a prepared uncertain comparison with at least two admitted
  candidates and 150 + 60 + 2.001 s before protected opportunity/notice expiry.
- The raw return predictor accepts remaining inventory 0..26, but assumes no
  subsequent release. M1's observed-origin adapter currently writes inventory 0.
  Directly attaching this route during deployment would misrepresent inventory.
- The measured 278.45 km displacement is a 60 s change to deorbit request time
  from one fully deployed origin, not a general rule for release holds.
- Existing delayed-hold evidence: hold 25.25 s shifted 25 release timestamps by
  10.75 s; deorbit residual was 0.174 m / 0.000909 m/s and contact 4.60835 m/s.
  Thus release timing changes can be small; candidate reachability must be
  measured per state rather than inferred from the deorbit displacement.
- Every M1 return candidate already requires three observed-state samples and
  a half-step center forecast to meet contact/area/time bounds. The roughly
  77 km grid displacement is not ignored, but these finite samples are not a
  converged numerical error bound; 20 km padding does not cover that discrepancy.

## Proposed scope and policy

1. Freeze study areas, notices, resource limits, allowed hold duration and model
   budgets before live evaluation. Development reachability screening may use
   the actual 6DOF predictor; record any scenario construction openly. Do not
   move regions or widen tolerances after seeing the evaluation result.
2. Introduce an explicit deployment-to-return handoff: measured inventory,
   confirmation quality, last release timestamps, state timestamp, sequencer
   terminal/held condition and authority revision. Never reconstruct inventory
   from a selected policy label or use future fault truth.
3. Initially run return prediction only once no further release is scheduled.
   A proposal to resume changes that condition and invalidates previous return
   forecasts. Supporting interleaved future release inside a forecast is a
   separate tool extension, not implicit coverage from the existing predictor.
4. Jev handles eligible short-window decisions only if the actual remaining
   deadline admits the call. Preapproved finite fallback handles immediate
   obligations. DeepSeek remains limited to prepared orbital comparisons with
   time and resource headroom; the 150 s window must never apply to a release
   slot or critical burn by accident.
5. Recompute return candidates from the resulting observed state. Every choice,
   fallback and actual dispatch receives fresh independent scope and feasibility
   checks. Retain a currently valid reservation while new calculations run;
   invalidate it if updated observations truly invalidate its premise.
6. If no evaluated candidate is admitted, record exactly that, bounded coast or
   inhibition, expiry and the resulting unresolved outcome. This does not prove
   every physically possible return is impossible. It is a successful refusal
   check only; it is not a recovered mission or a substitute for M2's positive
   end-to-end return case.

## Acceptance matrix to freeze before execution

- Normal: preserve the approved deployment and return without unnecessary
  intervention; later observations establish the resulting sequence.
- Changed notice: a checked hold/resume or deployment reduction changes the
  observed inventory or timing; the return computation uses that actual state;
  AI selects an admitted return and modeled contact/area checks pass.
- No admitted candidate: no unchecked burn or fabricated success; a bounded
  preapproved continuation is observed and mission outcome remains unresolved.
- Late/invalid/no response, including cancellation: no stale response commands
  later state; preserved reservations/fallbacks are rechecked and their actual
  terminal outcomes are recorded.

Keep live/model receipts separate from fixtures and injected faults. A fixed
model call and flight budget is set before execution; no success-until-retry.
A changed plan is not itself a pass; a justified maintained plan can be valid.
M2 does not claim arbitrary-site guidance, booster recovery or multi-flight
control. Numerical convergence and tighter footprint uncertainty are explicit
prerequisites before resource reservations are treated as operational guarantees.

## Human workload instrumentation now; claims later

Record approvals, delivered escalation requests, actual operator responses,
response latency, operator active interaction time, model wait, tool wait and
mission wall/simulated time separately. Zero inflight commands is an autonomy
observation, not measured labor saved. Before any labor-savings claim, register
a human-operated procedure with the same observations, tools, deadlines and
scenario distribution, collect actual operator activity and compare outcomes
and active work. Fixture decisions or hypothetical button counts are not a
human baseline. A first baseline can be a bounded replay exercise, labelled as
such; it must not be silently equated with real-time live operations.

## Parallel booster preparation (read-only, not a recovery claim)

The current generic RCS model is shared: 10 kN at a 4 m radius with an
opposing pair gives 80 kN m. The generic controller clips requested angular
acceleration at 0.03 rad/s²; allocation then applies the actual force limits.
This request ceiling is not an acceleration directly assigned to the vehicle.

At one saved minus-1-t coast sample, inertia was 131.766 million kg m², giving
about 0.000607 rad/s² from those jets alone. The request ceiling is about 49.4
times that value, and sampled coast commands saturate. This supports examining
coast attitude control; it does not prove the sole cause or establish that the
whole vehicle has 50 times too little control authority in every phase.
Existing development controllers already account for inertia and available
RCS torque. They are not the normal splashdown controller or qualified recovery.

The [saved failure and rejected powered-alignment candidate](../assets/starship-hold-reassessment-20261007/summary.json)
show why loosening the ignition gate or increasing thrust is not a demonstrated
repair. A later bounded comparison should keep physics and ignition limits fixed
and change only the coast attitude command/controller; measure fuel, alignment,
and remaining braking distance together. No new comparison was run for this
preparation. Historical records are not latest-source flight executions.

## Read-only source pointers

- `src/runtime/starship_mission_director.py`:68-92,133-188,256-285.
- `src/runtime/starship_sixdof_mission.py`:522-568.
- `src/intelligence/starship_mission_director.py`:36-126.
- `src/runtime/starship_orbital_window.py`:10-18,25-37,63-116.
- `src/runtime/starship_replanning.py`:141-169.
- `src/runtime/starship_return_prediction.py`:44-96.
- `docs/agents/starship-mission-control-contract.md`:149-177.
- `docs/agents/starship-return-replanning-m1.md`:162-169,421-442,554-571.
- `docs/agents/starship-high-cadence-operations.md`:45-66.
