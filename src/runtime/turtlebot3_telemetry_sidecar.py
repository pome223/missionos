"""Read-only TurtleBot3/Nav2 telemetry sidecar artifacts.

The sidecar path is an observation path only. It reads ROS2 telemetry already
being produced by the simulator and turns the JSONL samples into bounded
MissionOS evidence. It must not publish topics, send Nav2 actions, approve
dispatch, claim delivery completion, or claim physical execution.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TURTLEBOT3_TELEMETRY_SAMPLE_SCHEMA_VERSION = (
    "missionos_turtlebot3_telemetry_sample.v1"
)
TURTLEBOT3_TELEMETRY_WINDOW_SCHEMA_VERSION = (
    "missionos_turtlebot3_telemetry_window.v1"
)
TURTLEBOT3_STATE_CORRELATION_SCHEMA_VERSION = (
    "missionos_turtlebot3_state_correlation.v1"
)
TURTLEBOT3_TELEMETRY_SIDECAR_JSONL_ENV = (
    "MISSIONOS_TURTLEBOT3_TELEMETRY_SIDECAR_JSONL"
)
TURTLEBOT3_LIVE_TASK_ID_PATH_ENV = "MISSIONOS_TURTLEBOT3_LIVE_TASK_ID_PATH"

TelemetryWindowStatus = Literal["ready", "blocked"]


class TurtleBot3TelemetrySidecarError(RuntimeError):
    """Raised when TurtleBot3 telemetry sidecar evidence is unsafe."""


_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "action",
        "actuator",
        "cmd_vel",
        "command",
        "dispatch",
        "execute",
        "goal_pose",
        "initialpose",
        "joint",
        "motor",
        "navigate_to_pose",
        "physical_execution",
        "physical_execution_invoked",
        "raw_ros_topic_published",
        "raw_velocity",
        "ros_action",
        "setpoint",
        "thrust",
        "torque",
        "velocity_command",
    }
)
_FORBIDDEN_COMMAND_VALUE_TOKENS = frozenset(
    {
        "/cmd_vel",
        "/cmd_vel_nav",
        "/goal_pose",
        "/initialpose",
        "/navigate_to_pose",
        "navigate_to_pose",
        "raw_velocity",
    }
)


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


_FORBIDDEN_COMMAND_KEYS_NORMALIZED = frozenset(
    _normalize_key(key) for key in _FORBIDDEN_COMMAND_KEYS
)
_FORBIDDEN_COMMAND_VALUES_NORMALIZED = frozenset(
    _normalize_key(value) for value in _FORBIDDEN_COMMAND_VALUE_TOKENS
)


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    if value is None:
        return datetime.now(timezone.utc)
    return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _command_like_paths(value: Any, *, root: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, sub in value.items():
            key_text = str(key)
            path = f"{root}.{key_text}" if root else key_text
            if _normalize_key(key_text) in _FORBIDDEN_COMMAND_KEYS_NORMALIZED:
                findings.append(path)
            findings.extend(_command_like_paths(sub, root=path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            path = f"{root}.{index}" if root else str(index)
            findings.extend(_command_like_paths(item, root=path))
    elif isinstance(value, str):
        normalized = _normalize_key(value)
        if normalized in _FORBIDDEN_COMMAND_VALUES_NORMALIZED:
            findings.append(root or "<value>")
    return findings


def _raise_for_command_like_content(value: Any, *, root: str) -> None:
    findings = _command_like_paths(value, root=root)
    if findings:
        raise TurtleBot3TelemetrySidecarError(
            "turtlebot3 telemetry sidecar refused command-like content: "
            + ", ".join(sorted(findings))
        )


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _sample_kind(sample: Mapping[str, Any]) -> str:
    return str(sample.get("sample_kind") or sample.get("kind") or "").strip()


def _position_xy(sample: Mapping[str, Any]) -> tuple[float, float] | None:
    position = sample.get("position")
    if isinstance(position, Mapping):
        x_m = _as_float(position.get("x_m", position.get("x")))
        y_m = _as_float(position.get("y_m", position.get("y")))
    else:
        x_m = _as_float(sample.get("x_m", sample.get("x")))
        y_m = _as_float(sample.get("y_m", sample.get("y")))
    if x_m is None or y_m is None:
        return None
    return x_m, y_m


def _battery_pct(sample: Mapping[str, Any]) -> float | None:
    for key in ("battery_pct", "battery_remaining_pct", "percentage"):
        value = _as_float(sample.get(key))
        if value is None:
            continue
        if 0.0 <= value <= 1.0:
            return round(value * 100.0, 3)
        if 0.0 <= value <= 100.0:
            return round(value, 3)
    return None


def _scan_min_range_m(sample: Mapping[str, Any]) -> float | None:
    value = _as_float(sample.get("min_range_m"))
    if value is not None:
        return value
    ranges = sample.get("ranges_m")
    if not isinstance(ranges, Sequence) or isinstance(ranges, (str, bytes)):
        return None
    finite = [
        float(item)
        for item in ranges
        if isinstance(item, (int, float)) and not isinstance(item, bool) and item > 0
    ]
    return min(finite) if finite else None


def load_turtlebot3_telemetry_sidecar_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate read-only TurtleBot3 telemetry sidecar JSONL samples."""

    source_path = Path(path)
    try:
        text = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TurtleBot3TelemetrySidecarError(
            f"turtlebot3 telemetry sidecar JSONL not readable: {source_path}"
        ) from exc
    samples: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise TurtleBot3TelemetrySidecarError(
                f"turtlebot3 telemetry sidecar JSONL line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise TurtleBot3TelemetrySidecarError(
                f"turtlebot3 telemetry sidecar JSONL line {line_number} is not an object"
            )
        _raise_for_command_like_content(payload, root=f"line_{line_number}")
        samples.append(dict(payload))
    return samples


