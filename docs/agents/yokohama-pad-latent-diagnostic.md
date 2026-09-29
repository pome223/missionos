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
