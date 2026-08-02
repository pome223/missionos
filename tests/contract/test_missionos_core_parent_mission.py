from __future__ import annotations

from dataclasses import replace

import pytest

from missionos_core import (
    EvidenceOrigin,
    FrozenMissionContract,
    FrozenParentMissionContract,
    HardwareExecutionMode,
    ObservationRequirement,
    OutcomeClaimSpec,
    PredicatePackageBinding,
    QuantificationScope,
    QuantificationScopeKind,
    ReferenceInput,
    SHARED_TARGET_DESCRIPTOR_REF,
    TerminationPolicy,
    TerminationReason,
    VerificationBasis,
    bind_parent_mission_stage_result,
    build_parent_mission_approval_binding,
    build_parent_mission_stage_binding,
    canonical_sha256,
    evaluate_parent_mission_transition_authority,
    validate_frozen_parent_mission_contract,
    validate_parent_mission_approval_binding,
)


def _child_contract(name: str) -> FrozenMissionContract:
    return FrozenMissionContract(
        contract_id=f"child:{name}",
        contract_version="v1",
        execution_scope=HardwareExecutionMode.SIM,
        reference_inputs=(
            ReferenceInput(
                input_id=f"{name}_input",
                kind="approved_fixture_input",
                content_sha256=canonical_sha256({"name": name}),
            ),
        ),
        observation_requirements=(
            ObservationRequirement(
                requirement_id=f"{name}_result",
                evidence_kind=f"fixture_{name}_result",
                required_origin=EvidenceOrigin.STORED_ARTIFACT,
                maximum_age_seconds=30.0,
            ),
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason="The child claim concerns one bounded fixture result.",
        ),
        outcome_claim_spec=OutcomeClaimSpec(
            claim_id=f"{name}_completed",
            statement=f"The bounded {name} fixture completed.",
            claim_scope=f"fixture:{name}",
        ),
        predicate_package=PredicatePackageBinding(
            package_id=f"fixture_{name}_predicate",
            package_version="v1",
            content_sha256=canonical_sha256({"package": name}),
        ),
        termination_policy=TerminationPolicy(
            allowed_reasons=(
                TerminationReason.EXPIRY,
                TerminationReason.OPERATOR_INTERRUPTION,
                TerminationReason.SAFE_STOP,
                TerminationReason.TERMINAL_PREDICATE_SATISFIED,
            ),
        ),
        required_verification_basis=VerificationBasis.DETERMINISTIC,
    )


def _parent(
    *,
    descriptor: str = "approved-target-v1",
) -> tuple[
    FrozenParentMissionContract,
    FrozenMissionContract,
    FrozenMissionContract,
]:
    first = _child_contract("first")
    second = _child_contract("second")
    parent = FrozenParentMissionContract(
        parent_mission_id="parent:fixture",
        parent_mission_version="v1",
        shared_target_descriptor_sha256=canonical_sha256(
            {"descriptor": descriptor}
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason=(
                "The first implementation proves authority lineage only; "
                "it does not quantify a shared physical object."
            ),
        ),
        stages=(
            build_parent_mission_stage_binding(
                stage_index=1,
                stage_ref="stage_1",
                executor_ref="executor:first",
                child_contract=first,
            ),
            build_parent_mission_stage_binding(
                stage_index=2,
                stage_ref="stage_2",
                executor_ref="executor:second",
                child_contract=second,
            ),
        ),
    )
    return parent, first, second


def _approval(parent: FrozenParentMissionContract):
    return build_parent_mission_approval_binding(
        contract=parent,
        operator_approval_ref="approval:operator:fixture",
        authority_bundle_ref="catalog:fixture:bundle:v1",
    )


def _predicate_evaluation(
    *,
    parent: FrozenParentMissionContract,
    stage_index: int,
    status: str = "satisfied",
) -> dict:
    stage = parent.stages[stage_index - 1]
    return {
        "contract_id": stage.child_contract_id,
        "contract_sha256": stage.child_contract_sha256,
        "predicate_package_id": stage.predicate_package.package_id,
        "predicate_package_version": stage.predicate_package.package_version,
        "predicate_package_sha256": stage.predicate_package.content_sha256,
        "status": status,
        "evaluated_outcome_claim": status == "satisfied",
        "actual_verification_basis": "deterministic",
        "predicate_package_evaluated": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "runtime_effect_requested": False,
        "operational_closure_created": False,
        "physical_execution_invoked": False,
    }


