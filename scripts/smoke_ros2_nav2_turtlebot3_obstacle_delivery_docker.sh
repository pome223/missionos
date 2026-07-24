#!/usr/bin/env bash
set -eo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${MISSIONOS_TB3_DOCKER_IMAGE:-missionos-ros2-nav2-turtlebot3:local}"
instruction="${MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_INSTRUCTION:-TurtleBot3 indoor delivery route with obstacle avoidance}"
gateway_llm_backend="${MISSIONOS_LLM_BACKEND:-deepseek}"
planner_backend="${MISSIONOS_AGENT_MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_AGENT_LLM_BACKEND:-deepseek}"
if [ "$planner_backend" = deepseek ]; then
  planner_default_model_id=deepseek-v4-flash
else
  planner_default_model_id=gemini-3.1-flash-lite
fi
planner_model_id="${MISSIONOS_AGENT_MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_AGENT_MODEL_ID:-${MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_MODEL_ID:-${planner_default_model_id}}}"
planner_adk_enabled="${MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED:-1}"

docker run --rm -i --shm-size=1g \
  -e "MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_INSTRUCTION=${instruction}" \
  -e "MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_RECOVERY_INSTRUCTION=${MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_RECOVERY_INSTRUCTION:-}" \
  -e "MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_HTTP_TIMEOUT_SECONDS=${MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_HTTP_TIMEOUT_SECONDS:-600}" \
  -e "MISSIONOS_TB3_SKIP_GATEWAY_DEP_INSTALL=${MISSIONOS_TB3_SKIP_GATEWAY_DEP_INSTALL:-0}" \
  -e "MISSIONOS_LLM_BACKEND=${gateway_llm_backend}" \
  -e "DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY:-}" \
  -e "MISSIONOS_DEEPSEEK_MODEL=${MISSIONOS_DEEPSEEK_MODEL:-deepseek-v4-flash}" \
  -e "MISSIONOS_DEEPSEEK_API_BASE=${MISSIONOS_DEEPSEEK_API_BASE:-https://api.deepseek.com}" \
  -e "GOOGLE_API_KEY=${GOOGLE_API_KEY:-}" \
  -e "GOOGLE_GENAI_USE_VERTEXAI=${GOOGLE_GENAI_USE_VERTEXAI:-false}" \
  -e "MISSIONOS_AGENT_MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_AGENT_LLM_BACKEND=${planner_backend}" \
  -e "MISSIONOS_AGENT_MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_AGENT_MODEL_ID=${planner_model_id}" \
  -e "MISSIONOS_AGENT_MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_AGENT_OLLAMA_MODEL=${MISSIONOS_AGENT_MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_AGENT_OLLAMA_MODEL:-}" \
  -e "MISSIONOS_AGENT_MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_AGENT_OLLAMA_BASE_URL=${MISSIONOS_AGENT_MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_AGENT_OLLAMA_BASE_URL:-}" \
  -e "MISSIONOS_OLLAMA_BASE_URL=${MISSIONOS_OLLAMA_BASE_URL:-}" \
  -e "MISSIONOS_OLLAMA_MODEL=${MISSIONOS_OLLAMA_MODEL:-}" \
  -e "MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED=${planner_adk_enabled}" \
  -e "MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_COMMAND=${MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_COMMAND:-}" \
  -e "MISSIONOS_ALLOW_TURTLEBOT3_RECOVERY_PLANNER_COMMAND_OVERRIDE=${MISSIONOS_ALLOW_TURTLEBOT3_RECOVERY_PLANNER_COMMAND_OVERRIDE:-}" \
  -e "MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_MODEL_ID=${MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_MODEL_ID:-${planner_model_id}}" \
  -e "MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_TIMEOUT_SECONDS=${MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_TIMEOUT_SECONDS:-}" \
  -e "MISSIONOS_EXPECT_TURTLEBOT3_RECOVERY_PROPOSAL_SOURCE=${MISSIONOS_EXPECT_TURTLEBOT3_RECOVERY_PROPOSAL_SOURCE:-}" \
  -e "MISSIONOS_CHAT_TURTLEBOT3_MID_RECOVERY_SMOKE=${MISSIONOS_CHAT_TURTLEBOT3_MID_RECOVERY_SMOKE:-0}" \
  -e "MISSIONOS_CHAT_TURTLEBOT3_DYNAMIC_OBSTACLE_RECOVERY_SMOKE=${MISSIONOS_CHAT_TURTLEBOT3_DYNAMIC_OBSTACLE_RECOVERY_SMOKE:-0}" \
  -e "MISSIONOS_CHAT_TURTLEBOT3_DECISION_DEMO_SMOKE=${MISSIONOS_CHAT_TURTLEBOT3_DECISION_DEMO_SMOKE:-0}" \
  -e "MISSIONOS_CHAT_TURTLEBOT3_HUMAN_APPROVAL_DEMO_SMOKE=${MISSIONOS_CHAT_TURTLEBOT3_HUMAN_APPROVAL_DEMO_SMOKE:-0}" \
  -e "MISSIONOS_TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL=${MISSIONOS_TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL:-0}" \
  -e "MISSIONOS_CHAT_TURTLEBOT3_RECOVERY_GUARDRAIL_FALLBACK_SMOKE=${MISSIONOS_CHAT_TURTLEBOT3_RECOVERY_GUARDRAIL_FALLBACK_SMOKE:-0}" \
  -e "MISSIONOS_CHAT_TURTLEBOT3_LOCALIZATION_DRIFT_FAULT_SMOKE=${MISSIONOS_CHAT_TURTLEBOT3_LOCALIZATION_DRIFT_FAULT_SMOKE:-0}" \
  -e "MISSIONOS_CHAT_TURTLEBOT3_LOCALIZATION_DRIFT_INITIALPOSE_X_M=${MISSIONOS_CHAT_TURTLEBOT3_LOCALIZATION_DRIFT_INITIALPOSE_X_M:-8.0}" \
  -e "MISSIONOS_CHAT_TURTLEBOT3_LOCALIZATION_DRIFT_INITIALPOSE_Y_M=${MISSIONOS_CHAT_TURTLEBOT3_LOCALIZATION_DRIFT_INITIALPOSE_Y_M:-8.0}" \
  -e "MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_MAP_MODEL_OUT=${MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_MAP_MODEL_OUT:-}" \
  -e "MISSIONOS_TURTLEBOT3_WORLD_PROFILE=${MISSIONOS_TURTLEBOT3_WORLD_PROFILE:-house}" \
  -e "MISSIONOS_TURTLEBOT3_CAMERA_PERCEPTION_ENABLED=${MISSIONOS_TURTLEBOT3_CAMERA_PERCEPTION_ENABLED:-0}" \
  -e "MISSIONOS_TURTLEBOT3_SIM_MODEL=${MISSIONOS_TURTLEBOT3_SIM_MODEL:-}" \
  -e "ROS2_NAV2_CAMERA_TOPIC=${ROS2_NAV2_CAMERA_TOPIC:-}" \
  -e "ROS2_NAV2_CAMERA_INFO_TOPIC=${ROS2_NAV2_CAMERA_INFO_TOPIC:-}" \
  -e "ROS2_NAV2_LIDAR_TOPIC=${ROS2_NAV2_LIDAR_TOPIC:-}" \
  -e "ROS2_NAV2_CAMERA_LIDAR_OBSTACLE_RANGE_M=${ROS2_NAV2_CAMERA_LIDAR_OBSTACLE_RANGE_M:-}" \
  -e "ROS2_NAV2_CAMERA_TIMEOUT_S=${ROS2_NAV2_CAMERA_TIMEOUT_S:-}" \
  -e "MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED=${MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED:-0}" \
  -e "MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_MODEL_ID=${MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_MODEL_ID:-}" \
  -e "MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND=${MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND:-}" \
  -e "MISSIONOS_ALLOW_TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_OVERRIDE=${MISSIONOS_ALLOW_TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND_OVERRIDE:-}" \
  -e "MISSIONOS_TURTLEBOT3_PROMOTED_ACTIONS_JSON=${MISSIONOS_TURTLEBOT3_PROMOTED_ACTIONS_JSON:-}" \
  -e "ROS2_NAV2_BRIDGE_TIMEOUT_S=${ROS2_NAV2_BRIDGE_TIMEOUT_S:-420}" \
  -e "ROS2_NAV2_GOAL_RESULT_TIMEOUT_S=${ROS2_NAV2_GOAL_RESULT_TIMEOUT_S:-180}" \
  -e "MISSIONOS_TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION=${MISSIONOS_TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION:-1}" \
  -e "MISSIONOS_TURTLEBOT3_RECOVERY_CANDIDATE_CLEARANCE_M=${MISSIONOS_TURTLEBOT3_RECOVERY_CANDIDATE_CLEARANCE_M:-0.75}" \
  -e "ROS2_NAV2_USE_SIM_TIME=${ROS2_NAV2_USE_SIM_TIME:-1}" \
  -v "${repo_root}:/work/missionos" \
  -w /work/missionos \
  "${image}" \
  bash -s <<'CONTAINER'
