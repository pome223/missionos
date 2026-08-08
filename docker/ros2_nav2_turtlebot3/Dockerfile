FROM ros:humble-ros-base-jammy

ENV DEBIAN_FRONTEND=noninteractive
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
        libgl1-mesa-dri \
        libglx-mesa0 \
        libxkbcommon-x11-0 \
        python3-colcon-common-extensions \
        python3-pip \
        ros-humble-nav2-bringup \
        ros-humble-navigation2 \
        ros-humble-ros-gz \
        ros-humble-ros-gz-bridge \
        ros-humble-ros-gz-sim \
        ros-humble-turtlebot3 \
        ros-humble-turtlebot3-navigation2 \
        xvfb \
    && pip3 install --no-cache-dir \
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
        'rich>=13.0.0' \
        'uvicorn[standard]>=0.29.0' \
        'websockets>=12.0' \
    && mkdir -p /opt/turtlebot3_ws/src \
    && git clone --depth 1 --branch feature-gazebo-sim-migration \
        https://github.com/ROBOTIS-GIT/turtlebot3_simulations.git \
        /opt/turtlebot3_ws/src/turtlebot3_simulations \
    && source /opt/ros/humble/setup.bash \
    && cd /opt/turtlebot3_ws \
    && colcon build --symlink-install \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work/missionos
ENV PYTHONPATH=/work/missionos
ENV TURTLEBOT3_MODEL=burger

CMD ["/bin/bash"]
