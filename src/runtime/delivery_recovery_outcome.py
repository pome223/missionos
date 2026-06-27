"""Verify logic-only delivery recovery outcomes from observed facts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_recovery_request import (
    DELIVERY_RECOVERY_REQUEST_SCHEMA_VERSION,
    DeliveryRecoveryRequest,
    DeliveryRecoveryRequestKind,
)
from src.runtime.delivery_recovery_run import (
    DELIVERY_RECOVERY_RUN_SCHEMA_VERSION,
    DeliveryRecoveryRun,
    DeliveryRecoveryRunStatus,
)
from src.runtime.delivery_recovery_safety import raise_for_command_like_payload
from src.runtime.task_store import TaskStore, get_task_store

DELIVERY_RECOVERY_OUTCOME_SCHEMA_VERSION = "delivery_recovery_outcome.v1"

RECOVERY_OUTCOME_DEFAULT_DROPOFF_ZONE_RADIUS_M = 0.5
RECOVERY_OUTCOME_ABSOLUTE_DROPOFF_ZONE_RADIUS_M = 5.0
RECOVERY_OUTCOME_DEFAULT_ALTITUDE_TOLERANCE_M = 0.5
RECOVERY_OUTCOME_ABSOLUTE_ALTITUDE_TOLERANCE_M = 2.0
RECOVERY_OUTCOME_DEFAULT_RELEASE_TIME_WINDOW_SECONDS = 5.0
RECOVERY_OUTCOME_ABSOLUTE_RELEASE_TIME_WINDOW_SECONDS = 30.0

RecoveryOutcomePayloadReleaseEventSource = Literal[
    "logic_only_stub",
    "gazebo_gripper_detach_event",
    "gazebo_detachable_joint_detach_event",
    "mavlink_gripper_action_observed",
    "mavlink_actuator_release_observed",
]
RecoveryOutcomeSafeLandingEventSource = Literal[
    "logic_only_stub",
    "gazebo_landed_state",
    "px4_vehicle_status_landed",
    "px4_disarmed_after_landing",
]
RecoveryOutcomeHoldEventSource = Literal[
    "logic_only_stub",
    "px4_position_hold_state",
    "gazebo_stationary_pose_trace",
    "operator_escalation_record",
]


class DeliveryRecoveryOutcomeError(RuntimeError):
    """Raised when recovery outcome verification is inconsistent."""


class DeliveryRecoveryOutcomeCategory(str, Enum):
    RECOVERED = "recovered"
    BLOCKED = "blocked"
    ABORTED_SAFELY = "aborted_safely"
    OPERATOR_ESCALATION_REQUIRED = "operator_escalation_required"
    RETRY_FAILED = "retry_failed"


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


def _request_ref(request: DeliveryRecoveryRequest) -> str:
    return f"delivery_recovery_request:{request.request_id}"


def _run_ref(run: DeliveryRecoveryRun) -> str:
    return f"delivery_recovery_run:{run.recovery_run_id}"


def _bounded_float(*, value: float, default: float, absolute: float) -> float:
    if value <= 0:
        return default
    return min(float(value), absolute)


def _to_request(
    value: DeliveryRecoveryRequest | Mapping[str, Any],
) -> DeliveryRecoveryRequest:
    if isinstance(value, DeliveryRecoveryRequest):
        return value
    return DeliveryRecoveryRequest.model_validate(dict(value))


def _to_run(value: DeliveryRecoveryRun | Mapping[str, Any]) -> DeliveryRecoveryRun:
    if isinstance(value, DeliveryRecoveryRun):
        return value
    return DeliveryRecoveryRun.model_validate(dict(value))


def _validate_logic_only_chain(
    *,
    request: DeliveryRecoveryRequest,
    run: DeliveryRecoveryRun,
) -> None:
    if request.executed_against_real_sitl is not False:
        raise DeliveryRecoveryOutcomeError(
            "delivery recovery outcome requires logic-only request"
        )
    if request.recovery_chain_evidence_source != "logic_only_stub":
        raise DeliveryRecoveryOutcomeError(
            "delivery recovery outcome requires logic-only request evidence source"
        )
    if run.executed_against_real_sitl is not False:
        raise DeliveryRecoveryOutcomeError(
            "delivery recovery outcome requires logic-only recovery run"
        )
    if run.recovery_chain_evidence_source != "logic_only_stub":
        raise DeliveryRecoveryOutcomeError(
            "delivery recovery outcome requires logic-only run evidence source"
        )


class DeliveryRecoveryRetryDropoffObservedFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_schema: Literal["delivery_recovery_retry_dropoff_facts.v1"] = (
        "delivery_recovery_retry_dropoff_facts.v1"
    )
    payload_release_event_source: RecoveryOutcomePayloadReleaseEventSource
    position_in_zone_observed: bool = False
    altitude_within_tolerance_observed: bool = False
    release_within_time_window_observed: bool = False
    dropoff_zone_radius_m: float = Field(
        default=RECOVERY_OUTCOME_DEFAULT_DROPOFF_ZONE_RADIUS_M,
        gt=0,
        le=RECOVERY_OUTCOME_ABSOLUTE_DROPOFF_ZONE_RADIUS_M,
    )
    altitude_tolerance_m: float = Field(
        default=RECOVERY_OUTCOME_DEFAULT_ALTITUDE_TOLERANCE_M,
        gt=0,
        le=RECOVERY_OUTCOME_ABSOLUTE_ALTITUDE_TOLERANCE_M,
    )
    release_time_window_seconds: float = Field(
        default=RECOVERY_OUTCOME_DEFAULT_RELEASE_TIME_WINDOW_SECONDS,
        gt=0,
        le=RECOVERY_OUTCOME_ABSOLUTE_RELEASE_TIME_WINDOW_SECONDS,
    )
    default_narrow_predicates: Literal[True] = True
    absolute_caps_enforced: Literal[True] = True
    predicate_mode: Literal["position_in_zone_and_altitude_and_time_window"] = (
        "position_in_zone_and_altitude_and_time_window"
    )

    @field_validator(
        "dropoff_zone_radius_m",
        "altitude_tolerance_m",
        "release_time_window_seconds",
        mode="before",
    )
    @classmethod
    def _bound_predicate_window(cls, value: Any, info: Any) -> float:
        if info.field_name == "dropoff_zone_radius_m":
            return _bounded_float(
                value=float(value or RECOVERY_OUTCOME_DEFAULT_DROPOFF_ZONE_RADIUS_M),
                default=RECOVERY_OUTCOME_DEFAULT_DROPOFF_ZONE_RADIUS_M,
                absolute=RECOVERY_OUTCOME_ABSOLUTE_DROPOFF_ZONE_RADIUS_M,
            )
        if info.field_name == "altitude_tolerance_m":
            return _bounded_float(
                value=float(value or RECOVERY_OUTCOME_DEFAULT_ALTITUDE_TOLERANCE_M),
                default=RECOVERY_OUTCOME_DEFAULT_ALTITUDE_TOLERANCE_M,
                absolute=RECOVERY_OUTCOME_ABSOLUTE_ALTITUDE_TOLERANCE_M,
            )
        return _bounded_float(
            value=float(value or RECOVERY_OUTCOME_DEFAULT_RELEASE_TIME_WINDOW_SECONDS),
            default=RECOVERY_OUTCOME_DEFAULT_RELEASE_TIME_WINDOW_SECONDS,
            absolute=RECOVERY_OUTCOME_ABSOLUTE_RELEASE_TIME_WINDOW_SECONDS,
        )

    @property
    def all_predicates_observed(self) -> bool:
        return (
            self.position_in_zone_observed
            and self.altitude_within_tolerance_observed
            and self.release_within_time_window_observed
        )


class DeliveryRecoverySafeTerminationObservedFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_schema: Literal["delivery_recovery_safe_termination_facts.v1"] = (
        "delivery_recovery_safe_termination_facts.v1"
    )
    safe_landing_event_source: RecoveryOutcomeSafeLandingEventSource
    safe_landing_observed: bool = False
    mission_terminated_safely: bool = False
    vehicle_disarmed_or_landed: bool = False
    predicate_mode: Literal[
        "safe_landing_and_mission_terminated_and_vehicle_disarmed_or_landed"
    ] = "safe_landing_and_mission_terminated_and_vehicle_disarmed_or_landed"

    @property
    def all_predicates_observed(self) -> bool:
        return (
            self.safe_landing_observed
            and self.mission_terminated_safely
            and self.vehicle_disarmed_or_landed
        )


class DeliveryRecoveryHoldObservedFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_schema: Literal["delivery_recovery_hold_facts.v1"] = (
        "delivery_recovery_hold_facts.v1"
    )
    hold_event_source: RecoveryOutcomeHoldEventSource
    hold_state_observed: bool = False
    telemetry_restored: bool = False
    operator_escalation_required: bool = False
    predicate_mode: Literal[
        "hold_state_and_telemetry_restored_or_operator_escalation"
    ] = "hold_state_and_telemetry_restored_or_operator_escalation"

    @property
    def recovered_predicates_observed(self) -> bool:
        return self.hold_state_observed and self.telemetry_restored

    @property
    def escalation_predicates_observed(self) -> bool:
        return self.hold_state_observed and self.operator_escalation_required


class DeliveryRecoveryOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_RECOVERY_OUTCOME_SCHEMA_VERSION] = (
        DELIVERY_RECOVERY_OUTCOME_SCHEMA_VERSION
    )
    outcome_id: str
    recovery_request_ref: str
    recovery_run_ref: str
    outcome_category: DeliveryRecoveryOutcomeCategory
    request_kind: DeliveryRecoveryRequestKind
    observed_facts: dict[str, Any] = Field(default_factory=dict)
    observed_fact_refs: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    verified_at: datetime
    delivery_recovery_request_schema_version: Literal[
        DELIVERY_RECOVERY_REQUEST_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_REQUEST_SCHEMA_VERSION
    delivery_recovery_run_schema_version: Literal[
        DELIVERY_RECOVERY_RUN_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_RUN_SCHEMA_VERSION
    executed_against_real_sitl: Literal[False] = False
    recovery_chain_evidence_source: Literal["logic_only_stub"] = "logic_only_stub"
    logic_only_stub: Literal[True] = True
    real_sitl_execution_claimed: Literal[False] = False
    real_sitl_chain_required_for_epic_exit: Literal[True] = True
    observed_facts_only: Literal[True] = True
    synthetic_success_allowed: Literal[False] = False
    command_sent_by_verifier: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    external_dispatch_performed_by_verifier: Literal[False] = False
    mavlink_dispatch_performed_by_verifier: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    px4_mission_upload_performed_by_verifier: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "observed_fact_refs", "blocked_reasons", "warning_reasons", mode="before"
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
    def _validate_outcome(self) -> "DeliveryRecoveryOutcome":
        raise_for_command_like_payload(
            self.metadata,
            root="outcome.metadata",
            error_type=DeliveryRecoveryOutcomeError,
            prefix="delivery recovery outcome refused command-like metadata",
        )
        raise_for_command_like_payload(
            self.observed_facts,
            root="outcome.observed_facts",
            error_type=DeliveryRecoveryOutcomeError,
            prefix="delivery recovery outcome refused command-like observed facts",
        )
        successful = {
            DeliveryRecoveryOutcomeCategory.RECOVERED,
            DeliveryRecoveryOutcomeCategory.ABORTED_SAFELY,
        }
        if self.outcome_category in successful and self.blocked_reasons:
            raise DeliveryRecoveryOutcomeError(
                "successful recovery outcome cannot be blocked"
            )
        if self.outcome_category not in successful and not self.blocked_reasons:
            raise DeliveryRecoveryOutcomeError(
                "blocked/escalated recovery outcome requires reasons"
            )
        return self


def _retry_facts(value: Mapping[str, Any]) -> DeliveryRecoveryRetryDropoffObservedFacts:
    return DeliveryRecoveryRetryDropoffObservedFacts.model_validate(dict(value))


def _safe_facts(
    value: Mapping[str, Any],
) -> DeliveryRecoverySafeTerminationObservedFacts:
    return DeliveryRecoverySafeTerminationObservedFacts.model_validate(dict(value))


def _hold_facts(value: Mapping[str, Any]) -> DeliveryRecoveryHoldObservedFacts:
    return DeliveryRecoveryHoldObservedFacts.model_validate(dict(value))


def _facts_dict(value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value)


def _classify_outcome(
    *,
    request: DeliveryRecoveryRequest,
    run: DeliveryRecoveryRun,
    observed_facts: Mapping[str, Any],
) -> tuple[DeliveryRecoveryOutcomeCategory, dict[str, Any], tuple[str, ...]]:
    blocked: list[str] = []
    if run.status is DeliveryRecoveryRunStatus.BLOCKED:
        return (
            DeliveryRecoveryOutcomeCategory.BLOCKED,
            dict(observed_facts),
            _as_tuple(run.blocked_reasons or ("recovery_run_blocked",)),
        )
    if run.status is DeliveryRecoveryRunStatus.OPERATOR_ESCALATION_REQUIRED:
        return (
            DeliveryRecoveryOutcomeCategory.OPERATOR_ESCALATION_REQUIRED,
            dict(observed_facts),
            _as_tuple(run.blocked_reasons or ("operator_escalation_required",)),
        )

    if request.request_kind in {
        DeliveryRecoveryRequestKind.RETURN_TO_HOME_SIMULATION,
        DeliveryRecoveryRequestKind.ABORT_AND_LAND_SIMULATION,
    }:
        facts = _safe_facts(observed_facts)
        if not facts.safe_landing_observed:
            blocked.append("safe_landing_observed_missing")
        if not facts.mission_terminated_safely:
            blocked.append("mission_terminated_safely_missing")
        if not facts.vehicle_disarmed_or_landed:
            blocked.append("vehicle_disarmed_or_landed_missing")
        if not blocked:
            return (
                (
                    DeliveryRecoveryOutcomeCategory.ABORTED_SAFELY
                    if request.request_kind
                    is DeliveryRecoveryRequestKind.ABORT_AND_LAND_SIMULATION
                    else DeliveryRecoveryOutcomeCategory.RECOVERED
                ),
                facts.model_dump(mode="json"),
                (),
            )
        return (
            DeliveryRecoveryOutcomeCategory.BLOCKED,
            facts.model_dump(mode="json"),
            _as_tuple(blocked),
        )

    if request.request_kind is DeliveryRecoveryRequestKind.RETRY_DROPOFF_SIMULATION:
        facts = _retry_facts(observed_facts)
        if not facts.position_in_zone_observed:
            blocked.append("position_in_zone_observed_missing")
        if not facts.altitude_within_tolerance_observed:
            blocked.append("altitude_within_tolerance_observed_missing")
        if not facts.release_within_time_window_observed:
            blocked.append("release_within_time_window_observed_missing")
        return (
            (
                DeliveryRecoveryOutcomeCategory.RECOVERED
                if not blocked
                else DeliveryRecoveryOutcomeCategory.RETRY_FAILED
            ),
            facts.model_dump(mode="json"),
            _as_tuple(blocked),
        )

    if request.request_kind is DeliveryRecoveryRequestKind.HOLD_POSITION_SIMULATION:
        facts = _hold_facts(observed_facts)
        if facts.recovered_predicates_observed:
            return (
                DeliveryRecoveryOutcomeCategory.RECOVERED,
                facts.model_dump(mode="json"),
                (),
            )
        if facts.escalation_predicates_observed:
            return (
                DeliveryRecoveryOutcomeCategory.OPERATOR_ESCALATION_REQUIRED,
                facts.model_dump(mode="json"),
                ("operator_escalation_required",),
            )
        blocked.append("hold_state_and_resolution_predicates_missing")
        return (
            DeliveryRecoveryOutcomeCategory.BLOCKED,
            facts.model_dump(mode="json"),
            _as_tuple(blocked),
        )

    return (
        DeliveryRecoveryOutcomeCategory.OPERATOR_ESCALATION_REQUIRED,
        dict(observed_facts),
        ("operator_escalation_only",),
    )


def build_delivery_recovery_outcome(
    *,
    delivery_recovery_request: DeliveryRecoveryRequest | Mapping[str, Any],
    delivery_recovery_run: DeliveryRecoveryRun | Mapping[str, Any],
    observed_facts: Mapping[str, Any],
    observed_fact_refs: Sequence[str] | None = None,
    warning_reasons: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> DeliveryRecoveryOutcome:
    metadata_payload = dict(metadata or {})
    raise_for_command_like_payload(
        metadata_payload,
        root="metadata",
        error_type=DeliveryRecoveryOutcomeError,
        prefix="delivery recovery outcome refused command-like metadata",
    )
    request = _to_request(delivery_recovery_request)
    run = _to_run(delivery_recovery_run)
    _validate_logic_only_chain(request=request, run=run)
    if run.recovery_request_ref != _request_ref(request):
        raise DeliveryRecoveryOutcomeError("recovery run request ref mismatch")
    category, facts, blocked_reasons = _classify_outcome(
        request=request,
        run=run,
        observed_facts=observed_facts,
    )
    verified_at = _utc(now)
    payload = {
        "request": request.request_id,
        "run": run.recovery_run_id,
        "kind": request.request_kind.value,
        "category": category.value,
        "facts": facts,
        "blocked": blocked_reasons,
        "executed_against_real_sitl": False,
        "recovery_chain_evidence_source": "logic_only_stub",
    }
    return DeliveryRecoveryOutcome(
        outcome_id=_stable_id("delivery_recovery_outcome", payload),
        recovery_request_ref=_request_ref(request),
        recovery_run_ref=_run_ref(run),
        outcome_category=category,
        request_kind=request.request_kind,
        observed_facts=facts,
        observed_fact_refs=_as_tuple(observed_fact_refs),
        blocked_reasons=blocked_reasons,
        warning_reasons=_as_tuple([*run.warning_reasons, *(warning_reasons or ())]),
        verified_at=verified_at,
        metadata={
            **metadata_payload,
            "observed_fact_predicates_only": True,
            "logic_only_stub": True,
            "real_sitl_chain_required_for_epic_exit": True,
            "executed_against_real_sitl": False,
            "recovery_chain_evidence_source": "logic_only_stub",
        },
    )


def attach_delivery_recovery_outcome(
    task_id: str,
    *,
    delivery_recovery_request: DeliveryRecoveryRequest | Mapping[str, Any],
    delivery_recovery_run: DeliveryRecoveryRun | Mapping[str, Any],
    observed_facts: Mapping[str, Any],
    observed_fact_refs: Sequence[str] | None = None,
    warning_reasons: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    if store.get(task_id) is None:
        raise DeliveryRecoveryOutcomeError(
            f"task {task_id} not found; cannot attach recovery outcome"
        )
    outcome = build_delivery_recovery_outcome(
        delivery_recovery_request=delivery_recovery_request,
        delivery_recovery_run=delivery_recovery_run,
        observed_facts=observed_facts,
        observed_fact_refs=observed_fact_refs,
        warning_reasons=warning_reasons,
        now=now,
        metadata=metadata,
    )
    artifacts = {"delivery_recovery_outcome": outcome.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise DeliveryRecoveryOutcomeError(
            f"task {task_id} disappeared while attaching recovery outcome"
        )
    return {**artifacts, "task": updated}


__all__ = [
    "DELIVERY_RECOVERY_OUTCOME_SCHEMA_VERSION",
    "RECOVERY_OUTCOME_ABSOLUTE_ALTITUDE_TOLERANCE_M",
    "RECOVERY_OUTCOME_ABSOLUTE_DROPOFF_ZONE_RADIUS_M",
    "RECOVERY_OUTCOME_ABSOLUTE_RELEASE_TIME_WINDOW_SECONDS",
    "DeliveryRecoveryHoldObservedFacts",
    "DeliveryRecoveryOutcome",
    "DeliveryRecoveryOutcomeCategory",
    "DeliveryRecoveryOutcomeError",
    "DeliveryRecoveryRetryDropoffObservedFacts",
    "DeliveryRecoverySafeTerminationObservedFacts",
    "attach_delivery_recovery_outcome",
    "build_delivery_recovery_outcome",
]
