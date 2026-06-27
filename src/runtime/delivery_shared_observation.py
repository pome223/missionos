"""Intra-mission shared observation artifacts.

Vehicles may share observations, but shared observations are never command
authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_recovery_safety import raise_for_command_like_payload

DELIVERY_MISSION_SESSION_SCHEMA_VERSION = "delivery_mission_session.v1"
DELIVERY_VEHICLE_SESSION_SCHEMA_VERSION = "delivery_vehicle_session.v1"
MISSION_SHARED_OBSERVATION_SCHEMA_VERSION = "mission_shared_observation.v1"
DELIVERY_VEHICLE_DECISION_CONTEXT_SCHEMA_VERSION = (
    "delivery_vehicle_decision_context.v1"
)
INTRA_MISSION_SHARED_OBSERVATION_EPIC_EXIT_SCHEMA_VERSION = (
    "intra_mission_shared_observation_epic_exit.v1"
)


class DeliverySharedObservationError(RuntimeError):
    """Raised when a shared observation chain violates its authority boundary."""


class SharedObservationEventSource(str, Enum):
    VEHICLE_TELEMETRY = "vehicle_telemetry"
    PX4_GAZEBO_SITL_TELEMETRY = "px4_gazebo_sitl_telemetry"
    GAZEBO_POSE_SAMPLE = "gazebo_pose_sample"
    OPERATOR_OBSERVATION = "operator_observation"


class SharedObservationKind(str, Enum):
    VEHICLE_POSE = "vehicle_pose"
    BATTERY_STATUS = "battery_status"
    PAYLOAD_STATUS = "payload_status"
    ROUTE_PROGRESS = "route_progress"
    HAZARD_REPORT = "hazard_report"
    TELEMETRY_HEALTH = "telemetry_health"


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


def _as_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(
        sorted({str(item).strip() for item in (values or ()) if str(item).strip()})
    )


def _as_ref_tuple_preserve_duplicates(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(str(item).strip() for item in (values or ()) if str(item).strip())


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def _enum_value(value: Enum | str) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _mission_ref(session: "DeliveryMissionSession") -> str:
    return f"delivery_mission_session:{session.mission_session_id}"


def _vehicle_ref(session: "DeliveryVehicleSession") -> str:
    return f"delivery_vehicle_session:{session.vehicle_session_id}"


def _shared_observation_ref(observation: "MissionSharedObservation") -> str:
    return f"mission_shared_observation:{observation.observation_id}"


class DeliveryVehicleObservationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_ref: str
    observed_at: datetime
    event_source: SharedObservationEventSource
    observation_kind: SharedObservationKind
    observation_payload: dict[str, Any] = Field(default_factory=dict)
    advisory_source_only: Literal[True] = True
    command_authority_granted: Literal[False] = False
    dispatch_authority_granted: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_record(self) -> "DeliveryVehicleObservationRecord":
        if not self.observation_ref:
            raise DeliverySharedObservationError("vehicle_observation_ref_required")
        raise_for_command_like_payload(
            self.observation_payload,
            root="vehicle_observation.observation_payload",
            error_type=DeliverySharedObservationError,
            prefix="vehicle observation refused command-like payload",
        )
        return self


class DeliveryVehicleSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_VEHICLE_SESSION_SCHEMA_VERSION] = (
        DELIVERY_VEHICLE_SESSION_SCHEMA_VERSION
    )
    vehicle_session_id: str
    vehicle_id: str
    mission_session_ref: str
    telemetry_source_ref: str
    observation_refs: tuple[str, ...] = ()
    observation_records: tuple[DeliveryVehicleObservationRecord, ...] = ()
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    observation_only: Literal[True] = True
    command_authority_granted: Literal[False] = False
    dispatch_authority_granted: Literal[False] = False
    raw_mavlink_command_allowed: Literal[False] = False
    raw_ros_action_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    setpoint_stream_allowed: Literal[False] = False
    actuator_command_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False

    @field_validator("observation_refs", mode="before")
    @classmethod
    def _coerce_refs(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("observation_records", mode="before")
    @classmethod
    def _coerce_records(
        cls, value: Any
    ) -> tuple[DeliveryVehicleObservationRecord, ...]:
        return tuple(value or ())

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_vehicle_session(self) -> "DeliveryVehicleSession":
        if not self.vehicle_id.strip():
            raise DeliverySharedObservationError("vehicle_id_required")
        if not self.mission_session_ref.startswith("delivery_mission_session:"):
            raise DeliverySharedObservationError("vehicle_session_mission_ref_invalid")
        if not self.telemetry_source_ref:
            raise DeliverySharedObservationError(
                "vehicle_session_telemetry_ref_required"
            )
        raise_for_command_like_payload(
            self.metadata,
            root="vehicle_session.metadata",
            error_type=DeliverySharedObservationError,
            prefix="vehicle session refused command-like metadata",
        )
        record_refs = tuple(
            record.observation_ref for record in self.observation_records
        )
        if len(set(record_refs)) != len(record_refs):
            raise DeliverySharedObservationError("vehicle_observation_refs_duplicate")
        if tuple(sorted(record_refs)) != self.observation_refs:
            raise DeliverySharedObservationError(
                "vehicle_observation_refs_must_match_records"
            )
        return self


class DeliveryMissionSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_MISSION_SESSION_SCHEMA_VERSION] = (
        DELIVERY_MISSION_SESSION_SCHEMA_VERSION
    )
    mission_session_id: str
    vehicle_session_refs: tuple[str, ...]
    shared_observation_log_ref: str
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    multi_vehicle_observation_only: Literal[True] = True
    command_authority_granted: Literal[False] = False
    dispatch_authority_granted: Literal[False] = False
    raw_mavlink_command_allowed: Literal[False] = False
    raw_ros_action_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    setpoint_stream_allowed: Literal[False] = False
    actuator_command_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False

    @field_validator("vehicle_session_refs", mode="before")
    @classmethod
    def _coerce_vehicle_refs(cls, value: Any) -> tuple[str, ...]:
        return _as_ref_tuple_preserve_duplicates(value)

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_mission_session(self) -> "DeliveryMissionSession":
        if len(self.vehicle_session_refs) < 2:
            raise DeliverySharedObservationError(
                "mission_session_requires_at_least_two_vehicle_sessions"
            )
        if len(set(self.vehicle_session_refs)) != len(self.vehicle_session_refs):
            raise DeliverySharedObservationError(
                "mission_session_vehicle_refs_duplicate"
            )
        if not self.shared_observation_log_ref.startswith(
            "mission_shared_observation_log:"
        ):
            raise DeliverySharedObservationError("shared_observation_log_ref_invalid")
        raise_for_command_like_payload(
            self.metadata,
            root="mission_session.metadata",
            error_type=DeliverySharedObservationError,
            prefix="mission session refused command-like metadata",
        )
        return self


class MissionSharedObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[MISSION_SHARED_OBSERVATION_SCHEMA_VERSION] = (
        MISSION_SHARED_OBSERVATION_SCHEMA_VERSION
    )
    observation_id: str
    mission_session_ref: str
    source_vehicle_session_ref: str
    observed_at: datetime
    received_at: datetime
    event_source: SharedObservationEventSource
    observation_kind: SharedObservationKind
    observation_payload: dict[str, Any] = Field(default_factory=dict)
    source_observation_ref: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    advisory_only: Literal[True] = True
    append_only: Literal[True] = True
    shared_observation_is_command_authority: Literal[False] = False
    command_authority_granted: Literal[False] = False
    dispatch_authority_granted: Literal[False] = False
    raw_mavlink_command_allowed: Literal[False] = False
    raw_ros_action_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    setpoint_stream_allowed: Literal[False] = False
    actuator_command_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False

    @field_validator("observed_at", "received_at", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_shared_observation(self) -> "MissionSharedObservation":
        if not self.mission_session_ref.startswith("delivery_mission_session:"):
            raise DeliverySharedObservationError(
                "shared_observation_mission_ref_invalid"
            )
        if not self.source_vehicle_session_ref.startswith("delivery_vehicle_session:"):
            raise DeliverySharedObservationError(
                "shared_observation_source_vehicle_ref_invalid"
            )
        if not self.source_observation_ref:
            raise DeliverySharedObservationError(
                "shared_observation_source_ref_required"
            )
        if self.observed_at > self.received_at:
            raise DeliverySharedObservationError(
                "shared_observation_observed_after_received"
            )
        raise_for_command_like_payload(
            self.observation_payload,
            root="shared_observation.observation_payload",
            error_type=DeliverySharedObservationError,
            prefix="shared observation refused command-like payload",
        )
        raise_for_command_like_payload(
            self.metadata,
            root="shared_observation.metadata",
            error_type=DeliverySharedObservationError,
            prefix="shared observation refused command-like metadata",
        )
        return self


class SharedObservationValidationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mission_session_ref: str
    shared_observation_ref: str
    source_vehicle_session_ref: str
    source_observation_ref: str
    decision_at: datetime | None = None
    event_source_validated: Literal[True] = True
    observation_kind_validated: Literal[True] = True
    same_mission_validated: Literal[True] = True
    source_observation_ref_validated: Literal[True] = True
    payload_consistency_validated: Literal[True] = True
    temporal_causality_validated: Literal[True] = True
    stale_advisory_path_allowed: Literal[False] = False
    staleness_window_checked: bool = False
    max_observation_age_seconds: float | None = None
    advisory_only: Literal[True] = True
    shared_observation_is_command_authority: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False

    @field_validator("decision_at", mode="before")
    @classmethod
    def _coerce_decision_at(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


class DeliveryVehicleDecisionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_VEHICLE_DECISION_CONTEXT_SCHEMA_VERSION] = (
        DELIVERY_VEHICLE_DECISION_CONTEXT_SCHEMA_VERSION
    )
    decision_context_id: str
    mission_session_ref: str
    vehicle_session_ref: str
    decision_ref: str
    decision_at: datetime
    shared_observation_refs: tuple[str, ...] = ()
    ignored_shared_observation_refs: tuple[str, ...] = ()
    shared_observation_validation_evidence: tuple[
        SharedObservationValidationEvidence, ...
    ] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    shared_observation_decision_context_only: Literal[True] = True
    shared_observation_grants_command_authority: Literal[False] = False
    shared_observation_used_as_success_proof: Literal[False] = False
    shared_observation_used_as_scorecard_evidence: Literal[False] = False
    shared_observation_payload_copied_to_observed_facts: Literal[False] = False
    command_authority_granted: Literal[False] = False
    dispatch_authority_granted: Literal[False] = False
    raw_mavlink_command_allowed: Literal[False] = False
    raw_ros_action_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    setpoint_stream_allowed: Literal[False] = False
    actuator_command_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False

    @field_validator(
        "shared_observation_refs",
        "ignored_shared_observation_refs",
        mode="before",
    )
    @classmethod
    def _coerce_refs(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("decision_at", mode="before")
    @classmethod
    def _coerce_decision_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("shared_observation_validation_evidence", mode="before")
    @classmethod
    def _coerce_evidence(
        cls, value: Any
    ) -> tuple[SharedObservationValidationEvidence, ...]:
        return tuple(value or ())

    @model_validator(mode="after")
    def _validate_context(self) -> "DeliveryVehicleDecisionContext":
        if not self.mission_session_ref.startswith("delivery_mission_session:"):
            raise DeliverySharedObservationError("decision_context_mission_ref_invalid")
        if not self.vehicle_session_ref.startswith("delivery_vehicle_session:"):
            raise DeliverySharedObservationError(
                "decision_context_vehicle_session_ref_invalid"
            )
        if not self.decision_ref:
            raise DeliverySharedObservationError(
                "decision_context_decision_ref_required"
            )
        shared = set(self.shared_observation_refs)
        ignored = set(self.ignored_shared_observation_refs)
        if shared & ignored:
            raise DeliverySharedObservationError(
                "shared_observation_ref_cannot_be_used_and_ignored"
            )
        evidence_refs = {
            evidence.shared_observation_ref
            for evidence in self.shared_observation_validation_evidence
        }
        if evidence_refs != shared:
            raise DeliverySharedObservationError(
                "decision_context_shared_refs_must_match_validation_evidence"
            )
        for evidence in self.shared_observation_validation_evidence:
            if evidence.mission_session_ref != self.mission_session_ref:
                raise DeliverySharedObservationError(
                    "decision_context_validation_mission_ref_mismatch"
                )
            if evidence.decision_at != self.decision_at:
                raise DeliverySharedObservationError(
                    "decision_context_validation_decision_at_mismatch"
                )
        raise_for_command_like_payload(
            self.metadata,
            root="decision_context.metadata",
            error_type=DeliverySharedObservationError,
            prefix="vehicle decision context refused command-like metadata",
        )
        return self


class IntraMissionSharedObservationEpicExitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        INTRA_MISSION_SHARED_OBSERVATION_EPIC_EXIT_SCHEMA_VERSION
    ] = INTRA_MISSION_SHARED_OBSERVATION_EPIC_EXIT_SCHEMA_VERSION
    result_id: str
    mission_session_ref: str
    vehicle_session_refs: tuple[str, ...]
    source_vehicle_session_ref: str
    consuming_vehicle_session_ref: str
    source_observation_ref: str
    shared_observation_ref: str
    decision_context_ref: str
    decision_ref: str
    decision_at: datetime
    cited_shared_observation_refs: tuple[str, ...]
    validator_evidence: SharedObservationValidationEvidence
    completed_at: datetime
    epic_invariant_shared_observations_never_command_authority: Literal[True] = True
    at_least_two_vehicle_sessions: Literal[True] = True
    source_observation_published: Literal[True] = True
    shared_observation_appended: Literal[True] = True
    consuming_vehicle_cited_shared_observation: Literal[True] = True
    refs_attested: Literal[True] = True
    same_mission_membership_validated: Literal[True] = True
    source_observation_ref_validated: Literal[True] = True
    payload_consistency_validated: Literal[True] = True
    temporal_causality_validated: Literal[True] = True
    future_observation_negative_failed_closed: Literal[True] = True
    stale_observation_negative_failed_closed: Literal[True] = True
    advisory_only: Literal[True] = True
    shared_observation_is_command_authority: Literal[False] = False
    command_authority_granted: Literal[False] = False
    dispatch_authority_granted: Literal[False] = False
    raw_mavlink_command_allowed: Literal[False] = False
    raw_ros_action_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    setpoint_stream_allowed: Literal[False] = False
    actuator_command_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False

    @field_validator(
        "vehicle_session_refs", "cited_shared_observation_refs", mode="before"
    )
    @classmethod
    def _coerce_refs(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("decision_at", mode="before")
    @classmethod
    def _coerce_decision_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("completed_at", mode="before")
    @classmethod
    def _coerce_completed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_exit(self) -> "IntraMissionSharedObservationEpicExitResult":
        if len(self.vehicle_session_refs) < 2:
            raise DeliverySharedObservationError(
                "shared_observation_epic_exit_requires_two_vehicle_sessions"
            )
        if self.source_vehicle_session_ref not in self.vehicle_session_refs:
            raise DeliverySharedObservationError(
                "shared_observation_epic_exit_source_vehicle_not_in_mission"
            )
        if self.consuming_vehicle_session_ref not in self.vehicle_session_refs:
            raise DeliverySharedObservationError(
                "shared_observation_epic_exit_consuming_vehicle_not_in_mission"
            )
        if self.source_vehicle_session_ref == self.consuming_vehicle_session_ref:
            raise DeliverySharedObservationError(
                "shared_observation_epic_exit_requires_distinct_vehicles"
            )
        if not self.decision_ref:
            raise DeliverySharedObservationError(
                "shared_observation_epic_exit_decision_ref_required"
            )
        if self.shared_observation_ref not in self.cited_shared_observation_refs:
            raise DeliverySharedObservationError(
                "shared_observation_epic_exit_missing_cited_shared_ref"
            )
        if self.validator_evidence.mission_session_ref != self.mission_session_ref:
            raise DeliverySharedObservationError(
                "shared_observation_epic_exit_validator_mission_ref_mismatch"
            )
        if (
            self.validator_evidence.source_vehicle_session_ref
            != self.source_vehicle_session_ref
        ):
            raise DeliverySharedObservationError(
                "shared_observation_epic_exit_validator_source_vehicle_mismatch"
            )
        if (
            self.validator_evidence.shared_observation_ref
            != self.shared_observation_ref
        ):
            raise DeliverySharedObservationError(
                "shared_observation_epic_exit_validator_ref_mismatch"
            )
        if (
            self.validator_evidence.source_observation_ref
            != self.source_observation_ref
        ):
            raise DeliverySharedObservationError(
                "shared_observation_epic_exit_source_observation_ref_mismatch"
            )
        if self.validator_evidence.decision_at is None:
            raise DeliverySharedObservationError(
                "shared_observation_epic_exit_validator_decision_at_required"
            )
        if self.validator_evidence.decision_at != self.decision_at:
            raise DeliverySharedObservationError(
                "shared_observation_epic_exit_validator_decision_at_mismatch"
            )
        return self


def build_delivery_vehicle_observation_record(
    *,
    observation_ref: str,
    event_source: SharedObservationEventSource | str,
    observation_kind: SharedObservationKind | str,
    observation_payload: Mapping[str, Any],
    observed_at: datetime | None = None,
) -> DeliveryVehicleObservationRecord:
    return DeliveryVehicleObservationRecord(
        observation_ref=observation_ref,
        observed_at=_utc(observed_at),
        event_source=SharedObservationEventSource(_enum_value(event_source)),
        observation_kind=SharedObservationKind(_enum_value(observation_kind)),
        observation_payload=dict(observation_payload),
    )


def build_delivery_vehicle_session(
    *,
    vehicle_id: str,
    mission_session_ref: str,
    telemetry_source_ref: str,
    observation_records: Sequence[DeliveryVehicleObservationRecord | Mapping[str, Any]],
    created_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> DeliveryVehicleSession:
    records = tuple(
        (
            item
            if isinstance(item, DeliveryVehicleObservationRecord)
            else DeliveryVehicleObservationRecord.model_validate(item)
        )
        for item in observation_records
    )
    created = _utc(created_at)
    payload = {
        "vehicle_id": vehicle_id,
        "mission_session_ref": mission_session_ref,
        "telemetry_source_ref": telemetry_source_ref,
        "observation_refs": tuple(record.observation_ref for record in records),
        "created_at": created.isoformat(),
    }
    return DeliveryVehicleSession(
        vehicle_session_id=_stable_id("delivery_vehicle_session", payload),
        vehicle_id=vehicle_id,
        mission_session_ref=mission_session_ref,
        telemetry_source_ref=telemetry_source_ref,
        observation_refs=tuple(record.observation_ref for record in records),
        observation_records=records,
        created_at=created,
        metadata=dict(metadata or {}),
    )


def build_delivery_mission_session(
    *,
    vehicle_sessions: Sequence[DeliveryVehicleSession | Mapping[str, Any]],
    shared_observation_log_ref: str,
    created_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> DeliveryMissionSession:
    sessions = tuple(
        (
            item
            if isinstance(item, DeliveryVehicleSession)
            else DeliveryVehicleSession.model_validate(item)
        )
        for item in vehicle_sessions
    )
    vehicle_refs = tuple(_vehicle_ref(session) for session in sessions)
    vehicle_ids = tuple(session.vehicle_id for session in sessions)
    if len(set(vehicle_ids)) != len(vehicle_ids):
        raise DeliverySharedObservationError("mission_session_vehicle_ids_duplicate")
    mission_ref = sessions[0].mission_session_ref if sessions else ""
    for session in sessions:
        if session.mission_session_ref != mission_ref:
            raise DeliverySharedObservationError("vehicle_sessions_cross_mission")
    created = _utc(created_at)
    payload = {
        "vehicle_session_refs": vehicle_refs,
        "shared_observation_log_ref": shared_observation_log_ref,
        "created_at": created.isoformat(),
    }
    mission_id = mission_ref.split(":", 1)[1] if mission_ref else ""
    if not mission_id:
        mission_id = _stable_id("delivery_mission_session", payload)
    return DeliveryMissionSession(
        mission_session_id=mission_id,
        vehicle_session_refs=vehicle_refs,
        shared_observation_log_ref=shared_observation_log_ref,
        created_at=created,
        metadata=dict(metadata or {}),
    )


def build_mission_shared_observation(
    *,
    mission_session_ref: str,
    source_vehicle_session_ref: str,
    source_observation_ref: str,
    event_source: SharedObservationEventSource | str,
    observation_kind: SharedObservationKind | str,
    observation_payload: Mapping[str, Any],
    observed_at: datetime,
    received_at: datetime,
    metadata: Mapping[str, Any] | None = None,
) -> MissionSharedObservation:
    payload = {
        "mission_session_ref": mission_session_ref,
        "source_vehicle_session_ref": source_vehicle_session_ref,
        "source_observation_ref": source_observation_ref,
        "event_source": _enum_value(event_source),
        "observation_kind": _enum_value(observation_kind),
        "observation_payload": _jsonable(observation_payload),
        "observed_at": _utc(observed_at).isoformat(),
        "received_at": _utc(received_at).isoformat(),
    }
    return MissionSharedObservation(
        observation_id=_stable_id("mission_shared_observation", payload),
        mission_session_ref=mission_session_ref,
        source_vehicle_session_ref=source_vehicle_session_ref,
        observed_at=observed_at,
        received_at=received_at,
        event_source=SharedObservationEventSource(_enum_value(event_source)),
        observation_kind=SharedObservationKind(_enum_value(observation_kind)),
        observation_payload=dict(observation_payload),
        source_observation_ref=source_observation_ref,
        metadata=dict(metadata or {}),
    )


def _find_vehicle_session(
    sessions: Sequence[DeliveryVehicleSession | Mapping[str, Any]],
    ref: str,
) -> DeliveryVehicleSession:
    for raw in sessions:
        session = (
            raw
            if isinstance(raw, DeliveryVehicleSession)
            else DeliveryVehicleSession.model_validate(raw)
        )
        if _vehicle_ref(session) == ref:
            return session
    raise DeliverySharedObservationError(f"vehicle_session_ref_not_found:{ref}")


def _find_observation_record(
    session: DeliveryVehicleSession,
    ref: str,
) -> DeliveryVehicleObservationRecord:
    for record in session.observation_records:
        if record.observation_ref == ref:
            return record
    raise DeliverySharedObservationError(f"source_observation_ref_not_found:{ref}")


def _validate_payload_consistency(
    *,
    shared_payload: Mapping[str, Any],
    source_payload: Mapping[str, Any],
) -> None:
    for key, shared_value in shared_payload.items():
        if key not in source_payload:
            raise DeliverySharedObservationError(
                f"shared_observation_payload_key_not_in_source:{key}"
            )
        if _jsonable(source_payload[key]) != _jsonable(shared_value):
            raise DeliverySharedObservationError(
                f"shared_observation_payload_contradicts_source:{key}"
            )


def validate_shared_observation_refs(
    *,
    mission_session: DeliveryMissionSession | Mapping[str, Any],
    vehicle_sessions: Sequence[DeliveryVehicleSession | Mapping[str, Any]],
    shared_observation: MissionSharedObservation | Mapping[str, Any],
    decision_at: datetime | None = None,
    decision_shared_observation_refs: Sequence[str] | None = None,
    max_observation_age_seconds: float | None = None,
) -> SharedObservationValidationEvidence:
    mission = (
        mission_session
        if isinstance(mission_session, DeliveryMissionSession)
        else DeliveryMissionSession.model_validate(mission_session)
    )
    observation = (
        shared_observation
        if isinstance(shared_observation, MissionSharedObservation)
        else MissionSharedObservation.model_validate(shared_observation)
    )
    mission_ref = _mission_ref(mission)
    if observation.mission_session_ref != mission_ref:
        raise DeliverySharedObservationError("shared_observation_cross_mission_ref")
    if observation.source_vehicle_session_ref not in mission.vehicle_session_refs:
        raise DeliverySharedObservationError("source_vehicle_session_not_in_mission")
    source_session = _find_vehicle_session(
        vehicle_sessions,
        observation.source_vehicle_session_ref,
    )
    if source_session.mission_session_ref != mission_ref:
        raise DeliverySharedObservationError("source_vehicle_session_cross_mission")
    source_record = _find_observation_record(
        source_session,
        observation.source_observation_ref,
    )
    if source_record.observed_at != observation.observed_at:
        raise DeliverySharedObservationError("shared_observation_observed_at_mismatch")
    if source_record.event_source is not observation.event_source:
        raise DeliverySharedObservationError("shared_observation_event_source_mismatch")
    if source_record.observation_kind is not observation.observation_kind:
        raise DeliverySharedObservationError("shared_observation_kind_mismatch")
    _validate_payload_consistency(
        shared_payload=observation.observation_payload,
        source_payload=source_record.observation_payload,
    )
    resolved_decision_at = _utc(decision_at) if decision_at is not None else None
    if max_observation_age_seconds is not None and max_observation_age_seconds < 0:
        raise DeliverySharedObservationError(
            "shared_observation_max_age_must_be_non_negative"
        )
    if resolved_decision_at is not None:
        if observation.received_at > resolved_decision_at:
            raise DeliverySharedObservationError(
                "shared_observation_received_after_decision"
            )
        if max_observation_age_seconds is not None:
            age_seconds = (
                resolved_decision_at - observation.observed_at
            ).total_seconds()
            if age_seconds > max_observation_age_seconds:
                raise DeliverySharedObservationError(
                    "shared_observation_stale_for_decision"
                )
        cited_refs = _as_tuple(decision_shared_observation_refs)
        if _shared_observation_ref(observation) not in cited_refs:
            raise DeliverySharedObservationError(
                "decision_missing_shared_observation_ref"
            )
    return SharedObservationValidationEvidence(
        mission_session_ref=mission_ref,
        shared_observation_ref=_shared_observation_ref(observation),
        source_vehicle_session_ref=observation.source_vehicle_session_ref,
        source_observation_ref=observation.source_observation_ref,
        decision_at=resolved_decision_at,
        staleness_window_checked=max_observation_age_seconds is not None,
        max_observation_age_seconds=max_observation_age_seconds,
    )


def build_delivery_vehicle_decision_context(
    *,
    mission_session: DeliveryMissionSession | Mapping[str, Any],
    vehicle_session: DeliveryVehicleSession | Mapping[str, Any],
    vehicle_sessions: Sequence[DeliveryVehicleSession | Mapping[str, Any]],
    decision_ref: str,
    shared_observations: Sequence[MissionSharedObservation | Mapping[str, Any]],
    ignored_shared_observation_refs: Sequence[str] | None = None,
    decision_at: datetime,
    max_observation_age_seconds: float | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> DeliveryVehicleDecisionContext:
    mission = (
        mission_session
        if isinstance(mission_session, DeliveryMissionSession)
        else DeliveryMissionSession.model_validate(mission_session)
    )
    target_vehicle = (
        vehicle_session
        if isinstance(vehicle_session, DeliveryVehicleSession)
        else DeliveryVehicleSession.model_validate(vehicle_session)
    )
    mission_ref = _mission_ref(mission)
    vehicle_ref = _vehicle_ref(target_vehicle)
    if target_vehicle.mission_session_ref != mission_ref:
        raise DeliverySharedObservationError("decision_context_vehicle_cross_mission")
    if vehicle_ref not in mission.vehicle_session_refs:
        raise DeliverySharedObservationError("decision_context_vehicle_not_in_mission")
    resolved_observations = tuple(
        (
            item
            if isinstance(item, MissionSharedObservation)
            else MissionSharedObservation.model_validate(item)
        )
        for item in shared_observations
    )
    shared_refs = tuple(_shared_observation_ref(item) for item in resolved_observations)
    evidence = tuple(
        validate_shared_observation_refs(
            mission_session=mission,
            vehicle_sessions=vehicle_sessions,
            shared_observation=item,
            decision_at=decision_at,
            decision_shared_observation_refs=shared_refs,
            max_observation_age_seconds=max_observation_age_seconds,
        )
        for item in resolved_observations
    )
    resolved_decision_at = _utc(decision_at)
    ignored_refs = _as_tuple(ignored_shared_observation_refs)
    payload = {
        "mission_session_ref": mission_ref,
        "vehicle_session_ref": vehicle_ref,
        "decision_ref": decision_ref,
        "shared_observation_refs": shared_refs,
        "ignored_shared_observation_refs": ignored_refs,
        "decision_at": resolved_decision_at.isoformat(),
    }
    return DeliveryVehicleDecisionContext(
        decision_context_id=_stable_id("delivery_vehicle_decision_context", payload),
        mission_session_ref=mission_ref,
        vehicle_session_ref=vehicle_ref,
        decision_ref=decision_ref,
        decision_at=resolved_decision_at,
        shared_observation_refs=shared_refs,
        ignored_shared_observation_refs=ignored_refs,
        shared_observation_validation_evidence=evidence,
        metadata=dict(metadata or {}),
    )


def build_intra_mission_shared_observation_epic_exit_result(
    *,
    mission_session: DeliveryMissionSession | Mapping[str, Any],
    source_vehicle_session: DeliveryVehicleSession | Mapping[str, Any],
    consuming_vehicle_session: DeliveryVehicleSession | Mapping[str, Any],
    shared_observation: MissionSharedObservation | Mapping[str, Any],
    decision_context: DeliveryVehicleDecisionContext | Mapping[str, Any],
    future_observation_negative_failed_closed: bool,
    stale_observation_negative_failed_closed: bool,
    completed_at: datetime | None = None,
) -> IntraMissionSharedObservationEpicExitResult:
    mission = (
        mission_session
        if isinstance(mission_session, DeliveryMissionSession)
        else DeliveryMissionSession.model_validate(mission_session)
    )
    source_session = (
        source_vehicle_session
        if isinstance(source_vehicle_session, DeliveryVehicleSession)
        else DeliveryVehicleSession.model_validate(source_vehicle_session)
    )
    consuming_session = (
        consuming_vehicle_session
        if isinstance(consuming_vehicle_session, DeliveryVehicleSession)
        else DeliveryVehicleSession.model_validate(consuming_vehicle_session)
    )
    observation = (
        shared_observation
        if isinstance(shared_observation, MissionSharedObservation)
        else MissionSharedObservation.model_validate(shared_observation)
    )
    context = (
        decision_context
        if isinstance(decision_context, DeliveryVehicleDecisionContext)
        else DeliveryVehicleDecisionContext.model_validate(decision_context)
    )
    if not future_observation_negative_failed_closed:
        raise DeliverySharedObservationError(
            "shared_observation_epic_exit_future_negative_missing"
        )
    if not stale_observation_negative_failed_closed:
        raise DeliverySharedObservationError(
            "shared_observation_epic_exit_stale_negative_missing"
        )
    if context.mission_session_ref != _mission_ref(mission):
        raise DeliverySharedObservationError(
            "shared_observation_epic_exit_context_mission_mismatch"
        )
    if context.vehicle_session_ref != _vehicle_ref(consuming_session):
        raise DeliverySharedObservationError(
            "shared_observation_epic_exit_context_vehicle_mismatch"
        )
    shared_ref = _shared_observation_ref(observation)
    evidence = context.shared_observation_validation_evidence
    if len(evidence) != 1:
        raise DeliverySharedObservationError(
            "shared_observation_epic_exit_requires_one_validation_evidence"
        )
    completed = _utc(completed_at)
    payload = {
        "mission_session_ref": _mission_ref(mission),
        "vehicle_session_refs": mission.vehicle_session_refs,
        "source_observation_ref": observation.source_observation_ref,
        "shared_observation_ref": shared_ref,
        "decision_context_ref": (
            f"delivery_vehicle_decision_context:{context.decision_context_id}"
        ),
        "decision_ref": context.decision_ref,
        "decision_at": context.decision_at.isoformat(),
        "completed_at": completed.isoformat(),
    }
    return IntraMissionSharedObservationEpicExitResult(
        result_id=_stable_id(
            "intra_mission_shared_observation_epic_exit",
            payload,
        ),
        mission_session_ref=_mission_ref(mission),
        vehicle_session_refs=mission.vehicle_session_refs,
        source_vehicle_session_ref=_vehicle_ref(source_session),
        consuming_vehicle_session_ref=_vehicle_ref(consuming_session),
        source_observation_ref=observation.source_observation_ref,
        shared_observation_ref=shared_ref,
        decision_context_ref=(
            f"delivery_vehicle_decision_context:{context.decision_context_id}"
        ),
        decision_ref=context.decision_ref,
        decision_at=context.decision_at,
        cited_shared_observation_refs=context.shared_observation_refs,
        validator_evidence=evidence[0],
        completed_at=completed,
        future_observation_negative_failed_closed=True,
        stale_observation_negative_failed_closed=True,
    )


__all__ = [
    "DELIVERY_MISSION_SESSION_SCHEMA_VERSION",
    "DELIVERY_VEHICLE_SESSION_SCHEMA_VERSION",
    "DELIVERY_VEHICLE_DECISION_CONTEXT_SCHEMA_VERSION",
    "INTRA_MISSION_SHARED_OBSERVATION_EPIC_EXIT_SCHEMA_VERSION",
    "MISSION_SHARED_OBSERVATION_SCHEMA_VERSION",
    "DeliveryVehicleDecisionContext",
    "DeliveryMissionSession",
    "DeliverySharedObservationError",
    "DeliveryVehicleObservationRecord",
    "DeliveryVehicleSession",
    "IntraMissionSharedObservationEpicExitResult",
    "MissionSharedObservation",
    "SharedObservationEventSource",
    "SharedObservationKind",
    "SharedObservationValidationEvidence",
    "build_delivery_mission_session",
    "build_delivery_vehicle_decision_context",
    "build_delivery_vehicle_observation_record",
    "build_delivery_vehicle_session",
    "build_mission_shared_observation",
    "build_intra_mission_shared_observation_epic_exit_result",
    "validate_shared_observation_refs",
]
