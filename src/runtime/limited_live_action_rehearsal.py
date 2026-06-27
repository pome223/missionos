"""Limited live action rehearsal package.

This module assembles the final dry-run / evidence package before any future
operator-approved limited live physical action could be considered. It is still
not a dispatch path. A rehearsal can become ``ready_for_operator_review`` only
when the limited live action gate and approval package are ready and all
required evidence refs are present.

Out of scope
------------

- live robot control
- ROS / MAVLink dispatch
- actuator execution
- command payload execution
- approval completion
- runtime reuse or promotion
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.runtime.limited_live_action_gate import (
    LIMITED_LIVE_ACTION_APPROVAL_PACKAGE_SCHEMA_VERSION,
    LIMITED_LIVE_ACTION_GATE_SCHEMA_VERSION,
    LimitedLiveActionApprovalPackage,
    LimitedLiveActionGateResult,
    LimitedLiveActionGateStatus,
)
from src.runtime.task_store import TaskStore, get_task_store


LIMITED_LIVE_ACTION_REHEARSAL_SCHEMA_VERSION = "limited_live_action_rehearsal.v1"

REQUIRED_LIMITED_LIVE_ACTION_REHEARSAL_PRECONDITIONS: tuple[str, ...] = (
    "mission_contract_ref",
    "autonomy_gate_result_ref",
    "hil_telemetry_review_ref",
    "limited_live_action_gate_ref",
    "limited_live_action_approval_package_ref",
    "emergency_stop_evidence_ref",
    "rollback_plan_ref",
    "operator_responsibility_ack_ref",
    "audit_refs",
)


class LimitedLiveActionRehearsalStatus(str, Enum):
    READY_FOR_OPERATOR_REVIEW = "ready_for_operator_review"
    BLOCKED = "blocked"


class LimitedLiveActionRehearsalError(ValueError):
    """Raised when a limited live action rehearsal cannot be built or attached."""


class LimitedLiveActionRehearsal(BaseModel):
    """Final dry-run evidence package before future limited-live review.

    ``ready_for_operator_review`` means the evidence bundle is complete enough
    for human review. It never means live execution, dispatch, approval, or
    physical invocation is permitted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[LIMITED_LIVE_ACTION_REHEARSAL_SCHEMA_VERSION] = (
        LIMITED_LIVE_ACTION_REHEARSAL_SCHEMA_VERSION
    )
    rehearsal_id: str
    mission_contract_ref: str | None = None
    autonomy_gate_result_ref: str | None = None
    hil_telemetry_review_ref: str | None = None
    limited_live_action_gate_ref: str | None = None
    limited_live_action_approval_package_ref: str | None = None
    emergency_stop_evidence_ref: str | None = None
    rollback_plan_ref: str | None = None
    operator_responsibility_ack_ref: str | None = None
    audit_refs: tuple[str, ...] = ()
    readiness_status: LimitedLiveActionRehearsalStatus
    missing_preconditions: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    gate_snapshot: dict[str, Any] = Field(default_factory=dict)
    approval_package_snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    stronger_execution_allowed: Literal[False] = False
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False

    @model_validator(mode="after")
    def _validate_consistency(self) -> "LimitedLiveActionRehearsal":
        if (
            self.readiness_status
            is LimitedLiveActionRehearsalStatus.READY_FOR_OPERATOR_REVIEW
        ):
            if self.missing_preconditions:
                raise ValueError("ready rehearsal cannot have missing_preconditions")
            if self.blocked_reasons:
                raise ValueError("ready rehearsal cannot have blocked_reasons")
        else:
            if not self.blocked_reasons:
                raise ValueError("blocked rehearsal must include blocked_reasons")
        if tuple(sorted(self.missing_preconditions)) != self.missing_preconditions:
            raise ValueError("missing_preconditions must be sorted")
        if tuple(sorted(self.evidence_refs)) != self.evidence_refs:
            raise ValueError("evidence_refs must be sorted")
        if tuple(sorted(self.audit_refs)) != self.audit_refs:
            raise ValueError("audit_refs must be sorted")
        return self


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


