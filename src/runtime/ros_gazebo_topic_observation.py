"""Observation-only ROS/Gazebo topic adapter and command-surface diagnostics."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from src.runtime.px4_gazebo_state_correlation import (
    PX4SITLDeliveryReadinessDiagnostics,
)
from src.runtime.task_store import TaskStore, get_task_store

ROS_GAZEBO_TOPIC_OBSERVATION_ADAPTER_SCHEMA_VERSION = (
    "ros_gazebo_delivery_topic_observation_adapter.v1"
)
PX4_GAZEBO_COMMAND_SURFACE_DIAGNOSTICS_SCHEMA_VERSION = (
    "px4_gazebo_command_surface_diagnostics.v1"
)

DEFAULT_OBSERVATION_TOPIC_REFS = (
    "/clock",
    "/world/delivery_state_driven/pose/info",
    "/world/delivery_state_driven/scene/info",
)
COMMAND_SURFACE_MARKERS = (
    "action",
    "actuator",
    "arming",
    "cmd",
    "command",
    "dispatch",
    "goal",
    "mavlink",
    "mission",
    "offboard",
    "ros_action",
    "setpoint",
    "trajectory_setpoint",
    "vehicle_command",
)


class ROSGazeboTopicObservationError(RuntimeError):
    """Raised when ROS/Gazebo topic observation evidence is unsafe."""


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


def _normalize_marker_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


_COMMAND_MARKERS_NORMALIZED = tuple(
    _normalize_marker_text(marker) for marker in COMMAND_SURFACE_MARKERS
)


def _ordered_tuple(values: Sequence[str] | str | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        stripped = values.strip()
        return (stripped,) if stripped else ()
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _command_like_key_paths(value: Any, *, root: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, sub in value.items():
            key_text = str(key)
            path = f"{root}.{key_text}" if root else key_text
            if _normalize_marker_text(key_text) in _COMMAND_MARKERS_NORMALIZED:
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
        raise ROSGazeboTopicObservationError(
            "ROS/Gazebo topic observation refused command-like keys: "
            + ", ".join(sorted(findings))
        )


def _command_surface_markers(topic_refs: Sequence[str]) -> tuple[str, ...]:
    markers: set[str] = set()
    for ref in topic_refs:
        normalized = _normalize_marker_text(ref)
        for marker, normalized_marker in zip(
            COMMAND_SURFACE_MARKERS,
            _COMMAND_MARKERS_NORMALIZED,
            strict=True,
        ):
            if normalized_marker and normalized_marker in normalized:
                markers.add(marker)
    return tuple(sorted(markers))


def _readiness_ref(readiness: PX4SITLDeliveryReadinessDiagnostics) -> str:
    return f"px4_sitl_delivery_readiness_diagnostics:{readiness.diagnostics_id}"


class _ObservationOnlySafetyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    observation_only: Literal[True] = True
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    command_surface_present: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    socket_opened: Literal[False] = False
    mavlink_frame_sent: Literal[False] = False
    ros_action_dispatch_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False


class ROSGazeboTopicObservationAdapter(_ObservationOnlySafetyBoundary):
    """Read-only topic refs consumed as observation inputs only."""

    schema_version: Literal[ROS_GAZEBO_TOPIC_OBSERVATION_ADAPTER_SCHEMA_VERSION] = (
        ROS_GAZEBO_TOPIC_OBSERVATION_ADAPTER_SCHEMA_VERSION
    )
    adapter_id: str
    readiness_ref: str = Field(min_length=1)
    readiness_status: Literal["ready"] = "ready"
    observed_topic_refs: tuple[str, ...] = Field(min_length=1)
    observation_topic_refs: tuple[str, ...] = Field(min_length=1)
    topic_observation_mode: Literal["read_only_topic_refs"] = "read_only_topic_refs"
    ros_action_topic_refs: tuple[str, ...] = ()
    command_like_topic_refs: tuple[str, ...] = ()
    adapted_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "observed_topic_refs",
        "observation_topic_refs",
        "ros_action_topic_refs",
        "command_like_topic_refs",
        mode="before",
    )
    @classmethod
    def _coerce_topic_tuple(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple(value)

    @field_validator("adapted_at", mode="before")
    @classmethod
    def _coerce_adapted_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return _utc(parsed)

    @model_validator(mode="after")
    def _validate_adapter(self) -> "ROSGazeboTopicObservationAdapter":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        command_refs = _command_surface_markers(self.observed_topic_refs)
        if command_refs or self.command_like_topic_refs or self.ros_action_topic_refs:
            raise ValueError("topic observation adapter cannot include command topics")
        if set(self.observation_topic_refs) != set(self.observed_topic_refs):
            raise ValueError("observation topic refs must match observed topic refs")
        return self


class PX4GazeboCommandSurfaceDiagnostics(_ObservationOnlySafetyBoundary):
    """Diagnostics proving ROS/MAVLink command surfaces remain closed."""

    schema_version: Literal[PX4_GAZEBO_COMMAND_SURFACE_DIAGNOSTICS_SCHEMA_VERSION] = (
        PX4_GAZEBO_COMMAND_SURFACE_DIAGNOSTICS_SCHEMA_VERSION
    )
    diagnostics_id: str
    adapter_ref: str = Field(min_length=1)
    command_surface_status: Literal["closed"] = "closed"
    checked_topic_refs: tuple[str, ...] = Field(min_length=1)
    rejected_topic_refs: tuple[str, ...] = ()
    rejected_payload_markers: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    checked_at: datetime
    retry_attempted: Literal[False] = False
    stronger_execution_attempted: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "checked_topic_refs",
        "rejected_topic_refs",
        "rejected_payload_markers",
        "blocked_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple(value)

    @field_validator("checked_at", mode="before")
    @classmethod
    def _coerce_checked_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return _utc(parsed)

    @model_validator(mode="after")
    def _validate_diagnostics(self) -> "PX4GazeboCommandSurfaceDiagnostics":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        if self.rejected_topic_refs or self.rejected_payload_markers:
            raise ValueError("closed command surface diagnostics cannot include leaks")
        if self.blocked_reasons:
            raise ValueError("closed command surface diagnostics cannot be blocked")
        markers = _command_surface_markers(self.checked_topic_refs)
        if markers:
            raise ValueError(
                "closed command surface diagnostics found command-like topics: "
                + ", ".join(markers)
            )
        return self


def _as_readiness(
    value: PX4SITLDeliveryReadinessDiagnostics | Mapping[str, Any],
) -> PX4SITLDeliveryReadinessDiagnostics:
    if isinstance(value, PX4SITLDeliveryReadinessDiagnostics):
        return value
    return PX4SITLDeliveryReadinessDiagnostics.model_validate(dict(value))


def build_ros_gazebo_topic_observation_adapter(
    *,
    readiness_diagnostics: PX4SITLDeliveryReadinessDiagnostics | Mapping[str, Any],
    observed_topic_refs: Sequence[str] | None = None,
    adapted_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> ROSGazeboTopicObservationAdapter:
    """Build a read-only ROS/Gazebo observation adapter from readiness."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    readiness = _as_readiness(readiness_diagnostics)
    if readiness.readiness_status != "ready":
        raise ROSGazeboTopicObservationError(
            "ROS/Gazebo topic adapter requires ready diagnostics"
        )
    topics = _ordered_tuple(observed_topic_refs or DEFAULT_OBSERVATION_TOPIC_REFS)
    command_markers = _command_surface_markers(topics)
    if command_markers:
        raise ROSGazeboTopicObservationError(
            "ROS/Gazebo topic adapter rejected command-like topics: "
            + ", ".join(command_markers)
        )
    captured = _utc(adapted_at or readiness.checked_at)
    payload = {
        "readiness_id": readiness.diagnostics_id,
        "topics": topics,
        "adapted_at": captured.isoformat(),
    }
    try:
        return ROSGazeboTopicObservationAdapter(
            adapter_id=_stable_id("ros_gazebo_delivery_topic_adapter", payload),
            readiness_ref=_readiness_ref(readiness),
            observed_topic_refs=topics,
            observation_topic_refs=topics,
            adapted_at=captured,
            metadata={
                **metadata_payload,
                "artifact_only": True,
                "issue": 324,
                "parent_epic": 307,
                "topic_refs_are_observation_inputs_only": True,
                "no_ros_action_dispatch": True,
                "no_mavlink_command_surface": True,
            },
        )
    except ValidationError as exc:
        raise ROSGazeboTopicObservationError(
            f"invalid ROS/Gazebo topic observation adapter: {exc}"
        ) from exc


