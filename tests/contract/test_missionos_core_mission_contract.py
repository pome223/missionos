from __future__ import annotations

from dataclasses import fields, replace

import pytest

from missionos_core import (
    OUTCOME_REQUIRES_PREDICATE_REASON,
    EvidenceOrigin,
    FrozenMissionContract,
    HardwareExecutionMode,
    MissionContractEvaluation,
    MissionContractOutcomeStatus,
    MissionEvidenceReadiness,
    MissionObservation,
    MissionRuntimeEvidence,
    ObservationFreshnessBasis,
    ObservationRequirement,
    OutcomeClaimSpec,
    PredicatePackageBinding,
    QuantificationScope,
    QuantificationScopeKind,
    ReferenceInput,
    TerminationEvent,
    TerminationPolicy,
    TerminationReason,
    UTC_WALL_CLOCK_DOMAIN_REF,
    VerificationBasis,
    check_mission_evidence_readiness,
    frozen_contract_from_mapping,
    mission_observation_source_receipt_binding_sha256,
    validate_frozen_mission_contract,
)


pytestmark = pytest.mark.contract

EVALUATED_AT = "2026-07-28T12:00:10+00:00"
OBSERVED_AT = "2026-07-28T12:00:00+00:00"
REGION_DIGEST = "a" * 64
CONTENT_DIGEST = "b" * 64
PREDICATE_PACKAGE_DIGEST = "c" * 64


def _contract(**overrides) -> FrozenMissionContract:
    contract = FrozenMissionContract(
        contract_id="contract-1",
        contract_version="2026-07-28",
        execution_scope=HardwareExecutionMode.SIM,
        reference_inputs=(
            ReferenceInput(
                input_id="approved_region",
                kind="approved_spatial_region",
                content_sha256=REGION_DIGEST,
            ),
        ),
        observation_requirements=(
            ObservationRequirement(
                requirement_id="terminal_state",
                evidence_kind="executor_terminal_state",
                required_origin=EvidenceOrigin.MACHINE_OBSERVED,
                maximum_age_seconds=30.0,
            ),
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.SPATIAL_REGION,
            scope_ref="approved_region",
        ),
        outcome_claim_spec=OutcomeClaimSpec(
            claim_id="bounded_action_completed",
            statement="The approved bounded action completed.",
            claim_scope="sim_action",
        ),
        predicate_package=PredicatePackageBinding(
            package_id="bounded_action_completed",
            package_version="1",
            content_sha256=PREDICATE_PACKAGE_DIGEST,
        ),
        termination_policy=TerminationPolicy(
            allowed_reasons=(
                TerminationReason.EXPIRY,
                TerminationReason.OPERATOR_INTERRUPTION,
                TerminationReason.SAFE_STOP,
                TerminationReason.TERMINAL_PREDICATE_SATISFIED,
            )
        ),
        required_verification_basis=VerificationBasis.DETERMINISTIC,
    )
    return replace(contract, **overrides) if overrides else contract


def _observation(**overrides) -> MissionObservation:
    observation = MissionObservation(
        observation_id="observation-1",
        requirement_id="terminal_state",
        evidence_kind="executor_terminal_state",
        origin=EvidenceOrigin.MACHINE_OBSERVED,
        observed_at=OBSERVED_AT,
        content_sha256=CONTENT_DIGEST,
        execution_scope=HardwareExecutionMode.SIM,
    )
    return replace(observation, **overrides) if overrides else observation


def _evidence(
    contract: FrozenMissionContract, **overrides
) -> MissionRuntimeEvidence:
    evidence = MissionRuntimeEvidence(
        contract_sha256=contract.contract_sha256,
        observations=(_observation(),),
    )
    return replace(evidence, **overrides) if overrides else evidence


def _check(
    contract: FrozenMissionContract,
    evidence: MissionRuntimeEvidence | None = None,
    evaluated_at: str = EVALUATED_AT,
) -> MissionContractEvaluation:
    return check_mission_evidence_readiness(
        contract=contract,
        evidence=evidence if evidence is not None else _evidence(contract),
        evaluated_at=evaluated_at,
    )


def test_frozen_contract_carries_no_runtime_field() -> None:
    names = {item.name for item in fields(FrozenMissionContract)}

    assert names.isdisjoint(
        {
            "observations",
            "evaluated_outcome_claim",
            "termination_event",
            "closure_reason",
            "actual_verification_basis",
        }
    )


