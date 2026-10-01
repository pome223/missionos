# Final-block adaptation: one further bounded attempt

The output-attention continuation passed pixel criteria on 3/8 images but
failed corridor visual qualification at all four sites. Preserve that result.
This experiment continues its saved adapter, SHA256
`0a0375fff9acced7c5b46c29236f4ff9c48063342772ef13441634fbb9b33458`.

Train only the previous final-stage tensors plus `blocks.27` (the last of 28
Transformer blocks), with 2,048 batch-one AdamW updates, lr 0.00005, zero weight
decay, gradient clipping 1, seed 42 and native 1,000-step diffusion training loss.
All other ANWM parameters and the VAE remain frozen. No native flight changes.
Compare eight predictions with the previous adapter to eight after this fixed
continuation using the same input, action, seed and 250 sampling steps.
This is not an ablation isolating a block: training duration also changes.

Use freshly rendered `block-v3` sites: the same 12 training positions, with new
evaluation positions extending inland along segment two at fractions 1.05,
1.10, 1.15 and 1.20. These positions are **past the authored delivery endpoint**,
in a more open part of the scene. This changes the evaluation envelope; success
here would not resolve the previous narrow-corridor failure or extend flight
authority beyond the approved route. Model inputs are still urban observations.

Both endpoints of all new evaluation pairs must stay at least 15 m from all
sites inspected in either earlier experiment. The CPU geometric preflight
observed 19.7023 m separation, >22 m horizontal distance from source building
footprints, and >15 m height above source terrain at all eight camera positions.
These are renderer-position checks, not executed-flight clearance evidence.
Four nearby points in one area do not establish independent-scene performance.

Render 16 stationary RGBD context frames and later hold/2.8 m-forward target
images. Withhold all eight evaluation images on the host. Inference targets
are generated from commanded deltas, never from later measured outcomes.
Time condition one means a static viewpoint transition, not one second.

The frozen minimum remains one site passing **both** actions: RGB MAE <=15,
<=10% pixels with any channel error >40, inference <=75 s, and no invented
route-changing structure/water in manual inspection. Minor texture or blur
alone need not fail manual inspection. No comparison against Rules is required.
Keep every result; do not choose intermediate checkpoints or relax criteria.

Bind the initial adapter, dataset and code to the protocol. Save/reset/reload,
check finite tensors and the full unchanged frozen-parameter hash, and audit
the changed parameters against their actual starting tensors. Raw weights and
private cloud configuration stay outside public evidence. A pass is limited
offline adaptation, not native VLA/WAM flight or delivery.

This is one further GPU attempt within the existing cumulative USD 15 cap.
Use the closed attention-study receipt and at most a one-hour auto-delete VM.
Retain failures and verify owned instance/disk deletion before reporting cost.
