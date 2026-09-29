# Native ANWM post-training did not improve returning-lead anticipation

The user increased the cumulative GPU ceiling from $20 to **$23**. This experiment actually ran the pinned ANWM on an L4, performed **4,096 supervised updates**, reloaded the saved adapter, and recorded **59 native forecasts** (three development probes and 28 paired before/after conditions). It did not use the separate CPU state predictor, run VLA, or fly an aircraft.

Known-target occupancy agreement changed **13/26 to 12/26**; unknown outputs increased **13 to 14**. Both stages produced zero false-clear or false-occupied classifications, but that is not safety evidence: many images were unclassifiable. The condition where a currently clear pad becomes occupied four seconds later remained unknown. The current-image persistence comparator matched 23/26, with one false clear, one false occupied and one unknown. There is no fixed accuracy gate or requirement to beat ideal Rules; **no useful wait-decision improvement was demonstrated**, so the new weights are not adopted.

Some stationary forecasts preserve the lead's shape. Moving cases exhibit invented colored texture; even one numerically correct clear classification comes from a visibly corrupted image. Mean RGB error worsened from 8.24 to 14.81. [Visual review](qualitative-review.json), [all comparisons](index.html), [Japanese report](REPORT-ja.md).

Inputs are 16 past frames at 4 Hz, cropped to a frozen region. Native ANWM forecasts hold-only endpoints at one and four simulator seconds. New timing sequences share the camera, appearance and corridor with training; 10/28 histories and 10/28 target appearances repeat training pixels. This is not unseen-scene generalization. Two ambiguous ground-truth boundary conditions are retained but excluded from the known-target denominator. The reader itself also has limitations on actual transition images.

The frozen intervention changes crop, horizon, conditioning, trainable scope and loss together; it does not identify which factor causes the remaining failures. A held fixed camera permits the current crop to condition the zero-motion view. The final two transformer blocks and existing output heads were trained on 208 earlier training pairs; other base weights and VAE stayed fixed. Evaluation future images remained on the CPU host. No threshold tuning, checkpoint selection or repeated sampling used the new results.

Warm after-inference latency had a **4.52 wall-second median**, excluding startup and transport. At a hypothetical 1x simulation-to-real-time rate, all 28 requested endpoints already pass before inference completes. Actual flight-clock freshness, interval occupancy, moving-action alternatives and mission benefit remain unverified. Existing AP-only sea operation and registered flight weights are unchanged.

## E2E / Runtime Verification

The actual runtime boundary was CPU Gazebo recording (736 frames) → past-only payload → native L4 ANWM inference and weight updates → checkpoint reload → host-side withheld-target evaluation. All 149 downloaded files passed their remote hashes. The maintained source differs from exact executed Python only in documented formatting and closure capture. The independent CPU checker reopens all public hashes, 59 receipt bindings and 56 paired forecasts without GPU.

```sh
python scripts/capture_yokohama_pad_motion.py --approve-sitl \
  --source-rig "$RIG" --case-set native-focus-v1 --output-dir "$CAPTURE"
python train_yokohama_pad_focus.py --root "$STAGING" --execute-training
python scripts/check_yokohama_pad_focus.py --bundle docs/examples/yokohama-pad-native-focus
```

Local test suite: **3,623 passed**. The initial local collection attempt lacked the repository package paths; the configured rerun passed. That setup failure is retained privately and is not a model trial. Both exact GPU execution and the portable CPU evidence checker are separate from any flight or physical-delivery claim.

Incremental conservative estimate **$1.219**, cumulative **$19.752/$23**; invoice unconfirmed. The owned GPU VM, boot disk and capture container were removed. [Cost and cleanup](cost.json). Original negative results are retained. The remaining work is native waiting/forward proposals, reliable dynamic forecasting, useful latency, and only then a joint PX4 mission validation.