def test_runtime_evidence_cannot_declare_a_claim_or_basis() -> None:
    names = {item.name for item in fields(MissionRuntimeEvidence)}

    assert "evaluated_outcome_claim" not in names
    assert "actual_verification_basis" not in names


def test_ready_evidence_still_does_not_claim_the_outcome() -> None:
    result = _check(_contract())

    assert result.evidence_readiness is MissionEvidenceReadiness.READY
    assert result.status is MissionContractOutcomeStatus.UNVERIFIED
    assert result.evaluated_outcome_claim is False
    assert result.outcome_predicate_evaluated is False
    assert result.actual_verification_basis is VerificationBasis.UNVERIFIED
    assert OUTCOME_REQUIRES_PREDICATE_REASON in result.reasons


def test_failing_content_cannot_be_promoted_by_evidence_presence() -> None:
    """Readiness is about the evidence, never about what it says.

    An observation recording a failed terminal state is exactly as ready as one
    recording success. Neither may reach a verified outcome here.
    """

    failure = _observation(
        observation_id="observation-failed",
        content_sha256="c" * 64,
    )
    contract = _contract()

    result = _check(contract, _evidence(contract, observations=(failure,)))

    assert result.evidence_readiness is MissionEvidenceReadiness.READY
    assert result.evaluated_outcome_claim is False
    assert result.status is not MissionContractOutcomeStatus.BLOCKED


def test_outcome_status_vocabulary_has_no_verified_state() -> None:
    assert {member.value for member in MissionContractOutcomeStatus} == {
        "blocked",
        "unverified",
    }


def test_stored_artifact_origin_does_not_produce_a_deterministic_basis() -> None:
    contract = _contract(
        observation_requirements=(
            ObservationRequirement(
                requirement_id="terminal_state",
                evidence_kind="executor_terminal_state",
                required_origin=EvidenceOrigin.STORED_ARTIFACT,
                maximum_age_seconds=30.0,
            ),
        )
    )
    evidence = _evidence(
        contract,
        observations=(_observation(origin=EvidenceOrigin.STORED_ARTIFACT),),
    )

    result = _check(contract, evidence)

    assert result.evidence_readiness is MissionEvidenceReadiness.READY
    assert result.actual_verification_basis is VerificationBasis.UNVERIFIED


def test_evaluation_creates_no_authority() -> None:
    result = _check(_contract())

    assert result.approval_created is False
    assert result.dispatch_authority_created is False
    assert result.runtime_effect_requested is False
    assert result.physical_execution_invoked is False


def test_stale_observation_is_not_ready() -> None:
    contract = _contract(
        observation_requirements=(
            ObservationRequirement(
                requirement_id="terminal_state",
                evidence_kind="executor_terminal_state",
                required_origin=EvidenceOrigin.MACHINE_OBSERVED,
                maximum_age_seconds=0.1,
            ),
        )
    )

    result = _check(contract, _evidence(contract))

    assert "observation_stale:terminal_state" in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.INCOMPLETE


def test_unparseable_observation_time_is_not_ready() -> None:
    contract = _contract()
    evidence = _evidence(
        contract, observations=(_observation(observed_at="2000-01-01-not-a-time"),)
    )

    result = _check(contract, evidence)

    assert "observation_time_invalid:terminal_state" in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.INCOMPLETE


def test_observation_after_evaluation_time_is_not_ready() -> None:
    contract = _contract()
    evidence = _evidence(
        contract,
        observations=(_observation(observed_at="2026-07-28T12:00:20+00:00"),),
    )

    result = _check(contract, evidence)

    assert "observation_time_after_evaluation:terminal_state" in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.INCOMPLETE


def test_invalid_evaluation_time_is_not_ready() -> None:
    result = _check(_contract(), evaluated_at="")

    assert "evaluation_time_invalid" in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.INCOMPLETE


def test_source_time_in_a_different_clock_domain_is_not_compared() -> None:
    contract = _contract(
        observation_requirements=(
            ObservationRequirement(
                requirement_id="terminal_state",
                evidence_kind="executor_terminal_state",
                required_origin=EvidenceOrigin.MACHINE_OBSERVED,
                maximum_age_seconds=30.0,
                source_clock_domain_ref="clock:sim:episode-1",
            ),
        )
    )
    evidence = _evidence(
        contract,
        observations=(
            _observation(
                observed_at="1970-01-01T00:00:50+00:00",
                source_clock_domain_ref="clock:sim:episode-1",
            ),
        ),
    )

    result = _check(contract, evidence)

    assert (
        "observation_freshness_clock_domain_mismatch:terminal_state"
        in result.reasons
    )
    assert "observation_stale:terminal_state" not in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.INCOMPLETE


