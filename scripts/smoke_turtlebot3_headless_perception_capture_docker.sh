#!/usr/bin/env bash
set -eo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${MISSIONOS_TB3_DOCKER_IMAGE:-missionos-ros2-nav2-turtlebot3:local}"
output_dir="${repo_root}/output/turtlebot3_perception_smoke"
mkdir -p "${output_dir}"

# The image's normal ROS entrypoint does not accept this heredoc command path;
# invoke bash explicitly so the smoke is reproducible with the local image.
docker run --rm -i --entrypoint /bin/bash --shm-size=1g \
  -e "MISSIONOS_TB3_PERCEPTION_WORLD_WAIT_SECONDS=${MISSIONOS_TB3_PERCEPTION_WORLD_WAIT_SECONDS:-30}" \
  -v "${repo_root}:/work/missionos" \
  -w /work/missionos \
  "${image}" \
  -lc '
set -eo pipefail
source /opt/ros/humble/setup.bash
source /opt/turtlebot3_ws/install/setup.bash
export PYTHONPATH=/work/missionos:${PYTHONPATH:-}
export TURTLEBOT3_MODEL=waffle_pi
export RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT3_BRIDGE=1
export MISSIONOS_ROS2_NAV2_BRIDGE_PROFILE=turtlebot3
# Keep the TF cache in the Gazebo /clock domain so an exact scan-time lookup is
# not expired against host wall time. This remains a simulator-only setting.
export ROS2_NAV2_USE_SIM_TIME=1

xvfb-run -a ros2 launch turtlebot3_gazebo turtlebot3_world.launch.py \
  x_pose:=-2.0 y_pose:=-0.5 \
  >/tmp/missionos_perception_gazebo.log 2>&1 &
gz_pid=$!
trap "kill ${nav2_pid:-} ${gz_pid:-} 2>/dev/null || true" EXIT
sleep "${MISSIONOS_TB3_PERCEPTION_WORLD_WAIT_SECONDS:-30}"

obstacle_sdf='"'"'<sdf version="1.7"><model name="missionos_perception_smoke_obstacle"><static>true</static><link name="link"><collision name="collision"><geometry><box><size>0.50 0.50 0.70</size></box></geometry></collision><visual name="visual"><geometry><box><size>0.50 0.50 0.70</size></box></geometry><material><ambient>1 0 0 1</ambient><diffuse>1 0 0 1</diffuse></material></visual></link></model></sdf>'"'"'
ros2 run ros_gz_sim create \
  -world default \
  -string "$obstacle_sdf" \
  -name missionos_perception_smoke_obstacle \
  -x -1.15 -y -0.5 -z 0.35 \
  >/tmp/missionos_perception_spawn.log 2>&1
grep -q "OK creation of entity" /tmp/missionos_perception_spawn.log

# A map-frame observation needs Nav2 localization in the same graph; Gazebo
# alone publishes base_scan but cannot provide map<-base_scan.
xvfb-run -a ros2 launch turtlebot3_navigation2 navigation2.launch.py \
  use_sim_time:=True \
  >/tmp/missionos_perception_nav2.log 2>&1 &
nav2_pid=$!
sleep 55
ros2 topic pub --rate 5 /initialpose geometry_msgs/msg/PoseWithCovarianceStamped \
  "{header: {frame_id: map}, pose: {pose: {position: {x: -2.0, y: -0.5, z: 0.0}, orientation: {w: 1.0}}}}" \
  >/tmp/missionos_perception_initialpose.log 2>&1 &
initialpose_pid=$!
sleep 3
kill "$initialpose_pid" 2>/dev/null || true

# Persist a direct TF probe beside the receipt. It is diagnostic-only and lets
# the smoke distinguish a missing map frame from a bridge lookup failure.
timeout 5 ros2 run tf2_ros tf2_echo map base_scan \
  --ros-args -p use_sim_time:=true \
  >/work/missionos/output/turtlebot3_perception_smoke/tf_echo.log 2>&1 || true

python3 scripts/ros2_nav2_turtlebot4_bridge.py \
  > /work/missionos/output/turtlebot3_perception_smoke/capture.json <<'"'"'JSON'"'"'
{"schema_version":"missionos_ros2_nav2_bridge_request.v1","action":"capture_camera_frame","payload":{"output_path":"/work/missionos/output/turtlebot3_perception_smoke/frame.png"},"execution_mode":"sim","physical_execution_invoked":false,"raw_velocity_allowed":false,"raw_ros_topic_publication_allowed":false}
JSON
'

python3 - "${output_dir}/capture.json" <<'PY'
import json
from pathlib import Path
import sys

receipt = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sensor = receipt.get("camera_lidar_observation") or {}
summary = {
    "ack_status": receipt.get("ack_status"),
    "camera_frame_captured": receipt.get("camera_frame_captured"),
    "camera_frame_sha256": receipt.get("camera_frame_sha256"),
    "camera_observed_at": sensor.get("camera_observed_at"),
    "camera_received_at": sensor.get("camera_received_at"),
    "lidar_observed_at": sensor.get("lidar_observed_at"),
    "lidar_horizontal_sector": sensor.get("lidar_horizontal_sector"),
    "lidar_candidate_bearing_rad": sensor.get("lidar_candidate_bearing_rad"),
    "lidar_candidate_range_m": sensor.get("lidar_candidate_range_m"),
    "target_candidate_id": sensor.get("target_candidate_id"),
    "physical_execution_invoked": receipt.get("physical_execution_invoked"),
}
if not (
    summary["ack_status"] == "accepted"
    and summary["camera_frame_captured"] is True
    and sensor.get("camera_info_observed") is True
    and sensor.get("lidar_obstacle_observed") is True
    and summary["target_candidate_id"]
):
    raise SystemExit(2)
print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
PY
