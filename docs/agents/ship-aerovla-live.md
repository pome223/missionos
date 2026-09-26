# Fresh AeroVLA to bounded PX4 execution

`--aerovla-url http://127.0.0.1:<port>` explicitly opts a simulator run into one
native proposal at urban entry. Start the separately installed, reviewed
`scripts/ship_aerovla_server.py` on a CUDA host first and forward its loopback
port through SSH. The mission CLI does not provision compute, download models,
handle cloud credentials or open a hardware endpoint. Both service and client
accept only explicit IPv4 loopback endpoints. The simulator has no network.

```sh
missionos ship-delivery run-sitl \
  --scenario examples/fixture_missions/ship_delivery/urban-compact.json \
  --urban-case brake_stop --urban-policy onboard_stopping \
  --aerovla-url http://127.0.0.1:18117 --approve-sitl \
  --output-dir /tmp/new-live-aerovla-run
```

## Contract and sequence

The service verifies the same pinned base weights, flight adapter and custom
model code as the [offline native pilot](ship-native-model-admission.md).
It loads once, records a synthetic black-image warmup separately and returns a
session identity. The host verifies model revisions and hashes of the reviewed
server, model runner and imported helper. Freeze that identity, source hashes,
controller and guard limits into the mission plan before launching the vehicle.

At urban entry, verify armed AUTO_LOITER and start the existing 20 Hz position
stream. Its prestream permit uses an explicitly labelled test proposal solely
to qualify holding the observed position; no mode switch or translation is
allowed under that permit. After prestream, capture one pair of real front and
downward 640 x 360 RGB images with identical Gazebo sensor timestamps. Repeated,
misaligned or old image pairs do not become new observations.

The request contains those images, an unpredictable request ID, the source-row
hash and run/world/plan bindings. The prompt's direction is calculated from the
approved mapped delivery goal and observed PX4 pose/attitude. No obstacle truth,
future frame, case name or prerecorded model action enters the model. The
service stacks resized 224 x 224 views vertically and uses greedy generation
with at most 20 tokens, as in the earlier native pilot.

While the host makes the model call, continue checking LOITER, fresh telemetry,
estimator epoch, speed, the approved box and simulator clearance. The worker's
elapsed clock bounds the full mailbox/host/model exchange; remote Unix time is
reported only for model timing. No cross-machine clock subtraction establishes
input freshness.

Reopen the current observation immediately before OFFBOARD. Require the exact
model response, session, images, request and raw source-row bindings. Keep the
existing two-second observation-age limit, 0.5 m drift limit, phase/altitude
constraints and fresh simulator-geometry clearance. A native `LAND`, malformed,
stale or out-of-bounds proposal is retained and rejected. Do not clamp, repair,
retry or replace it with a fixture. Only a separate native execution permit can
authorize the mode command. The guard itself still has `dispatch_allowed=false`.

The [qualified position/yaw controller](ship-vla-executor.md) then checks actual
motion, settling and LOITER handoff. Preserve upstream yaw-first and large-yaw
horizontal-suppression semantics, but label the PX4 position-ramp controller as
an adaptation, not AeroVLA's original AirSim timed-velocity controller. A single
native maneuver does not establish continuous learned flight control.

## Evidence and limits

Keep the request, both images, exact HTTP response, model tokens, remote service
evidence and source hashes. `runtime_invocation_evidence.v1` records the actual
host HTTP call separately from model output, permission, packets and measured
motion. The host verifier reconstructs the model mosaic, reopens the mailbox,
recomputes the permit and retains all existing packet/motion/mission checks.

`vla_invoked` means the service returned an actual inference receipt, even if a
later guard rejects it. It does not mean dispatch or mission completion. The
execution verifier separately reports `native_inference_chain` and observed
motion. Development/failed trials stay in the denominator.

This is a single-drone, stationary-ship simulator integration. Ordinary rules
still control subsequent urban delivery and return. It does not demonstrate
native obstacle avoidance, WAM value, full 1 km sea transit, ten-aircraft
coordination, moving-deck landing or physical delivery. Further Step 2 work uses
the [absolute qualification requirements](ship-onboard-step2.md); superiority
over ordinary rules is not required.

## Recorded runtime verification

The 2026-09-25 pilot at `3ed2710`, run
`7bf52c310c8341ef94af84e3105d00cb`, completed one flight and one fresh native
request. AeroVLA returned `55 49 49</s>` (2.806 m forward, level, no yaw change).
Model inference took 0.738 s; the host exchange took 0.954 s; observation to
execution permit took 1.025 s. The existing two-second freshness bound passed.

The verifier checked 93 execution observations, 2.816 m measured displacement,
0.0344 m endpoint error and 0.0181 m altitude error. It also verified settling,
LOITER handoff, actual rule-route departure, delivery and stationary-ship
recovery. The matched GPU/host response and images were reopened. The ordinary
stopping-aware rule selected detour after the native maneuver.

Related regression tests: 314 passed. Twenty-two mutations of copied runtime
records were rejected, including changed native output/session/request, stale
permit timing, missing downward image and corrupted invocation evidence.
Two black-image service warmups and a pre-inference missing-venv environment
failure remain in the archive; neither is a flight trial. No flight retry or
manual intervention occurred. VM, boot disk and simulator container were removed.

This establishes the one-request native inference/execution chain in this
bounded scene. Native WAM prediction use and the broader absolute-performance
qualification remain unverified; a mission-value comparison is not required
for Step 2.
