# Initial uncertainty filter on the camera/PX4 boundary

`run-sitl --urban-policy onboard_uncertainty` opts into an initial-only filter
around the existing `onboard_stopping` rule. It runs at the observed urban-entry
hold. No route changes are made during flight. The default policy is unchanged.

```sh
missionos ship-delivery run-sitl \
  --scenario examples/fixture_missions/ship_delivery/urban-compact.json \
  --urban-case brake_stop --urban-policy onboard_uncertainty \
  --approve-sitl --output-dir /tmp/new-onboard-uncertainty-run
```

This is a separately defined runtime adaptation of the
[CPU uncertainty experiment](ship-uncertainty-gate.md). Its world has one visible
moving obstacle and a mapped static detour. It does not establish transfer of
the CPU panel's eight fresh gains, which depended on two dynamic route gates,
different histories and abstract crossing/return durations.

## Observations and proposals

The existing five-frame onboard RGB/PX4 pipeline supplies timestamped scalar
positions using a known red box, mapped depth and a surveyed visual marker.
Frames are irregular in wall time. The filter fits their actual timestamps;
it does not interpolate them into sixteen fictional 4 Hz observations. Camera
freshness, hashes, attitude, repeated timestamps and projection verification
remain enforced by the existing observation and independent verification code.
Gazebo obstacle poses, case IDs, actor scripts and future states never enter the
filter. Only observed time/position are accepted by its scalar input contract.

`ship_onboard_uncertainty.CONTRACT` defines the fixed filter:

- Start with the stopping-aware rule on exactly the same observations.
- Retain that action when a full-history constant-acceleration fit has residual
  RMS above 0.1 m. RMS is a heuristic, not a calibrated confidence probability.
- Retain an existing wait if nominal CA predicts two clear samples inside the
  ordinary rule's detour-time estimate.
- Otherwise check nominal CA, ten delayed brake-to-rest alternatives, and
  full-history constant velocity when its own RMS passes the same limit.
- Change wait to detour only when every admitted forecast misses the wait
  window. Change detour to wait only when all pass with one second of reserve.
  Disagreement retains the ordinary rule.

The window is the existing mapped estimate `170 / airspeed + 8` seconds. It is
not measured flight duration or a hard remaining-mission budget. Prediction uses
a declared 4 m/s velocity cap, 0.5-second samples, a 14 m clearance threshold and
braking after 0/2/4/6/8 seconds at 0.3/0.6 m/s². Brake hypotheses stop and hold;
they do not cover arbitrary reversals. These assumptions cannot establish
general collision avoidance or baseline preservation.

Every output records the exact history hash, latest observation clock, fits,
baseline action, selected action, gate reason and hypotheses. The gate's
`dispatch_allowed` is always false. Its proposal does not approve or upload a
mission. The operator-approved routes and existing executor still require two
fresh clear images before direct departure. Mission upload, observed PX4 motion,
parcel separation/contact/stability, and stable deck recovery remain separate
facts. No model endpoint, cloud resource or hardware endpoint is used.

## Reproducible verification

```sh
PYTHONPATH=packages/missionos-core/src:packages/missionos-cli/src:. \
  python -m pytest -q tests/contract/test_ship_*.py
PYTHONPATH=packages/missionos-core/src:packages/missionos-cli/src:. \
  python -m scripts.report_onboard_uncertainty \
  --batch-root /path/to/frozen-private-batch --output-dir /path/to/new-report
```

The report script only reopens existing records. It cannot start a flight or
model. A private batch freezes source hashes and all six case/policy pairs
before any run: `short_clear`, `long_block`, `brake_stop`, each with stopping and
uncertainty policies. One extra infrastructure attempt is allowed over the
batch only if the failed attempt never reached the urban decision. Every
attempt remains in the denominator. It is not acceptable to retry an urban
decision failure into a favorable result or mix runtime implementations.

Reverification reopens raw images, PX4 state, world/plan/source hashes, invocation
evidence and all existing outcome verifiers. The report retains failed flights
and marks a complete matched matrix only when both policies finish in all three
cases. Same-image rule replay is retrospective; its alternative action was not
flown. Timings from separate one-off flights include simulator load and are not
statistical evidence of a performance improvement.

The replay uses recorded positions and sampled saved images, aligned at entry
hold. It displays observations at or before the selected time; it generates no
future motion or imagery. Raw evidence and selected RGB stay in a private
archive. Public documentation should contain reviewed aggregate facts only.

The completed compact comparison uses the pre-calibration-fix implementation;
retain its recorded source when replaying it. A subsequent full-distance run
required the [expanded marker-heading envelope](ship-onboard-step2.md). Do not
pool those implementations or present the failed first full-distance attempt
as a completed flight. The route filter and its thresholds were not retuned.