set -eo pipefail

if [ "${MISSIONOS_TB3_SKIP_GATEWAY_DEP_INSTALL:-0}" != "1" ]; then
  if ! python3 - <<'PY' >/tmp/missionos_gateway_dep_probe.log 2>&1
import fastapi
import google.adk
import litellm
import uvicorn
PY
  then
    pip3 install --no-cache-dir \
      "fastapi>=0.110.0" \
      "uvicorn[standard]>=0.29.0" \
      "python-dotenv>=1.0.0" \
      "pyyaml>=6.0.0" \
      "click>=8.0.0" \
      "rich>=13.0.0" \
      "google-adk[extensions]>=0.1.0" \
      "websockets>=12.0" \
      "httpx>=0.27.0" \
      "ddgs>=0.1.0" \
      "croniter>=1.3.0" \
      "mcp>=1.0.0" \
      >/tmp/missionos_pip_gateway.log 2>&1
  fi
fi

source /opt/ros/humble/setup.bash
source /opt/turtlebot3_ws/install/setup.bash
export PYTHONPATH=/work/missionos:/work/missionos/src:/work/missionos/packages/missionos-core/src:/work/missionos/packages/missionos-gateway/src:${PYTHONPATH:-}
if [ "${MISSIONOS_TURTLEBOT3_CAMERA_PERCEPTION_ENABLED:-0}" = "1" ]; then
  export TURTLEBOT3_MODEL="${MISSIONOS_TURTLEBOT3_SIM_MODEL:-waffle_pi}"
