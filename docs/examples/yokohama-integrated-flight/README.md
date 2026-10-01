# Integrated city VLA + WAM mission

Two fresh real AeroVLA/ANWM decisions affected short PX4/Gazebo movements in one sortie, followed by the delivery waypoint, return, landing and disarm. This is an end-to-end city simulation result, not physical payload delivery.

[Interactive replay](index.html) · [actual onboard camera video, 8×](onboard-timelapse.mp4) · [Japanese report](REPORT-ja.md) · [portable result](mission-result.json)

Run `yokohama-280d65f068d7` at `f79a199c64f52cb2faa8aa19f1babd1084e6b685` recorded 5,035 positions over 696.51 m and 903.61 simulator seconds. Seven 30-second holds passed.

| Site | Observed movement | Target error | VLA exchange | WAM exchange |
| --- | ---: | ---: | ---: | ---: |
| D1 | 1.74 m | 0.082 m | 17.15 s | 51.58 s |
| D2 | 2.86 m | 0.114 m | 16.65 s | 48.98 s |

The two VLA calls use real observations; synthetic startup warm-up is accounted for separately. Compact-city grammar v2 selects parameters of an already requested translation and excludes zero-forward/LAND output types. MissionOS, Rules and WAM retain reject authority. An additional retained native v1 trial generated `0 49 48</s>` and stopped before WAM; it remains a failed trial. The model controls two bounded short segments in an authored AP route. Its returned outputs remain unchanged; independent geometry Rules constrain dispatch. The previously trained motion-v4 adapter is now installed in the native WAM service. Four real forecasts passed the unchanged visible-structure gate. This does not establish generic obstacle recognition, free space behind occlusions, pixel-perfect prediction, unseen-scene generalization or superiority to ideal Rules. Prior offline 4/5 results remain unchanged.

Two retained failures exposed slow physical-versus-estimated yaw drift during startup/inference. Fresh observation anchors and a measured Gazebo-world-to-current-EKF executor mapping address that integration issue. Both physical world-facing and estimated command-facing arrival errors are checked. Simulator ground truth is used; this is not a qualified hardware heading estimator.

Models start after an inland AP hold and stop after the second model segment; late authority is rejected. The remaining authored route completes with AP. No sea leg exists in this trial, so this is not evidence of zero sea-leg inference in an integrated sea/city sortie. Payload release, moving ship, strong wind, fleets, physical execution and energy savings remain untested. Nearby recorded arrival images are shown separately from predictions; exact future-time alignment is not claimed.

## E2E / Runtime Verification

```sh
# SCENE_DEPS contains the optional scene dependencies. RUN is a new output directory.
PYTHONPATH=.:"$SCENE_DEPS" python scripts/yokohama_sitl.py \
  --phase flight --decision-backend native --wam-profile motion-v4 \
  --native-service-config "$MODEL_SERVICE_CONFIG" --approve-sitl \
  --output-dir "$RUN" --timeout-seconds 1500
PYTHONPATH=.:"$SCENE_DEPS" python scripts/verify_yokohama_sitl.py "$RUN" --output "$RUN/verification.json"
PYTHONPATH=.:"$SCENE_DEPS" python scripts/verify_yokohama_decisions.py "$RUN" --output "$RUN/decision-verification.json"
python docs/examples/yokohama-integrated-flight/verify_bundle.py
```


The boundary covers live CPU-rendered observations, real GPU model outputs, independent Rules, MAVLink ACKs, measured position and orientation, delivery waypoint, return/landing/disarm, shutdown and late-response rejection. CPU latency qualification used 170/10/55-second startup/VLA/WAM fixture delays; these are not model timings. All 3,412 repository tests passed. The public verifier recomputes image gates, motion facts and artifact hashes; it does not rerun models or reopen all private raw sensor bytes.

Integration cost including two failed native attempts: $1.6387 (final attempt $0.7077); cumulative with prior studies: **$14.7101/$15**. Owned VM and disks were deleted and absence confirmed. Invoice unconfirmed. See [cost receipt](cost.json), [retained failures](retained-failures.json), [protocol](protocol.json), and the [maintainer contract](../../agents/yokohama-native-flight.md).

The next integration is the AP-only sea round trip connected to this city mission. Source geometry: Yokohama / Project PLATEAU; see [attribution and license](../yokohama-urban-scene/ATTRIBUTION.md).