class TurtleBot3TelemetryWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TURTLEBOT3_TELEMETRY_WINDOW_SCHEMA_VERSION] = (
        TURTLEBOT3_TELEMETRY_WINDOW_SCHEMA_VERSION
    )
    window_id: str
    source_kind: Literal["ros2_nav2_turtlebot3_telemetry_sidecar"] = (
        "ros2_nav2_turtlebot3_telemetry_sidecar"
    )
    source_id: str = Field(min_length=1)
    source_jsonl_path: str | None = None
    source_jsonl_sha256: str | None = None
    raw_logs_ref: str = Field(min_length=1)
    captured_at_start: datetime
    captured_at_end: datetime
    max_duration_seconds: float = Field(gt=0)
    max_sample_count: int = Field(gt=0)
    sample_count: int = Field(ge=0)
    odom_sample_count: int = Field(ge=0)
    battery_sample_count: int = Field(ge=0)
    scan_sample_count: int = Field(ge=0)
    measurement_keys: tuple[str, ...] = ()
    odom_topic: str = "/odom"
    odom_delta_m: float | None = None
    odom_motion_threshold_m: float = Field(gt=0)
    odom_motion_observed: bool = False
    battery_latest_pct: float | None = Field(default=None, ge=0, le=100)
    battery_topic: str = "/battery_state"
    scan_topic: str = "/scan"
    scan_min_range_m: float | None = None
    scan_obstacle_threshold_m: float = Field(gt=0)
    scan_obstacle_observed: bool = False
    window_status: TelemetryWindowStatus
    blocked_reasons: tuple[str, ...] = ()
    created_at: datetime
    simulation_only: Literal[True] = True
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    command_surface_present: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    raw_velocity_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    mission_delivery_completion_claimed: Literal[False] = False
    llm_judge_used: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("captured_at_start", "captured_at_end", "created_at", mode="before")
    @classmethod
    def _coerce_datetime(cls, value: Any) -> datetime:
        return _parse_datetime(value)

    @field_validator("blocked_reasons", "measurement_keys", mode="before")
    @classmethod
    def _normalize_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))

    @model_validator(mode="after")
    def _validate_window(self) -> "TurtleBot3TelemetryWindow":
        _raise_for_command_like_content(self.metadata, root="metadata")
        if self.window_status == "ready" and self.blocked_reasons:
            raise ValueError("ready telemetry window cannot include blocked reasons")
        if self.window_status == "blocked" and not self.blocked_reasons:
            raise ValueError("blocked telemetry window requires blocked reasons")
        if self.odom_motion_observed:
            if self.odom_delta_m is None:
                raise ValueError("observed odom motion requires odom_delta_m")
            if self.odom_delta_m < self.odom_motion_threshold_m:
                raise ValueError("observed odom motion must exceed threshold")
        return self


class TurtleBot3StateCorrelation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TURTLEBOT3_STATE_CORRELATION_SCHEMA_VERSION] = (
        TURTLEBOT3_STATE_CORRELATION_SCHEMA_VERSION
    )
    correlation_id: str
    telemetry_window_ref: str = Field(min_length=1)
    raw_logs_ref: str = Field(min_length=1)
    bridge_motion_observed: bool
    bridge_odom_delta_m: float | None = None
    sidecar_motion_observed: bool
    sidecar_odom_delta_m: float | None = None
    odom_delta_difference_m: float | None = None
    motion_correlation_confirmed: bool = False
    correlation_status: TelemetryWindowStatus
    blocked_reasons: tuple[str, ...] = ()
    observed_at: datetime
    simulation_only: Literal[True] = True
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    command_surface_present: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    mission_delivery_completion_claimed: Literal[False] = False

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        return _parse_datetime(value)

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _normalize_blocked_reasons(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))

    @model_validator(mode="after")
    def _validate_correlation(self) -> "TurtleBot3StateCorrelation":
        if self.correlation_status == "ready" and self.blocked_reasons:
            raise ValueError("ready correlation cannot include blocked reasons")
        if self.correlation_status == "blocked" and not self.blocked_reasons:
            raise ValueError("blocked correlation requires blocked reasons")
        if self.motion_correlation_confirmed and self.correlation_status != "ready":
            raise ValueError("motion correlation confirmation requires ready status")
        if self.motion_correlation_confirmed and not (
            self.bridge_motion_observed and self.sidecar_motion_observed
        ):
            raise ValueError("motion correlation requires both bridge and sidecar motion")
        return self


