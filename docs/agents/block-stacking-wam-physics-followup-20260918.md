# Block stacking follow-up: physical variation, frozen-model evaluation, and delayed collapse

Technical report — 18 September 2026

This continues the [initial report](block-stacking-wam-technical-report-20260918.md).
The original pilot and centered-game videos remain historical examples; they are
not footage of the two forty-game cohorts reported here. No new video is included.

## Findings

The frozen short-horizon WAM scored **292 points in v5**, versus 280 for a
validation-selected width-dependent stopping rule, 208 for the current-state
rule, and 130 for unconditional VLA continuation. The width-rule advantage was
not statistically established: paired mean difference +0.300, 95% interval
[-0.225, 0.625], Holm-adjusted p = 0.29670.

Extending the prediction target and retraining did **not** improve the original
game score in v6: the new WAM scored 273, the unchanged old WAM 281, and the
width rule 280 on the **same new forty starts**. Comparing 292 directly with 273
would mix a model change with a cohort change. The old model remains a useful
reference; the evidence does not justify replacing it with the new model for
higher original-game scores.

An important scoring limitation emerged: voluntary stopping included an extra
14.2-second bank/hold, but ten-block completion ended scoring at the placement
macro's endpoint. With the extra terminal hold also applied at ten, descriptive
v6 totals were new WAM 273, old WAM 261, width rule 280. These are a separately
specified diagnostic, not a replacement primary endpoint or proof of superiority.

## 1. What changed after the initial report

The early report's small neural state model and release-time gate are historical.
The models evaluated here are dedicated **ExtraTrees** collapse classifiers and
future-state/drop regressors, conditioned on the fixed policy. A preceding v4
study moved the decision gate **before approach**, trained on 48 actual-VLA
games, and selected settings on 16 validation games. Its twenty test starts
scored WAM 141, current rule 113, VLA 40, and a validation-selected width rule
140. That near tie motivated the independent v5 comparison; those twenty starts
are not pooled with either subsequent cohort.

The frozen SmolVLA checkpoint is unchanged. This is still staged placement:
common controllers handle approach and withdrawal, and grasp acquisition is
staged. Learned VLA actions drive lowering and release. It is not autonomous
continuous pick-and-place or a hardware demonstration.

## 2. Shared environment, decisions, and outcomes

- Maximum ten blocks; operational collapse yields zero. Collapse is a drop
  exceeding 30 mm from a scored block's accepted height and remains latched.
- Targets share the same XY, with initial XY variation within ±4 mm. There is
  no intentional accumulating lateral offset.
- Each cohort has twenty wide and twenty narrow games. Block width is 44–56 mm
  in the wide group and 24.64–31.36 mm in the narrow group; depth and height are
  40 mm. These are two distinct width groups, not only tiny perturbations.
- Each block has fixed within-game mass of 45–95 g and friction of 0.35–1.1.
  Center-of-mass offsets range over ±25% of half-width and ±10% of half-depth
  and half-height. Individual blocks differ; properties do not change mid-game.
- At 20 Hz, continuation comprises approach 60, grasp staging 20, VLA lowering
  60, VLA release 12, withdrawal 60, and settling 72 steps: 284 steps, 14.2 s.
  Banking uses another 284-step branch from the saved decision state.
- Within a game, methods share initial state, physical properties, and the actual
  VLA action prefix until stopping. Continue/bank counterfactuals use saved states.
  A stop decision is not automatically a successful bank: the bank branch must
  actually remain stable.

WAM inputs include exact object poses, velocities, dimensions, mass, friction,
center of mass, accepted reference positions, robot state, next target, and the
fixed control procedure. The future closed-loop VLA action sequence is unknown
at prediction time and is **not** supplied retrospectively. This is a
policy-conditioned predictor, not demonstrated generalization to arbitrary
candidate motor sequences or inference of hidden material properties from RGB.

Separate classification and regression heads predict collapse risk and future
poses/drop. Stopping uses the classifier, not the pose regression output. Risk
outputs are not established as calibrated probabilities. Stop requires continue
risk at or above the selected threshold and lower predicted bank risk.

Comparators are unconditional continuation, the current-state rule (tilt above
5 degrees or horizontal drift above 3 mm), and the width rule (wide: bank eight;
narrow: bank six). The width rule was selected on the earlier validation set,
not on either new test cohort. Fixed six, seven, eight, and nine are references.

## 3. v5: frozen short-horizon model, forty unused starts

