# Hardware Partner Integration Guide

This guide is the maintainer checklist for adding a partner-owned hardware
adapter to MissionOS. It is not a field-test authorization and not a claim that
MissionOS can safely operate a partner robot or drone.

## Required Boundary

Preserve the MissionOS authority split:

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

A partner adapter must sit below MissionOS and above the partner's existing
autopilot, Nav2 stack, safety controller, or vendor API. The adapter may accept
only bounded, approved actions. It must not expose raw unbounded command
streams to an LLM or to a MissionOS proposal artifact.

## Minimum Adapter Package

Every partner adapter PR must include:

- a capability artifact for the exact mode being exercised
- a preflight artifact with structured blocking reasons
- a dispatch candidate artifact that sends nothing
- an operator approval artifact bound to one MissionOS action
- an evidence artifact for accepted, rejected, timeout, stale telemetry,
  heartbeat loss, operator abort, and blocked preflight cases
- a runtime smoke that crosses only the intended boundary
- a short concept-layer summary if the user-facing story changes
- an `E2E / Runtime Verification` section in the PR body

## Bounded Action Rules

Start with one narrow action. Prefer these first:

- `nav2_goal_pose` for indoor ground robots
- `nav2_cancel_goal` for ground-robot stop/cancel behavior
- `bounded_local_move` for Unitree SDK2/MuJoCo simulation
- props-removed PX4 bench arm/disarm for actuator evidence only

Do not start with:

- raw velocity control
- raw motor control
- special motions such as flip, dance, or jump
- raw MAVLink from an LLM
- unbounded ROS topic publication
- takeoff
- mission start
- payload release
- outdoor autonomous delivery

## Evidence Requirements

The adapter must keep these facts separate:

- proposal
- operator approval
- dispatch request
- command ACK
- state readback
- progress observation
- completion claim
- completion scope
- physical execution
- safe stop or abort

ACK is not success. Progress is not mission completion. Simulator or loopback
evidence is not physical execution. If any required fact is missing, the
adapter must fail closed and write structured evidence.

## ROS2/Nav2 Ground Robot Slice

The current ROS2/Nav2 slice is in
`src/runtime/ros2_nav2_hardware_adapter.py`.

It provides:

- bounded Nav2 goal/cancel capability artifacts
- fail-closed preflight for missing client, stale telemetry, geofence failure,
  operating-volume violation, missing E-stop, and missing opt-in
- a dispatch wrapper around an injected `Nav2DispatchClient`
- evidence projection from Nav2 ACK, state, and progress observations
- loopback completion scoped to `loopback_action`

The core adapter does not import `rclpy`, start ROS2, publish raw command
topics, or claim physical execution. For the first simulator integration path,
see `docs/agents/ros2-nav2-turtlebot4-sim.md`. That bridge runs as an opt-in
external command in ROS2 Humble, sends a Nav2 `NavigateToPose` action goal, and
can claim only `completion_scope=sim_action` after Nav2 success and odometry
motion are observed. Diagnostic simulator shims such as odom-to-TF or
Nav2-velocity relay must be reported separately and cannot satisfy completion
by themselves. It is not physical execution.

## Unitree SDK2 / MuJoCo Slice

The current Unitree slice is in
`src/runtime/unitree_hardware_adapter.py`.

It provides:

- bounded Unitree SDK2/MuJoCo capability artifacts
- allowed `safe_stop`, `hold`, and `bounded_local_move`
- blocked `raw_motor`, `raw_velocity`, and `special_motion`
- max speed `0.3 m/s` and max distance `0.5 m` for the first local move
- fail-closed preflight for missing sim client, missing opt-in, stale telemetry,
  geofence failure, heartbeat loss, and operating-volume violation
- a dispatch wrapper around an injected `UnitreeSimClient`
- evidence projection from sim ACK and sim progress observations
- simulator completion scoped to `sim_action`

It does not import Unitree SDK2, start MuJoCo, command motors, issue raw
velocity, invoke Go2 special motions, or claim physical execution. A future
Unitree runner must add its own opt-in runtime smoke and a separate physical
execution story before any partner or physical claim is made.

For local environment setup and readiness checks, see
`docs/agents/unitree-mujoco-environment.md`. Do not use upstream low-level torque
examples as MissionOS bounded-action evidence.

For bounded dispatch, use an operator-provided bridge command that accepts the
MissionOS bounded JSON request and returns JSON receipts. The bridge must never
claim physical execution for MuJoCo simulation and must reject raw motor, raw
velocity, and special-motion requests.

For real Go2 hardware readiness, see
`docs/agents/unitree-go2-real-hardware.md`. The first real-Go2 smoke is
read-only service discovery for onboard `SportClient` and `RobotStateClient`;
it does not call `Move`, publish `rt/lowcmd`, or claim physical execution.

## PX4 Bench Slice

The PX4 slice remains props-removed bench scope only. It may record command
send, ACK, and state readback for arm/disarm. It must not claim takeoff, flight
execution, delivery completion, or mission completion.

## PR Verification Template

Use this section in every partner-adapter PR:

```text
## E2E / Runtime Verification

Command:

Scenario:

Boundary covered:

Observed result:

Evidence artifacts:

Limitations:
```

For a blocked-path smoke, the observed result must explicitly say that no
dispatch request was sent and `physical_execution_invoked=false`.
