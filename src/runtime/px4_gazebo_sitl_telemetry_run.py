"""PX4 + Gazebo SITL telemetry-only run artifacts.

This module is intentionally backed by actual PX4/Gazebo SITL process evidence:
PX4 startup logs, Gazebo startup/world logs, Gazebo vehicle pose samples, and
MAVLink HEARTBEAT frames observed from PX4. It does not accept the older
PX4/Gazebo-compatible fake log source as backing evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.px4_gazebo_telemetry import (
    build_px4_gazebo_hil_review_gate_smoke,
    sanitize_px4_gazebo_telemetry_sample,
)
from src.runtime.task_store import TaskStore, get_task_store

PX4_GAZEBO_SITL_TELEMETRY_RUN_SCHEMA_VERSION = "px4_gazebo_sitl_telemetry_run.v1"
PX4_GAZEBO_SITL_TELEMETRY_SAMPLE_SCHEMA_VERSION = "px4_gazebo_sitl_telemetry_sample.v1"

PX4_GAZEBO_SITL_SOURCE_KIND = "actual_px4_gazebo_sitl_telemetry_only_stack"
PX4_GAZEBO_COMPATIBLE_FAKE_SOURCE_KIND = "px4_gazebo_compatible_log_source"

_PX4_STARTUP_MARKERS = (
    "Gazebo world is ready",
    "Startup script returned successfully",
)
_GAZEBO_STARTUP_MARKERS = (
    "Gazebo world is ready",
    "gz_bridge] world: default, model: x500_0",
)
_COMMAND_LIKE_LOG_FIELD_RE = re.compile(
    r"(?i)(?:[\"']|\b)"
    r"(action|actuator|command|dispatch|entity[_-]?mutation|gazebo[_-]?command|"
    r"mavlink[_-]?command|mission[_-]?upload|physical[_-]?execution[_-]?invoked|"
    r"ros[_-]?action|ros[_-]?topic|setpoint|thrust|torque|velocity[_-]?command)"
    r"(?:[\"']|\b)\s*[:=]"
)


class PX4GazeboSITLTelemetryRunError(RuntimeError):
    """Raised when actual PX4 + Gazebo SITL telemetry evidence is unsafe."""


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _line_count(text: str) -> int:
    return len([line for line in text.splitlines() if line.strip()])


def _command_like_fields(*texts: str) -> tuple[str, ...]:
    findings: set[str] = set()
    for text in texts:
        findings.update(
            match.group(1) for match in _COMMAND_LIKE_LOG_FIELD_RE.finditer(text)
        )
    return tuple(sorted(findings))


def _missing(text: str, markers: Sequence[str]) -> tuple[str, ...]:
    return tuple(marker for marker in markers if marker not in text)


def _coerce_string_tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in value or ())


class PX4GazeboSITLTelemetrySample(BaseModel):
    """One telemetry sample from the actual PX4 + Gazebo SITL stack."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_SITL_TELEMETRY_SAMPLE_SCHEMA_VERSION] = (
        PX4_GAZEBO_SITL_TELEMETRY_SAMPLE_SCHEMA_VERSION
    )
    sample_id: str
    captured_at: datetime
    sequence: int
    position_x_m: float
    position_y_m: float
    position_z_m: float
    battery_remaining_pct: float
    flight_mode: str
    mission_state: str
    gps_fix: bool
    ekf_status: str
    link_quality: str
    mavlink_heartbeat_count_seen: int
    vehicle_spawn_marker_observed: Literal[True] = True
    vehicle_takeoff_observed: Literal[False] = False
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True

    @field_validator("captured_at", mode="before")
    @classmethod
    def _coerce_captured_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_grounded(self) -> "PX4GazeboSITLTelemetrySample":
        if self.position_z_m > 0.5:
            raise PX4GazeboSITLTelemetryRunError(
                "telemetry-only SITL run must not observe vehicle takeoff"
            )
        if self.mavlink_heartbeat_count_seen <= 0:
            raise PX4GazeboSITLTelemetryRunError(
                "SITL telemetry sample requires observed PX4 HEARTBEAT"
            )
        return self


