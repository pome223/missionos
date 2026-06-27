"""Collector for actual headless Gazebo Classic stdout logs."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from src.runtime.px4_gazebo_telemetry import (
    Px4GazeboSanitizedTelemetry,
    Px4GazeboTelemetryRejected,
    attach_px4_gazebo_hil_review_gate_artifacts,
    sanitize_px4_gazebo_telemetry_sample,
)


class HeadlessGazeboLogCollectorError(RuntimeError):
    """Raised when headless Gazebo logs cannot become telemetry evidence."""


REQUIRED_HEADLESS_GAZEBO_MARKERS = (
    "Gazebo multi-robot simulator",
    "Connected to gazebo master",
    "Loading world file",
)


def _meaningful_lines(log_text: str) -> list[str]:
    return [line.strip() for line in log_text.splitlines() if line.strip()]


def _validate_headless_gazebo_started(log_text: str) -> None:
    if not log_text.strip():
        raise HeadlessGazeboLogCollectorError("Headless Gazebo log output is empty")
    missing = [
        marker for marker in REQUIRED_HEADLESS_GAZEBO_MARKERS if marker not in log_text
    ]
    if missing:
        raise HeadlessGazeboLogCollectorError(
            "Headless Gazebo startup logs are incomplete: " + ", ".join(missing)
        )


def build_headless_gazebo_log_telemetry_sample(
    log_text: str,
    *,
    captured_at: datetime | None = None,
    source_id: str = "headless-gazebo-classic-container",
    vehicle_id: str = "gazebo-classic-empty-world",
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a telemetry-only sample from observed Gazebo Classic stdout logs.

    This treats Gazebo as a log source. It does not mutate Gazebo entities,
    publish ROS, send MAVLink, command setpoints, or expose actuator control.
    """

    captured = captured_at or datetime.now(timezone.utc)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    captured = captured.astimezone(timezone.utc)

    _validate_headless_gazebo_started(log_text)
    lines = _meaningful_lines(log_text)
    digest = sha256(log_text.encode("utf-8")).hexdigest()[:16]
    return {
        "sample_id": f"headless_gazebo_classic_stdout_{digest}",
        "source": {
            "source_kind": "headless_gazebo_classic_stdout_log",
            "source_id": source_id,
            "vehicle_id": vehicle_id,
        },
        "captured_at": captured.isoformat(),
        "telemetry": {
            "gazebo_process_started": True,
            "headless": True,
            "world_loaded": True,
            "log_line_count": len(lines),
            "log_digest": digest,
            "latest_log_excerpt": lines[-1][-300:],
        },
        "metadata": {
            "telemetry_only": True,
            "read_only": True,
            "source_image": "gazebo:gzserver11-focal",
            "image_tag": "gzserver11-focal",
            "simulator_family": "gazebo",
            "simulator_kind": "gazebo_classic_headless",
            "collection_mode": "stdout_logs_only",
            "world_name": "empty.world",
            "gazebo_command": ["gzserver", "--verbose", "worlds/empty.world"],
            "startup_markers": list(REQUIRED_HEADLESS_GAZEBO_MARKERS),
            "actual_gazebo_runtime": True,
            **(provenance or {}),
        },
    }


def collect_headless_gazebo_log_sanitized(
    log_text: str,
    *,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
) -> Px4GazeboSanitizedTelemetry:
    """Collect headless Gazebo stdout logs into sanitized telemetry."""

    sample = build_headless_gazebo_log_telemetry_sample(
        log_text,
        captured_at=captured_at,
        provenance=provenance,
    )
    try:
        return sanitize_px4_gazebo_telemetry_sample(sample)
    except Px4GazeboTelemetryRejected as exc:
        raise HeadlessGazeboLogCollectorError(
            f"Headless Gazebo stdout telemetry rejected before persistence: {exc}"
        ) from exc


def attach_headless_gazebo_log_hil_review_gate_artifacts(
    task_id: str,
    log_text: str,
    *,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
    task_store_factory,
) -> dict[str, Any]:
    """Attach headless Gazebo log-derived HIL review/gate artifacts to a task."""

    sanitized = collect_headless_gazebo_log_sanitized(
        log_text,
        captured_at=captured_at,
        provenance=provenance,
    )
    return attach_px4_gazebo_hil_review_gate_artifacts(
        task_id,
        sanitized,
        now=sanitized.captured_at,
        task_store_factory=task_store_factory,
    )


__all__ = [
    "HeadlessGazeboLogCollectorError",
    "REQUIRED_HEADLESS_GAZEBO_MARKERS",
    "attach_headless_gazebo_log_hil_review_gate_artifacts",
    "build_headless_gazebo_log_telemetry_sample",
    "collect_headless_gazebo_log_sanitized",
]
