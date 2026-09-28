# Conditional dynamic pad WAM post-training

This is an offline endpoint-capability experiment. It does not execute a waiting
policy, change VLA weights, authorize movement, or update a deployed provider.
Keep the AP-only sea policy and the existing live provider validation unchanged.
WAM predicts; MissionOS judges candidates; independent Rules constrain dispatch.

## Frozen data and training boundary

`capture_yokohama_pad_motion.py --case-set learning-v1` records six fresh CPU
Gazebo sequences with 1,472 synchronized RGBD frames and measured poses. Three
sequences train and three supply four evaluation histories. The same fixed
camera, actor, path and environment are reused, with different timings. Repeated
endpoint appearances overlap training: this is not unseen-scene generalization.
Two fresh RGB histories and all four future target pixel arrays exactly repeat
training appearances; the evaluator reports the two distinct-history cases too.
The inherited `phase: flight` world field is configuration, not executed flight.
Lead movement is scripted; the parcel is preplaced; no unloading physics occurs.

Preparation hashes every raw asset before extracting 16-frame, 4 Hz histories.
Fifteen training cutoffs each produce targets at frame offsets 1, 16 and 64
(0.25, 4 and 16 seconds), giving 45 supervised pairs. Training RGB targets remain
640x360 and receive the upstream crop exactly once. Previously inspected four
native cases are regression cases, not a new test set. All eight evaluation
future images stay on the CPU host and are absent from the uploaded dataset.
Actor pose/schedule is used only for measured truth, never model conditioning.

The grayscale reader is unchanged from prior development, including its mask,
templates, thresholds and bytes. It is a restricted image diagnostic, not a
general detector. Good background reconstruction does not establish occupancy.

## Conditional native execution

Default `train_yokohama_pad_wam.py --root "$STAGING"` verifies the uploaded data
and protocol without importing Torch or requesting a GPU. `--execute-training`
is explicit paid-compute opt-in, with no aircraft API. Pinned upstream, base,
initial motion-v4 adapter, VAE, source hashes and protocol accompany results.

1. On one development history, cross 50/250 sampling steps with frame offset
   1/64. This diagnoses both changed factors without test-based selection.
2. Three preselected training-only 50-step/64-frame probes decide whether to
   train. Any wrong/unknown state or RGB MAE above 12 triggers training.
3. Record the fixed eight before forecasts. If development already passed,
   stop without weight updates; the test does not determine this branch.
4. Otherwise perform exactly 2,048 AdamW steps, learning rate 5e-5, seed 42,
   gradient clip 1. Train only the existing final transformer block and output/
   attention heads (47,851,808 parameters). Base, VAE and time embedding stay
   frozen. Use actual frame offset /128 for time conditioning.
5. Loss is upstream diffusion loss plus 4 times latent epsilon MSE inside the
   old development-only motion mask. No mask or threshold fits generated tests.
6. Save the adapter, restore original parameters, reload the serialized adapter,
   verify nonzero parameter change and unchanged frozen base, then record eight
   after forecasts. No checkpoint selection, resampling, or second GPU attempt.

Predictions are hold-only at a requested 16-second endpoint. The frozen numeric
candidate bounds require at least 3/4 fresh matches, no false clear, RGB MAE at
most 12, and preservation of earlier successes. Even a numeric pass does not
admit flight: interval occupancy, action-dependent alternatives and timing at
dispatch remain unverified. Unknown or unqualified images cannot authorize go.
VLA still proposes immutable short movements; this experiment does not expand
its authority or define a new movement.

## Reopening and publication

`evaluate_yokohama_pad_learning.py` reopens native result, input, weight, time,
forecast and withheld-target hashes. It reports fresh and regression cohorts
separately and retains current-image persistence as a simple comparator. Ideal
Rules superiority is not a requirement. Warm latency excludes load and transport
and does not prove a live AP decision deadline.

`check_yokohama_pad_learning.py` additionally verifies the public manifest,
executed sources, capture pose/timestamp bindings, training target hashes and
actual target crops, and recomputes saved metrics on CPU. CI runs this command.
The public bundle includes the exact prepared training and evaluation inputs,
native forecast pixels, loss/update receipts, future targets, measured capture
metadata, original camera videos, protocol and reviewed cost receipt. Full raw
capture/depth recordings and model weights remain private. The new adapter is
hash-bound in receipts but is not registered or automatically deployed.

```sh
python scripts/capture_yokohama_pad_motion.py --approve-sitl \
  --source-rig "$SOURCE_RIG" --case-set learning-v1 --output-dir "$CAPTURE"
python scripts/prepare_yokohama_pad_learning.py --capture "$CAPTURE" \
  --previous docs/examples/yokohama-pad-temporal --output "$PREPARED"
python train_yokohama_pad_wam.py --root . --execute-training
python scripts/check_yokohama_pad_learning.py \
  --bundle docs/examples/yokohama-pad-learning
```

See the [human report](../examples/yokohama-pad-learning/REPORT-ja.md) for the
recorded results and limitations, and the [previous negative diagnostic](yokohama-pad-temporal.md).

Observed result: all three development probes required training; 23 native calls
and the serialized 2,048-step update completed. Fresh matches 1/4 → 2/4, RGB MAE
33.71 → 6.32, but false clears 1 → 2. Every after forecast reads clear; the small
lead disappears. Regression also scores 2/4 with two false clears. Qualification
and flight admission remain false. Cumulative estimate $18.532819/$20, invoice
unconfirmed; owned VM/disks/capture container removed and CUDA allocation zero.
