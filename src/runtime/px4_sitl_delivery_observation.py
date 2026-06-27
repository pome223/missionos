"""PX4 SITL telemetry-only delivery vehicle observation artifact."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.px4_gazebo_delivery_world_profile import (
    PX4GazeboDeliveryWorldProfile,
    build_px4_gazebo_delivery_world_profile,
)
from src.runtime.px4_gazebo_telemetry import Px4GazeboSanitizedTelemetry
from src.runtime.px4_sitl_log_collector import collect_px4_sitl_log_sanitized
from src.runtime.task_store import TaskStore, get_task_store

PX4_SITL_DELIVERY_OBSERVATION_SCHEMA_VERSION = "px4_sitl_delivery_observation.v1"


class PX4SitlDeliveryObservationError(RuntimeError):
    """Raised when PX4 SITL telemetry cannot become a delivery observation."""


_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "action",
        "actuator",
        "command",
        "dispatch",
        "gazebo_mutation",
        "hardware_target",
        "mavlink_command",
        "mission_upload",
        "position_setpoint",
        "ros_action",
        "setpoint",
        "thrust",
        "torque",
    }
)


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


_FORBIDDEN_COMMAND_KEYS_NORMALIZED = frozenset(
    _normalize_key(key) for key in _FORBIDDEN_COMMAND_KEYS
)


def _command_like_key_paths(value: Any, *, root: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, sub in value.items():
            key_text = str(key)
            path = f"{root}.{key_text}" if root else key_text
            if _normalize_key(key_text) in _FORBIDDEN_COMMAND_KEYS_NORMALIZED:
                findings.append(path)
            findings.extend(_command_like_key_paths(sub, root=path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            path = f"{root}.{index}" if root else str(index)
            findings.extend(_command_like_key_paths(item, root=path))
    return findings


def _raise_for_command_like_keys(value: Any, *, root: str) -> None:
    findings = _command_like_key_paths(value, root=root)
    if findings:
        raise PX4SitlDeliveryObservationError(
            "px4 sitl delivery observation refused command-like keys: "
            + ", ".join(sorted(findings))
        )


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


class PX4SitlDeliveryObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_SITL_DELIVERY_OBSERVATION_SCHEMA_VERSION] = (
        PX4_SITL_DELIVERY_OBSERVATION_SCHEMA_VERSION
    )
    observation_id: str
    profile_ref: str = Field(min_length=1)
    telemetry_ref: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    vehicle_id: str = Field(min_length=1)
    delivery_vehicle_ref: str = Field(min_length=1)
    observed_at: datetime
    measurement_keys: tuple[str, ...] = Field(min_length=1)
    measurements: dict[str, float | int | bool | str] = Field(default_factory=dict)
    observation_status: Literal["observed"] = "observed"
    simulation_only: Literal[True] = True
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    command_surface_present: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return _utc(parsed)

    @model_validator(mode="after")
    def _reject_commands(self) -> "PX4SitlDeliveryObservation":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


def _to_profile(
    value: PX4GazeboDeliveryWorldProfile | Mapping[str, Any] | None,
) -> PX4GazeboDeliveryWorldProfile:
    if isinstance(value, PX4GazeboDeliveryWorldProfile):
        return value
    if value is not None:
        return PX4GazeboDeliveryWorldProfile.model_validate(dict(value))
    return build_px4_gazebo_delivery_world_profile()


def _to_telemetry(
    value: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
) -> Px4GazeboSanitizedTelemetry:
    if isinstance(value, Px4GazeboSanitizedTelemetry):
        return value
    return Px4GazeboSanitizedTelemetry.model_validate(dict(value))


def _validate_profile_telemetry_correlation(
    *,
    profile: PX4GazeboDeliveryWorldProfile,
    telemetry: Px4GazeboSanitizedTelemetry,
) -> None:
    if telemetry.measurements.get("px4_sitl_started") is not True:
        raise PX4SitlDeliveryObservationError(
            "PX4 SITL delivery observation requires px4_sitl_started=true"
        )
    if telemetry.metadata.get("px4_sim_model") != profile.px4_sitl_model:
        raise PX4SitlDeliveryObservationError("PX4 SITL model mismatch")
    if telemetry.metadata.get("source_image") != profile.px4_sitl_image:
        raise PX4SitlDeliveryObservationError("PX4 SITL image mismatch")


def build_px4_sitl_delivery_observation(
    *,
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
    profile: PX4GazeboDeliveryWorldProfile | Mapping[str, Any] | None = None,
    delivery_vehicle_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4SitlDeliveryObservation:
    """Build a read-only delivery observation from sanitized PX4 SITL telemetry."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    resolved_profile = _to_profile(profile)
    telemetry = _to_telemetry(sanitized_telemetry)
    if not telemetry.source_kind.startswith("px4_sitl"):
        raise PX4SitlDeliveryObservationError(
            f"expected PX4 SITL telemetry source, got {telemetry.source_kind!r}"
        )
    if telemetry.command_payload_allowed or telemetry.mavlink_dispatch_allowed:
        raise PX4SitlDeliveryObservationError(
            "sanitized telemetry must not allow command payloads or MAVLink dispatch"
        )
    _validate_profile_telemetry_correlation(
        profile=resolved_profile,
        telemetry=telemetry,
    )
    payload = {
        "profile_id": resolved_profile.profile_id,
        "telemetry_id": telemetry.telemetry_id,
        "vehicle_id": telemetry.vehicle_id,
        "captured_at": telemetry.captured_at.isoformat(),
        "measurement_keys": telemetry.measurement_keys,
    }
    return PX4SitlDeliveryObservation(
        observation_id=_stable_id("px4_sitl_delivery_observation", payload),
        profile_ref=f"px4_gazebo_delivery_world_profile:{resolved_profile.profile_id}",
        telemetry_ref=f"px4_gazebo_sanitized_telemetry:{telemetry.telemetry_id}",
        source_kind=telemetry.source_kind,
        source_id=telemetry.source_id,
        vehicle_id=telemetry.vehicle_id,
        delivery_vehicle_ref=delivery_vehicle_ref
        or f"delivery_vehicle:{telemetry.vehicle_id}",
        observed_at=telemetry.captured_at,
        measurement_keys=tuple(telemetry.measurement_keys),
        measurements=dict(telemetry.measurements),
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "issue": 309,
            "parent_epic": 307,
            "profile_name": resolved_profile.profile_name,
            "telemetry_first": True,
            "no_command_dispatch": True,
            "no_hardware_target": True,
        },
    )


