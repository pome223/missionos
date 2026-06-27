"""Gateway-facing Mission Designer to PX4/Gazebo SITL runner.

This module bridges a prepared Mission Designer SITL execution request to the
existing SITL MAVLink mission upload machinery. It does not introduce a second
MAVLink implementation: actual upload attempts flow through
``build_px4_gazebo_sitl_mission_upload_receipt`` and remain hard-gated by a
local environment opt-in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_mission_contract import (
    DeliveryMissionContract,
    build_delivery_mission_contract,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    PX4GazeboBoundedSimulationRequest,
    PX4GazeboMissionDesignerSITLExecutionRequest,
    PX4GazeboMissionScenarioApproval,
    PX4GazeboMissionScenarioCompileResult,
    PX4GazeboMissionScenarioProposal,
    PX4GazeboMissionScenarioValidationResult,
)
from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_TAKEOFF,
    MAV_CMD_NAV_WAYPOINT,
    MAV_MISSION_ACCEPTED,
    PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
    PX4GazeboSITLMissionItem,
    PX4GazeboSITLMissionUploadReceipt,
    PX4GazeboSITLMissionUploader,
    PX4GazeboSITLMissionUploadStatus,
    build_px4_gazebo_sitl_mission_upload_receipt,
)
from src.runtime.px4_gazebo_sitl_dropoff_verification import (
    SITL_DROPOFF_DEFAULT_ALTITUDE_TOLERANCE_M,
    SITL_DROPOFF_DEFAULT_MISSION_ITEM_SEQ,
    SITL_DROPOFF_DEFAULT_RELEASE_TIME_WINDOW_SECONDS,
    SITL_DROPOFF_DEFAULT_ZONE_RADIUS_M,
    PX4GazeboSITLDropoffFlightFact,
    PX4GazeboSITLDropoffVerification,
    PX4GazeboSITLDropoffVerificationStatus,
    PX4GazeboSITLPayloadReleaseEvent,
    build_px4_gazebo_sitl_dropoff_flight_fact,
    build_px4_gazebo_sitl_dropoff_verification,
    build_px4_gazebo_sitl_payload_release_event,
)
from src.runtime.simulated_delivery_command import (
    SimulatedCommandApproval,
    SimulatedCommandApprovalStatus,
    SimulatedCommandCategory,
    SimulatedCommandProposal,
    SimulatorCommandExecutionPreflight,
    SimulatorCommandExecutionPreflightStatus,
)
from src.runtime.task_store import TaskStore, get_task_store

MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV = (
    "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_EXECUTION"
)
MISSION_DESIGNER_SITL_SAFE_ROUTE_MAX_DISTANCE_M = 20_000.0
MISSION_DESIGNER_SITL_UPLOAD_GEOFENCE_RADIUS_M = 20_000.0
MISSION_DESIGNER_SITL_TASK_KIND = "px4_gazebo_mission_designer_sitl_execution_request"
PX4_GAZEBO_MISSION_DESIGNER_SITL_EXECUTION_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_mission_designer_sitl_execution_result.v1"
)
PX4_GAZEBO_MISSION_DESIGNER_SITL_FLIGHT_EVIDENCE_SCHEMA_VERSION = (
    "px4_gazebo_mission_designer_sitl_flight_evidence.v1"
)
PX4_GAZEBO_MISSION_DESIGNER_SITL_PAYLOAD_RELEASE_OBSERVATION_SCHEMA_VERSION = (
    "px4_gazebo_mission_designer_sitl_payload_release_observation.v1"
)
PX4_GAZEBO_MISSION_DESIGNER_SITL_DROPOFF_VERIFICATION_SCHEMA_VERSION = (
    "px4_gazebo_mission_designer_sitl_dropoff_verification.v1"
)
PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_RUN_SCHEMA_VERSION = (
    "px4_gazebo_mission_designer_sitl_live_flight_run.v1"
)
MISSION_DESIGNER_SITL_FLIGHT_EVIDENCE_PENDING_REASONS = (
    "observed_flight_evidence_not_attached",
    "payload_release_event_not_observed",
    "dropoff_verification_not_observed",
)
MISSION_DESIGNER_SITL_PAYLOAD_DROPOFF_PENDING_REASONS = (
    "payload_release_event_not_observed",
    "dropoff_verification_not_observed",
)
MISSION_DESIGNER_SITL_PAYLOAD_RELEASE_EVENT_SOURCES = frozenset(
    (
        "gazebo_gripper_detach_event",
        "gazebo_detachable_joint_detach_event",
        "mavlink_gripper_action_observed",
        "mavlink_actuator_release_observed",
    )
)
MISSION_DESIGNER_SITL_DROPOFF_VEHICLE_ID = "x500_0"
MISSION_DESIGNER_SITL_DROPOFF_ZONE_ID = "mission-designer-dropoff-zone"


class PX4GazeboMissionDesignerSITLRunnerError(RuntimeError):
    """Raised when a Mission Designer SITL execution attempt cannot proceed."""


class PX4GazeboMissionDesignerSITLExecutionResult(BaseModel):
    """Observed-result surface for Mission Designer-triggered SITL execution.

    The result records what the Gateway runner actually observed. A mission
    upload receipt can prove upload/ACK facts, but it cannot synthesize flight,
    payload-release, or dropoff success.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_MISSION_DESIGNER_SITL_EXECUTION_RESULT_SCHEMA_VERSION
    ] = PX4_GAZEBO_MISSION_DESIGNER_SITL_EXECUTION_RESULT_SCHEMA_VERSION
    result_id: str
    execution_request_ref: str
    delivery_mission_contract_ref: str
    simulator_command_execution_preflight_ref: str
    px4_gazebo_sitl_mission_upload_receipt_ref: str
    result_status: Literal[
        "blocked",
        "mission_upload_observed_flight_evidence_pending",
        "flight_evidence_observed_payload_dropoff_pending",
        "delivery_observed_payload_dropoff_verified",
    ]
    sitl_execution_opted_in: bool
    artifact_only_dry_run: bool
    actual_sitl_mission_upload_observed: bool
    actual_sitl_flight_evidence_observed: bool
    flight_evidence_ref: str = ""
    flight_evidence_source_execution_result_ref: str = ""
    mission_upload_observed: bool
    mission_ack_observed: bool
    mission_ack_type: int | None = None
    mission_request_sequences: tuple[int, ...] = ()
    actual_takeoff_observed: bool = False
    actual_dropoff_region_reached: bool = False
    actual_land_observed: bool = False
    payload_release_observed: Literal[False] = False
    payload_release_verified: Literal[False] = False
    payload_release_event_ref: Literal[""] = ""
    payload_release_event_source: Literal[""] = ""
    dropoff_verified: Literal[False] = False
    dropoff_verification_ref: Literal[""] = ""
    failure_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    external_dispatch_performed: bool
    gazebo_simulator_command_performed: bool
    mavlink_dispatch_performed: bool
    px4_mission_upload_performed: bool
    gazebo_entity_mutation_performed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    synthetic_success_allowed: Literal[False] = False
    payload_dropoff_success_requires_observed_facts: Literal[True] = True
    artifact_only_dry_run_cannot_verify_payload_or_dropoff: Literal[True] = True
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("mission_request_sequences", mode="before")
    @classmethod
    def _coerce_int_tuple(cls, value: Any) -> tuple[int, ...]:
        return tuple(int(item) for item in (value or ()))

    @field_validator("failure_reasons", "warning_reasons", mode="before")
    @classmethod
    def _coerce_str_tuple(cls, value: Any) -> tuple[str, ...]:
        return tuple(str(item) for item in (value or ()))

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_result(self) -> "PX4GazeboMissionDesignerSITLExecutionResult":
        if not self.execution_request_ref.startswith(
            "px4_gazebo_mission_designer_sitl_execution_request:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "SITL execution result requires execution request ref"
            )
        if not self.delivery_mission_contract_ref.startswith(
            "delivery_mission_contract:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "SITL execution result requires delivery mission contract ref"
            )
        if not self.simulator_command_execution_preflight_ref.startswith(
            "simulator_command_execution_preflight:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "SITL execution result requires preflight ref"
            )
        if not self.px4_gazebo_sitl_mission_upload_receipt_ref.startswith(
            "px4_gazebo_sitl_mission_upload_receipt:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "SITL execution result requires mission upload receipt ref"
            )
        if self.mission_upload_observed != self.actual_sitl_mission_upload_observed:
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "SITL execution result mission upload observation fields mismatch"
            )
        if self.artifact_only_dry_run:
            if self.actual_sitl_mission_upload_observed:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "artifact-only result cannot claim SITL mission upload"
                )
            if self.actual_sitl_flight_evidence_observed:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "artifact-only result cannot claim SITL flight evidence"
                )
            if any(
                (
                    self.payload_release_observed,
                    self.payload_release_verified,
                    self.dropoff_verified,
                    self.actual_takeoff_observed,
                    self.actual_dropoff_region_reached,
                    self.actual_land_observed,
                )
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "artifact-only result cannot verify flight, payload, or dropoff"
                )
        if self.mission_upload_observed:
            if (
                self.mission_ack_observed is not True
                or self.mission_ack_type != MAV_MISSION_ACCEPTED
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "observed mission upload requires accepted mission ACK"
                )
            if self.px4_mission_upload_performed is not True:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "observed mission upload requires upload performed"
                )
        else:
            if any(
                (
                    self.external_dispatch_performed,
                    self.mavlink_dispatch_performed,
                    self.px4_mission_upload_performed,
                )
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "unobserved mission upload cannot dispatch"
                )
            if not self.failure_reasons:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "unobserved mission upload requires failure reasons"
                )
        if self.actual_sitl_flight_evidence_observed:
            if not self.flight_evidence_ref.startswith(
                "px4_gazebo_mission_designer_sitl_flight_evidence:"
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "observed SITL flight evidence requires flight evidence ref"
                )
            if not self.flight_evidence_source_execution_result_ref.startswith(
                "px4_gazebo_mission_designer_sitl_execution_result:"
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "observed SITL flight evidence requires source execution result ref"
                )
            if not (
                self.actual_takeoff_observed
                and self.actual_dropoff_region_reached
                and self.actual_land_observed
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "observed SITL flight evidence requires takeoff, dropoff-region, and land facts"
                )
        elif any(
            (
                self.actual_takeoff_observed,
                self.actual_dropoff_region_reached,
                self.actual_land_observed,
            )
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "flight facts require observed SITL flight evidence"
            )
        elif self.flight_evidence_source_execution_result_ref:
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "unobserved SITL flight evidence cannot include source execution result ref"
            )
        if self.payload_release_observed or self.payload_release_verified:
            if not self.actual_sitl_flight_evidence_observed:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "payload release requires observed SITL flight evidence"
                )
            if not self.payload_release_event_ref.startswith(
                "px4_gazebo_sitl_payload_release_event:"
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "payload release requires observed payload release event ref"
                )
            if (
                self.payload_release_event_source
                not in MISSION_DESIGNER_SITL_PAYLOAD_RELEASE_EVENT_SOURCES
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "payload release requires whitelisted observed event source"
                )
        if self.payload_release_verified and not self.payload_release_observed:
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "payload release verification requires observed release"
            )
        if self.dropoff_verified:
            if not self.payload_release_verified:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "dropoff verification requires verified payload release"
                )
            if not self.dropoff_verification_ref.startswith(
                "px4_gazebo_sitl_dropoff_verification:"
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "dropoff verification requires observed verification ref"
                )
        if self.result_status == "blocked" and not self.failure_reasons:
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "blocked SITL execution result requires failure reasons"
            )
        if self.result_status == "mission_upload_observed_flight_evidence_pending":
            if not self.mission_upload_observed or not self.failure_reasons:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "pending flight-evidence result requires upload and reasons"
                )
            if self.actual_sitl_flight_evidence_observed:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "pending flight-evidence result cannot include observed flight evidence"
                )
            if self.payload_release_observed or self.dropoff_verified:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "pending flight-evidence result cannot mark payload/dropoff success"
                )
        if self.result_status == "flight_evidence_observed_payload_dropoff_pending":
            if not (
                self.mission_upload_observed
                and self.actual_sitl_flight_evidence_observed
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "flight-evidence pending result requires upload and observed flight evidence"
                )
            if "observed_flight_evidence_not_attached" in self.failure_reasons:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "flight-evidence pending result cannot keep missing-flight reason"
                )
            if not set(MISSION_DESIGNER_SITL_PAYLOAD_DROPOFF_PENDING_REASONS).issubset(
                set(self.failure_reasons)
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "flight-evidence pending result requires payload/dropoff pending reasons"
                )
            if self.payload_release_observed or self.dropoff_verified:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "flight-evidence pending result cannot mark payload/dropoff success"
                )
        if self.result_status == "delivery_observed_payload_dropoff_verified":
            if self.failure_reasons:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "verified delivery result cannot have failure reasons"
                )
            if not (
                self.mission_upload_observed
                and self.actual_sitl_flight_evidence_observed
                and self.actual_takeoff_observed
                and self.actual_dropoff_region_reached
                and self.actual_land_observed
                and self.payload_release_observed
                and self.payload_release_verified
                and self.dropoff_verified
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "verified delivery result requires all observed SITL facts"
                )
        return self