else
  export TURTLEBOT3_MODEL="${MISSIONOS_TURTLEBOT3_SIM_MODEL:-burger}"
fi

cleanup() {
  kill ${telemetry_sidecar_pid:-} ${relay_pid:-} ${nav2_pid:-} ${gz_pid:-} 2>/dev/null || true
}

finish() {
  status=$?
  if [ "$status" -ne 0 ]; then
    for log_file in /tmp/missionos_*; do
      [ -f "$log_file" ] || continue
      printf "\n===== %s =====\n" "$log_file"
      tail -120 "$log_file" || true
    done
  fi
  # Keep Nav2/Gazebo tails on the mounted repo for post-run diagnosis.
  diag_dir=/work/missionos/output/turtlebot3_smoke
  mkdir -p "$diag_dir" 2>/dev/null || true
  tail -400 /tmp/missionos_nav2_delivery.log >"$diag_dir/last_nav2_delivery_tail.log" 2>/dev/null || true
  tail -200 /tmp/missionos_gazebo_delivery.log >"$diag_dir/last_gazebo_delivery_tail.log" 2>/dev/null || true
  cleanup
  return "$status"
}
trap finish EXIT

world_profile="${MISSIONOS_TURTLEBOT3_WORLD_PROFILE:-house}"
if [ "$world_profile" = "house" ]; then
  # The stock turtlebot3_house.launch.py still targets Gazebo Classic
  # (gazebo_ros), which this gz-sim image does not ship. Reuse the migrated
  # turtlebot3_world launch with the house world swapped in.
  world_launch_dir=/opt/turtlebot3_ws/install/turtlebot3_gazebo/share/turtlebot3_gazebo/launch
  sed "s/turtlebot3_world\.world/turtlebot3_house.world/" \
    "$world_launch_dir/turtlebot3_world.launch.py" \
    >/tmp/turtlebot3_house_gz.launch.py
  world_launch=/tmp/turtlebot3_house_gz.launch.py
  # turtlebot3_house.world declares <world name="default">.
  spawn_world="default"
  nav2_map_arg="map:=/work/missionos/config/turtlebot3_house/map.yaml"
else
  world_launch="turtlebot3_gazebo turtlebot3_world.launch.py"
  spawn_world="turtlebot3_world"
  nav2_map_arg=""
fi

# shellcheck disable=SC2086
xvfb-run -a ros2 launch $world_launch \
  x_pose:=-2.0 y_pose:=-0.5 \
  >/tmp/missionos_gazebo_delivery.log 2>&1 &
