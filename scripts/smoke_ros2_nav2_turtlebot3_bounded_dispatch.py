#!/usr/bin/env python3
"""Opt-in MissionOS -> ROS2/Nav2 TurtleBot3 bounded dispatch smoke."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os

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


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def main() -> int:
    if not _truthy_env(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV):
        print(
            json.dumps(
                {
                    "smoke": "ros2_nav2_turtlebot3_bounded_dispatch",
                    "ran": False,
                    "reason": (
                        f"{ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV} is not set "
                        "to 1; no Nav2 goal was sent."
                    ),
                    "bridge_command_present": bool(
                        os.environ.get(ROS2_NAV2_BRIDGE_COMMAND_ENV, "").strip()
                    ),
                    "dispatch_request_sent": False,
                    "completion_claimed": False,
                    "completion_scope": "none",
                    "physical_execution_invoked": False,
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    client = Ros2Nav2BridgeCommandClient()
    adapter = Ros2Nav2HardwareAdapter(
        config=Ros2Nav2HardwareAdapterConfig(
            missionos_action_ref="missionos_plan_turtlebot3_bounded_nav2_goal",
            goal_pose=Nav2GoalPose(
                frame_id=os.environ.get("ROS2_NAV2_GOAL_FRAME_ID", "map"),
                x_m=float(os.environ.get("ROS2_NAV2_GOAL_X_M", "0.75")),
                y_m=float(os.environ.get("ROS2_NAV2_GOAL_Y_M", "0.0")),
                yaw_rad=float(os.environ.get("ROS2_NAV2_GOAL_YAW_RAD", "0.0")),
                tolerance_m=float(os.environ.get("ROS2_NAV2_GOAL_TOLERANCE_M", "0.25")),
                max_speed_mps=0.25,
                max_distance_m=3.0,
                label="turtlebot3_short_nav2_goal",
            ),
            execution_mode=HardwareExecutionMode.SIM,
            operator_approval_ref="smoke_operator_approval_nav2_turtlebot3_001",
            approval_actor="smoke-operator",
            approval_timestamp=datetime.now(timezone.utc),
        ),
        client=client,
    )
    evidence = adapter.dispatch_approved_action()
    summary = {
        "smoke": "ros2_nav2_turtlebot3_bounded_dispatch",
        "ran": True,
        "bridge_command_present": bool(
            os.environ.get(ROS2_NAV2_BRIDGE_COMMAND_ENV, "").strip()
        ),
        "bridge_responses": client.collect_responses(),
        "evidence": evidence.model_dump(mode="json"),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))

    if evidence.physical_execution_invoked:
        raise SystemExit("Nav2 TurtleBot3 smoke claimed physical execution")
    if evidence.dispatch_request_sent and evidence.completion_scope not in {
        "sim_action",
        "none",
    }:
        raise SystemExit("Nav2 TurtleBot3 smoke used an invalid completion scope")
    return 0 if evidence.dispatch_request_sent and evidence.completion_claimed else 2


if __name__ == "__main__":
    raise SystemExit(main())
