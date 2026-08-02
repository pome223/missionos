#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${MISSIONOS_TB3_DOCKER_IMAGE:-missionos-ros2-nav2-turtlebot3:local}"
artifact_root="${ROS2_NAV2_MISSION_CONTRACT_ARTIFACT_ROOT:-}"
if [[ -z "${artifact_root}" ]]; then
  artifact_root="$(mktemp -d /tmp/missionos-nav2-contract-XXXXXX)"
fi
mkdir -p "${artifact_root}"
echo "ROS2_NAV2_MISSION_CONTRACT_ARTIFACT_ROOT=${artifact_root}"

docker run --rm --platform linux/amd64 --shm-size=1g \
  -v "${repo_root}:/work/missionos" \
  -v "${artifact_root}:/artifacts" \
  -w /work/missionos \
  "${image}" \
  /bin/bash -lc '
set -eo pipefail
source /opt/ros/humble/setup.bash
source /opt/turtlebot3_ws/install/setup.bash
set -u
export PYTHONPATH="/work/missionos:/work/missionos/packages/missionos-core/src:${PYTHONPATH:-}"
export TURTLEBOT3_MODEL=burger

xvfb-run -a ros2 launch turtlebot3_gazebo turtlebot3_world.launch.py \
  >/tmp/missionos-nav2-gazebo.log 2>&1 &
gazebo_pid=$!
xvfb-run -a ros2 launch turtlebot3_navigation2 navigation2.launch.py \
  use_sim_time:=True >/tmp/missionos-nav2-stack.log 2>&1 &
nav2_pid=$!

cleanup() {
  kill "${relay_pid:-}" "${nav2_pid}" "${gazebo_pid}" 2>/dev/null || true
  wait "${relay_pid:-}" "${nav2_pid}" "${gazebo_pid}" 2>/dev/null || true
}
trap cleanup EXIT

sleep 38
export RUN_MISSIONOS_ROS2_NAV2_NAV_VELOCITY_RELAY=1
export ROS2_NAV2_NAV_VELOCITY_RELAY_SOURCE_TOPIC=/cmd_vel_nav
export ROS2_NAV2_NAV_VELOCITY_RELAY_TARGET_TOPIC=/cmd_vel
python3 scripts/ros2_nav2_turtlebot3_nav_velocity_relay.py \
  >/tmp/missionos-nav2-relay.log 2>&1 &
relay_pid=$!
sleep 3

export RUN_MISSIONOS_ROS2_NAV2_BOUNDED_DISPATCH_SMOKE=1
export RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT3_BRIDGE=1
export ROS2_NAV2_BRIDGE_COMMAND="python3 /work/missionos/scripts/ros2_nav2_turtlebot3_bridge.py"
export ROS2_NAV2_BRIDGE_TIMEOUT_S=420
export ROS2_NAV2_GOAL_X_M=0.75
export ROS2_NAV2_GOAL_Y_M=0.0
export ROS2_NAV2_INITIALPOSE_ENABLE=1
export ROS2_NAV2_INITIALPOSE_X_M=0.25
export ROS2_NAV2_INITIALPOSE_Y_M=0.0
export ROS2_NAV2_INITIALPOSE_PUBLISHES=10
export ROS2_NAV2_TF_READY_TIMEOUT_S=20
export ROS2_NAV2_REQUIRE_TF_READY=1
export ROS2_NAV2_LIFECYCLE_READY_TIMEOUT_S=20
export ROS2_NAV2_REQUIRE_LIFECYCLE_READY=1
export ROS2_NAV2_VELOCITY_OBSERVE_ENABLE=1
export ROS2_NAV2_GOAL_RESULT_TIMEOUT_S=90
export ROS2_NAV2_POST_RESULT_SETTLE_S=2.0
export ROS2_NAV2_MISSION_CONTRACT_ARTIFACT_ROOT=/artifacts
python3 scripts/smoke_ros2_nav2_turtlebot3_bounded_dispatch.py
'
