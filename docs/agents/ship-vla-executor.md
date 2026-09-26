# Bounded VLA-format PX4 execution qualification

The fixture mode below remains available. A separate explicit
[fresh native inference mode](ship-aerovla-live.md) now uses this controller
after validating its own input/inference/authorization chain.

The [proposal guard](ship-vla-guard.md) remains advisory. This separate executor
qualifies a simulator-only controller contract with the explicit fixture
`55 47 55`: approximately 2.806 m forward, 0.204 m up, and 0.135 rad yaw.
No native model is invoked. Archived native outputs cannot enter this path.
The explicit fixture restriction is deliberate: qualifying transport does not
establish a fresh model-input/inference/dispatch chain.

```sh
missionos ship-delivery run-sitl \
  --scenario examples/fixture_missions/ship_delivery/urban-compact.json \
  --urban-case brake_stop --urban-policy onboard_stopping \
  --vla-executor-smoke --approve-sitl --output-dir /tmp/new-vla-execution-run
```

## Authority and execution boundary

Freeze `vla_executor_contract` and guard limits before plan hashing. Require
both simulator approval and the exact controller contract. A guard assessment
never changes its `dispatch_allowed=false`. A separate execution permit binds
proposal, input/current observations, guard decision, controller, clearance,
plan and expiry. It grants simulator dispatch eligibility, not completion.

The qualification controller preserves the candidate's yaw-first order and
endpoint, but uses a 1 m/s position-setpoint ramp followed by closed-loop
settling. This differs from AeroVLA's upstream timed velocity controller; it
must not be described as equivalent learned control. Only the fixed fixture
is qualified, including its small nonzero yaw and climb.

Clearance uses fresh Gazebo boxes and current ego pose, with 1.25 m total
inflation. This is simulator ground truth, explicitly not learned/onboard
obstacle perception. Recheck it throughout execution. PX4 position, attitude,
status, reset counters and image freshness remain separately checked.

## Transport and observation

Use a network-isolated container and fixed loopback ports 14606/14656. Nothing
accepts a remote hardware endpoint. Send position/yaw setpoints at 20 Hz, first
holding current position for 1.5 s. Reobserve and recompute the permit immediately
before requesting OFFBOARD. Require both a CRC-checked mode ACK and observed
PX4 nav_state=14. These implement the [PX4 Offboard stream requirement](https://docs.px4.io/main/en/flight_modes/offboard).

Use the worker's own elapsed-time callback for permit issuance. Reconstructing
its origin from a sample after image capture/write work produces a different
clock and can correctly trip the guard's future-observation rejection.
Accepted ACK frames can be duplicated; verify each distinct command's time
window and observed mode, preserving duplicate packets in the evidence.

Use `MAV_FRAME_LOCAL_NED`, mask `0x9f8` (xyz/yaw active; velocity, acceleration
and yaw rate ignored), system/component targets 1/1. Yaw is in radians, position
in metres, positive z down. Packet semantics follow the [MAVLink common messages](https://mavlink.io/en/messages/common.html#SET_POSITION_TARGET_LOCAL_NED).

Observe yaw alignment before translation. Monitor the inflated approved box,
0.75 m tracking tube, 2 m/s observed speed, fresh state and unchanged estimator
epoch. Require target error <=0.25 m, vertical error <=0.15 m, yaw error <=0.05 rad
and speed <=0.3 m/s continuously across sampled observations for at least 2 s.
Then require accepted LOITER command, observed nav_state=4 and stable target for
1 s before handing control back to the existing rule mission.

Reuse the existing mission-upload MAVLink session for urban and return uploads.
In this image, stopping/restarting MAVLink after Offboard left the vehicle in
AUTO MISSION without trajectory publications or motion. A diagnostic restart
of FlightModeManager restored them. The production path preserves publishers
and requires observed AUTO MISSION departure within 15 s. Do not treat the
mode indication alone as successful handoff. Mission ACK acceptance also waits
for requests for every item, so a duplicate clear-ACK cannot complete upload.

A 0.8 s observation watchdog stops the stream; a worker exception requests
LOITER, and the host terminates only this simulator container. This is not a
qualified real-aircraft failsafe: the image's configured PX4 offboard-loss
behavior and a killed-worker scenario require separate validation.

## Evidence and limitations

`vla-execution.json`, `vla-transport.jsonl` and ordinary telemetry/RGB preserve
permits, packets, ACKs, state and completion separately. The host verifier
recomputes permits and clearance from recorded rows, reopens images and code,
checks packet CRC/masks/targets/authority, stream gaps and ordering, and verifies
observed motion plus settling. A process exit, ACK or sent target alone cannot
establish completion. Existing delivery and ship recovery verification still
runs after the candidate maneuver.

This bounded test does not establish native VLA judgment, WAM value, dynamic
urban perception, continuous-time collision freedom, moving-ship recovery,
10-drone operation, or the full 1 km mission. Its purpose is to qualify the
controller/executor/verifier boundary before integrating fresh native inference.

## Recorded qualification

The 2026-09-25 final trial at `c381df9` (run
`7c32acad76bc4fb39f20779af8881922`) passed the executor and mission verifiers: 83 observed
states, 214 position/yaw packets, two distinct accepted mode transitions
(four ACK packets), 2.828 m observed displacement, 0.0491 m endpoint error and
0.0155 m vertical error. Rule-route resumption, delivery and stationary-ship
recovery also passed. Related tests: 295 passed; 14 mutations of copied runtime
records were rejected by the verifier.

Preserve the full denominator: three development trials, one final qualified
success. The first failed closed before dispatch due to clock reconstruction.
The second completed the candidate maneuver but failed ACK verification and
mission handoff; it included an explicitly recorded diagnostic module restart
and was interrupted, so it is not a qualified success. The third used fixed
code, preserved MAVLink sessions and completed without manual intervention.
