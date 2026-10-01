# Can native WAM images forecast delivery-pad availability?

**Four time-bound native forecasts completed, but this configuration did not preserve a recognizable lead aircraft and pad. None is qualified for a wait/advance decision.** A frozen scene-specific reader matched the future endpoint in 1/4 cases, returned unknown in 2/4, and incorrectly called a reoccupied pad clear in 1/4. The match is not proof of semantic understanding of generated images.

[Image comparison and recorded videos](index.html) · [Japanese report](REPORT-ja.md) · [Measurements](evaluation.json)

| Recorded condition | Actual endpoint, +16 sim seconds | Reader on WAM output |
|---|---|---|
| Departing | Clear | Unknown |
| Already departed | Clear | Clear |
| Stalled departure | Occupied | Unknown |
| Returning lead | Occupied | Clear, incorrect |

Current-image persistence matched 2/4. This is a small capability diagnostic, not a mission-benefit experiment or a requirement to beat ideal Rules. We inspected all original forecasts: generated texture replaces recognizable scene content. The reader is not qualified on this shifted image distribution. The unchanged background consistency check nevertheless passed 4/4, demonstrating why it must not serve as dynamic-object or collision evidence.

CPU Gazebo recorded 888 exact RGBD frames at 4 Hz with measured camera/lead poses. A static camera rig observes a scripted entity, not a second PX4 aircraft. A parcel is placed beforehand; no unloading mechanics or receipt is exercised. The first viewpoint attempt was abandoned when the ascending lead left view and is retained in [attempts.json](attempts.json). The revised authored camera is 10 m above the pad; an AP arrival/hold at this viewpoint has not been verified.

A grayscale high-pass template reader was fitted only on 192 development frames. Of the 696 evaluation frames, 27 boundary frames had uncertain ground truth and were excluded from known-class recall. Occupied: 387/420 correct, 33 unknown, zero false clear; clear: 249/249 correct. These are correlated frames from the same camera, lead and corridor with different timing, not independent trials or new-scene generalization. WAM weights were not updated.

Each native request contains only 16 past RGBD/camera-pose frames, one zero-displacement hold candidate, fixed seed 42, 50 diffusion steps and frame offset 64. Future targets, lead positions and schedules were withheld. Exact acquisition timestamps bind the target 16 simulator seconds later. Endpoint images do not establish interval safety. VLA, MissionAssuranceAgent, aircraft dispatch, native same-flight decisions and physical execution were not invoked.

The [pinned upstream time input](https://github.com/EmbodiedCity/ANWM.code/blob/657a80268505fa9149c4df502e35aa0f5bce11e5/anwm/rollout.py#L30-L36) is a frame offset, not seconds. At 4 Hz, offset 64 maps to a requested 16 seconds. Existing motion-v4 adaptation used time value 1 for one-second static-world targets; it has no demonstrated dynamic-time calibration. This trial changes both the former 250-step sampler and former time value 1. It cannot isolate sampler reduction, time conditioning, viewpoint shift or lack of dynamic adaptation as the cause. No threshold relaxation, resampling or tuning on these four cases followed evaluation.

Warm request computation was 5.66–7.45 wall seconds; model load was separately 16.68 seconds. Transport/lifecycle costs are excluded. Comparing these times to 16 simulator seconds assumes real-time simulation; no live AP deadline was measured, and unusable imagery does not constitute useful low-latency performance.

The next development diagnostic should isolate sampling steps and time conditioning before dynamic post-training. Any post-training must use a consistent frame/time contract and leave new movements/timings out of training. The existing [prediction-to-judgment adapter](../yokohama-pad-prediction/REPORT-ja.md) still requires both candidate forecasts, qualified semantics, calibrated horizon and interval conflict evidence. This endpoint-only trial cannot satisfy it.

New conservative cost estimate: **$0.480**; cumulative **$17.8505 / $20**, not an invoice. CUDA allocated memory was zero after work; owned VM/disks and capture containers were removed. [Cost](cost.json), [shutdown](results/shutdown.json). Actual camera videos have no battery overlay because this was not an aircraft flight.

CPU-only reproduction:

```sh
python scripts/probe_yokohama_pad_temporal.py \
  --inputs docs/examples/yokohama-pad-temporal/prepared/inputs \
  --output /tmp/unused-pad-temporal
python scripts/check_yokohama_pad_temporal.py \
  --bundle docs/examples/yokohama-pad-temporal
```

Initial full local suite: 3571 passed, 2 skipped, 3 warnings. CI exposed CPU-dependent background-reconstruction statistics with identical occupancy results and gate outcomes. The replay repair freezes the original diagnostic reference/mask, bound to input hashes, instead of relaxing comparison tolerances or changing the original evaluation. See [portability evidence](portability.json), [runtime contract and exact acquisition/native commands](../../agents/yokohama-pad-temporal.md) and [evidence manifest](evidence-manifest.json). Models/private cloud details are excluded. Source city data: Yokohama / Project PLATEAU, modified, [attribution](../yokohama-urban-scene/ATTRIBUTION.md).
