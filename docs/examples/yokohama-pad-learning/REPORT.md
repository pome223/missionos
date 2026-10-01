# WAM post-training for waiting behind another delivery aircraft

**Native WAM post-training reduced image-wide error by about 81%, but erased the small lead aircraft. The result is not qualified for wait/go decisions.** Perfect images are unnecessary; avoiding a false-clear reading while traffic remains is the unresolved capability.

[Interactive comparison](index.html) · [Departure recording](videos/heldout-depart.mp4) · [Reentry recording](videos/heldout-reenter.mp4) · [Metrics](evaluation.json)

## What ran

Six CPU Gazebo sequences recorded 1,472 RGBD frames of departure, interrupted departure and reentry. Forty-five supervised pairs trained the existing WAM adaptation for 2,048 steps. VLA weights and flight Rules were unchanged.

A development-only ablation crossed 50/250 diffusion steps with 0.25/16-second requested horizons. At 16 seconds, image error remained 33.94/31.46; increasing sampling steps did not repair it. At 0.25 seconds, error was 13.01/11.49, but occupancy reading still failed. All three fixed development probes failed, triggering the predeclared training branch. Evaluation results never selected a checkpoint or triggered training.

Evaluation future records stayed off the training machine. However, the camera, object and path are shared: **two of four fresh evaluation RGB histories and all four future RGB targets have identical pixel content in training.** This is a new-timing test with repeated appearances, not independent unseen-scene generalization. The four previously inspected cases are reported separately as regression.

## Results

| Metric | Before | After |
|---|---:|---:|
| Fresh timing endpoint matches | 1/4 | 2/4 |
| Fresh false-clear readings | 1 | **2** |
| Fresh unknown readings | 2 | 0 |
| Fresh image-wide mean RGB error, 0–255 | 33.71 | 6.32 |
| Two fresh distinct-RGB-history cases: matches | 0/2 | 1/2 |
| Prior regression matches | 1/4 | 2/4 |
| Prior regression false-clear readings | 1 | **2** |

Current-image persistence matches 2/4 in each cohort. Superiority over ideal Rules is not required.

**Every after forecast is read as clear.** Background and pad-like structure recover, but the lead aircraft disappears or becomes unreadable. The correct departure answer therefore does not establish motion understanding. Decreased uncertainty is not a success when false-clear errors increase. The fixed endpoint qualification fails both minimum matches and zero false-clear criteria; even a pass would not authorize flight.

![Observed, before, after and actual future](fresh-comparison.png)

The update changed 47,851,808 permitted parameters, preserved the frozen base, and was verified after saving and reloading the adapter. Training took 237.00 seconds. After-request computation took 5.92–6.10 seconds; model load was separately 16.84 seconds. Transport and live AP dispatch timing were not measured. The new adapter is retained locally and not registered as a flight provider.

## Limits and next step

The camera is fixed; the lead is a scripted entity; the parcel is preplaced. This is not a second PX4 aircraft, demonstrated AP hold, unloading mechanism, battery measurement or physical flight. Videos replay actual 4 Hz camera frames without interpolated movement.

All forecasts concern a hold-only endpoint 16 seconds ahead. No interval occupancy, alternative movement forecast, MissionOS judgment, dispatch, delivery or return was executed here. AP-only sea travel and independent current-state Rules remain unchanged.

The next development target is preservation of the small lead aircraft's presence and location over short occupancy horizons. Region-weighted training was already tried and remained insufficient; simply improving image-wide error will not solve the observed failure. Interval occupancy and both candidate actions must qualify before connecting these weights to the existing wait/reobserve/enter loop.

## Cost and verification

Additional estimated cost **$0.682329**; cumulative **$18.532819 / $20**, not an invoice. Owned VM, disks and CPU capture container were deleted; CUDA allocated memory after work was zero.

23 native calls completed and 97 collected files were hash-verified. The local suite passed **3,576 tests, 2 skipped**. Reopen the prepared training/evaluation inputs, captured pose/time bindings, unchanged reader, actual target crops, native forecasts, development-only trigger and metrics without GPU:

```sh
python scripts/check_yokohama_pad_learning.py --bundle docs/examples/yokohama-pad-learning
```

[Protocol](learning-protocol.json) · [Weight-update receipt](results/summary.json) · [Cost/cleanup](cost.json) · [Evidence manifest](evidence-manifest.json) · [Maintainer contract and commands](../../agents/yokohama-pad-learning.md)

Prepared data, forecast/target images, measurement metadata, videos and executed sources are public. Weights and full raw RGBD recordings are retained locally. [Yokohama/Project PLATEAU attribution and licensing](../yokohama-urban-scene/ATTRIBUTION.md).
