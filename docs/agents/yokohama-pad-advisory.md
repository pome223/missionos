# Optional learned pad-state advisory

This opt-in CPU integration connects the frozen `yokohama-pad-state` auxiliary
model to the aircraft/MissionOS mailbox. It does not qualify the native ANWM
prediction profile or run a VLA. The Mission Assurance judge is a deterministic
fixture; the RGB localization/state predictor uses actual learned weights.

Run an approved, zero-wind simulated cargo round trip with:

```sh
python scripts/yokohama_sitl.py --phase flight --approve-sitl --output-dir RUN \
  --sea-round-trip --deliver-payload --occupied-pad --pad-state-advisory assist \
  --timeout-seconds 2400
```

`shadow` records the same forecasts but leaves the pose-based proposal unchanged.
Both modes require `--occupied-pad`; no native backend or wind can be combined.
No hardware execution or cloud/GPU provisioning is performed.

## Observation and model scope

- A fixed camera at the delivery pad matches the frozen model's training pose,
  FOV and resolution. This is pad infrastructure, **not onboard perception**.
- Only the city `02-D3` pad-wait phase collects model input. Sixteen 4 Hz RGB
  frames, measured camera poses and timestamps enter `Model.predict`.
- Lead positions, schedule, future images and current occupancy from Gazebo
  are not passed to that learned predictor. Independent execution Rules still
  use Gazebo pose telemetry; this is a simulation perception substitute.
- Weights load lazily on the first complete city history. The aircraft closes
  the session when pad waiting ends, clears its history, and the host releases
  the model. Post-exit requests/responses are rejected. No sea-leg inference or
  warm-up is enabled. A fixed camera rendering before city entry is not onboard
  inference or measured onboard energy use.
- The original per-observation validity contract remains unchanged. No aggregate
  90% threshold blocks integration. Unknown evidence falls back to current Rules.

## Authority and timing

`MissionSituation.uncertainty.forecast` contains the hash-bound learned receipt.
The shared `MissionAssuranceAgent` produces a proposal with a fixture backend,
explicitly `model_inference_invoked=false` for the **mission judge**. Learned
predictor invocations are separately recorded in `pad-advisory-events.jsonl`.

In assist mode, an occupied future sample can request another bounded wait.
Clear/unknown/stale/unavailable forecasts cannot override current Rules. Entry
still requires five observed clear seconds, fresh current pad/approach clearance,
stable AP hold, battery reserve, identity/clock continuity, and explicit simulator
approval. A proposal is neither permission nor dispatch. Interval collision
coverage and candidate-action conditioning remain unverified/false.

The worker requests advice at least two simulator seconds apart. The existing
90 simulator / 180 wall second waiting deadline and two-second response freshness
limit remain. Missing MissionOS communication retains the current AP hold until
bounded owned-SITL termination; this does not qualify real-aircraft failsafes.

## Verification and publication

```sh
python scripts/verify_yokohama_pad_advisory.py RUN --output RUN/advisory-verification.json
python scripts/verify_yokohama_pad_queue.py RUN --output RUN/pad-verification.json
python scripts/verify_yokohama_sitl.py RUN --output RUN/verification.json
python scripts/verify_yokohama_payload.py RUN --output RUN/payload-verification.json
```

Retain all attempts, live inputs, forecasts, shared judgments, responses and
executor receipts. Recompute forecasts from recorded RGB, and bind their model,
camera, clocks and request identities. Compare enabled proposals against the
same current observations with the advisory disabled; label this a paired
**offline decision counterfactual**, not a second flight or mission benefit.
Report zero decision changes if that is the observed result. Successful
integration alone does not establish reduced waiting, better safety or energy.

Model adoption remains optional pending practical benefit. Do not rename this
fixed-camera CPU auxiliary model to WAM/ANWM/VLA or relax the native profile's
separate action-conditioned forecast admission contract.
