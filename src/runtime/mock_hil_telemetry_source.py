"""Mock hardware-in-the-loop telemetry source for the HIL chain.

Closes the HIL telemetry chain end-to-end without touching real hardware,
ROS, MAVLink, PX4, Gazebo, AirSim, or Isaac Sim. The mock source is a
deterministic test / fixture helper that produces envelopes and walks them
through the same read-only path the production HIL ingestion uses:

    mock source
      -> hil_telemetry_envelope.v1
      -> ingest_hil_telemetry_envelope (fail closed on command-like payloads)
      -> hil_telemetry_evidence.v1
      -> hil_telemetry_review.v1
      -> task.artifacts (attach helper)

Out of scope
------------

- real telemetry transports (PX4 / MAVLink / ROS / WebSocket / SSE)
- runtime-resident polling, source identity, or scheduling
- action / command / actuator / dispatch surfaces (HIL is read-only)
- mission API, promotion, runtime reuse, live execution

The mock builder always pushes payloads through
``ingest_hil_telemetry_envelope`` so a test that injects a command-like key
will raise ``HilTelemetryRejected`` before any envelope is produced. This
keeps the boundary the same for fixtures as for production.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from src.runtime.hil_telemetry_contract import (
    HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
    HilTelemetryContract,
    HilTelemetryEnvelope,
    HilTelemetryMode,
    ingest_hil_telemetry_envelope,
)
from src.runtime.hil_telemetry_evidence import build_hil_telemetry_evidence
from src.runtime.hil_telemetry_review import build_hil_telemetry_review
from src.runtime.task_store import TaskStore, get_task_store


__all__ = [
    "DEFAULT_MOCK_HIL_CONTRACT_ID",
    "DEFAULT_MOCK_HIL_SUBJECT_ID",
    "DEFAULT_MOCK_HIL_SUBJECT_KIND",
    "MockHilTelemetryAttachError",
    "attach_mock_hil_telemetry_chain",
    "build_mock_hil_telemetry_chain",
    "build_mock_hil_telemetry_contract",
    "build_mock_hil_telemetry_envelope",
]


DEFAULT_MOCK_HIL_CONTRACT_ID = "mock_hil_telemetry.v1"
DEFAULT_MOCK_HIL_SUBJECT_KIND = "mock_hil_subject"
DEFAULT_MOCK_HIL_SUBJECT_ID = "mock-hil-subject-001"


class MockHilTelemetryAttachError(RuntimeError):
    """Raised when a mock HIL telemetry chain cannot be attached to a task."""


def build_mock_hil_telemetry_contract(
    *,
    contract_id: str = DEFAULT_MOCK_HIL_CONTRACT_ID,
    subject_kind: str = DEFAULT_MOCK_HIL_SUBJECT_KIND,
) -> HilTelemetryContract:
    """Static HIL contract describing the mock source.

    The mock declares itself as ``telemetry_only`` with all action / command /
    live / physical / ROS dispatch capabilities pinned to ``False`` at the
    type level via ``hil_telemetry_contract.v1``.
    """

    return HilTelemetryContract(
        contract_id=contract_id,
        subject_kind=subject_kind,
        telemetry_envelope_schema=HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
        mode=HilTelemetryMode.TELEMETRY_ONLY,
    )


def _default_measurements() -> dict[str, float | int | bool | str]:
    return {"battery": 100.0, "comms_ok": True, "mode": "idle"}


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_mock_hil_telemetry_envelope(
    *,
    contract_id: str = DEFAULT_MOCK_HIL_CONTRACT_ID,
    subject_kind: str = DEFAULT_MOCK_HIL_SUBJECT_KIND,
    subject_id: str = DEFAULT_MOCK_HIL_SUBJECT_ID,
    measurements: Mapping[str, float | int | bool | str] | None = None,
    captured_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> HilTelemetryEnvelope:
    """Deterministic mock envelope.

    The envelope is always pushed through ``ingest_hil_telemetry_envelope``
    so any injected command-like key in ``metadata`` (or anywhere else the
    walker reaches) raises ``HilTelemetryRejected`` before an envelope is
    returned. This keeps the read-only ingestion boundary the same for
    fixtures as for production.
    """

    payload = {
        "schema_version": HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
        "contract_id": contract_id,
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "captured_at": _utc(captured_at).isoformat(),
        "measurements": dict(measurements) if measurements is not None else _default_measurements(),
        "metadata": dict(metadata) if metadata is not None else {},
    }
    return ingest_hil_telemetry_envelope(payload)


def build_mock_hil_telemetry_chain(
    *,
    contract_id: str = DEFAULT_MOCK_HIL_CONTRACT_ID,
    subject_kind: str = DEFAULT_MOCK_HIL_SUBJECT_KIND,
    subject_id: str = DEFAULT_MOCK_HIL_SUBJECT_ID,
    measurements: Mapping[str, float | int | bool | str] | None = None,
    captured_at: datetime | None = None,
    now: datetime | None = None,
    freshness_threshold_seconds: float = 60.0,
    rejected_command_like_payload_count: int = 0,
    required_review: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the full envelope -> evidence -> review chain deterministically.

    Returns a dict with keys ``hil_telemetry_contract``,
    ``hil_telemetry_envelope``, ``hil_telemetry_evidence``, and
    ``hil_telemetry_review``. The review aggregates a single evidence and
    mirrors the gate-input contract from ``hil_telemetry_review.v1``.

    Any rejection along the chain (command-like keys / extra fields)
    propagates as ``HilTelemetryRejected`` from ingestion. The chain does
    not catch or downgrade rejections — callers see the failure directly.
    """

    contract = build_mock_hil_telemetry_contract(
        contract_id=contract_id,
        subject_kind=subject_kind,
    )
    envelope = build_mock_hil_telemetry_envelope(
        contract_id=contract_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
        measurements=measurements,
        captured_at=captured_at,
        metadata=metadata,
    )
    evidence_now = _utc(now)
    evidence = build_hil_telemetry_evidence(
        envelope,
        hil_telemetry_contract=contract,
        freshness_threshold_seconds=freshness_threshold_seconds,
        now=evidence_now,
    )
    review = build_hil_telemetry_review(
        telemetry_evidences=[evidence],
        required=bool(required_review),
        rejected_command_like_payload_count=int(rejected_command_like_payload_count),
        now=evidence_now,
    )
    return {
        "hil_telemetry_contract": contract,
        "hil_telemetry_envelope": envelope,
        "hil_telemetry_evidence": evidence,
        "hil_telemetry_review": review,
    }


