"""Gazebo-compatible telemetry log source for opt-in smoke tests.

This process is not Gazebo, ROS, PX4, or an autopilot. It behaves like a
Gazebo-side telemetry/log source: it starts as a separate Docker process,
emits Gazebo-shaped telemetry JSON to stdout, and exposes no command,
network, ROS, MAVLink, or actuator surface.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any


SERVICE_NAME = "boiled-claw-gazebo-telemetry-log-source"
TELEMETRY_PREFIX = "PX4_GAZEBO_TELEMETRY "
STATUS_PREFIX = "GAZEBO_TELEMETRY_SOURCE_STATUS "
STARTUP_MARKER = "GAZEBO_TELEMETRY_SOURCE_READY"


def _status_payload() -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "status": "ok",
        "mode": "gazebo_compatible_telemetry_log_source",
        "gazebo_compatible_process_started": True,
        "gazebo_started": False,
        "px4_started": False,
        "telemetry_only": True,
        "read_only": True,
        "live_execution_allowed": False,
        "physical_execution_invoked": False,
    }


def _telemetry_sample(*, tick: int) -> dict[str, Any]:
    return {
        "sample_id": f"gazebo_compatible_log_tick_{tick}",
        "source": {
            "source_kind": "gazebo_compatible_log_source",
            "source_id": "gazebo-compatible-log-source",
            "vehicle_id": "gazebo-iris-log-001",
        },
        "captured_at": "2026-04-30T17:00:00+00:00",
        "telemetry": {
            "battery_remaining_pct": 88,
            "gazebo_process_started": True,
            "gps_fix": True,
            "heading_deg": 90.0,
            "position_x_m": 0.0,
            "position_y_m": 0.0,
            "position_z_m": 1.25,
            "sim_time_s": float(tick),
            "velocity_mps": 0.0,
        },
        "metadata": {
            "service": SERVICE_NAME,
            "telemetry_only": True,
            "read_only": True,
            "log_source": True,
            "simulator_family": "gazebo",
            "world_name": "empty_iris_telemetry_only",
            "collection_mode": "stdout_logs_only",
        },
    }


def _emit(prefix: str, payload: dict[str, Any]) -> None:
    print(prefix + json.dumps(payload, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--max-ticks", type=int, default=0)
    args = parser.parse_args()

    print(STARTUP_MARKER, flush=True)
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
