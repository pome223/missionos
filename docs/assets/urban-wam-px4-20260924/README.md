# Urban ANWM / PX4 simulator trials — 2026-09-24

This curated engineering report separates model invocation, route ranking,
independent geometry constraints, PX4 execution and observed arrival/landing.
It is not a randomized success-rate benchmark or physical-hardware validation.

The execution boundary works. Navigation improvement is **not established**:
the passage case's ANWM ranking selected an unnecessary ~35 m detour where the
geometric forward route completed in ~14 m. In the climb case, the model ranked
an obstructed forward route first; geometry admitted only the climb. That climb
is not evidence that WAM learned to avoid the building. Predicted images also
invented scenery absent from the actual simulator world.

See the [technical report](../../agents/aerial-wam-px4-technical-report-20260922.md#15-実anwmによる都市経路の順位付けとpx4実行2026-09-24)
and [execution contract](../../agents/urban-wam-px4-trial.md).

- `summary.json`: whitelisted measurements, all sampled route positions, bounded
  inference facts, scores and retained-source hashes. No session IDs, instruction
  references, keys, workstation/cloud identities or raw sensor messages.
- `geometric-flights.png`: observed routes and camera frames for the baseline trials.
- `wam-flights.png`: observed routes and camera frames after ANWM ranking and Rules.
- `wam-previews.png`: actual observation/goal crops and generated candidate images.
- `wam-flights.mp4`: actual onboard camera playback, **4x simulation time**. The
  inference wait is omitted from playback and recorded separately in the report.
- `verify_report.py` and `test_verify_report.py`: arithmetic, integrity, scope and
  mutation checks; they do not rerun the model or flight.

```sh
python docs/assets/urban-wam-px4-20260924/verify_report.py
python -m pytest docs/assets/urban-wam-px4-20260924/test_verify_report.py -q
```

The local source verifier is `scripts/verify_urban_wam_trial.py`. The exporter is
`scripts/export_urban_wam_report.py`; trajectory figures and video are rendered by
`scripts/render_urban_wam_trial.py` and `scripts/render_urban_wam_video.py`.
`render_previews.py <local-trial-root> ... <output.png>` creates the forecast gallery
from the retained local artifacts. Plotting uses NumPy, Pillow and Matplotlib;
video additionally uses FFmpeg. These renderers send no model or aircraft commands.

History images are sixteen genuine 4 Hz simulation observations, with matching
RGB/depth/calibration/pose timestamps. Route previews use five-metre path prefixes
and nominal eight-second conditioning. Future flight timing is not validated.
Goal RGB is used only for scoring. RGB MSE is not collision probability.
The separately declared goal reference is captured before candidate execution.

Geometry is known scene truth: mesh bounds plus a 0.6 m enclosing airframe sphere
and 0.25 m margin. Contact sensors supplied a positive ground-contact control;
zero building-contact notifications alone are not a contact-free guarantee.
The reported clearance comes from the recorded swept trajectory. The three
baseline and model runs use independent resets at the same nominal start, not
exactly cloned physics states. Original failed capture attempts and their
landing/disarm are described in the technical report, not counted as model success.

## Asset attribution

Apartment mesh and textures: [OSRF gazebo_models](https://github.com/osrf/gazebo_models/tree/8163eb4b5e7e21985c6591d1c0bfb56468c0093f/apartment),
commit `8163eb4b5e7e21985c6591d1c0bfb56468c0093f`; [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/),
copyright 2012 Nathan Koenig, model author Cole Biesemeyer. Source mesh/textures
are unchanged; instances are translated and uniformly scaled by the trial SDF.
Observed simulator images and their figure/video derivatives are attributed here.
ANWM-generated images are labeled separately from actual observations.
