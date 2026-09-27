# Yokohama repeated model-view experiment

Status: implementation and CPU qualification in progress. No native city result
is established by this contract. Historical ship/pillar runs are separate.

## Frozen scope

One stationary, wind-free, synthetic PLATEAU city world; one drone. The AP route
still supplies the approach, connectors, delivery waypoint and return. At D1
and D2, an observed 30-second hold precedes new sensor histories and a short
model-proposed movement. This is two model-controlled segments in an AP mission,
not a VLA-generated whole-city route. The delivery waypoint is not parcel release.
There is no offshore leg in this city-only qualification.

`--decision-backend fixture` uses explicit synthetic proposals and past-view
images; no learned model is invoked. `--decision-backend native` requires an
explicit private lifecycle configuration. Neither mode provisions a VM or
supports hardware endpoints. The disposable simulator retains `--network none`.

## Model and control boundary

1. The executor observes inland D1 hold before asking the host to start models.
   Downloading pinned weights may occur earlier; loading/warmup/inference may not.
   Start has a 180-second wall deadline. Native lifecycle receipts identify the
   actual remote processes, not merely an SSH tunnel.
2. Capture 24 exact 4 Hz RGB/depth/down timestamp joins. Retain the first eight
   as fixed startup data and use the following sixteen. Join each camera sample
   to measured Gazebo pose within 12 ms without interpolation. Preserve raw
   messages, pixels, poses, timestamps and hashes. Hold drift is at most 0.5 m,
   speed 0.3 m/s, heading drift 0.03 rad and reserve at least 20% throughout waits.
3. AeroVLA generates its native bins once with the explicit level-inspection
   grammar (vertical bins 47–51). Malformed, terminal, rotation-only, <0.5 m,
   >5 m or vertical displacement >0.205 m proposals reject without rewriting or
   retry. The AP reproduces the proposed endpoint; it does not reproduce the
   upstream low-level controller's exact timing or yaw-first motor sequence.
4. Capture a new sixteen-frame input history after VLA. WAM predicts the hold
   view and immutable VLA endpoint view with released weights, seed 42 and 250
   diffusion steps. Bind both predictions to the proposal, actual history,
   model/source identities and exact body-frame camera displacement. This uses
   simulator ego pose; it is not deployable visual odometry. The native model's
   four-step dynamic time calibration remains unverified, so only a stationary
   scene/view task is admitted. Each model exchange has a 75-second wall limit.
5. Reproject only the last past RGBD into the intended candidate view with an
   independent local z-buffer. Keep unobserved pixels unknown. In the native
   480×360 centre crop resized to 224×224, require known coverage ≥0.6, ≥100
   reference edge pixels, edge correspondence ≥0.55 within four pixels,
   luminance MAE ≤45 and predicted edge density ≤0.35. These are visible-structure
   consistency bounds, **not semantic building detection, metric future depth,
   free-space inference, or collision prediction**. A blank/noisy/unreadable view
   rejects. Neither service-returned projection images nor future outcomes are
   inputs to this host-side gate. Thresholds must be fixed before paid attempts.
6. Independent Rules require >2 m centreline clearance to all height-overlapping
   frozen building prisms for both the model segment and its AP connector. The
   model does not grant approval or clearance. Transfer the mission while holding;
   observe its ACK, then take a new observation and issue a separate single-use
   activation permit with a two-second validity. Observe the actual mode change,
   ≤1 m tracking tube, ≤0.25 m endpoint error, ≤0.15 m vertical error and ≤0.05 rad
   yaw error with speed ≤0.3 m/s for two seconds. ACK alone is not arrival.
7. Repeat at D2 with new inputs. Revoke the session, require remote process and
   CUDA compute-process absence, reject post-revocation authority, then continue
   the remaining AP route and observe launch-pad contact, landing and disarming.
   Models alternate CUDA residency; both return weights to CPU between requests.
   The response records CUDA allocated bytes after transfer, required to be zero.

PX4 EKF yaw and the rendered camera's true ENU yaw can differ under the simulator
magnetic-field settings. Use measured Gazebo camera orientation for interpreting
pixel-relative forward motion, and the original PX4 heading plus proposed yaw for
the AP yaw command. Preserve both headings; silently treating them as identical
rotates the physical movement. This simulator ground-truth dependency is explicit.

On failure, no further candidate is dispatched. Revoke model authority and stop
owned model processes; the enclosing runner tears down its owned simulator.
This bounded disposable-SITL abort is not a qualified physical landing/diversion.
No inference retry is allowed to replace an unfavorable output. Preserve failed
development and frozen attempts separately. There is no Rules-superiority gate.

## Runtime and verification

```sh
python scripts/yokohama_sitl.py --phase flight --decision-backend fixture \
  --approve-sitl --output-dir /tmp/new-yokohama-city-fixture --timeout-seconds 1300
python scripts/verify_yokohama_sitl.py /tmp/new-yokohama-city-fixture \
  --output /tmp/new-yokohama-city-fixture/verification.json
python scripts/verify_yokohama_decisions.py /tmp/new-yokohama-city-fixture \
  --output /tmp/new-yokohama-city-fixture/decision-verification.json
```

The Python environment needs the repository dependencies and the scene bundle's
pinned reproduction requirements. Native mode replaces `fixture` with `native`
and supplies `--native-service-config PATH`. That private JSON specifies
`start_argv`, `stop_argv`, `vla_port`, and `wam_port`; endpoints are IPv4 loopback.
Commands must own and verify their remote process lifetimes. An already-running
model endpoint is not sufficient evidence of inland startup or shutdown.

Claims about native participation require the reopened model/permit/motion chain
and the separate whole-trajectory/contact verifier. The fixture labels remain
false for native inference. No result establishes physical delivery, strong wind,
moving-deck recovery, ten-drone coordination or whole-mission battery savings.
