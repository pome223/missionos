"""Collector for real PX4 SIH container stdout logs."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from collections.abc import Callable
from typing import Any

from src.runtime.px4_gazebo_telemetry import (
    Px4GazeboSanitizedTelemetry,
    Px4GazeboTelemetryRejected,
    attach_px4_gazebo_hil_review_gate_artifacts,
    sanitize_px4_gazebo_telemetry_sample,
)
from src.runtime.task_store import TaskStore, get_task_store


class Px4SitlLogCollectorError(RuntimeError):
    """Raised when PX4 SIH logs cannot become telemetry-only evidence."""


_REQUIRED_STARTUP_MARKERS = (
    "INFO  [init] SIH simulator",
    "INFO  [simulator_sih] Simulation loop",
    "INFO  [px4] Startup script returned successfully",
)

_STARTUP_FAILURE_MARKERS = (
    "Error creating directory",
    "Read-only file system",
    "Unknown model",
    "Startup script returned with return value",
)


def _latest_meaningful_line(log_text: str) -> str:
    lines = _meaningful_lines(log_text)
    if not lines:
        raise Px4SitlLogCollectorError("PX4 SIH log output is empty")
    return lines[-1][-300:]


def _meaningful_lines(log_text: str) -> list[str]:
    return [line.strip() for line in log_text.splitlines() if line.strip()]


def _line_count(log_text: str) -> int:
    return len(_meaningful_lines(log_text))


def _validate_px4_sitl_started(log_text: str) -> None:
    if not log_text.strip():
        raise Px4SitlLogCollectorError("PX4 SIH log output is empty")
    if any(marker in log_text for marker in _STARTUP_FAILURE_MARKERS):
        raise Px4SitlLogCollectorError("PX4 SIH startup failure was observed")
    missing = [marker for marker in _REQUIRED_STARTUP_MARKERS if marker not in log_text]
    if missing:
        raise Px4SitlLogCollectorError(
            "PX4 SIH startup logs are incomplete: " + ", ".join(missing)
        )


def build_px4_sitl_log_telemetry_sample(
    log_text: str,
    *,
    captured_at: datetime | None = None,
    source_id: str = "px4-sitl-sih-container",
    vehicle_id: str = "px4-sitl-sih-001",
    provenance: dict[str, Any] | None = None,
    original_log_line_count: int | None = None,
    max_window_lines: int | None = None,
    max_duration_seconds: float | None = None,
) -> dict[str, Any]:
    """Build a telemetry-only sample from observed PX4 SIH stdout logs.

    This intentionally treats PX4 as a log source. It does not open MAVLink,
    publish ROS, upload missions, send setpoints, or command the simulator.
    """

    captured = captured_at or datetime.now(timezone.utc)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    captured = captured.astimezone(timezone.utc)

    _validate_px4_sitl_started(log_text)
    lines = _line_count(log_text)
    latest_line = _latest_meaningful_line(log_text)
    digest = sha256(log_text.encode("utf-8")).hexdigest()[:16]
    original_lines = (
        original_log_line_count if original_log_line_count is not None else lines
    )
    window_truncated = original_lines > lines
    return {
        "sample_id": f"px4_sitl_sih_stdout_{digest}",
        "source": {
            "source_kind": "px4_sitl_sih_stdout_log",
            "source_id": source_id,
            "vehicle_id": vehicle_id,
        },
        "captured_at": captured.isoformat(),
        "telemetry": {
            "px4_sitl_started": True,
            "log_line_count": lines,
            "window_line_count": lines,
            "original_log_line_count": original_lines,
            "window_truncated": window_truncated,
            "log_digest": digest,
            "latest_log_excerpt": latest_line,
        },
        "metadata": {
            "telemetry_only": True,
            "read_only": True,
            "source_image": "px4io/px4-sitl:latest",
            "image_tag": "latest",
            "simulator_kind": "px4_sitl_sih",
            "collection_mode": "stdout_logs_only",
            "window_bounded": max_window_lines is not None
            or max_duration_seconds is not None,
            "max_window_lines": max_window_lines,
            "max_duration_seconds": max_duration_seconds,
            "px4_sim_model": "sihsim_quadx",
            "px4_daemon_args": ["-d"],
            "startup_markers": list(_REQUIRED_STARTUP_MARKERS),
            **(provenance or {}),
        },
    }


def _bounded_log_window(log_text: str, *, max_window_lines: int) -> str:
    if max_window_lines <= 0:
        raise Px4SitlLogCollectorError("max_window_lines must be greater than zero")
    lines = _meaningful_lines(log_text)
    if not lines:
        raise Px4SitlLogCollectorError("PX4 SIH log output is empty")
    return "\n".join(lines[:max_window_lines])


def collect_px4_sitl_log_sanitized(
    log_text: str,
    *,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
) -> Px4GazeboSanitizedTelemetry:
    """Collect PX4 SIH stdout logs into the shared sanitized telemetry artifact."""

    sample = build_px4_sitl_log_telemetry_sample(
        log_text,
        captured_at=captured_at,
        provenance=provenance,
    )
    try:
        return sanitize_px4_gazebo_telemetry_sample(sample)
    except Px4GazeboTelemetryRejected as exc:
        raise Px4SitlLogCollectorError(
            f"PX4 SIH stdout telemetry rejected before persistence: {exc}"
        ) from exc


def collect_px4_sitl_bounded_log_window_sanitized(
    log_text: str,
    *,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
    max_window_lines: int = 80,
    max_duration_seconds: float = 3.0,
) -> Px4GazeboSanitizedTelemetry:
    """Collect a bounded PX4 SIH stdout log window into sanitized telemetry."""

    if max_duration_seconds <= 0:
        raise Px4SitlLogCollectorError(
            "max_duration_seconds must be greater than zero"
        )
    original_lines = _line_count(log_text)
    window = _bounded_log_window(log_text, max_window_lines=max_window_lines)
    sample = build_px4_sitl_log_telemetry_sample(
        window,
        captured_at=captured_at,
        provenance=provenance,
        original_log_line_count=original_lines,
        max_window_lines=max_window_lines,
        max_duration_seconds=float(max_duration_seconds),
    )
    try:
        return sanitize_px4_gazebo_telemetry_sample(sample)
    except Px4GazeboTelemetryRejected as exc:
        raise Px4SitlLogCollectorError(
            f"PX4 SIH bounded stdout telemetry rejected before persistence: {exc}"
        ) from exc


def attach_px4_sitl_log_hil_review_gate_artifacts(
    task_id: str,
    log_text: str,
    *,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
    max_window_lines: int | None = None,
    max_duration_seconds: float | None = None,
    task_store_factory: Callable[[], TaskStore] = get_task_store,
) -> dict[str, Any]:
    """Attach PX4 SIH log-derived HIL review/gate artifacts to a task.

    Invalid logs fail before any task persistence. The helper intentionally
    preserves the existing HIL attach path rather than adding a simulator-
    specific persistence route.
    """

    if max_window_lines is None and max_duration_seconds is None:
        sanitized = collect_px4_sitl_log_sanitized(
            log_text,
            captured_at=captured_at,
            provenance=provenance,
        )
    else:
        sanitized = collect_px4_sitl_bounded_log_window_sanitized(
            log_text,
            captured_at=captured_at,
            provenance=provenance,
            max_window_lines=max_window_lines or 80,
            max_duration_seconds=max_duration_seconds or 3.0,
        )
    return attach_px4_gazebo_hil_review_gate_artifacts(
        task_id,
        sanitized,
        now=sanitized.captured_at,
        task_store_factory=task_store_factory,
    )


__all__ = [
    "Px4SitlLogCollectorError",
    "attach_px4_sitl_log_hil_review_gate_artifacts",
    "build_px4_sitl_log_telemetry_sample",
    "collect_px4_sitl_bounded_log_window_sanitized",
    "collect_px4_sitl_log_sanitized",
]