def test_receipt_freshness_keeps_source_and_receipt_times_separate() -> None:
    contract = _contract(
        observation_requirements=(
            ObservationRequirement(
                requirement_id="terminal_state",
                evidence_kind="executor_terminal_state",
                required_origin=EvidenceOrigin.MODEL_INFERRED,
                maximum_age_seconds=30.0,
                freshness_basis=(
                    ObservationFreshnessBasis.MISSION_RECEIVED_AT
                ),
                source_clock_domain_ref="clock:sim:episode-1",
                receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
                require_source_receipt_binding=True,
            ),
        )
    )
    binding_sha256 = mission_observation_source_receipt_binding_sha256(
        observation_id="observation-1",
        content_sha256=CONTENT_DIGEST,
        observed_at="1970-01-01T00:00:50+00:00",
        source_clock_domain_ref="clock:sim:episode-1",
        received_at=OBSERVED_AT,
        receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
    )
    observation = _observation(
        origin=EvidenceOrigin.MODEL_INFERRED,
        observed_at="1970-01-01T00:00:50+00:00",
        source_clock_domain_ref="clock:sim:episode-1",
        received_at=OBSERVED_AT,
        receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
        source_receipt_binding_sha256=binding_sha256,
    )

    result = _check(
        contract,
        _evidence(contract, observations=(observation,)),
    )

    assert result.evidence_readiness is MissionEvidenceReadiness.READY
    assert result.evaluated_outcome_claim is False
    assert result.actual_verification_basis is VerificationBasis.UNVERIFIED
    material = result.observations[0].to_material()
    assert material["observed_at"].startswith("1970-")
    assert material["received_at"] == OBSERVED_AT


def test_receipt_freshness_requires_content_bound_source_receipt_pair() -> None:
    contract = _contract(
        observation_requirements=(
            ObservationRequirement(
                requirement_id="terminal_state",
                evidence_kind="executor_terminal_state",
                required_origin=EvidenceOrigin.MODEL_INFERRED,
                maximum_age_seconds=30.0,
                freshness_basis=(
                    ObservationFreshnessBasis.MISSION_RECEIVED_AT
                ),
                source_clock_domain_ref="clock:sim:episode-1",
                receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
                require_source_receipt_binding=True,
            ),
        )
    )
    observation = _observation(
        origin=EvidenceOrigin.MODEL_INFERRED,
        observed_at="1970-01-01T00:00:50+00:00",
        source_clock_domain_ref="clock:sim:episode-1",
        received_at=OBSERVED_AT,
        receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
        source_receipt_binding_sha256=None,
    )

    result = _check(
        contract,
        _evidence(contract, observations=(observation,)),
    )

    assert (
        "observation_source_receipt_binding_invalid:terminal_state"
        in result.reasons
    )
    assert result.evidence_readiness is MissionEvidenceReadiness.INCOMPLETE


def test_receipt_binding_digest_must_match_the_exact_time_pair() -> None:
    contract = _contract(
        observation_requirements=(
            ObservationRequirement(
                requirement_id="terminal_state",
                evidence_kind="executor_terminal_state",
                required_origin=EvidenceOrigin.MODEL_INFERRED,
                maximum_age_seconds=30.0,
                freshness_basis=(
                    ObservationFreshnessBasis.MISSION_RECEIVED_AT
                ),
                source_clock_domain_ref="clock:sim:episode-1",
                receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
                require_source_receipt_binding=True,
            ),
        )
    )
    observation = _observation(
        origin=EvidenceOrigin.MODEL_INFERRED,
        observed_at="1970-01-01T00:00:50+00:00",
        source_clock_domain_ref="clock:sim:episode-1",
        received_at=OBSERVED_AT,
        receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
        source_receipt_binding_sha256="d" * 64,
    )

    result = _check(
        contract,
        _evidence(contract, observations=(observation,)),
    )

    assert (
        "observation_source_receipt_binding_mismatch:terminal_state"
        in result.reasons
    )
    assert result.evidence_readiness is MissionEvidenceReadiness.INCOMPLETE