class PX4GazeboMissionDesignerSITLFlightEvidence(BaseModel):
    """Observed flight-evidence slice for a Mission Designer SITL execution.

    This artifact deliberately stops at flight facts. Payload release and
    dropoff verification remain separate observed-fact artifacts so an uploaded
    and flown route cannot synthesize delivery completion.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_MISSION_DESIGNER_SITL_FLIGHT_EVIDENCE_SCHEMA_VERSION
    ] = PX4_GAZEBO_MISSION_DESIGNER_SITL_FLIGHT_EVIDENCE_SCHEMA_VERSION
    flight_evidence_id: str
    execution_result_ref: str
    execution_request_ref: str
    delivery_mission_contract_ref: str
    px4_gazebo_sitl_mission_upload_receipt_ref: str
    evidence_source: Literal["actual_px4_gazebo_horizontal_route_smoke"] = (
        "actual_px4_gazebo_horizontal_route_smoke"
    )
    actual_sitl_mission_upload_observed: Literal[True] = True
    actual_sitl_flight_evidence_observed: Literal[True] = True
    actual_px4_gazebo_horizontal_smoke_observed: Literal[True] = True
    mission_ack_observed: Literal[True] = True
    mission_ack_type: Literal[MAV_MISSION_ACCEPTED] = MAV_MISSION_ACCEPTED
    mission_request_sequences: tuple[int, ...] = ()
    actual_takeoff_observed: Literal[True] = True
    actual_dropoff_region_reached: Literal[True] = True
    actual_land_observed: Literal[True] = True
    horizontal_summary_artifact_dir: str
    horizontal_summary_sha256: str
    horizontal_progress_m: float = Field(ge=0.0)
    completed_pose_z_m: float
    climb_sample_count: int = Field(ge=1)
    landing_sample_count: int = Field(ge=1)
    route_geofence_violation: Literal[False] = False
    blocked_reasons: tuple[str, ...] = ()
    payload_release_observed: Literal[False] = False
    payload_release_verified: Literal[False] = False
    dropoff_verified: Literal[False] = False
    payload_release_event_ref: Literal[""] = ""
    dropoff_verification_ref: Literal[""] = ""
    bounded_setpoint_stream_allowed: bool = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    offboard_mode_switch_observed: bool = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    synthetic_success_allowed: Literal[False] = False
    payload_dropoff_success_requires_observed_facts: Literal[True] = True
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("mission_request_sequences", mode="before")
    @classmethod
    def _coerce_int_tuple(cls, value: Any) -> tuple[int, ...]:
        return tuple(int(item) for item in (value or ()))

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_str_tuple(cls, value: Any) -> tuple[str, ...]:
        return tuple(str(item) for item in (value or ()))

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_evidence(self) -> "PX4GazeboMissionDesignerSITLFlightEvidence":
        if not self.execution_result_ref.startswith(
            "px4_gazebo_mission_designer_sitl_execution_result:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "flight evidence requires Mission Designer SITL execution result ref"
            )
        if not self.execution_request_ref.startswith(
            "px4_gazebo_mission_designer_sitl_execution_request:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "flight evidence requires Mission Designer SITL execution request ref"
            )
        if not self.delivery_mission_contract_ref.startswith(
            "delivery_mission_contract:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "flight evidence requires delivery mission contract ref"
            )
        if not self.px4_gazebo_sitl_mission_upload_receipt_ref.startswith(
            "px4_gazebo_sitl_mission_upload_receipt:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "flight evidence requires mission upload receipt ref"
            )
        if self.mission_request_sequences != (0, 1, 2, 3):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "flight evidence requires complete mission request sequence"
            )
        if self.completed_pose_z_m > 0.15:
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "flight evidence requires observed landing pose"
            )
        if self.blocked_reasons:
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "flight evidence cannot have blocked reasons"
            )
        _validate_flight_evidence_artifact_binding(
            self.horizontal_summary_artifact_dir,
            self.horizontal_summary_sha256,
            evidence=self,
        )
        return self


class PX4GazeboMissionDesignerSITLPayloadReleaseObservation(BaseModel):
    """Observed payload-release slice for a Mission Designer SITL execution.

    This artifact proves that a payload release event was observed. It does not
    verify dropoff predicates or mark the delivery complete.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_MISSION_DESIGNER_SITL_PAYLOAD_RELEASE_OBSERVATION_SCHEMA_VERSION
    ] = PX4_GAZEBO_MISSION_DESIGNER_SITL_PAYLOAD_RELEASE_OBSERVATION_SCHEMA_VERSION
    observation_id: str
    execution_result_ref: str
    flight_evidence_ref: str
    execution_request_ref: str
    delivery_mission_contract_ref: str
    px4_gazebo_sitl_mission_upload_receipt_ref: str
    payload_release_event_ref: str
    event_source: Literal[
        "gazebo_gripper_detach_event",
        "gazebo_detachable_joint_detach_event",
        "mavlink_gripper_action_observed",
        "mavlink_actuator_release_observed",
    ]
    payload_id: str
    payload_release_observed_at: datetime
    release_position_x_m: float
    release_position_y_m: float
    release_position_z_m: float
    horizontal_summary_artifact_dir: str
    horizontal_summary_sha256: str
    live_flight_run_ref: str = ""
    live_flight_run_schema_version: str = ""
    live_flight_run_summary_sha256: str = ""
    mission_item_binding_sha256: str = ""
    same_gateway_execution_run_observed: bool = False
    payload_release_bound_to_live_run: bool = False
    actual_sitl_mission_upload_observed: Literal[True] = True
    actual_sitl_flight_evidence_observed: Literal[True] = True
    actual_px4_gazebo_horizontal_smoke_observed: Literal[True] = True
    payload_release_observed: Literal[True] = True
    payload_release_event_verified: Literal[True] = True
    payload_release_does_not_verify_dropoff: Literal[True] = True
    dropoff_verified: Literal[False] = False
    gazebo_detachable_joint_release_performed: Literal[False] = False
    gazebo_detachable_joint_release_observed: bool = False
    mavlink_release_observed: bool = False
    command_sent_by_mission_os: Literal[False] = False
    external_dispatch_performed_by_observer: Literal[False] = False
    gazebo_entity_mutation_performed_by_observer: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    synthetic_success_allowed: Literal[False] = False
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload_release_observed_at", "observed_at", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_observation(
        self,
    ) -> "PX4GazeboMissionDesignerSITLPayloadReleaseObservation":
        if not self.execution_result_ref.startswith(
            "px4_gazebo_mission_designer_sitl_execution_result:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "payload release observation requires execution result ref"
            )
        if not self.flight_evidence_ref.startswith(
            "px4_gazebo_mission_designer_sitl_flight_evidence:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "payload release observation requires flight evidence ref"
            )
        if not self.execution_request_ref.startswith(
            "px4_gazebo_mission_designer_sitl_execution_request:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "payload release observation requires execution request ref"
            )
        if not self.delivery_mission_contract_ref.startswith(
            "delivery_mission_contract:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "payload release observation requires delivery mission contract ref"
            )
        if not self.px4_gazebo_sitl_mission_upload_receipt_ref.startswith(
            "px4_gazebo_sitl_mission_upload_receipt:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "payload release observation requires mission upload receipt ref"
            )
        if not self.payload_release_event_ref.startswith(
            "px4_gazebo_sitl_payload_release_event:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "payload release observation requires payload release event ref"
            )
        if any(
            (
                self.live_flight_run_ref,
                self.live_flight_run_schema_version,
                self.live_flight_run_summary_sha256,
                self.mission_item_binding_sha256,
                self.same_gateway_execution_run_observed,
                self.payload_release_bound_to_live_run,
            )
        ):
            if not self.live_flight_run_ref.startswith(
                "px4_gazebo_mission_designer_sitl_live_flight_run:"
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "payload release observation requires live flight run ref"
                )
            if (
                self.live_flight_run_schema_version
                != PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_RUN_SCHEMA_VERSION
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "payload release observation requires live flight run schema"
                )
            if self.live_flight_run_summary_sha256 != self.horizontal_summary_sha256:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "payload release observation live run summary mismatch"
                )
            if not self.mission_item_binding_sha256:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "payload release observation requires mission item binding"
                )
            if self.same_gateway_execution_run_observed is not True:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "payload release observation requires same Gateway live run"
                )
            if self.payload_release_bound_to_live_run is not True:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "payload release observation must be bound to live run"
                )
        if self.event_source.startswith("gazebo_"):
            if self.gazebo_detachable_joint_release_observed is not True:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "Gazebo payload release observation requires Gazebo event"
                )
        if self.event_source.startswith("mavlink_"):
            if self.mavlink_release_observed is not True:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "MAVLink payload release observation requires MAVLink event"
                )
        _validate_payload_release_observation_summary_fields(self)
        return self


