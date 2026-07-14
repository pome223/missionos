#!/usr/bin/env python3
"""Compatibility entrypoint for the shared TurtleBot Nav2 dispatch smoke."""

from ros2_nav2_bounded_dispatch_smoke import run_bounded_dispatch_smoke


if __name__ == "__main__":
    raise SystemExit(run_bounded_dispatch_smoke("turtlebot4"))
