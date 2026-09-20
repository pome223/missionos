# Predicting One Complete Block Placement Before Acting with ACWM

**Technical report · 20 September 2026 · simulator and offline replay evidence**

## Result

We fine-tuned the public ACWM VideoDiT so that a forecast begins **before the
robot approaches the next block** and spans one complete 14.2-second placement:
approach, staged grasp, lowering, release, withdrawal, and settling. The model
generates 37 sampled video frames from the current image, current simulator
state, and a summarized 7D VLA action plan.

On 16 unused cases covering placements one through ten, fine-tuning improved
mean block-mask IoU from **0.439 to 0.669** and reduced block-centroid error from
**19.1 to 9.4 pixels**. On the four held-out collapse cases, mean IoU improved
from **0.340 to 0.636**.

A small visual readout, fitted only on generated training futures, translated
the forecast into a place-or-bank decision. With the fixed primary noise seed,
it classified **13/16** unused futures correctly: all four collapses were
detected, while three of twelve stable placements were conservatively rejected.
A second noise seed classified 12/16, again detecting all four collapses but
rejecting four stable placements.

This is a real neural future-generation experiment. It is separate from the
task-specific ExtraTrees predictor reported in the earlier MissionOS stacking
study.

## Prediction boundary

The previous ACWM experiments forecast a 1.8-second window late in placement.
That could depict motion after the block had already been introduced into the
scene, but it could not answer the intended pre-action question. This experiment
moves the gate to the saved state before approach and predicts the full macro.

| Property | Value |
| --- | --- |
| Decision point | Before approach to the next block |
| Control horizon | 284 steps at 20 Hz |
| Physical duration | 14.2 seconds |
| Generated sequence | 37 RGB frames; 10 Wan VAE latent frames |
| Image input | The single current frame |
| State input | Current objects, velocities, physical properties, robot state, and count |
| Action input | Exact saved 7D VLA replay actions, averaged over the 36 frame intervals |
| Model updated | ACWM VideoDiT |
| Training | 720 updates at `2e-5`; no skipped updates |

The staged-grasp state transition at step 60 is part of the placement macro.
The replay renderer reproduces it explicitly rather than treating the motor
action tape as a complete description of the simulator transition. Across all
52 rendered cases, the maximum final-state difference from the source branch
was 2.11 micrometres, below the fixed 10-micrometre replay tolerance.

## Data and freeze discipline

The selection was fixed before fine-tuning:

- 36 training cases;
- 16 validation cases from disjoint seeds;
- stable examples for placements one through six;
- three stable and three collapse examples at each placement count from seven
  through ten in training;
- at least one stable validation example at every count;
- both stable and collapse validation outcomes at counts eight and ten.

Every training case received 20 updates. The final fixed-budget checkpoint was
evaluated without validation-based checkpoint selection or threshold tuning.
The model SHA-256 was
`3a0f20e4e100fc57b8cd03cec422397f770317e916c7397c35328d85c9a97b0b`.
The 419 MB checkpoint is intentionally excluded from this public repository.

The training loss keeps the initial latent clean, applies a shared diffusion
timestep to future latents, excludes the initial frame from the future loss,
and weights the dilated block region 16 times more heavily. Target masks are
used only for the training loss; they are absent from the prediction and
validation interfaces.

## Future-generation results

| Condition | Mean block-mask IoU | Final-three-frame IoU | Centroid error |
| --- | ---: | ---: | ---: |
| Before fine-tuning, noise 195 | 0.439 | 0.494 | 19.1 px |
| After fine-tuning, noise 195 | **0.669** | 0.518 | **9.4 px** |
| After fine-tuning, noise 196 | 0.653 | **0.579** | 11.3 px |

The improvement is strongest over the full placement trajectory. Generated
videos still contain temporal flicker and occasional transient geometry errors,
especially in high towers. The second noise seed confirms that this variation
is material rather than cosmetic.

## Reading the generated future

The visual readout uses block-mask area, width, height, center motion, and
frame-to-frame overlap. It is a deterministic logistic model fitted on the 36
fixed ACWM training forecasts at noise seed 195. Validation frames and labels
do not enter fitting or threshold selection.

| Evaluation | Correct | TP | FP | TN | FN |
| --- | ---: | ---: | ---: | ---: | ---: |
| Generated training futures, noise 195 | 34/36 | 11 | 1 | 23 | 1 |
| Unused generated futures, noise 195 | **13/16** | **4** | 3 | 9 | **0** |
| Unused generated futures, noise 196 | 12/16 | **4** | 4 | 8 | **0** |

This small validation set supports a narrower result: generated futures contain
enough collapse-related motion for the fixed readout to detect all four held-out
collapses. It also shows the current limitation clearly: visual artifacts make
the system stop too often on safe late placements.

## One-block-at-a-time replay

Unused seed 59000 contains a complete sequence of decision states. The fixed
ACWM and readout were invoked independently before each next placement:

| Next count | Predicted collapse | Decision | Measured branch |
| ---: | ---: | --- | --- |
| 1 | 0.040 | Place | Stable |
| 2 | 0.135 | Place | Stable |
| 3 | 0.083 | Place | Stable |
| 4 | 0.049 | Place | Stable |
| 5 | 0.048 | Place | Stable |
| 6 | 0.085 | Place | Stable |
| 7 | 0.097 | Place | Stable |
| 8 | 0.111 | Place | Stable |
| 9 | 1.000 | Bank | Stable; conservative false positive |

The replay policy banks eight points. The ninth placement was actually safe,
while the source branch later collapsed on placement ten. The result therefore
demonstrates the intended repeated forecast-decision structure, with a one-block
conservative error.

This is a counterfactual replay over exact saved VLA action plans, not a newly
executed online simulator game. Forecast, readout judgment, and measured branch
outcome remain separate records.

## Remaining online boundary

The full saved 14.2-second VLA motor tape is available in this offline study,
but the current closed-loop SmolVLA does not expose that complete future tape at
the live pre-action gate. Online deployment therefore requires replacing the
saved tape with information available at decision time, such as a placement
goal and registered macro representation, and then retraining or adapting the
action conditioning.

The measured result is consequently:

> ACWM can generate one complete next-block placement from the pre-action state,
> and the generated future can drive a repeated place-or-bank replay decision.

It is not yet evidence of a live MissionOS game driven by this ACWM checkpoint.

## Artifacts

- [Machine-readable readout results](../assets/acwm-preaction-stacking-20260920/future-video-readout.json)
- [Held-out sequential replay](../assets/acwm-preaction-stacking-20260920/heldout-sequential-replay-59000.json)
- [Frozen training record](../assets/acwm-preaction-stacking-20260920/frozen.json)
- [Verification record](../assets/acwm-preaction-stacking-20260920/verification.json)
- [First-block forecast video](../assets/acwm-preaction-stacking-20260920/validation-59000-01.mp4)
- [Stable seventh-block forecast video](../assets/acwm-preaction-stacking-20260920/validation-59000-07.mp4)
- [Eighth-block collapse forecast video](../assets/acwm-preaction-stacking-20260920/validation-59003-08.mp4)
- [Safe tenth-block forecast and false-positive example](../assets/acwm-preaction-stacking-20260920/validation-59006-10.mp4)

All training and inference ran locally on Apple MPS. No cloud GPU was used.

