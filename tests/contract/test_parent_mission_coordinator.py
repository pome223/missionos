from __future__ import annotations

from collections.abc import Callable

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
        contract_id=f"coordinator:{name}",
        contract_version="v1",
        execution_scope=HardwareExecutionMode.SIM,
        reference_inputs=(
            ReferenceInput(
                input_id=f"{name}_input",
                kind="fixture",
                content_sha256=canonical_sha256({"input": name}),
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
            reason="One fixture result.",
        ),
        outcome_claim_spec=OutcomeClaimSpec(
            claim_id=f"{name}_completed",
            statement=f"The {name} fixture completed.",
            claim_scope=f"fixture:{name}",
        ),
        predicate_package=PredicatePackageBinding(
            package_id=f"{name}_predicate",
            package_version="v1",
            content_sha256=canonical_sha256({"package": name}),
        ),
        termination_policy=TerminationPolicy(
            allowed_reasons=(
                TerminationReason.EXPIRY,
                TerminationReason.TERMINAL_PREDICATE_SATISFIED,
            ),
        ),
        required_verification_basis=VerificationBasis.DETERMINISTIC,
    )


def _setup():
    first = _child("first")
    second = _child("second")
    parent = FrozenParentMissionContract(
        parent_mission_id="coordinator:parent",
        parent_mission_version="v1",
        shared_target_descriptor_sha256=canonical_sha256(
            {"descriptor": "coordinator-fixture"}
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason="Authority lineage only; no shared physical object.",
        ),
        stages=(
            build_parent_mission_stage_binding(
                stage_index=1,
                stage_ref="stage_1",
                executor_ref="fixture:first",
                child_contract=first,
            ),
            build_parent_mission_stage_binding(
                stage_index=2,
                stage_ref="stage_2",
                executor_ref="fixture:second",
                child_contract=second,
            ),
        ),
    )
    approval = build_parent_mission_approval_binding(
        contract=parent,
        operator_approval_ref="approval:coordinator",
        authority_bundle_ref="catalog:coordinator:v1",
    )
    return parent, approval


def _evaluation(parent, stage_index: int, *, satisfied: bool = True) -> dict:
    stage = parent.stages[stage_index - 1]
    return {
        "contract_id": stage.child_contract_id,
        "contract_sha256": stage.child_contract_sha256,
        "predicate_package_id": stage.predicate_package.package_id,
        "predicate_package_version": stage.predicate_package.package_version,
        "predicate_package_sha256": stage.predicate_package.content_sha256,
        "status": "satisfied" if satisfied else "not_satisfied",
        "evaluated_outcome_claim": satisfied,
        "actual_verification_basis": "deterministic",
        "predicate_package_evaluated": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "runtime_effect_requested": False,
        "operational_closure_created": False,
        "physical_execution_invoked": False,
    }


def _runner(
    result: dict,
    calls: list[str],
    stage_ref: str,
) -> Callable[[], dict]:
    def run() -> dict:
        calls.append(stage_ref)
        return result

    return run


def test_coordinator_runs_exact_order_without_promoting_stage_conjunction() -> None:
    parent, approval = _setup()
    calls: list[str] = []

    record = run_parent_mission_coordinator(
        contract=parent,
        approval=approval,
        stage_runners={
            "stage_1": _runner(
                _evaluation(parent, 1),
                calls,
                "stage_1",
            ),
            "stage_2": _runner(
                _evaluation(parent, 2),
                calls,
                "stage_2",
            ),
        },
    )

    assert calls == ["stage_1", "stage_2"]
    assert record["stages_satisfied"] == 2
    assert record["coordinator_status"] == "stages_satisfied"
    assert record["blocking_reasons"] == []
    assert record["mission_completion_claimed"] is False
    assert record["mission_completion_status"] == "unverified"
    assert record["identity_continuity_claimed"] is False
    assert record["shared_world_claimed"] is False
    assert [
        item["transition_authority"]["dispatch_authority_source"]
        for item in record["stage_records"]
    ] == [
        "preexisting_mission_approval",
        "preexisting_mission_approval",
    ]


def test_coordinator_progress_reports_current_stage_then_final_state() -> None:
    parent, approval = _setup()
    calls: list[str] = []
    snapshots: list[dict] = []

    record = run_parent_mission_coordinator(
        contract=parent,
        approval=approval,
        stage_runners={
            "stage_1": _runner(_evaluation(parent, 1), calls, "stage_1"),
            "stage_2": _runner(_evaluation(parent, 2), calls, "stage_2"),
        },
        progress_callback=lambda value: snapshots.append(dict(value)),
    )

    assert [snapshot["current_stage_ref"] for snapshot in snapshots] == [
        "stage_1",
        "stage_2",
        None,
    ]
    assert snapshots[0]["mission_completion_claimed"] is False
    assert snapshots[-1] == record


def test_unsatisfied_first_stage_never_invokes_second_runner() -> None:
    parent, approval = _setup()
    calls: list[str] = []

    record = run_parent_mission_coordinator(
        contract=parent,
        approval=approval,
        stage_runners={
            "stage_1": _runner(
                _evaluation(parent, 1, satisfied=False),
                calls,
                "stage_1",
            ),
            "stage_2": _runner(
                _evaluation(parent, 2),
                calls,
                "stage_2",
            ),
        },
    )

    assert calls == ["stage_1"]
    assert record["coordinator_status"] == "blocked"
    assert record["stages_satisfied"] == 0
    assert record["unreached_stage_refs"] == ["stage_2"]
    assert (
        "parent_mission_coordinator_stage_not_satisfied:stage_1"
        in record["blocking_reasons"]
    )


def test_missing_or_unexpected_runner_refuses_before_any_runner_call() -> None:
    parent, approval = _setup()
    calls: list[str] = []

    record = run_parent_mission_coordinator(
        contract=parent,
        approval=approval,
        stage_runners={
            "stage_1": _runner(
                _evaluation(parent, 1),
                calls,
                "stage_1",
            ),
            "stage_wrong": _runner(
                _evaluation(parent, 2),
                calls,
                "stage_wrong",
            ),
        },
    )

    assert calls == []
    assert record["stage_records"] == []
    assert record["unreached_stage_refs"] == ["stage_1", "stage_2"]
    assert (
        "parent_mission_coordinator_runner_missing:stage_2"
        in record["blocking_reasons"]
    )
    assert (
        "parent_mission_coordinator_runner_unexpected:stage_wrong"
        in record["blocking_reasons"]
    )
