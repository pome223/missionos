"""Record real PX4/Gazebo SITL delivery recovery artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_fault_event import (
    DeliveryFaultCategory,
    DeliveryFaultSeverity,
)
from src.runtime.delivery_mission_contract import DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
from src.runtime.delivery_recovery_decision import (
    DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION,
    DeliveryRecoveryAction,
)
from src.runtime.delivery_recovery_outcome import DeliveryRecoveryOutcomeCategory
from src.runtime.delivery_recovery_request import DeliveryRecoveryRequestKind
from src.runtime.delivery_recovery_safety import raise_for_command_like_payload
from src.runtime.operator_minimal_delivery_simulation import (
    OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION,
)
from src.runtime.task_store import TaskStore, get_task_store

DELIVERY_FAULT_EVENT_REAL_SITL_SCHEMA_VERSION = "delivery_fault_event_real_sitl.v1"
DELIVERY_RECOVERY_REQUEST_REAL_SITL_SCHEMA_VERSION = (
    "delivery_recovery_request_real_sitl.v1"
)
DELIVERY_RECOVERY_RUN_REAL_SITL_SCHEMA_VERSION = "delivery_recovery_run_real_sitl.v1"
DELIVERY_RECOVERY_OUTCOME_REAL_SITL_SCHEMA_VERSION = (
    "delivery_recovery_outcome_real_sitl.v1"
)
DELIVERY_RECOVERY_LOOP_REAL_SITL_SCHEMA_VERSION = "delivery_recovery_loop_real_sitl.v1"
DELIVERY_FAULT_EVENT_REAL_SITL_PAYLOAD_RELEASE_MISSING_SCHEMA_VERSION = (
    "delivery_fault_event_real_sitl_payload_release_missing.v1"
)
DELIVERY_RECOVERY_REQUEST_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION = (
    "delivery_recovery_request_real_sitl_retry_dropoff.v1"
)
DELIVERY_RECOVERY_RUN_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION = (
    "delivery_recovery_run_real_sitl_retry_dropoff.v1"
)
DELIVERY_RECOVERY_OUTCOME_REAL_SITL_RETRY_RECOVERED_SCHEMA_VERSION = (
    "delivery_recovery_outcome_real_sitl_retry_recovered.v1"
)
DELIVERY_RECOVERY_LOOP_REAL_SITL_RETRY_RECOVERED_SCHEMA_VERSION = (
    "delivery_recovery_loop_real_sitl_retry_recovered.v1"
)

REAL_SITL_EVIDENCE_SOURCE = "actual_px4_gazebo_sitl_run"
SAFE_LANDING_FACT_SCHEMA = "delivery_recovery_real_sitl_safe_landing_facts.v1"
RETRY_DROPOFF_FACT_SCHEMA = "delivery_recovery_real_sitl_retry_dropoff_facts.v1"


class DeliveryRecoveryRealSITLError(RuntimeError):
    """Raised when a real-SITL recovery chain is inconsistent."""


class DeliveryRecoveryRealSITLRunStatus(str, Enum):
    EXECUTED_IN_REAL_SITL = "executed_in_real_sitl"
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


def _as_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(
        sorted({str(item).strip() for item in (values or ()) if str(item).strip()})
    )


def _require_real_sitl(
    *,
    artifact_name: str,
    executed_against_real_sitl: bool,
    recovery_chain_evidence_source: str,
) -> None:
    if executed_against_real_sitl is not True:
        raise DeliveryRecoveryRealSITLError(
            f"{artifact_name} requires executed_against_real_sitl=True"
        )
    if recovery_chain_evidence_source != REAL_SITL_EVIDENCE_SOURCE:
        raise DeliveryRecoveryRealSITLError(
            f"{artifact_name} requires actual PX4/Gazebo SITL evidence source"
        )


class DeliveryFaultEventRealSITL(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_FAULT_EVENT_REAL_SITL_SCHEMA_VERSION] = (
        DELIVERY_FAULT_EVENT_REAL_SITL_SCHEMA_VERSION
    )
    fault_event_id: str
    fault_category: Literal[DeliveryFaultCategory.BATTERY_RESERVE_VIOLATION]
    severity: Literal[DeliveryFaultSeverity.BLOCKING]
    observed_at: datetime
    sitl_session_ref: str
    source_artifact_refs: tuple[str, ...] = ()
    telemetry_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    bounded_run_ref: str = ""
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    executed_against_real_sitl: Literal[True] = True
    recovery_chain_evidence_source: Literal[REAL_SITL_EVIDENCE_SOURCE] = (
        REAL_SITL_EVIDENCE_SOURCE
    )
    evidence_only: Literal[True] = True
    command_sent: Literal[False] = False
    mission_upload_performed: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False

    @field_validator(
        "source_artifact_refs",
        "telemetry_refs",
        "evidence_refs",
        "blocked_reasons",
        "warning_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_event(self) -> "DeliveryFaultEventRealSITL":
        raise_for_command_like_payload(
            self.metadata,
            root="fault_event_real_sitl.metadata",
            error_type=DeliveryRecoveryRealSITLError,
            prefix="real-SITL fault event refused command-like metadata",
        )
        if not self.evidence_refs:
            raise DeliveryRecoveryRealSITLError(
                "real-SITL fault event requires evidence refs"
            )
        return self


class DeliveryRecoveryRequestRealSITL(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_RECOVERY_REQUEST_REAL_SITL_SCHEMA_VERSION] = (
        DELIVERY_RECOVERY_REQUEST_REAL_SITL_SCHEMA_VERSION
    )
    request_id: str
    fault_event_ref: str
    mission_contract_ref: str
    recovery_decision_ref: str
    operator_status_ref: str
    sitl_session_ref: str
    request_kind: Literal[DeliveryRecoveryRequestKind.ABORT_AND_LAND_SIMULATION]
    recovery_decision: Literal[DeliveryRecoveryAction.ABORT_RECOMMENDED]
    request_status: Literal["ready"]
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    delivery_recovery_decision_schema_version: Literal[
        DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    delivery_fault_event_schema_version: Literal[
        DELIVERY_FAULT_EVENT_REAL_SITL_SCHEMA_VERSION
    ] = DELIVERY_FAULT_EVENT_REAL_SITL_SCHEMA_VERSION
    operator_minimal_delivery_simulation_status_schema_version: Literal[
        OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION
    ] = OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION
    executed_against_real_sitl: Literal[True] = True
    recovery_chain_evidence_source: Literal[REAL_SITL_EVIDENCE_SOURCE] = (
        REAL_SITL_EVIDENCE_SOURCE
    )
    simulation_only: Literal[True] = True
    request_only: Literal[True] = True
    sitl_only: Literal[True] = True
    bounded: Literal[True] = True
    command_payload_allowed: Literal[False] = False
    raw_mavlink_command_allowed: Literal[False] = False
    raw_ros_action_allowed: Literal[False] = False
    setpoint_stream_allowed: Literal[False] = False
    actuator_command_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False

    @field_validator("blocked_reasons", "warning_reasons", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @model_validator(mode="after")
    def _validate_request(self) -> "DeliveryRecoveryRequestRealSITL":
        raise_for_command_like_payload(
            self.metadata,
            root="request_real_sitl.metadata",
            error_type=DeliveryRecoveryRealSITLError,
            prefix="real-SITL request refused command-like metadata",
        )
        if self.blocked_reasons:
            raise DeliveryRecoveryRealSITLError(
                "ready real-SITL recovery request cannot be blocked"
            )
        return self


class DeliveryRecoveryRunRealSITL(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_RECOVERY_RUN_REAL_SITL_SCHEMA_VERSION] = (
        DELIVERY_RECOVERY_RUN_REAL_SITL_SCHEMA_VERSION
    )
    recovery_run_id: str
    recovery_request_ref: str
    fault_event_ref: str
    sitl_session_ref: str
    source_sitl_summary_ref: str
    status: Literal[DeliveryRecoveryRealSITLRunStatus.EXECUTED_IN_REAL_SITL]
    started_at: datetime
    finished_at: datetime
    observed_facts: dict[str, Any] = Field(default_factory=dict)
    observed_fact_refs: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    delivery_recovery_request_schema_version: Literal[
        DELIVERY_RECOVERY_REQUEST_REAL_SITL_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_REQUEST_REAL_SITL_SCHEMA_VERSION
    delivery_fault_event_schema_version: Literal[
        DELIVERY_FAULT_EVENT_REAL_SITL_SCHEMA_VERSION
    ] = DELIVERY_FAULT_EVENT_REAL_SITL_SCHEMA_VERSION
    executed_against_real_sitl: Literal[True] = True
    recovery_chain_evidence_source: Literal[REAL_SITL_EVIDENCE_SOURCE] = (
        REAL_SITL_EVIDENCE_SOURCE
    )
    real_sitl_execution_claimed: Literal[True] = True
    sitl_only: Literal[True] = True
    bounded: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_observed: Literal[True] = True
    approval_free_stronger_execution_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_gazebo_container_started: Literal[True] = True
    safe_landing_observed: Literal[True] = True
    mission_terminated_safely: Literal[True] = True
    vehicle_disarmed_or_landed: Literal[True] = True

    @field_validator(
        "observed_fact_refs",
        "blocked_reasons",
        "warning_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("started_at", "finished_at", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_run(self) -> "DeliveryRecoveryRunRealSITL":
        raise_for_command_like_payload(
            self.metadata,
            root="run_real_sitl.metadata",
            error_type=DeliveryRecoveryRealSITLError,
            prefix="real-SITL recovery run refused command-like metadata",
        )
        if self.finished_at < self.started_at:
            raise DeliveryRecoveryRealSITLError(
                "real-SITL recovery run finished before it started"
            )
        if self.blocked_reasons:
            raise DeliveryRecoveryRealSITLError(
                "executed real-SITL recovery run cannot be blocked"
            )
        return self


class DeliveryRecoveryOutcomeRealSITL(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_RECOVERY_OUTCOME_REAL_SITL_SCHEMA_VERSION] = (
        DELIVERY_RECOVERY_OUTCOME_REAL_SITL_SCHEMA_VERSION
    )
    outcome_id: str
    recovery_request_ref: str
    recovery_run_ref: str
    fault_event_ref: str
    outcome_category: Literal[DeliveryRecoveryOutcomeCategory.ABORTED_SAFELY]
    observed_facts: dict[str, Any] = Field(default_factory=dict)
    observed_fact_refs: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    verified_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    delivery_recovery_request_schema_version: Literal[
        DELIVERY_RECOVERY_REQUEST_REAL_SITL_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_REQUEST_REAL_SITL_SCHEMA_VERSION
    delivery_recovery_run_schema_version: Literal[
        DELIVERY_RECOVERY_RUN_REAL_SITL_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_RUN_REAL_SITL_SCHEMA_VERSION
    delivery_fault_event_schema_version: Literal[
        DELIVERY_FAULT_EVENT_REAL_SITL_SCHEMA_VERSION
    ] = DELIVERY_FAULT_EVENT_REAL_SITL_SCHEMA_VERSION
    executed_against_real_sitl: Literal[True] = True
    recovery_chain_evidence_source: Literal[REAL_SITL_EVIDENCE_SOURCE] = (
        REAL_SITL_EVIDENCE_SOURCE
    )
    real_sitl_execution_claimed: Literal[True] = True
    observed_facts_only: Literal[True] = True
    synthetic_success_allowed: Literal[False] = False
    command_sent_by_verifier: Literal[False] = False
    external_dispatch_performed_by_verifier: Literal[False] = False
    mavlink_dispatch_performed_by_verifier: Literal[False] = False
    px4_mission_upload_performed_by_verifier: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    safe_landing_predicate_mode: Literal[
        "safe_landing_and_mission_terminated_and_vehicle_disarmed_or_landed"
    ] = "safe_landing_and_mission_terminated_and_vehicle_disarmed_or_landed"

    @field_validator(
        "observed_fact_refs",
        "blocked_reasons",
        "warning_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("verified_at", mode="before")
    @classmethod
    def _coerce_verified_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_outcome(self) -> "DeliveryRecoveryOutcomeRealSITL":
        raise_for_command_like_payload(
            self.metadata,
            root="outcome_real_sitl.metadata",
            error_type=DeliveryRecoveryRealSITLError,
            prefix="real-SITL outcome refused command-like metadata",
        )
        if self.blocked_reasons:
            raise DeliveryRecoveryRealSITLError(
                "aborted-safely real-SITL outcome cannot be blocked"
            )
        required = {
            "safe_landing_observed": True,
            "mission_terminated_safely": True,
            "vehicle_disarmed_or_landed": True,
        }
        for key, expected in required.items():
            if self.observed_facts.get(key) is not expected:
                raise DeliveryRecoveryRealSITLError(
                    f"real-SITL outcome missing observed fact: {key}"
                )
        return self


class DeliveryRecoveryLoopRealSITL(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_RECOVERY_LOOP_REAL_SITL_SCHEMA_VERSION] = (
        DELIVERY_RECOVERY_LOOP_REAL_SITL_SCHEMA_VERSION
    )
    loop_id: str
    fault_event_ref: str
    recovery_request_ref: str
    recovery_run_ref: str
    recovery_outcome_ref: str
    sitl_session_ref: str
    source_sitl_summary_ref: str
    completed_at: datetime
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    delivery_fault_event_schema_version: Literal[
        DELIVERY_FAULT_EVENT_REAL_SITL_SCHEMA_VERSION
    ] = DELIVERY_FAULT_EVENT_REAL_SITL_SCHEMA_VERSION
    delivery_recovery_request_schema_version: Literal[
        DELIVERY_RECOVERY_REQUEST_REAL_SITL_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_REQUEST_REAL_SITL_SCHEMA_VERSION
    delivery_recovery_run_schema_version: Literal[
        DELIVERY_RECOVERY_RUN_REAL_SITL_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_RUN_REAL_SITL_SCHEMA_VERSION
    delivery_recovery_outcome_schema_version: Literal[
        DELIVERY_RECOVERY_OUTCOME_REAL_SITL_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_OUTCOME_REAL_SITL_SCHEMA_VERSION
    executed_against_real_sitl: Literal[True] = True
    recovery_chain_evidence_source: Literal[REAL_SITL_EVIDENCE_SOURCE] = (
        REAL_SITL_EVIDENCE_SOURCE
    )
    replayable: Literal[True] = True
    task_status_mutated: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False

    @field_validator("blocked_reasons", "warning_reasons", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("completed_at", mode="before")
    @classmethod
    def _coerce_completed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_loop(self) -> "DeliveryRecoveryLoopRealSITL":
        raise_for_command_like_payload(
            self.metadata,
            root="loop_real_sitl.metadata",
            error_type=DeliveryRecoveryRealSITLError,
            prefix="real-SITL loop refused command-like metadata",
        )
        if self.blocked_reasons:
            raise DeliveryRecoveryRealSITLError(
                "completed real-SITL recovery loop cannot be blocked"
            )
        return self


class DeliveryFaultEventRealSITLPayloadReleaseMissing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        DELIVERY_FAULT_EVENT_REAL_SITL_PAYLOAD_RELEASE_MISSING_SCHEMA_VERSION
    ] = DELIVERY_FAULT_EVENT_REAL_SITL_PAYLOAD_RELEASE_MISSING_SCHEMA_VERSION
    fault_event_id: str
    fault_category: Literal[DeliveryFaultCategory.PAYLOAD_RELEASE_NOT_OBSERVED]
    severity: Literal[DeliveryFaultSeverity.BLOCKING]
    observed_at: datetime
    initial_sitl_session_ref: str
    retry_sitl_session_ref: str
    source_artifact_refs: tuple[str, ...] = ()
    telemetry_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    executed_against_real_sitl: Literal[True] = True
    recovery_chain_evidence_source: Literal[REAL_SITL_EVIDENCE_SOURCE] = (
        REAL_SITL_EVIDENCE_SOURCE
    )
    evidence_only: Literal[True] = True
    command_sent: Literal[False] = False
    mission_upload_performed: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    synthetic_success_allowed: Literal[False] = False

    @field_validator(
        "source_artifact_refs",
        "telemetry_refs",
        "evidence_refs",
        "blocked_reasons",
        "warning_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_event(self) -> "DeliveryFaultEventRealSITLPayloadReleaseMissing":
        raise_for_command_like_payload(
            self.metadata,
            root="fault_event_real_sitl_payload_missing.metadata",
            error_type=DeliveryRecoveryRealSITLError,
            prefix="real-SITL payload fault refused command-like metadata",
        )
        if not self.evidence_refs:
            raise DeliveryRecoveryRealSITLError(
                "real-SITL payload fault requires evidence refs"
            )
        return self


class DeliveryRecoveryRequestRealSITLRetryDropoff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        DELIVERY_RECOVERY_REQUEST_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_REQUEST_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION
    request_id: str
    fault_event_ref: str
    mission_contract_ref: str
    recovery_decision_ref: str
    operator_status_ref: str
    initial_sitl_session_ref: str
    retry_sitl_session_ref: str
    request_kind: Literal[DeliveryRecoveryRequestKind.RETRY_DROPOFF_SIMULATION]
    recovery_decision: Literal["retry_dropoff_recommended"]
    request_status: Literal["ready"]
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    delivery_recovery_decision_schema_version: Literal[
        DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    delivery_fault_event_schema_version: Literal[
        DELIVERY_FAULT_EVENT_REAL_SITL_PAYLOAD_RELEASE_MISSING_SCHEMA_VERSION
    ] = DELIVERY_FAULT_EVENT_REAL_SITL_PAYLOAD_RELEASE_MISSING_SCHEMA_VERSION
    operator_minimal_delivery_simulation_status_schema_version: Literal[
        OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION
    ] = OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION
    executed_against_real_sitl: Literal[True] = True
    recovery_chain_evidence_source: Literal[REAL_SITL_EVIDENCE_SOURCE] = (
        REAL_SITL_EVIDENCE_SOURCE
    )
    simulation_only: Literal[True] = True
    request_only: Literal[True] = True
    sitl_only: Literal[True] = True
    bounded: Literal[True] = True
    command_payload_allowed: Literal[False] = False
    raw_mavlink_command_allowed: Literal[False] = False
    raw_ros_action_allowed: Literal[False] = False
    setpoint_stream_allowed: Literal[False] = False
    actuator_command_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False

    @field_validator("blocked_reasons", "warning_reasons", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @model_validator(mode="after")
    def _validate_request(self) -> "DeliveryRecoveryRequestRealSITLRetryDropoff":
        raise_for_command_like_payload(
            self.metadata,
            root="request_real_sitl_retry_dropoff.metadata",
            error_type=DeliveryRecoveryRealSITLError,
            prefix="real-SITL retry request refused command-like metadata",
        )
        if self.blocked_reasons:
            raise DeliveryRecoveryRealSITLError(
                "ready real-SITL retry request cannot be blocked"
            )
        return self


class DeliveryRecoveryRunRealSITLRetryDropoff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        DELIVERY_RECOVERY_RUN_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_RUN_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION
    recovery_run_id: str
    recovery_request_ref: str
    fault_event_ref: str
    initial_sitl_session_ref: str
    retry_sitl_session_ref: str
    initial_sitl_summary_ref: str
    retry_sitl_summary_ref: str
    status: Literal[DeliveryRecoveryRealSITLRunStatus.EXECUTED_IN_REAL_SITL]
    started_at: datetime
    finished_at: datetime
    observed_facts: dict[str, Any] = Field(default_factory=dict)
    observed_fact_refs: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    delivery_recovery_request_schema_version: Literal[
        DELIVERY_RECOVERY_REQUEST_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_REQUEST_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION
    delivery_fault_event_schema_version: Literal[
        DELIVERY_FAULT_EVENT_REAL_SITL_PAYLOAD_RELEASE_MISSING_SCHEMA_VERSION
    ] = DELIVERY_FAULT_EVENT_REAL_SITL_PAYLOAD_RELEASE_MISSING_SCHEMA_VERSION
    executed_against_real_sitl: Literal[True] = True
    recovery_chain_evidence_source: Literal[REAL_SITL_EVIDENCE_SOURCE] = (
        REAL_SITL_EVIDENCE_SOURCE
    )
    real_sitl_execution_claimed: Literal[True] = True
    sitl_only: Literal[True] = True
    bounded: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_observed: Literal[True] = True
    approval_free_stronger_execution_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_gazebo_container_started: Literal[True] = True
    payload_release_observed: Literal[True] = True
    payload_release_verified: Literal[True] = True
    payload_release_event_source: Literal["gazebo_detachable_joint_detach_event"] = (
        "gazebo_detachable_joint_detach_event"
    )

    @field_validator(
        "observed_fact_refs",
        "blocked_reasons",
        "warning_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("started_at", "finished_at", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_run(self) -> "DeliveryRecoveryRunRealSITLRetryDropoff":
        raise_for_command_like_payload(
            self.metadata,
            root="run_real_sitl_retry_dropoff.metadata",
            error_type=DeliveryRecoveryRealSITLError,
            prefix="real-SITL retry run refused command-like metadata",
        )
        if self.finished_at < self.started_at:
            raise DeliveryRecoveryRealSITLError(
                "real-SITL retry run finished before it started"
            )
        if self.blocked_reasons:
            raise DeliveryRecoveryRealSITLError(
                "executed real-SITL retry run cannot be blocked"
            )
        return self


class DeliveryRecoveryOutcomeRealSITLRetryRecovered(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        DELIVERY_RECOVERY_OUTCOME_REAL_SITL_RETRY_RECOVERED_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_OUTCOME_REAL_SITL_RETRY_RECOVERED_SCHEMA_VERSION
    outcome_id: str
    recovery_request_ref: str
    recovery_run_ref: str
    fault_event_ref: str
    outcome_category: Literal[DeliveryRecoveryOutcomeCategory.RECOVERED]
    observed_facts: dict[str, Any] = Field(default_factory=dict)
    observed_fact_refs: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    verified_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    delivery_recovery_request_schema_version: Literal[
        DELIVERY_RECOVERY_REQUEST_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_REQUEST_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION
    delivery_recovery_run_schema_version: Literal[
        DELIVERY_RECOVERY_RUN_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_RUN_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION
    delivery_fault_event_schema_version: Literal[
        DELIVERY_FAULT_EVENT_REAL_SITL_PAYLOAD_RELEASE_MISSING_SCHEMA_VERSION
    ] = DELIVERY_FAULT_EVENT_REAL_SITL_PAYLOAD_RELEASE_MISSING_SCHEMA_VERSION
    executed_against_real_sitl: Literal[True] = True
    recovery_chain_evidence_source: Literal[REAL_SITL_EVIDENCE_SOURCE] = (
        REAL_SITL_EVIDENCE_SOURCE
    )
    real_sitl_execution_claimed: Literal[True] = True
    observed_facts_only: Literal[True] = True
    synthetic_success_allowed: Literal[False] = False
    command_sent_by_verifier: Literal[False] = False
    external_dispatch_performed_by_verifier: Literal[False] = False
    mavlink_dispatch_performed_by_verifier: Literal[False] = False
    px4_mission_upload_performed_by_verifier: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    retry_dropoff_predicate_mode: Literal[
        "position_in_zone_and_altitude_and_time_window"
    ] = "position_in_zone_and_altitude_and_time_window"
    default_narrow_predicates: Literal[True] = True
    absolute_caps_enforced: Literal[True] = True

    @field_validator(
        "observed_fact_refs",
        "blocked_reasons",
        "warning_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("verified_at", mode="before")
    @classmethod
    def _coerce_verified_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_outcome(self) -> "DeliveryRecoveryOutcomeRealSITLRetryRecovered":
        raise_for_command_like_payload(
            self.metadata,
            root="outcome_real_sitl_retry_recovered.metadata",
            error_type=DeliveryRecoveryRealSITLError,
            prefix="real-SITL retry outcome refused command-like metadata",
        )
        if self.blocked_reasons:
            raise DeliveryRecoveryRealSITLError(
                "recovered real-SITL retry outcome cannot be blocked"
            )
        required = {
            "position_in_zone_observed": True,
            "altitude_within_tolerance_observed": True,
            "release_within_time_window_observed": True,
            "payload_release_observed": True,
            "payload_release_verified": True,
        }
        for key, expected in required.items():
            if self.observed_facts.get(key) is not expected:
                raise DeliveryRecoveryRealSITLError(
                    f"real-SITL retry outcome missing observed fact: {key}"
                )
        if (
            self.observed_facts.get("payload_release_event_source")
            != "gazebo_detachable_joint_detach_event"
        ):
            raise DeliveryRecoveryRealSITLError(
                "real-SITL retry outcome requires Gazebo detachable-joint release"
            )
        return self


class DeliveryRecoveryLoopRealSITLRetryRecovered(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        DELIVERY_RECOVERY_LOOP_REAL_SITL_RETRY_RECOVERED_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_LOOP_REAL_SITL_RETRY_RECOVERED_SCHEMA_VERSION
    loop_id: str
    fault_event_ref: str
    recovery_request_ref: str
    recovery_run_ref: str
    recovery_outcome_ref: str
    initial_sitl_session_ref: str
    retry_sitl_session_ref: str
    initial_sitl_summary_ref: str
    retry_sitl_summary_ref: str
    completed_at: datetime
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    delivery_fault_event_schema_version: Literal[
        DELIVERY_FAULT_EVENT_REAL_SITL_PAYLOAD_RELEASE_MISSING_SCHEMA_VERSION
    ] = DELIVERY_FAULT_EVENT_REAL_SITL_PAYLOAD_RELEASE_MISSING_SCHEMA_VERSION
    delivery_recovery_request_schema_version: Literal[
        DELIVERY_RECOVERY_REQUEST_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_REQUEST_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION
    delivery_recovery_run_schema_version: Literal[
        DELIVERY_RECOVERY_RUN_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_RUN_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION
    delivery_recovery_outcome_schema_version: Literal[
        DELIVERY_RECOVERY_OUTCOME_REAL_SITL_RETRY_RECOVERED_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_OUTCOME_REAL_SITL_RETRY_RECOVERED_SCHEMA_VERSION
    executed_against_real_sitl: Literal[True] = True
    recovery_chain_evidence_source: Literal[REAL_SITL_EVIDENCE_SOURCE] = (
        REAL_SITL_EVIDENCE_SOURCE
    )
    replayable: Literal[True] = True
    task_status_mutated: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False

    @field_validator("blocked_reasons", "warning_reasons", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("completed_at", mode="before")
    @classmethod
    def _coerce_completed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_loop(self) -> "DeliveryRecoveryLoopRealSITLRetryRecovered":
        raise_for_command_like_payload(
            self.metadata,
            root="loop_real_sitl_retry_recovered.metadata",
            error_type=DeliveryRecoveryRealSITLError,
            prefix="real-SITL retry loop refused command-like metadata",
        )
        if self.blocked_reasons:
            raise DeliveryRecoveryRealSITLError(
                "completed real-SITL retry loop cannot be blocked"
            )
        return self


def _summary_ref(summary: Mapping[str, Any]) -> str:
    artifact_dir = str(summary.get("artifact_dir") or "")
    return f"px4_gazebo_horizontal_route_summary:{artifact_dir}"


def _sitl_session_ref(summary: Mapping[str, Any]) -> str:
    artifact_dir = str(summary.get("artifact_dir") or "unknown")
    return f"px4_gazebo_sitl_session:{artifact_dir}"


def _validate_px4_gazebo_battery_low_summary(summary: Mapping[str, Any]) -> None:
    if summary.get("actual_px4_gazebo_horizontal_smoke_observed") is not True:
        raise DeliveryRecoveryRealSITLError(
            "battery-low recovery requires actual PX4/Gazebo smoke observation"
        )
    if summary.get("task_status") != "completed":
        raise DeliveryRecoveryRealSITLError("battery-low recovery SITL task incomplete")
    if summary.get("final_status") != "completed":
        raise DeliveryRecoveryRealSITLError("battery-low recovery final status invalid")
    if float(summary.get("completed_pose_z_m", 999.0)) > 0.15:
        raise DeliveryRecoveryRealSITLError("safe landing z threshold not observed")
    if summary.get("hardware_target_allowed") is not False:
        raise DeliveryRecoveryRealSITLError("hardware target must remain disallowed")
    if summary.get("physical_execution_invoked") is not False:
        raise DeliveryRecoveryRealSITLError("physical execution must remain false")
    if summary.get("blocked_reasons") not in ([], ()):
        raise DeliveryRecoveryRealSITLError("real-SITL summary contains blockers")


def _validate_px4_gazebo_payload_initial_missing_summary(
    summary: Mapping[str, Any],
) -> None:
    if summary.get("actual_px4_gazebo_horizontal_smoke_observed") is not True:
        raise DeliveryRecoveryRealSITLError(
            "payload recovery requires actual initial PX4/Gazebo observation"
        )
    if summary.get("task_status") != "completed":
        raise DeliveryRecoveryRealSITLError("initial payload SITL task incomplete")
    if summary.get("final_status") != "completed":
        raise DeliveryRecoveryRealSITLError("initial payload final status invalid")
    if summary.get("dropoff_region_reached") is not True:
        raise DeliveryRecoveryRealSITLError("initial dropoff region was not reached")
    if summary.get("payload_release_observed") is not False:
        raise DeliveryRecoveryRealSITLError(
            "initial payload release must be missing before retry"
        )
    if summary.get("payload_release_event_source") not in ("", None):
        raise DeliveryRecoveryRealSITLError(
            "initial payload release event source must be empty"
        )
    if summary.get("hardware_target_allowed") is not False:
        raise DeliveryRecoveryRealSITLError("hardware target must remain disallowed")
    if summary.get("physical_execution_invoked") is not False:
        raise DeliveryRecoveryRealSITLError("physical execution must remain false")
    if summary.get("blocked_reasons") not in ([], ()):
        raise DeliveryRecoveryRealSITLError("initial payload summary contains blockers")


def _validate_px4_gazebo_payload_retry_success_summary(
    summary: Mapping[str, Any],
) -> None:
    if summary.get("actual_px4_gazebo_horizontal_smoke_observed") is not True:
        raise DeliveryRecoveryRealSITLError(
            "payload retry requires actual PX4/Gazebo observation"
        )
    if summary.get("task_status") != "completed":
        raise DeliveryRecoveryRealSITLError("payload retry SITL task incomplete")
    if summary.get("final_status") != "completed":
        raise DeliveryRecoveryRealSITLError("payload retry final status invalid")
    if summary.get("dropoff_region_reached") is not True:
        raise DeliveryRecoveryRealSITLError("retry dropoff region was not reached")
    if summary.get("payload_release_observed") is not True:
        raise DeliveryRecoveryRealSITLError(
            "payload retry requires observed release"
        )
    if summary.get("payload_release_event_source") != (
        "gazebo_detachable_joint_detach_event"
    ):
        raise DeliveryRecoveryRealSITLError(
            "payload retry requires Gazebo detachable-joint release"
        )
    for key in (
        "payload_release_position_x_m",
        "payload_release_position_y_m",
        "payload_release_position_z_m",
        "payload_release_observed_at",
    ):
        if summary.get(key) in (None, ""):
            raise DeliveryRecoveryRealSITLError(
                f"payload retry missing observed release field: {key}"
            )
    if summary.get("hardware_target_allowed") is not False:
        raise DeliveryRecoveryRealSITLError("hardware target must remain disallowed")
    if summary.get("physical_execution_invoked") is not False:
        raise DeliveryRecoveryRealSITLError("physical execution must remain false")
    if summary.get("blocked_reasons") not in ([], ()):
        raise DeliveryRecoveryRealSITLError("payload retry summary contains blockers")


def build_battery_low_recovery_chain_from_px4_gazebo_summary(
    summary: Mapping[str, Any],
    *,
    mission_contract_ref: str,
    recovery_decision_ref: str,
    operator_status_ref: str,
    observed_at: datetime | None = None,
) -> dict[str, BaseModel]:
    """Build the #432 real-SITL recovery chain from observed PX4/Gazebo output."""

    _validate_px4_gazebo_battery_low_summary(summary)
    observed = _utc(observed_at)
    summary_ref = _summary_ref(summary)
    sitl_session_ref = _sitl_session_ref(summary)
    evidence_refs = (
        summary_ref,
        f"px4_gazebo_tasks_db:{summary.get('artifact_dir')}/tasks.db",
        f"px4_gazebo_pose_samples:{summary.get('artifact_dir')}/pose_samples.jsonl",
        f"px4_gazebo_docker_log:{summary.get('artifact_dir')}/px4_docker.log",
    )
    fault_payload = {
        "category": DeliveryFaultCategory.BATTERY_RESERVE_VIOLATION.value,
        "session": sitl_session_ref,
        "summary": summary_ref,
        "observed_at": observed.isoformat(),
        "executed_against_real_sitl": True,
        "recovery_chain_evidence_source": REAL_SITL_EVIDENCE_SOURCE,
    }
    fault = DeliveryFaultEventRealSITL(
        fault_event_id=_stable_id("delivery_fault_event_real_sitl", fault_payload),
        fault_category=DeliveryFaultCategory.BATTERY_RESERVE_VIOLATION,
        severity=DeliveryFaultSeverity.BLOCKING,
        observed_at=observed,
        sitl_session_ref=sitl_session_ref,
        source_artifact_refs=(summary_ref,),
        telemetry_refs=(f"px4_gazebo_route_progress:{summary_ref}",),
        evidence_refs=evidence_refs,
        bounded_run_ref=summary_ref,
        blocked_reasons=("battery_reserve_violation",),
        metadata={
            "source_summary_final_status": summary.get("final_status"),
            "source_summary_task_status": summary.get("task_status"),
        },
    )
    _require_real_sitl(
        artifact_name="fault event",
        executed_against_real_sitl=fault.executed_against_real_sitl,
        recovery_chain_evidence_source=fault.recovery_chain_evidence_source,
    )
    fault_ref = f"delivery_fault_event_real_sitl:{fault.fault_event_id}"
    request_payload = {
        "fault": fault.fault_event_id,
        "session": sitl_session_ref,
        "kind": DeliveryRecoveryRequestKind.ABORT_AND_LAND_SIMULATION.value,
        "executed_against_real_sitl": True,
        "recovery_chain_evidence_source": REAL_SITL_EVIDENCE_SOURCE,
    }
    request = DeliveryRecoveryRequestRealSITL(
        request_id=_stable_id("delivery_recovery_request_real_sitl", request_payload),
        fault_event_ref=fault_ref,
        mission_contract_ref=mission_contract_ref,
        recovery_decision_ref=recovery_decision_ref,
        operator_status_ref=operator_status_ref,
        sitl_session_ref=sitl_session_ref,
        request_kind=DeliveryRecoveryRequestKind.ABORT_AND_LAND_SIMULATION,
        recovery_decision=DeliveryRecoveryAction.ABORT_RECOMMENDED,
        request_status="ready",
        metadata={
            "policy_reason": "battery_reserve_violation_requires_safe_landing",
        },
    )
    _require_real_sitl(
        artifact_name="recovery request",
        executed_against_real_sitl=request.executed_against_real_sitl,
        recovery_chain_evidence_source=request.recovery_chain_evidence_source,
    )
    request_ref = f"delivery_recovery_request_real_sitl:{request.request_id}"
    observed_facts = {
        "fact_schema": SAFE_LANDING_FACT_SCHEMA,
        "safe_landing_event_source": "gazebo_landed_state",
        "safe_landing_observed": True,
        "mission_terminated_safely": True,
        "vehicle_disarmed_or_landed": True,
        "completed_pose_z_m": float(summary["completed_pose_z_m"]),
        "container_started": True,
    }
    run_payload = {
        "request": request.request_id,
        "fault": fault.fault_event_id,
        "session": sitl_session_ref,
        "summary": summary_ref,
        "facts": observed_facts,
        "executed_against_real_sitl": True,
        "recovery_chain_evidence_source": REAL_SITL_EVIDENCE_SOURCE,
    }
    run = DeliveryRecoveryRunRealSITL(
        recovery_run_id=_stable_id("delivery_recovery_run_real_sitl", run_payload),
        recovery_request_ref=request_ref,
        fault_event_ref=fault_ref,
        sitl_session_ref=sitl_session_ref,
        source_sitl_summary_ref=summary_ref,
        status=DeliveryRecoveryRealSITLRunStatus.EXECUTED_IN_REAL_SITL,
        started_at=observed,
        finished_at=observed,
        observed_facts=observed_facts,
        observed_fact_refs=evidence_refs,
        metadata={
            "source_summary_final_status": summary.get("final_status"),
            "source_summary_task_status": summary.get("task_status"),
        },
    )
    _require_real_sitl(
        artifact_name="recovery run",
        executed_against_real_sitl=run.executed_against_real_sitl,
        recovery_chain_evidence_source=run.recovery_chain_evidence_source,
    )
    run_ref = f"delivery_recovery_run_real_sitl:{run.recovery_run_id}"
    outcome_payload = {
        "request": request.request_id,
        "run": run.recovery_run_id,
        "fault": fault.fault_event_id,
        "facts": observed_facts,
        "category": DeliveryRecoveryOutcomeCategory.ABORTED_SAFELY.value,
        "executed_against_real_sitl": True,
        "recovery_chain_evidence_source": REAL_SITL_EVIDENCE_SOURCE,
    }
    outcome = DeliveryRecoveryOutcomeRealSITL(
        outcome_id=_stable_id("delivery_recovery_outcome_real_sitl", outcome_payload),
        recovery_request_ref=request_ref,
        recovery_run_ref=run_ref,
        fault_event_ref=fault_ref,
        outcome_category=DeliveryRecoveryOutcomeCategory.ABORTED_SAFELY,
        observed_facts=observed_facts,
        observed_fact_refs=evidence_refs,
        verified_at=observed,
    )
    _require_real_sitl(
        artifact_name="recovery outcome",
        executed_against_real_sitl=outcome.executed_against_real_sitl,
        recovery_chain_evidence_source=outcome.recovery_chain_evidence_source,
    )
    outcome_ref = f"delivery_recovery_outcome_real_sitl:{outcome.outcome_id}"
    loop_payload = {
        "fault": fault.fault_event_id,
        "request": request.request_id,
        "run": run.recovery_run_id,
        "outcome": outcome.outcome_id,
        "session": sitl_session_ref,
        "summary": summary_ref,
        "executed_against_real_sitl": True,
        "recovery_chain_evidence_source": REAL_SITL_EVIDENCE_SOURCE,
    }
    loop = DeliveryRecoveryLoopRealSITL(
        loop_id=_stable_id("delivery_recovery_loop_real_sitl", loop_payload),
        fault_event_ref=fault_ref,
        recovery_request_ref=request_ref,
        recovery_run_ref=run_ref,
        recovery_outcome_ref=outcome_ref,
        sitl_session_ref=sitl_session_ref,
        source_sitl_summary_ref=summary_ref,
        completed_at=observed,
    )
    _require_real_sitl(
        artifact_name="recovery loop",
        executed_against_real_sitl=loop.executed_against_real_sitl,
        recovery_chain_evidence_source=loop.recovery_chain_evidence_source,
    )
    return {
        "delivery_fault_event_real_sitl": fault,
        "delivery_recovery_request_real_sitl": request,
        "delivery_recovery_run_real_sitl": run,
        "delivery_recovery_outcome_real_sitl": outcome,
        "delivery_recovery_loop_real_sitl": loop,
    }


