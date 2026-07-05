FROM ros:humble-ros-base-jammy

ENV DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libgl1-mesa-dri \
        libglx-mesa0 \
        libxkbcommon-x11-0 \
        gnupg \
        lsb-release \
        python3-pip \
        wget \
        xvfb \
    && wget https://packages.osrfoundation.org/gazebo.gpg \
        -O /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] http://packages.osrfoundation.org/gazebo/ubuntu-stable $(lsb_release -cs) main" \
        > /etc/apt/sources.list.d/gazebo-stable.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ros-humble-ros2controlcli \
        ros-humble-turtlebot4-simulator \
    && pip3 install --no-cache-dir 'pydantic>=2,<3' 'pydantic-settings>=2,<3' \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work/missionos
ENV PYTHONPATH=/work/missionos

CMD ["/bin/bash"]
