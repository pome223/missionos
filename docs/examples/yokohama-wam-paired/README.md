# Yokohama withheld-view WAM diagnostic

**All 12 real ANWM forecasts failed the predeclared appearance criteria.** Preserving depthless sky appearance improved the projected condition, but the learned outputs still introduced water and/or changed building structure. No new native VLA/WAM flight was admitted.

[Interactive comparison](index.html) · [Contact sheet](contact-sheet.png) · [AP recording](onboard-ap-timelapse.mp4) · [Japanese report](REPORT-ja.md) · [Protocol](protocol.json)

A CPU fixture flight collected four withheld outcomes: D1/D2 hold at +1 observed second, and the first frame after AP endpoint settling, +16.248/+16.748 seconds after the respective inputs. Future images stayed local and were never uploaded to the GPU. Targets came from the original commands, not measured future poses. Endpoint camera errors were 0.136/0.109 m and 0.0237/0.00522 rad, within the unchanged 0.30 m / 0.05 rad qualification bounds.

The AP completed two fixed-candidate movements, seven holds, the delivery waypoint, return, landing and disarm. The 68.25-second recording contains 273 actual Gazebo camera frames at an effective 8.87× speed. This is **fixture/AP execution plus separate offline WAM inference**, not learned-model control. No real VLA was invoked, and no payload release or physical execution occurred.

The first otherwise successful flight failed view pairing because D1 camera rotation error was 0.113 rad. Subsequent compass-parameter attempts stopped before arming or failed physical arrival yaw. All seven attempts are retained in [cpu-attempts.json](cpu-attempts.json). The final bounded simulation uses one pre-arm heading initialization from the authored spawn orientation and init-only magnetometer mode, then PX4 inertial/GNSS estimation. This is not an upstream magnetometer repair, continuous ground-truth control or hardware-heading validation. The [maintainer contract](../../agents/yokohama-paired-views.md) records exact source references and limits.

## Frozen results

Original projection/t4, appearance-only fill/t4 and fill/t1 used the same released checkpoint, 16-frame history, seed 42 and 250 diffusion steps. Filling uses only past RGB and positive-infinite depth on upward rays; finite geometry is immutable. Its mask never becomes depth, visibility or free-space authority.

| Pair | Projection | t | Projection MAE | WAM MAE | Error >40 | Result |
|---|---|---:|---:|---:|---:|---|
| D1-hold | original | 4 | 52.31 | 43.26 | 45.4% | fail |
| D1-hold | appearance | 4 | 2.69 | 38.91 | 28.5% | fail |
| D1-hold | appearance | 1 | 2.69 | 36.64 | 26.0% | fail |
| D1-vla | original | 4 | 53.67 | 44.73 | 46.1% | fail |
| D1-vla | appearance | 4 | 8.74 | 42.43 | 33.4% | fail |
| D1-vla | appearance | 1 | 8.74 | 40.42 | 30.7% | fail |
| D2-hold | original | 4 | 21.46 | 39.73 | 43.2% | fail |
| D2-hold | appearance | 4 | 1.45 | 55.43 | 48.5% | fail |
| D2-hold | appearance | 1 | 1.45 | 42.47 | 36.1% | fail |
| D2-vla | original | 4 | 20.78 | 44.38 | 53.0% | fail |
| D2-vla | appearance | 4 | 1.72 | 58.52 | 53.0% | fail |
| D2-vla | appearance | 1 | 1.72 | 50.62 | 47.3% | fail |

Errors compare 224-square RGB forecasts to withheld actual views. Bounds: RGB MAE ≤15; ≤10% of pixels with any channel error >40; inference ≤75 s. Latency was 19.6–20.9 s, but every image failed the numeric bounds and visual inspection. Filled projection MAE was 1.45–8.74; generated-image MAE remained 36.64–58.52. Missing sky alone does not explain the failure. Model/domain compatibility and temporal conditioning remain unresolved; no unique root cause or general model incapability is established. These are four correlated view pairs, not a benchmark success rate or a comparison against idealized Rules.

`t1/t4` are dataset-index offsets; correspondence to physical seconds is unverified. No time-zero reconstruction is admitted as future prediction. See the [paper](https://arxiv.org/abs/2512.21887) and [pinned loader](https://github.com/EmbodiedCity/ANWM.code/blob/657a80268505fa9149c4df502e35aa0f5bce11e5/anwm/data/airvln.py).

## E2E / Runtime Verification

Source `20e5c0c2`; 3,355 tests passed with three dependency warnings. `$RUN` below is a new retained run directory and `$SCENE_DEPS` denotes optional scene dependencies.

```sh
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src:"$SCENE_DEPS" python scripts/yokohama_sitl.py --phase flight --decision-backend fixture --capture-paired-views --approve-sitl --output-dir "$RUN" --timeout-seconds 1300
python scripts/verify_yokohama_sitl.py "$RUN" --output "$RUN/sitl-verification.json"
python scripts/verify_yokohama_decisions.py "$RUN" --output "$RUN/decision-verification.json"
python scripts/verify_yokohama_paired.py --root "$RUN" --export-dir "$EXPORT"
```

Observed boundary: isolated software-rendered PX4/Gazebo → one-time initial heading → fresh histories and withheld views → independent Rules → fresh upload/activation authority → measured short motion → revocation → AP completion and teardown. Run `yokohama-e13923f20e45` passed all three verifiers. The GPU diagnostic started after capture-only qualification while the remaining AP route ran; final qualification reopened identical pair data after completion. This does not make offline WAM inference part of that flight's control loop.

GPU boundary: hash-bound past-only inputs → pinned real ANWM wrapper → 12 generated views → local withheld-image evaluation → evidence collection → VM/disk deletion. Exact remote command: `wam-venv/bin/python -u diagnose.py`. [Executed diagnostic](diagnose.py), [appearance function](paired_appearance.py), [native helper](ship_anwm.py), [model loader](ship_anwm_server.py), [inference receipt](inference-summary.json).

Recompute public image errors and hashes without GPU:

```sh
python docs/examples/yokohama-wam-paired/verify_bundle.py
```

Raw sensor archives, model weights, inference input archives and private cloud configuration are not included. Published receipts/hashes do not independently reproduce the entire raw flight or rerun inference. [Checks](checks.json), [SITL verifier](sitl-verification.json), [decision verifier](decision-verification.json), [final pair verifier](paired-final-verification.json).

Additional estimated cost **$0.4348**, cumulative **$10.8782 / $15**, invoice unconfirmed. Owned GPU VM/disks and SITL containers were confirmed absent. [Closed receipt](budget.json). No sea leg, moving deck, strong wind, fleet operation or energy savings are claimed.

City data: Yokohama / Project PLATEAU, 2024 public catalog, processed, CC BY 4.0. [Attribution](../yokohama-urban-scene/ATTRIBUTION.md).
