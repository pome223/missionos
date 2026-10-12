# Post-M3 recovery sensitivity study

This is an opt-in diagnostic of the numerical and guidance tools used by
MissionOS. It does not rank AI against Rules, invoke a provider, grant flight
authority, modify the completed M3 protocol, or extend a return certificate.
The parent M3 runtime is `61ebb8fe` (production source unchanged from `8a55ee69`).

## Frozen questions and cases

Use one recorded M3 normal-flight origin and its exact profile, goals and model
source. Bind the input bytes, scripts, source and numerical backend before the
first simulation. Preserve raw results and negative outcomes; never overwrite a
case or rerun it merely to obtain a pass.

1. **Booster:** replay the exact recorded separation and fixed splashdown action.
   Require nominal trajectory reproduction before two predetermined continuations
   at separation propellant minus and plus 1,000 kg. Position, velocity, attitude,
   angular rate, engine/flap states, objective and limits stay unchanged. This
   changes initial inventory; it is not a modeled fuel-loss impulse or an ascent
   disturbance. Recheck the eight existing water-entry constraints independently.
   A contact failure is a result, not a reason to omit the other fixed case.
2. **Ship:** use the identical saved orbital-coast origin and return time. Run
   five cases: current cadence; half and quarter outer control/integration
   intervals; and twofold/fourfold subdivision of each RK4 advance while retaining
   the original configured outer control, event and sampling cadence. Threshold
   crossings and state-dependent cadence switches can still change their actual
   timestamps. The latter is a local
   diagnostic wrapper around the existing RK4 method, not an operational change.
   Retain the unchanged equations, actuator limits and contact solver.

The old approximately 77 km half-step displacement changed control timing as
well as integration. It must not be labelled isolated integration error. Compare
phase changes, ignition/cutoff, sampled alignment-gate behavior, propellant,
contact time, ECI position and rotating-Earth surface location. Do not mix a
change to the controller's sampling rate with a refinement of the physical plant.

## Bounds and interpretation

- CPU only; no Gateway flight, model/API call, Docker or cloud allocation.
- One attempt for each fixed case, at most two simultaneous numerical workers.
- At most 600 wall seconds and 200 MB per study case; at least 5 GiB free before
  starting a worker. End on malformed input, changed source, nominal replay
  mismatch, numerical execution error or resource-limit exhaustion. Keep already
  completed results. A valid physical failure is retained, not retried.
- Use qualified NumPy/SciPy versions and record them; never weaken the existing
  runtime registration or approval checks to run the development diagnostic.
- Report pairwise finite-grid differences. A smaller discrepancy on these finite
  grids alone does not prove convergence, real-vehicle accuracy or a global
  uncertainty bound. No new operating margin is inferred from a single case.
- No controller tuning is part of this frozen study. An identified repair needs
  a separate source-bound regression while retaining the original failure.

## Evidence and preservation

Persist the protocol before execution, then source/input hashes, initial-state
comparison, event records, actual final states, independent checks, timing and
failed-case evidence. New output lives under the task-owned
`output/starship-recovery-sensitivity-20261012/` tree; private raw input and output
are not distributed. Only reviewed scalar results and diagnostic traces may enter
Git. Preserve and verify a private archive outside the trial tree; do not delete
original evidence. This work has no authority to merge or publish to the public
repository.

The resulting report must distinguish:

- completed M3 control-path evidence from current physics-tool diagnostics;
- nominal replay from perturbed initial conditions;
- eight-constraint water entry from hull survival, recovery and reuse;
- fixed-controller numerical refinement from changed controller cadence;
- a negative or inconclusive result from fleet-operation readiness.

## Recorded execution, 2026-10-12

The frozen study source is `d9c1cb3a1e8c803000c82c0c696ce0043c691c69`.
All eight predetermined cases ran once, with no retry, model/API invocation,
Gateway mission or controller tuning. Both saved nominal replays matched their
respective inputs exactly. Production source, profiles, qualification files and
operational authority are unchanged by this diagnostic PR.

Input provenance must be kept explicit:

- Booster input: the actual normal-M3 recorded separation at T+135.6 s,
  260,000 kg initial booster propellant. Study SHA-256:
  `bde31bd529ae67474d3418ef3c593ceeab75e7c9a7a74faf51ad9be90e10468b`.