def test_receipt_freshness_requirement_without_binding_is_refused() -> None:
    contract = _contract(
        observation_requirements=(
            ObservationRequirement(
                requirement_id="terminal_state",
                evidence_kind="executor_terminal_state",
                required_origin=EvidenceOrigin.MODEL_INFERRED,
                maximum_age_seconds=30.0,
                freshness_basis=(
                    ObservationFreshnessBasis.MISSION_RECEIVED_AT
                ),
                source_clock_domain_ref="clock:sim:episode-1",
                receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
                require_source_receipt_binding=False,
            ),
        )
    )

    result = _check(contract)

    assert (
        "observation_requirement_source_receipt_binding_required:terminal_state"
        in result.reasons
    )
    assert result.evidence_readiness is MissionEvidenceReadiness.REFUSED


def test_source_freshness_binding_requirement_is_not_ignored() -> None:
    contract = _contract(
        observation_requirements=(
            ObservationRequirement(
                requirement_id="terminal_state",
                evidence_kind="executor_terminal_state",
                required_origin=EvidenceOrigin.MACHINE_OBSERVED,
                maximum_age_seconds=30.0,
                freshness_basis=(
                    ObservationFreshnessBasis.SOURCE_OBSERVED_AT
                ),
                source_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
                receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
                require_source_receipt_binding=True,
            ),
        )
    )

    result = _check(contract)

    assert (
        "observation_receipt_time_invalid:terminal_state" in result.reasons
    )
    assert (
        "observation_receipt_clock_domain_missing:terminal_state"
        in result.reasons
    )
    assert (
        "observation_source_receipt_binding_invalid:terminal_state"
        in result.reasons
    )
    assert result.evidence_readiness is MissionEvidenceReadiness.INCOMPLETE


def test_source_binding_requirement_without_receipt_domain_is_refused() -> None:
    contract = _contract(
        observation_requirements=(
            ObservationRequirement(
                requirement_id="terminal_state",
                evidence_kind="executor_terminal_state",
                required_origin=EvidenceOrigin.MACHINE_OBSERVED,
                maximum_age_seconds=30.0,
                freshness_basis=(
                    ObservationFreshnessBasis.SOURCE_OBSERVED_AT
                ),
                require_source_receipt_binding=True,
            ),
        )
    )

    result = _check(contract)

    assert (
        "observation_requirement_receipt_clock_domain_missing:terminal_state"
        in result.reasons
    )
    assert result.evidence_readiness is MissionEvidenceReadiness.REFUSED


def test_source_freshness_can_require_an_exact_receipt_binding() -> None:
    contract = _contract(
        observation_requirements=(
            ObservationRequirement(
                requirement_id="terminal_state",
                evidence_kind="executor_terminal_state",
                required_origin=EvidenceOrigin.MACHINE_OBSERVED,
                maximum_age_seconds=30.0,
                freshness_basis=(
                    ObservationFreshnessBasis.SOURCE_OBSERVED_AT
                ),
                source_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
                receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
                require_source_receipt_binding=True,
            ),
        )
    )
    binding_sha256 = mission_observation_source_receipt_binding_sha256(
        observation_id="observation-1",
        content_sha256=CONTENT_DIGEST,
        observed_at=OBSERVED_AT,
        source_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
        received_at="2026-07-28T12:00:05+00:00",
        receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
    )
    observation = _observation(
        received_at="2026-07-28T12:00:05+00:00",
        receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
        source_receipt_binding_sha256=binding_sha256,
    )

    result = _check(
        contract,
        _evidence(contract, observations=(observation,)),
    )

    assert result.evidence_readiness is MissionEvidenceReadiness.READY


def test_observation_outside_the_frozen_execution_scope_is_not_ready() -> None:
    contract = _contract()
    evidence = _evidence(
        contract,
        observations=(_observation(execution_scope=HardwareExecutionMode.FIELD),),
    )

    result = _check(contract, evidence)

    assert (
        "observation_execution_scope_mismatch:terminal_state" in result.reasons
    )
    assert result.evidence_readiness is MissionEvidenceReadiness.INCOMPLETE


def test_observation_without_a_content_digest_is_not_ready() -> None:
    contract = _contract()
    evidence = _evidence(
        contract, observations=(_observation(content_sha256="not-a-digest"),)
    )

    result = _check(contract, evidence)

    assert "observation_content_digest_invalid:terminal_state" in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.INCOMPLETE


