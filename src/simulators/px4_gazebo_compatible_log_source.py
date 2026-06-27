"""PX4/Gazebo-compatible telemetry log source for opt-in smoke tests.

This process is not PX4, Gazebo, or an autopilot. It behaves like a simulator
log source: it starts as a separate process, emits telemetry-shaped JSON to
stdout, and exposes no command, network, ROS, MAVLink, or actuator surface.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any


SERVICE_NAME = "boiled-claw-px4-gazebo-compatible-log-source"
TELEMETRY_PREFIX = "PX4_GAZEBO_TELEMETRY "
STATUS_PREFIX = "PX4_GAZEBO_SOURCE_STATUS "


def _status_payload() -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "status": "ok",
        "mode": "telemetry_log_source",
        "px4_started": False,
        "gazebo_started": False,
        "compatible_log_source_started": True,
        "command_payload_allowed": False,
        "ros_dispatch_allowed": False,
        "mavlink_dispatch_allowed": False,
        "actuator_execution_allowed": False,
        "live_execution_allowed": False,
        "physical_execution_invoked": False,
    }


def _telemetry_sample(*, tick: int) -> dict[str, Any]:
    return {
        "sample_id": f"px4_gazebo_compatible_log_tick_{tick}",
        "source": {
            "source_kind": "px4_gazebo_compatible_log_source",
            "source_id": "px4-gazebo-compatible-log-source",
            "vehicle_id": "iris-log-001",
        },
        "captured_at": "2026-04-30T16:00:00+00:00",
        "telemetry": {
            "altitude_m": 4.0,
            "battery_remaining_pct": 91,
            "gps_fix": True,
            "heading_deg": 180.0,
            "velocity_mps": 0.0,
            "vertical_speed_mps": 0.0,
        },
        "metadata": {
            "service": SERVICE_NAME,
            "telemetry_only": True,
            "read_only": True,
            "log_source": True,
        },
    }


def _emit(prefix: str, payload: dict[str, Any]) -> None:
    print(prefix + json.dumps(payload, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--max-ticks", type=int, default=0)
    args = parser.parse_args()

    _emit(STATUS_PREFIX, _status_payload())
    tick = 0
    while True:
        _emit(TELEMETRY_PREFIX, _telemetry_sample(tick=tick))
        tick += 1
        if args.max_ticks and tick >= args.max_ticks:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
