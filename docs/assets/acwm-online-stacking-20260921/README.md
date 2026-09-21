# Online ACWM stacking evidence

This directory contains the reviewed public subset of the tape-free ACWM
experiment. `benchmark.json` records the fixed 40-seed × 5-method online cohort;
`protocol.json` identifies the frozen conditions. Phase 1 files describe the
previously completed 16-placement feasibility gate.

## Reading the records

- `games`: one record for every attempted method/seed, including terminal outcome,
  collapse, retained count and actual execution totals.
- `decisions.csv`: readable per-decision scores, choices, labels and latency.
- `decision-evidence.json.gz`: complete decision records, including current-state
  and action digests, Prediction Core receipts, real VLA inference identifiers and
  ordering times, measured placement/hold outcomes and after-bank counterfactual
  labels. The verifier checks the CSV against these detailed records.
- `paired_rows`: final scores aligned by seed.
- `prefix_audit`: equality of states and actually executed placement actions
  across independently reset arms until their choices diverged.
- `comparisons`: paired score differences, width-stratified bootstrap intervals,
  sign-flip p-values and Holm correction across four comparisons.

`count_after` / `stop_count` records how many objects had entered the score-bearing
stack. A collapsed stack can therefore have a positive count and a zero score.
A bank event keeps that count; its `next_count` is the unexecuted proposed block.

ACWM confusion uses the measured **14.2-second placement**. ExtraTrees confusion
uses placement plus hold (**28.4 seconds**). These are different label horizons.
All actual game endings receive the common 14.2-second terminal hold. Intermediate
holds and after-bank continuations are diagnostic branches, restored before any
subsequent actual decision; their motor steps are excluded from actual-game
totals. An already collapsed placement remains zero after its diagnostic hold.

Each method reaches its own decision states and may stop at a different time.
Decision-level confusion counts are therefore descriptive, not an identical-case
accuracy contest or independent statistical samples.

## Verification

```sh
python docs/assets/acwm-online-stacking-20260921/verify_report.py
python docs/assets/acwm-online-stacking-20260921/verify_report.py --statistics
python docs/assets/acwm-online-stacking-20260921/verify_media.py
```

The first command uses the Python standard library, including gzip for the
compressed audit records. Statistics needs NumPy;
media decoding needs Pillow, ffmpeg and ffprobe. SHA-256 hashes detect altered
assets; the separate media command actually decodes them. Neither command reruns
the neural training or simulator.

Phase 1 MP4s show **predicted versus measured** validation frames. Phase 2
representative strips show **generated futures only**, selected by the first
seed/count in each nonempty TP/FP/TN/FN cell. Their measured outcomes are in the
individual records. Graphs summarize the new online cohort; they are not a
learning curve across earlier cohorts.

Large model weights, private runtime paths, credentials and full simulator
snapshots are excluded. Model hashes identify the executed artifacts; publication
of this evidence subset does not provide a standalone training environment.
