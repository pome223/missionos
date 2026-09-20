# ACWM pre-action block-placement artifacts

[Technical report](../../agents/acwm-preaction-stacking-forecast-20260920.md)

These files document the bounded 20 September 2026 ACWM experiment. Videos
show measured simulator futures on the left and ACWM forecasts generated from
the pre-action frame on the right. Contact sheets use the same ordering.

| Stem | Outcome |
| --- | --- |
| `validation-59000-01` | Stable first placement |
| `validation-59000-07` | Stable seventh placement |
| `validation-59003-08` | Eighth-placement collapse case |
| `validation-59006-10` | Safe tenth placement; ACWM readout false positive |

Machine-readable files preserve the fixed-model training record, all validation
readout decisions, the complete seed-59000 sequential replay, and verification
summary. The three `forecast-*.json` files contain the 16 per-case inputs to the
reported IoU, final-three-frame IoU, and centroid-error means. Model weights and
private source simulator bundles are excluded.

`v7-40-game-acwm-comparison.json` contains all 40 same-cohort scores and the
ACWM decision sequence at every reached state, together with paired comparisons
against the published ExtraTrees, width-rule, current-state, and VLA baselines.

Verify from the repository root:

```bash
python docs/assets/acwm-preaction-stacking-20260920/verify_report.py
```

The dependency-free verifier recomputes confusion matrices and metric means,
checks the sequential decisions and measured bank outcome, and validates every
media file against `media-sha256.json`. With FFmpeg installed, also decode and
inspect the video streams:

```bash
python docs/assets/acwm-preaction-stacking-20260920/verify_media.py
```

Recompute the width-stratified bootstrap intervals, sign-randomization tests,
and Holm adjustment for the forty-game comparison:

```bash
uv run --no-project --with numpy \
  python docs/assets/acwm-preaction-stacking-20260920/analyze_40_game.py
```