class PX4GazeboMissionDesignerSITLDropoffVerification(BaseModel):
    """Mission Designer chain binding for observed SITL dropoff verification.

    This artifact is the first Mission Designer layer allowed to claim dropoff
    verification. It must cite the underlying SITL dropoff verifier and keeps all
    authority-bearing surfaces closed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_MISSION_DESIGNER_SITL_DROPOFF_VERIFICATION_SCHEMA_VERSION
    ] = PX4_GAZEBO_MISSION_DESIGNER_SITL_DROPOFF_VERIFICATION_SCHEMA_VERSION
    verification_id: str
    execution_result_ref: str
    flight_evidence_ref: str
    payload_release_observation_ref: str
    execution_request_ref: str
    delivery_mission_contract_ref: str
    px4_gazebo_sitl_mission_upload_receipt_ref: str
    dropoff_flight_fact_ref: str
    payload_release_event_ref: str
    sitl_dropoff_verification_ref: str
    horizontal_summary_artifact_dir: str
    horizontal_summary_sha256: str
    live_flight_run_ref: str = ""
    live_flight_run_schema_version: str = ""
    live_flight_run_summary_sha256: str = ""
    mission_item_binding_sha256: str = ""
    same_gateway_execution_run_observed: bool = False
    dropoff_verification_bound_to_live_run: bool = False
    predicate_mode: Literal[
        "position_in_zone_and_altitude_and_mission_item_and_payload_release"
    ] = "position_in_zone_and_altitude_and_mission_item_and_payload_release"
    actual_sitl_mission_upload_observed: Literal[True] = True
    actual_sitl_flight_evidence_observed: Literal[True] = True
    payload_release_observed: Literal[True] = True
    payload_release_verified: Literal[True] = True
    dropoff_verified: Literal[True] = True
    pose_within_dropoff_zone: Literal[True] = True
    altitude_within_tolerance: Literal[True] = True
    mission_item_reached: Literal[True] = True
    release_position_within_dropoff_zone: Literal[True] = True
    release_altitude_within_tolerance: Literal[True] = True
    release_within_mission_item_time_window: Literal[True] = True
    dropoff_zone_radius_m: float
    altitude_tolerance_m: float
    expected_mission_item_seq: int
    observed_distance_to_dropoff_m: float
    observed_altitude_error_m: float
    release_distance_to_dropoff_m: float
    release_altitude_error_m: float
    release_time_delta_seconds: float
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    command_sent_by_verifier: Literal[False] = False
    external_dispatch_performed_by_verifier: Literal[False] = False
    mavlink_dispatch_performed_by_verifier: Literal[False] = False
    px4_mission_upload_performed_by_verifier: Literal[False] = False
    gazebo_entity_mutation_performed_by_verifier: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    synthetic_success_allowed: Literal[False] = False
    observed_facts_only: Literal[True] = True
    dropoff_verification_requires_observed_facts: Literal[True] = True
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_verification(
        self,
    ) -> "PX4GazeboMissionDesignerSITLDropoffVerification":
        if not self.execution_result_ref.startswith(
            "px4_gazebo_mission_designer_sitl_execution_result:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "dropoff verification requires execution result ref"
            )
        if not self.flight_evidence_ref.startswith(
            "px4_gazebo_mission_designer_sitl_flight_evidence:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "dropoff verification requires flight evidence ref"
            )
        if not self.payload_release_observation_ref.startswith(
            "px4_gazebo_mission_designer_sitl_payload_release_observation:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "dropoff verification requires payload release observation ref"
            )
        if not self.execution_request_ref.startswith(
            "px4_gazebo_mission_designer_sitl_execution_request:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "dropoff verification requires execution request ref"
            )
        if not self.delivery_mission_contract_ref.startswith(
            "delivery_mission_contract:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "dropoff verification requires delivery mission contract ref"
            )
        if not self.px4_gazebo_sitl_mission_upload_receipt_ref.startswith(
            "px4_gazebo_sitl_mission_upload_receipt:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "dropoff verification requires mission upload receipt ref"
            )
        if not self.dropoff_flight_fact_ref.startswith(
            "px4_gazebo_sitl_dropoff_flight_fact:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "dropoff verification requires flight fact ref"
            )
        if not self.payload_release_event_ref.startswith(
            "px4_gazebo_sitl_payload_release_event:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "dropoff verification requires payload release event ref"
            )
        if not self.sitl_dropoff_verification_ref.startswith(
            "px4_gazebo_sitl_dropoff_verification:"
        ):
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "dropoff verification requires SITL verifier ref"
            )
        if any(
            (
                self.live_flight_run_ref,
                self.live_flight_run_schema_version,
                self.live_flight_run_summary_sha256,
                self.mission_item_binding_sha256,
                self.same_gateway_execution_run_observed,
                self.dropoff_verification_bound_to_live_run,
            )
        ):
            if not self.live_flight_run_ref.startswith(
                "px4_gazebo_mission_designer_sitl_live_flight_run:"
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "dropoff verification requires live flight run ref"
                )
            if (
                self.live_flight_run_schema_version
                != PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_RUN_SCHEMA_VERSION
            ):
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "dropoff verification requires live flight run schema"
                )
            if self.live_flight_run_summary_sha256 != self.horizontal_summary_sha256:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "dropoff verification live run summary mismatch"
                )
            if not self.mission_item_binding_sha256:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "dropoff verification requires mission item binding"
                )
            if self.same_gateway_execution_run_observed is not True:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "dropoff verification requires same Gateway live run"
                )
            if self.dropoff_verification_bound_to_live_run is not True:
                raise PX4GazeboMissionDesignerSITLRunnerError(
                    "dropoff verification must be bound to live run"
                )
        _validate_flight_evidence_artifact_binding(
            self.horizontal_summary_artifact_dir,
            self.horizontal_summary_sha256,
        )
        _validate_dropoff_verification_summary_fields(self)
        return self


def _utc(value: datetime | None = None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        return resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _horizontal_summary_artifact_paths(artifact_dir: Path) -> tuple[Path, ...]:
    return (
        artifact_dir / "summary.json",
        artifact_dir / "tasks.db",
        artifact_dir / "px4_docker.log",
        artifact_dir / "pose_samples.jsonl",
        artifact_dir / "mission_artifacts.json",
    )


def _validate_horizontal_summary_artifacts(
    horizontal_summary: Mapping[str, Any],
) -> tuple[str, str]:
    artifact_dir_value = str(horizontal_summary.get("artifact_dir") or "").strip()
    if not artifact_dir_value:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires horizontal route artifact dir"
        )
    artifact_dir = Path(artifact_dir_value).expanduser()
    if not artifact_dir.exists() or not artifact_dir.is_dir():
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence horizontal route artifact dir missing"
        )
    required_paths = _horizontal_summary_artifact_paths(artifact_dir)
    missing = [str(path) for path in required_paths if not path.exists()]
    if missing:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence horizontal route artifacts missing: " + ", ".join(missing)
        )
    recorded_summary = json.loads((artifact_dir / "summary.json").read_text())
    provided_digest = _canonical_sha256(dict(horizontal_summary))
    recorded_digest = _canonical_sha256(recorded_summary)
    if provided_digest != recorded_digest:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence horizontal route summary artifact mismatch"
        )
    return str(artifact_dir), recorded_digest


def _validate_flight_evidence_artifact_binding(
    horizontal_summary_artifact_dir: str,
    horizontal_summary_sha256: str,
    *,
    evidence: Any | None = None,
) -> None:
    artifact_dir_value = str(horizontal_summary_artifact_dir or "").strip()
    if not artifact_dir_value:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires horizontal route artifact dir"
        )
    artifact_dir = Path(artifact_dir_value).expanduser()
    if not artifact_dir.exists() or not artifact_dir.is_dir():
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence horizontal route artifact dir missing"
        )
    missing = [
        str(path)
        for path in _horizontal_summary_artifact_paths(artifact_dir)
        if not path.exists()
    ]
    if missing:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence horizontal route artifacts missing: " + ", ".join(missing)
        )
    recorded_summary = json.loads((artifact_dir / "summary.json").read_text())
    recorded_digest = _canonical_sha256(recorded_summary)
    if recorded_digest != str(horizontal_summary_sha256):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence horizontal route summary digest mismatch"
        )
    if evidence is not None:
        _validate_flight_evidence_summary_fields(evidence, recorded_summary)


def _validate_flight_evidence_summary_fields(
    evidence: Any,
    summary: Mapping[str, Any],
) -> None:
    if summary.get("preupload_mission_performed") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires observed preupload mission"
        )
    if summary.get("preupload_mission_ack_observed") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires observed preupload mission ACK"
        )
    if summary.get("preupload_mission_ack_type") != MAV_MISSION_ACCEPTED:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires accepted preupload mission ACK"
        )
    if summary.get("actual_px4_gazebo_horizontal_smoke_observed") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires actual PX4/Gazebo horizontal smoke observation"
        )
    if summary.get("task_status") != "completed":
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires completed horizontal route task"
        )
    if summary.get("final_status") != "completed":
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires completed horizontal route final status"
        )
    if summary.get("dropoff_region_reached") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires observed dropoff-region reach"
        )
    if summary.get("route_geofence_violation") is not False:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence rejects geofence violation"
        )
    if tuple(summary.get("blocked_reasons") or ()) != ():
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence rejects blocked horizontal route summary"
        )
    if summary.get("bounded_setpoint_stream_allowed") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires bounded setpoint stream allowance"
        )
    if summary.get("unbounded_setpoint_stream_allowed") is not False:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence rejects unbounded setpoint stream allowance"
        )
    if summary.get("offboard_mode_switch_ack_observed") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires observed offboard mode switch ACK"
        )
    if summary.get("hardware_target_allowed") is not False:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence rejects hardware target allowance"
        )
    if summary.get("physical_execution_invoked") is not False:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence rejects physical execution"
        )
    summary_sequences = tuple(
        int(item) for item in (summary.get("preupload_mission_request_sequences") or ())
    )
    if summary_sequences != tuple(
        int(item) for item in evidence.mission_request_sequences
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence mission request sequences summary mismatch"
        )
    if float(summary.get("horizontal_progress_m")) != float(
        evidence.horizontal_progress_m
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence horizontal progress summary mismatch"
        )
    if float(summary.get("completed_pose_z_m")) != float(evidence.completed_pose_z_m):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence completed pose summary mismatch"
        )
    if int(summary.get("climb_sample_count") or 0) != int(evidence.climb_sample_count):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence climb sample count summary mismatch"
        )
    if int(summary.get("landing_sample_count") or 0) != int(
        evidence.landing_sample_count
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence landing sample count summary mismatch"
        )
    if evidence.bounded_setpoint_stream_allowed is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence bounded setpoint stream summary mismatch"
        )
    if evidence.offboard_mode_switch_observed is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence offboard mode switch summary mismatch"
        )


def _payload_release_summary_payload(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = summary.get("payload_release_summary") or {}
    return payload if isinstance(payload, Mapping) else {}


def _payload_release_payload_id(summary: Mapping[str, Any]) -> str:
    payload_id = str(_payload_release_summary_payload(summary).get("payload_id") or "")
    if not payload_id:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation requires payload id"
        )
    return payload_id


def _payload_release_observed_at(summary: Mapping[str, Any]) -> datetime:
    observed_at = str(summary.get("payload_release_observed_at") or "").strip()
    if not observed_at:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation requires observed_at"
        )
    return _utc(datetime.fromisoformat(observed_at.replace("Z", "+00:00")))


def _payload_release_position(summary: Mapping[str, Any], key: str) -> float:
    value = summary.get(key)
    if value is None:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation requires release position"
        )
    return float(value)


def _summary_float(summary: Mapping[str, Any], key: str) -> float:
    value = summary.get(key)
    if value is None:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            f"dropoff verification requires {key}"
        )
    return float(value)


def _summary_timestamp(summary: Mapping[str, Any], key: str) -> datetime:
    value = str(summary.get(key) or "").strip()
    if not value:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            f"dropoff verification requires {key}"
        )
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _completed_pose_xy(summary: Mapping[str, Any]) -> tuple[float, float]:
    value = summary.get("completed_pose_xy_m")
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification requires completed_pose_xy_m"
        )
    return float(value[0]), float(value[1])


def _validate_payload_release_observation_summary_fields(
    observation: PX4GazeboMissionDesignerSITLPayloadReleaseObservation,
) -> None:
    _validate_flight_evidence_artifact_binding(
        observation.horizontal_summary_artifact_dir,
        observation.horizontal_summary_sha256,
    )
    summary_path = Path(observation.horizontal_summary_artifact_dir) / "summary.json"
    summary = json.loads(summary_path.read_text())
    if summary.get("payload_release_observed") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation requires observed release"
        )
    if summary.get("payload_release_event_source") != observation.event_source:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation event source mismatch"
        )
    if _payload_release_payload_id(summary) != observation.payload_id:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation payload id mismatch"
        )
    if _payload_release_observed_at(summary) != observation.payload_release_observed_at:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation timestamp mismatch"
        )
    if _payload_release_position(summary, "payload_release_position_x_m") != float(
        observation.release_position_x_m
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation x position mismatch"
        )
    if _payload_release_position(summary, "payload_release_position_y_m") != float(
        observation.release_position_y_m
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation y position mismatch"
        )
    if _payload_release_position(summary, "payload_release_position_z_m") != float(
        observation.release_position_z_m
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation z position mismatch"
        )
    release_event = build_px4_gazebo_sitl_payload_release_event(
        event_source=observation.event_source,
        payload_id=observation.payload_id,
        release_position_x_m=observation.release_position_x_m,
        release_position_y_m=observation.release_position_y_m,
        release_position_z_m=observation.release_position_z_m,
        observed_at=observation.payload_release_observed_at,
    )
    expected_release_ref = (
        f"px4_gazebo_sitl_payload_release_event:{release_event.event_id}"
    )
    if observation.payload_release_event_ref != expected_release_ref:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation event ref mismatch"
        )


def _dropoff_target(summary: Mapping[str, Any]) -> tuple[float, float, float]:
    return (
        _summary_float(summary, "route_target_x_m"),
        _summary_float(summary, "route_target_y_m"),
        float(summary.get("dropoff_target_altitude_m") or 0.0),
    )


def _dropoff_release_event_from_summary(
    summary: Mapping[str, Any],
) -> PX4GazeboSITLPayloadReleaseEvent:
    event_source = str(summary.get("payload_release_event_source") or "")
    if event_source not in MISSION_DESIGNER_SITL_PAYLOAD_RELEASE_EVENT_SOURCES:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification requires allowlisted payload release event source"
        )
    return build_px4_gazebo_sitl_payload_release_event(
        event_source=event_source,  # type: ignore[arg-type]
        payload_id=_payload_release_payload_id(summary),
        release_position_x_m=_payload_release_position(
            summary, "payload_release_position_x_m"
        ),
        release_position_y_m=_payload_release_position(
            summary, "payload_release_position_y_m"
        ),
        release_position_z_m=_payload_release_position(
            summary, "payload_release_position_z_m"
        ),
        observed_at=_payload_release_observed_at(summary),
        metadata={
            "source": "px4_gazebo_mission_designer_sitl_dropoff_verification",
            "horizontal_summary_sha256": _canonical_sha256(dict(summary)),
        },
    )


def _dropoff_flight_fact_from_summary(
    summary: Mapping[str, Any],
    *,
    payload_release_event: PX4GazeboSITLPayloadReleaseEvent,
    sitl_mission_upload_receipt_ref: str,
    telemetry_ref: str,
) -> PX4GazeboSITLDropoffFlightFact:
    if summary.get("dropoff_region_reached") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification requires observed dropoff-region reach"
        )
    completed_x, completed_y = _completed_pose_xy(summary)
    target_x, target_y, target_altitude = _dropoff_target(summary)
    mission_item_reached_at = _summary_timestamp(summary, "recorded_at")
    return build_px4_gazebo_sitl_dropoff_flight_fact(
        vehicle_id=MISSION_DESIGNER_SITL_DROPOFF_VEHICLE_ID,
        dropoff_zone_id=MISSION_DESIGNER_SITL_DROPOFF_ZONE_ID,
        position_x_m=completed_x,
        position_y_m=completed_y,
        position_z_m=_summary_float(summary, "completed_pose_z_m"),
        dropoff_target_x_m=target_x,
        dropoff_target_y_m=target_y,
        dropoff_target_altitude_m=target_altitude,
        mission_item_reached_observed=True,
        mission_item_reached_seq=SITL_DROPOFF_DEFAULT_MISSION_ITEM_SEQ,
        mission_item_reached_at=mission_item_reached_at,
        payload_release_event=payload_release_event,
        telemetry_ref=telemetry_ref,
        sitl_mission_upload_receipt_ref=sitl_mission_upload_receipt_ref,
        observed_at=mission_item_reached_at,
        metadata={
            "source": "px4_gazebo_mission_designer_sitl_dropoff_verification",
            "horizontal_summary_sha256": _canonical_sha256(dict(summary)),
            "dropoff_target_altitude_source": "gazebo_ground_plane_default",
        },
    )


def _validate_dropoff_verification_summary_fields(
    verification: PX4GazeboMissionDesignerSITLDropoffVerification,
) -> None:
    summary_path = Path(verification.horizontal_summary_artifact_dir) / "summary.json"
    summary = json.loads(summary_path.read_text())
    release_event = _dropoff_release_event_from_summary(summary)
    if (
        f"px4_gazebo_sitl_payload_release_event:{release_event.event_id}"
        != verification.payload_release_event_ref
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification payload release event ref mismatch"
        )
    flight_fact = _dropoff_flight_fact_from_summary(
        summary,
        payload_release_event=release_event,
        sitl_mission_upload_receipt_ref=(
            verification.px4_gazebo_sitl_mission_upload_receipt_ref
        ),
        telemetry_ref=verification.flight_evidence_ref,
    )
    if (
        f"px4_gazebo_sitl_dropoff_flight_fact:{flight_fact.fact_id}"
        != verification.dropoff_flight_fact_ref
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification flight fact ref mismatch"
        )
    target_x, target_y, target_altitude = _dropoff_target(summary)
    completed_x, completed_y = _completed_pose_xy(summary)
    observed_distance = (
        (target_x - completed_x) ** 2 + (target_y - completed_y) ** 2
    ) ** 0.5
    observed_altitude_error = abs(
        _summary_float(summary, "completed_pose_z_m") - target_altitude
    )
    release_distance = (
        (target_x - _payload_release_position(summary, "payload_release_position_x_m"))
        ** 2
        + (
            target_y
            - _payload_release_position(summary, "payload_release_position_y_m")
        )
        ** 2
    ) ** 0.5
    release_altitude_error = abs(
        _payload_release_position(summary, "payload_release_position_z_m")
        - target_altitude
    )
    release_time_delta = abs(
        (
            _payload_release_observed_at(summary)
            - _summary_timestamp(summary, "recorded_at")
        ).total_seconds()
    )
    for expected, actual in (
        (observed_distance, verification.observed_distance_to_dropoff_m),
        (observed_altitude_error, verification.observed_altitude_error_m),
        (release_distance, verification.release_distance_to_dropoff_m),
        (release_altitude_error, verification.release_altitude_error_m),
        (release_time_delta, verification.release_time_delta_seconds),
    ):
        if abs(float(expected) - float(actual)) > 1e-9:
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "dropoff verification summary field mismatch"
            )


def mission_designer_sitl_execution_opted_in(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether Gateway-triggered Mission Designer SITL execution is enabled."""

    source = environ if environ is not None else os.environ
    return source.get(MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV) == "1"


def _artifact_ref(prefix: str, value: str) -> str:
    return f"{prefix}:{value}"


def _model_payload(value: BaseModel | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value)


def _live_flight_run_binding(
    live_flight_run: Mapping[str, Any] | None,
    *,
    execution_result: PX4GazeboMissionDesignerSITLExecutionResult,
    horizontal_summary_sha256: str,
    bound_execution_result_ref: str | None = None,
) -> dict[str, str | bool]:
    if live_flight_run is None:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "same-run evidence requires live flight run artifact"
        )
    live_run = dict(live_flight_run)
    live_run_ref = _artifact_ref(
        "px4_gazebo_mission_designer_sitl_live_flight_run",
        str(live_run.get("live_flight_run_id") or ""),
    )
    if live_run_ref.endswith(":"):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "same-run evidence requires live flight run id"
        )
    if (
        live_run.get("schema_version")
        != PX4_GAZEBO_MISSION_DESIGNER_SITL_LIVE_FLIGHT_RUN_SCHEMA_VERSION
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "same-run evidence requires live flight run schema"
        )
    if live_run.get("same_gateway_execution_run_observed") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "same-run evidence requires Gateway live run observation"
        )
    if live_run.get("mission_items_bound_to_gateway_receipt") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "same-run evidence requires mission item binding"
        )
    if live_run.get("actual_sitl_flight_evidence_observed") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "same-run evidence requires live flight observation"
        )
    execution_result_ref = str(live_run.get("execution_result_ref") or "")
    if not execution_result_ref.startswith(
        "px4_gazebo_mission_designer_sitl_execution_result:"
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "same-run evidence requires execution result ref"
        )
    if (
        bound_execution_result_ref is not None
        and execution_result_ref != bound_execution_result_ref
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "same-run evidence execution result ref mismatch"
        )
    if live_run.get("execution_request_ref") != execution_result.execution_request_ref:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "same-run evidence execution request ref mismatch"
        )
    if (
        live_run.get("mission_upload_receipt_ref")
        != execution_result.px4_gazebo_sitl_mission_upload_receipt_ref
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "same-run evidence mission upload receipt ref mismatch"
        )
    if live_run.get("horizontal_summary_sha256") != horizontal_summary_sha256:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "same-run evidence summary hash mismatch"
        )
    mission_item_binding = str(live_run.get("mission_item_binding_sha256") or "")
    if not mission_item_binding:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "same-run evidence requires mission item binding hash"
        )
    return {
        "live_flight_run_ref": live_run_ref,
        "live_flight_run_schema_version": str(live_run["schema_version"]),
        "live_flight_run_summary_sha256": str(live_run["horizontal_summary_sha256"]),
        "mission_item_binding_sha256": mission_item_binding,
        "same_gateway_execution_run_observed": True,
    }


def _coerce_artifact(
    artifacts: Mapping[str, Any],
    key: str,
    model,
):
    value = artifacts.get(key)
    if not isinstance(value, Mapping):
        raise PX4GazeboMissionDesignerSITLRunnerError(f"{key} artifact is required")
    return model.model_validate(dict(value))


def _required_live_flight_run_artifact(
    artifacts: Mapping[str, Any],
) -> Mapping[str, Any]:
    value = artifacts.get("px4_gazebo_mission_designer_sitl_live_flight_run")
    if not isinstance(value, Mapping):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "same-run evidence requires live flight run artifact"
        )
    return value


def _coordinate_route_wind_speed_mps(
    coordinate_route: Mapping[str, Any] | None,
) -> float | None:
    if not coordinate_route:
        return None
    raw_value = coordinate_route.get("wind_speed_mps")
    if raw_value in (None, ""):
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return min(max(value, 0.0), 100.0)


def _mission_designer_wind_constraint_mps(
    coordinate_route: Mapping[str, Any] | None,
) -> float:
    requested = _coordinate_route_wind_speed_mps(coordinate_route)
    if requested is None:
        return 0.1
    return min(max(requested, 0.1), 100.0)


