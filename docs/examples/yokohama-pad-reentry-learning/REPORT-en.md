# Post-training alone does not anticipate the returning lead

Post-training improved input support at the eight relevant decision windows but did not change the wait/entry decision. First-entry proposals followed by reoccupation remained 4/15. These are **recorded-image decision replays**, not fifteen flights, crashes avoided, or delivery outcomes. The CPU state learner is not native ANWM/VLA, and the shared MissionAssuranceAgent uses a deterministic fixture judge.

[Interactive observations and advice](index.html) · [Japanese report](REPORT-ja.md) · [Exact metrics](evaluation/evaluation.json)

Training added 240 return frames and selection added 248 different-speed frames to the previous splits. Training took about 11.3 wall seconds without a GPU. The model was frozen before reading 716 evaluation frames. Input support improved from one to eight of the eight relevant windows, but future occupancy remained unknown and the production policy fell back to current Rules. The future positional uncertainty is too wide to require confirmed occupancy early enough. This negative result is retained; there is no fixed 90% adoption gate or requirement to beat ideal Rules.

The predictor receives sixteen past RGB frames, camera poses and timestamps only. Gazebo lead poses are current-Rules/scoring data, never forecast inputs. Own-aircraft hold, battery and authority are explicitly synthetic replay fixtures. No dispatcher runs. The camera, appearance and corridor are shared with training, so the new timings provide narrow interpolation evidence. Overlapping windows and arrival conditions are correlated. The oracle uses future measurements for an upper bound only. The baseline is not intentionally allowed to collide.

The maintained entrypoint reopens 602 real CPU forecasts and 903 shared judgment/response-check receipts. All owned capture containers were removed. The videos show real fixed-camera Gazebo recordings at 1×; they carry no fabricated battery display. Sources, protocols, model weights, capture hashes and failures/negative evidence are retained.

```sh
python scripts/check_yokohama_pad_reentry.py --bundle docs/examples/yokohama-pad-reentry-learning
```

New GPU cost: **$0**. Cumulative estimate: **$18.53 / $20**. The flight CLI defaults and registered model remain unchanged. Native VLA/ANWM joint flight, live returning-lead AP recovery, cargo delivery benefit, onboard perception, power use and real-world safety are unverified. The next boundary is opt-in PX4 hold/reobserve and a completed delivery/return with this returning actor.

[Agent contract](../../agents/yokohama-pad-reentry.md) · [Source attribution](../yokohama-urban-scene/ATTRIBUTION.md)

RGB overlap audit: 399/716 evaluation frames and 46/152 evaluated 16-frame histories were byte-identical to training/development RGB. This includes static intervals; new timing schedules do not imply independent imagery. [Overlap record](overlap.json).