def test_non_positive_maximum_age_is_refused() -> None:
    contract = _contract(
        observation_requirements=(
            ObservationRequirement(
                requirement_id="terminal_state",
                evidence_kind="executor_terminal_state",
                required_origin=EvidenceOrigin.MACHINE_OBSERVED,
                maximum_age_seconds=0.0,
            ),
        )
    )

    assert "observation_requirement_age_not_positive:terminal_state" in (
        validate_frozen_mission_contract(contract)
    )


@pytest.mark.parametrize(
    "maximum_age_seconds",
    [float("nan"), float("inf"), -float("inf")],
)
def test_non_finite_maximum_age_is_refused(
    maximum_age_seconds: float,
) -> None:
    contract = _contract(
        observation_requirements=(
            ObservationRequirement(
                requirement_id="terminal_state",
                evidence_kind="executor_terminal_state",
                required_origin=EvidenceOrigin.MACHINE_OBSERVED,
                maximum_age_seconds=maximum_age_seconds,
            ),
        )
    )

    result = _check(
        contract,
        _evidence(
            contract,
            observations=(
                _observation(observed_at="2000-01-01T00:00:00+00:00"),
            ),
        ),
    )

    assert "observation_requirement_age_not_finite:terminal_state" in (
        result.reasons
    )
    assert result.evidence_readiness is MissionEvidenceReadiness.REFUSED
    assert result.status is MissionContractOutcomeStatus.BLOCKED


def test_missing_quantification_scope_reason_is_refused() -> None:
    contract = _contract(
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE
        )
    )

    reasons = validate_frozen_mission_contract(contract)

    assert "quantification_scope_none_reason_missing" in reasons


def test_explicit_none_quantification_scope_is_accepted() -> None:
    contract = _contract(
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason="the claim ranges over one approved action only",
        )
    )

    assert validate_frozen_mission_contract(contract) == ()
    assert _check(contract).evidence_readiness is MissionEvidenceReadiness.READY


def test_universal_claim_without_quantification_scope_is_refused() -> None:
    contract = _contract(
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason="not quantified",
        ),
        outcome_claim_spec=OutcomeClaimSpec(
            claim_id="all_members_recovered",
            statement="All members were recovered.",
            claim_scope="sim_action",
            universal=True,
        ),
    )

    result = _check(contract)

    assert "universal_claim_without_quantification_scope" in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.REFUSED
    assert result.status is MissionContractOutcomeStatus.BLOCKED


def test_unbound_quantification_scope_reference_is_refused() -> None:
    contract = _contract(
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.SPATIAL_REGION,
            scope_ref="unapproved_region",
        )
    )

    result = _check(contract)

    assert (
        "quantification_scope_ref_unbound:unapproved_region" in result.reasons
    )
    assert result.evidence_readiness is MissionEvidenceReadiness.REFUSED


def test_enumerated_members_must_not_be_empty() -> None:
    contract = _contract(
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.ENUMERATED_MEMBERS
        )
    )

    assert "quantification_scope_members_missing" in (
        validate_frozen_mission_contract(contract)
    )


def test_runtime_evidence_bound_to_another_contract_is_refused() -> None:
    contract = _contract()
    other = _contract(contract_id="contract-2")

    result = _check(
        contract, _evidence(contract, contract_sha256=other.contract_sha256)
    )

    assert "runtime_evidence_contract_binding_mismatch" in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.REFUSED
    assert result.status is MissionContractOutcomeStatus.BLOCKED


def test_contract_digest_changes_when_frozen_material_changes() -> None:
    contract = _contract()

    assert contract.contract_sha256 != _contract(
        required_verification_basis=VerificationBasis.MODEL_INFERRED
    ).contract_sha256
    assert contract.contract_sha256 != _contract(
        execution_scope=HardwareExecutionMode.FIELD
    ).contract_sha256
    assert contract.contract_sha256 != _contract(
        predicate_package=PredicatePackageBinding(
            package_id="another-package",
            package_version="1",
            content_sha256=PREDICATE_PACKAGE_DIGEST,
        )
    ).contract_sha256


def test_missing_observation_is_not_ready() -> None:
    contract = _contract()

    result = _check(contract, _evidence(contract, observations=()))

    assert "observation_missing:terminal_state" in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.INCOMPLETE


def test_wrong_origin_observation_is_not_ready() -> None:
    contract = _contract()
    evidence = _evidence(
        contract,
        observations=(_observation(origin=EvidenceOrigin.OPERATOR_DECLARED),),
    )

    result = _check(contract, evidence)

    assert "observation_origin_mismatch:terminal_state" in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.INCOMPLETE


