# Optional return-risk advice changes two first-entry decisions

On three fresh timing sequences and five predeclared arrival times, first-entry proposals followed by measured reoccupation within four seconds decreased from 4/15 to 2/15. These are **recorded-image decision replays**, not fifteen flights, crashes avoided, or delivery outcomes. The CPU state learner is not native ANWM/VLA, and the shared MissionAssuranceAgent uses a deterministic fixture judge.

[Interactive observations and advice](index.html) · [Japanese report](REPORT-ja.md) · [Exact metrics](evaluation/evaluation.json)

A separately frozen opt-in policy uses supported inward image motion and predicted center entry into the 6 m pad radius as a reason to wait and reobserve. Unknown occupancy remains unknown. It creates no entry authority, relaxes no current Rules and retains no stale prediction. The policy was diagnosed using the first negative experiment and then evaluated on 724 newly captured frames from three different timings. No further training occurred. Two arrival conditions delayed their first-entry proposal by 34 simulated seconds each until the lead left again; all fifteen eventually proposed entry. Two near-term reoccupation conditions remain unresolved. Four of eight relevant windows changed to waiting, with no additional waits without next-four-second reoccupation on the new sequences. Regression on the older recordings included one such additional wait.

The predictor receives sixteen past RGB frames, camera poses and timestamps only. Gazebo lead poses are current-Rules/scoring data, never forecast inputs. Own-aircraft hold, battery and authority are explicitly synthetic replay fixtures. No dispatcher runs. The camera, appearance and corridor are shared with training, so the new timings provide narrow interpolation evidence. Overlapping windows and arrival conditions are correlated. The oracle uses future measurements for an upper bound only. The baseline is not intentionally allowed to collide.

The maintained entrypoint reopens 606 real CPU forecasts and 1212 shared judgment/response-check receipts. All owned capture containers were removed. The videos show real fixed-camera Gazebo recordings at 1×; they carry no fabricated battery display. Sources, protocols, model weights, capture hashes and failures/negative evidence are retained.

```sh
python scripts/check_yokohama_pad_reentry.py --bundle docs/examples/yokohama-pad-reentry
```

New GPU cost: **$0**. Cumulative estimate: **$18.53 / $20**. The flight CLI defaults and registered model remain unchanged. Native VLA/ANWM joint flight, live returning-lead AP recovery, cargo delivery benefit, onboard perception, power use and real-world safety are unverified. The next boundary is opt-in PX4 hold/reobserve and a completed delivery/return with this returning actor.

[Agent contract](../../agents/yokohama-pad-reentry.md) · [Source attribution](../yokohama-urban-scene/ATTRIBUTION.md)

RGB overlap audit: 407/724 evaluation frames and 53/154 evaluated 16-frame histories were byte-identical to training/development RGB. This includes static intervals; new timing schedules do not imply independent imagery. [Overlap record](overlap.json).
