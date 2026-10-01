# Native flight-model admission for ship delivery

The native pilot follows the [Step 2 acceptance gates](ship-onboard-step2.md).
Current acceptance concerns usable absolute performance and online integration.
The comparisons below describe the historical offline pilot; outperforming
them is not a prerequisite for Step 2 or for bounded native-model integration.

The [fresh AeroVLA mode](ship-aerovla-live.md) adds a separate, opt-in online
input/inference/execution chain. The historical CLI workflow below remains
offline and never gains dispatch authority.

For bounded proposal inspection after native inference, see the
[VLA constraint adapter](ship-vla-guard.md). Its candidate receipts grant no
dispatch authority; archived native outputs remain ineligible for live use.
It has two separate boundaries: a PX4/Gazebo mission records real simulator
observations; opt-in model CLIs consume historical observations after capture.
An offline model proposal cannot change or improve that completed flight.
Neither CLI has a PX4 connection, approval authority or flight dispatch path.

## Observations

`missionos ship-delivery run-sitl --capture-anwm` adds a forward RGBD sensor and
a downward RGB sensor while retaining the original onboard decision camera.
It requires an onboard urban policy and `--approve-sitl`. PX4 remains armed in
AUTO_LOITER while twenty frames are collected. The first sixteen are history;
the following four are held-out outcomes. The existing stopping-aware image
rule subsequently flies the approved delivery and recovery route.

All sensors use native 640 × 360 images and 4 Hz simulation cadence. RGBD uses
one optical geometry with 60-degree horizontal FOV; depth is metric float32.
Nonfinite, nonpositive or out-of-range depths become an explicit unknown zero
sentinel only for the upstream positive-Z projection filter. Unknown is not
free space. Both cameras are attached to `base_link` at `(0.25, 0, 0.1)` FLU;
the downward sensor has pitch `pi/2`.

The ordinary scene pose topic does not provide every sensor timestamp. A
model-local PosePublisher instead emits `/model/x500_0/pose` at every physics
step. In Gazebo 8.11, the relevant timestamp is in each nested Pose header,
not the enclosing Pose_V header. The collector ignores initial off-grid sensor
messages. Preparation independently rejects missing/repeated frames and any
interval outside 0.25 ± 0.004 simulation seconds. No interpolation or repeated
images are permitted.

Capture uses a separate process. After successful capture and closed artifact
files, it bypasses Python interpreter destruction to avoid the observed
Gazebo/pybind11 callback GIL shutdown failure. Capture errors still fail the
process. Raw message bytes, decoded RGB/depth, intrinsics, ego pose and source
timestamps remain hash-bound for independent decoding.

Ego poses are simulator ground truth. This is an explicit admission limitation,
not a deployable visual odometry or GPS estimate. Obstacle truth, actor scripts
and case names are not model inputs. Native capture adds a hold interval, so
these flights must not be pooled with the previous VLM latency comparison.

## ANWM boundary

`scripts/ship_anwm.py prepare` writes separate `input/` and `outcomes/`
directories. Transfer only `input/` to the model host. Its NumPy archive permits
exactly historical RGB, depth, optical-camera-to-NED poses, intrinsics and
timestamps. Goal and future images are absent. The sixteen-frame EMA checkpoint
is loaded strictly; its public four-frame YAML is not used as the checkpoint
contract.

- Upstream: `EmbodiedCity/ANWM.code`, revision `657a80268505fa9149c4df502e35aa0f5bce11e5`.
- Model: `EmbodiedCity/ANWM`, revision `dfe59001de57a96d6620313897f09436c6940983`.
- Checkpoint SHA256: `bdd149cac6ec002ba7dc4ad99ec6f9eb02cd6d4f05320195cf174737b13b0bc2`.
- Auxiliary VAE: `stabilityai/sd-vae-ft-ema`, revision `f04b2c4b98319346dad8c65879f680b1997b204a`.
- Frozen settings: seed 42, 250 diffusion steps, four-frame nominal forecast offset.
- Candidate deltas in body FRD metres/radians: hold `[0,0,0,0]`, right `[0,5,0,0]`.

Model time calibration is unverified. `evaluate` scores the hold forecast against
the independently saved future RGB only if measured ego drift is within 0.35 m
and rotation within 0.03 rad. Compare repeat-last-image, geometric depth
projection, velocity fit and stopping fit. Red-mask and pixel errors are image
fidelity measurements, never collision probabilities or delivery improvements.
The right candidate has no observed flight outcome in this pilot.

## AeroVLA boundary

The smaller flight-trained [AeroVLA](https://github.com/XuPeng23/AeroVLA) provides
a feasible L4 candidate. The checked WorldVLN default loader moves its roughly
35 GB float32 backbone onto CUDA before its block conversion; that default path
does not fit a 24 GB L4. This does not establish that an optimized WorldVLN
implementation is impossible.

`scripts/ship_aerovla.py` follows AeroVLA's actual dual-view input: real forward
and downward images, resized to 224 × 224 and stacked vertically. The prompt's
semantic direction comes from the approved mapped delivery goal and observed
simulator ego pose. It contains no obstacle state or future observation.

- Upstream: `XuPeng23/AeroVLA`, revision `2c5ae0987a484ab92f00dd9d9ed493cb3e98e492`.
- Base: `openvla/openvla-7b`, revision `47a0ec7fc4ec123775a391911046cf33cf9ed83f`.
- Flight LoRA: `XuPeng23/AerialVLA`, revision `196f2f3253b69df6e90ac10b6ae041c7b3a9569e`.
- The runner verifies all base weight shards, the flight adapter, and the three
  reviewed custom Python modules before loading them.

Three bins in `[0,98]` decode into forward `[0,5]` m, down `[-5,5]` m, and yaw
`[-1.1,1.1]` radians. Malformed output is rejected instead of becoming a default
zero action. `LAND` is only a terminal proposal. The upstream AirSim controller
first rotates; when absolute yaw is at least 0.25 rad it suppresses horizontal
translation and moves vertically. Thus the vector cannot be assumed to be a
simultaneous PX4 velocity command, and no fixed action duration is established.

## Reproduction

```sh
missionos ship-delivery run-sitl \
  --scenario examples/fixture_missions/ship_delivery/urban-compact.json \
  --urban-case brake_stop --urban-policy onboard_stopping \
  --capture-anwm --approve-sitl --output-dir /tmp/native-ship-new
python scripts/ship_anwm.py prepare \
  --capture /tmp/native-ship-new/native-capture/capture.json \
  --output /tmp/native-wam-new
python scripts/ship_aerovla.py prepare \
  --capture /tmp/native-ship-new/native-capture/capture.json \
  --output /tmp/native-vla-new
```

Model execution is a separate explicit CLI invocation on an installed CUDA
environment. No model downloads, cloud provisioning or billing occurs through
the ship mission CLI. Keep inference, source admission, policy selection,
dispatch, motion and verified mission completion as separate receipts. Native
model invocation alone does not finish Step 2.

### Re-evaluating frozen model results

Keep the exact inference script with the model receipt. If the evaluator changes
after inference, pass that reviewed script with `evaluate --runtime-source`.
Its SHA256 must equal the receipt's `runtime_sha256`; a current but different
script fails the binding check. The evaluation records both the original model
runtime and the current evaluator hashes. Never rerun or replace an unfavorable
prediction merely to accommodate a reporting change.

The velocity fit requires its last six observations; the stopping fit requires
its last eight. Missing an earlier, unused color-mask observation must not
suppress either comparator. A missing observation within a comparator's actual
window makes only that comparator unavailable. These fits and image metrics
remain offline measurements, not flight eligibility or collision guarantees.
