#!/usr/bin/env python3
"""Read-only ROS2 telemetry sidecar for TurtleBot3/Nav2 simulator evidence.

The sidecar subscribes to existing telemetry topics and writes JSONL samples.
It does not publish, send actions, mutate simulator state, or claim physical
execution. MissionOS consumes the JSONL through
``MISSIONOS_TURTLEBOT3_TELEMETRY_SIDECAR_JSONL``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
from typing import Any


SAMPLE_SCHEMA = "missionos_turtlebot3_telemetry_sample.v1"
SUMMARY_SCHEMA = "missionos_turtlebot3_telemetry_sidecar_summary.v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite_ranges(values: Any) -> list[float]:
    ranges: list[float] = []
    for value in values:
        if not isinstance(value, (int, float)):
            continue
        if not math.isfinite(float(value)) or value <= 0:
            continue
        ranges.append(float(value))
    return ranges


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")
        self.counts: dict[str, int] = {"odom": 0, "battery": 0, "scan": 0}

    def write(self, payload: dict[str, Any]) -> None:
        self._file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        self._file.write("\n")
        self._file.flush()
        kind = str(payload.get("sample_kind") or "")
        if kind in self.counts:
            self.counts[kind] += 1

    def close(self) -> None:
        self._file.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="JSONL output path")
    parser.add_argument("--duration-s", type=float, default=300.0)
    parser.add_argument("--max-samples", type=int, default=10000)
    parser.add_argument("--odom-topic", default="/odom")
    parser.add_argument("--battery-topic", default="/battery_state")
    parser.add_argument("--scan-topic", default="/scan")
    args = parser.parse_args()

    import rclpy
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import BatteryState, LaserScan

    rclpy.init()
    node = rclpy.create_node("missionos_turtlebot3_telemetry_sidecar")
    writer = JsonlWriter(Path(args.output))
    started_at = _utc_now()
    print(
        json.dumps(
            {
                "schema_version": SUMMARY_SCHEMA,
                "status": "running",
                "started_at": started_at,
                "output_path": str(Path(args.output)),
                "telemetry_only": True,
                "read_only": True,
                "command_payload_allowed": False,
                "dispatch_implementation_present": False,
                "physical_execution_invoked": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )

    def write_odom(message: Any) -> None:
        writer.write(
            {
                "schema_version": SAMPLE_SCHEMA,
                "sample_kind": "odom",
                "captured_at": _utc_now(),
                "topic": args.odom_topic,
                "frame_id": str(message.header.frame_id or ""),
                "child_frame_id": str(message.child_frame_id or ""),
                "position": {
                    "x_m": float(message.pose.pose.position.x),
                    "y_m": float(message.pose.pose.position.y),
                    "z_m": float(message.pose.pose.position.z),
                },
                "twist": {
                    "linear_x_mps": float(message.twist.twist.linear.x),
                    "linear_y_mps": float(message.twist.twist.linear.y),
                    "angular_z_radps": float(message.twist.twist.angular.z),
                },
            }
        )

    def write_battery(message: Any) -> None:
        writer.write(
            {
                "schema_version": SAMPLE_SCHEMA,
                "sample_kind": "battery",
                "captured_at": _utc_now(),
                "topic": args.battery_topic,
                "percentage": float(message.percentage),
                "voltage_v": float(message.voltage),
                "current_a": float(message.current),
                "power_supply_status": int(message.power_supply_status),
            }
        )

    def write_scan(message: Any) -> None:
        ranges = _finite_ranges(message.ranges)
        writer.write(
            {
                "schema_version": SAMPLE_SCHEMA,
                "sample_kind": "scan",
                "captured_at": _utc_now(),
                "topic": args.scan_topic,
                "frame_id": str(message.header.frame_id or ""),
                "range_min_m": float(message.range_min),
                "range_max_m": float(message.range_max),
                "min_range_m": min(ranges) if ranges else None,
                "finite_range_count": len(ranges),
            }
        )

    subscriptions = [
        node.create_subscription(Odometry, args.odom_topic, write_odom, 10),
        node.create_subscription(BatteryState, args.battery_topic, write_battery, 10),
        node.create_subscription(LaserScan, args.scan_topic, write_scan, 10),
    ]
    deadline = time.monotonic() + max(args.duration_s, 0.0)
    try:
        while time.monotonic() < deadline and sum(writer.counts.values()) < args.max_samples:
            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        for subscription in subscriptions:
            node.destroy_subscription(subscription)
        node.destroy_node()
        rclpy.shutdown()
        writer.close()

    print(
        json.dumps(
            {
                "schema_version": SUMMARY_SCHEMA,
                "status": "completed",
                "started_at": started_at,
                "completed_at": _utc_now(),
                "output_path": str(Path(args.output)),
                "sample_counts": writer.counts,
                "telemetry_only": True,
                "read_only": True,
                "command_payload_allowed": False,
                "dispatch_implementation_present": False,
                "physical_execution_invoked": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "schema_version": SUMMARY_SCHEMA,
                    "status": "interrupted",
                    "telemetry_only": True,
                    "read_only": True,
                    "physical_execution_invoked": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        raise
