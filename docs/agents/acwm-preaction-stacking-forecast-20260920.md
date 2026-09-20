# Predicting One Complete Block Placement Before Acting with ACWM

**Technical report · 20 September 2026 · simulator and offline replay evidence**

## Result

We fine-tuned the public ACWM VideoDiT so that a forecast begins **before the
robot approaches the next block** and spans one complete 14.2-second placement:
approach, staged grasp, lowering, release, withdrawal, and settling. The model
generates 37 sampled video frames from the current image, current simulator
state, and a summarized 7D VLA action plan.

On 16 unused placement cases from six simulator seeds, covering placements one through ten, fine-tuning improved
mean block-mask IoU from **0.439 to 0.669** and reduced block-centroid error from
**19.1 to 9.4 pixels**. On the four held-out collapse cases, mean IoU improved
from **0.340 to 0.636**.

A small visual readout, fitted only on generated training futures, translated
the forecast into a place-or-bank decision. With the fixed primary noise seed,
it classified **13/16** unused futures correctly: all four collapses were
detected, while three of twelve stable placements were conservatively rejected.
A second generation of the same 16 physical cases with another noise seed classified 12/16, again detecting all four collapses but
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

Ten of the 16 validation placements come from the complete seed-59000
trajectory; the remaining six come from five other seeds. They are placement
cases rather than 16 independent games. The noise-196 result regenerates those
same physical cases and is not an additional physical cohort.

Every training case received 20 updates. The final fixed-budget checkpoint was
evaluated without validation-based checkpoint selection or threshold tuning.
The starting frozen checkpoint was
`stacking-delayed-forecast/results/final-model.pt`, SHA-256
`4d03fced174d43756cf3fdc01f3b03d92550ac633b627b9cae8f9cfb77fc1f15`.
The final model SHA-256 was
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

Metrics use 240 x 240 RGB frames. The block mask selects pixels where the
green/blue maximum exceeds red by 0.06 with intensity above 0.14, or red exceeds
green/blue by 0.12, is at least 1.5 times larger, and exceeds 0.2. IoU is
computed per future frame, excluding the known initial frame, then averaged per
case and across cases. A zero-union frame receives IoU 1.0. Centroid error is
computed only when both masks are nonempty, then averaged across future frames
and cases. “Final-three-frame IoU” applies the same calculation to the last
three generated frames. The per-case inputs to every reported aggregate are
published with this report.

The full-trajectory IoU improvement from 0.439 to 0.669 is substantially larger
than the final-three-frame change from 0.494 to 0.518 under the primary noise
seed. The strongest measured improvement is therefore action-trajectory
reproduction; final-state accuracy improves only modestly in that condition.

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

At count eight, the primary condition distinguishes both stable cases and both
collapse cases correctly (4/4). At count nine, it rejects the only stable case
(0/1). At count ten, it marks all four cases dangerous, detecting both
collapses and rejecting both stable placements. This shows state-dependent
selection at count eight while exposing weak late-tower discrimination.

This small validation set supports a narrower result: generated futures contain
enough collapse-related motion for the fixed readout to detect all four held-out
collapses. It also shows the current limitation clearly: visual artifacts are
a candidate explanation for conservative errors, but this experiment does
not isolate their causal contribution.

## Same forty-game cohort as the ExtraTrees study

After freezing the ACWM and readout, the batch run processed 355 saved decision states
from the same 40 games used for the final ExtraTrees comparison: seeds
67000–67039, the same pre-action simulator states, the same saved SmolVLA motor
actions, and the same 284-step bank branches and score outcomes. The policy
reached 327 of those states before stopping or collapse; all 327 reached
decision rows are published. No threshold,
weights, or generation settings were changed after seeing this cohort.

| Method | Forty-game score |
| --- | ---: |
| Width-specific fixed stopping | **280** |
| Final long-horizon ExtraTrees | **279** |
| **ACWM future video + fixed visual readout** | **243** |
| Fixed stop at six | 240 |
| Earlier short-horizon ExtraTrees | 225 |
| Current-state rule | 198 |
| VLA continuation without predictive stopping | 30 |

ACWM therefore exceeds VLA continuation, current-state stopping, the earlier
ExtraTrees predictor, and fixed-six stopping in observed total score. It remains
36 points below the final ExtraTrees predictor and 37 below the width-specific
fixed rule.

Across games, ACWM beats the final ExtraTrees policy seven times, ties fifteen,
and loses eighteen. Its paired mean difference is -0.90 points per game, with a
width-stratified bootstrap interval of [-1.725, -0.05]; the sign-randomization
test is `p=0.0769` and Holm-adjusted `p=0.179`. Against the width rule, the mean
difference is -0.925, interval [-1.6, -0.3], unadjusted `p=0.0281`, and
Holm-adjusted `p=0.112`. These results do not establish ACWM superiority over
either strong baseline.

The behavioral decomposition explains the score. ACWM banks in 32 games and
completes ten blocks in three. On reached decision states, it records 9 true
positives, 23 false positives, 290 true negatives, and 5 false negatives. The
five missed collapses produce five zero-score games. Two additional games score
zero even after a correct danger decision because the corresponding measured
bank branch also collapses. The remaining conservative errors surrender points
without collapse.

This is the same game cohort and scoring protocol, but the predictor horizons
are not identical. ACWM generates one 284-step placement. The final ExtraTrees
model predicts that placement plus a subsequent 284-step hold, giving it a
568-step risk horizon. Both methods also receive privileged simulator state and
physical properties; ACWM additionally conditions on the exact saved future VLA
action tape in this offline evaluation. The comparison measures the two frozen
systems as implemented and should not be read as an architecture-only contest.

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

The replay policy banks eight points. The stored bank branch then holds the
eight-block tower for 284 control steps (14.2 seconds), observes no collapse or
technical failure, records a maximum drop of 11.25 mm, and returns score eight.
The ninth placement was actually safe,
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
- [Per-case metrics before fine-tuning](../assets/acwm-preaction-stacking-20260920/forecast-before-seed195.json)
- [Per-case metrics after fine-tuning, noise 195](../assets/acwm-preaction-stacking-20260920/forecast-after-seed195.json)
- [Per-case metrics after fine-tuning, noise 196](../assets/acwm-preaction-stacking-20260920/forecast-after-seed196.json)
- [Held-out sequential replay](../assets/acwm-preaction-stacking-20260920/heldout-sequential-replay-59000.json)
- [Same-cohort forty-game comparison](../assets/acwm-preaction-stacking-20260920/v7-40-game-acwm-comparison.json)
- [Frozen training record](../assets/acwm-preaction-stacking-20260920/frozen.json)
- [Verification record](../assets/acwm-preaction-stacking-20260920/verification.json)
- [First-block forecast video](../assets/acwm-preaction-stacking-20260920/validation-59000-01.mp4)
- [Stable seventh-block forecast video](../assets/acwm-preaction-stacking-20260920/validation-59000-07.mp4)
- [Eighth-block collapse forecast video](../assets/acwm-preaction-stacking-20260920/validation-59003-08.mp4)
- [Safe tenth-block forecast and false-positive example](../assets/acwm-preaction-stacking-20260920/validation-59006-10.mp4)

All training and inference ran locally on Apple MPS. No cloud GPU was used.
