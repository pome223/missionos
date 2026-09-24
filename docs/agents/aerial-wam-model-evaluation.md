# Aerial world-model evaluation

This document defines the bounded evaluation of a released aerial world model
through MissionOS. It extends the [navigation predictor contract](navigation-wam-jev.md).
Model execution, prediction usefulness, Jev judgment, approval, and flight are
separate results. In the initial public dataset replay, a released ANWM checkpoint
ran on two supplied candidate actions, its recorded predictions were admitted,
and the real Jev API produced a bounded response. The learned choice was worse
than the projection baseline on the independent endpoint-distance proxy. That
replay performed no flight. The separate [PX4 SITL flight contract](aerial-wam-px4-flight.md)
defines airborne capture, typed model input, existing authorization, and the
bounded execution and verification path. Its separate trial completed observed
candidate motion, landing, and disarm, while choosing the wrong side for the
declared visual goal; that connection result does not establish model usefulness.

## Model and runtime requirements

| Model | Inputs and output | Runtime dependencies | Evaluation status |
| --- | --- | --- | --- |
| [UA-NWM](https://github.com/DurYi/UA-NWM) | Visual history, goal image, and candidate aerial trajectory; predicts DINO features and an uncertainty-aware goal discrepancy. | Released world-model checkpoint and the gated `facebook/dinov3-vitb16-pretrain-lvd1689m` encoder. Upstream recommends Python 3.10, PyTorch 2.5.1, torchvision 0.20.1, and transformers >= 5.3. | Encoder access is unavailable in the current evaluation environment. Runtime evaluation is blocked before inference. |
| [ANWM](https://github.com/EmbodiedCity/ANWM.code) | RGB history, depth/camera geometry, and 4-DoF displacement/yaw actions; predicts future RGB along candidate trajectories. | Released `0200000.pth.tar`, `stabilityai/sd-vae-ft-ema`, CUDA PyTorch, and compatible data. Images are 224 x 224; the downloaded checkpoint requires sixteen context frames despite the public configuration describing four. | Two actual candidate predictions completed on NVIDIA L4; admitted results reached the real Jev API. |

UA-NWM's encoder access restriction is independent of GPU availability. Do not
substitute a different encoder while reporting the result as the released model.
An authorized encoder or verified compatible cache is required to resume that
model's evaluation. The optional UA-NWM RGB decoder is not needed for scoring.
Its released input contract uses four 224 x 224 RGB context frames, a goal
image, and eight candidate steps expressed in the fixed decision-pose body-FRD
frame. Translation is scaled by 5 m before the configured action normalization.
Preserve the embedded statistics and yaw convention when adapting a new source.

The [ANWM model card](https://huggingface.co/EmbodiedCity/ANWM) identifies the
released checkpoint and labels its license Apache-2.0. This does not determine
the terms of every code, dataset, or auxiliary model dependency. ANWM's upstream
real-world results are offline; they do not establish autonomous PX4 deployment.
Neither model's published result supplies a MissionOS latency or memory result.

## Bounded replay design

For the optional PX4/Gazebo flight path, preserve gravity when changing simulation
speed. [PX4 issue #27480](https://github.com/PX4/PX4-Autopilot/issues/27480)
documents a startup bug where a `set_physics` request containing only
`real_time_factor` resets physics-engine gravity to zero. The installed image's
`px4-rc.gzsim` lines 153–158 contain that request. The SDF still declares gravity,
and cached IMU gravity can conceal the problem from the autopilot; neither is
sufficient proof of correct runtime physics.

`scripts/px4_aerial_flight_scene.py` explicitly sends gravity `(0, 0, -9.8)` m/s²,
real-time factor `0.1`, and maximum step `0.004` s after startup and before flight.
It requires service acceptance and records `physics-configuration.json`. That
receipt establishes configuration acceptance, not flight success. Preserve the
failed attempt separately and verify the subsequent hover and landing from
observed simulator motion. Do not compensate by changing thrust, teleporting the
aircraft, or claiming that a normal-looking IMU proves gravity was applied.

Start with a small, explicitly selected upstream dataset sample and the released
checkpoint. Preserve the upstream input normalization, camera convention, action
units, action timing, and depth semantics. Record any change to sampling steps,
horizon, candidate count, precision, or preprocessing. A reduced inference run
is not a reproduction of the paper's full benchmark.

Strict loading of the released checkpoint revealed a context-size discrepancy:
its `ema.pos_embed` has shape `(17, 196, 1152)`, while the public configuration
requests four historical frames. The adapter uses sixteen distinct historical
frames (indices 0 through 15), the declared goal at index 19, and a four-step
forecast offset. It neither crops checkpoint tensors nor repeats or fabricates
history. The manifest, model record, and runtime receipt preserve context size
16; this correction must remain visible in any reproduction claim.

The bounded ANWM replay uses the upstream nominal 4 fps setting to name its
horizon. Dataset timestamp metadata contains source row indices rather than
verified wall-clock times. Preserve `physical_frame_timing_verified: false` and
`horizon_seconds_nominal: true`; a nominal one-second replay is not evidence of
prediction over one second of physical flight.

Each request must bind the source dataset revision, sample identity, observation
history, goal reference, candidate action sequence, model checkpoint digest,
auxiliary model revision, and inference configuration. The goal is a declared
task input. Future frames used to verify predictions and candidate outcome
labels must remain outside predictor inputs. Dataset actions are replay inputs;
they must not be described as plans produced online by MissionOS.

Compare the same candidates under a declared simple baseline and the learned
score. Before testing improvement, establish candidate outcome differences and
the best achievable result among those candidates. If the baseline already
matches that result, the sample can validate invocation and contracts but cannot
demonstrate useful selection headroom. Retain unfavorable and tied results.

The [value experiment contract](aerial-wam-value-experiment.md) specifies the
next urban 3D navigation hypothesis: building passage, climbing and lateral
avoidance, with actual PX4 route outcomes, strong cheap baselines and latency
accounting. This supersedes the inspection-view proposal. The completed CPU
audit found the calibration target outside every history frame; the new urban
experiments remain unexecuted.

The first integration boundary is:

```text
source-bound replay inputs -> actual model inference -> typed prediction
  -> evidence admission -> configured Jev judgment -> approval still required
```

This boundary does not dispatch a PX4 command. The model service must reject
missing or incompatible image, depth, action, or environment contracts rather
than manufacture predictions from generic telemetry or a named `hold` action.

## Goal compatibility is not collision risk

A goal-image discrepancy describes how a generated observation agrees with a
goal reference. It does not establish obstacle clearance, feasibility, battery
margin, collision probability, or successful arrival. Do not normalize that
cost into a purported risk probability.

An uncalibrated goal forecast uses `risk_score: null` and this typed value under
`future_state.goal_compatibility`:

```json
{
  "schema_version": "missionos_goal_compatibility.v1",
  "metric_id": "anwm_goal_image_mse",
  "raw_cost": 0.0,
  "lower_is_better": true,
  "risk_assessed": false
}
```

The number above illustrates the schema; it is not a measured result. The other
supported metric is `uanwm_hep_residual`. `raw_cost` must be finite and
nonnegative; booleans and unknown metrics are rejected. Compare costs only under
the same metric, preprocessing, horizon, and goal reference. Existing numeric
risk forecasts retain their separate contract. Admission validates evidence
shape and binding; it does not prove that the server executed the named weights.

Jev may use admitted goal evidence when choosing among existing bounded
responses. Its response remains a judgment. Human approval or an existing
bounded policy, Rules, Executor, and Verifier retain their separate roles.

## Generic route input boundary

The current native route runtime launches `gz_x500`. Its
`_pose_sample()` in
[`entrypoint.py`](../../src/runtime/px4_gazebo_route/entrypoint.py) returns
world-frame XYZ, while the Assurance snapshot identifies
`gazebo_world_xy_altitude_up`. The source pose timestamp is retained, including
when an earlier deviation pose is cached. This snapshot does not supply RGB,
camera calibration, yaw, image history, or a goal image. The existing logical
camera smoke supplies detected objects, not RGB pixels.

Before a live PX4 prediction is claimed, provide:

1. A camera capture contract binding image bytes/hash, sensor time, receipt
   time, intrinsics, camera frame, and synchronized vehicle pose/orientation.
2. Model-compatible depth where required, plus its units, calibration, and
   provenance; estimated depth must remain distinguished from simulator depth.
3. An independently declared goal image and a bounded candidate trajectory with
   explicit frame transforms, yaw convention, displacement units, and timing.
4. Fresh observation and candidate revalidation through the existing approval
   boundary, followed by independent simulator outcome observations if a flight
   comparison is performed.

The existing generic action plus `hold` interface is insufficient to infer these
inputs. A successful replay and a successful fixture transport smoke do not
close this live sensor/action gap. The separate flight contract supplies an
explicit airborne adapter; it does not infer these fields from generic route
telemetry. Live simulator execution remains opt-in, and hardware is unsupported
by that bounded adapter.

### Grounded sensor acquisition probe

The follow-up [CPU registration contract](px4-aerial-camera-registration.md)
defines depth/RGB alignment, optical-frame conversion, recorded history timing,
and strict separation of frozen simulator captures from public dataset inputs.
This adapter does not itself make a capture eligible for ANWM or live dispatch.

[`probe_px4_aerial_camera.py`](../../scripts/probe_px4_aerial_camera.py) starts a
new disposable `gz_x500_depth` simulator with no container network or published
ports. It sends no arm or flight commands and removes its own container on exit.
It refuses to reuse an existing container name or a nonempty output directory.

```sh
RUN_PX4_AERIAL_CAMERA_PROBE=1 \
  python scripts/probe_px4_aerial_camera.py --output-dir output/aerial-camera-probe
```

The bounded runtime check collected four samples whose RGB, depth, both camera
information messages, and vehicle-pose messages shared exactly equal simulation
timestamps: 4.260, 4.624, 5.284, and 5.680 seconds. These are simulation times,
not wall-clock timestamps or evenly spaced model context frames. Receipt times,
source-message hashes, raw bytes, calibration, pose messages, and model SDF
digests are retained. PNG decoding was checked against the original RGB bytes;
container deletion was verified.

Native RGB is 1920 x 1080 and depth is 640 x 480, with different intrinsics.
The two sensor poses in the captured model SDF are identical: their relative
translation is zero and their relative rotation is identity. Their common
position relative to the vehicle model is `(0.13233, 0, 0.26078)` metres.
The probe derives sensor pose from the observed vehicle pose and these fixed
extrinsics, retaining the separately observed camera-link pose. It does not
reproject depth onto RGB, convert to the model's optical convention, fill invalid
depth, supply a goal image, or invoke ANWM. `model_input_ready` remains false.
The empty grounded default scene is useful for acquisition validation, not a
candidate-selection improvement comparison.

## E2E / Runtime Verification

The following commands express the executed preparation, inference, and
admission boundaries with portable paths. Install the pinned upstream code and
checkpoint first; supply the Secret Manager project through the local
`JEV_SECRET_PROJECT` environment variable. Its value and the retrieved key are
not recorded here.

The optional inference script imports an external ANWM checkout; neither its
implementation nor the checkpoint is vendored into MissionOS. Obtain the
[released weights](https://huggingface.co/EmbodiedCity/ANWM),
[public dataset](https://huggingface.co/datasets/EmbodiedCity/ANWM-Dataset), and
[auxiliary VAE](https://huggingface.co/stabilityai/sd-vae-ft-ema) from their
upstream sources, respecting each dependency's terms. Use a CUDA device with
bfloat16 support and the verified dependency versions in the runtime table.
The preparation script also requires Pillow. The runtime checks the upstream
revision and checkpoint digest before loading the model; unavailable or
incompatible assets must stop the run rather than select a substitute model.

```sh
python scripts/prepare_aerial_anwm_sample.py \
  --output output/aerial-wam \
  --upstream-root ../../third_party/ANWM.code \
  --checkpoint ../../pretrained/ANWM/0200000.pth.tar \
  --diffusion-steps 250

python scripts/aerial_anwm_runtime.py \
  --request output/aerial-wam/request.json \
  --output-dir output/aerial-wam/seed42

PYTHONPATH=.:packages/missionos-core/src \
  python scripts/evaluate_aerial_wam_jev.py \
  --result output/aerial-wam/seed42/result.json \
  --output output/aerial-wam/admission-jev.json \
  --jev-mode primary \
  --secret-project "$JEV_SECRET_PROJECT" \
  --jev-secret-name jev-api-key
```

The runtime resolves checkpoint and upstream paths relative to the request
directory; the two `../../` paths above therefore resolve from
`output/aerial-wam` to the repository root. These locations are runtime
configuration, not part of the data sent to Jev.

| Bound input | Verified value |
| --- | --- |
| Upstream code revision | `657a80268505fa9149c4df502e35aa0f5bce11e5` |
| ANWM model revision | `dfe59001de57a96d6620313897f09436c6940983` |
| ANWM checkpoint SHA-256 | `bdd149cac6ec002ba7dc4ad99ec6f9eb02cd6d4f05320195cf174737b13b0bc2` |
| VAE revision (`stabilityai/sd-vae-ft-ema`) | `f04b2c4b98319346dad8c65879f680b1997b204a` |
| Dataset revision | `f0fcc70df0b3c8c26286adbfb39ccdf5e3b7ad83` |
| Dataset trajectory | `302OLP89E75X7MFPRR58QG7CIDVACC_processed` |
| Input archive SHA-256 | `e608a745302746854f5f0ef6e74f8c5df327651de85b0d4829ac7b37945ab04e` |
| Canonical input manifest SHA-256 | `b9595b66a6a1b4c1cd493deeacda7a1295bcb78a21756a5893e019057e7c5fec` |
| Canonical forecast list SHA-256 | `c48d7965f53c9a0cbe4f2000828b9266e3c7cc094d7f43bb7311b822e173e89d` |

The sixteen historical images use indices 0 through 15, with goal image 19.
Both candidates were fixed before reading the future geometry: a forward
translation of 5 m, or a left translation of 5 m with a -15-degree yaw change,
in the declared body-FRD convention. The seed was 42 for both candidates and
the sampler used 250 steps. The four-frame prediction offset is one nominal
second under the upstream 4 fps convention; physical timing is unverified.

| Candidate | ANWM goal-image MSE | Projection-only goal-image MSE | Independent endpoint distance to goal |
| --- | ---: | ---: | ---: |
| `continue_forward` | 0.14815618 | **0.18356198** | **5.38516 m** |
| `replan_left` | **0.07572242** | 0.19169652 | 11.35782 m |

ANWM preferred `replan_left`; the projection-only baseline preferred
`continue_forward`. The evaluator-only goal-pose comparison also preferred
`continue_forward`, so the baseline already selected the best candidate on that
proxy and left no positive selection headroom in this sample. The learned
choice was worse on the endpoint proxy. These distances describe planned
endpoints, not executed outcomes or collision checks. No observed future frame
corresponds to either fixed alternative plan, so this run does not establish
forecast accuracy. The sample may occur in the training data and is not a
held-out benchmark.

| Runtime boundary | Observed result |
| --- | --- |
| Actual ANWM inference | Two calls using the pinned checkpoint on NVIDIA L4; `fixture_invocation: false` |
| Model load | 16.3710 s |
| Candidate forecasting | 40.9818 s total; 20.9717 s forward and 20.0100 s left |
| Peak PyTorch allocated GPU memory | 7,321,750,528 bytes; this is not total process or reserved memory |
| Runtime dependencies | torch `2.9.1+cu129`, torchvision `0.24.1+cu129`, diffusers `0.40.0`, timm `1.0.29`, numpy `2.2.6` |
| Admission | `adopted`, with `risk_score: null`; the saved result was replayed without rerunning ANWM |
| Jev | Requested `jev-latest`, observed `jev-1.13.0`; selected `replan`, corresponding to `replan_left` for visual-goal compatibility |
| Jev API latency and usage | 618.5365 ms; 3,796 input tokens and 71 output tokens |
| Authority | No approval requested, no dispatch, no flight, no physical execution, no completion claim |
| Live PX4 boundary | Separate grounded camera probe only; its frames were not inputs to this ANWM replay |
| Contract verification | 175 focused tests passed, covering goal metrics, replay validation, and navigation HTTP admission |
| Cloud resource cleanup | VM and boot disk deletion completed; both resources were subsequently confirmed absent |
| Cost estimate | $0.72577 including a conservative $0.25 ancillary allowance; under the $10 cap, invoice not yet confirmed |

The resource lifetime used for the estimate was 2,005.781 seconds. At the
recorded VM rate of $0.853624312 per hour, compute was approximately $0.4756065.
The Jev input-token estimate was $0.000159432; the separate $0.25 allowance
covers ancillary uncertainty. This is an estimate with an allowance, not a
confirmed invoice. The VM and its boot disk were removed after evidence was
retrieved.

The Jev receipt records an actual decision API response, not generated textual
reasoning. Its rationale string is an adapter summary. The geometric oracle
was never supplied to ANWM or Jev. Selecting the lower learned goal cost shows
that the evidence reached the configured judge; it does not show that the
learned score chose a better route.

An HTTP request/response receipt alone proves transport invocation. An actual
model-run record must also identify the loaded checkpoint and configuration and
bind produced forecasts to that run. Neither receipt establishes prediction
accuracy, a better route choice, flight execution, or mission completion.