def _mission_designer_wind_condition_artifacts(
    *,
    coordinate_route: Mapping[str, Any] | None,
    contract: DeliveryMissionContract,
    now: datetime,
) -> dict[str, dict[str, Any]]:
    requested_wind = _coordinate_route_wind_speed_mps(coordinate_route)
    route_id = str((coordinate_route or {}).get("route_id") or "operator_coordinate_pair")
    applied_wind = float(contract.weather_constraints.max_wind_speed_mps)
    if requested_wind is None:
        unsupported_reasons = ["wind_mean_unrequested"]
        return {
            "environment_condition_profile": {
                "schema_version": "environment_condition_profile.v1",
                "condition_id": f"environment_condition_profile:{route_id}:wind_speed",
                "condition_kind": "wind_speed",
                "requested": {
                    "wind_mean_mps": None,
                    "wind_direction_deg": None,
                },
                "requested_present": False,
                "source": "mission_designer_coordinate_route",
                "delivery_completion_claimed": False,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            },
            "simulator_capability_matrix": {
                "schema_version": "simulator_capability_matrix.v1",
                "condition_kind": "wind_speed",
                "wind_mean": "not_requested",
                "wind_gust": "not_requested",
                "wind_variance": "not_requested",
                "live_horizontal_runner_env_forwarding": "not_requested",
                "unsupported_reasons": unsupported_reasons,
                "approximation_reasons": [],
                "delivery_completion_claimed": False,
            },
            "simulator_condition_application": {
                "schema_version": "simulator_condition_application.v1",
                "condition_kind": "wind_speed",
                "application_status": "not_requested",
                "applied": {
                    "method": "not_requested",
                    "requested_wind_speed_mps": None,
                    "applied_max_wind_speed_mps": applied_wind,
                    "delivery_mission_contract_ref": _artifact_ref(
                        "delivery_mission_contract", contract.contract_id
                    ),
                    "coordinate_route_ref": (
                        f"mission_designer_coordinate_pair_route:{route_id}"
                    ),
                    "live_runner_env_key": "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS",
                    "live_runner_env_forwarded_wind_speed_mps": None,
                    "contract_constraint_floor_applied": False,
                    "unrequested_contract_floor_mps": applied_wind,
                    "applied_at": now.isoformat(),
                },
                "unsupported_reasons": unsupported_reasons,
                "approximation_reasons": [],
                "delivery_completion_claimed": False,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            },
            "observed_environment_evidence": {
                "schema_version": "observed_environment_evidence.v1",
                "condition_kind": "wind_speed",
                "observation_status": "not_requested",
                "observed": {
                    "source": "mission_designer_coordinate_route.wind_speed_mps",
                    "observed": False,
                    "requested_wind_speed_mps": None,
                    "contract_max_wind_speed_mps": applied_wind,
                    "live_runner_env_forwarded_wind_speed_mps": None,
                    "live_runner_env_forwarding_expected": False,
                    "unsupported_reasons": unsupported_reasons,
                    "task_status_mutated": False,
                    "gate_status_mutated": False,
                    "delivery_completion_claimed": False,
                },
                "delivery_completion_claimed": False,
            },
        }
    direction = None
    if coordinate_route:
        raw_direction = coordinate_route.get("wind_direction_deg")
        if raw_direction not in (None, ""):
            try:
                direction = float(raw_direction)
            except (TypeError, ValueError):
                direction = None
    approximation_reasons = []
    if requested_wind != applied_wind:
        approximation_reasons.append("delivery_contract_weather_constraint_requires_positive_wind")
    applied_at = now.isoformat()
    return {
        "environment_condition_profile": {
            "schema_version": "environment_condition_profile.v1",
            "condition_id": f"environment_condition_profile:{route_id}:wind_speed",
            "condition_kind": "wind_speed",
            "requested": {
                "wind_mean_mps": requested_wind,
                "wind_direction_deg": direction,
            },
            "requested_present": True,
            "source": "mission_designer_coordinate_route",
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "simulator_capability_matrix": {
            "schema_version": "simulator_capability_matrix.v1",
            "condition_kind": "wind_speed",
            "wind_mean": "supported_contract_constraint",
            "wind_gust": "not_requested",
            "wind_variance": "not_requested",
            "live_horizontal_runner_env_forwarding": "supported",
            "unsupported_reasons": [],
            "approximation_reasons": approximation_reasons,
            "delivery_completion_claimed": False,
        },
        "simulator_condition_application": {
            "schema_version": "simulator_condition_application.v1",
            "condition_kind": "wind_speed",
            "application_status": (
                "applied_with_approximations" if approximation_reasons else "applied"
            ),
            "applied": {
                "method": "delivery_mission_contract_weather_constraint",
                "requested_wind_speed_mps": requested_wind,
                "applied_max_wind_speed_mps": applied_wind,
                "delivery_mission_contract_ref": _artifact_ref(
                    "delivery_mission_contract", contract.contract_id
                ),
                "coordinate_route_ref": (
                    f"mission_designer_coordinate_pair_route:{route_id}"
                ),
                "live_runner_env_key": "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS",
                "live_runner_env_forwarded_wind_speed_mps": requested_wind,
                "contract_constraint_floor_applied": requested_wind != applied_wind,
                "applied_at": applied_at,
            },
            "unsupported_reasons": [],
            "approximation_reasons": approximation_reasons,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "observed_environment_evidence": {
            "schema_version": "observed_environment_evidence.v1",
            "condition_kind": "wind_speed",
            "observation_status": "contract_weather_constraint_observed",
            "observed": {
                "source": "delivery_mission_contract.weather_constraints",
                "observed": True,
                "requested_wind_speed_mps": requested_wind,
                "applied_max_wind_speed_mps": applied_wind,
                "contract_max_wind_speed_mps": applied_wind,
                "live_runner_env_forwarded_wind_speed_mps": requested_wind,
                "live_runner_env_forwarding_expected": True,
                "contract_constraint_floor_applied": requested_wind != applied_wind,
                "task_status_mutated": False,
                "gate_status_mutated": False,
                "delivery_completion_claimed": False,
            },
            "delivery_completion_claimed": False,
        },
    }


def _mission_contract_from_designer(
    *,
    execution_request: PX4GazeboMissionDesignerSITLExecutionRequest,
    bounded_request: PX4GazeboBoundedSimulationRequest,
    coordinate_route: Mapping[str, Any] | None = None,
    now: datetime,
) -> DeliveryMissionContract:
    raw_payload_weight_kg = bounded_request.payload_weight_kg
    payload_weight_kg = min(
        max(
            float(raw_payload_weight_kg if raw_payload_weight_kg is not None else 1.0),
            0.1,
        ),
        5.0,
    )
    raw_altitude_m = bounded_request.altitude_target_m
    requested_altitude_m = float(raw_altitude_m if raw_altitude_m is not None else 30.0)
    sitl_altitude_m = min(max(requested_altitude_m, 20.0), 120.0)
    max_wind_speed_mps = _mission_designer_wind_constraint_mps(coordinate_route)
    latest_dropoff = now + timedelta(minutes=30)
    if coordinate_route:
        return build_delivery_mission_contract(
            mission_id=f"mission-designer-sitl-{execution_request.execution_request_id}",
            pickup_location={
                "location_id": "mission-designer-operator-coordinate-takeoff",
                "latitude": float(coordinate_route["takeoff_latitude"]),
                "longitude": float(coordinate_route["takeoff_longitude"]),
            },
            dropoff_location={
                "location_id": "mission-designer-operator-coordinate-dropoff",
                "latitude": float(coordinate_route["dropoff_latitude"]),
                "longitude": float(coordinate_route["dropoff_longitude"]),
                "altitude_m": min(
                    max(
                        float(
                            coordinate_route.get("dropoff_roof_height_agl_m")
                            or sitl_altitude_m
                        ),
                        10.0,
                    ),
                    120.0,
                ),
            },
            delivery_window={
                "earliest_pickup_at": now.isoformat(),
                "latest_dropoff_at": latest_dropoff.isoformat(),
            },
            package_constraints={
                "package_id": f"pkg-{execution_request.execution_request_id}",
                "max_weight_kg": payload_weight_kg,
            },
            weather_constraints={
                "max_wind_speed_mps": max_wind_speed_mps,
                "max_precipitation_mm_per_hour": 0.0,
                "min_visibility_m": 1500.0,
            },
            battery_policy={
                "minimum_takeoff_percent": 80,
                "return_to_home_percent": 35,
                "reserve_landing_percent": 25,
            },
            landing_zone_policy={
                "min_clear_radius_m": 3.0,
                "max_slope_degrees": 5.0,
                "accepted_surface_kinds": ["operator_coordinate_dropoff"],
            },
            telemetry_requirements={
                "required_measurements": [
                    "position",
                    "battery_percent",
                    "vehicle_health",
                    "weather_snapshot",
                ],
                "max_freshness_seconds": 2.0,
            },
            route_constraints={
                "max_route_distance_m": MISSION_DESIGNER_SITL_SAFE_ROUTE_MAX_DISTANCE_M
            },
            geofence_constraints={"allowed_regions": ["operator_coordinate_pair"]},
            now=now,
            metadata={
                "source": "mission_designer_sitl_runner",
                "execution_request_id": execution_request.execution_request_id,
                "scenario_profile": bounded_request.scenario_profile,
                "route_profile": "operator_coordinate_pair_route",
                "requested_altitude_m": requested_altitude_m,
                "sitl_altitude_m": sitl_altitude_m,
                "fixed_safe_route_projection": False,
                "operator_coordinate_pair_route": True,
                "payload_weight_kg": payload_weight_kg,
                "requested_wind_speed_mps": _coordinate_route_wind_speed_mps(
                    coordinate_route
                ),
                "applied_max_wind_speed_mps": max_wind_speed_mps,
            },
        )
    return build_delivery_mission_contract(
        mission_id=f"mission-designer-sitl-{execution_request.execution_request_id}",
        pickup_location={
            "location_id": "mission-designer-pickup-pad-a",
            "latitude": 35.681236,
            "longitude": 139.767125,
        },
        dropoff_location={
            "location_id": "mission-designer-dropoff-pad-b",
            "latitude": 35.689487,
            "longitude": 139.691706,
            "altitude_m": sitl_altitude_m,
        },
        delivery_window={
            "earliest_pickup_at": now.isoformat(),
            "latest_dropoff_at": latest_dropoff.isoformat(),
        },
        package_constraints={
            "package_id": f"pkg-{execution_request.execution_request_id}",
            "max_weight_kg": payload_weight_kg,
        },
        weather_constraints={
            "max_wind_speed_mps": max_wind_speed_mps,
            "max_precipitation_mm_per_hour": 0.0,
            "min_visibility_m": 1500.0,
        },
        battery_policy={
            "minimum_takeoff_percent": 80,
            "return_to_home_percent": 35,
            "reserve_landing_percent": 25,
        },
        landing_zone_policy={
            "min_clear_radius_m": 3.0,
            "max_slope_degrees": 5.0,
            "accepted_surface_kinds": ["marked_pad"],
        },
        telemetry_requirements={
            "required_measurements": [
                "position",
                "battery_percent",
                "vehicle_health",
                "weather_snapshot",
            ],
            "max_freshness_seconds": 2.0,
        },
        route_constraints={
            "max_route_distance_m": MISSION_DESIGNER_SITL_SAFE_ROUTE_MAX_DISTANCE_M
        },
        geofence_constraints={"allowed_regions": ["tokyo_sitl_corridor"]},
        now=now,
        metadata={
            "source": "mission_designer_sitl_runner",
            "execution_request_id": execution_request.execution_request_id,
            "scenario_profile": bounded_request.scenario_profile,
            "route_profile": bounded_request.route_profile,
            "requested_altitude_m": requested_altitude_m,
            "sitl_altitude_m": sitl_altitude_m,
            "fixed_safe_route_projection": True,
            "source_prompt_route_is_not_geocoded": True,
            "requested_dropoff_lat_lon_not_available": True,
            "projected_dropoff_latitude": 35.689487,
            "projected_dropoff_longitude": 139.691706,
            "payload_weight_kg": payload_weight_kg,
        },
    )


def _operator_coordinate_sitl_mission_items(
    current: Mapping[str, Any],
) -> tuple[PX4GazeboSITLMissionItem, ...] | None:
    artifacts = current.get("artifacts") or {}
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    binding = artifacts.get("mission_designer_coordinate_pair_sitl_binding")
    binding = binding if isinstance(binding, Mapping) else {}
    if binding.get("binding_status") != "bound_to_operator_coordinate_route":
        return None
    raw_items = binding.get("mission_items")
    if not isinstance(raw_items, list | tuple) or not raw_items:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "operator coordinate route binding requires mission items"
        )
    items = tuple(
        PX4GazeboSITLMissionItem.model_validate(dict(item)) for item in raw_items
    )
    if tuple(item.seq for item in items) != tuple(range(len(items))):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "operator coordinate route mission items must be contiguous"
        )
    if tuple(item.command for item in items) != (
        MAV_CMD_NAV_TAKEOFF,
        MAV_CMD_NAV_WAYPOINT,
        MAV_CMD_NAV_WAYPOINT,
        MAV_CMD_NAV_LAND,
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "operator coordinate route mission item command sequence invalid"
        )
    return items


