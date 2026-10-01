# Native temporal pad endpoint diagnostic

This experiment records a **negative native capability result**, not a qualified
prediction provider. `pad_temporal_native_result.v1` cannot enter the existing
pad-flight adapter: only hold is present, physical time calibration is unverified,
and no interval-conflict semantics are supplied. No executor or mission judge is
called. Keep AP-only sea policy and the independent current-state Rules intact.

## Acquisition and truth

`capture_yokohama_pad_motion.py` is opt-in CPU Gazebo. It copies an owned static
camera world, animates only named lead/parcel entities, and records exact matched
RGBD timestamps plus nearest observed camera/lead poses (maximum join 12 ms).
The first FOV development attempt is preserved as aborted. The final authored
camera is at pad+10 m, pitched toward pad+4 m; it is not a demonstrated AP hold.
The world contains inherited flight configuration, but those stages are not run.
All 888 recorded frames belong to a static camera rig and scripted lead.

Development and evaluation use the same route and camera with different timing.
Parcel placement is not unloading/contact/receipt verification. Occupancy truth
uses measured horizontal distance to pad: <5.2 m occupied, >6.8 m clear,
otherwise unknown. This conservative 2-D zone is not a 3-D swept collision test.

`prepare_yokohama_pad_temporal.py` reopens every raw asset/capture hash. It fits a
grayscale high-pass nearest-template reader only on development images, then
evaluates fixed thresholds on other timing sequences. The reader uses no color
class, actor schedule or position at inference. Its strong observed-image recall
does not qualify it on generated images. The 696 evaluation frames are correlated;
27 uncertain truth frames are excluded from known-class recall.

## Frozen model request

Four input bundles contain only 16 historical RGBD/camera-pose frames at 4 Hz.
No target images, actor poses or schedules are uploaded. Metadata case names are
not model conditioning. Exact last stamp +16e9 ns binds the withheld endpoint.

- Base ANWM and motion-v4 adapter hashes are frozen in `results/identity.json`.
- Hold only; seed 42; `create_diffusion("50")`; `num_timesteps=64`.
- Upstream uses `num_timesteps / 128`; at this input cadence 64 means a requested
  16 seconds. Existing motion-v4 adaptation used value 1 for one-second static
  targets. Parameter conversion is not physical forecast calibration.
- The existing 250-step, index-1 live service validator is not relaxed. A separate
  diagnostic invokes the pinned rendering object and labels its outputs with the
  new schema and false flight/dispatch/time-calibration fields.
- No training, resampling, VLA inference, Mission Assurance judgment or flight.

Both sampler and time conditioning changed from the earlier profile. This is not
a controlled ablation attributing bad images to one cause. The warm request timer
includes prediction/serialization but excludes transport/model load; comparison
to simulator seconds assumes real-time speed and does not establish AP timing.

## Evidence and verification

The evaluator reopens input/result/target hashes, checks the frozen hold candidate
and target timestamp, and reads both actual and predicted images. It retains the
current-image persistence baseline. `native_flight_admitted` and
`interval_conflict_prediction_verified` remain false even when a state matches.
Background consistency passing does not override unqualified actor semantics.

The public bundle contains original input arrays, target crops, model results,
reader, capture metadata/measured poses, executed sources, camera videos, cost
receipt and hashes. Full raw camera/depth files are retained privately; only the
four native histories are included publicly. Video is 4 Hz H.264 from raw camera
PNGs, no interpolated motion or synthetic battery. Qualitative assistant review
is explicitly post-inference and separate from the frozen numerical evaluation.

Actual commands used (task-owned paths represented by portable variables):

```sh
python scripts/capture_yokohama_pad_motion.py --approve-sitl \
  --source-rig "$SOURCE_RIG" --output-dir "$CAPTURE"
python scripts/prepare_yokohama_pad_temporal.py \
  --capture "$CAPTURE" --output "$PREPARED"
python scripts/probe_yokohama_pad_temporal.py \
  --inputs "$PREPARED/inputs" --output "$RESULTS" --execute-native \
  --upstream "$ANWM_SOURCE" --checkpoint "$ANWM_CHECKPOINT" --adapter "$MOTION_ADAPTER"
python scripts/evaluate_yokohama_pad_temporal.py \
  --prepared "$PREPARED" --results "$RESULTS" --output "$EVALUATION"
```

Runtime observed: 888 CPU frames, four actual L4 WAM calls, 32 downloaded files
hash-verified, 1/4 endpoint matches, 2/4 unknown, one false-clear reentry. All four
generated images fail qualitative lead/pad recognition. CUDA allocated memory
zero after work; owned VM/disks and both capture containers removed. Cumulative
estimate $17.8504896/$20, invoice unconfirmed.

Public CPU recheck: `python scripts/check_yokohama_pad_temporal.py --bundle
docs/examples/yokohama-pad-temporal`. It verifies the manifest, source/capture
bindings and reruns the native-result evaluation without a model or simulator.
This proves the recorded negative result is reproducible, not a successful flight.

CPU portability: reconstructing the background raster on x86 produced small
diagnostic edge/MAE differences while every occupancy result and gate outcome
remained identical. Preserve the original `evaluation.json`; do not loosen its
comparison tolerance. `past-references/` freezes the original arm64 reconstruction
and mask, hash-bound to each historical input and the reconstruction source.
The maintained evaluator accepts `--reference-dir` to reopen those diagnostic
rasters; without it, the background is reconstructed on the current CPU and its
statistics need not match bit-for-bit. Original executed sources remain intact;
`replay-source/` records this portability repair. See `portability.json` for the
failed CI run and measured differences. Native forecasts, target images, reader
thresholds, classification results and flight-admission policy are unchanged.

[Human report](../examples/yokohama-pad-temporal/REPORT-ja.md)

Follow-up: [conditional dynamic post-training](yokohama-pad-learning.md) separates
the sampler/time factors and records a real weight update. It restores background
appearance but still erases lead traffic, so neither result is admitted for flight.
