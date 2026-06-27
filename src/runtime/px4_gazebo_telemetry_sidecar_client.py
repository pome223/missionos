"""Client for the PX4/Gazebo-style telemetry-only sidecar smoke service."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import json
from json import JSONDecodeError
from typing import Any
from urllib import error, parse, request

from src.runtime.px4_gazebo_telemetry import (
    Px4GazeboSanitizedTelemetry,
    Px4GazeboTelemetryEvidenceError,
    attach_px4_gazebo_hil_review_gate_artifacts,
    sanitize_px4_gazebo_telemetry_sample,
)
from src.runtime.task_store import TaskStore


DEFAULT_PX4_GAZEBO_TELEMETRY_SIDECAR_URL = "http://127.0.0.1:18889"


class Px4GazeboTelemetrySidecarClientError(RuntimeError):
    """Raised when the telemetry sidecar cannot be used safely."""


def _get_json(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    try:
        with request.urlopen(url, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise Px4GazeboTelemetrySidecarClientError(
            f"PX4/Gazebo telemetry sidecar HTTP {exc.code} for {url}: {detail}"
        ) from exc
    except TimeoutError as exc:
        raise Px4GazeboTelemetrySidecarClientError(
            f"PX4/Gazebo telemetry sidecar timed out for {url}"
        ) from exc
    except OSError as exc:
        raise Px4GazeboTelemetrySidecarClientError(
            f"PX4/Gazebo telemetry sidecar unavailable for {url}: {exc}"
        ) from exc

    try:
        payload = json.loads(body)
    except JSONDecodeError as exc:
        raise Px4GazeboTelemetrySidecarClientError(
            f"PX4/Gazebo telemetry sidecar returned invalid JSON for {url}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise Px4GazeboTelemetrySidecarClientError(
            f"PX4/Gazebo telemetry sidecar returned {type(payload).__name__}"
        )
    return payload


def fetch_px4_gazebo_telemetry_sidecar_health(
    *,
    base_url: str = DEFAULT_PX4_GAZEBO_TELEMETRY_SIDECAR_URL,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Fetch and validate the sidecar health/safety boundary payload."""

    health = _get_json(
        base_url.rstrip("/") + "/health",
        timeout_seconds=timeout_seconds,
    )
    required_false = [
        "command_payload_allowed",
        "ros_dispatch_allowed",
        "mavlink_dispatch_allowed",
        "actuator_execution_allowed",
        "live_execution_allowed",
        "physical_execution_invoked",
    ]
    for key in required_false:
        if health.get(key) is not False:
            raise Px4GazeboTelemetrySidecarClientError(
                f"PX4/Gazebo telemetry sidecar unsafe health flag {key}={health.get(key)!r}"
            )
    if health.get("status") != "ok":
        raise Px4GazeboTelemetrySidecarClientError(
            f"PX4/Gazebo telemetry sidecar not healthy: {health.get('status')!r}"
        )
    return health


def fetch_px4_gazebo_telemetry_sidecar_sample(
    *,
    base_url: str = DEFAULT_PX4_GAZEBO_TELEMETRY_SIDECAR_URL,
    telemetry_case: str = "nominal",
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Fetch raw telemetry/log JSON from the read-only sidecar."""

    query = parse.urlencode({"case": telemetry_case})
    return _get_json(
        base_url.rstrip("/") + f"/telemetry?{query}",
        timeout_seconds=timeout_seconds,
    )


def collect_px4_gazebo_telemetry_sidecar_sanitized(
    *,
    base_url: str = DEFAULT_PX4_GAZEBO_TELEMETRY_SIDECAR_URL,
    telemetry_case: str = "nominal",
    timeout_seconds: float = 10.0,
) -> Px4GazeboSanitizedTelemetry:
    """Read telemetry from the sidecar and sanitize it before persistence."""

    fetch_px4_gazebo_telemetry_sidecar_health(
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    sample = fetch_px4_gazebo_telemetry_sidecar_sample(
        base_url=base_url,
        telemetry_case=telemetry_case,
        timeout_seconds=timeout_seconds,
    )
    try:
        return sanitize_px4_gazebo_telemetry_sample(sample)
    except Exception as exc:
        raise Px4GazeboTelemetrySidecarClientError(
            f"PX4/Gazebo telemetry sidecar sample rejected before persistence: {exc}"
        ) from exc


def attach_px4_gazebo_telemetry_sidecar_smoke_artifacts(
    task_id: str,
    *,
    base_url: str = DEFAULT_PX4_GAZEBO_TELEMETRY_SIDECAR_URL,
    telemetry_case: str = "nominal",
    freshness_threshold_seconds: float = 60.0,
    now: datetime | None = None,
    timeout_seconds: float = 10.0,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Collect sidecar telemetry and attach validated HIL review/gate artifacts."""

    sanitized = collect_px4_gazebo_telemetry_sidecar_sanitized(
        base_url=base_url,
        telemetry_case=telemetry_case,
        timeout_seconds=timeout_seconds,
    )
    try:
        return attach_px4_gazebo_hil_review_gate_artifacts(
            task_id,
            sanitized,
            freshness_threshold_seconds=freshness_threshold_seconds,
            now=now,
            task_store_factory=task_store_factory,
        )
    except Px4GazeboTelemetryEvidenceError:
        raise
    except Exception as exc:
        raise Px4GazeboTelemetrySidecarClientError(
            f"PX4/Gazebo telemetry sidecar artifacts could not be attached: {exc}"
        ) from exc


__all__ = [
    "DEFAULT_PX4_GAZEBO_TELEMETRY_SIDECAR_URL",
    "Px4GazeboTelemetrySidecarClientError",
    "attach_px4_gazebo_telemetry_sidecar_smoke_artifacts",
    "collect_px4_gazebo_telemetry_sidecar_sanitized",
    "fetch_px4_gazebo_telemetry_sidecar_health",
    "fetch_px4_gazebo_telemetry_sidecar_sample",
]
