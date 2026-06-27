"""Delivery mission scorecard and gate artifacts.

``delivery_mission_scorecard.v1`` and ``delivery_mission_gate_result.v1`` lift
``delivery_mission_policy_review.v1`` into the Mission OS gate pattern. They are
rule-based aggregate artifacts only. They do not dispatch, approve, command,
mutate Gazebo, publish ROS/MAVLink, or execute actuators.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.delivery_mission_policy_review import (
    DELIVERY_MISSION_POLICY_REVIEW_SCHEMA_VERSION,
    DeliveryMissionPolicyReview,
    DeliveryMissionPolicyReviewStatus,
)
from src.runtime.task_store import TaskStore, get_task_store


DELIVERY_MISSION_SCORECARD_SCHEMA_VERSION = "delivery_mission_scorecard.v1"
DELIVERY_MISSION_GATE_RESULT_SCHEMA_VERSION = "delivery_mission_gate_result.v1"


class DeliveryMissionGateError(RuntimeError):
    """Raised when delivery mission scorecard/gate artifacts cannot be built."""


class DeliveryMissionGateStatus(str, Enum):
    PASSED = "passed"
    WARNING = "warning"
    BLOCKED = "blocked"


class DeliveryMissionScorecard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_MISSION_SCORECARD_SCHEMA_VERSION] = (
        DELIVERY_MISSION_SCORECARD_SCHEMA_VERSION
    )
    scorecard_id: str
    delivery_mission_contract_id: str
    delivery_mission_id: str
    delivery_mission_policy_review_id: str
    passed: bool
    status: DeliveryMissionGateStatus
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    operator_escalation_required: bool = False
    return_to_home_recommended: bool = False
    abort_recommended: bool = False
    missing_telemetry_measurements: tuple[str, ...] = ()
    battery_percent: float | None = None
    evidence_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    policy_review_snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    delivery_mission_policy_review_schema_version: Literal[
        DELIVERY_MISSION_POLICY_REVIEW_SCHEMA_VERSION
    ] = DELIVERY_MISSION_POLICY_REVIEW_SCHEMA_VERSION
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    stronger_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeliveryMissionGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_MISSION_GATE_RESULT_SCHEMA_VERSION] = (
        DELIVERY_MISSION_GATE_RESULT_SCHEMA_VERSION
    )
    gate_id: str
    delivery_mission_contract_id: str
    delivery_mission_id: str
    delivery_mission_policy_review_id: str
    delivery_mission_scorecard_id: str
    passed: bool
    status: DeliveryMissionGateStatus
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    operator_escalation_required: bool = False
    return_to_home_recommended: bool = False
    abort_recommended: bool = False
    evidence_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    scorecard_snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    stronger_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    return tuple(sorted({str(item).strip() for item in (values or ()) if str(item).strip()}))


def _to_contract(value: DeliveryMissionContract | Mapping[str, Any]) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_policy_review(
    value: DeliveryMissionPolicyReview | Mapping[str, Any],
) -> DeliveryMissionPolicyReview:
    if isinstance(value, DeliveryMissionPolicyReview):
        return value
    return DeliveryMissionPolicyReview.model_validate(dict(value))


def _status_for_review(
    policy_review: DeliveryMissionPolicyReview,
) -> DeliveryMissionGateStatus:
    if policy_review.status is DeliveryMissionPolicyReviewStatus.BLOCKED:
        return DeliveryMissionGateStatus.BLOCKED
    if policy_review.warning_reasons or policy_review.return_to_home_recommended:
        return DeliveryMissionGateStatus.WARNING
    return DeliveryMissionGateStatus.PASSED


def _source_refs(
    *,
    contract: DeliveryMissionContract,
    policy_review: DeliveryMissionPolicyReview,
    extra_source_refs: Sequence[str] | None,
) -> tuple[str, ...]:
    refs = [
        f"delivery_mission_contract:{contract.contract_id}",
        f"delivery_mission_policy_review:{policy_review.review_id}",
    ]
    if policy_review.sanitized_telemetry_id:
        refs.append(f"px4_gazebo_sanitized_telemetry:{policy_review.sanitized_telemetry_id}")
    if policy_review.hil_telemetry_review_id:
        refs.append(f"hil_telemetry_review:{policy_review.hil_telemetry_review_id}")
    refs.extend(extra_source_refs or ())
    return _as_tuple(refs)


def build_delivery_mission_scorecard(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    delivery_mission_policy_review: DeliveryMissionPolicyReview | Mapping[str, Any],
    evidence_refs: Sequence[str] | None = None,
    source_refs: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> DeliveryMissionScorecard:
    """Build a rule-based scorecard from delivery policy review output."""

    contract = _to_contract(delivery_mission_contract)
    policy_review = _to_policy_review(delivery_mission_policy_review)
    if policy_review.delivery_mission_contract_id != contract.contract_id:
        raise DeliveryMissionGateError("delivery policy review contract_id mismatch")
    if policy_review.delivery_mission_id != contract.mission_id:
        raise DeliveryMissionGateError("delivery policy review mission_id mismatch")

    created_at = _utc(now)
    status = _status_for_review(policy_review)
    passed = status is not DeliveryMissionGateStatus.BLOCKED
    blocked_reasons = _as_tuple(policy_review.blocked_reasons)
    warning_reasons = _as_tuple(policy_review.warning_reasons)
    refs = _as_tuple(evidence_refs)
    sources = _source_refs(
        contract=contract,
        policy_review=policy_review,
        extra_source_refs=source_refs,
    )
    payload = {
        "delivery_mission_contract_id": contract.contract_id,
        "delivery_mission_policy_review_id": policy_review.review_id,
        "blocked_reasons": blocked_reasons,
        "warning_reasons": warning_reasons,
        "operator_escalation_required": policy_review.operator_escalation_required,
        "return_to_home_recommended": policy_review.return_to_home_recommended,
        "abort_recommended": policy_review.abort_recommended,
        "evidence_refs": refs,
        "source_refs": sources,
    }
    return DeliveryMissionScorecard(
        scorecard_id=_stable_id("delivery_mission_scorecard", payload),
        delivery_mission_contract_id=contract.contract_id,
        delivery_mission_id=contract.mission_id,
        delivery_mission_policy_review_id=policy_review.review_id,
        passed=passed,
        status=status,
        blocked_reasons=blocked_reasons,
        warning_reasons=warning_reasons,
        operator_escalation_required=policy_review.operator_escalation_required,
        return_to_home_recommended=policy_review.return_to_home_recommended,
        abort_recommended=policy_review.abort_recommended,
        missing_telemetry_measurements=policy_review.missing_telemetry_measurements,
        battery_percent=policy_review.battery_percent,
        evidence_refs=refs,
        source_refs=sources,
        policy_review_snapshot=policy_review.model_dump(mode="json"),
        created_at=created_at,
        metadata={
            **(metadata or {}),
            "artifact_only": True,
            "scorecard_only": True,
            "return_to_home_is_recommendation_only": True,
            "abort_is_recommendation_only": True,
            "no_dispatch_surface": True,
        },
    )


def build_delivery_mission_gate_result(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    delivery_mission_policy_review: DeliveryMissionPolicyReview | Mapping[str, Any],
    delivery_mission_scorecard: DeliveryMissionScorecard | Mapping[str, Any] | None = None,
    evidence_refs: Sequence[str] | None = None,
    source_refs: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> DeliveryMissionGateResult:
    """Build the aggregate delivery gate result.

    ``passed=True`` with ``status=warning`` still does not authorize return-home
    or abort commands; recommendations remain policy findings only.
    """

    contract = _to_contract(delivery_mission_contract)
    policy_review = _to_policy_review(delivery_mission_policy_review)
    created_at = _utc(now)
    scorecard = (
        DeliveryMissionScorecard.model_validate(delivery_mission_scorecard)
        if delivery_mission_scorecard is not None
        else build_delivery_mission_scorecard(
            delivery_mission_contract=contract,
            delivery_mission_policy_review=policy_review,
            evidence_refs=evidence_refs,
            source_refs=source_refs,
            now=created_at,
            metadata=metadata,
        )
    )
    if scorecard.delivery_mission_contract_id != contract.contract_id:
        raise DeliveryMissionGateError("delivery scorecard contract_id mismatch")
    if scorecard.delivery_mission_policy_review_id != policy_review.review_id:
        raise DeliveryMissionGateError("delivery scorecard policy_review_id mismatch")

    payload = {
        "delivery_mission_contract_id": contract.contract_id,
        "delivery_mission_policy_review_id": policy_review.review_id,
        "delivery_mission_scorecard_id": scorecard.scorecard_id,
        "blocked_reasons": scorecard.blocked_reasons,
        "warning_reasons": scorecard.warning_reasons,
        "operator_escalation_required": scorecard.operator_escalation_required,
        "return_to_home_recommended": scorecard.return_to_home_recommended,
        "abort_recommended": scorecard.abort_recommended,
    }
    return DeliveryMissionGateResult(
        gate_id=_stable_id("delivery_mission_gate", payload),
        delivery_mission_contract_id=contract.contract_id,
        delivery_mission_id=contract.mission_id,
        delivery_mission_policy_review_id=policy_review.review_id,
        delivery_mission_scorecard_id=scorecard.scorecard_id,
        passed=scorecard.passed,
        status=scorecard.status,
        blocked_reasons=scorecard.blocked_reasons,
        warning_reasons=scorecard.warning_reasons,
        operator_escalation_required=scorecard.operator_escalation_required,
        return_to_home_recommended=scorecard.return_to_home_recommended,
        abort_recommended=scorecard.abort_recommended,
        evidence_refs=scorecard.evidence_refs,
        source_refs=scorecard.source_refs,
        scorecard_snapshot=scorecard.model_dump(mode="json"),
        created_at=created_at,
        metadata={
            **(metadata or {}),
            "artifact_only": True,
            "gate_only": True,
            "return_to_home_is_recommendation_only": True,
            "abort_is_recommendation_only": True,
            "no_dispatch_surface": True,
        },
    )


def build_delivery_mission_gate_artifacts(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    delivery_mission_policy_review: DeliveryMissionPolicyReview | Mapping[str, Any],
    evidence_refs: Sequence[str] | None = None,
    source_refs: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build scorecard + gate artifacts together."""

    created_at = _utc(now)
    scorecard = build_delivery_mission_scorecard(
        delivery_mission_contract=delivery_mission_contract,
        delivery_mission_policy_review=delivery_mission_policy_review,
        evidence_refs=evidence_refs,
        source_refs=source_refs,
        now=created_at,
        metadata=metadata,
    )
    gate = build_delivery_mission_gate_result(
        delivery_mission_contract=delivery_mission_contract,
        delivery_mission_policy_review=delivery_mission_policy_review,
        delivery_mission_scorecard=scorecard,
        now=created_at,
        metadata=metadata,
    )
    return {
        "delivery_mission_scorecard": scorecard.model_dump(mode="json"),
        "delivery_mission_gate_result": gate.model_dump(mode="json"),
    }