def build_px4_gazebo_command_surface_diagnostics(
    *,
    topic_adapter: ROSGazeboTopicObservationAdapter | Mapping[str, Any],
    checked_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboCommandSurfaceDiagnostics:
    """Build diagnostics confirming ROS/MAVLink command surfaces are closed."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    adapter = (
        topic_adapter
        if isinstance(topic_adapter, ROSGazeboTopicObservationAdapter)
        else ROSGazeboTopicObservationAdapter.model_validate(dict(topic_adapter))
    )
    captured = _utc(checked_at or adapter.adapted_at)
    payload = {
        "adapter_id": adapter.adapter_id,
        "topics": adapter.observed_topic_refs,
        "checked_at": captured.isoformat(),
    }
    try:
        return PX4GazeboCommandSurfaceDiagnostics(
            diagnostics_id=_stable_id(
                "px4_gazebo_command_surface_diagnostics", payload
            ),
            adapter_ref=f"ros_gazebo_delivery_topic_observation_adapter:{adapter.adapter_id}",
            checked_topic_refs=adapter.observed_topic_refs,
            checked_at=captured,
            metadata={
                **metadata_payload,
                "artifact_only": True,
                "issue": 325,
                "parent_epic": 307,
                "diagnostics_only": True,
                "no_socket_or_frame_send": True,
                "no_ros_action_dispatch": True,
                "no_mavlink_command_surface": True,
            },
        )
    except ValidationError as exc:
        raise ROSGazeboTopicObservationError(
            f"invalid PX4/Gazebo command surface diagnostics: {exc}"
        ) from exc


def attach_ros_gazebo_topic_observation_artifacts(
    task_id: str,
    *,
    readiness_diagnostics: PX4SITLDeliveryReadinessDiagnostics | Mapping[str, Any],
    observed_topic_refs: Sequence[str] | None = None,
    checked_at: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach ROS/Gazebo topic adapter and command surface diagnostics."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise ROSGazeboTopicObservationError(
            f"task {task_id} not found; cannot attach ROS/Gazebo topic observation"
        )
    adapter = build_ros_gazebo_topic_observation_adapter(
        readiness_diagnostics=readiness_diagnostics,
        observed_topic_refs=observed_topic_refs,
        adapted_at=checked_at,
    )
    diagnostics = build_px4_gazebo_command_surface_diagnostics(
        topic_adapter=adapter,
        checked_at=checked_at,
    )
    artifacts = {
        "ros_gazebo_delivery_topic_observation_adapter": adapter.model_dump(
            mode="json"
        ),
        "px4_gazebo_command_surface_diagnostics": diagnostics.model_dump(mode="json"),
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise ROSGazeboTopicObservationError(
            f"task {task_id} disappeared while attaching ROS/Gazebo topic observation"
        )
    return artifacts


__all__ = [
    "COMMAND_SURFACE_MARKERS",
    "DEFAULT_OBSERVATION_TOPIC_REFS",
    "PX4_GAZEBO_COMMAND_SURFACE_DIAGNOSTICS_SCHEMA_VERSION",
    "ROS_GAZEBO_TOPIC_OBSERVATION_ADAPTER_SCHEMA_VERSION",
    "PX4GazeboCommandSurfaceDiagnostics",
    "ROSGazeboTopicObservationAdapter",
    "ROSGazeboTopicObservationError",
    "attach_ros_gazebo_topic_observation_artifacts",
    "build_px4_gazebo_command_surface_diagnostics",
    "build_ros_gazebo_topic_observation_adapter",
]
