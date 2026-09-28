# CPU learned pad state, v1

This is a bounded **capability diagnostic**, not an ANWM adapter, VLA fine-tune,
world-model added-value proof, or flight-qualified selector. The owner has removed
the aggregate 90% adoption gate; proceed with advisory integration based on
practical usefulness, as recorded in `adoption-policy.json`. Live integration
is not claimed complete. The existing
`src/runtime/yokohama_pad_prediction.py` boundary is unchanged.

## Runtime inputs and authority

`Model.predict(rgb, stamps_ns, camera_poses)` accepts exactly 16 past RGB frames
at 320×180 (full FOV, bilinear-resized once), integer nanosecond timestamps at
4 Hz (4 ms tolerance), and the measured own camera poses. Every pose must match
the training camera to 1e-7 absolute tolerance. Neither actor pose, elapsed case
time, schedule, case ID, future image nor target label is passed to inference.
Camera pose is in the `ship_anwm.camera_pose` NED convention, not ENU.

Output schema `missionos.pad-state-forecast.v1` binds 17 samples to the final
input timestamp plus 0..4 seconds. It always declares:

- `flight_admitted=false`, `dispatch_invoked=false`;
- `action_conditioning_verified=false`, `interval_collision_verified=false`;
- CPU object/state model, not ANWM or VLA.

The inference API has no aircraft or GPU dependency. It rejects changed camera,
broken cadence or input geometry. Ambiguous/missing lead or unsupported history
makes **every** forecast unknown with null positions/occupancy scores. It cannot
infer absence from nondetection. `review_entry` in the evaluator is an advisory
only when every sampled state is clear, and never executes an action.

## Learning, not hidden simulator truth

A 17×17 two-channel linear convolution detector is trained on every fourth
training frame. Channels are grayscale minus .5 and four times the difference
from a 5×5 local mean. Targets are projected measured lead centers in training
only. Three rounds of hard-negative mining include the shadows that confused
the initial development detector. Detection uses a training-derived ROI, a
score floor .7 and a distinct-peak margin .1. The 10-term cubic pixel-to-3D fit
assumes the known recorded corridor; it is not arbitrary 3D reconstruction.

Kernel ridge regression maps current learned position and motion over 1, 2 and
3 seconds to 17 future position displacements and occupancy scores. Twelve
factor/regularization pairs are selected on the **development** sequences,
minimizing false clear, then maximizing known-label agreement, then minimizing
position error. Selected factor=1, ridge=.001. Empirical horizontal radii are
max(.2 m, development q99 error + .1 m), per horizon. Nearest training feature
support is limited to 1.25× the maximum development distance (at least .1).
These are empirical limits, not calibrated probabilistic safety guarantees.

Truth: horizontal pad distance <5.2 m means occupied, >6.8 m clear; the gap is
unknown. Predicted clear additionally requires distance minus empirical radius
>6.8 and occupancy score <.25. Predicted occupied requires distance plus radius
<5.2 and score >.75. Otherwise unknown. Visibility of one known lead cannot
establish that an arbitrary delivery pad is clear of all aircraft or people.

Training = old `train-*` 720 frames. Development = old `heldout-*` 752 frames,
explicitly no longer held out. Both precede the frozen protocol. Three new
`state-*` cases contain 704 frames; 155 overlapping 16-frame histories are
scored at 1 Hz against 0..4 second targets. Scene/camera/corridor/pixel reuse is
reported, not claimed as unseen-state generalization. The final new test was
run once without retuning. Failed one-stage regression and initial shadow
localization attempts are retained in the local experiment archive.

## Reproducible commands and observed boundary

```sh
python scripts/run_yokohama_pad_state.py prepare \
  --capture "$OLD_CPU_CAPTURE" --case-set learning-v1 --output "$OUT/development-data"
python scripts/run_yokohama_pad_state.py train \
  --dataset "$OUT/development-data" --output "$OUT/model"
# The weights and protocol are frozen before this new acquisition.
python scripts/capture_yokohama_pad_motion.py --approve-sitl \
  --source-rig "$SOURCE_CAMERA_RIG" --case-set state-eval-v1 --output-dir "$OUT/01-capture"
python scripts/run_yokohama_pad_state.py prepare \
  --capture "$OUT/01-capture" --case-set state-eval-v1 --output "$OUT/test-data"
python scripts/run_yokohama_pad_state.py evaluate \
  --dataset "$OUT/test-data" --model "$OUT/model" --output "$OUT/evaluation"
python scripts/check_yokohama_pad_state.py --bundle docs/examples/yokohama-pad-state
```

`--approve-sitl` explicitly opts into the CPU camera recorder; it creates and
cleans up only its owned Docker container. It does not fly PX4 or load models.
Environment variables above name task-owned paths, not repository defaults.
The model can be retrained directly from published `development-data` with the
`train` command into a new output directory; never overwrite the frozen model.

The runtime smoke used actual CPU Gazebo frames, performed real NumPy fitting,
saved/reloaded identical weights, and computed every new prediction through
`Model.predict`. The public checker verifies file hashes, measured target/time/
pose bindings and frozen source hashes, then recomputes all forecasts. Full
raw 640×360 RGBD is retained locally; only resized RGB is bundled, so the public
checker verifies source identities but does not rerender/recompute every raw
image resize. Training targets remain separate from inference arguments.

Result: 80.2% occupancy agreement, 0/1,356 false clear, 19.8% abstention,
139/155 (89.7%) supported histories against the historical >=90% experiment condition.
That condition no longer blocks adoption after the owner's explicit policy change. Other five conditions
passed. All 16 unsupported histories contain a localization-margin failure.
Per-observation thresholds and model weights are unchanged. Reported latency is local CPU computation, excluding
camera acquisition/transport/AP. Baseline latency fields reuse this common
pipeline, not separately measured comparator timings. Comparator state rules
lack the learned empirical-radius/occupancy-score guard, so this is diagnostic.

Full suite: 3,588 passed, 2 skipped. No new GPU/cloud spend; cumulative prior
estimate $18.532819 under $20 authorization. Owned capture container removed.
Future flight work must retain independent current-state Rules, operator scope,
executor receipts, stale-response handling, and the existing native profile
qualification. Do not register this single-action sampled forecast as native
ANWM hold/vla forecasts. Sea-leg model execution remains prohibited.

[Human report](../examples/yokohama-pad-state/REPORT-ja.md)

## Adoption policy override

`adoption-policy.json` is the current owner direction. Do not use a global
accuracy/support fraction, including the frozen protocol's 90%, as an adoption
veto. Decide by practical usefulness with model-enabled/disabled mission
behavior and total time/energy/cost. Do not require superiority to ideal Rules.
Current recorded inference establishes localization/forecast capability but
not improved delivery or waiting time. Proceed to optional city pad-wait
advisory integration; its live deployment is not yet complete.

Keep the frozen training/evaluation source and metrics intact. Their historical
`status=failed` remains reproducible and is not the current adoption decision.
Likewise `flight_admitted=false` declares the absence of dispatch authority,
not a percentage-based decision to reject all use. Missing evidence, stale
history and changed-camera checks remain per-request validity constraints.
No new GPU run or native model registration is implied by this policy update.