gz_pid=$!
sleep 30

: >/tmp/missionos_spawn_obstacle.log
spawn_status=0
spawned_count=0
spawn_model() {
  name="$1"
  x_m="$2"
  y_m="$3"
  z_m="$4"
  model_sdf="$5"
  set +e
  ros2 run ros_gz_sim create \
    -world "$spawn_world" \
    -string "$model_sdf" \
    -name "$name" \
    -x "$x_m" -y "$y_m" -z "$z_m" \
    >>/tmp/missionos_spawn_obstacle.log 2>&1
  status=$?
  set -e
  printf "%s spawn_status=%s\n" "$name" "$status" >>/tmp/missionos_spawn_obstacle.log
  if [ "$status" -ne 0 ]; then
    spawn_status="$status"
  else
    spawned_count=$((spawned_count + 1))
  fi
}

spawn_box() {
  name="$1"
  size_x="$2"
  size_y="$3"
  x_m="$4"
  y_m="$5"
  box_sdf="<sdf version='1.7'><model name='${name}'><static>true</static><link name='link'><collision name='collision'><geometry><box><size>${size_x} ${size_y} 0.5</size></box></geometry></collision><visual name='visual'><geometry><box><size>${size_x} ${size_y} 0.5</size></box></geometry><material><ambient>0.35 0.18 0.05 1</ambient><diffuse>0.35 0.18 0.05 1</diffuse></material></visual></link></model></sdf>"
  spawn_model "$name" "$x_m" "$y_m" 0.25 "$box_sdf"
}

spawn_humanoid() {
  name="$1"
  x_m="$2"
  y_m="$3"
  humanoid_sdf="<sdf version='1.7'><model name='${name}'><static>true</static><link name='base'><pose>0 0 0.10 0 0 0</pose><collision name='collision'><geometry><box><size>0.42 0.32 0.20</size></box></geometry></collision><visual name='visual'><geometry><box><size>0.42 0.32 0.20</size></box></geometry><material><ambient>0.12 0.12 0.16 1</ambient><diffuse>0.12 0.12 0.16 1</diffuse></material></visual></link><link name='left_leg'><pose>0 0.12 0.40 0 0 0</pose><collision name='collision'><geometry><cylinder><radius>0.08</radius><length>0.60</length></cylinder></geometry></collision><visual name='visual'><geometry><cylinder><radius>0.08</radius><length>0.60</length></cylinder></geometry><material><ambient>0.08 0.20 0.34 1</ambient><diffuse>0.08 0.20 0.34 1</diffuse></material></visual></link><link name='right_leg'><pose>0 -0.12 0.40 0 0 0</pose><collision name='collision'><geometry><cylinder><radius>0.08</radius><length>0.60</length></cylinder></geometry></collision><visual name='visual'><geometry><cylinder><radius>0.08</radius><length>0.60</length></cylinder></geometry><material><ambient>0.08 0.20 0.34 1</ambient><diffuse>0.08 0.20 0.34 1</diffuse></material></visual></link><link name='torso'><pose>0 0 1.08 0 0 0</pose><collision name='collision'><geometry><box><size>0.42 0.28 0.58</size></box></geometry></collision><visual name='visual'><geometry><box><size>0.42 0.28 0.58</size></box></geometry><material><ambient>0.75 0.80 0.86 1</ambient><diffuse>0.75 0.80 0.86 1</diffuse></material></visual></link><link name='left_arm'><pose>0 0.25 1.10 0 0 0</pose><collision name='collision'><geometry><box><size>0.12 0.12 0.54</size></box></geometry></collision><visual name='visual'><geometry><box><size>0.12 0.12 0.54</size></box></geometry><material><ambient>0.12 0.38 0.75 1</ambient><diffuse>0.12 0.38 0.75 1</diffuse></material></visual></link><link name='right_arm'><pose>0 -0.25 1.10 0 0 0</pose><collision name='collision'><geometry><box><size>0.12 0.12 0.54</size></box></geometry></collision><visual name='visual'><geometry><box><size>0.12 0.12 0.54</size></box></geometry><material><ambient>0.12 0.38 0.75 1</ambient><diffuse>0.12 0.38 0.75 1</diffuse></material></visual></link><link name='head'><pose>0 0 1.60 0 0 0</pose><collision name='collision'><geometry><sphere><radius>0.18</radius></sphere></geometry></collision><visual name='visual'><geometry><sphere><radius>0.18</radius></sphere></geometry><material><ambient>0.88 0.90 0.94 1</ambient><diffuse>0.88 0.90 0.94 1</diffuse></material></visual></link></model></sdf>"
  spawn_model "$name" "$x_m" "$y_m" 0 "$humanoid_sdf"
}

