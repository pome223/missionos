"""Approval-gated emergency MAVLink dispatcher for PX4/Gazebo routes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import math
import socket
import struct
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.px4_gazebo_route_recovery import PX4GazeboRouteRecoveryAction
from src.runtime.px4_live_mavlink_dispatcher import (
    DEFAULT_PX4_TARGET_COMPONENT,
    DEFAULT_PX4_TARGET_SYSTEM,
    MAVLINK_GCS_COMPONENT_ID,
    MAVLINK_GCS_SYSTEM_ID,
)
from src.runtime.px4_mavlink_ack_state import (
    MAV_RESULT_ACCEPTED,
)
from src.runtime.px4_real_mavlink_transport import (
    MAVLINK_MSG_ID_COMMAND_ACK,
    MAVLINK_MSG_ID_COMMAND_LONG,
    decode_mavlink2_frame,
    encode_mavlink2_frame,
    encode_mavlink2_heartbeat,
)

PX4_GAZEBO_EMERGENCY_COMMAND_APPROVAL_SCHEMA_VERSION = (
    "px4_gazebo_emergency_command_approval.v1"
)
PX4_GAZEBO_EMERGENCY_COMMAND_ALLOWLIST_SCHEMA_VERSION = (
    "px4_gazebo_emergency_command_allowlist.v1"
)
PX4_GAZEBO_EMERGENCY_COMMAND_DISPATCH_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_emergency_command_dispatch_result.v1"
)
PX4_GAZEBO_EMERGENCY_ACK_TRANSPORT_DIAGNOSTIC_SCHEMA_VERSION = (
    "px4_gazebo_emergency_ack_transport_diagnostic.v1"
)

MAV_CMD_NAV_LOITER_UNLIM = 17
MAV_CMD_NAV_RETURN_TO_LAUNCH = 20
MAV_CMD_NAV_LAND = 21
EMERGENCY_ACK_TIMEOUT_SECONDS = 15.0
EMERGENCY_HEARTBEAT_WARMUP_FRAMES = 3
EMERGENCY_HEARTBEAT_WARMUP_INTERVAL_SECONDS = 0.25
MAVLINK1_MAGIC = 0xFE


class PX4GazeboEmergencyDispatcherError(RuntimeError):
    """Raised when emergency dispatch evidence is unsafe or inconsistent."""


class PX4GazeboEmergencyCommandDispatchStatus(str, Enum):
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class PX4GazeboEmergencyAckTransportSupportStatus(str, Enum):
    ACK_COMPLETE_SUPPORTED = "ack_complete_supported"
    ACK_UNAVAILABLE_STATE_OBSERVED = "ack_unavailable_state_observed"
    ACK_UNAVAILABLE_STATE_NOT_OBSERVED = "ack_unavailable_state_not_observed"
    DISPATCH_BLOCKED = "dispatch_blocked"


_ACTION_COMMAND_IDS = {
    PX4GazeboRouteRecoveryAction.HOLD: MAV_CMD_NAV_LOITER_UNLIM,
    PX4GazeboRouteRecoveryAction.LAND: MAV_CMD_NAV_LAND,
    PX4GazeboRouteRecoveryAction.RETURN_TO_LAUNCH: MAV_CMD_NAV_RETURN_TO_LAUNCH,
}
_COMMAND_NAMES = {
    MAV_CMD_NAV_LOITER_UNLIM: "MAV_CMD_NAV_LOITER_UNLIM",
    MAV_CMD_NAV_LAND: "MAV_CMD_NAV_LAND",
    MAV_CMD_NAV_RETURN_TO_LAUNCH: "MAV_CMD_NAV_RETURN_TO_LAUNCH",
}
_COMMAND_ACTIONS = {value: key for key, value in _ACTION_COMMAND_IDS.items()}
_COMMAND_PARAMS = {
    MAV_CMD_NAV_LOITER_UNLIM: (0.0, 0.0, 0.0, 0.0, math.nan, math.nan, 0.0),
    MAV_CMD_NAV_LAND: (0.0, 0.0, 0.0, 0.0, math.nan, math.nan, 0.0),
    MAV_CMD_NAV_RETURN_TO_LAUNCH: (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
}
_ACK_RESULT_NAMES = {
    0: "ACCEPTED",
    1: "TEMPORARILY_REJECTED",
    2: "DENIED",
    3: "UNSUPPORTED",
    4: "FAILED",
    5: "IN_PROGRESS",
    6: "CANCELLED",
}


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


def _coerce_recovery_action(
    value: PX4GazeboRouteRecoveryAction | str,
) -> PX4GazeboRouteRecoveryAction:
    if isinstance(value, PX4GazeboRouteRecoveryAction):
        return value
    text = str(value)
    if text == "rtl":
        return PX4GazeboRouteRecoveryAction.RETURN_TO_LAUNCH
    return PX4GazeboRouteRecoveryAction(text)


def _ordered_actions(
    values: Sequence[PX4GazeboRouteRecoveryAction | str] | None,
) -> tuple[PX4GazeboRouteRecoveryAction, ...]:
    seen: set[PX4GazeboRouteRecoveryAction] = set()
    out: list[PX4GazeboRouteRecoveryAction] = []
    for item in values or ():
        action = _coerce_recovery_action(item)
        if action not in seen:
            seen.add(action)
            out.append(action)
    return tuple(out)


def _ordered_ints(values: Sequence[int] | None) -> tuple[int, ...]:
    seen: set[int] = set()
    out: list[int] = []
    for item in values or ():
        value = int(item)
        if value not in seen:
            seen.add(value)
            out.append(value)
    return tuple(out)


def _ordered_strings(values: Sequence[str] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in values or ():
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _approval_ref(approval: "PX4GazeboEmergencyCommandApproval") -> str:
    return f"px4_gazebo_emergency_command_approval:{approval.approval_id}"


def _allowlist_ref(allowlist: "PX4GazeboEmergencyCommandAllowlist") -> str:
    return f"px4_gazebo_emergency_command_allowlist:{allowlist.allowlist_id}"


def _action_command_id(action: PX4GazeboRouteRecoveryAction | str) -> int:
    resolved = _coerce_recovery_action(action)
    return _ACTION_COMMAND_IDS[resolved]


def _command_name(command_id: int) -> str:
    try:
        return _COMMAND_NAMES[int(command_id)]
    except KeyError as exc:
        raise PX4GazeboEmergencyDispatcherError(
            f"unsupported emergency MAVLink command id: {command_id}"
        ) from exc


def _command_action(command_id: int) -> PX4GazeboRouteRecoveryAction:
    try:
        return _COMMAND_ACTIONS[int(command_id)]
    except KeyError as exc:
        raise PX4GazeboEmergencyDispatcherError(
            f"unsupported emergency MAVLink command id: {command_id}"
        ) from exc


def _coerce_approval(
    value: "PX4GazeboEmergencyCommandApproval | Mapping[str, Any] | None",
) -> "PX4GazeboEmergencyCommandApproval | None":
    if value is None:
        return None
    if isinstance(value, PX4GazeboEmergencyCommandApproval):
        return value
    return PX4GazeboEmergencyCommandApproval.model_validate(dict(value))


def _coerce_allowlist(
    value: "PX4GazeboEmergencyCommandAllowlist | Mapping[str, Any] | None",
) -> "PX4GazeboEmergencyCommandAllowlist | None":
    if value is None:
        return None
    if isinstance(value, PX4GazeboEmergencyCommandAllowlist):
        return value
    return PX4GazeboEmergencyCommandAllowlist.model_validate(dict(value))


def _validate_endpoint(
    *,
    endpoint_host: str,
    endpoint_port: int,
    local_bind_port: int,
    live_mavlink_opt_in: bool,
) -> None:
    if live_mavlink_opt_in is not True:
        raise PX4GazeboEmergencyDispatcherError(
            "emergency command dispatch requires explicit live_mavlink_opt_in=true"
        )
    if endpoint_host != "127.0.0.1":
        raise PX4GazeboEmergencyDispatcherError(
            "emergency command dispatch endpoint_host must be 127.0.0.1"
        )
    if not 1 <= endpoint_port <= 65535:
        raise PX4GazeboEmergencyDispatcherError("endpoint port must fit uint16")
    if not 0 <= local_bind_port <= 65535:
        raise PX4GazeboEmergencyDispatcherError("local bind port must fit uint16")


def encode_px4_gazebo_emergency_command_long(
    *,
    command_id: int,
    target_system: int = DEFAULT_PX4_TARGET_SYSTEM,
    target_component: int = DEFAULT_PX4_TARGET_COMPONENT,
    sequence: int = 10,
    system_id: int = MAVLINK_GCS_SYSTEM_ID,
    component_id: int = MAVLINK_GCS_COMPONENT_ID,
) -> bytes:
    """Encode the bounded hold/land/RTL emergency COMMAND_LONG frame."""

    command = int(command_id)
    if command not in _COMMAND_PARAMS:
        raise PX4GazeboEmergencyDispatcherError(
            f"unsupported emergency MAVLink command id: {command_id}"
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


class _EmergencyCommandSafetyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    bounded_allowlist_enforced: Literal[True] = True
    emergency_command_dispatch_allowed: Literal[True] = True
    approval_free_recovery_dispatch_allowed: Literal[False] = False
    physical_actuator_execution_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_world_authority_granted: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    arbitrary_gazebo_mutation_allowed: Literal[False] = False
    retry_attempted: Literal[False] = False
    stronger_execution_attempted: Literal[False] = False


class PX4GazeboEmergencyCommandApproval(_EmergencyCommandSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_EMERGENCY_COMMAND_APPROVAL_SCHEMA_VERSION] = (
        PX4_GAZEBO_EMERGENCY_COMMAND_APPROVAL_SCHEMA_VERSION
    )
    approval_id: str
    operator_approval_performed: bool
    approved_recovery_actions: tuple[PX4GazeboRouteRecoveryAction, ...]
    approved_command_ids: tuple[int, ...]
    approved_at: datetime
    recovery_command_sent: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("approved_recovery_actions", mode="before")
    @classmethod
    def _coerce_actions(cls, value: Any) -> tuple[PX4GazeboRouteRecoveryAction, ...]:
        return _ordered_actions(value)

    @field_validator("approved_command_ids", mode="before")
    @classmethod
    def _coerce_command_ids(cls, value: Any) -> tuple[int, ...]:
        return _ordered_ints(value)

    @field_validator("approved_at", mode="before")
    @classmethod
    def _coerce_approved_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_approval(self) -> "PX4GazeboEmergencyCommandApproval":
        expected = tuple(
            _action_command_id(action) for action in self.approved_recovery_actions
        )
        if self.operator_approval_performed and not self.approved_recovery_actions:
            raise PX4GazeboEmergencyDispatcherError(
                "emergency approval requires approved recovery actions"
            )
        if self.approved_command_ids != expected:
            raise PX4GazeboEmergencyDispatcherError(
                "emergency approval command ids must match approved actions"
            )
        return self


class PX4GazeboEmergencyCommandAllowlist(_EmergencyCommandSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_EMERGENCY_COMMAND_ALLOWLIST_SCHEMA_VERSION] = (
        PX4_GAZEBO_EMERGENCY_COMMAND_ALLOWLIST_SCHEMA_VERSION
    )
    allowlist_id: str
    approval_ref: str = Field(min_length=1)
    operator_approval_performed: Literal[True] = True
    allowed_recovery_actions: tuple[PX4GazeboRouteRecoveryAction, ...]
    allowed_command_ids: tuple[int, ...]
    allowed_command_names: tuple[str, ...]
    allowed_mavlink_message_ids: Literal[(MAVLINK_MSG_ID_COMMAND_LONG,)] = (
        MAVLINK_MSG_ID_COMMAND_LONG,
    )
    recovery_command_sent: Literal[False] = False
    generated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("allowed_recovery_actions", mode="before")
    @classmethod
    def _coerce_actions(cls, value: Any) -> tuple[PX4GazeboRouteRecoveryAction, ...]:
        return _ordered_actions(value)

    @field_validator("allowed_command_ids", mode="before")
    @classmethod
    def _coerce_command_ids(cls, value: Any) -> tuple[int, ...]:
        return _ordered_ints(value)

    @field_validator("allowed_command_names", mode="before")
    @classmethod
    def _coerce_command_names(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_allowlist(self) -> "PX4GazeboEmergencyCommandAllowlist":
        expected_ids = tuple(
            _action_command_id(action) for action in self.allowed_recovery_actions
        )
        expected_names = tuple(_command_name(command_id) for command_id in expected_ids)
        if self.allowed_command_ids != expected_ids:
            raise PX4GazeboEmergencyDispatcherError(
                "emergency allowlist command ids must match allowed actions"
            )
        if self.allowed_command_names != expected_names:
            raise PX4GazeboEmergencyDispatcherError(
                "emergency allowlist command names must match allowed actions"
            )
        return self


class PX4GazeboEmergencyCommandDispatchResult(_EmergencyCommandSafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_EMERGENCY_COMMAND_DISPATCH_RESULT_SCHEMA_VERSION
    ] = PX4_GAZEBO_EMERGENCY_COMMAND_DISPATCH_RESULT_SCHEMA_VERSION
    dispatch_result_id: str
    dispatch_status: PX4GazeboEmergencyCommandDispatchStatus
    approval_ref: str | None = None
    allowlist_ref: str | None = None
    recovery_action: PX4GazeboRouteRecoveryAction
    command_id: int
    command_name: str = Field(min_length=1)
    mavlink_message_id: Literal[MAVLINK_MSG_ID_COMMAND_LONG] = (
        MAVLINK_MSG_ID_COMMAND_LONG
    )
    mavlink_message_name: Literal["COMMAND_LONG"] = "COMMAND_LONG"
    endpoint_host: Literal["127.0.0.1"] = "127.0.0.1"
    endpoint_port: int = Field(ge=1, le=65535)
    local_bind_host: Literal["127.0.0.1"] = "127.0.0.1"
    local_bind_port: int = Field(ge=0, le=65535)
    target_system: Literal[DEFAULT_PX4_TARGET_SYSTEM] = DEFAULT_PX4_TARGET_SYSTEM
    target_component: Literal[DEFAULT_PX4_TARGET_COMPONENT] = (
        DEFAULT_PX4_TARGET_COMPONENT
    )
    frame_length_bytes: int = Field(ge=0)
    heartbeat_frames_sent_before_command: int = Field(ge=0)
    mavlink_socket_opened: bool
    mavlink_frame_sent: bool
    frame_sent: bool
    recovery_command_sent: bool
    command_ack_wait_performed: Literal[True] = True
    command_ack_observed: bool
    command_ack_result_code: int | None = None
    command_ack_result_name: str | None = None
    ack_timeout_seconds: float = Field(gt=0)
    blocked_reasons: tuple[str, ...] = ()
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_blocked_reasons(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_dispatch(self) -> "PX4GazeboEmergencyCommandDispatchResult":
        if self.command_name != _command_name(self.command_id):
            raise PX4GazeboEmergencyDispatcherError(
                "emergency dispatch command name does not match command id"
            )
        if self.recovery_action != _command_action(self.command_id):
            raise PX4GazeboEmergencyDispatcherError(
                "emergency dispatch action does not match command id"
            )
        sent = self.dispatch_status in {
            PX4GazeboEmergencyCommandDispatchStatus.ACCEPTED,
            PX4GazeboEmergencyCommandDispatchStatus.REJECTED,
            PX4GazeboEmergencyCommandDispatchStatus.TIMEOUT,
        }
        if sent:
            if not (
                self.mavlink_socket_opened
                and self.mavlink_frame_sent
                and self.frame_sent
                and self.recovery_command_sent
            ):
                raise PX4GazeboEmergencyDispatcherError(
                    "sent emergency dispatch requires socket and frame evidence"
                )
            if self.approval_ref is None or self.allowlist_ref is None:
                raise PX4GazeboEmergencyDispatcherError(
                    "sent emergency dispatch requires approval and allowlist refs"
                )
        else:
            if self.mavlink_frame_sent or self.frame_sent or self.recovery_command_sent:
                raise PX4GazeboEmergencyDispatcherError(
                    "blocked emergency dispatch must not send a frame"
                )
            if not self.blocked_reasons:
                raise PX4GazeboEmergencyDispatcherError(
                    "blocked emergency dispatch requires blocked reasons"
                )
        if self.dispatch_status == PX4GazeboEmergencyCommandDispatchStatus.ACCEPTED:
            if (
                not self.command_ack_observed
                or self.command_ack_result_code != MAV_RESULT_ACCEPTED
            ):
                raise PX4GazeboEmergencyDispatcherError(
                    "accepted emergency dispatch requires accepted ACK"
                )
            if self.blocked_reasons:
                raise PX4GazeboEmergencyDispatcherError(
                    "accepted emergency dispatch cannot include blocked reasons"
                )
        if self.dispatch_status == PX4GazeboEmergencyCommandDispatchStatus.REJECTED:
            if (
                not self.command_ack_observed
                or self.command_ack_result_code is None
                or self.command_ack_result_code == MAV_RESULT_ACCEPTED
            ):
                raise PX4GazeboEmergencyDispatcherError(
                    "rejected emergency dispatch requires non-accepted ACK"
                )
            if "emergency_command_rejected" not in self.blocked_reasons:
                raise PX4GazeboEmergencyDispatcherError(
                    "rejected emergency dispatch requires rejected reason"
                )
        if self.dispatch_status == PX4GazeboEmergencyCommandDispatchStatus.TIMEOUT:
            if self.command_ack_observed or self.command_ack_result_code is not None:
                raise PX4GazeboEmergencyDispatcherError(
                    "timeout emergency dispatch cannot include ACK payload"
                )
            if "emergency_command_ack_timeout" not in self.blocked_reasons:
                raise PX4GazeboEmergencyDispatcherError(
                    "timeout emergency dispatch requires timeout reason"
                )
        return self


class PX4GazeboEmergencyAckTransportDiagnostic(_EmergencyCommandSafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_EMERGENCY_ACK_TRANSPORT_DIAGNOSTIC_SCHEMA_VERSION
    ] = PX4_GAZEBO_EMERGENCY_ACK_TRANSPORT_DIAGNOSTIC_SCHEMA_VERSION
    diagnostic_id: str
    transport_mode: str = Field(min_length=1)
    support_status: PX4GazeboEmergencyAckTransportSupportStatus
    command_id: int
    command_name: str = Field(min_length=1)
    frame_sent: bool
    command_ack_observed: bool
    command_ack_result_code: int | None = None
    command_ack_result_name: str | None = None
    ack_complete_transport_supported: bool
    px4_state_observed: bool
    px4_state_label: str | None = None
    completion_basis: Literal[
        "ack_observed_and_state_observed",
        "state_observed_after_dispatch_timeout",
        "state_not_observed_after_dispatch_timeout",
        "dispatch_blocked_before_send",
    ]
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_diagnostic(self) -> "PX4GazeboEmergencyAckTransportDiagnostic":
        if self.command_name != _command_name(self.command_id):
            raise PX4GazeboEmergencyDispatcherError(
                "ACK transport diagnostic command name does not match command id"
            )
        if self.support_status == (
            PX4GazeboEmergencyAckTransportSupportStatus.ACK_COMPLETE_SUPPORTED
        ):
            if not (
                self.frame_sent
                and self.command_ack_observed
                and self.command_ack_result_code == MAV_RESULT_ACCEPTED
                and self.command_ack_result_name == "ACCEPTED"
                and self.ack_complete_transport_supported
                and self.px4_state_observed
                and self.px4_state_label
                and self.completion_basis == "ack_observed_and_state_observed"
            ):
                raise PX4GazeboEmergencyDispatcherError(
                    "ACK-complete transport diagnostic requires accepted ACK and state evidence"
                )
        if self.support_status == (
            PX4GazeboEmergencyAckTransportSupportStatus.ACK_UNAVAILABLE_STATE_OBSERVED
        ):
            if not (
                self.frame_sent
                and not self.command_ack_observed
                and self.command_ack_result_code is None
                and self.command_ack_result_name is None
                and not self.ack_complete_transport_supported
                and self.px4_state_observed
                and self.px4_state_label
                and self.completion_basis == "state_observed_after_dispatch_timeout"
            ):
                raise PX4GazeboEmergencyDispatcherError(
                    "ACK-unavailable state-observed diagnostic requires sent frame, no ACK, and state evidence"
                )
        if self.support_status == (
            PX4GazeboEmergencyAckTransportSupportStatus.ACK_UNAVAILABLE_STATE_NOT_OBSERVED
        ):
            if not (
                self.frame_sent
                and not self.command_ack_observed
                and self.command_ack_result_code is None
                and self.command_ack_result_name is None
                and not self.ack_complete_transport_supported
                and not self.px4_state_observed
                and self.px4_state_label is None
                and self.completion_basis == "state_not_observed_after_dispatch_timeout"
            ):
                raise PX4GazeboEmergencyDispatcherError(
                    "ACK-unavailable unconfirmed diagnostic requires sent frame, no ACK, and no state evidence"
                )
        if (
            self.support_status
            == PX4GazeboEmergencyAckTransportSupportStatus.DISPATCH_BLOCKED
        ):
            if not (
                not self.frame_sent
                and not self.command_ack_observed
                and self.command_ack_result_code is None
                and self.command_ack_result_name is None
                and not self.ack_complete_transport_supported
                and not self.px4_state_observed
                and self.px4_state_label is None
                and self.completion_basis == "dispatch_blocked_before_send"
            ):
                raise PX4GazeboEmergencyDispatcherError(
                    "blocked ACK transport diagnostic requires no frame, no ACK, and no state evidence"
                )
        return self


def build_px4_gazebo_emergency_command_approval(
    *,
    operator_approval_performed: bool,
    approved_recovery_actions: Sequence[PX4GazeboRouteRecoveryAction | str] = (
        PX4GazeboRouteRecoveryAction.HOLD,
        PX4GazeboRouteRecoveryAction.LAND,
        PX4GazeboRouteRecoveryAction.RETURN_TO_LAUNCH,
    ),
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboEmergencyCommandApproval:
    approved_at = _utc(now)
    actions = (
        _ordered_actions(approved_recovery_actions)
        if operator_approval_performed
        else ()
    )
    command_ids = tuple(_action_command_id(action) for action in actions)
    payload = {
        "operator_approval_performed": bool(operator_approval_performed),
        "actions": [action.value for action in actions],
        "approved_at": approved_at.isoformat(),
    }
    return PX4GazeboEmergencyCommandApproval(
        approval_id=_stable_id("px4_gazebo_emergency_command_approval", payload),
        operator_approval_performed=bool(operator_approval_performed),
        approved_recovery_actions=actions,
        approved_command_ids=command_ids,
        approved_at=approved_at,
        metadata={**(metadata or {}), "issue": 360, "parent_epic": 356},
    )


def build_px4_gazebo_emergency_command_allowlist(
    *,
    approval: PX4GazeboEmergencyCommandApproval | Mapping[str, Any],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboEmergencyCommandAllowlist:
    resolved_approval = _coerce_approval(approval)
    assert resolved_approval is not None
    if resolved_approval.operator_approval_performed is not True:
        raise PX4GazeboEmergencyDispatcherError(
            "emergency command allowlist requires operator approval"
        )
    generated_at = _utc(now)
    payload = {
        "approval_id": resolved_approval.approval_id,
        "actions": [
            action.value for action in resolved_approval.approved_recovery_actions
        ],
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboEmergencyCommandAllowlist(
        allowlist_id=_stable_id("px4_gazebo_emergency_command_allowlist", payload),
        approval_ref=_approval_ref(resolved_approval),
        allowed_recovery_actions=resolved_approval.approved_recovery_actions,
        allowed_command_ids=resolved_approval.approved_command_ids,
        allowed_command_names=tuple(
            _command_name(command_id)
            for command_id in resolved_approval.approved_command_ids
        ),
        generated_at=generated_at,
        metadata={**(metadata or {}), "issue": 360, "parent_epic": 356},
    )


def _blocked_dispatch_result(
    *,
    recovery_action: PX4GazeboRouteRecoveryAction,
    endpoint_port: int,
    blocked_reasons: Sequence[str],
    approval: PX4GazeboEmergencyCommandApproval | None,
    allowlist: PX4GazeboEmergencyCommandAllowlist | None,
    ack_timeout_seconds: float,
    now: datetime | None,
) -> PX4GazeboEmergencyCommandDispatchResult:
    observed_at = _utc(now)
    command_id = _action_command_id(recovery_action)
    payload = {
        "action": recovery_action.value,
        "command_id": command_id,
        "blocked_reasons": list(blocked_reasons),
        "observed_at": observed_at.isoformat(),
    }
    return PX4GazeboEmergencyCommandDispatchResult(
        dispatch_result_id=_stable_id("px4_gazebo_emergency_command_dispatch", payload),
        dispatch_status=PX4GazeboEmergencyCommandDispatchStatus.BLOCKED,
        approval_ref=None if approval is None else _approval_ref(approval),
        allowlist_ref=None if allowlist is None else _allowlist_ref(allowlist),
        recovery_action=recovery_action,
        command_id=command_id,
        command_name=_command_name(command_id),
        endpoint_port=endpoint_port,
        local_bind_port=0,
        frame_length_bytes=0,
        heartbeat_frames_sent_before_command=0,
        mavlink_socket_opened=False,
        mavlink_frame_sent=False,
        frame_sent=False,
        recovery_command_sent=False,
        command_ack_observed=False,
        ack_timeout_seconds=ack_timeout_seconds,
        blocked_reasons=tuple(blocked_reasons),
        observed_at=observed_at,
        metadata={"issue": 360, "parent_epic": 356},
    )


def build_px4_gazebo_emergency_command_dispatch_result(
    *,
    approval: PX4GazeboEmergencyCommandApproval | Mapping[str, Any],
    allowlist: PX4GazeboEmergencyCommandAllowlist | Mapping[str, Any],
    recovery_action: PX4GazeboRouteRecoveryAction | str,
    endpoint_port: int,
    local_bind_port: int,
    frame_length_bytes: int,
    ack_observed: bool,
    ack_result_code: int | None,
    ack_timeout_seconds: float = EMERGENCY_ACK_TIMEOUT_SECONDS,
    heartbeat_frames_sent_before_command: int = EMERGENCY_HEARTBEAT_WARMUP_FRAMES,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboEmergencyCommandDispatchResult:
    resolved_approval = _coerce_approval(approval)
    resolved_allowlist = _coerce_allowlist(allowlist)
    assert resolved_approval is not None
    assert resolved_allowlist is not None
    action = (
        recovery_action
        if isinstance(recovery_action, PX4GazeboRouteRecoveryAction)
        else PX4GazeboRouteRecoveryAction(str(recovery_action))
    )
    command_id = _action_command_id(action)
    observed_at = _utc(now)
    if ack_observed:
        observed_code = -1 if ack_result_code is None else int(ack_result_code)
        result_name = _ACK_RESULT_NAMES.get(observed_code, "UNKNOWN")
        status = (
            PX4GazeboEmergencyCommandDispatchStatus.ACCEPTED
            if observed_code == MAV_RESULT_ACCEPTED
            else PX4GazeboEmergencyCommandDispatchStatus.REJECTED
        )
        blocked = (
            ()
            if status == PX4GazeboEmergencyCommandDispatchStatus.ACCEPTED
            else ("emergency_command_rejected",)
        )
    else:
        result_name = None
        status = PX4GazeboEmergencyCommandDispatchStatus.TIMEOUT
        blocked = ("emergency_command_ack_timeout",)
    payload = {
        "approval_id": resolved_approval.approval_id,
        "allowlist_id": resolved_allowlist.allowlist_id,
        "action": action.value,
        "command_id": command_id,
        "ack_observed": bool(ack_observed),
        "ack_result_code": ack_result_code,
        "observed_at": observed_at.isoformat(),
    }
    return PX4GazeboEmergencyCommandDispatchResult(
        dispatch_result_id=_stable_id("px4_gazebo_emergency_command_dispatch", payload),
        dispatch_status=status,
        approval_ref=_approval_ref(resolved_approval),
        allowlist_ref=_allowlist_ref(resolved_allowlist),
        recovery_action=action,
        command_id=command_id,
        command_name=_command_name(command_id),
        endpoint_port=endpoint_port,
        local_bind_port=local_bind_port,
        frame_length_bytes=int(frame_length_bytes),
        heartbeat_frames_sent_before_command=int(heartbeat_frames_sent_before_command),
        mavlink_socket_opened=True,
        mavlink_frame_sent=True,
        frame_sent=True,
        recovery_command_sent=True,
        command_ack_observed=bool(ack_observed),
        command_ack_result_code=(
            None if ack_result_code is None else int(ack_result_code)
        ),
        command_ack_result_name=result_name,
        ack_timeout_seconds=ack_timeout_seconds,
        blocked_reasons=blocked,
        observed_at=observed_at,
        metadata={**(metadata or {}), "issue": 360, "parent_epic": 356},
    )


def build_px4_gazebo_emergency_ack_transport_diagnostic(
    *,
    transport_mode: str,
    command_id: int,
    command_name: str,
    frame_sent: bool,
    command_ack_observed: bool,
    command_ack_result_code: int | None,
    command_ack_result_name: str | None,
    px4_state_observed: bool,
    px4_state_label: str | None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboEmergencyAckTransportDiagnostic:
    observed_at = _utc(now)
    ack_complete = (
        bool(frame_sent)
        and bool(command_ack_observed)
        and command_ack_result_code == MAV_RESULT_ACCEPTED
        and bool(px4_state_observed)
    )
    if ack_complete:
        support_status = (
            PX4GazeboEmergencyAckTransportSupportStatus.ACK_COMPLETE_SUPPORTED
        )
        completion_basis = "ack_observed_and_state_observed"
    elif not frame_sent:
        support_status = PX4GazeboEmergencyAckTransportSupportStatus.DISPATCH_BLOCKED
        completion_basis = "dispatch_blocked_before_send"
    elif px4_state_observed:
        support_status = (
            PX4GazeboEmergencyAckTransportSupportStatus.ACK_UNAVAILABLE_STATE_OBSERVED
        )
        completion_basis = "state_observed_after_dispatch_timeout"
    else:
        support_status = (
            PX4GazeboEmergencyAckTransportSupportStatus.ACK_UNAVAILABLE_STATE_NOT_OBSERVED
        )
        completion_basis = "state_not_observed_after_dispatch_timeout"
    payload = {
        "transport_mode": transport_mode,
        "command_id": int(command_id),
        "frame_sent": bool(frame_sent),
        "command_ack_observed": bool(command_ack_observed),
        "command_ack_result_code": command_ack_result_code,
        "px4_state_observed": bool(px4_state_observed),
        "completion_basis": completion_basis,
        "observed_at": observed_at.isoformat(),
    }
    return PX4GazeboEmergencyAckTransportDiagnostic(
        diagnostic_id=_stable_id("px4_gazebo_emergency_ack_transport", payload),
        transport_mode=str(transport_mode),
        support_status=support_status,
        command_id=int(command_id),
        command_name=str(command_name),
        frame_sent=bool(frame_sent),
        command_ack_observed=bool(command_ack_observed),
        command_ack_result_code=(
            None if command_ack_result_code is None else int(command_ack_result_code)
        ),
        command_ack_result_name=command_ack_result_name,
        ack_complete_transport_supported=ack_complete,
        px4_state_observed=bool(px4_state_observed),
        px4_state_label=px4_state_label if px4_state_observed else None,
        completion_basis=completion_basis,  # type: ignore[arg-type]
        observed_at=observed_at,
        metadata={**(metadata or {}), "issue": 371, "parent_epic": 356},
    )


def _validate_dispatch_inputs(
    *,
    approval: PX4GazeboEmergencyCommandApproval | None,
    allowlist: PX4GazeboEmergencyCommandAllowlist | None,
    recovery_action: PX4GazeboRouteRecoveryAction,
) -> tuple[str, ...]:
    blocked: list[str] = []
    if approval is None or approval.operator_approval_performed is not True:
        blocked.append("missing_emergency_command_approval")
    if allowlist is None:
        blocked.append("missing_emergency_command_allowlist")
    if approval is not None and allowlist is not None:
        if allowlist.approval_ref != _approval_ref(approval):
            blocked.append("emergency_allowlist_approval_mismatch")
        if recovery_action not in allowlist.allowed_recovery_actions:
            blocked.append("emergency_action_not_allowlisted")
        if _action_command_id(recovery_action) not in allowlist.allowed_command_ids:
            blocked.append("emergency_command_not_allowlisted")
    return tuple(blocked)


def _wait_emergency_command_ack(
    *,
    sock: socket.socket,
    command_id: int,
    timeout_seconds: float,
) -> tuple[bool, int | None]:
    sock.settimeout(timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            data, _addr = sock.recvfrom(2048)
        except socket.timeout:
            return (False, None)
        try:
            decoded = _decode_mavlink_ack_candidate(data)
        except Exception:
            continue
        if decoded["msg_id"] != MAVLINK_MSG_ID_COMMAND_ACK:
            continue
        ack = _decode_command_ack_payload(decoded["payload"])
        if int(ack["command_id"]) == command_id:
            return (True, int(ack["result_code"]))
    return (False, None)


def _decode_mavlink_ack_candidate(frame: bytes) -> dict[str, Any]:
    if len(frame) >= 8 and frame[0] == MAVLINK1_MAGIC:
        payload_len = frame[1]
        msg_id = frame[5]
        payload = frame[6 : 6 + payload_len]
        return {"msg_id": msg_id, "payload": payload}
    return decode_mavlink2_frame(frame)


def _decode_command_ack_payload(payload: bytes) -> dict[str, int | str]:
    if len(payload) < 10:
        raise PX4GazeboEmergencyDispatcherError("COMMAND_ACK payload is incomplete")
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


def run_px4_gazebo_emergency_command_dispatch(
    *,
    recovery_action: PX4GazeboRouteRecoveryAction | str,
    approval: PX4GazeboEmergencyCommandApproval | Mapping[str, Any] | None,
    allowlist: PX4GazeboEmergencyCommandAllowlist | Mapping[str, Any] | None,
    endpoint_host: str = "127.0.0.1",
    endpoint_port: int = 18570,
    local_bind_port: int = 0,
    live_mavlink_opt_in: bool,
    ack_timeout_seconds: float = EMERGENCY_ACK_TIMEOUT_SECONDS,
    heartbeat_warmup_frames: int = EMERGENCY_HEARTBEAT_WARMUP_FRAMES,
    heartbeat_warmup_interval_seconds: float = (
        EMERGENCY_HEARTBEAT_WARMUP_INTERVAL_SECONDS
    ),
    now: datetime | None = None,
) -> PX4GazeboEmergencyCommandDispatchResult:
    action = _coerce_recovery_action(recovery_action)
    _validate_endpoint(
        endpoint_host=endpoint_host,
        endpoint_port=endpoint_port,
        local_bind_port=local_bind_port,
        live_mavlink_opt_in=live_mavlink_opt_in,
    )
    resolved_approval = _coerce_approval(approval)
    resolved_allowlist = _coerce_allowlist(allowlist)
    blocked = _validate_dispatch_inputs(
        approval=resolved_approval,
        allowlist=resolved_allowlist,
        recovery_action=action,
    )
    if blocked:
        return _blocked_dispatch_result(
            recovery_action=action,
            endpoint_port=endpoint_port,
            blocked_reasons=blocked,
            approval=resolved_approval,
            allowlist=resolved_allowlist,
            ack_timeout_seconds=ack_timeout_seconds,
            now=now,
        )
    assert resolved_approval is not None
    assert resolved_allowlist is not None
    command_id = _action_command_id(action)
    heartbeat_count = max(1, int(heartbeat_warmup_frames))
    heartbeat_interval = max(0.0, float(heartbeat_warmup_interval_seconds))
    frame = encode_px4_gazebo_emergency_command_long(
        command_id=command_id,
        sequence=heartbeat_count,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", local_bind_port))
        local_host, local_port = sock.getsockname()
        if str(local_host) != "127.0.0.1":
            raise PX4GazeboEmergencyDispatcherError(
                "emergency command dispatch must bind from loopback"
            )
        remote = (endpoint_host, endpoint_port)
        for sequence in range(heartbeat_count):
            sock.sendto(encode_mavlink2_heartbeat(sequence=sequence), remote)
            if heartbeat_interval and sequence < heartbeat_count - 1:
                time.sleep(heartbeat_interval)
        sock.sendto(frame, remote)
        ack_observed, ack_result_code = _wait_emergency_command_ack(
            sock=sock,
            command_id=command_id,
            timeout_seconds=ack_timeout_seconds,
        )
    return build_px4_gazebo_emergency_command_dispatch_result(
        approval=resolved_approval,
        allowlist=resolved_allowlist,
        recovery_action=action,
        endpoint_port=endpoint_port,
        local_bind_port=int(local_port),
        frame_length_bytes=len(frame),
        ack_observed=ack_observed,
        ack_result_code=ack_result_code,
        ack_timeout_seconds=ack_timeout_seconds,
        heartbeat_frames_sent_before_command=heartbeat_count,
        now=now,
    )


__all__ = [
    "EMERGENCY_ACK_TIMEOUT_SECONDS",
    "EMERGENCY_HEARTBEAT_WARMUP_FRAMES",
    "EMERGENCY_HEARTBEAT_WARMUP_INTERVAL_SECONDS",
    "MAV_CMD_NAV_LAND",
    "MAV_CMD_NAV_LOITER_UNLIM",
    "MAV_CMD_NAV_RETURN_TO_LAUNCH",
    "PX4_GAZEBO_EMERGENCY_ACK_TRANSPORT_DIAGNOSTIC_SCHEMA_VERSION",
    "PX4_GAZEBO_EMERGENCY_COMMAND_ALLOWLIST_SCHEMA_VERSION",
    "PX4_GAZEBO_EMERGENCY_COMMAND_APPROVAL_SCHEMA_VERSION",
    "PX4_GAZEBO_EMERGENCY_COMMAND_DISPATCH_RESULT_SCHEMA_VERSION",
    "PX4GazeboEmergencyAckTransportDiagnostic",
    "PX4GazeboEmergencyAckTransportSupportStatus",
    "PX4GazeboEmergencyCommandAllowlist",
    "PX4GazeboEmergencyCommandApproval",
    "PX4GazeboEmergencyCommandDispatchResult",
    "PX4GazeboEmergencyCommandDispatchStatus",
    "PX4GazeboEmergencyDispatcherError",
    "build_px4_gazebo_emergency_ack_transport_diagnostic",
    "build_px4_gazebo_emergency_command_allowlist",
    "build_px4_gazebo_emergency_command_approval",
    "build_px4_gazebo_emergency_command_dispatch_result",
    "encode_px4_gazebo_emergency_command_long",
    "run_px4_gazebo_emergency_command_dispatch",
]
