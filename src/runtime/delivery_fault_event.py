"""Observed fault-event vocabulary for SITL delivery recovery."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_recovery_safety import raise_for_command_like_payload
from src.runtime.task_store import TaskStore, get_task_store

DELIVERY_FAULT_EVENT_SCHEMA_VERSION = "delivery_fault_event.v1"


class DeliveryFaultEventError(RuntimeError):
    """Raised when a delivery fault event cannot be represented safely."""


class DeliveryFaultCategory(str, Enum):
    BATTERY_LOW = "battery_low"
    BATTERY_RESERVE_VIOLATION = "battery_reserve_violation"
    TELEMETRY_STALE = "telemetry_stale"
    TELEMETRY_MISSING = "telemetry_missing"
    PAYLOAD_RELEASE_NOT_OBSERVED = "payload_release_not_observed"
    DROPOFF_EVIDENCE_MISSING = "dropoff_evidence_missing"
    ROUTE_DEVIATION = "route_deviation"
    LANDING_ZONE_UNAVAILABLE = "landing_zone_unavailable"
    VEHICLE_HEALTH_UNSAFE = "vehicle_health_unsafe"
    WORLD_LOAD_FAILURE = "world_load_failure"
    SIMULATOR_TIMEOUT = "simulator_timeout"


class DeliveryFaultSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"
    CRITICAL = "critical"


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


class DeliveryFaultEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_FAULT_EVENT_SCHEMA_VERSION] = (
        DELIVERY_FAULT_EVENT_SCHEMA_VERSION
    )
    fault_event_id: str
    fault_category: DeliveryFaultCategory
    severity: DeliveryFaultSeverity
    observed_at: datetime
    source_artifact_refs: tuple[str, ...] = ()
    telemetry_refs: tuple[str, ...] = ()
    episode_ref: str = ""
    bounded_run_ref: str = ""
    evidence_refs: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    executed_against_real_sitl: Literal[False] = False
    recovery_chain_evidence_source: Literal["logic_only_stub"] = "logic_only_stub"
    evidence_only: Literal[True] = True
    command_sent: Literal[False] = False
    mission_upload_performed: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    approval_artifact_created: Literal[False] = False
    promotion_artifact_created: Literal[False] = False
    reuse_artifact_created: Literal[False] = False

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
    def _validate_event(self) -> "DeliveryFaultEvent":
        raise_for_command_like_payload(
            self.metadata,
            root="fault_event.metadata",
            error_type=DeliveryFaultEventError,
            prefix="delivery fault event refused command-like metadata",
        )
        if self.fault_category in {
            DeliveryFaultCategory.BATTERY_LOW,
            DeliveryFaultCategory.BATTERY_RESERVE_VIOLATION,
            DeliveryFaultCategory.TELEMETRY_STALE,
            DeliveryFaultCategory.TELEMETRY_MISSING,
            DeliveryFaultCategory.PAYLOAD_RELEASE_NOT_OBSERVED,
        } and not (self.telemetry_refs or self.evidence_refs):
            raise DeliveryFaultEventError(
                "delivery fault event requires observed evidence refs"
            )
        return self


def build_delivery_fault_event(
    *,
    fault_category: DeliveryFaultCategory | str,
    severity: DeliveryFaultSeverity | str,
    source_artifact_refs: Sequence[str] | None = None,
    telemetry_refs: Sequence[str] | None = None,
    episode_ref: str = "",
    bounded_run_ref: str = "",
    evidence_refs: Sequence[str] | None = None,
    blocked_reasons: Sequence[str] | None = None,
    warning_reasons: Sequence[str] | None = None,
    observed_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> DeliveryFaultEvent:
    metadata_payload = dict(metadata or {})
    raise_for_command_like_payload(
        metadata_payload,
        root="metadata",
        error_type=DeliveryFaultEventError,
        prefix="delivery fault event refused command-like metadata",
    )
    category = (
        fault_category
        if isinstance(fault_category, DeliveryFaultCategory)
        else DeliveryFaultCategory(str(fault_category))
    )
    resolved_severity = (
        severity
        if isinstance(severity, DeliveryFaultSeverity)
        else DeliveryFaultSeverity(str(severity))
    )
    observed = _utc(observed_at)
    source_refs = _as_tuple(source_artifact_refs)
    telemetry = _as_tuple(telemetry_refs)
    evidence = _as_tuple([*telemetry, *(evidence_refs or ())])
    blocked = _as_tuple(blocked_reasons)
    warnings = _as_tuple(warning_reasons)
    payload = {
        "fault_category": category.value,
        "severity": resolved_severity.value,
        "source_artifact_refs": source_refs,
        "telemetry_refs": telemetry,
        "episode_ref": episode_ref,
        "bounded_run_ref": bounded_run_ref,
        "evidence_refs": evidence,
        "blocked_reasons": blocked,
        "warning_reasons": warnings,
        "observed_at": observed.isoformat(),
    }
    return DeliveryFaultEvent(
        fault_event_id=_stable_id("delivery_fault_event", payload),
        fault_category=category,
        severity=resolved_severity,
        observed_at=observed,
        source_artifact_refs=source_refs,
        telemetry_refs=telemetry,
        episode_ref=str(episode_ref or ""),
        bounded_run_ref=str(bounded_run_ref or ""),
        evidence_refs=evidence,
        blocked_reasons=blocked,
        warning_reasons=warnings,
        metadata=metadata_payload,
    )


def attach_delivery_fault_event(
    task_id: str,
    *,
    fault_category: DeliveryFaultCategory | str,
    severity: DeliveryFaultSeverity | str,
    source_artifact_refs: Sequence[str] | None = None,
    telemetry_refs: Sequence[str] | None = None,
    episode_ref: str = "",
    bounded_run_ref: str = "",
    evidence_refs: Sequence[str] | None = None,
    blocked_reasons: Sequence[str] | None = None,
    warning_reasons: Sequence[str] | None = None,
    observed_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    if store.get(task_id) is None:
        raise DeliveryFaultEventError(
            f"task {task_id} not found; cannot attach delivery fault event"
        )
    event = build_delivery_fault_event(
        fault_category=fault_category,
        severity=severity,
        source_artifact_refs=source_artifact_refs,
        telemetry_refs=telemetry_refs,
        episode_ref=episode_ref,
        bounded_run_ref=bounded_run_ref,
        evidence_refs=evidence_refs,
        blocked_reasons=blocked_reasons,
        warning_reasons=warning_reasons,
        observed_at=observed_at,
        metadata=metadata,
    )
    artifacts = {"delivery_fault_event": event.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise DeliveryFaultEventError(
            f"task {task_id} disappeared while attaching delivery fault event"
        )
    return {**artifacts, "task": updated}


__all__ = [
    "DELIVERY_FAULT_EVENT_SCHEMA_VERSION",
    "DeliveryFaultCategory",
    "DeliveryFaultEvent",
    "DeliveryFaultEventError",
    "DeliveryFaultSeverity",
    "attach_delivery_fault_event",
    "build_delivery_fault_event",
]
