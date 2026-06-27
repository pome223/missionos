"""Collector for actual Gazebo Sim (`gz sim`) stdout logs."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.task_store import get_task_store
from src.runtime.px4_gazebo_telemetry import (
    Px4GazeboSanitizedTelemetry,
    Px4GazeboTelemetryRejected,
    attach_px4_gazebo_hil_review_gate_artifacts,
    sanitize_px4_gazebo_telemetry_sample,
)


class GzSimLogCollectorError(RuntimeError):
    """Raised when Gazebo Sim logs cannot become telemetry evidence."""


GZ_SIM_TELEMETRY_DIAGNOSTICS_SCHEMA_VERSION = "gz_sim_telemetry_diagnostics.v1"
GAZEBO_DELIVERY_OBSERVATION_DIAGNOSTICS_SCHEMA_VERSION = (
    "gazebo_delivery_observation_diagnostics.v1"
)
GzSimTelemetryFailureReason = Literal[
    "no_output",
    "missing_server_marker",
    "missing_world_marker",
    "startup_only",
    "world_load_failure",
    "delivery_world_mismatch",
    "command_like_payload",
]
GazeboDeliveryObservationFailureReason = Literal[
    "no_output",
    "world_mismatch",
    "no_pose_topic_output",
    "missing_delivery_entity",
    "entity_no_motion",
    "incomplete_phase_sequence",
    "command_like_payload",
    "collector_timeout",
    "container_exited_early",
    "route_geofence_violation",
]

REQUIRED_GZ_SIM_MARKERS = (
    "Gazebo Sim Server v",
    "Loading SDF world file",
    "Loaded level [default]",
)
GZ_SIM_DELIVERY_WORLD_NAME = "delivery_minimal"
GZ_SIM_DELIVERY_WORLD_SDF_PATH = "/worlds/delivery_minimal.sdf"
GZ_SIM_DELIVERY_WORLD_REF = "simulators/gazebo/worlds/delivery_minimal.sdf"
GZ_SIM_DELIVERY_PROGRESS_PREFIX = "BOILED_CLAW_DELIVERY_PROGRESS "
GZ_SIM_DELIVERY_STATE_WORLD_NAME = "delivery_state_driven"
GZ_SIM_DELIVERY_STATE_WORLD_SDF_PATH = "/worlds/delivery_state_driven.sdf"
GZ_SIM_DELIVERY_STATE_WORLD_REF = (
    "simulators/gazebo/worlds/delivery_state_driven.sdf"
)
GZ_SIM_DELIVERY_STATE_ENTITY_NAME = "delivery_vehicle_state"
GZ_SIM_DELIVERY_ROUTE_START_X_M = -10.0
GZ_SIM_DELIVERY_ROUTE_END_X_M = 25.0
GZ_SIM_DELIVERY_ROUTE_LENGTH_M = (
    GZ_SIM_DELIVERY_ROUTE_END_X_M - GZ_SIM_DELIVERY_ROUTE_START_X_M
)

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# This is intentionally key-shaped rather than word-shaped: regular Gazebo log
# prose may mention services or actions, but telemetry evidence must not carry
# structured command-like fields such as `"RosTopic": ...` or `setpoint=...`.
_COMMAND_LIKE_LOG_FIELD_RE = re.compile(
    r"(?i)(?:[\"']|\b)"
    r"(action|actuator|command|dispatch|entity[_-]?mutation|gazebo[_-]?command|"
    r"gazebo[_-]?entity[_-]?mutation|gazebo[_-]?mutation|mavlink[_-]?command|mission[_-]?upload|"
    r"physical[_-]?execution[_-]?invoked|ros[_-]?action|ros[_-]?topic|"
    r"setpoint|thrust|torque|velocity[_-]?command)"
    r"(?:[\"']|\b)\s*[:=]"
)


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _meaningful_lines(log_text: str) -> list[str]:
    clean = _strip_ansi(log_text)
    return [line.strip() for line in clean.splitlines() if line.strip()]


def _command_like_log_fields(log_text: str) -> list[str]:
    clean = _strip_ansi(log_text)
    return sorted(set(_COMMAND_LIKE_LOG_FIELD_RE.findall(clean)))


def _missing_markers(log_text: str) -> list[str]:
    clean = _strip_ansi(log_text)
    return [marker for marker in REQUIRED_GZ_SIM_MARKERS if marker not in clean]


def _has_delivery_world_marker(log_text: str) -> bool:
    clean = _strip_ansi(log_text)
    return (
        GZ_SIM_DELIVERY_WORLD_SDF_PATH in clean
        or f"{GZ_SIM_DELIVERY_WORLD_NAME}.sdf" in clean
    )


def _has_delivery_state_world_marker(log_text: str) -> bool:
    clean = _strip_ansi(log_text)
    return (
        GZ_SIM_DELIVERY_STATE_WORLD_SDF_PATH in clean
        or f"{GZ_SIM_DELIVERY_STATE_WORLD_NAME}.sdf" in clean
    )


def _classify_gz_sim_log_failure(log_text: str) -> GzSimTelemetryFailureReason:
    clean = _strip_ansi(log_text)
    if not clean.strip():
        return "no_output"
    if _command_like_log_fields(clean):
        return "command_like_payload"
    if "Unable to find or load SDF world file" in clean:
        return "world_load_failure"
    missing = _missing_markers(clean)
    if "Gazebo Sim Server v" in missing:
        return "missing_server_marker"
    if "Gazebo Sim Server v" in clean and len(missing) == 2:
        return "startup_only"
    return "missing_world_marker"


def _safe_log_excerpt(log_text: str, *, command_like_fields: list[str]) -> str:
    if command_like_fields:
        return "[redacted command-like stdout payload]"
    lines = _meaningful_lines(log_text)
    if not lines:
        return ""
    return lines[-1][-300:]


def _validate_gz_sim_started(log_text: str) -> None:
    clean = _strip_ansi(log_text)
    if not clean.strip():
        raise GzSimLogCollectorError("Gazebo Sim log output is empty")
    command_like = _command_like_log_fields(clean)
    if command_like:
        raise GzSimLogCollectorError(
            "Gazebo Sim stdout logs contain command-like fields: "
            + ", ".join(command_like)
        )
    missing = [marker for marker in REQUIRED_GZ_SIM_MARKERS if marker not in clean]
    if missing:
        raise GzSimLogCollectorError(
            "Gazebo Sim startup logs are incomplete: " + ", ".join(missing)
        )


def _validate_gz_sim_delivery_world_started(log_text: str) -> None:
    _validate_gz_sim_started(log_text)
    clean = _strip_ansi(log_text)
    if not _has_delivery_world_marker(clean):
        raise GzSimLogCollectorError(
            "Gazebo Sim delivery-world logs are incomplete: "
            f"{GZ_SIM_DELIVERY_WORLD_SDF_PATH}"
        )


def _validate_gz_sim_delivery_state_world_started(log_text: str) -> None:
    clean = _strip_ansi(log_text)
    if not clean.strip():
        raise GzSimLogCollectorError("Gazebo Sim log output is empty")
    command_like = _command_like_log_fields(clean)
    if command_like:
        raise GzSimLogCollectorError(
            "Gazebo Sim stdout logs contain command-like fields: "
            + ", ".join(command_like)
        )
    for marker in ("Gazebo Sim Server v", "Loading SDF world file"):
        if marker not in clean:
            raise GzSimLogCollectorError(
                "Gazebo Sim delivery-state world startup logs are incomplete: "
                + marker
            )
    if (
        f"/world/{GZ_SIM_DELIVERY_STATE_WORLD_NAME}/state" not in clean
        and f"/world/{GZ_SIM_DELIVERY_STATE_WORLD_NAME}/scene/info" not in clean
    ):
        raise GzSimLogCollectorError(
            "Gazebo Sim delivery-state world startup logs are incomplete: "
            f"/world/{GZ_SIM_DELIVERY_STATE_WORLD_NAME}/state"
        )
    if not _has_delivery_state_world_marker(clean):
        raise GzSimLogCollectorError(
            "Gazebo Sim delivery-state world logs are incomplete: "
            f"{GZ_SIM_DELIVERY_STATE_WORLD_SDF_PATH}"
        )


def _pose_blocks(pose_text: str) -> list[str]:
    blocks: list[str] = []
    lines = _strip_ansi(pose_text).splitlines()
    current: list[str] = []
    depth = 0
    in_pose = False
    for line in lines:
        stripped = line.strip()
        if not in_pose and stripped == "pose {":
            in_pose = True
            current = [line]
            depth = 1
            continue
        if not in_pose:
            continue
        current.append(line)
        depth += stripped.count("{") - stripped.count("}")
        if depth <= 0:
            blocks.append("\n".join(current))
            current = []
            in_pose = False
    return blocks


def parse_gz_sim_entity_pose(
    pose_text: str,
    *,
    entity_name: str = GZ_SIM_DELIVERY_STATE_ENTITY_NAME,
) -> dict[str, float | str]:
    """Parse one `gz topic -e .../pose/info` text sample for an entity pose."""

    command_like = _command_like_log_fields(pose_text)
    if command_like:
        raise GzSimLogCollectorError(
            "Gazebo Sim pose text contains command-like fields: "
            + ", ".join(command_like)
        )
    for block in _pose_blocks(pose_text):
        name_match = re.search(r'name:\s*"([^"]+)"', block)
        if not name_match or name_match.group(1) != entity_name:
            continue
        position_match = re.search(r"position\s*\{(?P<body>.*?)\n\s*\}", block, re.S)
        if not position_match:
            raise GzSimLogCollectorError(
                f"entity pose missing position: {entity_name}"
            )
        body = position_match.group("body")

        def _component(key: str) -> float:
            match = re.search(rf"\b{key}:\s*([-+0-9.eE]+)", body)
            return float(match.group(1)) if match else 0.0

        return {
            "entity_name": entity_name,
            "x": _component("x"),
            "y": _component("y"),
            "z": _component("z"),
        }
    raise GzSimLogCollectorError(f"entity pose not observed: {entity_name}")


def _entity_motion_observed(poses: list[dict[str, float | str]]) -> bool:
    if len(poses) < 2:
        return False
    first = poses[0]
    last = poses[-1]
    return abs(float(last["x"]) - float(first["x"])) >= 0.25


def _delivery_phase_from_pose(x: float) -> str:
    if x >= GZ_SIM_DELIVERY_ROUTE_END_X_M:
        return "completed"
    if x >= 24.0:
        return "dropoff"
    if x >= 5.0:
        return "enroute"
    return "pickup"


def _delivery_progress_from_pose(x: float) -> tuple[str, bool, bool, float]:
    route_progress = max(
        0.0,
        min(
            100.0,
            (
                (x - GZ_SIM_DELIVERY_ROUTE_START_X_M)
                / GZ_SIM_DELIVERY_ROUTE_LENGTH_M
            )
            * 100.0,
        ),
    )
    pickup_reached = x >= -0.5
    dropoff_reached = x >= GZ_SIM_DELIVERY_ROUTE_END_X_M
    phase = _delivery_phase_from_pose(x)
    return phase, pickup_reached, dropoff_reached, route_progress


def delivery_phases_from_entity_poses(
    poses: list[dict[str, float | str]],
) -> list[str]:
    """Return canonical delivery phases observed from entity poses."""

    xs = [float(pose["x"]) for pose in poses]
    phases: list[str] = []
    if any(x < 5.0 for x in xs):
        phases.append("pickup")
    if any(5.0 <= x < 24.0 for x in xs):
        phases.append("enroute")
    if any(x >= 24.0 for x in xs):
        phases.append("dropoff")
    if xs and xs[-1] >= GZ_SIM_DELIVERY_ROUTE_END_X_M:
        phases.append("completed")
    return phases


def _delivery_progress_payloads(log_text: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for line in _meaningful_lines(log_text):
        if GZ_SIM_DELIVERY_PROGRESS_PREFIX not in line:
            continue
        _, payload_text = line.split(GZ_SIM_DELIVERY_PROGRESS_PREFIX, 1)
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise GzSimLogCollectorError(
                f"delivery_progress_malformed: {exc.msg}"
            ) from exc
        if not isinstance(payload, dict):
            raise GzSimLogCollectorError("delivery_progress_malformed: object required")
        command_like = _command_like_log_fields(payload_text)
        if command_like:
            raise GzSimLogCollectorError(
                "delivery_progress_command_like_payload: "
                + ", ".join(command_like)
            )
        payloads.append(payload)
    return payloads


def _latest_delivery_progress_payload(log_text: str) -> dict[str, Any]:
    payloads = _delivery_progress_payloads(log_text)
    if not payloads:
        raise GzSimLogCollectorError("delivery_progress_missing")
    return payloads[-1]


def _bool_progress(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "reached"}
    return bool(value) if isinstance(value, int | float) else False


def _float_progress(payload: dict[str, Any], key: str, *, default: float) -> float:
    value = payload.get(key, default)
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text_progress(payload: dict[str, Any], key: str, *, default: str) -> str:
    value = payload.get(key, default)
    return str(value).strip() or default


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


class GzSimTelemetryDiagnostics(BaseModel):
    """Debug artifact for rejected Gazebo Sim stdout evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[GZ_SIM_TELEMETRY_DIAGNOSTICS_SCHEMA_VERSION] = (
        GZ_SIM_TELEMETRY_DIAGNOSTICS_SCHEMA_VERSION
    )
    diagnostics_id: str
    status: Literal["invalid_evidence"] = "invalid_evidence"
    reason: GzSimTelemetryFailureReason
    error_message: str
    missing_markers: list[str] = Field(default_factory=list)
    command_like_fields: list[str] = Field(default_factory=list)
    log_line_count: int = 0
    log_digest: str
    safe_log_excerpt: str = ""
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hil_artifacts_persisted: Literal[False] = False
    gate_artifacts_persisted: Literal[False] = False
    approval_promotion_reuse_created: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class GazeboDeliveryObservationDiagnostics(BaseModel):
    """Debug-only artifact for rejected Gazebo delivery observations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[GAZEBO_DELIVERY_OBSERVATION_DIAGNOSTICS_SCHEMA_VERSION] = (
        GAZEBO_DELIVERY_OBSERVATION_DIAGNOSTICS_SCHEMA_VERSION
    )
    diagnostics_id: str
    status: Literal["invalid_evidence"] = "invalid_evidence"
    reason: GazeboDeliveryObservationFailureReason
    error_message: str
    observed_entity_name: str = GZ_SIM_DELIVERY_STATE_ENTITY_NAME
    world_name: str = GZ_SIM_DELIVERY_STATE_WORLD_NAME
    pose_topic: str = f"/world/{GZ_SIM_DELIVERY_STATE_WORLD_NAME}/pose/info"
    command_like_fields: list[str] = Field(default_factory=list)
    missing_markers: list[str] = Field(default_factory=list)
    pose_sample_count: int = 0
    observed_delivery_phases: list[str] = Field(default_factory=list)
    log_digest: str
    safe_log_excerpt: str = ""
    safe_pose_excerpt: str = ""
    debug_only: Literal[True] = True
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    hil_artifacts_persisted: Literal[False] = False
    gate_artifacts_persisted: Literal[False] = False
    runner_artifacts_persisted: Literal[False] = False
    approval_promotion_reuse_created: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)


def build_gz_sim_telemetry_diagnostics(
    log_text: str,
    *,
    error_message: str | None = None,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
    reason_override: GzSimTelemetryFailureReason | None = None,
) -> GzSimTelemetryDiagnostics:
    """Build a non-HIL diagnostics artifact for rejected `gz sim` stdout logs."""

    captured = captured_at or datetime.now(timezone.utc)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    captured = captured.astimezone(timezone.utc)

    clean = _strip_ansi(log_text)
    command_like_fields = _command_like_log_fields(clean)
    missing = _missing_markers(clean)
    reason = reason_override or _classify_gz_sim_log_failure(clean)
    digest = sha256(clean.encode("utf-8")).hexdigest()[:16]
    payload = {
        "reason": reason,
        "digest": digest,
        "missing_markers": missing,
        "command_like_fields": command_like_fields,
    }
    return GzSimTelemetryDiagnostics(
        diagnostics_id=_stable_id("gz_sim_diagnostics", payload),
        reason=reason,
        error_message=error_message
        or f"Gazebo Sim stdout rejected as {reason}",
        missing_markers=missing,
        command_like_fields=command_like_fields,
        log_line_count=len(_meaningful_lines(clean)),
        log_digest=digest,
        safe_log_excerpt=_safe_log_excerpt(
            clean,
            command_like_fields=command_like_fields,
        ),
        metadata={
            "artifact_only": True,
            "debug_only": True,
            "telemetry_only": True,
            "read_only": True,
            "captured_at": captured.isoformat(),
            "source": "gz_sim_stdout_failure",
            "collector": "gz_sim_log_collector.v1",
            "rejected_evidence_type": "gz_sim_stdout_log",
            "failure_classification": "invalid_evidence",
            **(provenance or {}),
        },
    )


def _safe_pose_excerpt(
    pose_text_samples: list[str],
    *,
    command_like_fields: list[str],
) -> str:
    if command_like_fields:
        return "[redacted command-like pose payload]"
    for sample in reversed(pose_text_samples):
        lines = _meaningful_lines(sample)
        if lines:
            return lines[-1][-300:]
    return ""


def _classify_gazebo_delivery_observation_failure(
    log_text: str,
    pose_text_samples: list[str],
) -> tuple[GazeboDeliveryObservationFailureReason, list[str]]:
    clean = _strip_ansi(log_text)
    pose_text = "\n".join(pose_text_samples)
    command_like = _command_like_log_fields(clean) + _command_like_log_fields(pose_text)
    if command_like:
        return "command_like_payload", sorted(set(command_like))
    if not clean.strip():
        return "no_output", []
    if not _has_delivery_state_world_marker(clean):
        return "world_mismatch", []
    if not [sample for sample in pose_text_samples if sample.strip()]:
        return "no_pose_topic_output", []
    try:
        poses = [
            parse_gz_sim_entity_pose(
                sample,
                entity_name=GZ_SIM_DELIVERY_STATE_ENTITY_NAME,
            )
            for sample in pose_text_samples
            if sample.strip()
        ]
    except GzSimLogCollectorError as exc:
        message = str(exc)
        if "command-like" in message:
            return "command_like_payload", sorted(set(command_like))
        if "entity pose not observed" in message:
            return "missing_delivery_entity", []
        return "missing_delivery_entity", []
    if not _entity_motion_observed(poses):
        return "entity_no_motion", []
    phases = delivery_phases_from_entity_poses(poses)
    required_phases = ("pickup", "enroute", "dropoff", "completed")
    if any(phase not in phases for phase in required_phases):
        return "incomplete_phase_sequence", []
    if any(
        bool(sample.strip()) and "route_geofence_violation" in sample
        for sample in pose_text_samples
    ):
        return "route_geofence_violation", []
    return "incomplete_phase_sequence", []


def build_gazebo_delivery_observation_diagnostics(
    log_text: str,
    pose_text_samples: list[str],
    *,
    error_message: str | None = None,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
    reason_override: GazeboDeliveryObservationFailureReason | None = None,
) -> GazeboDeliveryObservationDiagnostics:
    """Build debug-only diagnostics for rejected Gazebo delivery observations."""

    captured = captured_at or datetime.now(timezone.utc)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    captured = captured.astimezone(timezone.utc)
    clean = _strip_ansi(log_text)
    reason, command_like_fields = _classify_gazebo_delivery_observation_failure(
        clean,
        pose_text_samples,
    )
    if reason_override is not None:
        reason = reason_override
    pose_count = len([sample for sample in pose_text_samples if sample.strip()])
    missing = []
    for marker in ("Gazebo Sim Server v", "Loading SDF world file"):
        if marker not in clean:
            missing.append(marker)
    if not _has_delivery_state_world_marker(clean):
        missing.append(GZ_SIM_DELIVERY_STATE_WORLD_SDF_PATH)
    observed_phases: list[str] = []
    try:
        poses = [
            parse_gz_sim_entity_pose(sample)
            for sample in pose_text_samples
            if sample.strip()
        ]
        observed_phases = delivery_phases_from_entity_poses(poses)
    except GzSimLogCollectorError:
        observed_phases = []
    digest_payload = {
        "log": clean,
        "poses": [_strip_ansi(sample) for sample in pose_text_samples],
    }
    digest = sha256(
        json.dumps(digest_payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    payload = {
        "reason": reason,
        "digest": digest,
        "pose_sample_count": pose_count,
        "observed_delivery_phases": observed_phases,
    }
    return GazeboDeliveryObservationDiagnostics(
        diagnostics_id=_stable_id("gazebo_delivery_observation_diagnostics", payload),
        reason=reason,
        error_message=error_message
        or f"Gazebo delivery observation rejected as {reason}",
        command_like_fields=sorted(set(command_like_fields)),
        missing_markers=missing,
        pose_sample_count=pose_count,
        observed_delivery_phases=observed_phases,
        log_digest=digest,
        safe_log_excerpt=_safe_log_excerpt(
            clean,
            command_like_fields=command_like_fields,
        ),
        safe_pose_excerpt=_safe_pose_excerpt(
            pose_text_samples,
            command_like_fields=command_like_fields,
        ),
        metadata={
            "artifact_only": True,
            "debug_only": True,
            "captured_at": captured.isoformat(),
            "source": "gazebo_delivery_entity_state_observation_failure",
            "collector": "gz_sim_log_collector.v1",
            "rejected_evidence_type": "gazebo_delivery_entity_state_observation",
            "failure_classification": "invalid_evidence",
            "world_sdf_path": GZ_SIM_DELIVERY_STATE_WORLD_SDF_PATH,
            "delivery_world_ref": GZ_SIM_DELIVERY_STATE_WORLD_REF,
            **(provenance or {}),
        },
    )


def build_gz_sim_log_telemetry_sample(
    log_text: str,
    *,
    captured_at: datetime | None = None,
    source_id: str = "gz-sim-harmonic-container",
    vehicle_id: str = "gz-sim-harmonic-empty-world",
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build telemetry-only evidence from observed Gazebo Sim stdout logs.

    This treats `gz sim` as a log source. It does not mutate Gazebo entities,
    publish ROS, send MAVLink, command setpoints, or expose actuator control.
    """

    captured = captured_at or datetime.now(timezone.utc)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    captured = captured.astimezone(timezone.utc)

    _validate_gz_sim_started(log_text)
    lines = _meaningful_lines(log_text)
    clean_log_text = _strip_ansi(log_text)
    digest = sha256(clean_log_text.encode("utf-8")).hexdigest()[:16]
    return {
        "sample_id": f"gz_sim_harmonic_stdout_{digest}",
        "source": {
            "source_kind": "gz_sim_harmonic_stdout_log",
            "source_id": source_id,
            "vehicle_id": vehicle_id,
        },
        "captured_at": captured.isoformat(),
        "telemetry": {
            "gazebo_process_started": True,
            "gazebo_kind": "gz_sim_harmonic",
            "headless": True,
            "world_loaded": True,
            "log_line_count": len(lines),
            "log_digest": digest,
            "latest_log_excerpt": lines[-1][-300:],
        },
        "metadata": {
            "telemetry_only": True,
            "read_only": True,
            "source_image": "ghcr.io/openrobotics/gazebo:harmonic-full",
            "image_tag": "harmonic-full",
            "simulator_family": "gazebo",
            "simulator_kind": "gz_sim_harmonic_headless",
            "collection_mode": "stdout_logs_only",
            "world_name": "empty",
            "world_sdf_path": "/tmp/empty.sdf",
            "gazebo_command": [
                "gz",
                "sim",
                "-s",
                "-r",
                "-v",
                "4",
                "/tmp/empty.sdf",
            ],
            "startup_markers": list(REQUIRED_GZ_SIM_MARKERS),
            "actual_gz_sim_runtime": True,
            **(provenance or {}),
        },
    }


