"""Compile delivery recovery decisions into bounded SITL recovery requests."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_fault_event import (
    DELIVERY_FAULT_EVENT_SCHEMA_VERSION,
    DeliveryFaultCategory,
    DeliveryFaultEvent,
)
from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.delivery_recovery_decision import (
    DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION,
    DeliveryRecoveryAction,
    DeliveryRecoveryDecision,
)
from src.runtime.delivery_recovery_safety import raise_for_command_like_payload
from src.runtime.operator_minimal_delivery_simulation import (
    OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION,
    OperatorMinimalDeliverySimulationStatus,
    OperatorMinimalDeliverySimulationStatusValue,
)
from src.runtime.task_store import TaskStore, get_task_store

DELIVERY_RECOVERY_REQUEST_SCHEMA_VERSION = "delivery_recovery_request.v1"


class DeliveryRecoveryRequestError(RuntimeError):
    """Raised when a recovery request cannot be built safely."""


class DeliveryRecoveryRequestKind(str, Enum):
    RETURN_TO_HOME_SIMULATION = "return_to_home_simulation"
    ABORT_AND_LAND_SIMULATION = "abort_and_land_simulation"
    RETRY_DROPOFF_SIMULATION = "retry_dropoff_simulation"
    HOLD_POSITION_SIMULATION = "hold_position_simulation"
    OPERATOR_ESCALATION_ONLY = "operator_escalation_only"


class DeliveryRecoveryRequestStatus(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    OPERATOR_ESCALATION_REQUIRED = "operator_escalation_required"


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


def _contract_ref(contract: DeliveryMissionContract) -> str:
    return f"delivery_mission_contract:{contract.contract_id}"


def _decision_ref(decision: DeliveryRecoveryDecision) -> str:
    return f"delivery_recovery_decision:{decision.decision_id}"


def _fault_event_ref(event: DeliveryFaultEvent) -> str:
    return f"delivery_fault_event:{event.fault_event_id}"


def _operator_status_ref(status: OperatorMinimalDeliverySimulationStatus) -> str:
    return f"operator_minimal_delivery_simulation_status:{status.status_id}"


def _to_contract(
    value: DeliveryMissionContract | Mapping[str, Any],
) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_decision(
    value: DeliveryRecoveryDecision | Mapping[str, Any],
) -> DeliveryRecoveryDecision:
    if isinstance(value, DeliveryRecoveryDecision):
        return value
    return DeliveryRecoveryDecision.model_validate(dict(value))


def _to_fault_event(
    value: DeliveryFaultEvent | Mapping[str, Any],
) -> DeliveryFaultEvent:
    if isinstance(value, DeliveryFaultEvent):
        return value
    return DeliveryFaultEvent.model_validate(dict(value))


def _to_operator_status(
    value: OperatorMinimalDeliverySimulationStatus | Mapping[str, Any],
) -> OperatorMinimalDeliverySimulationStatus:
    if isinstance(value, OperatorMinimalDeliverySimulationStatus):
        return value
    return OperatorMinimalDeliverySimulationStatus.model_validate(dict(value))


def _validate_logic_only_fault_event(fault_event: DeliveryFaultEvent) -> None:
    if fault_event.executed_against_real_sitl is not False:
        raise DeliveryRecoveryRequestError(
            "delivery recovery request requires logic-only fault event"
        )
    if fault_event.recovery_chain_evidence_source != "logic_only_stub":
        raise DeliveryRecoveryRequestError(
            "delivery recovery request requires logic-only evidence source"
        )


class DeliveryRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_RECOVERY_REQUEST_SCHEMA_VERSION] = (
        DELIVERY_RECOVERY_REQUEST_SCHEMA_VERSION
    )
    request_id: str
    mission_contract_ref: str
    recovery_decision_ref: str
    fault_event_ref: str
    operator_minimal_delivery_simulation_status_ref: str
    request_kind: DeliveryRecoveryRequestKind
    request_status: DeliveryRecoveryRequestStatus
    compiled_from_action: DeliveryRecoveryAction
    fault_category: DeliveryFaultCategory
    evidence_refs: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    executed_against_real_sitl: Literal[False] = False
    recovery_chain_evidence_source: Literal["logic_only_stub"] = "logic_only_stub"
    allow_unsafe_health_abort_permitted: bool = False
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    delivery_recovery_decision_schema_version: Literal[
        DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    delivery_fault_event_schema_version: Literal[
        DELIVERY_FAULT_EVENT_SCHEMA_VERSION
    ] = DELIVERY_FAULT_EVENT_SCHEMA_VERSION
    operator_minimal_delivery_simulation_status_schema_version: Literal[
        OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION
    ] = OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION
    simulation_only: Literal[True] = True
    request_only: Literal[True] = True
    sitl_only: Literal[True] = True
    bounded: Literal[True] = True
    command_payload_allowed: Literal[False] = False
    raw_mavlink_command_allowed: Literal[False] = False
    raw_ros_action_allowed: Literal[False] = False
    setpoint_stream_allowed: Literal[False] = False
    actuator_command_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False

    @field_validator(
        "evidence_refs",
        "blocked_reasons",
        "warning_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_request(self) -> "DeliveryRecoveryRequest":
        raise_for_command_like_payload(
            self.metadata,
            root="request.metadata",
            error_type=DeliveryRecoveryRequestError,
            prefix="delivery recovery request refused command-like metadata",
        )
        if self.request_status is DeliveryRecoveryRequestStatus.READY:
            if self.blocked_reasons:
                raise DeliveryRecoveryRequestError("ready request cannot be blocked")
            if (
                self.request_kind
                is DeliveryRecoveryRequestKind.OPERATOR_ESCALATION_ONLY
            ):
                raise DeliveryRecoveryRequestError(
                    "operator escalation request cannot be ready for execution"
                )
        else:
            if not self.blocked_reasons:
                raise DeliveryRecoveryRequestError(
                    "blocked/escalated request requires blocked reasons"
                )
        return self


def _compile_kind(
    *,
    decision: DeliveryRecoveryDecision,
    fault_event: DeliveryFaultEvent,
    operator_status: OperatorMinimalDeliverySimulationStatus,
    allow_unsafe_health_abort: bool,
) -> tuple[DeliveryRecoveryRequestKind, DeliveryRecoveryRequestStatus, tuple[str, ...]]:
    blocked: list[str] = []
    category = fault_event.fault_category
    if (
        category is DeliveryFaultCategory.VEHICLE_HEALTH_UNSAFE
        and not allow_unsafe_health_abort
    ):
        return (
            DeliveryRecoveryRequestKind.OPERATOR_ESCALATION_ONLY,
            DeliveryRecoveryRequestStatus.BLOCKED,
            ("vehicle_health_unsafe_blocks_automatic_recovery_request",),
        )
    if decision.abort_recommended or decision.primary_action in {
        DeliveryRecoveryAction.ABORT,
        DeliveryRecoveryAction.ABORT_RECOMMENDED,
    }:
        return (
            DeliveryRecoveryRequestKind.ABORT_AND_LAND_SIMULATION,
            DeliveryRecoveryRequestStatus.READY,
            (),
        )
    if decision.return_to_home_recommended:
        return (
            DeliveryRecoveryRequestKind.RETURN_TO_HOME_SIMULATION,
            DeliveryRecoveryRequestStatus.READY,
            (),
        )
    if category is DeliveryFaultCategory.PAYLOAD_RELEASE_NOT_OBSERVED:
        return (
            DeliveryRecoveryRequestKind.RETRY_DROPOFF_SIMULATION,
            DeliveryRecoveryRequestStatus.READY,
            (),
        )
    if category in {
        DeliveryFaultCategory.TELEMETRY_STALE,
        DeliveryFaultCategory.TELEMETRY_MISSING,
    }:
        if decision.hold_recommended or decision.hold_proposed:
            return (
                DeliveryRecoveryRequestKind.HOLD_POSITION_SIMULATION,
                DeliveryRecoveryRequestStatus.READY,
                (),
            )
        blocked.append("telemetry_fault_requires_hold_or_operator_escalation")
    if operator_status.status is (
        OperatorMinimalDeliverySimulationStatusValue.OPERATOR_ESCALATION_REQUIRED
    ):
        blocked.extend(operator_status.escalation_triggers or ())
    if not blocked:
        blocked.append("recovery_decision_has_no_bounded_request")
    return (
        DeliveryRecoveryRequestKind.OPERATOR_ESCALATION_ONLY,
        DeliveryRecoveryRequestStatus.OPERATOR_ESCALATION_REQUIRED,
        tuple(dict.fromkeys(blocked)),
    )


def build_delivery_recovery_request(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    delivery_recovery_decision: DeliveryRecoveryDecision | Mapping[str, Any],
    delivery_fault_event: DeliveryFaultEvent | Mapping[str, Any],
    operator_minimal_delivery_simulation_status: (
        OperatorMinimalDeliverySimulationStatus | Mapping[str, Any]
    ),
    allow_unsafe_health_abort: bool = False,
    evidence_refs: Sequence[str] | None = None,
    warning_reasons: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> DeliveryRecoveryRequest:
    metadata_payload = dict(metadata or {})
    raise_for_command_like_payload(
        metadata_payload,
        root="metadata",
        error_type=DeliveryRecoveryRequestError,
        prefix="delivery recovery request refused command-like metadata",
    )
    contract = _to_contract(delivery_mission_contract)
    decision = _to_decision(delivery_recovery_decision)
    fault_event = _to_fault_event(delivery_fault_event)
    operator_status = _to_operator_status(operator_minimal_delivery_simulation_status)
    _validate_logic_only_fault_event(fault_event)
    created_at = _utc(now)
    contract_ref = _contract_ref(contract)
    decision_ref = _decision_ref(decision)
    if decision.delivery_mission_contract_id != contract.contract_id:
        raise DeliveryRecoveryRequestError("decision contract ref mismatch")
    if operator_status.delivery_mission_contract_ref != contract_ref:
        raise DeliveryRecoveryRequestError("operator status contract ref mismatch")
    if operator_status.delivery_recovery_decision_ref != decision_ref:
        raise DeliveryRecoveryRequestError("operator status decision ref mismatch")
    if fault_event.episode_ref and (
        fault_event.episode_ref != operator_status.simulated_delivery_episode_ref
    ):
        raise DeliveryRecoveryRequestError("fault event episode ref mismatch")

    request_kind, request_status, blocked = _compile_kind(
        decision=decision,
        fault_event=fault_event,
        operator_status=operator_status,
        allow_unsafe_health_abort=allow_unsafe_health_abort,
    )
    evidence = _as_tuple(
        [
            contract_ref,
            decision_ref,
            _fault_event_ref(fault_event),
            _operator_status_ref(operator_status),
            *fault_event.evidence_refs,
            *operator_status.evidence_refs,
            *(evidence_refs or ()),
        ]
    )
    warnings = _as_tuple([*fault_event.warning_reasons, *(warning_reasons or ())])
    payload = {
        "contract": contract_ref,
        "decision": decision_ref,
        "fault_event": _fault_event_ref(fault_event),
        "operator_status": _operator_status_ref(operator_status),
        "request_kind": request_kind.value,
        "status": request_status.value,
        "blocked": blocked,
        "executed_against_real_sitl": False,
        "recovery_chain_evidence_source": "logic_only_stub",
        "allow_unsafe_health_abort_permitted": allow_unsafe_health_abort,
    }
    return DeliveryRecoveryRequest(
        request_id=_stable_id("delivery_recovery_request", payload),
        mission_contract_ref=contract_ref,
        recovery_decision_ref=decision_ref,
        fault_event_ref=_fault_event_ref(fault_event),
        operator_minimal_delivery_simulation_status_ref=_operator_status_ref(
            operator_status
        ),
        request_kind=request_kind,
        request_status=request_status,
        compiled_from_action=decision.primary_action,
        fault_category=fault_event.fault_category,
        evidence_refs=evidence,
        blocked_reasons=blocked,
        warning_reasons=warnings,
        created_at=created_at,
        metadata={
            **metadata_payload,
            "compiled_from_observed_fault": True,
            "bounded_request_only": True,
            "no_raw_command_payload": True,
            "executed_against_real_sitl": False,
            "recovery_chain_evidence_source": "logic_only_stub",
            "allow_unsafe_health_abort_permitted": allow_unsafe_health_abort,
        },
        allow_unsafe_health_abort_permitted=allow_unsafe_health_abort,
    )


def attach_delivery_recovery_request(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    delivery_recovery_decision: DeliveryRecoveryDecision | Mapping[str, Any],
    delivery_fault_event: DeliveryFaultEvent | Mapping[str, Any],
    operator_minimal_delivery_simulation_status: (
        OperatorMinimalDeliverySimulationStatus | Mapping[str, Any]
    ),
    allow_unsafe_health_abort: bool = False,
    evidence_refs: Sequence[str] | None = None,
    warning_reasons: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    if store.get(task_id) is None:
        raise DeliveryRecoveryRequestError(
            f"task {task_id} not found; cannot attach recovery request"
        )
    request = build_delivery_recovery_request(
        delivery_mission_contract=delivery_mission_contract,
        delivery_recovery_decision=delivery_recovery_decision,
        delivery_fault_event=delivery_fault_event,
        operator_minimal_delivery_simulation_status=(
            operator_minimal_delivery_simulation_status
        ),
        allow_unsafe_health_abort=allow_unsafe_health_abort,
        evidence_refs=evidence_refs,
        warning_reasons=warning_reasons,
        now=now,
        metadata=metadata,
    )
    artifacts = {"delivery_recovery_request": request.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise DeliveryRecoveryRequestError(
            f"task {task_id} disappeared while attaching recovery request"
        )
    return {**artifacts, "task": updated}


__all__ = [
    "DELIVERY_RECOVERY_REQUEST_SCHEMA_VERSION",
    "DeliveryRecoveryRequest",
    "DeliveryRecoveryRequestError",
    "DeliveryRecoveryRequestKind",
    "DeliveryRecoveryRequestStatus",
    "attach_delivery_recovery_request",
    "build_delivery_recovery_request",
]