def attach_delivery_mission_gate_artifacts(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    delivery_mission_policy_review: DeliveryMissionPolicyReview | Mapping[str, Any],
    evidence_refs: Sequence[str] | None = None,
    source_refs: Sequence[str] | None = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach delivery scorecard/gate artifacts without mutating task status."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise DeliveryMissionGateError(
            f"task {task_id} not found; cannot attach delivery mission gate artifacts"
        )
    artifacts = build_delivery_mission_gate_artifacts(
        delivery_mission_contract=delivery_mission_contract,
        delivery_mission_policy_review=delivery_mission_policy_review,
        evidence_refs=evidence_refs,
        source_refs=source_refs,
        now=now,
    )
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise DeliveryMissionGateError(
            f"task {task_id} disappeared while attaching delivery mission gate artifacts"
        )
    return artifacts


__all__ = [
    "DELIVERY_MISSION_GATE_RESULT_SCHEMA_VERSION",
    "DELIVERY_MISSION_SCORECARD_SCHEMA_VERSION",
    "DeliveryMissionGateError",
    "DeliveryMissionGateResult",
    "DeliveryMissionGateStatus",
    "DeliveryMissionScorecard",
    "attach_delivery_mission_gate_artifacts",
    "build_delivery_mission_gate_artifacts",
    "build_delivery_mission_gate_result",
    "build_delivery_mission_scorecard",
]
