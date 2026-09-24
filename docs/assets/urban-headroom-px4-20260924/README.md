# CPU depth-baseline headroom gate — 2026-09-24

**Result: incomplete; no GPU comparison authorized by the gate.** Three new
passage flights completed arrival, landing and disarm. The fourth case stopped
before the flight controller started: its contact-probe process exited 134 with
`terminate called without an active exception`, after writing positive contact
and probe-removal observations. The remaining eight cases were not attempted.
All created trial containers were removed. This is an infrastructure/measurement
stop, not evidence of ANWM failure or absence of navigation headroom.

This artifact records a fixed twelve-case development screen under **static map
omissions**. It is separate from the earlier ANWM flight experiment. No learned
world model, Jev API or rented GPU is invoked in this screen.

| Completed case | Route | Observed distance | Minimum sphere clearance | Input age at dispatch |
| --- | --- | ---: | ---: | ---: |
| gap 0 | forward | 14.379 m | 0.798 m | 1.448 s |
| gap 1 | forward | 14.207 m | 0.822 m | 1.373 s |
| gap 2 | forward | 14.227 m | 0.836 m | 1.315 s |

The twelve-case denominator remains fixed. With only three verified arrivals,
the loose upper bound on additional oracle arrivals is nine, so the numerical
futility condition is **not** established. An experiment-incomplete report can
pass artifact-consistency CI; those are different outcomes.

The executed policy uses sixteen actual synchronized depth/pose frames and an
incomplete prior map. It freezes a route before independent truth-based safety
constraints. The map-only and latest-frame choices are retained as **shadow
choices**, not additional flights. Unknown space is not certified free.

The prospective decision rule is deliberately simple: the oracle can achieve at
most twelve arrivals. Therefore ten or more verified arrivals from this fixed
simple baseline leave fewer than the three improvements required to start the
GPU comparison. This is a conservative upper bound, **not** a completed same-start
candidate-outcome matrix or an ANWM failure-rate measurement.

- `summary.json`: reviewed, identity-free measurements, observed trajectories,
  baseline rankings, independent filter results, contact controls and gate arithmetic.
- `observed-paths.png`: actual trajectories over post-flight truth-map footprints;
  hatching identifies buildings omitted from the selector's map.
- `verify_report.py`: trajectory arithmetic, terminal/scope and stopping-rule checks.
- `test_verify_report.py`: mutations of denominators, safety, authority and outcomes.
- `manifest.json`: SHA256 of the reviewed report files.

```sh
python docs/assets/urban-headroom-px4-20260924/verify_report.py
python -m pytest docs/assets/urban-headroom-px4-20260924/test_verify_report.py -q
```

For retained local raw sources, run `scripts/verify_urban_headroom.py --root "$RUN"
--output "$REPORT"`. It recomputes the occupied surfaces from bound depth,
calibration and poses, verifies the frozen choices and HMAC dispatch, and checks
actual goal/landing/disarm, trajectory, sensor controls and container cleanup.
Public checks cannot independently attest to raw preimages omitted from this repo.

The [protocol and runtime command](../../agents/urban-wam-headroom-20260924.md)
were fixed before the new flights. All cases are deliberate development variants;
there is no randomized success-rate claim, exact cloning of simulator/controller
state, dynamic-obstacle claim or physical hardware execution. The recorded start
errors and freshness remain part of the result. A temporary passive collision
probe validates a building contact sensor before flight and is removed before
observation/selection. Flight clearance is independently bounded against mesh
AABBs using an enclosing 0.6 m airframe sphere; silent topics alone prove nothing.

Private raw images/depth, full transport messages, instruction references, HMAC
keys, session identities and workstation/cloud paths are excluded. The public
summaries contain measurement and source hashes, never dispatch authority.

Building mesh bounds derive from [OSRF gazebo_models Apartment](https://github.com/osrf/gazebo_models/tree/8163eb4b5e7e21985c6591d1c0bfb56468c0093f/apartment),
revision `8163eb4b5e7e21985c6591d1c0bfb56468c0093f`, [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/),
Nathan Koenig (2012), model author Cole Biesemeyer. Instances are translated and
uniformly scaled; visual and collision meshes are the same pinned asset.