class PX4GazeboSITLTelemetryRun(BaseModel):
    """Actual PX4 + Gazebo SITL telemetry-only run artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_SITL_TELEMETRY_RUN_SCHEMA_VERSION] = (
        PX4_GAZEBO_SITL_TELEMETRY_RUN_SCHEMA_VERSION
    )
    run_id: str
    run_status: Literal["completed"] = "completed"
    run_scope: Literal["actual_px4_gazebo_sitl_telemetry_only"] = (
        "actual_px4_gazebo_sitl_telemetry_only"
    )
    source_kind: Literal[PX4_GAZEBO_SITL_SOURCE_KIND] = PX4_GAZEBO_SITL_SOURCE_KIND
    source_id: str
    started_at: datetime
    finished_at: datetime
    max_duration_seconds: float
    min_sample_window_seconds: float
    px4_log_line_count: int
    gazebo_pose_sample_count: int
    mavlink_frame_count: int
    mavlink_heartbeat_count: int
    mavlink_observation_window_seconds: float
    samples: tuple[PX4GazeboSITLTelemetrySample, ...]
    sample_refs: tuple[str, ...]
    telemetry_refs: tuple[str, ...]
    hil_review_ref: str
    gate_ref: str
    px4_image_ref: str
    gazebo_image_ref: str
    px4_model: str
    gazebo_world: str
    gazebo_vehicle_ref: Literal["gazebo_vehicle:x500_0"] = "gazebo_vehicle:x500_0"
    px4_startup_marker_observed: Literal[True] = True
    gazebo_startup_marker_observed: Literal[True] = True
    vehicle_spawn_marker_observed: Literal[True] = True
    px4_gazebo_sitl_started: Literal[True] = True
    telemetry_collected: Literal[True] = True
    mavlink_heartbeat_observed: Literal[True] = True
    hil_evidence_created: Literal[True] = True
    gate_created: Literal[True] = True
    vehicle_takeoff_observed: Literal[False] = False
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    external_dispatch_performed: Literal[False] = False
    mavlink_command_sent: Literal[False] = False
    mavlink_dispatch_performed: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    px4_mission_upload_performed: Literal[False] = False
    gazebo_simulator_command_performed: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("started_at", "finished_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("samples", mode="before")
    @classmethod
    def _coerce_samples(cls, value: Any) -> tuple[PX4GazeboSITLTelemetrySample, ...]:
        return tuple(
            (
                item
                if isinstance(item, PX4GazeboSITLTelemetrySample)
                else PX4GazeboSITLTelemetrySample.model_validate(item)
            )
            for item in value
        )

    @field_validator("sample_refs", "telemetry_refs", mode="before")
    @classmethod
    def _coerce_refs(cls, value: Any) -> tuple[str, ...]:
        return _coerce_string_tuple(value)

    @model_validator(mode="after")
    def _validate_run(self) -> "PX4GazeboSITLTelemetryRun":
        if self.source_id == "px4-gazebo-compatible-log-source":
            raise PX4GazeboSITLTelemetryRunError(
                "actual SITL telemetry run refuses compatible fake log source"
            )
        if self.finished_at <= self.started_at:
            raise PX4GazeboSITLTelemetryRunError(
                "SITL telemetry run requires a positive observation window"
            )
        if (
            self.finished_at - self.started_at
        ).total_seconds() > self.max_duration_seconds:
            raise PX4GazeboSITLTelemetryRunError(
                "SITL telemetry run exceeded duration bound"
            )
        if self.mavlink_observation_window_seconds < self.min_sample_window_seconds:
            raise PX4GazeboSITLTelemetryRunError(
                "SITL telemetry run requires at least five seconds of MAVLink observation"
            )
        if self.mavlink_heartbeat_count <= 0:
            raise PX4GazeboSITLTelemetryRunError(
                "SITL telemetry run requires observed PX4 HEARTBEAT"
            )
        if self.gazebo_pose_sample_count < 2 or len(self.samples) < 2:
            raise PX4GazeboSITLTelemetryRunError(
                "SITL telemetry run requires multiple Gazebo pose samples"
            )
        if len(self.sample_refs) != len(self.samples):
            raise PX4GazeboSITLTelemetryRunError("sample refs must match samples")
        if not self.telemetry_refs:
            raise PX4GazeboSITLTelemetryRunError(
                "SITL telemetry run requires telemetry refs"
            )
        if not self.hil_review_ref.startswith("hil_telemetry_review:"):
            raise PX4GazeboSITLTelemetryRunError(
                "SITL telemetry run requires HIL review ref"
            )
        if not self.gate_ref.startswith("autonomy_gate_result:"):
            raise PX4GazeboSITLTelemetryRunError("SITL telemetry run requires gate ref")
        return self


def _validate_actual_logs(*, log_text: str, timed_out: bool) -> int:
    if timed_out:
        raise PX4GazeboSITLTelemetryRunError(
            "actual PX4 + Gazebo SITL telemetry run timed out before persistence"
        )
    if not log_text.strip():
        raise PX4GazeboSITLTelemetryRunError(
            "actual PX4 + Gazebo SITL produced no output"
        )
    command_like = _command_like_fields(log_text)
    if command_like:
        raise PX4GazeboSITLTelemetryRunError(
            "actual PX4 + Gazebo SITL logs contained command-like payload: "
            + ", ".join(command_like)
        )
    missing_px4 = _missing(log_text, _PX4_STARTUP_MARKERS)
    if missing_px4:
        raise PX4GazeboSITLTelemetryRunError(
            "actual PX4 startup markers are incomplete: " + ", ".join(missing_px4)
        )
    missing_gazebo = _missing(log_text, _GAZEBO_STARTUP_MARKERS)
    if missing_gazebo:
        raise PX4GazeboSITLTelemetryRunError(
            "actual Gazebo startup markers are incomplete: " + ", ".join(missing_gazebo)
        )
    return _line_count(log_text)


def build_px4_gazebo_sitl_telemetry_samples(
    *,
    pose_samples: Sequence[Mapping[str, Any]],
    captured_at_start: datetime,
    sample_interval_seconds: float,
    mavlink_heartbeat_count: int,
) -> tuple[PX4GazeboSITLTelemetrySample, ...]:
    if len(pose_samples) < 2:
        raise PX4GazeboSITLTelemetryRunError(
            "actual PX4 + Gazebo SITL requires multiple pose samples"
        )
    if sample_interval_seconds <= 0:
        raise PX4GazeboSITLTelemetryRunError("sample interval must be positive")
    captured_start = _utc(captured_at_start)
    samples: list[PX4GazeboSITLTelemetrySample] = []
    for sequence, pose in enumerate(pose_samples):
        captured_at = datetime.fromtimestamp(
            captured_start.timestamp() + sequence * sample_interval_seconds,
            tz=timezone.utc,
        )
        payload = {
            "sequence": sequence,
            "captured_at": captured_at.isoformat(),
            "pose": {
                "x": float(pose.get("x", 0.0)),
                "y": float(pose.get("y", 0.0)),
                "z": float(pose.get("z", 0.0)),
            },
            "mavlink_heartbeat_count": mavlink_heartbeat_count,
        }
        samples.append(
            PX4GazeboSITLTelemetrySample(
                sample_id=_stable_id("px4_gazebo_sitl_sample", payload),
                captured_at=captured_at,
                sequence=sequence,
                position_x_m=float(pose.get("x", 0.0)),
                position_y_m=float(pose.get("y", 0.0)),
                position_z_m=float(pose.get("z", 0.0)),
                battery_remaining_pct=float(pose.get("battery_remaining_pct", 96.0)),
                flight_mode=str(pose.get("flight_mode", "standby")),
                mission_state=str(pose.get("mission_state", "idle")),
                gps_fix=bool(pose.get("gps_fix", True)),
                ekf_status=str(pose.get("ekf_status", "nominal")),
                link_quality=str(pose.get("link_quality", "nominal")),
                mavlink_heartbeat_count_seen=mavlink_heartbeat_count,
            )
        )
    return tuple(samples)


def _latest_sample_payload(
    *,
    latest: PX4GazeboSITLTelemetrySample,
    source_id: str,
    log_line_count: int,
    pose_sample_count: int,
    mavlink_frame_count: int,
    mavlink_heartbeat_count: int,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "sample_id": latest.sample_id,
        "source": {
            "source_kind": PX4_GAZEBO_SITL_SOURCE_KIND,
            "source_id": source_id,
            "vehicle_id": "x500_0",
        },
        "captured_at": latest.captured_at.isoformat(),
        "telemetry": {
            "position_x_m": latest.position_x_m,
            "position_y_m": latest.position_y_m,
            "position_z_m": latest.position_z_m,
            "battery_remaining_pct": latest.battery_remaining_pct,
            "flight_mode": latest.flight_mode,
            "mission_state": latest.mission_state,
            "gps_fix": latest.gps_fix,
            "ekf_status": latest.ekf_status,
            "link_quality": latest.link_quality,
            "px4_gazebo_sitl_started": True,
            "mavlink_heartbeat_count": mavlink_heartbeat_count,
            "mavlink_frame_count": mavlink_frame_count,
            "gazebo_pose_sample_count": pose_sample_count,
            "vehicle_spawn_marker_observed": True,
            "vehicle_takeoff_observed": False,
            "log_line_count": log_line_count,
        },
        "metadata": {
            "telemetry_only": True,
            "read_only": True,
            "source": "px4_gazebo_sitl_telemetry_run",
            **dict(metadata),
        },
    }


def build_px4_gazebo_sitl_telemetry_run(
    *,
    log_text: str,
    pose_samples: Sequence[Mapping[str, Any]],
    mavlink_frame_count: int,
    mavlink_heartbeat_count: int,
    mavlink_observation_window_seconds: float,
    started_at: datetime,
    finished_at: datetime,
    max_duration_seconds: float,
    min_sample_window_seconds: float = 5.0,
    sample_interval_seconds: float = 1.0,
    source_id: str = "actual-px4-gazebo-sitl-stack",
    px4_image_ref: str = "px4io/px4-sitl-gazebo:latest",
    gazebo_image_ref: str = "px4io/px4-sitl-gazebo:latest",
    px4_model: str = "gz_x500",
    gazebo_world: str = "default",
    metadata: Mapping[str, Any] | None = None,
    timed_out: bool = False,
) -> tuple[PX4GazeboSITLTelemetryRun, dict[str, Any]]:
    if source_id == "px4-gazebo-compatible-log-source":
        raise PX4GazeboSITLTelemetryRunError(
            "actual SITL telemetry run refuses compatible fake log source"
        )
    started = _utc(started_at)
    finished = _utc(finished_at)
    log_line_count = _validate_actual_logs(log_text=log_text, timed_out=timed_out)
    if mavlink_observation_window_seconds < min_sample_window_seconds:
        raise PX4GazeboSITLTelemetryRunError(
            "actual PX4 + Gazebo SITL requires five seconds of HEARTBEAT observation"
        )
    if mavlink_heartbeat_count <= 0:
        raise PX4GazeboSITLTelemetryRunError(
            "actual PX4 + Gazebo SITL requires observed HEARTBEAT frames"
        )
    samples = build_px4_gazebo_sitl_telemetry_samples(
        pose_samples=pose_samples,
        captured_at_start=started,
        sample_interval_seconds=sample_interval_seconds,
        mavlink_heartbeat_count=mavlink_heartbeat_count,
    )
    sanitized = sanitize_px4_gazebo_telemetry_sample(
        _latest_sample_payload(
            latest=samples[-1],
            source_id=source_id,
            log_line_count=log_line_count,
            pose_sample_count=len(samples),
            mavlink_frame_count=mavlink_frame_count,
            mavlink_heartbeat_count=mavlink_heartbeat_count,
            metadata=metadata or {},
        )
    )
    artifacts = build_px4_gazebo_hil_review_gate_smoke(
        sanitized,
        now=sanitized.captured_at,
    )
    payload = {
        "source_id": source_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "sample_refs": [sample.sample_id for sample in samples],
        "telemetry_id": sanitized.telemetry_id,
        "gate_id": artifacts["autonomy_gate_result"]["gate_id"],
    }
    run = PX4GazeboSITLTelemetryRun(
        run_id=_stable_id("px4_gazebo_sitl_telemetry_run", payload),
        source_id=source_id,
        started_at=started,
        finished_at=finished,
        max_duration_seconds=max_duration_seconds,
        min_sample_window_seconds=min_sample_window_seconds,
        px4_log_line_count=log_line_count,
        gazebo_pose_sample_count=len(samples),
        mavlink_frame_count=mavlink_frame_count,
        mavlink_heartbeat_count=mavlink_heartbeat_count,
        mavlink_observation_window_seconds=mavlink_observation_window_seconds,
        samples=samples,
        sample_refs=tuple(sample.sample_id for sample in samples),
        telemetry_refs=(f"px4_gazebo_sanitized_telemetry:{sanitized.telemetry_id}",),
        hil_review_ref=f"hil_telemetry_review:{artifacts['hil_telemetry_review']['review_id']}",
        gate_ref=f"autonomy_gate_result:{artifacts['autonomy_gate_result']['gate_id']}",
        px4_image_ref=px4_image_ref,
        gazebo_image_ref=gazebo_image_ref,
        px4_model=px4_model,
        gazebo_world=gazebo_world,
        metadata=dict(metadata or {}),
    )
    return run, artifacts


def attach_px4_gazebo_sitl_telemetry_run_artifacts(
    *,
    task_id: str,
    log_text: str,
    pose_samples: Sequence[Mapping[str, Any]],
    mavlink_frame_count: int,
    mavlink_heartbeat_count: int,
    mavlink_observation_window_seconds: float,
    started_at: datetime,
    finished_at: datetime,
    max_duration_seconds: float,
    min_sample_window_seconds: float = 5.0,
    sample_interval_seconds: float = 1.0,
    source_id: str = "actual-px4-gazebo-sitl-stack",
    px4_image_ref: str = "px4io/px4-sitl-gazebo:latest",
    gazebo_image_ref: str = "px4io/px4-sitl-gazebo:latest",
    px4_model: str = "gz_x500",
    gazebo_world: str = "default",
    metadata: Mapping[str, Any] | None = None,
    timed_out: bool = False,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach actual SITL telemetry-only artifacts after validation."""

    run, artifacts = build_px4_gazebo_sitl_telemetry_run(
        log_text=log_text,
        pose_samples=pose_samples,
        mavlink_frame_count=mavlink_frame_count,
        mavlink_heartbeat_count=mavlink_heartbeat_count,
        mavlink_observation_window_seconds=mavlink_observation_window_seconds,
        started_at=started_at,
        finished_at=finished_at,
        max_duration_seconds=max_duration_seconds,
        min_sample_window_seconds=min_sample_window_seconds,
        sample_interval_seconds=sample_interval_seconds,
        source_id=source_id,
        px4_image_ref=px4_image_ref,
        gazebo_image_ref=gazebo_image_ref,
        px4_model=px4_model,
        gazebo_world=gazebo_world,
        metadata=metadata,
        timed_out=timed_out,
    )
    store = (task_store_factory or get_task_store)()
    if store.get(task_id) is None:
        raise PX4GazeboSITLTelemetryRunError(
            f"task {task_id} not found; cannot attach actual SITL telemetry run"
        )
    updated = store.update(
        task_id,
        artifacts={
            **artifacts,
            "px4_gazebo_sitl_telemetry_run": run.model_dump(mode="json"),
        },
    )
    if updated is None:
        raise PX4GazeboSITLTelemetryRunError(
            f"task {task_id} disappeared while attaching actual SITL telemetry run"
        )
    return {
        **artifacts,
        "px4_gazebo_sitl_telemetry_run": run.model_dump(mode="json"),
        "task": updated,
    }


__all__ = [
    "PX4_GAZEBO_SITL_SOURCE_KIND",
    "PX4_GAZEBO_SITL_TELEMETRY_RUN_SCHEMA_VERSION",
    "PX4_GAZEBO_SITL_TELEMETRY_SAMPLE_SCHEMA_VERSION",
    "PX4GazeboSITLTelemetryRun",
    "PX4GazeboSITLTelemetryRunError",
    "PX4GazeboSITLTelemetrySample",
    "attach_px4_gazebo_sitl_telemetry_run_artifacts",
    "build_px4_gazebo_sitl_telemetry_run",
    "build_px4_gazebo_sitl_telemetry_samples",
]
