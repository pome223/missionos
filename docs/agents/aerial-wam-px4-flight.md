# ANWM and Jev to an isolated PX4 SITL flight

This opt-in integration connects actual simulator camera observations, actual
ANWM inference, a Jev judgment, an existing user authorization, bounded Rules,
and a separate PX4 executor. A bounded trial on 2026-09-22 completed that
integration through observed candidate motion, landing, and disarm. Its goal
selection was wrong. The result below distinguishes the completed connection
from prediction usefulness; the remaining sections specify its runtime contract
and portable orchestration.

The [initial public dataset replay](aerial-wam-model-evaluation.md) and the
[grounded camera diagnostic](px4-aerial-camera-registration.md) remain separate
experiments. Hardware execution and arbitrary vehicle endpoints are unsupported.

## Observed integration result

The 2026-09-22 final trial's model receipt, Jev API receipt, signed command, simulator
telemetry, and terminal flight receipt agree on `right_5m`. Takeoff, the selected
candidate's motion, landing, and disarm were observed; the terminal receipt
contains no error. These results cover one isolated static-scene SITL trial.

A separate 2026-09-23 repeat again reached actual ANWM inference, Jev judgment,
bounded dispatch, 5.05848 m observed candidate motion, landing, and disarm.
Its left-side goal was again scored lower for the right candidate, so goal
selection failed again. Earlier captures aged past the 180 s input limit before
judgment; they produced no Jev judgment or candidate dispatch. The repeat
is documented in the [Japanese technical report](aerial-wam-px4-technical-report-20260922.md#9-2026-09-23追試鮮度失効と再飛行).

| Boundary | Observed result |
| --- | --- |
| ANWM | Two actual candidate calls; 39.6495 s forecasting in total. |
| Jev | Actual `jev-1.13.0` decision API call, 549.18 ms; `replan` mapped to `right_5m`. |
| Freshness at dispatch | Original image age 145.506 s; command age 0.1294 s. |
| Candidate motion | 5.10943 m observed displacement; 0.12880 m endpoint error relative to the selected target. |
| Timing | Candidate motion took 6.824 simulation seconds; the nominal model horizon was 1 s and was not matched to that duration. |
| Terminal state | Landing and disarm independently observed; no terminal error. |
| Cleanup and cost | Simulator removed after landing; rented GPU VM and disk confirmed deleted. Cumulative compute, disk, and IP estimate approximately $1.24, below the $10 cap; invoice and Jev API billing not confirmed. |

The declared reference camera was on the **left**. Its distance from the planned
left camera endpoint was 0.04154 m, versus 9.99991 m from the right endpoint.
Nevertheless, ANWM's right goal-image MSE was lower: `0.029646` versus left
`0.035748`. Jev followed that score and selected right. Thus **flight integration
succeeded, but goal selection failed** in this trial. The endpoint-distance
comparison is independent geometry, not an observed flight of the unselected
candidate. No model advantage, calibrated collision prediction, accurate future
image, or autonomous goal completion is established.

## Source and model contract

The flight source is `px4_gazebo_frozen_capture` with capture scope
`px4_sitl_airborne_observation`. It is never relabelled as public dataset replay.
The capture process sends no actuator commands; its bound flight-session record
separately establishes that the vehicle was in an actual PX4 offboard hover.

- Sixteen distinct RGB/depth/pose observations use measured Gazebo simulation
  timestamps spaced at 0.25 s, with at most 0.004 s interval error. Both camera
  information messages and the RGB, depth, and pose messages must share the
  exact timestamp within each observation. Frames are not repeated or invented.
- The source uses native 640 x 360 RGB. Depth is registered using the audited
  source SDF, CameraInfo, and sensor poses; preparation recomputes that geometry.
  ANWM then applies its existing image crop and 224 x 224 transform.
- Registered unknown depth remains NaN with a boolean mask in source artifacts.
  Preparation retains mask and depth hashes and uses zero only for the upstream
  forward projection's positive-Z filter. Unknown space does not become free
  space or a synthetic distant surface.
- The goal is an independently captured static Gazebo RGB camera with role
  `declared_goal_reference`. Its image, original message, camera SDF, intrinsics,
  pose, and scene identity are bound. It is used only for goal-image scoring;
  future candidate outcomes never enter the predictor archive. Contradictory
  future/outcome/flight declarations fail before admission.
- The fixed candidates are `left_5m` and `right_5m`, with body-FRD deltas
  `[0, -5, 0, 0]` and `[0, 5, 0, 0]` at the observed hover height near 3 m.
  Candidate hashes bind the deltas and target optical-camera poses. Gazebo-local
  NED and PX4 estimator-local NED remain distinct; the executor uses their
  measured alignment rather than assuming equal origins.

The released checkpoint requires sixteen context images, strict loading, and
the pinned upstream and auxiliary VAE revisions. The trial uses seed 42 and
250 diffusion steps. The four-frame forecast offset gives a nominal one-second
model horizon. Verified simulation cadence does not validate the model's time
calibration: `physical_frame_timing_verified` and
`model_time_alignment_verified` remain false, and `horizon_seconds_nominal`
remains true. The executor has its own motion speed and deadline.

Goal MSE is an image-compatibility cost with `risk_score: null`. It is neither
collision probability nor evidence that ANWM improves navigation. The declared
scene intentionally supplies independently clear lateral candidates for an
integration test; no learned-model advantage is implied.

## Authority, geometry, and freshness

The retained `session/user-authorization.json` must record an actual explicit
user instruction: `instruction_ref`, the original `text`,
`authorized_at_unix_s`, and `hardware_allowed: false`. Its reference must match
the initialized session. Do not invent authorization text or convert a Jev
response into approval. The policy builder hashes this existing record; it does
not create permission.

The policy builder checks the exact static collision-box SDFs, their declared
poses and sizes, scene hashes, and both candidate segments. Each segment must
clear boxes expanded by 0.6 m on every axis and remain above the ground with the
same margin. The reference camera must have no collision geometry. Candidate
endpoints are confined to horizontal coordinates within ±6 m and altitude
2.8–3.2 m. The controller independently checks observed scene poses remain
unchanged. This is a declared static-scene check, not general obstacle safety or
a dynamic-scene guarantee.

The evaluator checks the original observation before and after calling Jev:

| Boundary | Requirement |
| --- | --- |
| Original model input | At most 180 s old, measured from the earliest UTC receipt among the five messages in the last historical frame. Capture completion, file copy, preparation, and model output cannot renew it. |
| Current state | At most 2 s old; armed PX4 offboard mode, unchanged scene, position within 0.2 m of the captured hover, speed at most 0.2 m/s, yaw within 0.05 rad. |
| Candidate command | Existing session, candidate, scene, model receipt, Jev receipt, and authorization hashes; a validity window of at most 5 s. |
| Executor | HMAC verification, unused command identity, current stationary hover and scene revalidation, then the fixed candidate only. |

Run admission and dispatch on the same host clock. Dispatch immediately after
successful evaluation, without a manual pause. Expiration fails closed; do not
edit timestamps or reuse a stale forecast under a new observation time. The
local HMAC key protects the session inbox, not remote model authenticity or
human approval. It remains local and must not be published with evidence.

## Simulator physics prerequisite

[PX4 issue #27480](https://github.com/PX4/PX4-Autopilot/issues/27480) documents an
installed startup path that sends only `real_time_factor` to `set_physics` when
`PX4_SIM_SPEED_FACTOR` is set. This can zero physics-engine gravity while the
SDF and cached IMU gravity still appear normal. The scene helper explicitly
applies gravity `(0, 0, -9.8)` m/s², real-time factor `0.1`, and maximum step
`0.004` s after startup and before flight, retaining
`physics-configuration.json`. Service acceptance is a configuration receipt;
subsequent hover and landing must still be observed. Preserve failed attempts
and do not compensate with forced poses or altered thrust.

## Portable orchestration

Run from the repository with its Python dependencies installed. The simulator
image must already contain PX4, Gazebo, and the required sensors. Install and
warm the separate CUDA model environment before starting a flight or capturing
fresh input. Model weights and Jev credentials do not belong in the simulator
container. `JEV_SECRET_PROJECT` names the caller's Secret Manager project; the
evaluator retrieves the configured secret without printing it.

Use an unused output directory and an existing authorization record. The two
opt-in variables are required independently. The scene helper creates a
labelled, network-isolated `missionos-aerial-flight-e2e` container and refuses to
replace an existing one.

```sh
export PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src
export RUN_PX4_AERIAL_WAM_FLIGHT=1
export RUN_PX4_AERIAL_FLIGHT_SESSION=1
AERIAL_RUN="$PWD/output/aerial-flight"

python scripts/px4_aerial_flight_scene.py --output-dir "$AERIAL_RUN" --phase setup
python scripts/px4_aerial_flight_scene.py --output-dir "$AERIAL_RUN" --phase goal

python scripts/px4_aerial_flight_session.py initialize \
  --session-dir "$AERIAL_RUN/session" \
  --scene-sha256 "$AERIAL_SCENE_SHA256" \
  --approved-instruction-ref "$AERIAL_INSTRUCTION_REF" \
  --scene-entities-file "$AERIAL_RUN/session/scene-entities.json"
cp "$AERIAL_AUTHORIZATION_RECORD" "$AERIAL_RUN/session/user-authorization.json"
python scripts/px4_aerial_flight_session.py start --session-dir "$AERIAL_RUN/session"
```

`AERIAL_SCENE_SHA256` must come from the setup receipt; `AERIAL_INSTRUCTION_REF`
and `AERIAL_AUTHORIZATION_RECORD` must refer to the retained instruction. The
`start` command actually initiates the authorized SITL takeoff. Starting a
controller is not proof of takeoff or stable hover. Wait for its fresh
`session/status.json` to report `phase: holding`; a timeout must lead to the
controller's landing/failure path, not input fabrication.

```sh
python scripts/px4_aerial_flight_session.py status --session-dir "$AERIAL_RUN/session"
python scripts/px4_aerial_flight_scene.py --output-dir "$AERIAL_RUN" --phase history
python scripts/register_px4_aerial_capture.py \
  --capture "$AERIAL_RUN/session/history/capture.json" \
  --output-dir "$AERIAL_RUN/registered"
python scripts/prepare_px4_anwm_input.py \
  --capture-dir "$AERIAL_RUN/session/history" \
  --registration-dir "$AERIAL_RUN/registered" \
  --goal-reference "$AERIAL_RUN/session/goal/reference.json" \
  --output-dir "$AERIAL_RUN/input" \
  --upstream-root "$ANWM_UPSTREAM_ROOT" \
  --checkpoint-path "$ANWM_CHECKPOINT_PATH"
```

The request's model paths must resolve on the inference host. Transfer the
prepared request and assets to that host without changing source times or
contents, run the following command there, and return its complete result and
images. Copying artifacts is orchestration, not a new observation. A stale
result cannot authorize candidate motion.

```sh
python scripts/aerial_anwm_runtime.py \
  --request INPUT/request.json --output-dir MODEL_OUTPUT
```

After placing the model output at `$AERIAL_RUN/model`, build the independent
policy, then run evaluation and dispatch as one uninterrupted operation. The
second command executes only if evaluation succeeds.

```sh
python scripts/build_px4_aerial_execution_policy.py \
  --result "$AERIAL_RUN/model/result.json" \
  --session-dir "$AERIAL_RUN/session" --output "$AERIAL_RUN/policy.json"

python scripts/evaluate_px4_aerial_wam_jev.py \
  --result "$AERIAL_RUN/model/result.json" --policy "$AERIAL_RUN/policy.json" \
  --telemetry "$AERIAL_RUN/session/status.json" \
  --output "$AERIAL_RUN/evaluation.json" --command-output "$AERIAL_RUN/command.json" \
  --secret-project "$JEV_SECRET_PROJECT" && \
python scripts/px4_aerial_flight_session.py dispatch \
  --session-dir "$AERIAL_RUN/session" --command-file "$AERIAL_RUN/command.json"
```

An admitted forecast, a Jev response, a permitted command, and an inbox write
remain separate from observed motion. Verify `session/events.jsonl`,
`session/telemetry.jsonl`, and `session/flight-result.json` for candidate
acceptance, actual Gazebo displacement, terminal position, landing, and disarm.
The controller lands after the bounded candidate or on its timeout/failure
path. A separate retained `land` request is available when needed:

```sh
python scripts/px4_aerial_flight_session.py land \
  --session-dir "$AERIAL_RUN/session" --reason caller_requested_land
```

After collecting the terminal receipt, clean up the exact recorded simulator:

```sh
python scripts/px4_aerial_flight_scene.py --output-dir "$AERIAL_RUN" --phase cleanup
```

Keep `cleanup.json` and independently confirm any rented GPU and attached disk
are removed. The scene helper handles only its own simulator container. No file
in this sequence establishes hardware execution, calibrated forecast accuracy,
collision avoidance learned by ANWM, or general mission completion.