def test_operator_interruption_is_recorded_without_an_outcome() -> None:
    contract = _contract()
    evidence = _evidence(
        contract,
        termination_event=TerminationEvent(
            reason=TerminationReason.OPERATOR_INTERRUPTION,
            occurred_at=OBSERVED_AT,
        ),
        closure_reason="operator ended the operation",
    )

    result = _check(contract, evidence)

    assert result.evaluated_outcome_claim is False
    assert result.closure_reason == "operator ended the operation"
    assert result.status is MissionContractOutcomeStatus.UNVERIFIED


def test_terminal_predicate_termination_still_claims_nothing() -> None:
    contract = _contract()
    evidence = _evidence(
        contract,
        termination_event=TerminationEvent(
            reason=TerminationReason.TERMINAL_PREDICATE_SATISFIED,
            occurred_at=OBSERVED_AT,
        ),
    )

    result = _check(contract, evidence)

    assert result.evaluated_outcome_claim is False
    assert result.status is MissionContractOutcomeStatus.UNVERIFIED


def test_termination_reason_outside_the_frozen_policy_is_refused() -> None:
    contract = _contract(
        termination_policy=TerminationPolicy(
            allowed_reasons=(TerminationReason.TERMINAL_PREDICATE_SATISFIED,)
        )
    )
    evidence = _evidence(
        contract,
        termination_event=TerminationEvent(
            reason=TerminationReason.OPERATOR_INTERRUPTION,
            occurred_at=OBSERVED_AT,
        ),
    )

    result = _check(contract, evidence)

    assert (
        "termination_reason_not_allowed:operator_interruption"
        in result.reasons
    )
    assert result.evidence_readiness is MissionEvidenceReadiness.REFUSED


def test_termination_without_a_parseable_time_is_refused() -> None:
    contract = _contract()
    evidence = _evidence(
        contract,
        termination_event=TerminationEvent(
            reason=TerminationReason.SAFE_STOP, occurred_at="whenever"
        ),
    )

    result = _check(contract, evidence)

    assert "termination_time_invalid" in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.REFUSED


def test_closure_reason_without_termination_event_is_refused() -> None:
    contract = _contract()

    result = _check(
        contract, _evidence(contract, closure_reason="declared complete")
    )

    assert "closure_reason_without_termination_event" in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.REFUSED


def test_unverified_required_basis_is_refused() -> None:
    contract = _contract(
        required_verification_basis=VerificationBasis.UNVERIFIED
    )

    assert "required_verification_basis_unverified" in (
        validate_frozen_mission_contract(contract)
    )


def test_invalid_required_basis_is_structurally_refused_without_raising() -> None:
    valid_contract = _contract()
    invalid_contract = replace(
        valid_contract,
        required_verification_basis="not-a-verification-basis",
    )

    result = _check(invalid_contract, _evidence(valid_contract))

    assert "required_verification_basis_invalid" in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.REFUSED
    assert result.status is MissionContractOutcomeStatus.BLOCKED


@pytest.mark.parametrize(
    ("contract_override", "expected_reason"),
    [
        (
            {
                "quantification_scope": QuantificationScope(
                    kind="not-a-quantification-kind",
                    scope_ref="approved_region",
                )
            },
            "quantification_scope_missing",
        ),
        (
            {
                "observation_requirements": (
                    ObservationRequirement(
                        requirement_id="terminal_state",
                        evidence_kind="executor_terminal_state",
                        required_origin="not-an-evidence-origin",
                        maximum_age_seconds=30.0,
                    ),
                )
            },
            "observation_requirement_origin_invalid:terminal_state",
        ),
        (
            {
                "termination_policy": TerminationPolicy(
                    allowed_reasons=("not-a-termination-reason",),
                )
            },
            "termination_policy_reason_invalid:0",
        ),
    ],
)
def test_invalid_nested_enum_is_structurally_refused_without_raising(
    contract_override: dict[str, object],
    expected_reason: str,
) -> None:
    valid_contract = _contract()
    invalid_contract = replace(valid_contract, **contract_override)

    result = _check(invalid_contract, _evidence(valid_contract))

    assert expected_reason in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.REFUSED
    assert result.status is MissionContractOutcomeStatus.BLOCKED


