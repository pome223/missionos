#!/usr/bin/env python3
"""Opt-in TurtleBot3 simulator motion probe.

This diagnostic publishes a small velocity command to `/cmd_vel` and checks
whether `/odom` changes. It is not a MissionOS dispatch path, does not send a
Nav2 goal, and must not claim completion.
"""

from __future__ import annotations

import json
import math
import os
import signal
import time
from typing import Any


ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_ENV = (
    "RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT3_MOTION_PROBE"
)
ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_TARGET_ENV = (
    "ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_TARGET_TOPIC"
)
ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_ODOM_ENV = (
    "ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_ODOM_TOPIC"
)
ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_LINEAR_X_ENV = (
    "ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_LINEAR_X_MPS"
)
ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_ANGULAR_Z_ENV = (
    "ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_ANGULAR_Z_RADPS"
)
ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_DURATION_ENV = (
    "ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_DURATION_S"
)
ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_RATE_ENV = (
    "ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_RATE_HZ"
)
ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_ODOM_TIMEOUT_ENV = (
    "ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_ODOM_TIMEOUT_S"
)
ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_SETTLE_ENV = (
    "ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_SETTLE_S"
)
ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_THRESHOLD_ENV = (
    "ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_MOTION_THRESHOLD_M"
)
ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_STOP_PUBLISHES_ENV = (
    "ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_STOP_PUBLISHES"
)

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return float(raw)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _base_summary(**values: Any) -> dict[str, Any]:
    summary = {
        "probe": "ros2_nav2_turtlebot3_motion_probe",
        "diagnostic_probe": True,
        "missionos_dispatch_path": False,
        "dispatch_request_sent": False,
        "completion_claimed": False,
        "completion_scope": "none",
        "physical_execution_invoked": False,
        "cmd_vel_published_by_missionos": False,
        "velocity_generated_by_missionos": False,
    }
    summary.update(values)
    return summary


def _odom_xy(message: Any) -> tuple[float, float]:
    pose = message.pose.pose
    return (float(pose.position.x), float(pose.position.y))


def _delta_m(
    before: tuple[float, float] | None,
    after: tuple[float, float] | None,
) -> float | None:
    if before is None or after is None:
        return None
    return math.hypot(after[0] - before[0], after[1] - before[1])


