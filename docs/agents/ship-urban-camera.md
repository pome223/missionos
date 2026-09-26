# Ship urban RGB perception screen

This is a perception and comparator checkpoint. It does not change the existing
PX4 executor or claim completion of Step 2, onboard vision, VLA or WAM integration.

## Runtime

```sh
missionos ship-delivery urban-camera-screen --approve-gazebo \
  --output-dir /tmp/urban-camera-fresh
missionos ship-delivery urban-camera-verify --run-dir /tmp/urban-camera-fresh
```

The first command requires explicit opt-in and an unused output directory. It
uses the existing local `px4io/px4-sitl-gazebo:latest` image by resolved image ID;
no image pull, host port, hardware or external model is used. Docker runs with
network disabled, 2 CPUs, a 2 GiB memory limit and a 180-second host timeout
(configurable 30–600 seconds). Cleanup targets only that invocation's container.
The second command is read-only and starts no process other than the CLI.

The worker starts Gazebo Sim 8 with OGRE2 headless rendering and software GL.
It subscribes through `gz.transport13` to actual `gz.msgs.Image` RGB messages,
preserves sensor timestamps, and writes lossless PNGs. See the upstream
[headless rendering](https://gazebosim.org/api/sim/8/headless_rendering.html) and
[Transport Python](https://gazebosim.org/api/transport/13/python.html) contracts.

No aircraft is spawned. A fixed camera at the compact mission's entry
`(0, 70, 30)` faces north. The collision-enabled obstruction remains at
north=170 m, with dimensions 16×8×60 m. Six corridor buildings retain the
compact urban geometry. Camera resolution is 640×360, horizontal FOV π/3.

The actor is staged at specified x coordinates while the simulation is paused,
then the simulation clock advances to obtain each new camera observation. This
is a stepped perception sequence, not continuous motion or an onboard video.
Sensor timestamps must agree with the staged relative times within 0.15 s.

## Input split

Three development cases are fixed before capture:

- `short_clear`: 2 m/s.
- `long_block`: 0.15 m/s.
- `brake_stop`: x=4t−0.5t² until t=4 s, then stopped at x=8 m until t=40 s,
  followed by movement at 2 m/s. All scripts cap x at 16 m.

Each produces seven frames at 0, 0.5, 1, 1.5, 2, 4 and 8 seconds. Only the first
five enter a policy. The last two are post-decision validation frames. None of
the resume times, case names, scripted positions or future images enter
`choose_camera_action`. The selector accepts only timestamped image centers.
Unknown fields, non-finite values, unsupported policies, missing frames and
clock discontinuities are rejected.

A color threshold extracts a single red rectangular silhouette. It rejects
missing, clipped and substantially occluded/ambiguous observations. This is
synthetic color segmentation, not general visual detection. The policy's
clearance pixel coordinate assumes a known fixed camera and near-face depth of
96 m; it is not an inferred depth, moving-camera calibration or transferable
real-world clearance estimate.

All four comparators share the same five frames: always wait, always detour,
constant velocity and stopping-aware. The latter estimates deceleration from
two observed secants and predicts a stopping point with a two-pixel curvature
deadband. Outputs are `action_proposal`; `dispatch_authorized` stays false.
Policies run offline after capture, restricted to the prefix ending at 2 s;
this is not an online decision or a measured perception-to-control latency.

## Evaluation and claim boundary

Offline evaluation uses the scripts to estimate remaining clearance time at
t=2 s and compares it with a detour allowance of 170/12+8 seconds. These are
**analytic extra costs**, not measured flight durations. Common direct travel
is omitted from both alternatives. Report each fixed comparator and the
stronger simple baseline; do not claim model headroom just because the
constant-velocity comparator fails on braking.

Calibration is independently checked against the staged geometry with a
three-pixel tolerance (including perspective silhouette differences). This
truth check belongs to evaluation, never policy selection. Original RGB bytes,
frame roles, timestamps, code, world/config hashes and canonical invocation
evidence are retained outside Git. Replay reopens images and recomputes tracks
and proposals; modified, incomplete, stale, failed or mismatched records block
verification. The recorded analysis revision is required for exact replay.
Hashes detect inconsistencies; they do not authenticate a hostile producer.

`learned_model_comparison_admitted` stays false: this screen has no measured
flight outcome, no held-out result and no learned model. The existing four-run
PX4 report remains a separate experiment with privileged pose inputs.
