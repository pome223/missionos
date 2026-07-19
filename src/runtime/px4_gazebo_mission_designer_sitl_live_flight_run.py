"""Mission Designer live SITL flight-run artifact.

This artifact records the live run boundary that will replace preexisting
horizontal-route summaries in the Mission Designer delivery exit chain. It
requires a Gateway task chain plus a live-run stamped horizontal summary whose
mission items are bound to the Gateway upload receipt.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.agents.model_config import agent_model_label, llm_provider_label
from src.runtime.delivery_mission_contract import DeliveryMissionContract
from src.runtime.px4_gazebo_mission_designer_sitl_runner import (
    PX4GazeboMissionDesignerSITLExecutionResult,
    _validate_horizontal_summary_artifacts,
    attach_px4_gazebo_mission_designer_sitl_dropoff_verification,
    attach_px4_gazebo_mission_designer_sitl_flight_evidence,
    attach_px4_gazebo_mission_designer_sitl_payload_release_observation,
)
from src.runtime.px4_gazebo_mission_designer_sitl_delivery_epic_exit import (
    attach_px4_gazebo_mission_designer_sitl_delivery_epic_exit_result,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    PX4GazeboMissionDesignerSITLExecutionRequest,
)
from src.runtime.px4_gazebo_route_dispatcher import (
    derive_px4_gazebo_route_target_ned,
)
from src.runtime.px4_gazebo_route_plan import PX4GazeboPickupDropoffRoutePlan
from src.runtime.missionos_auto_mission_runner import (
    MissionOSAutoMissionCompilation,
    compile_operator_coordinate_route_auto_mission,
)
from src.runtime.px4_active_runner_recovery_request import (
    queue_px4_active_runner_recovery_request,
)
from src.runtime.px4_gazebo_route.recovery_intent_compiler import (
    verify_runtime_recovery_outcome,
)
from src.runtime.recovery_window_summary import build_recovery_window_summary
from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_WAYPOINT,
    MAV_MISSION_ACCEPTED,
    PX4GazeboSITLMissionItem,
    PX4GazeboSITLMissionUploadReceipt,
    PX4GazeboSITLMissionUploadStatus,
    build_sitl_mission_items_from_contract,
)
from src.runtime.task_store import TaskStore, get_task_store
from src.intelligence.missionos_agent_runtime import (
    MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION,
    guard_runtime_recovery_planner_result,
    plan_runtime_recovery_maneuver,
    run_missionos_runtime_recovery_agent,
)

PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_RUN_SCHEMA_VERSION = (
    "px4_gazebo_mission_designer_sitl_live_flight_run.v1"
)
PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_BLOCKED_RECEIPT_SCHEMA_VERSION = (
    "px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt.v1"
)
PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_FAILED_RECEIPT_SCHEMA_VERSION = (
    "px4_gazebo_mission_designer_sitl_live_flight_failed_receipt.v1"
)
MISSION_DESIGNER_LIVE_SITL_FLIGHT_OPT_IN_ENV = (
    "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_LIVE_FLIGHT"
)
MISSION_DESIGNER_LIVE_SITL_RUN_SOURCE = "gateway_execute_sitl_live_flight"
MISSION_DESIGNER_LIVE_SITL_TARGET_BINDING_SOURCE = (
    "horizontal_route_plan_gateway_receipt_contract"
)
MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_OPT_IN_ENV = (
    "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE"
)
MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_ARTIFACT_ROOT_ENV = (
    "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT"
)
MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_PREUPLOAD_ENV = (
    "PX4_GAZEBO_HORIZONTAL_ROUTE_PREUPLOAD_MISSION"
)
MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_PAYLOAD_RELEASE_MODEL_ENV = (
    "PX4_GAZEBO_HORIZONTAL_ROUTE_PAYLOAD_RELEASE_MODEL"
)
MISSION_DESIGNER_REALISM_WIND_MEAN_MPS_ENV = "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS"
MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG_ENV = (
    "MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG"
)
MISSION_DESIGNER_REALISM_WIND_GUST_MPS_ENV = "MISSION_DESIGNER_REALISM_WIND_GUST_MPS"
MISSION_DESIGNER_REALISM_WIND_VARIANCE_ENV = "MISSION_DESIGNER_REALISM_WIND_VARIANCE"
MISSION_DESIGNER_REALISM_TEMPERATURE_C_ENV = (
    "MISSION_DESIGNER_REALISM_TEMPERATURE_C"
)
MISSION_DESIGNER_REALISM_PRESSURE_HPA_ENV = "MISSION_DESIGNER_REALISM_PRESSURE_HPA"
MISSION_DESIGNER_REALISM_PRECIPITATION_MM_PER_HOUR_ENV = (
    "MISSION_DESIGNER_REALISM_PRECIPITATION_MM_PER_HOUR"
)
MISSION_DESIGNER_REALISM_RAIN_VISUAL_MODE_ENV = (
    "MISSION_DESIGNER_REALISM_RAIN_VISUAL_MODE"
)
MISSION_DESIGNER_REALISM_RAIN_BATTERY_DRAIN_FACTOR_ENV = (
    "MISSION_DESIGNER_REALISM_RAIN_BATTERY_DRAIN_FACTOR"
)
MISSION_DESIGNER_REALISM_RAIN_SENSOR_DEGRADATION_FACTOR_ENV = (
    "MISSION_DESIGNER_REALISM_RAIN_SENSOR_DEGRADATION_FACTOR"
)
MISSION_DESIGNER_REALISM_RAIN_LANDING_RISK_FACTOR_ENV = (
    "MISSION_DESIGNER_REALISM_RAIN_LANDING_RISK_FACTOR"
)
MISSION_DESIGNER_REALISM_THERMAL_BATTERY_DRAIN_FACTOR_ENV = (
    "MISSION_DESIGNER_REALISM_THERMAL_BATTERY_DRAIN_FACTOR"
)
MISSION_DESIGNER_REALISM_THERMAL_MOTOR_DERATE_FACTOR_ENV = (
    "MISSION_DESIGNER_REALISM_THERMAL_MOTOR_DERATE_FACTOR"
)
MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG_ENV = (
    "MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG"
)
MISSION_DESIGNER_REALISM_BATTERY_SCENARIO_ENV = (
    "MISSION_DESIGNER_REALISM_BATTERY_SCENARIO"
)
MISSION_DESIGNER_REALISM_BATTERY_REMAINING_PERCENT_ENV = (
    "MISSION_DESIGNER_REALISM_BATTERY_REMAINING_PERCENT"
)
MISSION_DESIGNER_REALISM_SENSOR_FAILURE_COMPONENT_ENV = (
    "MISSION_DESIGNER_REALISM_SENSOR_FAILURE_COMPONENT"
)
MISSION_DESIGNER_REALISM_SENSOR_FAILURE_TYPE_ENV = (
    "MISSION_DESIGNER_REALISM_SENSOR_FAILURE_TYPE"
)
MISSION_DESIGNER_REALISM_LANDING_ZONE_BLOCKED_ENV = (
    "MISSION_DESIGNER_REALISM_LANDING_ZONE_BLOCKED"
)
MISSION_DESIGNER_REALISM_VISIBILITY_MODE_ENV = (
    "MISSION_DESIGNER_REALISM_VISIBILITY_MODE"
)
MISSION_DESIGNER_REALISM_NO_FLY_ZONE_MARKER_ENV = (
    "MISSION_DESIGNER_REALISM_NO_FLY_ZONE_MARKER"
)
MISSION_DESIGNER_REALISM_TRAFFIC_CONFLICT_MARKER_ENV = (
    "MISSION_DESIGNER_REALISM_TRAFFIC_CONFLICT_MARKER"
)
MISSION_DESIGNER_REALISM_ALTERNATE_LANDING_MARKER_ENV = (
    "MISSION_DESIGNER_REALISM_ALTERNATE_LANDING_MARKER"
)
MISSION_DESIGNER_REALISM_MOVING_ACTOR_MARKER_ENV = (
    "MISSION_DESIGNER_REALISM_MOVING_ACTOR_MARKER"
)
MISSION_DESIGNER_REALISM_MULTI_DRONE_CONFLICT_PROBE_ENV = (
    "MISSION_DESIGNER_REALISM_MULTI_DRONE_CONFLICT_PROBE"
)
MISSION_DESIGNER_REALISM_TELEMETRY_DROPOUT_MODE_ENV = (
    "MISSION_DESIGNER_REALISM_TELEMETRY_DROPOUT_MODE"
)
MISSION_DESIGNER_REALISM_MAVLINK_LINK_DEGRADATION_MODE_ENV = (
    "MISSION_DESIGNER_REALISM_MAVLINK_LINK_DEGRADATION_MODE"
)
MISSION_DESIGNER_LIVE_SITL_TERRAIN_WORLD_SDF_ENV = (
    "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_WORLD_SDF"
)
MISSION_DESIGNER_LIVE_SITL_TERRAIN_WORLD_SHA256_ENV = (
    "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_WORLD_SHA256"
)
MISSION_DESIGNER_LIVE_SITL_TERRAIN_WORLD_SOURCE_REF_ENV = (
    "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_WORLD_SOURCE_REF"
)
MISSION_DESIGNER_LIVE_SITL_TERRAIN_PROVIDER_STATUS_ENV = (
    "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_PROVIDER_STATUS"
)
MISSION_DESIGNER_LIVE_SITL_TERRAIN_SAMPLING_MODE_ENV = (
    "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_SAMPLING_MODE"
)
MISSIONOS_AUTO_TERRAIN_CONTACT_VERIFICATION_ENV = (
    "MISSIONOS_AUTO_TERRAIN_CONTACT_VERIFICATION"
)
MISSIONOS_AUTO_TERRAIN_CLEARANCE_GRACE_M = 1.0
MISSION_DESIGNER_LIVE_SITL_TERRAIN_VERTICAL_REFERENCE_ENV = (
    "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_VERTICAL_REFERENCE"
)
MISSION_DESIGNER_LIVE_SITL_TERRAIN_COLLISION_MODE_ENV = (
    "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_COLLISION_MODE"
)
MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_MODULE = (
    "src.runtime.px4_gazebo_route.entrypoint"
)
MISSIONOS_AUTO_MISSION_GUI_DISPATCH_OPT_IN_ENV = (
    "RUN_MISSIONOS_AUTO_MISSION_GUI_DISPATCH"
)
MISSIONOS_AUTO_MISSION_FULL_RUNTIME_PROBE_OPT_IN_ENV = (
    "RUN_MISSIONOS_AUTO_MISSION_FULL_RUNTIME_PROBE"
)
MISSIONOS_AUTO_MISSION_L1_CARGO_ENV = "MISSIONOS_AUTO_RUNTIME_L1_GAZEBO_CARGO"
MISSIONOS_AUTO_MISSION_OPERATOR_ROUTE_JSON_ENV = (
    "MISSIONOS_AUTO_RUNTIME_OPERATOR_ROUTE_JSON"
)
MISSIONOS_AUTO_MISSION_ARTIFACT_ROOT_ENV = "MISSIONOS_AUTO_RUNTIME_ARTIFACT_ROOT"
MISSIONOS_AUTO_OPERATOR_RECOVERY_REQUEST_PATH_ENV = (
    "MISSIONOS_AUTO_RUNTIME_OPERATOR_RECOVERY_REQUEST_PATH"
)
MISSIONOS_AUTO_MISSION_MONITOR_SECONDS_ENV = (
    "MISSIONOS_AUTO_RUNTIME_MONITOR_SECONDS"
)
# Motor-load battery coupler wiring. The full-runtime probe only injects the
# MotorLoadBatteryCoupler (rotor-effort -> battery discharge) when all three of
# these are set; without them the gz battery signal is absent and the UI falls
# back to PX4's own SITL battery_status estimate. The coupler .so is mounted by
# the probe only when GZ_COUPLER_PLUGIN_SO points at an existing file, so the
# wiring degrades truthfully when the plugin has not been built.
MISSIONOS_AUTO_MISSION_GZ_PHYSICAL_BATTERY_ENV = (
    "MISSIONOS_AUTO_RUNTIME_GZ_PHYSICAL_BATTERY"
)
MISSIONOS_AUTO_MISSION_GZ_BATTERY_MOTOR_COUPLING_ENV = (
    "MISSIONOS_AUTO_RUNTIME_GZ_BATTERY_MOTOR_COUPLING"
)
MISSIONOS_AUTO_MISSION_GZ_COUPLER_PLUGIN_SO_ENV = (
    "MISSIONOS_AUTO_RUNTIME_GZ_COUPLER_PLUGIN_SO"
)
# Canonical build output of the coupler plugin (gitignored; see the plugin
# README for the cmake build). Used as the default .so location when the
# operator has not supplied an explicit override.
MISSIONOS_AUTO_MISSION_GZ_COUPLER_PLUGIN_SO_DEFAULT = (
    "simulators/gazebo/plugins/motor_load_battery_coupler/build/"
    "libMotorLoadBatteryCoupler.so"
)
MISSIONOS_AUTO_MISSION_FULL_RUNTIME_PROBE_SCRIPT = (
    "scripts/smoke_missionos_auto_mission_full_runtime_probe.py"
)
MISSIONOS_RUNTIME_RECOVERY_AGENT_TIMEOUT_SECONDS = 45.0
MISSIONOS_RUNTIME_RECOVERY_AGENT_TIMEOUT_MAX_SECONDS = 90.0
MISSIONOS_RUNTIME_RECOVERY_AGENT_TIMEOUT_SECONDS_ENV = (
    "MISSIONOS_RUNTIME_RECOVERY_AGENT_TIMEOUT_SECONDS"
)
MISSIONOS_RUNTIME_RECOVERY_AGENT_REFRESH_SECONDS = 10.0
MISSIONOS_RUNTIME_RECOVERY_AGENT_REFRESH_SECONDS_ENV = (
    "MISSIONOS_RUNTIME_RECOVERY_AGENT_REFRESH_SECONDS"
)
MISSIONOS_RUNTIME_RECOVERY_AGENT_SOFT_REFRESH_SECONDS = 30.0
MISSIONOS_RUNTIME_RECOVERY_AGENT_SOFT_REFRESH_SECONDS_ENV = (
    "MISSIONOS_RUNTIME_RECOVERY_AGENT_SOFT_REFRESH_SECONDS"
)
MISSIONOS_RUNTIME_RECOVERY_AGENT_WINDOW_SECONDS = 30.0
MISSIONOS_RUNTIME_RECOVERY_AGENT_WINDOW_SECONDS_ENV = (
    "MISSIONOS_RUNTIME_RECOVERY_AGENT_WINDOW_SECONDS"
)
MISSIONOS_RUNTIME_RECOVERY_AGENT_BUCKET_SECONDS = 5.0
MISSIONOS_RUNTIME_RECOVERY_AGENT_BUCKET_SECONDS_ENV = (
    "MISSIONOS_RUNTIME_RECOVERY_AGENT_BUCKET_SECONDS"
)
MISSIONOS_RUNTIME_RECOVERY_PROPOSAL_MAX_AGE_SECONDS = 60.0
MISSIONOS_RUNTIME_RECOVERY_PROPOSAL_MAX_AGE_SECONDS_ENV = (
    "MISSIONOS_RUNTIME_RECOVERY_PROPOSAL_MAX_AGE_SECONDS"
)
MISSIONOS_RUNTIME_RECOVERY_PROPOSAL_MAX_ORIGIN_DRIFT_M = 30.0
MISSIONOS_RUNTIME_RECOVERY_PROPOSAL_MAX_ORIGIN_DRIFT_M_ENV = (
    "MISSIONOS_RUNTIME_RECOVERY_PROPOSAL_MAX_ORIGIN_DRIFT_M"
)
MISSIONOS_RUNTIME_RECOVERY_AGENT_MAX_WINDOW_SAMPLES = 256
MISSIONOS_AUTO_MISSION_LIVE_TRAJECTORY_MAX_SAMPLES = 4000
MISSION_DESIGNER_LIVE_SITL_DROPOFF_MISSION_ITEM_SEQ = 2
MISSION_DESIGNER_LIVE_SITL_LAND_MISSION_ITEM_SEQ = 3


class PX4GazeboMissionDesignerSITLLiveFlightRunError(RuntimeError):
    """Raised when a Mission Designer live SITL run is not safely bound."""


class MissionDesignerSITLLiveTargetBinding(BaseModel):
    """Canonical link between Gateway mission items and the live route target."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_source: Literal[MISSION_DESIGNER_LIVE_SITL_TARGET_BINDING_SOURCE] = (
        MISSION_DESIGNER_LIVE_SITL_TARGET_BINDING_SOURCE
    )
    delivery_mission_contract_ref: str
    horizontal_route_plan_ref: str
    horizontal_route_plan_schema_version: str
    mission_item_binding_sha256: str
    dropoff_mission_item_seq: Literal[
        MISSION_DESIGNER_LIVE_SITL_DROPOFF_MISSION_ITEM_SEQ
    ]
    dropoff_mission_item_command: Literal[MAV_CMD_NAV_WAYPOINT]
    dropoff_mission_item_latitude_deg: float = Field(ge=-90, le=90)
    dropoff_mission_item_longitude_deg: float = Field(ge=-180, le=180)
    dropoff_mission_item_altitude_m: float = Field(ge=0.0)
    land_mission_item_seq: Literal[MISSION_DESIGNER_LIVE_SITL_LAND_MISSION_ITEM_SEQ]
    land_mission_item_command: Literal[MAV_CMD_NAV_LAND]
    route_target_x_m: float
    route_target_y_m: float
    route_target_z_m: float
    dropoff_target_altitude_m: float
    route_target_bound_to_gateway_receipt: Literal[True] = True
    route_target_bound_to_delivery_contract: Literal[True] = True
    dropoff_verifier_expected_mission_item_seq: Literal[
        MISSION_DESIGNER_LIVE_SITL_DROPOFF_MISSION_ITEM_SEQ
    ] = MISSION_DESIGNER_LIVE_SITL_DROPOFF_MISSION_ITEM_SEQ

    @model_validator(mode="after")
    def _validate_binding(self) -> "MissionDesignerSITLLiveTargetBinding":
        if not self.delivery_mission_contract_ref.startswith(
            "delivery_mission_contract:"
        ):
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                "Mission Designer live target binding requires delivery contract ref"
            )
        if len(self.mission_item_binding_sha256) != 64:
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                "Mission Designer live target binding requires mission item hash"
            )
        return self


class PX4GazeboMissionDesignerSITLLiveFlightBlockedReceipt(BaseModel):
    """Blocked receipt for an operator-requested live SITL flight mode.

    This artifact exists so live mode can fail closed before any live runner,
    MAVLink, Gazebo mutation, ROS, hardware, or physical path is invoked.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_BLOCKED_RECEIPT_SCHEMA_VERSION
    ] = PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_BLOCKED_RECEIPT_SCHEMA_VERSION
    receipt_id: str
    task_ref: str
    live_flight_mode_requested: Literal[True] = True
    live_flight_execution_status: Literal["blocked"] = "blocked"
    explicit_execution_approval_observed: Literal[True] = True
    sitl_execution_opted_in: bool
    live_flight_opted_in: bool
    live_flight_opt_in_env: Literal[MISSION_DESIGNER_LIVE_SITL_FLIGHT_OPT_IN_ENV] = (
        MISSION_DESIGNER_LIVE_SITL_FLIGHT_OPT_IN_ENV
    )
    live_flight_runner_invoked: Literal[False] = False
    actual_sitl_flight_evidence_observed: Literal[False] = False
    preexisting_summary_input_allowed: Literal[False] = False
    payload_dropoff_success_claimed: Literal[False] = False
    blocked_reasons: tuple[str, ...]
    external_dispatch_performed: Literal[False] = False
    mavlink_dispatch_performed: Literal[False] = False
    px4_mission_upload_performed: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    synthetic_success_allowed: Literal[False] = False
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_reasons(cls, value: Any) -> tuple[str, ...]:
        return tuple(str(item) for item in (value or ()))

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_blocked_receipt(
        self,
    ) -> "PX4GazeboMissionDesignerSITLLiveFlightBlockedReceipt":
        if not self.task_ref.startswith("task:"):
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                "Mission Designer live SITL blocked receipt requires task ref"
            )
        if not self.blocked_reasons:
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                "Mission Designer live SITL blocked receipt requires reasons"
            )
        return self


class PX4GazeboMissionDesignerSITLLiveFlightFailedReceipt(BaseModel):
    """Operational failure receipt for a live SITL runner that was invoked.

    This is distinct from the blocked receipt: mission upload and the live
    runner may already have been attempted, but flight evidence did not
    materialize. It must remain an observed simulator outcome, not a delivery
    completion or hardware authority claim.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_FAILED_RECEIPT_SCHEMA_VERSION
    ] = PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_FAILED_RECEIPT_SCHEMA_VERSION
    receipt_id: str
    task_ref: str
    live_flight_mode_requested: Literal[True] = True
    live_flight_execution_status: Literal["blocked"] = "blocked"
    sitl_execution_opted_in: bool
    live_flight_opted_in: bool
    live_flight_runner_invoked: Literal[True] = True
    mission_upload_observed: bool
    mission_ack_observed: bool
    mission_ack_type: int | None = None
    actual_sitl_flight_evidence_observed: Literal[False] = False
    failure_category: str
    failure_reason_digest: str
    stdout_log: str
    stderr_log: str
    blocked_reasons: tuple[str, ...]
    external_dispatch_performed: bool
    mavlink_dispatch_performed: bool
    px4_mission_upload_performed: bool
    gazebo_entity_mutation_performed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    synthetic_success_allowed: Literal[False] = False
    payload_dropoff_success_claimed: Literal[False] = False
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_reasons(cls, value: Any) -> tuple[str, ...]:
        return tuple(str(item) for item in (value or ()))

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_failed_receipt(
        self,
    ) -> "PX4GazeboMissionDesignerSITLLiveFlightFailedReceipt":
        if not self.task_ref.startswith("task:"):
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                "Mission Designer live SITL failed receipt requires task ref"
            )
        if not self.failure_reason_digest:
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                "Mission Designer live SITL failed receipt requires failure digest"
            )
        if not self.blocked_reasons:
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                "Mission Designer live SITL failed receipt requires blocked reasons"
            )
        return self


class PX4GazeboMissionDesignerSITLLiveFlightRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_RUN_SCHEMA_VERSION
    ] = PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_RUN_SCHEMA_VERSION
    live_flight_run_id: str
    task_ref: str
    execution_request_ref: str
    delivery_mission_contract_ref: str
    mission_upload_receipt_ref: str
    execution_result_ref: str
    live_run_source: Literal[MISSION_DESIGNER_LIVE_SITL_RUN_SOURCE]
    live_run_id: str
    horizontal_summary_artifact_dir: str
    horizontal_summary_sha256: str
    mission_item_binding_sha256: str
    live_target_binding_sha256: str
    live_target_binding: MissionDesignerSITLLiveTargetBinding
    mission_request_sequences: tuple[int, ...]
    mission_ack_observed: Literal[True]
    mission_ack_type: Literal[MAV_MISSION_ACCEPTED]
    actual_px4_gazebo_sitl_upload_observed: Literal[True]
    actual_sitl_flight_evidence_observed: Literal[True]
    actual_px4_gazebo_horizontal_smoke_observed: Literal[True]
    dropoff_region_reached: Literal[True]
    route_geofence_violation: Literal[False]
    horizontal_progress_m: float = Field(ge=0.0)
    completed_pose_z_m: float
    route_target_x_m: float
    route_target_y_m: float
    route_target_z_m: float
    flight_path_frame: Literal["gazebo_world_local"] = "gazebo_world_local"
    flight_path_profile: tuple[dict[str, Any], ...] = ()
    flight_path_trace_path: str = ""
    environment_condition_profile: dict[str, Any] = Field(default_factory=dict)
    simulator_capability_matrix: dict[str, Any] = Field(default_factory=dict)
    simulator_condition_application: dict[str, Any] = Field(default_factory=dict)
    observed_environment_evidence: dict[str, Any] = Field(default_factory=dict)
    scenario_cleanup_receipt: dict[str, Any] = Field(default_factory=dict)
    vehicle_condition_profile: dict[str, Any] = Field(default_factory=dict)
    payload_simulator_capability_matrix: dict[str, Any] = Field(default_factory=dict)
    payload_simulator_condition_application: dict[str, Any] = Field(default_factory=dict)
    observed_vehicle_condition_evidence: dict[str, Any] = Field(default_factory=dict)
    battery_condition_profile: dict[str, Any] = Field(default_factory=dict)
    battery_simulator_capability_matrix: dict[str, Any] = Field(default_factory=dict)
    battery_simulator_condition_application: dict[str, Any] = Field(default_factory=dict)
    observed_battery_condition_evidence: dict[str, Any] = Field(default_factory=dict)
    thermal_weather_condition_profile: dict[str, Any] = Field(default_factory=dict)
    thermal_weather_simulator_capability_matrix: dict[str, Any] = Field(
        default_factory=dict
    )
    thermal_weather_simulator_condition_application: dict[str, Any] = Field(
        default_factory=dict
    )
    observed_thermal_weather_evidence: dict[str, Any] = Field(default_factory=dict)
    rain_weather_condition_profile: dict[str, Any] = Field(default_factory=dict)
    rain_weather_simulator_capability_matrix: dict[str, Any] = Field(
        default_factory=dict
    )
    rain_weather_simulator_condition_application: dict[str, Any] = Field(
        default_factory=dict
    )
    observed_rain_weather_evidence: dict[str, Any] = Field(default_factory=dict)
    sensor_condition_profile: dict[str, Any] = Field(default_factory=dict)
    sensor_simulator_capability_matrix: dict[str, Any] = Field(default_factory=dict)
    sensor_failure_injection_application: dict[str, Any] = Field(default_factory=dict)
    observed_sensor_condition_evidence: dict[str, Any] = Field(default_factory=dict)
    gazebo_world_condition_profile: dict[str, Any] = Field(default_factory=dict)
    gazebo_world_capability_matrix: dict[str, Any] = Field(default_factory=dict)
    gazebo_world_application: dict[str, Any] = Field(default_factory=dict)
    obstacle_manifest: dict[str, Any] = Field(default_factory=dict)
    observed_world_condition_evidence: dict[str, Any] = Field(default_factory=dict)
    visibility_condition_profile: dict[str, Any] = Field(default_factory=dict)
    visibility_capability_matrix: dict[str, Any] = Field(default_factory=dict)
    visibility_application: dict[str, Any] = Field(default_factory=dict)
    observed_visibility_condition_evidence: dict[str, Any] = Field(default_factory=dict)
    operational_condition_profile: dict[str, Any] = Field(default_factory=dict)
    geofence_condition_profile: dict[str, Any] = Field(default_factory=dict)
    traffic_conflict_profile: dict[str, Any] = Field(default_factory=dict)
    alternate_landing_profile: dict[str, Any] = Field(default_factory=dict)
    dynamic_actor_profile: dict[str, Any] = Field(default_factory=dict)
    moving_actor_waypoint_motion_application: dict[str, Any] = Field(default_factory=dict)
    moving_actor_pose_observation: dict[str, Any] = Field(default_factory=dict)
    moving_actor_proximity_evidence: dict[str, Any] = Field(default_factory=dict)
    operational_capability_matrix: dict[str, Any] = Field(default_factory=dict)
    operational_application: dict[str, Any] = Field(default_factory=dict)
    observed_operational_condition_evidence: dict[str, Any] = Field(default_factory=dict)
    telemetry_degradation_profile: dict[str, Any] = Field(default_factory=dict)
    telemetry_degradation_application: dict[str, Any] = Field(default_factory=dict)
    observed_telemetry_gap_evidence: dict[str, Any] = Field(default_factory=dict)
    telemetry_freshness_report: dict[str, Any] = Field(default_factory=dict)
    mavlink_link_degradation_profile: dict[str, Any] = Field(default_factory=dict)
    mavlink_link_degradation_capability_matrix: dict[str, Any] = Field(default_factory=dict)
    mavlink_link_degradation_application: dict[str, Any] = Field(default_factory=dict)
    observed_mavlink_gap_evidence: dict[str, Any] = Field(default_factory=dict)
    terrain_world_readback: dict[str, Any] = Field(default_factory=dict)
    payload_release_observed: bool
    payload_release_event_source: str
    preexisting_summary_input_allowed: Literal[False] = False
    same_gateway_execution_run_required: Literal[True] = True
    same_gateway_execution_run_observed: Literal[True]
    mission_items_bound_to_gateway_receipt: Literal[True]
    live_route_target_bound_to_gateway_receipt: Literal[True]
    live_route_target_bound_to_delivery_contract: Literal[True]
    payload_dropoff_success_claimed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    synthetic_success_allowed: Literal[False] = False
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("mission_request_sequences", mode="before")
    @classmethod
    def _coerce_int_tuple(cls, value: Any) -> tuple[int, ...]:
        return tuple(int(item) for item in (value or ()))

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_live_run(self) -> "PX4GazeboMissionDesignerSITLLiveFlightRun":
        expected_prefixes = {
            "task_ref": "task:",
            "execution_request_ref": (
                "px4_gazebo_mission_designer_sitl_execution_request:"
            ),
            "delivery_mission_contract_ref": "delivery_mission_contract:",
            "mission_upload_receipt_ref": "px4_gazebo_sitl_mission_upload_receipt:",
            "execution_result_ref": (
                "px4_gazebo_mission_designer_sitl_execution_result:"
            ),
        }
        for field_name, prefix in expected_prefixes.items():
            if not str(getattr(self, field_name)).startswith(prefix):
                raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                    f"Mission Designer live SITL run requires {field_name}"
                )
        if self.mission_request_sequences != (0, 1, 2, 3):
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                "Mission Designer live SITL run requires mission request sequence 0..3"
            )
        if self.live_target_binding.delivery_mission_contract_ref != (
            self.delivery_mission_contract_ref
        ):
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                "Mission Designer live SITL run target binding contract mismatch"
            )
        if self.live_target_binding.mission_item_binding_sha256 != (
            self.mission_item_binding_sha256
        ):
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                "Mission Designer live SITL run target binding item mismatch"
            )
        if (
            mission_designer_sitl_live_target_binding_sha256(self.live_target_binding)
            != self.live_target_binding_sha256
        ):
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                "Mission Designer live SITL run target binding hash mismatch"
            )
        if not (
            _float_equal(
                self.route_target_x_m, self.live_target_binding.route_target_x_m
            )
            and _float_equal(
                self.route_target_y_m, self.live_target_binding.route_target_y_m
            )
            and _float_equal(
                self.route_target_z_m, self.live_target_binding.route_target_z_m
            )
        ):
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                "Mission Designer live SITL run target binding route mismatch"
            )
        if self.completed_pose_z_m > 0.15:
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                "Mission Designer live SITL run requires observed landing pose"
            )
        if self.payload_release_observed and (
            self.payload_release_event_source != "gazebo_detachable_joint_detach_event"
        ):
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                "Mission Designer live SITL run requires allowlisted payload source"
            )
        return self


