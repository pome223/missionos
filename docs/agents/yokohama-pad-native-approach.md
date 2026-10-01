# Native VLA and WAM on the occupied-pad approach

Stage 1 of joining the occupied-pad wait with the native city models. After
the pose-Rules wait clears, one extra D3 model step moves the aircraft toward
the pad; the rest of the approach, cargo release and ship return stay AP-only.
It is an opt-in SITL integration. It does not make VLA or WAM decide to wait.

```sh
python scripts/yokohama_sitl.py --phase flight --approve-sitl --output-dir RUN \
  --sea-round-trip --deliver-payload --occupied-pad \
  --decision-backend fixture --wam-profile motion-v4 --pad-approach-decision \
  --timeout-seconds 3000
python scripts/verify_yokohama_decisions.py RUN --output RUN/decision-verification.json
python scripts/verify_yokohama_pad_queue.py RUN --output RUN/pad-verification.json
```

`--decision-backend native --native-service-config SERVICE` replaces the
fixture with the pinned AeroVLA and motion-v4 ANWM services.
`--pad-approach-decision` requires `--occupied-pad`, a decision backend, the
motion-v4 profile, and no pad-state advisory, hold recovery or paired capture.
Without it, `--occupied-pad` with a model backend runs the city-only mode below.

## City models with the Rules-plus-advisory queue

The models make only the D1/D2 building-constrained steps. The session is
revoked and the models are stopped at D2, before the pad is reported occupied.
Waiting is decided by the pose Rules and the MissionOS fixture judge. The
optional CPU pad-state advisory (`--pad-state-advisory assist`) can only change
the proposal to `wait_at_current_hold`. It can never admit entry.

```sh
python scripts/yokohama_sitl.py --phase flight --approve-sitl --output-dir RUN \
  --sea-round-trip --deliver-payload --occupied-pad --pad-state-advisory assist \
  --decision-backend native --native-service-config SERVICE --wam-profile motion-v4 \
  --timeout-seconds 3000
```

The mode requires cargo delivery, zero wind and the motion-v4 profile. Proposal
size is bounded from the proposal observation, as described below.
`verify_yokohama_pad_queue.py` replaces its no-models check with
`city_models_stopped_before_pad_wait`:

- `city_session_revoked` must come before `pad_occupied_reported`;
- no non-stop `city_request` may follow the pad report.

Run `verify_yokohama_decisions.py`, `verify_yokohama_pad_advisory.py`,
`verify_yokohama_payload.py` and `verify_yokohama_sitl.py` on the same run.

### Observed native run (2026-09-29, `yokohama-64b4376e193b`)

The run used one L4 cloud GPU for the model services. The GPU was deleted after
the D2 model stop was verified. Delivery and return were AP-only on CPU.

| Step | Observation |
|---|---|
| D1, D2 | Native VLA about 17 s, WAM about 50 s per cycle. Both candidates passed the unchanged city WAM gate. Final target error was 0.14 m and 0.09 m. |
| Pad wait | Pad reported occupied at 934 s. Rules reaffirmed the wait about every 2 s until entry was granted at 970 s. |
| Advisory | 15 inference calls, 13 supported. The advisory changed 0 actions compared with the Rules proposal on the same observations. |
| Delivery, return | One release, one receipt, then a landed and disarmed ship return. |

All five verifiers passed: decisions, pad_queue (19 checks), pad_advisory,
payload and sitl (21 checks). The estimated incremental cloud cost was $0.50.

Limits of this run:

- It shows that the combination runs end to end. It does not show that the
  advisory adds safety or saves time, because the Rules were already waiting.
- The WAM gate checks visible structure consistency only. It does not predict
  free space or collisions.
- The lead aircraft is scripted and occupancy is pose-based.
- This is a single simulator run, not physical execution.

Flight evidence is kept outside the repository.

## Authority split

- **Wait:** unchanged pose Rules and the deterministic MissionOS fixture judge
  (`yokohama_pad_queue.propose`). The compact AeroVLA grammar has no hold bin
  and cannot propose waiting. Never report the wait as a VLA or WAM decision.
- **Propose:** after the first entry permission, AeroVLA proposes one short
  level step (0.5–5 m) from a fresh D3 observation. Output is never rewritten.
- **Forecast:** ANWM predicts hold and candidate views from a new history.
  The pad-view gate below must pass for both.
- **Constrain:** host Rules require mapped building clearance, a currently
  clear pad and approach, an endpoint within 1 m laterally of the
  wait-to-approach segment, and progress toward the approach point, at both
  authorization and activation.
- **Reconfirm:** the first entry permission predates roughly a minute of model
  exchange, and the executor rejects permissions older than 30 s. Just before
  model authority, and again at the reached endpoint before the delivery
  connector, the aircraft collects a new five-second clear window and receives
  a new MissionOS response. Any reoccupation is fatal; there is no retry.
