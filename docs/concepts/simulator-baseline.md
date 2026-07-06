# Simulator Baseline

MissionOS currently uses TurtleBot3 as the primary indoor ROS2/Nav2 simulator
baseline, even though TurtleBot3 is older than TurtleBot4.

That choice is about evidence, not preference for older hardware.

## Why TurtleBot3

TurtleBot3 is the current baseline because it has produced a reproducible
MissionOS simulator loop:

```text
chat request
-> Gateway task proposal
-> human approval
-> bounded Nav2 dispatch
-> odometry-backed motion observation
-> watch, operate, and map evidence
```

That loop can keep the MissionOS claim boundary intact:

```text
completion_scope=sim_action
physical_execution_invoked=false
mission_delivery_completion_claimed=false
```

The important part is not that TurtleBot3 is the newest robot. The important
part is that MissionOS can show what was proposed, what was approved, what was
sent to the simulator boundary, what motion was observed, and what remains
unproven.

TurtleBot3 also has a practical public demo shape for indoor work. The
`turtlebot3_house` world gives MissionOS a room-and-door floor plan that can be
rendered as a readable local indoor map without claiming real home operation or
payload delivery.

## Why Not Make TurtleBot4 The Baseline Yet

TurtleBot4 support is useful and should continue, but it is not yet the default
baseline because the current TurtleBot4/Create3/Gazebo simulator runtime has not
produced the same reproducible motion evidence.

Current TurtleBot4 work can create MissionOS task artifacts and preserve the
TurtleBot4 profile:

```text
robot_profile=turtlebot4
robot_model=turtlebot4_lite
execution_target=ros2_nav2_turtlebot4_sim
```

But the simulator completion path is still blocked below MissionOS and below
Nav2. Direct diagnostic velocity to the Create3 diffdrive controller has not
produced meaningful wheel or `/odom` motion, pure Create3 drive actions have
accepted goals and then timed out, and replacing the installed simulator xacro
strings from `ign_ros2_control` to `gz_ros2_control` did not make the robot move.

Because of that, MissionOS must keep TurtleBot4 simulator runs bounded as:

```text
completion_claimed=false
completion_scope=none
physical_execution_invoked=false
```

This is intentional. A newer simulator profile should not become the public
baseline until it can produce the same evidence quality as the older one.

## Graduation Rule

TurtleBot4 can replace TurtleBot3 as the indoor simulator baseline when it can
reproduce all of these in a documented runtime smoke:

- direct Create3 or controller-level velocity produces meaningful `/odom`
  movement
- Nav2 `NavigateToPose` accepts and succeeds for a bounded goal
- the same run observes non-zero odometry motion above the configured threshold
- the MissionOS bridge reports `completion_scope=sim_action`
- the task, watch, operate, and map surfaces preserve
  `physical_execution_invoked=false`
- the run is repeatable enough to use as PR verification

Until then, TurtleBot3 remains the baseline because it is the path that keeps
the MissionOS evidence story honest.
