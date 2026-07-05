# Hardware Adapter Contract

This page defines the first MissionOS Real Hardware Bridge v1 contract slices.
It adds hardware-adapter evidence contracts, projects the existing PX4 bench
executor result into those contracts, and adds a bounded ROS2/Nav2 ground-robot
adapter wrapper and Unitree SDK2/MuJoCo adapter wrapper around injected client
boundaries. It does not add a fake production adapter and it does not claim
physical robot or drone execution.

## Authority Boundary

Preserve the MissionOS split:

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

A hardware adapter may translate an approved bounded action into its own
dispatch surface, but it must not decide mission goals, self-approve, infer
completion from ACK, or treat loopback evidence as physical execution.

## Schemas

The current runtime slice lives in
`src/runtime/hardware_adapter_contract.py`.

- `missionos_hardware_adapter_contract.v1`
- `missionos_hardware_adapter_preflight.v1`
- `missionos_hardware_adapter_dispatch_candidate.v1`
- `missionos_hardware_adapter_operator_approval.v1`
- `missionos_hardware_adapter_evidence.v1`

## Capabilities

`HardwareAdapterCapabilities` declares what the adapter can accept before any
dispatch attempt:

- `adapter_id`
- `adapter_kind`: `schema_example_only`, `ros2_nav2`, `px4_mavlink`,
  `px4_mavsdk`, `unitree_sdk2`, or `vendor_specific`
- `vehicle_class`: `ground_robot`, `multirotor`, `fixed_wing`, or `other`
- `execution_mode`: `schema_example_only`, `loopback`, `sim`, `hitl`, `bench`,
  `cage`, or `field`
- `allowed_actions`
- `blocked_actions`
- `requires_operator_approval`
- `requires_fresh_telemetry`
- `requires_physical_estop`
- `requires_geofence`
- `max_speed_mps`
- `max_altitude_m`
- `max_distance_m`
- `supports_abort`
- `supports_return`
- `supports_hold`

The schema example is a static artifact, not an adapter runtime. The moving PX4
runtime slice is the existing arm/disarm bench executor; tests run it with a
loopback MAVLink connection and require the persisted
`missionos_hardware_adapter_evidence` artifact. The ROS2/Nav2 slice calls an
injected `Nav2DispatchClient` only after preflight and approval pass, and still
keeps physical execution unclaimed until a real ROS2 client wrapper and runtime
smoke exist. The Unitree SDK2/MuJoCo slice does the same for an injected
`UnitreeSimClient`, with `execution_mode=sim` and `completion_scope=sim_action`
only when simulated ACK and progress evidence both exist.

## Preflight

`HardwareAdapterPreflightResult` is a non-dispatch check. It must not create
dispatch authority and must not invoke physical execution.

Required fail-closed reasons include:

- `telemetry_stale`
- `heartbeat_lost`
- `geofence_violation`
- `operating_volume_violation`
- `action_not_allowed_by_adapter_capabilities`
- `action_blocked_by_adapter_capabilities`

## Dispatch Candidate

`HardwareDispatchCandidate` is an adapter-specific translation of a MissionOS
bounded action. It is still not dispatch.

The candidate must keep:

- `operator_approval_required=true`
- `raw_command_preview=false`
- `dispatch_request_sent=false`
- `dispatch_authority_created=false`
- `physical_execution_invoked=false`

## Operator Approval

`HardwareOperatorApproval` is scoped to one `missionos_action_ref` and one
`adapter_action_kind`. A mismatch blocks dispatch. Approval is evidence, not an
ACK, progress observation, completion claim, or physical-execution claim.

## Evidence

`HardwareAdapterEvidence` separates:

- proposal/candidate
- operator approval
- preflight
- dispatch request
- ACK
- progress observation
- completion claim
- completion scope
- physical execution
- safe stop or abort

The evidence validator rejects these claim-boundary violations:

- `dispatch_request_sent=true` without `operator_approval_ref`
- `dispatch_request_sent=true` with stale telemetry
- `completion_claimed=true` without ACK
- `completion_claimed=true` from ACK alone
- `completion_scope=mission` for the PX4 arm/disarm bench action
- `completion_scope=adapter_action` for loopback execution
- `completion_scope=adapter_action` for sim execution
- `completion_scope=sim_action` outside sim execution
- schema-example evidence claiming physical execution

## PX4 Bench Evidence Projection

When `invoke_missionos_real_hardware_dispatch_runtime()` completes the existing
PX4 bench arm/disarm executor path, it also writes
`missionos_hardware_adapter_evidence`.

On both blocked and executed paths it also writes:

- `missionos_hardware_adapter_capabilities`
- `missionos_hardware_adapter_preflight`
- `missionos_hardware_dispatch_candidates`
- `missionos_hardware_operator_approvals`, when Gateway approval exists