Seeds 63000–63039 were fixed in advance. Neither model weights nor threshold
0.2 changed. Training and selection used 48 and 16 games respectively, with
834 training and 280 validation branch samples. The selected minimum leaf size
was one. At depths eight, nine, and ten, training included only seven, four,
and nine immediate-collapse examples, respectively.

| Method | Total | Mean |
| --- | ---: | ---: |
| VLA only | 130 | 3.250 |
| Current-state rule | 208 | 5.200 |
| WAM | 292 | 7.300 |
| Width rule | 280 | 7.000 |
| Fixed six | 240 | 6.000 |
| Fixed seven | 189 | 4.725 |
| Fixed eight | 160 | 4.000 |
| Fixed nine | 153 | 3.825 |

The predeclared primary comparison was WAM versus width rule; current rule and
VLA were secondary. Paired bootstrap intervals used 20,000 resamples preserving
the width strata; two-sided sign-flip tests used 100,000 draws. Holm correction
covered all three comparisons. Evaluation stopped at forty games, without adding
trials until significance. All forty comparisons completed without technical errors.

| WAM minus comparator | Mean difference | 95% interval | Holm p | Wins / ties / losses |
| --- | ---: | --- | ---: | --- |
| Width rule | +0.300 | [-0.225, 0.625] | 0.29670 | 15 / 24 / 1 |
| Current rule | +2.100 | [1.325, 2.800] | 0.00080 | 20 / 11 / 9 |
| VLA only | +4.050 | [3.150, 5.000] | 0.00003 | 26 / 6 / 8 |

The current-rule gain was concentrated in narrow games: WAM 120 versus rule 34;
all twenty WAM decisions banked six, exactly matching the simple narrow rule.
In wide games WAM scored 172 versus current rule 174 and width rule 160.
Thus beating the current-state comparator does not establish that detailed
future-state prediction was necessary.

At the same wide tenth-placement depth, seed 63006 continued (risk 0.1650) and
completed ten within the original horizon; seed 63016 stopped (risk 0.2675),
banked nine, and its matched continuation collapsed during withdrawal. These
are examples of state-dependent decisions, not a causal ablation proving use
of each physical attribute.

Failure seed 63034 banked too late: the ninth placement passed the short check,
but stopping before the tenth collapsed after 12.0 seconds of banking. WAM
scored zero, width rule eight, current rule six. Seed 63024 stopped at eight
although the original-horizon continuation path could reach ten.

On WAM-reached states, confusion counts were TP 10, FP 25, TN 301, FN 0.
There were 35 stops, nine successful immediate-collapse rescues, one bank
collapse, and five ten-point games. Maximum-drop MAE was 22.25 mm. Mean
per-decision final-XYZ RMSE was 9.51 mm, worse than the simple planned/current
pose predictor's 7.76 mm. Classifier and regressor outputs can disagree; this
is not evidence of uniformly accurate object-state prediction.

## 4. v6: forecast placement plus subsequent banking

Seeds 65000–65039 form a separate unused forty-game evaluation. The old WAM
was run unchanged alongside the retrained WAM on the same games.

| Component | Old WAM | New WAM |
| --- | --- | --- |
| Continue target | Placement macro, 284 steps / 14.2 s | Placement plus bank/hold, 568 steps / 28.4 s |
| Bank target | 284 steps / 14.2 s | Unchanged |
| Minimum leaf size | 1 | 6 |
| Stop threshold | 0.2 | 0.5 |
| VLA, input features, physical distribution | Fixed | Unchanged |
| Main game actions and scoring | Original | Unchanged |

The same 48 training and 16 validation starts/actions were replayed with the
longer target. There were 834 training and 280 validation branches across 557
original decision states; 21 labels changed from safe to collapsed after the
extra wait. No extra wait was inserted into the actual continuation game:
label collection branched, observed the future, then restored the original state.
Those observations never became prediction inputs.

Leaf sizes 1, 3, 6 and thresholds 0.1, 0.2, 0.35, 0.5, 0.65, 0.8 were considered.
Selection maximized original validation-game score, then long-target balanced
accuracy, then larger leaf size. Therefore this is retraining and reselection,
not a one-parameter horizon ablation. Weights and settings were frozen before testing.

| Next block | Training safe / collapse, long target | Validation safe / collapse |
| --- | ---: | ---: |
| 1–5, each | 48 / 0 | 16 / 0 |
| 6 | 46 / 2 | 16 / 0 |
| 7 | 31 / 17 | 11 / 5 |
| 8 | 24 / 9 | 8 / 6 |
| 9 | 19 / 7 | 5 / 3 |
| 10 | 7 / 15 | 0 / 6 |

