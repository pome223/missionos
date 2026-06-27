"""Opt-in real MAVLink transport for PX4 SITL loopback.

This module is the first non-stub MAVLink transport boundary in the PX4/Gazebo
delivery epic. It opens a real UDP socket and sends MAVLink v2 frames, but only
to explicit loopback PX4 SITL endpoints and only after the caller opts in.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import socket
import struct
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.px4_delivery_command_preflight import (
    PX4SimulationCommandAllowlist,
    PX4SimulationCommandApproval,
    PX4SimulationCommandKind,
)

PX4_REAL_MAVLINK_TRANSPORT_CONNECTION_SCHEMA_VERSION = (
    "px4_real_mavlink_transport_connection.v1"
)
PX4_MAVLINK_HEARTBEAT_STATUS_QUERY_SCHEMA_VERSION = (
    "px4_mavlink_heartbeat_status_query.v1"
)
PX4_REAL_MAVLINK_DISPATCH_RESULT_SCHEMA_VERSION = "px4_real_mavlink_dispatch_result.v1"
PX4_SITL_MAVLINK_STATUS_SMOKE_SCHEMA_VERSION = "px4_sitl_mavlink_status_smoke.v1"

MAVLINK2_MAGIC = 0xFD
MAVLINK_MSG_ID_HEARTBEAT = 0
MAVLINK_MSG_ID_COMMAND_LONG = 76
MAVLINK_MSG_ID_COMMAND_ACK = 77
MAVLINK_MSG_ID_SET_POSITION_TARGET_LOCAL_NED = 84
MAVLINK_MSG_ID_MISSION_COUNT = 44
MAVLINK_MSG_ID_MISSION_ACK = 47
MAVLINK_MSG_ID_MISSION_REQUEST_INT = 51
MAVLINK_MSG_ID_MISSION_ITEM_INT = 73
MAV_CMD_REQUEST_MESSAGE = 512
MAV_TYPE_GCS = 6
MAV_AUTOPILOT_INVALID = 8
MAV_STATE_ACTIVE = 4
MAVLINK_VERSION = 3

_CRC_EXTRA = {
    MAVLINK_MSG_ID_HEARTBEAT: 50,
    MAVLINK_MSG_ID_COMMAND_LONG: 152,
    MAVLINK_MSG_ID_COMMAND_ACK: 143,
    MAVLINK_MSG_ID_SET_POSITION_TARGET_LOCAL_NED: 143,
    MAVLINK_MSG_ID_MISSION_COUNT: 221,
    MAVLINK_MSG_ID_MISSION_ACK: 153,
    MAVLINK_MSG_ID_MISSION_REQUEST_INT: 196,
    MAVLINK_MSG_ID_MISSION_ITEM_INT: 38,
}


class PX4RealMAVLinkTransportError(RuntimeError):
    """Raised when real MAVLink transport cannot proceed safely."""


class PX4RealMAVLinkFrameKind(str, Enum):
    HEARTBEAT = "heartbeat"
    REQUEST_MESSAGE = "request_message"


class PX4RealMAVLinkDispatchStatus(str, Enum):
    SENT = "sent"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"


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


def _x25_crc_accumulate(byte: int, crc: int) -> int:
    tmp = byte ^ (crc & 0xFF)
    tmp = (tmp ^ (tmp << 4)) & 0xFF
    return ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF


def mavlink_x25_crc(data: bytes, crc_extra: int) -> int:
    crc = 0xFFFF
    for byte in data:
        crc = _x25_crc_accumulate(byte, crc)
    return _x25_crc_accumulate(crc_extra, crc)


def encode_mavlink2_frame(
    *,
    msg_id: int,
    payload: bytes,
    sequence: int,
    system_id: int,
    component_id: int,
) -> bytes:
    if msg_id not in _CRC_EXTRA:
        raise PX4RealMAVLinkTransportError(f"unsupported MAVLink message id: {msg_id}")
    if not 0 <= sequence <= 255:
        raise PX4RealMAVLinkTransportError("MAVLink sequence must fit uint8")
    header_without_magic = bytes(
        [
            len(payload),
            0,
            0,
            sequence,
            system_id,
            component_id,
            msg_id & 0xFF,
            (msg_id >> 8) & 0xFF,
            (msg_id >> 16) & 0xFF,
        ]
    )
    crc = mavlink_x25_crc(header_without_magic + payload, _CRC_EXTRA[msg_id])
    return (
        bytes([MAVLINK2_MAGIC])
        + header_without_magic
        + payload
        + struct.pack("<H", crc)
    )


def encode_mavlink2_heartbeat(
    *,
    sequence: int = 0,
    system_id: int = 255,
    component_id: int = 190,
) -> bytes:
    payload = struct.pack(
        "<IBBBBB",
        0,
        MAV_TYPE_GCS,
        MAV_AUTOPILOT_INVALID,
        0,
        MAV_STATE_ACTIVE,
        MAVLINK_VERSION,
    )
    return encode_mavlink2_frame(
        msg_id=MAVLINK_MSG_ID_HEARTBEAT,
        payload=payload,
        sequence=sequence,
        system_id=system_id,
        component_id=component_id,
    )


def encode_mavlink2_request_message(
    *,
    requested_message_id: int,
    target_system: int,
    target_component: int,
    sequence: int = 1,
    system_id: int = 255,
    component_id: int = 190,
) -> bytes:
    payload = struct.pack(
        "<fffffffHBBB",
        float(requested_message_id),
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        MAV_CMD_REQUEST_MESSAGE,
        target_system,
        target_component,
        0,
    )
    return encode_mavlink2_frame(
        msg_id=MAVLINK_MSG_ID_COMMAND_LONG,
        payload=payload,
        sequence=sequence,
        system_id=system_id,
        component_id=component_id,
    )


def decode_mavlink2_frame(frame: bytes) -> dict[str, Any]:
    if len(frame) < 12 or frame[0] != MAVLINK2_MAGIC:
        raise PX4RealMAVLinkTransportError("not a MAVLink v2 frame")
    payload_len = frame[1]
    expected_len = 10 + payload_len + 2
    if len(frame) < expected_len:
        raise PX4RealMAVLinkTransportError("incomplete MAVLink v2 frame")
    packet = frame[:expected_len]
    msg_id = packet[7] | (packet[8] << 8) | (packet[9] << 16)
    if msg_id not in _CRC_EXTRA:
        raise PX4RealMAVLinkTransportError(f"unsupported MAVLink message id: {msg_id}")
    payload = packet[10 : 10 + payload_len]
    expected_crc = mavlink_x25_crc(packet[1 : 10 + payload_len], _CRC_EXTRA[msg_id])
    observed_crc = struct.unpack("<H", packet[10 + payload_len : expected_len])[0]
    if observed_crc != expected_crc:
        raise PX4RealMAVLinkTransportError("MAVLink frame checksum mismatch")
    return {
        "msg_id": msg_id,
        "payload_len": payload_len,
        "sequence": packet[4],
        "system_id": packet[5],
        "component_id": packet[6],
        "payload": payload,
        "frame_len": expected_len,
    }


def decode_mavlink2_header(frame: bytes) -> dict[str, Any]:
    if len(frame) < 10 or frame[0] != MAVLINK2_MAGIC:
        raise PX4RealMAVLinkTransportError("not a MAVLink v2 frame")
    payload_len = frame[1]
    expected_len = 10 + payload_len + 2
    if len(frame) < expected_len:
        raise PX4RealMAVLinkTransportError("incomplete MAVLink v2 frame")
    return {
        "msg_id": frame[7] | (frame[8] << 8) | (frame[9] << 16),
        "payload_len": payload_len,
        "sequence": frame[4],
        "system_id": frame[5],
        "component_id": frame[6],
        "frame_len": expected_len,
    }


def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def _validate_endpoint(*, endpoint_host: str, endpoint_port: int, opt_in: bool) -> None:
    if opt_in is not True:
        raise PX4RealMAVLinkTransportError(
            "real MAVLink transport requires explicit opt_in=true"
        )
    if not _is_loopback_host(endpoint_host):
        raise PX4RealMAVLinkTransportError(
            "real MAVLink transport is restricted to loopback PX4 SITL endpoints"
        )
    if not 1 <= endpoint_port <= 65535:
        raise PX4RealMAVLinkTransportError("endpoint port must fit uint16")


def _approval_ref(approval: PX4SimulationCommandApproval) -> str:
    return f"px4_simulation_command_approval:{approval.approval_id}"


def _proposal_ref(proposal_ref: str) -> str:
    return proposal_ref


def _to_approval(
    value: PX4SimulationCommandApproval | Mapping[str, Any],
) -> PX4SimulationCommandApproval:
    if isinstance(value, PX4SimulationCommandApproval):
        return value
    return PX4SimulationCommandApproval.model_validate(dict(value))


def _to_allowlist(
    value: PX4SimulationCommandAllowlist | Mapping[str, Any],
) -> PX4SimulationCommandAllowlist:
    if isinstance(value, PX4SimulationCommandAllowlist):
        return value
    return PX4SimulationCommandAllowlist.model_validate(dict(value))


def _validate_approval_allowlist(
    *,
    approval: PX4SimulationCommandApproval,
    allowlist: PX4SimulationCommandAllowlist,
    command_kind: PX4SimulationCommandKind,
) -> None:
    if approval.operator_approval_performed is not True:
        raise PX4RealMAVLinkTransportError(
            "real MAVLink dispatch requires operator_approval_performed=true"
        )
    if allowlist.proposal_ref != _proposal_ref(approval.proposal_ref):
        raise PX4RealMAVLinkTransportError("allowlist proposal mismatch")
    if allowlist.approval_ref != _approval_ref(approval):
        raise PX4RealMAVLinkTransportError("allowlist approval mismatch")
    if command_kind not in allowlist.allowed_command_kinds:
        raise PX4RealMAVLinkTransportError(
            f"command kind is not allowlisted: {command_kind}"
        )


class _RealMAVLinkSafetyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    opt_in_required: Literal[True] = True
    opt_in_performed: Literal[True] = True
    loopback_only: Literal[True] = True
    hardware_target_allowed: Literal[False] = False
    real_world_authority_granted: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False


class PX4RealMAVLinkTransportConnection(_RealMAVLinkSafetyBoundary):
    schema_version: Literal[PX4_REAL_MAVLINK_TRANSPORT_CONNECTION_SCHEMA_VERSION] = (
        PX4_REAL_MAVLINK_TRANSPORT_CONNECTION_SCHEMA_VERSION
    )
    connection_id: str
    endpoint_host: Literal["127.0.0.1"]
    endpoint_port: int = Field(ge=1, le=65535)
    local_host: str = Field(min_length=1)
    local_port: int = Field(ge=1, le=65535)
    transport: Literal["udp_loopback_px4_sitl"] = "udp_loopback_px4_sitl"
    mavlink_socket_opened: Literal[True] = True
    connection_closed: Literal[True] = True
    opened_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("opened_at", mode="before")
    @classmethod
    def _coerce_opened_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


class PX4MAVLinkHeartbeatStatusQuery(_RealMAVLinkSafetyBoundary):
    schema_version: Literal[PX4_MAVLINK_HEARTBEAT_STATUS_QUERY_SCHEMA_VERSION] = (
        PX4_MAVLINK_HEARTBEAT_STATUS_QUERY_SCHEMA_VERSION
    )
    query_id: str
    connection_ref: str = Field(min_length=1)
    endpoint_host: Literal["127.0.0.1"]
    endpoint_port: int = Field(ge=1, le=65535)
    frame_kind_sent: Literal[PX4RealMAVLinkFrameKind.HEARTBEAT] = (
        PX4RealMAVLinkFrameKind.HEARTBEAT
    )
    mavlink_socket_opened: Literal[True] = True
    mavlink_frame_sent: Literal[True] = True
    mavlink_frame_received: bool
    received_msg_id: int | None = None
    received_system_id: int | None = None
    received_component_id: int | None = None
    timeout_seconds: float = Field(gt=0)
    queried_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("queried_at", mode="before")
    @classmethod
    def _coerce_queried_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


class PX4RealMAVLinkDispatchResult(_RealMAVLinkSafetyBoundary):
    schema_version: Literal[PX4_REAL_MAVLINK_DISPATCH_RESULT_SCHEMA_VERSION] = (
        PX4_REAL_MAVLINK_DISPATCH_RESULT_SCHEMA_VERSION
    )
    dispatch_result_id: str
    approval_ref: str = Field(min_length=1)
    allowlist_ref: str = Field(min_length=1)
    command_kind: PX4SimulationCommandKind
    dispatch_status: PX4RealMAVLinkDispatchStatus
    frame_kind_sent: Literal[PX4RealMAVLinkFrameKind.REQUEST_MESSAGE] = (
        PX4RealMAVLinkFrameKind.REQUEST_MESSAGE
    )
    dispatch_transport_semantics: Literal["heartbeat_status_query"] = (
        "heartbeat_status_query"
    )
    mavlink_command_id: Literal[MAV_CMD_REQUEST_MESSAGE] = MAV_CMD_REQUEST_MESSAGE
    mavlink_command_name: Literal["MAV_CMD_REQUEST_MESSAGE"] = "MAV_CMD_REQUEST_MESSAGE"
    requested_message_name: Literal["HEARTBEAT"] = "HEARTBEAT"
    delivery_phase_command_executed: Literal[False] = False
    endpoint_host: Literal["127.0.0.1"]
    endpoint_port: int = Field(ge=1, le=65535)
    target_system: int = Field(ge=1, le=255)
    target_component: int = Field(ge=1, le=255)
    requested_message_id: Literal[MAVLINK_MSG_ID_HEARTBEAT] = MAVLINK_MSG_ID_HEARTBEAT
    mavlink_socket_opened: Literal[True] = True
    mavlink_frame_sent: Literal[True] = True
    mavlink_frame_received: bool
    raw_mavlink_payload_present: Literal[False] = False
    sent_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sent_at", mode="before")
    @classmethod
    def _coerce_sent_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_status(self) -> "PX4RealMAVLinkDispatchResult":
        if (
            self.dispatch_status == PX4RealMAVLinkDispatchStatus.SENT
            and not self.mavlink_frame_received
        ):
            raise PX4RealMAVLinkTransportError(
                "sent real MAVLink dispatch requires response evidence"
            )
        return self


class PX4SITLMAVLinkStatusSmoke(_RealMAVLinkSafetyBoundary):
    schema_version: Literal[PX4_SITL_MAVLINK_STATUS_SMOKE_SCHEMA_VERSION] = (
        PX4_SITL_MAVLINK_STATUS_SMOKE_SCHEMA_VERSION
    )
    actual_px4_sitl_container_started: Literal[True] = True
    px4_startup_confirmed: bool
    mavlink_socket_opened: Literal[True] = True
    mavlink_frame_received_from_px4: bool
    mavlink_frame_sent_to_px4: bool
    heartbeat_frame_sent_to_px4: bool
    request_message_frame_sent_to_px4: bool
    delivery_phase_command_executed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_source_addr: tuple[str, int]
    px4_mavlink_remote_host: str = Field(min_length=1)
    first_received_msg_id: int = Field(ge=0)
    followup_msg_ids: tuple[int, ...] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("px4_source_addr", mode="before")
    @classmethod
    def _coerce_addr(cls, value: Any) -> tuple[str, int]:
        host, port = value
        return (str(host), int(port))

    @field_validator("followup_msg_ids", mode="before")
    @classmethod
    def _coerce_msg_ids(cls, value: Any) -> tuple[int, ...]:
        return tuple(int(item) for item in value)

    @model_validator(mode="after")
    def _validate_smoke(self) -> "PX4SITLMAVLinkStatusSmoke":
        if not self.px4_startup_confirmed:
            raise PX4RealMAVLinkTransportError(
                "PX4 SITL status smoke requires startup confirmation"
            )
        if not self.mavlink_frame_received_from_px4:
            raise PX4RealMAVLinkTransportError(
                "PX4 SITL status smoke requires frame received from PX4"
            )
        if not self.mavlink_frame_sent_to_px4:
            raise PX4RealMAVLinkTransportError(
                "PX4 SITL status smoke requires frame sent to PX4"
            )
        return self


def _connection_ref(connection: PX4RealMAVLinkTransportConnection) -> str:
    return f"px4_real_mavlink_transport_connection:{connection.connection_id}"


def _allowlist_ref(allowlist: PX4SimulationCommandAllowlist) -> str:
    return f"px4_simulation_command_allowlist:{allowlist.allowlist_id}"


def open_px4_sitl_mavlink_transport_connection(
    *,
    endpoint_host: str = "127.0.0.1",
    endpoint_port: int = 14540,
    opt_in: bool,
    timeout_seconds: float = 1.0,
    now: datetime | None = None,
) -> PX4RealMAVLinkTransportConnection:
    _validate_endpoint(
        endpoint_host=endpoint_host,
        endpoint_port=endpoint_port,
        opt_in=opt_in,
    )
    opened_at = _utc(now)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout_seconds)
        sock.bind(("127.0.0.1", 0))
        local_host, local_port = sock.getsockname()
    payload = {
        "endpoint_host": "127.0.0.1",
        "endpoint_port": endpoint_port,
        "local_host": local_host,
        "local_port": local_port,
        "opened_at": opened_at.isoformat(),
    }
    return PX4RealMAVLinkTransportConnection(
        connection_id=_stable_id("px4_real_mavlink_transport_connection", payload),
        endpoint_host="127.0.0.1",
        endpoint_port=endpoint_port,
        local_host=str(local_host),
        local_port=int(local_port),
        opened_at=opened_at,
        metadata={
            "artifact_only": False,
            "issue": 327,
            "parent_epic": 307,
            "real_udp_socket_opened": True,
            "socket_closed_before_artifact_return": True,
        },
    )


def run_px4_mavlink_heartbeat_status_query(
    *,
    endpoint_host: str = "127.0.0.1",
    endpoint_port: int = 14540,
    opt_in: bool,
    timeout_seconds: float = 1.0,
    now: datetime | None = None,
) -> tuple[PX4RealMAVLinkTransportConnection, PX4MAVLinkHeartbeatStatusQuery]:
    _validate_endpoint(
        endpoint_host=endpoint_host,
        endpoint_port=endpoint_port,
        opt_in=opt_in,
    )
    queried_at = _utc(now)
    received: dict[str, Any] | None = None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout_seconds)
        sock.bind(("127.0.0.1", 0))
        local_host, local_port = sock.getsockname()
        sock.sendto(
            encode_mavlink2_heartbeat(sequence=0),
            ("127.0.0.1", endpoint_port),
        )
        try:
            data, _addr = sock.recvfrom(2048)
            received = decode_mavlink2_frame(data)
        except socket.timeout:
            received = None
    connection = PX4RealMAVLinkTransportConnection(
        connection_id=_stable_id(
            "px4_real_mavlink_transport_connection",
            {
                "endpoint_host": "127.0.0.1",
                "endpoint_port": endpoint_port,
                "local_host": local_host,
                "local_port": local_port,
                "opened_at": queried_at.isoformat(),
            },
        ),
        endpoint_host="127.0.0.1",
        endpoint_port=endpoint_port,
        local_host=str(local_host),
        local_port=int(local_port),
        opened_at=queried_at,
    )
    query = PX4MAVLinkHeartbeatStatusQuery(
        query_id=_stable_id(
            "px4_mavlink_heartbeat_status_query",
            {
                "connection_id": connection.connection_id,
                "endpoint_port": endpoint_port,
                "received": received is not None,
            },
        ),
        connection_ref=_connection_ref(connection),
        endpoint_host="127.0.0.1",
        endpoint_port=endpoint_port,
        mavlink_frame_received=received is not None,
        received_msg_id=received["msg_id"] if received else None,
        received_system_id=received["system_id"] if received else None,
        received_component_id=received["component_id"] if received else None,
        timeout_seconds=timeout_seconds,
        queried_at=queried_at,
        metadata={
            "issue": 328,
            "parent_epic": 307,
            "real_mavlink_frame_sent": True,
            "heartbeat_query_only": True,
        },
    )
    return connection, query


def run_px4_real_mavlink_dispatch_result(
    *,
    approval: PX4SimulationCommandApproval | Mapping[str, Any],
    allowlist: PX4SimulationCommandAllowlist | Mapping[str, Any],
    command_kind: PX4SimulationCommandKind | str,
    endpoint_host: str = "127.0.0.1",
    endpoint_port: int = 14540,
    target_system: int = 1,
    target_component: int = 1,
    opt_in: bool,
    timeout_seconds: float = 1.0,
    now: datetime | None = None,
) -> PX4RealMAVLinkDispatchResult:
    _validate_endpoint(
        endpoint_host=endpoint_host,
        endpoint_port=endpoint_port,
        opt_in=opt_in,
    )
    resolved_approval = _to_approval(approval)
    resolved_allowlist = _to_allowlist(allowlist)
    kind = (
        command_kind
        if isinstance(command_kind, PX4SimulationCommandKind)
        else PX4SimulationCommandKind(str(command_kind))
    )
    _validate_approval_allowlist(
        approval=resolved_approval,
        allowlist=resolved_allowlist,
        command_kind=kind,
    )
    sent_at = _utc(now)
    received = False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout_seconds)
        sock.bind(("127.0.0.1", 0))
        sock.sendto(
            encode_mavlink2_request_message(
                requested_message_id=MAVLINK_MSG_ID_HEARTBEAT,
                target_system=target_system,
                target_component=target_component,
                sequence=1,
            ),
            ("127.0.0.1", endpoint_port),
        )
        try:
            data, _addr = sock.recvfrom(2048)
            decoded = decode_mavlink2_frame(data)
            received = decoded["msg_id"] == MAVLINK_MSG_ID_HEARTBEAT
        except socket.timeout:
            received = False
    status = (
        PX4RealMAVLinkDispatchStatus.SENT
        if received
        else PX4RealMAVLinkDispatchStatus.TIMEOUT
    )
    return PX4RealMAVLinkDispatchResult(
        dispatch_result_id=_stable_id(
            "px4_real_mavlink_dispatch_result",
            {
                "approval_id": resolved_approval.approval_id,
                "allowlist_id": resolved_allowlist.allowlist_id,
                "command_kind": kind.value,
                "endpoint_port": endpoint_port,
                "target_system": target_system,
                "target_component": target_component,
                "received": received,
            },
        ),
        approval_ref=_approval_ref(resolved_approval),
        allowlist_ref=_allowlist_ref(resolved_allowlist),
        command_kind=kind,
        dispatch_status=status,
        endpoint_host="127.0.0.1",
        endpoint_port=endpoint_port,
        target_system=target_system,
        target_component=target_component,
        mavlink_frame_received=received,
        sent_at=sent_at,
        metadata={
            "issue": 329,
            "parent_epic": 307,
            "real_mavlink_frame_sent": True,
            "frame_semantics": "heartbeat_status_query",
            "delivery_command_not_executed": True,
        },
    )


__all__ = [
    "MAVLINK_MSG_ID_HEARTBEAT",
    "MAVLINK_MSG_ID_COMMAND_ACK",
    "MAVLINK_MSG_ID_MISSION_ACK",
    "MAVLINK_MSG_ID_MISSION_COUNT",
    "MAVLINK_MSG_ID_MISSION_ITEM_INT",
    "MAVLINK_MSG_ID_MISSION_REQUEST_INT",
    "MAVLINK_MSG_ID_SET_POSITION_TARGET_LOCAL_NED",
    "MAV_CMD_REQUEST_MESSAGE",
    "PX4_MAVLINK_HEARTBEAT_STATUS_QUERY_SCHEMA_VERSION",
    "PX4_REAL_MAVLINK_DISPATCH_RESULT_SCHEMA_VERSION",
    "PX4_REAL_MAVLINK_TRANSPORT_CONNECTION_SCHEMA_VERSION",
    "PX4_SITL_MAVLINK_STATUS_SMOKE_SCHEMA_VERSION",
    "PX4MAVLinkHeartbeatStatusQuery",
    "PX4RealMAVLinkDispatchResult",
    "PX4RealMAVLinkDispatchStatus",
    "PX4RealMAVLinkFrameKind",
    "PX4RealMAVLinkTransportConnection",
    "PX4RealMAVLinkTransportError",
    "PX4SITLMAVLinkStatusSmoke",
    "decode_mavlink2_frame",
    "decode_mavlink2_header",
    "encode_mavlink2_frame",
    "encode_mavlink2_heartbeat",
    "encode_mavlink2_request_message",
    "mavlink_x25_crc",
    "open_px4_sitl_mavlink_transport_connection",
    "run_px4_mavlink_heartbeat_status_query",
    "run_px4_real_mavlink_dispatch_result",
]
