"""10th-stage readiness checklist for limited live action.

This module is still not a live action path. It turns a completed limited live
action rehearsal into an organization-review checklist and keeps live execution
blocked. The checklist exists to make the remaining 10合目 preconditions
explicit before any future adopting organization considers a constrained,
auditable, reversible limited live action.

Out of scope
------------

- approval completion
- live robot control
- ROS / MAVLink dispatch
- actuator execution
- command payload execution
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

from src.runtime.limited_live_action_rehearsal import (
    LIMITED_LIVE_ACTION_REHEARSAL_SCHEMA_VERSION,
    LimitedLiveActionRehearsal,
    LimitedLiveActionRehearsalStatus,
)
from src.runtime.task_store import TaskStore, get_task_store


TENTH_STAGE_READINESS_CHECK_SCHEMA_VERSION = "tenth_stage_readiness_check.v1"

REQUIRED_TENTH_STAGE_READINESS_PRECONDITIONS: tuple[str, ...] = (
    "limited_live_action_rehearsal_ref",
    "limited_live_action_rehearsal_ready_for_operator_review",
    "mission_contract_ref",
    "autonomy_gate_result_ref",
    "hil_telemetry_review_ref",
    "limited_live_action_gate_ref",
    "limited_live_action_approval_package_ref",
    "emergency_stop_evidence_ref",
    "rollback_plan_ref",
    "operator_responsibility_ack_ref",
    "audit_refs",
    "adopting_organization_ref",
    "hardware_owner_ref",
    "certified_or_autopilot_controller_ref",
    "emergency_stop_process_ref",
)


class TenthStageReadinessStatus(str, Enum):
    READY_FOR_ORGANIZATION_REVIEW = "ready_for_organization_review"
    BLOCKED = "blocked"


class TenthStageLiveActionStatus(str, Enum):
    BLOCKED_FOR_LIVE_ACTION = "blocked_for_live_action"


class TenthStageReadinessError(ValueError):
    """Raised when a 10th-stage readiness check cannot be built or attached."""


class TenthStageReadinessCheck(BaseModel):
    """Organization-review checklist before any future limited live action.

    ``ready_for_organization_review`` means the evidence and responsibility
    refs are complete enough for a future adopting organization to inspect. It
    does not mean approval has been performed, dispatch exists, or live physical
    action is allowed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TENTH_STAGE_READINESS_CHECK_SCHEMA_VERSION] = (
        TENTH_STAGE_READINESS_CHECK_SCHEMA_VERSION
    )
    check_id: str
    limited_live_action_rehearsal_ref: str | None = None
    mission_contract_ref: str | None = None
    autonomy_gate_result_ref: str | None = None
    hil_telemetry_review_ref: str | None = None
    limited_live_action_gate_ref: str | None = None
    limited_live_action_approval_package_ref: str | None = None
    emergency_stop_evidence_ref: str | None = None
    rollback_plan_ref: str | None = None
    operator_responsibility_ack_ref: str | None = None
    audit_refs: tuple[str, ...] = ()
    adopting_organization_ref: str | None = None
    hardware_owner_ref: str | None = None
    certified_or_autopilot_controller_ref: str | None = None
    emergency_stop_process_ref: str | None = None
    readiness_status: TenthStageReadinessStatus
    live_action_status: Literal[TenthStageLiveActionStatus.BLOCKED_FOR_LIVE_ACTION] = (
        TenthStageLiveActionStatus.BLOCKED_FOR_LIVE_ACTION
    )
    missing_preconditions: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    live_action_blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    rehearsal_snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    organization_review_required: Literal[True] = True
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
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False

    @model_validator(mode="after")
    def _validate_consistency(self) -> "TenthStageReadinessCheck":
        if self.readiness_status is TenthStageReadinessStatus.READY_FOR_ORGANIZATION_REVIEW:
            if self.missing_preconditions:
                raise ValueError("ready check cannot have missing_preconditions")
            if self.blocked_reasons:
                raise ValueError("ready check cannot have blocked_reasons")
        else:
            if not self.blocked_reasons:
                raise ValueError("blocked check must include blocked_reasons")
        if not self.live_action_blocked_reasons:
            raise ValueError("10th-stage check must keep live action blocked")
        for field_name in (
            "missing_preconditions",
            "blocked_reasons",
            "live_action_blocked_reasons",
            "warning_reasons",
            "evidence_refs",
            "audit_refs",
        ):
            values = getattr(self, field_name)
            if tuple(sorted(values)) != values:
                raise ValueError(f"{field_name} must be sorted")
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


