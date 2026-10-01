# CPU admission for ship-to-urban WAM selection

`ship-delivery wam-headroom` evaluates a synthetic analytic outcome matrix. It
never dispatches a command, invokes a model, or starts PX4/Gazebo. Feasibility
means a procedure fits an abstract time budget after subtracting common mission
cost; it does not mean observed delivery, recovery, or battery sufficiency.

Freeze the protocol before scoring:

```sh
missionos ship-delivery wam-headroom --freeze protocol-freeze.json
missionos ship-delivery wam-headroom --protocol protocol-freeze.json --output screen.json
```

The first command creates a receipt exclusively and refuses overwrites. The
second verifies its protocol hash and supported schema/content. A receipt is a
local provenance record, not a trusted timestamp or external preregistration.
The screen records producer source hashes, freeze/score timestamps, every
candidate outcome, every policy choice, and paired summary denominators.

## Scope and inputs

- One monotonically clearing obstacle, an always-clear approved detour, and
  exact synthetic scalar positions. No occlusion, re-entry, wind, deck motion,
  perception error, energy model, or intent cues are simulated.
- Sixteen observed frames at 4 Hz end at decision time zero. Histories and
  remaining slack are the only selector inputs; case IDs, future families,
  future clearance times, and oracle results are rejected as online inputs.
- The existing `choose_onboard_action` implementation supplies the velocity and
  stopping comparators without changing its production behavior. This screen
  provides ideal scalar observations, not RGB images to that implementation.
- `budget_detour_else_wait` is an analytic comparator, not a deployed policy.
- Development/evaluation each have 192 conditions and eight distinct histories.
  Evaluation uses different position, velocity, acceleration, and future-speed
  parameters. Conditions are a deterministic grid, not independent trials or
  a learned model's held-out generalization result. No fitting occurs.

## Outcome and oracle contracts

Seven procedures comprise immediate detour, wait, and wait up to 2/8/20/60/120
seconds followed by detour. Waiting requires two consecutive clear samples at
0.5-second intervals; indefinite obstruction causes a 120-second wait timeout.
All procedures start from the same state and candidate table. The best fixed
procedure is selected on development and applied unchanged to evaluation.

With detour cost `D = 2*85/12 + 8 = 22.1667` seconds and remaining slack `B`:

- If `B >= D`, immediate detour is feasible regardless of the future obstacle.
- If `B < D`, no detour (including a delayed detour) fits. Direct departure after
  confirmed clearance is the only possible feasible candidate.

Consequently, detour-if-it-fits/otherwise-wait attains the feasibility upper
bound within this candidate family. This is not a theorem about arbitrary
navigation scenes. An accurate remaining-time estimate and permanently safe
detour are strong assumptions that require separate runtime validation.

The full oracle minimizes time after maximizing per-case feasibility. The
encoded-input oracle chooses one procedure for all four futures sharing the
same observed input, maximizing aggregate feasible count then minimizing time
on feasible cases. It is an optimistic, distribution-aware retrospective bound,
not an executable learned policy or an information bound for richer RGB input.

Latency is charged as a departure-blocking hold while fresh clearance monitoring
continues. Wall and mission time are assumed 1:1. The delayed oracle receives
perfect future knowledge and can depart immediately if clearance was already
confirmed. This favors the hypothetical predictor. The 41.19-second sensitivity
point comes from a historical native ANWM two-candidate run; it excludes model
load/network time and is not a fresh latency measurement or calibrated forecast.
Concurrent asynchronous forecasting is outside this screen.

Admission requires preserving every budget-rule success and improving success
count or mean time by at least five seconds at the declared native latency.
Timing means always state the jointly feasible denominator; feasibility losses
must be reported alongside them. Admission is permission to investigate a
bounded comparison, never approval, dispatch, model adoption, or Step 2 completion.

## Verification

```sh
PYTHONPATH=packages/missionos-core/src:packages/missionos-cli/src:. \
  python -m pytest -q tests/contract/test_ship_wam_headroom.py \
  tests/contract/test_ship_urban_decision.py
```

The contract tests check the algebraic upper bound outside the frozen grid,
sampling and latency costs, future-input rejection, disjoint observations,
identical-input action consistency, protocol tampering, and the real Click
entrypoint. Run the CLI commands above as an additional runtime smoke.

See [the bounded result](../examples/ship-wam-headroom-report.md).
