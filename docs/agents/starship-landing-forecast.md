# Finite six-DOF landing-start forecast — protocol, 2026-10-05

This is an opt-in offline forecast experiment, not an approved production return
policy. No vehicle/aero/actuator/guidance coefficient or handoff bound is tuned.
The initial goal is a forecast that retains actual position, velocity, quaternion,
body rates, all engine/fin states, and the controller's conditioned roll reference
and entry memory. It follows the existing finite 13/5/3 landing controller,
including horizontal correction and mass/centroid/inertia change.

## Frozen experiment before execution

- Inputs: five conventional scheduled-cutoff records already retained under
  `starship-boostback-comparison-20261005`. Cutoff times remain unchanged.
- Origin per case: the last saved entry-coast checkpoint at or before four
  seconds ahead of its recorded legacy landing request. This is retrospective
  development sampling using that old request time, not an online trigger and
  not a held-out or hardware evaluation.
- Reproduce each full recovery once, capturing full physical/controller context
  at that origin. Require all saved physical states/commands and terminal state
  to match the original conventional record exactly before comparing forecasts.
- Four forecasts per origin, fixed order: legacy trigger, burn now, wait 2 s,
  wait 4 s. Waiting integrates the unchanged coast controller and finite dynamics.
  Burn start is the only candidate decision; entry and landing control laws and
  physics are fixed. The failed fin allocator remains excluded.
- Budget: five baseline recoveries and twenty forecasts; each forecast at most
  90 simulated seconds and never beyond the inherited recovery deadline. At
  most 10 minutes of wall time for the CLI's work (checked between bounded calls;
  this is not a hard interrupt inside an integrator). No provider/GPU/hardware,
  retries to replace an unfavourable result, gain sweep, coefficient fitting or
  further search in this protocol. Recording/implementation defects are retained
  and any repaired verification invocation is reported separately.
- Primary field: independently reconstructed **predicted handoff eligibility**
  at the forecast's actual terminal state, including position, both support
  points' velocities/heights, both attitude bounds, body rate and support fuel.
  Predicted arrival is not contact/support or mission completion.
- Legacy forecast must match the reproduced baseline's entire saved suffix and
  terminal state. This checks deterministic continuation/context completeness,
  not independent dynamics accuracy or added operational value.
- Selection: preserve legacy if eligible; otherwise the first eligible fixed
  candidate. If none is eligible, explicitly return `no_capture_candidate`.
  Do not choose a failed candidate and relabel it admitted. No selection is
  dispatched into a real or MissionOS flight by this experiment.
- Retain all outcomes and hashes. Do not pool the twenty forecasts as twenty
  executed missions or independent model-validity trials. Forecast computation
  does not advance the caller's physical clock; real-time scheduling is untested.

## Public basis and limits

[NASA's time-varying-mass six-DOF powered-descent paper](https://ntrs.nasa.gov/api/citations/20230017074/downloads/SCPVarMP_Rev2.pdf),
checked 2026-10-05, includes moving mass properties and attitude dynamics and
discusses terminal tilt/angular-velocity constraints. This motivates retaining
them in a continuation. This implementation does not implement that paper's
SCvx optimizer, its convergence guarantees, or SpaceX flight software. The
existing surrogate aero/shared reservoir and unpublished coefficients remain
explicit assumptions. A deterministic self-replay cannot establish fidelity.

The prior diagnostic is [the capture-deficit record](starship-capture-diagnostics.md).
This protocol generates distinct command candidates because every previous
terminal option failed support; it is not an LLM/learned-selector value trial.

## Verification boundaries

Snapshots require an explicitly scheduled development run. They cannot change
physical values or become a catalog option. Forecast-only continuations reject
invalid clocks, non-matching profile/catch hashes, malformed controller memory,
unbounded delays and deadline extension. The ordinary recovery checker rejects
forecast records, including attempts to remove their `forecast_only` flag while
leaving the forecast marker. A dedicated independent stored-record checker
reconstructs physical gate arithmetic and finite-state continuity without
importing the producer/integrator. It does not replay dynamics or certify the
controller cache independently of its captured/source-bound provenance.

Production HTTP regression after changes must use a freshly restarted Gateway
and the unchanged `sixdof_launch_catch` scenario. The new predictor must remain
absent from that production flight. Runtime results and commands are appended
only after actual execution.
