# ROS2/Nav2 TurtleBot4 Simulator Bridge

This page documents the opt-in TurtleBot4 simulator path for the ROS2/Nav2
hardware adapter. It is an agent-facing runtime contract, not a user tutorial.

## Purpose

The Unitree MuJoCo public simulator path can observe a Go2 scene, but it cannot
currently prove MissionOS bounded high-level control through the official Go2
Sport service. The ROS2/Nav2 TurtleBot4 path is the first simulator route where
MissionOS can aim for:

- a real ROS2 action boundary
- a real Nav2 `/navigate_to_pose` request
- a short bounded ground-robot goal in simulation
- `completion_scope=sim_action`
- `physical_execution_invoked=false`

Do not use this path as evidence of physical execution or real robot operation.

## Repo Components

- `src/runtime/ros2_nav2_hardware_adapter.py`
  - owns MissionOS adapter capabilities, preflight, approval, dispatch evidence,
    and claim boundaries
- `src/runtime/ros2_nav2_dispatch_bridge.py`
  - calls an operator-provided command with bounded JSON and rejects receipts
    that claim physical execution or raw ROS velocity/topic publication
- `scripts/ros2_nav2_turtlebot4_bridge.py`
  - runs inside a ROS2 Humble environment and sends a Nav2 `NavigateToPose`
    action goal
- `scripts/smoke_ros2_nav2_turtlebot4_bounded_dispatch.py`
  - wires `Ros2Nav2HardwareAdapter` to the bridge command and produces adapter
    evidence

The bridge command is intentionally outside the core adapter. A normal macOS
repo checkout can run tests and gate-off smoke without importing `rclpy`.

## External Runtime

Use a Linux host or Linux Docker container with ROS2 Humble and TurtleBot4
simulator packages. The TurtleBot4 upstream simulator documentation names the
Humble package as:

```bash
sudo apt update
sudo apt install ros-humble-turtlebot4-simulator
```

The repo includes a minimal image definition for this external runtime:

```bash
docker build \
  -f docker/ros2_nav2_turtlebot4/Dockerfile \
  -t missionos-ros2-nav2-turtlebot4:local \
  .
```

The image is a runtime convenience only. It is not part of the MissionOS core
adapter and does not make simulator completion physical.

On a Linux Docker host, start it with the repo mounted:

```bash
docker run --rm -it \
  --net=host \
  --ipc=host \
  -v "$PWD:/work/missionos" \
  missionos-ros2-nav2-turtlebot4:local
```

Inside Docker, prefer UDP transport for Fast DDS while launching the TurtleBot4
graph. Without this, `robot_state_publisher` can crash or the controller
manager can become unreachable in the headless container:

```bash
export RMW_FASTRTPS_USE_SHM=0
export FASTDDS_BUILTIN_TRANSPORTS=UDPv4
```

The current Humble apt package separates the simulator launch from the spawn
launch arguments that enable Nav2. A quick GUI/offscreen simulator launch is:

```bash
source /opt/ros/humble/setup.bash
export QT_QPA_PLATFORM=offscreen
ros2 launch turtlebot4_ignition_bringup turtlebot4_ignition.launch.py rviz:=false model:=lite
```

That path starts the Gazebo/TurtleBot4 launch graph, but in headless Docker it
can still fail at the OpenGL/GUI layer. The Docker image includes `xvfb` because
the Ignition server can still require a display when rendering sensors are
present.

For a server-only Gazebo check, keep the upstream resource-path shape. The
description packages are resolved from their parent share directory, not from
the package directories themselves:

```bash
source /opt/ros/humble/setup.bash
export IGN_GAZEBO_RESOURCE_PATH=/opt/ros/humble/share/turtlebot4_ignition_bringup/worlds:/opt/ros/humble/share/irobot_create_ignition_bringup/worlds:/opt/ros/humble/share
export GZ_SIM_RESOURCE_PATH="$IGN_GAZEBO_RESOURCE_PATH"
xvfb-run -a ros2 launch ros_ign_gazebo ign_gazebo.launch.py ign_args:="warehouse.sdf -s -r -v 3"
```

