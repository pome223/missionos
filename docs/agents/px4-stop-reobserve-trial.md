# PX4 stop, observe and resume development comparison

## Predeclared scope

Two fresh PX4/Gazebo sessions use the same gap scene, camera, initial hover,
checkpoint and approved candidate family. After departure passes ENU east 1 m,
the host inserts a static orange 1 × 3 × 5 m barrier at (8, 0, 2.5). It was absent
from the initial observation and remains absent from the planner's prior map.
The barrier is a scripted world change, not a moving-object prediction task.

Both controllers stop at (3, 0, 3), observe a one-second simulated dwell below
0.1 m/s, then acquire sixteen new aligned native RGB-D/pose frames. This is a
planned stop before the next segment, not depth-triggered emergency braking.
The initial and checkpoint observations are retained separately. The selector
uses the latest measured depth surfaces and the old map; world truth is available
only to the separate common safety filter and verifier.

* `frozen_route`: retain the initially selected forward route. The common safety
  gate rejects it after the change, then the controller lands at the checkpoint.
* `reobserve`: rank the supplied forward and left-detour candidates again from
  the new depth observation. If admitted by the same safety filter, execute the
  selected route, dwell at the goal, land and disarm.

The expected outcome divergence is safe non-arrival versus arrival, **not**
collision versus safety. Both runs receive the same observation opportunities;
ignoring checkpoint depth for route selection is the deliberate baseline ablation.
An always-detour policy can also avoid this particular barrier. This pair tests
the reobservation/route-change mechanism, not superiority over every simple policy.

## Admission, budget and stop rules

Primary endpoint: observed goal dwell followed by landing/disarm. Secondary
records: observed contacts, conservative swept-envelope clearance, distance,
simulated elapsed time and observation age in wall seconds. The initial route
and alternative actually diverge under the changed geometry. Maximum oracle
additional arrivals over the frozen baseline in this single pair is one.

The pair permits two flights and zero WAM/Jev/GPU/training calls. Freeze protocol
and runtime source hashes before the first flight. Preserve failed attempts and
stop on infrastructure or safety failure; no replacement inside the frozen pair.
There are no held-out cases and no exact physics-state cloning claim. If the
simple reobservation controller reaches the destination, this case supplies no
arrival headroom for WAM and does not reopen the closed R2 model experiment.

## Authority and runtime boundary

This is a separate opt-in research script. It reuses the existing PX4 executor,
camera collector, depth scorer and conservative safety checks, without extending
the existing single-route Gateway approval. A retained instruction authorizes
one approach plus at most one resume decision, restricted to this simulator,
source-bound policy and supplied routes. Signed stage-specific dispatch expires
after four wall seconds. Observations must be at most five wall seconds old and
still agree with the stationary aircraft pose. Replayed, changed or stale input
fails closed and enters landing cleanup. It is not integrated into the normal
Gateway closed-loop task lifecycle or arbitrary missions.

```sh
python scripts/px4_urban_reobserve_trial.py --phase freeze --output-dir "$RUN"
RUN_PX4_URBAN_WAM_TRIAL=1 python scripts/px4_urban_reobserve_trial.py \
  --phase run --output-dir "$RUN" --assets-dir "$ASSETS" \
  --approved-instruction-ref "$APPROVAL_REF"
python scripts/verify_urban_reobserve.py "$RUN/frozen_route"
python scripts/verify_urban_reobserve.py "$RUN/reobserve"
```

Use the pinned image already required by the depth Gateway. Fresh session
directories, the shared simulator lock, isolated Docker networking, fresh
positive contact control, signal-to-land recovery and owned-container cleanup
are mandatory. Camera images and telemetry remain local until individually
reviewed for a portable report. No secrets, dispatch keys or approval reference
are included in public artifacts.

## First development attempt and correction (2026-09-25)

The first frozen pair stopped after its first attempt. PX4 took off, moved to
the checkpoint, observed the inserted barrier and stopped. The existing origin-
only camera probe then refused capture at east 3 m. Host recovery requested land;
landing/disarm and container removal were observed. The goal was not reached,
the resume decision was not dispatched, and the second method was not attempted.
See the [retained failure record](../assets/px4-stop-reobserve-20260925/development-attempt-01.json).

`urban_checkpoint_capture.py` now derives a separately scoped collector from the
unchanged origin-only probe. Its sole spatial change is a 0.2 m box around the
fixed checkpoint (3, 0, 3); arbitrary capture positions remain unsupported. Native
sensor timestamps, calibration, raw data and stationary flight-status checks are
unchanged. A regression test reproduces the old scope mismatch and checks the new
box. Initially the corrected comparison was blocked by approximately 200 MiB
free space, below the runner's 350 MiB admission threshold. Removing seven
unreferenced, untagged Docker images later restored approximately 5.2 GiB free
space without archiving experiment data or removing volumes. The corrected pair
was frozen separately as `pair02`; it does not replace the failed first attempt.

Validation performed after the correction:

```sh
export PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src
python -m pytest tests/contract/test_urban_reobserve.py \
  tests/contract/test_urban_headroom.py tests/contract/test_urban_navigation.py \
  tests/contract/test_urban_contact_probe.py tests/contract/test_px4_depth_navigation.py -q
python scripts/check_px4_depth_gateway.py --output-dir "$HTTP_RUN" --scene all
```

Result: 69 tests passed; a freshly started loopback Gateway and packaged CLI
passed the three existing nonflight profile checks, including authentication,
approval and opt-in refusal. Lint passed for the new scripts and tests. The HTTP
check does not cover the new stop/observe/resume loop. No WAM/Jev/GPU/training
calls occurred. Keep this work in Draft.

## Corrected frozen pair results

The separately frozen `pair02` completed both sessions. The frozen-route controller
retained `forward`, was rejected by the common gate and safely landed at the
checkpoint. The latest-depth controller changed to `left_detour`, reached and
dwelled at the destination, landed/disarmed and removed its simulator. Both
original records passed independent verification. See the [measured report and
synchronized camera/trajectory replay](../assets/px4-stop-reobserve-20260925/README.md).

There were three flights across development: one failed first attempt and two
corrected comparison sessions. The reobservation pause was 37.212 wall seconds
(7.444 sim seconds), including a contiguous-history reset that discarded 12
intermediate frames before retaining sixteen valid ones. This is not a real-time
emergency avoidance result. Latest-frame age at dispatch was 1.424 wall seconds.
No WAM advantage or new WAM/GPU admission follows from this successful depth case.

## Subsequent Gateway integration

The separate paired trial above remains development evidence with its original
frozen source hashes. The same session controller is now reusable by the normal
depth task through [`--scene gap --reobserve`](px4-depth-gateway.md#planned-stop-and-reobservation).
This option has its own request and approval scope for one checkpoint/resume;
it does not expand previously issued single-route approvals. Gateway integration
flights are recorded separately from the two-method research comparison.
