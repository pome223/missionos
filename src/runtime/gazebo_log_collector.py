"""Collector for Gazebo-compatible telemetry-only stdout logs."""

from __future__ import annotations

from typing import Any

from src.runtime.px4_gazebo_log_collector import (
    Px4GazeboLogCollectorError,
    collect_px4_gazebo_log_sanitized,
)
from src.runtime.px4_gazebo_telemetry import (
    Px4GazeboSanitizedTelemetry,
    attach_px4_gazebo_hil_review_gate_artifacts,
)


GAZEBO_TELEMETRY_STARTUP_MARKER = "GAZEBO_TELEMETRY_SOURCE_READY"


class GazeboLogCollectorError(RuntimeError):
    """Raised when Gazebo-compatible logs cannot become telemetry evidence."""


def _validate_gazebo_log_source_started(log_text: str) -> None:
    if not log_text.strip():
        raise GazeboLogCollectorError("Gazebo telemetry log output is empty")
    if GAZEBO_TELEMETRY_STARTUP_MARKER not in log_text:
        raise GazeboLogCollectorError(
            "Gazebo telemetry startup marker missing: "
            f"{GAZEBO_TELEMETRY_STARTUP_MARKER}"
        )


def collect_gazebo_log_sanitized(log_text: str) -> Px4GazeboSanitizedTelemetry:
    """Collect Gazebo-compatible logs into the shared sanitized telemetry artifact."""

    _validate_gazebo_log_source_started(log_text)
    try:
        return collect_px4_gazebo_log_sanitized(log_text)
    except Px4GazeboLogCollectorError as exc:
        raise GazeboLogCollectorError(
            f"Gazebo telemetry log rejected before persistence: {exc}"
        ) from exc


def attach_gazebo_log_smoke_artifacts(
    task_id: str,
    log_text: str,
    *,
    task_store_factory,
    **kwargs: Any,
) -> dict[str, Any]:
    """Attach artifacts from Gazebo-compatible logs after fail-closed validation."""

    sanitized = collect_gazebo_log_sanitized(log_text)
    return attach_px4_gazebo_hil_review_gate_artifacts(
        task_id,
        sanitized,
        task_store_factory=task_store_factory,
        **kwargs,
    )


__all__ = [
    "GAZEBO_TELEMETRY_STARTUP_MARKER",
    "GazeboLogCollectorError",
    "attach_gazebo_log_smoke_artifacts",
    "collect_gazebo_log_sanitized",
]