The packaged `warehouse.sdf` uses a 3 ms physics step. The installed Create3
controller config expects a 1 kHz update rate, and `diffdrive_controller` can
stall during configuration at 3 ms. For the current Docker smoke, use a
temporary 1 ms copy of the warehouse world:

```bash
python3 - <<'PY'
from pathlib import Path

src = Path("/opt/ros/humble/share/turtlebot4_ignition_bringup/worlds/warehouse.sdf")
out = Path("/tmp/missionos_warehouse_1ms.sdf")
text = src.read_text().replace(
    "<max_step_size>0.003</max_step_size>",
    "<max_step_size>0.001</max_step_size>\n      <real_time_update_rate>1000</real_time_update_rate>",
)
out.write_text(text)
PY

xvfb-run -a ros2 launch ros_ign_gazebo ign_gazebo.launch.py \
  ign_args:="/tmp/missionos_warehouse_1ms.sdf -s -r -v 3"
```

In the current installed package, the Nav2/Slam arguments live on the spawn
launch file:

```bash
source /opt/ros/humble/setup.bash
ros2 launch turtlebot4_ignition_bringup turtlebot4_spawn.launch.py model:=lite world:=warehouse localization:=true nav2:=true rviz:=false
```

That command normally expects a Gazebo server/world to already be running. The
required MissionOS boundary is that a Nav2 `NavigateToPose` action server is
available at `/navigate_to_pose` and odometry is observable at `/odom`.
For localization runs, publish an initial pose before treating Nav2 as ready:

```bash
ros2 topic pub --once /initialpose geometry_msgs/msg/PoseWithCovarianceStamped \
  "{header: {frame_id: map}, pose: {pose: {position: {x: 0.0, y: 0.0, z: 0.0}, orientation: {w: 1.0}}}}"
```

## MissionOS Chat Profile

MissionOS can build the normal chat -> Gateway -> task-artifact proposal with a
TurtleBot4/Nav2 profile:

```bash
missionos chat --robot turtlebot4
```

or by asking for `TurtleBot4` / `TB4` in the operator instruction. The proposal
keeps the existing home-robot artifact keys for backward compatibility, but
records:

- `robot_profile=turtlebot4`
- `robot_model=turtlebot4_lite`
- `execution_target=ros2_nav2_turtlebot4_sim`

The CLI `operate`, `watch`, and `map` surfaces must treat those artifacts as a
TurtleBot4/Nav2 sim task while preserving the legacy `turtlebot3_*` artifact
keys for compatibility with existing task readers.

This is a profile selection, not a completion claim. Runtime completion still
requires the external TurtleBot4 bridge to report Nav2 success and non-zero
odometry motion, with `physical_execution_invoked=false`. If the bridge is not
configured, or if the TurtleBot4 simulator remains blocked at TF, lifecycle, or
controller motion, MissionOS must keep `completion_claimed=false` and surface
the blocking reasons.

## MissionOS Smoke

Run the smoke in the same ROS2 environment where the TurtleBot4 simulator and
Nav2 action server are visible:

```bash
source /opt/ros/humble/setup.bash
cd /work/missionos
export PYTHONPATH=/work/missionos:${PYTHONPATH:-}
export RUN_MISSIONOS_ROS2_NAV2_BOUNDED_DISPATCH_SMOKE=1
export RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT4_BRIDGE=1
export ROS2_NAV2_BRIDGE_COMMAND="python3 /work/missionos/scripts/ros2_nav2_turtlebot4_bridge.py"
python3 /work/missionos/scripts/smoke_ros2_nav2_turtlebot4_bounded_dispatch.py
```

Optional knobs:

- `ROS2_NAV2_GOAL_X_M` default `0.25`
- `ROS2_NAV2_GOAL_Y_M` default `0.0`
- `ROS2_NAV2_GOAL_FRAME_ID` default `map`
- `ROS2_NAV2_ACTION_NAME` default `/navigate_to_pose`
- `ROS2_NAV2_ODOM_TOPIC` default `/odom`
- `ROS2_NAV2_ACTION_SERVER_TIMEOUT_S` default `10`
- `ROS2_NAV2_GOAL_RESULT_TIMEOUT_S` default `45`
- `ROS2_NAV2_MOTION_THRESHOLD_M` default `0.03`
- `ROS2_NAV2_INITIALPOSE_ENABLE=1` publishes `/initialpose` before sending the
  goal
