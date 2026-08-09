FROM ros:humble-ros-base-jammy

ARG ROS_GZ_HUMBLE_COMMIT=9d7f8c721c233a9ac8b43950129d51e67905523e
ARG TURTLEBOT3_SIMULATIONS_COMMIT=970a3d026d3e86a89c2fba254934e62edaecbcbb

ENV DEBIAN_FRONTEND=noninteractive
ENV GZ_VERSION=garden
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        gnupg \
        lsb-release \
        wget \
    && wget https://packages.osrfoundation.org/gazebo.gpg \
        -O /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] http://packages.osrfoundation.org/gazebo/ubuntu-stable $(lsb_release -cs) main" \
        > /etc/apt/sources.list.d/gazebo-stable.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        gz-sim7-cli \
        libgflags-dev \
        libgz-sim7-dev \
        libgl1-mesa-dri \
        libglx-mesa0 \
        libxkbcommon-x11-0 \
        python3-colcon-common-extensions \
        python3-pip \
        ros-humble-nav2-bringup \
        ros-humble-navigation2 \
        ros-humble-ros-gz-bridge \
        ros-humble-ros-gz-image \
        ros-humble-ros-gz-interfaces \
        ros-humble-turtlebot3 \
        ros-humble-turtlebot3-navigation2 \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv --without-pip /opt/missionos-venv \
    && PYTHONPATH=/usr/lib/python3/dist-packages \
        /opt/missionos-venv/bin/python -m pip install --no-cache-dir --upgrade pip \
    && /opt/missionos-venv/bin/pip install --no-cache-dir \
        'click>=8.0.0' \
        'croniter>=1.3.0' \
        'ddgs>=0.1.0' \
        'fastapi>=0.110.0' \
        'google-adk[extensions]>=2.5.0,<3.0.0' \
        'httpx>=0.27.0' \
        'mcp>=1.0.0' \
        'numpy>=1.24.0' \
        'pydantic>=2,<3' \
        'pydantic-settings>=2,<3' \
        'python-dotenv>=1.0.0' \
        'pyyaml>=6.0.0' \
        'redis>=5.0.0,<7.0.0' \
        'rich>=13.0.0' \
        'uvicorn[standard]>=0.29.0' \
        'websockets>=12.0'

RUN mkdir -p /opt/turtlebot3_ws/src \
    && git clone --filter=blob:none --no-checkout --branch humble \
        https://github.com/gazebosim/ros_gz.git \
        /opt/turtlebot3_ws/src/ros_gz \
    && git -C /opt/turtlebot3_ws/src/ros_gz sparse-checkout init --cone \
    && git -C /opt/turtlebot3_ws/src/ros_gz sparse-checkout set ros_gz_sim \
    && git -C /opt/turtlebot3_ws/src/ros_gz checkout \
        "$ROS_GZ_HUMBLE_COMMIT" \
    && git clone --filter=blob:none --no-checkout \
        --branch feature-gazebo-sim-migration \
        https://github.com/ROBOTIS-GIT/turtlebot3_simulations.git \
        /opt/turtlebot3_ws/src/turtlebot3_simulations \
    && git -C /opt/turtlebot3_ws/src/turtlebot3_simulations checkout \
        "$TURTLEBOT3_SIMULATIONS_COMMIT" \
    && source /opt/ros/humble/setup.bash \
    && cd /opt/turtlebot3_ws \
    && colcon build --symlink-install

WORKDIR /work/missionos
ENV PATH=/opt/missionos-venv/bin:$PATH
ENV PYTHONPATH=/work/missionos
ENV TURTLEBOT3_MODEL=burger

CMD ["/bin/bash"]
