# Perception Corroboration Binding

This contract binds one live VLM observation to one exact camera frame and one
independently observed LaserScan candidate. It is evidence for recovery
deliberation. It does not approve, dispatch, execute, verify mission completion,
or create physical authority.

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

## Headless TurtleBot3 sensor profile

Headless execution does not prevent ROS image topics. The stock TurtleBot3
`burger` model has `/scan` but no RGB camera. When camera perception is enabled,
the Docker launchers select the stock `waffle_pi` model by default; it publishes
both `/camera/image_raw` and `/scan` under Xvfb. An operator may override the
model explicitly:

```bash
export MISSIONOS_TURTLEBOT3_CAMERA_PERCEPTION_ENABLED=1
export MISSIONOS_TURTLEBOT3_SIM_MODEL=waffle_pi
export MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED=1
export MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_MODEL_ID=gemini-3.1-flash-lite
```

The headless Gazebo smoke also sets `ROS2_NAV2_USE_SIM_TIME=1` internally. It
keeps the bridge TF buffer in the `/clock` domain, so the required
`map <- lidar_frame` lookup uses the LaserScan timestamp without treating a
simulator timestamp as expired host-wall-clock history. Hardware remains
wall-clock by default.

The usual `burger` default remains unchanged when camera perception is off.

The focused headless capture smoke starts `waffle_pi`, adds one collision-backed
red box 0.85 m in front of the robot, and writes a PNG plus bridge receipt:

```bash
scripts/smoke_turtlebot3_headless_perception_capture_docker.sh
```

With Vertex ADC configured, run the live source-bound VLM check immediately
after capture:

```bash
export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_PROJECT="$(gcloud config get-value project)"
export GOOGLE_CLOUD_LOCATION=global
export MISSIONOS_LLM_BACKEND=gemini
export MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED=1
export MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_MODEL_ID=gemini-3.1-flash-lite

.venv/bin/python scripts/smoke_turtlebot3_live_perception_binding.py \
  --capture-receipt output/turtlebot3_perception_smoke/capture.json \
  --image output/turtlebot3_perception_smoke/frame.png
```

The second command exits with status 2 unless the live-VLM and corroboration
binding passes. It does not dispatch a Nav2 goal.

## Evidence chain

The production path records these separate facts:

```text
camera capture receipt
  camera_frame_sha256
  camera_observed_at and camera_received_at
  camera intrinsics and frame id

live VLM invocation
  runtime_invocation_evidence.v1
  invocation_kind=llm_api
  input_image_sha256
  provider, model_id, invocation_ref, start and completion timestamps

independent LaserScan observation
  lidar_observed_at and frame id
  closest candidate bearing and range inside the camera field of view
  target_candidate_id and lidar_evidence_ref

core-owned verdict
  missionos_perception_corroboration_binding.v1
```

The VLM may return only the claim kind, confidence, horizontal sector, and a
normalized target-center x coordinate. MissionOS discards self-reported
`corroborated_by`, authority, execution, and progress fields.

## Progressive support gate

`progressive_action_supported=true` requires all of the following:

1. `runtime_invocation_evidence.v1` validates and represents a live `llm_api`
   call, not the command-override fixture backend.
2. The captured PNG hash, VLM input hash, and `source_frame_ref` are identical.
3. The decision epoch is present.
4. Camera and LaserScan source timestamps differ by no more than 750 ms.
5. The VLM invocation starts after capture receipt and within 120 seconds.
6. Camera intrinsics project the VLM target center to a bearing within 0.20 rad
   of the LaserScan candidate, with a consistent left/center/right sector.
7. The core-derived LaserScan `target_candidate_id` is present.

Missing, stale, mismatched, malformed, or fixture-only evidence sets
`progressive_action_supported=false`. Camera evidence may still support
conservative actions such as hold or stop. A broad costmap boolean or a
non-empty legacy `corroborated_by` list is not enough for progressive support.

The target binding is geometric. It shows that camera and LiDAR evidence align
with the same angular candidate in one observation window; it does not prove a
semantic identity such as “the same person” across time.

## Claim boundaries

Even a fully bound claim keeps these facts false:

```text
approval_created=false
dispatch_authority_created=false
physical_execution_invoked=false
completion_claimed=false
```

A later approved executor action and verifier evidence are still required.
Fake-sidecar tests prove parsing and fail-closed behavior only; they are not
live-VLM or simulator completion evidence.
