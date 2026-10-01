# One-pair ANWM conditioning diagnostic

This opt-in experiment diagnoses a failed moving training example. It is not
delivery adoption, a held-out evaluation, or evidence of VLA participation.

`scripts/diagnose_yokohama_pad_latents.py --root RUN` validates the frozen
protocol and inputs without importing Torch or requesting a GPU. Add
`--execute-training` only on the separately authorized, capped GPU worker.
Cloud provisioning and credentials are outside this repository entrypoint.

The input is exactly 16 RGB frames ending at the decision cutoff, plus one
explicit training target three seconds later. Training uses 1,024 updates,
the existing trainable parameter scope, and no extra moving-region loss.
There is no model capacity or impossible-to-predict conclusion from failure.

At updates 0, 256 and 1,024, and generation seeds 42 and 43:

1. Run the pinned upstream ANWM wrapper with observation-only conditioning.
2. Capture the wrapper's sampler inputs and CUDA RNG state. Replay them to
   require pixel-tensor parity with the unmodified native path (max 1e-5).
3. Use the identical sampler noise and RNG state but replace the conditioning
   latents with the cache used in training. Keep action and horizon unchanged.

This yields exactly 18 forecast images. A cached/native difference concerns
this pair and this conditioning change; it is not generalization evidence.
Teacher-forced images deliberately include a noised future target and must
never be counted among forecasts. VAE round trips and target/background error
measurements are separate diagnostic outputs, not motion proposals.

Preserve the initial and final selected parameter tensors in float32, the
training cache, protocol/input/source hashes, loss trajectory, and all images.
The host evaluator requires a completed run, every planned unique forecast,
parity, and both weight receipts. Missing records cannot qualify a fit.
The image readout is a synthetic orange-body detector, not general perception.

The three-second horizon is diagnostic only. Deployment requires a forecast
horizon beyond measured inference, communication and execution delay, as well
as fresh Rules checks. No flight, payload release or model adoption is invoked
by either diagnostic script.

CPU verification:

```sh
PYTHONPATH=. python -m pytest -q tests/test_yokohama_pad_latents.py tests/test_yokohama_pad_wam_study.py
python scripts/diagnose_yokohama_pad_latents.py --root RUN
python scripts/evaluate_yokohama_pad_latents.py --root RUN --results RESULTS --output OUTPUT
```

## Retained-evidence denoising audit

`scripts/analyze_yokohama_pad_denoising.py --root RUN --results RESULTS
--output OUTPUT [--image IMAGE] [--weights]` performs no inference, training,
cloud operation, or accelerator call. `--weights` requires CPU Torch and loads
only the retained selected parameters with `weights_only=True`, CPU mapping,
and receipt checks. It checks that all expected updates and teacher records
exist, and the native evidence is complete before reporting diagnostics.

For the pinned 1,000-step linear EPSILON schedule, the inferred unclipped
latent x0 error is epsilon MSE multiplied by `(1-alpha_bar)/alpha_bar`.
Do not compare raw epsilon MSE across noise levels as if it measured image
quality. The converted quantity is not RGB error or a new optimization loss.
All teacher inputs contain the true future. Three one-step teacher checks
cannot locate the failure inside an unsaved multi-step generation trajectory.

The audit reports foreground and background separately, records raw synthetic
colour readouts without treating texture as aircraft, and keeps the prior fit
criterion unchanged. Parameter changes show that updates were applied, not
that optimization or the limited trainable scope was sufficient. No learned
weight adoption follows from this analysis.

For the next inference diagnosis, hold the retained weights, pair and seeds
fixed; capture the denoised image estimate along the ordinary sampler and
compare 50 versus 250 sampling steps before changing the loss or retraining.
Keep both seeds and unreadable results. More sampling steps may be diagnostic
but worsen flight latency; no new forecast or improved flight is implied by
this proposed check.

## Frozen-weight sampling trace

`trace_yokohama_pad_sampling.py --root RUN` validates history-only input on CPU.
`--execute-inference` is separately opt-in on a capped worker. It restores the
retained final parameters, freezes every parameter, and compares 50/250 steps
with seeds 42/43 and a fixed three-second forecast horizon. No optimizer,
training target, or cached future latent is sent to that worker.

Each of four ordinary native forecasts has a traced replay. The replay uses
the unmodified upstream progressive sampler and records all x_t, predicted
x0, and next-sample tensors. It must match the ordinary decoded output within
1e-5. Conditioning and initial-noise hashes must match across step counts for
each seed; weights must remain identical before and after inference.

All 600 sampling states are retained. Only predicted x0 at seven predeclared
noise anchors per call is decoded for the contact sheet; a sampled image does
not establish what happened between those anchors. Timings from uninstrumented
native calls exclude tracing and extra diagnostic decoding. Step counts alter
the respaced stochastic sampler, not just its wall time.

`evaluate_yokohama_pad_sampling.py` runs only on the host with the separate
target/target-latent files. It rejects missing/duplicate/unfinished runs,
parity or weight changes, missing trace frames, altered hashes, and broken
trace continuity. Report all seeds, shape limitations and latency, and do not
turn a training-pair readout improvement into deployment or generalization.
