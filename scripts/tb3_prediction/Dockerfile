# Build the base with docker/ros2_nav2_turtlebot3/Dockerfile first.
ARG TB3_BASE_IMAGE=missionos-public-tb3-arm64:issue65
FROM ${TB3_BASE_IMAGE}
SHELL ["/bin/bash", "-c"]
# Humble binary bridges use Fortress messages; this benchmark uses Garden.
# Build bridges from the same pinned ROS-Gazebo revision as the base simulator.
RUN test "$(git -C /opt/turtlebot3_ws/src/ros_gz rev-parse HEAD)" = 9d7f8c721c233a9ac8b43950129d51e67905523e \
    && git -C /opt/turtlebot3_ws/src/ros_gz sparse-checkout set ros_gz_sim ros_gz_bridge ros_gz_image \
    && source /opt/ros/humble/setup.bash \
    && cd /opt/turtlebot3_ws \
    && PATH=/usr/bin:/bin:/opt/ros/humble/bin CMAKE_BUILD_PARALLEL_LEVEL=1 MAKEFLAGS=-j1 \
       /usr/bin/colcon build --packages-select ros_gz_bridge ros_gz_image --executor sequential \
       --cmake-args -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release \
       -DPython3_EXECUTABLE=/usr/bin/python3 -DPYTHON_EXECUTABLE=/usr/bin/python3