def _project_simulator_preflight_chain(
    *,
    contract: DeliveryMissionContract,
    execution_request: PX4GazeboMissionDesignerSITLExecutionRequest,
    proposal: PX4GazeboMissionScenarioProposal,
    validation: PX4GazeboMissionScenarioValidationResult,
    approval: PX4GazeboMissionScenarioApproval,
    compile_result: PX4GazeboMissionScenarioCompileResult,
    bounded_request: PX4GazeboBoundedSimulationRequest,
    now: datetime,
) -> tuple[
    SimulatedCommandProposal,
    SimulatedCommandApproval,
    SimulatorCommandExecutionPreflight,
]:
    contract_ref = _artifact_ref("delivery_mission_contract", contract.contract_id)
    execution_request_ref = _artifact_ref(
        "px4_gazebo_mission_designer_sitl_execution_request",
        execution_request.execution_request_id,
    )
    scenario_proposal_ref = _artifact_ref(
        "px4_gazebo_mission_scenario_proposal", proposal.proposal_id
    )
    validation_ref = _artifact_ref(
        "px4_gazebo_mission_scenario_validation_result", validation.validation_id
    )
    scenario_approval_ref = _artifact_ref(
        "px4_gazebo_mission_scenario_approval", approval.approval_id
    )
    compile_ref = _artifact_ref(
        "px4_gazebo_mission_scenario_compile_result",
        compile_result.compile_result_id,
    )
    bounded_ref = _artifact_ref(
        "px4_gazebo_bounded_simulation_request", bounded_request.request_id
    )
    projected_refs = {
        "episode": _artifact_ref(
            "simulated_delivery_episode",
            f"mission_designer_sitl_{execution_request.execution_request_id}",
        ),
        "scorecard": _artifact_ref(
            "delivery_scorecard",
            f"mission_designer_sitl_{execution_request.execution_request_id}",
        ),
        "review": _artifact_ref(
            "delivery_episode_review",
            f"mission_designer_sitl_{execution_request.execution_request_id}",
        ),
        "decision": _artifact_ref(
            "delivery_recovery_decision",
            f"mission_designer_sitl_{execution_request.execution_request_id}",
        ),
        "operator_status": _artifact_ref(
            "operator_minimal_delivery_simulation_status",
            f"mission_designer_sitl_{execution_request.execution_request_id}",
        ),
        "hil": _artifact_ref(
            "hil_telemetry_review",
            f"mission_designer_sitl_{execution_request.execution_request_id}",
        ),
        "gate": _artifact_ref(
            "autonomy_gate_result",
            f"mission_designer_sitl_{execution_request.execution_request_id}",
        ),
    }
    evidence_refs = (
        execution_request_ref,
        scenario_proposal_ref,
        validation_ref,
        scenario_approval_ref,
        compile_ref,
        bounded_ref,
        contract_ref,
    )
    command_category = SimulatedCommandCategory.START_SIMULATED_DELIVERY
    command_proposal = SimulatedCommandProposal(
        proposal_id=_stable_id(
            "simulated_command_proposal",
            {
                "contract": contract.contract_id,
                "execution_request": execution_request.execution_request_id,
                "category": command_category.value,
            },
        ),
        delivery_mission_contract_ref=contract_ref,
        simulated_delivery_episode_ref=projected_refs["episode"],
        delivery_scorecard_ref=projected_refs["scorecard"],
        delivery_episode_review_ref=projected_refs["review"],
        delivery_recovery_decision_ref=projected_refs["decision"],
        operator_minimal_delivery_simulation_status_ref=projected_refs[
            "operator_status"
        ],
        hil_telemetry_review_ref=projected_refs["hil"],
        autonomy_gate_result_ref=projected_refs["gate"],
        command_category=command_category,
        approval_required=True,
        explicit_operator_approval_required=True,
        operator_escalation_required=False,
        evidence_refs=evidence_refs,
        rationale=(
            "Mission Designer SITL request is projected into the existing "
            "simulator-command preflight boundary before SITL-only upload."
        ),
        created_at=now,
        metadata={
            "source": "mission_designer_sitl_runner_preflight_projection",
            "execution_request_ref": execution_request_ref,
            "synthesized_preflight_projection": True,
            "projected_refs_are_not_observed_artifacts": True,
            "real_simulator_command_preflight_complete": False,
        },
    )
    command_proposal_ref = _artifact_ref(
        "simulated_command_proposal", command_proposal.proposal_id
    )
    command_approval = SimulatedCommandApproval(
        approval_id=_stable_id(
            "simulated_command_approval",
            {
                "proposal": command_proposal.proposal_id,
                "execution_request": execution_request.execution_request_id,
            },
        ),
        simulated_command_proposal_ref=command_proposal_ref,
        command_category=command_category,
        approval_status=SimulatedCommandApprovalStatus.APPROVED,
        approved_at=now,
        evidence_refs=evidence_refs,
        metadata={
            "source": "mission_designer_sitl_runner_explicit_operator_approval",
            "execution_request_ref": execution_request_ref,
            "synthesized_preflight_projection": True,
        },
    )
    command_approval_ref = _artifact_ref(
        "simulated_command_approval", command_approval.approval_id
    )
    receipt_ref = _artifact_ref(
        "simulated_command_receipt",
        f"mission_designer_sitl_{execution_request.execution_request_id}",
    )
    rehearsal_ref = _artifact_ref(
        "simulated_command_rehearsal_result",
        f"mission_designer_sitl_{execution_request.execution_request_id}",
    )
    run_ref = _artifact_ref(
        "px4_gazebo_bounded_simulation_run",
        f"mission_designer_sitl_{execution_request.execution_request_id}",
    )
    preflight = SimulatorCommandExecutionPreflight(
        preflight_id=_stable_id(
            "simulator_command_execution_preflight",
            {
                "proposal": command_proposal.proposal_id,
                "approval": command_approval.approval_id,
                "execution_request": execution_request.execution_request_id,
            },
        ),
        simulated_command_proposal_ref=command_proposal_ref,
        simulated_command_approval_ref=command_approval_ref,
        simulated_command_receipt_ref=receipt_ref,
        simulated_command_rehearsal_result_ref=rehearsal_ref,
        bounded_simulation_run_ref=run_ref,
        simulated_delivery_episode_ref=projected_refs["episode"],
        delivery_scorecard_ref=projected_refs["scorecard"],
        delivery_episode_review_ref=projected_refs["review"],
        delivery_recovery_decision_ref=projected_refs["decision"],
        operator_minimal_delivery_simulation_status_ref=projected_refs[
            "operator_status"
        ],
        hil_telemetry_review_ref=projected_refs["hil"],
        autonomy_gate_result_ref=projected_refs["gate"],
        command_category=command_category,
        status=SimulatorCommandExecutionPreflightStatus.READY_FOR_SIMULATOR_COMMAND,
        ready_reasons=(
            "mission_designer_prepared_request_present",
            "explicit_operator_execution_approval_observed",
            "target_endpoint_literal_whitelisted",
            "existing_sitl_upload_machinery_selected",
        ),
        approval_not_expired=True,
        rehearsal_passed=True,
        bounded_run_completed=True,
        autonomy_gate_passed=True,
        scorecard_passed=True,
        episode_review_passed=True,
        operator_minimal_status_allows_rehearsal=True,
        recorded_at=now,
        evidence_refs=evidence_refs,
        metadata={
            "source": "mission_designer_sitl_runner_preflight_projection",
            "bounded_run_ref_projected": run_ref,
            "synthesized_preflight_projection": True,
            "projected_refs_are_not_observed_artifacts": True,
            "real_simulator_command_preflight_complete": False,
            "ready_flags_are_projection_for_existing_upload_boundary": True,
        },
    )
    return command_proposal, command_approval, preflight


def _validated_task_artifacts(
    task: Mapping[str, Any],
) -> tuple[
    PX4GazeboMissionDesignerSITLExecutionRequest,
    PX4GazeboMissionScenarioProposal,
    PX4GazeboMissionScenarioValidationResult,
    PX4GazeboMissionScenarioApproval,
    PX4GazeboMissionScenarioCompileResult,
    PX4GazeboBoundedSimulationRequest,
]:
    artifacts = task.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise PX4GazeboMissionDesignerSITLRunnerError("task artifacts are required")
    execution_request = _coerce_artifact(
        artifacts,
        "px4_gazebo_mission_designer_sitl_execution_request",
        PX4GazeboMissionDesignerSITLExecutionRequest,
    )
    proposal = _coerce_artifact(
        artifacts,
        "px4_gazebo_mission_scenario_proposal",
        PX4GazeboMissionScenarioProposal,
    )
    validation = _coerce_artifact(
        artifacts,
        "px4_gazebo_mission_scenario_validation_result",
        PX4GazeboMissionScenarioValidationResult,
    )
    approval = _coerce_artifact(
        artifacts,
        "px4_gazebo_mission_scenario_approval",
        PX4GazeboMissionScenarioApproval,
    )
    compile_result = _coerce_artifact(
        artifacts,
        "px4_gazebo_mission_scenario_compile_result",
        PX4GazeboMissionScenarioCompileResult,
    )
    bounded_request = _coerce_artifact(
        artifacts,
        "px4_gazebo_bounded_simulation_request",
        PX4GazeboBoundedSimulationRequest,
    )
    if execution_request.scenario_proposal_ref != _artifact_ref(
        "px4_gazebo_mission_scenario_proposal", proposal.proposal_id
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "execution request scenario proposal ref mismatch"
        )
    if execution_request.validation_ref != _artifact_ref(
        "px4_gazebo_mission_scenario_validation_result", validation.validation_id
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "execution request validation ref mismatch"
        )
    if execution_request.approval_ref != _artifact_ref(
        "px4_gazebo_mission_scenario_approval", approval.approval_id
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "execution request approval ref mismatch"
        )
    if execution_request.compile_result_ref != _artifact_ref(
        "px4_gazebo_mission_scenario_compile_result",
        compile_result.compile_result_id,
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "execution request compile result ref mismatch"
        )
    if execution_request.bounded_simulation_request_ref != _artifact_ref(
        "px4_gazebo_bounded_simulation_request", bounded_request.request_id
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "execution request bounded request ref mismatch"
        )
    return (
        execution_request,
        proposal,
        validation,
        approval,
        compile_result,
        bounded_request,
    )


def build_px4_gazebo_mission_designer_sitl_execution_result(
    *,
    execution_request: PX4GazeboMissionDesignerSITLExecutionRequest,
    delivery_mission_contract: DeliveryMissionContract,
    simulator_command_execution_preflight: SimulatorCommandExecutionPreflight,
    mission_upload_receipt: PX4GazeboSITLMissionUploadReceipt,
    sitl_execution_opted_in: bool,
    observed_at: datetime | None = None,
) -> PX4GazeboMissionDesignerSITLExecutionResult:
    """Build a Mission Designer SITL result from observed upload facts.

    This builder intentionally treats mission-upload ACK as the only observed
    fact available at this boundary. Flight, payload-release, and dropoff success
    remain pending until a later same-session observed-flight artifact supplies
    concrete refs.
    """

    resolved_at = _utc(observed_at)
    execution_request_ref = _artifact_ref(
        "px4_gazebo_mission_designer_sitl_execution_request",
        execution_request.execution_request_id,
    )
    contract_ref = _artifact_ref(
        "delivery_mission_contract", delivery_mission_contract.contract_id
    )
    preflight_ref = _artifact_ref(
        "simulator_command_execution_preflight",
        simulator_command_execution_preflight.preflight_id,
    )
    receipt_ref = _artifact_ref(
        "px4_gazebo_sitl_mission_upload_receipt",
        mission_upload_receipt.receipt_id,
    )
    upload_observed = (
        mission_upload_receipt.upload_status
        is PX4GazeboSITLMissionUploadStatus.UPLOADED
    )
    if upload_observed:
        result_status = "mission_upload_observed_flight_evidence_pending"
        failure_reasons = MISSION_DESIGNER_SITL_FLIGHT_EVIDENCE_PENDING_REASONS
    else:
        result_status = "blocked"
        failure_reasons = tuple(mission_upload_receipt.blocked_reasons)
    artifact_only_dry_run = not sitl_execution_opted_in
    payload = {
        "execution_request_ref": execution_request_ref,
        "contract_ref": contract_ref,
        "preflight_ref": preflight_ref,
        "receipt_ref": receipt_ref,
        "status": result_status,
        "failure_reasons": failure_reasons,
        "observed_at": resolved_at.isoformat(),
    }
    return PX4GazeboMissionDesignerSITLExecutionResult(
        result_id=_stable_id(
            "px4_gazebo_mission_designer_sitl_execution_result",
            payload,
        ),
        execution_request_ref=execution_request_ref,
        delivery_mission_contract_ref=contract_ref,
        simulator_command_execution_preflight_ref=preflight_ref,
        px4_gazebo_sitl_mission_upload_receipt_ref=receipt_ref,
        result_status=result_status,
        sitl_execution_opted_in=sitl_execution_opted_in,
        artifact_only_dry_run=artifact_only_dry_run,
        actual_sitl_mission_upload_observed=upload_observed,
        actual_sitl_flight_evidence_observed=False,
        mission_upload_observed=upload_observed,
        mission_ack_observed=mission_upload_receipt.mission_ack_observed,
        mission_ack_type=mission_upload_receipt.mission_ack_type,
        mission_request_sequences=mission_upload_receipt.mission_request_sequences,
        failure_reasons=failure_reasons,
        external_dispatch_performed=mission_upload_receipt.external_dispatch_performed,
        gazebo_simulator_command_performed=(
            mission_upload_receipt.gazebo_simulator_command_performed
        ),
        mavlink_dispatch_performed=mission_upload_receipt.mavlink_dispatch_performed,
        px4_mission_upload_performed=(
            mission_upload_receipt.px4_mission_upload_performed
        ),
        gazebo_entity_mutation_performed=(
            mission_upload_receipt.gazebo_entity_mutation_performed
        ),
        hardware_target_allowed=mission_upload_receipt.hardware_target_allowed,
        physical_execution_invoked=mission_upload_receipt.physical_execution_invoked,
        ros_dispatch_performed=mission_upload_receipt.ros_dispatch_performed,
        actuator_execution_performed=(
            mission_upload_receipt.actuator_execution_performed
        ),
        observed_at=resolved_at,
        metadata={
            "source": "px4_gazebo_mission_designer_sitl_runner",
            "mission_upload_receipt_only": True,
            "flight_payload_dropoff_evidence_pending": True,
            "synthetic_payload_dropoff_success_rejected": True,
        },
    )


def build_px4_gazebo_mission_designer_sitl_flight_evidence(
    *,
    execution_result: PX4GazeboMissionDesignerSITLExecutionResult | Mapping[str, Any],
    horizontal_summary: Mapping[str, Any],
    observed_at: datetime | None = None,
) -> PX4GazeboMissionDesignerSITLFlightEvidence:
    """Build a Mission Designer flight-evidence artifact from observed SITL facts."""

    result = (
        execution_result
        if isinstance(execution_result, PX4GazeboMissionDesignerSITLExecutionResult)
        else PX4GazeboMissionDesignerSITLExecutionResult.model_validate(
            dict(execution_result)
        )
    )
    if result.actual_sitl_mission_upload_observed is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires observed Mission Designer SITL mission upload"
        )
    if result.mission_ack_observed is not True or result.mission_ack_type != (
        MAV_MISSION_ACCEPTED
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires accepted Mission Designer SITL upload ACK"
        )
    artifact_dir, summary_sha256 = _validate_horizontal_summary_artifacts(
        horizontal_summary
    )
    if horizontal_summary.get("preupload_mission_performed") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires observed preupload mission"
        )
    if horizontal_summary.get("preupload_mission_ack_observed") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires observed preupload mission ACK"
        )
    if horizontal_summary.get("preupload_mission_ack_type") != MAV_MISSION_ACCEPTED:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires accepted preupload mission ACK"
        )
    if (
        horizontal_summary.get("actual_px4_gazebo_horizontal_smoke_observed")
        is not True
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires actual PX4/Gazebo horizontal smoke observation"
        )
    if horizontal_summary.get("task_status") != "completed":
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires completed horizontal route task"
        )
    if horizontal_summary.get("final_status") != "completed":
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires completed horizontal route final status"
        )
    if horizontal_summary.get("dropoff_region_reached") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires observed dropoff-region reach"
        )
    if horizontal_summary.get("route_geofence_violation") is not False:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence rejects geofence violation"
        )
    if tuple(horizontal_summary.get("blocked_reasons") or ()) != ():
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence rejects blocked horizontal route summary"
        )
    if horizontal_summary.get("bounded_setpoint_stream_allowed") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires bounded setpoint stream allowance"
        )
    if horizontal_summary.get("unbounded_setpoint_stream_allowed") is not False:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence rejects unbounded setpoint stream allowance"
        )
    if horizontal_summary.get("offboard_mode_switch_ack_observed") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires observed offboard mode switch ACK"
        )
    if horizontal_summary.get("hardware_target_allowed") is not False:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence rejects hardware target allowance"
        )
    if horizontal_summary.get("physical_execution_invoked") is not False:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence rejects physical execution"
        )
    completed_pose_z_m = float(horizontal_summary.get("completed_pose_z_m"))
    if completed_pose_z_m > 0.15:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires observed landing pose"
        )
    mission_request_sequences = tuple(
        int(item) for item in result.mission_request_sequences
    )
    summary_sequences = tuple(
        int(item)
        for item in (
            horizontal_summary.get("preupload_mission_request_sequences") or ()
        )
    )
    if not summary_sequences:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence requires preupload mission request sequences"
        )
    if summary_sequences != mission_request_sequences:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence mission request sequences mismatch"
        )
    payload = {
        "execution_result_ref": f"px4_gazebo_mission_designer_sitl_execution_result:{result.result_id}",
        "execution_request_ref": result.execution_request_ref,
        "contract_ref": result.delivery_mission_contract_ref,
        "receipt_ref": result.px4_gazebo_sitl_mission_upload_receipt_ref,
        "horizontal_progress_m": float(horizontal_summary.get("horizontal_progress_m")),
        "completed_pose_z_m": completed_pose_z_m,
        "mission_request_sequences": mission_request_sequences,
        "horizontal_summary_sha256": summary_sha256,
    }
    return PX4GazeboMissionDesignerSITLFlightEvidence(
        flight_evidence_id=_stable_id(
            "px4_gazebo_mission_designer_sitl_flight_evidence",
            payload,
        ),
        execution_result_ref=payload["execution_result_ref"],
        execution_request_ref=result.execution_request_ref,
        delivery_mission_contract_ref=result.delivery_mission_contract_ref,
        px4_gazebo_sitl_mission_upload_receipt_ref=(
            result.px4_gazebo_sitl_mission_upload_receipt_ref
        ),
        mission_request_sequences=mission_request_sequences,
        horizontal_summary_artifact_dir=artifact_dir,
        horizontal_summary_sha256=summary_sha256,
        horizontal_progress_m=float(horizontal_summary.get("horizontal_progress_m")),
        completed_pose_z_m=completed_pose_z_m,
        climb_sample_count=int(horizontal_summary.get("climb_sample_count") or 0),
        landing_sample_count=int(horizontal_summary.get("landing_sample_count") or 0),
        route_geofence_violation=horizontal_summary.get("route_geofence_violation"),
        blocked_reasons=tuple(horizontal_summary.get("blocked_reasons") or ()),
        bounded_setpoint_stream_allowed=bool(
            horizontal_summary.get("bounded_setpoint_stream_allowed")
        ),
        unbounded_setpoint_stream_allowed=horizontal_summary.get(
            "unbounded_setpoint_stream_allowed"
        ),
        offboard_mode_switch_observed=bool(
            horizontal_summary.get("offboard_mode_switch_ack_observed")
        ),
        hardware_target_allowed=horizontal_summary.get("hardware_target_allowed"),
        physical_execution_invoked=horizontal_summary.get("physical_execution_invoked"),
        observed_at=_utc(observed_at),
        metadata={
            "source": "px4_gazebo_mission_designer_sitl_flight_evidence",
            "horizontal_route_artifact_dir": artifact_dir,
            "horizontal_summary_sha256": summary_sha256,
            "payload_release_evidence_available_but_not_attached": bool(
                horizontal_summary.get("payload_release_observed")
            ),
            "payload_release_and_dropoff_remain_separate_observed_fact_artifacts": True,
        },
    )