def _utc(value: datetime | None = None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        return resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _env_truthy(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def mission_designer_sitl_mission_item_binding_sha256(
    items: Sequence[PX4GazeboSITLMissionItem | Mapping[str, Any]],
) -> str:
    payload = []
    for item in items:
        if isinstance(item, PX4GazeboSITLMissionItem):
            value = item.model_dump(mode="json")
        else:
            value = dict(item)
        payload.append(
            {
                "seq": int(value["seq"]),
                "command": int(value["command"]),
                "frame": int(value.get("frame", 6)),
                "latitude_deg": round(float(value["latitude_deg"]), 7),
                "longitude_deg": round(float(value["longitude_deg"]), 7),
                "altitude_m": round(float(value["altitude_m"]), 3),
                "current": int(value.get("current", 0)),
                "autocontinue": int(value.get("autocontinue", 1)),
            }
        )
    return _canonical_sha256(payload)


def mission_designer_sitl_live_target_binding_sha256(
    binding: MissionDesignerSITLLiveTargetBinding | Mapping[str, Any],
) -> str:
    value = (
        binding.model_dump(mode="json")
        if isinstance(binding, MissionDesignerSITLLiveTargetBinding)
        else dict(binding)
    )
    return _canonical_sha256(value)


def _float_equal(left: Any, right: Any) -> bool:
    return abs(float(left) - float(right)) <= 1e-9


def _summary_float(summary: Mapping[str, Any], key: str) -> float:
    if key not in summary:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"Mission Designer live SITL run requires {key}"
        )
    return float(summary[key])


def _require_summary_float(
    summary: Mapping[str, Any],
    key: str,
    expected: float,
) -> None:
    if not _float_equal(_summary_float(summary, key), expected):
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"Mission Designer live SITL run requires {key}"
        )


def _mission_item_by_seq(
    items: Sequence[PX4GazeboSITLMissionItem],
    seq: int,
) -> PX4GazeboSITLMissionItem:
    for item in items:
        if item.seq == seq:
            return item
    raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
        f"Mission Designer live SITL run requires mission item seq {seq}"
    )


def _contract_ref(contract: DeliveryMissionContract) -> str:
    return _artifact_ref("delivery_mission_contract", contract.contract_id)


def _route_plan_ref(route_plan: PX4GazeboPickupDropoffRoutePlan) -> str:
    return _artifact_ref(
        "px4_gazebo_pickup_dropoff_route_plan", route_plan.route_plan_id
    )


def _summary_artifacts_path(horizontal_summary: Mapping[str, Any]) -> Path:
    artifact_dir_value = str(horizontal_summary.get("artifact_dir") or "").strip()
    if not artifact_dir_value:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run requires horizontal artifact dir"
        )
    return Path(artifact_dir_value).expanduser() / "mission_artifacts.json"


def _horizontal_route_plan_from_summary_artifacts(
    horizontal_summary: Mapping[str, Any],
) -> PX4GazeboPickupDropoffRoutePlan:
    manifest_path = _summary_artifacts_path(horizontal_summary)
    if not manifest_path.exists():
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run requires horizontal mission artifacts"
        )
    manifest = json.loads(manifest_path.read_text())
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run requires horizontal artifact manifest"
        )
    route_plan_payload = artifacts.get("px4_gazebo_pickup_dropoff_route_plan")
    if not isinstance(route_plan_payload, Mapping):
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run requires horizontal route plan"
        )
    return PX4GazeboPickupDropoffRoutePlan.model_validate(dict(route_plan_payload))


def _route_plan_live_target(
    route_plan: PX4GazeboPickupDropoffRoutePlan,
) -> dict[str, float]:
    target_x, target_y, target_z = derive_px4_gazebo_route_target_ned(route_plan)
    return {
        "route_target_x_m": float(target_x),
        "route_target_y_m": float(target_y),
        "route_target_z_m": float(target_z),
        "dropoff_target_altitude_m": 0.0,
    }


def build_mission_designer_sitl_live_target_binding(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    mission_upload_receipt: PX4GazeboSITLMissionUploadReceipt | Mapping[str, Any],
    horizontal_summary: Mapping[str, Any],
) -> MissionDesignerSITLLiveTargetBinding:
    contract = (
        delivery_mission_contract
        if isinstance(delivery_mission_contract, DeliveryMissionContract)
        else DeliveryMissionContract.model_validate(dict(delivery_mission_contract))
    )
    receipt = (
        mission_upload_receipt
        if isinstance(mission_upload_receipt, PX4GazeboSITLMissionUploadReceipt)
        else PX4GazeboSITLMissionUploadReceipt.model_validate(
            dict(mission_upload_receipt)
        )
    )
    expected_items = build_sitl_mission_items_from_contract(
        contract,
        max_altitude_m=receipt.max_altitude_m,
        max_mission_items=receipt.max_mission_items,
    )
    item_binding = mission_designer_sitl_mission_item_binding_sha256(
        receipt.mission_items
    )
    if (
        mission_designer_sitl_mission_item_binding_sha256(expected_items)
        != item_binding
    ):
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run receipt items do not match delivery contract"
        )
    route_plan = _horizontal_route_plan_from_summary_artifacts(horizontal_summary)
    target = _route_plan_live_target(route_plan)
    for key, expected in target.items():
        if key == "dropoff_target_altitude_m":
            observed = float(horizontal_summary.get(key, 0.0))
            if not _float_equal(observed, expected):
                raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                    "Mission Designer live SITL run requires dropoff target altitude"
                )
            continue
        _require_summary_float(horizontal_summary, key, expected)
    dropoff_item = _mission_item_by_seq(
        receipt.mission_items, MISSION_DESIGNER_LIVE_SITL_DROPOFF_MISSION_ITEM_SEQ
    )
    land_item = _mission_item_by_seq(
        receipt.mission_items, MISSION_DESIGNER_LIVE_SITL_LAND_MISSION_ITEM_SEQ
    )
    if dropoff_item.command != MAV_CMD_NAV_WAYPOINT:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run requires waypoint dropoff mission item"
        )
    if land_item.command != MAV_CMD_NAV_LAND:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run requires land mission item"
        )
    if not (
        _float_equal(dropoff_item.latitude_deg, contract.dropoff_location.latitude)
        and _float_equal(
            dropoff_item.longitude_deg, contract.dropoff_location.longitude
        )
    ):
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run dropoff item contract mismatch"
        )
    return MissionDesignerSITLLiveTargetBinding(
        delivery_mission_contract_ref=_contract_ref(contract),
        horizontal_route_plan_ref=_route_plan_ref(route_plan),
        horizontal_route_plan_schema_version=route_plan.schema_version,
        mission_item_binding_sha256=item_binding,
        dropoff_mission_item_seq=MISSION_DESIGNER_LIVE_SITL_DROPOFF_MISSION_ITEM_SEQ,
        dropoff_mission_item_command=MAV_CMD_NAV_WAYPOINT,
        dropoff_mission_item_latitude_deg=dropoff_item.latitude_deg,
        dropoff_mission_item_longitude_deg=dropoff_item.longitude_deg,
        dropoff_mission_item_altitude_m=dropoff_item.altitude_m,
        land_mission_item_seq=MISSION_DESIGNER_LIVE_SITL_LAND_MISSION_ITEM_SEQ,
        land_mission_item_command=MAV_CMD_NAV_LAND,
        route_target_x_m=target["route_target_x_m"],
        route_target_y_m=target["route_target_y_m"],
        route_target_z_m=target["route_target_z_m"],
        dropoff_target_altitude_m=target["dropoff_target_altitude_m"],
    )


def _artifact_ref(prefix: str, value: str) -> str:
    return f"{prefix}:{value}"


def _artifact(artifacts: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = artifacts.get(key)
    if not isinstance(value, Mapping):
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"Mission Designer live SITL run requires {key}"
        )
    return value


def mission_designer_live_sitl_flight_opted_in() -> bool:
    return os.getenv(MISSION_DESIGNER_LIVE_SITL_FLIGHT_OPT_IN_ENV) == "1"


def missionos_auto_mission_gui_dispatch_opted_in() -> bool:
    return os.getenv(MISSIONOS_AUTO_MISSION_GUI_DISPATCH_OPT_IN_ENV) == "1"


def build_px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt(
    *,
    task_id: str,
    sitl_execution_opted_in: bool,
    live_flight_opted_in: bool,
    blocked_reasons: Sequence[str],
    observed_at: datetime | None = None,
) -> PX4GazeboMissionDesignerSITLLiveFlightBlockedReceipt:
    resolved_at = _utc(observed_at)
    payload = {
        "task_id": task_id,
        "sitl_execution_opted_in": bool(sitl_execution_opted_in),
        "live_flight_opted_in": bool(live_flight_opted_in),
        "blocked_reasons": tuple(str(item) for item in blocked_reasons),
        "observed_at": resolved_at.isoformat(),
    }
    return PX4GazeboMissionDesignerSITLLiveFlightBlockedReceipt(
        receipt_id=_stable_id(
            "px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt",
            payload,
        ),
        task_ref=_artifact_ref("task", task_id),
        sitl_execution_opted_in=bool(sitl_execution_opted_in),
        live_flight_opted_in=bool(live_flight_opted_in),
        blocked_reasons=tuple(str(item) for item in blocked_reasons),
        observed_at=resolved_at,
        metadata={
            "source": "px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt",
            "live_flight_opt_in_env": MISSION_DESIGNER_LIVE_SITL_FLIGHT_OPT_IN_ENV,
            "live_runner_not_invoked": True,
        },
    )


def attach_px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt(
    task_id: str,
    *,
    sitl_execution_opted_in: bool,
    live_flight_opted_in: bool,
    blocked_reasons: Sequence[str],
    task_store_factory: Callable[[], TaskStore] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"task {task_id} not found; cannot attach live SITL blocked receipt"
        )
    receipt = build_px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt(
        task_id=task_id,
        sitl_execution_opted_in=sitl_execution_opted_in,
        live_flight_opted_in=live_flight_opted_in,
        blocked_reasons=blocked_reasons,
        observed_at=now,
    )
    updated = store.update(
        task_id,
        status="blocked",
        artifacts={
            "px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt": (
                receipt.model_dump(mode="json")
            )
        },
        metadata={
            "mission_designer_live_sitl_flight_mode_requested": True,
            "mission_designer_live_sitl_flight_status": "blocked",
            "mission_designer_live_sitl_flight_opted_in": receipt.live_flight_opted_in,
            "mission_designer_live_sitl_flight_runner_invoked": False,
            "mission_designer_live_sitl_flight_blocked_reasons": list(
                receipt.blocked_reasons
            ),
            "hardware_target_allowed": receipt.hardware_target_allowed,
            "physical_execution_invoked": receipt.physical_execution_invoked,
        },
    )
    if updated is None:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"task {task_id} disappeared while attaching live SITL blocked receipt"
        )
    return {
        "task": updated,
        "px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt": (
            receipt.model_dump(mode="json")
        ),
        "summary": {
            "task_id": updated["task_id"],
            "task_status": updated["status"],
            "live_flight_mode_requested": True,
            "live_flight_status": receipt.live_flight_execution_status,
            "sitl_execution_opted_in": receipt.sitl_execution_opted_in,
            "live_flight_opted_in": receipt.live_flight_opted_in,
            "live_flight_runner_invoked": receipt.live_flight_runner_invoked,
            "blocked_reasons": list(receipt.blocked_reasons),
            "preexisting_summary_input_allowed": (
                receipt.preexisting_summary_input_allowed
            ),
            "hardware_target_allowed": receipt.hardware_target_allowed,
            "physical_execution_invoked": receipt.physical_execution_invoked,
            "synthetic_success_allowed": receipt.synthetic_success_allowed,
        },
    }


def _failure_category_from_digest(digest: str) -> str:
    """Return the v1 symptom category for a runner failure digest.

    These categories intentionally describe the observed runner symptom, not a
    root cause. Cause-specific categories such as payload thrust envelope or
    drift envelope failures should be added by a later source-bound analyzer.
    """

    normalized = digest.lower()
    if "z predicate" in normalized or "takeoff" in normalized or "climb" in normalized:
        return "takeoff_or_climb_predicate_timeout"
    if "timed out" in normalized:
        return "horizontal_route_runner_timeout"
    return "horizontal_route_runner_failed"


def _parse_runner_failure_message(message: str) -> tuple[str, str, str]:
    """Parse the current runner error string into digest and log refs.

    This is a compatibility parser for the current string-formatted runner
    exception. A future structured runner exception should replace the literal
    `reason=...; stdout_log=...; stderr_log=...` parsing here.
    """

    reason = message
    stdout_log = ""
    stderr_log = ""
    if "reason=" in message:
        reason = message.split("reason=", 1)[1]
    if "; stdout_log=" in reason:
        reason, remainder = reason.split("; stdout_log=", 1)
        if "; stderr_log=" in remainder:
            stdout_log, stderr_log = remainder.split("; stderr_log=", 1)
        else:
            stdout_log = remainder
    return reason.strip(), stdout_log.strip(), stderr_log.strip()


def build_px4_gazebo_mission_designer_sitl_live_flight_failed_receipt(
    *,
    task_id: str,
    sitl_execution_opted_in: bool,
    live_flight_opted_in: bool,
    mission_upload_observed: bool,
    mission_ack_observed: bool,
    mission_ack_type: int | None,
    external_dispatch_performed: bool,
    mavlink_dispatch_performed: bool,
    px4_mission_upload_performed: bool,
    failure_message: str,
    observed_at: datetime | None = None,
) -> PX4GazeboMissionDesignerSITLLiveFlightFailedReceipt:
    resolved_at = _utc(observed_at)
    reason, stdout_log, stderr_log = _parse_runner_failure_message(failure_message)
    category = _failure_category_from_digest(reason)
    blocked_reasons = (
        category,
        "observed_flight_evidence_not_attached",
        "payload_release_event_not_observed",
        "dropoff_verification_not_observed",
    )
    payload = {
        "task_id": task_id,
        "sitl_execution_opted_in": bool(sitl_execution_opted_in),
        "live_flight_opted_in": bool(live_flight_opted_in),
        "mission_upload_observed": bool(mission_upload_observed),
        "mission_ack_observed": bool(mission_ack_observed),
        "mission_ack_type": mission_ack_type,
        "failure_category": category,
        "failure_reason_digest": reason,
        "blocked_reasons": blocked_reasons,
        "observed_at": resolved_at.isoformat(),
    }
    return PX4GazeboMissionDesignerSITLLiveFlightFailedReceipt(
        receipt_id=_stable_id(
            "px4_gazebo_mission_designer_sitl_live_flight_failed_receipt",
            payload,
        ),
        task_ref=_artifact_ref("task", task_id),
        sitl_execution_opted_in=bool(sitl_execution_opted_in),
        live_flight_opted_in=bool(live_flight_opted_in),
        mission_upload_observed=bool(mission_upload_observed),
        mission_ack_observed=bool(mission_ack_observed),
        mission_ack_type=mission_ack_type,
        failure_category=category,
        failure_reason_digest=reason,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        blocked_reasons=blocked_reasons,
        external_dispatch_performed=bool(external_dispatch_performed),
        mavlink_dispatch_performed=bool(mavlink_dispatch_performed),
        px4_mission_upload_performed=bool(px4_mission_upload_performed),
        observed_at=resolved_at,
        metadata={
            "source": "px4_gazebo_mission_designer_sitl_live_flight_failed_receipt",
            "runner_invoked_but_no_flight_evidence": True,
            "delivery_completion_claimed": False,
        },
    )


def attach_px4_gazebo_mission_designer_sitl_live_flight_failed_receipt(
    task_id: str,
    *,
    sitl_execution_opted_in: bool,
    live_flight_opted_in: bool,
    mission_upload_observed: bool,
    mission_ack_observed: bool,
    mission_ack_type: int | None,
    external_dispatch_performed: bool,
    mavlink_dispatch_performed: bool,
    px4_mission_upload_performed: bool,
    failure_message: str,
    task_store_factory: Callable[[], TaskStore] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"task {task_id} not found; cannot attach live SITL failed receipt"
        )
    receipt = build_px4_gazebo_mission_designer_sitl_live_flight_failed_receipt(
        task_id=task_id,
        sitl_execution_opted_in=sitl_execution_opted_in,
        live_flight_opted_in=live_flight_opted_in,
        mission_upload_observed=mission_upload_observed,
        mission_ack_observed=mission_ack_observed,
        mission_ack_type=mission_ack_type,
        external_dispatch_performed=external_dispatch_performed,
        mavlink_dispatch_performed=mavlink_dispatch_performed,
        px4_mission_upload_performed=px4_mission_upload_performed,
        failure_message=failure_message,
        observed_at=now,
    )
    updated = store.update(
        task_id,
        status="blocked",
        artifacts={
            "px4_gazebo_mission_designer_sitl_live_flight_failed_receipt": (
                receipt.model_dump(mode="json")
            )
        },
        metadata={
            "mission_designer_live_sitl_flight_mode_requested": True,
            "mission_designer_live_sitl_flight_status": "blocked",
            "mission_designer_live_sitl_flight_opted_in": receipt.live_flight_opted_in,
            "mission_designer_live_sitl_flight_runner_invoked": True,
            "mission_designer_live_sitl_flight_failure_category": (
                receipt.failure_category
            ),
            "mission_designer_live_sitl_flight_blocked_reasons": list(
                receipt.blocked_reasons
            ),
            "hardware_target_allowed": receipt.hardware_target_allowed,
            "physical_execution_invoked": receipt.physical_execution_invoked,
        },
    )
    if updated is None:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"task {task_id} disappeared while attaching live SITL failed receipt"
        )
    return {
        "task": updated,
        "px4_gazebo_mission_designer_sitl_live_flight_failed_receipt": (
            receipt.model_dump(mode="json")
        ),
        "summary": {
            "task_id": updated["task_id"],
            "task_status": updated["status"],
            "live_flight_mode_requested": True,
            "live_flight_status": receipt.live_flight_execution_status,
            "sitl_execution_opted_in": receipt.sitl_execution_opted_in,
            "live_flight_opted_in": receipt.live_flight_opted_in,
            "live_flight_runner_invoked": receipt.live_flight_runner_invoked,
            "mission_upload_observed": receipt.mission_upload_observed,
            "mission_ack_observed": receipt.mission_ack_observed,
            "mission_ack_type": receipt.mission_ack_type,
            "actual_sitl_flight_evidence_observed": (
                receipt.actual_sitl_flight_evidence_observed
            ),
            "failure_category": receipt.failure_category,
            "failure_reason_digest": receipt.failure_reason_digest,
            "blocked_reasons": list(receipt.blocked_reasons),
            "external_dispatch_performed": receipt.external_dispatch_performed,
            "mavlink_dispatch_performed": receipt.mavlink_dispatch_performed,
            "px4_mission_upload_performed": receipt.px4_mission_upload_performed,
            "hardware_target_allowed": receipt.hardware_target_allowed,
            "physical_execution_invoked": receipt.physical_execution_invoked,
            "synthetic_success_allowed": receipt.synthetic_success_allowed,
        },
    }


def _require_summary_value(summary: Mapping[str, Any], key: str, expected: Any) -> None:
    if summary.get(key) != expected:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"Mission Designer live SITL run requires {key}"
        )


def _load_horizontal_route_flight_path_profile(
    artifact_dir: str,
    *,
    max_samples: int = 200,
) -> list[dict[str, Any]]:
    pose_path = Path(artifact_dir).expanduser() / "pose_samples.jsonl"
    if not pose_path.is_file():
        return []
    samples: list[dict[str, Any]] = []
    last_pose: dict[str, float] | None = None
    for line in pose_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping):
            continue
        pose = row.get("sample")
        if row.get("phase") == "telemetry_gap" and last_pose is not None:
            samples.append(
                {
                    "sample_index": len(samples),
                    "phase": "telemetry_gap",
                    "local_x_m": last_pose["x"],
                    "local_y_m": last_pose["y"],
                    "local_z_m": last_pose["z"],
                    "telemetry_gap": True,
                    "gap_reason": str(row.get("gap_reason") or "observer_sample_pause"),
                    "gap_duration_seconds": row.get("gap_duration_seconds"),
                    "missing_sample_count": row.get("missing_sample_count"),
                    "publisher_state_mutated": False,
                    "mission_upload_path_mutated": False,
                    "mission_progress_mutated": False,
                    "publisher_transport_loss_claimed": False,
                    "vehicle_recovery_behavior_claimed": False,
                    "mission_failure_claimed": False,
                    "mavlink_link_loss_claimed": False,
                    "vehicle_failsafe_claimed": False,
                    "delivery_completion_claimed": False,
                }
            )
            continue
        if not isinstance(pose, Mapping):
            continue
        try:
            local_x_m = float(pose["x"])
            local_y_m = float(pose["y"])
            local_z_m = float(pose["z"])
        except (KeyError, TypeError, ValueError):
            continue
        sample: dict[str, Any] = {
            "sample_index": len(samples),
            "phase": str(row.get("phase") or "unknown"),
            "local_x_m": local_x_m,
            "local_y_m": local_y_m,
            "local_z_m": local_z_m,
        }
        last_pose = {"x": local_x_m, "y": local_y_m, "z": local_z_m}
        battery_status = row.get("battery_status")
        if isinstance(battery_status, Mapping):
            for source_key, target_key in (
                ("battery_status_observed", "battery_status_observed"),
                ("battery_remaining_percent", "battery_remaining_percent"),
                ("battery_warning", "battery_warning"),
                ("battery_voltage_v", "battery_voltage_v"),
                ("battery_current_a", "battery_current_a"),
                ("battery_connected", "battery_connected"),
                ("battery_state_source", "battery_state_source"),
            ):
                if source_key in battery_status:
                    sample[target_key] = battery_status[source_key]
        if row.get("sample_index") is not None:
            try:
                sample["phase_sample_index"] = int(row["sample_index"])
            except (TypeError, ValueError):
                pass
        samples.append(sample)
    if len(samples) <= max_samples:
        return samples
    step = max(1, len(samples) // max_samples)
    reduced = samples[::step][: max_samples - 1]
    if reduced[-1] != samples[-1]:
        reduced.append(samples[-1])
    return reduced


def build_px4_gazebo_mission_designer_sitl_live_flight_run(
    *,
    task: Mapping[str, Any],
    horizontal_summary: Mapping[str, Any],
    observed_at: datetime | None = None,
) -> PX4GazeboMissionDesignerSITLLiveFlightRun:
    artifacts_value = task.get("artifacts")
    if not isinstance(artifacts_value, Mapping):
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run requires task artifacts"
        )
    artifacts = artifacts_value
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run requires task id"
        )
    execution_request = PX4GazeboMissionDesignerSITLExecutionRequest.model_validate(
        _artifact(artifacts, "px4_gazebo_mission_designer_sitl_execution_request")
    )
    contract = DeliveryMissionContract.model_validate(
        _artifact(artifacts, "delivery_mission_contract")
    )
    receipt = PX4GazeboSITLMissionUploadReceipt.model_validate(
        _artifact(artifacts, "px4_gazebo_sitl_mission_upload_receipt")
    )
    execution_result = PX4GazeboMissionDesignerSITLExecutionResult.model_validate(
        _artifact(artifacts, "px4_gazebo_mission_designer_sitl_execution_result")
    )
    execution_request_ref = _artifact_ref(
        "px4_gazebo_mission_designer_sitl_execution_request",
        execution_request.execution_request_id,
    )
    contract_ref = _artifact_ref("delivery_mission_contract", contract.contract_id)
    receipt_ref = _artifact_ref(
        "px4_gazebo_sitl_mission_upload_receipt", receipt.receipt_id
    )
    execution_result_ref = _artifact_ref(
        "px4_gazebo_mission_designer_sitl_execution_result",
        execution_result.result_id,
    )
    if execution_result.execution_request_ref != execution_request_ref:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run execution request ref mismatch"
        )
    if execution_result.delivery_mission_contract_ref != contract_ref:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run contract ref mismatch"
        )
    if execution_result.px4_gazebo_sitl_mission_upload_receipt_ref != receipt_ref:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run upload receipt ref mismatch"
        )
    if receipt.upload_status is not PX4GazeboSITLMissionUploadStatus.UPLOADED:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run requires uploaded receipt"
        )
    if receipt.mission_ack_observed is not True or receipt.mission_ack_type != 0:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run requires accepted upload ACK"
        )
    artifact_dir, summary_sha256 = _validate_horizontal_summary_artifacts(
        horizontal_summary
    )
    _require_summary_value(
        horizontal_summary,
        "mission_designer_live_sitl_run_source",
        MISSION_DESIGNER_LIVE_SITL_RUN_SOURCE,
    )
    _require_summary_value(horizontal_summary, "mission_designer_task_id", task_id)
    _require_summary_value(
        horizontal_summary,
        "mission_designer_execution_request_ref",
        execution_request_ref,
    )
    _require_summary_value(
        horizontal_summary, "delivery_mission_contract_ref", contract_ref
    )
    _require_summary_value(
        horizontal_summary, "px4_gazebo_sitl_mission_upload_receipt_ref", receipt_ref
    )
    item_binding = mission_designer_sitl_mission_item_binding_sha256(
        receipt.mission_items
    )
    _require_summary_value(
        horizontal_summary, "mission_item_binding_sha256", item_binding
    )
    target_binding = build_mission_designer_sitl_live_target_binding(
        delivery_mission_contract=contract,
        mission_upload_receipt=receipt,
        horizontal_summary=horizontal_summary,
    )
    target_binding_payload = target_binding.model_dump(mode="json")
    target_binding_hash = mission_designer_sitl_live_target_binding_sha256(
        target_binding
    )
    _require_summary_value(
        horizontal_summary,
        "mission_designer_live_target_binding_sha256",
        target_binding_hash,
    )
    if horizontal_summary.get("mission_designer_live_target_binding") != (
        target_binding_payload
    ):
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run target binding summary mismatch"
        )
    if tuple(horizontal_summary.get("preupload_mission_request_sequences") or ()) != (
        tuple(receipt.mission_request_sequences)
    ):
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run request sequence mismatch"
        )
    required_summary_true = (
        "preupload_mission_performed",
        "preupload_mission_ack_observed",
        "actual_px4_gazebo_horizontal_smoke_observed",
        "dropoff_region_reached",
        "same_gateway_execution_run_observed",
    )
    for key in required_summary_true:
        if horizontal_summary.get(key) is not True:
            raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                f"Mission Designer live SITL run requires {key}"
            )
    if horizontal_summary.get("preupload_mission_ack_type") != MAV_MISSION_ACCEPTED:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run requires accepted upload ACK"
        )
    if horizontal_summary.get("route_geofence_violation") is not False:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run rejects geofence violation"
        )
    if tuple(horizontal_summary.get("blocked_reasons") or ()) != ():
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run rejects blocked summary"
        )
    if horizontal_summary.get("hardware_target_allowed") is not False:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run rejects hardware target allowance"
        )
    if horizontal_summary.get("physical_execution_invoked") is not False:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run rejects physical execution"
        )
    live_run_id = str(horizontal_summary.get("mission_designer_live_sitl_run_id") or "")
    if not live_run_id:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL run requires live run id"
        )
    payload = {
        "task_id": task_id,
        "execution_request_ref": execution_request_ref,
        "receipt_ref": receipt_ref,
        "live_run_id": live_run_id,
        "summary_sha256": summary_sha256,
        "item_binding": item_binding,
        "target_binding": target_binding_hash,
    }
    flight_path_profile = _load_horizontal_route_flight_path_profile(artifact_dir)
    return PX4GazeboMissionDesignerSITLLiveFlightRun(
        live_flight_run_id=_stable_id(
            "px4_gazebo_mission_designer_sitl_live_flight_run", payload
        ),
        task_ref=_artifact_ref("task", task_id),
        execution_request_ref=execution_request_ref,
        delivery_mission_contract_ref=contract_ref,
        mission_upload_receipt_ref=receipt_ref,
        execution_result_ref=execution_result_ref,
        live_run_source=MISSION_DESIGNER_LIVE_SITL_RUN_SOURCE,
        live_run_id=live_run_id,
        horizontal_summary_artifact_dir=artifact_dir,
        horizontal_summary_sha256=summary_sha256,
        mission_item_binding_sha256=item_binding,
        live_target_binding_sha256=target_binding_hash,
        live_target_binding=target_binding,
        mission_request_sequences=receipt.mission_request_sequences,
        mission_ack_observed=True,
        mission_ack_type=MAV_MISSION_ACCEPTED,
        actual_px4_gazebo_sitl_upload_observed=True,
        actual_sitl_flight_evidence_observed=True,
        actual_px4_gazebo_horizontal_smoke_observed=True,
        dropoff_region_reached=True,
        route_geofence_violation=False,
        horizontal_progress_m=float(horizontal_summary["horizontal_progress_m"]),
        completed_pose_z_m=float(horizontal_summary["completed_pose_z_m"]),
        route_target_x_m=float(horizontal_summary["route_target_x_m"]),
        route_target_y_m=float(horizontal_summary["route_target_y_m"]),
        route_target_z_m=float(horizontal_summary["route_target_z_m"]),
        flight_path_profile=tuple(flight_path_profile),
        flight_path_trace_path=str(Path(artifact_dir) / "pose_samples.jsonl"),
        environment_condition_profile=dict(
            horizontal_summary.get("environment_condition_profile") or {}
        ),
        simulator_capability_matrix=dict(
            horizontal_summary.get("simulator_capability_matrix") or {}
        ),
        simulator_condition_application=dict(
            horizontal_summary.get("simulator_condition_application") or {}
        ),
        observed_environment_evidence=dict(
            horizontal_summary.get("observed_environment_evidence") or {}
        ),
        scenario_cleanup_receipt=dict(
            horizontal_summary.get("scenario_cleanup_receipt") or {}
        ),
        vehicle_condition_profile=dict(
            horizontal_summary.get("vehicle_condition_profile") or {}
        ),
        payload_simulator_capability_matrix=dict(
            horizontal_summary.get("payload_simulator_capability_matrix") or {}
        ),
        payload_simulator_condition_application=dict(
            horizontal_summary.get("payload_simulator_condition_application") or {}
        ),
        observed_vehicle_condition_evidence=dict(
            horizontal_summary.get("observed_vehicle_condition_evidence") or {}
        ),
        battery_condition_profile=dict(
            horizontal_summary.get("battery_condition_profile") or {}
        ),
        battery_simulator_capability_matrix=dict(
            horizontal_summary.get("battery_simulator_capability_matrix") or {}
        ),
        battery_simulator_condition_application=dict(
            horizontal_summary.get("battery_simulator_condition_application") or {}
        ),
        observed_battery_condition_evidence=dict(
            horizontal_summary.get("observed_battery_condition_evidence") or {}
        ),
        thermal_weather_condition_profile=dict(
            horizontal_summary.get("thermal_weather_condition_profile") or {}
        ),
        thermal_weather_simulator_capability_matrix=dict(
            horizontal_summary.get("thermal_weather_simulator_capability_matrix") or {}
        ),
        thermal_weather_simulator_condition_application=dict(
            horizontal_summary.get("thermal_weather_simulator_condition_application")
            or {}
        ),
        observed_thermal_weather_evidence=dict(
            horizontal_summary.get("observed_thermal_weather_evidence") or {}
        ),
        rain_weather_condition_profile=dict(
            horizontal_summary.get("rain_weather_condition_profile") or {}
        ),
        rain_weather_simulator_capability_matrix=dict(
            horizontal_summary.get("rain_weather_simulator_capability_matrix") or {}
        ),
        rain_weather_simulator_condition_application=dict(
            horizontal_summary.get("rain_weather_simulator_condition_application") or {}
        ),
        observed_rain_weather_evidence=dict(
            horizontal_summary.get("observed_rain_weather_evidence") or {}
        ),
        sensor_condition_profile=dict(
            horizontal_summary.get("sensor_condition_profile") or {}
        ),
        sensor_simulator_capability_matrix=dict(
            horizontal_summary.get("sensor_simulator_capability_matrix") or {}
        ),
        sensor_failure_injection_application=dict(
            horizontal_summary.get("sensor_failure_injection_application") or {}
        ),
        observed_sensor_condition_evidence=dict(
            horizontal_summary.get("observed_sensor_condition_evidence") or {}
        ),
        gazebo_world_condition_profile=dict(
            horizontal_summary.get("gazebo_world_condition_profile") or {}
        ),
        gazebo_world_capability_matrix=dict(
            horizontal_summary.get("gazebo_world_capability_matrix") or {}
        ),
        gazebo_world_application=dict(
            horizontal_summary.get("gazebo_world_application") or {}
        ),
        obstacle_manifest=dict(
            horizontal_summary.get("obstacle_manifest") or {}
        ),
        observed_world_condition_evidence=dict(
            horizontal_summary.get("observed_world_condition_evidence") or {}
        ),
        visibility_condition_profile=dict(
            horizontal_summary.get("visibility_condition_profile") or {}
        ),
        visibility_capability_matrix=dict(
            horizontal_summary.get("visibility_capability_matrix") or {}
        ),
        visibility_application=dict(
            horizontal_summary.get("visibility_application") or {}
        ),
        observed_visibility_condition_evidence=dict(
            horizontal_summary.get("observed_visibility_condition_evidence") or {}
        ),
        operational_condition_profile=dict(
            horizontal_summary.get("operational_condition_profile") or {}
        ),
        geofence_condition_profile=dict(
            horizontal_summary.get("geofence_condition_profile") or {}
        ),
        traffic_conflict_profile=dict(
            horizontal_summary.get("traffic_conflict_profile") or {}
        ),
        alternate_landing_profile=dict(
            horizontal_summary.get("alternate_landing_profile") or {}
        ),
        dynamic_actor_profile=dict(
            horizontal_summary.get("dynamic_actor_profile") or {}
        ),
        moving_actor_waypoint_motion_application=dict(
            horizontal_summary.get("moving_actor_waypoint_motion_application") or {}
        ),
        moving_actor_pose_observation=dict(
            horizontal_summary.get("moving_actor_pose_observation") or {}
        ),
        moving_actor_proximity_evidence=dict(
            horizontal_summary.get("moving_actor_proximity_evidence") or {}
        ),
        operational_capability_matrix=dict(
            horizontal_summary.get("operational_capability_matrix") or {}
        ),
        operational_application=dict(
            horizontal_summary.get("operational_application") or {}
        ),
        observed_operational_condition_evidence=dict(
            horizontal_summary.get("observed_operational_condition_evidence") or {}
        ),
        telemetry_degradation_profile=dict(
            horizontal_summary.get("telemetry_degradation_profile") or {}
        ),
        telemetry_degradation_application=dict(
            horizontal_summary.get("telemetry_degradation_application") or {}
        ),
        observed_telemetry_gap_evidence=dict(
            horizontal_summary.get("observed_telemetry_gap_evidence") or {}
        ),
        telemetry_freshness_report=dict(
            horizontal_summary.get("telemetry_freshness_report") or {}
        ),
        mavlink_link_degradation_profile=dict(
            horizontal_summary.get("mavlink_link_degradation_profile") or {}
        ),
        mavlink_link_degradation_capability_matrix=dict(
            horizontal_summary.get("mavlink_link_degradation_capability_matrix") or {}
        ),
        mavlink_link_degradation_application=dict(
            horizontal_summary.get("mavlink_link_degradation_application") or {}
        ),
        observed_mavlink_gap_evidence=dict(
            horizontal_summary.get("observed_mavlink_gap_evidence") or {}
        ),
        terrain_world_readback=dict(
            horizontal_summary.get("terrain_world_readback") or {}
        ),
        payload_release_observed=bool(
            horizontal_summary.get("payload_release_observed")
        ),
        payload_release_event_source=str(
            horizontal_summary.get("payload_release_event_source") or ""
        ),
        same_gateway_execution_run_observed=True,
        mission_items_bound_to_gateway_receipt=True,
        live_route_target_bound_to_gateway_receipt=True,
        live_route_target_bound_to_delivery_contract=True,
        hardware_target_allowed=False,
        physical_execution_invoked=False,
        observed_at=_utc(observed_at),
        metadata={
            "source": "px4_gazebo_mission_designer_sitl_live_flight_run",
            "preexisting_summary_input_rejected": True,
            "horizontal_route_artifact_dir": artifact_dir,
            "horizontal_summary_sha256": summary_sha256,
            "live_target_binding_sha256": target_binding_hash,
            "flight_path_sample_count": len(flight_path_profile),
            "flight_path_trace_path": str(Path(artifact_dir) / "pose_samples.jsonl"),
        },
    )