def _as_ref(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _as_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(sorted({str(item).strip() for item in (values or ()) if str(item).strip()}))


def _normalize_gate(
    value: LimitedLiveActionGateResult | Mapping[str, Any],
) -> LimitedLiveActionGateResult:
    if isinstance(value, LimitedLiveActionGateResult):
        return value
    if isinstance(value, Mapping):
        return LimitedLiveActionGateResult.model_validate(dict(value))
    raise LimitedLiveActionRehearsalError(
        "limited_live_action_gate must be LimitedLiveActionGateResult or dict"
    )


def _normalize_approval_package(
    value: LimitedLiveActionApprovalPackage | Mapping[str, Any],
) -> LimitedLiveActionApprovalPackage:
    if isinstance(value, LimitedLiveActionApprovalPackage):
        return value
    if isinstance(value, Mapping):
        return LimitedLiveActionApprovalPackage.model_validate(dict(value))
    raise LimitedLiveActionRehearsalError(
        "limited_live_action_approval_package must be "
        "LimitedLiveActionApprovalPackage or dict"
    )


def _ref_or_snapshot_id(snapshot: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _as_ref(snapshot.get(key))
        if value:
            return value
    return None


def _collect_missing(
    *,
    mission_contract_ref: str | None,
    autonomy_gate_result_ref: str | None,
    hil_telemetry_review_ref: str | None,
    limited_live_action_gate_ref: str | None,
    limited_live_action_approval_package_ref: str | None,
    emergency_stop_evidence_ref: str | None,
    rollback_plan_ref: str | None,
    operator_responsibility_ack_ref: str | None,
    audit_refs: tuple[str, ...],
    gate: LimitedLiveActionGateResult,
    approval_package: LimitedLiveActionApprovalPackage,
) -> tuple[str, ...]:
    missing: list[str] = []
    if not mission_contract_ref:
        missing.append("mission_contract_ref")
    if not autonomy_gate_result_ref:
        missing.append("autonomy_gate_result_ref")
    if not hil_telemetry_review_ref:
        missing.append("hil_telemetry_review_ref")
    if not limited_live_action_gate_ref:
        missing.append("limited_live_action_gate_ref")
    if not limited_live_action_approval_package_ref:
        missing.append("limited_live_action_approval_package_ref")
    if not emergency_stop_evidence_ref:
        missing.append("emergency_stop_evidence_ref")
    if not rollback_plan_ref:
        missing.append("rollback_plan_ref")
    if not operator_responsibility_ack_ref:
        missing.append("operator_responsibility_ack_ref")
    if not audit_refs:
        missing.append("audit_refs")
    if (
        gate.schema_version != LIMITED_LIVE_ACTION_GATE_SCHEMA_VERSION
        or gate.status is not LimitedLiveActionGateStatus.OPERATOR_REVIEW_READY
        or not gate.passed
    ):
        missing.append("limited_live_action_gate_operator_review_ready")
    if (
        approval_package.schema_version
        != LIMITED_LIVE_ACTION_APPROVAL_PACKAGE_SCHEMA_VERSION
        or not approval_package.operator_approval_required
        or approval_package.operator_approval_performed
        or approval_package.approval_ref is not None
        or not approval_package.required_evidence_refs
    ):
        missing.append("limited_live_action_approval_package_ready")
    return tuple(sorted(set(missing)))


def build_limited_live_action_rehearsal(
    *,
    limited_live_action_gate: LimitedLiveActionGateResult | Mapping[str, Any],
    limited_live_action_approval_package: LimitedLiveActionApprovalPackage
    | Mapping[str, Any]
    | None = None,
    mission_contract_ref: str | None = None,
    autonomy_gate_result_ref: str | None = None,
    hil_telemetry_review_ref: str | None = None,
    emergency_stop_evidence_ref: str | None = None,
    rollback_plan_ref: str | None = None,
    operator_responsibility_ack_ref: str | None = None,
    audit_refs: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> LimitedLiveActionRehearsal:
    """Build a dry-run rehearsal/evidence package.

    The rehearsal is ready only when all evidence refs are present and the
    upstream gate is ``operator_review_ready``. It never performs approval or
    enables live / physical / dispatch execution.
    """

    gate = _normalize_gate(limited_live_action_gate)
    approval_package = (
        _normalize_approval_package(limited_live_action_approval_package)
        if limited_live_action_approval_package is not None
        else gate.approval_package
    )
    if approval_package.subject_id != gate.subject_id:
        raise LimitedLiveActionRehearsalError(
            "limited live action approval package subject_id mismatch"
        )
    if approval_package.proposed_action_ref != gate.proposed_action_ref:
        raise LimitedLiveActionRehearsalError(
            "limited live action approval package proposed_action_ref mismatch"
        )

    created_at = _utc(now)
    gate_payload = gate.model_dump(mode="json")
    approval_payload = approval_package.model_dump(mode="json")
    gate_ref = _as_ref(_ref_or_snapshot_id(gate_payload, "gate_id"))
    approval_ref = _as_ref(
        _ref_or_snapshot_id(approval_payload, "approval_package_id")
    )
    mission_ref = _as_ref(mission_contract_ref)
    autonomy_ref = _as_ref(autonomy_gate_result_ref) or (
        gate.autonomy_gate_result_refs[0] if gate.autonomy_gate_result_refs else None
    )
    hil_ref = _as_ref(hil_telemetry_review_ref) or (
        gate.hil_telemetry_review_refs[0] if gate.hil_telemetry_review_refs else None
    )
    emergency_ref = _as_ref(emergency_stop_evidence_ref) or (
        gate.emergency_stop_evidence_refs[0]
        if gate.emergency_stop_evidence_refs
        else None
    )
    rollback_ref = _as_ref(rollback_plan_ref) or (
        gate.rollback_plan_refs[0] if gate.rollback_plan_refs else None
    )
    responsibility_ref = _as_ref(operator_responsibility_ack_ref) or (
        gate.responsibility_ack_refs[0] if gate.responsibility_ack_refs else None
    )
    audit_ref_tuple = _as_tuple(audit_refs) or gate.audit_refs
    missing = _collect_missing(
        mission_contract_ref=mission_ref,
        autonomy_gate_result_ref=autonomy_ref,
        hil_telemetry_review_ref=hil_ref,
        limited_live_action_gate_ref=gate_ref,
        limited_live_action_approval_package_ref=approval_ref,
        emergency_stop_evidence_ref=emergency_ref,
        rollback_plan_ref=rollback_ref,
        operator_responsibility_ack_ref=responsibility_ref,
        audit_refs=audit_ref_tuple,
        gate=gate,
        approval_package=approval_package,
    )
    ready = not missing
    refs = tuple(
        sorted(
            {
                item
                for item in (
                    mission_ref,
                    autonomy_ref,
                    hil_ref,
                    gate_ref,
                    approval_ref,
                    emergency_ref,
                    rollback_ref,
                    responsibility_ref,
                    *audit_ref_tuple,
                )
                if item
            }
        )
    )
    blocked_reasons = tuple(f"missing_precondition:{item}" for item in missing)
    warning_reasons = (
        "rehearsal_only_no_dispatch",
        "operator_approval_required_before_any_stronger_execution",
    )
    metadata_payload = dict(metadata or {})
    metadata_payload.update(
        {
            "artifact_only": True,
            "rehearsal_only": True,
            "final_dry_run_before_operator_review": True,
            "dispatch_surface_added": False,
            "approval_created": False,
            "promotion_created": False,
            "runtime_reuse_created": False,
            "schema_slice": "#173",
        }
    )
    return LimitedLiveActionRehearsal(
        rehearsal_id=_stable_id(
            "limited_live_action_rehearsal",
            {
                "subject_id": gate.subject_id,
                "proposed_action_ref": gate.proposed_action_ref,
                "evidence_refs": refs,
                "missing_preconditions": missing,
            },
        ),
        mission_contract_ref=mission_ref,
        autonomy_gate_result_ref=autonomy_ref,
        hil_telemetry_review_ref=hil_ref,
        limited_live_action_gate_ref=gate_ref,
        limited_live_action_approval_package_ref=approval_ref,
        emergency_stop_evidence_ref=emergency_ref,
        rollback_plan_ref=rollback_ref,
        operator_responsibility_ack_ref=responsibility_ref,
        audit_refs=audit_ref_tuple,
        readiness_status=(
            LimitedLiveActionRehearsalStatus.READY_FOR_OPERATOR_REVIEW
            if ready
            else LimitedLiveActionRehearsalStatus.BLOCKED
        ),
        missing_preconditions=missing,
        blocked_reasons=blocked_reasons,
        warning_reasons=warning_reasons,
        evidence_refs=refs,
        gate_snapshot=gate_payload,
        approval_package_snapshot=approval_payload,
        created_at=created_at,
        metadata=metadata_payload,
    )


def attach_limited_live_action_rehearsal(
    task_id: str,
    rehearsal: LimitedLiveActionRehearsal | Mapping[str, Any],
    *,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach a rehearsal artifact to an existing task.

    This only merges ``limited_live_action_rehearsal`` into task artifacts. It
    preserves task status and existing artifacts and does not create approvals,
    promotion artifacts, runtime reuse artifacts, command payloads, or dispatch
    state.
    """

    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise LimitedLiveActionRehearsalError(
            f"task {task_id} not found in task store; cannot attach rehearsal"
        )
    normalized = (
        rehearsal
        if isinstance(rehearsal, LimitedLiveActionRehearsal)
        else LimitedLiveActionRehearsal.model_validate(dict(rehearsal))
    )
    artifacts = {
        "limited_live_action_rehearsal": normalized.model_dump(mode="json"),
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise LimitedLiveActionRehearsalError(
            f"task {task_id} disappeared while attaching rehearsal"
        )
    return artifacts


__all__ = [
    "LIMITED_LIVE_ACTION_REHEARSAL_SCHEMA_VERSION",
    "REQUIRED_LIMITED_LIVE_ACTION_REHEARSAL_PRECONDITIONS",
    "LimitedLiveActionRehearsal",
    "LimitedLiveActionRehearsalError",
    "LimitedLiveActionRehearsalStatus",
    "attach_limited_live_action_rehearsal",
    "build_limited_live_action_rehearsal",
]
