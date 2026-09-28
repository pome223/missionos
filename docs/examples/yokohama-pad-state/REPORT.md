# Learning the lead aircraft's position and pad occupancy

**A small CPU model changed its advisory from waiting to reviewing entry in all three new timing sequences, with zero false-clear occupied targets. The owner has removed the fixed 90% adoption gate: proceed with advisory integration and decide adoption by practical usefulness. This does not improve native ANWM imagery or VLA weights.**

[Interactive observations and forecasts](index.html) · [Departure video](videos/state-depart.mp4) · [Stall video](videos/state-stall.mp4) · [Reentry video](videos/state-reenter.mp4)

The [previous ANWM post-training](../yokohama-pad-learning/REPORT.md) reduced background image error while losing the small lead aircraft. This experiment learns localization directly from RGB and forecasts short future positions and occupancy. No GPU or native ANWM/VLA inference was used.

## Measured result

A frozen model processed three newly captured CPU Gazebo sequences: departure, a stall before departure, and reentry. There are 704 frames, 155 overlapping histories, and 17 scored timestamps from 0 through 4 seconds per history. Of the resulting targets, 2,491 have a definite occupied/clear label. These are correlated samples, not independent flight trials.

| Metric | Result |
|---|---:|
| Occupancy agreement, counting abstentions as mismatches | 1,999 / 2,491 = **80.2%** |
| False clear on occupied targets | **0 / 1,356** |
| Abstention on definite targets | 19.8% |
| Supported histories | **139 / 155 = 89.7%**, versus the historical 90% experiment condition, no longer an adoption gate |
| Mean position error across supported histories and horizons | 0.279 m |
| Position error at 4 seconds | 0.570 m |
| CPU inference for 16 images, mean / p95 | 0.036 / 0.037 seconds |
| Wait → review-entry transition | 3 / 3 sequences |

Position errors exclude unsupported histories; agreement includes abstentions. Five of six frozen conditions passed. In all 16 unsupported histories, one image's localization peak was insufficiently separated from an alternative. The overlapping four-second history propagates that abstention to four consecutive advisories. The threshold and weights were not retuned after these results.

In reentry, the advisory changed from reviewing entry to waiting near 12 seconds, and back near 49 seconds. It abstained again around 51–54 seconds before recovering near 55 seconds. These are recorded-image advisories, with no dispatch or aircraft motion caused by them. No review-entry advisory overlapped an occupied **sampled** target; this does not establish continuous collision freedom.

## Updated adoption policy

Use the model if enabling it provides practical benefit in arrival, avoidable waiting, rework or human intervention, taking total time/energy/cost into account. There is no universal accuracy or support-rate cutoff, and no requirement to beat ideal Rules. Per-observation validity checks and independent current-state Rules remain in place.

The current decision is **proceed with optional city pad-wait advisory integration**. Reduced waiting or improved delivery outcomes have not yet been demonstrated. The next connected run will compare model-enabled and model-disabled behavior. Missing/ambiguous evidence falls back to fresh observation and existing bounded hold/reobserve or mission-level escalation.

The original frozen protocol and failed experiment status remain historical evidence. They are superseded for adoption by the separate [adoption policy](adoption-policy.json), not relabeled as a passing experiment. Live integration is still incomplete; that is distinct from rejection at 89.7%.

## Diagnostic comparison

All methods use the same learned current image localizations and history support gate.

| Predictor | Occupancy agreement | False clear | Mean position error |
|---|---:|---:|---:|
| Current position persists | 86.1% | 0 | 0.692 m |
| Constant velocity | 89.7% | 0 | 0.306 m |
| Learned state forecast | 80.2% | 0 | 0.279 m |

Lower position error did not produce better occupancy agreement. The learned predictor additionally applies development-calibrated error radii and an occupancy-score condition; the comparators classify their predicted positions by distance. This is a diagnostic, not a matched uncertainty-policy comparison or evidence of mission advantage. Beating an ideal Rules baseline was not required. Comparator timing fields reuse the common learned pipeline measurement and are not independent comparator benchmarks.

## Scope and data reuse

Training reused 720 frames. The previous evaluation's 752 frames are now explicitly **development** data for model selection and empirical uncertainty calibration. The new records were acquired after model/protocol freeze. However, appearance, camera, geometry and corridor are shared: 400/704 new frames and 61/155 complete image histories have identical training pixels. This is new timing evaluation, not unseen-scene generalization.

The learned localization and pixel-to-3D mapping apply only to one fixed camera and a known visible lead in the recorded corridor. The model is not a general absence detector. Missing detection, ambiguous localization, changed camera pose or unsupported history cannot become permission to move. Targets and actor schedules are excluded from its inference API.

The camera is an authored static rig; the lead is a scripted Gazebo entity and the parcel is preplaced. There is no AP hold verification, battery telemetry, unloading, delivery, return, or physical execution in this experiment. Native ANWM weights and VLA are unchanged. Sampled forecasts have neither own-action conditioning nor continuous-interval collision semantics; the production flight adapter was not relaxed to accept them. Sea legs remain AP-only.

The next integration uses this optional advisory alongside real VLA+WAM while MissionOS is waiting, handles the transition from a fixed rig to a moving onboard camera, and measures enabled/disabled mission effects. Flight authorization remains separate.

## Reproduction and cost

New GPU cost **$0**; cumulative estimate **$18.532819 / $20**, not a settled invoice. No cloud resources were created; the owned local capture container was removed. The full suite passed **3,588 tests**, with 2 skipped.

```sh
python scripts/check_yokohama_pad_state.py --bundle docs/examples/yokohama-pad-state
```

This reloads the published model and recomputes all 155 forecasts from recorded RGB without a simulator or GPU. The bundle includes resized RGB arrays, measured targets/timestamps/poses, learned CPU weights, reports and videos. Raw RGBD and failed development attempts remain local. See the [protocol](model/protocol.json), [training](model/training.json), [results](evaluation/evaluation.json), [diagnostics](diagnostics.json), [manifest](evidence-manifest.json) and [maintainer contract](../../agents/yokohama-pad-state.md).

Source: modified Yokohama / Project PLATEAU scene. [Attribution and license](../yokohama-urban-scene/ATTRIBUTION.md).
