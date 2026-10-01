# Native ANWM regional short-horizon follow-up

This is an opt-in, offline capability experiment. It uses the actual pinned ANWM
base and a serialized native adapter, not the separate CPU state predictor. It
does not run VLA, invoke a mission judge, dispatch, or execute a cargo mission.
Keep the AP-only sea policy and existing flight-provider admission unchanged.

## Frozen observation and outcome contract

`capture_yokohama_pad_motion.py --case-set native-focus-v1 --approve-sitl`
records three new fixed-camera timing sequences: departure, interrupted departure,
and return. Fourteen cutoffs, each with one- and four-second endpoints, yield 28
correlated conditions. The lead is a scripted Gazebo actor, not another PX4.
The camera is not an airborne aircraft. No battery or physical delivery occurs.

`prepare_yokohama_pad_focus.py` reopens recorded RGB hashes and crops raw pixels
at `(256, 48, 584, 328)` into 224×224. This region was chosen from the earlier
training scene and is fixed before new native inference. The 16-frame context
contains RGB and timestamps only, at 4 Hz. Requested offsets 4 and 16 correspond
to one and four simulator seconds. Native requests receive no future target,
lead pose, schedule, or occupancy label. Case names are metadata, not conditioning.

The same view, lead appearance, and path recur across timing sequences. Report
exact history overlap; do not claim unseen-world generalization. Ground truth
comes from measured lead position: distance below 5.2 m is occupied, above 6.8 m
is clear, and the boundary is unknown. All cases are retained; unknown ground
truth is excluded only from the explicitly labelled known-target denominator.

The grayscale reader is trained on training RGB only, with its mask, references,
24-RMSE maximum and 1-unit margin frozen before native outputs. Its generated-
image reliability is limited. Inspect actual forecast images before interpreting
numeric agreement as lead preservation. Real targets also have reader failures.

## Native learning boundary

`train_yokohama_pad_focus.py --root "$STAGING"` validates without importing
Torch or creating cloud resources. Actual computation requires the explicit
`--execute-training` flag and a separately provisioned, time-capped GPU.

- Load the pinned ANWM base, motion-v4 adapter, then the exact prior pad adapter
  `5b78a8c97e6ed42ece769bf1d5bdeb35e8352ae8b64f7f4c27b0edc6b7518542`.
- The experimental hold-only input uses the last observed crop as the exact
  zero-motion camera-view conditioning image. This differs from the live
  all-frame 3D projection profile. Never register it as that qualified provider.
- Three fixed training probes determine whether learning is needed. Evaluation
  future images stay on the CPU host and do not determine this branch.
- Record all before forecasts with 50 diffusion steps and seed 42.
- If needed, perform exactly 4,096 AdamW updates, learning rate 5e-5, clipping 1,
  with 208 pairs from earlier training sequences only. Train blocks 26 and 27
  and the existing output/attention heads. The remaining base and VAE stay fixed.
- Loss adds a per-target moving-region epsilon penalty (weight 8) and an
  SNR-weighted regional latent reconstruction penalty (weight 0.1). A training-
  only background defines the region. No evaluation target is used for masks.
- Serialize, restore original trainable parameters, reload, confirm nonzero
  change and unchanged frozen base, then record the after forecasts. No repeat
  sampling, checkpoint selection, or tuning against the new results.

The crop, horizon, conditioning, trainable scope and losses all change. This is
a combined intervention, not attribution to one factor. The exact executed
sources are preserved separately from maintained formatting and closure binding.

## Adoption and time limits

No 90% threshold or ideal-Rules win is required. Report known-state matches,
false clear, unknown, useful future-occupied anticipation, and unnecessary wait
against the same-image persistence comparator. Better image error alone is not
mission value or flight adoption. Even an improved point forecast lacks interval
occupancy, alternate-action prediction, and actual dispatch-clock freshness.

Measure warm wall-clock latency separately from simulation horizon, model load,
transfer and AP holding. A one- or four-second forecast that arrives after that
horizon is not a live navigation signal. The shared controller must retain
independent current Rules and reject stale requests before any future integration.

## Evidence and verification

The public export includes cropped past inputs, training targets, host-only
evaluation targets after execution, native before/after PNGs and receipts,
training losses/update receipts, exact executed Python, actual camera videos,
measured capture metadata, hashes, budget authorization and cleanup. It excludes
weights, credentials, private cloud logs and full raw depth captures. Publishing
evaluation targets after the experiment does not make them training inputs.

