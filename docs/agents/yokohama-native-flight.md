# Yokohama repeated model-view experiment

Status: the original CPU qualification passed; the first native city attempt
stopped at D1 before dispatch. Its VLA output was `79 48 0</s>` (rotation-only),
and 9,568,256 CUDA bytes remained after CPU transfer. WAM was started but never
invoked on a flight observation. The revised, separately qualified condition
below adds cyclic-reference collection and an explicit short-segment decoder.
That revised trial also rejected residual CUDA memory. Neither failure is reclassified.
A diagnostic found no live model CUDA tensors: cuBLAS/Lt workspaces survive
CPU transfer, garbage collection and empty_cache. The current revision explicitly
synchronizes and clears these workspaces before measuring allocation, using the
[PyTorch 2.9 documented private API](https://docs.pytorch.org/docs/2.9/notes/cuda.html#cublas-workspaces).
A missing API rejects. Actual process shutdown is still required; zero allocator
bytes does not mean zero CUDA-context memory or zero power. Historical ship/pillar
runs are separate.

The corrected run `yokohama-e9da88bd375a` on `fa0d0d21` reached actual VLA
and WAM inference with zero post-request CUDA allocation. Both forecast candidates
failed the unchanged image bounds; no model segment was dispatched. See the
[retained result and all attempts](../examples/yokohama-native-flight/REPORT-ja.md).
Repeated native city movement and return remain unverified.

A [six-case offline diagnostic](../examples/yokohama-wam-diagnosis/REPORT-ja.md)
reproduced the original forecast byte for byte and isolated strong dependence
on projection gaps and time conditioning. Full observed RGB at time 4 passed
the existing numeric bounds while still visibly inventing water. These bounds
alone are insufficient admission evidence. Full observed RGB at time 0 is only
a static identity diagnostic; do not promote it to a moving-view/future forecast
fix or modify the frozen trial result. No production behavior changed.

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
   grammar (vertical bins 47–51) and `--short-segment-flight` yaw bins 38–60
   (absolute yaw <0.25 rad). This optional generation mask matches this phase's
   pre-existing translation envelope; it does not rewrite returned text. All
   forward bins and terminal proposals remain available, and terminal proposals
   still reject. It is a bounded model choice, not whole-route planning.
   Malformed, terminal, rotation-only, <0.5 m,
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
   Record allocation before cyclic-reference collection and after cache release;
   process shutdown must still be verified separately. Collection is not evidence
   of power savings, and a CPU lifetime double cannot prove real CUDA release.

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

## Opt-in motion-v4 mission integration

`yokohama_sitl.py --wam-profile motion-v4` selects an opt-in city-flight
profile. Legacy trials retain their original contracts and negative findings.
The objective is two fresh, observed model-to-AP updates followed by the authored
city delivery waypoint, return, landing and disarm. Pixel-perfect reconstruction
and superiority over Rules are not acceptance conditions. This profile still has
no sea leg, ship deck, parcel-release actuator, moving obstacles or hardware.

- ANWM loads the pinned `motion-v4` adapter (SHA-256
  `e4b88ec38ce8f08ff90b692a3b545e573aadaacf0f080a0357409dfc2b308dd3`)
  only with `--motion-adapter`. Its 24 finite tensors must match the named final
  block/head parameters and the released base checkpoint. No online training.
- `yokohama_anwm_request.v2` binds this adapter, model time index **1**, and the
  past-only appearance policy. These match the offline adaptation inputs.
  Physical arrival after precisely one second remains **unverified**: the AP
  executes a positional segment and verifies its actual arrival independently.
- Inputs add a boolean `last_depth_infinite` mask from the last **observed** depth
  image. Only positive-infinite, upward world rays can contribute sky appearance.
  NaN/zero depth, occlusion holes and inferred empty space cannot. Existing metric
  geometry is immutable; appearance is never added to the collision map.
- AeroVLA uses `--compact-city-flight`: generation-time forward bins 20–58
  (1.0204–2.9592 m), yaw bins 45–53 (±0.0898 rad), vertical bins 47–51.
  Its v2 grammar proposes parameters for an already requested translation;
  zero translation and terminal proposals are outside this phase's output type.
  MissionOS and independent Rules/WAM guards retain hold/reject authority.
  Native returned values are not rewritten. This limits initial integration to
  short steps near the adapter's training displacement; it is not unconstrained
  VLA navigation.
- Service health binds adapter, helper source hashes, decoding ranges and time
  index. Mixing the new request with legacy weights, or vice versa, fails before
  CUDA inference. Remote helper files `yokohama_appearance.py` and
  `yokohama_wam_profile.py` must accompany `ship_anwm_server.py`.
- The existing visible-structure gate is **unchanged**. Independent Rules still
  constrain both the native step and its AP connector; uncertainty still stops
  the disposable simulation. Successful image generation alone is insufficient.
- Startup is permitted only after the measured D1 hold. The D2 arrival/decision
  uses new captures. After the second model segment, revoke authority and observe
  remote process release before AP continuation; delayed authorization is rejected.

CPU fixture qualification and native GPU flight are separate receipts. Require
`verify_yokohama_sitl.py` and `verify_yokohama_decisions.py` to pass on the actual
native run before claiming this integrated profile flew. The latter reopens the
input mask, unchanged VLA output, generated predictions, permits and observed AP
arrivals; the offline adaptation's image scores are not substituted for them.

### Cold-start lifecycle correction

The first motion-v4 native attempt (`yokohama-69a58ecbd93a`) stopped before any
real-observation model request. During its 169.84-second service startup, true
Gazebo heading slowly changed; the pre-start camera-anchor guard rejected a
0.03008-radian change after 65.21 seconds, while position moved only 0.05773 m
and the EKF heading/reset counters remained stable. Shutdown queued behind startup
then timed out, masking that primary failure in the worker result. Retain this
attempt as failed; it is not a forecast-quality measurement.

The corrected motion-v4 lifecycle maintains AP mode, arming, position, reserve,
velocity and estimator checks while starting/stopping services. A camera-view
anchor is not used before any observation-bound judgment exists. After startup,
use a fresh anchor and capture. Refresh again after VLA and at the fresh WAM
capture. Subsequent CPU latency qualification also exposed yaw drift during
inference; use the world-frame mapping below. This corrects anchor lifetime; it does not
repair the simulator's physical-versus-estimated yaw drift or prove hardware yaw.
Primary exceptions are retained even when cleanup also fails.

The motion-v4 startup deadline is 300 seconds. CPU qualification can exercise
actual AP holding with `--decision-backend fixture --wam-profile motion-v4
--fixture-cold-start`: 170 seconds at startup, 10 seconds per VLA exchange and
55 seconds per WAM exchange. These delays are allowed only for explicit fixtures,
and must never be reported as native model latencies or capabilities.

### World-frame goals while inference runs

In the latency fixture `yokohama-2e6a5f977ed4`, the initial lifecycle correction
passed the 170-second startup. At WAM time the physical camera changed 0.03103 rad
in 11.16 seconds while position changed 0.06615 m and EKF heading changed only
0.00317 rad. This CPU failure is retained and consumed no GPU budget.

For the motion-v4 **static world**, keep the original VLA world position and world
heading immutable. Capture histories still require at most 0.03 rad of rotation;
only the waiting period for VLA/WAM permits up to 0.25 rad while all existing
position, velocity, EKF continuity and reserve constraints remain. This is a
conditional target-view forecast, not a time-aligned rolling flight forecast.

At authorization, map the fixed world heading into the newly observed AP frame:
`executor_heading = world_goal_heading + PX4_heading - Gazebo_heading`, wrapped
into ±pi. Preserve the logical candidate and record the independent executor field
and mapping-observation hash. Check that the physical heading changes by no more
than 0.03 rad during upload, then require both actual world-facing error and
commanded EKF-facing error ≤0.05 rad at the observed arrival. The verifier reopens
the mapping observation and MAVLink upload ACK. No returned VLA value is rewritten.
This explicit simulator-ground-truth transform is not a qualified onboard heading
estimator; dynamic scenes, camera motion during capture and hardware remain outside
this trial.

### Translation-phase contract correction

Native run `yokohama-84e200069b7b` passed startup/hold but returned
`0 49 48</s>` at D1. Compact-city grammar v1 allowed this zero-forward action,
whereas the downstream phase required a translation; it stopped before WAM.
The original response remains a failed trial, not a later motion receipt.

Compact-city grammar v2 removes zero-distance and LAND tokens at generation
time. VLA now chooses only displacement/orientation parameters inside the
preapproved short-translation envelope. It does **not** decide whether the
mission may advance or whether a location is safe; the image gate, independent
geometry Rules and executor checks can still reject every proposal. This is a
deliberately narrower VLA role, not improved unconstrained navigation or a
post-hoc replacement of a model's stop decision. Legacy non-compact grammars
still represent hold and LAND. Service identity must declare the v2 policy and
both disabled proposal types, otherwise startup rejects it.

### Verified bounded integration

Run `yokohama-280d65f068d7` at source
`f79a199c64f52cb2faa8aa19f1babd1084e6b685` passed the full-flight and native
decision-chain verifiers. Two real-observation AeroVLA calls and four adapted
ANWM forecasts led to two observed model-segment arrivals, followed by the
delivery waypoint, return, landing and disarm. Startup followed the first inland
hold; shutdown and late-authority rejection preceded the AP remainder.

The [portable report](../examples/yokohama-integrated-flight/README.md) preserves
the original forecasts, native VLA responses, permits, trajectory, actual camera
video, three retained development failures and closed cost receipts. This is one
static, zero-wind city trial with constrained translation-parameter selection;
the route connectors remain authored. It is not sea-leg, payload-release,
hardware, unconstrained-navigation or energy-saving evidence.

### Same-sortie stationary offshore integration

Run `yokohama-f17245dce007` at source
`51d6de5962fb1f70cc53e85bfb06fd8a85449042` passed both verifiers with the
[stationary offshore extension](yokohama-sitl.md#stationary-offshore-extension).
The same vehicle flew 1 km AP sea legs in both directions, the authored 400 m
coastal connectors, two fresh native VLA/WAM segments, the delivery waypoint,
and returned to land/disarm with recent deck contact. All eleven 30-second holds
passed; measured world-frame path length was 3,501.39 m. Model requests occurred
only at D1/D2, after the initial inland hold; shutdown and late-request rejection
preceded the AP remainder. No previous native outputs were replayed.

The [combined-flight report and actual camera video](../examples/yokohama-sea-city-flight/README.md)
preserve the new evidence separately from the city-only trial. The coastal gateway
is an authored boundary beyond the source crop, not a surveyed shoreline. The
first CPU attempt retained a ~0.19 m altitude error after moving home offshore;
an observed world-to-PX4-relative-altitude mapping fixed that datum mismatch
without changing the 0.15 m vertical arrival bound. The successful native segment
position errors were 0.040 m and 0.093 m. This is a single static, zero-wind
simulation; payload release/receipt, moving ship, fleets, hardware and energy
savings remain outside the qualified scope.

### Same-sortie cargo follow-up

The later [cargo report](../examples/yokohama-cargo-flight/README.md) records
`yokohama-6acc29204d62` at source `819b90c13ffc03a5fbd3773aeecb91278e988380`:
the same vehicle carried a dynamic 50 g box across the sea, used two fresh native
VLA/WAM city decisions, released the parcel, obtained an independently computed
simulated pad-contact/rest receipt, and returned to deck landing/disarm. All
thirteen holds and the flight, decision and cargo verifiers passed. Require all
three verifiers; the decision verifier alone still does not certify delivery.

The first cargo native attempt failed before inference because an external
lifecycle wrapper targeted a deleted old VM. Its failure and cost are retained.
The recovery uses one reviewed resource manifest and a pre-allocation start/stop
binding check. After observed model shutdown and late-request rejection, its GPU
VM was deleted while the same local AP flight continued. No model output was
replayed from the earlier cargo-free result, and no new training was performed.

See the [cargo evidence contract](yokohama-payload-delivery.md). This establishes
simulated receipt for one drone in zero wind on a static scene; human receipt,
package integrity, moving decks, fleets, hardware and energy savings remain
unverified.