def _normalize_rehearsal(
    value: LimitedLiveActionRehearsal | Mapping[str, Any],
) -> LimitedLiveActionRehearsal:
    if isinstance(value, LimitedLiveActionRehearsal):
        return value
    if isinstance(value, Mapping):
        return LimitedLiveActionRehearsal.model_validate(dict(value))
    raise TenthStageReadinessError(
        "limited_live_action_rehearsal must be LimitedLiveActionRehearsal or dict"
    )


def _collect_missing(
    *,
    rehearsal: LimitedLiveActionRehearsal,
    limited_live_action_rehearsal_ref: str | None,
    mission_contract_ref: str | None,
    autonomy_gate_result_ref: str | None,
    hil_telemetry_review_ref: str | None,
    limited_live_action_gate_ref: str | None,
    limited_live_action_approval_package_ref: str | None,
    emergency_stop_evidence_ref: str | None,
    rollback_plan_ref: str | None,
    operator_responsibility_ack_ref: str | None,
    audit_refs: tuple[str, ...],
    adopting_organization_ref: str | None,
    hardware_owner_ref: str | None,
    certified_or_autopilot_controller_ref: str | None,
    emergency_stop_process_ref: str | None,
) -> tuple[str, ...]:
    missing: list[str] = []
    if not limited_live_action_rehearsal_ref:
        missing.append("limited_live_action_rehearsal_ref")
    if (
        rehearsal.schema_version != LIMITED_LIVE_ACTION_REHEARSAL_SCHEMA_VERSION
        or rehearsal.readiness_status
        is not LimitedLiveActionRehearsalStatus.READY_FOR_OPERATOR_REVIEW
    ):
        missing.append("limited_live_action_rehearsal_ready_for_operator_review")
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
    if not adopting_organization_ref:
        missing.append("adopting_organization_ref")
    if not hardware_owner_ref:
        missing.append("hardware_owner_ref")
    if not certified_or_autopilot_controller_ref:
        missing.append("certified_or_autopilot_controller_ref")
    if not emergency_stop_process_ref:
        missing.append("emergency_stop_process_ref")
    return tuple(sorted(set(missing)))