def build_px4_gazebo_mission_designer_sitl_execution_result_with_flight_evidence(
    *,
    execution_result: PX4GazeboMissionDesignerSITLExecutionResult | Mapping[str, Any],
    flight_evidence: PX4GazeboMissionDesignerSITLFlightEvidence | Mapping[str, Any],
    observed_at: datetime | None = None,
) -> PX4GazeboMissionDesignerSITLExecutionResult:
    """Return a Mission Designer SITL result updated with observed flight evidence."""

    result = (
        execution_result
        if isinstance(execution_result, PX4GazeboMissionDesignerSITLExecutionResult)
        else PX4GazeboMissionDesignerSITLExecutionResult.model_validate(
            dict(execution_result)
        )
    )
    evidence = (
        flight_evidence
        if isinstance(flight_evidence, PX4GazeboMissionDesignerSITLFlightEvidence)
        else PX4GazeboMissionDesignerSITLFlightEvidence.model_validate(
            dict(flight_evidence)
        )
    )
    _validate_flight_evidence_artifact_binding(
        evidence.horizontal_summary_artifact_dir,
        evidence.horizontal_summary_sha256,
        evidence=evidence,
    )
    expected_result_ref = (
        f"px4_gazebo_mission_designer_sitl_execution_result:{result.result_id}"
    )
    if evidence.execution_result_ref != expected_result_ref:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence execution result ref mismatch"
        )
    if evidence.execution_request_ref != result.execution_request_ref:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence execution request ref mismatch"
        )
    if evidence.delivery_mission_contract_ref != result.delivery_mission_contract_ref:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence contract ref mismatch"
        )
    if (
        evidence.px4_gazebo_sitl_mission_upload_receipt_ref
        != result.px4_gazebo_sitl_mission_upload_receipt_ref
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "flight evidence mission upload receipt ref mismatch"
        )
    evidence_ref = (
        "px4_gazebo_mission_designer_sitl_flight_evidence:"
        f"{evidence.flight_evidence_id}"
    )
    payload = {
        "previous_result": result.result_id,
        "flight_evidence_ref": evidence_ref,
        "observed_at": _utc(observed_at).isoformat(),
    }
    return PX4GazeboMissionDesignerSITLExecutionResult(
        result_id=_stable_id(
            "px4_gazebo_mission_designer_sitl_execution_result",
            payload,
        ),
        execution_request_ref=result.execution_request_ref,
        delivery_mission_contract_ref=result.delivery_mission_contract_ref,
        simulator_command_execution_preflight_ref=(
            result.simulator_command_execution_preflight_ref
        ),
        px4_gazebo_sitl_mission_upload_receipt_ref=(
            result.px4_gazebo_sitl_mission_upload_receipt_ref
        ),
        result_status="flight_evidence_observed_payload_dropoff_pending",
        sitl_execution_opted_in=result.sitl_execution_opted_in,
        artifact_only_dry_run=False,
        actual_sitl_mission_upload_observed=True,
        actual_sitl_flight_evidence_observed=True,
        flight_evidence_ref=evidence_ref,
        flight_evidence_source_execution_result_ref=expected_result_ref,
        mission_upload_observed=True,
        mission_ack_observed=True,
        mission_ack_type=result.mission_ack_type,
        mission_request_sequences=result.mission_request_sequences,
        actual_takeoff_observed=True,
        actual_dropoff_region_reached=True,
        actual_land_observed=True,
        payload_release_observed=False,
        payload_release_verified=False,
        dropoff_verified=False,
        failure_reasons=MISSION_DESIGNER_SITL_PAYLOAD_DROPOFF_PENDING_REASONS,
        external_dispatch_performed=result.external_dispatch_performed,
        gazebo_simulator_command_performed=result.gazebo_simulator_command_performed,
        mavlink_dispatch_performed=result.mavlink_dispatch_performed,
        px4_mission_upload_performed=result.px4_mission_upload_performed,
        gazebo_entity_mutation_performed=result.gazebo_entity_mutation_performed,
        hardware_target_allowed=result.hardware_target_allowed,
        physical_execution_invoked=result.physical_execution_invoked,
        ros_dispatch_performed=result.ros_dispatch_performed,
        actuator_execution_performed=result.actuator_execution_performed,
        observed_at=_utc(observed_at),
        metadata={
            **result.metadata,
            "mission_upload_receipt_only": False,
            "flight_evidence_attached": True,
            "flight_payload_dropoff_evidence_pending": True,
            "synthetic_payload_dropoff_success_rejected": True,
        },
    )


def build_px4_gazebo_mission_designer_sitl_payload_release_observation(
    *,
    execution_result: PX4GazeboMissionDesignerSITLExecutionResult | Mapping[str, Any],
    flight_evidence: PX4GazeboMissionDesignerSITLFlightEvidence | Mapping[str, Any],
    horizontal_summary: Mapping[str, Any],
    live_flight_run: Mapping[str, Any] | None = None,
    observed_at: datetime | None = None,
) -> tuple[
    PX4GazeboMissionDesignerSITLPayloadReleaseObservation,
    PX4GazeboSITLPayloadReleaseEvent,
]:
    """Build observed payload-release evidence without verifying dropoff."""

    result = (
        execution_result
        if isinstance(execution_result, PX4GazeboMissionDesignerSITLExecutionResult)
        else PX4GazeboMissionDesignerSITLExecutionResult.model_validate(
            dict(execution_result)
        )
    )
    evidence = (
        flight_evidence
        if isinstance(flight_evidence, PX4GazeboMissionDesignerSITLFlightEvidence)
        else PX4GazeboMissionDesignerSITLFlightEvidence.model_validate(
            dict(flight_evidence)
        )
    )
    if result.actual_sitl_flight_evidence_observed is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation requires observed SITL flight evidence"
        )
    expected_flight_ref = (
        "px4_gazebo_mission_designer_sitl_flight_evidence:"
        f"{evidence.flight_evidence_id}"
    )
    if result.flight_evidence_ref != expected_flight_ref:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation flight evidence ref mismatch"
        )
    if evidence.execution_request_ref != result.execution_request_ref:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation execution request ref mismatch"
        )
    if evidence.delivery_mission_contract_ref != result.delivery_mission_contract_ref:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation contract ref mismatch"
        )
    if (
        evidence.px4_gazebo_sitl_mission_upload_receipt_ref
        != result.px4_gazebo_sitl_mission_upload_receipt_ref
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation mission upload receipt ref mismatch"
        )
    artifact_dir, summary_sha256 = _validate_horizontal_summary_artifacts(
        horizontal_summary
    )
    if summary_sha256 != evidence.horizontal_summary_sha256:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation summary hash mismatch"
        )
    if (
        evidence.execution_result_ref
        != result.flight_evidence_source_execution_result_ref
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation flight evidence source result ref mismatch"
        )
    live_binding = _live_flight_run_binding(
        live_flight_run,
        execution_result=result,
        horizontal_summary_sha256=summary_sha256,
        bound_execution_result_ref=result.flight_evidence_source_execution_result_ref,
    )
    _validate_flight_evidence_artifact_binding(
        evidence.horizontal_summary_artifact_dir,
        evidence.horizontal_summary_sha256,
        evidence=evidence,
    )
    if horizontal_summary.get("payload_release_observed") is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation requires observed release"
        )
    event_source = str(horizontal_summary.get("payload_release_event_source") or "")
    if event_source not in MISSION_DESIGNER_SITL_PAYLOAD_RELEASE_EVENT_SOURCES:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation requires allowlisted event source"
        )
    release_observed_at = _payload_release_observed_at(horizontal_summary)
    release_x = _payload_release_position(
        horizontal_summary, "payload_release_position_x_m"
    )
    release_y = _payload_release_position(
        horizontal_summary, "payload_release_position_y_m"
    )
    release_z = _payload_release_position(
        horizontal_summary, "payload_release_position_z_m"
    )
    payload_id = _payload_release_payload_id(horizontal_summary)
    release_event = build_px4_gazebo_sitl_payload_release_event(
        event_source=event_source,  # type: ignore[arg-type]
        payload_id=payload_id,
        release_position_x_m=release_x,
        release_position_y_m=release_y,
        release_position_z_m=release_z,
        observed_at=release_observed_at,
        metadata={
            "source": "px4_gazebo_mission_designer_sitl_payload_release_observation",
            "horizontal_summary_sha256": summary_sha256,
        },
    )
    payload = {
        "execution_result_ref": (
            f"px4_gazebo_mission_designer_sitl_execution_result:{result.result_id}"
        ),
        "flight_evidence_ref": expected_flight_ref,
        "payload_release_event_ref": (
            f"px4_gazebo_sitl_payload_release_event:{release_event.event_id}"
        ),
        "event_source": event_source,
        "payload_id": payload_id,
        "release_position": (release_x, release_y, release_z),
        "release_observed_at": release_observed_at.isoformat(),
        "horizontal_summary_sha256": summary_sha256,
        "live_flight_run_ref": live_binding.get("live_flight_run_ref", ""),
        "mission_item_binding_sha256": live_binding.get(
            "mission_item_binding_sha256", ""
        ),
    }
    payload_summary = _payload_release_summary_payload(horizontal_summary)
    observation = PX4GazeboMissionDesignerSITLPayloadReleaseObservation(
        observation_id=_stable_id(
            "px4_gazebo_mission_designer_sitl_payload_release_observation",
            payload,
        ),
        execution_result_ref=payload["execution_result_ref"],
        flight_evidence_ref=expected_flight_ref,
        execution_request_ref=result.execution_request_ref,
        delivery_mission_contract_ref=result.delivery_mission_contract_ref,
        px4_gazebo_sitl_mission_upload_receipt_ref=(
            result.px4_gazebo_sitl_mission_upload_receipt_ref
        ),
        payload_release_event_ref=payload["payload_release_event_ref"],
        event_source=event_source,  # type: ignore[arg-type]
        payload_id=payload_id,
        payload_release_observed_at=release_observed_at,
        release_position_x_m=release_x,
        release_position_y_m=release_y,
        release_position_z_m=release_z,
        horizontal_summary_artifact_dir=artifact_dir,
        horizontal_summary_sha256=summary_sha256,
        live_flight_run_ref=str(live_binding.get("live_flight_run_ref", "")),
        live_flight_run_schema_version=str(
            live_binding.get("live_flight_run_schema_version", "")
        ),
        live_flight_run_summary_sha256=str(
            live_binding.get("live_flight_run_summary_sha256", "")
        ),
        mission_item_binding_sha256=str(
            live_binding.get("mission_item_binding_sha256", "")
        ),
        same_gateway_execution_run_observed=bool(
            live_binding.get("same_gateway_execution_run_observed", False)
        ),
        payload_release_bound_to_live_run=bool(live_binding),
        gazebo_detachable_joint_release_observed=bool(
            payload_summary.get("gazebo_detachable_joint_release_observed")
        )
        or event_source.startswith("gazebo_"),
        mavlink_release_observed=event_source.startswith("mavlink_"),
        observed_at=_utc(observed_at),
        metadata={
            "source": "px4_gazebo_mission_designer_sitl_payload_release_observation",
            "payload_release_event_ref": payload["payload_release_event_ref"],
            "dropoff_verification_pending": True,
            "payload_release_does_not_verify_dropoff": True,
            "horizontal_summary_sha256": summary_sha256,
            "live_flight_run_ref": live_binding.get("live_flight_run_ref", ""),
            "mission_item_binding_sha256": live_binding.get(
                "mission_item_binding_sha256", ""
            ),
        },
    )
    return observation, release_event


