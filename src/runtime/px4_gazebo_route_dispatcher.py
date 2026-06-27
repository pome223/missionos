"""Bounded route command dispatcher for PX4/Gazebo pickup-dropoff delivery."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import socket
import struct
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.px4_gazebo_coupled_delivery import PX4GazeboCoupledCommandApproval
from src.runtime.px4_gazebo_route_plan import PX4GazeboPickupDropoffRoutePlan
from src.runtime.px4_live_mavlink_dispatcher import (
    DEFAULT_PX4_TARGET_COMPONENT,
    DEFAULT_PX4_TARGET_SYSTEM,
)
from src.runtime.px4_real_mavlink_transport import (
    MAVLINK_MSG_ID_COMMAND_LONG,
    MAVLINK_MSG_ID_SET_POSITION_TARGET_LOCAL_NED,
    encode_mavlink2_frame,
    encode_mavlink2_heartbeat,
)

PX4_GAZEBO_ROUTE_COMMAND_ALLOWLIST_SCHEMA_VERSION = (
    "px4_gazebo_route_command_allowlist.v1"
)
PX4_GAZEBO_ROUTE_COMMAND_DISPATCH_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_route_command_dispatch_result.v2"
)
PX4_GAZEBO_ROUTE_PROGRESS_EVIDENCE_SCHEMA_VERSION = (
    "px4_gazebo_route_progress_evidence.v2"
)
PX4_GAZEBO_ROUTE_DEVIATION_ABORT_SCHEMA_VERSION = "px4_gazebo_route_deviation_abort.v1"
PX4_GAZEBO_ROUTE_RECOVERY_COMPLETION_SCHEMA_VERSION = (
    "px4_gazebo_route_recovery_completion.v1"
)

MAV_FRAME_LOCAL_NED = 1
MAV_CMD_DO_SET_MODE = 176
ROUTE_SETPOINT_BURST_MAX_FRAMES = 5
ROUTE_SETPOINT_STREAM_MAX_FRAMES = 700
ROUTE_SETPOINT_STREAM_MAX_DURATION_SECONDS = 30.0
ROUTE_OFFBOARD_ACK_TIMEOUT_SECONDS = 5.0


class PX4GazeboRouteDispatcherError(RuntimeError):
    """Raised when bounded route dispatch cannot proceed safely."""


class PX4GazeboRouteDispatchStatus(str, Enum):
    SENT = "sent"
    BLOCKED = "blocked"


class PX4GazeboRouteRecoveryCompletionBasis(str, Enum):
    ACK_OBSERVED_AND_STATE_OBSERVED = "ack_observed_and_state_observed"
    STATE_OBSERVED_AFTER_DISPATCH_TIMEOUT = "state_observed_after_dispatch_timeout"
    STATE_NOT_OBSERVED_AFTER_DISPATCH_TIMEOUT = (
        "state_not_observed_after_dispatch_timeout"
    )
    DISPATCH_BLOCKED_BEFORE_SEND = "dispatch_blocked_before_send"


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


def _route_plan_ref(route_plan: PX4GazeboPickupDropoffRoutePlan) -> str:
    return f"px4_gazebo_pickup_dropoff_route_plan:{route_plan.route_plan_id}"


def _route_allowlist_ref(allowlist: "PX4GazeboRouteCommandAllowlist") -> str:
    return f"px4_gazebo_route_command_allowlist:{allowlist.allowlist_id}"


def _coerce_route_plan(
    value: PX4GazeboPickupDropoffRoutePlan | Mapping[str, Any],
) -> PX4GazeboPickupDropoffRoutePlan:
    if isinstance(value, PX4GazeboPickupDropoffRoutePlan):
        return value
    return PX4GazeboPickupDropoffRoutePlan.model_validate(dict(value))


def _coerce_approval(
    value: PX4GazeboCoupledCommandApproval | Mapping[str, Any],
) -> PX4GazeboCoupledCommandApproval:
    if isinstance(value, PX4GazeboCoupledCommandApproval):
        return value
    return PX4GazeboCoupledCommandApproval.model_validate(dict(value))


def _coerce_emergency_dispatch(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        payload = dict(value)
    elif hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
    else:
        raise PX4GazeboRouteDispatcherError(
            "route recovery completion requires emergency dispatch artifact"
        )
    required = {
        "dispatch_result_id",
        "dispatch_status",
        "recovery_action",
        "command_ack_observed",
        "command_ack_result_code",
        "frame_sent",
        "recovery_command_sent",
    }
    if missing := sorted(required - set(payload)):
        raise PX4GazeboRouteDispatcherError(
            f"emergency dispatch artifact missing required fields: {missing}"
        )
    _validate_emergency_dispatch_payload(payload)
    return payload


def _validate_emergency_dispatch_payload(payload: Mapping[str, Any]) -> None:
    status = str(payload["dispatch_status"])
    ack_observed = bool(payload["command_ack_observed"])
    ack_result = payload.get("command_ack_result_code")
    frame_sent = bool(payload["frame_sent"])
    recovery_command_sent = bool(payload["recovery_command_sent"])
    if status == "accepted":
        if not frame_sent or not recovery_command_sent:
            raise PX4GazeboRouteDispatcherError(
                "accepted recovery completion requires sent emergency dispatch evidence"
            )
        if not ack_observed or ack_result != 0:
            raise PX4GazeboRouteDispatcherError(
                "accepted recovery completion requires accepted emergency ACK evidence"
            )
        return
    if status == "timeout":
        if not frame_sent or not recovery_command_sent:
            raise PX4GazeboRouteDispatcherError(
                "timeout recovery completion requires sent emergency dispatch evidence"
            )
        if ack_observed or ack_result is not None:
            raise PX4GazeboRouteDispatcherError(
                "timeout recovery completion cannot include emergency ACK evidence"
            )
        return
    if status == "blocked":
        if frame_sent or recovery_command_sent:
            raise PX4GazeboRouteDispatcherError(
                "blocked recovery completion must not include sent emergency command evidence"
            )
        if ack_observed or ack_result is not None:
            raise PX4GazeboRouteDispatcherError(
                "blocked recovery completion must not include emergency ACK evidence"
            )
        return
    if status == "rejected":
        raise PX4GazeboRouteDispatcherError(
            "rejected emergency dispatch cannot complete route recovery"
        )


def _validate_endpoint(
    *, endpoint_host: str, endpoint_port: int, live_mavlink_opt_in: bool
) -> None:
    if live_mavlink_opt_in is not True:
        raise PX4GazeboRouteDispatcherError(
            "route command dispatch requires explicit live_mavlink_opt_in=true"
        )
    if endpoint_host != "127.0.0.1":
        raise PX4GazeboRouteDispatcherError(
            "route command dispatch endpoint_host must be 127.0.0.1"
        )
    if not 1 <= endpoint_port <= 65535:
        raise PX4GazeboRouteDispatcherError("endpoint port must fit uint16")


def _inside_bbox(
    *,
    point: tuple[float, float],
    polygon: tuple[tuple[float, float], ...],
) -> bool:
    xs = [item[0] for item in polygon]
    ys = [item[1] for item in polygon]
    return min(xs) <= point[0] <= max(xs) and min(ys) <= point[1] <= max(ys)


def _route_target_xy(
    route_plan: PX4GazeboPickupDropoffRoutePlan,
) -> tuple[float, float]:
    xs = [item[0] for item in route_plan.geofence_polygon]
    ys = [item[1] for item in route_plan.geofence_polygon]
    return (max(xs) - route_plan.route_completion_radius_m, max(ys) / 2.0)


def derive_px4_gazebo_route_target_ned(
    route_plan: PX4GazeboPickupDropoffRoutePlan | Mapping[str, Any],
) -> tuple[float, float, float]:
    resolved_route = _coerce_route_plan(route_plan)
    target_x, target_y = _route_target_xy(resolved_route)
    return (target_x, target_y, -resolved_route.altitude_max_m)


def _validate_route_target(
    *,
    route_plan: PX4GazeboPickupDropoffRoutePlan,
    target_x_m: float,
    target_y_m: float,
    target_z_m: float,
) -> None:
    expected_x, expected_y, expected_z = derive_px4_gazebo_route_target_ned(route_plan)
    if (
        abs(float(target_x_m) - expected_x) > 1e-6
        or abs(float(target_y_m) - expected_y) > 1e-6
        or abs(float(target_z_m) - expected_z) > 1e-6
    ):
        raise PX4GazeboRouteDispatcherError("route target does not match route plan")
    if not _inside_bbox(
        point=(float(target_x_m), float(target_y_m)),
        polygon=route_plan.geofence_polygon,
    ):
        raise PX4GazeboRouteDispatcherError("route target is outside geofence")
    dropoff_distance = (
        (float(target_x_m) - expected_x) ** 2 + (float(target_y_m) - expected_y) ** 2
    ) ** 0.5
    if dropoff_distance > route_plan.route_completion_radius_m:
        raise PX4GazeboRouteDispatcherError("route target is outside dropoff radius")
    altitude_m = abs(float(target_z_m))
    if not route_plan.altitude_min_m <= altitude_m <= route_plan.altitude_max_m:
        raise PX4GazeboRouteDispatcherError("route target altitude outside bounds")


def encode_set_position_target_local_ned(
    *,
    x_m: float,
    y_m: float,
    z_m: float,
    sequence: int = 20,
    target_system: int = DEFAULT_PX4_TARGET_SYSTEM,
    target_component: int = DEFAULT_PX4_TARGET_COMPONENT,
    system_id: int = 255,
    component_id: int = 190,
) -> bytes:
    type_mask_position_only = 0b0000_1101_1111_1000
    payload = struct.pack(
        "<IfffffffffffHBBB",
        0,
        float(x_m),
        float(y_m),
        float(z_m),
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        type_mask_position_only,
        int(target_system),
        int(target_component),
        MAV_FRAME_LOCAL_NED,
    )
    return encode_mavlink2_frame(
        msg_id=MAVLINK_MSG_ID_SET_POSITION_TARGET_LOCAL_NED,
        payload=payload,
        sequence=sequence,
        system_id=system_id,
        component_id=component_id,
    )


class _RouteDispatchSafetyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[True] = True
    bounded_allowlist_enforced: Literal[True] = True
    route_plan_required: Literal[True] = True
    live_mavlink_opt_in_required: Literal[True] = True
    live_mavlink_opt_in_performed: Literal[True] = True
    hardware_target_allowed: Literal[False] = False
    real_world_authority_granted: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    arbitrary_mission_upload_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    retry_attempted: Literal[False] = False
    stronger_execution_attempted: Literal[False] = False


class PX4GazeboRouteCommandAllowlist(_RouteDispatchSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_ROUTE_COMMAND_ALLOWLIST_SCHEMA_VERSION] = (
        PX4_GAZEBO_ROUTE_COMMAND_ALLOWLIST_SCHEMA_VERSION
    )
    allowlist_id: str
    route_plan_ref: str = Field(min_length=1)
    operator_approval_ref: str = Field(min_length=1)
    allowed_mavlink_message_ids: tuple[int, ...]
    allowed_command_ids: tuple[int, ...] = (MAV_CMD_DO_SET_MODE,)
    allowed_route_primitive: Literal["bounded_position_setpoint_burst"] = (
        "bounded_position_setpoint_burst"
    )
    allowed_route_primitives: tuple[
        Literal["bounded_position_setpoint_burst", "bounded_position_setpoint_stream"],
        ...,
    ] = ("bounded_position_setpoint_burst", "bounded_position_setpoint_stream")
    max_setpoint_frames: Literal[ROUTE_SETPOINT_BURST_MAX_FRAMES] = (
        ROUTE_SETPOINT_BURST_MAX_FRAMES
    )
    max_setpoint_stream_frames: Literal[ROUTE_SETPOINT_STREAM_MAX_FRAMES] = (
        ROUTE_SETPOINT_STREAM_MAX_FRAMES
    )
    max_setpoint_stream_duration_seconds: Literal[
        ROUTE_SETPOINT_STREAM_MAX_DURATION_SECONDS
    ] = ROUTE_SETPOINT_STREAM_MAX_DURATION_SECONDS
    bounded_setpoint_stream_allowed: Literal[True] = True
    offboard_mode_switch_allowed: Literal[True] = True
    offboard_mode_switch_command_id: Literal[MAV_CMD_DO_SET_MODE] = MAV_CMD_DO_SET_MODE
    generated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("allowed_mavlink_message_ids", mode="before")
    @classmethod
    def _coerce_message_ids(cls, value: Any) -> tuple[int, ...]:
        return tuple(int(item) for item in value)

    @field_validator("allowed_command_ids", mode="before")
    @classmethod
    def _coerce_command_ids(cls, value: Any) -> tuple[int, ...]:
        return tuple(int(item) for item in value)

    @model_validator(mode="after")
    def _validate_allowlist(self) -> "PX4GazeboRouteCommandAllowlist":
        if self.allowed_mavlink_message_ids != (
            MAVLINK_MSG_ID_SET_POSITION_TARGET_LOCAL_NED,
            MAVLINK_MSG_ID_COMMAND_LONG,
        ):
            raise PX4GazeboRouteDispatcherError(
                "route allowlist must permit only SET_POSITION_TARGET_LOCAL_NED "
                "and COMMAND_LONG"
            )
        if self.allowed_command_ids != (MAV_CMD_DO_SET_MODE,):
            raise PX4GazeboRouteDispatcherError(
                "route allowlist must permit only MAV_CMD_DO_SET_MODE"
            )
        return self


class PX4GazeboRouteCommandDispatchResult(_RouteDispatchSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_ROUTE_COMMAND_DISPATCH_RESULT_SCHEMA_VERSION] = (
        PX4_GAZEBO_ROUTE_COMMAND_DISPATCH_RESULT_SCHEMA_VERSION
    )
    dispatch_result_id: str
    route_plan_ref: str = Field(min_length=1)
    route_allowlist_ref: str = Field(min_length=1)
    mavlink_message_id: Literal[MAVLINK_MSG_ID_SET_POSITION_TARGET_LOCAL_NED] = (
        MAVLINK_MSG_ID_SET_POSITION_TARGET_LOCAL_NED
    )
    mavlink_message_name: Literal["SET_POSITION_TARGET_LOCAL_NED"] = (
        "SET_POSITION_TARGET_LOCAL_NED"
    )
    route_dispatch_status: PX4GazeboRouteDispatchStatus
    route_primitive: Literal[
        "bounded_position_setpoint_burst", "bounded_position_setpoint_stream"
    ] = "bounded_position_setpoint_burst"
    endpoint_host: Literal["127.0.0.1"] = "127.0.0.1"
    endpoint_port: int = Field(ge=1, le=65535)
    target_system: Literal[DEFAULT_PX4_TARGET_SYSTEM] = DEFAULT_PX4_TARGET_SYSTEM
    target_component: Literal[DEFAULT_PX4_TARGET_COMPONENT] = (
        DEFAULT_PX4_TARGET_COMPONENT
    )
    target_x_m: float
    target_y_m: float
    target_z_m: float
    setpoint_frames_sent: int = Field(ge=0, le=ROUTE_SETPOINT_STREAM_MAX_FRAMES)
    setpoint_stream_duration_seconds: float = Field(
        default=0.0, ge=0, le=ROUTE_SETPOINT_STREAM_MAX_DURATION_SECONDS
    )
    bounded_setpoint_stream_allowed: Literal[True] = True
    offboard_mode_switch_allowed: Literal[True] = True
    offboard_mode_switch_command_id: Literal[MAV_CMD_DO_SET_MODE] = MAV_CMD_DO_SET_MODE
    offboard_mode_switch_frame_sent: bool = False
    offboard_mode_switch_ack_required: bool = False
    offboard_mode_switch_ack_command_id: Literal[MAV_CMD_DO_SET_MODE] = (
        MAV_CMD_DO_SET_MODE
    )
    offboard_mode_switch_ack_timeout_seconds: float | None = None
    offboard_mode_switch_ack_observed: bool = False
    offboard_mode_switch_ack_result_code: int | None = None
    offboard_mode_switch_ack_result_name: str | None = None
    mavlink_socket_opened: Literal[True] = True
    mavlink_frame_sent: Literal[True] = True
    route_command_frame_sent: bool = True
    route_command_ack_applicable: Literal[False] = False
    route_command_ack_observed: Literal[False] = False
    horizontal_route_motion_observed: Literal[False] = False
    route_state_wait_deferred_to_issue: Literal[345] = 345
    telemetry_completion_gate_deferred_to_issue: Literal[346] = 346
    blocked_reasons: tuple[str, ...] = ()
    sent_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sent_at", mode="before")
    @classmethod
    def _coerce_sent_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_blocked_reasons(cls, value: Any) -> tuple[str, ...]:
        return tuple(str(item) for item in value or ())

    @model_validator(mode="after")
    def _validate_dispatch_result(self) -> "PX4GazeboRouteCommandDispatchResult":
        is_stream = self.route_primitive == "bounded_position_setpoint_stream"
        if is_stream:
            if self.offboard_mode_switch_frame_sent is not True:
                raise PX4GazeboRouteDispatcherError(
                    "route setpoint stream requires offboard mode switch frame"
                )
            if self.offboard_mode_switch_ack_required is not True:
                raise PX4GazeboRouteDispatcherError(
                    "route setpoint stream requires offboard mode switch ACK gate"
                )
            if (
                self.offboard_mode_switch_ack_timeout_seconds is None
                or self.offboard_mode_switch_ack_timeout_seconds <= 0
            ):
                raise PX4GazeboRouteDispatcherError(
                    "route setpoint stream requires offboard ACK timeout"
                )
        if self.route_dispatch_status == PX4GazeboRouteDispatchStatus.SENT:
            if self.blocked_reasons:
                raise PX4GazeboRouteDispatcherError(
                    "sent route dispatch cannot include blocked reasons"
                )
            if (
                self.route_command_frame_sent is not True
                or self.setpoint_frames_sent < 1
            ):
                raise PX4GazeboRouteDispatcherError(
                    "sent route dispatch requires setpoint frames"
                )
            if is_stream and self.offboard_mode_switch_ack_observed is not True:
                raise PX4GazeboRouteDispatcherError(
                    "sent route setpoint stream requires offboard ACK observed"
                )
            if is_stream and self.offboard_mode_switch_ack_result_code != 0:
                raise PX4GazeboRouteDispatcherError(
                    "sent route setpoint stream requires accepted offboard ACK"
                )
        elif not self.blocked_reasons:
            raise PX4GazeboRouteDispatcherError(
                "blocked route dispatch requires blocked reasons"
            )
        return self


class PX4GazeboRouteProgressEvidence(_RouteDispatchSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_ROUTE_PROGRESS_EVIDENCE_SCHEMA_VERSION] = (
        PX4_GAZEBO_ROUTE_PROGRESS_EVIDENCE_SCHEMA_VERSION
    )
    progress_evidence_id: str
    route_plan_ref: str = Field(min_length=1)
    route_dispatch_result_ref: str = Field(min_length=1)
    pickup_pose_xy_m: tuple[float, float]
    dropoff_pose_xy_m: tuple[float, float]
    observed_pose_xy_m: tuple[float, float]
    horizontal_progress_m: float = Field(ge=0)
    dropoff_region_reached: bool
    route_geofence_violation: bool
    deviation_samples: tuple[dict[str, Any], ...] = ()
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator(
        "pickup_pose_xy_m", "dropoff_pose_xy_m", "observed_pose_xy_m", mode="before"
    )
    @classmethod
    def _coerce_xy(cls, value: Any) -> tuple[float, float]:
        x, y = value
        return (float(x), float(y))

    @field_validator("deviation_samples", mode="before")
    @classmethod
    def _coerce_deviation_samples(cls, value: Any) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in value or ())

    @model_validator(mode="after")
    def _validate_progress(self) -> "PX4GazeboRouteProgressEvidence":
        if self.route_geofence_violation and self.dropoff_region_reached:
            raise PX4GazeboRouteDispatcherError(
                "route progress cannot both reach dropoff and violate geofence"
            )
        for item in self.deviation_samples:
            if float(item.get("deviation_xy_m", 0.0)) < 0:
                raise PX4GazeboRouteDispatcherError(
                    "route deviation sample cannot have negative xy deviation"
                )
            if float(item.get("deviation_z_m", 0.0)) < 0:
                raise PX4GazeboRouteDispatcherError(
                    "route deviation sample cannot have negative z deviation"
                )
        return self


class PX4GazeboRouteDeviationAbort(_RouteDispatchSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_ROUTE_DEVIATION_ABORT_SCHEMA_VERSION] = (
        PX4_GAZEBO_ROUTE_DEVIATION_ABORT_SCHEMA_VERSION
    )
    abort_id: str
    final_status: Literal["aborted_pose_deviation"] = "aborted_pose_deviation"
    route_plan_ref: str = Field(min_length=1)
    route_allowlist_ref: str = Field(min_length=1)
    deviation_samples: tuple[dict[str, Any], ...]
    route_monitor_sample_count: int = Field(ge=1)
    setpoint_frames_sent: Literal[0] = 0
    recovery_dispatch_deferred_to_issue: Literal[360] = 360
    closed_loop_recovery_deferred_to_issue: Literal[361] = 361
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("deviation_samples", mode="before")
    @classmethod
    def _coerce_deviation_samples(cls, value: Any) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in value or ())

    @model_validator(mode="after")
    def _validate_abort(self) -> "PX4GazeboRouteDeviationAbort":
        if not self.deviation_samples:
            raise PX4GazeboRouteDispatcherError(
                "route deviation abort requires deviation samples"
            )
        for item in self.deviation_samples:
            if float(item.get("deviation_xy_m", 0.0)) < 0:
                raise PX4GazeboRouteDispatcherError(
                    "route deviation abort cannot have negative xy deviation"
                )
            if float(item.get("deviation_z_m", 0.0)) < 0:
                raise PX4GazeboRouteDispatcherError(
                    "route deviation abort cannot have negative z deviation"
                )
        return self


class PX4GazeboRouteRecoveryCompletion(_RouteDispatchSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_ROUTE_RECOVERY_COMPLETION_SCHEMA_VERSION] = (
        PX4_GAZEBO_ROUTE_RECOVERY_COMPLETION_SCHEMA_VERSION
    )
    recovery_completion_id: str
    final_status: Literal[
        "recovered_land",
        "recovered_land_state_observed_ack_timeout",
        "recovered_hold",
        "recovered_hold_state_observed_ack_timeout",
        "recovered_rtl",
        "recovered_rtl_state_observed_ack_timeout",
        "emergency_recovery_unconfirmed",
        "emergency_recovery_dispatch_blocked",
    ]
    recovery_completion_basis: PX4GazeboRouteRecoveryCompletionBasis
    deviation_abort_ref: str = Field(min_length=1)
    emergency_dispatch_ref: str = Field(min_length=1)
    recovery_action: str = Field(min_length=1)
    recovery_dispatch_status: str = Field(min_length=1)
    recovery_ack_complete: bool
    recovery_state_observed: bool
    recovery_completed: bool
    recovery_pose_z_m: float | None = None
    recovery_state_threshold_z_m: float = Field(default=0.15, gt=0)
    recovery_state_label: str | None = None
    recovery_state_observation_basis: Literal[
        "landing_pose_z",
        "commander_state_label",
        "not_observed",
    ] = "not_observed"
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_recovery_completion(self) -> "PX4GazeboRouteRecoveryCompletion":
        if self.recovery_ack_complete and not self.recovery_state_observed:
            raise PX4GazeboRouteDispatcherError(
                "ACK-complete recovery still requires state observation"
            )
        if self.recovery_completed and not self.recovery_state_observed:
            raise PX4GazeboRouteDispatcherError(
                "completed route recovery requires state observation"
            )
        if self.recovery_state_observed:
            action = _recovery_status_action(self.recovery_action)
            if action == "land" and self.recovery_pose_z_m is None:
                raise PX4GazeboRouteDispatcherError(
                    "state-observed recovery requires recovery pose z evidence"
                )
            if (
                action == "land"
                and self.recovery_pose_z_m is not None
                and self.recovery_pose_z_m > self.recovery_state_threshold_z_m
            ):
                raise PX4GazeboRouteDispatcherError(
                    "state-observed recovery pose is above threshold"
                )
            if action != "land" and not self.recovery_state_label:
                raise PX4GazeboRouteDispatcherError(
                    "hold/RTL state-observed recovery requires state label evidence"
                )
            if action == "hold" and self.recovery_state_label is not None:
                label = self.recovery_state_label.lower()
                if "hold" not in label and "loiter" not in label:
                    raise PX4GazeboRouteDispatcherError(
                        "hold recovery requires hold/loiter state label"
                    )
            if action == "rtl" and self.recovery_state_label is not None:
                label = self.recovery_state_label.lower()
                if "rtl" not in label and "return_to_launch" not in label:
                    raise PX4GazeboRouteDispatcherError(
                        "RTL recovery requires rtl/return_to_launch state label"
                    )
            if action == "land":
                expected_basis = "landing_pose_z"
            else:
                expected_basis = "commander_state_label"
            if self.recovery_state_observation_basis != expected_basis:
                raise PX4GazeboRouteDispatcherError(
                    "route recovery state observation basis disagrees with action"
                )
        elif self.recovery_state_observation_basis != "not_observed":
            raise PX4GazeboRouteDispatcherError(
                "unobserved route recovery cannot include state observation basis"
            )
        expected = _expected_recovery_completion_tuple(
            action=self.recovery_action,
            basis=self.recovery_completion_basis,
        )
        if (
            self.final_status,
            self.recovery_ack_complete,
            self.recovery_state_observed,
            self.recovery_completed,
        ) != expected:
            raise PX4GazeboRouteDispatcherError(
                "route recovery completion basis disagrees with recovery evidence"
            )
        if (
            self.recovery_completion_basis
            == PX4GazeboRouteRecoveryCompletionBasis.DISPATCH_BLOCKED_BEFORE_SEND
            and self.recovery_dispatch_status != "blocked"
        ):
            raise PX4GazeboRouteDispatcherError(
                "dispatch-blocked recovery completion requires blocked dispatch"
            )
        if (
            self.recovery_completion_basis
            == PX4GazeboRouteRecoveryCompletionBasis.STATE_OBSERVED_AFTER_DISPATCH_TIMEOUT
            and self.recovery_dispatch_status != "timeout"
        ):
            raise PX4GazeboRouteDispatcherError(
                "state-observed timeout recovery requires timeout dispatch"
            )
        return self


def _recovery_status_action(action: str) -> str:
    if action in {"return_to_launch", "rtl"}:
        return "rtl"
    return action


def _expected_recovery_completion_tuple(
    *,
    action: str,
    basis: PX4GazeboRouteRecoveryCompletionBasis,
) -> tuple[str, bool, bool, bool]:
    status_action = _recovery_status_action(action)
    if basis == PX4GazeboRouteRecoveryCompletionBasis.ACK_OBSERVED_AND_STATE_OBSERVED:
        return (f"recovered_{status_action}", True, True, True)
    if (
        basis
        == PX4GazeboRouteRecoveryCompletionBasis.STATE_OBSERVED_AFTER_DISPATCH_TIMEOUT
    ):
        return (
            f"recovered_{status_action}_state_observed_ack_timeout",
            False,
            True,
            True,
        )
    if (
        basis
        == PX4GazeboRouteRecoveryCompletionBasis.STATE_NOT_OBSERVED_AFTER_DISPATCH_TIMEOUT
    ):
        return ("emergency_recovery_unconfirmed", False, False, False)
    if basis == PX4GazeboRouteRecoveryCompletionBasis.DISPATCH_BLOCKED_BEFORE_SEND:
        return ("emergency_recovery_dispatch_blocked", False, False, False)
    raise PX4GazeboRouteDispatcherError("unsupported route recovery completion basis")


def build_px4_gazebo_route_command_allowlist(
    *,
    route_plan: PX4GazeboPickupDropoffRoutePlan | Mapping[str, Any],
    approval: PX4GazeboCoupledCommandApproval | Mapping[str, Any],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboRouteCommandAllowlist:
    resolved_route = _coerce_route_plan(route_plan)
    resolved_approval = _coerce_approval(approval)
    if resolved_approval.operator_approval_performed is not True:
        raise PX4GazeboRouteDispatcherError(
            "route command allowlist requires operator approval"
        )
    generated_at = _utc(now)
    payload = {
        "route_plan_id": resolved_route.route_plan_id,
        "approval_id": resolved_approval.approval_id,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboRouteCommandAllowlist(
        allowlist_id=_stable_id("px4_gazebo_route_command_allowlist", payload),
        route_plan_ref=_route_plan_ref(resolved_route),
        operator_approval_ref=_approval_ref(resolved_approval),
        allowed_mavlink_message_ids=(
            MAVLINK_MSG_ID_SET_POSITION_TARGET_LOCAL_NED,
            MAVLINK_MSG_ID_COMMAND_LONG,
        ),
        generated_at=generated_at,
        metadata={
            **(metadata or {}),
            "issue": 344,
            "parent_epic": 339,
            "route_smoke_deferred_to_issue": 345,
        },
    )


def _validate_route_dispatch_inputs(
    *,
    route_plan: PX4GazeboPickupDropoffRoutePlan,
    route_allowlist: PX4GazeboRouteCommandAllowlist,
    approval: PX4GazeboCoupledCommandApproval,
    endpoint_host: str,
    endpoint_port: int,
    live_mavlink_opt_in: bool,
    setpoint_frames: int,
) -> None:
    _validate_endpoint(
        endpoint_host=endpoint_host,
        endpoint_port=endpoint_port,
        live_mavlink_opt_in=live_mavlink_opt_in,
    )
    if approval.operator_approval_performed is not True:
        raise PX4GazeboRouteDispatcherError(
            "route command dispatch requires operator approval"
        )
    if route_allowlist.route_plan_ref != _route_plan_ref(route_plan):
        raise PX4GazeboRouteDispatcherError("route allowlist route plan mismatch")
    if route_allowlist.operator_approval_ref != _approval_ref(approval):
        raise PX4GazeboRouteDispatcherError("route allowlist approval mismatch")
    if not 1 <= setpoint_frames <= route_allowlist.max_setpoint_frames:
        raise PX4GazeboRouteDispatcherError("setpoint burst exceeds route allowlist")
    _validate_route_target(
        route_plan=route_plan,
        target_x_m=derive_px4_gazebo_route_target_ned(route_plan)[0],
        target_y_m=derive_px4_gazebo_route_target_ned(route_plan)[1],
        target_z_m=derive_px4_gazebo_route_target_ned(route_plan)[2],
    )


def run_px4_gazebo_route_command_dispatch(
    *,
    route_plan: PX4GazeboPickupDropoffRoutePlan | Mapping[str, Any],
    route_allowlist: PX4GazeboRouteCommandAllowlist | Mapping[str, Any],
    approval: PX4GazeboCoupledCommandApproval | Mapping[str, Any],
    endpoint_host: str = "127.0.0.1",
    endpoint_port: int = 18570,
    live_mavlink_opt_in: bool,
    setpoint_frames: int = 3,
    now: datetime | None = None,
) -> PX4GazeboRouteCommandDispatchResult:
    resolved_route = _coerce_route_plan(route_plan)
    resolved_allowlist = (
        route_allowlist
        if isinstance(route_allowlist, PX4GazeboRouteCommandAllowlist)
        else PX4GazeboRouteCommandAllowlist.model_validate(dict(route_allowlist))
    )
    resolved_approval = _coerce_approval(approval)
    _validate_route_dispatch_inputs(
        route_plan=resolved_route,
        route_allowlist=resolved_allowlist,
        approval=resolved_approval,
        endpoint_host=endpoint_host,
        endpoint_port=endpoint_port,
        live_mavlink_opt_in=live_mavlink_opt_in,
        setpoint_frames=setpoint_frames,
    )
    target_x, target_y, target_z = derive_px4_gazebo_route_target_ned(resolved_route)
    sent_at = _utc(now)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        remote = (endpoint_host, endpoint_port)
        sock.sendto(encode_mavlink2_heartbeat(sequence=0), remote)
        for sequence in range(setpoint_frames):
            frame = encode_set_position_target_local_ned(
                x_m=target_x,
                y_m=target_y,
                z_m=target_z,
                sequence=20 + sequence,
            )
            sock.sendto(frame, remote)
            time.sleep(0.01)
    payload = {
        "route_plan_id": resolved_route.route_plan_id,
        "allowlist_id": resolved_allowlist.allowlist_id,
        "target": (target_x, target_y, target_z),
        "setpoint_frames": setpoint_frames,
        "sent_at": sent_at.isoformat(),
    }
    return PX4GazeboRouteCommandDispatchResult(
        dispatch_result_id=_stable_id("px4_gazebo_route_command_dispatch", payload),
        route_plan_ref=_route_plan_ref(resolved_route),
        route_allowlist_ref=_route_allowlist_ref(resolved_allowlist),
        route_dispatch_status=PX4GazeboRouteDispatchStatus.SENT,
        endpoint_port=endpoint_port,
        target_x_m=target_x,
        target_y_m=target_y,
        target_z_m=target_z,
        setpoint_frames_sent=setpoint_frames,
        sent_at=sent_at,
        metadata={
            "issue": 344,
            "parent_epic": 339,
            "pickup_dropoff_smoke_deferred_to_issue": 345,
        },
    )


def build_px4_gazebo_route_command_dispatch_result_from_observed_stream(
    *,
    route_plan: PX4GazeboPickupDropoffRoutePlan | Mapping[str, Any],
    route_allowlist: PX4GazeboRouteCommandAllowlist | Mapping[str, Any],
    approval: PX4GazeboCoupledCommandApproval | Mapping[str, Any],
    endpoint_port: int,
    target_x_m: float,
    target_y_m: float,
    target_z_m: float,
    setpoint_frames_sent: int,
    setpoint_stream_duration_seconds: float,
    offboard_mode_switch_frame_sent: bool,
    offboard_mode_switch_ack_observed: bool,
    offboard_mode_switch_ack_result_code: int | None = None,
    offboard_mode_switch_ack_result_name: str | None = None,
    offboard_mode_switch_ack_timeout_seconds: float = (
        ROUTE_OFFBOARD_ACK_TIMEOUT_SECONDS
    ),
    now: datetime | None = None,
) -> PX4GazeboRouteCommandDispatchResult:
    resolved_route = _coerce_route_plan(route_plan)
    resolved_allowlist = (
        route_allowlist
        if isinstance(route_allowlist, PX4GazeboRouteCommandAllowlist)
        else PX4GazeboRouteCommandAllowlist.model_validate(dict(route_allowlist))
    )
    resolved_approval = _coerce_approval(approval)
    _validate_route_dispatch_inputs(
        route_plan=resolved_route,
        route_allowlist=resolved_allowlist,
        approval=resolved_approval,
        endpoint_host="127.0.0.1",
        endpoint_port=endpoint_port,
        live_mavlink_opt_in=True,
        setpoint_frames=1,
    )
    if setpoint_frames_sent > resolved_allowlist.max_setpoint_stream_frames:
        raise PX4GazeboRouteDispatcherError(
            "setpoint stream frames exceed route allowlist"
        )
    if (
        float(setpoint_stream_duration_seconds)
        > resolved_allowlist.max_setpoint_stream_duration_seconds
    ):
        raise PX4GazeboRouteDispatcherError(
            "setpoint stream duration exceeds route allowlist"
        )
    _validate_route_target(
        route_plan=resolved_route,
        target_x_m=float(target_x_m),
        target_y_m=float(target_y_m),
        target_z_m=float(target_z_m),
    )
    sent_at = _utc(now)
    payload = {
        "route_plan_id": resolved_route.route_plan_id,
        "allowlist_id": resolved_allowlist.allowlist_id,
        "target": (float(target_x_m), float(target_y_m), float(target_z_m)),
        "setpoint_frames_sent": int(setpoint_frames_sent),
        "setpoint_stream_duration_seconds": float(setpoint_stream_duration_seconds),
        "offboard_mode_switch_frame_sent": bool(offboard_mode_switch_frame_sent),
        "offboard_mode_switch_ack_observed": bool(offboard_mode_switch_ack_observed),
        "offboard_mode_switch_ack_result_code": offboard_mode_switch_ack_result_code,
        "sent_at": sent_at.isoformat(),
    }
    ack_missing = offboard_mode_switch_ack_observed is not True
    return PX4GazeboRouteCommandDispatchResult(
        dispatch_result_id=_stable_id(
            "px4_gazebo_route_command_stream_dispatch", payload
        ),
        route_plan_ref=_route_plan_ref(resolved_route),
        route_allowlist_ref=_route_allowlist_ref(resolved_allowlist),
        route_dispatch_status=(
            PX4GazeboRouteDispatchStatus.BLOCKED
            if ack_missing
            else PX4GazeboRouteDispatchStatus.SENT
        ),
        route_primitive="bounded_position_setpoint_stream",
        endpoint_port=endpoint_port,
        target_x_m=float(target_x_m),
        target_y_m=float(target_y_m),
        target_z_m=float(target_z_m),
        setpoint_frames_sent=int(setpoint_frames_sent),
        setpoint_stream_duration_seconds=float(setpoint_stream_duration_seconds),
        offboard_mode_switch_frame_sent=bool(offboard_mode_switch_frame_sent),
        offboard_mode_switch_ack_required=True,
        offboard_mode_switch_ack_timeout_seconds=float(
            offboard_mode_switch_ack_timeout_seconds
        ),
        offboard_mode_switch_ack_observed=bool(offboard_mode_switch_ack_observed),
        offboard_mode_switch_ack_result_code=offboard_mode_switch_ack_result_code,
        offboard_mode_switch_ack_result_name=offboard_mode_switch_ack_result_name,
        route_command_frame_sent=not ack_missing,
        blocked_reasons=("blocked_offboard_ack_missing",) if ack_missing else (),
        sent_at=sent_at,
        metadata={
            "issue": 345,
            "parent_epic": 339,
            "actual_px4_gazebo_horizontal_smoke_observed": True,
            "offboard_ack_gate_issue": 357,
        },
    )


def build_px4_gazebo_route_progress_evidence(
    *,
    route_plan: PX4GazeboPickupDropoffRoutePlan | Mapping[str, Any],
    route_dispatch_result: PX4GazeboRouteCommandDispatchResult | Mapping[str, Any],
    pickup_pose_xy_m: tuple[float, float],
    observed_pose_xy_m: tuple[float, float],
    deviation_samples: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    now: datetime | None = None,
) -> PX4GazeboRouteProgressEvidence:
    resolved_route = _coerce_route_plan(route_plan)
    dispatch = (
        route_dispatch_result
        if isinstance(route_dispatch_result, PX4GazeboRouteCommandDispatchResult)
        else PX4GazeboRouteCommandDispatchResult.model_validate(
            dict(route_dispatch_result)
        )
    )
    if dispatch.route_plan_ref != _route_plan_ref(resolved_route):
        raise PX4GazeboRouteDispatcherError(
            "route progress evidence route plan mismatch"
        )
    if dispatch.route_dispatch_status != PX4GazeboRouteDispatchStatus.SENT:
        raise PX4GazeboRouteDispatcherError(
            "route progress evidence requires sent route dispatch"
        )
    dropoff = _route_target_xy(resolved_route)
    progress = max(
        0.0,
        (
            (observed_pose_xy_m[0] - pickup_pose_xy_m[0]) ** 2
            + (observed_pose_xy_m[1] - pickup_pose_xy_m[1]) ** 2
        )
        ** 0.5,
    )
    dropoff_distance = (
        (observed_pose_xy_m[0] - dropoff[0]) ** 2
        + (observed_pose_xy_m[1] - dropoff[1]) ** 2
    ) ** 0.5
    outside = not _inside_bbox(
        point=(float(observed_pose_xy_m[0]), float(observed_pose_xy_m[1])),
        polygon=resolved_route.geofence_polygon,
    )
    observed_at = _utc(now)
    payload = {
        "route_plan_id": resolved_route.route_plan_id,
        "dispatch_result_id": dispatch.dispatch_result_id,
        "observed_pose": observed_pose_xy_m,
        "observed_at": observed_at.isoformat(),
    }
    return PX4GazeboRouteProgressEvidence(
        progress_evidence_id=_stable_id("px4_gazebo_route_progress", payload),
        route_plan_ref=_route_plan_ref(resolved_route),
        route_dispatch_result_ref=(
            f"px4_gazebo_route_command_dispatch_result:{dispatch.dispatch_result_id}"
        ),
        pickup_pose_xy_m=pickup_pose_xy_m,
        dropoff_pose_xy_m=dropoff,
        observed_pose_xy_m=observed_pose_xy_m,
        horizontal_progress_m=progress,
        dropoff_region_reached=dropoff_distance
        <= resolved_route.route_completion_radius_m,
        route_geofence_violation=outside,
        deviation_samples=tuple(deviation_samples),
        observed_at=observed_at,
        metadata={
            "issue": 359,
            "parent_epic": 339,
            "actual_px4_gazebo_horizontal_smoke_not_claimed": True,
            "deviation_gate": True,
        },
    )


def build_px4_gazebo_route_deviation_abort(
    *,
    route_plan: PX4GazeboPickupDropoffRoutePlan | Mapping[str, Any],
    route_allowlist: PX4GazeboRouteCommandAllowlist | Mapping[str, Any],
    deviation_samples: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    route_monitor_sample_count: int,
    now: datetime | None = None,
) -> PX4GazeboRouteDeviationAbort:
    resolved_route = _coerce_route_plan(route_plan)
    resolved_allowlist = (
        route_allowlist
        if isinstance(route_allowlist, PX4GazeboRouteCommandAllowlist)
        else PX4GazeboRouteCommandAllowlist.model_validate(dict(route_allowlist))
    )
    if resolved_allowlist.route_plan_ref != _route_plan_ref(resolved_route):
        raise PX4GazeboRouteDispatcherError(
            "route deviation abort allowlist route plan mismatch"
        )
    observed_at = _utc(now)
    payload = {
        "route_plan_id": resolved_route.route_plan_id,
        "route_allowlist_id": resolved_allowlist.allowlist_id,
        "deviation_samples": list(deviation_samples),
        "route_monitor_sample_count": int(route_monitor_sample_count),
        "observed_at": observed_at.isoformat(),
    }
    return PX4GazeboRouteDeviationAbort(
        abort_id=_stable_id("px4_gazebo_route_deviation_abort", payload),
        route_plan_ref=_route_plan_ref(resolved_route),
        route_allowlist_ref=_route_allowlist_ref(resolved_allowlist),
        deviation_samples=tuple(deviation_samples),
        route_monitor_sample_count=int(route_monitor_sample_count),
        observed_at=observed_at,
        metadata={
            "issue": 359,
            "parent_epic": 356,
            "recovery_dispatch_deferred_to_issue": 360,
            "closed_loop_recovery_deferred_to_issue": 361,
        },
    )


def build_px4_gazebo_route_recovery_completion(
    *,
    deviation_abort: PX4GazeboRouteDeviationAbort | Mapping[str, Any],
    emergency_dispatch: Any,
    recovery_state_observed: bool,
    recovery_pose_z_m: float | None = None,
    recovery_state_label: str | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboRouteRecoveryCompletion:
    resolved_abort = (
        deviation_abort
        if isinstance(deviation_abort, PX4GazeboRouteDeviationAbort)
        else PX4GazeboRouteDeviationAbort.model_validate(dict(deviation_abort))
    )
    dispatch = _coerce_emergency_dispatch(emergency_dispatch)
    observed_at = _utc(now)
    ack_complete = (
        dispatch["dispatch_status"] == "accepted"
        and dispatch["command_ack_observed"] is True
    )
    if ack_complete and recovery_state_observed:
        basis = PX4GazeboRouteRecoveryCompletionBasis.ACK_OBSERVED_AND_STATE_OBSERVED
        completed = True
    elif dispatch["dispatch_status"] == "timeout" and recovery_state_observed:
        basis = (
            PX4GazeboRouteRecoveryCompletionBasis.STATE_OBSERVED_AFTER_DISPATCH_TIMEOUT
        )
        completed = True
    elif dispatch["dispatch_status"] == "timeout":
        basis = (
            PX4GazeboRouteRecoveryCompletionBasis.STATE_NOT_OBSERVED_AFTER_DISPATCH_TIMEOUT
        )
        completed = False
    elif dispatch["dispatch_status"] == "blocked":
        basis = PX4GazeboRouteRecoveryCompletionBasis.DISPATCH_BLOCKED_BEFORE_SEND
        completed = False
    else:
        raise PX4GazeboRouteDispatcherError(
            "recovery completion requires accepted, timeout, or blocked emergency dispatch"
        )
    final_status, _, _, _ = _expected_recovery_completion_tuple(
        action=str(dispatch["recovery_action"]),
        basis=basis,
    )
    state_observation_basis: Literal[
        "landing_pose_z", "commander_state_label", "not_observed"
    ]
    if not recovery_state_observed:
        state_observation_basis = "not_observed"
    elif _recovery_status_action(str(dispatch["recovery_action"])) == "land":
        state_observation_basis = "landing_pose_z"
    else:
        state_observation_basis = "commander_state_label"
    payload = {
        "abort_id": resolved_abort.abort_id,
        "dispatch_id": dispatch["dispatch_result_id"],
        "basis": basis.value,
        "action": str(dispatch["recovery_action"]),
        "state_observed": bool(recovery_state_observed),
        "pose_z": recovery_pose_z_m,
        "state_label": recovery_state_label,
        "observed_at": observed_at.isoformat(),
    }
    return PX4GazeboRouteRecoveryCompletion(
        recovery_completion_id=_stable_id("px4_gazebo_route_recovery", payload),
        final_status=final_status,  # type: ignore[arg-type]
        recovery_completion_basis=basis,
        deviation_abort_ref=f"px4_gazebo_route_deviation_abort:{resolved_abort.abort_id}",
        emergency_dispatch_ref=(
            "px4_gazebo_emergency_command_dispatch_result:"
            f"{dispatch['dispatch_result_id']}"
        ),
        recovery_action=str(dispatch["recovery_action"]),
        recovery_dispatch_status=str(dispatch["dispatch_status"]),
        recovery_ack_complete=ack_complete,
        recovery_state_observed=bool(recovery_state_observed),
        recovery_completed=completed,
        recovery_pose_z_m=recovery_pose_z_m,
        recovery_state_label=recovery_state_label,
        recovery_state_observation_basis=state_observation_basis,
        observed_at=observed_at,
        metadata={**(metadata or {}), "issue": 361, "parent_epic": 356},
    )


__all__ = [
    "PX4_GAZEBO_ROUTE_COMMAND_ALLOWLIST_SCHEMA_VERSION",
    "PX4_GAZEBO_ROUTE_COMMAND_DISPATCH_RESULT_SCHEMA_VERSION",
    "PX4_GAZEBO_ROUTE_DEVIATION_ABORT_SCHEMA_VERSION",
    "PX4_GAZEBO_ROUTE_PROGRESS_EVIDENCE_SCHEMA_VERSION",
    "PX4_GAZEBO_ROUTE_RECOVERY_COMPLETION_SCHEMA_VERSION",
    "ROUTE_SETPOINT_STREAM_MAX_DURATION_SECONDS",
    "ROUTE_SETPOINT_STREAM_MAX_FRAMES",
    "ROUTE_OFFBOARD_ACK_TIMEOUT_SECONDS",
    "MAV_CMD_DO_SET_MODE",
    "PX4GazeboRouteCommandAllowlist",
    "PX4GazeboRouteCommandDispatchResult",
    "PX4GazeboRouteDeviationAbort",
    "PX4GazeboRouteDispatchStatus",
    "PX4GazeboRouteDispatcherError",
    "PX4GazeboRouteProgressEvidence",
    "PX4GazeboRouteRecoveryCompletion",
    "PX4GazeboRouteRecoveryCompletionBasis",
    "build_px4_gazebo_route_command_allowlist",
    "build_px4_gazebo_route_command_dispatch_result_from_observed_stream",
    "build_px4_gazebo_route_deviation_abort",
    "build_px4_gazebo_route_progress_evidence",
    "build_px4_gazebo_route_recovery_completion",
    "derive_px4_gazebo_route_target_ned",
    "encode_set_position_target_local_ned",
    "run_px4_gazebo_route_command_dispatch",
]
