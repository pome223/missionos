"""Pre-dispatch PX4/Gazebo delivery command artifacts.

This module covers the observation-first bridge between PX4 SITL telemetry and
future bounded simulation-only command dispatch. It defines connection,
telemetry-adapter, proposal, approval, and allowlist artifacts, but it does not
open a socket, send MAVLink/ROS commands, upload PX4 missions, mutate Gazebo, or
target hardware.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.px4_gazebo_delivery_world_profile import (
    PX4GazeboDeliveryWorldProfile,
    build_px4_gazebo_delivery_world_profile,
)
from src.runtime.px4_sitl_delivery_observation import PX4SitlDeliveryObservation
from src.runtime.task_store import TaskStore, get_task_store

PX4_SIMULATION_MAVLINK_CONNECTION_CONTRACT_SCHEMA_VERSION = (
    "px4_simulation_mavlink_connection_contract.v1"
)
PX4_SIMULATION_MAVLINK_TELEMETRY_ADAPTER_SCHEMA_VERSION = (
    "px4_simulation_mavlink_telemetry_adapter.v1"
)
PX4_SIMULATION_DELIVERY_COMMAND_PROPOSAL_SCHEMA_VERSION = (
    "px4_simulation_delivery_command_proposal.v1"
)
PX4_SIMULATION_COMMAND_APPROVAL_SCHEMA_VERSION = "px4_simulation_command_approval.v1"
PX4_SIMULATION_COMMAND_ALLOWLIST_SCHEMA_VERSION = "px4_simulation_command_allowlist.v1"


class PX4SimulationCommandPreflightError(RuntimeError):
    """Raised when pre-dispatch PX4 simulation command artifacts are unsafe."""


class PX4SimulationCommandKind(str, Enum):
    START_DELIVERY_MISSION = "start_delivery_mission"
    ADVANCE_DELIVERY_PHASE = "advance_delivery_phase"


DEFAULT_ALLOWED_COMMAND_KINDS: tuple[PX4SimulationCommandKind, ...] = (
    PX4SimulationCommandKind.START_DELIVERY_MISSION,
    PX4SimulationCommandKind.ADVANCE_DELIVERY_PHASE,
)


_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "actuator",
        "actuator_execution_allowed",
        "command_payload",
        "command_payload_allowed",
        "dispatch_implementation_present",
        "execute",
        "gazebo_mutation",
        "hardware_target",
        "live_execution_allowed",
        "mavlink_command",
        "mavlink_dispatch_allowed",
        "mission_item",
        "mission_upload",
        "physical_execution_invoked",
        "position_setpoint",
        "raw_command",
        "ros_action",
        "ros_dispatch_allowed",
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
        raise PX4SimulationCommandPreflightError(
            "PX4 simulation command preflight refused command-like keys: "
            + ", ".join(sorted(findings))
        )


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _text_tuple(values: Sequence[str] | str | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        candidate = values.strip()
        return (candidate,) if candidate else ()
    return tuple(str(item).strip() for item in values if str(item).strip())


def _command_kind_tuple(
    values: Sequence[PX4SimulationCommandKind | str] | None,
) -> tuple[PX4SimulationCommandKind, ...]:
    if values is None:
        return ()
    seen: set[PX4SimulationCommandKind] = set()
    out: list[PX4SimulationCommandKind] = []
    for item in values:
        kind = (
            item
            if isinstance(item, PX4SimulationCommandKind)
            else PX4SimulationCommandKind(str(item))
        )
        if kind not in seen:
            seen.add(kind)
            out.append(kind)
    return tuple(out)


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


def _to_profile(
    value: PX4GazeboDeliveryWorldProfile | Mapping[str, Any] | None,
) -> PX4GazeboDeliveryWorldProfile:
    if isinstance(value, PX4GazeboDeliveryWorldProfile):
        return value
    if value is not None:
        return PX4GazeboDeliveryWorldProfile.model_validate(dict(value))
    return build_px4_gazebo_delivery_world_profile()


def _to_observation(
    value: PX4SitlDeliveryObservation | Mapping[str, Any],
) -> PX4SitlDeliveryObservation:
    if isinstance(value, PX4SitlDeliveryObservation):
        return value
    return PX4SitlDeliveryObservation.model_validate(dict(value))


class _SafetyBoundaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    hardware_target_allowed: Literal[False] = False
    real_world_authority_granted: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False


class PX4SimulationMAVLinkConnectionContract(_SafetyBoundaryModel):
    schema_version: Literal[
        PX4_SIMULATION_MAVLINK_CONNECTION_CONTRACT_SCHEMA_VERSION
    ] = PX4_SIMULATION_MAVLINK_CONNECTION_CONTRACT_SCHEMA_VERSION
    connection_contract_id: str
    profile_ref: str = Field(min_length=1)
    transport: Literal["udp_loopback_px4_sitl"] = "udp_loopback_px4_sitl"
    endpoint_host: Literal["127.0.0.1"] = "127.0.0.1"
    endpoint_port: int = Field(default=14540, ge=1, le=65535)
    system_id: int = Field(default=1, ge=1, le=255)
    component_id: int = Field(default=1, ge=1, le=255)
    telemetry_observation_only: Literal[True] = True
    connection_opened: Literal[False] = False
    network_ports_exposed: Literal[False] = False
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "PX4SimulationMAVLinkConnectionContract":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class PX4SimulationMAVLinkTelemetryAdapter(_SafetyBoundaryModel):
    schema_version: Literal[PX4_SIMULATION_MAVLINK_TELEMETRY_ADAPTER_SCHEMA_VERSION] = (
        PX4_SIMULATION_MAVLINK_TELEMETRY_ADAPTER_SCHEMA_VERSION
    )
    adapter_id: str
    connection_contract_ref: str = Field(min_length=1)
    observation_ref: str = Field(min_length=1)
    telemetry_ref: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    vehicle_id: str = Field(min_length=1)
    measurement_keys: tuple[str, ...] = Field(min_length=1)
    adapter_mode: Literal["telemetry_observation_only"] = "telemetry_observation_only"
    command_frames_observed: Literal[0] = 0
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("measurement_keys", mode="before")
    @classmethod
    def _strip_keys(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "PX4SimulationMAVLinkTelemetryAdapter":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class PX4SimulationDeliveryCommandProposal(_SafetyBoundaryModel):
    schema_version: Literal[PX4_SIMULATION_DELIVERY_COMMAND_PROPOSAL_SCHEMA_VERSION] = (
        PX4_SIMULATION_DELIVERY_COMMAND_PROPOSAL_SCHEMA_VERSION
    )
    proposal_id: str
    connection_contract_ref: str = Field(min_length=1)
    telemetry_adapter_ref: str = Field(min_length=1)
    observation_ref: str = Field(min_length=1)
    proposed_command_kinds: tuple[PX4SimulationCommandKind, ...] = Field(min_length=1)
    proposal_status: Literal["proposed"] = "proposed"
    operator_approval_required: Literal[True] = True
    dry_run_required_before_dispatch: Literal[True] = True
    raw_command_payload_present: Literal[False] = False
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("proposed_command_kinds", mode="before")
    @classmethod
    def _coerce_command_kinds(cls, value: Any) -> tuple[PX4SimulationCommandKind, ...]:
        return _command_kind_tuple(value)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "PX4SimulationDeliveryCommandProposal":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class PX4SimulationCommandApproval(_SafetyBoundaryModel):
    schema_version: Literal[PX4_SIMULATION_COMMAND_APPROVAL_SCHEMA_VERSION] = (
        PX4_SIMULATION_COMMAND_APPROVAL_SCHEMA_VERSION
    )
    approval_id: str
    proposal_ref: str = Field(min_length=1)
    approved_command_kinds: tuple[PX4SimulationCommandKind, ...] = Field(min_length=1)
    operator_approval_required: Literal[True] = True
    operator_approval_performed: bool
    approved_by: str = "operator"
    approved_at: datetime
    simulation_command_authority_only: Literal[True] = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("approved_command_kinds", mode="before")
    @classmethod
    def _coerce_command_kinds(cls, value: Any) -> tuple[PX4SimulationCommandKind, ...]:
        return _command_kind_tuple(value)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "PX4SimulationCommandApproval":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class PX4SimulationCommandAllowlist(_SafetyBoundaryModel):
    schema_version: Literal[PX4_SIMULATION_COMMAND_ALLOWLIST_SCHEMA_VERSION] = (
        PX4_SIMULATION_COMMAND_ALLOWLIST_SCHEMA_VERSION
    )
    allowlist_id: str
    approval_ref: str = Field(min_length=1)
    proposal_ref: str = Field(min_length=1)
    allowed_command_kinds: tuple[PX4SimulationCommandKind, ...] = Field(min_length=1)
    allowed_protocols: tuple[Literal["mavlink", "ros_gazebo"], ...] = (
        "mavlink",
        "ros_gazebo",
    )
    denied_command_families: tuple[str, ...] = (
        "hardware_target",
        "physical_execution",
        "unbounded_mission_upload",
        "raw_setpoint",
        "actuator_output",
    )
    operator_approval_required: Literal[True] = True
    bounded_dispatch_only: Literal[True] = True
    raw_command_payload_allowed: Literal[False] = False
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("allowed_command_kinds", mode="before")
    @classmethod
    def _coerce_command_kinds(cls, value: Any) -> tuple[PX4SimulationCommandKind, ...]:
        return _command_kind_tuple(value)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "PX4SimulationCommandAllowlist":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


def _profile_ref(profile: PX4GazeboDeliveryWorldProfile) -> str:
    return f"px4_gazebo_delivery_world_profile:{profile.profile_id}"


def _connection_ref(connection: PX4SimulationMAVLinkConnectionContract) -> str:
    return (
        "px4_simulation_mavlink_connection_contract:"
        f"{connection.connection_contract_id}"
    )


def _adapter_ref(adapter: PX4SimulationMAVLinkTelemetryAdapter) -> str:
    return f"px4_simulation_mavlink_telemetry_adapter:{adapter.adapter_id}"


def _proposal_ref(proposal: PX4SimulationDeliveryCommandProposal) -> str:
    return f"px4_simulation_delivery_command_proposal:{proposal.proposal_id}"


def _approval_ref(approval: PX4SimulationCommandApproval) -> str:
    return f"px4_simulation_command_approval:{approval.approval_id}"


def build_px4_simulation_mavlink_connection_contract(
    *,
    profile: PX4GazeboDeliveryWorldProfile | Mapping[str, Any] | None = None,
    endpoint_port: int = 14540,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4SimulationMAVLinkConnectionContract:
    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    resolved_profile = _to_profile(profile)
    created_at = _utc(now)
    payload = {
        "profile_id": resolved_profile.profile_id,
        "endpoint_host": "127.0.0.1",
        "endpoint_port": endpoint_port,
        "transport": "udp_loopback_px4_sitl",
    }
    return PX4SimulationMAVLinkConnectionContract(
        connection_contract_id=_stable_id(
            "px4_simulation_mavlink_connection_contract", payload
        ),
        profile_ref=_profile_ref(resolved_profile),
        endpoint_port=endpoint_port,
        created_at=created_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "issue": 310,
            "parent_epic": 307,
            "connection_descriptor_only": True,
            "no_socket_opened": True,
            "no_hardware_target": True,
        },
    )


def build_px4_simulation_mavlink_telemetry_adapter(
    *,
    connection_contract: PX4SimulationMAVLinkConnectionContract | Mapping[str, Any],
    observation: PX4SitlDeliveryObservation | Mapping[str, Any],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4SimulationMAVLinkTelemetryAdapter:
    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    connection = (
        connection_contract
        if isinstance(connection_contract, PX4SimulationMAVLinkConnectionContract)
        else PX4SimulationMAVLinkConnectionContract.model_validate(
            dict(connection_contract)
        )
    )
    resolved_observation = _to_observation(observation)
    if connection.profile_ref != resolved_observation.profile_ref:
        raise PX4SimulationCommandPreflightError(
            "MAVLink telemetry adapter profile mismatch"
        )
    if resolved_observation.measurements.get("px4_sitl_started") is not True:
        raise PX4SimulationCommandPreflightError(
            "MAVLink telemetry adapter requires px4_sitl_started=true"
        )
    created_at = _utc(now)
    payload = {
        "connection_contract_id": connection.connection_contract_id,
        "observation_id": resolved_observation.observation_id,
        "telemetry_ref": resolved_observation.telemetry_ref,
    }
    return PX4SimulationMAVLinkTelemetryAdapter(
        adapter_id=_stable_id("px4_simulation_mavlink_telemetry_adapter", payload),
        connection_contract_ref=_connection_ref(connection),
        observation_ref=(
            f"px4_sitl_delivery_observation:{resolved_observation.observation_id}"
        ),
        telemetry_ref=resolved_observation.telemetry_ref,
        source_kind=resolved_observation.source_kind,
        vehicle_id=resolved_observation.vehicle_id,
        measurement_keys=resolved_observation.measurement_keys,
        created_at=created_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "issue": 311,
            "parent_epic": 307,
            "adapter_descriptor_only": True,
            "no_mavlink_frames_sent": True,
        },
    )


def build_px4_simulation_delivery_command_proposal(
    *,
    connection_contract: PX4SimulationMAVLinkConnectionContract | Mapping[str, Any],
    telemetry_adapter: PX4SimulationMAVLinkTelemetryAdapter | Mapping[str, Any],
    observation: PX4SitlDeliveryObservation | Mapping[str, Any],
    proposed_command_kinds: Sequence[
        PX4SimulationCommandKind | str
    ] = DEFAULT_ALLOWED_COMMAND_KINDS,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4SimulationDeliveryCommandProposal:
    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    connection = (
        connection_contract
        if isinstance(connection_contract, PX4SimulationMAVLinkConnectionContract)
        else PX4SimulationMAVLinkConnectionContract.model_validate(
            dict(connection_contract)
        )
    )
    adapter = (
        telemetry_adapter
        if isinstance(telemetry_adapter, PX4SimulationMAVLinkTelemetryAdapter)
        else PX4SimulationMAVLinkTelemetryAdapter.model_validate(
            dict(telemetry_adapter)
        )
    )
    resolved_observation = _to_observation(observation)
    if adapter.connection_contract_ref != _connection_ref(connection):
        raise PX4SimulationCommandPreflightError("proposal connection mismatch")
    if adapter.observation_ref != (
        f"px4_sitl_delivery_observation:{resolved_observation.observation_id}"
    ):
        raise PX4SimulationCommandPreflightError("proposal observation mismatch")
    kinds = _command_kind_tuple(proposed_command_kinds)
    created_at = _utc(now)
    payload = {
        "connection_contract_id": connection.connection_contract_id,
        "adapter_id": adapter.adapter_id,
        "observation_id": resolved_observation.observation_id,
        "proposed_command_kinds": [kind.value for kind in kinds],
    }
    return PX4SimulationDeliveryCommandProposal(
        proposal_id=_stable_id("px4_simulation_delivery_command_proposal", payload),
        connection_contract_ref=_connection_ref(connection),
        telemetry_adapter_ref=_adapter_ref(adapter),
        observation_ref=(
            f"px4_sitl_delivery_observation:{resolved_observation.observation_id}"
        ),
        proposed_command_kinds=kinds,
        created_at=created_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "issue": 312,
            "parent_epic": 307,
            "proposal_only": True,
            "no_dispatch_before_approval": True,
        },
    )


def build_px4_simulation_command_approval(
    *,
    proposal: PX4SimulationDeliveryCommandProposal | Mapping[str, Any],
    operator_approval_performed: bool,
    approved_by: str = "operator",
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4SimulationCommandApproval:
    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    resolved_proposal = (
        proposal
        if isinstance(proposal, PX4SimulationDeliveryCommandProposal)
        else PX4SimulationDeliveryCommandProposal.model_validate(dict(proposal))
    )
    approved_at = _utc(now)
    payload = {
        "proposal_id": resolved_proposal.proposal_id,
        "approved_command_kinds": [
            kind.value for kind in resolved_proposal.proposed_command_kinds
        ],
        "operator_approval_performed": operator_approval_performed,
    }
    return PX4SimulationCommandApproval(
        approval_id=_stable_id("px4_simulation_command_approval", payload),
        proposal_ref=_proposal_ref(resolved_proposal),
        approved_command_kinds=resolved_proposal.proposed_command_kinds,
        operator_approval_performed=operator_approval_performed,
        approved_by=approved_by or "operator",
        approved_at=approved_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "issue": 313,
            "parent_epic": 307,
            "approval_scope": "simulation_only_px4_sitl_delivery_commands",
            "real_world_authority_granted": False,
        },
    )


def build_px4_simulation_command_allowlist(
    *,
    proposal: PX4SimulationDeliveryCommandProposal | Mapping[str, Any],
    approval: PX4SimulationCommandApproval | Mapping[str, Any],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4SimulationCommandAllowlist:
    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    resolved_proposal = (
        proposal
        if isinstance(proposal, PX4SimulationDeliveryCommandProposal)
        else PX4SimulationDeliveryCommandProposal.model_validate(dict(proposal))
    )
    resolved_approval = (
        approval
        if isinstance(approval, PX4SimulationCommandApproval)
        else PX4SimulationCommandApproval.model_validate(dict(approval))
    )
    if resolved_approval.proposal_ref != _proposal_ref(resolved_proposal):
        raise PX4SimulationCommandPreflightError("allowlist proposal mismatch")
    if resolved_approval.operator_approval_performed is not True:
        raise PX4SimulationCommandPreflightError(
            "allowlist requires operator_approval_performed=true"
        )
    if tuple(resolved_approval.approved_command_kinds) != tuple(
        resolved_proposal.proposed_command_kinds
    ):
        raise PX4SimulationCommandPreflightError("allowlist command kind mismatch")
    created_at = _utc(now)
    payload = {
        "proposal_id": resolved_proposal.proposal_id,
        "approval_id": resolved_approval.approval_id,
        "allowed_command_kinds": [
            kind.value for kind in resolved_approval.approved_command_kinds
        ],
    }
    return PX4SimulationCommandAllowlist(
        allowlist_id=_stable_id("px4_simulation_command_allowlist", payload),
        approval_ref=_approval_ref(resolved_approval),
        proposal_ref=_proposal_ref(resolved_proposal),
        allowed_command_kinds=resolved_approval.approved_command_kinds,
        created_at=created_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "issue": 314,
            "parent_epic": 307,
            "allowlist_only": True,
            "allowed_protocols_are_declarative_candidates": True,
            "dispatch_requires_future_dispatcher": True,
        },
    )


def build_px4_simulation_command_preflight_artifacts(
    *,
    profile: PX4GazeboDeliveryWorldProfile | Mapping[str, Any] | None,
    observation: PX4SitlDeliveryObservation | Mapping[str, Any],
    operator_approval_performed: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    resolved_profile = _to_profile(profile)
    resolved_observation = _to_observation(observation)
    connection = build_px4_simulation_mavlink_connection_contract(
        profile=resolved_profile,
        now=now,
    )
    adapter = build_px4_simulation_mavlink_telemetry_adapter(
        connection_contract=connection,
        observation=resolved_observation,
        now=now,
    )
    proposal = build_px4_simulation_delivery_command_proposal(
        connection_contract=connection,
        telemetry_adapter=adapter,
        observation=resolved_observation,
        now=now,
    )
    approval = build_px4_simulation_command_approval(
        proposal=proposal,
        operator_approval_performed=operator_approval_performed,
        now=now,
    )
    allowlist = build_px4_simulation_command_allowlist(
        proposal=proposal,
        approval=approval,
        now=now,
    )
    return {
        "px4_simulation_mavlink_connection_contract": connection.model_dump(
            mode="json"
        ),
        "px4_simulation_mavlink_telemetry_adapter": adapter.model_dump(mode="json"),
        "px4_simulation_delivery_command_proposal": proposal.model_dump(mode="json"),
        "px4_simulation_command_approval": approval.model_dump(mode="json"),
        "px4_simulation_command_allowlist": allowlist.model_dump(mode="json"),
    }


def attach_px4_simulation_command_preflight_artifacts(
    task_id: str,
    *,
    profile: PX4GazeboDeliveryWorldProfile | Mapping[str, Any] | None,
    observation: PX4SitlDeliveryObservation | Mapping[str, Any],
    operator_approval_performed: bool,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4SimulationCommandPreflightError(
            f"task {task_id} not found; cannot attach PX4 command preflight artifacts"
        )
    artifacts = build_px4_simulation_command_preflight_artifacts(
        profile=profile,
        observation=observation,
        operator_approval_performed=operator_approval_performed,
        now=now,
    )
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise PX4SimulationCommandPreflightError(
            f"task {task_id} disappeared while attaching PX4 command preflight artifacts"
        )
    return artifacts


__all__ = [
    "DEFAULT_ALLOWED_COMMAND_KINDS",
    "PX4_SIMULATION_COMMAND_ALLOWLIST_SCHEMA_VERSION",
    "PX4_SIMULATION_COMMAND_APPROVAL_SCHEMA_VERSION",
    "PX4_SIMULATION_DELIVERY_COMMAND_PROPOSAL_SCHEMA_VERSION",
    "PX4_SIMULATION_MAVLINK_CONNECTION_CONTRACT_SCHEMA_VERSION",
    "PX4_SIMULATION_MAVLINK_TELEMETRY_ADAPTER_SCHEMA_VERSION",
    "PX4SimulationCommandAllowlist",
    "PX4SimulationCommandApproval",
    "PX4SimulationCommandKind",
    "PX4SimulationCommandPreflightError",
    "PX4SimulationDeliveryCommandProposal",
    "PX4SimulationMAVLinkConnectionContract",
    "PX4SimulationMAVLinkTelemetryAdapter",
    "attach_px4_simulation_command_preflight_artifacts",
    "build_px4_simulation_command_allowlist",
    "build_px4_simulation_command_approval",
    "build_px4_simulation_command_preflight_artifacts",
    "build_px4_simulation_delivery_command_proposal",
    "build_px4_simulation_mavlink_connection_contract",
    "build_px4_simulation_mavlink_telemetry_adapter",
]