The absence of a safe tenth placement in validation limits selection of a policy
that should exploit safe tenth placements. It is a plausible limitation, not
an established causal explanation for conservatism.

| Method | Original total | Mean | Extra terminal hold, descriptive total |
| --- | ---: | ---: | ---: |
| VLA only | 80 | 2.000 | 10 |
| Current-state rule | 202 | 5.050 | 192 |
| New WAM | 273 | 6.825 | 273 |
| Old WAM | 281 | 7.025 | 261 |
| Width rule | 280 | 7.000 | 280 |
| Fixed six | 240 | 6.000 | 240 |
| Fixed seven | 217 | 5.425 | 217 |
| Fixed eight | 160 | 4.000 | 160 |
| Fixed nine | 135 | 3.375 | 135 |

Primary comparisons were new versus width rule and old WAM; secondary comparisons
were current rule and VLA. The same resampling counts were used, with Holm
correction across four comparisons. All forty games completed without technical errors.
Intervals are per-comparison, unadjusted bootstrap intervals; p-values are
multiplicity-adjusted, so their significance boundaries need not coincide.

| New WAM minus comparator | Mean difference | 95% interval | Holm p | Wins / ties / losses |
| --- | ---: | --- | ---: | --- |
| Width rule | -0.175 | [-0.825, 0.300] | 0.51918 | 9 / 29 / 2 |
| Old WAM | -0.200 | [-0.350, -0.050] | 0.07004 | 1 / 31 / 8 |
| Current rule | +1.775 | [0.825, 2.725] | 0.01287 | 15 / 13 / 12 |
| VLA only | +4.825 | [3.800, 5.850] | 0.00004 | 30 / 2 / 8 |

### Prediction and failure analysis

The new WAM stopped in all forty games: all twenty narrow games at six; wide
games eleven times at nine and nine times at eight. Two nine-block bank attempts
collapsed. It earned nine points nine times and ten points zero times.

| Reached-state scope / target | TP | FP | TN | FN |
| --- | ---: | ---: | ---: | ---: |
| New reached / long target | 22 | 18 | 289 | 2 |
| New reached / immediate target | 13 | 27 | 291 | 0 |
| Old reached / immediate target | 16 | 21 | 299 | 0 |
| Old reached / long diagnostic | 25 | 12 | 295 | 4 |

Reached-state sets differ between policies. The old model was not trained on
the long target; this table is not a like-for-like prediction-accuracy ranking.
There were eleven immediate-collapse rescues and nine avoided hazards that
appeared only after placing and then stopping. The latter do not prove that
continuing immediately to further placements would necessarily collapse.

- **Delayed collapse avoided, 65016:** before block ten, new risk 0.776 caused
  banking nine; old risk 0.143 allowed ten. The old path scored ten originally
  but collapsed during the extra terminal hold.
- **Unnecessary stopping, 65002:** before block nine, new risk 0.556 banked eight.
  The matched ninth placement and extra hold were safe. Old WAM earned nine.
- **Missed early warning, 65020 and 65034:** before block nine, new risks 0.418
  and 0.349 were below 0.5. Both later stopped before ten but collapsed while
  banking. New and old WAM scored zero; width stopping preserved eight.
- **Safe completion rejected, 65008:** ten remained stable even with the extra
  wait. Old WAM earned ten; new WAM stopped at nine.

The two known development failures, 63034 and 61004, were excluded from the new
test set. Their reproduced delayed collapse supplied a diagnosis, but retraining
still gave ninth-placement risks 0.279 and 0.479, below the stop threshold.
Reproducing a failure is distinct from learning to prevent it.

Long-target maximum-drop MAE was 28.98 mm; mean final-XYZ RMSE was 14.61 mm
versus 17.84 mm for planned/current pose. These metrics have a different target
horizon from v5 and cannot be treated as direct improvements over its errors.
Separate regression and classification heads need not be physically consistent.

## 5. Terminal stability and interpretation

Voluntary stopping already observed an additional 284-step bank/hold, whereas
original ten-block completion did not. Of eight common-path ten-point games
in v6, seven collapsed in the extra terminal wait. Original ten-point records
are correct under that finite endpoint but do not establish longer stability.

The additional terminal-hold diagnostic was specified before fitting/testing.
It replaces only ten-point outcomes with their measured extra-hold outcomes;
earlier stops keep their measured banking scores. It was not used for model
selection and has no new superiority test. Its reversal of old/new ranking
must not be used to replace the preregistered primary result after inspection.
The width rule still scores higher than either WAM under this diagnostic.

