"""Limited live physical action gate — design/schema slice (#173).

This module defines ``limited_live_action_gate.v1``. It is intentionally a
design artifact, not an execution bridge. The gate records whether the evidence
needed for a future limited live physical action review is present, but it never
permits dispatch, actuator execution, ROS/MAVLink publication, or stronger
execution by itself.

The artifact is meant to sit after the current evidence-gated stack:

    toy-grid / HIL evidence
      -> autonomy gate / HIL telemetry review
      -> limited_live_action_gate.v1
      -> explicit future operator review

Out of scope
------------

- live robot control
- ROS / MAVLink dispatch
- actuator execution
- command payload execution
- approval completion
- runtime mission integration
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


LIMITED_LIVE_ACTION_GATE_SCHEMA_VERSION = "limited_live_action_gate.v1"
LIMITED_LIVE_ACTION_APPROVAL_PACKAGE_SCHEMA_VERSION = (
    "limited_live_action_approval_package.v1"
)

REQUIRED_LIMITED_LIVE_ACTION_PRECONDITIONS: tuple[str, ...] = (
    "autonomy_gate_result",
    "hil_telemetry_review",
    "emergency_stop_evidence",
    "rollback_plan",
    "action_allowlist",
    "operator_responsibility_ack",
    "audit_log",
)


class LimitedLiveActionGateStatus(str, Enum):
    BLOCKED = "blocked"
    OPERATOR_REVIEW_READY = "operator_review_ready"


class LimitedLiveActionGateError(ValueError):
    """Raised when a limited live action gate artifact cannot be built."""


class LimitedLiveActionApprovalPackage(BaseModel):
    """Operator-review package for a future limited live action request.

    This package is still not approval. ``operator_approval_performed`` is
    pinned to ``False`` and no execution capability is exposed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[LIMITED_LIVE_ACTION_APPROVAL_PACKAGE_SCHEMA_VERSION] = (
        LIMITED_LIVE_ACTION_APPROVAL_PACKAGE_SCHEMA_VERSION
    )
    approval_package_id: str
    subject_id: str
    proposed_action_ref: str
    required_operator_role: str = "responsible_operator"
    responsibility_summary: str
    required_evidence_refs: tuple[str, ...] = ()
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    approval_ref: None = None
    emergency_stop_required: Literal[True] = True
    rollback_plan_required: Literal[True] = True
    action_allowlist_required: Literal[True] = True
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reject_command_like_metadata(self) -> "LimitedLiveActionApprovalPackage":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class LimitedLiveActionGateResult(BaseModel):
    """Rule-based readiness artifact for future limited live action review.

    ``passed`` only means the evidence package is complete enough for operator
    review. It does **not** permit any stronger execution. Action allowlist refs
    describe proposal categories only; they are not dispatch grants. All live,
    physical, command, dispatch, ROS, MAVLink, and actuator flags are pinned to
    False at the type level.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[LIMITED_LIVE_ACTION_GATE_SCHEMA_VERSION] = (
        LIMITED_LIVE_ACTION_GATE_SCHEMA_VERSION
    )
    gate_id: str
    subject_id: str
    proposed_action_ref: str
    passed: bool
    status: LimitedLiveActionGateStatus
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    required_preconditions: tuple[str, ...] = REQUIRED_LIMITED_LIVE_ACTION_PRECONDITIONS
    missing_preconditions: tuple[str, ...] = ()
    autonomy_gate_result_refs: tuple[str, ...] = ()
    hil_telemetry_review_refs: tuple[str, ...] = ()
    emergency_stop_evidence_refs: tuple[str, ...] = ()
    rollback_plan_refs: tuple[str, ...] = ()
    action_allowlist_refs: tuple[str, ...] = ()
    responsibility_ack_refs: tuple[str, ...] = ()
    audit_refs: tuple[str, ...] = ()
    approval_package: LimitedLiveActionApprovalPackage
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
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_consistency(self) -> "LimitedLiveActionGateResult":
        if self.passed and self.status is not LimitedLiveActionGateStatus.OPERATOR_REVIEW_READY:
            raise ValueError("passed gate must have operator_review_ready status")
        if not self.passed and self.status is not LimitedLiveActionGateStatus.BLOCKED:
            raise ValueError("non-passing gate must have blocked status")
        if self.passed and self.blocked_reasons:
            raise ValueError("passed gate cannot carry blocked_reasons")
        if not self.passed and not self.blocked_reasons:
            raise ValueError("blocked gate must carry at least one blocked reason")
        if tuple(sorted(self.missing_preconditions)) != self.missing_preconditions:
            raise ValueError("missing_preconditions must be sorted for deterministic output")
        _raise_for_command_like_keys(self.metadata, root="metadata")
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


def _as_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(sorted({str(item).strip() for item in (values or ()) if str(item).strip()}))


_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "action",
        "actions",
        "command",
        "commands",
        "dispatch",
        "actuator",
        "actuators",
        "ros_topic",
        "ros2_topic",
        "mavlink_message",
        "motor_command",
        "servo",
        "setpoint",
        "velocity_command",
        "torque_command",
        "execute",
        "execute_now",
        "live_execution_allowed",
        "physical_execution_invoked",
    }
)


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


_FORBIDDEN_METADATA_KEYS_NORMALIZED = frozenset(
    _normalize_key(key) for key in _FORBIDDEN_METADATA_KEYS
)


def _command_like_key_paths(value: Any, *, root: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, sub in value.items():
            key_text = str(key)
            path = f"{root}.{key_text}" if root else key_text
            if _normalize_key(key_text) in _FORBIDDEN_METADATA_KEYS_NORMALIZED:
                findings.append(path)
            findings.extend(_command_like_key_paths(sub, root=path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            path = f"{root}.{index}" if root else str(index)
            findings.extend(_command_like_key_paths(item, root=path))
    return findings


def _raise_for_command_like_keys(value: Any, *, root: str) -> None:
    findings = _command_like_key_paths(value, root=root)
    if findings:
        raise LimitedLiveActionGateError(
            "limited live action gate metadata refused command-like keys: "
            + ", ".join(sorted(findings))
        )


def _missing_preconditions(
    *,
    autonomy_gate_result_refs: tuple[str, ...],
    hil_telemetry_review_refs: tuple[str, ...],
    emergency_stop_evidence_refs: tuple[str, ...],
    rollback_plan_refs: tuple[str, ...],
    action_allowlist_refs: tuple[str, ...],
    responsibility_ack_refs: tuple[str, ...],
    audit_refs: tuple[str, ...],
) -> tuple[str, ...]:
    missing = []
    if not autonomy_gate_result_refs:
        missing.append("autonomy_gate_result")
    if not hil_telemetry_review_refs:
        missing.append("hil_telemetry_review")
    if not emergency_stop_evidence_refs:
        missing.append("emergency_stop_evidence")
    if not rollback_plan_refs:
        missing.append("rollback_plan")
    if not action_allowlist_refs:
        missing.append("action_allowlist")
    if not responsibility_ack_refs:
        missing.append("operator_responsibility_ack")
    if not audit_refs:
        missing.append("audit_log")
    return tuple(sorted(missing))


def build_limited_live_action_gate_result(
    *,
    subject_id: str,
    proposed_action_ref: str,
    autonomy_gate_result_refs: Sequence[str] | None = None,
    hil_telemetry_review_refs: Sequence[str] | None = None,
    emergency_stop_evidence_refs: Sequence[str] | None = None,
    rollback_plan_refs: Sequence[str] | None = None,
    action_allowlist_refs: Sequence[str] | None = None,
    responsibility_ack_refs: Sequence[str] | None = None,
    audit_refs: Sequence[str] | None = None,
    responsibility_summary: str | None = None,
    required_operator_role: str = "responsible_operator",
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> LimitedLiveActionGateResult:
    """Build a limited-live-action readiness gate artifact.

    A fully populated evidence package returns ``passed=True`` with
    ``status=operator_review_ready``. Even then, all stronger execution flags
    remain false; action allowlist refs describe proposal categories only, and a
    future operator approval / dispatch implementation would require a
    different contract.
    """

    if not str(subject_id).strip():
        raise LimitedLiveActionGateError("subject_id is required")
    if not str(proposed_action_ref).strip():
        raise LimitedLiveActionGateError("proposed_action_ref is required")

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")

    created_at = _utc(now)
    autonomy_refs = _as_tuple(autonomy_gate_result_refs)
    hil_refs = _as_tuple(hil_telemetry_review_refs)
    emergency_refs = _as_tuple(emergency_stop_evidence_refs)
    rollback_refs = _as_tuple(rollback_plan_refs)
    allowlist_refs = _as_tuple(action_allowlist_refs)
    responsibility_refs = _as_tuple(responsibility_ack_refs)
    audit_ref_tuple = _as_tuple(audit_refs)
    missing = _missing_preconditions(
        autonomy_gate_result_refs=autonomy_refs,
        hil_telemetry_review_refs=hil_refs,
        emergency_stop_evidence_refs=emergency_refs,
        rollback_plan_refs=rollback_refs,
        action_allowlist_refs=allowlist_refs,
        responsibility_ack_refs=responsibility_refs,
        audit_refs=audit_ref_tuple,
    )
    passed = not missing
    blocked_reasons = tuple(f"missing_precondition:{item}" for item in missing)
    warning_reasons = (
        "operator_approval_required_before_any_stronger_execution",
        "design_only_no_dispatch_implementation",
    )
    all_required_refs = (
        autonomy_refs
        + hil_refs
        + emergency_refs
        + rollback_refs
        + allowlist_refs
        + responsibility_refs
        + audit_ref_tuple
    )
    base_payload = {
        "subject_id": subject_id,
        "proposed_action_ref": proposed_action_ref,
        "required_evidence_refs": all_required_refs,
        "created_at": created_at.isoformat(),
    }
    approval_package = LimitedLiveActionApprovalPackage(
        approval_package_id=_stable_id("limited_live_action_approval_package", base_payload),
        subject_id=subject_id,
        proposed_action_ref=proposed_action_ref,
        required_operator_role=required_operator_role,
        responsibility_summary=(
            responsibility_summary
            or "Operator must verify emergency stop, rollback plan, proposal-category "
            "action allowlist, telemetry freshness, and responsibility boundary "
            "before any stronger execution."
        ),
        required_evidence_refs=all_required_refs,
        created_at=created_at,
        metadata={
            "design_only": True,
            "approval_package_only": True,
            "action_allowlist_scope": "proposal_categories_only",
        },
    )
    return LimitedLiveActionGateResult(
        gate_id=_stable_id(
            "limited_live_action_gate",
            {
                **base_payload,
                "missing_preconditions": missing,
            },
        ),
        subject_id=subject_id,
        proposed_action_ref=proposed_action_ref,
        passed=passed,
        status=(
            LimitedLiveActionGateStatus.OPERATOR_REVIEW_READY
            if passed
            else LimitedLiveActionGateStatus.BLOCKED
        ),
        blocked_reasons=blocked_reasons,
        warning_reasons=warning_reasons,
        missing_preconditions=missing,
        autonomy_gate_result_refs=autonomy_refs,
        hil_telemetry_review_refs=hil_refs,
        emergency_stop_evidence_refs=emergency_refs,
        rollback_plan_refs=rollback_refs,
        action_allowlist_refs=allowlist_refs,
        responsibility_ack_refs=responsibility_refs,
        audit_refs=audit_ref_tuple,
        approval_package=approval_package,
        created_at=created_at,
        metadata={
            **metadata_payload,
            "design_only": True,
            "schema_slice": "#173",
            "approval_package_only": True,
            "action_allowlist_scope": "proposal_categories_only",
            "dispatch_surface_added": False,
        },
    )


__all__ = [
    "LIMITED_LIVE_ACTION_APPROVAL_PACKAGE_SCHEMA_VERSION",
    "LIMITED_LIVE_ACTION_GATE_SCHEMA_VERSION",
    "REQUIRED_LIMITED_LIVE_ACTION_PRECONDITIONS",
    "LimitedLiveActionApprovalPackage",
    "LimitedLiveActionGateError",
    "LimitedLiveActionGateResult",
    "LimitedLiveActionGateStatus",
    "build_limited_live_action_gate_result",
]