def build_tenth_stage_readiness_check(
    *,
    limited_live_action_rehearsal: LimitedLiveActionRehearsal | Mapping[str, Any],
    adopting_organization_ref: str | None = None,
    hardware_owner_ref: str | None = None,
    certified_or_autopilot_controller_ref: str | None = None,
    emergency_stop_process_ref: str | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> TenthStageReadinessCheck:
    """Build a 10th-stage readiness checklist.

    A complete checklist can become ``ready_for_organization_review``. Live
    action remains blocked because operator approval is not performed here and
    no dispatch implementation exists.
    """

    rehearsal = _normalize_rehearsal(limited_live_action_rehearsal)
    created_at = _utc(now)
    rehearsal_payload = rehearsal.model_dump(mode="json")
    rehearsal_ref = _as_ref(rehearsal.rehearsal_id)
    mission_ref = _as_ref(rehearsal.mission_contract_ref)
    autonomy_ref = _as_ref(rehearsal.autonomy_gate_result_ref)
    hil_ref = _as_ref(rehearsal.hil_telemetry_review_ref)
    gate_ref = _as_ref(rehearsal.limited_live_action_gate_ref)
    approval_package_ref = _as_ref(rehearsal.limited_live_action_approval_package_ref)
    emergency_ref = _as_ref(rehearsal.emergency_stop_evidence_ref)
    rollback_ref = _as_ref(rehearsal.rollback_plan_ref)
    responsibility_ref = _as_ref(rehearsal.operator_responsibility_ack_ref)
    audit_ref_tuple = _as_tuple(rehearsal.audit_refs)
    organization_ref = _as_ref(adopting_organization_ref)
    owner_ref = _as_ref(hardware_owner_ref)
    controller_ref = _as_ref(certified_or_autopilot_controller_ref)
    stop_process_ref = _as_ref(emergency_stop_process_ref)
    missing = _collect_missing(
        rehearsal=rehearsal,
        limited_live_action_rehearsal_ref=rehearsal_ref,
        mission_contract_ref=mission_ref,
        autonomy_gate_result_ref=autonomy_ref,
        hil_telemetry_review_ref=hil_ref,
        limited_live_action_gate_ref=gate_ref,
        limited_live_action_approval_package_ref=approval_package_ref,
        emergency_stop_evidence_ref=emergency_ref,
        rollback_plan_ref=rollback_ref,
        operator_responsibility_ack_ref=responsibility_ref,
        audit_refs=audit_ref_tuple,
        adopting_organization_ref=organization_ref,
        hardware_owner_ref=owner_ref,
        certified_or_autopilot_controller_ref=controller_ref,
        emergency_stop_process_ref=stop_process_ref,
    )
    ready = not missing
    refs = tuple(
        sorted(
            {
                item
                for item in (
                    rehearsal_ref,
                    mission_ref,
                    autonomy_ref,
                    hil_ref,
                    gate_ref,
                    approval_package_ref,
                    emergency_ref,
                    rollback_ref,
                    responsibility_ref,
                    organization_ref,
                    owner_ref,
                    controller_ref,
                    stop_process_ref,
                    *audit_ref_tuple,
                )
                if item
            }
        )
    )
    blocked_reasons = tuple(f"missing_precondition:{item}" for item in missing)
    live_action_blocked_reasons = tuple(
        sorted(
            {
                "operator_approval_not_performed",
                "live_dispatch_not_implemented",
                "adopting_organization_must_control_execution_environment",
            }
        )
    )
    warning_reasons = tuple(
        sorted(
            {
                "readiness_check_only_no_dispatch",
                "operator_approval_required_before_any_limited_live_action",
            }
        )
    )
    metadata_payload = dict(metadata or {})
    metadata_payload.update(
        {
            "artifact_only": True,
            "checklist_only": True,
            "tenth_stage_candidate": True,
            "organization_review_only": True,
            "approval_created": False,
            "promotion_created": False,
            "runtime_reuse_created": False,
            "dispatch_surface_added": False,
            "schema_slice": "#173",
        }
    )
    return TenthStageReadinessCheck(
        check_id=_stable_id(
            "tenth_stage_readiness_check",
            {
                "limited_live_action_rehearsal_ref": rehearsal_ref,
                "evidence_refs": refs,
                "missing_preconditions": missing,
            },
        ),
        limited_live_action_rehearsal_ref=rehearsal_ref,
        mission_contract_ref=mission_ref,
        autonomy_gate_result_ref=autonomy_ref,
        hil_telemetry_review_ref=hil_ref,
        limited_live_action_gate_ref=gate_ref,
        limited_live_action_approval_package_ref=approval_package_ref,
        emergency_stop_evidence_ref=emergency_ref,
        rollback_plan_ref=rollback_ref,
        operator_responsibility_ack_ref=responsibility_ref,
        audit_refs=audit_ref_tuple,
        adopting_organization_ref=organization_ref,
        hardware_owner_ref=owner_ref,
        certified_or_autopilot_controller_ref=controller_ref,
        emergency_stop_process_ref=stop_process_ref,
        readiness_status=(
            TenthStageReadinessStatus.READY_FOR_ORGANIZATION_REVIEW
            if ready
            else TenthStageReadinessStatus.BLOCKED
        ),
        missing_preconditions=missing,
        blocked_reasons=blocked_reasons,
        live_action_blocked_reasons=live_action_blocked_reasons,
        warning_reasons=warning_reasons,
        evidence_refs=refs,
        rehearsal_snapshot=rehearsal_payload,
        created_at=created_at,
        metadata=metadata_payload,
    )


def attach_tenth_stage_readiness_check(
    task_id: str,
    readiness_check: TenthStageReadinessCheck | Mapping[str, Any],
    *,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach a 10th-stage readiness checklist to an existing task.

    This only merges ``tenth_stage_readiness_check`` into task artifacts. It
    preserves task status and existing artifacts and does not create approvals,
    promotion artifacts, runtime reuse artifacts, command payloads, or dispatch
    state.
    """

    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise TenthStageReadinessError(
            f"task {task_id} not found in task store; cannot attach readiness check"
        )
    normalized = (
        readiness_check
        if isinstance(readiness_check, TenthStageReadinessCheck)
        else TenthStageReadinessCheck.model_validate(dict(readiness_check))
    )
    artifacts = {
        "tenth_stage_readiness_check": normalized.model_dump(mode="json"),
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise TenthStageReadinessError(
            f"task {task_id} disappeared while attaching readiness check"
        )
    return artifacts


__all__ = [
    "REQUIRED_TENTH_STAGE_READINESS_PRECONDITIONS",
    "TENTH_STAGE_READINESS_CHECK_SCHEMA_VERSION",
    "TenthStageLiveActionStatus",
    "TenthStageReadinessCheck",
    "TenthStageReadinessError",
    "TenthStageReadinessStatus",
    "attach_tenth_stage_readiness_check",
    "build_tenth_stage_readiness_check",
]