def build_turtlebot3_telemetry_window(
    *,
    samples: Sequence[Mapping[str, Any]],
    source_jsonl_path: str | Path | None = None,
    source_jsonl_sha256: str | None = None,
    max_duration_seconds: float = 300.0,
    max_sample_count: int = 10000,
    odom_motion_threshold_m: float = 0.03,
    scan_obstacle_threshold_m: float = 0.8,
    now: datetime | None = None,
) -> TurtleBot3TelemetryWindow:
    """Build a bounded read-only telemetry window from sidecar samples."""

    for index, sample in enumerate(samples, start=1):
        _raise_for_command_like_content(sample, root=f"samples.{index}")
    created_at = _utc(now)
    odom_positions: list[tuple[float, float]] = []
    captured_at_values: list[datetime] = []
    battery_latest_pct: float | None = None
    scan_ranges: list[float] = []
    odom_count = 0
    battery_count = 0
    scan_count = 0
    measurement_keys: set[str] = set()

    for sample in samples:
        kind = _sample_kind(sample)
        if kind:
            measurement_keys.add(kind)
        captured_at_values.append(_parse_datetime(sample.get("captured_at")))
        if kind == "odom":
            odom_count += 1
            xy = _position_xy(sample)
            if xy is not None:
                odom_positions.append(xy)
        elif kind == "battery":
            battery_count += 1
            pct = _battery_pct(sample)
            if pct is not None:
                battery_latest_pct = pct
        elif kind == "scan":
            scan_count += 1
            min_range = _scan_min_range_m(sample)
            if min_range is not None:
                scan_ranges.append(min_range)

    odom_delta_m = None
    if len(odom_positions) >= 2:
        first_x, first_y = odom_positions[0]
        last_x, last_y = odom_positions[-1]
        odom_delta_m = ((last_x - first_x) ** 2 + (last_y - first_y) ** 2) ** 0.5
    odom_motion_observed = (
        odom_delta_m is not None and odom_delta_m >= odom_motion_threshold_m
    )
    scan_min_range_m = min(scan_ranges) if scan_ranges else None
    scan_obstacle_observed = (
        scan_min_range_m is not None and scan_min_range_m <= scan_obstacle_threshold_m
    )
    blocked_reasons: list[str] = []
    if odom_count < 2:
        blocked_reasons.append("telemetry_sidecar_odom_samples_missing")
    if not odom_motion_observed:
        blocked_reasons.append("telemetry_sidecar_odom_motion_not_observed")

    source_path_text = str(source_jsonl_path) if source_jsonl_path is not None else None
    payload_for_id = {
        "source_jsonl_path": source_path_text,
        "source_jsonl_sha256": source_jsonl_sha256,
        "sample_count": len(samples),
        "odom_delta_m": odom_delta_m,
        "measurement_keys": sorted(measurement_keys),
    }
    digest = source_jsonl_sha256 or sha256(
        json.dumps(payload_for_id, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return TurtleBot3TelemetryWindow(
        window_id=_stable_id("turtlebot3_telemetry_window", payload_for_id),
        source_id="turtlebot3-telemetry-sidecar",
        source_jsonl_path=source_path_text,
        source_jsonl_sha256=source_jsonl_sha256,
        raw_logs_ref=f"turtlebot3_telemetry_sidecar_jsonl:{digest[:16]}",
        captured_at_start=min(captured_at_values, default=created_at),
        captured_at_end=max(captured_at_values, default=created_at),
        max_duration_seconds=max_duration_seconds,
        max_sample_count=max_sample_count,
        sample_count=len(samples),
        odom_sample_count=odom_count,
        battery_sample_count=battery_count,
        scan_sample_count=scan_count,
        measurement_keys=tuple(sorted(measurement_keys)),
        odom_delta_m=odom_delta_m,
        odom_motion_threshold_m=odom_motion_threshold_m,
        odom_motion_observed=odom_motion_observed,
        battery_latest_pct=battery_latest_pct,
        scan_min_range_m=scan_min_range_m,
        scan_obstacle_threshold_m=scan_obstacle_threshold_m,
        scan_obstacle_observed=scan_obstacle_observed,
        window_status="blocked" if blocked_reasons else "ready",
        blocked_reasons=tuple(blocked_reasons),
        created_at=created_at,
    )


def build_turtlebot3_telemetry_window_from_jsonl(
    path: str | Path,
    *,
    max_duration_seconds: float = 300.0,
    max_sample_count: int = 10000,
    odom_motion_threshold_m: float = 0.03,
    scan_obstacle_threshold_m: float = 0.8,
    now: datetime | None = None,
) -> TurtleBot3TelemetryWindow:
    source_path = Path(path)
    samples = load_turtlebot3_telemetry_sidecar_jsonl(source_path)
    try:
        digest = sha256(source_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise TurtleBot3TelemetrySidecarError(
            f"turtlebot3 telemetry sidecar JSONL not readable: {source_path}"
        ) from exc
    return build_turtlebot3_telemetry_window(
        samples=samples,
        source_jsonl_path=source_path,
        source_jsonl_sha256=digest,
        max_duration_seconds=max_duration_seconds,
        max_sample_count=max_sample_count,
        odom_motion_threshold_m=odom_motion_threshold_m,
        scan_obstacle_threshold_m=scan_obstacle_threshold_m,
        now=now,
    )


def build_turtlebot3_state_correlation(
    *,
    telemetry_window: TurtleBot3TelemetryWindow | Mapping[str, Any],
    bridge_motion: Mapping[str, Any],
    now: datetime | None = None,
) -> TurtleBot3StateCorrelation:
    window = (
        telemetry_window
        if isinstance(telemetry_window, TurtleBot3TelemetryWindow)
        else TurtleBot3TelemetryWindow.model_validate(dict(telemetry_window))
    )
    bridge_motion_observed = bridge_motion.get("robot_motion_observed") is True
    bridge_odom_delta_m = _as_float(bridge_motion.get("odom_delta_m"))
    sidecar_odom_delta_m = window.odom_delta_m
    odom_delta_difference_m = (
        abs(bridge_odom_delta_m - sidecar_odom_delta_m)
        if bridge_odom_delta_m is not None and sidecar_odom_delta_m is not None
        else None
    )
    blocked_reasons: list[str] = []
    if window.window_status != "ready":
        blocked_reasons.append("telemetry_window_not_ready")
        blocked_reasons.extend(window.blocked_reasons)
    if not bridge_motion_observed:
        blocked_reasons.append("bridge_motion_not_observed")
    if not window.odom_motion_observed:
        blocked_reasons.append("sidecar_motion_not_observed")
    motion_confirmed = not blocked_reasons
    payload = {
        "telemetry_window_ref": f"turtlebot3_telemetry_window:{window.window_id}",
        "raw_logs_ref": window.raw_logs_ref,
        "bridge_motion_observed": bridge_motion_observed,
        "bridge_odom_delta_m": bridge_odom_delta_m,
        "sidecar_motion_observed": window.odom_motion_observed,
        "sidecar_odom_delta_m": sidecar_odom_delta_m,
    }
    return TurtleBot3StateCorrelation(
        correlation_id=_stable_id("turtlebot3_state_correlation", payload),
        telemetry_window_ref=f"turtlebot3_telemetry_window:{window.window_id}",
        raw_logs_ref=window.raw_logs_ref,
        bridge_motion_observed=bridge_motion_observed,
        bridge_odom_delta_m=bridge_odom_delta_m,
        sidecar_motion_observed=window.odom_motion_observed,
        sidecar_odom_delta_m=sidecar_odom_delta_m,
        odom_delta_difference_m=odom_delta_difference_m,
        motion_correlation_confirmed=motion_confirmed,
        correlation_status="ready" if motion_confirmed else "blocked",
        blocked_reasons=tuple(blocked_reasons),
        observed_at=_utc(now),
    )


__all__ = [
    "TURTLEBOT3_STATE_CORRELATION_SCHEMA_VERSION",
    "TURTLEBOT3_TELEMETRY_SAMPLE_SCHEMA_VERSION",
    "TURTLEBOT3_TELEMETRY_SIDECAR_JSONL_ENV",
    "TURTLEBOT3_TELEMETRY_WINDOW_SCHEMA_VERSION",
    "TurtleBot3StateCorrelation",
    "TurtleBot3TelemetrySidecarError",
    "TurtleBot3TelemetryWindow",
    "build_turtlebot3_state_correlation",
    "build_turtlebot3_telemetry_window",
    "build_turtlebot3_telemetry_window_from_jsonl",
    "load_turtlebot3_telemetry_sidecar_jsonl",
]
