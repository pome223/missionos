#!/usr/bin/env python3
"""Opt-in TF readiness probe for TurtleBot4/Nav2 simulation.

The probe subscribes to TF only. It does not send goals, publish velocity, or
claim simulator/physical completion.
"""

from __future__ import annotations

from collections import deque
import json
import os
import time
from typing import Any


ROS2_NAV2_TF_READINESS_PROBE_ENV = "RUN_MISSIONOS_ROS2_NAV2_TF_READINESS_PROBE"
ROS2_NAV2_TF_PROBE_TIMEOUT_ENV = "ROS2_NAV2_TF_PROBE_TIMEOUT_S"
ROS2_NAV2_TF_PROBE_TARGET_FRAME_ENV = "ROS2_NAV2_TF_PROBE_TARGET_FRAME"
ROS2_NAV2_TF_PROBE_SOURCE_FRAME_ENV = "ROS2_NAV2_TF_PROBE_SOURCE_FRAME"

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return float(raw)


def _edge_key(parent: str, child: str) -> str:
    return f"{parent}->{child}"


def _has_path(edges: set[tuple[str, str]], source: str, target: str) -> bool:
    if source == target:
        return True
    graph: dict[str, set[str]] = {}
    for parent, child in edges:
        graph.setdefault(parent, set()).add(child)
        graph.setdefault(child, set()).add(parent)
    queue = deque([source])
    visited = {source}
    while queue:
        frame = queue.popleft()
        for next_frame in graph.get(frame, set()):
            if next_frame == target:
                return True
            if next_frame not in visited:
                visited.add(next_frame)
                queue.append(next_frame)
    return False


def _base_summary(**values: Any) -> dict[str, Any]:
    summary = {
        "probe": "ros2_nav2_turtlebot4_tf_readiness",
        "physical_execution_invoked": False,
        "dispatch_request_sent": False,
        "cmd_vel_published_by_missionos": False,
        "raw_ros_topic_published": False,
    }
    summary.update(values)
    return summary


def _qos_profile(*, durability: Any, reliability: Any) -> Any:
    from rclpy.qos import QoSProfile

    qos = QoSProfile(depth=100)
    qos.durability = durability
    qos.reliability = reliability
    return qos


def run_probe() -> dict[str, Any]:
    try:
        import rclpy
        from rclpy.qos import QoSDurabilityPolicy, QoSReliabilityPolicy
        from tf2_msgs.msg import TFMessage
    except Exception as exc:
        return _base_summary(
            ran=False,
            reason="ros2_tf_python_dependencies_missing",
            error=f"{type(exc).__name__}: {str(exc)[:240]}",
        )

    target_frame = os.environ.get(ROS2_NAV2_TF_PROBE_TARGET_FRAME_ENV, "map")
    source_frame = os.environ.get(ROS2_NAV2_TF_PROBE_SOURCE_FRAME_ENV, "base_link")
    timeout_s = _float_env(ROS2_NAV2_TF_PROBE_TIMEOUT_ENV, 20.0)
    edges: set[tuple[str, str]] = set()
    edge_profiles: dict[str, set[str]] = {}
    sample_counts: dict[str, int] = {}

    def _handler(profile_name: str) -> Any:
        def _inner(message: Any) -> None:
            sample_counts[profile_name] = sample_counts.get(profile_name, 0) + 1
            for transform in message.transforms:
                parent = str(transform.header.frame_id).strip()
                child = str(transform.child_frame_id).strip()
                if not parent or not child:
                    continue
                edges.add((parent, child))
                edge_profiles.setdefault(_edge_key(parent, child), set()).add(
                    profile_name
                )

        return _inner

    rclpy.init(args=None)
    node = rclpy.create_node("missionos_ros2_nav2_tf_readiness_probe")
    subscriptions = []
    try:
        qos_options = (
            (
                "/tf",
                "tf_volatile_reliable",
                _qos_profile(
                    durability=QoSDurabilityPolicy.VOLATILE,
                    reliability=QoSReliabilityPolicy.RELIABLE,
                ),
            ),
            (
                "/tf",
                "tf_volatile_best_effort",
                _qos_profile(
                    durability=QoSDurabilityPolicy.VOLATILE,
                    reliability=QoSReliabilityPolicy.BEST_EFFORT,
                ),
            ),
            (
                "/tf",
                "tf_transient_local_reliable",
                _qos_profile(
                    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
                    reliability=QoSReliabilityPolicy.RELIABLE,
                ),
            ),
            (
                "/tf",
                "tf_transient_local_best_effort",
                _qos_profile(
                    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
                    reliability=QoSReliabilityPolicy.BEST_EFFORT,
                ),
            ),
            (
                "/tf_static",
                "tf_static_transient_local_reliable",
                _qos_profile(
                    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
                    reliability=QoSReliabilityPolicy.RELIABLE,
                ),
            ),
        )
        for topic, profile_name, qos in qos_options:
            subscriptions.append(
                node.create_subscription(
                    TFMessage,
                    topic,
                    _handler(profile_name),
                    qos,
                )
            )

        deadline = time.monotonic() + max(timeout_s, 0.0)
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
            if _has_path(edges, source_frame, target_frame):
                break
    finally:
        for subscription in subscriptions:
            node.destroy_subscription(subscription)
        node.destroy_node()
        rclpy.shutdown()

    edge_keys = sorted(_edge_key(parent, child) for parent, child in edges)
    readiness_edges = {
        "map_to_odom": _edge_key("map", "odom") in edge_keys,
        "odom_to_base_link": _edge_key("odom", "base_link") in edge_keys,
        "map_to_base_link": _edge_key("map", "base_link") in edge_keys,
    }
    connected = _has_path(edges, source_frame, target_frame)
    return _base_summary(
        ran=True,
        tf_ready=connected,
        tf_target_frame=target_frame,
        tf_source_frame=source_frame,
        tf_wait_timeout_s=timeout_s,
        edge_count=len(edge_keys),
        edges=edge_keys,
        readiness_edges=readiness_edges,
        edge_profiles={
            edge: sorted(profiles) for edge, profiles in sorted(edge_profiles.items())
        },
        sample_counts=sample_counts,
    )


def main() -> int:
    if not _truthy_env(ROS2_NAV2_TF_READINESS_PROBE_ENV):
        print(
            json.dumps(
                _base_summary(
                    ran=False,
                    reason=(
                        f"{ROS2_NAV2_TF_READINESS_PROBE_ENV} is not set to 1; "
                        "no TF probe was started."
                    ),
                ),
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    result = run_probe()
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result.get("tf_ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
