"""Collector for PX4/Gazebo-compatible telemetry logs."""

from __future__ import annotations

import json
from json import JSONDecodeError
from typing import Any

from src.runtime.px4_gazebo_telemetry import (
    Px4GazeboSanitizedTelemetry,
    Px4GazeboTelemetryRejected,
    attach_px4_gazebo_hil_review_gate_artifacts,
    sanitize_px4_gazebo_telemetry_sample,
)


PX4_GAZEBO_TELEMETRY_LOG_PREFIX = "PX4_GAZEBO_TELEMETRY "


class Px4GazeboLogCollectorError(RuntimeError):
    """Raised when PX4/Gazebo-compatible telemetry logs cannot be collected."""


def extract_latest_px4_gazebo_telemetry_sample(log_text: str) -> dict[str, Any]:
    """Extract the latest telemetry JSON object from process logs."""

    for line in reversed(log_text.splitlines()):
        if PX4_GAZEBO_TELEMETRY_LOG_PREFIX not in line:
            continue
        _, payload_text = line.split(PX4_GAZEBO_TELEMETRY_LOG_PREFIX, 1)
        try:
            payload = json.loads(payload_text)
        except JSONDecodeError as exc:
            raise Px4GazeboLogCollectorError(
                f"PX4/Gazebo telemetry log contains invalid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise Px4GazeboLogCollectorError(
                f"PX4/Gazebo telemetry log payload must be an object; got {type(payload).__name__}"
            )
        return payload
    raise Px4GazeboLogCollectorError("PX4/Gazebo telemetry log line not found")


def collect_px4_gazebo_log_sanitized(log_text: str) -> Px4GazeboSanitizedTelemetry:
    """Collect and sanitize the latest PX4/Gazebo-compatible telemetry log."""

    sample = extract_latest_px4_gazebo_telemetry_sample(log_text)
    try:
        return sanitize_px4_gazebo_telemetry_sample(sample)
    except Px4GazeboTelemetryRejected as exc:
        raise Px4GazeboLogCollectorError(
            f"PX4/Gazebo telemetry log rejected before persistence: {exc}"
        ) from exc


def attach_px4_gazebo_log_smoke_artifacts(
    task_id: str,
    log_text: str,
    *,
    task_store_factory,
    **kwargs: Any,
) -> dict[str, Any]:
    """Attach artifacts from PX4/Gazebo-compatible logs after validation.

    Log parsing and sanitizer validation happen before `TaskStore.update`, so
    telemetry/HIL/gate artifacts are not persisted when the log source is
    missing, malformed, or command-like.
    """

    sanitized = collect_px4_gazebo_log_sanitized(log_text)
    return attach_px4_gazebo_hil_review_gate_artifacts(
        task_id,
        sanitized,
        task_store_factory=task_store_factory,
        **kwargs,
    )


__all__ = [
    "PX4_GAZEBO_TELEMETRY_LOG_PREFIX",
    "Px4GazeboLogCollectorError",
    "attach_px4_gazebo_log_smoke_artifacts",
    "collect_px4_gazebo_log_sanitized",
    "extract_latest_px4_gazebo_telemetry_sample",
]
