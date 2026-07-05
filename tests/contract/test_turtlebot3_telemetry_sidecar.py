from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runtime.turtlebot3_telemetry_sidecar import (
    TurtleBot3TelemetrySidecarError,
    build_turtlebot3_state_correlation,
    build_turtlebot3_telemetry_window_from_jsonl,
    load_turtlebot3_telemetry_sidecar_jsonl,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_turtlebot3_telemetry_sidecar_window_and_correlation(tmp_path: Path) -> None:
    jsonl = tmp_path / "telemetry.jsonl"
    _write_jsonl(
        jsonl,
        [
            {
                "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                "sample_kind": "odom",
                "captured_at": "2026-07-02T00:00:00+00:00",
                "topic": "/odom",
                "position": {"x_m": 0.0, "y_m": 0.0},
            },
            {
                "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                "sample_kind": "scan",
                "captured_at": "2026-07-02T00:00:01+00:00",
                "topic": "/scan",
                "min_range_m": 0.42,
            },
            {
                "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                "sample_kind": "battery",
                "captured_at": "2026-07-02T00:00:02+00:00",
                "topic": "/battery_state",
                "percentage": 0.86,
            },
            {
                "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                "sample_kind": "odom",
                "captured_at": "2026-07-02T00:00:03+00:00",
                "topic": "/odom",
                "position": {"x_m": 0.3, "y_m": 0.4},
            },
        ],
    )

    window = build_turtlebot3_telemetry_window_from_jsonl(jsonl)
    correlation = build_turtlebot3_state_correlation(
        telemetry_window=window,
        bridge_motion={"robot_motion_observed": True, "odom_delta_m": 0.52},
    )

    assert window.schema_version == "missionos_turtlebot3_telemetry_window.v1"
    assert window.window_status == "ready"
    assert window.sample_count == 4
    assert window.odom_sample_count == 2
    assert window.battery_sample_count == 1
    assert window.scan_sample_count == 1
    assert window.odom_motion_observed is True
    assert window.odom_delta_m == pytest.approx(0.5)
    assert window.battery_latest_pct == 86.0
    assert window.scan_obstacle_observed is True
    assert window.raw_logs_ref.startswith("turtlebot3_telemetry_sidecar_jsonl:")
    assert window.command_payload_allowed is False
    assert window.physical_execution_invoked is False

    assert correlation.correlation_status == "ready"
    assert correlation.motion_correlation_confirmed is True
    assert correlation.bridge_motion_observed is True
    assert correlation.sidecar_motion_observed is True
    assert correlation.physical_execution_invoked is False


def test_turtlebot3_telemetry_sidecar_rejects_command_like_samples(
    tmp_path: Path,
) -> None:
    jsonl = tmp_path / "telemetry.jsonl"
    _write_jsonl(
        jsonl,
        [
            {
                "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                "sample_kind": "odom",
                "captured_at": "2026-07-02T00:00:00+00:00",
                "topic": "/cmd_vel",
                "position": {"x_m": 0.0, "y_m": 0.0},
            },
        ],
    )

    with pytest.raises(TurtleBot3TelemetrySidecarError, match="command-like"):
        load_turtlebot3_telemetry_sidecar_jsonl(jsonl)


def test_turtlebot3_telemetry_sidecar_blocks_without_odom_motion(
    tmp_path: Path,
) -> None:
    jsonl = tmp_path / "telemetry.jsonl"
    _write_jsonl(
        jsonl,
        [
            {
                "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                "sample_kind": "odom",
                "captured_at": "2026-07-02T00:00:00+00:00",
                "topic": "/odom",
                "position": {"x_m": 0.0, "y_m": 0.0},
            },
            {
                "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                "sample_kind": "odom",
                "captured_at": "2026-07-02T00:00:01+00:00",
                "topic": "/odom",
                "position": {"x_m": 0.0, "y_m": 0.0},
            },
        ],
    )

    window = build_turtlebot3_telemetry_window_from_jsonl(jsonl)
    correlation = build_turtlebot3_state_correlation(
        telemetry_window=window,
        bridge_motion={"robot_motion_observed": True, "odom_delta_m": 0.26},
    )

    assert window.window_status == "blocked"
    assert "telemetry_sidecar_odom_motion_not_observed" in window.blocked_reasons
    assert correlation.correlation_status == "blocked"
    assert correlation.motion_correlation_confirmed is False
    assert "sidecar_motion_not_observed" in correlation.blocked_reasons
