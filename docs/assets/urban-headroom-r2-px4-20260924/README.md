# CPU headroom repeat R2: reviewed measurements

This artifact belongs only to the newly registered R2 cohort. The earlier
[stopped cohort](../urban-headroom-px4-20260924/README.md) is preserved separately;
its arrivals are not pooled with these results. See the
[experiment report](../../agents/urban-wam-headroom-r2-20260924.md) and
[prospective registration](https://github.com/pome223/missionos/pull/113#issuecomment-5815010766).

**Result:** 12/12 registered CPU depth-baseline flights reached the goal, landed
and disarmed; no building contacts were reported and all trajectory-envelope
checks passed. Zero failures, retries or unattempted cases. The additional-arrival
upper bound is 0, below the required 3: `stop_no_headroom_in_fixed_cohort`.
No WAM inference, post-training or rented GPU was used. This is a fixed static
development cohort, not evidence of general superiority or WAM failure.

- `frozen-protocol.json` is the original byte-for-byte pre-run freeze, matching the hash posted before execution. It contains public scene/protocol/source declarations only.
- `registration.json` binds the fixed protocol, runtime sources, scene order and image.
- `registration-timing.json` records freeze, public registration and driver-start times.
- `case-bindings.json` binds each new case start and repaired contact receipt to its verified flight-result hash.
- `summary.json` contains reviewed trajectories, choices, outcome checks and the conservative oracle bound. It is exported only after independent verification of the raw records.
- `observed-paths.png` plots measured paths and altitudes; `render_paths.py` reproduces it with Matplotlib.
- `observations.json` binds three illustrative recorded RGB frames to their original frame-index hashes; `observed-rgb.png` lays out these observations without generating scenery.
- `manifest.json` binds published artifact bytes. `verify_report.py` checks registration and reuses the original arithmetic/trajectory/authority verifier; its tests reject altered claims.

Raw RGB-D archives, HMAC keys, retained user instructions, local paths and session
identifiers are excluded. The source hashes bind locally retained observations;
these curated checks do not replay physics or replace access to the raw evidence.

```sh
python docs/assets/urban-headroom-r2-px4-20260924/verify_report.py
python -m pytest docs/assets/urban-headroom-r2-px4-20260924/test_verify_report.py -q
# Optional figure reproduction; requires Matplotlib (recorded render: 3.11.2).
python docs/assets/urban-headroom-r2-px4-20260924/render_paths.py
```

![Recorded CPU-baseline flight trajectories](observed-paths.png)

All scenes are static development cases with fixed candidate templates. Fresh
simulators are used, but exact internal-state cloning is not claimed. Only
`history_depth` is executed; other rankings are shadows. The independent safety
filter uses declared truth geometry after the choice is frozen. This is neither
physical flight nor a general navigation benchmark. Model/GPU calls are zero.

## Observed camera examples and attribution

![Actual camera observations from verified variant-zero flights](observed-rgb.png)

Each image is the recorded RGB frame nearest a stated illustrative route position;
see `observations.json`. It is not a forecast, an extra trial, or a substitute for
the full trajectory measurements. The images are laid out by `render_observations.py`.

Apartment mesh and textures: [OSRF gazebo_models](https://github.com/osrf/gazebo_models/tree/8163eb4b5e7e21985c6591d1c0bfb56468c0093f/apartment),
commit `8163eb4b5e7e21985c6591d1c0bfb56468c0093f`; [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/),
copyright 2012 Nathan Koenig, model author Cole Biesemeyer. Meshes/textures are
unchanged; trial instances are translated and uniformly scaled. Camera images
and their figure derivatives are attributed here.