spawn_robot_dog() {
  name="$1"
  x_m="$2"
  y_m="$3"
  robot_dog_sdf="<sdf version='1.7'><model name='${name}'><static>true</static><link name='body'><pose>0 0 0.44 0 0 0</pose><collision name='collision'><geometry><box><size>0.70 0.38 0.26</size></box></geometry></collision><visual name='visual'><geometry><box><size>0.70 0.38 0.26</size></box></geometry><material><ambient>0.10 0.34 0.78 1</ambient><diffuse>0.10 0.34 0.78 1</diffuse></material></visual></link><link name='head'><pose>0.42 0 0.46 0 0 0</pose><collision name='collision'><geometry><box><size>0.20 0.28 0.20</size></box></geometry></collision><visual name='visual'><geometry><box><size>0.20 0.28 0.20</size></box></geometry><material><ambient>0.75 0.80 0.86 1</ambient><diffuse>0.75 0.80 0.86 1</diffuse></material></visual></link><link name='front_left_leg'><pose>0.24 0.17 0.20 0 0 0</pose><collision name='collision'><geometry><cylinder><radius>0.055</radius><length>0.40</length></cylinder></geometry></collision><visual name='visual'><geometry><cylinder><radius>0.055</radius><length>0.40</length></cylinder></geometry><material><ambient>0.18 0.20 0.24 1</ambient><diffuse>0.18 0.20 0.24 1</diffuse></material></visual></link><link name='front_right_leg'><pose>0.24 -0.17 0.20 0 0 0</pose><collision name='collision'><geometry><cylinder><radius>0.055</radius><length>0.40</length></cylinder></geometry></collision><visual name='visual'><geometry><cylinder><radius>0.055</radius><length>0.40</length></cylinder></geometry><material><ambient>0.18 0.20 0.24 1</ambient><diffuse>0.18 0.20 0.24 1</diffuse></material></visual></link><link name='rear_left_leg'><pose>-0.24 0.17 0.20 0 0 0</pose><collision name='collision'><geometry><cylinder><radius>0.055</radius><length>0.40</length></cylinder></geometry></collision><visual name='visual'><geometry><cylinder><radius>0.055</radius><length>0.40</length></cylinder></geometry><material><ambient>0.18 0.20 0.24 1</ambient><diffuse>0.18 0.20 0.24 1</diffuse></material></visual></link><link name='rear_right_leg'><pose>-0.24 -0.17 0.20 0 0 0</pose><collision name='collision'><geometry><cylinder><radius>0.055</radius><length>0.40</length></cylinder></geometry></collision><visual name='visual'><geometry><cylinder><radius>0.055</radius><length>0.40</length></cylinder></geometry><material><ambient>0.18 0.20 0.24 1</ambient><diffuse>0.18 0.20 0.24 1</diffuse></material></visual></link></model></sdf>"
  spawn_model "$name" "$x_m" "$y_m" 0 "$robot_dog_sdf"
}
if [ "$world_profile" = "house" ]; then
  spawn_box missionos_closed_door_blocker 0.32 0.32 0.2 -1.2
  spawn_humanoid missionos_humanoid_blocker -1.75 1.6
  spawn_robot_dog missionos_robot_dog_blocker -0.7 2.6
else
  spawn_box missionos_closed_door_blocker 0.32 0.32 -1.15 -0.5
  spawn_humanoid missionos_humanoid_blocker -1.00 0.55
  spawn_robot_dog missionos_robot_dog_blocker 0.70 0.55
fi

# shellcheck disable=SC2086
xvfb-run -a ros2 launch turtlebot3_navigation2 navigation2.launch.py \
  use_sim_time:=True $nav2_map_arg \
  >/tmp/missionos_nav2_delivery.log 2>&1 &
nav2_pid=$!
sleep 55

RUN_MISSIONOS_ROS2_NAV2_NAV_VELOCITY_RELAY=1 \
ROS2_NAV2_NAV_VELOCITY_RELAY_SOURCE_TOPIC=/cmd_vel_nav \
ROS2_NAV2_NAV_VELOCITY_RELAY_TARGET_TOPIC=/cmd_vel \
python3 /work/missionos/scripts/ros2_nav2_turtlebot3_nav_velocity_relay.py \
  >/tmp/missionos_relay_delivery.log 2>&1 &
