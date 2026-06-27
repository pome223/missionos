"""Replayable delivery recovery loop ledger with previous-receipt validation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_fault_event import DeliveryFaultEvent
from src.runtime.delivery_recovery_safety import raise_for_command_like_payload
from src.runtime.px4_gazebo_sitl_recovery_upload_loop import (
    validate_previous_receipt_refs_for_task,
)
from src.runtime.task_store import TaskStore, get_task_store

DELIVERY_RECOVERY_LOOP_SCHEMA_VERSION = "delivery_recovery_loop.v1"


class DeliveryRecoveryLoopError(RuntimeError):
    """Raised when a recovery loop ledger cannot be built safely."""


class DeliveryRecoveryLoopStatus(str, Enum):
    READY_FOR_RECOVERY_REQUEST = "ready_for_recovery_request"
    WAITING_FOR_RECOVERY_RUN = "waiting_for_recovery_run"
    WAITING_FOR_OUTCOME = "waiting_for_outcome"
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


def _single_or_many(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, list | tuple):
        return tuple(value)
    return (value,)


def _artifact_ref(prefix: str, value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        identifier = value.get(key)
        if isinstance(identifier, str) and identifier:
            return f"{prefix}:{identifier}"
    return ""


def _fault_ref(event: DeliveryFaultEvent) -> str:
    return f"delivery_fault_event:{event.fault_event_id}"


def _schema_ref(
    *,
    artifact_key: str,
    prefix: str,
    value: Any,
    id_keys: Sequence[str],
) -> str:
    if not isinstance(value, Mapping):
        return ""
    ref = _artifact_ref(prefix, value, *id_keys)
    if ref:
        return ref
    identifier = value.get("ref")
    if isinstance(identifier, str) and identifier:
        return identifier
    return ""


class DeliveryRecoveryLoop(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_RECOVERY_LOOP_SCHEMA_VERSION] = (
        DELIVERY_RECOVERY_LOOP_SCHEMA_VERSION
    )
    recovery_loop_id: str
    mission_contract_ref: str
    delivery_episode_ref: str
    fault_event_refs: tuple[str, ...] = ()
    recovery_decision_refs: tuple[str, ...] = ()
    recovery_request_refs: tuple[str, ...] = ()
    bounded_run_refs: tuple[str, ...] = ()
    command_receipt_refs: tuple[str, ...] = ()
    previous_receipt_refs: tuple[str, ...] = ()
    outcome_refs: tuple[str, ...] = ()
    loop_status: DeliveryRecoveryLoopStatus
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    executed_against_real_sitl: Literal[False] = False
    recovery_chain_evidence_source: Literal["logic_only_stub"] = "logic_only_stub"
    replayable: Literal[True] = True
    task_status_mutated: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False

    @field_validator(
        "fault_event_refs",
        "recovery_decision_refs",
        "recovery_request_refs",
        "bounded_run_refs",
        "command_receipt_refs",
        "previous_receipt_refs",
        "outcome_refs",
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
    def _validate_loop(self) -> "DeliveryRecoveryLoop":
        raise_for_command_like_payload(
            self.metadata,
            root="loop.metadata",
            error_type=DeliveryRecoveryLoopError,
            prefix="delivery recovery loop refused command-like metadata",
        )
        if self.loop_status is DeliveryRecoveryLoopStatus.BLOCKED:
            if not self.blocked_reasons:
                raise DeliveryRecoveryLoopError("blocked loop requires reasons")
        elif self.blocked_reasons:
            raise DeliveryRecoveryLoopError("non-blocked loop cannot be blocked")
        return self


def _coerce_fault_events(values: Sequence[Any]) -> tuple[DeliveryFaultEvent, ...]:
    return tuple(
        (
            value
            if isinstance(value, DeliveryFaultEvent)
            else DeliveryFaultEvent.model_validate(dict(value))
        )
        for value in values
    )


def _derive_status(
    *,
    blocked_reasons: Sequence[str],
    recovery_request_refs: Sequence[str],
    bounded_run_refs: Sequence[str],
    outcome_refs: Sequence[str],
) -> DeliveryRecoveryLoopStatus:
    if blocked_reasons:
        return DeliveryRecoveryLoopStatus.BLOCKED
    if outcome_refs:
        return DeliveryRecoveryLoopStatus.WAITING_FOR_OUTCOME
    if bounded_run_refs:
        return DeliveryRecoveryLoopStatus.WAITING_FOR_OUTCOME
    if recovery_request_refs:
        return DeliveryRecoveryLoopStatus.WAITING_FOR_RECOVERY_RUN
    return DeliveryRecoveryLoopStatus.READY_FOR_RECOVERY_REQUEST


def build_delivery_recovery_loop(
    *,
    mission_contract_ref: str,
    delivery_episode_ref: str,
    fault_events: Sequence[DeliveryFaultEvent | Mapping[str, Any]] | None = None,
    recovery_decision_refs: Sequence[str] | None = None,
    recovery_request_refs: Sequence[str] | None = None,
    bounded_run_refs: Sequence[str] | None = None,
    command_receipt_refs: Sequence[str] | None = None,
    previous_receipt_refs: Sequence[str] | None = None,
    outcome_refs: Sequence[str] | None = None,
    warning_reasons: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> DeliveryRecoveryLoop:
    metadata_payload = dict(metadata or {})
    raise_for_command_like_payload(
        metadata_payload,
        root="metadata",
        error_type=DeliveryRecoveryLoopError,
        prefix="delivery recovery loop refused command-like metadata",
    )
    created_at = _utc(now)
    faults = _coerce_fault_events(tuple(fault_events or ()))
    fault_refs = _as_tuple(_fault_ref(event) for event in faults)
    decision_refs = _as_tuple(recovery_decision_refs)
    request_refs = _as_tuple(recovery_request_refs)
    run_refs = _as_tuple(bounded_run_refs)
    receipt_refs = _as_tuple(command_receipt_refs)
    previous_refs = _as_tuple(previous_receipt_refs)
    outcomes = _as_tuple(outcome_refs)
    blocked: list[str] = []
    warnings: list[str] = list(warning_reasons or ())
    for event in faults:
        if event.executed_against_real_sitl is not False:
            blocked.append("fault_event_real_sitl_source_not_supported_in_this_slice")
        if event.recovery_chain_evidence_source != "logic_only_stub":
            blocked.append("fault_event_evidence_source_not_logic_only_stub")
        if (
            event.episode_ref
            and delivery_episode_ref
            and event.episode_ref != delivery_episode_ref
        ):
            blocked.append("fault_event_episode_ref_mismatch")
        if event.bounded_run_ref and event.bounded_run_ref not in run_refs:
            blocked.append("fault_event_bounded_run_ref_missing")
        warnings.extend(event.warning_reasons)
    blocked_reasons = tuple(dict.fromkeys(blocked))
    status = _derive_status(
        blocked_reasons=blocked_reasons,
        recovery_request_refs=request_refs,
        bounded_run_refs=run_refs,
        outcome_refs=outcomes,
    )
    payload = {
        "mission_contract_ref": mission_contract_ref,
        "delivery_episode_ref": delivery_episode_ref,
        "fault_event_refs": fault_refs,
        "decision_refs": decision_refs,
        "request_refs": request_refs,
        "run_refs": run_refs,
        "receipt_refs": receipt_refs,
        "previous_refs": previous_refs,
        "outcome_refs": outcomes,
        "status": status.value,
        "blocked": blocked_reasons,
    }
    return DeliveryRecoveryLoop(
        recovery_loop_id=_stable_id("delivery_recovery_loop", payload),
        mission_contract_ref=str(mission_contract_ref or ""),
        delivery_episode_ref=str(delivery_episode_ref or ""),
        fault_event_refs=fault_refs,
        recovery_decision_refs=decision_refs,
        recovery_request_refs=request_refs,
        bounded_run_refs=run_refs,
        command_receipt_refs=receipt_refs,
        previous_receipt_refs=previous_refs,
        outcome_refs=outcomes,
        loop_status=status,
        blocked_reasons=blocked_reasons,
        warning_reasons=tuple(dict.fromkeys(warnings)),
        created_at=created_at,
        metadata={**metadata_payload, "previous_receipt_refs_validated": True},
    )


def build_delivery_recovery_loop_from_task(
    task: Mapping[str, Any],
    *,
    previous_receipt_refs: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> DeliveryRecoveryLoop:
    validated_previous_refs = validate_previous_receipt_refs_for_task(
        task,
        previous_receipt_refs,
    )
    artifacts = task.get("artifacts") if isinstance(task, Mapping) else None
    if not isinstance(artifacts, Mapping):
        artifacts = {}
    contract = artifacts.get("delivery_mission_contract")
    episode = artifacts.get("simulated_delivery_episode")
    decision = artifacts.get("delivery_recovery_decision")
    mission_contract_ref = ""
    delivery_episode_ref = ""
    if isinstance(contract, Mapping):
        mission_contract_ref = _artifact_ref(
            "delivery_mission_contract",
            contract,
            "contract_id",
        )
    if isinstance(episode, Mapping):
        delivery_episode_ref = _artifact_ref(
            "simulated_delivery_episode",
            episode,
            "episode_id",
        )
    if not delivery_episode_ref:
        fault = artifacts.get("delivery_fault_event")
        if isinstance(fault, Mapping):
            delivery_episode_ref = str(fault.get("episode_ref") or "")
    decision_refs = []
    if isinstance(decision, Mapping):
        decision_refs.append(
            _artifact_ref("delivery_recovery_decision", decision, "decision_id")
        )
    request_refs = []
    request = artifacts.get("delivery_recovery_request")
    if isinstance(request, Mapping):
        request_refs.append(
            _schema_ref(
                artifact_key="delivery_recovery_request",
                prefix="delivery_recovery_request",
                value=request,
                id_keys=("request_id",),
            )
        )
        if not mission_contract_ref:
            mission_contract_ref = str(request.get("mission_contract_ref") or "")
    bounded_refs: list[str] = []
    bounded_run = artifacts.get("px4_gazebo_bounded_simulation_run")
    if isinstance(bounded_run, Mapping):
        bounded_refs.append(
            _artifact_ref("px4_gazebo_bounded_simulation_run", bounded_run, "run_id")
        )
    recovery_run = artifacts.get("delivery_recovery_run")
    if isinstance(recovery_run, Mapping):
        bounded_refs.append(
            _schema_ref(
                artifact_key="delivery_recovery_run",
                prefix="delivery_recovery_run",
                value=recovery_run,
                id_keys=("recovery_run_id",),
            )
        )
    receipt_refs: list[str] = []
    receipt = artifacts.get("px4_gazebo_sitl_mission_upload_receipt")
    if isinstance(receipt, Mapping):
        receipt_refs.append(
            _artifact_ref(
                "px4_gazebo_sitl_mission_upload_receipt",
                receipt,
                "receipt_id",
            )
        )
    simulator_receipt = artifacts.get("simulator_command_execution_receipt")
    if isinstance(simulator_receipt, Mapping):
        receipt_refs.append(
            _artifact_ref(
                "simulator_command_execution_receipt",
                simulator_receipt,
                "execution_receipt_id",
            )
        )
    recovery_upload_loop = artifacts.get("px4_gazebo_sitl_recovery_upload_loop")
    if isinstance(recovery_upload_loop, Mapping):
        receipt_refs.extend(
            str(ref) for ref in recovery_upload_loop.get("receipt_refs") or ()
        )
    outcome_refs = []
    outcome = artifacts.get("delivery_recovery_outcome")
    if isinstance(outcome, Mapping):
        outcome_refs.append(
            _schema_ref(
                artifact_key="delivery_recovery_outcome",
                prefix="delivery_recovery_outcome",
                value=outcome,
                id_keys=("outcome_id",),
            )
        )
    return build_delivery_recovery_loop(
        mission_contract_ref=mission_contract_ref,
        delivery_episode_ref=delivery_episode_ref,
        fault_events=_single_or_many(artifacts.get("delivery_fault_event"))
        + _single_or_many(artifacts.get("delivery_fault_events")),
        recovery_decision_refs=decision_refs,
        recovery_request_refs=request_refs,
        bounded_run_refs=bounded_refs,
        command_receipt_refs=receipt_refs,
        previous_receipt_refs=validated_previous_refs,
        outcome_refs=outcome_refs,
        now=now,
        metadata=metadata,
    )


def attach_delivery_recovery_loop(
    task_id: str,
    *,
    previous_receipt_refs: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise DeliveryRecoveryLoopError(
            f"task {task_id} not found; cannot attach recovery loop"
        )
    loop = build_delivery_recovery_loop_from_task(
        current,
        previous_receipt_refs=previous_receipt_refs,
        now=now,
        metadata=metadata,
    )
    artifacts = {"delivery_recovery_loop": loop.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise DeliveryRecoveryLoopError(
            f"task {task_id} disappeared while attaching recovery loop"
        )
    return {**artifacts, "task": updated}


__all__ = [
    "DELIVERY_RECOVERY_LOOP_SCHEMA_VERSION",
    "DeliveryRecoveryLoop",
    "DeliveryRecoveryLoopError",
    "DeliveryRecoveryLoopStatus",
    "attach_delivery_recovery_loop",
    "build_delivery_recovery_loop",
    "build_delivery_recovery_loop_from_task",
]
