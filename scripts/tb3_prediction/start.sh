#!/usr/bin/env bash
# Container-only startup. The calling Docker runner owns and removes the container.
set -eo pipefail
test "${RUN_MISSIONOS_TB3_PREDICTION_SIM:-0}" = 1 || { echo 'simulator opt-in required' >&2; exit 2; }
source /opt/ros/humble/setup.bash
source /opt/turtlebot3_ws/install/setup.bash
export TURTLEBOT3_MODEL=waffle_pi
export GZ_SIM_RESOURCE_PATH=/opt/turtlebot3_ws/install/turtlebot3_gazebo/share/turtlebot3_gazebo/models
export PYTHONPATH=/work/missionos:${PYTHONPATH:-}
out="$1"
mkdir -p "$out"
/usr/bin/python3 scripts/tb3_prediction/prepare.py --output "$out" --obstacle-shape "${2:-box}" --controller-profile "${3:-legacy}"
xvfb-run -a gz sim -r -s "$out/arena.sdf" >"$out/gazebo.log" 2>&1 &
ros2 launch turtlebot3_gazebo robot_state_publisher.launch.py use_sim_time:=true >"$out/robot-state.log" 2>&1 &
ros2 run ros_gz_bridge parameter_bridge --ros-args -p config_file:=/opt/turtlebot3_ws/install/turtlebot3_gazebo/share/turtlebot3_gazebo/params/turtlebot3_waffle_bridge.yaml >"$out/bridge.log" 2>&1 &
ros2 run ros_gz_bridge parameter_bridge \
  '/world/default/pose/info@tf2_msgs/msg/TFMessage[gz.msgs.Pose_V' \
  '/world/default/set_pose@ros_gz_interfaces/srv/SetEntityPose' \
  >"$out/observation-bridge.log" 2>&1 &
cat >"$out/contact-bridge.yaml" <<'YAML'
- ros_topic_name: /benchmark/contacts
  gz_topic_name: /world/default/model/crossing_cart/link/body/sensor/contact/contact
  ros_type_name: ros_gz_interfaces/msg/Contacts
  gz_type_name: gz.msgs.Contacts
  direction: GZ_TO_ROS
YAML
ros2 run ros_gz_bridge parameter_bridge --ros-args -p config_file:="$out/contact-bridge.yaml" >"$out/contact-bridge.log" 2>&1 &
ros2 run ros_gz_image image_bridge /camera/image_raw >"$out/camera.log" 2>&1 &
export RUN_MISSIONOS_ROS2_NAV2_NAV_VELOCITY_RELAY=1
export ROS2_NAV2_NAV_VELOCITY_RELAY_SOURCE_TOPIC=/cmd_vel_nav
export ROS2_NAV2_NAV_VELOCITY_RELAY_TARGET_TOPIC=/cmd_vel
/usr/bin/python3 scripts/ros2_nav2_turtlebot3_nav_velocity_relay.py >"$out/relay.log" 2>&1 &
ros2 launch nav2_bringup bringup_launch.py use_sim_time:=true map:="$out/map.yaml" params_file:="$out/nav2.yaml" >"$out/nav2.log" 2>&1 &
wait
