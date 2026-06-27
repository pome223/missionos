"""Simulation-only recovery and golden corpus artifacts for PX4/Gazebo routes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.px4_gazebo_route_delivery import (
    PX4GazeboRouteDeliveryCompletionGate,
    PX4GazeboRouteDeliveryStatus,
)
from src.runtime.px4_gazebo_route_dispatcher import (
    PX4_GAZEBO_ROUTE_COMMAND_DISPATCH_RESULT_SCHEMA_VERSION,
    PX4_GAZEBO_ROUTE_PROGRESS_EVIDENCE_SCHEMA_VERSION,
)
from src.runtime.task_store import TaskStore, get_task_store

PX4_GAZEBO_ROUTE_RECOVERY_PROPOSAL_SCHEMA_VERSION = (
    "px4_gazebo_route_recovery_proposal.v1"
)
PX4_GAZEBO_ROUTE_RECOVERY_APPROVAL_SCHEMA_VERSION = (
    "px4_gazebo_route_recovery_approval.v1"
)
PX4_GAZEBO_ROUTE_RECOVERY_ALLOWLIST_SCHEMA_VERSION = (
    "px4_gazebo_route_recovery_allowlist.v1"
)
PX4_GAZEBO_ROUTE_RECOVERY_DIAGNOSTICS_SCHEMA_VERSION = (
    "px4_gazebo_route_recovery_diagnostics.v1"
)
PX4_GAZEBO_ROUTE_GOLDEN_CORPUS_SCHEMA_VERSION = (
    "px4_gazebo_route_delivery_golden_corpus.v1"
)


class PX4GazeboRouteRecoveryError(RuntimeError):
    """Raised when route recovery evidence is unsafe or inconsistent."""


class PX4GazeboRouteRecoveryAction(str, Enum):
    HOLD = "hold"
    LAND = "land"
    RETURN_TO_LAUNCH = "return_to_launch"


class PX4GazeboRouteRecoveryStatus(str, Enum):
    PROPOSED = "proposed"
    BLOCKED = "blocked"
    ALLOWLISTED = "allowlisted"


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


def _ordered_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in values or ():
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _completion_gate_ref(gate: PX4GazeboRouteDeliveryCompletionGate) -> str:
    return f"px4_gazebo_route_delivery_completion_gate:{gate.completion_gate_id}"


def _proposal_ref(proposal: "PX4GazeboRouteRecoveryProposal") -> str:
    return f"px4_gazebo_route_recovery_proposal:{proposal.proposal_id}"


def _approval_ref(approval: "PX4GazeboRouteRecoveryApproval") -> str:
    return f"px4_gazebo_route_recovery_approval:{approval.approval_id}"


def _coerce_gate(
    value: PX4GazeboRouteDeliveryCompletionGate | Mapping[str, Any],
) -> PX4GazeboRouteDeliveryCompletionGate:
    if isinstance(value, PX4GazeboRouteDeliveryCompletionGate):
        return value
    return PX4GazeboRouteDeliveryCompletionGate.model_validate(dict(value))


class _RouteRecoverySafetyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    bounded_allowlist_enforced: Literal[True] = True
    route_failure_evidence_required: Literal[True] = True
    hardware_target_allowed: Literal[False] = False
    real_world_authority_granted: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    physical_actuator_execution_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    arbitrary_mission_upload_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    approval_free_recovery_dispatch_allowed: Literal[False] = False
    recovery_command_sent: Literal[False] = False
    retry_attempted: Literal[False] = False
    stronger_execution_attempted: Literal[False] = False


class PX4GazeboRouteRecoveryProposal(_RouteRecoverySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_ROUTE_RECOVERY_PROPOSAL_SCHEMA_VERSION] = (
        PX4_GAZEBO_ROUTE_RECOVERY_PROPOSAL_SCHEMA_VERSION
    )
    proposal_id: str
    recovery_status: Literal[PX4GazeboRouteRecoveryStatus.PROPOSED] = (
        PX4GazeboRouteRecoveryStatus.PROPOSED
    )
    completion_gate_ref: str = Field(min_length=1)
    source_blocked_reasons: tuple[str, ...]
    recommended_action: PX4GazeboRouteRecoveryAction
    recommended_action_reason: str = Field(min_length=1)
    recovery_command_dispatch_allowed: Literal[False] = False
    recovery_dispatch_deferred_to_approval: Literal[True] = True
    proposed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_blocked_reasons", mode="before")
    @classmethod
    def _coerce_reasons(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])

    @field_validator("proposed_at", mode="before")
    @classmethod
    def _coerce_proposed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_proposal(self) -> "PX4GazeboRouteRecoveryProposal":
        if not self.source_blocked_reasons:
            raise PX4GazeboRouteRecoveryError(
                "route recovery proposal requires blocked reasons"
            )
        return self


class PX4GazeboRouteRecoveryApproval(_RouteRecoverySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_ROUTE_RECOVERY_APPROVAL_SCHEMA_VERSION] = (
        PX4_GAZEBO_ROUTE_RECOVERY_APPROVAL_SCHEMA_VERSION
    )
    approval_id: str
    proposal_ref: str = Field(min_length=1)
    operator_approval_performed: bool
    approved_recovery_actions: tuple[PX4GazeboRouteRecoveryAction, ...]
    approved_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("approved_recovery_actions", mode="before")
    @classmethod
    def _coerce_actions(cls, value: Any) -> tuple[PX4GazeboRouteRecoveryAction, ...]:
        return tuple(
            (
                item
                if isinstance(item, PX4GazeboRouteRecoveryAction)
                else PX4GazeboRouteRecoveryAction(str(item))
            )
            for item in value
        )

    @field_validator("approved_at", mode="before")
    @classmethod
    def _coerce_approved_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


class PX4GazeboRouteRecoveryAllowlist(_RouteRecoverySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_ROUTE_RECOVERY_ALLOWLIST_SCHEMA_VERSION] = (
        PX4_GAZEBO_ROUTE_RECOVERY_ALLOWLIST_SCHEMA_VERSION
    )
    allowlist_id: str
    proposal_ref: str = Field(min_length=1)
    approval_ref: str = Field(min_length=1)
    operator_approval_performed: Literal[True] = True
    allowed_recovery_actions: tuple[PX4GazeboRouteRecoveryAction, ...]
    recovery_status: Literal[PX4GazeboRouteRecoveryStatus.ALLOWLISTED] = (
        PX4GazeboRouteRecoveryStatus.ALLOWLISTED
    )
    recovery_command_dispatch_allowed: Literal[False] = False
    generated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("allowed_recovery_actions", mode="before")
    @classmethod
    def _coerce_allowed_actions(
        cls, value: Any
    ) -> tuple[PX4GazeboRouteRecoveryAction, ...]:
        return tuple(
            (
                item
                if isinstance(item, PX4GazeboRouteRecoveryAction)
                else PX4GazeboRouteRecoveryAction(str(item))
            )
            for item in value
        )

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_allowlist(self) -> "PX4GazeboRouteRecoveryAllowlist":
        if not self.allowed_recovery_actions:
            raise PX4GazeboRouteRecoveryError(
                "route recovery allowlist requires allowed actions"
            )
        return self


class PX4GazeboRouteRecoveryDiagnostics(_RouteRecoverySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_ROUTE_RECOVERY_DIAGNOSTICS_SCHEMA_VERSION] = (
        PX4_GAZEBO_ROUTE_RECOVERY_DIAGNOSTICS_SCHEMA_VERSION
    )
    diagnostics_id: str
    final_status: Literal["blocked"] = "blocked"
    proposal_ref: str = Field(min_length=1)
    approval_ref: str | None = None
    allowlist_ref: str | None = None
    recovery_unavailable_reason: str = Field(min_length=1)
    blocked_reasons: tuple[str, ...]
    operator_approval_available: bool
    allowlist_available: bool
    recovery_action_allowlisted: bool
    original_failure_evidence_preserved: Literal[True] = True
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_blocked_reasons(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_diagnostics(self) -> "PX4GazeboRouteRecoveryDiagnostics":
        if not self.blocked_reasons:
            raise PX4GazeboRouteRecoveryError(
                "route recovery diagnostics require blocked reasons"
            )
        if self.recovery_unavailable_reason not in self.blocked_reasons:
            raise PX4GazeboRouteRecoveryError(
                "blocked reasons must include the recovery unavailable reason"
            )
        if self.recovery_unavailable_reason == "missing_recovery_approval":
            if self.operator_approval_available:
                raise PX4GazeboRouteRecoveryError(
                    "missing approval diagnostics cannot have approval available"
                )
            if self.recovery_command_sent:
                raise PX4GazeboRouteRecoveryError(
                    "missing approval diagnostics must block before send"
                )
        if self.recovery_unavailable_reason == "missing_recovery_allowlist":
            if self.allowlist_available:
                raise PX4GazeboRouteRecoveryError(
                    "missing allowlist diagnostics cannot have allowlist available"
                )
            if self.recovery_command_sent:
                raise PX4GazeboRouteRecoveryError(
                    "missing allowlist diagnostics must block before send"
                )
        if self.recovery_unavailable_reason == "recovery_action_not_allowlisted":
            if self.recovery_action_allowlisted:
                raise PX4GazeboRouteRecoveryError(
                    "not-allowlisted diagnostics cannot have allowlisted action"
                )
            if self.recovery_command_sent:
                raise PX4GazeboRouteRecoveryError(
                    "not-allowlisted diagnostics must block before send"
                )
        return self


class PX4GazeboRouteGoldenCorpusCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    expected_terminal_status: Literal["completed", "blocked"]
    required_artifact_schema_versions: tuple[str, ...]
    expected_blocked_reasons: tuple[str, ...] = ()
    expected_recovery_completion_basis: str | None = None
    expected_recovery_action: str | None = None
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_recovery_dispatch_allowed: Literal[False] = False

    @field_validator("required_artifact_schema_versions", mode="before")
    @classmethod
    def _coerce_versions(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])

    @field_validator("expected_blocked_reasons", mode="before")
    @classmethod
    def _coerce_expected_blocked(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])


class PX4GazeboRouteGoldenCorpus(_RouteRecoverySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_ROUTE_GOLDEN_CORPUS_SCHEMA_VERSION] = (
        PX4_GAZEBO_ROUTE_GOLDEN_CORPUS_SCHEMA_VERSION
    )
    corpus_id: str
    corpus_cases: tuple[PX4GazeboRouteGoldenCorpusCase, ...]
    nominal_completion_case_id: str = Field(min_length=1)
    blocked_case_ids: tuple[str, ...]
    command_leakage_rejection_cases: tuple[str, ...]
    coverage_labels: tuple[str, ...]
    generated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("corpus_cases", mode="before")
    @classmethod
    def _coerce_cases(cls, value: Any) -> tuple[PX4GazeboRouteGoldenCorpusCase, ...]:
        return tuple(
            PX4GazeboRouteGoldenCorpusCase.model_validate(item) for item in value
        )

    @field_validator(
        "blocked_case_ids", "command_leakage_rejection_cases", mode="before"
    )
    @classmethod
    def _coerce_ids(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])

    @field_validator("coverage_labels", mode="before")
    @classmethod
    def _coerce_coverage_labels(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_corpus(self) -> "PX4GazeboRouteGoldenCorpus":
        case_ids = {case.case_id for case in self.corpus_cases}
        if self.nominal_completion_case_id not in case_ids:
            raise PX4GazeboRouteRecoveryError(
                "golden corpus nominal case must be present"
            )
        for case_id in self.blocked_case_ids + self.command_leakage_rejection_cases:
            if case_id not in case_ids:
                raise PX4GazeboRouteRecoveryError(
                    "golden corpus referenced case is missing"
                )
        for case in self.corpus_cases:
            if (
                case.expected_terminal_status == "blocked"
                and not case.expected_blocked_reasons
            ):
                raise PX4GazeboRouteRecoveryError(
                    "blocked golden corpus cases require blocked reasons"
                )
        required_coverage = {
            "nominal_route_completion",
            "timeout_or_stale_telemetry",
            "rejected_command",
            "wrong_target",
            "geofence_violation",
            "missing_telemetry_or_pose",
            "state_observed_recovery",
            "hold_state_observed_recovery",
            "rtl_state_observed_recovery",
            "recovery_unconfirmed",
            "recovery_dispatch_blocked",
            "no_hardware_target_regression",
            "no_physical_execution_regression",
            "command_leakage_rejection",
        }
        if not required_coverage.issubset(set(self.coverage_labels)):
            missing = sorted(required_coverage.difference(set(self.coverage_labels)))
            raise PX4GazeboRouteRecoveryError(
                f"golden corpus missing coverage labels: {missing}"
            )
        return self


def _recommend_action(
    blocked_reasons: tuple[str, ...],
) -> tuple[PX4GazeboRouteRecoveryAction, str]:
    reason_set = set(blocked_reasons)
    if reason_set & {"route_pose_missing", "stale_route_progress"}:
        return (
            PX4GazeboRouteRecoveryAction.HOLD,
            "hold while route pose or telemetry freshness is restored",
        )
    if reason_set & {"route_geofence_violation", "wrong_delivery_vehicle"}:
        return (
            PX4GazeboRouteRecoveryAction.LAND,
            "land in simulation because route evidence is unsafe",
        )
    return (
        PX4GazeboRouteRecoveryAction.RETURN_TO_LAUNCH,
        "return to launch for unresolved route completion failure",
    )


def build_px4_gazebo_route_recovery_proposal(
    *,
    completion_gate: PX4GazeboRouteDeliveryCompletionGate | Mapping[str, Any],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboRouteRecoveryProposal:
    gate = _coerce_gate(completion_gate)
    if gate.final_status != PX4GazeboRouteDeliveryStatus.BLOCKED:
        raise PX4GazeboRouteRecoveryError(
            "route recovery proposal requires blocked route completion gate"
        )
    action, reason = _recommend_action(gate.blocked_reasons)
    proposed_at = _utc(now)
    payload = {
        "completion_gate_id": gate.completion_gate_id,
        "blocked_reasons": gate.blocked_reasons,
        "action": action.value,
        "proposed_at": proposed_at.isoformat(),
    }
    return PX4GazeboRouteRecoveryProposal(
        proposal_id=_stable_id("px4_gazebo_route_recovery_proposal", payload),
        completion_gate_ref=_completion_gate_ref(gate),
        source_blocked_reasons=gate.blocked_reasons,
        recommended_action=action,
        recommended_action_reason=reason,
        proposed_at=proposed_at,
        metadata={**(metadata or {}), "issue": 347, "parent_epic": 339},
    )


def build_px4_gazebo_route_recovery_approval(
    *,
    proposal: PX4GazeboRouteRecoveryProposal | Mapping[str, Any],
    operator_approval_performed: bool,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboRouteRecoveryApproval:
    resolved_proposal = (
        proposal
        if isinstance(proposal, PX4GazeboRouteRecoveryProposal)
        else PX4GazeboRouteRecoveryProposal.model_validate(dict(proposal))
    )
    approved_at = _utc(now)
    actions = (
        (resolved_proposal.recommended_action,) if operator_approval_performed else ()
    )
    payload = {
        "proposal_id": resolved_proposal.proposal_id,
        "operator_approval_performed": bool(operator_approval_performed),
        "actions": [action.value for action in actions],
        "approved_at": approved_at.isoformat(),
    }
    return PX4GazeboRouteRecoveryApproval(
        approval_id=_stable_id("px4_gazebo_route_recovery_approval", payload),
        proposal_ref=_proposal_ref(resolved_proposal),
        operator_approval_performed=bool(operator_approval_performed),
        approved_recovery_actions=actions,
        approved_at=approved_at,
        metadata={**(metadata or {}), "issue": 347, "parent_epic": 339},
    )


def build_px4_gazebo_route_recovery_allowlist(
    *,
    proposal: PX4GazeboRouteRecoveryProposal | Mapping[str, Any],
    approval: PX4GazeboRouteRecoveryApproval | Mapping[str, Any],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboRouteRecoveryAllowlist:
    resolved_proposal = (
        proposal
        if isinstance(proposal, PX4GazeboRouteRecoveryProposal)
        else PX4GazeboRouteRecoveryProposal.model_validate(dict(proposal))
    )
    resolved_approval = (
        approval
        if isinstance(approval, PX4GazeboRouteRecoveryApproval)
        else PX4GazeboRouteRecoveryApproval.model_validate(dict(approval))
    )
    if resolved_approval.proposal_ref != _proposal_ref(resolved_proposal):
        raise PX4GazeboRouteRecoveryError("route recovery approval/proposal mismatch")
    if resolved_approval.operator_approval_performed is not True:
        raise PX4GazeboRouteRecoveryError(
            "route recovery allowlist requires operator approval"
        )
    if (
        resolved_proposal.recommended_action
        not in resolved_approval.approved_recovery_actions
    ):
        raise PX4GazeboRouteRecoveryError(
            "route recovery approval does not include recommended action"
        )
    generated_at = _utc(now)
    payload = {
        "proposal_id": resolved_proposal.proposal_id,
        "approval_id": resolved_approval.approval_id,
        "action": resolved_proposal.recommended_action.value,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboRouteRecoveryAllowlist(
        allowlist_id=_stable_id("px4_gazebo_route_recovery_allowlist", payload),
        proposal_ref=_proposal_ref(resolved_proposal),
        approval_ref=_approval_ref(resolved_approval),
        allowed_recovery_actions=(resolved_proposal.recommended_action,),
        generated_at=generated_at,
        metadata={**(metadata or {}), "issue": 347, "parent_epic": 339},
    )


def build_px4_gazebo_route_recovery_diagnostics(
    *,
    proposal: PX4GazeboRouteRecoveryProposal | Mapping[str, Any],
    recovery_unavailable_reason: str,
    approval: PX4GazeboRouteRecoveryApproval | Mapping[str, Any] | None = None,
    allowlist: PX4GazeboRouteRecoveryAllowlist | Mapping[str, Any] | None = None,
    recovery_action_allowlisted: bool = False,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboRouteRecoveryDiagnostics:
    resolved_proposal = (
        proposal
        if isinstance(proposal, PX4GazeboRouteRecoveryProposal)
        else PX4GazeboRouteRecoveryProposal.model_validate(dict(proposal))
    )
    resolved_approval = (
        None
        if approval is None
        else (
            approval
            if isinstance(approval, PX4GazeboRouteRecoveryApproval)
            else PX4GazeboRouteRecoveryApproval.model_validate(dict(approval))
        )
    )
    resolved_allowlist = (
        None
        if allowlist is None
        else (
            allowlist
            if isinstance(allowlist, PX4GazeboRouteRecoveryAllowlist)
            else PX4GazeboRouteRecoveryAllowlist.model_validate(dict(allowlist))
        )
    )
    observed_at = _utc(now)
    blocked = _ordered_tuple(
        [recovery_unavailable_reason, *resolved_proposal.source_blocked_reasons]
    )
    payload = {
        "proposal_id": resolved_proposal.proposal_id,
        "reason": recovery_unavailable_reason,
        "blocked_reasons": blocked,
        "observed_at": observed_at.isoformat(),
    }
    return PX4GazeboRouteRecoveryDiagnostics(
        diagnostics_id=_stable_id("px4_gazebo_route_recovery_diagnostics", payload),
        proposal_ref=_proposal_ref(resolved_proposal),
        approval_ref=(
            None if resolved_approval is None else _approval_ref(resolved_approval)
        ),
        allowlist_ref=(
            None
            if resolved_allowlist is None
            else f"px4_gazebo_route_recovery_allowlist:{resolved_allowlist.allowlist_id}"
        ),
        recovery_unavailable_reason=recovery_unavailable_reason,
        blocked_reasons=blocked,
        operator_approval_available=resolved_approval is not None
        and resolved_approval.operator_approval_performed,
        allowlist_available=resolved_allowlist is not None,
        recovery_action_allowlisted=bool(recovery_action_allowlisted),
        observed_at=observed_at,
        metadata={**(metadata or {}), "issue": 347, "parent_epic": 339},
    )


def build_px4_gazebo_route_golden_corpus(
    *,
    completion_gates: Sequence[
        PX4GazeboRouteDeliveryCompletionGate | Mapping[str, Any]
    ],
    recovery_proposals: Sequence[
        PX4GazeboRouteRecoveryProposal | Mapping[str, Any]
    ] = (),
    extra_cases: Sequence[PX4GazeboRouteGoldenCorpusCase | Mapping[str, Any]] = (),
    command_leakage_rejection_case_ids: Sequence[str] = (),
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboRouteGoldenCorpus:
    gates = tuple(_coerce_gate(gate) for gate in completion_gates)
    proposals = tuple(
        (
            proposal
            if isinstance(proposal, PX4GazeboRouteRecoveryProposal)
            else PX4GazeboRouteRecoveryProposal.model_validate(dict(proposal))
        )
        for proposal in recovery_proposals
    )
    additional_cases = tuple(
        (
            case
            if isinstance(case, PX4GazeboRouteGoldenCorpusCase)
            else PX4GazeboRouteGoldenCorpusCase.model_validate(dict(case))
        )
        for case in extra_cases
    )
    cases: list[PX4GazeboRouteGoldenCorpusCase] = []
    for gate in gates:
        case_id = f"completion:{gate.completion_gate_id}"
        versions = [
            gate.schema_version,
            PX4_GAZEBO_ROUTE_COMMAND_DISPATCH_RESULT_SCHEMA_VERSION,
            PX4_GAZEBO_ROUTE_PROGRESS_EVIDENCE_SCHEMA_VERSION,
        ]
        cases.append(
            PX4GazeboRouteGoldenCorpusCase(
                case_id=case_id,
                expected_terminal_status=gate.final_status.value,
                required_artifact_schema_versions=versions,
                expected_blocked_reasons=gate.blocked_reasons,
            )
        )
    for proposal in proposals:
        cases.append(
            PX4GazeboRouteGoldenCorpusCase(
                case_id=f"recovery:{proposal.proposal_id}",
                expected_terminal_status="blocked",
                required_artifact_schema_versions=(proposal.schema_version,),
                expected_blocked_reasons=proposal.source_blocked_reasons,
            )
        )
    cases.extend(additional_cases)
    generated_at = _utc(now)
    nominal = next(
        (
            case.case_id
            for case in cases
            if case.expected_terminal_status == "completed"
        ),
        "",
    )
    if not nominal:
        raise PX4GazeboRouteRecoveryError(
            "golden corpus requires a nominal completed route case"
        )
    blocked_case_ids = tuple(
        case.case_id for case in cases if case.expected_terminal_status == "blocked"
    )
    command_leakage_ids = (
        _ordered_tuple(command_leakage_rejection_case_ids)
        if command_leakage_rejection_case_ids
        else tuple(
            case.case_id
            for case in additional_cases
            if case.case_id.startswith("rejection:")
        )
    )
    coverage_labels = {
        "nominal_route_completion",
        "no_hardware_target_regression",
        "no_physical_execution_regression",
    }
    for case in cases:
        reasons = set(case.expected_blocked_reasons)
        if {"stale_route_progress", "mavlink_timeout"} & reasons:
            coverage_labels.add("timeout_or_stale_telemetry")
        if {"command_rejected", "rejected_command"} & reasons:
            coverage_labels.add("rejected_command")
        if {"wrong_target", "target_system_mismatch"} & reasons:
            coverage_labels.add("wrong_target")
        if "route_geofence_violation" in reasons:
            coverage_labels.add("geofence_violation")
        if {"route_pose_missing", "missing_px4_telemetry_correlated"} & reasons:
            coverage_labels.add("missing_telemetry_or_pose")
        if case.case_id.startswith("rejection:") or case.case_id in command_leakage_ids:
            coverage_labels.add("command_leakage_rejection")
        if (
            case.expected_recovery_completion_basis
            == "state_observed_after_dispatch_timeout"
        ):
            coverage_labels.add("state_observed_recovery")
            if case.expected_recovery_action == "hold":
                coverage_labels.add("hold_state_observed_recovery")
            if case.expected_recovery_action in {"return_to_launch", "rtl"}:
                coverage_labels.add("rtl_state_observed_recovery")
        if case.expected_recovery_completion_basis == "ack_observed_and_state_observed":
            coverage_labels.add("state_observed_recovery")
            if case.expected_recovery_action == "hold":
                coverage_labels.add("hold_state_observed_recovery")
            if case.expected_recovery_action in {"return_to_launch", "rtl"}:
                coverage_labels.add("rtl_state_observed_recovery")
        if (
            case.expected_recovery_completion_basis
            == "state_not_observed_after_dispatch_timeout"
        ):
            coverage_labels.add("recovery_unconfirmed")
        if case.expected_recovery_completion_basis == "dispatch_blocked_before_send":
            coverage_labels.add("recovery_dispatch_blocked")
    payload = {
        "case_ids": [case.case_id for case in cases],
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboRouteGoldenCorpus(
        corpus_id=_stable_id("px4_gazebo_route_golden_corpus", payload),
        corpus_cases=tuple(cases),
        nominal_completion_case_id=nominal,
        blocked_case_ids=blocked_case_ids,
        command_leakage_rejection_cases=command_leakage_ids,
        coverage_labels=tuple(sorted(coverage_labels)),
        generated_at=generated_at,
        metadata={**(metadata or {}), "issue": 348, "parent_epic": 339},
    )


def run_px4_gazebo_route_recovery_task(
    task_id: str,
    *,
    completion_gate: PX4GazeboRouteDeliveryCompletionGate | Mapping[str, Any],
    recovery_proposal: PX4GazeboRouteRecoveryProposal | Mapping[str, Any],
    recovery_approval: PX4GazeboRouteRecoveryApproval | Mapping[str, Any] | None = None,
    recovery_allowlist: (
        PX4GazeboRouteRecoveryAllowlist | Mapping[str, Any] | None
    ) = None,
    recovery_diagnostics: (
        PX4GazeboRouteRecoveryDiagnostics | Mapping[str, Any] | None
    ) = None,
    golden_corpus: PX4GazeboRouteGoldenCorpus | Mapping[str, Any] | None = None,
    task_store_factory: Any | None = None,
) -> dict[str, Any]:
    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboRouteRecoveryError(
            f"task {task_id} not found; cannot attach route recovery evidence"
        )
    gate = _coerce_gate(completion_gate)
    proposal = (
        recovery_proposal
        if isinstance(recovery_proposal, PX4GazeboRouteRecoveryProposal)
        else PX4GazeboRouteRecoveryProposal.model_validate(dict(recovery_proposal))
    )
    if proposal.completion_gate_ref != _completion_gate_ref(gate):
        raise PX4GazeboRouteRecoveryError("route recovery proposal gate mismatch")
    artifacts: dict[str, Any] = {
        "px4_gazebo_route_delivery_completion_gate": gate.model_dump(mode="json"),
        "px4_gazebo_route_recovery_proposal": proposal.model_dump(mode="json"),
    }
    if recovery_approval is not None:
        approval = (
            recovery_approval
            if isinstance(recovery_approval, PX4GazeboRouteRecoveryApproval)
            else PX4GazeboRouteRecoveryApproval.model_validate(dict(recovery_approval))
        )
        if approval.proposal_ref != _proposal_ref(proposal):
            raise PX4GazeboRouteRecoveryError(
                "route recovery approval/proposal mismatch"
            )
        artifacts["px4_gazebo_route_recovery_approval"] = approval.model_dump(
            mode="json"
        )
    else:
        approval = None
    if recovery_allowlist is not None:
        allowlist = (
            recovery_allowlist
            if isinstance(recovery_allowlist, PX4GazeboRouteRecoveryAllowlist)
            else PX4GazeboRouteRecoveryAllowlist.model_validate(
                dict(recovery_allowlist)
            )
        )
        if allowlist.proposal_ref != _proposal_ref(proposal):
            raise PX4GazeboRouteRecoveryError(
                "route recovery allowlist/proposal mismatch"
            )
        if approval is not None and allowlist.approval_ref != _approval_ref(approval):
            raise PX4GazeboRouteRecoveryError(
                "route recovery allowlist/approval mismatch"
            )
        artifacts["px4_gazebo_route_recovery_allowlist"] = allowlist.model_dump(
            mode="json"
        )
    else:
        allowlist = None
    if recovery_diagnostics is not None:
        diagnostics = (
            recovery_diagnostics
            if isinstance(recovery_diagnostics, PX4GazeboRouteRecoveryDiagnostics)
            else PX4GazeboRouteRecoveryDiagnostics.model_validate(
                dict(recovery_diagnostics)
            )
        )
        if diagnostics.proposal_ref != _proposal_ref(proposal):
            raise PX4GazeboRouteRecoveryError(
                "route recovery diagnostics/proposal mismatch"
            )
        if (
            diagnostics.approval_ref is not None
            and approval is not None
            and diagnostics.approval_ref != _approval_ref(approval)
        ):
            raise PX4GazeboRouteRecoveryError(
                "route recovery diagnostics/approval mismatch"
            )
        if (
            diagnostics.allowlist_ref is not None
            and allowlist is not None
            and diagnostics.allowlist_ref
            != f"px4_gazebo_route_recovery_allowlist:{allowlist.allowlist_id}"
        ):
            raise PX4GazeboRouteRecoveryError(
                "route recovery diagnostics/allowlist mismatch"
            )
        artifacts["px4_gazebo_route_recovery_diagnostics"] = diagnostics.model_dump(
            mode="json"
        )
    if golden_corpus is not None:
        corpus = (
            golden_corpus
            if isinstance(golden_corpus, PX4GazeboRouteGoldenCorpus)
            else PX4GazeboRouteGoldenCorpus.model_validate(dict(golden_corpus))
        )
        artifacts["px4_gazebo_route_delivery_golden_corpus"] = corpus.model_dump(
            mode="json"
        )
    updated = store.update(
        task_id,
        status="blocked",
        artifacts=artifacts,
        ended_at=time.time(),
    )
    if updated is None:
        raise PX4GazeboRouteRecoveryError(
            f"task {task_id} disappeared while attaching route recovery evidence"
        )
    return updated


__all__ = [
    "PX4_GAZEBO_ROUTE_RECOVERY_PROPOSAL_SCHEMA_VERSION",
    "PX4_GAZEBO_ROUTE_RECOVERY_APPROVAL_SCHEMA_VERSION",
    "PX4_GAZEBO_ROUTE_RECOVERY_ALLOWLIST_SCHEMA_VERSION",
    "PX4_GAZEBO_ROUTE_RECOVERY_DIAGNOSTICS_SCHEMA_VERSION",
    "PX4_GAZEBO_ROUTE_GOLDEN_CORPUS_SCHEMA_VERSION",
    "PX4GazeboRouteRecoveryAction",
    "PX4GazeboRouteRecoveryStatus",
    "PX4GazeboRouteRecoveryError",
    "PX4GazeboRouteRecoveryProposal",
    "PX4GazeboRouteRecoveryApproval",
    "PX4GazeboRouteRecoveryAllowlist",
    "PX4GazeboRouteRecoveryDiagnostics",
    "PX4GazeboRouteGoldenCorpusCase",
    "PX4GazeboRouteGoldenCorpus",
    "build_px4_gazebo_route_recovery_proposal",
    "build_px4_gazebo_route_recovery_approval",
    "build_px4_gazebo_route_recovery_allowlist",
    "build_px4_gazebo_route_recovery_diagnostics",
    "build_px4_gazebo_route_golden_corpus",
    "run_px4_gazebo_route_recovery_task",
]
