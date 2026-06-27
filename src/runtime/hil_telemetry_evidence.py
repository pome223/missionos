"""Read-only HIL telemetry evidence artifacts.

This module is the second slice after ``hil_telemetry_contract.v1``. It turns a
validated telemetry-only envelope into Mission OS evidence and can attach that
evidence to an existing task. It deliberately does not add an ingestion
endpoint, UI control, runtime reuse, promotion, ROS dispatch, actuator command,
or live execution path.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.hil_telemetry_contract import (
    HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION,
    HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
    HilTelemetryContract,
    HilTelemetryEnvelope,
    ingest_hil_telemetry_envelope,
)
from src.runtime.task_store import TaskStore, get_task_store


HIL_TELEMETRY_EVIDENCE_SCHEMA_VERSION = "hil_telemetry_evidence.v1"


class HilTelemetryEvidenceStatus(str, Enum):
    FRESH = "fresh"
    STALE = "stale"


class HilTelemetryEvidenceError(RuntimeError):
    """Raised when HIL telemetry evidence cannot be created or attached."""


class HilTelemetryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[HIL_TELEMETRY_EVIDENCE_SCHEMA_VERSION] = (
        HIL_TELEMETRY_EVIDENCE_SCHEMA_VERSION
    )
    evidence_id: str
    contract_id: str
    subject_kind: str
    subject_id: str
    envelope_id: str
    telemetry_envelope_schema: Literal[HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION] = (
        HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION
    )
    captured_at: datetime
    evaluated_at: datetime
    freshness_seconds: float
    freshness_threshold_seconds: float
    status: HilTelemetryEvidenceStatus
    measurement_keys: list[str] = Field(default_factory=list)
    hil_telemetry_envelope_snapshot: dict[str, Any] = Field(default_factory=dict)
    gate_findings: list[dict[str, Any]] = Field(default_factory=list)
    review_findings: list[dict[str, Any]] = Field(default_factory=list)
    rejected_command_like_payload_count: int = 0
    read_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    supports_action_dispatch: Literal[False] = False
    supports_command_payload: Literal[False] = False
    supports_live_execution: Literal[False] = False
    supports_physical_execution: Literal[False] = False
    supports_ros_dispatch: Literal[False] = False
    action_envelope_created: Literal[False] = False
    command_payload_created: Literal[False] = False
    promotion_created: Literal[False] = False
    runtime_reuse_created: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _contract_payload(
    contract: HilTelemetryContract | dict[str, Any] | None,
) -> dict[str, Any] | None:
    if contract is None:
        return None
    normalized = (
        contract
        if isinstance(contract, HilTelemetryContract)
        else HilTelemetryContract.model_validate(contract)
    )
    return normalized.model_dump(mode="json")


def build_hil_telemetry_evidence(
    telemetry_envelope: HilTelemetryEnvelope | dict[str, Any],
    *,
    hil_telemetry_contract: HilTelemetryContract | dict[str, Any] | None = None,
    freshness_threshold_seconds: float = 60.0,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> HilTelemetryEvidence:
    """Build read-only HIL telemetry evidence from a validated envelope."""

    envelope = ingest_hil_telemetry_envelope(telemetry_envelope)
    contract = _contract_payload(hil_telemetry_contract)
    if contract is not None:
        if contract["contract_id"] != envelope.contract_id:
            raise HilTelemetryEvidenceError("HIL telemetry contract_id mismatch")
        if contract["subject_kind"] != envelope.subject_kind:
            raise HilTelemetryEvidenceError("HIL telemetry subject_kind mismatch")
        if contract["telemetry_envelope_schema"] != envelope.schema_version:
            raise HilTelemetryEvidenceError("HIL telemetry envelope schema mismatch")

    evaluated_at = _to_utc(now or _utc_now())
    captured_at = _to_utc(envelope.captured_at)
    freshness = max(0.0, (evaluated_at - captured_at).total_seconds())
    stale = freshness > float(freshness_threshold_seconds)
    envelope_snapshot = envelope.model_dump(mode="json")
    findings: list[dict[str, Any]] = []
    if stale:
        findings.append(
            {
                "bucket": "hil_telemetry_stale",
                "reason": "freshness_threshold_exceeded",
                "freshness_seconds": freshness,
                "freshness_threshold_seconds": float(freshness_threshold_seconds),
            }
        )

    evidence_id = _stable_id(
        "hil_telemetry_evidence",
        {
            "contract_id": envelope.contract_id,
            "subject_kind": envelope.subject_kind,
            "subject_id": envelope.subject_id,
            "captured_at": captured_at.isoformat(),
            "measurements": envelope_snapshot["measurements"],
        },
    )
    envelope_id = _stable_id(
        "hil_telemetry_envelope",
        {
            "contract_id": envelope.contract_id,
            "subject_id": envelope.subject_id,
            "captured_at": captured_at.isoformat(),
            "measurements": envelope_snapshot["measurements"],
        },
    )
    return HilTelemetryEvidence(
        evidence_id=evidence_id,
        contract_id=envelope.contract_id,
        subject_kind=envelope.subject_kind,
        subject_id=envelope.subject_id,
        envelope_id=envelope_id,
        captured_at=captured_at,
        evaluated_at=evaluated_at,
        freshness_seconds=freshness,
        freshness_threshold_seconds=float(freshness_threshold_seconds),
        status=(
            HilTelemetryEvidenceStatus.STALE
            if stale
            else HilTelemetryEvidenceStatus.FRESH
        ),
        measurement_keys=sorted(envelope.measurements.keys()),
        hil_telemetry_envelope_snapshot=envelope_snapshot,
        gate_findings=findings,
        review_findings=findings,
        metadata={
            **(metadata or {}),
            "artifact_only": True,
            "telemetry_only": True,
            "read_only": True,
            "contract_schema_version": (
                contract["schema_version"]
                if contract is not None
                else HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION
            ),
            "source": "hil_telemetry_envelope",
        },
    )


def attach_hil_telemetry_artifacts(
    task_id: str,
    telemetry_envelope: HilTelemetryEnvelope | dict[str, Any],
    *,
    hil_telemetry_contract: HilTelemetryContract | dict[str, Any] | None = None,
    freshness_threshold_seconds: float = 60.0,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach read-only HIL telemetry evidence to an existing task.

    Invalid, malformed, or command-like telemetry raises before any task update,
    so rejected payloads are not persisted. Successful attachment only merges
    artifacts and never changes task status, approvals, promotion, runtime reuse,
    or execution permissions.
    """

    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise HilTelemetryEvidenceError(
            f"task {task_id} not found in task store; cannot attach HIL telemetry"
        )

    contract_payload = _contract_payload(hil_telemetry_contract)
    evidence = build_hil_telemetry_evidence(
        telemetry_envelope,
        hil_telemetry_contract=contract_payload,
        freshness_threshold_seconds=freshness_threshold_seconds,
        now=now,
    )
    envelope_snapshot = evidence.hil_telemetry_envelope_snapshot
    artifacts: dict[str, Any] = {
        "hil_telemetry_envelope": envelope_snapshot,
        "hil_telemetry_evidence": evidence.model_dump(mode="json"),
    }
    if contract_payload is not None:
        artifacts["hil_telemetry_contract"] = contract_payload

    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise HilTelemetryEvidenceError(
            f"task {task_id} disappeared while attaching HIL telemetry"
        )
    return artifacts


__all__ = [
    "HIL_TELEMETRY_EVIDENCE_SCHEMA_VERSION",
    "HilTelemetryEvidence",
    "HilTelemetryEvidenceError",
    "HilTelemetryEvidenceStatus",
    "attach_hil_telemetry_artifacts",
    "build_hil_telemetry_evidence",
]