def build_px4_sitl_delivery_observation_from_logs(
    log_text: str,
    *,
    captured_at: datetime | None = None,
    profile: PX4GazeboDeliveryWorldProfile | Mapping[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> PX4SitlDeliveryObservation:
    """Collect PX4 SITL stdout logs into a delivery observation artifact."""

    telemetry = collect_px4_sitl_log_sanitized(
        log_text,
        captured_at=captured_at,
        provenance=provenance,
    )
    return build_px4_sitl_delivery_observation(
        sanitized_telemetry=telemetry,
        profile=profile,
    )


def attach_px4_sitl_delivery_observation(
    task_id: str,
    *,
    log_text: str,
    captured_at: datetime | None = None,
    profile: PX4GazeboDeliveryWorldProfile | Mapping[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach a PX4 SITL delivery observation without mutating task status."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4SitlDeliveryObservationError(
            f"task {task_id} not found; cannot attach PX4 SITL delivery observation"
        )
    observation = build_px4_sitl_delivery_observation_from_logs(
        log_text,
        captured_at=captured_at,
        profile=profile,
        provenance=provenance,
    )
    artifacts = {"px4_sitl_delivery_observation": observation.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise PX4SitlDeliveryObservationError(
            f"task {task_id} disappeared while attaching PX4 SITL delivery observation"
        )
    return artifacts


__all__ = [
    "PX4_SITL_DELIVERY_OBSERVATION_SCHEMA_VERSION",
    "PX4SitlDeliveryObservation",
    "PX4SitlDeliveryObservationError",
    "attach_px4_sitl_delivery_observation",
    "build_px4_sitl_delivery_observation",
    "build_px4_sitl_delivery_observation_from_logs",
]
