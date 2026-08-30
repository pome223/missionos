from __future__ import annotations

from missionos_core import (
    REPAIR_DIAGNOSTIC_AXIS_ORDER,
    RepairAxisObservation,
    RepairAxisStatus,
    RepairDiagnosticAxis,
    RepairDiagnosticContext,
    RepairDiagnosticValidationStatus,
    RepairEvidenceBasis,
    RepairFailureStage,
    evaluate_repair_diagnostics,
)


CONTEXT = RepairDiagnosticContext(
    report_id="repair-diagnostic:test",
    executor_ref="executor:test",
    task_ref="task:test",
    fixture_ref="fixture:test",
    evaluation_scope="simulator_diagnostic_clone",
)


def _observation(
    axis: RepairDiagnosticAxis,
    status: RepairAxisStatus,
    basis: RepairEvidenceBasis,
    *,
    measurements: dict[str, object] | None = None,
) -> RepairAxisObservation:
    return RepairAxisObservation(
        axis=axis,
        status=status,
        evidence_basis=basis,
        criterion_ref=f"criterion:{axis.value}:v1",
        observation_scope_ref="scope:exact-fixture-seed-0",
        evidence_refs=()
        if status is RepairAxisStatus.NOT_OBSERVED
        else (f"evidence:{axis.value}",),
        measurements=measurements or {},
    )


def test_activity_and_alignment_are_separate_failure_stages() -> None:
    observations = (
        _observation(
            RepairDiagnosticAxis.ACTION_ACTIVITY,
            RepairAxisStatus.SATISFIED,
            RepairEvidenceBasis.MODEL_OUTPUT,
        ),
        _observation(
            RepairDiagnosticAxis.CORRECTIVE_ALIGNMENT,
            RepairAxisStatus.NOT_SATISFIED,
            RepairEvidenceBasis.DIAGNOSTIC_REFERENCE,
        ),
        _observation(
            RepairDiagnosticAxis.PREDICATE_RECOVERY,
            RepairAxisStatus.NOT_SATISFIED,
            RepairEvidenceBasis.SIMULATOR_OBSERVATION,
        ),
        _observation(
            RepairDiagnosticAxis.PRESERVATION,
            RepairAxisStatus.NOT_SATISFIED,
            RepairEvidenceBasis.SIMULATOR_OBSERVATION,
        ),
        _observation(
            RepairDiagnosticAxis.STABLE_HOLD,
            RepairAxisStatus.NOT_OBSERVED,
            RepairEvidenceBasis.NOT_OBSERVED,
        ),
    )

    result = evaluate_repair_diagnostics(observations, context=CONTEXT)

    assert result.validation_status is RepairDiagnosticValidationStatus.VERIFIED
    assert result.first_failed_axis is RepairFailureStage.CORRECTIVE_ALIGNMENT
    assert result.next_unobserved_axis is None
    assert result.bounded_stable_repair_observed is False
    assert result.executor_repair_capability_established is False
    assert result.mission_completion_claimed is False


def test_all_axes_can_establish_only_bounded_stable_repair_observation() -> None:
    effect_basis = RepairEvidenceBasis.SIMULATOR_OBSERVATION
    observations = tuple(
        _observation(
            axis,
            RepairAxisStatus.SATISFIED,
            (
                RepairEvidenceBasis.MODEL_OUTPUT
                if axis is RepairDiagnosticAxis.ACTION_ACTIVITY
                else RepairEvidenceBasis.DIAGNOSTIC_REFERENCE
                if axis is RepairDiagnosticAxis.CORRECTIVE_ALIGNMENT
                else effect_basis
            ),
            measurements=(
                {"required_hold_steps": 20, "observed_hold_steps": 20}
                if axis is RepairDiagnosticAxis.STABLE_HOLD
                else None
            ),
        )
        for axis in REPAIR_DIAGNOSTIC_AXIS_ORDER
    )

    result = evaluate_repair_diagnostics(observations, context=CONTEXT)

    assert result.validation_status is RepairDiagnosticValidationStatus.VERIFIED
    assert result.first_failed_axis is RepairFailureStage.NONE
    assert result.bounded_stable_repair_observed is True
    assert result.executor_repair_capability_established is False
    assert result.dispatch_authority_created is False
    assert result.physical_execution_invoked is False