```sh
python scripts/check_yokohama_pad_focus.py \
  --bundle docs/examples/yokohama-pad-native-focus
```

This GPU-free checker verifies all public assets, source/protocol/data bindings,
each native forecast's history/adapter/clock/image, and recomputes image-derived
endpoint results. It is not a native model rerun or a flight smoke test. Preserve
the actual GPU/camera execution receipt as the affected runtime boundary.

The user increased the cumulative compute ceiling from $20 to $23. A separately
owned one-hour G2 VM with DELETE termination and auto-delete boot disk is used;
all owned resources must be verified absent before closing the cost estimate.

Recorded outcome: 59 native forecasts, 4,096 updates, 13/26 → 12/26 known-target
matches and no useful reentry anticipation. The final `summary.json` records
successful experiment execution, not capability success. `training-receipt.json`
is the intermediate snapshot before the after forecasts and retains the initial
conservative `status: failed`; it is not the final run status. No receipt was
rewritten. In-process cleanup still measured 327,168 allocated CUDA bytes; the
subsequent process-exit GPU query was empty and owned VM/disks were removed.
Do not claim zero in-process CUDA allocation. Training targets and training-only
reader labels were on the GPU host; evaluation targets and actor trajectories
were withheld from native model inputs.

## Post-hoc failure diagnosis

`diagnosis.json` explains the negative result; it is not a new experiment, a
native rerun, attribution to one changed factor, or an adoption change.

```sh
python scripts/diagnose_yokohama_pad_focus.py \
  --bundle docs/examples/yokohama-pad-native-focus --write
```

- Inputs are the published bundle plus `docs/examples/yokohama-pad-queue/protocol.json`
  for pad geometry. The script first rejects unless that geometry reproduces all
  28 `truth.json` future and current labels at the bound capture indices.
- Recorded actor poses are used only to explain ground truth (distance at
  -3.75, 0, +1 and +4 seconds, and the clear-to-occupied window). They were never
  native model inputs; `actor_pose_model_input` stays false.
- Headroom counts known targets whose future state differs from the current
  pose state. Groups split paired conditions by exact training-history overlap
  and horizon; they remain correlated, not independent missions.
- Object scale compares the last history crop with the training background at
  the training ROI threshold (any channel > 20), with 8-connected components and
  the VAE stride of 8. Components are changed regions, not a validated detector.
- `check_yokohama_pad_focus.py` recomputes the diagnosis and rejects any drift.

Before and after share crop, horizon and conditioning; the before/after delta is
the training change (data, trainable scope, losses together). The combined crop/
horizon/conditioning change is relative to the earlier native experiment. Keep
these two comparisons separate.

Recorded facts: 3/26 known targets change state, one clear-now/occupied-later
condition, zero wrong-state native outputs but visibly corrupted unreadable
images, mean four-second RGB error 9.64→21.78, median 9.35→4.11 on the five
stationary training-identical histories versus 9.76→29.04 on unseen ones, 0/9
readable unseen four-second forecasts before training, 59 distinct history
pixel contents among 104 training history files (56 with motion), zero
post-training forecasts on training scenes, and a median lead component of
about 40×10 pixels. The clear-to-occupied window is 1.75 seconds for the
four-second horizon and absent for one second.

Report as untested hypotheses, never as causes: persistence bias from the
latest-frame conditioning image (auxiliary input, not an output constraint),
overfitting, and texture from the 8× moving-region weight. The small
anticipation denominator limits measuring decision value; it does not explain
why the model's own endpoint agreement stayed low. Do not describe CPU advice
as solving returning-lead anticipation (post-training alone waited in 0/8).

Only the reports, `index.html`, `diagnosis.json` and the manifest were added or
edited; receipts, forecasts and evaluation files are unchanged.

## Separate maintained capture entrypoint

The original run used the extended `capture_yokohama_pad_motion.py`; its exact
source is retained at `capture/executed_capture.py` in the evidence bundle.
The maintained equivalent is now `scripts/capture_yokohama_pad_focus.py`, with
the Docker worker path derived from that script's basename. Camera worker and
case function ASTs are unchanged. Use this new name with the same arguments
for a future capture; the original command in reports describes the historical
execution. The older motion entrypoint is restored byte-for-byte so previously
frozen CPU experiments continue to verify. Neither historical protocols nor
predictions were rewritten, and no new capture or GPU result is claimed.