def build_gz_sim_delivery_world_log_telemetry_sample(
    log_text: str,
    *,
    captured_at: datetime | None = None,
    source_id: str = "gz-sim-harmonic-container",
    vehicle_id: str = "gz-sim-delivery-world",
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build telemetry-only evidence from the delivery Gazebo world logs."""

    _validate_gz_sim_delivery_world_started(log_text)
    delivery_provenance = {
        "collection_mode": "delivery_world_stdout_logs_only",
        "world_name": GZ_SIM_DELIVERY_WORLD_NAME,
        "world_sdf_path": GZ_SIM_DELIVERY_WORLD_SDF_PATH,
        "delivery_world_ref": GZ_SIM_DELIVERY_WORLD_REF,
        "delivery_world_loaded": True,
        **(provenance or {}),
    }
    sample = build_gz_sim_log_telemetry_sample(
        log_text,
        captured_at=captured_at,
        source_id=source_id,
        vehicle_id=vehicle_id,
        provenance=delivery_provenance,
    )
    sample["telemetry"]["delivery_world_loaded"] = True
    return sample


def build_gz_sim_delivery_in_loop_log_telemetry_sample(
    log_text: str,
    *,
    captured_at: datetime | None = None,
    source_id: str = "gz-sim-delivery-in-loop",
    vehicle_id: str = "gz-sim-delivery-world",
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build telemetry from actual Gazebo delivery-world logs plus progress markers."""

    sample = build_gz_sim_delivery_world_log_telemetry_sample(
        log_text,
        captured_at=captured_at,
        source_id=source_id,
        vehicle_id=vehicle_id,
        provenance={
            "collection_mode": "delivery_world_stdout_logs_with_progress_markers",
            "actual_gazebo_in_loop_delivery": True,
            "actual_gz_sim_process_started": True,
            "delivery_progress_source": "scripted_stdout_marker",
            "gazebo_entity_state_observed": False,
            "gazebo_entity_motion_observed": False,
            "mission_os_gazebo_mutation_allowed": False,
            **(provenance or {}),
        },
    )
    progress = _latest_delivery_progress_payload(log_text)
    phase = _text_progress(progress, "phase", default="unknown")
    sample["sample_id"] = f"{sample['sample_id']}_delivery_progress_{phase}"
    sample["source"]["source_kind"] = "gz_sim_delivery_in_loop_stdout_log"
    sample["telemetry"].update(
        {
            "delivery_progress_observed": True,
            "delivery_phase": phase,
            "position": _text_progress(
                progress,
                "position",
                default="35.681236,139.767125,16.0",
            ),
            "battery_percent": _float_progress(
                progress,
                "battery_percent",
                default=80.0,
            ),
            "vehicle_health": _text_progress(
                progress,
                "vehicle_health",
                default="nominal",
            ),
            "weather_snapshot": _text_progress(
                progress,
                "weather_snapshot",
                default="clear",
            ),
            "pickup_reached": _bool_progress(progress, "pickup_reached"),
            "dropoff_reached": _bool_progress(progress, "dropoff_reached"),
            "route_progress_percent": max(
                0.0,
                min(
                    100.0,
                    _float_progress(
                        progress,
                        "route_progress_percent",
                        default=0.0,
                    ),
                ),
            ),
            "route_geofence_violation": _bool_progress(
                progress,
                "route_geofence_violation",
            )
            or _bool_progress(progress, "geofence_violation"),
        }
    )
    sample["metadata"].update(
        {
            "delivery_progress_marker_count": len(
                _delivery_progress_payloads(log_text)
            ),
            "latest_delivery_progress_phase": phase,
            "delivery_progress_source": "scripted_stdout_marker",
            "actual_gz_sim_process_started": True,
            "gazebo_entity_state_observed": False,
            "gazebo_entity_motion_observed": False,
            "mission_os_gazebo_mutation_allowed": False,
        }
    )
    return sample


def build_gz_sim_delivery_entity_state_telemetry_sample(
    log_text: str,
    pose_text_samples: list[str],
    *,
    captured_at: datetime | None = None,
    source_id: str = "gz-sim-delivery-entity-state",
    vehicle_id: str = GZ_SIM_DELIVERY_STATE_ENTITY_NAME,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build telemetry from actual Gazebo entity pose/state observations."""

    _validate_gz_sim_delivery_state_world_started(log_text)
    poses = [
        parse_gz_sim_entity_pose(
            pose_text,
            entity_name=GZ_SIM_DELIVERY_STATE_ENTITY_NAME,
        )
        for pose_text in pose_text_samples
        if pose_text.strip()
    ]
    if not poses:
        raise GzSimLogCollectorError("entity_pose_samples_missing")
    latest_pose = poses[-1]
    latest_x = float(latest_pose["x"])
    phase, pickup_reached, dropoff_reached, route_progress = (
        _delivery_progress_from_pose(latest_x)
    )
    motion_observed = _entity_motion_observed(poses)
    if not motion_observed:
        raise GzSimLogCollectorError("entity_motion_not_observed")
    observed_phases = delivery_phases_from_entity_poses(poses)
    required_phases = ("pickup", "enroute", "dropoff", "completed")
    missing_phases = [
        phase for phase in required_phases if phase not in observed_phases
    ]
    if missing_phases:
        raise GzSimLogCollectorError(
            "delivery_phase_sequence_incomplete: " + ", ".join(missing_phases)
        )

    captured = captured_at or datetime.now(timezone.utc)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    captured = captured.astimezone(timezone.utc)
    lines = _meaningful_lines(log_text)
    clean_log_text = _strip_ansi(log_text)
    digest = sha256(clean_log_text.encode("utf-8")).hexdigest()[:16]
    metadata = {
            "collection_mode": "delivery_world_entity_pose_topic",
            "telemetry_only": True,
            "read_only": True,
            "source_image": "ghcr.io/openrobotics/gazebo:harmonic-full",
            "image_tag": "harmonic-full",
            "simulator_family": "gazebo",
            "simulator_kind": "gz_sim_harmonic_headless",
            "world_name": GZ_SIM_DELIVERY_STATE_WORLD_NAME,
            "world_sdf_path": GZ_SIM_DELIVERY_STATE_WORLD_SDF_PATH,
            "delivery_world_ref": GZ_SIM_DELIVERY_STATE_WORLD_REF,
            "delivery_world_loaded": True,
            "actual_gazebo_in_loop_delivery": True,
            "actual_gz_sim_process_started": True,
            "delivery_progress_source": "gazebo_entity_pose_topic",
            "gazebo_entity_state_observed": True,
            "gazebo_entity_motion_observed": True,
            "mission_os_gazebo_mutation_allowed": False,
            "observed_entity_name": GZ_SIM_DELIVERY_STATE_ENTITY_NAME,
            "pose_topic": f"/world/{GZ_SIM_DELIVERY_STATE_WORLD_NAME}/pose/info",
            "dynamic_pose_topic": (
                f"/world/{GZ_SIM_DELIVERY_STATE_WORLD_NAME}/dynamic_pose/info"
            ),
            "trajectory_source": "gz_sim_trajectory_follower_system",
            **(provenance or {}),
    }
    sample = {
        "sample_id": f"gz_sim_delivery_entity_state_{digest}_{phase}",
        "source": {
            "source_kind": "gz_sim_delivery_entity_state_pose",
            "source_id": source_id,
            "vehicle_id": vehicle_id,
        },
        "captured_at": captured.isoformat(),
        "telemetry": {
            "gazebo_process_started": True,
            "gazebo_kind": "gz_sim_harmonic",
            "headless": True,
            "world_loaded": True,
            "log_line_count": len(lines),
            "log_digest": digest,
            "latest_log_excerpt": lines[-1][-300:] if lines else "",
            "delivery_world_loaded": True,
            "delivery_progress_observed": True,
            "delivery_phase": phase,
            "position": f"{latest_pose['x']},{latest_pose['y']},{latest_pose['z']}",
            "battery_percent": 83.0,
            "vehicle_health": "nominal",
            "weather_snapshot": "clear",
            "pickup_reached": pickup_reached,
            "dropoff_reached": dropoff_reached,
            "route_progress_percent": route_progress,
            "route_geofence_violation": False,
            "entity_pose_x_m": latest_x,
            "entity_pose_y_m": float(latest_pose["y"]),
            "entity_pose_z_m": float(latest_pose["z"]),
            "entity_pose_sample_count": len(poses),
            "entity_motion_delta_x_m": latest_x - float(poses[0]["x"]),
            "observed_delivery_phase_count": len(observed_phases),
        },
        "metadata": metadata,
    }
    sample["metadata"].update(
        {
            "actual_gz_sim_process_started": True,
            "delivery_progress_source": "gazebo_entity_pose_topic",
            "gazebo_entity_state_observed": True,
            "gazebo_entity_motion_observed": True,
            "mission_os_gazebo_mutation_allowed": False,
            "observed_entity_name": GZ_SIM_DELIVERY_STATE_ENTITY_NAME,
            "entity_pose_sample_count": len(poses),
            "first_entity_pose_x_m": float(poses[0]["x"]),
            "latest_entity_pose_x_m": latest_x,
            "entity_motion_delta_x_m": latest_x - float(poses[0]["x"]),
            "observed_delivery_phases": observed_phases,
            "latest_delivery_progress_phase": phase,
        }
    )
    return sample


def collect_gz_sim_log_sanitized(
    log_text: str,
    *,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
) -> Px4GazeboSanitizedTelemetry:
    """Collect Gazebo Sim stdout logs into sanitized telemetry."""

    sample = build_gz_sim_log_telemetry_sample(
        log_text,
        captured_at=captured_at,
        provenance=provenance,
    )
    try:
        return sanitize_px4_gazebo_telemetry_sample(sample)
    except Px4GazeboTelemetryRejected as exc:
        raise GzSimLogCollectorError(
            f"Gazebo Sim stdout telemetry rejected before persistence: {exc}"
        ) from exc


def collect_gz_sim_delivery_world_log_sanitized(
    log_text: str,
    *,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
) -> Px4GazeboSanitizedTelemetry:
    """Collect delivery-world Gazebo Sim stdout into sanitized telemetry."""

    sample = build_gz_sim_delivery_world_log_telemetry_sample(
        log_text,
        captured_at=captured_at,
        provenance=provenance,
    )
    try:
        return sanitize_px4_gazebo_telemetry_sample(sample)
    except Px4GazeboTelemetryRejected as exc:
        raise GzSimLogCollectorError(
            "Gazebo Sim delivery-world stdout telemetry rejected before "
            f"persistence: {exc}"
        ) from exc


def collect_gz_sim_delivery_in_loop_log_sanitized(
    log_text: str,
    *,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
) -> Px4GazeboSanitizedTelemetry:
    """Collect actual Gazebo delivery-world stdout plus progress into telemetry."""

    sample = build_gz_sim_delivery_in_loop_log_telemetry_sample(
        log_text,
        captured_at=captured_at,
        provenance=provenance,
    )
    try:
        return sanitize_px4_gazebo_telemetry_sample(sample)
    except Px4GazeboTelemetryRejected as exc:
        raise GzSimLogCollectorError(
            "Gazebo Sim delivery in-loop stdout telemetry rejected before "
            f"persistence: {exc}"
        ) from exc


def collect_gz_sim_delivery_entity_state_sanitized(
    log_text: str,
    pose_text_samples: list[str],
    *,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
) -> Px4GazeboSanitizedTelemetry:
    """Collect actual Gazebo entity pose observations into telemetry."""

    sample = build_gz_sim_delivery_entity_state_telemetry_sample(
        log_text,
        pose_text_samples,
        captured_at=captured_at,
        provenance=provenance,
    )
    try:
        return sanitize_px4_gazebo_telemetry_sample(sample)
    except Px4GazeboTelemetryRejected as exc:
        raise GzSimLogCollectorError(
            "Gazebo Sim delivery entity-state telemetry rejected before "
            f"persistence: {exc}"
        ) from exc


def attach_gz_sim_log_hil_review_gate_artifacts(
    task_id: str,
    log_text: str,
    *,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
    task_store_factory,
) -> dict[str, Any]:
    """Attach Gazebo Sim log-derived HIL review/gate artifacts to a task."""

    sanitized = collect_gz_sim_log_sanitized(
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


def attach_gz_sim_delivery_world_hil_review_gate_artifacts(
    task_id: str,
    log_text: str,
    *,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
    task_store_factory,
) -> dict[str, Any]:
    """Attach delivery-world log-derived HIL review/gate artifacts to a task."""

    sanitized = collect_gz_sim_delivery_world_log_sanitized(
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


def build_gz_sim_delivery_world_telemetry_diagnostics(
    log_text: str,
    *,
    error_message: str | None = None,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
) -> GzSimTelemetryDiagnostics:
    """Build debug-only diagnostics for rejected delivery-world stdout logs."""

    clean = _strip_ansi(log_text)
    reason_override: GzSimTelemetryFailureReason | None = None
    if (
        clean.strip()
        and not _command_like_log_fields(clean)
        and not _missing_markers(clean)
        and "Unable to find or load SDF world file" not in clean
        and not _has_delivery_world_marker(clean)
    ):
        reason_override = "delivery_world_mismatch"
    return build_gz_sim_telemetry_diagnostics(
        clean,
        error_message=error_message,
        captured_at=captured_at,
        provenance={
            "expected_world_name": GZ_SIM_DELIVERY_WORLD_NAME,
            "expected_world_sdf_path": GZ_SIM_DELIVERY_WORLD_SDF_PATH,
            "expected_delivery_world_ref": GZ_SIM_DELIVERY_WORLD_REF,
            **(provenance or {}),
        },
        reason_override=reason_override,
    )


def attach_gz_sim_failure_diagnostics_artifact(
    task_id: str,
    log_text: str,
    *,
    error_message: str | None = None,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
    task_store_factory=get_task_store,
) -> dict[str, Any]:
    """Attach rejected `gz sim` diagnostics without creating runtime artifacts."""

    diagnostics = build_gz_sim_telemetry_diagnostics(
        log_text,
        error_message=error_message,
        captured_at=captured_at,
        provenance=provenance,
    ).model_dump(mode="json")
    store = task_store_factory()
    updated = store.update(
        task_id,
        artifacts={"gz_sim_telemetry_diagnostics": diagnostics},
    )
    if updated is None:
        raise GzSimLogCollectorError(f"task not found: {task_id}")
    return diagnostics


def attach_gz_sim_delivery_world_failure_diagnostics_artifact(
    task_id: str,
    log_text: str,
    *,
    error_message: str | None = None,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
    task_store_factory=get_task_store,
) -> dict[str, Any]:
    """Attach rejected delivery-world diagnostics without runtime artifacts."""

    diagnostics = build_gz_sim_delivery_world_telemetry_diagnostics(
        log_text,
        error_message=error_message,
        captured_at=captured_at,
        provenance=provenance,
    ).model_dump(mode="json")
    store = task_store_factory()
    updated = store.update(
        task_id,
        artifacts={"gz_sim_telemetry_diagnostics": diagnostics},
    )
    if updated is None:
        raise GzSimLogCollectorError(f"task not found: {task_id}")
    return diagnostics


def attach_gazebo_delivery_observation_diagnostics_artifact(
    task_id: str,
    log_text: str,
    pose_text_samples: list[str],
    *,
    error_message: str | None = None,
    captured_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
    reason_override: GazeboDeliveryObservationFailureReason | None = None,
    task_store_factory=get_task_store,
) -> dict[str, Any]:
    """Attach rejected delivery observation diagnostics without runtime artifacts."""

    diagnostics = build_gazebo_delivery_observation_diagnostics(
        log_text,
        pose_text_samples,
        error_message=error_message,
        captured_at=captured_at,
        provenance=provenance,
        reason_override=reason_override,
    ).model_dump(mode="json")
    store = task_store_factory()
    updated = store.update(
        task_id,
        artifacts={"gazebo_delivery_observation_diagnostics": diagnostics},
    )
    if updated is None:
        raise GzSimLogCollectorError(f"task not found: {task_id}")
    return diagnostics


__all__ = [
    "GAZEBO_DELIVERY_OBSERVATION_DIAGNOSTICS_SCHEMA_VERSION",
    "GzSimLogCollectorError",
    "GZ_SIM_TELEMETRY_DIAGNOSTICS_SCHEMA_VERSION",
    "GazeboDeliveryObservationDiagnostics",
    "GzSimTelemetryDiagnostics",
    "GZ_SIM_DELIVERY_WORLD_NAME",
    "GZ_SIM_DELIVERY_WORLD_REF",
    "GZ_SIM_DELIVERY_WORLD_SDF_PATH",
    "GZ_SIM_DELIVERY_STATE_ENTITY_NAME",
    "GZ_SIM_DELIVERY_STATE_WORLD_NAME",
    "GZ_SIM_DELIVERY_STATE_WORLD_REF",
    "GZ_SIM_DELIVERY_STATE_WORLD_SDF_PATH",
    "REQUIRED_GZ_SIM_MARKERS",
    "attach_gazebo_delivery_observation_diagnostics_artifact",
    "attach_gz_sim_failure_diagnostics_artifact",
    "attach_gz_sim_delivery_world_failure_diagnostics_artifact",
    "attach_gz_sim_delivery_world_hil_review_gate_artifacts",
    "attach_gz_sim_log_hil_review_gate_artifacts",
    "build_gazebo_delivery_observation_diagnostics",
    "build_gz_sim_delivery_entity_state_telemetry_sample",
    "build_gz_sim_delivery_in_loop_log_telemetry_sample",
    "build_gz_sim_delivery_world_log_telemetry_sample",
    "build_gz_sim_delivery_world_telemetry_diagnostics",
    "build_gz_sim_log_telemetry_sample",
    "build_gz_sim_telemetry_diagnostics",
    "collect_gz_sim_delivery_entity_state_sanitized",
    "collect_gz_sim_delivery_in_loop_log_sanitized",
    "collect_gz_sim_delivery_world_log_sanitized",
    "collect_gz_sim_log_sanitized",
    "delivery_phases_from_entity_poses",
    "parse_gz_sim_entity_pose",
]
