"""Live simulation-only MAVLink COMMAND_LONG dispatcher for PX4/Gazebo SITL.

This module promotes the coupled-delivery smoke helper into a reusable runtime
boundary. It sends only approval-backed, allowlisted COMMAND_LONG frames to a
loopback PX4 SITL endpoint, and it deliberately stops before ACK/state-machine
handling; ACK waiting belongs to the next runtime layer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import math
import socket
import struct
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.px4_gazebo_coupled_delivery import (
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_TAKEOFF,
    PX4GazeboCoupledCommandAllowlist,
    PX4GazeboCoupledCommandApproval,
    PX4GazeboCoupledDeliveryError,
    validate_px4_gazebo_coupled_command_dispatch,
)
from src.runtime.px4_real_mavlink_transport import (
    MAVLINK_MSG_ID_COMMAND_LONG,
    encode_mavlink2_frame,
    encode_mavlink2_heartbeat,
)

PX4_GAZEBO_LIVE_MAVLINK_DISPATCH_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_live_mavlink_dispatch_result.v1"
)

MAVLINK_GCS_SYSTEM_ID = 255
MAVLINK_GCS_COMPONENT_ID = 190
DEFAULT_PX4_TARGET_SYSTEM = 1
DEFAULT_PX4_TARGET_COMPONENT = 1

_COMMAND_NAMES = {
    MAV_CMD_COMPONENT_ARM_DISARM: "MAV_CMD_COMPONENT_ARM_DISARM",
    MAV_CMD_NAV_TAKEOFF: "MAV_CMD_NAV_TAKEOFF",
    MAV_CMD_NAV_LAND: "MAV_CMD_NAV_LAND",
}
_COMMAND_PARAMS = {
    MAV_CMD_COMPONENT_ARM_DISARM: (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    MAV_CMD_NAV_TAKEOFF: (0.0, 0.0, 0.0, 0.0, math.nan, math.nan, 2.5),
    MAV_CMD_NAV_LAND: (0.0, 0.0, 0.0, 0.0, math.nan, math.nan, 0.0),
}


class PX4LiveMAVLinkDispatcherError(RuntimeError):
    """Raised when live simulation-only MAVLink dispatch is unsafe."""


class PX4LiveMAVLinkDispatchMode(str, Enum):
    ARTIFACT_STUB = "artifact_stub"
    LIVE_MAVLINK = "live_mavlink"


class PX4LiveMAVLinkDispatchStatus(str, Enum):
    SENT = "sent"


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


def _approval_ref(approval: PX4GazeboCoupledCommandApproval) -> str:
    return f"px4_gazebo_coupled_command_approval:{approval.approval_id}"


def _allowlist_ref(allowlist: PX4GazeboCoupledCommandAllowlist) -> str:
    return f"px4_gazebo_coupled_command_allowlist:{allowlist.allowlist_id}"


def _to_approval(
    value: PX4GazeboCoupledCommandApproval | Mapping[str, Any],
) -> PX4GazeboCoupledCommandApproval:
    if isinstance(value, PX4GazeboCoupledCommandApproval):
        return value
    return PX4GazeboCoupledCommandApproval.model_validate(dict(value))


def _to_allowlist(
    value: PX4GazeboCoupledCommandAllowlist | Mapping[str, Any],
) -> PX4GazeboCoupledCommandAllowlist:
    if isinstance(value, PX4GazeboCoupledCommandAllowlist):
        return value
    return PX4GazeboCoupledCommandAllowlist.model_validate(dict(value))


def _validate_endpoint(
    *,
    endpoint_host: str,
    endpoint_port: int,
    local_bind_host: str,
    local_bind_port: int,
    live_mavlink_opt_in: bool,
) -> None:
    if live_mavlink_opt_in is not True:
        raise PX4LiveMAVLinkDispatcherError(
            "live MAVLink dispatch requires explicit live_mavlink_opt_in=true"
        )
    if endpoint_host != "127.0.0.1":
        raise PX4LiveMAVLinkDispatcherError(
            "live MAVLink dispatch endpoint_host must be 127.0.0.1"
        )
    if local_bind_host != "127.0.0.1":
        raise PX4LiveMAVLinkDispatcherError(
            "live MAVLink dispatch must bind from loopback"
        )
    if not 1 <= endpoint_port <= 65535:
        raise PX4LiveMAVLinkDispatcherError("endpoint port must fit uint16")
    if not 0 <= local_bind_port <= 65535:
        raise PX4LiveMAVLinkDispatcherError("local bind port must fit uint16")


def _command_name(command_id: int) -> str:
    try:
        return _COMMAND_NAMES[int(command_id)]
    except KeyError as exc:
        raise PX4LiveMAVLinkDispatcherError(
            f"unsupported coupled delivery MAVLink command id: {command_id}"
        ) from exc


def encode_px4_gazebo_command_long(
    *,
    command_id: int,
    target_system: int = DEFAULT_PX4_TARGET_SYSTEM,
    target_component: int = DEFAULT_PX4_TARGET_COMPONENT,
    sequence: int = 10,
    system_id: int = MAVLINK_GCS_SYSTEM_ID,
    component_id: int = MAVLINK_GCS_COMPONENT_ID,
) -> bytes:
    """Encode the bounded arm/takeoff/land COMMAND_LONG frame."""

    command = int(command_id)
    if command not in _COMMAND_PARAMS:
        raise PX4LiveMAVLinkDispatcherError(
            f"unsupported coupled delivery MAVLink command id: {command_id}"
        )
    payload = struct.pack(
        "<fffffffHBBB",
        *[float(item) for item in _COMMAND_PARAMS[command]],
        command,
        int(target_system),
        int(target_component),
        0,
    )
    return encode_mavlink2_frame(
        msg_id=MAVLINK_MSG_ID_COMMAND_LONG,
        payload=payload,
        sequence=sequence,
        system_id=system_id,
        component_id=component_id,
    )


class PX4GazeboLiveMAVLinkDispatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_LIVE_MAVLINK_DISPATCH_RESULT_SCHEMA_VERSION] = (
        PX4_GAZEBO_LIVE_MAVLINK_DISPATCH_RESULT_SCHEMA_VERSION
    )
    dispatch_result_id: str
    dispatch_mode: Literal[PX4LiveMAVLinkDispatchMode.LIVE_MAVLINK] = (
        PX4LiveMAVLinkDispatchMode.LIVE_MAVLINK
    )
    dispatch_status: Literal[PX4LiveMAVLinkDispatchStatus.SENT] = (
        PX4LiveMAVLinkDispatchStatus.SENT
    )
    approval_ref: str = Field(min_length=1)
    allowlist_ref: str = Field(min_length=1)
    command_id: int
    command_name: str = Field(min_length=1)
    mavlink_message_id: Literal[MAVLINK_MSG_ID_COMMAND_LONG] = (
        MAVLINK_MSG_ID_COMMAND_LONG
    )
    mavlink_message_name: Literal["COMMAND_LONG"] = "COMMAND_LONG"
    target_system: Literal[DEFAULT_PX4_TARGET_SYSTEM] = DEFAULT_PX4_TARGET_SYSTEM
    target_component: Literal[DEFAULT_PX4_TARGET_COMPONENT] = (
        DEFAULT_PX4_TARGET_COMPONENT
    )
    endpoint_host: Literal["127.0.0.1"] = "127.0.0.1"
    endpoint_port: int = Field(ge=1, le=65535)
    local_bind_host: Literal["127.0.0.1"] = "127.0.0.1"
    local_bind_port: int = Field(ge=0, le=65535)
    frame_length_bytes: int = Field(gt=0)
    heartbeat_frames_sent_before_command: int = Field(ge=0)
    mavlink_socket_opened: Literal[True] = True
    mavlink_frame_sent: Literal[True] = True
    mavlink_command_sent_to_px4: Literal[True] = True
    delivery_phase_command_frame_sent: Literal[True] = True
    delivery_phase_command_ack_observed: Literal[False] = False
    delivery_phase_command_executed: Literal[False] = False
    state_transition_observed: Literal[False] = False
    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[True] = True
    bounded_allowlist_enforced: Literal[True] = True
    live_mavlink_opt_in_required: Literal[True] = True
    live_mavlink_opt_in_performed: Literal[True] = True
    loopback_only: Literal[True] = True
    simulation_mavlink_dispatch_allowed: Literal[True] = True
    simulation_actuator_effect_allowed: Literal[True] = True
    physical_actuator_execution_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_world_authority_granted: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    arbitrary_mavlink_dispatch_allowed: Literal[False] = False
    arbitrary_gazebo_mutation_allowed: Literal[False] = False
    ack_wait_performed: Literal[False] = False
    sent_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sent_at", mode="before")
    @classmethod
    def _coerce_sent_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_command(self) -> "PX4GazeboLiveMAVLinkDispatchResult":
        if self.command_name != _command_name(self.command_id):
            raise PX4LiveMAVLinkDispatcherError(
                "live MAVLink dispatch command name does not match command id"
            )
        return self


def run_px4_gazebo_live_mavlink_dispatch(
    *,
    approval: PX4GazeboCoupledCommandApproval | Mapping[str, Any],
    allowlist: PX4GazeboCoupledCommandAllowlist | Mapping[str, Any],
    command_id: int,
    endpoint_host: str = "127.0.0.1",
    endpoint_port: int = 18570,
    target_system: int = DEFAULT_PX4_TARGET_SYSTEM,
    target_component: int = DEFAULT_PX4_TARGET_COMPONENT,
    local_bind_host: str = "127.0.0.1",
    local_bind_port: int = 0,
    live_mavlink_opt_in: bool,
    heartbeat_sequences: Sequence[int] = (0, 1, 2),
    now: datetime | None = None,
) -> PX4GazeboLiveMAVLinkDispatchResult:
    _validate_endpoint(
        endpoint_host=endpoint_host,
        endpoint_port=endpoint_port,
        local_bind_host=local_bind_host,
        local_bind_port=local_bind_port,
        live_mavlink_opt_in=live_mavlink_opt_in,
    )
    sent_at = _utc(now)
    command_frame = encode_px4_gazebo_command_long(
        command_id=command_id,
        target_system=target_system,
        target_component=target_component,
        sequence=10,
    )
    build_px4_gazebo_live_mavlink_dispatch_result(
        approval=approval,
        allowlist=allowlist,
        command_id=command_id,
        endpoint_host=endpoint_host,
        endpoint_port=endpoint_port,
        target_system=target_system,
        target_component=target_component,
        local_bind_host=local_bind_host,
        local_bind_port=local_bind_port,
        frame_length_bytes=len(command_frame),
        heartbeat_frames_sent_before_command=len(tuple(heartbeat_sequences)),
        live_mavlink_opt_in=live_mavlink_opt_in,
        sent_at=sent_at,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((local_bind_host, local_bind_port))
        actual_local_host, actual_local_port = sock.getsockname()
        remote = (endpoint_host, endpoint_port)
        for sequence in heartbeat_sequences:
            sock.sendto(encode_mavlink2_heartbeat(sequence=int(sequence)), remote)
        sock.sendto(command_frame, remote)
    return build_px4_gazebo_live_mavlink_dispatch_result(
        approval=approval,
        allowlist=allowlist,
        command_id=command_id,
        endpoint_host=endpoint_host,
        endpoint_port=endpoint_port,
        target_system=target_system,
        target_component=target_component,
        local_bind_host=str(actual_local_host),
        local_bind_port=int(actual_local_port),
        frame_length_bytes=len(command_frame),
        heartbeat_frames_sent_before_command=len(tuple(heartbeat_sequences)),
        live_mavlink_opt_in=live_mavlink_opt_in,
        sent_at=sent_at,
    )


def build_px4_gazebo_live_mavlink_dispatch_result(
    *,
    approval: PX4GazeboCoupledCommandApproval | Mapping[str, Any],
    allowlist: PX4GazeboCoupledCommandAllowlist | Mapping[str, Any],
    command_id: int,
    endpoint_host: str = "127.0.0.1",
    endpoint_port: int = 18570,
    target_system: int = DEFAULT_PX4_TARGET_SYSTEM,
    target_component: int = DEFAULT_PX4_TARGET_COMPONENT,
    local_bind_host: str = "127.0.0.1",
    local_bind_port: int,
    frame_length_bytes: int,
    heartbeat_frames_sent_before_command: int,
    live_mavlink_opt_in: bool,
    sent_at: datetime | None = None,
) -> PX4GazeboLiveMAVLinkDispatchResult:
    _validate_endpoint(
        endpoint_host=endpoint_host,
        endpoint_port=endpoint_port,
        local_bind_host=local_bind_host,
        local_bind_port=local_bind_port,
        live_mavlink_opt_in=live_mavlink_opt_in,
    )
    if (
        target_system != DEFAULT_PX4_TARGET_SYSTEM
        or target_component != DEFAULT_PX4_TARGET_COMPONENT
    ):
        raise PX4LiveMAVLinkDispatcherError(
            "live coupled delivery dispatch is restricted to PX4 target 1/1"
        )
    resolved_approval = _to_approval(approval)
    resolved_allowlist = _to_allowlist(allowlist)
    command = int(command_id)
    try:
        validate_px4_gazebo_coupled_command_dispatch(
            approval=resolved_approval,
            allowlist=resolved_allowlist,
            command_id=command,
        )
    except PX4GazeboCoupledDeliveryError as exc:
        raise PX4LiveMAVLinkDispatcherError(str(exc)) from exc
    recorded_at = _utc(sent_at)
    payload = {
        "approval_id": resolved_approval.approval_id,
        "allowlist_id": resolved_allowlist.allowlist_id,
        "command_id": command,
        "endpoint_port": endpoint_port,
        "target_system": target_system,
        "target_component": target_component,
        "sent_at": recorded_at.isoformat(),
    }
    return PX4GazeboLiveMAVLinkDispatchResult(
        dispatch_result_id=_stable_id("px4_gazebo_live_mavlink_dispatch", payload),
        approval_ref=_approval_ref(resolved_approval),
        allowlist_ref=_allowlist_ref(resolved_allowlist),
        command_id=command,
        command_name=_command_name(command),
        endpoint_host="127.0.0.1",
        endpoint_port=endpoint_port,
        local_bind_host=local_bind_host,
        local_bind_port=local_bind_port,
        frame_length_bytes=frame_length_bytes,
        heartbeat_frames_sent_before_command=heartbeat_frames_sent_before_command,
        sent_at=recorded_at,
        metadata={
            "issue": 340,
            "parent_epic": 339,
            "ack_wait_deferred_to_issue": 341,
            "default_dispatch_mode_remains": (
                PX4LiveMAVLinkDispatchMode.ARTIFACT_STUB.value
            ),
        },
    )


__all__ = [
    "PX4_GAZEBO_LIVE_MAVLINK_DISPATCH_RESULT_SCHEMA_VERSION",
    "PX4GazeboLiveMAVLinkDispatchResult",
    "PX4LiveMAVLinkDispatcherError",
    "PX4LiveMAVLinkDispatchMode",
    "PX4LiveMAVLinkDispatchStatus",
    "build_px4_gazebo_live_mavlink_dispatch_result",
    "encode_px4_gazebo_command_long",
    "run_px4_gazebo_live_mavlink_dispatch",
]
