# Figure sources for the MissionOS WAM system report

[Technical report](../../agents/missionos-wam-system-technical-report-20260919.md)

All plots are derived from reviewed public score tables and summarized runtime
records. No new experiments or probability calibration are performed here.
`figure-data.json` includes the full 120 score rows from the three forty-game
cohorts, reported v7 paired intervals and adjusted p-values, and the two known-case
DeepSeek comparisons. The cohorts must not be pooled.

| Stem | Content |
| --- | --- |
| 01-cohort-scores | Three separate cohort score summaries |
| 02-v7-game-scores | All forty v7 games, grouped by width |
| 03-v7-paired-intervals | Recorded bootstrap intervals and Holm p-values |
| 04-v7-tradeoff | Gain/loss decomposition and attempted stopping counts |
| 05-deepseek-correction | Known-case measured scores and uncalibrated utility proxies |
| 06-governed-architecture | Evidence, judgment, authority and verification boundaries |

Each stem has a PNG for browser reading and SVG for scalable reuse. All axes,
labels, values and interpretation qualifications were visually reviewed.
The architecture diagram is a responsibility sequence: human approval is bounded
preapproval supplied before execution, not a fresh click after each LLM call.

Reproduce and verify from the repository root:

```bash
uv run --no-project --with matplotlib \
  python docs/assets/missionos-wam-20260919/render_figures.py
python docs/assets/missionos-wam-20260919/verify_report.py
```

Rendering was checked with matplotlib 3.10.9. Reproduction requires matplotlib
and NumPy, but no model weights, credentials or simulator. SVG generation omits
timestamps; font/rendering differences between environments may still occur.
Intervals and p-values are transcribed, not recomputed by this figure renderer.