def build_px4_gazebo_mission_designer_sitl_dropoff_verification(
    *,
    execution_result: PX4GazeboMissionDesignerSITLExecutionResult | Mapping[str, Any],
    flight_evidence: PX4GazeboMissionDesignerSITLFlightEvidence | Mapping[str, Any],
    payload_release_observation: (
        PX4GazeboMissionDesignerSITLPayloadReleaseObservation | Mapping[str, Any]
    ),
    payload_release_event: PX4GazeboSITLPayloadReleaseEvent | Mapping[str, Any],
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    horizontal_summary: Mapping[str, Any],
    live_flight_run: Mapping[str, Any] | None = None,
    observed_at: datetime | None = None,
) -> tuple[
    PX4GazeboMissionDesignerSITLDropoffVerification,
    PX4GazeboSITLDropoffFlightFact,
    PX4GazeboSITLDropoffVerification,
]:
    """Build Mission Designer dropoff verification from observed SITL facts."""

    result = PX4GazeboMissionDesignerSITLExecutionResult.model_validate(
        _model_payload(execution_result)
    )
    evidence = PX4GazeboMissionDesignerSITLFlightEvidence.model_validate(
        _model_payload(flight_evidence)
    )
    observation = PX4GazeboMissionDesignerSITLPayloadReleaseObservation.model_validate(
        _model_payload(payload_release_observation)
    )
    release_event = PX4GazeboSITLPayloadReleaseEvent.model_validate(
        _model_payload(payload_release_event)
    )
    contract = (
        delivery_mission_contract
        if isinstance(delivery_mission_contract, DeliveryMissionContract)
        else DeliveryMissionContract.model_validate(dict(delivery_mission_contract))
    )
    if result.actual_sitl_flight_evidence_observed is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification requires observed SITL flight evidence"
        )
    expected_execution_result_ref = (
        f"px4_gazebo_mission_designer_sitl_execution_result:{result.result_id}"
    )
    expected_flight_ref = f"px4_gazebo_mission_designer_sitl_flight_evidence:{evidence.flight_evidence_id}"
    expected_payload_observation_ref = (
        "px4_gazebo_mission_designer_sitl_payload_release_observation:"
        f"{observation.observation_id}"
    )
    expected_payload_release_ref = (
        f"px4_gazebo_sitl_payload_release_event:{release_event.event_id}"
    )
    if result.flight_evidence_ref != expected_flight_ref:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification flight evidence ref mismatch"
        )
    if observation.execution_result_ref != expected_execution_result_ref:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification payload observation result ref mismatch"
        )
    if observation.flight_evidence_ref != expected_flight_ref:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification payload observation flight ref mismatch"
        )
    if observation.payload_release_event_ref != expected_payload_release_ref:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification payload release event ref mismatch"
        )
    if observation.execution_request_ref != result.execution_request_ref:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification execution request ref mismatch"
        )
    if (
        observation.delivery_mission_contract_ref
        != result.delivery_mission_contract_ref
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification contract ref mismatch"
        )
    if (
        observation.delivery_mission_contract_ref
        != f"delivery_mission_contract:{contract.contract_id}"
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification delivery contract artifact mismatch"
        )
    if (
        observation.px4_gazebo_sitl_mission_upload_receipt_ref
        != result.px4_gazebo_sitl_mission_upload_receipt_ref
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification mission upload receipt ref mismatch"
        )
    artifact_dir, summary_sha256 = _validate_horizontal_summary_artifacts(
        horizontal_summary
    )
    if summary_sha256 != evidence.horizontal_summary_sha256:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification flight evidence summary hash mismatch"
        )
    if summary_sha256 != observation.horizontal_summary_sha256:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification payload observation summary hash mismatch"
        )
    if (
        evidence.execution_result_ref
        != result.flight_evidence_source_execution_result_ref
    ):
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification flight evidence source result ref mismatch"
        )
    live_binding = _live_flight_run_binding(
        live_flight_run,
        execution_result=result,
        horizontal_summary_sha256=summary_sha256,
        bound_execution_result_ref=result.flight_evidence_source_execution_result_ref,
    )
    if live_binding:
        live_run_ref = str(live_binding["live_flight_run_ref"])
        mission_item_binding = str(live_binding["mission_item_binding_sha256"])
        if observation.live_flight_run_ref != live_run_ref:
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "dropoff verification payload observation live run ref mismatch"
            )
        if observation.mission_item_binding_sha256 != mission_item_binding:
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "dropoff verification payload observation mission item binding mismatch"
            )
        if observation.payload_release_bound_to_live_run is not True:
            raise PX4GazeboMissionDesignerSITLRunnerError(
                "dropoff verification requires payload observation bound to live run"
            )
    _validate_flight_evidence_artifact_binding(
        evidence.horizontal_summary_artifact_dir,
        evidence.horizontal_summary_sha256,
        evidence=evidence,
    )
    _validate_payload_release_observation_summary_fields(observation)
    expected_release_event = _dropoff_release_event_from_summary(horizontal_summary)
    if expected_release_event.event_id != release_event.event_id:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification release event summary mismatch"
        )
    dropoff_flight_fact = _dropoff_flight_fact_from_summary(
        horizontal_summary,
        payload_release_event=release_event,
        sitl_mission_upload_receipt_ref=(
            result.px4_gazebo_sitl_mission_upload_receipt_ref
        ),
        telemetry_ref=expected_flight_ref,
    )
    sitl_verification = build_px4_gazebo_sitl_dropoff_verification(
        delivery_mission_contract=contract,
        dropoff_flight_fact=dropoff_flight_fact,
        payload_release_event=release_event,
        dropoff_zone_radius_m=SITL_DROPOFF_DEFAULT_ZONE_RADIUS_M,
        altitude_tolerance_m=SITL_DROPOFF_DEFAULT_ALTITUDE_TOLERANCE_M,
        release_time_window_seconds=SITL_DROPOFF_DEFAULT_RELEASE_TIME_WINDOW_SECONDS,
        expected_mission_item_seq=SITL_DROPOFF_DEFAULT_MISSION_ITEM_SEQ,
        now=observed_at,
    )
    if sitl_verification.status is not PX4GazeboSITLDropoffVerificationStatus.VERIFIED:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "dropoff verification requires all observed predicates: "
            + ", ".join(sitl_verification.blocked_reasons)
        )
    release_distance = float(
        sitl_verification.metadata.get("release_distance_to_dropoff_m")
    )
    release_altitude_error = float(
        sitl_verification.metadata.get("release_altitude_error_m")
    )
    release_time_delta = float(
        sitl_verification.metadata.get("release_time_delta_seconds")
    )
    payload = {
        "execution_result_ref": expected_execution_result_ref,
        "flight_evidence_ref": expected_flight_ref,
        "payload_release_observation_ref": expected_payload_observation_ref,
        "dropoff_flight_fact_ref": (
            "px4_gazebo_sitl_dropoff_flight_fact:" f"{dropoff_flight_fact.fact_id}"
        ),
        "payload_release_event_ref": expected_payload_release_ref,
        "sitl_dropoff_verification_ref": (
            "px4_gazebo_sitl_dropoff_verification:"
            f"{sitl_verification.verification_id}"
        ),
        "horizontal_summary_sha256": summary_sha256,
        "live_flight_run_ref": live_binding.get("live_flight_run_ref", ""),
        "mission_item_binding_sha256": live_binding.get(
            "mission_item_binding_sha256", ""
        ),
    }
    verification = PX4GazeboMissionDesignerSITLDropoffVerification(
        verification_id=_stable_id(
            "px4_gazebo_mission_designer_sitl_dropoff_verification",
            payload,
        ),
        execution_result_ref=expected_execution_result_ref,
        flight_evidence_ref=expected_flight_ref,
        payload_release_observation_ref=expected_payload_observation_ref,
        execution_request_ref=result.execution_request_ref,
        delivery_mission_contract_ref=result.delivery_mission_contract_ref,
        px4_gazebo_sitl_mission_upload_receipt_ref=(
            result.px4_gazebo_sitl_mission_upload_receipt_ref
        ),
        dropoff_flight_fact_ref=payload["dropoff_flight_fact_ref"],
        payload_release_event_ref=expected_payload_release_ref,
        sitl_dropoff_verification_ref=payload["sitl_dropoff_verification_ref"],
        horizontal_summary_artifact_dir=artifact_dir,
        horizontal_summary_sha256=summary_sha256,
        live_flight_run_ref=str(live_binding.get("live_flight_run_ref", "")),
        live_flight_run_schema_version=str(
            live_binding.get("live_flight_run_schema_version", "")
        ),
        live_flight_run_summary_sha256=str(
            live_binding.get("live_flight_run_summary_sha256", "")
        ),
        mission_item_binding_sha256=str(
            live_binding.get("mission_item_binding_sha256", "")
        ),
        same_gateway_execution_run_observed=bool(
            live_binding.get("same_gateway_execution_run_observed", False)
        ),
        dropoff_verification_bound_to_live_run=bool(live_binding),
        dropoff_zone_radius_m=sitl_verification.dropoff_zone_radius_m,
        altitude_tolerance_m=sitl_verification.altitude_tolerance_m,
        expected_mission_item_seq=sitl_verification.expected_mission_item_seq,
        observed_distance_to_dropoff_m=(
            sitl_verification.observed_distance_to_dropoff_m
        ),
        observed_altitude_error_m=sitl_verification.observed_altitude_error_m,
        release_distance_to_dropoff_m=release_distance,
        release_altitude_error_m=release_altitude_error,
        release_time_delta_seconds=release_time_delta,
        observed_at=_utc(observed_at),
        metadata={
            "source": "px4_gazebo_mission_designer_sitl_dropoff_verification",
            "horizontal_summary_sha256": summary_sha256,
            "payload_release_observation_ref": expected_payload_observation_ref,
            "sitl_dropoff_verification_ref": payload["sitl_dropoff_verification_ref"],
            "live_flight_run_ref": live_binding.get("live_flight_run_ref", ""),
            "mission_item_binding_sha256": live_binding.get(
                "mission_item_binding_sha256", ""
            ),
            "predicate": sitl_verification.metadata.get("predicate"),
            "observed_facts_only": True,
        },
    )
    return verification, dropoff_flight_fact, sitl_verification


def attach_px4_gazebo_mission_designer_sitl_payload_release_observation(
    task_id: str,
    *,
    horizontal_summary: Mapping[str, Any],
    task_store_factory: Callable[[], TaskStore] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Attach observed payload-release evidence to a Mission Designer SITL task."""

    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            f"task {task_id} not found; cannot attach SITL payload release observation"
        )
    artifacts = current.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    if "px4_gazebo_mission_designer_sitl_execution_result" not in artifacts:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation requires existing execution result artifact"
        )
    if "px4_gazebo_mission_designer_sitl_flight_evidence" not in artifacts:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "payload release observation requires existing flight evidence artifact"
        )
    execution_result = PX4GazeboMissionDesignerSITLExecutionResult.model_validate(
        artifacts["px4_gazebo_mission_designer_sitl_execution_result"]
    )
    flight_evidence = PX4GazeboMissionDesignerSITLFlightEvidence.model_validate(
        artifacts["px4_gazebo_mission_designer_sitl_flight_evidence"]
    )
    live_flight_run = _required_live_flight_run_artifact(artifacts)
    observation, release_event = (
        build_px4_gazebo_mission_designer_sitl_payload_release_observation(
            execution_result=execution_result,
            flight_evidence=flight_evidence,
            horizontal_summary=horizontal_summary,
            live_flight_run=live_flight_run,
            observed_at=now,
        )
    )
    updated = store.update(
        task_id,
        artifacts={
            "px4_gazebo_sitl_payload_release_event": release_event.model_dump(
                mode="json"
            ),
            "px4_gazebo_mission_designer_sitl_payload_release_observation": (
                observation.model_dump(mode="json")
            ),
        },
        metadata={
            "payload_release_observation_attached": True,
            "payload_release_observed": True,
            "payload_release_event_ref": observation.payload_release_event_ref,
            "live_flight_run_ref": observation.live_flight_run_ref,
            "mission_item_binding_sha256": observation.mission_item_binding_sha256,
            "dropoff_verified": False,
        },
    )
    if updated is None:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            f"task {task_id} disappeared while attaching SITL payload release observation"
        )
    return {
        "task": updated,
        "px4_gazebo_sitl_payload_release_event": release_event.model_dump(mode="json"),
        "px4_gazebo_mission_designer_sitl_payload_release_observation": (
            observation.model_dump(mode="json")
        ),
        "summary": {
            "task_id": updated["task_id"],
            "task_status": updated["status"],
            "payload_release_observation_ref": (
                "px4_gazebo_mission_designer_sitl_payload_release_observation:"
                f"{observation.observation_id}"
            ),
            "payload_release_event_ref": observation.payload_release_event_ref,
            "payload_release_observed": observation.payload_release_observed,
            "payload_release_event_source": observation.event_source,
            "live_flight_run_ref": observation.live_flight_run_ref,
            "payload_release_bound_to_live_run": (
                observation.payload_release_bound_to_live_run
            ),
            "dropoff_verified": observation.dropoff_verified,
            "hardware_target_allowed": observation.hardware_target_allowed,
            "physical_execution_invoked": observation.physical_execution_invoked,
            "ros_dispatch_performed": observation.ros_dispatch_performed,
            "actuator_execution_performed": observation.actuator_execution_performed,
            "synthetic_success_allowed": observation.synthetic_success_allowed,
        },
    }


def attach_px4_gazebo_mission_designer_sitl_dropoff_verification(
    task_id: str,
    *,
    horizontal_summary: Mapping[str, Any],
    task_store_factory: Callable[[], TaskStore] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Attach observed dropoff verification to a Mission Designer SITL task."""

    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            f"task {task_id} not found; cannot attach SITL dropoff verification"
        )
    artifacts = current.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    required_artifacts = (
        "delivery_mission_contract",
        "px4_gazebo_mission_designer_sitl_execution_result",
        "px4_gazebo_mission_designer_sitl_flight_evidence",
        "px4_gazebo_mission_designer_sitl_payload_release_observation",
        "px4_gazebo_sitl_payload_release_event",
    )
    for key in required_artifacts:
        if key not in artifacts:
            raise PX4GazeboMissionDesignerSITLRunnerError(
                f"dropoff verification requires existing {key} artifact"
            )
    live_flight_run = _required_live_flight_run_artifact(artifacts)
    verification, dropoff_flight_fact, sitl_verification = (
        build_px4_gazebo_mission_designer_sitl_dropoff_verification(
            execution_result=artifacts[
                "px4_gazebo_mission_designer_sitl_execution_result"
            ],
            flight_evidence=artifacts[
                "px4_gazebo_mission_designer_sitl_flight_evidence"
            ],
            payload_release_observation=artifacts[
                "px4_gazebo_mission_designer_sitl_payload_release_observation"
            ],
            payload_release_event=artifacts["px4_gazebo_sitl_payload_release_event"],
            delivery_mission_contract=artifacts["delivery_mission_contract"],
            horizontal_summary=horizontal_summary,
            live_flight_run=live_flight_run,
            observed_at=now,
        )
    )
    updated = store.update(
        task_id,
        artifacts={
            "px4_gazebo_sitl_dropoff_flight_fact": (
                dropoff_flight_fact.model_dump(mode="json")
            ),
            "px4_gazebo_sitl_dropoff_verification": (
                sitl_verification.model_dump(mode="json")
            ),
            "px4_gazebo_mission_designer_sitl_dropoff_verification": (
                verification.model_dump(mode="json")
            ),
        },
        metadata={
            "dropoff_verification_attached": True,
            "dropoff_verified": verification.dropoff_verified,
            "dropoff_verification_ref": verification.sitl_dropoff_verification_ref,
            "mission_designer_dropoff_verification_ref": (
                "px4_gazebo_mission_designer_sitl_dropoff_verification:"
                f"{verification.verification_id}"
            ),
            "payload_release_observed": True,
            "payload_release_verified": True,
            "live_flight_run_ref": verification.live_flight_run_ref,
            "mission_item_binding_sha256": verification.mission_item_binding_sha256,
            "synthetic_success_allowed": False,
        },
    )
    if updated is None:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            f"task {task_id} disappeared while attaching SITL dropoff verification"
        )
    return {
        "task": updated,
        "px4_gazebo_sitl_dropoff_flight_fact": dropoff_flight_fact.model_dump(
            mode="json"
        ),
        "px4_gazebo_sitl_dropoff_verification": sitl_verification.model_dump(
            mode="json"
        ),
        "px4_gazebo_mission_designer_sitl_dropoff_verification": (
            verification.model_dump(mode="json")
        ),
        "summary": {
            "task_id": updated["task_id"],
            "task_status": updated["status"],
            "mission_designer_dropoff_verification_ref": (
                "px4_gazebo_mission_designer_sitl_dropoff_verification:"
                f"{verification.verification_id}"
            ),
            "dropoff_verification_ref": verification.sitl_dropoff_verification_ref,
            "dropoff_verified": verification.dropoff_verified,
            "payload_release_observed": verification.payload_release_observed,
            "payload_release_verified": verification.payload_release_verified,
            "predicate_mode": verification.predicate_mode,
            "live_flight_run_ref": verification.live_flight_run_ref,
            "dropoff_verification_bound_to_live_run": (
                verification.dropoff_verification_bound_to_live_run
            ),
            "observed_distance_to_dropoff_m": (
                verification.observed_distance_to_dropoff_m
            ),
            "release_distance_to_dropoff_m": (
                verification.release_distance_to_dropoff_m
            ),
            "release_time_delta_seconds": verification.release_time_delta_seconds,
            "hardware_target_allowed": verification.hardware_target_allowed,
            "physical_execution_invoked": verification.physical_execution_invoked,
            "ros_dispatch_performed": verification.ros_dispatch_performed,
            "actuator_execution_performed": verification.actuator_execution_performed,
            "synthetic_success_allowed": verification.synthetic_success_allowed,
        },
    }