def stamp_mission_designer_live_sitl_horizontal_summary(
    *,
    task: Mapping[str, Any],
    horizontal_summary: Mapping[str, Any],
    live_run_id: str | None = None,
) -> dict[str, Any]:
    """Stamp a fresh horizontal-route summary with Gateway execution refs.

    The horizontal route runner remains a generic PX4/Gazebo smoke. This stamp
    binds the observed route output to the Gateway task, upload receipt, Mission
    Designer delivery contract, and route-plan target before the live-run
    artifact is built.
    """

    artifacts_value = task.get("artifacts")
    if not isinstance(artifacts_value, Mapping):
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL summary stamp requires task artifacts"
        )
    artifacts = artifacts_value
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL summary stamp requires task id"
        )
    execution_request = PX4GazeboMissionDesignerSITLExecutionRequest.model_validate(
        _artifact(artifacts, "px4_gazebo_mission_designer_sitl_execution_request")
    )
    contract = DeliveryMissionContract.model_validate(
        _artifact(artifacts, "delivery_mission_contract")
    )
    receipt = PX4GazeboSITLMissionUploadReceipt.model_validate(
        _artifact(artifacts, "px4_gazebo_sitl_mission_upload_receipt")
    )
    if receipt.upload_status is not PX4GazeboSITLMissionUploadStatus.UPLOADED:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL summary stamp requires uploaded receipt"
        )
    if receipt.mission_ack_observed is not True or receipt.mission_ack_type != 0:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL summary stamp requires accepted upload ACK"
        )
    summary = dict(horizontal_summary)
    target_binding = build_mission_designer_sitl_live_target_binding(
        delivery_mission_contract=contract,
        mission_upload_receipt=receipt,
        horizontal_summary=summary,
    )
    item_binding = mission_designer_sitl_mission_item_binding_sha256(
        receipt.mission_items
    )
    target_binding_payload = target_binding.model_dump(mode="json")
    target_binding_hash = mission_designer_sitl_live_target_binding_sha256(
        target_binding
    )
    resolved_live_run_id = live_run_id or _stable_id(
        "mission_designer_live_sitl_run",
        {
            "task_id": task_id,
            "receipt_id": receipt.receipt_id,
            "artifact_dir": str(summary.get("artifact_dir") or ""),
        },
    )
    summary.update(
        {
            "mission_designer_live_sitl_run_source": (
                MISSION_DESIGNER_LIVE_SITL_RUN_SOURCE
            ),
            "mission_designer_live_sitl_run_id": resolved_live_run_id,
            "mission_designer_task_id": task_id,
            "mission_designer_execution_request_ref": _artifact_ref(
                "px4_gazebo_mission_designer_sitl_execution_request",
                execution_request.execution_request_id,
            ),
            "delivery_mission_contract_ref": _artifact_ref(
                "delivery_mission_contract", contract.contract_id
            ),
            "px4_gazebo_sitl_mission_upload_receipt_ref": _artifact_ref(
                "px4_gazebo_sitl_mission_upload_receipt", receipt.receipt_id
            ),
            "preupload_mission_performed": True,
            "preupload_mission_ack_observed": True,
            "preupload_mission_ack_type": receipt.mission_ack_type,
            "preupload_mission_request_sequences": list(
                receipt.mission_request_sequences
            ),
            "mission_item_binding_sha256": item_binding,
            "mission_designer_live_target_binding": target_binding_payload,
            "mission_designer_live_target_binding_sha256": target_binding_hash,
            "same_gateway_execution_run_observed": True,
        }
    )
    artifact_dir_value = str(summary.get("artifact_dir") or "").strip()
    if not artifact_dir_value:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL summary stamp requires artifact dir"
        )
    summary_path = Path(artifact_dir_value).expanduser()
    summary_file = summary_path / "summary.json"
    if not summary_file.exists():
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL summary stamp requires summary.json"
        )
    summary_file.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_gz_coupler_plugin_so(root: Path) -> Path | None:
    """Resolve the MotorLoadBatteryCoupler .so to mount into the gz runtime.

    Precedence: an explicit operator override env wins; otherwise the canonical
    gitignored build output under the plugin directory. Returns ``None`` when no
    built .so exists so the caller can leave the coupler envs unset and let the
    run fall back to PX4's own SITL battery_status estimate -- truthfully, with
    no silent injection of a plugin reference that cannot load.
    """

    override = os.getenv(
        MISSIONOS_AUTO_MISSION_GZ_COUPLER_PLUGIN_SO_ENV, ""
    ).strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None
    default = root / MISSIONOS_AUTO_MISSION_GZ_COUPLER_PLUGIN_SO_DEFAULT
    return default if default.is_file() else None


def _latest_horizontal_summary(artifact_root: Path) -> dict[str, Any]:
    candidates = [
        path
        for path in artifact_root.glob("horizontal_route_*/summary.json")
        if path.is_file()
    ]
    if not candidates:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL runner did not produce summary.json"
        )
    summary_path = max(candidates, key=lambda path: path.stat().st_mtime)
    return json.loads(summary_path.read_text())


def _coordinate_route_realism_env(task: Mapping[str, Any]) -> dict[str, str]:
    artifacts = task.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, Mapping) else {}
    keys = {
        "wind_speed_mps": MISSION_DESIGNER_REALISM_WIND_MEAN_MPS_ENV,
        "wind_direction_deg": MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG_ENV,
        "wind_gust_mps": MISSION_DESIGNER_REALISM_WIND_GUST_MPS_ENV,
        "wind_variance": MISSION_DESIGNER_REALISM_WIND_VARIANCE_ENV,
        "temperature_c": MISSION_DESIGNER_REALISM_TEMPERATURE_C_ENV,
        "pressure_hpa": MISSION_DESIGNER_REALISM_PRESSURE_HPA_ENV,
        "precipitation_mm_per_hour": (
            MISSION_DESIGNER_REALISM_PRECIPITATION_MM_PER_HOUR_ENV
        ),
        "rain_visual_mode": MISSION_DESIGNER_REALISM_RAIN_VISUAL_MODE_ENV,
        "rain_battery_drain_factor": (
            MISSION_DESIGNER_REALISM_RAIN_BATTERY_DRAIN_FACTOR_ENV
        ),
        "rain_sensor_degradation_factor": (
            MISSION_DESIGNER_REALISM_RAIN_SENSOR_DEGRADATION_FACTOR_ENV
        ),
        "rain_landing_risk_factor": (
            MISSION_DESIGNER_REALISM_RAIN_LANDING_RISK_FACTOR_ENV
        ),
        "thermal_battery_drain_factor": (
            MISSION_DESIGNER_REALISM_THERMAL_BATTERY_DRAIN_FACTOR_ENV
        ),
        "thermal_motor_derate_factor": (
            MISSION_DESIGNER_REALISM_THERMAL_MOTOR_DERATE_FACTOR_ENV
        ),
        "payload_weight_kg": MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG_ENV,
        "battery_scenario": MISSION_DESIGNER_REALISM_BATTERY_SCENARIO_ENV,
        "battery_remaining_percent": MISSION_DESIGNER_REALISM_BATTERY_REMAINING_PERCENT_ENV,
        "sensor_failure_component": MISSION_DESIGNER_REALISM_SENSOR_FAILURE_COMPONENT_ENV,
        "sensor_failure_type": MISSION_DESIGNER_REALISM_SENSOR_FAILURE_TYPE_ENV,
        "landing_zone_blocked": MISSION_DESIGNER_REALISM_LANDING_ZONE_BLOCKED_ENV,
        "visibility_mode": MISSION_DESIGNER_REALISM_VISIBILITY_MODE_ENV,
        "no_fly_zone_marker": MISSION_DESIGNER_REALISM_NO_FLY_ZONE_MARKER_ENV,
        "traffic_conflict_marker": MISSION_DESIGNER_REALISM_TRAFFIC_CONFLICT_MARKER_ENV,
        "alternate_landing_marker": MISSION_DESIGNER_REALISM_ALTERNATE_LANDING_MARKER_ENV,
        "moving_actor_marker": MISSION_DESIGNER_REALISM_MOVING_ACTOR_MARKER_ENV,
        "multi_drone_conflict_probe": MISSION_DESIGNER_REALISM_MULTI_DRONE_CONFLICT_PROBE_ENV,
        "telemetry_dropout_mode": MISSION_DESIGNER_REALISM_TELEMETRY_DROPOUT_MODE_ENV,
        "mavlink_link_degradation_mode": MISSION_DESIGNER_REALISM_MAVLINK_LINK_DEGRADATION_MODE_ENV,
    }
    env: dict[str, str] = {}
    for source_key, env_key in keys.items():
        value = route.get(source_key)
        if source_key in {
            "wind_direction_deg",
            "wind_gust_mps",
            "wind_variance",
        } and route.get("wind_speed_mps") in (None, ""):
            continue
        if value not in (None, ""):
            env[env_key] = str(value)
    return env


def _coordinate_route_with_explicit_realism_env(
    route: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind explicit runtime obstacle realism into the route artifact."""

    resolved = dict(route)
    raw = os.getenv(MISSION_DESIGNER_REALISM_LANDING_ZONE_BLOCKED_ENV)
    if "landing_zone_blocked" in resolved or raw is None:
        return resolved
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        resolved["landing_zone_blocked"] = True
    elif normalized in {"0", "false", "no", "off"}:
        resolved["landing_zone_blocked"] = False
    elif normalized:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"{MISSION_DESIGNER_REALISM_LANDING_ZONE_BLOCKED_ENV} must be a "
            "boolean opt-in value"
        )
    return resolved


def _mission_designer_terrain_world_env(task: Mapping[str, Any]) -> dict[str, str]:
    artifacts = task.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    world = artifacts.get("gazebo_world_artifact")
    world = world if isinstance(world, Mapping) else {}
    if not world:
        return {}
    world_path_value = str(world.get("world_file_path_or_artifact_uri") or "").strip()
    world_sha = str(world.get("world_file_sha256") or world.get("sha256") or "").strip()
    if not world_path_value or not world_sha:
        return {}
    root = _repo_root()
    world_path = Path(world_path_value).expanduser()
    if not world_path.is_absolute():
        world_path = root / world_path
    if not world_path.exists():
        return {}

    dem = artifacts.get("terrain_dem_source_snapshot")
    dem = dem if isinstance(dem, Mapping) else {}
    mission_item = artifacts.get("digital_twin_px4_mission_item_candidate")
    mission_item = mission_item if isinstance(mission_item, Mapping) else {}
    source_ref = (
        "gazebo_world_artifact:" + str(world.get("world_artifact_id") or "").strip()
    )
    if source_ref == "gazebo_world_artifact:":
        source_ref = ""
    vertical_reference = "takeoff_agl_rebased_visual_terrain"
    if mission_item.get("takeoff_terrain_elevation_m") not in (None, ""):
        vertical_reference = "takeoff_agl_rebased_from_terrain_sample"
    return {
        MISSION_DESIGNER_LIVE_SITL_TERRAIN_WORLD_SDF_ENV: str(world_path),
        MISSION_DESIGNER_LIVE_SITL_TERRAIN_WORLD_SHA256_ENV: world_sha,
        MISSION_DESIGNER_LIVE_SITL_TERRAIN_WORLD_SOURCE_REF_ENV: source_ref,
        MISSION_DESIGNER_LIVE_SITL_TERRAIN_PROVIDER_STATUS_ENV: str(
            dem.get("provider_response_status") or ""
        ),
        MISSION_DESIGNER_LIVE_SITL_TERRAIN_SAMPLING_MODE_ENV: str(
            mission_item.get("terrain_sampling_mode") or ""
        ),
        MISSION_DESIGNER_LIVE_SITL_TERRAIN_VERTICAL_REFERENCE_ENV: vertical_reference,
        MISSION_DESIGNER_LIVE_SITL_TERRAIN_COLLISION_MODE_ENV: "visual_only_horizontal_route_runtime",
    }


def _latest_horizontal_pose_trace(artifact_root: Path) -> Path | None:
    candidates = [
        path
        for path in artifact_root.glob("horizontal_route_*/pose_samples.jsonl")
        if path.is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _tail_text(path: Path, *, limit: int = 12000) -> str:
    if not path.exists():
        return ""
    return path.read_text(errors="replace")[-limit:]


def _runner_failure_digest(stdout_tail: str, stderr_tail: str) -> str:
    for text in (stderr_tail, stdout_tail):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in reversed(lines):
            if "samples=" in line:
                return line.split("samples=", 1)[0].rstrip(" ;")[:360]
            if "{'x':" in line or '"x":' in line or len(line) > 360:
                continue
            return line[:360]
    return "runner exited without concise failure line; see stdout/stderr logs"


def _persist_live_telemetry_snapshot(
    *,
    task_id: str,
    artifact_root: Path,
    task_store_factory: Callable[[], TaskStore] | None,
    min_sample_count: int = 0,
) -> int:
    pose_path = _latest_horizontal_pose_trace(artifact_root)
    if pose_path is None:
        return 0
    profile = _load_horizontal_route_flight_path_profile(str(pose_path.parent))
    if not profile:
        return 0
    if len(profile) <= min_sample_count:
        return len(profile)
    store = (task_store_factory or get_task_store)()
    latest_battery = next(
        (
            sample
            for sample in reversed(profile)
            if sample.get("battery_status_observed") is True
        ),
        {},
    )
    snapshot = {
        "schema_version": "mission_designer_live_telemetry_snapshot.v1",
        "snapshot_status": "running",
        "task_ref": _artifact_ref("task", task_id),
        "flight_path_trace_path": str(pose_path),
        "flight_path_profile": profile,
        "sample_count": len(profile),
        "latest_sample": profile[-1],
        "battery_status_observed": bool(latest_battery),
        "battery_state_source": latest_battery.get(
            "battery_state_source", "px4-listener:battery_status"
        )
        if latest_battery
        else "px4-listener:battery_status_not_observed",
        "battery_remaining_percent": latest_battery.get("battery_remaining_percent"),
        "battery_warning": latest_battery.get("battery_warning"),
        "battery_voltage_v": latest_battery.get("battery_voltage_v"),
        "battery_current_a": latest_battery.get("battery_current_a"),
        "battery_connected": latest_battery.get("battery_connected"),
        "delivery_completion_claimed": False,
        "read_only": True,
        "observed_at": _utc().isoformat(),
    }
    store.update(
        task_id,
        artifacts={"mission_designer_live_telemetry_snapshot": snapshot},
        metadata={
            "mission_designer_live_telemetry_snapshot_attached": True,
            "mission_designer_live_telemetry_sample_count": len(profile),
        },
    )
    battery_remaining = snapshot.get("battery_remaining_percent")
    if isinstance(battery_remaining, (int, float)) and not isinstance(
        battery_remaining, bool
    ):
        _attach_px4_reflex_watch(
            task_id=task_id,
            battery_remaining_percent=float(battery_remaining),
            store=store,
        )
    return len(profile)


def _attach_px4_reflex_watch(
    *,
    task_id: str,
    battery_remaining_percent: float,
    store: TaskStore,
    dispatcher_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the reflex budget against live battery; dispatch RTL if due.

    Called once per telemetry poll during a live SITL flight. Record-only
    unless MISSIONOS_PX4_REFLEX_RTL_ENABLED=1 supplies the env-gated
    dispatcher, and a run dispatches at most once — a prior dispatched watch
    on the task makes later exhausted polls record already_dispatched.
    """

    from src.runtime.px4_recovery_reflex_dispatch import (
        default_px4_reflex_dispatcher_from_env,
        watch_px4_recovery_reflex_from_battery,
    )

    factory = dispatcher_factory or default_px4_reflex_dispatcher_from_env
    existing_task = store.get(task_id) or {}
    existing_artifacts = existing_task.get("artifacts")
    existing_watch = (
        existing_artifacts.get("px4_recovery_reflex_watch")
        if isinstance(existing_artifacts, Mapping)
        else None
    )
    already_dispatched = (
        isinstance(existing_watch, Mapping)
        and existing_watch.get("watch_status") == "dispatched"
    )
    watch = watch_px4_recovery_reflex_from_battery(
        battery_remaining_percent=battery_remaining_percent,
        dispatcher=factory(),
        already_dispatched=already_dispatched,
    )
    if already_dispatched:
        # Keep the dispatched record authoritative; only refresh the reflex
        # assessment alongside it.
        watch = {
            **dict(existing_watch),
            "latest_reflex": watch["reflex"],
            "watch_status": "dispatched",
        }
    store.update(
        task_id,
        artifacts={"px4_recovery_reflex_watch": watch},
    )
    return watch


def _horizontal_route_runtime_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_MODULE,
    ]


