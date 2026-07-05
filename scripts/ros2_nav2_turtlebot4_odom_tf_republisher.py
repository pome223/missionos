#!/usr/bin/env python3
"""Opt-in odometry-to-TF republisher for TurtleBot4/Nav2 simulation.

This script republishes real `/odom` pose samples as `odom -> base_link` TF for
simulator stacks where the controller odometry topic is visible but late-joining
TF consumers do not receive a usable dynamic transform. It does not publish
velocity, send Nav2 goals, or claim simulator/physical completion.
"""

from __future__ import annotations

import json
import os
import signal
import time
from typing import Any


ROS2_NAV2_ODOM_TF_REPUBLISHER_ENV = "RUN_MISSIONOS_ROS2_NAV2_ODOM_TF_REPUBLISHER"
ROS2_NAV2_ODOM_TF_REPUBLISHER_DURATION_ENV = (
    "ROS2_NAV2_ODOM_TF_REPUBLISHER_DURATION_S"
)
ROS2_NAV2_ODOM_TF_REPUBLISHER_RATE_ENV = "ROS2_NAV2_ODOM_TF_REPUBLISHER_RATE_HZ"
ROS2_NAV2_ODOM_TF_REPUBLISHER_ODOM_TOPIC_ENV = (
    "ROS2_NAV2_ODOM_TF_REPUBLISHER_ODOM_TOPIC"
)
ROS2_NAV2_ODOM_TF_REPUBLISHER_TF_TOPIC_ENV = "ROS2_NAV2_ODOM_TF_REPUBLISHER_TF_TOPIC"

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return float(raw)


def _base_summary(**values: Any) -> dict[str, Any]:
    summary = {
        "shim": "ros2_nav2_turtlebot4_odom_tf_republisher",
        "physical_execution_invoked": False,
        "dispatch_request_sent": False,
        "cmd_vel_published_by_missionos": False,
        "raw_control_topic_published": False,
    }
    summary.update(values)
    return summary


def run_republisher() -> dict[str, Any]:
    try:
        import rclpy
        from geometry_msgs.msg import TransformStamped
        from nav_msgs.msg import Odometry
        from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
        from tf2_msgs.msg import TFMessage
    except Exception as exc:
        return _base_summary(
            ran=False,
            reason="ros2_odom_tf_python_dependencies_missing",
            error=f"{type(exc).__name__}: {str(exc)[:240]}",
        )

    odom_topic = os.environ.get(ROS2_NAV2_ODOM_TF_REPUBLISHER_ODOM_TOPIC_ENV, "/odom")
    tf_topic = os.environ.get(ROS2_NAV2_ODOM_TF_REPUBLISHER_TF_TOPIC_ENV, "/tf")
    duration_s = _float_env(ROS2_NAV2_ODOM_TF_REPUBLISHER_DURATION_ENV, 120.0)
    rate_hz = max(_float_env(ROS2_NAV2_ODOM_TF_REPUBLISHER_RATE_ENV, 20.0), 0.1)
    latest_odom: Any | None = None
    odom_observed = False
    odom_sample_count = 0
    tf_publish_count = 0
    stop_requested = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    def _odom_handler(message: Any) -> None:
        nonlocal latest_odom, odom_observed, odom_sample_count
        latest_odom = message
        odom_observed = True
        odom_sample_count += 1

    rclpy.init(args=None)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    node = rclpy.create_node("missionos_ros2_nav2_odom_tf_republisher")
    odom_qos = QoSProfile(depth=1)
    odom_qos.reliability = QoSReliabilityPolicy.RELIABLE
    odom_qos.durability = QoSDurabilityPolicy.TRANSIENT_LOCAL
    tf_qos = QoSProfile(depth=100)
    tf_qos.reliability = QoSReliabilityPolicy.RELIABLE
    tf_qos.durability = QoSDurabilityPolicy.VOLATILE
    subscription = node.create_subscription(Odometry, odom_topic, _odom_handler, odom_qos)
    publisher = node.create_publisher(TFMessage, tf_topic, tf_qos)
    deadline = time.monotonic() + max(duration_s, 0.0)
    period_s = 1.0 / rate_hz
    try:
        while rclpy.ok() and not stop_requested and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.05)
            if latest_odom is None:
                time.sleep(min(period_s, 0.05))
                continue
            transform = TransformStamped()
            transform.header = latest_odom.header
            transform.child_frame_id = latest_odom.child_frame_id or "base_link"
            transform.transform.translation.x = latest_odom.pose.pose.position.x
            transform.transform.translation.y = latest_odom.pose.pose.position.y
            transform.transform.translation.z = latest_odom.pose.pose.position.z
            transform.transform.rotation = latest_odom.pose.pose.orientation
            publisher.publish(TFMessage(transforms=[transform]))
            tf_publish_count += 1
            time.sleep(period_s)
    finally:
        node.destroy_subscription(subscription)
        node.destroy_publisher(publisher)
        node.destroy_node()
        rclpy.shutdown()

    return _base_summary(
        ran=True,
        odom_topic=odom_topic,
        tf_topic=tf_topic,
        tf_topic_republished=True,
        odom_observed=odom_observed,
        odom_sample_count=odom_sample_count,
        tf_publish_count=tf_publish_count,
    )


def main() -> int:
    if not _truthy_env(ROS2_NAV2_ODOM_TF_REPUBLISHER_ENV):
        print(
            json.dumps(
                _base_summary(
                    ran=False,
                    reason=(
                        f"{ROS2_NAV2_ODOM_TF_REPUBLISHER_ENV} is not set to 1; "
                        "no odom-to-TF republisher was started."
                    ),
                ),
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    result = run_republisher()
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True), flush=True)
    return 0 if result.get("odom_observed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