def run_probe() -> dict[str, Any]:
    try:
        import rclpy
        from geometry_msgs.msg import Twist
        from nav_msgs.msg import Odometry
        from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
    except Exception as exc:
        return _base_summary(
            ran=False,
            reason="ros2_turtlebot3_motion_probe_python_dependencies_missing",
            diagnostic_raw_velocity_published=False,
            target_control_topic_published=False,
            error=f"{type(exc).__name__}: {str(exc)[:240]}",
        )

    target_topic = os.environ.get(ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_TARGET_ENV, "/cmd_vel")
    odom_topic = os.environ.get(ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_ODOM_ENV, "/odom")
    linear_x_mps = _float_env(ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_LINEAR_X_ENV, 0.08)
    angular_z_radps = _float_env(ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_ANGULAR_Z_ENV, 0.0)
    duration_s = _float_env(ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_DURATION_ENV, 3.0)
    rate_hz = max(_float_env(ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_RATE_ENV, 20.0), 0.1)
    odom_timeout_s = _float_env(ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_ODOM_TIMEOUT_ENV, 10.0)
    settle_s = _float_env(ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_SETTLE_ENV, 1.0)
    motion_threshold_m = _float_env(
        ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_THRESHOLD_ENV,
        0.03,
    )
    stop_publishes = max(_int_env(ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_STOP_PUBLISHES_ENV, 5), 0)
    stop_requested = False
    latest_odom: tuple[float, float] | None = None
    odom_sample_count = 0

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    def _odom_handler(message: Any) -> None:
        nonlocal latest_odom, odom_sample_count
        latest_odom = _odom_xy(message)
        odom_sample_count += 1

    rclpy.init(args=None)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    node = rclpy.create_node("missionos_ros2_nav2_turtlebot3_motion_probe")
    odom_qos = QoSProfile(depth=10)
    odom_qos.reliability = QoSReliabilityPolicy.RELIABLE
    odom_qos.durability = QoSDurabilityPolicy.VOLATILE
    command_qos = QoSProfile(depth=10)
    command_qos.reliability = QoSReliabilityPolicy.RELIABLE
    command_qos.durability = QoSDurabilityPolicy.VOLATILE
    subscription = node.create_subscription(Odometry, odom_topic, _odom_handler, odom_qos)
    publisher = node.create_publisher(Twist, target_topic, command_qos)
    command = Twist()
    command.linear.x = linear_x_mps
    command.angular.z = angular_z_radps
    stop_command = Twist()
    publish_count = 0
    stop_publish_count = 0
    odom_before: tuple[float, float] | None = None
    odom_after: tuple[float, float] | None = None
    try:
        odom_deadline = time.monotonic() + max(odom_timeout_s, 0.0)
        while rclpy.ok() and latest_odom is None and time.monotonic() < odom_deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
        odom_before = latest_odom
        if odom_before is None:
            return _base_summary(
                ran=True,
                target_topic=target_topic,
                odom_topic=odom_topic,
                diagnostic_raw_velocity_published=False,
                target_control_topic_published=False,
                linear_x_mps=linear_x_mps,
                angular_z_radps=angular_z_radps,
                duration_s=duration_s,
                rate_hz=rate_hz,
                settle_s=settle_s,
                motion_threshold_m=motion_threshold_m,
                odom_before_observed=False,
                odom_after_observed=False,
                odom_delta_m=None,
                robot_motion_observed=False,
                odom_sample_count=odom_sample_count,
                blocking_reasons=["diagnostic_odom_before_not_observed"],
            )

        period_s = 1.0 / rate_hz
        deadline = time.monotonic() + max(duration_s, 0.0)
        while rclpy.ok() and not stop_requested and time.monotonic() < deadline:
            publisher.publish(command)
            publish_count += 1
            rclpy.spin_once(node, timeout_sec=0.0)
            time.sleep(period_s)
        for _ in range(stop_publishes):
            publisher.publish(stop_command)
            stop_publish_count += 1
            rclpy.spin_once(node, timeout_sec=0.0)
            time.sleep(period_s)

        settle_deadline = time.monotonic() + max(settle_s, 0.0)
        while rclpy.ok() and time.monotonic() < settle_deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
            odom_after = latest_odom
        odom_after = latest_odom
    finally:
        node.destroy_subscription(subscription)
        node.destroy_publisher(publisher)
        node.destroy_node()
        rclpy.shutdown()

    odom_delta_m = _delta_m(odom_before, odom_after)
    robot_motion_observed = (
        odom_delta_m is not None and odom_delta_m >= motion_threshold_m
    )
    blocking_reasons = [] if robot_motion_observed else ["diagnostic_odom_motion_not_observed"]
    return _base_summary(
        ran=True,
        target_topic=target_topic,
        odom_topic=odom_topic,
        diagnostic_raw_velocity_published=publish_count > 0,
        diagnostic_velocity_generated=True,
        target_control_topic_published=publish_count > 0 or stop_publish_count > 0,
        linear_x_mps=linear_x_mps,
        angular_z_radps=angular_z_radps,
        duration_s=duration_s,
        rate_hz=rate_hz,
        settle_s=settle_s,
        publish_count=publish_count,
        stop_publish_count=stop_publish_count,
        odom_before_observed=odom_before is not None,
        odom_after_observed=odom_after is not None,
        odom_before_xy=odom_before,
        odom_after_xy=odom_after,
        odom_delta_m=odom_delta_m,
        motion_threshold_m=motion_threshold_m,
        robot_motion_observed=robot_motion_observed,
        odom_sample_count=odom_sample_count,
        blocking_reasons=blocking_reasons,
    )


def main() -> int:
    if not _truthy_env(ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_ENV):
        print(
            json.dumps(
                _base_summary(
                    ran=False,
                    reason=(
                        f"{ROS2_NAV2_TURTLEBOT3_MOTION_PROBE_ENV} is not set "
                        "to 1; no diagnostic velocity was published."
                    ),
                    diagnostic_raw_velocity_published=False,
                    target_control_topic_published=False,
                    robot_motion_observed=False,
                    blocking_reasons=["turtlebot3_motion_probe_opt_in_not_enabled"],
                ),
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    result = run_probe()
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True), flush=True)
    return 0 if result.get("robot_motion_observed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
