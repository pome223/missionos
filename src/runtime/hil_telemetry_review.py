"""HIL telemetry review — design slice (gate / scorecard input contract).

Layer between ``hil_telemetry_evidence.v1`` (read-only operator-visible
evidence) and any future safety gate that wants to consume HIL telemetry.
This module produces ``hil_telemetry_review.v1``: a normalized, deterministic
artifact that aggregates one or more HIL telemetry evidences into a single
pass / block decision plus typed findings, suitable as gate / scorecard input.

What it normalizes
------------------

For each input evidence:
- ``stale`` status     -> finding ``hil_telemetry_stale`` (blocking)
- empty measurements   -> finding ``hil_telemetry_malformed`` (blocking)

Across the input:
- caller asserts the data was required but provided no evidence
                       -> finding ``hil_telemetry_missing`` (blocking)
- caller observed N command-like payloads that the ingestion path rejected
                       -> finding ``command_payload_rejected`` (blocking)

What it intentionally does NOT do
---------------------------------

- It does not call into ``hil_telemetry_contract`` ingestion. Rejection of
  command-like payloads happens at ingestion time; the review only counts
  rejections the caller hands it.
- It does not connect to real hardware, ROS, MAVLink, PX4 SITL, Gazebo,
  AirSim, or Isaac Sim.
- It does not create or wire HIL evidence into ``autonomy_gate_result.v1``
  yet. That is a separate slice; this slice produces the contract / artifact
  that such a gate would read.
- It does not approve, promote, reuse, or permit live / physical / stronger
  execution. ``operator_approval_required`` stays ``True`` and the
  live / physical / command flags stay ``False`` at the type level.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.hil_telemetry_contract import (
    HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION,
)
from src.runtime.hil_telemetry_evidence import (
    HilTelemetryEvidence,
    HilTelemetryEvidenceStatus,
)


HIL_TELEMETRY_REVIEW_SCHEMA_VERSION = "hil_telemetry_review.v1"

# Stable bucket / reason vocabulary so gate / UI / corpus can pin against it.
HIL_REVIEW_BUCKET_STALE = "hil_telemetry_stale"
HIL_REVIEW_BUCKET_MISSING = "hil_telemetry_missing"
HIL_REVIEW_BUCKET_MALFORMED = "hil_telemetry_malformed"
HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED = "command_payload_rejected"


class HilTelemetryReviewStatus(str, Enum):
    PASSED = "passed"
    BLOCKED = "blocked"


class HilTelemetryReviewSeverity(str, Enum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


class HilTelemetryFinding(BaseModel):
    """Single typed observation about HIL telemetry input.

    The bucket is one of the ``HIL_REVIEW_BUCKET_*`` constants (or a future
    bucket added to that vocabulary). The severity tells consumers whether
    this finding alone is sufficient to block the gate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket: str
    reason: str
    severity: HilTelemetryReviewSeverity
    detail: dict[str, Any] = Field(default_factory=dict)