def attach_px4_gazebo_mission_designer_sitl_flight_evidence(
    task_id: str,
    *,
    horizontal_summary: Mapping[str, Any],
    task_store_factory: Callable[[], TaskStore] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Attach observed flight evidence to an executed Mission Designer SITL task."""

    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            f"task {task_id} not found; cannot attach SITL flight evidence"
        )
    artifacts = current.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    if "px4_gazebo_mission_designer_sitl_execution_result" not in artifacts:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "SITL flight evidence requires existing execution result artifact"
        )
    execution_result = PX4GazeboMissionDesignerSITLExecutionResult.model_validate(
        artifacts["px4_gazebo_mission_designer_sitl_execution_result"]
    )
    flight_evidence = build_px4_gazebo_mission_designer_sitl_flight_evidence(
        execution_result=execution_result,
        horizontal_summary=horizontal_summary,
        observed_at=now,
    )
    updated_result = (
        build_px4_gazebo_mission_designer_sitl_execution_result_with_flight_evidence(
            execution_result=execution_result,
            flight_evidence=flight_evidence,
            observed_at=now,
        )
    )
    updated = store.update(
        task_id,
        status="completed",
        artifacts={
            "px4_gazebo_mission_designer_sitl_flight_evidence": (
                flight_evidence.model_dump(mode="json")
            ),
            "px4_gazebo_mission_designer_sitl_execution_result": (
                updated_result.model_dump(mode="json")
            ),
        },
        metadata={
            "actual_sitl_flight_evidence_observed": True,
            "sitl_execution_result_status": updated_result.result_status,
            "flight_evidence_ref": updated_result.flight_evidence_ref,
            "payload_release_observed": False,
            "payload_release_verified": False,
            "dropoff_verified": False,
        },
    )
    if updated is None:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            f"task {task_id} disappeared while attaching SITL flight evidence"
        )
    return {
        "task": updated,
        "px4_gazebo_mission_designer_sitl_flight_evidence": (
            flight_evidence.model_dump(mode="json")
        ),
        "px4_gazebo_mission_designer_sitl_execution_result": (
            updated_result.model_dump(mode="json")
        ),
        "summary": {
            "task_id": updated["task_id"],
            "task_status": updated["status"],
            "sitl_execution_result_status": updated_result.result_status,
            "actual_sitl_mission_upload_observed": (
                updated_result.actual_sitl_mission_upload_observed
            ),
            "actual_sitl_flight_evidence_observed": (
                updated_result.actual_sitl_flight_evidence_observed
            ),
            "flight_evidence_ref": updated_result.flight_evidence_ref,
            "actual_takeoff_observed": updated_result.actual_takeoff_observed,
            "actual_dropoff_region_reached": (
                updated_result.actual_dropoff_region_reached
            ),
            "actual_land_observed": updated_result.actual_land_observed,
            "payload_release_observed": updated_result.payload_release_observed,
            "payload_release_verified": updated_result.payload_release_verified,
            "dropoff_verified": updated_result.dropoff_verified,
            "failure_reasons": list(updated_result.failure_reasons),
            "synthetic_success_allowed": updated_result.synthetic_success_allowed,
        },
    }


def run_px4_gazebo_mission_designer_sitl_execution(
    task_id: str,
    *,
    explicit_execution_approval: bool,
    allow_sitl_execution: bool,
    uploader: PX4GazeboSITLMissionUploader | None = None,
    timeout_seconds: float = 5.0,
    task_store_factory: Callable[[], TaskStore] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Attach a SITL mission-upload receipt to a prepared Mission Designer task."""

    if explicit_execution_approval is not True:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "explicit execution approval is required"
        )
    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            f"task {task_id} not found; cannot run Mission Designer SITL execution"
        )
    if current.get("kind") != MISSION_DESIGNER_SITL_TASK_KIND:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "Mission Designer SITL execution requires prepared SITL task kind"
        )
    if current.get("status") != "pending":
        raise PX4GazeboMissionDesignerSITLRunnerError(
            "Mission Designer SITL execution requires pending prepared task"
        )
    (
        execution_request,
        proposal,
        validation,
        approval,
        compile_result,
        bounded_request,
    ) = _validated_task_artifacts(current)
    executed_at = _utc(now)
    current_artifacts = current.get("artifacts") or {}
    current_artifacts = (
        current_artifacts if isinstance(current_artifacts, Mapping) else {}
    )
    coordinate_route = current_artifacts.get("mission_designer_coordinate_pair_route")
    coordinate_route = (
        coordinate_route if isinstance(coordinate_route, Mapping) else None
    )
    coordinate_route_items = _operator_coordinate_sitl_mission_items(current)
    mission_items_source = (
        "operator_coordinate_pair_route"
        if coordinate_route_items is not None
        else "fixed_safe_route_projection"
    )
    contract = _mission_contract_from_designer(
        execution_request=execution_request,
        bounded_request=bounded_request,
        coordinate_route=coordinate_route,
        now=executed_at,
    )
    wind_condition_artifacts = _mission_designer_wind_condition_artifacts(
        coordinate_route=coordinate_route,
        contract=contract,
        now=executed_at,
    )
    command_proposal, command_approval, preflight = _project_simulator_preflight_chain(
        contract=contract,
        execution_request=execution_request,
        proposal=proposal,
        validation=validation,
        approval=approval,
        compile_result=compile_result,
        bounded_request=bounded_request,
        now=executed_at,
    )
    receipt = build_px4_gazebo_sitl_mission_upload_receipt(
        delivery_mission_contract=contract,
        simulator_command_execution_preflight=preflight,
        simulated_command_proposal=command_proposal,
        simulated_command_approval=command_approval,
        target_endpoint=PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
        allow_sitl_mission_upload=allow_sitl_execution,
        uploader=uploader,
        timeout_seconds=timeout_seconds,
        geofence_radius_m=MISSION_DESIGNER_SITL_UPLOAD_GEOFENCE_RADIUS_M,
        mission_items_override=coordinate_route_items,
        now=executed_at,
    )
    execution_result = build_px4_gazebo_mission_designer_sitl_execution_result(
        execution_request=execution_request,
        delivery_mission_contract=contract,
        simulator_command_execution_preflight=preflight,
        mission_upload_receipt=receipt,
        sitl_execution_opted_in=allow_sitl_execution,
        observed_at=executed_at,
    )
    uploaded = receipt.upload_status is PX4GazeboSITLMissionUploadStatus.UPLOADED
    status = "completed" if uploaded else "blocked"
    artifacts = {
        "delivery_mission_contract": contract.model_dump(mode="json"),
        "simulated_command_proposal": command_proposal.model_dump(mode="json"),
        "simulated_command_approval": command_approval.model_dump(mode="json"),
        "simulator_command_execution_preflight": preflight.model_dump(mode="json"),
        "px4_gazebo_sitl_mission_upload_receipt": receipt.model_dump(mode="json"),
        "px4_gazebo_mission_designer_sitl_execution_result": (
            execution_result.model_dump(mode="json")
        ),
        **wind_condition_artifacts,
    }
    metadata = {
        "execution_runner_source": "px4_gazebo_mission_designer_sitl_runner",
        "sitl_execution_opt_in_env": MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV,
        "sitl_execution_opted_in": allow_sitl_execution,
        "explicit_execution_approval": explicit_execution_approval,
        "upload_status": receipt.upload_status.value,
        "blocked_reasons": list(receipt.blocked_reasons),
        "external_dispatch_performed": receipt.external_dispatch_performed,
        "mavlink_dispatch_performed": receipt.mavlink_dispatch_performed,
        "px4_mission_upload_performed": receipt.px4_mission_upload_performed,
        "hardware_target_allowed": receipt.hardware_target_allowed,
        "physical_execution_invoked": receipt.physical_execution_invoked,
        "sitl_execution_result_status": execution_result.result_status,
        "actual_sitl_mission_upload_observed": (
            execution_result.actual_sitl_mission_upload_observed
        ),
        "actual_sitl_flight_evidence_observed": (
            execution_result.actual_sitl_flight_evidence_observed
        ),
        "payload_release_observed": execution_result.payload_release_observed,
        "payload_release_verified": execution_result.payload_release_verified,
        "dropoff_verified": execution_result.dropoff_verified,
        "synthesized_preflight_projection": True,
        "projected_refs_are_not_observed_artifacts": True,
        "mission_items_source": mission_items_source,
        "requested_wind_speed_mps": (
            wind_condition_artifacts.get("observed_environment_evidence", {})
            .get("observed", {})
            .get("requested_wind_speed_mps")
        ),
        "applied_max_wind_speed_mps": (
            wind_condition_artifacts.get("observed_environment_evidence", {})
            .get("observed", {})
            .get("applied_max_wind_speed_mps")
        ),
    }
    updated = store.update(
        task_id, status=status, artifacts=artifacts, metadata=metadata
    )
    if updated is None:
        raise PX4GazeboMissionDesignerSITLRunnerError(
            f"task {task_id} disappeared while attaching SITL execution receipt"
        )
    return {
        "task": updated,
        "delivery_mission_contract": artifacts["delivery_mission_contract"],
        "simulated_command_proposal": artifacts["simulated_command_proposal"],
        "simulated_command_approval": artifacts["simulated_command_approval"],
        "simulator_command_execution_preflight": artifacts[
            "simulator_command_execution_preflight"
        ],
        "px4_gazebo_sitl_mission_upload_receipt": artifacts[
            "px4_gazebo_sitl_mission_upload_receipt"
        ],
        "px4_gazebo_mission_designer_sitl_execution_result": artifacts[
            "px4_gazebo_mission_designer_sitl_execution_result"
        ],
        **wind_condition_artifacts,
        "summary": {
            "task_id": updated["task_id"],
            "task_status": updated["status"],
            "execution_request_id": execution_request.execution_request_id,
            "sitl_execution_opt_in_env": MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV,
            "sitl_execution_opted_in": allow_sitl_execution,
            "explicit_execution_approval": explicit_execution_approval,
            "upload_status": receipt.upload_status.value,
            "blocked_reasons": list(receipt.blocked_reasons),
            "target_endpoint": receipt.target_endpoint,
            "mission_item_count": receipt.mission_item_count,
            "mission_ack_observed": receipt.mission_ack_observed,
            "mission_ack_type": receipt.mission_ack_type,
            "sitl_execution_result_status": execution_result.result_status,
            "actual_sitl_mission_upload_observed": (
                execution_result.actual_sitl_mission_upload_observed
            ),
            "actual_sitl_flight_evidence_observed": (
                execution_result.actual_sitl_flight_evidence_observed
            ),
            "flight_evidence_ref": execution_result.flight_evidence_ref,
            "actual_takeoff_observed": execution_result.actual_takeoff_observed,
            "actual_dropoff_region_reached": (
                execution_result.actual_dropoff_region_reached
            ),
            "actual_land_observed": execution_result.actual_land_observed,
            "payload_release_observed": execution_result.payload_release_observed,
            "payload_release_verified": execution_result.payload_release_verified,
            "payload_release_event_ref": execution_result.payload_release_event_ref,
            "dropoff_verified": execution_result.dropoff_verified,
            "dropoff_verification_ref": execution_result.dropoff_verification_ref,
            "failure_reasons": list(execution_result.failure_reasons),
            "external_dispatch_performed": receipt.external_dispatch_performed,
            "mavlink_dispatch_performed": receipt.mavlink_dispatch_performed,
            "px4_mission_upload_performed": receipt.px4_mission_upload_performed,
            "hardware_target_allowed": receipt.hardware_target_allowed,
            "physical_execution_invoked": receipt.physical_execution_invoked,
            "gazebo_entity_mutation_performed": (
                receipt.gazebo_entity_mutation_performed
            ),
            "ros_dispatch_performed": receipt.ros_dispatch_performed,
            "actuator_execution_performed": receipt.actuator_execution_performed,
            "synthesized_preflight_projection": True,
            "projected_refs_are_not_observed_artifacts": True,
            "real_simulator_command_preflight_complete": False,
            "synthetic_success_allowed": execution_result.synthetic_success_allowed,
            "mission_items_source": mission_items_source,
            "requested_wind_speed_mps": (
                wind_condition_artifacts.get("observed_environment_evidence", {})
                .get("observed", {})
                .get("requested_wind_speed_mps")
            ),
            "applied_max_wind_speed_mps": (
                wind_condition_artifacts.get("observed_environment_evidence", {})
                .get("observed", {})
                .get("applied_max_wind_speed_mps")
            ),
            "payload_dropoff_success_requires_observed_facts": (
                execution_result.payload_dropoff_success_requires_observed_facts
            ),
        },
    }


__all__ = [
    "MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV",
    "MISSION_DESIGNER_SITL_TASK_KIND",
    "PX4_GAZEBO_MISSION_DESIGNER_SITL_EXECUTION_RESULT_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_DESIGNER_SITL_FLIGHT_EVIDENCE_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_DESIGNER_SITL_PAYLOAD_RELEASE_OBSERVATION_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_DESIGNER_SITL_DROPOFF_VERIFICATION_SCHEMA_VERSION",
    "PX4GazeboMissionDesignerSITLDropoffVerification",
    "PX4GazeboMissionDesignerSITLFlightEvidence",
    "PX4GazeboMissionDesignerSITLExecutionResult",
    "PX4GazeboMissionDesignerSITLPayloadReleaseObservation",
    "PX4GazeboMissionDesignerSITLRunnerError",
    "attach_px4_gazebo_mission_designer_sitl_dropoff_verification",
    "attach_px4_gazebo_mission_designer_sitl_flight_evidence",
    "attach_px4_gazebo_mission_designer_sitl_payload_release_observation",
    "build_px4_gazebo_mission_designer_sitl_dropoff_verification",
    "build_px4_gazebo_mission_designer_sitl_execution_result",
    "build_px4_gazebo_mission_designer_sitl_execution_result_with_flight_evidence",
    "build_px4_gazebo_mission_designer_sitl_flight_evidence",
    "build_px4_gazebo_mission_designer_sitl_payload_release_observation",
    "mission_designer_sitl_execution_opted_in",
    "run_px4_gazebo_mission_designer_sitl_execution",
]
