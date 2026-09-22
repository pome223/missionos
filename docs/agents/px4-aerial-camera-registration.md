# Frozen PX4 aerial camera registration

This optional CPU adapter converts recorded `x500_depth` / `OakD-Lite` sensor
data into registered RGB, optical depth, masks, intrinsics, and camera poses.
It does not call a model, approve an action, command a vehicle, or establish
live observation freshness. See [aerial model evaluation](aerial-wam-model-evaluation.md)
for the separate model and judgment boundary.

## Acquisition and conversion

```sh
RUN_PX4_AERIAL_CAMERA_PROBE=1 python scripts/probe_px4_aerial_camera.py \
  --output-dir output/px4-aerial-capture --frames 16 \
  --scene calibration-wall --sensor-profile history-4hz --sim-speed-factor 0.1

PYTHONPATH=. python scripts/register_px4_aerial_capture.py \
  --capture output/px4-aerial-capture/capture.json \
  --output-dir output/px4-aerial-registration
```

Output directories must be unused. The acquisition command creates and deletes
its own network-isolated simulator container, with no arm or flight commands.
The `history-4hz` profile changes the actual simulated RGB sensor to 640 x 360
and both sensor update rates to 4 Hz. Depth remains 640 x 480. It records original
and effective SDF hashes; actual CameraInfo must match the effective SDF. The
default `native` profile retains the installed model's sensor settings.

The speed factor slows simulation relative to wall time so CPU rendering can
keep up. Sensor time remains the measured simulation time. The collector keeps
raw synchronized messages before encoding PNGs and tolerates one 4 ms physics
step around each requested 250 ms target. It records every actual timestamp and
phase error. Requesting 4 Hz does not prove that frames arrived at 4 Hz; inspect
the receipt. Frames are never repeated, interpolated, or given replacement times.

## Geometry and binding

`missionos_registered_aerial_capture.v1` binds the source capture, effective
SDFs, raw RGB/depth, pose wire bytes, per-frame calibration, and output NPZs by
SHA-256. The fixed camera mount is independently composed from the airframe SDF,
the observed parent-local camera-link pose, and each sensor's SDF pose. The
camera inertial offset is not a sensor offset. Mismatches fail before output.

The output frame is optical right/down/forward in an explicitly local NED world:

```text
local_NED_from_Gazebo_ENU = [[0, 1, 0], [1, 0, 0], [0, 0, -1]]
sensor_FLU_from_optical   = [[0, 0, 1], [-1, 0, 0], [0, -1, 0]]
```

This coordinate convention does not establish GPS coordinates or geographic
north. Intrinsics remain separate for the native RGB and depth cameras. Measured
depth is optical Z in metres, not radial distance. Unprojection, the measured
relative camera transform, projection, and a nearest-pixel Z-buffer produce the
registered depth. The source near clip is axial; the far clip is radial.

Unknown depth remains NaN with an explicit boolean mask. Original depth bytes
and invalid values remain available. No hole filling or sky-to-far-plane
substitution occurs. Optional `--width` and `--height` use nearest RGB sampling
and a recorded half-pixel transform of intrinsics. Downsampling can assign a
measurement to every output pixel; it does not prove a swept volume is visible
or collision-free.

CameraInfo and pose fields are decoded by the acquisition process. Their wire
hashes are retained, but the host conversion does not independently decode all
original transport messages. The receipt keeps
`transport_field_decode_independently_verified: false`.

Primary implementation references are Gazebo's
[SceneBroadcaster](https://github.com/gazebosim/gz-sim/blob/gz-sim8/src/systems/scene_broadcaster/SceneBroadcaster.cc),
[Ogre2DepthCamera](https://github.com/gazebosim/gz-rendering/blob/gz-rendering8/ogre2/src/Ogre2DepthCamera.cc),
and [depth shader](https://github.com/gazebosim/gz-rendering/blob/gz-rendering8/ogre2/src/media/materials/programs/GLSL/depth_camera_fs.glsl).

## Model and comparison admission

The source is `px4_gazebo_frozen_capture`, never `public_dataset_replay`.
The ANWM runner rejects public-dataset metadata attached to an explicit PX4
source. Its separate [airborne input adapter](aerial-wam-px4-flight.md) accepts
only the typed PX4 source contract. Registered geometry alone does not satisfy
the model contract: a declared goal, candidate trajectories, an explicit
missing-depth policy, history selection, and validated model timing remain
separate requirements. `model_input_ready` and `model_time_alignment_verified`
remain false. Saved sensor times do not establish current dispatch freshness.

A calibration wall provides a geometric check, not a learned-model benchmark.
Before additional GPU use, screen fixed candidates against an independently
bound scene and a simple depth baseline. Out-of-view or missing-depth regions
are unknown, not clear. A camera-reference corridor is not an airframe swept
volume, and planned intersections are not observed flight outcomes.

Run the CPU screen on the bound calibration-wall capture:

```sh
PYTHONPATH=. python scripts/screen_px4_aerial_candidates.py \
  --capture-dir output/px4-aerial-capture \
  --registration-dir output/px4-aerial-registration \
  --output output/px4-aerial-candidate-screen.json
```

The screen rechecks source hashes, recomputes registered arrays, and matches
declared static boxes to observed scene poses. Fixed forward 5 m and left 5 m
camera-reference segments use margins of 0.35 m horizontally and 0.10 m
vertically. The box oracle covers only the declared boxes. The depth baseline
reports measured blockage, sampled observed free space, or unobserved space;
zero surface hits cannot establish clearance. Its coverage is finite sampling,
not a continuous collision certificate. A visible front wall and an unobserved
side corridor do not justify a comparative GPU test. The output keeps comparison
eligibility false and invokes no simulator, model, Jev, or aircraft action.
