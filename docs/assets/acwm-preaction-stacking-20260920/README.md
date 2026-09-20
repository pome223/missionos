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
summary. Model weights and private source simulator bundles are excluded.

Verify from the repository root:

```bash
python docs/assets/acwm-preaction-stacking-20260920/verify_report.py
```