class HilTelemetryReview(BaseModel):
    """Normalized aggregate over one or more HIL telemetry evidences.

    Static safety invariants are pinned at the type level via Pydantic
    ``Literal`` so a review that advertises stronger capabilities cannot
    be constructed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[HIL_TELEMETRY_REVIEW_SCHEMA_VERSION] = (
        HIL_TELEMETRY_REVIEW_SCHEMA_VERSION
    )
    review_id: str
    contract_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    envelope_ids: tuple[str, ...] = ()
    passed: bool
    status: HilTelemetryReviewStatus
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    findings: tuple[HilTelemetryFinding, ...] = ()
    measurement_keys: tuple[str, ...] = ()
    freshness_seconds_max: float = 0.0
    freshness_threshold_seconds: float = 0.0
    evaluated_at: datetime
    rejected_command_like_payload_count: int = 0
    required: bool = False
    contract_schema_version: Literal[HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION] = (
        HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION
    )
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _to_evidence(
    value: HilTelemetryEvidence | Mapping[str, Any],
) -> HilTelemetryEvidence:
    if isinstance(value, HilTelemetryEvidence):
        return value
    return HilTelemetryEvidence.model_validate(dict(value))


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_hil_telemetry_review(
    *,
    telemetry_evidences: Sequence[HilTelemetryEvidence | Mapping[str, Any]] = (),
    required: bool = False,
    rejected_command_like_payload_count: int = 0,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> HilTelemetryReview:
    """Aggregate HIL telemetry evidence into a gate-consumable review.

    Inputs
    ------

    - ``telemetry_evidences``: zero or more ``HilTelemetryEvidence`` objects
      (or dicts that parse into one). Order is not significant; the review
      sorts and dedupes.
    - ``required``: when True, an empty input list emits a
      ``hil_telemetry_missing`` blocking finding so the gate can require
      HIL telemetry to be present.
    - ``rejected_command_like_payload_count``: caller's own count of
      payloads the HIL ingestion path rejected for command-like content.
      Non-zero produces a single ``command_payload_rejected`` blocking
      finding with the count in its detail.

    Output
    ------

    A ``HilTelemetryReview`` with:
    - ``passed`` / ``status`` ``passed | blocked`` set by whether any
      blocking finding fired
    - ``blocked_reasons`` / ``warning_reasons`` sorted + deduped string sets
    - ``findings`` one per observation, in stable order
    - ``measurement_keys`` aggregated across all evidences, sorted + deduped
    - ``freshness_seconds_max`` max ``freshness_seconds`` seen across input
    - ``contract_ids`` / ``evidence_ids`` / ``envelope_ids`` aggregated
    - ``review_id`` deterministic ``_stable_id`` over the input

    The result does not approve, promote, reuse, or permit any live /
    physical / command execution; the corresponding fields are pinned to
    ``False`` (or ``True`` for ``operator_approval_required``) at the type
    level via Pydantic ``Literal``.
    """

    evaluated_at = _utc(now)
    parsed = [_to_evidence(item) for item in telemetry_evidences]

    findings: list[HilTelemetryFinding] = []
    blocked: list[str] = []

    if required and not parsed:
        findings.append(
            HilTelemetryFinding(
                bucket=HIL_REVIEW_BUCKET_MISSING,
                reason="hil_telemetry_evidence_required_but_absent",
                severity=HilTelemetryReviewSeverity.BLOCKING,
                detail={"required": True, "evidence_count": 0},
            )
        )
        blocked.append(HIL_REVIEW_BUCKET_MISSING)

    measurement_keys: set[str] = set()
    contract_ids: set[str] = set()
    evidence_ids: set[str] = set()
    envelope_ids: set[str] = set()
    freshness_threshold_seconds = 0.0
    freshness_seconds_max = 0.0

    for evidence in parsed:
        contract_ids.add(evidence.contract_id)
        evidence_ids.add(evidence.evidence_id)
        envelope_ids.add(evidence.envelope_id)
        measurement_keys.update(evidence.measurement_keys)
        freshness_threshold_seconds = max(
            freshness_threshold_seconds, float(evidence.freshness_threshold_seconds)
        )
        freshness_seconds_max = max(
            freshness_seconds_max, float(evidence.freshness_seconds)
        )

        if evidence.status is HilTelemetryEvidenceStatus.STALE:
            findings.append(
                HilTelemetryFinding(
                    bucket=HIL_REVIEW_BUCKET_STALE,
                    reason="freshness_threshold_exceeded",
                    severity=HilTelemetryReviewSeverity.BLOCKING,
                    detail={
                        "evidence_id": evidence.evidence_id,
                        "freshness_seconds": float(evidence.freshness_seconds),
                        "freshness_threshold_seconds": float(
                            evidence.freshness_threshold_seconds
                        ),
                    },
                )
            )
            blocked.append(HIL_REVIEW_BUCKET_STALE)

        if not evidence.measurement_keys:
            findings.append(
                HilTelemetryFinding(
                    bucket=HIL_REVIEW_BUCKET_MALFORMED,
                    reason="hil_telemetry_envelope_carries_no_measurements",
                    severity=HilTelemetryReviewSeverity.BLOCKING,
                    detail={"evidence_id": evidence.evidence_id},
                )
            )
            blocked.append(HIL_REVIEW_BUCKET_MALFORMED)

    if int(rejected_command_like_payload_count) > 0:
        findings.append(
            HilTelemetryFinding(
                bucket=HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED,
                reason="hil_telemetry_ingestion_rejected_command_like_payload",
                severity=HilTelemetryReviewSeverity.BLOCKING,
                detail={
                    "rejected_count": int(rejected_command_like_payload_count),
                },
            )
        )
        blocked.append(HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED)

    sorted_blocked = tuple(sorted(set(blocked)))
    passed = not sorted_blocked

    review_id = _stable_id(
        "hil_telemetry_review",
        {
            "contract_ids": sorted(contract_ids),
            "evidence_ids": sorted(evidence_ids),
            "blocked_reasons": list(sorted_blocked),
            "required": required,
            "rejected_command_like_payload_count": int(
                rejected_command_like_payload_count
            ),
        },
    )

    return HilTelemetryReview(
        review_id=review_id,
        contract_ids=tuple(sorted(contract_ids)),
        evidence_ids=tuple(sorted(evidence_ids)),
        envelope_ids=tuple(sorted(envelope_ids)),
        passed=passed,
        status=(
            HilTelemetryReviewStatus.PASSED
            if passed
            else HilTelemetryReviewStatus.BLOCKED
        ),
        blocked_reasons=sorted_blocked,
        warning_reasons=(),
        findings=tuple(findings),
        measurement_keys=tuple(sorted(measurement_keys)),
        freshness_seconds_max=round(freshness_seconds_max, 6),
        freshness_threshold_seconds=round(freshness_threshold_seconds, 6),
        evaluated_at=evaluated_at,
        rejected_command_like_payload_count=int(rejected_command_like_payload_count),
        required=bool(required),
        metadata={
            **(metadata or {}),
            "artifact_only": True,
            "telemetry_only": True,
            "read_only": True,
            "no_external_hardware_connection": True,
            "rule_based": True,
            "llm_judge_used": False,
        },
    )


__all__ = [
    "HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED",
    "HIL_REVIEW_BUCKET_MALFORMED",
    "HIL_REVIEW_BUCKET_MISSING",
    "HIL_REVIEW_BUCKET_STALE",
    "HIL_TELEMETRY_REVIEW_SCHEMA_VERSION",
    "HilTelemetryFinding",
    "HilTelemetryReview",
    "HilTelemetryReviewSeverity",
    "HilTelemetryReviewStatus",
    "build_hil_telemetry_review",
]
