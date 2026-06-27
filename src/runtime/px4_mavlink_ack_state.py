"""ACK/state wait artifacts for PX4/Gazebo live MAVLink dispatch."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import socket
import struct
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.px4_gazebo_coupled_delivery import (
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_TAKEOFF,
    PX4GazeboCoupledCommandAllowlist,
    PX4GazeboCoupledCommandApproval,
)
from src.runtime.px4_live_mavlink_dispatcher import (
    DEFAULT_PX4_TARGET_COMPONENT,
    DEFAULT_PX4_TARGET_SYSTEM,
    PX4GazeboLiveMAVLinkDispatchResult,
    build_px4_gazebo_live_mavlink_dispatch_result,
    encode_px4_gazebo_command_long,
)
from src.runtime.px4_real_mavlink_transport import (
    MAVLINK_MSG_ID_COMMAND_ACK,
    decode_mavlink2_frame,
    encode_mavlink2_frame,
    encode_mavlink2_heartbeat,
)
from src.runtime.task_store import TaskStore, get_task_store

PX4_GAZEBO_MAVLINK_COMMAND_ACK_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_mavlink_command_ack_result.v1"
)
PX4_GAZEBO_MAVLINK_STATE_WAIT_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_mavlink_state_wait_result.v1"
)
PX4_GAZEBO_RUNTIME_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_runtime_delivery_runner_result.v1"
)

MAV_RESULT_ACCEPTED = 0
MAV_RESULT_TEMPORARILY_REJECTED = 1
MAV_RESULT_DENIED = 2
MAV_RESULT_UNSUPPORTED = 3
MAV_RESULT_FAILED = 4
MAV_RESULT_IN_PROGRESS = 5
MAV_RESULT_CANCELLED = 6

_ACK_RESULT_NAMES = {
    MAV_RESULT_ACCEPTED: "ACCEPTED",
    MAV_RESULT_TEMPORARILY_REJECTED: "TEMPORARILY_REJECTED",
    MAV_RESULT_DENIED: "DENIED",
    MAV_RESULT_UNSUPPORTED: "UNSUPPORTED",
    MAV_RESULT_FAILED: "FAILED",
    MAV_RESULT_IN_PROGRESS: "IN_PROGRESS",
    MAV_RESULT_CANCELLED: "CANCELLED",
}
_COMMAND_NAMES = {
    MAV_CMD_COMPONENT_ARM_DISARM: "MAV_CMD_COMPONENT_ARM_DISARM",
    MAV_CMD_NAV_TAKEOFF: "MAV_CMD_NAV_TAKEOFF",
    MAV_CMD_NAV_LAND: "MAV_CMD_NAV_LAND",
}
_STATE_MARKERS = {
    "armed": ("Armed by external command",),
    "airborne": ("Takeoff detected",),
    "landing": ("Landing detected",),
    "landed_disarmed": ("Disarmed by landing",),
}
_REQUIRED_PHASES = ("pickup", "enroute", "dropoff", "completed")
_REQUIRED_COMMAND_IDS = (
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_NAV_TAKEOFF,
    MAV_CMD_NAV_LAND,
)


class PX4MAVLinkAckStateError(RuntimeError):
    """Raised when ACK/state evidence is inconsistent or unsafe."""


class PX4MAVLinkCommandAckStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class PX4MAVLinkStateWaitStatus(str, Enum):
    OBSERVED = "observed"
    TIMEOUT = "timeout"


class PX4RuntimeDeliveryRunnerStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"


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


def _dispatch_ref(dispatch: PX4GazeboLiveMAVLinkDispatchResult) -> str:
    return f"px4_gazebo_live_mavlink_dispatch_result:{dispatch.dispatch_result_id}"


def _ack_ref(ack: "PX4GazeboMAVLinkCommandAckResult") -> str:
    return f"px4_gazebo_mavlink_command_ack_result:{ack.ack_result_id}"


def _state_ref(state: "PX4GazeboMAVLinkStateWaitResult") -> str:
    return f"px4_gazebo_mavlink_state_wait_result:{state.state_wait_id}"


def encode_mavlink2_command_ack(
    *,
    command_id: int,
    result_code: int = MAV_RESULT_ACCEPTED,
    target_system: int = DEFAULT_PX4_TARGET_SYSTEM,
    target_component: int = DEFAULT_PX4_TARGET_COMPONENT,
    sequence: int = 42,
    system_id: int = DEFAULT_PX4_TARGET_SYSTEM,
    component_id: int = DEFAULT_PX4_TARGET_COMPONENT,
) -> bytes:
    payload = struct.pack(
        "<HBBiBB",
        int(command_id),
        int(result_code),
        0,
        0,
        int(target_system),
        int(target_component),
    )
    return encode_mavlink2_frame(
        msg_id=MAVLINK_MSG_ID_COMMAND_ACK,
        payload=payload,
        sequence=sequence,
        system_id=system_id,
        component_id=component_id,
    )


def decode_mavlink2_command_ack(frame: bytes) -> dict[str, int | str]:
    decoded = decode_mavlink2_frame(frame)
    if decoded["msg_id"] != MAVLINK_MSG_ID_COMMAND_ACK:
        raise PX4MAVLinkAckStateError("MAVLink frame is not COMMAND_ACK")
    payload = decoded["payload"]
    if len(payload) < 10:
        raise PX4MAVLinkAckStateError("COMMAND_ACK payload is incomplete")
    command_id, result_code, _progress, _param2, target_system, target_component = (
        struct.unpack("<HBBiBB", payload[:10])
    )
    return {
        "command_id": int(command_id),
        "result_code": int(result_code),
        "result_name": _ACK_RESULT_NAMES.get(int(result_code), "UNKNOWN"),
        "target_system": int(target_system),
        "target_component": int(target_component),
    }


class _AckStateSafetyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[True] = True
    bounded_allowlist_enforced: Literal[True] = True
    simulation_mavlink_dispatch_allowed: Literal[True] = True
    physical_actuator_execution_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_world_authority_granted: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    arbitrary_gazebo_mutation_allowed: Literal[False] = False
    retry_attempted: Literal[False] = False
    stronger_execution_attempted: Literal[False] = False


class PX4GazeboMAVLinkCommandAckResult(_AckStateSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_MAVLINK_COMMAND_ACK_RESULT_SCHEMA_VERSION] = (
        PX4_GAZEBO_MAVLINK_COMMAND_ACK_RESULT_SCHEMA_VERSION
    )
    ack_result_id: str
    dispatch_result_ref: str = Field(min_length=1)
    command_id: int
    command_name: str = Field(min_length=1)
    ack_status: PX4MAVLinkCommandAckStatus
    ack_received: bool
    ack_result_code: int | None = None
    ack_result_name: str | None = None
    target_system: Literal[DEFAULT_PX4_TARGET_SYSTEM] = DEFAULT_PX4_TARGET_SYSTEM
    target_component: Literal[DEFAULT_PX4_TARGET_COMPONENT] = (
        DEFAULT_PX4_TARGET_COMPONENT
    )
    ack_wait_performed: Literal[True] = True
    timeout_seconds: float = Field(gt=0)
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_ack(self) -> "PX4GazeboMAVLinkCommandAckResult":
        if self.command_name != _COMMAND_NAMES.get(self.command_id):
            raise PX4MAVLinkAckStateError("ACK command name does not match command id")
        if self.ack_status == PX4MAVLinkCommandAckStatus.TIMEOUT:
            if self.ack_received or self.ack_result_code is not None:
                raise PX4MAVLinkAckStateError("timeout ACK cannot include ACK payload")
        elif not self.ack_received or self.ack_result_code is None:
            raise PX4MAVLinkAckStateError("non-timeout ACK requires ACK payload")
        if (
            self.ack_status == PX4MAVLinkCommandAckStatus.ACCEPTED
            and self.ack_result_code != MAV_RESULT_ACCEPTED
        ):
            raise PX4MAVLinkAckStateError("accepted ACK requires MAV_RESULT_ACCEPTED")
        if (
            self.ack_status == PX4MAVLinkCommandAckStatus.REJECTED
            and self.ack_result_code == MAV_RESULT_ACCEPTED
        ):
            raise PX4MAVLinkAckStateError("rejected ACK cannot use accepted result")
        return self


class PX4GazeboMAVLinkStateWaitResult(_AckStateSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_MAVLINK_STATE_WAIT_RESULT_SCHEMA_VERSION] = (
        PX4_GAZEBO_MAVLINK_STATE_WAIT_RESULT_SCHEMA_VERSION
    )
    state_wait_id: str
    expected_state: Literal["armed", "airborne", "landing", "landed_disarmed"]
    state_wait_status: PX4MAVLinkStateWaitStatus
    state_transition_observed: bool
    source_kind: Literal["px4_log_markers"] = "px4_log_markers"
    required_markers: tuple[str, ...]
    observed_markers: tuple[str, ...]
    timeout_seconds: float = Field(gt=0)
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("required_markers", "observed_markers", mode="before")
    @classmethod
    def _coerce_markers(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])

    @model_validator(mode="after")
    def _validate_state(self) -> "PX4GazeboMAVLinkStateWaitResult":
        expected_markers = _STATE_MARKERS[self.expected_state]
        if self.required_markers != expected_markers:
            raise PX4MAVLinkAckStateError(
                "state wait required markers do not match expected state"
            )
        if self.state_wait_status == PX4MAVLinkStateWaitStatus.OBSERVED:
            if not self.state_transition_observed:
                raise PX4MAVLinkAckStateError(
                    "observed state wait requires state_transition_observed=true"
                )
            for marker in self.required_markers:
                if marker not in self.observed_markers:
                    raise PX4MAVLinkAckStateError(
                        "observed state wait is missing required marker"
                    )
        if (
            self.state_wait_status == PX4MAVLinkStateWaitStatus.TIMEOUT
            and self.state_transition_observed
        ):
            raise PX4MAVLinkAckStateError(
                "timeout state wait cannot observe a state transition"
            )
        return self


class PX4GazeboRuntimeDeliveryRunnerResult(_AckStateSafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_RUNTIME_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION
    ] = PX4_GAZEBO_RUNTIME_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION
    runner_result_id: str
    final_status: PX4RuntimeDeliveryRunnerStatus
    dispatch_result_refs: tuple[str, ...]
    command_ack_result_refs: tuple[str, ...]
    state_wait_result_refs: tuple[str, ...]
    observed_delivery_phases: tuple[str, ...]
    missing_phases: tuple[str, ...]
    required_command_ids: tuple[int, ...]
    observed_command_ids: tuple[int, ...]
    missing_command_ids: tuple[int, ...]
    ack_wait_performed: Literal[True] = True
    all_command_acks_accepted: bool
    all_state_transitions_observed: bool
    completed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("completed_at", mode="before")
    @classmethod
    def _coerce_completed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator(
        "dispatch_result_refs",
        "command_ack_result_refs",
        "state_wait_result_refs",
        "observed_delivery_phases",
        "missing_phases",
        mode="before",
    )
    @classmethod
    def _coerce_refs(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])

    @model_validator(mode="after")
    def _validate_runner(self) -> "PX4GazeboRuntimeDeliveryRunnerResult":
        if self.final_status == PX4RuntimeDeliveryRunnerStatus.COMPLETED:
            if self.missing_phases:
                raise PX4MAVLinkAckStateError(
                    "completed runtime delivery runner cannot have missing phases"
                )
            if not self.all_command_acks_accepted:
                raise PX4MAVLinkAckStateError(
                    "completed runtime delivery runner requires accepted ACKs"
                )
            if not self.all_state_transitions_observed:
                raise PX4MAVLinkAckStateError(
                    "completed runtime delivery runner requires observed states"
                )
            if self.missing_command_ids:
                raise PX4MAVLinkAckStateError(
                    "completed runtime delivery runner requires all commands"
                )
        return self


def wait_for_px4_state_from_logs(
    *,
    expected_state: str,
    px4_logs: str,
    timeout_seconds: float,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboMAVLinkStateWaitResult:
    if expected_state not in _STATE_MARKERS:
        raise PX4MAVLinkAckStateError(f"unsupported PX4 state wait: {expected_state}")
    required = _STATE_MARKERS[expected_state]
    observed = tuple(marker for marker in required if marker in px4_logs)
    status = (
        PX4MAVLinkStateWaitStatus.OBSERVED
        if observed == required
        else PX4MAVLinkStateWaitStatus.TIMEOUT
    )
    observed_at = _utc(now)
    payload = {
        "expected_state": expected_state,
        "status": status.value,
        "observed_markers": observed,
        "observed_at": observed_at.isoformat(),
    }
    return PX4GazeboMAVLinkStateWaitResult(
        state_wait_id=_stable_id("px4_gazebo_mavlink_state_wait", payload),
        expected_state=expected_state,  # type: ignore[arg-type]
        state_wait_status=status,
        state_transition_observed=status == PX4MAVLinkStateWaitStatus.OBSERVED,
        required_markers=required,
        observed_markers=observed,
        timeout_seconds=timeout_seconds,
        observed_at=observed_at,
        metadata={
            **(metadata or {}),
            "issue": 341,
            "parent_epic": 339,
            "recovery_deferred_to_issue": 347,
        },
    )


def wait_for_px4_command_ack(
    *,
    dispatch_result: PX4GazeboLiveMAVLinkDispatchResult,
    sock: socket.socket,
    timeout_seconds: float,
    now: datetime | None = None,
) -> PX4GazeboMAVLinkCommandAckResult:
    sock.settimeout(timeout_seconds)
    ack_payload: dict[str, int | str] | None = None
    try:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            data, _addr = sock.recvfrom(2048)
            decoded = decode_mavlink2_frame(data)
            if decoded["msg_id"] != MAVLINK_MSG_ID_COMMAND_ACK:
                continue
            candidate = decode_mavlink2_command_ack(data)
            if int(candidate["command_id"]) == dispatch_result.command_id:
                ack_payload = candidate
                break
    except socket.timeout:
        ack_payload = None
    observed_at = _utc(now)
    if ack_payload is None:
        status = PX4MAVLinkCommandAckStatus.TIMEOUT
        result_code = None
        result_name = None
    else:
        result_code = int(ack_payload["result_code"])
        result_name = str(ack_payload["result_name"])
        status = (
            PX4MAVLinkCommandAckStatus.ACCEPTED
            if result_code == MAV_RESULT_ACCEPTED
            else PX4MAVLinkCommandAckStatus.REJECTED
        )
    payload = {
        "dispatch_result_id": dispatch_result.dispatch_result_id,
        "command_id": dispatch_result.command_id,
        "ack_status": status.value,
        "ack_result_code": result_code,
        "observed_at": observed_at.isoformat(),
    }
    return PX4GazeboMAVLinkCommandAckResult(
        ack_result_id=_stable_id("px4_gazebo_mavlink_command_ack", payload),
        dispatch_result_ref=_dispatch_ref(dispatch_result),
        command_id=dispatch_result.command_id,
        command_name=dispatch_result.command_name,
        ack_status=status,
        ack_received=ack_payload is not None,
        ack_result_code=result_code,
        ack_result_name=result_name,
        target_system=DEFAULT_PX4_TARGET_SYSTEM,
        target_component=DEFAULT_PX4_TARGET_COMPONENT,
        timeout_seconds=timeout_seconds,
        observed_at=observed_at,
        metadata={
            "issue": 341,
            "parent_epic": 339,
            "recovery_deferred_to_issue": 347,
        },
    )


def run_px4_gazebo_live_mavlink_dispatch_with_ack(
    *,
    approval: PX4GazeboCoupledCommandApproval | Mapping[str, Any],
    allowlist: PX4GazeboCoupledCommandAllowlist | Mapping[str, Any],
    command_id: int,
    endpoint_host: str = "127.0.0.1",
    endpoint_port: int = 18570,
    live_mavlink_opt_in: bool,
    ack_timeout_seconds: float = 1.0,
    now: datetime | None = None,
) -> tuple[PX4GazeboLiveMAVLinkDispatchResult, PX4GazeboMAVLinkCommandAckResult]:
    command_frame = encode_px4_gazebo_command_long(command_id=command_id)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        local_host, local_port = sock.getsockname()
        dispatch = build_px4_gazebo_live_mavlink_dispatch_result(
            approval=approval,
            allowlist=allowlist,
            command_id=command_id,
            endpoint_host=endpoint_host,
            endpoint_port=endpoint_port,
            local_bind_host=str(local_host),
            local_bind_port=int(local_port),
            frame_length_bytes=len(command_frame),
            heartbeat_frames_sent_before_command=1,
            live_mavlink_opt_in=live_mavlink_opt_in,
            sent_at=now,
        )
        remote = (endpoint_host, endpoint_port)
        sock.sendto(encode_mavlink2_heartbeat(sequence=0), remote)
        sock.sendto(command_frame, remote)
        ack = wait_for_px4_command_ack(
            dispatch_result=dispatch,
            sock=sock,
            timeout_seconds=ack_timeout_seconds,
            now=now,
        )
    return dispatch, ack


def build_px4_gazebo_runtime_delivery_runner_result(
    *,
    dispatch_results: Sequence[PX4GazeboLiveMAVLinkDispatchResult | Mapping[str, Any]],
    command_ack_results: Sequence[PX4GazeboMAVLinkCommandAckResult | Mapping[str, Any]],
    state_wait_results: Sequence[PX4GazeboMAVLinkStateWaitResult | Mapping[str, Any]],
    observed_delivery_phases: Sequence[str],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboRuntimeDeliveryRunnerResult:
    dispatches = [
        (
            item
            if isinstance(item, PX4GazeboLiveMAVLinkDispatchResult)
            else PX4GazeboLiveMAVLinkDispatchResult.model_validate(dict(item))
        )
        for item in dispatch_results
    ]
    acks = [
        (
            item
            if isinstance(item, PX4GazeboMAVLinkCommandAckResult)
            else PX4GazeboMAVLinkCommandAckResult.model_validate(dict(item))
        )
        for item in command_ack_results
    ]
    states = [
        (
            item
            if isinstance(item, PX4GazeboMAVLinkStateWaitResult)
            else PX4GazeboMAVLinkStateWaitResult.model_validate(dict(item))
        )
        for item in state_wait_results
    ]
    phases = _ordered_tuple(observed_delivery_phases)
    missing = tuple(phase for phase in _REQUIRED_PHASES if phase not in phases)
    dispatch_by_ref = {_dispatch_ref(dispatch): dispatch for dispatch in dispatches}
    for ack in acks:
        dispatch = dispatch_by_ref.get(ack.dispatch_result_ref)
        if dispatch is None:
            raise PX4MAVLinkAckStateError(
                "ACK result must reference one of the runtime dispatch results"
            )
        if dispatch.command_id != ack.command_id:
            raise PX4MAVLinkAckStateError(
                "ACK result command id must match referenced dispatch command id"
            )
    observed_commands = tuple(
        command_id
        for command_id in _REQUIRED_COMMAND_IDS
        if any(dispatch.command_id == command_id for dispatch in dispatches)
    )
    missing_commands = tuple(
        command_id
        for command_id in _REQUIRED_COMMAND_IDS
        if command_id not in observed_commands
    )
    accepted_ack_command_ids = {
        ack.command_id
        for ack in acks
        if ack.ack_status == PX4MAVLinkCommandAckStatus.ACCEPTED
    }
    all_acks = not missing_commands and all(
        command_id in accepted_ack_command_ids for command_id in _REQUIRED_COMMAND_IDS
    )
    all_ack_payloads_consistent = bool(acks) and all(
        ack.ack_status == PX4MAVLinkCommandAckStatus.ACCEPTED for ack in acks
    )
    all_acks = all_acks and all_ack_payloads_consistent
    all_states = bool(states) and all(
        state.state_wait_status == PX4MAVLinkStateWaitStatus.OBSERVED
        for state in states
    )
    final_status = (
        PX4RuntimeDeliveryRunnerStatus.COMPLETED
        if not missing and all_acks and all_states
        else PX4RuntimeDeliveryRunnerStatus.BLOCKED
    )
    completed_at = _utc(now)
    payload = {
        "dispatches": [item.dispatch_result_id for item in dispatches],
        "acks": [item.ack_result_id for item in acks],
        "states": [item.state_wait_id for item in states],
        "phases": phases,
        "missing": missing,
        "observed_commands": observed_commands,
        "missing_commands": missing_commands,
        "final_status": final_status.value,
    }
    return PX4GazeboRuntimeDeliveryRunnerResult(
        runner_result_id=_stable_id("px4_gazebo_runtime_delivery_runner", payload),
        final_status=final_status,
        dispatch_result_refs=tuple(_dispatch_ref(item) for item in dispatches),
        command_ack_result_refs=tuple(_ack_ref(item) for item in acks),
        state_wait_result_refs=tuple(_state_ref(item) for item in states),
        observed_delivery_phases=phases,
        missing_phases=missing,
        required_command_ids=_REQUIRED_COMMAND_IDS,
        observed_command_ids=observed_commands,
        missing_command_ids=missing_commands,
        all_command_acks_accepted=all_acks,
        all_state_transitions_observed=all_states,
        completed_at=completed_at,
        metadata={
            **(metadata or {}),
            "issue": 342,
            "parent_epic": 339,
            "route_delivery_deferred_to_issue": 345,
        },
    )


def run_px4_gazebo_runtime_delivery_task(
    task_id: str,
    *,
    dispatch_results: Sequence[PX4GazeboLiveMAVLinkDispatchResult | Mapping[str, Any]],
    command_ack_results: Sequence[PX4GazeboMAVLinkCommandAckResult | Mapping[str, Any]],
    state_wait_results: Sequence[PX4GazeboMAVLinkStateWaitResult | Mapping[str, Any]],
    observed_delivery_phases: Sequence[str],
    now: datetime | None = None,
    task_store_factory: Any | None = None,
) -> dict[str, Any]:
    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4MAVLinkAckStateError(
            f"task {task_id} not found; cannot run runtime delivery"
        )
    runner = build_px4_gazebo_runtime_delivery_runner_result(
        dispatch_results=dispatch_results,
        command_ack_results=command_ack_results,
        state_wait_results=state_wait_results,
        observed_delivery_phases=observed_delivery_phases,
        now=now,
    )
    updated = store.update(
        task_id,
        status=runner.final_status.value,
        artifacts={
            "px4_gazebo_runtime_delivery_runner_result": runner.model_dump(mode="json"),
        },
        ended_at=time.time(),
    )
    if updated is None:
        raise PX4MAVLinkAckStateError(
            f"task {task_id} disappeared while running runtime delivery"
        )
    return updated


__all__ = [
    "MAV_RESULT_ACCEPTED",
    "MAV_RESULT_CANCELLED",
    "MAV_RESULT_DENIED",
    "MAV_RESULT_FAILED",
    "MAV_RESULT_IN_PROGRESS",
    "MAV_RESULT_TEMPORARILY_REJECTED",
    "MAV_RESULT_UNSUPPORTED",
    "PX4_GAZEBO_MAVLINK_COMMAND_ACK_RESULT_SCHEMA_VERSION",
    "PX4_GAZEBO_MAVLINK_STATE_WAIT_RESULT_SCHEMA_VERSION",
    "PX4_GAZEBO_RUNTIME_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION",
    "PX4GazeboMAVLinkCommandAckResult",
    "PX4GazeboMAVLinkStateWaitResult",
    "PX4GazeboRuntimeDeliveryRunnerResult",
    "PX4MAVLinkAckStateError",
    "PX4MAVLinkCommandAckStatus",
    "PX4MAVLinkStateWaitStatus",
    "PX4RuntimeDeliveryRunnerStatus",
    "build_px4_gazebo_runtime_delivery_runner_result",
    "decode_mavlink2_command_ack",
    "encode_mavlink2_command_ack",
    "run_px4_gazebo_live_mavlink_dispatch_with_ack",
    "run_px4_gazebo_runtime_delivery_task",
    "wait_for_px4_command_ack",
    "wait_for_px4_state_from_logs",
]