@pytest.mark.parametrize(
    ("maximum_duration_seconds", "expected_reason"),
    [
        (0.0, "termination_policy_maximum_duration_not_positive"),
        (-1.0, "termination_policy_maximum_duration_not_positive"),
        (float("nan"), "termination_policy_maximum_duration_not_finite"),
        (float("inf"), "termination_policy_maximum_duration_not_finite"),
        (-float("inf"), "termination_policy_maximum_duration_not_finite"),
        ("three", "termination_policy_maximum_duration_invalid"),
    ],
)
def test_invalid_maximum_duration_is_refused(
    maximum_duration_seconds: object,
    expected_reason: str,
) -> None:
    contract = _contract(
        termination_policy=TerminationPolicy(
            allowed_reasons=(TerminationReason.EXPIRY,),
            maximum_duration_seconds=maximum_duration_seconds,
        )
    )

    result = _check(contract)

    assert expected_reason in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.REFUSED
    assert result.status is MissionContractOutcomeStatus.BLOCKED


def test_reference_input_without_digest_is_refused() -> None:
    contract = _contract(
        reference_inputs=(
            ReferenceInput(
                input_id="approved_region",
                kind="approved_spatial_region",
                content_sha256="not-a-digest",
            ),
        )
    )

    assert "reference_input_digest_invalid:approved_region" in (
        validate_frozen_mission_contract(contract)
    )


@pytest.mark.parametrize(
    ("binding", "expected_reason"),
    [
        (
            PredicatePackageBinding(
                package_id="",
                package_version="1",
                content_sha256=PREDICATE_PACKAGE_DIGEST,
            ),
            "predicate_package_id_missing",
        ),
        (
            PredicatePackageBinding(
                package_id="bounded_action_completed",
                package_version="",
                content_sha256=PREDICATE_PACKAGE_DIGEST,
            ),
            "predicate_package_version_missing",
        ),
        (
            PredicatePackageBinding(
                package_id="bounded_action_completed",
                package_version="1",
                content_sha256="not-a-digest",
            ),
            "predicate_package_digest_invalid",
        ),
    ],
)
def test_invalid_predicate_package_binding_is_refused(
    binding: PredicatePackageBinding,
    expected_reason: str,
) -> None:
    contract = _contract(predicate_package=binding)

    result = _check(contract)

    assert expected_reason in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.REFUSED
    assert result.status is MissionContractOutcomeStatus.BLOCKED


def test_contract_without_observation_requirements_is_refused() -> None:
    contract = _contract(observation_requirements=())

    assert "observation_requirements_missing" in (
        validate_frozen_mission_contract(contract)
    )


def test_unrequired_observation_is_reported() -> None:
    contract = _contract()
    evidence = _evidence(
        contract,
        observations=(
            _observation(),
            _observation(
                observation_id="observation-2",
                requirement_id="unrequested_state",
            ),
        ),
    )

    result = _check(contract, evidence)

    assert "observation_not_required:unrequested_state" in result.reasons
    assert result.evidence_readiness is MissionEvidenceReadiness.INCOMPLETE


def test_contract_round_trips_through_stored_material() -> None:
    contract = _contract()

    restored = frozen_contract_from_mapping(contract.to_material())

    assert restored is not None
    assert restored.contract_sha256 == contract.contract_sha256


def test_unparseable_stored_material_is_refused() -> None:
    material = _contract().to_material()
    material.pop("outcome_claim_spec")

    assert frozen_contract_from_mapping(material) is None


def test_stored_material_without_schema_version_is_not_silently_upgraded() -> None:
    material = _contract().to_material()
    material.pop("schema_version")

    assert frozen_contract_from_mapping(material) is None


def test_v1_material_without_predicate_package_is_not_silently_upgraded() -> None:
    material = _contract().to_material()
    material["schema_version"] = "missionos_core_mission_contract.v1"
    material.pop("predicate_package")

    assert frozen_contract_from_mapping(material) is None


def test_evaluation_record_serializes_the_runtime_fields() -> None:
    payload = _check(_contract()).to_dict()

    assert set(payload).issuperset(
        {
            "observations",
            "evaluated_outcome_claim",
            "termination_event",
            "closure_reason",
            "actual_verification_basis",
            "evidence_readiness",
            "outcome_predicate_evaluated",
        }
    )
    assert payload["evaluated_outcome_claim"] is False
    assert payload["actual_verification_basis"] == "unverified"
    assert payload["physical_execution_invoked"] is False
