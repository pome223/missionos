# Occupied delivery pad: native VLA/WAM image probe

The fixed-weight native probe did **not qualify for flight integration**. AeroVLA proposed forward motion in all four observations, including an occupied pad. The three near-view checks were **1/3** correct (busy, clear, reoccupied); the repeated occupied image is identical, so these are not three independent generalization cases. Native texts were `55 49 49`, `58 49 49`, `55 49 49`, `58 49 49`. The returned bins were preserved.

[Side-by-side images](index.html) · [Japanese report](REPORT-ja.md) · [Reopened I/O verification](evaluation.json)

A CPU Gazebo camera rig, using the aircraft's front RGBD/downward-camera geometry, recorded 24 exact 4 Hz joins per case; the last 16 formed each WAM input. The near rig was 20 m horizontally from the pad and 5 m above its datum. Its position was authored and observed, not reached by PX4. The connecting geometry had 9.455 m minimum building-footprint clearance; this is not AP or flight-feasibility evidence. The lead aircraft and parcel remained scripted entities. The previous complete CPU delivery flight remains [separate](../yokohama-pad-queue/REPORT.md).

Native AeroVLA ran four times, with the same prompt and the same zero-or-1-to-3-m forward grammar for every case. Neither current occupancy labels, lead poses, nor future actor schedules entered the model input. Native ANWM generated eight candidate views, binding hold and the unchanged VLA output. The motion-v4 weights, index 1, seed 42 and 250 diffusion steps were fixed. No training, AP dispatch, native same-sortie flight, receipt, dynamic-time prediction or physical execution occurred.

The WAM gate has a separate input-contract problem: near-view known geometry covers about 45%, below the frozen 60% requirement. A perfect reference compared with itself also fails, so this is not evidence that WAM prediction quality caused rejection. Other near-view image bounds were within their existing limits; thresholds were not relaxed. This eligibility issue was diagnosed after GPU startup. A new CPU [input screen](input-screen.json) catches it before another paid flight qualification. The reference is past-only RGBD reprojection, not a subsequently flown actual view. The predicted images must not authorize pad entry or be called a validated departure-time forecast.

The generation timers omit model startup, CPU/GPU residency transfer and transport; they are not end-to-end flight-response latencies. Both model processes ended, CUDA process absence was recorded, and the owned VM/disk were deleted. The conservative additional estimate is **$0.665**, cumulative **$17.371 / $20**, not an invoice.

If adding a learned wait decision, the next training target is VLA's task-appropriate wait/forward selection on separately collected scene variations, with unseen positions/timings reserved for evaluation. Do not train on these four diagnostic cases and call memorization improvement. WAM needs an observable pad view and a measured camera-pose contract before learning or a new flight qualification. MissionOS proposals, Rules, dispatch, arrival and independent cargo receipt remain separate. Ideal Rules superiority is not an acceptance condition.

Runtime commands are preserved in [the Japanese report](REPORT-ja.md). Public input histories and native results allow `python scripts/evaluate_yokohama_pad_models.py --inputs docs/examples/yokohama-pad-native-probe/inputs --results docs/examples/yokohama-pad-native-probe/results --output NEW.json` to reopen the result without a GPU. `screen_yokohama_pad_models.py` exits **2**, as expected, on this ineligible reference.

Source: [Yokohama / Project PLATEAU attribution](../yokohama-urban-scene/ATTRIBUTION.md), CC BY 4.0, modified synthetic scene.
