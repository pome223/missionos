#!/usr/bin/env bash
set -eo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${MISSIONOS_TB3_DOCKER_IMAGE:-missionos-ros2-nav2-turtlebot3:local}"
container="${MISSIONOS_TB3_GATEWAY_CONTAINER:-missionos-turtlebot3-gateway}"
host_port="${MISSIONOS_TB3_GATEWAY_PORT:-18791}"
gateway_port="${MISSIONOS_TB3_GATEWAY_CONTAINER_PORT:-18791}"

docker rm -f "${container}" >/dev/null 2>&1 || true

docker run --rm -d \
  --name "${container}" \
  --shm-size=1g \
  -p "127.0.0.1:${host_port}:${gateway_port}" \
  -e "MISSIONOS_TB3_GATEWAY_CONTAINER_PORT=${gateway_port}" \
  -e "MISSIONOS_TURTLEBOT3_WORLD_PROFILE=${MISSIONOS_TURTLEBOT3_WORLD_PROFILE:-house}" \
  -e "MISSIONOS_LLM_BACKEND=${MISSIONOS_LLM_BACKEND:-off}" \
  -e "MISSIONOS_OLLAMA_BASE_URL=${MISSIONOS_OLLAMA_BASE_URL:-}" \
  -e "MISSIONOS_OLLAMA_MODEL=${MISSIONOS_OLLAMA_MODEL:-}" \
  -e "MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED=${MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED:-0}" \
  -e "MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_TIMEOUT_SECONDS=${MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_TIMEOUT_SECONDS:-}" \
  -e "ROS2_NAV2_BRIDGE_TIMEOUT_S=${ROS2_NAV2_BRIDGE_TIMEOUT_S:-420}" \
  -e "ROS2_NAV2_GOAL_RESULT_TIMEOUT_S=${ROS2_NAV2_GOAL_RESULT_TIMEOUT_S:-180}" \
  -v "${repo_root}:/work/missionos" \
  -w /work/missionos \
  "${image}" \
  bash -lc '
set -eo pipefail

source /opt/ros/humble/setup.bash
source /opt/turtlebot3_ws/install/setup.bash
export PYTHONPATH=/work/missionos:/work/missionos/src:/work/missionos/packages/missionos-gateway/src:${PYTHONPATH:-}
export TURTLEBOT3_MODEL=burger

cleanup() {
  kill ${gateway_pid:-} ${telemetry_sidecar_pid:-} ${relay_pid:-} ${nav2_pid:-} ${gz_pid:-} 2>/dev/null || true
}
trap cleanup EXIT INT TERM

world_profile="${MISSIONOS_TURTLEBOT3_WORLD_PROFILE:-house}"
if [ "$world_profile" = "house" ]; then
  world_launch_dir=/opt/turtlebot3_ws/install/turtlebot3_gazebo/share/turtlebot3_gazebo/launch
  sed "s/turtlebot3_world\.world/turtlebot3_house.world/" \
    "$world_launch_dir/turtlebot3_world.launch.py" \
    >/tmp/turtlebot3_house_gz.launch.py
  world_launch=/tmp/turtlebot3_house_gz.launch.py
  spawn_world=default
  nav2_map_arg="map:=/work/missionos/config/turtlebot3_house/map.yaml"
else
  world_launch="turtlebot3_gazebo turtlebot3_world.launch.py"
  spawn_world=turtlebot3_world
  nav2_map_arg=""
fi

# shellcheck disable=SC2086
xvfb-run -a ros2 launch $world_launch \
  x_pose:=-2.0 y_pose:=-0.5 \
  >/tmp/missionos_gazebo_delivery.log 2>&1 &
gz_pid=$!
sleep "${MISSIONOS_TB3_GAZEBO_WAIT_SECONDS:-30}"

spawn_box() {
  name="$1"
  size_x="$2"
  size_y="$3"
  x_m="$4"
  y_m="$5"
  box_sdf="<sdf version=\"1.7\"><model name=\"${name}\"><static>true</static><link name=\"link\"><collision name=\"collision\"><geometry><box><size>${size_x} ${size_y} 0.5</size></box></geometry></collision><visual name=\"visual\"><geometry><box><size>${size_x} ${size_y} 0.5</size></box></geometry></visual></link></model></sdf>"
  set +e
  ros2 run ros_gz_sim create \
    -world "$spawn_world" \
    -string "$box_sdf" \
    -name "$name" \
    -x "$x_m" -y "$y_m" -z 0.25 \
    >>/tmp/missionos_spawn_obstacle.log 2>&1
  status=$?
  set -e
  printf "%s spawn_status=%s\n" "$name" "$status" >>/tmp/missionos_spawn_obstacle.log
}
: >/tmp/missionos_spawn_obstacle.log
if [ "$world_profile" = "house" ]; then
  spawn_box missionos_closed_door_blocker 0.32 0.32 0.2 -1.2
  spawn_box missionos_human_blocker 0.24 0.24 -1.75 1.6
  spawn_box missionos_dog_blocker 0.16 0.16 -0.7 2.6
else
  spawn_box missionos_closed_door_blocker 0.32 0.32 -1.15 -0.5
  spawn_box missionos_human_blocker 0.24 0.24 -1.00 0.55
  spawn_box missionos_dog_blocker 0.16 0.16 0.70 0.55
