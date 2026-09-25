# Curated PX4 aerial integration evidence

This reviewed export supports the [Japanese technical report](../../agents/aerial-wam-px4-technical-report-20260922.md).
It contains aggregate results, public model identities, relative simulator
coordinates, and a figure rendered solely from those data. The flight completed
the selected simulator action; its choice of goal direction was wrong.

`summary.json` distinguishes source age, model computation, API latency, command
age, and simulator motion time. `trajectory.json` contains sampled observations
plus the exact dispatch, arrival, landing, and disarm checkpoint observations.
Connecting samples in the figure is a visualization, not evidence of continuous
collision checking. No future or counterfactual motion has been fabricated.

Raw camera captures, generated model images, full simulator logs, model weights,
private task databases, user authorization text, session credentials, cloud
identities and workstation paths are not included. The source-file digests
identify retained bytes, whose private preimages are not shipped. These hashes
are provenance references, not independent proof that the model or API ran.
The source chain was checked locally against model output hashes, Jev receipts,
the accepted command, simulator observations and terminal state before export.

```sh
python docs/assets/aerial-wam-px4-20260922/verify_report.py
python -m pytest docs/assets/aerial-wam-px4-20260922/test_verify_report.py -q
```

The standard-library verifier checks file integrity, arithmetic, timing, goal
selection, and trace/checkpoint consistency. It does not rerun inference,
contact an API, launch a simulator, or attest to an omitted raw source.
Mutation tests ensure misleading success, expired source data, altered paths,
inconsistent landing records, and damaged files are rejected.

To redraw the figure, install Matplotlib in a separate plotting environment and
run `render_report.py` in this directory. The recorded rendering used Matplotlib
3.10.8. Rendering uses no private files. A PNG may vary across library versions;
a reviewed regeneration must update its entry in `manifest.json`.

The budget is an estimate for compute, disk and IP resources across this work;
cloud invoices and Jev API billing are not verified. No hardware execution,
learned collision prediction, general navigation benefit, or goal completion is
claimed. The nominal one-second model horizon is not calibrated to the measured
6.824-second simulator maneuver.
