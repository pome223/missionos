#!/usr/bin/env python3
"""Opt-in Nav2 velocity-to-controller relay for TurtleBot4 simulation.

This script copies Nav2-produced velocity commands to the simulator controller
input when the installed TurtleBot4 Create3 motion-control layer does not relay
them. It does not generate velocity, send Nav2 goals, or claim completion.
"""

from __future__ import annotations

import json
import os
import signal
import time
from typing import Any


ROS2_NAV2_NAV_VELOCITY_RELAY_ENV = "RUN_MISSIONOS_ROS2_NAV2_NAV_VELOCITY_RELAY"
ROS2_NAV2_NAV_VELOCITY_RELAY_SOURCE_ENV = "ROS2_NAV2_NAV_VELOCITY_RELAY_SOURCE_TOPIC"
ROS2_NAV2_NAV_VELOCITY_RELAY_TARGET_ENV = "ROS2_NAV2_NAV_VELOCITY_RELAY_TARGET_TOPIC"
ROS2_NAV2_NAV_VELOCITY_RELAY_DURATION_ENV = "ROS2_NAV2_NAV_VELOCITY_RELAY_DURATION_S"
ROS2_NAV2_NAV_VELOCITY_RELAY_THRESHOLD_ENV = "ROS2_NAV2_NAV_VELOCITY_RELAY_THRESHOLD"
ROS2_NAV2_NAV_VELOCITY_RELAY_PROFILE_ENV = (
    "MISSIONOS_ROS2_NAV2_NAV_VELOCITY_RELAY_PROFILE"
)

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return float(raw)


def _relay_profile() -> str:
    raw = os.environ.get(ROS2_NAV2_NAV_VELOCITY_RELAY_PROFILE_ENV, "").strip().lower()
    if raw in {"turtlebot3", "turtlebot4"}:
        return raw
    return "turtlebot4"


def _relay_name() -> str:
    return f"ros2_nav2_{_relay_profile()}_nav_velocity_relay"


def _base_summary(**values: Any) -> dict[str, Any]:
    summary = {
        "shim": _relay_name(),
        "physical_execution_invoked": False,
        "dispatch_request_sent": False,
        "cmd_vel_published_by_missionos": False,
        "velocity_generated_by_missionos": False,
        "nav2_velocity_relay_shim": True,
    }
    summary.update(values)
    return summary


def run_relay() -> dict[str, Any]:
    try:
        import rclpy
        from geometry_msgs.msg import Twist
        from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
    except Exception as exc:
        return _base_summary(
            ran=False,
            reason="ros2_velocity_relay_python_dependencies_missing",
            error=f"{type(exc).__name__}: {str(exc)[:240]}",
        )

    source_topic = os.environ.get(ROS2_NAV2_NAV_VELOCITY_RELAY_SOURCE_ENV, "/cmd_vel")
    target_topic = os.environ.get(
        ROS2_NAV2_NAV_VELOCITY_RELAY_TARGET_ENV,
        "/diffdrive_controller/cmd_vel_unstamped",
    )
    duration_s = _float_env(ROS2_NAV2_NAV_VELOCITY_RELAY_DURATION_ENV, 180.0)
    threshold = _float_env(ROS2_NAV2_NAV_VELOCITY_RELAY_THRESHOLD_ENV, 0.001)
    source_message_count = 0
    relayed_message_count = 0
    nonzero_relay_count = 0
    max_abs_linear_x = 0.0
    max_abs_angular_z = 0.0
    stop_requested = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    rclpy.init(args=None)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    node = rclpy.create_node(f"missionos_{_relay_name()}")
    source_qos = QoSProfile(depth=20)
    source_qos.reliability = QoSReliabilityPolicy.RELIABLE
    source_qos.durability = QoSDurabilityPolicy.VOLATILE
    target_qos = QoSProfile(depth=20)
    target_qos.reliability = QoSReliabilityPolicy.RELIABLE
    target_qos.durability = QoSDurabilityPolicy.VOLATILE
    publisher = node.create_publisher(Twist, target_topic, target_qos)

    def _handler(message: Any) -> None:
        nonlocal source_message_count
        nonlocal relayed_message_count
        nonlocal nonzero_relay_count
        nonlocal max_abs_linear_x
        nonlocal max_abs_angular_z
        source_message_count += 1
        linear_x = abs(float(message.linear.x))
        angular_z = abs(float(message.angular.z))
        max_abs_linear_x = max(max_abs_linear_x, linear_x)
        max_abs_angular_z = max(max_abs_angular_z, angular_z)
        if linear_x >= threshold or angular_z >= threshold:
            nonzero_relay_count += 1
        publisher.publish(message)
        relayed_message_count += 1

    subscription = node.create_subscription(Twist, source_topic, _handler, source_qos)
    print(
        json.dumps(
            _base_summary(
                ran=True,
                status="running",
                source_topic=source_topic,
                target_topic=target_topic,
                target_control_topic_published=True,
            ),
            ensure_ascii=True,
            sort_keys=True,
        ),
        flush=True,
    )
    deadline = time.monotonic() + max(duration_s, 0.0)
    try:
        while rclpy.ok() and not stop_requested and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        node.destroy_subscription(subscription)
        node.destroy_publisher(publisher)
        node.destroy_node()
        rclpy.shutdown()

    return _base_summary(
        ran=True,
        source_topic=source_topic,
        target_topic=target_topic,
        target_control_topic_published=True,
        source_message_count=source_message_count,
        relayed_message_count=relayed_message_count,
        nonzero_relay_count=nonzero_relay_count,
        nonzero_velocity_relayed=nonzero_relay_count > 0,
        max_abs_linear_x=max_abs_linear_x,
        max_abs_angular_z=max_abs_angular_z,
    )


def main() -> int:
    if not _truthy_env(ROS2_NAV2_NAV_VELOCITY_RELAY_ENV):
        print(
            json.dumps(
                _base_summary(
                    ran=False,
                    reason=(
                        f"{ROS2_NAV2_NAV_VELOCITY_RELAY_ENV} is not set to 1; "
                        "no Nav2 velocity relay was started."
                    ),
                    target_control_topic_published=False,
                ),
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    result = run_relay()
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True), flush=True)
    return 0 if result.get("nonzero_velocity_relayed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