The executed-path evidence is derived from:

- `mavlink_command_sent`
- `command_ack_observed`
- `state_readback_observed`
- `link_kind`
- `physical_execution_invoked`

If the executor runs through an injected loopback connection, the evidence uses
`execution_mode=loopback` and `physical_execution_invoked=false`. If the opt-in
real serial bench path runs, the evidence may use `execution_mode=bench` and
`physical_execution_invoked=true`, while still keeping flight execution and
delivery completion out of scope.

`completion_claimed=true` uses `completion_scope=loopback_action` for loopback
execution and `completion_scope=adapter_action` only for the opt-in real bench
path. It means the bounded arm/disarm sequence had ACK and state readback at
that scope; it must not be described as mission completion, delivery completion,
flight execution, or proof of safe physical operation.

When the runtime blocks before command send, for example because
`RUN_MISSIONOS_REAL_HARDWARE_DISPATCH_RUNTIME` is not enabled, MissionOS still
persists blocked preflight and adapter evidence with `dispatch_request_sent=false`
and `physical_execution_invoked=false`.

## Runtime Contract Tests

The contract tests are in
`tests/contract/test_hardware_adapter_contract.py`.

They must prove at least:

- no dispatch without approval
- ACK is not completion
- loopback completion uses `completion_scope=loopback_action`, not
  `adapter_action`
- blocked preflight writes structured adapter evidence rather than only a string
- the existing PX4 bench executor writes hardware adapter evidence after
  command send, ACK, and state readback
- loopback execution does not claim physical execution
- delivery completion and flight execution remain unclaimed
- ROS2/Nav2 goal dispatch calls only an injected client boundary after approval
- ROS2/Nav2 TurtleBot4 sim dispatch can cross an opt-in external bridge command
  and still claim only `sim_action`
- ROS2/Nav2 physical modes block without client, opt-in, and E-stop evidence
- Unitree MuJoCo `bounded_local_move` calls only an injected sim client boundary
  after approval and opt-in
- Unitree `special_motion`, `raw_motor`, and `raw_velocity` stay blocked
- sim completion uses `completion_scope=sim_action`, not `adapter_action`

## ROS2/Nav2 Ground Robot Slice

The bounded ground-robot adapter lives in
`src/runtime/ros2_nav2_hardware_adapter.py`.

It provides:

- `Ros2Nav2HardwareAdapter`, a structural `HardwareAdapter` implementation
  around an injected `Nav2DispatchClient`
- bounded `nav2_goal_pose`, `nav2_cancel_goal`, `hold`, and `safe_stop`
  capability artifacts
- preflight blocking for missing client, missing goal pose, stale telemetry,
  heartbeat loss, geofence violation, operating-volume violation, missing
  physical E-stop, missing opt-in, unsupported HITL, and unsupported field mode
- evidence projection from Nav2 ACK, state readback, and progress observations
- loopback completion as `completion_scope=loopback_action`

The core adapter slice does not import `rclpy`, publish raw ROS command topics,
or claim physical execution. The TurtleBot4 simulator bridge is documented in
`docs/agents/ros2-nav2-turtlebot4-sim.md`; it runs as an opt-in external command
inside ROS2 Humble and sends a Nav2 `NavigateToPose` action goal. Simulator
completion remains `completion_scope=sim_action`, and the bridge receipt is
rejected if it claims physical execution or MissionOS-generated raw
velocity/topic publication. Diagnostic simulator shims such as odom-to-TF or
Nav2-velocity relay must be reported separately and cannot satisfy completion
without Nav2 success plus odometry motion. Raw-velocity controller probes are
diagnostic-only and must not be passed through the MissionOS dispatch bridge.

## Unitree SDK2 / MuJoCo Slice

The bounded Unitree adapter lives in
`src/runtime/unitree_hardware_adapter.py`.

It provides:

- `UnitreeHardwareAdapter`, a structural `HardwareAdapter` implementation
  around an injected `UnitreeSimClient`
- bounded `safe_stop`, `hold`, and `bounded_local_move` capability artifacts
- blocked `raw_motor`, `raw_velocity`, and `special_motion` actions
- max speed `0.3 m/s` and max distance `0.5 m` for the initial local move
- preflight blocking for missing sim client, missing opt-in, stale telemetry,
  heartbeat loss, geofence violation, operating-volume violation, and action
  capability mismatch
- evidence projection from sim ACK and sim progress observations
- sim completion as `completion_scope=sim_action`

This slice does not import Unitree SDK2, start MuJoCo, command motors, issue raw
velocity, or invoke Go2 special motions such as flip, dance, or jump.
`physical_execution_invoked=false` is fixed for this slice. A future real
Unitree SDK2 runner must add its own opt-in smoke and must not reuse
`sim_action` for physical movement.