def run_px4_gazebo_horizontal_route_live_summary(
    *,
    task_id: str,
    artifact_root: Path | None = None,
    timeout_seconds: float = 900.0,
    task_store_factory: Callable[[], TaskStore] | None = None,
    task: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the real horizontal-route runtime and return its observed summary."""

    root = _repo_root()
    resolved_artifact_root = (
        artifact_root or root / "output" / "mission_designer_live_sitl_runs" / task_id
    )
    resolved_artifact_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env[MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_OPT_IN_ENV] = "1"
    env[MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_ARTIFACT_ROOT_ENV] = str(
        resolved_artifact_root
    )
    # Gateway already performed and persisted the mission upload receipt. The
    # live flight runner must not silently upload a second, script-local mission.
    env.pop(MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_PREUPLOAD_ENV, None)
    env[MISSION_DESIGNER_LIVE_SITL_HORIZONTAL_ROUTE_PAYLOAD_RELEASE_MODEL_ENV] = "1"
    if task is not None:
        env.update(_coordinate_route_realism_env(task))
        env.update(_mission_designer_terrain_world_env(task))
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(root)
        if not existing_pythonpath
        else os.pathsep.join([str(root), existing_pythonpath])
    )
    command = _horizontal_route_runtime_command()
    stdout_log = resolved_artifact_root / f"{task_id}_horizontal_stdout.log"
    stderr_log = resolved_artifact_root / f"{task_id}_horizontal_stderr.log"
    with stdout_log.open("w") as stdout_file, stderr_log.open("w") as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            text=True,
            stdout=stdout_file,
            stderr=stderr_file,
        )
        deadline = time.monotonic() + timeout_seconds
        last_sample_count = 0
        while process.poll() is None:
            if time.monotonic() > deadline:
                process.kill()
                process.wait(timeout=5)
                stderr_tail = _tail_text(stderr_log)
                stdout_tail = _tail_text(stdout_log)
                digest = _runner_failure_digest(stdout_tail, stderr_tail)
                raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                    "Mission Designer live SITL horizontal route runner timed out: "
                    f"reason={digest}; stdout_log={stdout_log}; stderr_log={stderr_log}"
                )
            sample_count = _persist_live_telemetry_snapshot(
                task_id=task_id,
                artifact_root=resolved_artifact_root,
                task_store_factory=task_store_factory,
                min_sample_count=last_sample_count,
            )
            last_sample_count = max(last_sample_count, sample_count)
            time.sleep(1.0)
        process.wait(timeout=5)
        _persist_live_telemetry_snapshot(
            task_id=task_id,
            artifact_root=resolved_artifact_root,
            task_store_factory=task_store_factory,
            min_sample_count=last_sample_count,
        )
    if process.returncode != 0:
        stderr_tail = _tail_text(stderr_log)
        stdout_tail = _tail_text(stdout_log)
        digest = _runner_failure_digest(stdout_tail, stderr_tail)
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "Mission Designer live SITL horizontal route runner failed: "
            f"reason={digest}; stdout_log={stdout_log}; stderr_log={stderr_log}"
        )
    summary = _latest_horizontal_summary(resolved_artifact_root)
    cleanup = dict(summary.get("scenario_cleanup_receipt") or {})
    if cleanup:
        cleanup["cleanup_status"] = "isolated_container_teardown_observed"
        cleanup["observed_at"] = _utc().isoformat()
        summary["scenario_cleanup_receipt"] = cleanup
        summary_path = Path(str(summary["artifact_dir"])) / "summary.json"
        if summary_path.exists():
            summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def _missionos_auto_coordinate_route(task: Mapping[str, Any]) -> dict[str, Any]:
    artifacts = task.get("artifacts") or {}
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    route = artifacts.get("mission_designer_coordinate_pair_route")
    if not isinstance(route, Mapping):
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "MissionOS AUTO GUI dispatch requires operator coordinate route artifact"
        )
    required = (
        "takeoff_latitude",
        "takeoff_longitude",
        "dropoff_latitude",
        "dropoff_longitude",
        "dropoff_roof_height_agl_m",
    )
    missing = [key for key in required if key not in route]
    if missing:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "MissionOS AUTO GUI dispatch route missing fields: " + ", ".join(missing)
        )
    return dict(route)


def _latest_auto_mission_summary(root: Path) -> dict[str, Any]:
    candidates = sorted(
        root.glob("*/summary.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"MissionOS AUTO GUI dispatch produced no summary under {root}"
        )
    return json.loads(candidates[0].read_text(encoding="utf-8"))


def _auto_gate(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def _auto_terminal_blocked_reasons(
    runtime_summary: Mapping[str, Any],
    waypoint_gate: Mapping[str, Any],
    dropoff_gate: Mapping[str, Any],
    sitl_delivery_gate: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []

    def _append(reason: Any) -> None:
        text = str(reason or "").strip()
        if text and text not in reasons:
            reasons.append(text)

    for source in (runtime_summary, waypoint_gate, dropoff_gate, sitl_delivery_gate):
        values = source.get("blocked_reasons")
        if isinstance(values, (list, tuple)):
            for item in values:
                _append(item)

    if waypoint_gate.get("route_completed_claimed") is not True:
        _append("route_completion_gate_phase4_pending")
    if dropoff_gate.get("dropoff_verified") is not True:
        _append("dropoff_verification_phase5_pending")
    if sitl_delivery_gate.get("sitl_delivery_claimed") is not True:
        _append("delivery_claim_gate_phase6_pending")
    _append(runtime_summary.get("recovery_incomplete_reason"))
    _append(runtime_summary.get("abort_reason"))
    return reasons


def _auto_gui_dispatch_terminal_status(
    runtime_summary: Mapping[str, Any],
    waypoint_gate: Mapping[str, Any],
    dropoff_gate: Mapping[str, Any],
    sitl_delivery_gate: Mapping[str, Any],
) -> tuple[str, list[str]]:
    route_completed = waypoint_gate.get("route_completed_claimed") is True
    dropoff_verified = dropoff_gate.get("dropoff_verified") is True
    sitl_delivery_claimed = sitl_delivery_gate.get("sitl_delivery_claimed") is True
    if route_completed and dropoff_verified and sitl_delivery_claimed:
        return "completed", []
    return (
        "blocked",
        _auto_terminal_blocked_reasons(
            runtime_summary,
            waypoint_gate,
            dropoff_gate,
            sitl_delivery_gate,
        ),
    )


def _auto_replay_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _auto_replay_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _auto_replay_phase(
    row: Mapping[str, Any],
    *,
    route_waypoint_seq_end: int | None,
    dropoff_dwell_seq: int | None,
) -> str:
    current_seq = _auto_replay_int(row.get("mission_current_seq"))
    reached_seq = _auto_replay_int(row.get("mission_reached_seq"))
    altitude_m = _auto_replay_float(row.get("global_altitude_m"))
    if (
        dropoff_dwell_seq is not None
        and current_seq is not None
        and current_seq >= dropoff_dwell_seq
    ):
        return "dropoff_hover"
    if (
        route_waypoint_seq_end is not None
        and reached_seq is not None
        and reached_seq >= route_waypoint_seq_end
    ):
        return "dropoff_target"
    if current_seq is not None and current_seq > 0:
        return "route"
    if altitude_m is not None and altitude_m > 1.0:
        return "takeoff"
    return "prepared"


def _auto_replay_decimate(
    samples: Sequence[Mapping[str, Any]],
    *,
    max_samples: int,
) -> list[dict[str, Any]]:
    if len(samples) <= max_samples:
        return [dict(sample) for sample in samples]
    step = max(1, len(samples) // max_samples)
    reduced = [dict(sample) for sample in samples[::step][: max_samples - 1]]
    if reduced[-1] != samples[-1]:
        reduced.append(dict(samples[-1]))
    return reduced


def _build_auto_mission_runtime_replay_artifact(
    *,
    task_id: str,
    payload: Mapping[str, Any],
    observed_at: datetime | None = None,
    max_samples: int = 240,
) -> dict[str, Any]:
    """Build a bounded read-only 3D replay artifact from AUTO pose telemetry."""

    artifact_dir_value = payload.get("artifact_dir")
    if not artifact_dir_value:
        return {}
    artifact_dir = Path(str(artifact_dir_value)).expanduser()
    runtime_summary = _auto_gate(payload, "summary")
    waypoint_gate = _auto_gate(payload, "waypoint_gate")
    dropoff_gate = _auto_gate(payload, "dropoff_gate")
    sitl_delivery_gate = _auto_gate(payload, "sitl_delivery_gate")
    payload_release_sim_gate = _auto_gate(payload, "payload_release_sim_gate")
    compilation = _auto_gate(payload, "compilation")

    pose_path_value = (
        runtime_summary.get("local_ned_pose_samples_path")
        or runtime_summary.get("pose_samples_path")
        or "auto_mission_pose_samples.jsonl"
    )
    pose_path = Path(str(pose_path_value)).expanduser()
    if not pose_path.is_absolute():
        artifact_relative_path = artifact_dir / pose_path
        repo_relative_path = _repo_root() / pose_path
        pose_path = (
            artifact_relative_path
            if artifact_relative_path.is_file()
            else repo_relative_path
        )
    if not pose_path.is_file():
        return {}

    route_waypoint_seq_end = _auto_replay_int(
        waypoint_gate.get("route_waypoint_seq_end")
        or runtime_summary.get("route_waypoint_seq_end")
    )
    dropoff_dwell_seq = _auto_replay_int(
        compilation.get("dropoff_dwell_mission_seq")
        or runtime_summary.get("dropoff_dwell_mission_seq")
    )
    raw_samples: list[dict[str, Any]] = []
    for line in pose_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping):
            continue
        lat = _auto_replay_float(row.get("global_latitude_deg"))
        lon = _auto_replay_float(row.get("global_longitude_deg"))
        local_x = _auto_replay_float(row.get("local_x_m"))
        local_y = _auto_replay_float(row.get("local_y_m"))
        if lat is None or lon is None or local_x is None or local_y is None:
            continue
        altitude = _auto_replay_float(row.get("global_altitude_m"))
        local_z = _auto_replay_float(row.get("local_z_m"))
        if altitude is None and local_z is not None:
            altitude = -local_z
        sample: dict[str, Any] = {
            "sample_index": _auto_replay_int(row.get("sample_index"))
            or len(raw_samples),
            "phase": _auto_replay_phase(
                row,
                route_waypoint_seq_end=route_waypoint_seq_end,
                dropoff_dwell_seq=dropoff_dwell_seq,
            ),
            "latitude_deg": lat,
            "longitude_deg": lon,
            "relative_alt_m": altitude if altitude is not None else 0.0,
            "local_x_m": local_x,
            "local_y_m": local_y,
            "local_z_m": local_z,
            "horizontal_progress_m": (local_x * local_x + local_y * local_y) ** 0.5,
            "elapsed_s": row.get("elapsed_seconds"),
            "seq_reached": row.get("mission_reached_seq"),
            "mission_current_seq": row.get("mission_current_seq"),
            "battery_remaining_percent": row.get("battery_remaining_percent"),
            "battery_warning": row.get("battery_warning"),
            "battery_status_observed": row.get("battery_status_observed"),
            "heartbeat_observed": row.get("heartbeat_observed"),
        }
        raw_samples.append(sample)

    if not raw_samples:
        return {}
    profile = _auto_replay_decimate(raw_samples, max_samples=max_samples)
    latest_sample = profile[-1]
    latest_battery = next(
        (
            sample
            for sample in reversed(profile)
            if sample.get("battery_status_observed") is True
            or sample.get("battery_remaining_percent") is not None
        ),
        {},
    )
    return {
        "schema_version": "missionos_auto_mission_runtime_replay.v1",
        "snapshot_status": "completed",
        "task_ref": _artifact_ref("task", task_id),
        "flight_path_source": "missionos_auto_mission_runtime_pose_log",
        "flight_path_status": "payload_release_observed_sim"
        if payload_release_sim_gate.get("payload_release_observed_sim") is True
        else "completed",
        "flight_path_replay_kind": "auto_mission",
        "flight_path_frame": "wgs84",
        "flight_path_trace_path": str(pose_path),
        "flight_path_profile": profile,
        "sample_count": len(profile),
        "raw_sample_count": len(raw_samples),
        "latest_sample": latest_sample,
        "dropoff_latitude_deg": dropoff_gate.get("dropoff_latitude_deg"),
        "dropoff_longitude_deg": dropoff_gate.get("dropoff_longitude_deg"),
        "horizontal_progress_m": runtime_summary.get("observed_progress_m")
        or latest_sample.get("horizontal_progress_m"),
        "route_completed_claimed": bool(waypoint_gate.get("route_completed_claimed")),
        "dropoff_verified": bool(dropoff_gate.get("dropoff_verified")),
        "sitl_delivery_claimed": bool(sitl_delivery_gate.get("sitl_delivery_claimed")),
        "payload_release_observed_sim": bool(
            payload_release_sim_gate.get("payload_release_observed_sim")
        ),
        "delivery_completion_claimed": False,
        "physical_delivery_verified": False,
        "read_only": True,
        "battery_status_observed": bool(latest_battery),
        "battery_remaining_percent": latest_battery.get("battery_remaining_percent"),
        "battery_warning": latest_battery.get("battery_warning"),
        "observed_at": _utc(observed_at).isoformat(),
    }


def _auto_gui_dispatch_subprocess_timeout_seconds(
    *,
    compilation: MissionOSAutoMissionCompilation,
    requested_timeout_seconds: float,
) -> float:
    """Keep the Gateway wrapper alive for full-route monitor plus recovery."""

    route_derived_timeout = (
        float(compilation.timeout_seconds) + float(compilation.timeout_seconds) + 60.0
    )
    return max(float(requested_timeout_seconds), route_derived_timeout)


def _missionos_auto_operator_recovery_request_container_path(task_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in task_id)
    safe = safe or "unknown_task"
    return f"/tmp/missionos_auto_operator_recovery_request_{safe}.json"


def _latest_auto_running_snapshot_path(artifact_root: Path) -> Path | None:
    """Locate the most recent in-flight snapshot the AUTO probe is overwriting.

    The probe creates a timestamped run directory under the artifact root and
    overwrites ``running_snapshot.json`` once per monitor sample while the long
    AUTO route is still flying. Reading the newest file lets the Gateway surface
    progress/position/battery telemetry before the final verifier summary exists.
    """

    candidates = sorted(
        artifact_root.glob("*/running_snapshot.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _auto_snapshot_number(*values: Any) -> float | int | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            numeric = float(value)
        elif isinstance(value, str):
            try:
                numeric = float(value)
            except ValueError:
                continue
        else:
            continue
        if not (numeric == numeric and numeric not in {float("inf"), float("-inf")}):
            continue
        return int(numeric) if numeric.is_integer() else numeric
    return None


def _auto_runtime_planned_route_m(artifacts: Mapping[str, Any]) -> float | None:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, Mapping) else {}
    compilation = artifacts.get("missionos_auto_mission_compilation")
    compilation = compilation if isinstance(compilation, Mapping) else {}
    for value in (
        route.get("derived_route_distance_m"),
        route.get("planned_route_m"),
        compilation.get("planned_route_m"),
    ):
        resolved = _auto_snapshot_number(value)
        if resolved is not None and resolved > 0:
            return float(resolved)
    return None


def _auto_runtime_battery_endurance_projection(
    snapshot: Mapping[str, Any],
    *,
    planned_route_m: float | None,
    reserve_percent: float = 15.0,
) -> dict[str, Any]:
    progress_m = _auto_snapshot_number(snapshot.get("progress_m"))
    battery_remaining = _auto_snapshot_number(snapshot.get("battery_remaining_percent"))
    battery_delta = _auto_snapshot_number(snapshot.get("battery_remaining_delta_percent"))
    if planned_route_m is None or progress_m is None or battery_remaining is None:
        return {
            "projection_status": "insufficient_observation",
            "battery_reserve_required_percent": reserve_percent,
        }
    remaining_route_m = max(0.0, float(planned_route_m) - float(progress_m))
    consumed_percent = abs(float(battery_delta)) if battery_delta is not None else None
    minimum_progress_m = min(
        50.0,
        max(10.0, float(planned_route_m) * 0.05),
    )
    if (
        consumed_percent is None
        or consumed_percent <= 0
        or progress_m < minimum_progress_m
    ):
        return {
            "projection_status": "insufficient_observation",
            "projection_reason": (
                "battery_burn_baseline_distance_insufficient"
                if progress_m < minimum_progress_m
                else "battery_consumption_not_observed"
            ),
            "planned_route_m": round(float(planned_route_m), 3),
            "progress_m": round(float(progress_m), 3),
            "minimum_progress_m": round(minimum_progress_m, 3),
            "remaining_route_m": round(remaining_route_m, 3),
            "battery_remaining_percent": round(float(battery_remaining), 3),
            "battery_reserve_required_percent": reserve_percent,
        }
    burn_percent_per_m = consumed_percent / float(progress_m)
    projected_required_percent = burn_percent_per_m * remaining_route_m
    projected_arrival_percent = float(battery_remaining) - projected_required_percent
    projected_margin_percent = projected_arrival_percent - reserve_percent
    return {
        "projection_status": "computed",
        "planned_route_m": round(float(planned_route_m), 3),
        "progress_m": round(float(progress_m), 3),
        "remaining_route_m": round(remaining_route_m, 3),
        "battery_consumed_percent": round(consumed_percent, 3),
        "battery_remaining_percent": round(float(battery_remaining), 3),
        "battery_burn_percent_per_km": round(burn_percent_per_m * 1000.0, 3),
        "projected_battery_required_percent": round(projected_required_percent, 3),
        "projected_arrival_battery_percent": round(projected_arrival_percent, 3),
        "battery_reserve_required_percent": reserve_percent,
        "projected_reserve_margin_percent": round(projected_margin_percent, 3),
        "projected_insufficient_for_route": projected_margin_percent < 0.0,
    }


def _auto_runtime_battery_return_home_projection(
    snapshot: Mapping[str, Any],
    *,
    reserve_percent: float = 15.0,
) -> dict[str, Any]:
    progress_m = _auto_snapshot_number(snapshot.get("progress_m"))
    distance_to_home_m = _auto_snapshot_number(snapshot.get("distance_to_home_m"))
    battery_remaining = _auto_snapshot_number(snapshot.get("battery_remaining_percent"))
    battery_delta = _auto_snapshot_number(snapshot.get("battery_remaining_delta_percent"))
    if (
        progress_m is None
        or distance_to_home_m is None
        or battery_remaining is None
    ):
        return {
            "projection_status": "insufficient_observation",
            "battery_reserve_required_percent": reserve_percent,
        }
    consumed_percent = abs(float(battery_delta)) if battery_delta is not None else None
    if consumed_percent is None or consumed_percent <= 0 or progress_m <= 0:
        return {
            "projection_status": "insufficient_observation",
            "progress_m": round(float(progress_m), 3),
            "distance_to_home_m": round(float(distance_to_home_m), 3),
            "battery_remaining_percent": round(float(battery_remaining), 3),
            "battery_reserve_required_percent": reserve_percent,
        }
    burn_percent_per_m = consumed_percent / float(progress_m)
    projected_required_percent = burn_percent_per_m * float(distance_to_home_m)
    projected_arrival_percent = float(battery_remaining) - projected_required_percent
    projected_margin_percent = projected_arrival_percent - reserve_percent
    return {
        "projection_status": "computed",
        "progress_m": round(float(progress_m), 3),
        "distance_to_home_m": round(float(distance_to_home_m), 3),
        "battery_consumed_percent": round(consumed_percent, 3),
        "battery_remaining_percent": round(float(battery_remaining), 3),
        "battery_burn_percent_per_km": round(burn_percent_per_m * 1000.0, 3),
        "projected_return_battery_required_percent": round(
            projected_required_percent, 3
        ),
        "projected_return_arrival_battery_percent": round(
            projected_arrival_percent, 3
        ),
        "battery_reserve_required_percent": reserve_percent,
        "projected_return_reserve_margin_percent": round(projected_margin_percent, 3),
        "projected_insufficient_for_return_home": projected_margin_percent < 0.0,
    }


def _auto_runtime_terrain_clearance_projection(
    snapshot: Mapping[str, Any],
    *,
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    compilation = artifacts.get("missionos_auto_mission_compilation")
    compilation = compilation if isinstance(compilation, Mapping) else {}
    profile = compilation.get("terrain_clearance_profile")
    if not isinstance(profile, list | tuple) or not profile:
        return {"projection_status": "not_configured"}
    planned_route_m = _auto_runtime_planned_route_m(artifacts)
    progress_m = _auto_snapshot_number(snapshot.get("progress_m"))
    altitude_above_home_m = _auto_snapshot_number(snapshot.get("altitude_above_home_m"))
    if planned_route_m is None or progress_m is None or altitude_above_home_m is None:
        return {"projection_status": "insufficient_observation"}

    target_fraction = min(1.0, max(0.0, float(progress_m) / float(planned_route_m)))
    samples: list[tuple[float, Mapping[str, Any]]] = []
    for raw in profile:
        if not isinstance(raw, Mapping):
            continue
        fraction = _auto_snapshot_number(raw.get("fraction"))
        distance_m = _auto_snapshot_number(raw.get("distance_m"))
        if fraction is None:
            if distance_m is None or planned_route_m <= 0:
                continue
            fraction = float(distance_m) / float(planned_route_m)
        samples.append((min(1.0, max(0.0, float(fraction))), raw))
    if not samples:
        return {"projection_status": "insufficient_observation"}
    samples.sort(key=lambda item: item[0])

    if target_fraction <= samples[0][0]:
        nearest = samples[0][1]
    elif target_fraction >= samples[-1][0]:
        nearest = samples[-1][1]
    else:
        nearest = samples[-1][1]
        for left, right in zip(samples, samples[1:], strict=False):
            if left[0] <= target_fraction <= right[0]:
                nearest = left[1] if abs(target_fraction - left[0]) <= abs(right[0] - target_fraction) else right[1]
                break

    terrain_elevation_m = _auto_snapshot_number(nearest.get("terrain_elevation_m"))
    target_clearance_m = _auto_snapshot_number(
        nearest.get("target_clearance_m"),
        compilation.get("terrain_clearance_target_m"),
    )
    planned_altitude_m = _auto_snapshot_number(nearest.get("mission_altitude_m"))
    if terrain_elevation_m is None or target_clearance_m is None:
        return {"projection_status": "insufficient_observation"}
    clearance_m = float(altitude_above_home_m) - (
        float(planned_altitude_m or altitude_above_home_m) - float(target_clearance_m)
    )
    clearance_margin_m = clearance_m - float(target_clearance_m)
    clearance_below_minimum = clearance_margin_m < (
        -MISSIONOS_AUTO_TERRAIN_CLEARANCE_GRACE_M
    )
    return {
        "projection_status": "computed",
        "progress_m": round(float(progress_m), 3),
        "route_fraction": round(float(target_fraction), 6),
        "terrain_elevation_m": round(float(terrain_elevation_m), 3),
        "terrain_clearance_m": round(clearance_m, 3),
        "terrain_clearance_target_m": round(float(target_clearance_m), 3),
        "terrain_clearance_margin_m": round(clearance_margin_m, 3),
        "terrain_clearance_grace_m": MISSIONOS_AUTO_TERRAIN_CLEARANCE_GRACE_M,
        "terrain_clearance_below_minimum": clearance_below_minimum,
        "planned_mission_altitude_m": round(float(planned_altitude_m), 3)
        if planned_altitude_m is not None
        else None,
    }


def _auto_runtime_route_drift_projection(
    snapshot: Mapping[str, Any],
    *,
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, Mapping) else {}
    local_x_m = _auto_snapshot_number(snapshot.get("local_x_m"))
    local_y_m = _auto_snapshot_number(snapshot.get("local_y_m"))
    takeoff_lat = _auto_snapshot_number(route.get("takeoff_latitude"))
    takeoff_lon = _auto_snapshot_number(route.get("takeoff_longitude"))
    dropoff_lat = _auto_snapshot_number(route.get("dropoff_latitude"))
    dropoff_lon = _auto_snapshot_number(route.get("dropoff_longitude"))
    if None in (local_x_m, local_y_m, takeoff_lat, takeoff_lon, dropoff_lat, dropoff_lon):
        return {"projection_status": "insufficient_observation"}

    earth_radius_m = 6_371_000.0
    mean_lat_rad = math.radians((float(takeoff_lat) + float(dropoff_lat)) / 2.0)
    target_north_m = math.radians(float(dropoff_lat) - float(takeoff_lat)) * earth_radius_m
    target_east_m = (
        math.radians(float(dropoff_lon) - float(takeoff_lon))
        * earth_radius_m
        * math.cos(mean_lat_rad)
    )
    target_len_m = math.hypot(target_north_m, target_east_m)
    if target_len_m <= 0:
        return {"projection_status": "insufficient_observation"}

    # PX4 local position is NED: x=north, y=east. Cross-track distance is the
    # lateral deviation from the planned takeoff->dropoff line.
    position_north_m = float(local_x_m)
    position_east_m = float(local_y_m)
    along_track_m = (
        (position_north_m * target_north_m + position_east_m * target_east_m)
        / target_len_m
    )
    cross_track_m = (
        (position_north_m * target_east_m - position_east_m * target_north_m)
        / target_len_m
    )
    return {
        "projection_status": "computed",
        "target_north_m": round(target_north_m, 3),
        "target_east_m": round(target_east_m, 3),
        "planned_route_m": round(target_len_m, 3),
        "along_track_m": round(along_track_m, 3),
        "cross_track_m": round(cross_track_m, 3),
        "deviation_xy_m": round(abs(cross_track_m), 3),
    }


def _auto_runtime_obstacle_projection(
    *,
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, Mapping) else {}
    obstacle_manifest: Mapping[str, Any] = {}
    obstacle_application: Mapping[str, Any] = {}
    gazebo_obstacle_model_spawned = False
    for key in (
        "missionos_auto_mission_runtime_snapshot",
        "px4_gazebo_mission_designer_sitl_live_flight_run",
        "missionos_auto_mission_gui_dispatch_receipt",
        "missionos_auto_mission_compilation",
    ):
        payload = artifacts.get(key)
        payload = payload if isinstance(payload, Mapping) else {}
        application = payload.get("gazebo_obstacle_application")
        if isinstance(application, Mapping) and application:
            obstacle_application = application
        spawned = payload.get("gazebo_obstacle_model_spawned")
        if spawned is True:
            gazebo_obstacle_model_spawned = True
        manifest = payload.get("obstacle_manifest")
        if isinstance(manifest, Mapping) and manifest:
            obstacle_manifest = manifest
            if manifest.get("gazebo_obstacle_model_spawned") is True:
                gazebo_obstacle_model_spawned = True
            break
    if not obstacle_manifest:
        route_manifest = route.get("obstacle_manifest")
        if isinstance(route_manifest, Mapping) and route_manifest:
            obstacle_manifest = route_manifest
        elif isinstance(route.get("obstacles"), list) and route.get("obstacles"):
            obstacle_manifest = {
                "schema_version": "missionos_gazebo_obstacle_manifest.v1",
                "manifest_status": "configured",
                "source": "mission_designer_coordinate_pair_route",
                "obstacles": list(route.get("obstacles") or []),
                "building_risk_detected": True,
            }
    landing_zone_blocked = route.get("landing_zone_blocked") is True
    configured = bool(obstacle_manifest) or landing_zone_blocked
    # Route configuration is not runtime observation. Claim detection only
    # after Gazebo reports that the collision model was spawned.
    detected = configured and gazebo_obstacle_model_spawned
    return {
        "projection_status": (
            "source_backed"
            if detected
            else "configured_unobserved"
            if configured
            else "not_configured"
        ),
        "obstacle_condition_requested": configured,
        "obstacle_detected": detected,
        "building_risk_detected": bool(
            detected
            and (
                (
                    obstacle_manifest.get("building_risk_detected")
                    if obstacle_manifest
                    else False
                )
                or route.get("building_risk_detected")
                or landing_zone_blocked
            )
        ),
        "landing_zone_blocked": landing_zone_blocked,
        "obstacle_manifest": dict(obstacle_manifest),
        "gazebo_obstacle_application": dict(obstacle_application),
        "gazebo_obstacle_model_spawned": gazebo_obstacle_model_spawned,
    }


def _auto_runtime_obstacle_conflict_projection(
    *,
    snapshot: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    obstacle_projection: Mapping[str, Any],
) -> dict[str, Any]:
    """Measure route intersection and time-to-conflict for local avoidance.

    This projection does not select an action. It prevents a source-backed but
    distant destination obstacle from being treated as an immediate local
    avoidance condition while retaining the obstacle as visible route context.
    """

    if obstacle_projection.get("projection_status") != "source_backed":
        return {
            "assessment_status": "not_source_backed",
            "local_avoidance_required": False,
        }
    current_x_m = _auto_snapshot_number(snapshot.get("local_x_m"))
    current_y_m = _auto_snapshot_number(snapshot.get("local_y_m"))
    if current_x_m is None or current_y_m is None:
        return {
            "assessment_status": "position_unavailable",
            "local_avoidance_required": False,
        }
    drift = _auto_runtime_route_drift_projection(snapshot, artifacts=artifacts)
    target_x_m = _auto_snapshot_number(drift.get("target_north_m"))
    target_y_m = _auto_snapshot_number(drift.get("target_east_m"))
    if target_x_m is None or target_y_m is None:
        return {
            "assessment_status": "route_unavailable",
            "local_avoidance_required": False,
        }
    route_length_m = math.hypot(target_x_m, target_y_m)
    if route_length_m <= 1e-6:
        return {
            "assessment_status": "route_unavailable",
            "local_avoidance_required": False,
        }
    unit_x = target_x_m / route_length_m
    unit_y = target_y_m / route_length_m
    manifest = obstacle_projection.get("obstacle_manifest")
    manifest = manifest if isinstance(manifest, Mapping) else {}
    obstacles = manifest.get("obstacles")
    obstacles = obstacles if isinstance(obstacles, list) else []
    assessments: list[dict[str, Any]] = []
    ground_speed_mps = None
    velocity_x_mps = _auto_snapshot_number(snapshot.get("local_vx_mps"))
    velocity_y_mps = _auto_snapshot_number(snapshot.get("local_vy_mps"))
    if velocity_x_mps is not None and velocity_y_mps is not None:
        ground_speed_mps = math.hypot(velocity_x_mps, velocity_y_mps)
    if ground_speed_mps is None or ground_speed_mps <= 0.1:
        ground_speed_mps = _auto_snapshot_number(snapshot.get("ground_speed_mps"))
    lookahead_m = 150.0
    time_limit_s = 30.0
    for index, obstacle in enumerate(obstacles):
        if not isinstance(obstacle, Mapping):
            continue
        obstacle_x_m = _auto_snapshot_number(
            obstacle.get("x_m"), obstacle.get("local_x_m"), obstacle.get("x")
        )
        obstacle_y_m = _auto_snapshot_number(
            obstacle.get("y_m"), obstacle.get("local_y_m"), obstacle.get("y")
        )
        if obstacle_x_m is None or obstacle_y_m is None:
            continue
        relative_x_m = obstacle_x_m - current_x_m
        relative_y_m = obstacle_y_m - current_y_m
        distance_m = math.hypot(relative_x_m, relative_y_m)
        along_track_m = relative_x_m * unit_x + relative_y_m * unit_y
        cross_track_m = abs(relative_x_m * unit_y - relative_y_m * unit_x)
        obstacle_radius_m = max(
            _auto_snapshot_number(obstacle.get("size_x_m")) or 0.0,
            _auto_snapshot_number(obstacle.get("size_y_m")) or 0.0,
        ) / 2.0
        corridor_half_width_m = max(30.0, obstacle_radius_m + 20.0)
        time_to_conflict_s = (
            max(0.0, along_track_m - obstacle_radius_m) / ground_speed_mps
            if ground_speed_mps is not None and ground_speed_mps > 0.1
            else None
        )
        route_intersects = cross_track_m <= corridor_half_width_m
        ahead = along_track_m >= -obstacle_radius_m
        local_required = bool(
            ahead
            and route_intersects
            and distance_m <= lookahead_m
            and (time_to_conflict_s is None or time_to_conflict_s <= time_limit_s)
        )
        assessments.append(
            {
                "obstacle_name": str(obstacle.get("name") or f"obstacle_{index}"),
                "distance_to_obstacle_m": round(distance_m, 3),
                "along_track_to_obstacle_m": round(along_track_m, 3),
                "cross_track_to_obstacle_m": round(cross_track_m, 3),
                "route_corridor_intersects": route_intersects,
                "obstacle_ahead": ahead,
                "time_to_conflict_s": (
                    round(time_to_conflict_s, 3)
                    if time_to_conflict_s is not None
                    else None
                ),
                "local_avoidance_required": local_required,
            }
        )
    selected = min(
        assessments,
        key=lambda item: float(item.get("distance_to_obstacle_m") or math.inf),
        default={},
    )
    local_required = any(
        item.get("local_avoidance_required") is True for item in assessments
    )
    return {
        "assessment_status": (
            "computed" if assessments else "obstacle_position_unavailable"
        ),
        "local_avoidance_required": local_required,
        "conflict_class": (
            "local_route_conflict"
            if local_required
            else "distant_or_non_intersecting_obstacle"
        ),
        "lookahead_m": lookahead_m,
        "time_to_conflict_limit_s": time_limit_s,
        "ground_speed_mps": (
            round(ground_speed_mps, 3) if ground_speed_mps is not None else None
        ),
        "nearest_obstacle": selected,
        "obstacles": assessments,
    }


def _auto_runtime_recovery_agent_telemetry_snapshot(
    snapshot: Mapping[str, Any],
    *,
    artifacts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    planned_route_m = _auto_runtime_planned_route_m(artifacts)
    battery_projection = _auto_runtime_battery_endurance_projection(
        snapshot,
        planned_route_m=planned_route_m,
    )
    return_home_projection = _auto_runtime_battery_return_home_projection(snapshot)
    drift_projection = _auto_runtime_route_drift_projection(
        snapshot,
        artifacts=artifacts,
    )
    terrain_projection = _auto_runtime_terrain_clearance_projection(
        snapshot,
        artifacts=artifacts,
    )
    sample_index = _auto_snapshot_number(snapshot.get("sample_index"))
    altitude_above_home_m = _auto_snapshot_number(
        snapshot.get("altitude_above_home_m")
    )
    progress_m = _auto_snapshot_number(snapshot.get("progress_m"))
    elapsed_seconds = _auto_snapshot_number(snapshot.get("elapsed_seconds"))
    request_observed = snapshot.get("operator_recovery_request_observed") is True
    terrain_clearance_m = _auto_snapshot_number(
        terrain_projection.get("terrain_clearance_m")
    )
    terrain_clearance_target_m = _auto_snapshot_number(
        terrain_projection.get("terrain_clearance_target_m")
    )
    terrain_clearance_grace_m = _auto_snapshot_number(
        terrain_projection.get("terrain_clearance_grace_m")
    ) or 0.0
    takeoff_clearance_not_established = bool(
        terrain_clearance_target_m is not None
        and (
            terrain_clearance_m is None
            or terrain_clearance_m
            < terrain_clearance_target_m - terrain_clearance_grace_m
        )
    )
    preflight_observation = bool(
        not request_observed
        and (
            sample_index is None
            or sample_index <= 0
            or (
                (progress_m is None or progress_m < 5.0)
                and (
                    elapsed_seconds is None
                    or elapsed_seconds <= 30.0
                    or altitude_above_home_m is None
                    or altitude_above_home_m < 5.0
                    or takeoff_clearance_not_established
                )
            )
        )
    )
    if preflight_observation and terrain_projection.get("projection_status") == "computed":
        # AGL while PX4 is still on the ground or climbing through the takeoff
        # envelope is not a route-clearance failure. Keep the measurement, but
        # prevent it from poisoning the rolling recovery window.
        terrain_projection = {
            **terrain_projection,
            "terrain_clearance_below_minimum": False,
            "terrain_clearance_evaluation_phase": "preflight_takeoff_grace",
        }
    obstacle_projection = _auto_runtime_obstacle_projection(artifacts=artifacts)
    obstacle_projection = {
        **obstacle_projection,
        "conflict_assessment": _auto_runtime_obstacle_conflict_projection(
            snapshot=snapshot,
            artifacts=artifacts,
            obstacle_projection=obstacle_projection,
        ),
    }
    return {
        "source": "missionos_auto_mission_runtime_snapshot",
        "sample_index": snapshot.get("sample_index"),
        "elapsed_seconds": snapshot.get("elapsed_seconds"),
        "route": {
            "progress_m": snapshot.get("progress_m"),
            "planned_route_m": planned_route_m,
            "remaining_route_m": battery_projection.get("remaining_route_m"),
            "deviation_xy_m": drift_projection.get("deviation_xy_m"),
            "wind_drift_deviation_xy_m": drift_projection.get("deviation_xy_m"),
            "drift_projection": drift_projection,
            "mission_current_seq": snapshot.get("mission_current_seq"),
            "mission_reached_seq": snapshot.get("mission_reached_seq"),
            "waypoint_total": snapshot.get("waypoint_total"),
            "dropoff_dwell_candidate": snapshot.get("dropoff_dwell_candidate"),
            "ground_speed_mps": snapshot.get("ground_speed_mps"),
        },
        "terrain": terrain_projection,
        "obstacle": obstacle_projection,
        "position": {
            "local_x_m": snapshot.get("local_x_m"),
            "local_y_m": snapshot.get("local_y_m"),
            "local_z_m": snapshot.get("local_z_m"),
            "altitude_above_home_m": snapshot.get("altitude_above_home_m"),
            "distance_to_home_m": snapshot.get("distance_to_home_m"),
        },
        "battery": {
            "remaining_percent": _auto_snapshot_number(
                snapshot.get("battery_remaining_percent")
            ),
            "delta_percent": _auto_snapshot_number(
                snapshot.get("battery_remaining_delta_percent")
            ),
            "warning": snapshot.get("battery_warning"),
            "source": snapshot.get("battery_state_source"),
            "endurance_projection": battery_projection,
            "return_home_projection": return_home_projection,
        },
        "wind": {
            "speed_mps": _auto_snapshot_number(
                snapshot.get("wind_speed_mps"),
                snapshot.get("weather_wind_speed_mps"),
            ),
        },
        "telemetry": {
            # The takeoff listener can miss a heartbeat poll while position
            # and altitude observations are already advancing. Do not let that
            # transient preflight sample poison the next 30-second recovery
            # window; retain the raw heartbeat fact separately.
            "stale": (
                False
                if preflight_observation
                else snapshot.get("heartbeat_observed") is False
            ),
            "dropout": False,
            "heartbeat_observed": snapshot.get("heartbeat_observed"),
        },
        "recovery": {
            "request_observed": snapshot.get(
                "operator_recovery_request_observed"
            ),
            "action": snapshot.get("operator_recovery_action"),
            "parameters": snapshot.get("operator_recovery_parameters"),
            "command_ack_observed": snapshot.get(
                "operator_recovery_command_ack_observed"
            ),
            "command_ack_result": snapshot.get(
                "operator_recovery_command_ack_result"
            ),
            "assist_attempted": snapshot.get(
                "operator_recovery_assist_attempted"
            ),
            "assist_status": snapshot.get("operator_recovery_assist_status"),
            "target_reached": snapshot.get("operator_recovery_target_reached"),
            "resume_status": snapshot.get(
                "operator_recovery_resume_auto_status"
            ),
            "resume_safety_verification": snapshot.get(
                "operator_recovery_resume_safety_verification"
            ),
            "terminal": snapshot.get("operator_recovery_terminal"),
        },
        "nav_state": snapshot.get("nav_state"),
        "arming_state": snapshot.get("arming_state"),
        "landed": snapshot.get("landed"),
        "preflight_observation": preflight_observation,
    }


def _should_refresh_auto_runtime_recovery_agent(
    *,
    bridge: Mapping[str, Any],
    telemetry_snapshot: Mapping[str, Any],
    refresh_seconds: float = MISSIONOS_RUNTIME_RECOVERY_AGENT_REFRESH_SECONDS,
    last_invoked_elapsed_seconds: float | None = None,
) -> bool:
    if not bridge:
        return True
    bridge_snapshot = bridge.get("telemetry_snapshot")
    bridge_snapshot = bridge_snapshot if isinstance(bridge_snapshot, Mapping) else {}
    if bridge_snapshot.get("sample_index") == telemetry_snapshot.get("sample_index"):
        return False
    elapsed = _auto_snapshot_number(telemetry_snapshot.get("elapsed_seconds"))
    if elapsed is None:
        return True
    if last_invoked_elapsed_seconds is None:
        last_invoked_elapsed_seconds = _auto_snapshot_number(
            bridge.get("last_agent_invoked_elapsed_seconds")
        )
    if last_invoked_elapsed_seconds is None:
        return True
    return float(elapsed) - float(last_invoked_elapsed_seconds) >= refresh_seconds


def _runtime_recovery_agent_env_seconds(env_name: str, default: float) -> float:
    raw = os.getenv(env_name)
    if raw is not None and raw.strip():
        try:
            parsed = float(raw)
        except ValueError:
            parsed = None
        if parsed is not None and parsed > 0:
            return parsed
    return default


def _runtime_recovery_agent_refresh_seconds() -> float:
    return _runtime_recovery_agent_env_seconds(
        MISSIONOS_RUNTIME_RECOVERY_AGENT_REFRESH_SECONDS_ENV,
        MISSIONOS_RUNTIME_RECOVERY_AGENT_REFRESH_SECONDS,
    )


def _runtime_recovery_agent_soft_refresh_seconds() -> float:
    return _runtime_recovery_agent_env_seconds(
        MISSIONOS_RUNTIME_RECOVERY_AGENT_SOFT_REFRESH_SECONDS_ENV,
        MISSIONOS_RUNTIME_RECOVERY_AGENT_SOFT_REFRESH_SECONDS,
    )


def _runtime_recovery_agent_window_seconds() -> float:
    return _runtime_recovery_agent_env_seconds(
        MISSIONOS_RUNTIME_RECOVERY_AGENT_WINDOW_SECONDS_ENV,
        MISSIONOS_RUNTIME_RECOVERY_AGENT_WINDOW_SECONDS,
    )


def _runtime_recovery_agent_bucket_seconds() -> float:
    return _runtime_recovery_agent_env_seconds(
        MISSIONOS_RUNTIME_RECOVERY_AGENT_BUCKET_SECONDS_ENV,
        MISSIONOS_RUNTIME_RECOVERY_AGENT_BUCKET_SECONDS,
    )


def _runtime_recovery_proposal_max_age_seconds() -> float:
    return _runtime_recovery_agent_env_seconds(
        MISSIONOS_RUNTIME_RECOVERY_PROPOSAL_MAX_AGE_SECONDS_ENV,
        MISSIONOS_RUNTIME_RECOVERY_PROPOSAL_MAX_AGE_SECONDS,
    )


def _runtime_recovery_proposal_max_origin_drift_m() -> float:
    return _runtime_recovery_agent_env_seconds(
        MISSIONOS_RUNTIME_RECOVERY_PROPOSAL_MAX_ORIGIN_DRIFT_M_ENV,
        MISSIONOS_RUNTIME_RECOVERY_PROPOSAL_MAX_ORIGIN_DRIFT_M,
    )


def _runtime_recovery_agent_window_samples(
    bridge: Mapping[str, Any],
    telemetry_snapshot: Mapping[str, Any],
    *,
    window_s: float,
) -> list[dict[str, Any]]:
    prior = bridge.get("recovery_window_samples")
    samples: list[dict[str, Any]] = []
    if isinstance(prior, Sequence) and not isinstance(prior, (str, bytes)):
        for item in prior:
            if isinstance(item, Mapping):
                samples.append(dict(item))
    samples.append(dict(telemetry_snapshot))

    deduped: dict[Any, dict[str, Any]] = {}
    without_index: list[dict[str, Any]] = []
    for sample in samples:
        sample_index = sample.get("sample_index")
        if sample_index is None:
            without_index.append(sample)
            continue
        deduped[sample_index] = sample
    samples = without_index + list(deduped.values())

    def _elapsed(sample: Mapping[str, Any]) -> float:
        elapsed = _auto_snapshot_number(
            sample.get("elapsed_seconds"),
            sample.get("elapsed_s"),
            sample.get("sample_time_s"),
            sample.get("timestamp_s"),
        )
        if elapsed is None:
            index = _auto_snapshot_number(sample.get("sample_index"), sample.get("index"))
            return float(index) if index is not None else 0.0
        return float(elapsed)

    samples.sort(key=_elapsed)
    latest_elapsed = _elapsed(samples[-1]) if samples else 0.0
    keep_after = latest_elapsed - max(0.001, float(window_s))
    trimmed = [sample for sample in samples if _elapsed(sample) >= keep_after]
    return trimmed[-MISSIONOS_RUNTIME_RECOVERY_AGENT_MAX_WINDOW_SAMPLES:]


def _recovery_window_summary_hash(summary: Mapping[str, Any]) -> str:
    """Hash coarse numeric facts only; omit timestamps and sample identities."""
    overall = summary.get("overall")
    overall = overall if isinstance(overall, Mapping) else {}
    latest = summary.get("latest")
    latest = latest if isinstance(latest, Mapping) else {}
    material = {
        "summary_status": summary.get("summary_status"),
        "hard_breaches": summary.get("hard_breaches"),
        "soft_signals": summary.get("soft_signals"),
        "overall": {
            key: overall.get(key)
            for key in (
                "sample_count",
                "progress_delta_m",
                "battery_min_percent",
                "battery_delta_percent",
                "terrain_clearance_min_m",
                "terrain_clearance_margin_min_m",
                "cross_track_max_m",
                "wind_speed_max_mps",
                "nav_state_values",
                "telemetry_stale_count",
            )
        },
        "latest": {
            key: latest.get(key)
            for key in (
                "progress_m",
                "battery_remaining_percent",
                "terrain_clearance_m",
                "cross_track_m",
                "telemetry_stale",
            )
        },
    }
    return sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _runtime_recovery_decision_signature(
    summary: Mapping[str, Any],
    *,
    telemetry_snapshot: Mapping[str, Any],
) -> str:
    """Hash decision-changing facts, not continuously changing telemetry.

    Numeric progress, battery, and clearance remain available as evidence, but
    they must not cause a new hosted LLM invocation every refresh interval.
    A new decision epoch begins when thresholded risk, navigation state, or
    grounded state changes.
    """

    hard = summary.get("hard_breaches")
    hard = hard if isinstance(hard, Mapping) else {}
    soft = summary.get("soft_signals")
    soft = soft if isinstance(soft, Mapping) else {}
    material = {
        "hard_breaches": {
            key: value is True
            for key, value in sorted(hard.items())
            if key != "any"
        },
        "soft_signals": {
            key: value is True
            for key, value in sorted(soft.items())
            if key != "any"
        },
        "nav_state": telemetry_snapshot.get("nav_state"),
        "landed": telemetry_snapshot.get("landed") is True,
        "recovery_observation_state": _runtime_recovery_observation_state(
            telemetry_snapshot
        ),
    }
    return _canonical_sha256(material)


def _runtime_recovery_observation_state(
    telemetry_snapshot: Mapping[str, Any],
) -> str:
    recovery = telemetry_snapshot.get("recovery")
    recovery = recovery if isinstance(recovery, Mapping) else {}
    assist_status = str(recovery.get("assist_status") or "").strip().lower()
    resume_status = str(recovery.get("resume_status") or "").strip().lower()
    if telemetry_snapshot.get("landed") is True:
        return "landed"
    if telemetry_snapshot.get("preflight_observation") is True:
        # The first PX4 listener sample can precede the first trustworthy
        # airborne/terrain observation. The whole takeoff envelope, not only
        # sample zero, must not create a hosted-model proposal from transient
        # negative AGL or missing heartbeat.
        return "preflight"
    if assist_status in {
        "failed",
        "aborted",
        "canceled",
        "cancelled",
        "timeout",
        "target_not_reached",
    } or resume_status in {"failed", "not_resumed", "resume_failed"}:
        return "failed"
    if (
        assist_status == "stream_window_complete"
        and recovery.get("target_reached") is False
    ):
        # The bounded OFFBOARD window ended without reaching its approved
        # target. AUTO must not resume; the failed action becomes a fresh
        # observation/decision epoch for the safe terminal recovery path.
        return "failed"
    if resume_status in {
        "held_remaining_route_or_dropoff_unsafe",
        "hold_remaining_route_or_dropoff_unsafe_failed",
    }:
        # The bounded target was reached, but fresh deterministic verification
        # rejected the original AUTO continuation.  This is a new observation
        # epoch and must return to the Recovery Agent for a new proposal and a
        # separately bound human approval.
        return "failed"
    if resume_status == "hold_at_alternate_dropoff_failed":
        return "failed"
    if resume_status == "held_at_alternate_dropoff_awaiting_operator_decision":
        # Reaching an alternate hover point does not authorize payload release,
        # landing, or resumption of the now-invalid original route.
        return "held_at_alternate_dropoff"
    if (
        recovery.get("target_reached") is True
        and resume_status == "resumed_auto_mission"
    ):
        return "succeeded"
    if (
        str(recovery.get("action") or "").strip().lower() == "safety_hold"
        and assist_status == "safety_hold_observed"
        and resume_status == "held_awaiting_operator_recovery_approval"
    ):
        return "held_awaiting_operator_approval"
    if recovery.get("request_observed") is True and (
        recovery.get("command_ack_observed") is True
        or recovery.get("assist_attempted") is True
    ):
        return "in_progress"
    return "none"


def _runtime_recovery_safety_hold_stable(
    *,
    bridge: Mapping[str, Any],
    telemetry_snapshot: Mapping[str, Any],
) -> bool:
    """Require two fresh HOLD observations before paying for an LLM judgment."""

    if _runtime_recovery_observation_state(telemetry_snapshot) != (
        "held_awaiting_operator_approval"
    ):
        return False
    current_telemetry = telemetry_snapshot.get("telemetry")
    current_telemetry = (
        current_telemetry if isinstance(current_telemetry, Mapping) else {}
    )
    if current_telemetry.get("stale") is True:
        return False
    previous = bridge.get("telemetry_snapshot")
    previous = previous if isinstance(previous, Mapping) else {}
    if _runtime_recovery_observation_state(previous) != (
        "held_awaiting_operator_approval"
    ):
        return False
    previous_telemetry = previous.get("telemetry")
    previous_telemetry = (
        previous_telemetry if isinstance(previous_telemetry, Mapping) else {}
    )
    if previous_telemetry.get("stale") is True:
        return False
    current_elapsed = _auto_snapshot_number(
        telemetry_snapshot.get("elapsed_seconds")
    )
    previous_elapsed = _auto_snapshot_number(previous.get("elapsed_seconds"))
    return bool(
        current_elapsed is not None
        and previous_elapsed is not None
        and current_elapsed > previous_elapsed
    )


def _runtime_recovery_attempt_evidence(
    *,
    task_id: str,
    telemetry_snapshot: Mapping[str, Any],
    observation_state: str,
    receipt: Mapping[str, Any],
    last_proposal: Mapping[str, Any],
    observed_at: str,
) -> dict[str, Any] | None:
    """Build durable evidence for a completed bounded maneuver attempt.

    Runtime snapshots are current-state views and can later be replaced by RTL
    or LAND telemetry.  This collection record preserves only the authority,
    executor observation, target result, and Verifier outcome for each maneuver;
    it does not turn display telemetry into completion evidence.
    """

    recovery = telemetry_snapshot.get("recovery")
    recovery = recovery if isinstance(recovery, Mapping) else {}
    action = str(recovery.get("action") or "").strip().lower()
    if action not in {
        "adjust_altitude",
        "adjust_speed",
        "reroute",
        "avoid_obstacle",
    } or observation_state not in {
        "failed",
        "succeeded",
        "held_at_alternate_dropoff",
    }:
        return None
    parameters = recovery.get("parameters")
    parameters = dict(parameters) if isinstance(parameters, Mapping) else {}
    receipt_revalidation = receipt.get("proposal_revalidation")
    receipt_revalidation = (
        receipt_revalidation if isinstance(receipt_revalidation, Mapping) else {}
    )
    source_proposal_id = str(
        receipt_revalidation.get("proposal_id")
        or last_proposal.get("proposal_id")
        or ""
    )
    proposal_origin = receipt_revalidation.get("proposal_origin")
    if not isinstance(proposal_origin, Mapping):
        proposal_origin = last_proposal.get("proposal_origin")
    proposal_origin = (
        dict(proposal_origin) if isinstance(proposal_origin, Mapping) else {}
    )
    attempt_id = _stable_id(
        "runtime_recovery_attempt",
        {
            "task_id": task_id,
            "source_proposal_id": source_proposal_id,
            "action": action,
            "parameters": parameters,
        },
    )
    position = telemetry_snapshot.get("position")
    position = dict(position) if isinstance(position, Mapping) else {}
    resume_verification = recovery.get("resume_safety_verification")
    resume_verification = (
        dict(resume_verification)
        if isinstance(resume_verification, Mapping)
        else None
    )
    outcome_verification = verify_runtime_recovery_outcome(
        action=action,
        recovery_observation=recovery,
        dispatch_authority_created=(
            receipt.get("dispatch_authority_created") is True
        ),
    )
    return {
        "schema_version": "missionos_runtime_recovery_attempt_evidence.v1",
        "attempt_id": attempt_id,
        "task_id": task_id,
        "source_proposal_id": source_proposal_id,
        "proposal_origin": proposal_origin,
        "proposal_origin_sha256": str(
            receipt_revalidation.get("proposal_origin_sha256")
            or last_proposal.get("proposal_origin_sha256")
            or proposal_origin.get("origin_sha256")
            or ""
        ),
        "observed_at": observed_at,
        "sample_index": telemetry_snapshot.get("sample_index"),
        "attempt_status": observation_state,
        "recovery_action": action,
        "recovery_parameters": parameters,
        "position": position,
        "command_ack_observed": recovery.get("command_ack_observed") is True,
        "assist_attempted": recovery.get("assist_attempted") is True,
        "assist_status": str(recovery.get("assist_status") or ""),
        "target_reached": recovery.get("target_reached") is True,
        "target_distance_m": recovery.get("target_distance_m"),
        "resume_status": str(recovery.get("resume_status") or ""),
        "resume_auto_attempted": recovery.get("resume_auto_attempted") is True,
        "resume_safety_verification": resume_verification,
        "outcome_verification": outcome_verification,
        "outcome_verification_id": outcome_verification.get(
            "recovery_outcome_verification_id"
        ),
        "outcome_verification_sha256": outcome_verification.get(
            "recovery_outcome_verification_sha256"
        ),
        "dispatch_authority_created": (
            receipt.get("dispatch_authority_created") is True
        ),
        "simulator_execution_observed": (
            recovery.get("command_ack_observed") is True
            and recovery.get("assist_attempted") is True
        ),
        "delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _runtime_recovery_pending_proposal_invalidation_reasons(
    *,
    proposal: Mapping[str, Any],
    telemetry_snapshot: Mapping[str, Any],
    now: datetime,
) -> list[str]:
    """Return reasons a pending proposal can no longer block a new epoch."""

    reasons: list[str] = []
    valid_until_raw = str(proposal.get("valid_until") or "").strip()
    try:
        valid_until = _utc(
            datetime.fromisoformat(valid_until_raw.replace("Z", "+00:00"))
        )
    except ValueError:
        valid_until = None
    if valid_until is None:
        reasons.append("runtime_recovery_proposal_valid_until_missing_or_invalid")
    elif now > valid_until:
        reasons.append("runtime_recovery_proposal_stale")

    proposal_result = proposal.get("runtime_recovery_agent_result")
    proposal_result = proposal_result if isinstance(proposal_result, Mapping) else {}
    assessment = proposal_result.get("assessment")
    assessment = assessment if isinstance(assessment, Mapping) else {}
    candidate = assessment.get("recovery_planner_tool_candidate")
    candidate = candidate if isinstance(candidate, Mapping) else {}
    candidate_parameters = candidate.get("proposed_parameters")
    candidate_parameters = (
        candidate_parameters if isinstance(candidate_parameters, Mapping) else {}
    )
    manifest_bound_alternate = (
        candidate.get("selected_bounded_action") == "reroute"
        and candidate_parameters.get("alternate_dropoff") is True
        and candidate_parameters.get("resume_original_route") is False
    )
    if manifest_bound_alternate:
        obstacle = telemetry_snapshot.get("obstacle")
        obstacle = obstacle if isinstance(obstacle, Mapping) else {}
        manifest = obstacle.get("obstacle_manifest")
        manifest = manifest if isinstance(manifest, Mapping) else {}
        manifest_candidate = manifest.get("alternate_dropoff_candidate")
        manifest_candidate = (
            manifest_candidate if isinstance(manifest_candidate, Mapping) else {}
        )
        manifest_parameters = manifest_candidate.get("proposed_parameters")
        manifest_parameters = (
            manifest_parameters if isinstance(manifest_parameters, Mapping) else {}
        )
        source_obstacle_name = str(
            candidate_parameters.get("source_obstacle_name") or ""
        )
        collision_obstacle_still_present = any(
            isinstance(item, Mapping)
            and str(item.get("name") or "") == source_obstacle_name
            and item.get("collision_enabled") is True
            for item in manifest.get("obstacles") or []
        )
        if (
            manifest.get("original_dropoff_available") is not False
            or dict(manifest_parameters) != dict(candidate_parameters)
            or not collision_obstacle_still_present
        ):
            reasons.append(
                "runtime_recovery_alternate_dropoff_manifest_binding_invalidated"
            )

    origin = proposal.get("origin_position")
    origin = origin if isinstance(origin, Mapping) else {}
    position = telemetry_snapshot.get("position")
    position = position if isinstance(position, Mapping) else {}
    origin_x = _auto_snapshot_number(origin.get("local_x_m"))
    origin_y = _auto_snapshot_number(origin.get("local_y_m"))
    current_x = _auto_snapshot_number(position.get("local_x_m"))
    current_y = _auto_snapshot_number(position.get("local_y_m"))
    max_origin_drift_m = _auto_snapshot_number(
        proposal.get("max_origin_drift_m")
    )
    if (
        not manifest_bound_alternate
        and origin_x is not None
        and origin_y is not None
        and current_x is not None
        and current_y is not None
        and max_origin_drift_m is not None
        and max_origin_drift_m > 0
    ):
        origin_drift_m = math.hypot(current_x - origin_x, current_y - origin_y)
        if origin_drift_m > max_origin_drift_m:
            reasons.append("runtime_recovery_proposal_origin_drift_exceeded")
    return list(dict.fromkeys(reasons))


def _runtime_recovery_agent_skipped_result(
    *,
    reason: str,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": "missionos_runtime_recovery_agent_result.v1",
        "runtime_status": "proposal_skipped",
        "assessment": {},
        "agent_invocations": [],
        "blocking_reasons": [reason],
        "metadata": {
            "source": "missionos_auto_live_telemetry_advisory_guard",
            "detail": detail[:500],
        },
        "dispatch_authority_created": False,
        "progress_counted": False,
    }


def _runtime_recovery_policy() -> dict[str, Any]:
    return {
        "policy_ref": "operator_approved_live_sitl_intervention_policy",
        "preauthorized_actions": [
            "return_to_launch",
            "land",
            "adjust_altitude",
            "adjust_speed",
            "reroute",
            "avoid_obstacle",
        ],
        "battery_return_threshold_percent": 20,
        "max_route_deviation_xy_m": 100,
        "emergency_landing_route_deviation_xy_m": 250,
        "min_terrain_clearance_m": 30,
        "max_wind_speed_mps": 6,
        "max_adjust_altitude_m": 500,
        "max_adjust_speed_mps": 30,
        "max_reroute_target_abs_m": 5000,
        "max_recovery_duration_s": 75,
        "max_recovery_horizontal_speed_mps": 10,
        "max_recovery_vertical_speed_mps": 3,
        "reachability_duration_margin_factor": 1.25,
        "reachability_setup_seconds": 5,
        "wind_uncertainty_floor_mps": 1,
    }


def _runtime_recovery_agent_fallback_result(
    *,
    telemetry_snapshot: Mapping[str, Any],
    task_id: str,
    reason: str,
    detail: str = "",
) -> dict[str, Any]:
    policy = _runtime_recovery_policy()
    mission_context = {
        "task_id": task_id,
        "mission_phase": "live_auto_mission",
        "authority_status": "proposal_only",
    }
    planner_result = plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry_snapshot,
        mission_context=mission_context,
        recovery_policy=policy,
    )
    guarded = guard_runtime_recovery_planner_result(
        planner_result=planner_result,
        telemetry_snapshot=telemetry_snapshot,
        recovery_policy=policy,
    )
    assessment = guarded.get("recovery_guardrail_assessment")
    assessment = dict(assessment) if isinstance(assessment, Mapping) else {}
    if not assessment:
        return _runtime_recovery_agent_skipped_result(reason=reason, detail=detail)
    return {
        "schema_version": MISSIONOS_RUNTIME_RECOVERY_RESULT_SCHEMA_VERSION,
        "runtime_status": str(
            assessment.get("assessment_status") or "proposal_skipped"
        ),
        "blocking_reasons": list(assessment.get("blocking_reasons") or []),
        "assessment": assessment,
        "agent_output": {
            "selected_bounded_action": assessment.get("selected_bounded_action"),
            "requires_human_approval": assessment.get("requires_human_approval"),
            "proposed_parameters": assessment.get("proposed_parameters") or {},
            "trigger_reasons": [
                "runtime_recovery_agent_fallback",
                reason,
            ],
        },
        "agent_invocations": [
            {
                "agent_name": "missionos_runtime_recovery_agent_fallback",
                "provider": "deterministic",
                "model_id": "",
                "invocation_kind": "deterministic_guardrail_fallback",
                "runtime_status": reason,
                "fallback_reason": reason,
                "detail": str(detail or "")[:500],
                "function_tool_called": True,
                "function_tool_results": [guarded],
                "progress_counted": False,
            }
        ],
        "fallback_planner_result": guarded,
        "dispatch_authority_created": False,
        "progress_counted": False,
    }


def _runtime_recovery_agent_timeout_seconds() -> float:
    """Resolve the runtime recovery agent timeout, env-overridable.

    Gemini gets a 45 second default because live ADK function-tool calls can
    legitimately exceed the old five-second budget.  The environment override
    is capped at 90 seconds so a hosted judgment cannot stall the runtime
    supervisor indefinitely.  Local backends can use the same bounded override.
    """
    raw = os.getenv(MISSIONOS_RUNTIME_RECOVERY_AGENT_TIMEOUT_SECONDS_ENV)
    if raw is not None and raw.strip():
        try:
            parsed = float(raw)
        except ValueError:
            parsed = None
        if parsed is not None and parsed > 0:
            return min(parsed, MISSIONOS_RUNTIME_RECOVERY_AGENT_TIMEOUT_MAX_SECONDS)
    return MISSIONOS_RUNTIME_RECOVERY_AGENT_TIMEOUT_SECONDS


def _run_auto_runtime_recovery_agent_with_timeout(
    *,
    telemetry_snapshot: Mapping[str, Any],
    task_id: str,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    timeout_seconds = (
        _runtime_recovery_agent_timeout_seconds()
        if timeout_seconds is None
        else timeout_seconds
    )
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def _fallback_with_hosted_attempt(
        *,
        reason: str,
        detail: str,
        outcome: str,
        source_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        fallback = _runtime_recovery_agent_fallback_result(
            telemetry_snapshot=telemetry_snapshot,
            task_id=task_id,
            reason=reason,
            detail=detail,
        )
        invocations = (
            source_result.get("agent_invocations")
            if isinstance(source_result, Mapping)
            else []
        )
        invocation = next(
            (dict(item) for item in invocations or [] if isinstance(item, Mapping)),
            {},
        )
        fallback["hosted_model_attempt"] = {
            "provider": str(
                invocation.get("provider")
                or llm_provider_label("missionos_runtime_recovery_agent")
            ),
            "model_id": str(
                invocation.get("model_id")
                or agent_model_label(
                    None,
                    agent_name="missionos_runtime_recovery_agent",
                )
            ),
            "invocation_kind": str(invocation.get("invocation_kind") or ""),
            "prompt_sha256": str(invocation.get("prompt_sha256") or ""),
            "response_sha256": str(invocation.get("response_sha256") or ""),
            "function_calls_sha256": str(
                invocation.get("function_calls_sha256") or ""
            ),
            "function_tool_results_sha256": str(
                invocation.get("function_tool_results_sha256") or ""
            ),
            "validated_output_source": str(
                invocation.get("validated_output_source") or ""
            ),
            "outcome": outcome,
            "timeout_seconds": timeout_seconds,
            "contains_prompt_or_response_text": False,
        }
        return fallback

    def _invoke() -> None:
        try:
            result = run_missionos_runtime_recovery_agent(
                telemetry_snapshot=telemetry_snapshot,
                mission_context={
                    "task_id": task_id,
                    "mission_phase": "live_auto_mission",
                    "authority_status": "proposal_only",
                },
                recovery_policy=_runtime_recovery_policy(),
            )
        except Exception as exc:  # Advisory path must not break live telemetry.
            result_queue.put(("error", exc))
            return
        result_queue.put(("result", result))

    thread = threading.Thread(
        target=_invoke,
        name=f"missionos-recovery-agent-{task_id}",
        daemon=True,
    )
    thread.start()

    try:
        kind, value = result_queue.get(timeout=max(0.001, timeout_seconds))
    except queue.Empty:
        return _fallback_with_hosted_attempt(
            reason="runtime_recovery_agent_timeout",
            detail=f"timeout_seconds={timeout_seconds}",
            outcome="timeout",
        )
    if kind == "error":
        return _fallback_with_hosted_attempt(
            reason="runtime_recovery_agent_error",
            detail=f"{type(value).__name__}: {value}",
            outcome="error",
        )
    if isinstance(value, Mapping):
        result = dict(value)
        if result.get("runtime_status") == "not_configured":
            return _fallback_with_hosted_attempt(
                reason="runtime_recovery_agent_not_configured",
                detail=";".join(str(item) for item in result.get("blocking_reasons") or ()),
                outcome="not_configured",
                source_result=result,
            )
        if result.get("runtime_status") == "guardrail_blocked":
            return _fallback_with_hosted_attempt(
                reason="runtime_recovery_agent_guardrail_blocked",
                detail=";".join(
                    str(item) for item in result.get("blocking_reasons") or ()
                ),
                outcome="guardrail_blocked",
                source_result=result,
            )
        return result
    return _fallback_with_hosted_attempt(
        reason="runtime_recovery_agent_invalid_result",
        detail=type(value).__name__,
        outcome="invalid_result",
    )


def _runtime_recovery_proposal_origin(
    *,
    result: Mapping[str, Any],
    proposal_recompiled: bool,
    source_proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Return immutable, content-free provenance for a recovery proposal."""

    source_origin = source_proposal.get("proposal_origin")
    source_origin = (
        dict(source_origin) if isinstance(source_origin, Mapping) else {}
    )
    invocations = [
        dict(item)
        for item in result.get("agent_invocations") or []
        if isinstance(item, Mapping)
    ]
    hosted_invocation = next(
        (
            item
            for item in invocations
            if str(item.get("agent_name") or "")
            == "missionos_runtime_recovery_agent"
            and str(item.get("provider") or "") != "deterministic"
        ),
        None,
    )
    fallback_invocation = next(
        (
            item
            for item in invocations
            if str(item.get("invocation_kind") or "")
            == "deterministic_guardrail_fallback"
        ),
        None,
    )
    hosted_attempt = result.get("hosted_model_attempt")
    hosted_attempt = (
        dict(hosted_attempt) if isinstance(hosted_attempt, Mapping) else {}
    )
    if proposal_recompiled:
        origin_kind = "deterministic_recompile_of_prior_judgment"
        provider = str(source_origin.get("provider") or "")
        model_id = str(source_origin.get("model_id") or "")
        invocation_kind = "deterministic_recompile"
        prompt_sha256 = str(source_origin.get("prompt_sha256") or "")
        response_sha256 = str(source_origin.get("response_sha256") or "")
        function_calls_sha256 = str(
            source_origin.get("function_calls_sha256") or ""
        )
        function_tool_results_sha256 = str(
            source_origin.get("function_tool_results_sha256") or ""
        )
        validated_output_source = str(
            source_origin.get("validated_output_source") or ""
        )
        fallback_reason = "runtime_recovery_proposal_deterministic_recompile"
    elif hosted_invocation is not None:
        origin_kind = "hosted_llm"
        provider = str(hosted_invocation.get("provider") or "")
        model_id = str(hosted_invocation.get("model_id") or "")
        invocation_kind = str(hosted_invocation.get("invocation_kind") or "")
        prompt_sha256 = str(hosted_invocation.get("prompt_sha256") or "")
        response_sha256 = str(hosted_invocation.get("response_sha256") or "")
        function_calls_sha256 = str(
            hosted_invocation.get("function_calls_sha256") or ""
        )
        function_tool_results_sha256 = str(
            hosted_invocation.get("function_tool_results_sha256") or ""
        )
        validated_output_source = str(
            hosted_invocation.get("validated_output_source") or ""
        )
        fallback_reason = ""
    elif fallback_invocation is not None:
        origin_kind = "deterministic_guardrail_fallback"
        provider = str(hosted_attempt.get("provider") or "deterministic")
        model_id = str(hosted_attempt.get("model_id") or "")
        invocation_kind = str(
            fallback_invocation.get("invocation_kind")
            or "deterministic_guardrail_fallback"
        )
        prompt_sha256 = str(hosted_attempt.get("prompt_sha256") or "")
        response_sha256 = str(hosted_attempt.get("response_sha256") or "")
        function_calls_sha256 = str(
            hosted_attempt.get("function_calls_sha256") or ""
        )
        function_tool_results_sha256 = str(
            hosted_attempt.get("function_tool_results_sha256") or ""
        )
        validated_output_source = str(
            hosted_attempt.get("validated_output_source") or ""
        )
        fallback_reason = str(
            fallback_invocation.get("fallback_reason")
            or fallback_invocation.get("runtime_status")
            or "runtime_recovery_agent_fallback"
        )
    else:
        origin_kind = "runtime_recovery_agent_result_unattributed"
        provider = ""
        model_id = ""
        invocation_kind = ""
        prompt_sha256 = ""
        response_sha256 = ""
        function_calls_sha256 = ""
        function_tool_results_sha256 = ""
        validated_output_source = ""
        fallback_reason = ""
    origin = {
        "schema_version": "missionos_runtime_recovery_proposal_origin.v1",
        "origin_kind": origin_kind,
        "provider": provider,
        "model_id": model_id,
        "invocation_kind": invocation_kind,
        "prompt_sha256": prompt_sha256,
        "response_sha256": response_sha256,
        "function_calls_sha256": function_calls_sha256,
        "function_tool_results_sha256": function_tool_results_sha256,
        "validated_output_source": validated_output_source,
        "fallback_reason": fallback_reason,
        "source_proposal_id": (
            str(source_proposal.get("proposal_id") or "")
            if proposal_recompiled
            else ""
        ),
        "contains_prompt_or_response_text": False,
        "dispatch_authority_created": False,
        "progress_counted": False,
    }
    return {
        **origin,
        "origin_sha256": _canonical_sha256(origin),
    }


def _attach_auto_runtime_recovery_agent_proposal(
    *,
    store: TaskStore,
    task_id: str,
    snapshot: Mapping[str, Any],
) -> None:
    try:
        task = store.get(task_id)
    except Exception:
        return
    if not task or task.get("status") != "running":
        return
    artifacts = task.get("artifacts") if isinstance(task.get("artifacts"), Mapping) else {}
    last_proposal = artifacts.get("missionos_runtime_recovery_last_proposal")
    last_proposal = (
        last_proposal if isinstance(last_proposal, Mapping) else {}
    )
    bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    bridge = bridge if isinstance(bridge, Mapping) else {}
    telemetry_snapshot = _auto_runtime_recovery_agent_telemetry_snapshot(
        snapshot,
        artifacts=artifacts,
    )
    conflict_assessment = telemetry_snapshot.get("obstacle")
    conflict_assessment = (
        conflict_assessment.get("conflict_assessment")
        if isinstance(conflict_assessment, Mapping)
        else {}
    )
    conflict_assessment = (
        conflict_assessment if isinstance(conflict_assessment, Mapping) else {}
    )
    safety_hold_receipt = artifacts.get(
        "missionos_runtime_recovery_safety_hold_receipt"
    )
    safety_hold_receipt = (
        safety_hold_receipt if isinstance(safety_hold_receipt, Mapping) else {}
    )
    current_recovery = telemetry_snapshot.get("recovery")
    current_recovery = (
        current_recovery if isinstance(current_recovery, Mapping) else {}
    )
    current_recovery_succeeded = bool(
        str(current_recovery.get("assist_status") or "").strip().lower()
        in {"target_reached", "succeeded", "completed"}
        and current_recovery.get("target_reached") is True
        and str(current_recovery.get("resume_status") or "").strip().lower()
        == "resumed_auto_mission"
    )
    if (
        safety_hold_receipt.get("request_status") == "queued"
        and str(current_recovery.get("action") or "").strip().lower()
        == "safety_hold"
        and str(current_recovery.get("assist_status") or "").strip().lower()
        == "safety_hold_observed"
    ):
        safety_hold_receipt = {
            **dict(safety_hold_receipt),
            "request_status": "observed",
            "runner_observed": True,
            "runner_ack_observed": (
                current_recovery.get("command_ack_observed") is True
            ),
            "runner_assist_status": "safety_hold_observed",
            "runner_resume_status": current_recovery.get("resume_status"),
            "runner_observed_at": _utc().isoformat(),
        }
        try:
            updated = store.update(
                task_id,
                replace_artifacts={
                    "missionos_runtime_recovery_safety_hold_receipt": (
                        safety_hold_receipt
                    )
                },
                metadata={
                    "missionos_runtime_recovery_safety_hold_status": "observed"
                },
            )
        except Exception:
            updated = None
        if isinstance(updated, Mapping):
            updated_artifacts = updated.get("artifacts")
            if isinstance(updated_artifacts, Mapping):
                artifacts = updated_artifacts
    if (
        conflict_assessment.get("local_avoidance_required") is True
        and not current_recovery_succeeded
        and safety_hold_receipt.get("request_status") not in {"queued", "observed"}
    ):
        running_receipt = artifacts.get(
            "missionos_auto_mission_gui_dispatch_running_receipt"
        )
        running_receipt = (
            running_receipt if isinstance(running_receipt, Mapping) else {}
        )
        request_path = str(
            running_receipt.get("operator_recovery_request_container_path") or ""
        )
        hold_observed_at = _utc().isoformat()
        hold_request = {
            "schema_version": "missionos_auto_safety_hold_request.v1",
            "task_id": task_id,
            "request_status": "queued",
            "recovery_action": "safety_hold",
            "recovery_parameters": {},
            "request_reason": "source_backed_local_route_conflict",
            "preauthorized_safety_reflex": True,
            "operator_approved": False,
            "explicit_recovery_dispatch_approval": False,
            "agent_created_dispatch_authority": False,
            "delivery_completion_claimed": False,
            "progress_counted": False,
            "physical_execution_invoked": False,
            "hardware_target_allowed": False,
            "observed_at": hold_observed_at,
        }
        if not request_path:
            hold_write: dict[str, Any] = {
                "request_status": "blocked",
                "blocked_reasons": [
                    "active_runner_recovery_request_path_missing"
                ],
            }
        else:
            try:
                hold_write = queue_px4_active_runner_recovery_request(
                    container_path=request_path,
                    request_payload=hold_request,
                )
            except (
                OSError,
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                ValueError,
            ) as exc:
                hold_write = {
                    "request_status": "blocked",
                    "blocked_reasons": [
                        "safety_hold_request_queue_failed:" + str(exc)
                    ],
                }
        safety_hold_receipt = {
            "schema_version": "missionos_runtime_recovery_safety_hold_receipt.v1",
            "task_id": task_id,
            "request_status": hold_write.get("request_status"),
            "preauthorized_safety_reflex": True,
            "safety_action": "hold",
            "trigger": "source_backed_local_route_conflict",
            "conflict_assessment": dict(conflict_assessment),
            "operator_approved": False,
            "explicit_recovery_dispatch_approval": False,
            "agent_created_dispatch_authority": False,
            "safety_policy_created_dispatch_authority": (
                hold_write.get("request_status") == "queued"
            ),
            "active_runner_request": hold_request,
            "active_runner_request_write": hold_write,
            "delivery_completion_claimed": False,
            "progress_counted": False,
            "physical_execution_invoked": False,
            "observed_at": hold_observed_at,
        }
        try:
            updated = store.update(
                task_id,
                replace_artifacts={
                    "missionos_runtime_recovery_safety_hold_receipt": (
                        safety_hold_receipt
                    )
                },
                metadata={
                    "missionos_runtime_recovery_safety_hold_status": (
                        hold_write.get("request_status")
                    )
                },
            )
        except Exception:
            updated = None
        if isinstance(updated, Mapping):
            updated_artifacts = updated.get("artifacts")
            if isinstance(updated_artifacts, Mapping):
                artifacts = updated_artifacts
    hard_refresh_seconds = _runtime_recovery_agent_refresh_seconds()
    soft_refresh_seconds = _runtime_recovery_agent_soft_refresh_seconds()
    window_s = _runtime_recovery_agent_window_seconds()
    bucket_s = _runtime_recovery_agent_bucket_seconds()
    window_samples = _runtime_recovery_agent_window_samples(
        bridge,
        telemetry_snapshot,
        window_s=window_s,
    )
    recovery_window_summary = build_recovery_window_summary(
        window_samples,
        window_s=window_s,
        cadence_s=hard_refresh_seconds,
        bucket_s=bucket_s,
    )
    recovery_window_summary_hash = _recovery_window_summary_hash(
        recovery_window_summary
    )
    telemetry_snapshot = dict(telemetry_snapshot)
    telemetry_snapshot["recovery_window_summary"] = recovery_window_summary

    hard_breaches = recovery_window_summary.get("hard_breaches")
    hard_breaches = hard_breaches if isinstance(hard_breaches, Mapping) else {}
    soft_signals = recovery_window_summary.get("soft_signals")
    soft_signals = soft_signals if isinstance(soft_signals, Mapping) else {}
    has_hard_news = hard_breaches.get("any") is True
    has_soft_news = soft_signals.get("any") is True
    has_news = has_hard_news or has_soft_news
    active_refresh_seconds = (
        hard_refresh_seconds if has_hard_news else soft_refresh_seconds
    )
    recovery_window_summary["cadence_s"] = float(active_refresh_seconds)
    prior_summary_hash = bridge.get("last_agent_recovery_window_summary_hash")
    if prior_summary_hash is None:
        prior_summary_hash = bridge.get("recovery_window_summary_hash")
    prior_agent_hard_news = bridge.get("last_agent_hard_breach_any") is True
    prior_result = bridge.get("runtime_recovery_agent_result")
    prior_result = prior_result if isinstance(prior_result, Mapping) else {}
    prior_reasons = prior_result.get("blocking_reasons")
    prior_reasons = prior_reasons if isinstance(prior_reasons, list) else []
    decision_signature = _runtime_recovery_decision_signature(
        recovery_window_summary,
        telemetry_snapshot=telemetry_snapshot,
    )
    prior_decision_signature = str(
        bridge.get("last_recovery_decision_signature") or ""
    )
    observation_state = _runtime_recovery_observation_state(telemetry_snapshot)
    receipt = artifacts.get("missionos_runtime_recovery_dispatch_receipt")
    receipt = receipt if isinstance(receipt, Mapping) else {}
    proposal_observed_at = str(bridge.get("proposal_observed_at") or "")
    receipt_observed_at = str(receipt.get("observed_at") or "")
    prior_assessment = prior_result.get("assessment")
    prior_assessment = (
        prior_assessment if isinstance(prior_assessment, Mapping) else {}
    )
    last_proposal_result = last_proposal.get("runtime_recovery_agent_result")
    last_proposal_result = (
        last_proposal_result
        if isinstance(last_proposal_result, Mapping)
        else {}
    )
    last_proposal_assessment = last_proposal_result.get("assessment")
    last_proposal_assessment = (
        last_proposal_assessment
        if isinstance(last_proposal_assessment, Mapping)
        else {}
    )
    proposal_selected_action = str(
        prior_assessment.get("selected_bounded_action")
        or last_proposal_assessment.get("selected_bounded_action")
        or ""
    ).strip()
    prior_proposal_passed = (
        last_proposal_result.get("runtime_status") == "proposal_guardrail_passed"
        and last_proposal_assessment.get("assessment_status")
        == "proposal_guardrail_passed"
        and last_proposal.get("proposal_status") == "awaiting_operator_approval"
    )
    proposal_awaiting_approval = prior_proposal_passed and (
        not receipt_observed_at
        or not proposal_observed_at
        or proposal_observed_at > receipt_observed_at
    )
    receipt_blocking_reasons = receipt.get("blocked_reasons")
    receipt_blocking_reasons = (
        receipt_blocking_reasons
        if isinstance(receipt_blocking_reasons, Sequence)
        and not isinstance(receipt_blocking_reasons, (str, bytes))
        else []
    )
    receipt_invalidated_proposal = bool(
        proposal_observed_at
        and receipt_observed_at
        and receipt_observed_at >= proposal_observed_at
        and any(
            str(reason).startswith("runtime_recovery_proposal_")
            or str(reason) == "runtime_recovery_current_telemetry_stale"
            for reason in receipt_blocking_reasons
        )
    )
    observed_at_datetime = _utc()
    receipt_revalidation = receipt.get("proposal_revalidation")
    receipt_revalidation = (
        receipt_revalidation
        if isinstance(receipt_revalidation, Mapping)
        else {}
    )
    receipt_datetime: datetime | None
    try:
        receipt_datetime = _utc(
            datetime.fromisoformat(receipt_observed_at.replace("Z", "+00:00"))
        )
    except ValueError:
        receipt_datetime = None
    matching_dispatch_authority_recent = bool(
        last_proposal.get("proposal_id")
        and receipt_revalidation.get("proposal_id")
        == last_proposal.get("proposal_id")
        and receipt.get("dispatch_authority_created") is True
        and receipt.get("active_runner_request_queued") is True
        and receipt_datetime is not None
        and (
            observed_at_datetime - receipt_datetime
        ).total_seconds()
        <= _runtime_recovery_proposal_max_age_seconds()
    )
    proposal_lifecycle_reasons = (
        _runtime_recovery_pending_proposal_invalidation_reasons(
            proposal=last_proposal,
            telemetry_snapshot=telemetry_snapshot,
            now=observed_at_datetime,
        )
        if proposal_awaiting_approval
        else []
    )
    prior_window_summary = bridge.get("recovery_window_summary")
    prior_window_summary = (
        prior_window_summary if isinstance(prior_window_summary, Mapping) else {}
    )
    prior_hard_breaches = prior_window_summary.get("hard_breaches")
    prior_hard_breaches = (
        prior_hard_breaches if isinstance(prior_hard_breaches, Mapping) else {}
    )
    bridge_telemetry = bridge.get("telemetry_snapshot")
    bridge_telemetry = (
        bridge_telemetry if isinstance(bridge_telemetry, Mapping) else {}
    )
    newly_observed_hard_breach_keys = {
        key
        for key, value in hard_breaches.items()
        if key != "any"
        and value is True
        and prior_hard_breaches.get(key) is not True
    }
    nav_state_changed = bool(
        bridge_telemetry
        and bridge_telemetry.get("nav_state") != telemetry_snapshot.get("nav_state")
    )
    current_recovery = telemetry_snapshot.get("recovery")
    current_recovery = (
        current_recovery if isinstance(current_recovery, Mapping) else {}
    )
    current_conflict = telemetry_snapshot.get("obstacle")
    current_conflict = (
        current_conflict.get("conflict_assessment")
        if isinstance(current_conflict, Mapping)
        else {}
    )
    current_conflict = (
        current_conflict if isinstance(current_conflict, Mapping) else {}
    )
    safety_hold_preserves_local_avoidance_proposal = bool(
        proposal_selected_action == "avoid_obstacle"
        and current_conflict.get("local_avoidance_required") is True
        and str(current_recovery.get("action") or "").strip().lower()
        == "safety_hold"
        and str(current_recovery.get("assist_status") or "").strip().lower()
        == "safety_hold_observed"
        and str(current_recovery.get("resume_status") or "").strip().lower()
        == "held_awaiting_operator_recovery_approval"
    )
    # A transient heartbeat gap while the aircraft is already in the bounded
    # safety HOLD invalidates immediate dispatch, not the human-visible
    # recovery judgment. Gateway revalidation still fails closed until fresh
    # telemetry returns. Replacing the proposal here caused an endless chain
    # of equivalent checkpoints during harmless two-poll gaps.
    materially_new_hard_breach = any(
        not (
            safety_hold_preserves_local_avoidance_proposal
            and key == "telemetry_lost"
        )
        for key in newly_observed_hard_breach_keys
    )
    local_conflict_requires_hold = bool(
        current_conflict.get("local_avoidance_required") is True
        and observation_state not in {"in_progress", "succeeded"}
    )
    safety_hold_observed = bool(
        safety_hold_receipt.get("request_status") == "observed"
        and observation_state == "held_awaiting_operator_approval"
    )
    safety_hold_stable = _runtime_recovery_safety_hold_stable(
        bridge=bridge,
        telemetry_snapshot=telemetry_snapshot,
    )
    proposal_materially_changed = bool(
        proposal_awaiting_approval
        and (
            materially_new_hard_breach
            or (
                nav_state_changed
                and not safety_hold_preserves_local_avoidance_proposal
            )
            or observation_state == "failed"
        )
    )
    if proposal_materially_changed:
        proposal_lifecycle_reasons.append(
            "runtime_recovery_proposal_superseded_by_material_change"
        )
    proposal_lifecycle_reasons = list(dict.fromkeys(proposal_lifecycle_reasons))
    proposal_invalidated = bool(
        receipt_invalidated_proposal or proposal_lifecycle_reasons
    )
    proposal_invalidated_without_new_epoch = bool(
        proposal_lifecycle_reasons
        and not receipt_invalidated_proposal
        and "runtime_recovery_proposal_superseded_by_material_change"
        not in proposal_lifecycle_reasons
    )
    if proposal_invalidated:
        proposal_awaiting_approval = False
    should_refresh = _should_refresh_auto_runtime_recovery_agent(
        bridge=bridge,
        telemetry_snapshot=telemetry_snapshot,
        refresh_seconds=active_refresh_seconds,
    )
    if has_news and "runtime_recovery_window_no_news" in prior_reasons:
        should_refresh = True
    if has_hard_news and not prior_agent_hard_news:
        should_refresh = True
    next_decision_signature = decision_signature
    proposal_recompiled = False
    if observation_state == "preflight":
        agent_invoked = False
        result = _runtime_recovery_agent_skipped_result(
            reason="runtime_recovery_preflight_telemetry_not_ready",
            detail="wait_for_first_complete_airborne_observation",
        )
        refresh_status = "preflight"
    elif observation_state == "landed":
        agent_invoked = False
        result = _runtime_recovery_agent_skipped_result(
            reason="runtime_recovery_vehicle_landed",
            detail="no_recovery_proposal_after_landing",
        )
        refresh_status = "landed"
    elif local_conflict_requires_hold and not safety_hold_observed:
        # Queue and observe the deterministic safety action before asking the
        # hosted model to judge the stabilized situation.  The HOLD is not a
        # proposal and creates no human dispatch authority.
        agent_invoked = False
        result = _runtime_recovery_agent_skipped_result(
            reason="runtime_recovery_waiting_for_safety_hold",
            detail=str(safety_hold_receipt.get("request_status") or "not_queued"),
        )
        refresh_status = "waiting_for_safety_hold"
        next_decision_signature = prior_decision_signature
    elif matching_dispatch_authority_recent:
        # The operator has bound a fresh authority to this exact proposal and
        # the request is queued for the active runner.  The runner executes the
        # bounded maneuver synchronously, so there can be a short interval
        # before a new telemetry snapshot reports in-progress/succeeded.  Do
        # not pay for or surface a second proposal during that handoff.
        agent_invoked = False
        result = _runtime_recovery_agent_skipped_result(
            reason="runtime_recovery_dispatch_pending_runner_observation",
            detail=str(last_proposal.get("proposal_id") or ""),
        )
        refresh_status = "dispatch_pending_runner_observation"
    elif (
        local_conflict_requires_hold
        and safety_hold_observed
        and not safety_hold_stable
        and not proposal_awaiting_approval
    ):
        # One fresh HOLD sample is not enough to distinguish a settled vehicle
        # from the mode transition itself. Persist it, then judge the next
        # fresh observation once.
        agent_invoked = False
        result = _runtime_recovery_agent_skipped_result(
            reason="runtime_recovery_safety_hold_settling",
            detail="waiting_for_second_fresh_hold_observation",
        )
        refresh_status = "safety_hold_settling"
        next_decision_signature = prior_decision_signature
    elif proposal_awaiting_approval:
        # The prior proposal is an immutable decision awaiting the human. Do
        # not ask the hosted model to propose the same action again while that
        # authority boundary is unresolved.
        agent_invoked = False
        result = dict(prior_result)
        refresh_status = "awaiting_operator_approval"
        next_decision_signature = prior_decision_signature or decision_signature
    elif (
        safety_hold_preserves_local_avoidance_proposal
        and (
            proposal_invalidated_without_new_epoch
            or (
                observation_state == "held_awaiting_operator_approval"
                and last_proposal.get("proposal_status")
                in {"stale", "superseded"}
            )
        )
    ):
        # The human may take longer than the proposal's freshness window while
        # the aircraft remains in the preauthorized safety HOLD. Recompile the
        # already judged avoid intent against the newest observation without a
        # second hosted-model call. This creates a fresh concrete proposal and
        # still requires a new human approval.
        agent_invoked = False
        proposal_recompiled = True
        result = _runtime_recovery_agent_fallback_result(
            telemetry_snapshot=telemetry_snapshot,
            task_id=task_id,
            reason="runtime_recovery_proposal_deterministic_recompile",
            detail=str(last_proposal.get("proposal_id") or ""),
        )
        refresh_status = "proposal_recompiled"
    elif observation_state == "in_progress":
        agent_invoked = False
        result = _runtime_recovery_agent_skipped_result(
            reason="runtime_recovery_action_in_progress",
            detail="wait_for_executor_observation_before_new_proposal",
        )
        refresh_status = "recovery_in_progress"
    elif observation_state == "succeeded":
        agent_invoked = False
        result = _runtime_recovery_agent_skipped_result(
            reason="runtime_recovery_action_succeeded",
            detail="wait_for_materially_new_runtime_risk",
        )
        refresh_status = "recovery_succeeded"
    elif not has_news and observation_state != "failed":
        agent_invoked = False
        result = _runtime_recovery_agent_skipped_result(
            reason="runtime_recovery_window_no_news",
            detail="window_summary_has_no_hard_or_soft_signals",
        )
        refresh_status = "no_news"
    elif proposal_invalidated_without_new_epoch:
        # Expiry or origin drift removes approval authority and releases the
        # proposal slot, but neither is by itself a reason to pay for an
        # identical hosted-model judgment. A new hard signal, operator refresh,
        # or blocked dispatch creates the next decision epoch.
        agent_invoked = False
        result = _runtime_recovery_agent_skipped_result(
            reason=str(
                proposal_lifecycle_reasons[0]
                if proposal_lifecycle_reasons
                else "runtime_recovery_proposal_stale"
            ),
            detail="invalidated_without_material_decision_change",
        )
        refresh_status = "proposal_stale"
        next_decision_signature = prior_decision_signature or decision_signature
    elif prior_decision_signature == decision_signature and not proposal_invalidated:
        agent_invoked = False
        result = _runtime_recovery_agent_skipped_result(
            reason="runtime_recovery_decision_unchanged",
            detail="thresholded_risk_signature_unchanged",
        )
        refresh_status = "decision_unchanged"
    elif not should_refresh and observation_state != "failed":
        agent_invoked = False
        result = _runtime_recovery_agent_skipped_result(
            reason="runtime_recovery_window_waiting",
            detail=f"refresh_seconds={active_refresh_seconds}",
        )
        refresh_status = "waiting"
    else:
        agent_invoked = True
        result = _run_auto_runtime_recovery_agent_with_timeout(
            telemetry_snapshot=telemetry_snapshot,
            task_id=task_id,
        )
        refresh_status = "agent_invoked"
    runtime_status = result.get("runtime_status")
    proposal_created = bool(
        (agent_invoked or proposal_recompiled)
        and runtime_status == "proposal_guardrail_passed"
    )
    last_agent_invoked_elapsed_seconds = bridge.get(
        "last_agent_invoked_elapsed_seconds"
    )
    last_agent_invoked_sample_index = bridge.get("last_agent_invoked_sample_index")
    last_agent_summary_hash = bridge.get("last_agent_recovery_window_summary_hash")
    last_agent_hard_breach_any = bridge.get("last_agent_hard_breach_any")
    if agent_invoked:
        last_agent_invoked_elapsed_seconds = telemetry_snapshot.get("elapsed_seconds")
        last_agent_invoked_sample_index = telemetry_snapshot.get("sample_index")
        last_agent_summary_hash = recovery_window_summary_hash
        last_agent_hard_breach_any = has_hard_news
    observed_at = observed_at_datetime.isoformat()
    attempt_evidence = _runtime_recovery_attempt_evidence(
        task_id=task_id,
        telemetry_snapshot=telemetry_snapshot,
        observation_state=observation_state,
        receipt=receipt,
        last_proposal=last_proposal,
        observed_at=observed_at,
    )
    existing_attempts = artifacts.get("missionos_runtime_recovery_attempts")
    existing_attempts = (
        existing_attempts if isinstance(existing_attempts, Mapping) else {}
    )
    attempt_evidence_missing = bool(
        attempt_evidence
        and attempt_evidence.get("attempt_id") not in existing_attempts
    )
    if proposal_created:
        proposal_observed_at = observed_at
        proposal_sample_index = telemetry_snapshot.get("sample_index")
    else:
        proposal_sample_index = bridge.get("proposal_sample_index")
    if (
        not agent_invoked
        and not proposal_created
        and bridge.get("agent_refresh_status") == refresh_status
        and bridge.get("recovery_observation_state") == observation_state
        and bridge.get("recovery_decision_signature") == decision_signature
        and not attempt_evidence_missing
    ):
        # The runtime snapshot is persisted independently. Avoid emitting a
        # duplicate agent-current-state artifact (and task timeline event)
        # when neither the decision nor recovery observation changed.
        return
    bridge_payload = {
        "schema_version": "missionos_runtime_recovery_agent_live_bridge.v1",
        "bridge_status": (
            "agent_not_configured"
            if runtime_status == "not_configured"
            else "proposal_skipped"
            if runtime_status == "proposal_skipped"
            else "proposal_attached"
        ),
        "telemetry_snapshot": telemetry_snapshot,
        "recovery_window_summary": recovery_window_summary,
        "recovery_window_summary_hash": recovery_window_summary_hash,
        "recovery_window_samples": window_samples,
        "active_refresh_seconds": active_refresh_seconds,
        "agent_refresh_status": refresh_status,
        "recovery_observation_state": observation_state,
        "recovery_decision_signature": decision_signature,
        "last_recovery_decision_signature": next_decision_signature,
        "last_agent_invoked_elapsed_seconds": last_agent_invoked_elapsed_seconds,
        "last_agent_invoked_sample_index": last_agent_invoked_sample_index,
        "last_agent_recovery_window_summary_hash": last_agent_summary_hash,
        "last_agent_hard_breach_any": last_agent_hard_breach_any,
        "proposal_observed_at": proposal_observed_at or None,
        "proposal_sample_index": proposal_sample_index,
        "invalidated_proposal_id": (
            last_proposal.get("proposal_id") if proposal_lifecycle_reasons else None
        ),
        "proposal_invalidation_reasons": proposal_lifecycle_reasons,
        "runtime_recovery_agent_result": result,
        "observed_at": observed_at,
        "dispatch_authority_created": False,
        "progress_counted": False,
    }
    artifact_updates: dict[str, Any] = {}
    replaced_artifacts: dict[str, Any] = {
        "missionos_runtime_recovery_agent_live_bridge": bridge_payload
    }
    if attempt_evidence is not None:
        attempt_id = str(attempt_evidence.get("attempt_id") or "")
        if attempt_id:
            artifact_updates["missionos_runtime_recovery_attempts"] = {
                attempt_id: attempt_evidence
            }
            replaced_artifacts["missionos_runtime_recovery_last_attempt"] = (
                attempt_evidence
            )
    if proposal_lifecycle_reasons and last_proposal:
        invalidated_proposal = {
            **dict(last_proposal),
            "proposal_status": (
                "superseded"
                if "runtime_recovery_proposal_superseded_by_material_change"
                in proposal_lifecycle_reasons
                else "stale"
            ),
            "invalidated_at": observed_at,
            "invalidation_reasons": proposal_lifecycle_reasons,
            "dispatch_authority_created": False,
            "progress_counted": False,
        }
        invalidated_proposal_id = str(
            invalidated_proposal.get("proposal_id") or ""
        )
        if invalidated_proposal_id:
            artifact_updates["missionos_runtime_recovery_proposals"] = {
                invalidated_proposal_id: invalidated_proposal
            }
        replaced_artifacts["missionos_runtime_recovery_last_proposal"] = (
            invalidated_proposal
        )
    if proposal_created:
        proposal_origin_position = telemetry_snapshot.get("position")
        proposal_origin_position = (
            dict(proposal_origin_position)
            if isinstance(proposal_origin_position, Mapping)
            else {}
        )
        proposal_id = _stable_id(
            "runtime_recovery_proposal",
            {
                "task_id": task_id,
                "observed_at": observed_at,
                "recovery_decision_signature": decision_signature,
            },
        )
        proposal_origin = _runtime_recovery_proposal_origin(
            result=result,
            proposal_recompiled=proposal_recompiled,
            source_proposal=last_proposal,
        )
        proposal_origin_kind = str(proposal_origin.get("origin_kind") or "")
        result_assessment = result.get("assessment")
        result_assessment = (
            dict(result_assessment)
            if isinstance(result_assessment, Mapping)
            else {}
        )
        recovery_intent = result_assessment.get("recovery_intent")
        recovery_intent = (
            dict(recovery_intent)
            if isinstance(recovery_intent, Mapping)
            else {}
        )
        intent_compilation = result_assessment.get("intent_compilation")
        intent_compilation = (
            dict(intent_compilation)
            if isinstance(intent_compilation, Mapping)
            else {}
        )
        reachability_verification = result_assessment.get(
            "reachability_verification"
        )
        reachability_verification = (
            dict(reachability_verification)
            if isinstance(reachability_verification, Mapping)
            else {}
        )
        proposal_evidence = {
            "schema_version": "missionos_runtime_recovery_proposal_evidence.v2",
            "proposal_id": proposal_id,
            "task_id": task_id,
            "proposal_status": "awaiting_operator_approval",
            "observed_at": observed_at,
            "valid_until": (
                observed_at_datetime
                + timedelta(seconds=_runtime_recovery_proposal_max_age_seconds())
            ).isoformat(),
            "origin_position": proposal_origin_position,
            "max_origin_drift_m": (
                _runtime_recovery_proposal_max_origin_drift_m()
            ),
            "sample_index": telemetry_snapshot.get("sample_index"),
            "recovery_decision_signature": decision_signature,
            "runtime_recovery_agent_result": result,
            "recovery_intent": recovery_intent,
            "recovery_intent_id": recovery_intent.get("recovery_intent_id"),
            "recovery_intent_sha256": recovery_intent.get(
                "recovery_intent_sha256"
            ),
            "intent_compilation": intent_compilation,
            "recovery_compilation_id": intent_compilation.get(
                "recovery_compilation_id"
            ),
            "recovery_compilation_sha256": intent_compilation.get(
                "recovery_compilation_sha256"
            ),
            "reachability_verification": reachability_verification,
            "recovery_reachability_id": reachability_verification.get(
                "recovery_reachability_id"
            ),
            "recovery_reachability_sha256": reachability_verification.get(
                "recovery_reachability_sha256"
            ),
            "proposal_origin": proposal_origin,
            "proposal_origin_sha256": proposal_origin["origin_sha256"],
            "proposal_source": (
                "deterministic_recompile_of_prior_llm_judgment"
                if proposal_recompiled
                else "hosted_runtime_recovery_judgment"
                if proposal_origin_kind == "hosted_llm"
                else "deterministic_guardrail_fallback"
                if proposal_origin_kind == "deterministic_guardrail_fallback"
                else proposal_origin_kind
            ),
            "source_proposal_id": (
                last_proposal.get("proposal_id") if proposal_recompiled else None
            ),
            "hosted_model_invoked_for_proposal": agent_invoked,
            "hosted_model_judgment_used_for_proposal": (
                proposal_origin_kind == "hosted_llm"
            ),
            "dispatch_authority_created": False,
            "progress_counted": False,
        }
        proposal_updates = artifact_updates.setdefault(
            "missionos_runtime_recovery_proposals",
            {},
        )
        proposal_updates[proposal_id] = proposal_evidence
        replaced_artifacts["missionos_runtime_recovery_last_proposal"] = (
            proposal_evidence
        )
    try:
        store.update(
            task_id,
            artifacts=artifact_updates or None,
            replace_artifacts=replaced_artifacts,
            metadata={
                "missionos_runtime_recovery_agent_last_sample_index": (
                    telemetry_snapshot.get("sample_index")
                ),
                "missionos_runtime_recovery_agent_status": runtime_status,
            },
        )
    except Exception:
        return


def _updated_auto_live_trajectory(
    *,
    existing: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one source-backed display sample without inventing continuity."""

    existing = existing if isinstance(existing, Mapping) else {}
    prior_samples = existing.get("samples")
    samples = [
        dict(item)
        for item in (prior_samples if isinstance(prior_samples, list) else [])
        if isinstance(item, Mapping)
    ]
    sample_index = snapshot.get("sample_index")
    elapsed_seconds = _auto_snapshot_number(snapshot.get("elapsed_seconds"))
    local_x_m = _auto_snapshot_number(snapshot.get("local_x_m"))
    local_y_m = _auto_snapshot_number(snapshot.get("local_y_m"))
    if local_x_m is None or local_y_m is None:
        return {
            **dict(existing),
            "schema_version": "missionos_auto_mission_live_trajectory.v1",
            "trajectory_status": "waiting_for_position",
            "read_only": True,
            "dispatch_authority_created": False,
            "progress_counted": False,
        }
    previous = samples[-1] if samples else {}
    previous_index = previous.get("sample_index")
    previous_index = (
        int(previous_index) if isinstance(previous_index, (int, float)) else None
    )
    previous_elapsed = _auto_snapshot_number(previous.get("elapsed_seconds"))
    segment_index = int(previous.get("segment_index") or 0) if previous else 0
    break_reason = ""
    if previous:
        if previous_index is not None and isinstance(sample_index, (int, float)):
            index_delta = int(sample_index) - previous_index
            if index_delta <= 0:
                break_reason = "sample_index_not_monotonic"
            elif index_delta > 5:
                break_reason = "sample_index_gap"
        if (
            not break_reason
            and previous_elapsed is not None
            and elapsed_seconds is not None
        ):
            elapsed_delta = elapsed_seconds - previous_elapsed
            if elapsed_delta <= 0:
                break_reason = "elapsed_time_not_monotonic"
            elif elapsed_delta > 15.0:
                break_reason = "elapsed_time_gap"
        if break_reason:
            segment_index += 1
    sample = {
        "sample_index": sample_index,
        "elapsed_seconds": elapsed_seconds,
        "segment_index": segment_index,
        "segment_break_reason": break_reason or None,
        "local_x_m": local_x_m,
        "local_y_m": local_y_m,
        "local_z_m": _auto_snapshot_number(snapshot.get("local_z_m")),
        "altitude_above_home_m": _auto_snapshot_number(
            snapshot.get("altitude_above_home_m")
        ),
        "phase": snapshot.get("phase") or snapshot.get("snapshot_status"),
        "battery_remaining_percent": _auto_snapshot_number(
            snapshot.get("battery_remaining_percent")
        ),
        "battery_state_source": snapshot.get("battery_state_source"),
        "battery_sample_accepted": snapshot.get("battery_sample_accepted"),
        "observed_at": snapshot.get("observed_at"),
        "source": "missionos_auto_mission_runtime_snapshot",
    }
    samples.append(sample)
    samples = samples[-MISSIONOS_AUTO_MISSION_LIVE_TRAJECTORY_MAX_SAMPLES:]
    segment_count = len(
        {
            int(item.get("segment_index") or 0)
            for item in samples
            if isinstance(item, Mapping)
        }
    )
    return {
        "schema_version": "missionos_auto_mission_live_trajectory.v1",
        "trajectory_status": "observed",
        "task_ref": snapshot.get("task_ref"),
        "sample_count": len(samples),
        "segment_count": segment_count,
        "latest_sample_index": sample_index,
        "samples": samples,
        "read_only": True,
        "dispatch_authority_created": False,
        "progress_counted": False,
        "delivery_completion_claimed": False,
        "physical_execution_invoked": False,
    }


def _persist_auto_live_telemetry_snapshot(
    *,
    task_id: str,
    artifact_root: Path,
    task_store_factory: Callable[[], TaskStore] | None,
    min_sample_index: int = -1,
) -> int:
    """Attach the AUTO probe's latest in-flight telemetry snapshot to the task.

    This is the AUTO analogue of ``_persist_live_telemetry_snapshot`` for the
    horizontal route. It is read-only evidence: it never claims delivery,
    progress, or physical execution, and it does not expose recovery dispatch
    controls. Returns the snapshot ``sample_index`` so the poll loop only
    re-attaches when fresh telemetry has arrived.
    """

    snapshot_path = _latest_auto_running_snapshot_path(artifact_root)
    if snapshot_path is None:
        return min_sample_index
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return min_sample_index
    if not isinstance(payload, Mapping):
        return min_sample_index
    sample_index = payload.get("sample_index")
    sample_index = int(sample_index) if isinstance(sample_index, (int, float)) else 0
    if sample_index <= min_sample_index:
        return min_sample_index
    snapshot_status = str(payload.get("snapshot_status") or "running")
    monitor_window_ended = bool(payload.get("monitor_window_ended"))
    snapshot = {
        "schema_version": "missionos_auto_mission_runtime_snapshot.v1",
        "snapshot_status": snapshot_status,
        "monitor_window_ended": monitor_window_ended,
        "monitor_stop_reason": payload.get("monitor_stop_reason"),
        "task_ref": _artifact_ref("task", task_id),
        "running_snapshot_path": str(snapshot_path),
        "sample_index": sample_index,
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "monitor_effective_elapsed_seconds": payload.get(
            "monitor_effective_elapsed_seconds"
        ),
        "monitor_excluded_recovery_seconds": payload.get(
            "monitor_excluded_recovery_seconds"
        ),
        "progress_m": payload.get("progress_m"),
        "mission_current_seq": payload.get("mission_current_seq"),
        "mission_reached_seq": payload.get("mission_reached_seq"),
        "waypoint_total": payload.get("waypoint_total"),
        "altitude_above_home_m": payload.get("altitude_above_home_m"),
        "local_x_m": payload.get("local_x_m"),
        "local_y_m": payload.get("local_y_m"),
        "local_z_m": payload.get("local_z_m"),
        "local_vx_mps": payload.get("local_vx_mps"),
        "local_vy_mps": payload.get("local_vy_mps"),
        "ground_speed_mps": payload.get("ground_speed_mps"),
        "distance_to_home_m": payload.get("distance_to_home_m"),
        "nav_state": payload.get("nav_state"),
        "battery_remaining_percent": payload.get("battery_remaining_percent"),
        "battery_remaining_first_percent": payload.get(
            "battery_remaining_first_percent"
        ),
        "battery_remaining_latest_percent": payload.get(
            "battery_remaining_latest_percent"
        ),
        "battery_remaining_delta_percent": payload.get(
            "battery_remaining_delta_percent"
        ),
        "battery_remaining_sample_count": payload.get(
            "battery_remaining_sample_count"
        ),
        "battery_remaining_dynamic": payload.get("battery_remaining_dynamic"),
        "battery_state_source": payload.get("battery_state_source"),
        "battery_sample_accepted": payload.get("battery_sample_accepted"),
        "battery_sample_rejected_reason": payload.get(
            "battery_sample_rejected_reason"
        ),
        "battery_warning": payload.get("battery_warning"),
        # Gazebo physical-battery observed signal (segment C). Kept separate
        # from the PX4 battery_status fields; both are SITL-simulated, not real
        # power-module endurance evidence.
        "gz_battery_state_observed": payload.get("gz_battery_state_observed"),
        "gz_battery_percent": payload.get("gz_battery_percent"),
        "gz_battery_voltage_v": payload.get("gz_battery_voltage_v"),
        "gz_battery_current_a": payload.get("gz_battery_current_a"),
        "gz_battery_charge_ah": payload.get("gz_battery_charge_ah"),
        "gz_battery_state_source": payload.get("gz_battery_state_source"),
        "gz_battery_read_error": payload.get("gz_battery_read_error"),
        "gz_battery_motor_coupling_requested": payload.get(
            "gz_battery_motor_coupling_requested"
        ),
        "heartbeat_observed": payload.get("heartbeat_observed"),
        "dropoff_dwell_candidate": payload.get("dropoff_dwell_candidate"),
        "wind_mean_started": payload.get("wind_mean_started"),
        "wind_mean_pending_reason": payload.get("wind_mean_pending_reason"),
        "wind_takeoff_clearance_min_altitude_m": payload.get(
            "wind_takeoff_clearance_min_altitude_m"
        ),
        "wind_mean_application_elapsed_seconds": payload.get(
            "wind_mean_application_elapsed_seconds"
        ),
        "wind_mean_application_altitude_m": payload.get(
            "wind_mean_application_altitude_m"
        ),
        "gazebo_obstacle_model_spawned": payload.get(
            "gazebo_obstacle_model_spawned"
        ),
        "gazebo_obstacle_model_spawn_requested": payload.get(
            "gazebo_obstacle_model_spawn_requested"
        ),
        "gazebo_obstacle_application_status": payload.get(
            "gazebo_obstacle_application_status"
        ),
        "obstacle_manifest": payload.get("obstacle_manifest"),
        "gazebo_obstacle_application": payload.get("gazebo_obstacle_application"),
        "operator_recovery_request_observed": payload.get(
            "operator_recovery_request_observed"
        ),
        "operator_recovery_action": payload.get("operator_recovery_action"),
        "operator_recovery_parameters": payload.get("operator_recovery_parameters"),
        "operator_recovery_command_ack_observed": payload.get(
            "operator_recovery_command_ack_observed"
        ),
        "operator_recovery_command_ack_result": payload.get(
            "operator_recovery_command_ack_result"
        ),
        "operator_recovery_path": payload.get("operator_recovery_path"),
        "operator_recovery_target": payload.get("operator_recovery_target"),
        "operator_recovery_assist_attempted": payload.get(
            "operator_recovery_assist_attempted"
        ),
        "operator_recovery_assist_status": payload.get(
            "operator_recovery_assist_status"
        ),
        "operator_recovery_assist_kind": payload.get("operator_recovery_assist_kind"),
        "operator_recovery_assist_offboard_ack_observed": payload.get(
            "operator_recovery_assist_offboard_ack_observed"
        ),
        "operator_recovery_assist_offboard_ack_result": payload.get(
            "operator_recovery_assist_offboard_ack_result"
        ),
        "operator_recovery_assist_offboard_state_observed": payload.get(
            "operator_recovery_assist_offboard_state_observed"
        ),
        "operator_recovery_assist_offboard_nav_state": payload.get(
            "operator_recovery_assist_offboard_nav_state"
        ),
        "operator_recovery_assist_setpoint_frames_sent": payload.get(
            "operator_recovery_assist_setpoint_frames_sent"
        ),
        "operator_recovery_assist_stream_duration_seconds": payload.get(
            "operator_recovery_assist_stream_duration_seconds"
        ),
        "operator_recovery_target_reached": payload.get(
            "operator_recovery_target_reached"
        ),
        "operator_recovery_target_distance_m": payload.get(
            "operator_recovery_target_distance_m"
        ),
        "operator_recovery_target_altitude_m": payload.get(
            "operator_recovery_target_altitude_m"
        ),
        "operator_recovery_altitude_error_m": payload.get(
            "operator_recovery_altitude_error_m"
        ),
        "operator_recovery_local_delta_x_m": payload.get(
            "operator_recovery_local_delta_x_m"
        ),
        "operator_recovery_local_delta_y_m": payload.get(
            "operator_recovery_local_delta_y_m"
        ),
        "operator_recovery_altitude_delta_m": payload.get(
            "operator_recovery_altitude_delta_m"
        ),
        "operator_recovery_terminal": payload.get("operator_recovery_terminal"),
        "operator_recovery_resume_auto_attempted": payload.get(
            "operator_recovery_resume_auto_attempted"
        ),
        "operator_recovery_resume_auto_ack_observed": payload.get(
            "operator_recovery_resume_auto_ack_observed"
        ),
        "operator_recovery_resume_auto_ack_result": payload.get(
            "operator_recovery_resume_auto_ack_result"
        ),
        "operator_recovery_resume_auto_nav_state_observed": payload.get(
            "operator_recovery_resume_auto_nav_state_observed"
        ),
        "operator_recovery_resume_auto_nav_state": payload.get(
            "operator_recovery_resume_auto_nav_state"
        ),
        "operator_recovery_resume_auto_status": payload.get(
            "operator_recovery_resume_auto_status"
        ),
        "operator_recovery_resume_safety_verification": payload.get(
            "operator_recovery_resume_safety_verification"
        ),
        "operator_recovery_assist_low_altitude_disarm_ack_observed": payload.get(
            "operator_recovery_assist_low_altitude_disarm_ack_observed"
        ),
        "operator_recovery_assist_low_altitude_disarm_ack_result": payload.get(
            "operator_recovery_assist_low_altitude_disarm_ack_result"
        ),
        "operator_recovery_assist_low_altitude_force_disarm_ack_observed": payload.get(
            "operator_recovery_assist_low_altitude_force_disarm_ack_observed"
        ),
        "operator_recovery_assist_low_altitude_force_disarm_ack_result": payload.get(
            "operator_recovery_assist_low_altitude_force_disarm_ack_result"
        ),
        "post_abort_tracking": payload.get("post_abort_tracking"),
        "post_abort_elapsed_seconds": payload.get("post_abort_elapsed_seconds"),
        "post_abort_observation_seconds": payload.get(
            "post_abort_observation_seconds"
        ),
        "post_abort_home_distance_delta_m": payload.get(
            "post_abort_home_distance_delta_m"
        ),
        "post_abort_altitude_delta_m": payload.get("post_abort_altitude_delta_m"),
        "post_abort_outcome_status": payload.get("post_abort_outcome_status"),
        "landed": payload.get("landed"),
        "maybe_landed": payload.get("maybe_landed"),
        "ground_contact": payload.get("ground_contact"),
        "arming_state": payload.get("arming_state"),
        "delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "read_only": True,
        "observed_at": _utc().isoformat(),
    }
    try:
        task_for_terrain = (task_store_factory or get_task_store)().get(task_id)
    except Exception:
        task_for_terrain = {}
    terrain_artifacts = (
        task_for_terrain.get("artifacts")
        if isinstance(task_for_terrain, Mapping)
        and isinstance(task_for_terrain.get("artifacts"), Mapping)
        else {}
    )
    terrain_projection = _auto_runtime_terrain_clearance_projection(
        snapshot,
        artifacts=terrain_artifacts,
    )
    if terrain_projection.get("projection_status") == "computed":
        snapshot.update(
            {
                "terrain_clearance_status": (
                    "below_minimum"
                    if terrain_projection.get("terrain_clearance_below_minimum")
                    else "ok"
                ),
                "terrain_elevation_m": terrain_projection.get("terrain_elevation_m"),
                "terrain_clearance_m": terrain_projection.get("terrain_clearance_m"),
                "terrain_clearance_target_m": terrain_projection.get(
                    "terrain_clearance_target_m"
                ),
                "terrain_clearance_margin_m": terrain_projection.get(
                    "terrain_clearance_margin_m"
                ),
                "terrain_clearance_below_minimum": terrain_projection.get(
                    "terrain_clearance_below_minimum"
                ),
            }
        )
    live_trajectory = _updated_auto_live_trajectory(
        existing=terrain_artifacts.get("missionos_auto_mission_live_trajectory"),
        snapshot=snapshot,
    )
    store = (task_store_factory or get_task_store)()
    store.update(
        task_id,
        replace_artifacts={
            "missionos_auto_mission_runtime_snapshot": snapshot,
            "missionos_auto_mission_live_trajectory": live_trajectory,
        },
        metadata={
            "missionos_auto_mission_runtime_snapshot_attached": True,
            "missionos_auto_mission_runtime_sample_index": sample_index,
            "missionos_auto_mission_recovery_agent_evidence_status": (
                "monitor_window_ended_telemetry"
                if monitor_window_ended
                else "running_telemetry"
            ),
        },
    )
    _attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task_id,
        snapshot=snapshot,
    )
    return sample_index


def run_missionos_auto_mission_gui_dispatch_execution(
    task_id: str,
    *,
    timeout_seconds: float = 2400.0,
    task_store_factory: Callable[[], TaskStore] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the operator-approved AUTO MissionOS runtime from the GUI boundary."""

    if not missionos_auto_mission_gui_dispatch_opted_in():
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"Set {MISSIONOS_AUTO_MISSION_GUI_DISPATCH_OPT_IN_ENV}=1 to run AUTO GUI dispatch"
        )
    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"task {task_id} not found; cannot run AUTO GUI dispatch"
        )
    if current.get("status") != "pending":
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "MissionOS AUTO GUI dispatch requires pending prepared task"
        )
    root = _repo_root()
    route = _coordinate_route_with_explicit_realism_env(
        _missionos_auto_coordinate_route(current)
    )
    route_compilation = compile_operator_coordinate_route_auto_mission(route)
    route_compilation_artifact = route_compilation.model_dump(mode="json")
    monitor_seconds = float(route_compilation.timeout_seconds)
    process_timeout_seconds = _auto_gui_dispatch_subprocess_timeout_seconds(
        compilation=route_compilation,
        requested_timeout_seconds=timeout_seconds,
    )
    artifact_root = root / "output" / "mission_designer_live_sitl_runs" / task_id / "auto_mission"
    artifact_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    realism_env = _coordinate_route_realism_env(current)
    env.update(realism_env)
    env[MISSIONOS_AUTO_MISSION_FULL_RUNTIME_PROBE_OPT_IN_ENV] = "1"
    env[MISSIONOS_AUTO_MISSION_L1_CARGO_ENV] = "1"
    # Wire the motor-load battery coupler so the gz battery signal tracks rotor
    # effort instead of the UI falling back to PX4's own SITL battery estimate.
    # Only enable when a built coupler .so is resolvable; otherwise leave the
    # coupler off and let the run report PX4 battery_status truthfully.
    coupler_plugin_so = _resolve_gz_coupler_plugin_so(root)
    if coupler_plugin_so is not None:
        env[MISSIONOS_AUTO_MISSION_GZ_PHYSICAL_BATTERY_ENV] = "1"
        env[MISSIONOS_AUTO_MISSION_GZ_BATTERY_MOTOR_COUPLING_ENV] = "1"
        env[MISSIONOS_AUTO_MISSION_GZ_COUPLER_PLUGIN_SO_ENV] = str(
            coupler_plugin_so.resolve()
        )
    env[MISSIONOS_AUTO_MISSION_OPERATOR_ROUTE_JSON_ENV] = json.dumps(
        route, sort_keys=True
    )
    env[MISSIONOS_AUTO_MISSION_ARTIFACT_ROOT_ENV] = str(artifact_root)
    operator_recovery_request_container_path = (
        _missionos_auto_operator_recovery_request_container_path(task_id)
    )
    env[MISSIONOS_AUTO_OPERATOR_RECOVERY_REQUEST_PATH_ENV] = (
        operator_recovery_request_container_path
    )
    env[MISSIONOS_AUTO_MISSION_MONITOR_SECONDS_ENV] = str(round(monitor_seconds, 3))
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(root)
        if not existing_pythonpath
        else os.pathsep.join([str(root), existing_pythonpath])
    )
    stdout_log = artifact_root / f"{task_id}_auto_stdout.log"
    stderr_log = artifact_root / f"{task_id}_auto_stderr.log"
    command = [
        sys.executable,
        str(root / MISSIONOS_AUTO_MISSION_FULL_RUNTIME_PROBE_SCRIPT),
    ]
    terrain_contact_verification_requested = _env_truthy(
        MISSIONOS_AUTO_TERRAIN_CONTACT_VERIFICATION_ENV
    )
    terrain_contact_verification = {
        "schema_version": "missionos_auto_terrain_contact_verification_request.v1",
        "verification_status": (
            "requested" if terrain_contact_verification_requested else "not_requested"
        ),
        "opt_in_env": MISSIONOS_AUTO_TERRAIN_CONTACT_VERIFICATION_ENV,
        "source_backed_terrain_required": True,
        "contact_event_claimed": False,
        "delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "observed_at": _utc(now).isoformat(),
    }
    running_receipt = {
        "schema_version": "missionos_auto_mission_gui_dispatch_running_receipt.v1",
        "task_id": task_id,
        "dispatch_status": "running",
        "runner_script": MISSIONOS_AUTO_MISSION_FULL_RUNTIME_PROBE_SCRIPT,
        "artifact_root": str(artifact_root.relative_to(root)),
        "stdout_log": str(stdout_log.relative_to(root)),
        "stderr_log": str(stderr_log.relative_to(root)),
        "operator_route_ref": f"mission_designer_coordinate_pair_route:{route.get('route_id')}",
        "auto_mission_gui_dispatch_opted_in": True,
        "l1_gazebo_cargo_enabled": True,
        "gz_battery_motor_coupling_enabled": coupler_plugin_so is not None,
        "gz_coupler_plugin_so": (
            str(coupler_plugin_so.resolve())
            if coupler_plugin_so is not None
            else None
        ),
        "monitor_seconds_env": MISSIONOS_AUTO_MISSION_MONITOR_SECONDS_ENV,
        "monitor_seconds": monitor_seconds,
        "process_timeout_seconds": process_timeout_seconds,
        "operator_recovery_request_path_env": (
            MISSIONOS_AUTO_OPERATOR_RECOVERY_REQUEST_PATH_ENV
        ),
        "operator_recovery_request_container_path": (
            operator_recovery_request_container_path
        ),
        "operator_recovery_request_path_kind": "px4_gazebo_container_file",
        "terrain_contact_verification_requested": terrain_contact_verification_requested,
        "terrain_contact_verification_status": terrain_contact_verification[
            "verification_status"
        ],
        "terrain_contact_verification_opt_in_env": (
            MISSIONOS_AUTO_TERRAIN_CONTACT_VERIFICATION_ENV
        ),
        "coordinate_route_realism_env": dict(sorted(realism_env.items())),
        "recovery_agent_evidence_status": "pending",
        "delivery_completion_claimed": False,
        "physical_delivery_verified": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": _utc(now).isoformat(),
    }
    store.update(
        task_id,
        status="running",
        artifacts={
            "missionos_auto_mission_gui_dispatch_running_receipt": running_receipt,
            "missionos_auto_mission_compilation": route_compilation_artifact,
            "missionos_auto_terrain_contact_verification_request": (
                terrain_contact_verification
            ),
        },
        metadata={
            "missionos_auto_mission_gui_dispatch_invoked": True,
            "missionos_auto_mission_gui_dispatch_status": "running",
            "missionos_auto_terrain_contact_verification_requested": (
                terrain_contact_verification_requested
            ),
            "missionos_auto_mission_recovery_agent_evidence_status": "pending",
            "missionos_auto_mission_monitor_seconds": monitor_seconds,
            "missionos_auto_mission_process_timeout_seconds": process_timeout_seconds,
            "delivery_completion_claimed": False,
            "physical_delivery_verified": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    )
    with stdout_log.open("w") as stdout_file, stderr_log.open("w") as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            text=True,
            stdout=stdout_file,
            stderr=stderr_file,
        )
        # The AUTO route flies for tens of minutes, so the final verifier summary
        # is not attached until the run ends. Poll the probe's in-flight snapshot
        # once per second so the GUI Runtime Recovery Agent view can render live
        # progress/position/battery instead of only the pending receipt. This is
        # the AUTO analogue of the horizontal route live-snapshot polling.
        deadline = time.monotonic() + process_timeout_seconds
        last_sample_index = -1
        while process.poll() is None:
            if time.monotonic() > deadline:
                process.kill()
                process.wait(timeout=5)
                raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
                    "MissionOS AUTO GUI dispatch timed out: "
                    f"timeout_seconds={process_timeout_seconds}; "
                    f"stdout_log={stdout_log}; stderr_log={stderr_log}"
                )
            try:
                last_sample_index = _persist_auto_live_telemetry_snapshot(
                    task_id=task_id,
                    artifact_root=artifact_root,
                    task_store_factory=task_store_factory,
                    min_sample_index=last_sample_index,
                )
            except Exception:  # pragma: no cover - snapshot attach must never abort the run.
                pass
            time.sleep(1.0)
        process.wait(timeout=5)
        try:
            _persist_auto_live_telemetry_snapshot(
                task_id=task_id,
                artifact_root=artifact_root,
                task_store_factory=task_store_factory,
                min_sample_index=last_sample_index,
            )
        except Exception:  # pragma: no cover - final snapshot attach is best-effort evidence.
            pass
    if process.returncode != 0:
        stderr_tail = _tail_text(stderr_log)
        stdout_tail = _tail_text(stdout_log)
        digest = _runner_failure_digest(stdout_tail, stderr_tail)
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            "MissionOS AUTO GUI dispatch failed: "
            f"reason={digest}; stdout_log={stdout_log}; stderr_log={stderr_log}"
        )
    payload = _latest_auto_mission_summary(artifact_root)
    runtime_summary = _auto_gate(payload, "summary")
    waypoint_gate = _auto_gate(payload, "waypoint_gate")
    dropoff_gate = _auto_gate(payload, "dropoff_gate")
    sitl_delivery_gate = _auto_gate(payload, "sitl_delivery_gate")
    payload_release_sim_gate = _auto_gate(payload, "payload_release_sim_gate")
    payload_release_event = _auto_gate(payload, "payload_release_event")
    final_verification_chain = _auto_gate(payload, "final_verification_chain")
    runtime_recovery_provenance = _auto_gate(
        payload, "runtime_recovery_provenance"
    )
    upload_observed = _auto_gate(payload, "upload_observed")
    probe_observed = _auto_gate(payload, "probe_observed")
    compilation = _auto_gate(payload, "compilation")
    compilation = {**route_compilation_artifact, **compilation}
    px4_home_alignment = _auto_gate(payload, "px4_home_alignment")
    guard_config = _auto_gate(payload, "guard_config")
    thermal_weather_condition_profile = _auto_gate(
        payload, "thermal_weather_condition_profile"
    )
    thermal_weather_simulator_capability_matrix = _auto_gate(
        payload, "thermal_weather_simulator_capability_matrix"
    )
    thermal_weather_simulator_condition_application = _auto_gate(
        payload, "thermal_weather_simulator_condition_application"
    )
    observed_thermal_weather_evidence = _auto_gate(
        payload, "observed_thermal_weather_evidence"
    )
    rain_weather_condition_profile = _auto_gate(
        payload, "rain_weather_condition_profile"
    )
    rain_weather_simulator_capability_matrix = _auto_gate(
        payload, "rain_weather_simulator_capability_matrix"
    )
    rain_weather_simulator_condition_application = _auto_gate(
        payload, "rain_weather_simulator_condition_application"
    )
    observed_rain_weather_evidence = _auto_gate(
        payload, "observed_rain_weather_evidence"
    )
    environment_condition_profile = _auto_gate(payload, "environment_condition_profile")
    simulator_capability_matrix = _auto_gate(payload, "simulator_capability_matrix")
    simulator_condition_application = _auto_gate(
        payload, "simulator_condition_application"
    )
    observed_environment_evidence = _auto_gate(
        payload, "observed_environment_evidence"
    )
    runtime_replay = _build_auto_mission_runtime_replay_artifact(
        task_id=task_id,
        payload=payload,
        observed_at=now,
    )
    terminal_status, terminal_blocked_reasons = _auto_gui_dispatch_terminal_status(
        runtime_summary,
        waypoint_gate,
        dropoff_gate,
        sitl_delivery_gate,
    )
    summary = {
        **runtime_summary,
        "task_id": task_id,
        "task_status": terminal_status,
        "live_flight_status": terminal_status,
        "auto_mission_process_status": "completed",
        "auto_mission_terminal_gates_passed": terminal_status == "completed",
        "auto_mission_terminal_status_reason": (
            "sitl_delivery_gate_passed"
            if terminal_status == "completed"
            else "auto_mission_terminal_gates_pending"
        ),
        "blocked_reasons": terminal_blocked_reasons,
        "live_flight_mode_requested": True,
        "live_flight_runner_invoked": True,
        "auto_mission_gui_dispatch_invoked": True,
        "auto_mission_artifact_dir": payload.get("artifact_dir"),
        "auto_mission_stdout_log": str(stdout_log.relative_to(root)),
        "auto_mission_stderr_log": str(stderr_log.relative_to(root)),
        "auto_mission_monitor_seconds": monitor_seconds,
        "auto_mission_process_timeout_seconds": process_timeout_seconds,
        "terrain_contact_verification_requested": terrain_contact_verification_requested,
        "terrain_contact_verification_status": terrain_contact_verification[
            "verification_status"
        ],
        "route_completed_claimed": waypoint_gate.get("route_completed_claimed", False),
        "dropoff_verified": dropoff_gate.get("dropoff_verified", False),
        "dropoff_verification_basis": (
            "phase5_monitor_telemetry_release_envelope"
        ),
        "sitl_delivery_claimed": sitl_delivery_gate.get(
            "sitl_delivery_claimed", False
        ),
        "payload_release_observed_sim": payload_release_sim_gate.get(
            "payload_release_observed_sim", False
        ),
        "delivery_completion_claimed": False,
        "physical_delivery_verified": False,
        "actual_sitl_flight_evidence_observed": bool(
            runtime_summary.get("auto_mission_started")
            and runtime_summary.get("telemetry_sample_count", 0)
        ),
        "px4_mission_upload_performed": bool(
            runtime_summary.get("mission_upload_accepted")
        ),
        "mavlink_dispatch_performed": True,
        "external_dispatch_performed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "final_verification_status": final_verification_chain.get(
            "final_verification_status"
        ),
        "runtime_recovery_provenance_status": runtime_recovery_provenance.get(
            "provenance_status"
        ),
        "runtime_recovery_proposal_origin_kind": (
            runtime_recovery_provenance.get("proposal_origin", {}).get(
                "origin_kind"
            )
            if isinstance(
                runtime_recovery_provenance.get("proposal_origin"), Mapping
            )
            else None
        ),
    }
    recovery_agent_telemetry_snapshot = runtime_summary.get(
        "recovery_agent_telemetry_snapshot"
    )
    recovery_agent_evidence_window_path = runtime_summary.get(
        "recovery_agent_evidence_window_path"
    )
    guard_failure_reasons = runtime_summary.get("guard_failure_reasons") or ()
    recovery_was_guard_response = bool(
        runtime_summary.get("guard_abort_requested") or guard_failure_reasons
    )
    recovery_agent_evidence_status = (
        (
            "completed_recovery_evidence"
            if recovery_was_guard_response
            else "completed_post_run_return_evidence"
        )
        if recovery_agent_telemetry_snapshot or recovery_agent_evidence_window_path
        else "completed_without_recovery_evidence"
    )
    artifacts = {
        "missionos_auto_mission_runtime_monitor_summary": runtime_summary,
        "missionos_auto_mission_waypoint_gate_summary": waypoint_gate,
        "missionos_auto_mission_dropoff_gate_summary": dropoff_gate,
        "missionos_auto_mission_sitl_delivery_gate_summary": sitl_delivery_gate,
        "missionos_auto_mission_payload_release_sim_gate_summary": payload_release_sim_gate,
        "missionos_auto_mission_payload_release_event": payload_release_event,
        "missionos_auto_mission_final_verification_chain": (
            final_verification_chain
        ),
        "missionos_runtime_recovery_run_provenance": (
            runtime_recovery_provenance
        ),
        "missionos_auto_mission_upload_observed": upload_observed,
        "missionos_auto_mission_probe_observed": probe_observed,
        "missionos_auto_mission_compilation": compilation,
        "missionos_auto_mission_px4_home_alignment": px4_home_alignment,
        "missionos_auto_mission_guard_config": guard_config,
        "missionos_auto_environment_condition_profile": environment_condition_profile,
        "missionos_auto_simulator_capability_matrix": simulator_capability_matrix,
        "missionos_auto_simulator_condition_application": (
            simulator_condition_application
        ),
        "missionos_auto_observed_environment_evidence": observed_environment_evidence,
        "missionos_auto_thermal_weather_condition_profile": (
            thermal_weather_condition_profile
        ),
        "missionos_auto_thermal_weather_simulator_capability_matrix": (
            thermal_weather_simulator_capability_matrix
        ),
        "missionos_auto_thermal_weather_simulator_condition_application": (
            thermal_weather_simulator_condition_application
        ),
        "missionos_auto_observed_thermal_weather_evidence": (
            observed_thermal_weather_evidence
        ),
        "missionos_auto_rain_weather_condition_profile": rain_weather_condition_profile,
        "missionos_auto_rain_weather_simulator_capability_matrix": (
            rain_weather_simulator_capability_matrix
        ),
        "missionos_auto_rain_weather_simulator_condition_application": (
            rain_weather_simulator_condition_application
        ),
        "missionos_auto_observed_rain_weather_evidence": (
            observed_rain_weather_evidence
        ),
        "missionos_auto_terrain_contact_verification_request": (
            terrain_contact_verification
        ),
        "missionos_auto_mission_gui_dispatch_receipt": {
            "schema_version": "missionos_auto_mission_gui_dispatch_receipt.v1",
            "task_id": task_id,
            "dispatch_status": terminal_status,
            "auto_mission_process_status": "completed",
            "auto_mission_terminal_gates_passed": terminal_status == "completed",
            "blocked_reasons": terminal_blocked_reasons,
            "runner_script": MISSIONOS_AUTO_MISSION_FULL_RUNTIME_PROBE_SCRIPT,
            "artifact_dir": payload.get("artifact_dir"),
            "operator_route_ref": f"mission_designer_coordinate_pair_route:{route.get('route_id')}",
            "auto_mission_gui_dispatch_opted_in": True,
            "l1_gazebo_cargo_enabled": True,
            "monitor_seconds_env": MISSIONOS_AUTO_MISSION_MONITOR_SECONDS_ENV,
            "monitor_seconds": monitor_seconds,
            "process_timeout_seconds": process_timeout_seconds,
            "terrain_contact_verification_requested": (
                terrain_contact_verification_requested
            ),
            "terrain_contact_verification_status": terrain_contact_verification[
                "verification_status"
            ],
            "terrain_contact_verification_opt_in_env": (
                MISSIONOS_AUTO_TERRAIN_CONTACT_VERIFICATION_ENV
            ),
            "recovery_agent_evidence_status": recovery_agent_evidence_status,
            "recovery_agent_evidence_window_path": recovery_agent_evidence_window_path,
            "recovery_agent_telemetry_snapshot": recovery_agent_telemetry_snapshot,
            "recovery_path_taken": runtime_summary.get("recovery_path_taken"),
            "recovery_command_ack_observed": runtime_summary.get(
                "recovery_command_ack_observed"
            ),
            "recovery_command_ack_result": runtime_summary.get(
                "recovery_command_ack_result"
            ),
            "thermal_weather_application_status": (
                thermal_weather_simulator_condition_application.get(
                    "application_status"
                )
            ),
            "thermal_weather_observation_status": (
                observed_thermal_weather_evidence.get("observation_status")
            ),
            "rain_weather_application_status": (
                rain_weather_simulator_condition_application.get(
                    "application_status"
                )
            ),
            "rain_weather_observation_status": (
                observed_rain_weather_evidence.get("observation_status")
            ),
            "wind_gust_application_status": simulator_condition_application.get(
                "application_status"
            ),
            "wind_gust_observation_status": observed_environment_evidence.get(
                "observation_status"
            ),
            "final_landing_safe": runtime_summary.get("final_landing_safe"),
            "delivery_completion_claimed": False,
            "physical_delivery_verified": False,
            "observed_at": _utc(now).isoformat(),
        },
    }
    if runtime_replay:
        artifacts["missionos_auto_mission_runtime_replay"] = runtime_replay
    updated = store.update(
        task_id,
        status=terminal_status,
        artifacts=artifacts,
        metadata={
            "missionos_auto_mission_gui_dispatch_invoked": True,
            "missionos_auto_mission_gui_dispatch_status": terminal_status,
            "missionos_auto_mission_process_status": "completed",
            "missionos_auto_mission_terminal_gates_passed": (
                terminal_status == "completed"
            ),
            "missionos_auto_mission_terminal_blocked_reasons": (
                terminal_blocked_reasons
            ),
            "missionos_auto_mission_artifact_dir": payload.get("artifact_dir"),
            "missionos_auto_terrain_contact_verification_requested": (
                terrain_contact_verification_requested
            ),
            "missionos_auto_mission_runtime_replay_attached": bool(runtime_replay),
            "missionos_auto_mission_runtime_replay_sample_count": runtime_replay.get(
                "sample_count", 0
            )
            if runtime_replay
            else 0,
            "missionos_auto_mission_recovery_agent_evidence_status": (
                recovery_agent_evidence_status
            ),
            "actual_sitl_flight_evidence_observed": summary[
                "actual_sitl_flight_evidence_observed"
            ],
            "route_completed_claimed": summary["route_completed_claimed"],
            "dropoff_verified": summary["dropoff_verified"],
            "dropoff_verification_basis": summary[
                "dropoff_verification_basis"
            ],
            "sitl_delivery_claimed": summary["sitl_delivery_claimed"],
            "payload_release_observed_sim": summary["payload_release_observed_sim"],
            "missionos_auto_thermal_weather_application_status": (
                thermal_weather_simulator_condition_application.get(
                    "application_status"
                )
            ),
            "missionos_auto_thermal_weather_observation_status": (
                observed_thermal_weather_evidence.get("observation_status")
            ),
            "missionos_auto_rain_weather_application_status": (
                rain_weather_simulator_condition_application.get(
                    "application_status"
                )
            ),
            "missionos_auto_rain_weather_observation_status": (
                observed_rain_weather_evidence.get("observation_status")
            ),
            "missionos_auto_wind_gust_application_status": (
                simulator_condition_application.get("application_status")
            ),
            "missionos_auto_wind_gust_observation_status": (
                observed_environment_evidence.get("observation_status")
            ),
            "delivery_completion_claimed": False,
            "physical_delivery_verified": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    )
    return {
        "task": updated or current,
        "summary": summary,
        **artifacts,
    }


def run_px4_gazebo_mission_designer_live_sitl_flight_execution(
    task_id: str,
    *,
    horizontal_runner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the live horizontal-route mode and persist its live-run artifact."""

    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"task {task_id} not found; cannot run live SITL flight"
        )
    runner = horizontal_runner or (
        lambda task: run_px4_gazebo_horizontal_route_live_summary(
            task_id=str(task["task_id"]),
            task_store_factory=task_store_factory,
            task=task,
        )
    )
    raw_summary = runner(current)
    stamped_summary = stamp_mission_designer_live_sitl_horizontal_summary(
        task=current,
        horizontal_summary=raw_summary,
    )
    attached = attach_px4_gazebo_mission_designer_sitl_live_flight_run(
        task_id,
        horizontal_summary=stamped_summary,
        task_store_factory=task_store_factory,
        now=now,
    )
    flight_attached = attach_px4_gazebo_mission_designer_sitl_flight_evidence(
        task_id,
        horizontal_summary=stamped_summary,
        task_store_factory=task_store_factory,
        now=now,
    )
    payload_attached = (
        attach_px4_gazebo_mission_designer_sitl_payload_release_observation(
            task_id,
            horizontal_summary=stamped_summary,
            task_store_factory=task_store_factory,
            now=now,
        )
    )
    dropoff_attached = attach_px4_gazebo_mission_designer_sitl_dropoff_verification(
        task_id,
        horizontal_summary=stamped_summary,
        task_store_factory=task_store_factory,
        now=now,
    )
    exit_attached = attach_px4_gazebo_mission_designer_sitl_delivery_epic_exit_result(
        task_id,
        prompt=_task_prompt(current),
        upload_only_delivery_success_rejected=True,
        missing_flight_delivery_success_rejected=True,
        missing_payload_release_delivery_success_rejected=True,
        missing_dropoff_delivery_success_rejected=True,
        task_store_factory=task_store_factory,
        now=now,
    )
    live_run = attached["px4_gazebo_mission_designer_sitl_live_flight_run"]
    attached["task"] = exit_attached["task"]
    attached.update(
        {
            "px4_gazebo_mission_designer_sitl_flight_evidence": flight_attached[
                "px4_gazebo_mission_designer_sitl_flight_evidence"
            ],
            "px4_gazebo_mission_designer_sitl_execution_result": flight_attached[
                "px4_gazebo_mission_designer_sitl_execution_result"
            ],
            "px4_gazebo_sitl_payload_release_event": payload_attached[
                "px4_gazebo_sitl_payload_release_event"
            ],
            "px4_gazebo_mission_designer_sitl_payload_release_observation": (
                payload_attached[
                    "px4_gazebo_mission_designer_sitl_payload_release_observation"
                ]
            ),
            "px4_gazebo_sitl_dropoff_flight_fact": dropoff_attached[
                "px4_gazebo_sitl_dropoff_flight_fact"
            ],
            "px4_gazebo_sitl_dropoff_verification": dropoff_attached[
                "px4_gazebo_sitl_dropoff_verification"
            ],
            "px4_gazebo_mission_designer_sitl_dropoff_verification": (
                dropoff_attached[
                    "px4_gazebo_mission_designer_sitl_dropoff_verification"
                ]
            ),
            "px4_gazebo_mission_designer_sitl_delivery_epic_exit": exit_attached[
                "px4_gazebo_mission_designer_sitl_delivery_epic_exit"
            ],
        }
    )
    attached["summary"] = {
        **attached["summary"],
        **flight_attached["summary"],
        **payload_attached["summary"],
        **dropoff_attached["summary"],
        **exit_attached["summary"],
        "live_flight_status": "completed",
        "live_flight_opted_in": True,
        "live_flight_runner_invoked": True,
        "failure_reasons": [],
        "actual_px4_gazebo_horizontal_smoke_observed": live_run[
            "actual_px4_gazebo_horizontal_smoke_observed"
        ],
        "actual_sitl_flight_evidence_observed": live_run[
            "actual_sitl_flight_evidence_observed"
        ],
    }
    return attached


def _task_prompt(task: Mapping[str, Any]) -> str:
    artifacts = task.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    proposal = artifacts.get("px4_gazebo_mission_scenario_proposal")
    proposal = proposal if isinstance(proposal, Mapping) else {}
    prompt = str(proposal.get("mission_objective") or "").strip()
    return prompt or "Mission Designer live SITL execution"


def attach_px4_gazebo_mission_designer_sitl_live_flight_run(
    task_id: str,
    *,
    horizontal_summary: Mapping[str, Any],
    task_store_factory: Callable[[], TaskStore] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"task {task_id} not found; cannot attach live SITL flight run"
        )
    live_run = build_px4_gazebo_mission_designer_sitl_live_flight_run(
        task=current,
        horizontal_summary=horizontal_summary,
        observed_at=now,
    )
    live_run_payload = live_run.model_dump(mode="json")
    realism_artifacts = {
        key: live_run_payload[key]
        for key in (
            "environment_condition_profile",
            "simulator_capability_matrix",
            "simulator_condition_application",
            "observed_environment_evidence",
            "scenario_cleanup_receipt",
            "vehicle_condition_profile",
            "payload_simulator_capability_matrix",
            "payload_simulator_condition_application",
            "observed_vehicle_condition_evidence",
            "battery_condition_profile",
            "battery_simulator_capability_matrix",
            "battery_simulator_condition_application",
            "observed_battery_condition_evidence",
            "thermal_weather_condition_profile",
            "thermal_weather_simulator_capability_matrix",
            "thermal_weather_simulator_condition_application",
            "observed_thermal_weather_evidence",
            "rain_weather_condition_profile",
            "rain_weather_simulator_capability_matrix",
            "rain_weather_simulator_condition_application",
            "observed_rain_weather_evidence",
            "sensor_condition_profile",
            "sensor_simulator_capability_matrix",
            "sensor_failure_injection_application",
            "observed_sensor_condition_evidence",
            "gazebo_world_condition_profile",
            "gazebo_world_capability_matrix",
            "gazebo_world_application",
            "obstacle_manifest",
            "observed_world_condition_evidence",
            "visibility_condition_profile",
            "visibility_capability_matrix",
            "visibility_application",
            "observed_visibility_condition_evidence",
            "operational_condition_profile",
            "geofence_condition_profile",
            "traffic_conflict_profile",
            "alternate_landing_profile",
            "dynamic_actor_profile",
            "moving_actor_waypoint_motion_application",
            "moving_actor_pose_observation",
            "moving_actor_proximity_evidence",
            "operational_capability_matrix",
            "operational_application",
            "observed_operational_condition_evidence",
            "telemetry_degradation_profile",
            "telemetry_degradation_application",
            "observed_telemetry_gap_evidence",
            "telemetry_freshness_report",
            "mavlink_link_degradation_profile",
            "mavlink_link_degradation_capability_matrix",
            "mavlink_link_degradation_application",
            "observed_mavlink_gap_evidence",
        )
        if live_run_payload.get(key)
    }
    updated = store.update(
        task_id,
        artifacts={
            "px4_gazebo_mission_designer_sitl_live_flight_run": live_run_payload,
            **realism_artifacts,
        },
        metadata={
            "mission_designer_live_sitl_flight_run_attached": True,
            "mission_designer_live_sitl_run_id": live_run.live_run_id,
            "mission_item_binding_sha256": live_run.mission_item_binding_sha256,
            "live_target_binding_sha256": live_run.live_target_binding_sha256,
            "preexisting_summary_input_allowed": False,
            "same_gateway_execution_run_observed": True,
        },
    )
    if updated is None:
        raise PX4GazeboMissionDesignerSITLLiveFlightRunError(
            f"task {task_id} disappeared while attaching live SITL flight run"
        )
    return {
        "task": updated,
        "px4_gazebo_mission_designer_sitl_live_flight_run": live_run_payload,
        **realism_artifacts,
        "summary": {
            "task_id": updated["task_id"],
            "task_status": updated["status"],
            "live_flight_run_ref": _artifact_ref(
                "px4_gazebo_mission_designer_sitl_live_flight_run",
                live_run.live_flight_run_id,
            ),
            "live_run_id": live_run.live_run_id,
            "mission_item_binding_sha256": live_run.mission_item_binding_sha256,
            "live_target_binding_sha256": live_run.live_target_binding_sha256,
            "preexisting_summary_input_allowed": False,
            "same_gateway_execution_run_observed": True,
            "actual_px4_gazebo_horizontal_smoke_observed": (
                live_run.actual_px4_gazebo_horizontal_smoke_observed
            ),
            "actual_sitl_flight_evidence_observed": (
                live_run.actual_sitl_flight_evidence_observed
            ),
            "actual_dropoff_region_reached": live_run.dropoff_region_reached,
            "dropoff_region_reached": live_run.dropoff_region_reached,
            "horizontal_progress_m": live_run.horizontal_progress_m,
            "completed_pose_z_m": live_run.completed_pose_z_m,
            "payload_release_observed": live_run.payload_release_observed,
            "payload_release_event_source": live_run.payload_release_event_source,
            "wind_gust_application_status": live_run.simulator_condition_application.get(
                "application_status", ""
            ),
            "wind_gust_observation_status": live_run.observed_environment_evidence.get(
                "observation_status", ""
            ),
            "scenario_cleanup_status": live_run.scenario_cleanup_receipt.get(
                "cleanup_status", ""
            ),
            "payload_mass_application_status": live_run.payload_simulator_condition_application.get(
                "application_status", ""
            ),
            "payload_mass_observation_status": live_run.observed_vehicle_condition_evidence.get(
                "observation_status", ""
            ),
            "thermal_weather_application_status": live_run.thermal_weather_simulator_condition_application.get(
                "application_status", ""
            ),
            "thermal_weather_observation_status": live_run.observed_thermal_weather_evidence.get(
                "observation_status", ""
            ),
            "rain_weather_application_status": live_run.rain_weather_simulator_condition_application.get(
                "application_status", ""
            ),
            "rain_weather_observation_status": live_run.observed_rain_weather_evidence.get(
                "observation_status", ""
            ),
            "failure_reasons": (
                ()
                if live_run.payload_release_observed and live_run.dropoff_region_reached
                else ("live_sitl_flight_evidence_incomplete",)
            ),
            "mission_ack_observed": live_run.mission_ack_observed,
            "mission_ack_type": live_run.mission_ack_type,
            "mission_request_sequences": live_run.mission_request_sequences,
            "hardware_target_allowed": live_run.hardware_target_allowed,
            "physical_execution_invoked": live_run.physical_execution_invoked,
            "synthetic_success_allowed": live_run.synthetic_success_allowed,
        },
    }


__all__ = [
    "MISSION_DESIGNER_LIVE_SITL_FLIGHT_OPT_IN_ENV",
    "MISSION_DESIGNER_LIVE_SITL_RUN_SOURCE",
    "MISSIONOS_AUTO_MISSION_GUI_DISPATCH_OPT_IN_ENV",
    "MissionDesignerSITLLiveTargetBinding",
    "PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_BLOCKED_RECEIPT_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_FAILED_RECEIPT_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_RUN_SCHEMA_VERSION",
    "PX4GazeboMissionDesignerSITLLiveFlightBlockedReceipt",
    "PX4GazeboMissionDesignerSITLLiveFlightFailedReceipt",
    "PX4GazeboMissionDesignerSITLLiveFlightRun",
    "PX4GazeboMissionDesignerSITLLiveFlightRunError",
    "attach_px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt",
    "attach_px4_gazebo_mission_designer_sitl_live_flight_failed_receipt",
    "attach_px4_gazebo_mission_designer_sitl_live_flight_run",
    "build_px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt",
    "build_px4_gazebo_mission_designer_sitl_live_flight_failed_receipt",
    "build_mission_designer_sitl_live_target_binding",
    "build_px4_gazebo_mission_designer_sitl_live_flight_run",
    "mission_designer_live_sitl_flight_opted_in",
    "missionos_auto_mission_gui_dispatch_opted_in",
    "mission_designer_sitl_live_target_binding_sha256",
    "mission_designer_sitl_mission_item_binding_sha256",
    "run_missionos_auto_mission_gui_dispatch_execution",
    "run_px4_gazebo_horizontal_route_live_summary",
    "run_px4_gazebo_mission_designer_live_sitl_flight_execution",
    "stamp_mission_designer_live_sitl_horizontal_summary",
]
