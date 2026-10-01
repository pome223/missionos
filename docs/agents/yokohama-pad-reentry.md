# Returning-lead advisory: CPU capture and decision replay

This experiment diagnoses and optionally adds **waiting advice** for a lead aircraft
moving back toward a delivery pad. It is a learned CPU object/state model, **not
native ANWM or VLA**, and uses the shared MissionAssuranceAgent with a deterministic
judge. It does not authorize flight or demonstrate delivery improvement.

## Authority and scope

`reentry_risk_advisory: true` in the existing `pad_state_advisory` config enables
an additional optional reason, `possible_reentry_reobserve`. The default remains
off. This experiment does not expose or enable the option in the flight CLI and
does not replace its registered weights. Existing flight configurations and
unknown/current-Rules fallback retain their previous behavior.

The incoming forecast must pass the existing identity, input-support, freshness,
17-sample timestamp and authority checks. Sixteen supported image localizations
must show at least 0.25 m of inward radial movement in the last nominal second.
At least one still-future sampled center must enter the current pad exclusion
radius (6 m in this scene). Current actor telemetry must still place the lead
outside that radius. Missing/nonfinite geometry or contradictory support is an
invalid response; unsupported/stale forecasts retain the existing fallback.

This means **possible future entry**, not confirmed occupancy, probability,
continuous collision prediction or action-conditioned world simulation. It does
not modify `clear/occupied/unknown` labels, authorize entry, keep an expired
forecast alive or let a forecast clear current occupancy. Assist mode may only
turn entry advice into a wait; shadow mode leaves current Rules unchanged.
Rechecks use the existing 2 s cadence and bounded city hold contract. The shared
judge emits a proposal, and `require_response` independently checks it. No motors,
PX4 setpoints, dispatch, landing, receipt or battery consumption are exercised by
this offline replay. Late/new occupancy and actual AP interruption recovery after
entry still require an opt-in flight test.

## Frozen sequence

1. `run_yokohama_pad_reentry.py freeze` specifies training/development/evaluation
   timings and comparison endpoints. `replay-protocol.json` separately freezes
   all five arrival times before training or viewing the new evaluation output.
2. `capture --approve-sitl` records an existing calibrated static rig and scripted
   lead with software rendering in an owned CPU-only, network-none container.
   Raw RGB, measured actor/rig poses and clocks are recorded; owned resources are
   removed. The authored schedule is scoring/acquisition data only.
3. `train` adds only `train-return` to the original training split and
   `development-return` to its selection split. The original CPU algorithm is
   unchanged. The model is frozen/reloaded before evaluation records are loaded.
   The older `state-*` records are now diagnostic/regression data, not fresh tests.
4. The first evaluation compares current Rules, original/retrained forecast,
   image-localization constant velocity and a future-pose oracle. Its negative
   result remains in `yokohama-pad-reentry-learning`.
5. After diagnosing that result, `freeze-followup` fixes the possible-entry policy
   sources and three fresh timings **before acquisition**. It reuses the previously
   frozen post-trained weights, without further training. It also retains all
   five predeclared arrivals. The follow-up is a narrow timing generalization test
   with the same camera, appearance and corridor, not broad real-world validation.

## Replay boundaries and scoring

- The predictor sees exactly 16 past 320×180 RGB arrays, camera poses and 4 Hz
  timestamps. Actor positions, future frames, case IDs and motion schedules are
  not passed to `Model.predict`.
- `ReplayHost` replaces file acquisition with the actual CPU forecast from those
  arrays. It reuses production `respond`, MissionAssuranceAgent, `propose`,
  `summarize`, `selected_action` and `require_response`. The saved shared judgment
  is hash-bound to the saved response.
- Current pad/approach facts use measured Gazebo lead poses. **Own-aircraft pose,
  hold, arming, battery and approval are explicitly synthesized replay fixtures**;
  `wall_s` follows recorded time, not a live communication latency measurement.
- A window requires five recorded seconds of current clearance. Each method sees
  identical observations and independent current Rules. The oracle sees the next
  four seconds only for the comparison upper bound, never model input.
- The primary episode endpoint is whether a first permitted entry is followed by
  sampled pad/approach reoccupation in four seconds. Replay stops at that proposal;
  it is **not a collision count, actual interruption, avoided crash or mission
  success metric**. Five arrival times per new sequence are correlated conditions,
  not fifteen independent flights. Windows overlap and are not independent trials.
- The constant-velocity comparator shares the original image localizations and
  support gate. No requirement to outperform ideal Rules or fixed 90% accuracy is
  used. Training loss, supported-frame count and dispatch integration alone do not
  establish mission benefit. Small useful advice can qualify for further opt-in
  validation; no default model replacement occurs here.

## Runtime reproduction and public evidence

```sh
python scripts/run_yokohama_pad_reentry.py freeze --root FIRST
python scripts/run_yokohama_pad_reentry.py capture --root FIRST --source-rig RIG --approve-sitl
python scripts/run_yokohama_pad_reentry.py train --root FIRST
python scripts/evaluate_yokohama_pad_reentry.py --root FIRST --output FIRST/evaluation
python scripts/run_yokohama_pad_reentry.py freeze-followup --root SECOND --parent FIRST
python scripts/run_yokohama_pad_reentry.py capture --root SECOND --source-rig RIG --approve-sitl
python scripts/evaluate_yokohama_pad_reentry.py --root SECOND --output SECOND/evaluation
python scripts/check_yokohama_pad_reentry.py --bundle docs/examples/yokohama-pad-reentry-learning
python scripts/check_yokohama_pad_reentry.py --bundle docs/examples/yokohama-pad-reentry
```

`RIG` is a previously verified raw static camera capture with `assets`, `models`
and world hash, not the reduced public example. Never overwrite prior attempts.
The public checker requires no simulator or GPU: it verifies public hashes,
source/protocol identities, capture pose/time/target bindings, recomputes RGB-only
forecasts and episode metrics and rechecks saved production judgment/Rules receipts.
The raw acquisition remains in a durable experiment archive. Public videos are
fixed-camera recordings, not self-aircraft flight videos; no battery overlay is
invented. The static replay uses the recorded actor radius and separately labels
hindsight future truth and model predictions.