The next justified protocol change is to apply the same post-placement terminal
stability interval at every stopping count, including ten, and align training,
validation selection, and evaluation with it. That next experiment has **not**
been performed in this update. Previously inspected games cannot become fresh
test data after redesign. Neither cohort establishes superiority over the width
rule, equivalence of methods, hardware safety, or general-purpose WAM capability.

## 6. Per-game original scores

Stop counts record decisions, not necessarily successful banking. A dash means
no voluntary stop; a score of ten means completion under the original horizon.
The two cohorts remain separate and must not be pooled.

### v5

| Seed | Width | VLA | Current rule | WAM | Width rule | WAM stop |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 63000 | wide | 10 | 10 | 9 | 8 | 9 |
| 63001 | narrow | 0 | 0 | 6 | 6 | 6 |
| 63002 | wide | 0 | 8 | 8 | 8 | 8 |
| 63003 | narrow | 0 | 6 | 6 | 6 | 6 |
| 63004 | wide | 10 | 8 | 9 | 8 | 9 |
| 63005 | narrow | 0 | 0 | 6 | 6 | 6 |
| 63006 | wide | 10 | 8 | 10 | 8 | completed ten |
| 63007 | narrow | 0 | 7 | 6 | 6 | 6 |
| 63008 | wide | 10 | 10 | 9 | 8 | 9 |
| 63009 | narrow | 0 | 0 | 6 | 6 | 6 |
| 63010 | wide | 10 | 9 | 9 | 8 | 9 |
| 63011 | narrow | 0 | 0 | 6 | 6 | 6 |
| 63012 | wide | 10 | 9 | 9 | 8 | 9 |
| 63013 | narrow | 0 | 0 | 6 | 6 | 6 |
| 63014 | wide | 10 | 7 | 10 | 8 | completed ten |
| 63015 | narrow | 0 | 0 | 6 | 6 | 6 |
| 63016 | wide | 0 | 9 | 9 | 8 | 9 |
| 63017 | narrow | 0 | 0 | 6 | 6 | 6 |
| 63018 | wide | 10 | 10 | 10 | 8 | completed ten |
| 63019 | narrow | 0 | 0 | 6 | 6 | 6 |
| 63020 | wide | 10 | 10 | 10 | 8 | completed ten |
| 63021 | narrow | 0 | 7 | 6 | 6 | 6 |
| 63022 | wide | 10 | 9 | 10 | 8 | completed ten |
| 63023 | narrow | 0 | 0 | 6 | 6 | 6 |
| 63024 | wide | 10 | 9 | 8 | 8 | 8 |
| 63025 | narrow | 0 | 0 | 6 | 6 | 6 |
| 63026 | wide | 0 | 8 | 8 | 8 | 8 |
| 63027 | narrow | 0 | 7 | 6 | 6 | 6 |
| 63028 | wide | 0 | 9 | 9 | 8 | 9 |
| 63029 | narrow | 0 | 0 | 6 | 6 | 6 |
| 63030 | wide | 10 | 9 | 9 | 8 | 9 |
| 63031 | narrow | 0 | 0 | 6 | 6 | 6 |
| 63032 | wide | 0 | 9 | 9 | 8 | 9 |
| 63033 | narrow | 0 | 7 | 6 | 6 | 6 |
| 63034 | wide | 0 | 6 | 0 | 8 | 9 |
| 63035 | narrow | 0 | 0 | 6 | 6 | 6 |
| 63036 | wide | 0 | 9 | 8 | 8 | 8 |
| 63037 | narrow | 0 | 0 | 6 | 6 | 6 |
| 63038 | wide | 10 | 8 | 9 | 8 | 9 |
| 63039 | narrow | 0 | 0 | 6 | 6 | 6 |

### v6