def attach_mock_hil_telemetry_chain(
    task_id: str,
    *,
    contract_id: str = DEFAULT_MOCK_HIL_CONTRACT_ID,
    subject_kind: str = DEFAULT_MOCK_HIL_SUBJECT_KIND,
    subject_id: str = DEFAULT_MOCK_HIL_SUBJECT_ID,
    measurements: Mapping[str, float | int | bool | str] | None = None,
    captured_at: datetime | None = None,
    now: datetime | None = None,
    freshness_threshold_seconds: float = 60.0,
    rejected_command_like_payload_count: int = 0,
    required_review: bool = False,
    metadata: Mapping[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Build a mock HIL chain and attach it to a task's artifacts.

    The chain is built FIRST, so any rejection (command-like keys, extra
    fields) raises before the task store is touched and ``task.artifacts``
    is never polluted with a partially-formed payload. Successful attach
    merges via ``TaskStore.update``'s deep-merge semantics so any
    pre-existing artifacts on the task survive.

    Read-only contract: the helper does not change ``task.status``,
    ``task.metadata``, approvals, promotion state, or runtime reuse. It
    only writes the four HIL artifact keys:
    ``hil_telemetry_contract``, ``hil_telemetry_envelope``,
    ``hil_telemetry_evidence``, ``hil_telemetry_review``.
    """

    store_factory = task_store_factory or get_task_store
    store = store_factory()
    if store.get(task_id) is None:
        raise MockHilTelemetryAttachError(
            f"task {task_id} not found in task store; cannot attach mock HIL telemetry"
        )

    # Build the chain BEFORE touching the store. Ingestion / contract /
    # evidence rejections propagate before any task update.
    chain = build_mock_hil_telemetry_chain(
        contract_id=contract_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
        measurements=measurements,
        captured_at=captured_at,
        now=now,
        freshness_threshold_seconds=freshness_threshold_seconds,
        rejected_command_like_payload_count=rejected_command_like_payload_count,
        required_review=required_review,
        metadata=metadata,
    )
    artifacts_to_attach = {
        "hil_telemetry_contract": chain["hil_telemetry_contract"].model_dump(mode="json"),
        "hil_telemetry_envelope": chain["hil_telemetry_envelope"].model_dump(mode="json"),
        "hil_telemetry_evidence": chain["hil_telemetry_evidence"].model_dump(mode="json"),
        "hil_telemetry_review": chain["hil_telemetry_review"].model_dump(mode="json"),
    }
    updated = store.update(task_id, artifacts=artifacts_to_attach)
    if updated is None:
        raise MockHilTelemetryAttachError(
            f"task {task_id} disappeared while attaching mock HIL telemetry"
        )
    return chain
