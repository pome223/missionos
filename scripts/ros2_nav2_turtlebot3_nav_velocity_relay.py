#!/usr/bin/env python3
"""Opt-in Nav2 velocity-to-controller relay for TurtleBot3 simulation.

This entrypoint relays only Nav2-produced velocity commands to the TurtleBot3
simulator controller. It does not generate velocity, send Nav2 goals, or claim
completion.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MISSIONOS_ROS2_NAV2_NAV_VELOCITY_RELAY_PROFILE", "turtlebot3")

from scripts.ros2_nav2_turtlebot4_nav_velocity_relay import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