| Seed | Width | VLA | Current rule | New WAM | Old WAM | Width rule | New / old stop |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 65000 | wide | 0 | 9 | 9 | 9 | 8 | 9 / 9 |
| 65001 | narrow | 0 | 7 | 6 | 6 | 6 | 6 / 6 |
| 65002 | wide | 0 | 9 | 8 | 9 | 8 | 8 / 9 |
| 65003 | narrow | 0 | 0 | 6 | 6 | 6 | 6 / 6 |
| 65004 | wide | 0 | 0 | 9 | 9 | 8 | 9 / 9 |
| 65005 | narrow | 0 | 0 | 6 | 6 | 6 | 6 / 6 |
| 65006 | wide | 0 | 8 | 8 | 8 | 8 | 8 / 8 |
| 65007 | narrow | 0 | 7 | 6 | 6 | 6 | 6 / 6 |
| 65008 | wide | 10 | 10 | 9 | 10 | 8 | 9 / — |
| 65009 | narrow | 0 | 0 | 6 | 6 | 6 | 6 / 6 |
| 65010 | wide | 0 | 9 | 9 | 9 | 8 | 9 / 9 |
| 65011 | narrow | 0 | 0 | 6 | 7 | 6 | 6 / 7 |
| 65012 | wide | 10 | 9 | 9 | 9 | 8 | 9 / 9 |
| 65013 | narrow | 0 | 0 | 6 | 6 | 6 | 6 / 6 |
| 65014 | wide | 0 | 8 | 8 | 8 | 8 | 8 / 8 |
| 65015 | narrow | 0 | 0 | 6 | 6 | 6 | 6 / 6 |
| 65016 | wide | 10 | 9 | 9 | 10 | 8 | 9 / — |
| 65017 | narrow | 0 | 7 | 6 | 6 | 6 | 6 / 6 |
| 65018 | wide | 0 | 9 | 8 | 9 | 8 | 8 / 9 |
| 65019 | narrow | 0 | 0 | 6 | 6 | 6 | 6 / 6 |
| 65020 | wide | 0 | 8 | 0 | 0 | 8 | 9 / 9 |
| 65021 | narrow | 0 | 0 | 6 | 6 | 6 | 6 / 6 |
| 65022 | wide | 10 | 9 | 8 | 9 | 8 | 8 / 9 |
| 65023 | narrow | 0 | 7 | 6 | 6 | 6 | 6 / 6 |
| 65024 | wide | 0 | 4 | 8 | 9 | 8 | 8 / 9 |
| 65025 | narrow | 0 | 7 | 6 | 6 | 6 | 6 / 6 |
| 65026 | wide | 10 | 9 | 8 | 8 | 8 | 8 / 8 |
| 65027 | narrow | 0 | 0 | 6 | 6 | 6 | 6 / 6 |
| 65028 | wide | 10 | 9 | 9 | 9 | 8 | 9 / 9 |
| 65029 | narrow | 0 | 6 | 6 | 6 | 6 | 6 / 6 |
| 65030 | wide | 0 | 9 | 9 | 9 | 8 | 9 / 9 |
| 65031 | narrow | 0 | 0 | 6 | 6 | 6 | 6 / 6 |
| 65032 | wide | 0 | 8 | 8 | 8 | 8 | 8 / 8 |
| 65033 | narrow | 0 | 0 | 6 | 6 | 6 | 6 / 6 |
| 65034 | wide | 0 | 0 | 0 | 0 | 8 | 9 / 9 |
| 65035 | narrow | 0 | 0 | 6 | 6 | 6 | 6 / 6 |
| 65036 | wide | 10 | 10 | 8 | 10 | 8 | 8 / — |
| 65037 | narrow | 0 | 0 | 6 | 6 | 6 | 6 / 6 |
| 65038 | wide | 10 | 9 | 9 | 8 | 8 | 9 / 8 |
| 65039 | narrow | 0 | 6 | 6 | 6 | 6 | 6 / 6 |

## 7. Verification and publication boundary

Both evaluations recorded 355 prediction-input hash checks and 155 saved-state
full-placement replays, with replayed trajectories checked to tolerance 1e-9.
Recorded VLA-output/action correspondence checks covered 4,561 chunks in v5 and
4,537 in v6. Training replay in v6 checked 7,199 original VLA chunks; 315 test
checks matched extra forecast waiting with the corresponding next bank branch.
Frozen models/code were checked against their pre-test bindings. Completion
records report no technical failures, local processes stopped, and no cloud
resources created. These are recorded run audits, not new simulator runs for
publication or a current cloud-account billing audit.

| Artifact | SHA-256 |
| --- | --- |
| Frozen SmolVLA | `254ec40e3be4a44f62073be9cea999ed165ca533d26204beb7f1238890fd38b4` |
| Short-horizon WAM, v5 and old v6 | `3114956c577511c385436f625ecb9936fa98a8c2290fe77b7282f0b1823a7077` |
| Long-horizon WAM, new v6 | `fac4cf0ea85e1d7eef3a0af361e842b88fbe60148003530ac00c337b4660730b` |

This is a reviewed English summary and per-game score transcription from local
protocols, training/evaluation results, and completion audits. It publishes no
raw private logs, checkpoints, training data, workstation paths, or simulator
launchers. Hashes identify audited artifacts but are not downloadable evidence
or independent runtime proof. The PR is not an independently runnable replication
package. Public documentation checks can establish arithmetic and link integrity,
not independently validate the physics. The existing videos only cover the
initial report's pilot and centered examples.
