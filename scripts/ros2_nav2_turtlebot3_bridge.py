#!/usr/bin/env python3
"""Opt-in ROS2 action bridge for TurtleBot3/Nav2 simulation.

This entrypoint uses the shared bounded Nav2 bridge implementation with a
TurtleBot3 evidence profile. It sends only Nav2 ``NavigateToPose`` goals; it
must not publish raw ``cmd_vel`` or claim physical execution.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MISSIONOS_ROS2_NAV2_BRIDGE_PROFILE", "turtlebot3")

from scripts.ros2_nav2_turtlebot4_bridge import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