The external environment readiness slice is documented in
`docs/agents/unitree-mujoco-environment.md`. Its smoke validates a local
`unitree_mujoco` checkout and the Go2 Python simulator config without importing
Unitree SDK2, starting MuJoCo, or sending commands.

The next opt-in slices add SDK2 import readiness, MuJoCo process launch, and a
bounded dispatch command bridge. The bridge sends only bounded JSON to an
operator-provided command and rejects any receipt that claims physical
execution.

The real Go2 hardware branch is documented in
`docs/agents/unitree-go2-real-hardware.md`. Its first smoke is read-only: it
checks onboard `SportClient` and `RobotStateClient` service availability on an
operator-provided non-loopback robot interface, but it does not call
`SportClient.Move`, publish `rt/lowcmd`, send MissionOS dispatch, or claim
physical execution.

## Future Adapter Rule

A ROS2/Nav2, Unitree SDK2, PX4/MAVSDK, vendor, cage, or field adapter must
first attach to a real runtime boundary and produce the same evidence
separation, then add its own runtime smoke that crosses only the intended
boundary. Do not wire a real adapter directly to LLM output or to raw unbounded
command surfaces.

## V1 Completion Boundary

This v1 slice is complete when these are true:

- the structural `HardwareAdapter` protocol exists for the Issue #1 method
  surface
- schema examples are clearly marked as `schema_example_only`, not runtime
  adapters
- PX4 bench capabilities, preflight, dispatch candidate, operator approval, and
  evidence artifacts can be persisted on the Gateway-owned task
- blocked executor paths persist structured adapter evidence
- executed loopback paths persist command send, ACK, state readback, and
  `completion_scope=loopback_action`
- loopback evidence never claims physical execution
- validator rejects ACK-only completion, loopback `adapter_action` completion,
  and mission completion for the props-removed bench action
- contract tests and the Gateway loopback smoke pass
- ROS2/Nav2 contract tests, blocked-path smoke, and TurtleBot4 bridge fixture
  smoke pass without importing ROS2 or touching hardware
- Unitree MuJoCo contract tests and blocked-path smoke pass without importing
  Unitree SDK2, starting MuJoCo, or touching hardware
- Unitree MuJoCo environment readiness smoke exists and fails closed unless an
  operator-provided checkout uses Go2, domain id `1`, and loopback `lo`
- Unitree SDK2 import readiness, MuJoCo process launch, and bounded dispatch
  smokes are opt-in and keep physical execution false
- Unitree Go2 real-hardware readiness smoke exists and fails closed unless an
  operator provides a non-loopback robot interface and explicit opt-in; the
  readiness smoke is read-only and does not move the robot

This v1 slice does not complete a physical ROS2/Nav2 robot, real Unitree
SDK2/MuJoCo runner, MAVSDK, field flight, mission upload, takeoff, payload
release, outdoor operation, or partner hardware onboarding. TurtleBot4 simulator
completion is allowed only as `sim_action` after the opt-in runtime smoke
observes Nav2 success and simulator odometry motion.

## Safety Case

The v1 safety case is intentionally narrow:

- no direct LLM dispatch
- no raw MAVLink or ROS command generation by an LLM
- no dispatch request without Gateway approval evidence
- no real serial command without `RUN_MISSIONOS_REAL_HARDWARE_DISPATCH_RUNTIME=1`,
  `opt_in=true`, and a serial device
- no takeoff, mission start, Offboard setpoint, raw MAVLink, LAND, or RTL action
  through the PX4 bench adapter contract
- no success claim from ACK alone
- no mission or delivery completion claim from bench arm/disarm
- no physical execution claim from loopback or injected connections
- no physical execution claim from simulator evidence
- no Unitree raw motor, raw velocity, or special-motion dispatch
- blocked runtime paths must write structured preflight and adapter evidence

The only executable action in scope is props-removed PX4 arm then disarm through
the existing approval-gated bench executor. The action may claim
`completion_scope=adapter_action` only after a real serial bench run with ACK and
state readback. Loopback may claim only `completion_scope=loopback_action`.

## Partner Checklist

Before any partner adapter is added, require:

- adapter capabilities artifact for the exact mode
- documented allowed and blocked actions
- fresh telemetry source and staleness rule
- operator approval reference bound to one action
- abort or safe-stop evidence path
- runtime smoke that crosses the intended boundary only
- explicit statement of whether `physical_execution_invoked` can ever be true
- evidence examples for accepted, rejected, timeout, stale telemetry, heartbeat
  loss, and operator abort
- docs update in this agent layer and a short concept-layer summary if the
  user-facing story changes

See `docs/agents/hardware-partner-integration-guide.md` for the PR checklist
and runtime verification template.