def test_parent_approval_binds_ordered_child_contracts_and_clock_material() -> None:
    parent, first, second = _parent()
    approval = _approval(parent)

    assert validate_frozen_parent_mission_contract(parent) == ()
    assert (
        validate_parent_mission_approval_binding(
            contract=parent,
            approval=approval,
        )
        == ()
    )
    assert approval.parent_mission_sha256 == parent.parent_mission_sha256
    assert approval.approved_stage_binding_sha256s == tuple(
        stage.stage_binding_sha256 for stage in parent.stages
    )
    assert [stage.child_contract_sha256 for stage in parent.stages] == [
        first.contract_sha256,
        second.contract_sha256,
    ]
    assert [
        clock.source_clock_domain_ref
        for stage in parent.stages
        for clock in stage.observation_clock_bindings
    ] == ["clock:utc-wall", "clock:utc-wall"]
    assert parent.identity_continuity_claimed is False
    assert parent.shared_world_claimed is False


def test_first_stage_authority_comes_only_from_preexisting_approval() -> None:
    parent, _, _ = _parent()
    transition = evaluate_parent_mission_transition_authority(
        contract=parent,
        approval=_approval(parent),
        target_stage_index=1,
        target_stage_ref="stage_1",
        previous_stage_result=None,
        previous_predicate_evaluation=None,
    )

    assert transition.transition_status == "authorized"
    assert transition.dispatch_authority_present is True
    assert (
        transition.dispatch_authority_source
        == "preexisting_mission_approval"
    )
    assert transition.prerequisite_predicate_satisfied is None
    assert transition.approval_created is False
    assert transition.dispatch_authority_created is False
    assert transition.runtime_effect_requested is False
    assert transition.mission_completion_claimed is False
    assert transition.mission_completion_status == "unverified"


def test_second_stage_requires_exact_satisfied_first_stage_result() -> None:
    parent, _, _ = _parent()
    first_result = bind_parent_mission_stage_result(
        contract=parent,
        stage_index=1,
        predicate_evaluation=(
            first_evaluation := _predicate_evaluation(
                parent=parent,
                stage_index=1,
            )
        ),
    )
    transition = evaluate_parent_mission_transition_authority(
        contract=parent,
        approval=_approval(parent),
        target_stage_index=2,
        target_stage_ref="stage_2",
        previous_stage_result=first_result,
        previous_predicate_evaluation=first_evaluation,
    )

    assert first_result.lineage_verified is True
    assert first_result.predicate_satisfied is True
    assert transition.transition_status == "authorized"
    assert transition.prerequisite_stage_ref == "stage_1"
    assert transition.prerequisite_predicate_satisfied is True
    assert transition.dispatch_authority_present is True
    assert transition.dispatch_authority_created is False
    assert transition.mission_completion_claimed is False


@pytest.mark.parametrize(
    "mutation,expected_reason",
    [
        (
            "missing_approval",
            "parent_mission_approval_binding_missing",
        ),
        (
            "target_ref",
            "parent_mission_transition_target_ref_mismatch",
        ),
        (
            "missing_prerequisite",
            "parent_mission_transition_prerequisite_result_missing",
        ),
        (
            "unsatisfied_prerequisite",
            "parent_mission_transition_prerequisite_not_satisfied",
        ),
        (
            "other_parent",
            "parent_mission_transition_prerequisite_parent_mismatch",
        ),
    ],
)
def test_transition_rejects_missing_reordered_or_reused_material(
    mutation: str,
    expected_reason: str,
) -> None:
    parent, _, _ = _parent()
    approval = _approval(parent)
    target_ref = "stage_2"
    previous = bind_parent_mission_stage_result(
        contract=parent,
        stage_index=1,
        predicate_evaluation=(
            previous_evaluation := _predicate_evaluation(
                parent=parent,
                stage_index=1,
            )
        ),
    )
    if mutation == "missing_approval":
        approval = None
    elif mutation == "target_ref":
        target_ref = "stage_1"
    elif mutation == "missing_prerequisite":
        previous = None
        previous_evaluation = None
    elif mutation == "unsatisfied_prerequisite":
        previous_evaluation = _predicate_evaluation(
            parent=parent,
            stage_index=1,
            status="not_satisfied",
        )
        previous = bind_parent_mission_stage_result(
            contract=parent,
            stage_index=1,
            predicate_evaluation=previous_evaluation,
        )
    elif mutation == "other_parent":
        other, _, _ = _parent(descriptor="other-approved-target")
        previous_evaluation = _predicate_evaluation(
            parent=other,
            stage_index=1,
        )
        previous = bind_parent_mission_stage_result(
            contract=other,
            stage_index=1,
            predicate_evaluation=previous_evaluation,
        )

    transition = evaluate_parent_mission_transition_authority(
        contract=parent,
        approval=approval,
        target_stage_index=2,
        target_stage_ref=target_ref,
        previous_stage_result=previous,
        previous_predicate_evaluation=previous_evaluation,
    )

    assert transition.transition_status == "blocked"
    assert transition.dispatch_authority_present is False
    assert transition.dispatch_authority_source is None
    assert expected_reason in transition.blocking_reasons
    assert transition.mission_completion_claimed is False


