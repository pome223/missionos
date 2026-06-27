"""PX4-controlled Gazebo delivery dispatcher and runner v0.

This module is the first bounded command-dispatch layer for the PX4/Gazebo
delivery epic. It only accepts preflight artifacts that already contain
operator approval and a bounded allowlist. Dispatch results are simulation-only
artifacts scoped to PX4 SITL / Gazebo; they do not target hardware, do not grant
real-world authority, and do not contain raw MAVLink payloads.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.px4_delivery_command_preflight import (
    PX4SimulationCommandAllowlist,
    PX4SimulationCommandApproval,
    PX4SimulationCommandKind,
    PX4SimulationDeliveryCommandProposal,
    PX4SimulationMAVLinkConnectionContract,
    PX4SimulationMAVLinkTelemetryAdapter,
)
from src.runtime.task_store import TaskStore, get_task_store

PX4_SIMULATION_MAVLINK_DISPATCH_RESULT_SCHEMA_VERSION = (
    "px4_simulation_mavlink_dispatch_result.v1"
)
PX4_CONTROLLED_GAZEBO_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION = (
    "px4_controlled_gazebo_delivery_runner_result.v1"
)
DEFAULT_DELIVERY_PHASE_SEQUENCE: tuple[tuple["PX4SimulationCommandKind", str], ...] = (
    (PX4SimulationCommandKind.START_DELIVERY_MISSION, "pickup"),
    (PX4SimulationCommandKind.ADVANCE_DELIVERY_PHASE, "enroute"),
    (PX4SimulationCommandKind.ADVANCE_DELIVERY_PHASE, "dropoff"),
    (PX4SimulationCommandKind.ADVANCE_DELIVERY_PHASE, "completed"),
)


class PX4ControlledDeliveryRunnerError(RuntimeError):
    """Raised when PX4-controlled simulation dispatch cannot proceed."""


class PX4SimulationDispatchStatus(str, Enum):
    DISPATCHED = "dispatched"
    BLOCKED = "blocked"


class PX4ControlledDeliveryMissionStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"


_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "actuator",
        "hardware_target",
        "live_execution_allowed",
        "mission_upload",
        "physical_execution_invoked",
        "raw_command",
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
        raise PX4ControlledDeliveryRunnerError(
            "PX4 controlled delivery runner refused unsafe keys: "
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


def _ordered_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in values or ():
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _kind(value: PX4SimulationCommandKind | str) -> PX4SimulationCommandKind:
    if isinstance(value, PX4SimulationCommandKind):
        return value
    return PX4SimulationCommandKind(str(value))


class _SimulationDispatchBoundary(BaseModel):
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
    raw_mavlink_payload_present: Literal[False] = False
    command_payload_allowed: Literal[False] = False


class PX4SimulationMAVLinkDispatchResult(_SimulationDispatchBoundary):
    schema_version: Literal[PX4_SIMULATION_MAVLINK_DISPATCH_RESULT_SCHEMA_VERSION] = (
        PX4_SIMULATION_MAVLINK_DISPATCH_RESULT_SCHEMA_VERSION
    )
    dispatch_result_id: str
    command_kind: PX4SimulationCommandKind
    dispatch_status: PX4SimulationDispatchStatus
    connection_contract_ref: str = Field(min_length=1)
    telemetry_adapter_ref: str = Field(min_length=1)
    proposal_ref: str = Field(min_length=1)
    approval_ref: str = Field(min_length=1)
    allowlist_ref: str = Field(min_length=1)
    mission_phase_after_dispatch: str = Field(min_length=1)
    dispatched_at: datetime
    dispatch_mode: Literal["artifact_stub"] = "artifact_stub"
    mavlink_socket_opened: Literal[False] = False
    mavlink_frame_sent: Literal[False] = False
    simulation_mavlink_dispatch_performed: Literal[True] = True
    bounded_allowlist_enforced: Literal[True] = True
    operator_approval_performed: Literal[True] = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("dispatched_at", mode="before")
    @classmethod
    def _coerce_dispatched_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "PX4SimulationMAVLinkDispatchResult":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class PX4ControlledGazeboDeliveryRunnerResult(_SimulationDispatchBoundary):
    schema_version: Literal[
        PX4_CONTROLLED_GAZEBO_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION
    ] = PX4_CONTROLLED_GAZEBO_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION
    runner_result_id: str
    final_status: PX4ControlledDeliveryMissionStatus
    dispatch_result_refs: tuple[str, ...]
    observed_delivery_phases: tuple[str, ...]
    completed_at: datetime
    simulation_mavlink_dispatch_performed: Literal[True] = True
    bounded_allowlist_enforced: Literal[True] = True
    operator_approval_performed: Literal[True] = True
    pickup_reached: bool
    enroute_reached: bool
    dropoff_reached: bool
    completed_reached: bool
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("completed_at", mode="before")
    @classmethod
    def _coerce_completed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "PX4ControlledGazeboDeliveryRunnerResult":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


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


def _allowlist_ref(allowlist: PX4SimulationCommandAllowlist) -> str:
    return f"px4_simulation_command_allowlist:{allowlist.allowlist_id}"


def _normalize_preflight_artifacts(
    artifacts: Mapping[str, Any],
) -> tuple[
    PX4SimulationMAVLinkConnectionContract,
    PX4SimulationMAVLinkTelemetryAdapter,
    PX4SimulationDeliveryCommandProposal,
    PX4SimulationCommandApproval,
    PX4SimulationCommandAllowlist,
]:
    try:
        connection = PX4SimulationMAVLinkConnectionContract.model_validate(
            dict(artifacts["px4_simulation_mavlink_connection_contract"])
        )
        adapter = PX4SimulationMAVLinkTelemetryAdapter.model_validate(
            dict(artifacts["px4_simulation_mavlink_telemetry_adapter"])
        )
        proposal = PX4SimulationDeliveryCommandProposal.model_validate(
            dict(artifacts["px4_simulation_delivery_command_proposal"])
        )
        approval = PX4SimulationCommandApproval.model_validate(
            dict(artifacts["px4_simulation_command_approval"])
        )
        allowlist = PX4SimulationCommandAllowlist.model_validate(
            dict(artifacts["px4_simulation_command_allowlist"])
        )
    except KeyError as exc:
        raise PX4ControlledDeliveryRunnerError(
            f"missing preflight artifact: {exc.args[0]}"
        ) from exc
    return connection, adapter, proposal, approval, allowlist


def _validate_preflight_chain(
    *,
    connection: PX4SimulationMAVLinkConnectionContract,
    adapter: PX4SimulationMAVLinkTelemetryAdapter,
    proposal: PX4SimulationDeliveryCommandProposal,
    approval: PX4SimulationCommandApproval,
    allowlist: PX4SimulationCommandAllowlist,
) -> None:
    if connection.connection_opened is not False:
        raise PX4ControlledDeliveryRunnerError("connection must not already be open")
    if adapter.connection_contract_ref != _connection_ref(connection):
        raise PX4ControlledDeliveryRunnerError("adapter connection mismatch")
    if proposal.connection_contract_ref != _connection_ref(connection):
        raise PX4ControlledDeliveryRunnerError("proposal connection mismatch")
    if proposal.telemetry_adapter_ref != _adapter_ref(adapter):
        raise PX4ControlledDeliveryRunnerError("proposal adapter mismatch")
    if approval.proposal_ref != _proposal_ref(proposal):
        raise PX4ControlledDeliveryRunnerError("approval proposal mismatch")
    if approval.operator_approval_performed is not True:
        raise PX4ControlledDeliveryRunnerError(
            "dispatcher requires operator_approval_performed=true"
        )
    if allowlist.proposal_ref != _proposal_ref(proposal):
        raise PX4ControlledDeliveryRunnerError("allowlist proposal mismatch")
    if allowlist.approval_ref != _approval_ref(approval):
        raise PX4ControlledDeliveryRunnerError("allowlist approval mismatch")


def build_px4_simulation_mavlink_dispatch_result(
    *,
    preflight_artifacts: Mapping[str, Any],
    command_kind: PX4SimulationCommandKind | str,
    mission_phase_after_dispatch: str,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4SimulationMAVLinkDispatchResult:
    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    connection, adapter, proposal, approval, allowlist = _normalize_preflight_artifacts(
        preflight_artifacts
    )
    _validate_preflight_chain(
        connection=connection,
        adapter=adapter,
        proposal=proposal,
        approval=approval,
        allowlist=allowlist,
    )
    kind = _kind(command_kind)
    if kind not in allowlist.allowed_command_kinds:
        raise PX4ControlledDeliveryRunnerError(
            f"command kind is not allowlisted: {kind}"
        )
    dispatched_at = _utc(now)
    payload = {
        "connection_contract_id": connection.connection_contract_id,
        "adapter_id": adapter.adapter_id,
        "proposal_id": proposal.proposal_id,
        "approval_id": approval.approval_id,
        "allowlist_id": allowlist.allowlist_id,
        "command_kind": kind.value,
        "mission_phase_after_dispatch": mission_phase_after_dispatch,
    }
    return PX4SimulationMAVLinkDispatchResult(
        dispatch_result_id=_stable_id(
            "px4_simulation_mavlink_dispatch_result", payload
        ),
        command_kind=kind,
        dispatch_status=PX4SimulationDispatchStatus.DISPATCHED,
        connection_contract_ref=_connection_ref(connection),
        telemetry_adapter_ref=_adapter_ref(adapter),
        proposal_ref=_proposal_ref(proposal),
        approval_ref=_approval_ref(approval),
        allowlist_ref=_allowlist_ref(allowlist),
        mission_phase_after_dispatch=str(mission_phase_after_dispatch),
        dispatched_at=dispatched_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "issue": 315,
            "parent_epic": 307,
            "simulation_only_mavlink_dispatch": True,
            "no_raw_mavlink_payload": True,
            "no_hardware_target": True,
        },
    )


def build_px4_controlled_gazebo_delivery_runner_result(
    *,
    dispatch_results: Sequence[PX4SimulationMAVLinkDispatchResult | Mapping[str, Any]],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4ControlledGazeboDeliveryRunnerResult:
    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    normalized = [
        (
            item
            if isinstance(item, PX4SimulationMAVLinkDispatchResult)
            else PX4SimulationMAVLinkDispatchResult.model_validate(dict(item))
        )
        for item in dispatch_results
    ]
    if not normalized:
        raise PX4ControlledDeliveryRunnerError(
            "at least one dispatch result is required"
        )
    phases = _ordered_tuple([item.mission_phase_after_dispatch for item in normalized])
    required = ("pickup", "enroute", "dropoff", "completed")
    missing = [phase for phase in required if phase not in phases]
    final_status = (
        PX4ControlledDeliveryMissionStatus.COMPLETED
        if not missing
        else PX4ControlledDeliveryMissionStatus.BLOCKED
    )
    refs = tuple(
        f"px4_simulation_mavlink_dispatch_result:{item.dispatch_result_id}"
        for item in normalized
    )
    completed_at = _utc(now)
    payload = {
        "dispatch_result_refs": refs,
        "observed_delivery_phases": phases,
        "final_status": final_status.value,
    }
    return PX4ControlledGazeboDeliveryRunnerResult(
        runner_result_id=_stable_id(
            "px4_controlled_gazebo_delivery_runner_result", payload
        ),
        final_status=final_status,
        dispatch_result_refs=refs,
        observed_delivery_phases=phases,
        completed_at=completed_at,
        pickup_reached="pickup" in phases,
        enroute_reached="enroute" in phases,
        dropoff_reached="dropoff" in phases,
        completed_reached="completed" in phases,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "issue": 316,
            "parent_epic": 307,
            "required_phases": list(required),
            "missing_phases": missing,
        },
    )


def run_px4_controlled_gazebo_delivery_mission_v0_task(
    task_id: str,
    *,
    preflight_artifacts: Mapping[str, Any],
    delivery_phase_sequence: (
        Sequence[tuple[PX4SimulationCommandKind | str, str]] | None
    ) = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4ControlledDeliveryRunnerError(
            f"task {task_id} not found; cannot run PX4 controlled delivery"
        )
    base_time = _utc(now)
    sequence = tuple(
        delivery_phase_sequence
        if delivery_phase_sequence is not None
        else DEFAULT_DELIVERY_PHASE_SEQUENCE
    )
    dispatch_results = [
        build_px4_simulation_mavlink_dispatch_result(
            preflight_artifacts=preflight_artifacts,
            command_kind=kind,
            mission_phase_after_dispatch=phase,
            now=base_time,
        )
        for kind, phase in sequence
    ]
    runner_result = build_px4_controlled_gazebo_delivery_runner_result(
        dispatch_results=dispatch_results,
        now=base_time,
    )
    updated = store.update(
        task_id,
        status=runner_result.final_status.value,
        artifacts={
            "px4_simulation_mavlink_dispatch_results": [
                item.model_dump(mode="json") for item in dispatch_results
            ],
            "px4_controlled_gazebo_delivery_runner_result": runner_result.model_dump(
                mode="json"
            ),
        },
        ended_at=time.time(),
    )
    if updated is None:
        raise PX4ControlledDeliveryRunnerError(
            f"task {task_id} disappeared while running PX4 controlled delivery"
        )
    return updated


__all__ = [
    "PX4_CONTROLLED_GAZEBO_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION",
    "PX4_SIMULATION_MAVLINK_DISPATCH_RESULT_SCHEMA_VERSION",
    "PX4ControlledDeliveryMissionStatus",
    "PX4ControlledDeliveryRunnerError",
    "PX4ControlledGazeboDeliveryRunnerResult",
    "PX4SimulationDispatchStatus",
    "PX4SimulationMAVLinkDispatchResult",
    "build_px4_controlled_gazebo_delivery_runner_result",
    "build_px4_simulation_mavlink_dispatch_result",
    "run_px4_controlled_gazebo_delivery_mission_v0_task",
]