- `ROS2_NAV2_INITIALPOSE_X_M`, `ROS2_NAV2_INITIALPOSE_Y_M`, and
  `ROS2_NAV2_INITIALPOSE_YAW_RAD` default to `0.0`
- `ROS2_NAV2_INITIALPOSE_PUBLISHES` default `5`
- `ROS2_NAV2_TF_READY_TIMEOUT_S` waits for a TF path before the goal
- `ROS2_NAV2_TF_READY_TARGET_FRAME` default to the goal frame
- `ROS2_NAV2_TF_READY_SOURCE_FRAME` default `base_link`
- `ROS2_NAV2_REQUIRE_TF_READY=1` blocks the goal if TF readiness is not observed
- `ROS2_NAV2_LIFECYCLE_READY_TIMEOUT_S` waits for Nav2 lifecycle nodes to become
  active after initial-pose and TF readiness, before sending the goal
- `ROS2_NAV2_LIFECYCLE_NODES` default
  `amcl,map_server,controller_server,planner_server,bt_navigator`
- `ROS2_NAV2_REQUIRE_LIFECYCLE_READY=1` blocks the goal if those nodes are not
  active
- `ROS2_NAV2_VELOCITY_OBSERVE_ENABLE=1` subscribes to velocity command topics
  during the Nav2 result wait
- `ROS2_NAV2_VELOCITY_OBSERVE_TOPICS` default
  `/cmd_vel_nav,/cmd_vel,/diffdrive_controller/cmd_vel_unstamped`
- `ROS2_NAV2_VELOCITY_THRESHOLD` default `0.001`

When TF is the suspected blocker, run the read-only probe in the same ROS2
environment:

```bash
source /opt/ros/humble/setup.bash
cd /work/missionos
export RUN_MISSIONOS_ROS2_NAV2_TF_READINESS_PROBE=1
python3 /work/missionos/scripts/ros2_nav2_turtlebot4_tf_readiness_probe.py
```

The probe subscribes to `/tf` with volatile and transient-local QoS profiles and
to `/tf_static`, then reports the observed frame edges. It does not send a Nav2
goal, publish velocity, or claim completion.

Some TurtleBot4 simulator launches expose real `/odom` samples while late TF
consumers still miss a usable `odom -> base_link` transform. For that specific
diagnostic case, an opt-in odometry-to-TF republisher is available:

```bash
source /opt/ros/humble/setup.bash
cd /work/missionos
export RUN_MISSIONOS_ROS2_NAV2_ODOM_TF_REPUBLISHER=1
python3 /work/missionos/scripts/ros2_nav2_turtlebot4_odom_tf_republisher.py
```

This republisher copies the observed `/odom` pose into `/tf`. It must be reported
as a runtime shim if used. It does not prove robot motion by itself and must not
be treated as MissionOS completion.

Some installed TurtleBot4/Create3 simulator stacks expose Nav2 velocity on
`/cmd_vel_nav` or `/cmd_vel`, while the simulator controller listens on
`/diffdrive_controller/cmd_vel_unstamped`. For that wiring case, an opt-in
Nav2-velocity relay is available:

```bash
source /opt/ros/humble/setup.bash
cd /work/missionos
export RUN_MISSIONOS_ROS2_NAV2_NAV_VELOCITY_RELAY=1
python3 /work/missionos/scripts/ros2_nav2_turtlebot4_nav_velocity_relay.py
```

This relay copies Nav2-produced `geometry_msgs/Twist` messages from `/cmd_vel`
to `/diffdrive_controller/cmd_vel_unstamped`. It does not create velocity
commands, send a Nav2 goal, or claim completion. It publishes to the simulator
controller input, so every run that uses it must report
`nav2_velocity_relay_shim=true`, `velocity_generated_by_missionos=false`, and
`cmd_vel_published_by_missionos=false`.

If velocity reaches the controller input but `/odom` still does not move, isolate
the TurtleBot4 controller/physics layer with the opt-in raw-velocity diagnostic:

```bash
source /opt/ros/humble/setup.bash
cd /work/missionos
export RUN_MISSIONOS_ROS2_NAV2_CONTROLLER_MOTION_PROBE=1
python3 /work/missionos/scripts/ros2_nav2_turtlebot4_controller_motion_probe.py
```

