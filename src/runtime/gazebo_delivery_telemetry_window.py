"""Bounded Gazebo delivery telemetry/log window artifact.

``gazebo_delivery_telemetry_window.v1`` records a bounded observation window
from a Gazebo delivery scenario. It is an observation artifact only: it does
not advance Gazebo, mutate entities, send ROS/MAVLink payloads, set setpoints,
or execute actuators.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.gazebo_delivery_scenario import (
    GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION,
    GazeboDeliveryScenario,
)
from src.runtime.px4_gazebo_telemetry import (
    PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION,
    Px4GazeboSanitizedTelemetry,
    build_px4_gazebo_hil_review_gate_smoke,
)
from src.runtime.task_store import TaskStore, get_task_store


GAZEBO_DELIVERY_TELEMETRY_WINDOW_SCHEMA_VERSION = (
    "gazebo_delivery_telemetry_window.v1"
)


class GazeboDeliveryTelemetryWindowError(RuntimeError):
    """Raised when a Gazebo delivery telemetry window is unsafe."""


_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "action",
        "actuator",
        "actuator_execution_allowed",
        "attitude_setpoint",
        "command",
        "command_payload_allowed",
        "dispatch",
        "dispatch_implementation_present",
        "entity_mutation",
        "execute",
        "gazebo_command",
        "gazebo_mutation",
        "joint",
        "landing_command",
        "live_execution_allowed",
        "mavlink_command",
        "mavlink_dispatch_allowed",
        "mission_upload",
        "physical_execution_invoked",
        "position_setpoint",
        "return_to_home_command",
        "ros_action",
        "ros_dispatch_allowed",
        "ros_topic",
        "setpoint",
        "thrust",
        "torque",
        "velocity_command",
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
        raise GazeboDeliveryTelemetryWindowError(
            "gazebo delivery telemetry window refused command-like keys: "
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


def _as_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(sorted({str(item).strip() for item in values or () if str(item).strip()}))


def _to_contract(value: DeliveryMissionContract | Mapping[str, Any]) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_scenario(
    value: GazeboDeliveryScenario | Mapping[str, Any],
) -> GazeboDeliveryScenario:
    if isinstance(value, GazeboDeliveryScenario):
        return value
    return GazeboDeliveryScenario.model_validate(dict(value))


def _to_telemetry(
    value: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
) -> Px4GazeboSanitizedTelemetry:
    if isinstance(value, Px4GazeboSanitizedTelemetry):
        return value
    return Px4GazeboSanitizedTelemetry.model_validate(dict(value))


def _safe_source_metadata(metadata: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    safe: dict[str, Any] = {}
    redacted: list[str] = []
    for key, value in metadata.items():
        if _normalize_key(str(key)) in _FORBIDDEN_COMMAND_KEYS_NORMALIZED:
            redacted.append(str(key))
            continue
        if isinstance(value, str | int | float | bool) or value is None:
            safe[str(key)] = value
        elif isinstance(value, list) and all(
            isinstance(item, str | int | float | bool) or item is None for item in value
        ):
            safe[str(key)] = value
        elif isinstance(value, dict):
            nested, nested_redacted = _safe_source_metadata(value)
            safe[str(key)] = nested
            redacted.extend(f"{key}.{item}" for item in nested_redacted)
    return safe, tuple(sorted(redacted))


class GazeboDeliveryTelemetryWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[GAZEBO_DELIVERY_TELEMETRY_WINDOW_SCHEMA_VERSION] = (
        GAZEBO_DELIVERY_TELEMETRY_WINDOW_SCHEMA_VERSION
    )
    window_id: str
    delivery_mission_contract_id: str
    delivery_mission_id: str
    gazebo_delivery_scenario_id: str
    phase: Literal["preflight", "pickup", "enroute", "dropoff", "completion"]
    source_kind: str
    source_id: str
    captured_at: datetime
    max_duration_seconds: float = Field(gt=0)
    max_sample_count: int = Field(gt=0)
    sample_count: int = Field(ge=1)
    window_bounded: Literal[True] = True
    window_truncated: bool = False
    measurement_keys: tuple[str, ...] = Field(min_length=1)
    telemetry_refs: tuple[str, ...] = Field(min_length=1)
    stale_count: int = Field(ge=0)
    malformed_count: int = Field(ge=0)
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    redacted_source_metadata_keys: tuple[str, ...] = ()
    created_at: datetime
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    gazebo_delivery_scenario_schema_version: Literal[
        GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION
    ] = GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION
    px4_gazebo_sanitized_telemetry_schema_version: Literal[
        PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION
    ] = PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION
    simulation_only: Literal[True] = True
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    stronger_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("captured_at", "created_at", mode="before")
    @classmethod
    def _coerce_datetime(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return _utc(parsed)

    @model_validator(mode="after")
    def _validate_payload(self) -> "GazeboDeliveryTelemetryWindow":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        _raise_for_command_like_keys(self.source_metadata, root="source_metadata")
        if self.sample_count > self.max_sample_count:
            raise ValueError("sample_count cannot exceed max_sample_count")
        return self


def build_gazebo_delivery_telemetry_window(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
    phase: str = "preflight",
    max_duration_seconds: float = 10.0,
    max_sample_count: int = 1,
    malformed_count: int = 0,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> GazeboDeliveryTelemetryWindow:
    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    contract = _to_contract(delivery_mission_contract)
    scenario = _to_scenario(gazebo_delivery_scenario)
    telemetry = _to_telemetry(sanitized_telemetry)
    if scenario.delivery_mission_contract_id != contract.contract_id:
        raise GazeboDeliveryTelemetryWindowError("gazebo scenario contract_id mismatch")
    if scenario.delivery_mission_id != contract.mission_id:
        raise GazeboDeliveryTelemetryWindowError("gazebo scenario mission_id mismatch")

    safe_metadata, redacted_keys = _safe_source_metadata(telemetry.metadata)
    refs = (f"px4_gazebo_sanitized_telemetry:{telemetry.telemetry_id}",)
    measurement_keys = _as_tuple(telemetry.measurement_keys)
    created_at = _utc(now)
    payload = {
        "delivery_mission_contract_id": contract.contract_id,
        "gazebo_delivery_scenario_id": scenario.scenario_id,
        "phase": phase,
        "source_kind": telemetry.source_kind,
        "source_id": telemetry.source_id,
        "telemetry_refs": refs,
        "measurement_keys": measurement_keys,
        "malformed_count": malformed_count,
    }
    return GazeboDeliveryTelemetryWindow(
        window_id=_stable_id("gazebo_delivery_telemetry_window", payload),
        delivery_mission_contract_id=contract.contract_id,
        delivery_mission_id=contract.mission_id,
        gazebo_delivery_scenario_id=scenario.scenario_id,
        phase=phase,
        source_kind=telemetry.source_kind,
        source_id=telemetry.source_id,
        captured_at=telemetry.captured_at,
        max_duration_seconds=max_duration_seconds,
        max_sample_count=max_sample_count,
        sample_count=1,
        window_truncated=False,
        measurement_keys=measurement_keys,
        telemetry_refs=refs,
        stale_count=0,
        malformed_count=malformed_count,
        source_metadata=safe_metadata,
        redacted_source_metadata_keys=redacted_keys,
        created_at=created_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "telemetry_window_only": True,
            "observation_only": True,
            "no_dispatch_surface": True,
            "no_entity_mutation": True,
        },
    )


def build_gazebo_delivery_telemetry_window_hil_artifacts(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
    phase: str = "preflight",
    freshness_threshold_seconds: float = 60.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a telemetry window plus HIL evidence/review/gate artifacts."""

    telemetry = _to_telemetry(sanitized_telemetry)
    window = build_gazebo_delivery_telemetry_window(
        delivery_mission_contract=delivery_mission_contract,
        gazebo_delivery_scenario=gazebo_delivery_scenario,
        sanitized_telemetry=telemetry,
        phase=phase,
        now=now,
    )
    hil_artifacts = build_px4_gazebo_hil_review_gate_smoke(
        telemetry,
        freshness_threshold_seconds=freshness_threshold_seconds,
        now=now,
    )
    return {
        "gazebo_delivery_telemetry_window": window.model_dump(mode="json"),
        **hil_artifacts,
    }