def test_stage_order_and_descriptor_are_approval_bound() -> None:
    parent, _, _ = _parent()
    approval = _approval(parent)
    swapped = replace(parent, stages=(parent.stages[1], parent.stages[0]))
    different_descriptor = replace(
        parent,
        shared_target_descriptor_sha256=canonical_sha256(
            {"descriptor": "different"}
        ),
    )

    swapped_reasons = validate_parent_mission_approval_binding(
        contract=swapped,
        approval=approval,
    )
    descriptor_reasons = validate_parent_mission_approval_binding(
        contract=different_descriptor,
        approval=approval,
    )

    assert "parent_mission_stage:1:index_invalid" in swapped_reasons
    assert "parent_mission_approval_contract_digest_mismatch" in swapped_reasons
    assert (
        "parent_mission_approval_shared_target_descriptor_mismatch"
        in descriptor_reasons
    )


def test_transition_rebinds_stored_result_to_exact_predicate_evaluation() -> None:
    parent, _, _ = _parent()
    original_evaluation = _predicate_evaluation(
        parent=parent,
        stage_index=1,
    )
    stored_result = bind_parent_mission_stage_result(
        contract=parent,
        stage_index=1,
        predicate_evaluation=original_evaluation,
    )
    changed_evaluation = {
        **original_evaluation,
        "observation_content_sha256": "f" * 64,
    }

    transition = evaluate_parent_mission_transition_authority(
        contract=parent,
        approval=_approval(parent),
        target_stage_index=2,
        target_stage_ref="stage_2",
        previous_stage_result=stored_result,
        previous_predicate_evaluation=changed_evaluation,
    )

    assert transition.transition_status == "blocked"
    assert (
        "parent_mission_transition_prerequisite_binding_mismatch"
        in transition.blocking_reasons
    )


def test_predicate_result_cannot_claim_new_authority() -> None:
    parent, _, _ = _parent()
    evaluation = {
        **_predicate_evaluation(parent=parent, stage_index=1),
        "dispatch_authority_created": True,
    }

    result = bind_parent_mission_stage_result(
        contract=parent,
        stage_index=1,
        predicate_evaluation=evaluation,
    )

    assert result.lineage_verified is False
    assert result.predicate_satisfied is False
    assert (
        "parent_mission_stage_result_dispatch_authority_created_forbidden"
        in result.reasons
    )


def test_quantification_scope_is_required_even_without_quantification() -> None:
    parent, _, _ = _parent()
    missing_reason = replace(
        parent,
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
        ),
    )
    unbound_scope = replace(
        parent,
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.SPATIAL_REGION,
            scope_ref="child_only_reference",
        ),
    )
    shared_descriptor_scope = replace(
        parent,
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.SPATIAL_REGION,
            scope_ref=SHARED_TARGET_DESCRIPTOR_REF,
        ),
    )

    assert (
        "parent_mission_quantification_scope_reason_missing"
        in validate_frozen_parent_mission_contract(missing_reason)
    )
    assert (
        "parent_mission_quantification_scope_ref_invalid"
        in validate_frozen_parent_mission_contract(unbound_scope)
    )
    assert (
        validate_frozen_parent_mission_contract(shared_descriptor_scope)
        == ()
    )


def test_child_contracts_are_projected_without_mutation() -> None:
    parent, first, second = _parent()
    before = (first.to_material(), second.to_material())

    _ = parent.stages

    assert (first.to_material(), second.to_material()) == before
    assert [item.input_id for item in first.reference_inputs] == [
        "first_input"
    ]
    assert [item.input_id for item in second.reference_inputs] == [
        "second_input"
    ]
