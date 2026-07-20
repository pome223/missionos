# TurtleBot3 Nav2 Execution Boundary

`src/runtime/turtlebot3_nav2_execution.py` owns the bounded executor-facing
portion of the TurtleBot3 workflow. It is downstream of proposal, compilation,
rules validation, and human approval.

The module may:

- bind an already concrete `Nav2GoalPose` to an existing approval reference
- send that goal through the opt-in ROS2/Nav2 bridge
- send a harness-authorized `cancel_goal` request
- project bridge state, odometry, obstacle, and sidecar observations
- return blocked adapter evidence when the bridge receipt is unavailable

It must not:

- generate or select a Recovery proposal
- mint, refresh, or broaden approval
- change the compiled goal
- infer motion from an ACK
- infer a stop from a cancel ACK
- infer completion from motion or status text alone
- decide that the remaining route may resume
- claim payload delivery or physical execution

## Inputs

`dispatch_nav2_goal` receives individual authority and execution fields rather
than the full mission proposal:

- stable proposal identifier
- approval actor and approval artifact reference
- concrete Nav2 goal
- dispatch timestamp and bounded action suffix
- optional raw-log reference
- initial-pose and simulator-fault controls

This keeps the executor from interpreting a larger proposal artifact or
creating authority from it. The mission coordinator remains responsible for
validating the proposal/approval/checkpoint chain before calling this boundary.

## Observation semantics

The bridge receipt keeps these facts separate:

- `dispatch_request_sent`: the executor request crossed the bridge boundary
- ACK fields: Nav2 accepted or rejected a request
- `robot_motion_observed`: source-backed odometry changed
- obstacle fields: the bridge or its state/progress result reported the fact
- `completion_claimed`: the hardware adapter's Nav2-success plus motion contract

When the telemetry sidecar is configured, its time-window correlation is
projected separately. An unreadable or mismatched sidecar blocks the correlated
motion claim; it does not fall back to an unverified success.

`dispatch_harness_stop` similarly requires both an accepted cancel request and
post-cancel odometry observation before `stop_confirmed=true`.

## Ownership around this module

The authority flow remains:

```text
Recovery intent -> Compiler -> Rules -> Human approval
    -> turtlebot3_nav2_execution -> Verifier -> Repair/resume decision
```

`turtlebot3_home_mission.py` retains compatibility wrappers while it is reduced
to a coordinator. Tests that need to replace the executor client should patch
`src.runtime.turtlebot3_nav2_execution.Ros2Nav2BridgeCommandClient` rather than
the coordinator module.

The CLI correlation boundary downstream of this executor is documented in
`docs/agents/turtlebot3-cli-companions.md`. It keeps chat, operate, watch, and
map on the same task ID without treating display refresh as execution evidence.