def test_effect_axes_reject_model_output_as_observation_authority() -> None:
    observations = tuple(
        _observation(
            axis,
            RepairAxisStatus.SATISFIED,
            RepairEvidenceBasis.MODEL_OUTPUT,
            measurements=(
                {"required_hold_steps": 20, "observed_hold_steps": 20}
                if axis is RepairDiagnosticAxis.STABLE_HOLD
                else None
            ),
        )
        for axis in REPAIR_DIAGNOSTIC_AXIS_ORDER
    )

    result = evaluate_repair_diagnostics(observations, context=CONTEXT)

    assert result.validation_status is RepairDiagnosticValidationStatus.INVALID
    assert "repair_diagnostic_alignment_reference_missing" in result.reasons
    assert "repair_diagnostic_effect_requires_observation:predicate_recovery" in result.reasons
    assert result.bounded_stable_repair_observed is False


def test_stable_hold_requires_preregistered_step_count_to_match_status() -> None:
    observations = tuple(
        _observation(
            axis,
            RepairAxisStatus.SATISFIED,
            (
                RepairEvidenceBasis.MODEL_OUTPUT
                if axis is RepairDiagnosticAxis.ACTION_ACTIVITY
                else RepairEvidenceBasis.DIAGNOSTIC_REFERENCE
                if axis is RepairDiagnosticAxis.CORRECTIVE_ALIGNMENT
                else RepairEvidenceBasis.SIMULATOR_OBSERVATION
            ),
            measurements=(
                {"required_hold_steps": 20, "observed_hold_steps": 5}
                if axis is RepairDiagnosticAxis.STABLE_HOLD
                else None
            ),
        )
        for axis in REPAIR_DIAGNOSTIC_AXIS_ORDER
    )

    result = evaluate_repair_diagnostics(observations, context=CONTEXT)

    assert result.validation_status is RepairDiagnosticValidationStatus.INVALID
    assert "stable_hold_satisfied_without_required_steps" in result.reasons


def test_missing_axis_fails_closed() -> None:
    observations = (
        _observation(
            RepairDiagnosticAxis.ACTION_ACTIVITY,
            RepairAxisStatus.NOT_SATISFIED,
            RepairEvidenceBasis.MODEL_OUTPUT,
        ),
    )

    result = evaluate_repair_diagnostics(observations, context=CONTEXT)

    assert result.validation_status is RepairDiagnosticValidationStatus.INVALID
    assert "repair_diagnostic_axis_missing:corrective_alignment" in result.reasons
    assert result.first_failed_axis is RepairFailureStage.UNDETERMINED


def test_axes_from_different_observation_scopes_cannot_be_combined() -> None:
    observations = list(
        _observation(
            axis,
            RepairAxisStatus.SATISFIED,
            (
                RepairEvidenceBasis.MODEL_OUTPUT
                if axis is RepairDiagnosticAxis.ACTION_ACTIVITY
                else RepairEvidenceBasis.DIAGNOSTIC_REFERENCE
                if axis is RepairDiagnosticAxis.CORRECTIVE_ALIGNMENT
                else RepairEvidenceBasis.SIMULATOR_OBSERVATION
            ),
            measurements=(
                {"required_hold_steps": 20, "observed_hold_steps": 20}
                if axis is RepairDiagnosticAxis.STABLE_HOLD
                else None
            ),
        )
        for axis in REPAIR_DIAGNOSTIC_AXIS_ORDER
    )
    observations[-1] = RepairAxisObservation(
        axis=observations[-1].axis,
        status=observations[-1].status,
        evidence_basis=observations[-1].evidence_basis,
        criterion_ref=observations[-1].criterion_ref,
        observation_scope_ref="scope:different-run",
        evidence_refs=observations[-1].evidence_refs,
        measurements=observations[-1].measurements,
    )

    result = evaluate_repair_diagnostics(observations, context=CONTEXT)

    assert result.validation_status is RepairDiagnosticValidationStatus.INVALID
    assert "repair_diagnostic_observation_scope_mismatch" in result.reasons
