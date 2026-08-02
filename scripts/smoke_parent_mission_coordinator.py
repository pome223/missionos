"""Fixture smoke for the backend-neutral parent mission coordinator."""

from __future__ import annotations

import json

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
    TerminationPolicy,
    TerminationReason,
    VerificationBasis,
    build_parent_mission_approval_binding,
    build_parent_mission_stage_binding,
    canonical_sha256,
)
from src.runtime.parent_mission_coordinator import (
    run_parent_mission_coordinator,
)


def _child(name: str) -> FrozenMissionContract:
    return FrozenMissionContract(
        contract_id=f"smoke:{name}",
        contract_version="v1",
        execution_scope=HardwareExecutionMode.LOOPBACK,
        reference_inputs=(
            ReferenceInput(
                input_id=f"{name}_input",
                kind="smoke_fixture",
                content_sha256=canonical_sha256({"input": name}),
            ),
        ),
        observation_requirements=(
            ObservationRequirement(
                requirement_id=f"{name}_result",
                evidence_kind=f"smoke_{name}_result",
                required_origin=EvidenceOrigin.STORED_ARTIFACT,
                maximum_age_seconds=30.0,
            ),
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason="One loopback fixture result.",
        ),
        outcome_claim_spec=OutcomeClaimSpec(
            claim_id=f"{name}_completed",
            statement=f"The {name} loopback fixture completed.",
            claim_scope=f"loopback:{name}",
        ),
        predicate_package=PredicatePackageBinding(
            package_id=f"{name}_predicate",
            package_version="v1",
            content_sha256=canonical_sha256({"predicate": name}),
        ),
        termination_policy=TerminationPolicy(
            allowed_reasons=(
                TerminationReason.EXPIRY,
                TerminationReason.TERMINAL_PREDICATE_SATISFIED,
            ),
        ),
        required_verification_basis=VerificationBasis.DETERMINISTIC,
    )


def main() -> None:
    children = (_child("first"), _child("second"))
    parent = FrozenParentMissionContract(
        parent_mission_id="smoke:parent",
        parent_mission_version="v1",
        shared_target_descriptor_sha256=canonical_sha256(
            {"descriptor": "smoke-only"}
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason="Loopback authority-lineage smoke only.",
        ),
        stages=tuple(
            build_parent_mission_stage_binding(
                stage_index=index,
                stage_ref=f"stage_{index}",
                executor_ref=f"loopback:{index}",
                child_contract=child,
            )
            for index, child in enumerate(children, start=1)
        ),
    )
    approval = build_parent_mission_approval_binding(
        contract=parent,
        operator_approval_ref="approval:smoke:parent",
        authority_bundle_ref="catalog:smoke:parent:v1",
    )

    def evaluation(stage_index: int) -> dict:
        stage = parent.stages[stage_index - 1]
        return {
            "contract_id": stage.child_contract_id,
            "contract_sha256": stage.child_contract_sha256,
            "predicate_package_id": stage.predicate_package.package_id,
            "predicate_package_version": (
                stage.predicate_package.package_version
            ),
            "predicate_package_sha256": (
                stage.predicate_package.content_sha256
            ),
            "status": "satisfied",
            "evaluated_outcome_claim": True,
            "actual_verification_basis": "deterministic",
            "predicate_package_evaluated": True,
            "approval_created": False,
            "dispatch_authority_created": False,
            "runtime_effect_requested": False,
            "operational_closure_created": False,
            "physical_execution_invoked": False,
        }

    record = run_parent_mission_coordinator(
        contract=parent,
        approval=approval,
        stage_runners={
            "stage_1": lambda: evaluation(1),
            "stage_2": lambda: evaluation(2),
        },
    )
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    if (
        record["coordinator_status"] != "stages_satisfied"
        or record["stages_satisfied"] != 2
        or record["mission_completion_claimed"] is not False
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