- Ship input: the saved normal-M3 prediction's
  `synthetic_navigation_estimate_v1` at T+1,000.7 s, with return at T+2,280.7 s.
  This is a navigation estimate, **not the actual plant state at return**.
  Prediction SHA-256:
  `3be1634cb06a6de066f3b1ad87ce752d0c6e4919108663ae6ffad0bf64830f55`.
  Its baseline contact is 4.612507 m/s; the actual M3 normal flight was
  4.608259 m/s. Exact saved-prediction reproduction is not exact reproduction
  of the executed M3 suffix.
- Profile SHA-256:
  `4a0bebe69c97978daeabe7f16a4b30a1b69864a75668217106b1ca0ef4439df5`.
  Backend: NumPy 2.4.6 / SciPy 1.17.1.

### Booster

| Separation propellant change | Contact speed | Fuel remaining | Eight-constraint water-entry envelope |
| --- | ---: | ---: | --- |
| 0 kg | 1.883584 m/s | 11,419.538 kg | Passed |
| −1,000 kg | 165.000082 m/s | 81,557.460 kg | Failed: area, speed, vertical/horizontal speed, tilt |
| +1,000 kg | 1.897990 m/s | 9,206.951 kg | Failed: 10,000 kg reserve |

All three are valid records, including the physical failures. For −1,000 kg,
none of the saved terminal samples passes the thrust-axis alignment gate;
sampled main-engine thrust remains effectively zero. These are approximately
2-second samples, not a complete substep ignition history. Full quaternion
attitude error must not be substituted for the thrust-axis gate angle.
Both successful low-speed trajectories also exhibit large earlier attitude
errors. The existing booster guidance is not robust to this ±1 t disturbance;
the study does not identify a unique root cause or qualify a repaired controller.

### Ship

| Case | Configured outer control intervals | RK4 subdivisions | Surface displacement from baseline |
| --- | --- | ---: | ---: |
| baseline | Original | 1 | 0 m |
| coupled_half | Half | 1 | 77,280.282 m |
| coupled_quarter | Quarter | 1 | 170,440.688 m |
| rk4_half | Original | 2 | 11.759 m |
| rk4_quarter | Original | 4 | 6.522 m |

The coupled half-to-quarter difference is 93.160 km; this sequence does not
establish cadence convergence. The fixed-cadence RK4 half-to-quarter difference
is 5.237 m, a finite-grid comparison for one origin, not a global error bound.
All five end in model low-speed surface contact (4.589–4.613 m/s).

With coupled refinement, deorbit commands held off by the alignment gate total
19.5 / 10.0 / 3.6875 s, and burn cutoff moves from T+2360.600 to 2350.925 to
2344.500 s. Fixed-cadence RK4 refinements retain that 19.5 s gate-off duration
and T+2360.600 cutoff. At the return request, the original/half trajectories
still coincide to approximately 1.4e-7 m. Frame rotation alone accounts for
about 4.87 km of the half-case timing shift, not 77.28 km.

These observations support control/event-cadence and alignment-gating sensitivity
as the main source of the old mixed-step discrepancy. They do not demonstrate
real-vehicle accuracy or authorize narrowing the operational safety margin.
For the unchanged synthetic primary area (125 km radius), the coupled quarter
case is 193.5 km from its center and outside it; this changes the controller
cadence, so it is not a new claim about fixed-cadence runtime admission.

### Independent verification and preservation

The saved-record audit recomputes kinematics, contact geometry, booster water
constraints, source/input bindings and nominal equality for all eight cases.
It also rejects a deliberately altered contact-speed record. It performs no
simulation and is not independent validation of the shared physical equations.
Audit SHA-256:
`267ad5bde520e6ce3770f23f1e53aa89e2d0fbfe13b6dcd65abb7a2485204b5a`.
The campaign protocol SHA-256 is
`10644fb5faca934b3023e6f4e1cda130d08d79842e0a9f92b9367cca34e4b1f4`.

Raw inputs/results stay private under the task-owned output directory.
[Reviewed report and scalar evidence](../assets/starship-recovery-sensitivity-20261012/report.html)
are portable. Any repair must preserve these failures and use a new source-bound
regression. The next engineering work is booster attitude/ignition robustness
and an explicit controller clock for numerical refinement, before fleet claims.

Preservation completed: 690 files / 183,802,536 bytes were archived into an
18,862,790-byte private archive. Every member's size, type, mode and SHA-256 was
compared; protocol, failed-booster trajectory and quarter-cadence Ship trajectory
were separately restored and checked. The original files remain unchanged.
Archive SHA-256:
`6b4fa4af7410fce758321affaad7f78eea1b77dc6192405a9d05b2ec6c89d157`.
No files, worktrees or Docker resources were deleted.
