#!/usr/bin/env python3
"""Shared opt-in MissionOS -> ROS2/Nav2 bounded dispatch smoke."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from typing import Sequence

from src.runtime.hardware_adapter_contract import HardwareExecutionMode
from src.runtime.ros2_nav2_dispatch_bridge import (
    ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV,
    ROS2_NAV2_BRIDGE_COMMAND_ENV,
    Ros2Nav2BridgeCommandClient,
)
from src.runtime.ros2_nav2_hardware_adapter import (
    Nav2GoalPose,
    Ros2Nav2HardwareAdapter,
    Ros2Nav2HardwareAdapterConfig,
)

_TRUE_VALUES = {"1", "true", "yes", "on"}

# The only robot-specific input in this shared scenario is the short default
# goal. Contract identifiers are derived from the profile name so that adding a
# profile does not copy the smoke implementation again.
DEFAULT_GOAL_X_M = {"turtlebot3": 0.75, "turtlebot4": 0.25}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _disabled_summary(robot_profile: str) -> dict[str, object]:
    return {
        "smoke": f"ros2_nav2_{robot_profile}_bounded_dispatch",
        "ran": False,
        "reason": (
            f"{ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV} is not set to 1; "
            "no Nav2 goal was sent."
        ),
        "bridge_command_present": bool(
            os.environ.get(ROS2_NAV2_BRIDGE_COMMAND_ENV, "").strip()
        ),
        "dispatch_request_sent": False,
        "completion_claimed": False,
        "completion_scope": "none",
        "physical_execution_invoked": False,
    }


def run_bounded_dispatch_smoke(robot_profile: str) -> int:
    """Run one profile through the shared bounded Nav2 dispatch contract."""

    default_goal_x_m = DEFAULT_GOAL_X_M[robot_profile]
    if not _truthy_env(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV):
        print(json.dumps(_disabled_summary(robot_profile), indent=2, sort_keys=True))
        return 0

    client = Ros2Nav2BridgeCommandClient()
    adapter = Ros2Nav2HardwareAdapter(
        config=Ros2Nav2HardwareAdapterConfig(
            missionos_action_ref=(
                f"missionos_plan_{robot_profile}_bounded_nav2_goal"
            ),
            goal_pose=Nav2GoalPose(
                frame_id=os.environ.get("ROS2_NAV2_GOAL_FRAME_ID", "map"),
                x_m=float(
                    os.environ.get("ROS2_NAV2_GOAL_X_M", default_goal_x_m)
                ),
                y_m=float(os.environ.get("ROS2_NAV2_GOAL_Y_M", "0.0")),
                yaw_rad=float(os.environ.get("ROS2_NAV2_GOAL_YAW_RAD", "0.0")),
                tolerance_m=float(
                    os.environ.get("ROS2_NAV2_GOAL_TOLERANCE_M", "0.25")
                ),
                max_speed_mps=0.25,
                max_distance_m=3.0,
                label=f"{robot_profile}_short_nav2_goal",
            ),
            execution_mode=HardwareExecutionMode.SIM,
            operator_approval_ref=(
                f"smoke_operator_approval_nav2_{robot_profile}_001"
            ),
            approval_actor="smoke-operator",
            approval_timestamp=datetime.now(timezone.utc),
        ),
        client=client,
    )
    evidence = adapter.dispatch_approved_action()
    print(
        json.dumps(
            {
                "smoke": f"ros2_nav2_{robot_profile}_bounded_dispatch",
                "ran": True,
                "bridge_command_present": bool(
                    os.environ.get(ROS2_NAV2_BRIDGE_COMMAND_ENV, "").strip()
                ),
                "bridge_responses": client.collect_responses(),
                "evidence": evidence.model_dump(mode="json"),
            },
            indent=2,
            sort_keys=True,
        )
    )

    if evidence.physical_execution_invoked:
        robot_label = robot_profile.replace("turtlebot", "TurtleBot")
        raise SystemExit(f"Nav2 {robot_label} smoke claimed physical execution")
    if evidence.dispatch_request_sent and evidence.completion_scope not in {
        "sim_action",
        "none",
    }:
        robot_label = robot_profile.replace("turtlebot", "TurtleBot")
        raise SystemExit(f"Nav2 {robot_label} smoke used an invalid completion scope")
    return 0 if evidence.dispatch_request_sent and evidence.completion_claimed else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--robot-profile",
        choices=sorted(DEFAULT_GOAL_X_M),
        required=True,
    )
    return run_bounded_dispatch_smoke(parser.parse_args(argv).robot_profile)


if __name__ == "__main__":
    raise SystemExit(main())