- **Execute:** the executor dispatch check requires a fresh permission issued
  at the current hold. The pad guard revokes continuation on reoccupation or
  separation loss at every sample. Models stop after the D3 step, before any
  cargo release.

In this mode `size_bound_origin` is `proposal_observation`: the 0.5–5.01 m and
0.205 m vertical proposal bounds are measured from the observation the VLA saw,
recorded as `proposal_origin_world_xyz_m`. Mapped clearance is still checked on
the leg flown from the current held position, and hold drift during inference
remains capped at 0.5 m. A CPU run with model-scale latency (retained as
`fixture-02-latency`) otherwise failed at D2 when 56 s of AP hold sag (0.209 m)
was attributed to the model. Existing modes keep the original bound.

The reached endpoint becomes the pad hold only if it is the consumed model
permit's target and within the 5.01 m model-translation bound of the authored
wait point (`approved_hold`). Requests made at a moved hold carry
`hold_xyz_m`, which is inside the request digest.

## Pad-view WAM gate (`pad_approach_structure.v1`)

Harbour views near the pad are about half sky. Sky has no depth, so the city
gate's full-frame `known_pixel_fraction >= 0.6` fails even for the exact
reference image (0.45–0.52 on the recorded probe views). The pad profile:

- replaces full-frame coverage with the share of the hold view's visible
  structure still known at the candidate pose (`>= 0.9`), plus a 0.3 full-frame
  floor;
- keeps the edge-count, matched-edge, luminance and edge-density bounds
  unchanged;
- is admissible only if those same bounds reject a mirrored and a uniform image
  of the same reference; otherwise it fails closed.

Calibration used reference self-checks and synthetic controls on
`docs/examples/yokohama-pad-native-probe/inputs` before any new native output.
On the far (wait-point) view, the exact reference passes for 0–5 m steps while
mirrored, uniform and black images fail. On near (20 m) views a mirrored image
passes the unchanged bounds, so those views fail closed. The bounds do not
reject an 8-pixel shift. Afterwards, the eight existing probe forecasts were
reopened: both far-view forecasts pass and all six near-view forecasts fail
closed. Treat a pass only as coarse scene consistency: it does not show that
WAM forecast the lead aircraft, free space or collisions correctly.

## Latency and fault injection (CPU only)

`--fixture-cold-start` adds the measured-scale model delays to the fixture
(170 s start, 10 s VLA, 55 s WAM), so the first entry permission expires before
model authority and only a fresh reconfirmation can admit the D3 step.
`--fault-lead-return-on-d3-wam` (fixture pad approach only) starts flying the
departed lead back over the pad when the D3 WAM request is issued, so the
reoccupation lands inside model inference rather than at an assumed time. It is
recorded as `fault_lead_return` in `config.json` and as a
`fault_lead_return_triggered` event.

Pass conditions are explicit. Latency success: the first entry permission is
older than 30 s at the pre-authority reconfirmation, a fresh permission admits
the D3 step, and delivery and return complete. Fault: a reoccupied observation
is recorded after the D3 WAM request and before any D3 WAM response, the run
stops with a pad-reoccupation reason, no D3 upload or dispatch exists, and the
model session and simulator are released (`verify_yokohama_pad_approach_fault.py`).
A failed flight alone is not a passing fault test.

After the `proposal_observation` bound was introduced, the latency run was
repeated on CPU (fixture backend, `--fixture-cold-start`, run
`yokohama-c1b1328b77fb`):

- D1, D2 and D3 all arrived, with final target errors of 0.10, 0.07 and 0.10 m.
- The first entry permission was 85 s old at the pre-authority
  reconfirmation. A fresh permission admitted the D3 step.
- The second reconfirmation passed at the reached endpoint.
- Delivery and a landed, disarmed ship return completed.
- The decisions, pad_queue (23 checks), payload and sitl verifiers passed.

The pad-state advisory is not part of this mode.

An independent reopening of the same run passed all four verifiers and eleven
latency-specific checks. The initial permit was 85.20 wall seconds old when
replaced; the D3 and delivery dispatch checks used their latest permit IDs at
ages 3.45 and 3.25 wall seconds. Replaying the recorded D3 dispatch observation
through `PadQueue.require_dispatch` rejects the expired initial permit and
accepts its replacement, without sending a flight command. The
[Japanese report](../examples/yokohama-pad-d3-latency/REPORT-ja.md) and
[qualification receipt](../examples/yokohama-pad-d3-latency/qualification.json)
retain the timings, verifier checks and hashes of the private raw evidence.

## Verification

`verify_yokohama_decisions.py` reopens every configured cycle, including the
pad-view gate, pad Rules and corridor for D3, and requires models to stop
before the AP continuation after the last model phase. `verify_yokohama_pad_queue.py`
additionally requires wait → entry → two reconfirmations in order, the moved
hold equal to the consumed model endpoint, and every dispatch check within
30 s of a fresh grant. Neither verifier re-runs models.
