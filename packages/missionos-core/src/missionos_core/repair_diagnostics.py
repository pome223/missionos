"""Backend-neutral five-axis Repair diagnostic contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


REPAIR_DIAGNOSTIC_REPORT_SCHEMA_VERSION = "missionos_core_repair_diagnostic_report.v1"


class RepairDiagnosticAxis(str, Enum):
    ACTION_ACTIVITY = "action_activity"
    CORRECTIVE_ALIGNMENT = "corrective_alignment"
    PREDICATE_RECOVERY = "predicate_recovery"
    PRESERVATION = "preservation"
    STABLE_HOLD = "stable_hold"


REPAIR_DIAGNOSTIC_AXIS_ORDER = tuple(RepairDiagnosticAxis)


class RepairAxisStatus(str, Enum):
    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    NOT_OBSERVED = "not_observed"


class RepairEvidenceBasis(str, Enum):
    MODEL_OUTPUT = "model_output"
    DIAGNOSTIC_REFERENCE = "diagnostic_reference"
    SIMULATOR_OBSERVATION = "simulator_observation"
    EXTERNAL_RUNTIME_OBSERVATION = "external_runtime_observation"
    PHYSICAL_OBSERVATION = "physical_observation"
    NOT_OBSERVED = "not_observed"


class RepairDiagnosticValidationStatus(str, Enum):
    VERIFIED = "verified"
    INVALID = "invalid"


class RepairFailureStage(str, Enum):
    ACTION_ACTIVITY = RepairDiagnosticAxis.ACTION_ACTIVITY.value
    CORRECTIVE_ALIGNMENT = RepairDiagnosticAxis.CORRECTIVE_ALIGNMENT.value
    PREDICATE_RECOVERY = RepairDiagnosticAxis.PREDICATE_RECOVERY.value
    PRESERVATION = RepairDiagnosticAxis.PRESERVATION.value
    STABLE_HOLD = RepairDiagnosticAxis.STABLE_HOLD.value
    NONE = "none"
    UNDETERMINED = "undetermined"


_OBSERVED_EFFECT_BASES = frozenset(
    {
        RepairEvidenceBasis.SIMULATOR_OBSERVATION,
        RepairEvidenceBasis.EXTERNAL_RUNTIME_OBSERVATION,
        RepairEvidenceBasis.PHYSICAL_OBSERVATION,
    }
)
_EFFECT_AXES = frozenset(
    {
        RepairDiagnosticAxis.PREDICATE_RECOVERY,
        RepairDiagnosticAxis.PRESERVATION,
        RepairDiagnosticAxis.STABLE_HOLD,
    }
)


@dataclass(frozen=True)
class RepairAxisObservation:
    """One axis judgment bound to its criterion and evidence."""

    axis: RepairDiagnosticAxis
    status: RepairAxisStatus
    evidence_basis: RepairEvidenceBasis
    criterion_ref: str
    observation_scope_ref: str
    evidence_refs: tuple[str, ...] = ()
    measurements: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis.value,
            "status": self.status.value,
            "evidence_basis": self.evidence_basis.value,
            "criterion_ref": self.criterion_ref,
            "observation_scope_ref": self.observation_scope_ref,
            "evidence_refs": list(self.evidence_refs),
            "measurements": dict(self.measurements),
        }


@dataclass(frozen=True)
class RepairDiagnosticContext:
    """Exact executor/task/fixture scope consumed by one assessment."""

    report_id: str
    executor_ref: str
    task_ref: str
    fixture_ref: str
    evaluation_scope: str


@dataclass(frozen=True)
class RepairDiagnosticAssessment:
    validation_status: RepairDiagnosticValidationStatus
    reasons: tuple[str, ...]
    first_failed_axis: RepairFailureStage
    next_unobserved_axis: RepairDiagnosticAxis | None
    bounded_stable_repair_observed: bool
    axes: tuple[RepairAxisObservation, ...]
    context: RepairDiagnosticContext
    approval_created: bool = False
    dispatch_authority_created: bool = False
    dispatch_request_sent: bool = False
    mission_completion_claimed: bool = False
    executor_repair_capability_established: bool = False
    physical_execution_invoked: bool = False
    schema_version: str = REPAIR_DIAGNOSTIC_REPORT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "validation_status": self.validation_status.value,
            "reasons": list(self.reasons),
            "context": {
                "report_id": self.context.report_id,
                "executor_ref": self.context.executor_ref,
                "task_ref": self.context.task_ref,
                "fixture_ref": self.context.fixture_ref,
                "evaluation_scope": self.context.evaluation_scope,
            },
            "axes": [observation.to_dict() for observation in self.axes],
            "first_failed_axis": self.first_failed_axis.value,
            "next_unobserved_axis": (
                self.next_unobserved_axis.value if self.next_unobserved_axis else None
            ),
            "bounded_stable_repair_observed": self.bounded_stable_repair_observed,
            "approval_created": self.approval_created,
            "dispatch_authority_created": self.dispatch_authority_created,
            "dispatch_request_sent": self.dispatch_request_sent,
            "mission_completion_claimed": self.mission_completion_claimed,
            "executor_repair_capability_established": (self.executor_repair_capability_established),
            "physical_execution_invoked": self.physical_execution_invoked,
        }


def _non_empty(value: str) -> bool:
    return bool(str(value or "").strip())


def _validate_stable_hold(observation: RepairAxisObservation) -> list[str]:
    if observation.status is RepairAxisStatus.NOT_OBSERVED:
        return []
    required = observation.measurements.get("required_hold_steps")
    observed = observation.measurements.get("observed_hold_steps")
    if (
        isinstance(required, bool)
        or not isinstance(required, int)
        or required <= 0
        or isinstance(observed, bool)
        or not isinstance(observed, int)
        or observed < 0
    ):
        return ["stable_hold_step_measurement_invalid"]
    if observation.status is RepairAxisStatus.SATISFIED and observed < required:
        return ["stable_hold_satisfied_without_required_steps"]
    if observation.status is RepairAxisStatus.NOT_SATISFIED and observed >= required:
        return ["stable_hold_failed_despite_required_steps"]
    return []


def evaluate_repair_diagnostics(
    observations: Sequence[RepairAxisObservation],
    *,
    context: RepairDiagnosticContext,
) -> RepairDiagnosticAssessment:
    """Validate and classify one bounded Repair attempt without creating authority."""

    reasons: list[str] = []
    for field_name in (
        "report_id",
        "executor_ref",
        "task_ref",
        "fixture_ref",
        "evaluation_scope",
    ):
        if not _non_empty(getattr(context, field_name)):
            reasons.append(f"repair_diagnostic_context_{field_name}_missing")

    by_axis: dict[RepairDiagnosticAxis, RepairAxisObservation] = {}
    for observation in observations:
        if observation.axis in by_axis:
            reasons.append(f"repair_diagnostic_axis_duplicate:{observation.axis.value}")
            continue
        by_axis[observation.axis] = observation
        if not _non_empty(observation.criterion_ref):
            reasons.append(f"repair_diagnostic_criterion_missing:{observation.axis.value}")
        if not _non_empty(observation.observation_scope_ref):
            reasons.append(f"repair_diagnostic_scope_missing:{observation.axis.value}")
        if observation.status is RepairAxisStatus.NOT_OBSERVED:
            if observation.evidence_basis is not RepairEvidenceBasis.NOT_OBSERVED:
                reasons.append(
                    f"repair_diagnostic_unobserved_basis_invalid:{observation.axis.value}"
                )
            if observation.evidence_refs:
                reasons.append(
                    f"repair_diagnostic_unobserved_has_evidence:{observation.axis.value}"
                )
        else:
            if observation.evidence_basis is RepairEvidenceBasis.NOT_OBSERVED:
                reasons.append(f"repair_diagnostic_observed_basis_missing:{observation.axis.value}")
            if not observation.evidence_refs or any(
                not _non_empty(reference) for reference in observation.evidence_refs
            ):
                reasons.append(f"repair_diagnostic_evidence_ref_missing:{observation.axis.value}")
        if (
            observation.axis in _EFFECT_AXES
            and observation.status is not RepairAxisStatus.NOT_OBSERVED
            and observation.evidence_basis not in _OBSERVED_EFFECT_BASES
        ):
            reasons.append(
                f"repair_diagnostic_effect_requires_observation:{observation.axis.value}"
            )
        if (
            observation.axis is RepairDiagnosticAxis.CORRECTIVE_ALIGNMENT
            and observation.status is not RepairAxisStatus.NOT_OBSERVED
            and observation.evidence_basis
            not in _OBSERVED_EFFECT_BASES | {RepairEvidenceBasis.DIAGNOSTIC_REFERENCE}
        ):
            reasons.append("repair_diagnostic_alignment_reference_missing")
        if observation.axis is RepairDiagnosticAxis.STABLE_HOLD:
            reasons.extend(_validate_stable_hold(observation))

    missing = [axis for axis in REPAIR_DIAGNOSTIC_AXIS_ORDER if axis not in by_axis]
    reasons.extend(f"repair_diagnostic_axis_missing:{axis.value}" for axis in missing)
    observation_scopes = {
        observation.observation_scope_ref.strip()
        for observation in by_axis.values()
        if _non_empty(observation.observation_scope_ref)
    }
    if len(observation_scopes) > 1:
        reasons.append("repair_diagnostic_observation_scope_mismatch")
    reasons = list(dict.fromkeys(reasons))
    ordered = tuple(by_axis[axis] for axis in REPAIR_DIAGNOSTIC_AXIS_ORDER if axis in by_axis)

    first_failed = RepairFailureStage.UNDETERMINED
    next_unobserved = None
    bounded_stable_repair_observed = False
    if not reasons:
        all_prior_satisfied = True
        first_failed = RepairFailureStage.NONE
        for axis in REPAIR_DIAGNOSTIC_AXIS_ORDER:
            status = by_axis[axis].status
            if status is RepairAxisStatus.NOT_OBSERVED:
                next_unobserved = axis
                first_failed = RepairFailureStage.UNDETERMINED
                break
            if status is RepairAxisStatus.NOT_SATISFIED:
                first_failed = (
                    RepairFailureStage(axis.value)
                    if all_prior_satisfied
                    else RepairFailureStage.UNDETERMINED
                )
                break
            all_prior_satisfied = all_prior_satisfied and (status is RepairAxisStatus.SATISFIED)
        bounded_stable_repair_observed = all(
            by_axis[axis].status is RepairAxisStatus.SATISFIED
            for axis in REPAIR_DIAGNOSTIC_AXIS_ORDER
        )

    return RepairDiagnosticAssessment(
        validation_status=(
            RepairDiagnosticValidationStatus.INVALID
            if reasons
            else RepairDiagnosticValidationStatus.VERIFIED
        ),
        reasons=tuple(reasons),
        first_failed_axis=first_failed,
        next_unobserved_axis=next_unobserved,
        bounded_stable_repair_observed=bounded_stable_repair_observed,
        axes=ordered,
        context=context,
    )


__all__ = [
    "REPAIR_DIAGNOSTIC_AXIS_ORDER",
    "REPAIR_DIAGNOSTIC_REPORT_SCHEMA_VERSION",
    "RepairAxisObservation",
    "RepairAxisStatus",
    "RepairDiagnosticAssessment",
    "RepairDiagnosticAxis",
    "RepairDiagnosticContext",
    "RepairDiagnosticValidationStatus",
    "RepairEvidenceBasis",
    "RepairFailureStage",
    "evaluate_repair_diagnostics",
]
