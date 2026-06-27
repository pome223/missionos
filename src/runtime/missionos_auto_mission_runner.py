"""MissionOS AUTO mission runner helpers.

This module owns route compilation, MAVLink mission upload, AUTO.MISSION mode
transition evidence, and the first guarded runtime monitor surface. Payload
release, dropoff verification, waypoint hard gates, and delivery completion
claims remain later verifier phases and stay fail-closed here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
import math
import socket
import struct
import time
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_LOITER_TIME,
    MAV_CMD_NAV_TAKEOFF,
    MAV_CMD_NAV_WAYPOINT,
    MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
    MAV_MISSION_ACCEPTED,
    PX4GazeboSITLMissionItem,
    decode_mavlink2_mission_ack_type,
    decode_mavlink2_mission_request_int,
    encode_mavlink2_mission_count,
    encode_mavlink2_mission_item_int,
)
from src.runtime.px4_real_mavlink_transport import (
    MAVLINK_MSG_ID_COMMAND_ACK,
    MAVLINK_MSG_ID_COMMAND_LONG,
    PX4RealMAVLinkTransportError,
    decode_mavlink2_frame,
    encode_mavlink2_frame,
    encode_mavlink2_heartbeat,
)


MISSIONOS_AUTO_MISSION_COMPILATION_SCHEMA_VERSION = (
    "missionos_auto_mission_compilation.v1"
)
MISSIONOS_AUTO_MISSION_UPLOAD_SUMMARY_SCHEMA_VERSION = (
    "missionos_auto_mission_upload_summary.v1"
)
MISSIONOS_AUTO_MISSION_MODE_TRANSITION_SUMMARY_SCHEMA_VERSION = (
    "missionos_auto_mission_mode_transition_summary.v1"
)
MISSIONOS_AUTO_MISSION_PHASE3B_LIVE_BOUNDARY_SUMMARY_SCHEMA_VERSION = (
    "missionos_auto_mission_phase3b_live_boundary_summary.v1"
)
MISSIONOS_AUTO_MISSION_TELEMETRY_SAMPLE_SCHEMA_VERSION = (
    "missionos_auto_mission_telemetry_sample.v1"
)
MISSIONOS_AUTO_MISSION_RUNTIME_MONITOR_SUMMARY_SCHEMA_VERSION = (
    "missionos_auto_mission_runtime_monitor_summary.v1"
)
MISSIONOS_AUTO_MISSION_WAYPOINT_GATE_SUMMARY_SCHEMA_VERSION = (
    "missionos_auto_mission_waypoint_gate_summary.v1"
)
MISSIONOS_AUTO_MISSION_DROPOFF_GATE_SUMMARY_SCHEMA_VERSION = (
    "missionos_auto_mission_dropoff_gate_summary.v1"
)
MISSIONOS_AUTO_MISSION_PAYLOAD_RELEASE_L0_SUMMARY_SCHEMA_VERSION = (
    "missionos_auto_mission_payload_release_l0_summary.v1"
)
MISSIONOS_AUTO_MISSION_SITL_DELIVERY_GATE_SUMMARY_SCHEMA_VERSION = (
    "missionos_auto_mission_sitl_delivery_gate_summary.v1"
)
MISSIONOS_AUTO_MISSION_PAYLOAD_RELEASE_SIM_GATE_SUMMARY_SCHEMA_VERSION = (
    "missionos_auto_mission_payload_release_sim_gate_summary.v1"
)

DEFAULT_AUTO_WAYPOINT_SPACING_M = 100.0
DEFAULT_AUTO_MAX_ROUTE_WAYPOINTS = 80
DEFAULT_AUTO_CRUISE_SPEED_MPS = 5.0
DEFAULT_TAKEOFF_ALLOWANCE_SECONDS = 45.0
DEFAULT_DROPOFF_DWELL_SECONDS = 3.0
DEFAULT_PAYLOAD_RELEASE_COMMAND_COMPLETION_MARGIN_SECONDS = 5.0
DEFAULT_DROPOFF_LOITER_SECONDS = (
    DEFAULT_DROPOFF_DWELL_SECONDS
    + DEFAULT_PAYLOAD_RELEASE_COMMAND_COMPLETION_MARGIN_SECONDS
)
DEFAULT_LANDING_ALLOWANCE_SECONDS = 60.0
DEFAULT_TIMEOUT_SAFETY_FACTOR = 1.5
DEFAULT_MAX_TIMEOUT_SECONDS = 1800.0
DEFAULT_AUTO_MODE_ACK_TIMEOUT_SECONDS = 5.0
DEFAULT_AUTO_RUNTIME_MONITOR_SECONDS = 90.0
DEFAULT_AUTO_RUNTIME_MIN_PROGRESS_M = 5.0
DEFAULT_AUTO_RUNTIME_NO_PROGRESS_GRACE_SECONDS = 20.0
DEFAULT_AUTO_RUNTIME_MIN_ROUTE_ALTITUDE_M = 8.0
DEFAULT_AUTO_RUNTIME_ALTITUDE_GRACE_SECONDS = 20.0
DEFAULT_AUTO_RUNTIME_BATTERY_MIN_REMAINING_PERCENT = 20.0
DEFAULT_TERRAIN_CLEARANCE_AGL_M = 30.0
# PX4 SITL's simulated battery floors at SIM_BAT_MIN_PCT (default ~50%). We set a
# low floor and a gradual full->empty drain interval so SITL telemetry shows an
# observable trend instead of pinning at ~50%. This is not real power-module
# endurance evidence.
DEFAULT_AUTO_RUNTIME_SIM_BATTERY_MIN_PCT = 5.0
DEFAULT_AUTO_RUNTIME_SIM_BATTERY_DRAIN_SECONDS = 1800.0
DEFAULT_DROPOFF_RELEASE_RADIUS_M = 5.0
DEFAULT_DROPOFF_RELEASE_ALTITUDE_TOLERANCE_M = 2.0
DEFAULT_PAYLOAD_RELEASE_GRIPPER_ID = 1.0
SOURCE_BOUND_TERRAIN_PROFILE_SOURCES = frozenset(
    {
        "gsi_dem_elevation_tiles",
        "open_meteo_forecast_elevation_fallback",
    }
)
TERRAIN_PROFILE_REF_PREFIX = "missionos_terrain_elevation_resolver_tool_result:"

MAV_CMD_DO_SET_MODE = 176
MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_DO_GRIPPER = 211
MAV_GRIPPER_ACTION_RELEASE = 0
MAV_RESULT_ACCEPTED = 0
MAV_RESULT_IN_PROGRESS = 5
MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = 1
PX4_CUSTOM_MAIN_MODE_AUTO = 4
PX4_CUSTOM_SUB_MODE_AUTO_MISSION = 4
DEFAULT_MAVLINK_GCS_SYSTEM_ID = 255
DEFAULT_MAVLINK_GCS_COMPONENT_ID = 190
DEFAULT_PX4_TARGET_SYSTEM = 1
DEFAULT_PX4_TARGET_COMPONENT = 1
PHASE3B_ABORT_REASON = "phase3b_stop_after_auto_mission_mode_ack"
AUTO_RUNTIME_ABORT_REASON = "phase3c3d_guarded_runtime_stop"
AUTO_RUNTIME_PROBE_STOP_REASON_MONITOR_WINDOW_COMPLETE = (
    "probe_monitor_window_elapsed"
)
PX4_NAVIGATION_STATE_AUTO_MISSION = 3
PX4_ARMING_STATE_ARMED = 2
PX4_LANDED_STATE_IN_AIR = 2


class MissionOSAutoMissionRunnerError(RuntimeError):
    """Raised when AUTO mission route compilation or upload fails."""


class MissionOSAutoMissionUploadStatus(str, Enum):
    UPLOADED = "uploaded"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"


class MissionOSAutoMissionModeTransitionStatus(str, Enum):
    COMMAND_ACKS_ACCEPTED = "command_acks_accepted"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"


class MissionOSAutoMissionRuntimeStatus(str, Enum):
    MONITOR_WINDOW_COMPLETED = "monitor_window_completed"
    GUARD_ABORTED = "guard_aborted"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"


class MissionOSAutoMissionCompilation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[MISSIONOS_AUTO_MISSION_COMPILATION_SCHEMA_VERSION] = (
        MISSIONOS_AUTO_MISSION_COMPILATION_SCHEMA_VERSION
    )
    route_ref: str
    planned_route_m: float = Field(gt=0)
    planned_waypoint_count: int = Field(ge=0)
    generated_route_waypoint_count: int = Field(ge=0)
    dropoff_dwell_mission_seq: int | None = Field(default=None, ge=0)
    land_mission_seq: int | None = Field(default=None, ge=0)
    waypoint_spacing_m: float = Field(gt=0)
    cruise_speed_mps: float = Field(gt=0)
    takeoff_altitude_m: float = Field(ge=0)
    cruise_altitude_m: float = Field(ge=0)
    terrain_aware_altitudes_enabled: bool = False
    terrain_profile_source: str | None = None
    terrain_profile_ref: str | None = None
    terrain_profile_treated_as_visual_only: bool = False
    terrain_profile_blocked_reason: str | None = None
    terrain_clearance_target_m: float | None = Field(default=None, ge=0)
    terrain_clearance_profile: tuple[dict[str, float | int | bool], ...] = ()
    dropoff_dwell_seconds: float = Field(ge=0)
    dropoff_release_min_dwell_seconds: float = Field(ge=0)
    expected_duration_seconds: float = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    mission_items: tuple[PX4GazeboSITLMissionItem, ...]
    route_completed_claimed: Literal[False] = False
    dropoff_verified: Literal[False] = False
    payload_release_observed: Literal[False] = False
    delivery_completion_claimed: Literal[False] = False

    @field_validator("mission_items", mode="before")
    @classmethod
    def _coerce_items(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @field_validator("terrain_clearance_profile", mode="before")
    @classmethod
    def _coerce_terrain_profile(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @model_validator(mode="after")
    def _validate_compilation(self) -> "MissionOSAutoMissionCompilation":
        if self.planned_waypoint_count != len(self.mission_items):
            raise MissionOSAutoMissionRunnerError(
                "planned_waypoint_count_mismatch"
            )
        if tuple(item.seq for item in self.mission_items) != tuple(
            range(len(self.mission_items))
        ):
            raise MissionOSAutoMissionRunnerError("mission_item_sequence_not_contiguous")
        if self.generated_route_waypoint_count > len(self.mission_items):
            raise MissionOSAutoMissionRunnerError("route_waypoint_count_invalid")
        return self


class MissionOSAutoMissionUploadSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[MISSIONOS_AUTO_MISSION_UPLOAD_SUMMARY_SCHEMA_VERSION] = (
        MISSIONOS_AUTO_MISSION_UPLOAD_SUMMARY_SCHEMA_VERSION
    )
    auto_mission_runner_invoked: Literal[True] = True
    auto_mission_runner_version: Literal["phase3a_wire_v1"] = "phase3a_wire_v1"
    auto_mission_execution_mode: Literal["auto_mission_upload_wire_only"] = (
        "auto_mission_upload_wire_only"
    )
    auto_mission_upload_status: MissionOSAutoMissionUploadStatus
    auto_mission_upload_protocol: Literal["mavlink_mission_item_int"] = (
        "mavlink_mission_item_int"
    )
    mission_count_sent: int = Field(ge=0)
    mission_count_expected: int = Field(ge=0)
    mission_request_int_sequences: tuple[int, ...] = ()
    mission_item_int_sequences_sent: tuple[int, ...] = ()
    mission_ack_observed: bool
    mission_ack_result: int | None = None
    mission_upload_retry_count: int = Field(default=0, ge=0)
    mission_upload_retry_reasons: tuple[str, ...] = ()
    planned_route_m: float = Field(gt=0)
    planned_waypoint_count: int = Field(ge=0)
    generated_route_waypoint_count: int = Field(ge=0)
    waypoint_spacing_m: float = Field(gt=0)
    cruise_speed_mps: float = Field(gt=0)
    takeoff_altitude_m: float = Field(ge=0)
    cruise_altitude_m: float = Field(ge=0)
    expected_duration_seconds: float = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    auto_mission_started: Literal[False] = False
    route_completed_claimed: Literal[False] = False
    dropoff_region_reached: Literal[False] = False
    dropoff_verified: Literal[False] = False
    payload_release_observed: Literal[False] = False
    delivery_completion_claimed: Literal[False] = False
    blocked_reasons: tuple[str, ...] = ()

    @field_validator(
        "mission_request_int_sequences",
        "mission_item_int_sequences_sent",
        "mission_upload_retry_reasons",
        "blocked_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @model_validator(mode="after")
    def _validate_summary(self) -> "MissionOSAutoMissionUploadSummary":
        if self.mission_count_sent != self.mission_count_expected:
            raise MissionOSAutoMissionRunnerError("mission_count_summary_mismatch")
        if self.auto_mission_upload_status is MissionOSAutoMissionUploadStatus.UPLOADED:
            if self.blocked_reasons:
                raise MissionOSAutoMissionRunnerError(
                    "uploaded auto mission summary cannot be blocked"
                )
            if self.mission_ack_result != MAV_MISSION_ACCEPTED:
                raise MissionOSAutoMissionRunnerError(
                    "uploaded auto mission summary requires accepted ACK"
                )
            if self.mission_item_int_sequences_sent != tuple(
                range(self.mission_count_sent)
            ):
                raise MissionOSAutoMissionRunnerError(
                    "uploaded auto mission summary requires contiguous item upload"
                )
        else:
            if not self.blocked_reasons:
                raise MissionOSAutoMissionRunnerError(
                    "blocked/timeout auto mission summary requires reasons"
                )
        return self


class _CommandAck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observed: bool
    result_code: int | None = None
    result_name: str | None = None


class _ModeTransitionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    heartbeats_sent: int = Field(ge=0)
    arm_command_frame_sent: bool
    arm_command_ack: _CommandAck
    auto_mission_mode_command_frame_sent: bool
    auto_mission_mode_command_ack: _CommandAck
    auto_mission_abort_command_frame_sent: bool
    auto_mission_abort_command_ack: _CommandAck


class _PayloadReleaseTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    heartbeats_sent: int = Field(ge=0)
    payload_release_command_frame_sent: bool
    payload_release_command_ack: _CommandAck


class MissionOSAutoMissionModeTransitionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        MISSIONOS_AUTO_MISSION_MODE_TRANSITION_SUMMARY_SCHEMA_VERSION
    ] = MISSIONOS_AUTO_MISSION_MODE_TRANSITION_SUMMARY_SCHEMA_VERSION
    auto_mission_runner_invoked: Literal[True] = True
    auto_mission_runner_version: Literal["phase3b_mode_transition_wire_v1"] = (
        "phase3b_mode_transition_wire_v1"
    )
    auto_mission_execution_mode: Literal["auto_mission_mode_transition_wire_only"] = (
        "auto_mission_mode_transition_wire_only"
    )
    mode_transition_status: MissionOSAutoMissionModeTransitionStatus
    mission_upload_accepted: bool
    mission_count_sent: int = Field(ge=0)
    mission_ack_observed: bool
    mission_ack_result: int | None = None
    heartbeats_sent_before_commands: int = Field(ge=0)
    arm_command_id: Literal[MAV_CMD_COMPONENT_ARM_DISARM] = (
        MAV_CMD_COMPONENT_ARM_DISARM
    )
    arm_command_frame_sent: bool
    arm_command_ack_required: Literal[True] = True
    arm_command_ack_observed: bool
    arm_command_ack_result: int | None = None
    arm_command_ack_result_name: str | None = None
    auto_mission_mode_command_id: Literal[MAV_CMD_DO_SET_MODE] = MAV_CMD_DO_SET_MODE
    auto_mission_mode_base_mode: Literal[MAV_MODE_FLAG_CUSTOM_MODE_ENABLED] = (
        MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
    )
    auto_mission_mode_custom_main_mode: Literal[PX4_CUSTOM_MAIN_MODE_AUTO] = (
        PX4_CUSTOM_MAIN_MODE_AUTO
    )
    auto_mission_mode_custom_sub_mode: Literal[
        PX4_CUSTOM_SUB_MODE_AUTO_MISSION
    ] = PX4_CUSTOM_SUB_MODE_AUTO_MISSION
    auto_mission_mode_command_frame_sent: bool
    auto_mission_mode_ack_required: Literal[True] = True
    auto_mission_mode_ack_observed: bool
    auto_mission_mode_ack_result: int | None = None
    auto_mission_mode_ack_result_name: str | None = None
    auto_mission_abort_policy: Literal[PHASE3B_ABORT_REASON] = PHASE3B_ABORT_REASON
    auto_mission_abort_command_id: Literal[MAV_CMD_NAV_LAND] = MAV_CMD_NAV_LAND
    auto_mission_abort_command_frame_sent: bool
    auto_mission_abort_ack_required: Literal[True] = True
    auto_mission_abort_ack_observed: bool
    auto_mission_abort_ack_result: int | None = None
    auto_mission_abort_ack_result_name: str | None = None
    nav_state_auto_mission_observed: Literal[False] = False
    monitor_loop_started: Literal[False] = False
    auto_mission_started: Literal[False] = False
    route_completed_claimed: Literal[False] = False
    dropoff_region_reached: Literal[False] = False
    dropoff_verified: Literal[False] = False
    payload_release_observed: Literal[False] = False
    delivery_completion_claimed: Literal[False] = False
    timeout_seconds: float = Field(gt=0)
    blocked_reasons: tuple[str, ...] = ()

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_reasons(cls, value: Any) -> tuple[str, ...]:
        return tuple(value or ())

    @model_validator(mode="after")
    def _validate_transition(self) -> "MissionOSAutoMissionModeTransitionSummary":
        if (
            self.mode_transition_status
            is MissionOSAutoMissionModeTransitionStatus.COMMAND_ACKS_ACCEPTED
        ):
            if self.blocked_reasons:
                raise MissionOSAutoMissionRunnerError(
                    "accepted AUTO mode transition cannot be blocked"
                )
            if self.mission_upload_accepted is not True:
                raise MissionOSAutoMissionRunnerError(
                    "accepted AUTO mode transition requires upload accepted"
                )
            if (
                self.arm_command_ack_observed is not True
                or self.arm_command_ack_result != MAV_RESULT_ACCEPTED
            ):
                raise MissionOSAutoMissionRunnerError(
                    "accepted AUTO mode transition requires accepted arm ACK"
                )
            if (
                self.auto_mission_mode_ack_observed is not True
                or self.auto_mission_mode_ack_result != MAV_RESULT_ACCEPTED
            ):
                raise MissionOSAutoMissionRunnerError(
                    "accepted AUTO mode transition requires accepted mode ACK"
                )
            if (
                self.auto_mission_abort_ack_observed is not True
                or self.auto_mission_abort_ack_result != MAV_RESULT_ACCEPTED
            ):
                raise MissionOSAutoMissionRunnerError(
                    "accepted AUTO mode transition requires accepted abort ACK"
                )
        else:
            if not self.blocked_reasons:
                raise MissionOSAutoMissionRunnerError(
                    "blocked/timeout AUTO mode transition requires reasons"
                )
        return self


class MissionOSAutoMissionPhase3BLiveBoundarySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        MISSIONOS_AUTO_MISSION_PHASE3B_LIVE_BOUNDARY_SUMMARY_SCHEMA_VERSION
    ] = MISSIONOS_AUTO_MISSION_PHASE3B_LIVE_BOUNDARY_SUMMARY_SCHEMA_VERSION
    auto_mission_runner_invoked: Literal[True] = True
    auto_mission_runner_version: Literal["phase3b_live_boundary_v1"] = (
        "phase3b_live_boundary_v1"
    )
    auto_mission_execution_mode: Literal["auto_mission_mode_probe_immediate_abort"] = (
        "auto_mission_mode_probe_immediate_abort"
    )
    simulation_only: Literal[True] = True
    loopback_sitl_only: Literal[True] = True
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    mission_upload_accepted: bool
    mission_count_sent: int = Field(ge=0)
    mission_ack_observed: bool
    mission_ack_result: int | None = None
    arm_command_id: Literal[MAV_CMD_COMPONENT_ARM_DISARM] = (
        MAV_CMD_COMPONENT_ARM_DISARM
    )
    arm_command_ack_observed: bool
    arm_command_ack_result: int | None = None
    auto_mission_mode_command_id: Literal[MAV_CMD_DO_SET_MODE] = MAV_CMD_DO_SET_MODE
    auto_mission_mode_ack_observed: bool
    auto_mission_mode_ack_result: int | None = None
    nav_state_auto_mission_observed: bool
    observed_nav_state: int | None = None
    immediate_abort_policy: Literal[PHASE3B_ABORT_REASON] = PHASE3B_ABORT_REASON
    immediate_abort_command_id: Literal[MAV_CMD_NAV_LAND] = MAV_CMD_NAV_LAND
    immediate_abort_ack_observed: bool
    immediate_abort_ack_result: int | None = None
    disarm_observed: bool
    arming_state_after_abort: int | None = None
    landed_state_after_abort: int | None = None
    observed_progress_m: float = Field(ge=0)
    auto_mission_started: bool
    monitor_loop_started: Literal[False] = False
    route_completed_claimed: Literal[False] = False
    dropoff_region_reached: Literal[False] = False
    dropoff_verified: Literal[False] = False
    payload_release_observed: Literal[False] = False
    delivery_completion_claimed: Literal[False] = False
    blocked_reasons: tuple[str, ...] = ()

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_blocked_reasons(cls, value: Any) -> tuple[str, ...]:
        return tuple(value or ())

    @model_validator(mode="after")
    def _validate_live_boundary(
        self,
    ) -> "MissionOSAutoMissionPhase3BLiveBoundarySummary":
        if self.auto_mission_started:
            if self.mission_upload_accepted is not True:
                raise MissionOSAutoMissionRunnerError(
                    "AUTO mission start observation requires accepted upload"
                )
            if (
                self.arm_command_ack_observed is not True
                or self.arm_command_ack_result != MAV_RESULT_ACCEPTED
            ):
                raise MissionOSAutoMissionRunnerError(
                    "AUTO mission start observation requires accepted arm ACK"
                )
            if (
                self.auto_mission_mode_ack_observed is not True
                or self.auto_mission_mode_ack_result != MAV_RESULT_ACCEPTED
            ):
                raise MissionOSAutoMissionRunnerError(
                    "AUTO mission start observation requires accepted mode ACK"
                )
            if self.nav_state_auto_mission_observed is not True:
                raise MissionOSAutoMissionRunnerError(
                    "AUTO mission start observation requires AUTO nav state"
                )
        if self.immediate_abort_ack_observed is not True and not self.blocked_reasons:
            raise MissionOSAutoMissionRunnerError(
                "3B live boundary requires blocked reason when abort ACK is missing"
            )
        return self


class MissionOSAutoMissionPayloadReleaseL0Summary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        MISSIONOS_AUTO_MISSION_PAYLOAD_RELEASE_L0_SUMMARY_SCHEMA_VERSION
    ] = MISSIONOS_AUTO_MISSION_PAYLOAD_RELEASE_L0_SUMMARY_SCHEMA_VERSION
    release_model: Literal["hover_and_release"] = "hover_and_release"
    dropoff_verified: bool
    payload_release_command_id: Literal[MAV_CMD_DO_GRIPPER] = MAV_CMD_DO_GRIPPER
    payload_release_gripper_id: float = DEFAULT_PAYLOAD_RELEASE_GRIPPER_ID
    payload_release_action: Literal[MAV_GRIPPER_ACTION_RELEASE] = (
        MAV_GRIPPER_ACTION_RELEASE
    )
    heartbeats_sent_before_payload_release: int = Field(ge=0)
    payload_release_command_frame_sent: bool
    payload_release_command_ack_required: Literal[True] = True
    payload_release_command_ack_observed: bool
    payload_release_command_ack_result: int | None = None
    payload_release_command_ack_result_name: str | None = None
    payload_release_command_acked: bool
    payload_release_observed_sim: Literal[False] = False
    sitl_delivery_claimed: Literal[False] = False
    delivery_completion_claimed: Literal[False] = False
    blocked_reasons: tuple[str, ...] = ()

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @model_validator(mode="after")
    def _validate_payload_release_l0(
        self,
    ) -> "MissionOSAutoMissionPayloadReleaseL0Summary":
        if self.payload_release_command_acked:
            if not self.dropoff_verified:
                raise MissionOSAutoMissionRunnerError(
                    "payload release ACK claim requires verified dropoff"
                )
            if self.payload_release_command_frame_sent is not True:
                raise MissionOSAutoMissionRunnerError(
                    "payload release ACK claim requires command frame"
                )
            if (
                self.payload_release_command_ack_observed is not True
                or self.payload_release_command_ack_result != MAV_RESULT_ACCEPTED
            ):
                raise MissionOSAutoMissionRunnerError(
                    "payload release ACK claim requires accepted COMMAND_ACK"
                )
            if self.blocked_reasons:
                raise MissionOSAutoMissionRunnerError(
                    "payload release ACK claim cannot be blocked"
                )
        else:
            if not self.blocked_reasons:
                raise MissionOSAutoMissionRunnerError(
                    "false payload release ACK claim requires blocked reasons"
                )
        return self


class MissionOSAutoMissionTelemetrySample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        MISSIONOS_AUTO_MISSION_TELEMETRY_SAMPLE_SCHEMA_VERSION
    ] = MISSIONOS_AUTO_MISSION_TELEMETRY_SAMPLE_SCHEMA_VERSION
    sample_index: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    sample_source: Literal["px4_listener"] = "px4_listener"
    nav_state: int | None = None
    arming_state: int | None = None
    landed_state: int | None = None
    local_x_m: float | None = None
    local_y_m: float | None = None
    local_z_m: float | None = None
    local_vx_mps: float | None = None
    local_vy_mps: float | None = None
    local_vz_mps: float | None = None
    global_latitude_deg: float | None = None
    global_longitude_deg: float | None = None
    global_altitude_m: float | None = None
    mission_current_seq: int | None = None
    mission_reached_seq: int | None = None
    battery_status_observed: bool = False
    battery_remaining_percent: float | None = None
    battery_warning: int | None = None
    telemetry_stale: bool = False

    @property
    def altitude_above_home_m(self) -> float | None:
        if self.local_z_m is None:
            return None
        return max(0.0, -float(self.local_z_m))


class MissionOSAutoMissionRuntimeMonitorSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        MISSIONOS_AUTO_MISSION_RUNTIME_MONITOR_SUMMARY_SCHEMA_VERSION
    ] = MISSIONOS_AUTO_MISSION_RUNTIME_MONITOR_SUMMARY_SCHEMA_VERSION
    auto_mission_runner_invoked: Literal[True] = True
    auto_mission_runner_version: Literal["phase3c3d_runtime_monitor_v1"] = (
        "phase3c3d_runtime_monitor_v1"
    )
    auto_mission_execution_mode: Literal[
        "auto_mission_runtime_monitor_with_guarded_abort"
    ] = "auto_mission_runtime_monitor_with_guarded_abort"
    simulation_only: Literal[True] = True
    loopback_sitl_only: Literal[True] = True
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    operator_coordinate_execution: bool
    mission_upload_accepted: bool
    mission_count_sent: int = Field(ge=0)
    mission_ack_observed: bool
    mission_ack_result: int | None = None
    arm_command_ack_observed: bool
    arm_command_ack_result: int | None = None
    auto_mission_mode_ack_observed: bool
    auto_mission_mode_ack_result: int | None = None
    auto_mission_started: bool
    auto_mission_started_at: str | None = None
    auto_mission_completed_at: str | None = None
    monitor_loop_started: bool
    monitor_target_seconds: float = Field(ge=0)
    monitor_elapsed_seconds: float = Field(ge=0)
    telemetry_sample_count: int = Field(ge=0)
    auto_mission_nav_state_observed: bool
    auto_mission_nav_state_samples: tuple[int, ...] = ()
    heartbeat_samples: int = Field(ge=0)
    statustext_during_auto: tuple[str, ...] = ()
    local_ned_pose_samples_path: str | None = None
    global_position_samples_path: str | None = None
    mavlink_event_log_path: str | None = None
    mission_current_samples: tuple[int, ...] = ()
    mission_item_reached_events: tuple[int, ...] = ()
    last_reached_waypoint_seq: int | None = None
    waypoint_reached_count: int = Field(ge=0)
    route_waypoint_seq_start: int = Field(ge=0)
    route_waypoint_seq_end: int = Field(ge=0)
    dropoff_dwell_mission_seq: int | None = Field(default=None, ge=0)
    land_mission_seq: int = Field(ge=0)
    route_waypoint_reached_count: int = Field(ge=0)
    last_reached_route_waypoint_index: int | None = Field(default=None, ge=1)
    route_waypoint_reached_fraction: float = Field(ge=0, le=1)
    planned_route_m: float = Field(gt=0)
    planned_waypoint_count: int = Field(ge=0)
    generated_route_waypoint_count: int = Field(ge=0)
    expected_duration_seconds: float = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    observed_progress_m: float = Field(ge=0)
    distance_to_dropoff_m: float | None = None
    route_terminal_local_ned_pose: dict[str, float | int | None] | None = None
    landing_terminal_pose: dict[str, float | int | None] | None = None
    completed_terminal_pose: dict[str, float | int | None] | None = None
    battery_guard_status: str
    altitude_envelope_status: str
    geofence_status: str
    no_progress_status: str
    telemetry_stale_status: str
    mode_loss_status: str
    guard_failure_reasons: tuple[str, ...] = ()
    abort_policy_selected_action: Literal[
        "land",
        "return_to_launch",
        "adjust_altitude",
        "adjust_speed",
        "reroute",
        "avoid_obstacle",
        "none",
    ]
    abort_reason: str | None = None
    probe_stop_reason: str | None = None
    guard_abort_requested: bool
    abort_retry_count: int = Field(default=0, ge=0)
    recovery_path_taken: str | None = None
    recovery_command_ack_observed: bool
    recovery_command_ack_result: int | None = None
    final_landing_safe: bool
    recovery_agent_evidence_window_path: str | None = None
    recovery_agent_telemetry_snapshot: dict[str, Any] | None = None
    recovery_return_started: bool = False
    recovery_return_progress_m: float = Field(default=0.0, ge=0)
    recovery_distance_to_home_start_m: float | None = None
    recovery_distance_to_home_end_m: float | None = None
    recovery_distance_to_home_min_m: float | None = None
    recovery_distance_to_home_closing_steps: int = Field(default=0, ge=0)
    recovery_distance_to_home_opening_steps: int = Field(default=0, ge=0)
    recovery_telemetry_stale: bool = False
    recovery_telemetry_stale_after_sample: int | None = Field(default=None, ge=0)
    recovery_heartbeat_observed_count: int = Field(default=0, ge=0)
    recovery_latest_heartbeat_observed: bool | None = None
    recovery_observation_lost: bool = False
    recovery_observation_loss_classification: Literal[
        "none",
        "topic_stale_heartbeat_alive",
        "topic_stale_heartbeat_missing",
        "topic_stale_heartbeat_unknown",
    ] = "none"
    recovery_observation_lost_after_sample: int | None = Field(default=None, ge=0)
    recovery_incomplete_reason: str | None = None
    runtime_status: MissionOSAutoMissionRuntimeStatus
    route_completed_claimed: Literal[False] = False
    all_waypoints_reached: Literal[False] = False
    dropoff_region_reached: Literal[False] = False
    dropoff_verified: Literal[False] = False
    payload_release_command_id: Literal[MAV_CMD_DO_GRIPPER] = MAV_CMD_DO_GRIPPER
    payload_release_command_frame_sent: bool = False
    payload_release_command_ack_observed: bool = False
    payload_release_command_ack_result: int | None = None
    payload_release_command_ack_result_name: str | None = None
    payload_release_command_acked: bool = False
    payload_release_observed: Literal[False] = False
    payload_release_observed_sim: Literal[False] = False
    sitl_delivery_claimed: Literal[False] = False
    delivery_completion_claimed: Literal[False] = False
    blocked_reasons: tuple[str, ...] = ()

    @field_validator(
        "auto_mission_nav_state_samples",
        "statustext_during_auto",
        "mission_current_samples",
        "mission_item_reached_events",
        "guard_failure_reasons",
        "blocked_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @model_validator(mode="after")
    def _validate_runtime_monitor(
        self,
    ) -> "MissionOSAutoMissionRuntimeMonitorSummary":
        if self.auto_mission_started:
            if self.mission_upload_accepted is not True:
                raise MissionOSAutoMissionRunnerError(
                    "AUTO runtime start requires accepted mission upload"
                )
            if (
                self.arm_command_ack_observed is not True
                or self.arm_command_ack_result != MAV_RESULT_ACCEPTED
            ):
                raise MissionOSAutoMissionRunnerError(
                    "AUTO runtime start requires accepted arm ACK"
                )
            if (
                self.auto_mission_mode_ack_observed is not True
                or self.auto_mission_mode_ack_result != MAV_RESULT_ACCEPTED
            ):
                raise MissionOSAutoMissionRunnerError(
                    "AUTO runtime start requires accepted mode ACK"
                )
            if self.auto_mission_nav_state_observed is not True:
                raise MissionOSAutoMissionRunnerError(
                    "AUTO runtime start requires AUTO.MISSION nav state"
                )
        if self.monitor_loop_started and self.telemetry_sample_count <= 0:
            raise MissionOSAutoMissionRunnerError(
                "started AUTO runtime monitor requires telemetry samples"
            )
        if self.runtime_status is MissionOSAutoMissionRuntimeStatus.GUARD_ABORTED:
            if not self.guard_failure_reasons:
                raise MissionOSAutoMissionRunnerError(
                    "guard-aborted AUTO runtime requires guard failure reasons"
                )
            if self.abort_policy_selected_action not in {
                "land",
                "return_to_launch",
                "adjust_altitude",
                "adjust_speed",
                "reroute",
                "avoid_obstacle",
            }:
                raise MissionOSAutoMissionRunnerError(
                    "guard-aborted AUTO runtime requires bounded abort policy"
                )
        if self.payload_release_command_acked:
            if self.payload_release_command_frame_sent is not True:
                raise MissionOSAutoMissionRunnerError(
                    "runtime payload release ACK requires command frame"
                )
            if (
                self.payload_release_command_ack_observed is not True
                or self.payload_release_command_ack_result != MAV_RESULT_ACCEPTED
            ):
                raise MissionOSAutoMissionRunnerError(
                    "runtime payload release ACK requires accepted COMMAND_ACK"
                )
        if not self.blocked_reasons and self.delivery_completion_claimed is False:
            raise MissionOSAutoMissionRunnerError(
                "AUTO runtime monitor must explain false delivery claim"
            )
        return self


class MissionOSAutoMissionWaypointGateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        MISSIONOS_AUTO_MISSION_WAYPOINT_GATE_SUMMARY_SCHEMA_VERSION
    ] = MISSIONOS_AUTO_MISSION_WAYPOINT_GATE_SUMMARY_SCHEMA_VERSION
    gate_name: Literal["phase4_waypoint_reach_minimal"] = (
        "phase4_waypoint_reach_minimal"
    )
    route_waypoint_seq_start: int = Field(ge=0)
    route_waypoint_seq_end: int = Field(ge=0)
    expected_route_waypoint_sequences: tuple[int, ...] = ()
    mission_item_reached_events: tuple[int, ...] = ()
    reached_route_waypoint_sequences: tuple[int, ...] = ()
    missing_route_waypoint_sequences: tuple[int, ...] = ()
    route_waypoint_reached_count: int = Field(ge=0)
    expected_route_waypoint_count: int = Field(ge=0)
    route_waypoint_reached_fraction: float = Field(ge=0, le=1)
    all_waypoints_reached: bool
    route_completed_claimed: bool
    delivery_completion_claimed: Literal[False] = False
    blocked_reasons: tuple[str, ...] = ()

    @field_validator(
        "expected_route_waypoint_sequences",
        "mission_item_reached_events",
        "reached_route_waypoint_sequences",
        "missing_route_waypoint_sequences",
        "blocked_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @model_validator(mode="after")
    def _validate_waypoint_gate(self) -> "MissionOSAutoMissionWaypointGateSummary":
        if self.route_waypoint_seq_end < self.route_waypoint_seq_start:
            if self.expected_route_waypoint_sequences:
                raise MissionOSAutoMissionRunnerError(
                    "waypoint gate expected route sequences for empty route"
                )
        elif self.expected_route_waypoint_sequences != tuple(
            range(self.route_waypoint_seq_start, self.route_waypoint_seq_end + 1)
        ):
            raise MissionOSAutoMissionRunnerError(
                "waypoint gate expected route sequence mismatch"
            )
        if self.route_waypoint_reached_count != len(
            self.reached_route_waypoint_sequences
        ):
            raise MissionOSAutoMissionRunnerError(
                "waypoint gate reached count mismatch"
            )
        if self.expected_route_waypoint_count != len(
            self.expected_route_waypoint_sequences
        ):
            raise MissionOSAutoMissionRunnerError(
                "waypoint gate expected count mismatch"
            )
        if self.all_waypoints_reached:
            if self.missing_route_waypoint_sequences:
                raise MissionOSAutoMissionRunnerError(
                    "waypoint gate cannot claim complete with missing route waypoints"
                )
            if not self.route_completed_claimed:
                raise MissionOSAutoMissionRunnerError(
                    "waypoint gate all reached requires route completed claim"
                )
        else:
            if self.route_completed_claimed:
                raise MissionOSAutoMissionRunnerError(
                    "waypoint gate cannot claim route complete with missing waypoints"
                )
            if self.expected_route_waypoint_sequences and not self.blocked_reasons:
                raise MissionOSAutoMissionRunnerError(
                    "waypoint gate false route claim requires blocked reasons"
                )
        return self


class MissionOSAutoMissionDropoffGateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        MISSIONOS_AUTO_MISSION_DROPOFF_GATE_SUMMARY_SCHEMA_VERSION
    ] = MISSIONOS_AUTO_MISSION_DROPOFF_GATE_SUMMARY_SCHEMA_VERSION
    gate_name: Literal["phase5_dropoff_hover_release_envelope"] = (
        "phase5_dropoff_hover_release_envelope"
    )
    release_model: Literal["hover_and_release"] = "hover_and_release"
    dropoff_latitude_deg: float
    dropoff_longitude_deg: float
    release_radius_m: float = Field(gt=0)
    release_altitude_target_m: float = Field(ge=0)
    release_altitude_min_m: float = Field(ge=0)
    release_altitude_max_m: float = Field(ge=0)
    required_dwell_seconds: float = Field(ge=0)
    observed_min_residual_xy_m: float | None = None
    observed_dwell_seconds: float = Field(ge=0)
    qualifying_sample_indices: tuple[int, ...] = ()
    residual_xy_ok: bool
    altitude_ok: bool
    dwell_ok: bool
    route_completed_claimed: bool
    dropoff_verified: bool
    payload_release_command_acked: Literal[False] = False
    payload_release_observed_sim: Literal[False] = False
    sitl_delivery_claimed: Literal[False] = False
    delivery_completion_claimed: Literal[False] = False
    blocked_reasons: tuple[str, ...] = ()

    @field_validator("qualifying_sample_indices", "blocked_reasons", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @model_validator(mode="after")
    def _validate_dropoff_gate(self) -> "MissionOSAutoMissionDropoffGateSummary":
        if not (
            self.release_altitude_min_m
            <= self.release_altitude_target_m
            <= self.release_altitude_max_m
        ):
            raise MissionOSAutoMissionRunnerError(
                "dropoff gate altitude band must contain target altitude"
            )
        all_gate_inputs_passed = (
            self.route_completed_claimed
            and self.residual_xy_ok
            and self.altitude_ok
            and self.dwell_ok
        )
        if self.dropoff_verified != all_gate_inputs_passed:
            raise MissionOSAutoMissionRunnerError(
                "dropoff gate claim must match route/residual/altitude/dwell checks"
            )
        if self.dropoff_verified and self.blocked_reasons:
            raise MissionOSAutoMissionRunnerError(
                "verified dropoff gate cannot carry blocked reasons"
            )
        if not self.dropoff_verified and not self.blocked_reasons:
            raise MissionOSAutoMissionRunnerError(
                "false dropoff gate requires blocked reasons"
            )
        return self


class MissionOSAutoMissionSITLDeliveryGateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        MISSIONOS_AUTO_MISSION_SITL_DELIVERY_GATE_SUMMARY_SCHEMA_VERSION
    ] = MISSIONOS_AUTO_MISSION_SITL_DELIVERY_GATE_SUMMARY_SCHEMA_VERSION
    gate_name: Literal["phase6_sitl_delivery_command_ack"] = (
        "phase6_sitl_delivery_command_ack"
    )
    claim_model: Literal["sitl_command_ack_only"] = "sitl_command_ack_only"
    route_completed_claimed: bool
    dropoff_verified: bool
    payload_release_command_acked: bool
    payload_release_observed_sim: Literal[False] = False
    physical_delivery_verified: Literal[False] = False
    sitl_delivery_claimed: bool
    delivery_completion_claimed: Literal[False] = False
    blocked_reasons: tuple[str, ...] = ()

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @model_validator(mode="after")
    def _validate_sitl_delivery_gate(
        self,
    ) -> "MissionOSAutoMissionSITLDeliveryGateSummary":
        expected = (
            self.route_completed_claimed
            and self.dropoff_verified
            and self.payload_release_command_acked
        )
        if self.sitl_delivery_claimed != expected:
            raise MissionOSAutoMissionRunnerError(
                "SITL delivery claim must match route/dropoff/payload ACK chain"
            )
        if self.sitl_delivery_claimed:
            if self.blocked_reasons:
                raise MissionOSAutoMissionRunnerError(
                    "claimed SITL delivery cannot carry blocked reasons"
                )
        elif not self.blocked_reasons:
            raise MissionOSAutoMissionRunnerError(
                "false SITL delivery claim requires blocked reasons"
            )
        return self


class MissionOSAutoMissionPayloadReleaseSimGateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        MISSIONOS_AUTO_MISSION_PAYLOAD_RELEASE_SIM_GATE_SUMMARY_SCHEMA_VERSION
    ] = MISSIONOS_AUTO_MISSION_PAYLOAD_RELEASE_SIM_GATE_SUMMARY_SCHEMA_VERSION
    gate_name: Literal["l1_gazebo_cargo_release_observation"] = (
        "l1_gazebo_cargo_release_observation"
    )
    observation_model: Literal["gazebo_detachable_joint_detach_event_only"] = (
        "gazebo_detachable_joint_detach_event_only"
    )
    route_completed_claimed: bool
    dropoff_verified: bool
    payload_release_command_acked: bool
    payload_release_observed_sim: bool
    payload_release_event_source: str | None = None
    payload_release_observed_at: str | None = None
    gazebo_detachable_joint_release_performed: bool = False
    gazebo_detachable_joint_release_observed: bool = False
    physical_delivery_verified: Literal[False] = False
    delivery_completion_claimed: Literal[False] = False
    blocked_reasons: tuple[str, ...] = ()

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @model_validator(mode="after")
    def _validate_payload_release_sim_gate(
        self,
    ) -> "MissionOSAutoMissionPayloadReleaseSimGateSummary":
        event_source_ok = (
            self.payload_release_event_source == "gazebo_detachable_joint_detach_event"
        )
        event_observed = (
            event_source_ok
            and self.gazebo_detachable_joint_release_performed
            and self.gazebo_detachable_joint_release_observed
            and bool(self.payload_release_observed_at)
        )
        expected = (
            self.route_completed_claimed
            and self.dropoff_verified
            and self.payload_release_command_acked
            and event_observed
        )
        if self.payload_release_observed_sim != expected:
            raise MissionOSAutoMissionRunnerError(
                "L1 simulated payload release claim must match route/dropoff/"
                "payload ACK plus Gazebo detachable-joint event evidence"
            )
        if self.payload_release_observed_sim:
            if self.blocked_reasons:
                raise MissionOSAutoMissionRunnerError(
                    "observed L1 payload release cannot carry blocked reasons"
                )
        elif not self.blocked_reasons:
            raise MissionOSAutoMissionRunnerError(
                "false L1 payload release claim requires blocked reasons"
            )
        return self


class MissionOSAutoMissionLoopbackUploader:
    """Upload compiled mission items to a loopback MAVLink peer."""

    def upload(
        self,
        *,
        items: Sequence[PX4GazeboSITLMissionItem],
        target_endpoint: str,
        timeout_seconds: float,
    ) -> tuple[tuple[int, ...], int]:
        host, port = _loopback_udp_host_port(target_endpoint)
        requests: list[int] = []
        sequence = 0
        deadline = _deadline(timeout_seconds)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(_remaining(deadline))
            sock.bind(("127.0.0.1", 0))
            sock.sendto(
                encode_mavlink2_mission_count(
                    count=len(items),
                    target_system=1,
                    target_component=1,
                    sequence=sequence,
                ),
                (host, port),
            )
            sequence += 1
            while len(requests) < len(items):
                sock.settimeout(_remaining(deadline))
                data, _addr = sock.recvfrom(2048)
                ack_type = decode_mavlink2_mission_ack_type(data)
                if ack_type is not None:
                    return tuple(requests), ack_type
                requested_seq = decode_mavlink2_mission_request_int(data)
                if requested_seq is None:
                    continue
                if requested_seq >= len(items):
                    raise MissionOSAutoMissionRunnerError(
                        "mission_request_seq_out_of_range"
                    )
                requests.append(requested_seq)
                sock.sendto(
                    encode_mavlink2_mission_item_int(
                        item=items[requested_seq],
                        target_system=1,
                        target_component=1,
                        sequence=sequence,
                    ),
                    (host, port),
                )
                sequence += 1
            while True:
                sock.settimeout(_remaining(deadline))
                data, _addr = sock.recvfrom(2048)
                ack_type = decode_mavlink2_mission_ack_type(data)
                if ack_type is not None:
                    return tuple(requests), ack_type


class MissionOSAutoMissionModeTransitioner:
    """Send ARM and AUTO.MISSION COMMAND_LONG frames to a loopback MAVLink peer."""

    def transition(
        self,
        *,
        target_endpoint: str,
        timeout_seconds: float,
    ) -> _ModeTransitionTrace:
        host, port = _loopback_udp_host_port(target_endpoint)
        sequence = 0
        deadline = _deadline(timeout_seconds)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(_remaining(deadline))
            sock.bind(("127.0.0.1", 0))
            remote = (host, port)
            sock.sendto(encode_mavlink2_heartbeat(sequence=sequence), remote)
            sequence += 1
            sock.sendto(
                encode_auto_mission_command_long(
                    command_id=MAV_CMD_COMPONENT_ARM_DISARM,
                    params=(1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                    sequence=sequence,
                ),
                remote,
            )
            sequence += 1
            arm_ack = _wait_command_ack(
                sock=sock,
                command_id=MAV_CMD_COMPONENT_ARM_DISARM,
                deadline=deadline,
            )
            if arm_ack.result_code != MAV_RESULT_ACCEPTED:
                return _ModeTransitionTrace(
                    heartbeats_sent=1,
                    arm_command_frame_sent=True,
                    arm_command_ack=arm_ack,
                    auto_mission_mode_command_frame_sent=False,
                    auto_mission_mode_command_ack=_CommandAck(observed=False),
                    auto_mission_abort_command_frame_sent=False,
                    auto_mission_abort_command_ack=_CommandAck(observed=False),
                )

            sock.settimeout(_remaining(deadline))
            sock.sendto(encode_mavlink2_heartbeat(sequence=sequence), remote)
            sequence += 1
            sock.sendto(
                encode_auto_mission_command_long(
                    command_id=MAV_CMD_DO_SET_MODE,
                    params=_auto_mission_mode_params(),
                    sequence=sequence,
                ),
                remote,
            )
            mode_ack = _wait_command_ack(
                sock=sock,
                command_id=MAV_CMD_DO_SET_MODE,
                deadline=deadline,
            )
            if mode_ack.result_code != MAV_RESULT_ACCEPTED:
                return _ModeTransitionTrace(
                    heartbeats_sent=2,
                    arm_command_frame_sent=True,
                    arm_command_ack=arm_ack,
                    auto_mission_mode_command_frame_sent=True,
                    auto_mission_mode_command_ack=mode_ack,
                    auto_mission_abort_command_frame_sent=False,
                    auto_mission_abort_command_ack=_CommandAck(observed=False),
                )

            sock.settimeout(_remaining(deadline))
            sock.sendto(encode_mavlink2_heartbeat(sequence=sequence), remote)
            sequence += 1
            sock.sendto(
                encode_auto_mission_command_long(
                    command_id=MAV_CMD_NAV_LAND,
                    params=_land_abort_params(),
                    sequence=sequence,
                ),
                remote,
            )
            abort_ack = _wait_command_ack(
                sock=sock,
                command_id=MAV_CMD_NAV_LAND,
                deadline=deadline,
            )
            return _ModeTransitionTrace(
                heartbeats_sent=3,
                arm_command_frame_sent=True,
                arm_command_ack=arm_ack,
                auto_mission_mode_command_frame_sent=True,
                auto_mission_mode_command_ack=mode_ack,
                auto_mission_abort_command_frame_sent=True,
                auto_mission_abort_command_ack=abort_ack,
            )


class MissionOSAutoMissionPayloadReleaseCommander:
    """Send the L0 payload release COMMAND_LONG to a loopback MAVLink peer."""

    def release(
        self,
        *,
        target_endpoint: str,
        timeout_seconds: float,
        gripper_id: float = DEFAULT_PAYLOAD_RELEASE_GRIPPER_ID,
    ) -> _PayloadReleaseTrace:
        host, port = _loopback_udp_host_port(target_endpoint)
        sequence = 0
        deadline = _deadline(timeout_seconds)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(_remaining(deadline))
            sock.bind(("127.0.0.1", 0))
            remote = (host, port)
            sock.sendto(encode_mavlink2_heartbeat(sequence=sequence), remote)
            sequence += 1
            sock.sendto(
                encode_auto_mission_command_long(
                    command_id=MAV_CMD_DO_GRIPPER,
                    params=_payload_release_params(gripper_id),
                    sequence=sequence,
                ),
                remote,
            )
            release_ack = _wait_command_ack(
                sock=sock,
                command_id=MAV_CMD_DO_GRIPPER,
                deadline=deadline,
            )
            return _PayloadReleaseTrace(
                heartbeats_sent=1,
                payload_release_command_frame_sent=True,
                payload_release_command_ack=release_ack,
            )


def compile_operator_coordinate_route_auto_mission(
    route: Mapping[str, Any],
    *,
    waypoint_spacing_m: float = DEFAULT_AUTO_WAYPOINT_SPACING_M,
    max_route_waypoints: int = DEFAULT_AUTO_MAX_ROUTE_WAYPOINTS,
    cruise_speed_mps: float = DEFAULT_AUTO_CRUISE_SPEED_MPS,
) -> MissionOSAutoMissionCompilation:
    if waypoint_spacing_m <= 0:
        raise MissionOSAutoMissionRunnerError("waypoint_spacing_m_must_be_positive")
    if max_route_waypoints <= 0:
        raise MissionOSAutoMissionRunnerError("max_route_waypoints_must_be_positive")
    if cruise_speed_mps <= 0:
        raise MissionOSAutoMissionRunnerError("cruise_speed_mps_must_be_positive")

    takeoff_lat = _required_float(route, "takeoff_latitude")
    takeoff_lon = _required_float(route, "takeoff_longitude")
    dropoff_lat = _required_float(route, "dropoff_latitude")
    dropoff_lon = _required_float(route, "dropoff_longitude")
    roof_agl_m = _required_float(route, "dropoff_roof_height_agl_m")
    planned_route_m = _planned_route_m(
        route,
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
        dropoff_lat=dropoff_lat,
        dropoff_lon=dropoff_lon,
    )
    requested_route_waypoint_count = _optional_positive_int(
        route.get("auto_route_waypoint_count"),
        route.get("auto_waypoint_count"),
        route.get("route_waypoint_count"),
    )
    if (
        requested_route_waypoint_count is not None
        and waypoint_spacing_m == DEFAULT_AUTO_WAYPOINT_SPACING_M
    ):
        waypoint_spacing_m = planned_route_m / requested_route_waypoint_count
    route_waypoint_count = max(1, int(math.ceil(planned_route_m / waypoint_spacing_m)))
    if route_waypoint_count > max_route_waypoints:
        raise MissionOSAutoMissionRunnerError("route_waypoint_count_exceeds_limit")

    base_altitude_m = max(float(roof_agl_m), 10.0)
    terrain_clearance_target_m = _optional_positive_float(
        route.get("terrain_clearance_agl_m"),
        route.get("terrain_clearance_target_m"),
        route.get("minimum_terrain_clearance_m"),
    )
    if terrain_clearance_target_m is None:
        terrain_clearance_target_m = DEFAULT_TERRAIN_CLEARANCE_AGL_M
    terrain_samples = _terrain_profile_samples(route)
    terrain_profile_blocked_reason = (
        _terrain_profile_execution_blocked_reason(
            route,
            terrain_samples,
            planned_route_m=planned_route_m,
        )
        if terrain_samples
        else None
    )
    terrain_clearance_enabled = bool(terrain_samples) and (
        terrain_profile_blocked_reason is None
    )
    terrain_profile_source, terrain_profile_ref = _terrain_profile_source_ref(route)
    takeoff_terrain_elevation_m = _sample_terrain_elevation_m(
        terrain_samples,
        fraction=0.0,
        distance_m=0.0,
        planned_route_m=planned_route_m,
    )
    takeoff_altitude_m, takeoff_clearance_m, takeoff_terrain_applied = (
        _terrain_relative_altitude_m(
            terrain_elevation_m=takeoff_terrain_elevation_m,
            takeoff_terrain_elevation_m=takeoff_terrain_elevation_m,
            clearance_agl_m=terrain_clearance_target_m,
            fallback_altitude_m=base_altitude_m,
        )
        if terrain_clearance_enabled
        else (base_altitude_m, None, False)
    )
    terrain_clearance_profile: list[dict[str, float | int | bool]] = [
        {
            "seq": 0,
            "fraction": 0.0,
            "distance_m": 0.0,
            "terrain_elevation_m": round(float(takeoff_terrain_elevation_m), 3)
            if takeoff_terrain_elevation_m is not None
            else 0.0,
            "target_clearance_m": round(float(terrain_clearance_target_m), 3),
            "mission_altitude_m": round(float(takeoff_altitude_m), 3),
            "clearance_m": round(float(takeoff_clearance_m), 3)
            if takeoff_clearance_m is not None
            else 0.0,
            "terrain_applied": takeoff_terrain_applied,
        }
    ]
    mission_items: list[PX4GazeboSITLMissionItem] = [
        PX4GazeboSITLMissionItem(
            seq=0,
            command=MAV_CMD_NAV_TAKEOFF,
            frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            latitude_deg=round(takeoff_lat, 7),
            longitude_deg=round(takeoff_lon, 7),
            altitude_m=takeoff_altitude_m,
            current=1,
        )
    ]
    for index in range(1, route_waypoint_count + 1):
        fraction = index / route_waypoint_count
        distance_m = planned_route_m * fraction
        terrain_elevation_m = _sample_terrain_elevation_m(
            terrain_samples,
            fraction=fraction,
            distance_m=distance_m,
            planned_route_m=planned_route_m,
        )
        waypoint_altitude_m, clearance_m, terrain_applied = (
            _terrain_relative_altitude_m(
                terrain_elevation_m=terrain_elevation_m,
                takeoff_terrain_elevation_m=takeoff_terrain_elevation_m,
                clearance_agl_m=terrain_clearance_target_m,
                fallback_altitude_m=base_altitude_m,
            )
            if terrain_clearance_enabled
            else (base_altitude_m, None, False)
        )
        terrain_clearance_profile.append(
            {
                "seq": len(mission_items),
                "fraction": round(float(fraction), 6),
                "distance_m": round(float(distance_m), 3),
                "terrain_elevation_m": round(float(terrain_elevation_m), 3)
                if terrain_elevation_m is not None
                else 0.0,
                "target_clearance_m": round(float(terrain_clearance_target_m), 3),
                "mission_altitude_m": round(float(waypoint_altitude_m), 3),
                "clearance_m": round(float(clearance_m), 3)
                if clearance_m is not None
                else 0.0,
                "terrain_applied": terrain_applied,
            }
        )
        mission_items.append(
            PX4GazeboSITLMissionItem(
                seq=len(mission_items),
                command=MAV_CMD_NAV_WAYPOINT,
                frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                latitude_deg=round(
                    takeoff_lat + (dropoff_lat - takeoff_lat) * fraction,
                    7,
                ),
                longitude_deg=round(
                    takeoff_lon + (dropoff_lon - takeoff_lon) * fraction,
                    7,
                ),
                altitude_m=waypoint_altitude_m,
            )
        )
    dropoff_dwell_seq = len(mission_items)
    mission_items.append(
        PX4GazeboSITLMissionItem(
            seq=dropoff_dwell_seq,
            command=MAV_CMD_NAV_LOITER_TIME,
            frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            param1=DEFAULT_DROPOFF_LOITER_SECONDS,
            latitude_deg=round(dropoff_lat, 7),
            longitude_deg=round(dropoff_lon, 7),
            altitude_m=mission_items[-1].altitude_m,
        )
    )
    land_seq = len(mission_items)
    mission_items.append(
        PX4GazeboSITLMissionItem(
            seq=land_seq,
            command=MAV_CMD_NAV_LAND,
            frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            latitude_deg=round(dropoff_lat, 7),
            longitude_deg=round(dropoff_lon, 7),
            altitude_m=0.0,
        )
    )

    expected_cruise_seconds = planned_route_m / cruise_speed_mps
    expected_total_seconds = (
        expected_cruise_seconds
        + DEFAULT_TAKEOFF_ALLOWANCE_SECONDS
        + DEFAULT_DROPOFF_LOITER_SECONDS
        + DEFAULT_LANDING_ALLOWANCE_SECONDS
    )
    timeout_seconds = min(
        expected_total_seconds * DEFAULT_TIMEOUT_SAFETY_FACTOR,
        DEFAULT_MAX_TIMEOUT_SECONDS,
    )
    route_id = str(route.get("route_id") or "unknown")
    return MissionOSAutoMissionCompilation(
        route_ref=f"mission_designer_coordinate_pair_route:{route_id}",
        planned_route_m=round(planned_route_m, 3),
        planned_waypoint_count=len(mission_items),
        generated_route_waypoint_count=route_waypoint_count,
        dropoff_dwell_mission_seq=dropoff_dwell_seq,
        land_mission_seq=land_seq,
        waypoint_spacing_m=float(waypoint_spacing_m),
        cruise_speed_mps=float(cruise_speed_mps),
        takeoff_altitude_m=takeoff_altitude_m,
        cruise_altitude_m=max(item.altitude_m for item in mission_items[:-1]),
        terrain_aware_altitudes_enabled=terrain_clearance_enabled,
        terrain_profile_source=terrain_profile_source or None,
        terrain_profile_ref=terrain_profile_ref or None,
        terrain_profile_treated_as_visual_only=bool(
            terrain_samples and not terrain_clearance_enabled
        ),
        terrain_profile_blocked_reason=terrain_profile_blocked_reason,
        terrain_clearance_target_m=(
            terrain_clearance_target_m if terrain_clearance_enabled else None
        ),
        terrain_clearance_profile=tuple(terrain_clearance_profile)
        if terrain_clearance_enabled
        else (),
        dropoff_dwell_seconds=DEFAULT_DROPOFF_LOITER_SECONDS,
        dropoff_release_min_dwell_seconds=DEFAULT_DROPOFF_DWELL_SECONDS,
        expected_duration_seconds=round(expected_total_seconds, 3),
        timeout_seconds=round(timeout_seconds, 3),
        mission_items=tuple(mission_items),
    )


def request_auto_mission_mode_transition_to_loopback_peer(
    upload_summary: MissionOSAutoMissionUploadSummary,
    *,
    target_endpoint: str,
    timeout_seconds: float = DEFAULT_AUTO_MODE_ACK_TIMEOUT_SECONDS,
    transitioner: MissionOSAutoMissionModeTransitioner | None = None,
) -> MissionOSAutoMissionModeTransitionSummary:
    resolved_timeout = float(timeout_seconds)
    upload_accepted = (
        upload_summary.auto_mission_upload_status
        is MissionOSAutoMissionUploadStatus.UPLOADED
        and upload_summary.mission_ack_observed is True
        and upload_summary.mission_ack_result == MAV_MISSION_ACCEPTED
    )
    if not upload_accepted:
        return _mode_transition_summary(
            upload_summary,
            status=MissionOSAutoMissionModeTransitionStatus.BLOCKED,
            trace=_ModeTransitionTrace(
                heartbeats_sent=0,
                arm_command_frame_sent=False,
                arm_command_ack=_CommandAck(observed=False),
                auto_mission_mode_command_frame_sent=False,
                auto_mission_mode_command_ack=_CommandAck(observed=False),
                auto_mission_abort_command_frame_sent=False,
                auto_mission_abort_command_ack=_CommandAck(observed=False),
            ),
            blocked_reasons=("mission_upload_not_accepted",),
            timeout_seconds=resolved_timeout,
        )

    try:
        trace = (transitioner or MissionOSAutoMissionModeTransitioner()).transition(
            target_endpoint=target_endpoint,
            timeout_seconds=resolved_timeout,
        )
    except socket.timeout:
        return _mode_transition_summary(
            upload_summary,
            status=MissionOSAutoMissionModeTransitionStatus.TIMEOUT,
            trace=_ModeTransitionTrace(
                heartbeats_sent=0,
                arm_command_frame_sent=False,
                arm_command_ack=_CommandAck(observed=False),
                auto_mission_mode_command_frame_sent=False,
                auto_mission_mode_command_ack=_CommandAck(observed=False),
                auto_mission_abort_command_frame_sent=False,
                auto_mission_abort_command_ack=_CommandAck(observed=False),
            ),
            blocked_reasons=("auto_mission_mode_transition_timeout",),
            timeout_seconds=resolved_timeout,
        )
    except (OSError, MissionOSAutoMissionRunnerError) as exc:
        return _mode_transition_summary(
            upload_summary,
            status=MissionOSAutoMissionModeTransitionStatus.BLOCKED,
            trace=_ModeTransitionTrace(
                heartbeats_sent=0,
                arm_command_frame_sent=False,
                arm_command_ack=_CommandAck(observed=False),
                auto_mission_mode_command_frame_sent=False,
                auto_mission_mode_command_ack=_CommandAck(observed=False),
                auto_mission_abort_command_frame_sent=False,
                auto_mission_abort_command_ack=_CommandAck(observed=False),
            ),
            blocked_reasons=(str(exc),),
            timeout_seconds=resolved_timeout,
        )

    if trace.arm_command_ack.result_code != MAV_RESULT_ACCEPTED:
        reason = (
            "arm_command_ack_timeout"
            if trace.arm_command_ack.observed is not True
            else f"arm_command_ack_result_{trace.arm_command_ack.result_code}"
        )
        return _mode_transition_summary(
            upload_summary,
            status=(
                MissionOSAutoMissionModeTransitionStatus.TIMEOUT
                if trace.arm_command_ack.observed is not True
                else MissionOSAutoMissionModeTransitionStatus.BLOCKED
            ),
            trace=trace,
            blocked_reasons=(reason,),
            timeout_seconds=resolved_timeout,
        )
    if trace.auto_mission_mode_command_ack.result_code != MAV_RESULT_ACCEPTED:
        reason = (
            "auto_mission_mode_ack_timeout"
            if trace.auto_mission_mode_command_ack.observed is not True
            else (
                "auto_mission_mode_ack_result_"
                f"{trace.auto_mission_mode_command_ack.result_code}"
            )
        )
        return _mode_transition_summary(
            upload_summary,
            status=(
                MissionOSAutoMissionModeTransitionStatus.TIMEOUT
                if trace.auto_mission_mode_command_ack.observed is not True
                else MissionOSAutoMissionModeTransitionStatus.BLOCKED
            ),
            trace=trace,
            blocked_reasons=(reason,),
            timeout_seconds=resolved_timeout,
        )
    if trace.auto_mission_abort_command_ack.result_code != MAV_RESULT_ACCEPTED:
        reason = (
            "auto_mission_abort_ack_timeout"
            if trace.auto_mission_abort_command_ack.observed is not True
            else (
                "auto_mission_abort_ack_result_"
                f"{trace.auto_mission_abort_command_ack.result_code}"
            )
        )
        return _mode_transition_summary(
            upload_summary,
            status=(
                MissionOSAutoMissionModeTransitionStatus.TIMEOUT
                if trace.auto_mission_abort_command_ack.observed is not True
                else MissionOSAutoMissionModeTransitionStatus.BLOCKED
            ),
            trace=trace,
            blocked_reasons=(reason,),
            timeout_seconds=resolved_timeout,
        )
    return _mode_transition_summary(
        upload_summary,
        status=MissionOSAutoMissionModeTransitionStatus.COMMAND_ACKS_ACCEPTED,
        trace=trace,
        blocked_reasons=(),
        timeout_seconds=resolved_timeout,
    )


def build_auto_mission_payload_release_l0_summary(
    *,
    dropoff_verified: bool,
    trace: _PayloadReleaseTrace,
    gripper_id: float = DEFAULT_PAYLOAD_RELEASE_GRIPPER_ID,
    blocked_reasons: Sequence[str] = (),
) -> MissionOSAutoMissionPayloadReleaseL0Summary:
    ack = trace.payload_release_command_ack
    command_acked = (
        bool(trace.payload_release_command_frame_sent)
        and ack.observed is True
        and ack.result_code == MAV_RESULT_ACCEPTED
        and bool(dropoff_verified)
    )
    resolved_reasons: list[str] = [
        str(item) for item in blocked_reasons if str(item).strip()
    ]
    if not dropoff_verified:
        resolved_reasons.append("dropoff_not_verified")
    if trace.payload_release_command_frame_sent and ack.observed is not True:
        resolved_reasons.append("payload_release_command_ack_timeout")
    elif (
        trace.payload_release_command_frame_sent
        and ack.result_code is not None
        and ack.result_code != MAV_RESULT_ACCEPTED
    ):
        resolved_reasons.append(f"payload_release_command_ack_result_{ack.result_code}")
    return MissionOSAutoMissionPayloadReleaseL0Summary(
        dropoff_verified=bool(dropoff_verified),
        payload_release_gripper_id=float(gripper_id),
        heartbeats_sent_before_payload_release=trace.heartbeats_sent,
        payload_release_command_frame_sent=trace.payload_release_command_frame_sent,
        payload_release_command_ack_observed=ack.observed,
        payload_release_command_ack_result=ack.result_code,
        payload_release_command_ack_result_name=ack.result_name,
        payload_release_command_acked=command_acked,
        blocked_reasons=_unique_tuple(resolved_reasons),
    )


def request_auto_mission_payload_release_to_loopback_peer(
    *,
    dropoff_verified: bool,
    target_endpoint: str,
    timeout_seconds: float = DEFAULT_AUTO_MODE_ACK_TIMEOUT_SECONDS,
    gripper_id: float = DEFAULT_PAYLOAD_RELEASE_GRIPPER_ID,
    commander: MissionOSAutoMissionPayloadReleaseCommander | None = None,
) -> MissionOSAutoMissionPayloadReleaseL0Summary:
    if not dropoff_verified:
        return build_auto_mission_payload_release_l0_summary(
            dropoff_verified=False,
            trace=_PayloadReleaseTrace(
                heartbeats_sent=0,
                payload_release_command_frame_sent=False,
                payload_release_command_ack=_CommandAck(observed=False),
            ),
            gripper_id=gripper_id,
        )
    try:
        trace = (commander or MissionOSAutoMissionPayloadReleaseCommander()).release(
            target_endpoint=target_endpoint,
            timeout_seconds=float(timeout_seconds),
            gripper_id=gripper_id,
        )
    except socket.timeout:
        trace = _PayloadReleaseTrace(
            heartbeats_sent=0,
            payload_release_command_frame_sent=False,
            payload_release_command_ack=_CommandAck(observed=False),
        )
        return build_auto_mission_payload_release_l0_summary(
            dropoff_verified=True,
            trace=trace,
            gripper_id=gripper_id,
            blocked_reasons=("payload_release_command_timeout",),
        )
    except (OSError, MissionOSAutoMissionRunnerError) as exc:
        trace = _PayloadReleaseTrace(
            heartbeats_sent=0,
            payload_release_command_frame_sent=False,
            payload_release_command_ack=_CommandAck(observed=False),
        )
        return build_auto_mission_payload_release_l0_summary(
            dropoff_verified=True,
            trace=trace,
            gripper_id=gripper_id,
            blocked_reasons=(str(exc),),
        )
    return build_auto_mission_payload_release_l0_summary(
        dropoff_verified=True,
        trace=trace,
        gripper_id=gripper_id,
    )


def build_auto_mission_runtime_monitor_summary(
    *,
    compilation: MissionOSAutoMissionCompilation,
    mission_upload_accepted: bool,
    mission_ack_observed: bool,
    mission_ack_result: int | None,
    arm_command_ack_observed: bool,
    arm_command_ack_result: int | None,
    auto_mission_mode_ack_observed: bool,
    auto_mission_mode_ack_result: int | None,
    samples: Sequence[MissionOSAutoMissionTelemetrySample | Mapping[str, Any]],
    monitor_target_seconds: float = DEFAULT_AUTO_RUNTIME_MONITOR_SECONDS,
    monitor_elapsed_seconds: float | None = None,
    heartbeat_samples: int = 0,
    operator_coordinate_execution: bool = True,
    statustext_during_auto: Sequence[str] = (),
    local_ned_pose_samples_path: str | None = None,
    global_position_samples_path: str | None = None,
    mavlink_event_log_path: str | None = None,
    abort_ack_observed: bool = False,
    abort_ack_result: int | None = None,
    abort_policy_selected_action: Literal[
        "land",
        "return_to_launch",
        "adjust_altitude",
        "adjust_speed",
        "reroute",
        "avoid_obstacle",
        "none",
    ] = "land",
    recovery_path_taken: str | None = "MAV_CMD_NAV_LAND",
    final_landing_safe: bool = False,
    recovery_agent_evidence_window: Mapping[str, Any] | None = None,
    recovery_agent_evidence_window_path: str | None = None,
    payload_release_command_frame_sent: bool = False,
    payload_release_command_ack_observed: bool = False,
    payload_release_command_ack_result: int | None = None,
    probe_stop_reason_override: str | None = None,
    auto_mission_started_at: str | None = None,
    auto_mission_completed_at: str | None = None,
    abort_retry_count: int = 0,
    min_progress_m: float = DEFAULT_AUTO_RUNTIME_MIN_PROGRESS_M,
    no_progress_grace_seconds: float = DEFAULT_AUTO_RUNTIME_NO_PROGRESS_GRACE_SECONDS,
    min_route_altitude_m: float = DEFAULT_AUTO_RUNTIME_MIN_ROUTE_ALTITUDE_M,
    altitude_grace_seconds: float = DEFAULT_AUTO_RUNTIME_ALTITUDE_GRACE_SECONDS,
    min_battery_remaining_percent: float = (
        DEFAULT_AUTO_RUNTIME_BATTERY_MIN_REMAINING_PERCENT
    ),
) -> MissionOSAutoMissionRuntimeMonitorSummary:
    parsed_samples = tuple(
        sample
        if isinstance(sample, MissionOSAutoMissionTelemetrySample)
        else MissionOSAutoMissionTelemetrySample.model_validate(sample)
        for sample in samples
    )
    elapsed = (
        float(monitor_elapsed_seconds)
        if monitor_elapsed_seconds is not None
        else (
            max((sample.elapsed_seconds for sample in parsed_samples), default=0.0)
            if parsed_samples
            else 0.0
        )
    )
    nav_state_samples = tuple(
        int(sample.nav_state)
        for sample in parsed_samples
        if sample.nav_state is not None
    )
    mission_current_samples = tuple(
        int(sample.mission_current_seq)
        for sample in parsed_samples
        if sample.mission_current_seq is not None
    )
    reached_events = _unique_int_tuple(
        int(sample.mission_reached_seq)
        for sample in parsed_samples
        if sample.mission_reached_seq is not None and sample.mission_reached_seq >= 0
    )
    auto_nav_observed = PX4_NAVIGATION_STATE_AUTO_MISSION in nav_state_samples
    auto_started = (
        bool(mission_upload_accepted)
        and mission_ack_result == MAV_MISSION_ACCEPTED
        and arm_command_ack_result == MAV_RESULT_ACCEPTED
        and auto_mission_mode_ack_result == MAV_RESULT_ACCEPTED
        and auto_nav_observed
    )
    progress_m = _observed_local_xy_progress_m(parsed_samples)
    terminal_pose = _terminal_local_ned_pose(parsed_samples)
    route_waypoint_seq_start = 1 if compilation.generated_route_waypoint_count > 0 else 0
    route_waypoint_seq_end = compilation.generated_route_waypoint_count
    dropoff_dwell_mission_seq = compilation.dropoff_dwell_mission_seq
    land_mission_seq = (
        compilation.land_mission_seq
        if compilation.land_mission_seq is not None
        else len(compilation.mission_items) - 1
    )
    guard_failures: list[str] = []

    telemetry_stale_status = "ok"
    if not parsed_samples or any(sample.telemetry_stale for sample in parsed_samples):
        telemetry_stale_status = "blocked"
        guard_failures.append("auto_mission_telemetry_stale")

    mode_loss_status = "ok"
    if auto_nav_observed and any(
        sample.nav_state is not None
        and sample.nav_state != PX4_NAVIGATION_STATE_AUTO_MISSION
        for sample in parsed_samples
    ):
        mode_loss_status = "blocked"
        guard_failures.append("auto_mission_mode_lost")
    elif not auto_nav_observed:
        mode_loss_status = "blocked"
        guard_failures.append("auto_mission_nav_state_not_observed")

    battery_guard_status = "ok"
    battery_samples = [
        sample for sample in parsed_samples if sample.battery_status_observed
    ]
    if battery_samples:
        latest_battery = battery_samples[-1]
        if (
            latest_battery.battery_warning is not None
            and latest_battery.battery_warning > 0
        ):
            battery_guard_status = "blocked"
            guard_failures.append("auto_mission_battery_warning")
        if (
            latest_battery.battery_remaining_percent is not None
            and latest_battery.battery_remaining_percent
            < min_battery_remaining_percent
        ):
            battery_guard_status = "blocked"
            guard_failures.append("auto_mission_battery_reserve_low")
    else:
        battery_guard_status = "unknown"

    altitude_samples = [
        sample
        for sample in parsed_samples
        if sample.elapsed_seconds >= altitude_grace_seconds
        and sample.altitude_above_home_m is not None
        and _is_route_altitude_guard_sample(
            sample,
            route_waypoint_seq_end=route_waypoint_seq_end,
        )
    ]
    if altitude_samples:
        min_observed_altitude = min(
            sample.altitude_above_home_m or 0.0 for sample in altitude_samples
        )
        if min_observed_altitude < min_route_altitude_m:
            altitude_envelope_status = "blocked"
            guard_failures.append("auto_mission_altitude_below_min")
        else:
            altitude_envelope_status = "ok"
    else:
        altitude_envelope_status = "not_evaluated_takeoff_grace"

    route_progress_samples = tuple(
        sample
        for sample in parsed_samples
        if sample.mission_current_seq is not None
        and route_waypoint_seq_start <= sample.mission_current_seq <= route_waypoint_seq_end
        and sample.altitude_above_home_m is not None
        and sample.altitude_above_home_m >= min_route_altitude_m
    )
    route_progress_guard_elapsed = (
        (elapsed - route_progress_samples[0].elapsed_seconds)
        if route_progress_samples
        else 0.0
    )
    if (
        route_progress_samples
        and route_progress_guard_elapsed >= no_progress_grace_seconds
        and progress_m < min_progress_m
    ):
        no_progress_status = "blocked"
        guard_failures.append("auto_mission_no_progress")
    else:
        no_progress_status = "ok"

    # Phase 4 owns waypoint envelope verification; Phase 3 only records data.
    geofence_status = "not_evaluated_phase4_pending"

    blocked_reasons: list[str] = []
    if not mission_upload_accepted or mission_ack_result != MAV_MISSION_ACCEPTED:
        blocked_reasons.append("mission_upload_not_accepted")
    if arm_command_ack_result != MAV_RESULT_ACCEPTED:
        blocked_reasons.append("arm_ack_not_accepted")
    if auto_mission_mode_ack_result != MAV_RESULT_ACCEPTED:
        blocked_reasons.append("auto_mission_mode_ack_not_accepted")
    blocked_reasons.extend(guard_failures)
    payload_release_command_acked = (
        bool(payload_release_command_frame_sent)
        and bool(payload_release_command_ack_observed)
        and payload_release_command_ack_result == MAV_RESULT_ACCEPTED
    )
    if payload_release_command_frame_sent and not payload_release_command_acked:
        if not payload_release_command_ack_observed:
            blocked_reasons.append("payload_release_command_ack_timeout")
        else:
            blocked_reasons.append(
                f"payload_release_command_ack_result_{payload_release_command_ack_result}"
            )
    blocked_reasons.extend(
        (
            "route_completion_gate_phase4_pending",
            "dropoff_verification_phase5_pending",
            "delivery_claim_gate_phase6_pending",
        )
    )
    recovery_window = dict(recovery_agent_evidence_window or {})
    recovery_snapshot = recovery_window.get("telemetry_snapshot")
    recovery_snapshot = (
        dict(recovery_snapshot) if isinstance(recovery_snapshot, Mapping) else None
    )
    recovery_return_started = bool(recovery_window.get("recovery_return_started"))
    recovery_return_progress_m = max(
        0.0,
        float(recovery_window.get("recovery_return_progress_m") or 0.0),
    )
    recovery_telemetry_stale = bool(recovery_window.get("recovery_telemetry_stale"))
    recovery_heartbeat_observed_count = int(
        recovery_window.get("recovery_heartbeat_observed_count") or 0
    )
    recovery_latest_heartbeat_observed = recovery_window.get(
        "recovery_latest_heartbeat_observed"
    )
    recovery_observation_lost = bool(
        recovery_window.get("recovery_observation_lost")
        or (recovery_telemetry_stale and not final_landing_safe)
    )
    recovery_observation_loss_classification = str(
        recovery_window.get("recovery_observation_loss_classification") or ""
    ).strip()
    if not recovery_observation_loss_classification:
        if not recovery_observation_lost:
            recovery_observation_loss_classification = "none"
        elif recovery_latest_heartbeat_observed is True:
            recovery_observation_loss_classification = "topic_stale_heartbeat_alive"
        elif recovery_latest_heartbeat_observed is False:
            recovery_observation_loss_classification = "topic_stale_heartbeat_missing"
        else:
            recovery_observation_loss_classification = "topic_stale_heartbeat_unknown"
    recovery_incomplete_reason = (
        str(recovery_window.get("recovery_incomplete_reason") or "").strip() or None
    )
    if not final_landing_safe:
        if recovery_observation_lost:
            blocked_reasons.append("recovery_observation_lost_before_safe_landing")
        elif recovery_incomplete_reason:
            blocked_reasons.append(recovery_incomplete_reason)
        else:
            blocked_reasons.append("recovery_final_landing_not_observed")
    runtime_status = (
        MissionOSAutoMissionRuntimeStatus.GUARD_ABORTED
        if guard_failures
        else (
            MissionOSAutoMissionRuntimeStatus.MONITOR_WINDOW_COMPLETED
            if auto_started
            else MissionOSAutoMissionRuntimeStatus.BLOCKED
        )
    )
    route_waypoint_events = tuple(
        seq
        for seq in reached_events
        if route_waypoint_seq_start <= seq <= route_waypoint_seq_end
    )
    last_route_waypoint_seq = route_waypoint_events[-1] if route_waypoint_events else None
    last_route_waypoint_index = (
        last_route_waypoint_seq - route_waypoint_seq_start + 1
        if last_route_waypoint_seq is not None
        else None
    )
    route_waypoint_fraction = (
        len(route_waypoint_events) / compilation.generated_route_waypoint_count
        if compilation.generated_route_waypoint_count
        else 0.0
    )
    probe_stop_reason = (
        str(probe_stop_reason_override).strip()
        if probe_stop_reason_override is not None
        and str(probe_stop_reason_override).strip()
        else (
            guard_failures[0]
            if guard_failures
            else AUTO_RUNTIME_PROBE_STOP_REASON_MONITOR_WINDOW_COMPLETE
        )
    )
    return MissionOSAutoMissionRuntimeMonitorSummary(
        operator_coordinate_execution=bool(operator_coordinate_execution),
        mission_upload_accepted=bool(mission_upload_accepted),
        mission_count_sent=len(compilation.mission_items),
        mission_ack_observed=bool(mission_ack_observed),
        mission_ack_result=mission_ack_result,
        arm_command_ack_observed=bool(arm_command_ack_observed),
        arm_command_ack_result=arm_command_ack_result,
        auto_mission_mode_ack_observed=bool(auto_mission_mode_ack_observed),
        auto_mission_mode_ack_result=auto_mission_mode_ack_result,
        auto_mission_started=auto_started,
        auto_mission_started_at=auto_mission_started_at,
        auto_mission_completed_at=auto_mission_completed_at,
        monitor_loop_started=bool(parsed_samples),
        monitor_target_seconds=float(monitor_target_seconds),
        monitor_elapsed_seconds=round(elapsed, 3),
        telemetry_sample_count=len(parsed_samples),
        auto_mission_nav_state_observed=auto_nav_observed,
        auto_mission_nav_state_samples=nav_state_samples,
        heartbeat_samples=int(heartbeat_samples),
        statustext_during_auto=_unique_tuple(
            tuple(str(item) for item in statustext_during_auto)
        )[:20],
        local_ned_pose_samples_path=local_ned_pose_samples_path,
        global_position_samples_path=global_position_samples_path,
        mavlink_event_log_path=mavlink_event_log_path,
        mission_current_samples=mission_current_samples,
        mission_item_reached_events=reached_events,
        last_reached_waypoint_seq=(reached_events[-1] if reached_events else None),
        waypoint_reached_count=len(set(reached_events)),
        route_waypoint_seq_start=route_waypoint_seq_start,
        route_waypoint_seq_end=route_waypoint_seq_end,
        dropoff_dwell_mission_seq=dropoff_dwell_mission_seq,
        land_mission_seq=land_mission_seq,
        route_waypoint_reached_count=len(route_waypoint_events),
        last_reached_route_waypoint_index=last_route_waypoint_index,
        route_waypoint_reached_fraction=round(route_waypoint_fraction, 6),
        planned_route_m=compilation.planned_route_m,
        planned_waypoint_count=compilation.planned_waypoint_count,
        generated_route_waypoint_count=compilation.generated_route_waypoint_count,
        expected_duration_seconds=compilation.expected_duration_seconds,
        timeout_seconds=compilation.timeout_seconds,
        observed_progress_m=round(progress_m, 3),
        route_terminal_local_ned_pose=terminal_pose,
        battery_guard_status=battery_guard_status,
        altitude_envelope_status=altitude_envelope_status,
        geofence_status=geofence_status,
        no_progress_status=no_progress_status,
        telemetry_stale_status=telemetry_stale_status,
        mode_loss_status=mode_loss_status,
        guard_failure_reasons=_unique_tuple(guard_failures),
        abort_policy_selected_action=abort_policy_selected_action,
        abort_reason=probe_stop_reason,
        probe_stop_reason=probe_stop_reason,
        guard_abort_requested=bool(guard_failures),
        abort_retry_count=int(abort_retry_count),
        recovery_path_taken=recovery_path_taken,
        recovery_command_ack_observed=bool(abort_ack_observed),
        recovery_command_ack_result=abort_ack_result,
        final_landing_safe=bool(final_landing_safe),
        recovery_agent_evidence_window_path=recovery_agent_evidence_window_path,
        recovery_agent_telemetry_snapshot=recovery_snapshot,
        recovery_return_started=recovery_return_started,
        recovery_return_progress_m=round(recovery_return_progress_m, 3),
        recovery_distance_to_home_start_m=(
            float(recovery_window["recovery_distance_to_home_start_m"])
            if recovery_window.get("recovery_distance_to_home_start_m") is not None
            else None
        ),
        recovery_distance_to_home_end_m=(
            float(recovery_window["recovery_distance_to_home_end_m"])
            if recovery_window.get("recovery_distance_to_home_end_m") is not None
            else None
        ),
        recovery_distance_to_home_min_m=(
            float(recovery_window["recovery_distance_to_home_min_m"])
            if recovery_window.get("recovery_distance_to_home_min_m") is not None
            else None
        ),
        recovery_distance_to_home_closing_steps=int(
            recovery_window.get("recovery_distance_to_home_closing_steps") or 0
        ),
        recovery_distance_to_home_opening_steps=int(
            recovery_window.get("recovery_distance_to_home_opening_steps") or 0
        ),
        recovery_telemetry_stale=recovery_telemetry_stale,
        recovery_telemetry_stale_after_sample=(
            int(recovery_window["recovery_telemetry_stale_after_sample"])
            if recovery_window.get("recovery_telemetry_stale_after_sample")
            is not None
            else None
        ),
        recovery_heartbeat_observed_count=recovery_heartbeat_observed_count,
        recovery_latest_heartbeat_observed=(
            bool(recovery_latest_heartbeat_observed)
            if recovery_latest_heartbeat_observed is not None
            else None
        ),
        recovery_observation_lost=recovery_observation_lost,
        recovery_observation_loss_classification=(
            recovery_observation_loss_classification
        ),
        recovery_observation_lost_after_sample=(
            int(recovery_window["recovery_observation_lost_after_sample"])
            if recovery_window.get("recovery_observation_lost_after_sample")
            is not None
            else (
                int(recovery_window["recovery_telemetry_stale_after_sample"])
                if recovery_observation_lost
                and recovery_window.get("recovery_telemetry_stale_after_sample")
                is not None
                else None
            )
        ),
        recovery_incomplete_reason=recovery_incomplete_reason,
        runtime_status=runtime_status,
        payload_release_command_frame_sent=bool(payload_release_command_frame_sent),
        payload_release_command_ack_observed=bool(
            payload_release_command_ack_observed
        ),
        payload_release_command_ack_result=payload_release_command_ack_result,
        payload_release_command_ack_result_name=_mav_result_name(
            payload_release_command_ack_result
        ),
        payload_release_command_acked=payload_release_command_acked,
        blocked_reasons=_unique_tuple(blocked_reasons),
    )


def build_auto_mission_waypoint_gate_summary(
    *,
    route_waypoint_seq_start: int,
    route_waypoint_seq_end: int,
    mission_item_reached_events: Sequence[int],
) -> MissionOSAutoMissionWaypointGateSummary:
    start = int(route_waypoint_seq_start)
    end = int(route_waypoint_seq_end)
    expected = tuple(range(start, end + 1)) if end >= start and start > 0 else ()
    reached_events = _unique_int_tuple(mission_item_reached_events)
    reached_set = set(reached_events)
    reached_route = tuple(seq for seq in expected if seq in reached_set)
    missing_route = tuple(seq for seq in expected if seq not in reached_set)
    expected_count = len(expected)
    reached_fraction = (
        len(reached_route) / expected_count if expected_count > 0 else 0.0
    )
    all_reached = bool(expected) and not missing_route
    blocked_reasons = () if all_reached else ("missing_route_waypoint_sequences",)
    return MissionOSAutoMissionWaypointGateSummary(
        route_waypoint_seq_start=start,
        route_waypoint_seq_end=end,
        expected_route_waypoint_sequences=expected,
        mission_item_reached_events=reached_events,
        reached_route_waypoint_sequences=reached_route,
        missing_route_waypoint_sequences=missing_route,
        route_waypoint_reached_count=len(reached_route),
        expected_route_waypoint_count=expected_count,
        route_waypoint_reached_fraction=round(reached_fraction, 6),
        all_waypoints_reached=all_reached,
        route_completed_claimed=all_reached,
        blocked_reasons=blocked_reasons,
    )


def build_auto_mission_waypoint_gate_summary_from_runtime(
    summary: MissionOSAutoMissionRuntimeMonitorSummary | Mapping[str, Any],
) -> MissionOSAutoMissionWaypointGateSummary:
    parsed = (
        summary
        if isinstance(summary, MissionOSAutoMissionRuntimeMonitorSummary)
        else MissionOSAutoMissionRuntimeMonitorSummary.model_validate(summary)
    )
    return build_auto_mission_waypoint_gate_summary(
        route_waypoint_seq_start=parsed.route_waypoint_seq_start,
        route_waypoint_seq_end=parsed.route_waypoint_seq_end,
        mission_item_reached_events=parsed.mission_item_reached_events,
    )


def build_auto_mission_dropoff_gate_summary(
    *,
    dropoff_latitude_deg: float,
    dropoff_longitude_deg: float,
    release_altitude_target_m: float,
    samples: Sequence[MissionOSAutoMissionTelemetrySample | Mapping[str, Any]],
    route_completed_claimed: bool,
    release_radius_m: float = DEFAULT_DROPOFF_RELEASE_RADIUS_M,
    release_altitude_tolerance_m: float = DEFAULT_DROPOFF_RELEASE_ALTITUDE_TOLERANCE_M,
    required_dwell_seconds: float = DEFAULT_DROPOFF_DWELL_SECONDS,
) -> MissionOSAutoMissionDropoffGateSummary:
    parsed_samples = tuple(
        sample
        if isinstance(sample, MissionOSAutoMissionTelemetrySample)
        else MissionOSAutoMissionTelemetrySample.model_validate(sample)
        for sample in samples
    )
    altitude_min = max(0.0, release_altitude_target_m - release_altitude_tolerance_m)
    altitude_max = release_altitude_target_m + release_altitude_tolerance_m
    residuals: list[tuple[int, float]] = []
    qualifying: list[MissionOSAutoMissionTelemetrySample] = []
    for sample in parsed_samples:
        if (
            sample.global_latitude_deg is None
            or sample.global_longitude_deg is None
        ):
            continue
        residual_m = _haversine_distance_m(
            latitude_a=float(sample.global_latitude_deg),
            longitude_a=float(sample.global_longitude_deg),
            latitude_b=float(dropoff_latitude_deg),
            longitude_b=float(dropoff_longitude_deg),
        )
        residuals.append((sample.sample_index, residual_m))
        altitude_m = sample.altitude_above_home_m
        if (
            residual_m <= release_radius_m
            and altitude_m is not None
            and altitude_min <= altitude_m <= altitude_max
        ):
            qualifying.append(sample)

    min_residual = min((value for _index, value in residuals), default=None)
    residual_ok = min_residual is not None and min_residual <= release_radius_m
    altitude_ok = bool(qualifying)
    max_dwell_seconds = 0.0
    best_indices: tuple[int, ...] = ()
    current: list[MissionOSAutoMissionTelemetrySample] = []
    previous_elapsed: float | None = None
    for sample in qualifying:
        if previous_elapsed is None or sample.elapsed_seconds - previous_elapsed <= 2.5:
            current.append(sample)
        else:
            if current:
                dwell_seconds = current[-1].elapsed_seconds - current[0].elapsed_seconds
                if dwell_seconds > max_dwell_seconds:
                    max_dwell_seconds = dwell_seconds
                    best_indices = tuple(item.sample_index for item in current)
            current = [sample]
        previous_elapsed = sample.elapsed_seconds
    if current:
        dwell_seconds = current[-1].elapsed_seconds - current[0].elapsed_seconds
        if dwell_seconds > max_dwell_seconds:
            max_dwell_seconds = dwell_seconds
            best_indices = tuple(item.sample_index for item in current)

    dwell_ok = max_dwell_seconds >= required_dwell_seconds
    dropoff_verified = (
        bool(route_completed_claimed)
        and bool(residual_ok)
        and bool(altitude_ok)
        and bool(dwell_ok)
    )
    blocked_reasons: list[str] = []
    if not route_completed_claimed:
        blocked_reasons.append("route_completion_not_claimed")
    if not residual_ok:
        blocked_reasons.append("dropoff_residual_xy_outside_release_radius")
    if not altitude_ok:
        blocked_reasons.append("dropoff_release_altitude_outside_band")
    if not dwell_ok:
        blocked_reasons.append("dropoff_dwell_time_insufficient")

    return MissionOSAutoMissionDropoffGateSummary(
        dropoff_latitude_deg=round(float(dropoff_latitude_deg), 7),
        dropoff_longitude_deg=round(float(dropoff_longitude_deg), 7),
        release_radius_m=float(release_radius_m),
        release_altitude_target_m=float(release_altitude_target_m),
        release_altitude_min_m=round(altitude_min, 3),
        release_altitude_max_m=round(altitude_max, 3),
        required_dwell_seconds=float(required_dwell_seconds),
        observed_min_residual_xy_m=(
            round(min_residual, 3) if min_residual is not None else None
        ),
        observed_dwell_seconds=round(max_dwell_seconds, 3),
        qualifying_sample_indices=best_indices,
        residual_xy_ok=bool(residual_ok),
        altitude_ok=bool(altitude_ok),
        dwell_ok=bool(dwell_ok),
        route_completed_claimed=bool(route_completed_claimed),
        dropoff_verified=dropoff_verified,
        blocked_reasons=_unique_tuple(blocked_reasons),
    )


def build_auto_mission_sitl_delivery_gate_summary(
    *,
    route_completed_claimed: bool,
    dropoff_verified: bool,
    payload_release_command_acked: bool,
) -> MissionOSAutoMissionSITLDeliveryGateSummary:
    sitl_claimed = (
        bool(route_completed_claimed)
        and bool(dropoff_verified)
        and bool(payload_release_command_acked)
    )
    blocked_reasons: list[str] = []
    if not route_completed_claimed:
        blocked_reasons.append("route_completion_not_claimed")
    if not dropoff_verified:
        blocked_reasons.append("dropoff_not_verified")
    if not payload_release_command_acked:
        blocked_reasons.append("payload_release_command_not_acked")
    return MissionOSAutoMissionSITLDeliveryGateSummary(
        route_completed_claimed=bool(route_completed_claimed),
        dropoff_verified=bool(dropoff_verified),
        payload_release_command_acked=bool(payload_release_command_acked),
        sitl_delivery_claimed=sitl_claimed,
        blocked_reasons=_unique_tuple(blocked_reasons),
    )


def build_auto_mission_payload_release_sim_gate_summary(
    *,
    route_completed_claimed: bool,
    dropoff_verified: bool,
    payload_release_command_acked: bool,
    payload_release_event: Mapping[str, Any] | None,
) -> MissionOSAutoMissionPayloadReleaseSimGateSummary:
    event = dict(payload_release_event or {})
    event_source = str(event.get("payload_release_event_source") or "").strip() or None
    performed = bool(event.get("gazebo_detachable_joint_release_performed"))
    observed = bool(event.get("gazebo_detachable_joint_release_observed"))
    observed_at = str(event.get("payload_release_observed_at") or "").strip() or None
    payload_release_observed_sim = (
        bool(route_completed_claimed)
        and bool(dropoff_verified)
        and bool(payload_release_command_acked)
        and event_source == "gazebo_detachable_joint_detach_event"
        and performed
        and observed
        and observed_at is not None
    )
    blocked_reasons: list[str] = []
    if not route_completed_claimed:
        blocked_reasons.append("route_completion_not_claimed")
    if not dropoff_verified:
        blocked_reasons.append("dropoff_not_verified")
    if not payload_release_command_acked:
        blocked_reasons.append("payload_release_command_not_acked")
    if not payload_release_observed_sim:
        if event_source != "gazebo_detachable_joint_detach_event":
            blocked_reasons.append("payload_release_sim_event_source_not_observed")
        elif not performed:
            blocked_reasons.append("gazebo_detachable_joint_release_not_performed")
        elif not observed:
            blocked_reasons.append("gazebo_detachable_joint_release_not_observed")
        elif observed_at is None:
            blocked_reasons.append("payload_release_sim_observed_at_missing")

    return MissionOSAutoMissionPayloadReleaseSimGateSummary(
        route_completed_claimed=bool(route_completed_claimed),
        dropoff_verified=bool(dropoff_verified),
        payload_release_command_acked=bool(payload_release_command_acked),
        payload_release_observed_sim=payload_release_observed_sim,
        payload_release_event_source=event_source,
        payload_release_observed_at=observed_at,
        gazebo_detachable_joint_release_performed=performed,
        gazebo_detachable_joint_release_observed=observed,
        blocked_reasons=_unique_tuple(blocked_reasons),
    )


def encode_auto_mission_command_long(
    *,
    command_id: int,
    params: Sequence[float],
    sequence: int,
    target_system: int = DEFAULT_PX4_TARGET_SYSTEM,
    target_component: int = DEFAULT_PX4_TARGET_COMPONENT,
    system_id: int = DEFAULT_MAVLINK_GCS_SYSTEM_ID,
    component_id: int = DEFAULT_MAVLINK_GCS_COMPONENT_ID,
) -> bytes:
    if len(tuple(params)) != 7:
        raise MissionOSAutoMissionRunnerError("COMMAND_LONG requires seven params")
    payload = struct.pack(
        "<fffffffHBBB",
        *[float(item) for item in params],
        int(command_id),
        int(target_system),
        int(target_component),
        0,
    )
    return encode_mavlink2_frame(
        msg_id=MAVLINK_MSG_ID_COMMAND_LONG,
        payload=payload,
        sequence=int(sequence),
        system_id=int(system_id),
        component_id=int(component_id),
    )


def _auto_mission_mode_params() -> tuple[float, float, float, float, float, float, float]:
    return (
        float(MAV_MODE_FLAG_CUSTOM_MODE_ENABLED),
        float(PX4_CUSTOM_MAIN_MODE_AUTO),
        float(PX4_CUSTOM_SUB_MODE_AUTO_MISSION),
        0.0,
        0.0,
        0.0,
        0.0,
    )


def _land_abort_params() -> tuple[float, float, float, float, float, float, float]:
    return (0.0, 0.0, 0.0, 0.0, math.nan, math.nan, 0.0)


def _payload_release_params(
    gripper_id: float = DEFAULT_PAYLOAD_RELEASE_GRIPPER_ID,
) -> tuple[float, float, float, float, float, float, float]:
    return (
        float(gripper_id),
        float(MAV_GRIPPER_ACTION_RELEASE),
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    )


def _mav_result_name(result_code: int | None) -> str | None:
    result_names = {
        0: "ACCEPTED",
        1: "TEMPORARILY_REJECTED",
        2: "DENIED",
        3: "UNSUPPORTED",
        4: "FAILED",
        5: "IN_PROGRESS",
        6: "CANCELLED",
    }
    if result_code is None:
        return None
    return result_names.get(int(result_code), "UNKNOWN")


def _wait_command_ack(
    *,
    sock: socket.socket,
    command_id: int,
    deadline: float,
) -> _CommandAck:
    in_progress_ack: _CommandAck | None = None
    while time.monotonic() < deadline:
        sock.settimeout(_remaining(deadline))
        try:
            data, _addr = sock.recvfrom(2048)
        except socket.timeout:
            return _CommandAck(observed=False)
        try:
            decoded = decode_mavlink2_frame(data)
        except PX4RealMAVLinkTransportError:
            continue
        if decoded["msg_id"] != MAVLINK_MSG_ID_COMMAND_ACK:
            continue
        payload = decoded["payload"]
        if len(payload) < 10:
            continue
        ack_command_id, result_code, _progress, _param2, _target_system, _target_component = struct.unpack(
            "<HBBiBB", payload[:10]
        )
        if int(ack_command_id) != int(command_id):
            continue
        ack = _CommandAck(
            observed=True,
            result_code=int(result_code),
            result_name=_mav_result_name(int(result_code)),
        )
        if int(result_code) == MAV_RESULT_IN_PROGRESS:
            in_progress_ack = ack
            continue
        return ack
    if in_progress_ack is not None:
        return in_progress_ack
    return _CommandAck(observed=False)


def _mode_transition_summary(
    upload_summary: MissionOSAutoMissionUploadSummary,
    *,
    status: MissionOSAutoMissionModeTransitionStatus,
    trace: _ModeTransitionTrace,
    blocked_reasons: Sequence[str],
    timeout_seconds: float,
) -> MissionOSAutoMissionModeTransitionSummary:
    return MissionOSAutoMissionModeTransitionSummary(
        mode_transition_status=status,
        mission_upload_accepted=(
            upload_summary.auto_mission_upload_status
            is MissionOSAutoMissionUploadStatus.UPLOADED
        ),
        mission_count_sent=upload_summary.mission_count_sent,
        mission_ack_observed=upload_summary.mission_ack_observed,
        mission_ack_result=upload_summary.mission_ack_result,
        heartbeats_sent_before_commands=trace.heartbeats_sent,
        arm_command_frame_sent=trace.arm_command_frame_sent,
        arm_command_ack_observed=trace.arm_command_ack.observed,
        arm_command_ack_result=trace.arm_command_ack.result_code,
        arm_command_ack_result_name=trace.arm_command_ack.result_name,
        auto_mission_mode_command_frame_sent=(
            trace.auto_mission_mode_command_frame_sent
        ),
        auto_mission_mode_ack_observed=(
            trace.auto_mission_mode_command_ack.observed
        ),
        auto_mission_mode_ack_result=(
            trace.auto_mission_mode_command_ack.result_code
        ),
        auto_mission_mode_ack_result_name=(
            trace.auto_mission_mode_command_ack.result_name
        ),
        auto_mission_abort_command_frame_sent=(
            trace.auto_mission_abort_command_frame_sent
        ),
        auto_mission_abort_ack_observed=(
            trace.auto_mission_abort_command_ack.observed
        ),
        auto_mission_abort_ack_result=(
            trace.auto_mission_abort_command_ack.result_code
        ),
        auto_mission_abort_ack_result_name=(
            trace.auto_mission_abort_command_ack.result_name
        ),
        timeout_seconds=timeout_seconds,
        blocked_reasons=tuple(str(item) for item in blocked_reasons if str(item)),
    )


def upload_auto_mission_to_loopback_peer(
    compilation: MissionOSAutoMissionCompilation,
    *,
    target_endpoint: str,
    timeout_seconds: float | None = None,
    uploader: MissionOSAutoMissionLoopbackUploader | None = None,
) -> MissionOSAutoMissionUploadSummary:
    resolved_timeout = float(timeout_seconds or compilation.timeout_seconds)
    try:
        request_sequences, ack_type = (
            uploader or MissionOSAutoMissionLoopbackUploader()
        ).upload(
            items=compilation.mission_items,
            target_endpoint=target_endpoint,
            timeout_seconds=resolved_timeout,
        )
    except socket.timeout:
        return _upload_summary(
            compilation,
            status=MissionOSAutoMissionUploadStatus.TIMEOUT,
            request_sequences=(),
            ack_type=None,
            blocked_reasons=("mission_upload_timeout",),
            timeout_seconds=resolved_timeout,
        )
    except (OSError, MissionOSAutoMissionRunnerError) as exc:
        return _upload_summary(
            compilation,
            status=MissionOSAutoMissionUploadStatus.BLOCKED,
            request_sequences=(),
            ack_type=None,
            blocked_reasons=(str(exc),),
            timeout_seconds=resolved_timeout,
        )

    if ack_type != MAV_MISSION_ACCEPTED:
        return _upload_summary(
            compilation,
            status=MissionOSAutoMissionUploadStatus.BLOCKED,
            request_sequences=request_sequences,
            ack_type=ack_type,
            blocked_reasons=(f"mission_ack_type_{ack_type}",),
            timeout_seconds=resolved_timeout,
        )
    return _upload_summary(
        compilation,
        status=MissionOSAutoMissionUploadStatus.UPLOADED,
        request_sequences=request_sequences,
        ack_type=ack_type,
        blocked_reasons=(),
        timeout_seconds=resolved_timeout,
    )


def _upload_summary(
    compilation: MissionOSAutoMissionCompilation,
    *,
    status: MissionOSAutoMissionUploadStatus,
    request_sequences: Sequence[int],
    ack_type: int | None,
    blocked_reasons: Sequence[str],
    timeout_seconds: float,
) -> MissionOSAutoMissionUploadSummary:
    request_tuple = tuple(int(item) for item in request_sequences)
    return MissionOSAutoMissionUploadSummary(
        auto_mission_upload_status=status,
        mission_count_sent=len(compilation.mission_items),
        mission_count_expected=len(compilation.mission_items),
        mission_request_int_sequences=request_tuple,
        mission_item_int_sequences_sent=request_tuple,
        mission_ack_observed=ack_type is not None,
        mission_ack_result=ack_type,
        planned_route_m=compilation.planned_route_m,
        planned_waypoint_count=compilation.planned_waypoint_count,
        generated_route_waypoint_count=compilation.generated_route_waypoint_count,
        waypoint_spacing_m=compilation.waypoint_spacing_m,
        cruise_speed_mps=compilation.cruise_speed_mps,
        takeoff_altitude_m=compilation.takeoff_altitude_m,
        cruise_altitude_m=compilation.cruise_altitude_m,
        expected_duration_seconds=compilation.expected_duration_seconds,
        timeout_seconds=timeout_seconds,
        blocked_reasons=tuple(str(item) for item in blocked_reasons if str(item)),
    )


def _observed_local_xy_progress_m(
    samples: Sequence[MissionOSAutoMissionTelemetrySample],
) -> float:
    positioned = [
        sample
        for sample in samples
        if sample.local_x_m is not None and sample.local_y_m is not None
    ]
    if len(positioned) < 2:
        return 0.0
    first = positioned[0]
    last = positioned[-1]
    return math.hypot(
        float(last.local_x_m or 0.0) - float(first.local_x_m or 0.0),
        float(last.local_y_m or 0.0) - float(first.local_y_m or 0.0),
    )


def _terminal_local_ned_pose(
    samples: Sequence[MissionOSAutoMissionTelemetrySample],
) -> dict[str, float | int | None] | None:
    for sample in reversed(tuple(samples)):
        if sample.local_x_m is None or sample.local_y_m is None:
            continue
        return {
            "sample_index": sample.sample_index,
            "elapsed_seconds": round(sample.elapsed_seconds, 3),
            "local_x_m": sample.local_x_m,
            "local_y_m": sample.local_y_m,
            "local_z_m": sample.local_z_m,
            "nav_state": sample.nav_state,
            "mission_current_seq": sample.mission_current_seq,
            "mission_reached_seq": sample.mission_reached_seq,
        }
    return None


def _is_route_altitude_guard_sample(
    sample: MissionOSAutoMissionTelemetrySample,
    *,
    route_waypoint_seq_end: int,
) -> bool:
    if route_waypoint_seq_end <= 0:
        return True
    if (
        sample.mission_reached_seq is not None
        and sample.mission_reached_seq >= route_waypoint_seq_end
    ):
        return False
    if (
        sample.mission_current_seq is not None
        and sample.mission_current_seq > route_waypoint_seq_end
    ):
        return False
    return True


def _unique_tuple(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


def _unique_int_tuple(values: Sequence[int]) -> tuple[int, ...]:
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        resolved = int(value)
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return tuple(result)


def _required_float(route: Mapping[str, Any], key: str) -> float:
    if key not in route:
        raise MissionOSAutoMissionRunnerError(f"operator_route_missing_{key}")
    value = route[key]
    if not isinstance(value, int | float):
        raise MissionOSAutoMissionRunnerError(f"operator_route_malformed_{key}")
    return float(value)


def _optional_positive_int(*values: Any) -> int | None:
    for value in values:
        if value is None:
            continue
        if not isinstance(value, int | float):
            raise MissionOSAutoMissionRunnerError("operator_route_malformed_auto_route_waypoint_count")
        resolved = int(value)
        if float(value) != float(resolved) or resolved <= 0:
            raise MissionOSAutoMissionRunnerError("operator_route_malformed_auto_route_waypoint_count")
        return resolved
    return None


def _optional_float(*values: Any) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _optional_positive_float(*values: Any) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            resolved = float(value)
        except (TypeError, ValueError):
            continue
        if resolved > 0:
            return resolved
    return None


def _terrain_profile_samples(route: Mapping[str, Any]) -> tuple[dict[str, float], ...]:
    raw_profile = (
        route.get("terrain_profile")
        or route.get("terrain_elevation_profile")
        or route.get("terrain_elevation_samples")
    )
    if isinstance(raw_profile, Mapping):
        raw_profile = raw_profile.get("samples") or raw_profile.get("profile")
    if not isinstance(raw_profile, Sequence) or isinstance(raw_profile, (str, bytes)):
        return ()
    samples: list[dict[str, float]] = []
    for raw_sample in raw_profile:
        if not isinstance(raw_sample, Mapping):
            continue
        fraction = _optional_float(
            raw_sample.get("fraction"),
            raw_sample.get("route_fraction"),
        )
        distance_m = _optional_float(
            raw_sample.get("distance_m"),
            raw_sample.get("along_track_m"),
            raw_sample.get("progress_m"),
        )
        elevation_m = _optional_float(
            raw_sample.get("terrain_elevation_m"),
            raw_sample.get("elevation_m"),
            raw_sample.get("amsl_m"),
            raw_sample.get("altitude_m"),
        )
        if elevation_m is None:
            continue
        sample: dict[str, float] = {"terrain_elevation_m": float(elevation_m)}
        if fraction is not None:
            sample["fraction"] = min(1.0, max(0.0, float(fraction)))
        if distance_m is not None:
            sample["distance_m"] = max(0.0, float(distance_m))
        if "fraction" not in sample and "distance_m" not in sample:
            sample["fraction"] = (
                len(samples) / max(1.0, float(len(raw_profile) - 1))
            )
        samples.append(sample)
    if not samples:
        return ()
    return tuple(
        sorted(
            samples,
            key=lambda item: (
                item.get("fraction")
                if item.get("fraction") is not None
                else item.get("distance_m", 0.0)
            ),
        )
    )


def _terrain_profile_source_ref(route: Mapping[str, Any]) -> tuple[str, str]:
    source = str(route.get("terrain_profile_source") or "").strip()
    ref = str(route.get("terrain_profile_ref") or "").strip()
    return source, ref


def _route_source_refs(route: Mapping[str, Any]) -> frozenset[str]:
    raw_refs = route.get("source_refs") or ()
    if isinstance(raw_refs, str):
        raw_refs = (raw_refs,)
    if not isinstance(raw_refs, Sequence):
        return frozenset()
    return frozenset(str(ref).strip() for ref in raw_refs if str(ref).strip())


def _terrain_profile_covers_route_endpoints(
    samples: Sequence[Mapping[str, float]],
    *,
    planned_route_m: float,
) -> bool:
    if len(samples) < 2:
        return False
    start_seen = False
    end_seen = False
    endpoint_distance_tolerance_m = max(5.0, planned_route_m * 0.01)
    for sample in samples:
        fraction = sample.get("fraction")
        distance_m = sample.get("distance_m")
        if fraction is not None:
            start_seen = start_seen or fraction <= 0.01
            end_seen = end_seen or fraction >= 0.99
        if distance_m is not None:
            start_seen = start_seen or distance_m <= endpoint_distance_tolerance_m
            end_seen = end_seen or distance_m >= (
                planned_route_m - endpoint_distance_tolerance_m
            )
    return start_seen and end_seen


def _terrain_profile_execution_blocked_reason(
    route: Mapping[str, Any],
    samples: Sequence[Mapping[str, float]],
    *,
    planned_route_m: float,
) -> str | None:
    source, ref = _terrain_profile_source_ref(route)
    if not samples:
        return "terrain_profile_missing"
    if source not in SOURCE_BOUND_TERRAIN_PROFILE_SOURCES:
        return "terrain_profile_source_not_source_bound"
    if not ref.startswith(TERRAIN_PROFILE_REF_PREFIX):
        return "terrain_profile_ref_missing"
    if ref not in _route_source_refs(route):
        return "terrain_profile_ref_not_in_source_refs"
    if not _terrain_profile_covers_route_endpoints(
        samples,
        planned_route_m=planned_route_m,
    ):
        return "terrain_profile_does_not_cover_route_endpoints"
    return None


def _sample_terrain_elevation_m(
    samples: Sequence[Mapping[str, float]],
    *,
    fraction: float,
    distance_m: float,
    planned_route_m: float,
) -> float | None:
    if not samples:
        return None
    resolved: list[tuple[float, float]] = []
    for sample in samples:
        elevation = _optional_float(sample.get("terrain_elevation_m"))
        if elevation is None:
            continue
        sample_fraction = _optional_float(sample.get("fraction"))
        sample_distance = _optional_float(sample.get("distance_m"))
        if sample_fraction is None:
            if sample_distance is None or planned_route_m <= 0:
                continue
            sample_fraction = sample_distance / planned_route_m
        resolved.append((min(1.0, max(0.0, float(sample_fraction))), elevation))
    if not resolved:
        return None
    resolved.sort(key=lambda item: item[0])
    target = min(1.0, max(0.0, float(fraction)))
    if target <= resolved[0][0]:
        return resolved[0][1]
    if target >= resolved[-1][0]:
        return resolved[-1][1]
    for (left_fraction, left_elevation), (right_fraction, right_elevation) in zip(
        resolved,
        resolved[1:],
        strict=False,
    ):
        if left_fraction <= target <= right_fraction:
            span = right_fraction - left_fraction
            if span <= 0:
                return right_elevation
            t = (target - left_fraction) / span
            return left_elevation + (right_elevation - left_elevation) * t
    return resolved[-1][1]


def _terrain_relative_altitude_m(
    *,
    terrain_elevation_m: float | None,
    takeoff_terrain_elevation_m: float | None,
    clearance_agl_m: float,
    fallback_altitude_m: float,
) -> tuple[float, float | None, bool]:
    if terrain_elevation_m is None or takeoff_terrain_elevation_m is None:
        return fallback_altitude_m, None, False
    relative_altitude_m = (
        float(terrain_elevation_m)
        - float(takeoff_terrain_elevation_m)
        + float(clearance_agl_m)
    )
    return max(fallback_altitude_m, relative_altitude_m), clearance_agl_m, True


def _planned_route_m(
    route: Mapping[str, Any],
    *,
    takeoff_lat: float,
    takeoff_lon: float,
    dropoff_lat: float,
    dropoff_lon: float,
) -> float:
    value = route.get("derived_route_distance_m")
    if isinstance(value, int | float) and float(value) > 0:
        return float(value)
    value = route.get("actual_route_distance_m")
    if isinstance(value, int | float) and float(value) > 0:
        return float(value)
    return _haversine_distance_m(
        latitude_a=takeoff_lat,
        longitude_a=takeoff_lon,
        latitude_b=dropoff_lat,
        longitude_b=dropoff_lon,
    )


def _haversine_distance_m(
    *, latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float
) -> float:
    radius_m = 6_371_000.0
    phi_a = math.radians(latitude_a)
    phi_b = math.radians(latitude_b)
    delta_phi = math.radians(latitude_b - latitude_a)
    delta_lambda = math.radians(longitude_b - longitude_a)
    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2.0) ** 2
    )
    return radius_m * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _loopback_udp_host_port(endpoint: str) -> tuple[str, int]:
    parsed = urlparse(endpoint)
    if parsed.scheme != "udp":
        raise MissionOSAutoMissionRunnerError("target_endpoint_not_udp")
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise MissionOSAutoMissionRunnerError("target_endpoint_not_loopback")
    port = int(parsed.port or 0)
    if not 1 <= port <= 65535:
        raise MissionOSAutoMissionRunnerError("target_endpoint_port_invalid")
    return "127.0.0.1", port


def _deadline(timeout_seconds: float) -> float:
    return time.monotonic() + max(0.01, float(timeout_seconds))


def _remaining(deadline: float) -> float:
    return max(0.01, deadline - time.monotonic())


__all__ = [
    "MISSIONOS_AUTO_MISSION_COMPILATION_SCHEMA_VERSION",
    "MISSIONOS_AUTO_MISSION_MODE_TRANSITION_SUMMARY_SCHEMA_VERSION",
    "MISSIONOS_AUTO_MISSION_PAYLOAD_RELEASE_L0_SUMMARY_SCHEMA_VERSION",
    "MISSIONOS_AUTO_MISSION_PAYLOAD_RELEASE_SIM_GATE_SUMMARY_SCHEMA_VERSION",
    "MISSIONOS_AUTO_MISSION_PHASE3B_LIVE_BOUNDARY_SUMMARY_SCHEMA_VERSION",
    "MISSIONOS_AUTO_MISSION_DROPOFF_GATE_SUMMARY_SCHEMA_VERSION",
    "MISSIONOS_AUTO_MISSION_RUNTIME_MONITOR_SUMMARY_SCHEMA_VERSION",
    "MISSIONOS_AUTO_MISSION_SITL_DELIVERY_GATE_SUMMARY_SCHEMA_VERSION",
    "MISSIONOS_AUTO_MISSION_TELEMETRY_SAMPLE_SCHEMA_VERSION",
    "MISSIONOS_AUTO_MISSION_UPLOAD_SUMMARY_SCHEMA_VERSION",
    "MISSIONOS_AUTO_MISSION_WAYPOINT_GATE_SUMMARY_SCHEMA_VERSION",
    "AUTO_RUNTIME_ABORT_REASON",
    "AUTO_RUNTIME_PROBE_STOP_REASON_MONITOR_WINDOW_COMPLETE",
    "DEFAULT_PAYLOAD_RELEASE_GRIPPER_ID",
    "MAV_CMD_COMPONENT_ARM_DISARM",
    "MAV_CMD_DO_GRIPPER",
    "MAV_CMD_DO_SET_MODE",
    "MAV_CMD_NAV_LOITER_TIME",
    "MAV_GRIPPER_ACTION_RELEASE",
    "MAV_MODE_FLAG_CUSTOM_MODE_ENABLED",
    "PX4_ARMING_STATE_ARMED",
    "MissionOSAutoMissionCompilation",
    "MissionOSAutoMissionDropoffGateSummary",
    "MissionOSAutoMissionLoopbackUploader",
    "MissionOSAutoMissionModeTransitionStatus",
    "MissionOSAutoMissionModeTransitionSummary",
    "MissionOSAutoMissionModeTransitioner",
    "MissionOSAutoMissionPayloadReleaseCommander",
    "MissionOSAutoMissionPayloadReleaseL0Summary",
    "MissionOSAutoMissionPayloadReleaseSimGateSummary",
    "MissionOSAutoMissionPhase3BLiveBoundarySummary",
    "MissionOSAutoMissionRuntimeMonitorSummary",
    "MissionOSAutoMissionRuntimeStatus",
    "MissionOSAutoMissionRunnerError",
    "MissionOSAutoMissionSITLDeliveryGateSummary",
    "MissionOSAutoMissionTelemetrySample",
    "MissionOSAutoMissionUploadStatus",
    "MissionOSAutoMissionUploadSummary",
    "MissionOSAutoMissionWaypointGateSummary",
    "PX4_CUSTOM_MAIN_MODE_AUTO",
    "PX4_CUSTOM_SUB_MODE_AUTO_MISSION",
    "PX4_LANDED_STATE_IN_AIR",
    "PX4_NAVIGATION_STATE_AUTO_MISSION",
    "build_auto_mission_runtime_monitor_summary",
    "build_auto_mission_dropoff_gate_summary",
    "build_auto_mission_payload_release_l0_summary",
    "build_auto_mission_payload_release_sim_gate_summary",
    "build_auto_mission_sitl_delivery_gate_summary",
    "build_auto_mission_waypoint_gate_summary",
    "build_auto_mission_waypoint_gate_summary_from_runtime",
    "compile_operator_coordinate_route_auto_mission",
    "encode_auto_mission_command_long",
    "request_auto_mission_mode_transition_to_loopback_peer",
    "request_auto_mission_payload_release_to_loopback_peer",
    "upload_auto_mission_to_loopback_peer",
]