fi

# shellcheck disable=SC2086
xvfb-run -a ros2 launch turtlebot3_navigation2 navigation2.launch.py \
  use_sim_time:=True $nav2_map_arg \
  >/tmp/missionos_nav2_delivery.log 2>&1 &
nav2_pid=$!
sleep "${MISSIONOS_TB3_NAV2_WAIT_SECONDS:-55}"

RUN_MISSIONOS_ROS2_NAV2_NAV_VELOCITY_RELAY=1 \
ROS2_NAV2_NAV_VELOCITY_RELAY_SOURCE_TOPIC=/cmd_vel_nav \
ROS2_NAV2_NAV_VELOCITY_RELAY_TARGET_TOPIC=/cmd_vel \
python3 /work/missionos/scripts/ros2_nav2_turtlebot3_nav_velocity_relay.py \
  >/tmp/missionos_relay_delivery.log 2>&1 &
relay_pid=$!
sleep 5

telemetry_sidecar_jsonl=/tmp/missionos_turtlebot3_telemetry_sidecar.jsonl
rm -f "$telemetry_sidecar_jsonl"
python3 /work/missionos/scripts/ros2_nav2_turtlebot3_telemetry_sidecar.py \
  --output "$telemetry_sidecar_jsonl" \
  --duration-s "${MISSIONOS_TB3_TELEMETRY_DURATION_S:-7200}" \
  --max-samples "${MISSIONOS_TB3_TELEMETRY_MAX_SAMPLES:-240000}" \
  >/tmp/missionos_turtlebot3_telemetry_sidecar.log 2>&1 &
telemetry_sidecar_pid=$!

export RUN_MISSIONOS_ROS2_NAV2_BOUNDED_DISPATCH_SMOKE=1
export RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT3_BRIDGE=1
export ROS2_NAV2_BRIDGE_COMMAND="python3 /work/missionos/scripts/ros2_nav2_turtlebot3_bridge.py"
export ROS2_NAV2_BRIDGE_TIMEOUT_S="${ROS2_NAV2_BRIDGE_TIMEOUT_S:-420}"
export ROS2_NAV2_INITIALPOSE_ENABLE=1
export ROS2_NAV2_INITIALPOSE_X_M=-2.0
export ROS2_NAV2_INITIALPOSE_Y_M=-0.5
export ROS2_NAV2_INITIALPOSE_PUBLISHES=12
export ROS2_NAV2_TF_READY_TIMEOUT_S=30
export ROS2_NAV2_REQUIRE_TF_READY=1
export ROS2_NAV2_LIFECYCLE_READY_TIMEOUT_S=40
export ROS2_NAV2_REQUIRE_LIFECYCLE_READY=1
export ROS2_NAV2_VELOCITY_OBSERVE_ENABLE=1
export ROS2_NAV2_OBSTACLE_OBSERVE_ENABLE=1
export ROS2_NAV2_TRAJECTORY_OBSERVE_ENABLE=1
export ROS2_NAV2_TRAJECTORY_LATERAL_DEVIATION_THRESHOLD_M=0.03
export ROS2_NAV2_GOAL_RESULT_TIMEOUT_S="${ROS2_NAV2_GOAL_RESULT_TIMEOUT_S:-180}"
export ROS2_NAV2_POST_RESULT_SETTLE_S=3.0
export MISSIONOS_TURTLEBOT3_TELEMETRY_SIDECAR_JSONL="$telemetry_sidecar_jsonl"
export MISSIONOS_TURTLEBOT3_LOG_BUNDLE_PATHS="{\"gazebo\":\"/tmp/missionos_gazebo_delivery.log\",\"nav2\":\"/tmp/missionos_nav2_delivery.log\",\"relay\":\"/tmp/missionos_relay_delivery.log\",\"telemetry_sidecar\":\"/tmp/missionos_turtlebot3_telemetry_sidecar.log\",\"spawn_obstacle\":\"/tmp/missionos_spawn_obstacle.log\"}"
export MISSIONOS_GATEWAY_BACKEND=production

python3 -m missionos_gateway web --host 0.0.0.0 --port "${MISSIONOS_TB3_GATEWAY_CONTAINER_PORT:-18791}" &
gateway_pid=$!
wait "$gateway_pid"
'

python3 - "${host_port}" "${container}" <<'PY'
import json
import sys
import time
from urllib import request, error

port = sys.argv[1]
container = sys.argv[2]
deadline = time.monotonic() + 180.0
url = f"http://127.0.0.1:{port}/health"
last_error = ""
while time.monotonic() < deadline:
    try:
        with request.urlopen(url, timeout=2.0) as response:
            if response.status == 200:
                print(
                    json.dumps(
                        {
                            "turtlebot3_gateway_container": container,
                            "gateway_url": f"http://127.0.0.1:{port}",
                            "status": "ready",
                        },
                        sort_keys=True,
                    )
                )
                raise SystemExit(0)
    except (OSError, error.URLError) as exc:
        last_error = str(exc)
    time.sleep(1.0)

print(
    json.dumps(
        {
            "turtlebot3_gateway_container": container,
            "gateway_url": f"http://127.0.0.1:{port}",
            "status": "timeout",
            "last_error": last_error,
        },
        sort_keys=True,
    ),
    file=sys.stderr,
)
raise SystemExit(1)
PY