def attach_gazebo_delivery_telemetry_window_hil_artifacts(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
    phase: str = "preflight",
    freshness_threshold_seconds: float = 60.0,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach telemetry window and HIL artifacts without mutating task status."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise GazeboDeliveryTelemetryWindowError(
            f"task {task_id} not found; cannot attach Gazebo delivery telemetry window"
        )
    artifacts = build_gazebo_delivery_telemetry_window_hil_artifacts(
        delivery_mission_contract=delivery_mission_contract,
        gazebo_delivery_scenario=gazebo_delivery_scenario,
        sanitized_telemetry=sanitized_telemetry,
        phase=phase,
        freshness_threshold_seconds=freshness_threshold_seconds,
        now=now,
    )
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise GazeboDeliveryTelemetryWindowError(
            f"task {task_id} disappeared while attaching Gazebo delivery telemetry window"
        )
    return artifacts


__all__ = [
    "GAZEBO_DELIVERY_TELEMETRY_WINDOW_SCHEMA_VERSION",
    "GazeboDeliveryTelemetryWindow",
    "GazeboDeliveryTelemetryWindowError",
    "attach_gazebo_delivery_telemetry_window_hil_artifacts",
    "build_gazebo_delivery_telemetry_window",
    "build_gazebo_delivery_telemetry_window_hil_artifacts",
]