This probe publishes a small diagnostic `Twist` directly to
`/diffdrive_controller/cmd_vel_unstamped` and checks `/odom` before and after.
It is deliberately not a MissionOS dispatch path: it reports
`missionos_dispatch_path=false`, `dispatch_request_sent=false`,
`diagnostic_raw_velocity_published=true`, and `completion_claimed=false`. A
successful probe proves only that the simulator controller and physics can move
the robot; it does not prove Nav2 or MissionOS bounded dispatch completion.
Set `ROS2_NAV2_CONTROLLER_MOTION_PROBE_COMMAND_RELIABILITY=best_effort` when
isolating a simulator graph whose controller endpoint requests best-effort
Twist messages. The probe result includes `target_command_sample_count`,
`target_command_nonzero_count`, and the maximum observed command magnitudes so
the diagnostic can distinguish "no command observed on the target topic" from
"command observed but `/odom` did not move."

## Evidence Rules

The smoke may claim `completion_scope=sim_action` only when all of these are
true:

- the adapter preflight passes
- an operator approval ref is present
- the bridge command sends a bounded Nav2 goal request
- the Nav2 action server accepts the goal
- Nav2 reports a succeeded result
- odometry before and after the goal shows robot motion above the configured
  threshold
- the bridge response keeps `physical_execution_invoked=false`
- the bridge response keeps `cmd_vel_published_by_missionos=false`
- any odom-to-TF or Nav2-velocity relay shim used in the simulator is reported
  separately and is not treated as MissionOS-generated raw velocity
- controller-motion probe results are diagnostic only and cannot be used as a
  bridge response or `sim_action` completion evidence

If Nav2 accepts the goal but odometry motion is not observed, the bridge returns
`nav2_succeeded_without_robot_motion_observed`. If Nav2 velocity is observed but
odometry still does not move, the bridge returns
`nav2_velocity_observed_without_odom_motion`. MissionOS must not claim sim
action completion in either case. ACK is not success.

## Current Boundary

Current checked-in tests cover:

- adapter evidence projection for simulated Nav2 completion
- command bridge subprocess boundary
- rejection of physical-execution claims from the bridge
- rejection of raw ROS topic publication claims from the bridge
- gate-off TurtleBot4 bridge behavior without importing ROS2
- gate-off controller-motion probe behavior without importing ROS2 or
  publishing diagnostic raw velocity

Current external verification for this slice also reached:

- Docker image build for ROS2 Humble/TurtleBot4 simulator dependencies
- ROS2/Nav2 Python imports inside that image
- gate-off MissionOS smoke inside that image
- `xvfb-run` server-only Gazebo `warehouse` world load in that image
- TurtleBot4 robot and dock entity creation in Gazebo
- Nav2 `/navigate_to_pose` action visibility
- MissionOS bounded dispatch sent to the real bridge/action boundary with
  `dispatch_request_sent=true`, while still reporting completion as false
- `ros2 control` diagnostics after adding `ros-humble-ros2controlcli`
- 1 ms warehouse physics stepping, which lets `joint_state_broadcaster` and
  `diffdrive_controller` configure and activate
- `/odom` readback from `diffdrive_controller`
- `localization:=true` map-server startup with `/map` observable
- deterministic bridge-side initial-pose publication, lifecycle-active wait,
  and TF readiness wait before sending the Nav2 goal
- with the opt-in odometry-to-TF shim started before Nav2 activation:
  `goal_accepted=true`, all configured lifecycle nodes active, TF readiness true,
  Nav2 feedback observed, and odometry before/after sampled
- with velocity observation enabled: non-zero Nav2 velocity was observed on
  `/cmd_vel_nav` and `/cmd_vel`
- with the opt-in Nav2-velocity relay shim enabled: non-zero Nav2 velocity was
  copied to `/diffdrive_controller/cmd_vel_unstamped`
- a controller-motion probe script now exists to isolate whether direct
  diagnostic velocity can produce `/odom` motion without involving Nav2 or
  MissionOS dispatch
