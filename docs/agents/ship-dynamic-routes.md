# Two dynamic routes: CPU prediction admission

`ship-delivery dynamic-routes-screen` is an explicitly synthetic kinematic
experiment. It does not invoke WAM/VLA, PX4, Gazebo, or a dispatcher. Completion
means passing one guarded crossing and fitting an abstract delivery/return
duration within a deadline. It is not observed delivery or ship recovery.

```sh
missionos ship-delivery dynamic-routes-screen --freeze protocol.json --panel windows
missionos ship-delivery dynamic-routes-screen --protocol protocol.json --output screen.json
```

`--panel` is allowed only while freezing. Scoring uses the hash-checked saved
protocol. Freezing refuses overwrites; scoring cannot overwrite its protocol.
The local timestamp is provenance, not trusted external preregistration.

## Two retained design stages

`sparse` is protocol v1: 576 development and 576 evaluation conditions. Its
deadline grid left the fixed direct/wait procedure at the success upper bound.
This negative panel is retained. `windows` is a subsequent, separately frozen
protocol v2 with 2,160 conditions per cohort, new motion parameters, and denser
deadlines around route-choice windows. These are sequential design experiments,
not one untouched confirmation study. Neither panel trains a model.

Each window cohort has 36 pairs of obstacle profiles, 15 deadlines, two initial
history-noise amplitudes, and two future variants. Position/velocity/acceleration
profiles differ between development, evaluation, and the earlier panel.
Grid cells are not independent flight trials. Noise is a fixed sinusoid of
amplitude 0 or 0.25 m, not measured sensor noise or a stochastic robustness test.

## Procedure, world, and authority boundaries

- Direct approach takes 6 seconds, crossing 1 second, and the remaining
  delivery/return 17 seconds. Detour uses 12 + 1 + 23 seconds. These are fixture
  assumptions, not calibrated flight, battery, or moving-deck measurements.
- Obstacles follow constant acceleration with velocity clipped continuously
  to ±4 m/s. The unannounced-turn variant changes acceleration at 4 seconds
  with continuous position/velocity. Both variants share the exact same past.
- Six procedures choose direct/detour with either waiting or a single switch
  after 4/8 seconds blocked at the gate. A switch pays for the entire retreat
  to the branch and the other approach. Deadline infeasibility stops safely.
- Entry, staging, retreat, and the post-crossing path are always safe in this
  model. Both gates can be blocked; neither detour nor direct is always open.
- A shared guard requires two fresh samples 0.5 seconds apart with
  `abs(x) >= 10 + 1 + 4*1 = 15 m`. With the declared speed bound, the one-second
  crossing retains at least 1 m beyond the obstacle's 10 m exclusion width.
  Current range samples are exact synthetic values for **all** procedures;
  the declared noise affects the initial planning history only.
- Guard input is current/past range, never future trajectory or scenario ID.
  Its bound is analytic within this model, not continuous real-world safety.

## Comparators and information contracts

Only sixteen position frames at 4 Hz ending at time zero and the deadline enter
selection. Unknown fields, future frames, clock gaps, NaN, and oracle policy
arguments are rejected. Route geometry and timing are fixed protocol constants.

Comparators include a deadline-based detour preference, current clearance,
current clearance plus a four-second reactive switch, constant velocity from
the last four frames, and constant acceleration from all sixteen frames.
The latter two use least-squares kinematics and the same bounded procedures;
they are not learned world models. Equal predicted completion times break by
candidate name. This may favor a shorter switch timeout when forecasts are
wrong; CV-versus-CA differences must not be read as predictor quality alone.

A 0.5-second decision cost is assigned equally to all simple methods. This is
a declared clock allowance, not measured CPU inference latency. Future oracles
are evaluated at 0/0.5/1/5/10/21.5/41.5 seconds of departure-blocking latency.
The last two reflect historical ANWM processing rounded up to the sample clock,
excluding load/network. They are sensitivities, not fresh model executions.

The best fixed procedure and best adaptive simple method are selected only on
development, by success count then successful completion time. The stronger
of those is `best_comparator_from_development`, used for the admission gate.
`best_simple_from_development` refers only to the named adaptive policies;
it must not replace the stronger fixed comparator in a result claim.

The full oracle chooses a procedure using each true future. The encoded-input
oracle must choose the same procedure for both futures sharing the same input;
it uses the evaluation distribution retrospectively and is not an online model.
The noiseless continued-motion stratum gives CA the correct model class by
construction. Success there proves conditional predictive selection value,
not WAM advantage. Report every stratum and preserved/lost successes.

## Verification and next boundary

```sh
PYTHONPATH=packages/missionos-core/src:packages/missionos-cli/src:. \
  python -m pytest -q tests/contract/test_ship_dynamic_routes.py \
  tests/contract/test_ship_wam_headroom.py tests/contract/test_ship_urban_decision.py
```

The CLI runtime smoke exercises freeze validation, actual comparator selection,
guarded procedures, and output serialization. Tests cover the velocity cap,
causal samples, complete switching cost, monotone trace times, future-input
rejection, latent-future twins, and fresh window-panel parameters. Private
evidence additionally includes an independent clipped-velocity integral and
tick-set crossing audit, source hashes, unchanged outcome checks after a
summary-only correction, and a synthetic witness trace.

No model is qualified or adopted. Before live comparison, add uncertainty-aware
planning and verify its ability to preserve successes under noisy/unannounced
motion, then test matching sensing and route timing in a simulator.
See [the bounded result](../examples/ship-dynamic-routes-report.md).