relay_pid=$!
sleep 5

telemetry_sidecar_jsonl=/tmp/missionos_turtlebot3_telemetry_sidecar.jsonl
telemetry_live_task_id_path=/tmp/missionos_turtlebot3_live_task_id
rm -f "$telemetry_sidecar_jsonl"
rm -f "$telemetry_live_task_id_path"
python3 /work/missionos/scripts/ros2_nav2_turtlebot3_telemetry_sidecar.py \
  --output "$telemetry_sidecar_jsonl" \
  --task-id-path "$telemetry_live_task_id_path" \
  --duration-s 600 \
  --max-samples 12000 \
  >/tmp/missionos_turtlebot3_telemetry_sidecar.log 2>&1 &
telemetry_sidecar_pid=$!
sleep 2

export RUN_MISSIONOS_ROS2_NAV2_BOUNDED_DISPATCH_SMOKE=1
export RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT3_BRIDGE=1
export ROS2_NAV2_BRIDGE_COMMAND="python3 /work/missionos/scripts/ros2_nav2_turtlebot3_bridge.py"
export ROS2_NAV2_BRIDGE_TIMEOUT_S="${ROS2_NAV2_BRIDGE_TIMEOUT_S:-420}"
export ROS2_NAV2_USE_SIM_TIME="${ROS2_NAV2_USE_SIM_TIME:-1}"
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
export ROS2_NAV2_RECOVERY_ORBIT_GUARD_ENABLE=1
export ROS2_NAV2_RECOVERY_ORBIT_MIN_DURATION_S="${ROS2_NAV2_RECOVERY_ORBIT_MIN_DURATION_S:-25}"
export ROS2_NAV2_RECOVERY_ORBIT_NO_PROGRESS_S="${ROS2_NAV2_RECOVERY_ORBIT_NO_PROGRESS_S:-18}"
export ROS2_NAV2_RECOVERY_ORBIT_MIN_PATH_M="${ROS2_NAV2_RECOVERY_ORBIT_MIN_PATH_M:-1.0}"
export ROS2_NAV2_GOAL_RESULT_TIMEOUT_S="${ROS2_NAV2_GOAL_RESULT_TIMEOUT_S:-180}"
export ROS2_NAV2_POST_RESULT_SETTLE_S=3.0
export MISSIONOS_TURTLEBOT3_TELEMETRY_SIDECAR_JSONL="$telemetry_sidecar_jsonl"
export MISSIONOS_TURTLEBOT3_LIVE_TASK_ID_PATH="$telemetry_live_task_id_path"
export MISSIONOS_TURTLEBOT3_LOG_BUNDLE_PATHS='{"gazebo":"/tmp/missionos_gazebo_delivery.log","nav2":"/tmp/missionos_nav2_delivery.log","relay":"/tmp/missionos_relay_delivery.log","telemetry_sidecar":"/tmp/missionos_turtlebot3_telemetry_sidecar.log","spawn_obstacle":"/tmp/missionos_spawn_obstacle.log"}'
export RUN_MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_SMOKE_WITH_BRIDGE=1
case "${MISSIONOS_CHAT_TURTLEBOT3_RECOVERY_GUARDRAIL_FALLBACK_SMOKE:-0}" in
1|true|TRUE|yes|YES|on|ON)
  export MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED=0
  export MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_COMMAND="python3 /work/missionos/scripts/turtlebot3_recovery_planner_guardrail_fault_fixture.py"
  export MISSIONOS_ALLOW_TURTLEBOT3_RECOVERY_PLANNER_COMMAND_OVERRIDE=1
  export MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_MODEL_ID="guardrail_fault_fixture"
  export MISSIONOS_EXPECT_TURTLEBOT3_RECOVERY_PROPOSAL_SOURCE="${MISSIONOS_EXPECT_TURTLEBOT3_RECOVERY_PROPOSAL_SOURCE:-deterministic_fallback}"
  ;;
esac

printf "sim_obstacle_spawn_status=%s\n" "$spawn_status"
printf "sim_obstacle_spawned_count=%s\n" "$spawned_count"
python3 /work/missionos/scripts/smoke_missionos_chat_turtlebot3_home_mission.py
CONTAINER