- current Docker checks show the Create3 diffdrive spawner can hit its default
  10 second service-call timeout before `/odom` is available; when the spawner
  is patched inside the container with `--service-call-timeout 60.0` and
  `--switch-timeout 60.0`, `diffdrive_controller` can configure and activate
- with that timeout patch, the controller-motion probe published diagnostic raw
  velocity directly to `/diffdrive_controller/cmd_vel_unstamped`; `/odom`
  before/after were observed, but `odom_delta_m` was still only about `0.0019`
  after an 8 second `-0.25 m/s` command, so `robot_motion_observed=false`
- a 2026-07-06 runtime check with the normal MissionOS
  `chat --robot turtlebot4` -> approval -> run path created a TurtleBot4 task
  proposal and preserved `robot_profile=turtlebot4`,
  `execution_target=ros2_nav2_turtlebot4_sim`,
  `completion_claimed=false`, and `physical_execution_invoked=false`; without a
  configured ROS2 bridge in the Gateway environment it correctly blocked with
  bridge-command preflight reasons instead of claiming simulator completion
- a 2026-07-06 Docker check with split TurtleBot4 spawn,
  odometry-to-TF shim before localization/Nav2, bridge-side initial-pose
  publication, lifecycle wait, and extended bridge timeout showed that TF can
  become ready in this launch shape, but the startup remains flaky; retries can
  still lose `map -> odom` or miss `/navigate_to_pose`, so this is not yet a
  reproducible Nav2 completion path
- a 2026-07-06 controller-motion probe against the stock TurtleBot4 spawn found
  `joint_state_broadcaster=active` and `diffdrive_controller=active`; diagnostic
  velocity reached both `/diffdrive_controller/cmd_vel_unstamped` and
  `/cmd_vel`, but `/odom` changed by only about `5e-19 m` after an 8 second
  `0.25 m/s` command, so `robot_motion_observed=false`
- a 2026-07-06 temporary no-dock TurtleBot4 launch isolated the dock constraint:
  the upstream diffdrive spawner left `diffdrive_controller=unconfigured`;
  manually moving the controller through `inactive` then `active` succeeded, but
  the same direct diagnostic velocity still produced only about `5e-19 m` of
  `/odom` delta, so no-dock alone is not enough to reach TB3-equivalent motion
- 2026-07-06 pure Create3 checks showed the `/drive_distance` and
  `/rotate_angle` action servers accepting goals but timing out without wheel
  joint or odometry motion; direct diagnostic velocity to the active
  `diffdrive_controller` also left `/odom` effectively unchanged, so the current
  blocker is below Nav2 and below the MissionOS bridge
- 2026-07-06 replacing the installed Create3 xacro strings from
  `ign_ros2_control` to `gz_ros2_control` made the modern plugin load cleanly
  and claim the wheel velocity command interfaces, but direct velocity still
  produced only about `1.6e-17 m` of `/odom` delta; plugin-name migration alone
  is not enough to claim TB3-equivalent TurtleBot4 simulator motion

They do not yet prove Nav2 completion or simulator robot motion. The latest
Docker launch checks reached active diffdrive control, `/odom`, `/map`,
MissionOS `dispatch_request_sent=true`, and Nav2 `goal_accepted=true` through
the real bridge command. The same run still returned `completion_claimed=false`.
Nav2 remained `executing` until result timeout, velocity reached the simulator
controller input when the relay shim was enabled, and observed odometry delta
was still approximately zero, so `robot_motion_observed=false`. The next opt-in
runtime step is to fix the TurtleBot4/Gazebo controller layer before changing
Nav2: either make the diffdrive spawner timeout durable in the simulator setup
and remove the dock/startup constraint that leaves odometry nearly static, or
switch the simulator launch to a no-dock mobile base that can show meaningful
`/odom` motion. Only after direct diagnostic velocity produces real odometry
motion should the Nav2 relay, TF, and goal-frame path be retried. A
`sim_action` claim still requires both Nav2 success and non-zero odometry motion.

## Sources

- TurtleBot4 simulator user manual:
  `https://turtlebot.github.io/turtlebot4-user-manual/software/turtlebot4_simulator.html`
- TurtleBot4 simulator source:
  `https://github.com/turtlebot/turtlebot4_simulator`
- Nav2 action API:
  `https://api.nav2.org/actions/humble/navigatetopose.html`
