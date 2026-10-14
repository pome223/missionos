# M2 and M3: continuous mission control

Status: implementation and bounded development checks in progress. Neither
milestone is complete merely because its components or record tests pass.
Internal development only; no public synchronization or hardware authority.

The current full-M3 closure uses the [fixed completion campaign](starship-m3-completion.md).
Historical trials below retain their original finite budgets and evidence.

## Intended outcome

A person approves the mission and delegated limits before launch. MissionOS
then monitors, obtains evidence, decides, executes checked actions, and verifies
later observations. Only a requested extension of the approved limits goes back
to an operator. Numeric prediction and estimation are tools within MissionOS.
Scenario-written Rules and fallback parity are regression/safety-net evidence,
not an AI-value score or an acceptance condition requiring AI superiority.

M2 closes one continuous Ship flight: deployment choices change actual retained
inventory and timing; return planning uses that exact resulting state, while
prediction sees quantized observations; an admitted choice is executed from the
existing plant state and its contact, area, resources and time are checked.

M3 extends that flight through ascent monitoring and concurrent booster/Ship
control, with an observed booster splashdown attempt and a real in-flight
scope-amendment route. Tower catch is not required. A time-sorted concatenation
of independently finished flights is not concurrent control.

## Authority and phase timing

- One outer grant binds deployment and return authorities, profile, source
  identity, inventory generation, forecasts and budgets. A resumed or changed
  deployment invalidates a prior handoff.
- Deployment uses Jev with a deadline derived from the current release slot.
  Late/invalid replies never command a later state. DeepSeek's long orbital
  decision window does not apply to ascent, release slots or terminal burns.
- Return decisions require fresh checks at selection and actual dispatch,
  including fallback. A forecast is not area clearance or an executed return.
- Optional calculation must not consume an admitted return reservation's final
  decision/burn margin. Model and tool waits advance simulated physical time.
- In-flight authority extension names registered actions/resource ceilings,
  mission, vehicle, current revision, observations, original sim/wall deadlines
  and preapproved fallback. The local operator's authenticated response is
  signed and single-use. Receipt consumption still requires fresh physical
  feasibility and later observation. Local bearer authentication is not SSO or
  proof of a real person's identity.

## Frozen development sequence

1. Eight observed-origin forecasts of the retained-25 handoff at the original
   and next-orbit times, including the existing uncertainty/refinement checks.
   Model calls zero, no retries, original areas/limits unchanged.
2. One fixture M2 operations-notice flight from launch through actual Ship
   return, using the resulting retained inventory. Search, if required, uses
   three forecasts at next-orbit minus 120, zero, plus 120 seconds to suggest
   one departure time. Four real prediction trials must then admit it. This is
   bounded time search, not arbitrary-site guidance or exhaustive reachability.
3. Booster saved-state coast diagnosis: existing versus inertia/torque-limited
   stopping-distance controller, one attempt each, no change to physical RCS
   strength or ignition alignment gate. Stop before executing terminal thrust.
4. Connect actual concurrent stepping, inspect contact from the complete
   booster continuation and record failures. Freeze any further physical trial
   budget and inputs before running; never retry until success.
5. Real Gateway HTTP/worker checks: preflight scope, phase-separated providers,
   later observations, in-flight approval/rejection/expiry and single-use
   enforcement. Fixture or injected failures remain separately labelled.
6. Only after runtime readiness, bounded live cases with preregistered model
   budgets. Preserve unsuccessful output and cumulative provider counts.

## Completion evidence

| Boundary | Required evidence |
| --- | --- |
| M2 normal | All intended releases and return without unnecessary intervention |
| M2 deployment change | Actual inventory/time changes, fresh predictions, AI decision, same-flight execution and accepted observed Ship return |
| M2 unavailable/late reply | No unchecked burn, stale dispatch or fabricated recovered outcome |
| M3 ascent | Observed stage/health changes trigger bounded supervision before deployment; critical GNC never waits on a model |
| M3 concurrency | Bodies start at the identical physical separation state, advance on one coordinator clock, and are supervised while both remain active |
| M3 booster | Finite actuator/propellant dynamics through terminal contact and declared site/resource criteria; coast alignment alone is insufficient |
| M3 escalation | Delivered authenticated request, approval/rejection/expiry, checked actual operation, later observation, replay rejection |

No evaluated candidate means unresolved within the tested search, not proof that
all possible returns are impossible. Low-speed surface contact is not water
survivability or recovered hardware. Real SpaceX aerodynamics, avionics, thermal
protection, operational procedures and certification are not claimed.

Numerical step sensitivity remains a limitation: the existing four-trial check
includes a half-step forecast, but this finite sample is not a proven global
error bound. Keep it visible before treating area occupancy as a multi-flight
reservation guarantee. Actual operator activity is needed before labor savings
can be measured; zero in-flight commands alone does not measure labor saved.


## Current live evidence

The repaired `fe07fa2f` single flight passed record verification and its case:
Jev 7 attempts / 6 valid and adopted, DeepSeek 1 / 1, 12 validated numerical
references, 26 payload releases, accepted Ship contact/area and 8/8 booster
water-entry criteria. The deployment-to-return state is the executed flight's
state. One invalid deployment response used the checked continuation fallback;
no return fallback was used. There were zero in-flight human commands.

This closes the requested main-path demonstration, not the full completion
matrix. It does not combine earlier fixtures into a live scope-amendment claim
or establish robustness. The prior `cadfe521` failure remains intact: record
checks passed but deployment stopped and Ship return remained unresolved.
See the [repaired live evidence](starship-m23-review-notes.md#repaired-real-provider-flight-fe07fa2f)
and [previous unresolved attempt](starship-m23-review-notes.md#bounded-real-provider-m3-flight-cadfe521).

## Repaired M1-through-M3 live trial

The follow-up keeps the same normal deployment and orbital notice scenario,
physical models, areas, contact thresholds, provider ceilings and runtime
budget. It changes integration, not the model's answer:

- Deployment input states the scope of saved-trajectory return qualification
  and the actual effects of each approved action. A missing trajectory match
  at deployment is not itself a release failure or a prediction of no return.
- A recovery publication can be refreshed once per published value. Health,
  notice and deadline monitoring continue; a new publication reopens refresh.
- New orbital grants bind independent Jev/DeepSeek reservations into requests.
  The host checks eligibility and its own provider limit. Missing transport
  cannot silently change the provider assigned to a later request.
- Screening and independent admission share the existing preparation time
  budget, including worker startup and cleanup. Every forecast and pending
  operation remains accounted for; no booking deadline or constraint widens.

Before execution, freeze source and authority hashes in a new output directory.
Run one production-Gateway flight only, at most 27 Jev calls, one DeepSeek call,
20 forecasts, two prediction workers and 2400 wall seconds. Keep failures and
do not repeat until an answer changes. The planner fixture selects the scenario
only; flight decisions use real providers. DeepSeek still requires an admissible
uncertain comparison with enough time. No invocation is forced for appearance.

This trial tests the main M1/M2/M3 path: comparison and decision, actual payload
inventory handoff, ascent and concurrent booster supervision, checked execution
and later observations. One successful flight would not finish the full
milestone matrix above, especially in-flight scope amendment and robustness.

The frozen follow-up was executed once and accepted. Eight forecasts used
560.987 wall seconds for the flight, within the above caps. A valid booking
prevented optional time search; that repair has regression/runtime test coverage
but no new live-search evidence from this flight. All 94 executed source hashes
match the frozen protocol and `fe07fa2f`; independently rechecking the saved
study reproduced its verifier verdict without new inference or propagation.