def attach_battery_low_recovery_chain_from_px4_gazebo_summary(
    task_id: str,
    summary: Mapping[str, Any],
    *,
    mission_contract_ref: str,
    recovery_decision_ref: str,
    operator_status_ref: str,
    observed_at: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    if store.get(task_id) is None:
        raise DeliveryRecoveryRealSITLError(
            f"task {task_id} not found; cannot attach real-SITL recovery chain"
        )
    chain = build_battery_low_recovery_chain_from_px4_gazebo_summary(
        summary,
        mission_contract_ref=mission_contract_ref,
        recovery_decision_ref=recovery_decision_ref,
        operator_status_ref=operator_status_ref,
        observed_at=observed_at,
    )
    artifacts = {
        key: value.model_dump(mode="json") for key, value in chain.items()
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise DeliveryRecoveryRealSITLError(
            f"task {task_id} disappeared while attaching real-SITL recovery chain"
        )
    return {**artifacts, "task": updated}


def build_payload_release_retry_recovered_chain_from_px4_gazebo_summaries(
    *,
    initial_summary: Mapping[str, Any],
    retry_summary: Mapping[str, Any],
    mission_contract_ref: str,
    recovery_decision_ref: str,
    operator_status_ref: str,
    observed_at: datetime | None = None,
) -> dict[str, BaseModel]:
    """Build the #433 Path A real-SITL chain from initial miss and retry success."""

    _validate_px4_gazebo_payload_initial_missing_summary(initial_summary)
    _validate_px4_gazebo_payload_retry_success_summary(retry_summary)
    observed = _utc(observed_at)
    initial_summary_ref = _summary_ref(initial_summary)
    retry_summary_ref = _summary_ref(retry_summary)
    initial_session_ref = _sitl_session_ref(initial_summary)
    retry_session_ref = _sitl_session_ref(retry_summary)
    evidence_refs = (
        initial_summary_ref,
        retry_summary_ref,
        f"initial_px4_gazebo_pose_samples:{initial_summary.get('artifact_dir')}/pose_samples.jsonl",
        f"retry_px4_gazebo_pose_samples:{retry_summary.get('artifact_dir')}/pose_samples.jsonl",
        f"retry_payload_release_event:{retry_summary.get('payload_release_observed_at')}",
    )
    fault_payload = {
        "category": DeliveryFaultCategory.PAYLOAD_RELEASE_NOT_OBSERVED.value,
        "initial_session": initial_session_ref,
        "retry_session": retry_session_ref,
        "initial_summary": initial_summary_ref,
        "retry_summary": retry_summary_ref,
        "observed_at": observed.isoformat(),
        "executed_against_real_sitl": True,
        "recovery_chain_evidence_source": REAL_SITL_EVIDENCE_SOURCE,
    }
    fault = DeliveryFaultEventRealSITLPayloadReleaseMissing(
        fault_event_id=_stable_id(
            "delivery_fault_event_real_sitl_payload_missing", fault_payload
        ),
        fault_category=DeliveryFaultCategory.PAYLOAD_RELEASE_NOT_OBSERVED,
        severity=DeliveryFaultSeverity.BLOCKING,
        observed_at=observed,
        initial_sitl_session_ref=initial_session_ref,
        retry_sitl_session_ref=retry_session_ref,
        source_artifact_refs=(initial_summary_ref,),
        telemetry_refs=(f"px4_gazebo_route_progress:{initial_summary_ref}",),
        evidence_refs=evidence_refs,
        blocked_reasons=("payload_release_not_observed",),
        metadata={
            "initial_payload_release_observed": False,
            "retry_payload_release_observed": True,
        },
    )
    _require_real_sitl(
        artifact_name="payload fault event",
        executed_against_real_sitl=fault.executed_against_real_sitl,
        recovery_chain_evidence_source=fault.recovery_chain_evidence_source,
    )
    fault_ref = (
        "delivery_fault_event_real_sitl_payload_release_missing:"
        f"{fault.fault_event_id}"
    )
    request_payload = {
        "fault": fault.fault_event_id,
        "initial_session": initial_session_ref,
        "retry_session": retry_session_ref,
        "kind": DeliveryRecoveryRequestKind.RETRY_DROPOFF_SIMULATION.value,
        "executed_against_real_sitl": True,
        "recovery_chain_evidence_source": REAL_SITL_EVIDENCE_SOURCE,
    }
    request = DeliveryRecoveryRequestRealSITLRetryDropoff(
        request_id=_stable_id(
            "delivery_recovery_request_real_sitl_retry_dropoff", request_payload
        ),
        fault_event_ref=fault_ref,
        mission_contract_ref=mission_contract_ref,
        recovery_decision_ref=recovery_decision_ref,
        operator_status_ref=operator_status_ref,
        initial_sitl_session_ref=initial_session_ref,
        retry_sitl_session_ref=retry_session_ref,
        request_kind=DeliveryRecoveryRequestKind.RETRY_DROPOFF_SIMULATION,
        recovery_decision="retry_dropoff_recommended",
        request_status="ready",
        metadata={"policy_reason": "payload_release_missing_retry_dropoff"},
    )
    _require_real_sitl(
        artifact_name="payload retry request",
        executed_against_real_sitl=request.executed_against_real_sitl,
        recovery_chain_evidence_source=request.recovery_chain_evidence_source,
    )
    request_ref = f"delivery_recovery_request_real_sitl_retry_dropoff:{request.request_id}"
    observed_facts = {
        "fact_schema": RETRY_DROPOFF_FACT_SCHEMA,
        "payload_release_event_source": "gazebo_detachable_joint_detach_event",
        "position_in_zone_observed": True,
        "altitude_within_tolerance_observed": True,
        "release_within_time_window_observed": True,
        "payload_release_observed": True,
        "payload_release_verified": True,
        "initial_payload_release_observed": False,
        "retry_dropoff_region_reached": True,
        "payload_release_position_x_m": float(
            retry_summary["payload_release_position_x_m"]
        ),
        "payload_release_position_y_m": float(
            retry_summary["payload_release_position_y_m"]
        ),
        "payload_release_position_z_m": float(
            retry_summary["payload_release_position_z_m"]
        ),
        "payload_release_observed_at": str(
            retry_summary["payload_release_observed_at"]
        ),
        "predicate_mode": "position_in_zone_and_altitude_and_time_window",
        "default_narrow_predicates": True,
        "absolute_caps_enforced": True,
    }
    run_payload = {
        "request": request.request_id,
        "fault": fault.fault_event_id,
        "initial_session": initial_session_ref,
        "retry_session": retry_session_ref,
        "initial_summary": initial_summary_ref,
        "retry_summary": retry_summary_ref,
        "facts": observed_facts,
        "executed_against_real_sitl": True,
        "recovery_chain_evidence_source": REAL_SITL_EVIDENCE_SOURCE,
    }
    run = DeliveryRecoveryRunRealSITLRetryDropoff(
        recovery_run_id=_stable_id(
            "delivery_recovery_run_real_sitl_retry_dropoff", run_payload
        ),
        recovery_request_ref=request_ref,
        fault_event_ref=fault_ref,
        initial_sitl_session_ref=initial_session_ref,
        retry_sitl_session_ref=retry_session_ref,
        initial_sitl_summary_ref=initial_summary_ref,
        retry_sitl_summary_ref=retry_summary_ref,
        status=DeliveryRecoveryRealSITLRunStatus.EXECUTED_IN_REAL_SITL,
        started_at=observed,
        finished_at=observed,
        observed_facts=observed_facts,
        observed_fact_refs=evidence_refs,
        metadata={
            "initial_payload_release_observed": False,
            "retry_payload_release_observed": True,
        },
    )
    _require_real_sitl(
        artifact_name="payload retry run",
        executed_against_real_sitl=run.executed_against_real_sitl,
        recovery_chain_evidence_source=run.recovery_chain_evidence_source,
    )
    run_ref = f"delivery_recovery_run_real_sitl_retry_dropoff:{run.recovery_run_id}"
    outcome_payload = {
        "request": request.request_id,
        "run": run.recovery_run_id,
        "fault": fault.fault_event_id,
        "facts": observed_facts,
        "category": DeliveryRecoveryOutcomeCategory.RECOVERED.value,
        "executed_against_real_sitl": True,
        "recovery_chain_evidence_source": REAL_SITL_EVIDENCE_SOURCE,
    }
    outcome = DeliveryRecoveryOutcomeRealSITLRetryRecovered(
        outcome_id=_stable_id(
            "delivery_recovery_outcome_real_sitl_retry_recovered", outcome_payload
        ),
        recovery_request_ref=request_ref,
        recovery_run_ref=run_ref,
        fault_event_ref=fault_ref,
        outcome_category=DeliveryRecoveryOutcomeCategory.RECOVERED,
        observed_facts=observed_facts,
        observed_fact_refs=evidence_refs,
        verified_at=observed,
    )
    _require_real_sitl(
        artifact_name="payload retry outcome",
        executed_against_real_sitl=outcome.executed_against_real_sitl,
        recovery_chain_evidence_source=outcome.recovery_chain_evidence_source,
    )
    outcome_ref = (
        "delivery_recovery_outcome_real_sitl_retry_recovered:"
        f"{outcome.outcome_id}"
    )
    loop_payload = {
        "fault": fault.fault_event_id,
        "request": request.request_id,
        "run": run.recovery_run_id,
        "outcome": outcome.outcome_id,
        "initial_session": initial_session_ref,
        "retry_session": retry_session_ref,
        "initial_summary": initial_summary_ref,
        "retry_summary": retry_summary_ref,
        "executed_against_real_sitl": True,
        "recovery_chain_evidence_source": REAL_SITL_EVIDENCE_SOURCE,
    }
    loop = DeliveryRecoveryLoopRealSITLRetryRecovered(
        loop_id=_stable_id(
            "delivery_recovery_loop_real_sitl_retry_recovered", loop_payload
        ),
        fault_event_ref=fault_ref,
        recovery_request_ref=request_ref,
        recovery_run_ref=run_ref,
        recovery_outcome_ref=outcome_ref,
        initial_sitl_session_ref=initial_session_ref,
        retry_sitl_session_ref=retry_session_ref,
        initial_sitl_summary_ref=initial_summary_ref,
        retry_sitl_summary_ref=retry_summary_ref,
        completed_at=observed,
    )
    _require_real_sitl(
        artifact_name="payload retry loop",
        executed_against_real_sitl=loop.executed_against_real_sitl,
        recovery_chain_evidence_source=loop.recovery_chain_evidence_source,
    )
    return {
        "delivery_fault_event_real_sitl_payload_release_missing": fault,
        "delivery_recovery_request_real_sitl_retry_dropoff": request,
        "delivery_recovery_run_real_sitl_retry_dropoff": run,
        "delivery_recovery_outcome_real_sitl_retry_recovered": outcome,
        "delivery_recovery_loop_real_sitl_retry_recovered": loop,
    }


def attach_payload_release_retry_recovered_chain_from_px4_gazebo_summaries(
    task_id: str,
    *,
    initial_summary: Mapping[str, Any],
    retry_summary: Mapping[str, Any],
    mission_contract_ref: str,
    recovery_decision_ref: str,
    operator_status_ref: str,
    observed_at: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    if store.get(task_id) is None:
        raise DeliveryRecoveryRealSITLError(
            f"task {task_id} not found; cannot attach real-SITL payload retry chain"
        )
    chain = build_payload_release_retry_recovered_chain_from_px4_gazebo_summaries(
        initial_summary=initial_summary,
        retry_summary=retry_summary,
        mission_contract_ref=mission_contract_ref,
        recovery_decision_ref=recovery_decision_ref,
        operator_status_ref=operator_status_ref,
        observed_at=observed_at,
    )
    artifacts = {key: value.model_dump(mode="json") for key, value in chain.items()}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise DeliveryRecoveryRealSITLError(
            f"task {task_id} disappeared while attaching real-SITL payload retry chain"
        )
    return {**artifacts, "task": updated}


__all__ = [
    "DELIVERY_FAULT_EVENT_REAL_SITL_SCHEMA_VERSION",
    "DELIVERY_FAULT_EVENT_REAL_SITL_PAYLOAD_RELEASE_MISSING_SCHEMA_VERSION",
    "DELIVERY_RECOVERY_LOOP_REAL_SITL_SCHEMA_VERSION",
    "DELIVERY_RECOVERY_LOOP_REAL_SITL_RETRY_RECOVERED_SCHEMA_VERSION",
    "DELIVERY_RECOVERY_OUTCOME_REAL_SITL_SCHEMA_VERSION",
    "DELIVERY_RECOVERY_OUTCOME_REAL_SITL_RETRY_RECOVERED_SCHEMA_VERSION",
    "DELIVERY_RECOVERY_REQUEST_REAL_SITL_SCHEMA_VERSION",
    "DELIVERY_RECOVERY_REQUEST_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION",
    "DELIVERY_RECOVERY_RUN_REAL_SITL_SCHEMA_VERSION",
    "DELIVERY_RECOVERY_RUN_REAL_SITL_RETRY_DROPOFF_SCHEMA_VERSION",
    "REAL_SITL_EVIDENCE_SOURCE",
    "DeliveryFaultEventRealSITL",
    "DeliveryFaultEventRealSITLPayloadReleaseMissing",
    "DeliveryRecoveryLoopRealSITL",
    "DeliveryRecoveryLoopRealSITLRetryRecovered",
    "DeliveryRecoveryOutcomeRealSITL",
    "DeliveryRecoveryOutcomeRealSITLRetryRecovered",
    "DeliveryRecoveryRealSITLError",
    "DeliveryRecoveryRealSITLRunStatus",
    "DeliveryRecoveryRequestRealSITL",
    "DeliveryRecoveryRequestRealSITLRetryDropoff",
    "DeliveryRecoveryRunRealSITL",
    "DeliveryRecoveryRunRealSITLRetryDropoff",
    "attach_battery_low_recovery_chain_from_px4_gazebo_summary",
    "attach_payload_release_retry_recovered_chain_from_px4_gazebo_summaries",
    "build_battery_low_recovery_chain_from_px4_gazebo_summary",
    "build_payload_release_retry_recovered_chain_from_px4_gazebo_summaries",
]
