# Upstream six-DOF command candidates — frozen protocol, 2026-10-05

The [landing-start forecast](starship-landing-forecast.md) found no handoff in
twenty fixed continuations. This is candidate generation before any selector or
LLM-value comparison. It is not an optimizer or an adopted recovery controller.

## Before execution

- One original launch-derived separation at T+135.6 s, from the retained
  conventional boostback comparison. Bind input/comparison/raw byte hashes,
  initial-state hash, profile, catch configuration and the old cutoff.
- Six complete recovery calls: one unchanged baseline at the original cutoff
  T+191.2 s, then candidate indices 0 through 4, in that order. No further calls
  to replace failures. Each has the original maximum 1200 simulated seconds.
  Wall guard 600 s checked between calls, not preemptive inside the integrator.
- At initial planning and its existing alignment refresh, choose the specified
  index from the existing five point-model velocity proposals (vertical targets
  200, 400, 600, 800, 1000 m/s). Horizontal targets are recomputed from each
  candidate's own current state by the unchanged point planner. These are five
  deterministic command policies, not five constant velocity vectors or new
  aerodynamic parameters. Baseline retains its original selection rule.
- Keep scheduled cutoff, all physics/actuator/control coefficients, entry and
  landing laws and material-point arrival/support gates fixed. Fuel/time guards
  remain operative; if one stops earlier, record that fact. No teleportation,
  fixture replacement, fuel addition, oracle future state or gain fitting.
- Baseline must reproduce all old saved physical states, commands and terminal
  state exactly. Candidate records keep every macrostep (at most 0.25 s) to
  expose fuel-cutoff reversals to the unchanged continuity heuristic.
- Primary endpoint: actual simulated simultaneous eight-bound handoff. On a
  genuine handoff only, pass the exact terminal state to a maximum 30 s contact
  simulation and independently check support. No handoff means no catch call.
  Catch-call budget is at most six. Ship/payload/launch are not reintegrated.
- Record exact first landing-request state: horizontal pin error, actual tilt,
  rates, velocity and fuel, plus each signed margin at each saved landing state.
  Individual best margins cannot be combined across times into a success.
- Persist lossless compressed raw records before checking them. A failed check
  makes the CLI unsuccessful, preserves the failed raw record and failure.json,
  and prevents running later candidates. Retain source snapshots/hash and
  completed-call count. No provider, GPU, hardware or MissionOS dispatch.
- Adoption remains false even if a candidate succeeds. This is one development
  start with known history, not held-out robustness, launch-to-catch execution,
  independent dynamics validation or SpaceX control fidelity.

## Public grounding and implementation boundary

[NASA's six-DOF time-varying mass-properties work](https://ntrs.nasa.gov/citations/20230018515)
and [its precision lunar landing implementation](https://ntrs.nasa.gov/citations/20210024113),
checked 2026-10-05, treat translational/attitude dynamics and terminal constraints
together. They motivate checking executable candidate states, not using ideal
thrust direction or an isolated stopping-fuel estimate as arrival certification.
These lunar studies do not supply SpaceX's coefficients or guidance. No SCvx,
optimality guarantee, convergence proof or realtime capability is implemented.

`_development_boostback_candidate_index` accepts only integer 0..4, only with an
explicit scheduled development cutoff; it rejects forecasts, alternative fin
allocation and landing-context probes. The ordinary verification call rejects
the marker. An explicitly scoped check binds the marker, selected candidate at
both planning events, finite actuation, each stored step and reconstructed gates.
Its producer and verifier remain separate. It checks recorded consistency, not
an independent dynamics replay or proof that every guidance command is optimal.
Removing only the development marker still leaves an invalid selection rule.
Source-bound enclosing verification is required against arbitrary rewritten data.

## Remaining completion stages

1. Generate a reachable landing-entry state and close the position, velocity,
   attitude/rate and fuel constraints through arrival and support.
2. Reexecute launch-to-catch and test declared wind/failure/latency conditions.
3. Connect MissionOS approval, fresh observation, escalation and deterministic
   diversion to the flight, then confirm each action in later observations and
   compare final outcomes with the simple-rule baseline.
4. Integrate replay/3DCG/operator UI and run the complete reproducible mission.

These are milestones, not a promise of four attempts. At present stage 1 is
unfinished. Production verification and MissionOS command receipts alone do
not finish it. Model value and real-hardware fidelity require separate evidence.
