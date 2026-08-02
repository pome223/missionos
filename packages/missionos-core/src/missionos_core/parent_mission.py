"""Backend-neutral parent mission lineage and transition authority.

The types in this module bind multiple already-existing child contracts to one
pre-existing operator approval. They do not evaluate child predicates, create
approval, create dispatch authority, or combine child claims into a mission
completion claim.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping

from .action_feasibility import VerificationBasis, canonical_sha256
from .execution_scope import HardwareExecutionMode, parse_hardware_execution_mode
from .mission_contract import (
    FrozenMissionContract,
    ObservationFreshnessBasis,
    PredicatePackageBinding,
    QuantificationScope,
    QuantificationScopeKind,
    UTC_WALL_CLOCK_DOMAIN_REF,
    validate_frozen_mission_contract,
)


PARENT_MISSION_CONTRACT_SCHEMA_VERSION = "missionos_core_parent_mission.v1"
PARENT_MISSION_APPROVAL_SCHEMA_VERSION = (
    "missionos_core_parent_mission_approval.v1"
)
PARENT_MISSION_STAGE_RESULT_SCHEMA_VERSION = (
    "missionos_core_parent_mission_stage_result.v1"
)
PARENT_MISSION_TRANSITION_AUTHORITY_SCHEMA_VERSION = (
    "missionos_core_parent_mission_transition_authority.v1"
)

SHARED_TARGET_DESCRIPTOR_REF = "shared_target_descriptor"


def _sha256_is_valid(value: str | None) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


@dataclass(frozen=True)
class ParentMissionObservationClockBinding:
    """Clock material copied from one frozen child requirement."""

    requirement_id: str
    freshness_basis: ObservationFreshnessBasis
    source_clock_domain_ref: str
    receipt_clock_domain_ref: str | None
    require_source_receipt_binding: bool

    def to_material(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "freshness_basis": (
                self.freshness_basis.value
                if isinstance(
                    self.freshness_basis, ObservationFreshnessBasis
                )
                else str(self.freshness_basis)
            ),
            "source_clock_domain_ref": self.source_clock_domain_ref,
            "receipt_clock_domain_ref": self.receipt_clock_domain_ref,
            "require_source_receipt_binding": (
                self.require_source_receipt_binding
            ),
        }


@dataclass(frozen=True)
class ParentMissionStageBinding:
    """One ordered child contract in a frozen parent mission."""

    stage_index: int
    stage_ref: str
    executor_ref: str
    execution_scope: HardwareExecutionMode
    child_contract_id: str
    child_contract_sha256: str
    predicate_package: PredicatePackageBinding
    required_verification_basis: VerificationBasis
    observation_clock_bindings: tuple[
        ParentMissionObservationClockBinding, ...
    ]
    evaluation_clock_domain_ref: str = UTC_WALL_CLOCK_DOMAIN_REF

    def to_material(self) -> dict[str, Any]:
        return {
            "stage_index": self.stage_index,
            "stage_ref": self.stage_ref,
            "executor_ref": self.executor_ref,
            "execution_scope": (
                self.execution_scope.value
                if isinstance(self.execution_scope, HardwareExecutionMode)
                else str(self.execution_scope)
            ),
            "child_contract_id": self.child_contract_id,
            "child_contract_sha256": self.child_contract_sha256,
            "predicate_package": (
                self.predicate_package.to_material()
                if isinstance(self.predicate_package, PredicatePackageBinding)
                else {"invalid": str(self.predicate_package)}
            ),
            "required_verification_basis": (
                self.required_verification_basis.value
                if isinstance(
                    self.required_verification_basis, VerificationBasis
                )
                else str(self.required_verification_basis)
            ),
            "observation_clock_bindings": [
                binding.to_material()
                for binding in self.observation_clock_bindings
            ],
            "evaluation_clock_domain_ref": self.evaluation_clock_domain_ref,
        }

    @property
    def stage_binding_sha256(self) -> str:
        return canonical_sha256(self.to_material())


@dataclass(frozen=True)
class FrozenParentMissionContract:
    """Approval-frozen parent material with no runtime result fields."""

    parent_mission_id: str
    parent_mission_version: str
    shared_target_descriptor_sha256: str
    quantification_scope: QuantificationScope
    stages: tuple[ParentMissionStageBinding, ...]
    identity_continuity_claimed: Literal[False] = False
    shared_world_claimed: Literal[False] = False
    schema_version: str = PARENT_MISSION_CONTRACT_SCHEMA_VERSION

    def to_material(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "parent_mission_id": self.parent_mission_id,
            "parent_mission_version": self.parent_mission_version,
            "shared_target_descriptor_sha256": (
                self.shared_target_descriptor_sha256
            ),
            "quantification_scope": self.quantification_scope.to_material(),
            "stages": [stage.to_material() for stage in self.stages],
            "identity_continuity_claimed": self.identity_continuity_claimed,
            "shared_world_claimed": self.shared_world_claimed,
        }

    @property
    def parent_mission_sha256(self) -> str:
        return canonical_sha256(self.to_material())


@dataclass(frozen=True)
class ParentMissionApprovalBinding:
    """A pre-existing approval bound to one exact parent mission."""

    parent_mission_sha256: str
    operator_approval_ref: str
    authority_bundle_ref: str
    shared_target_descriptor_sha256: str
    quantification_scope_sha256: str
    approved_stage_binding_sha256s: tuple[str, ...]
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    runtime_effect_requested: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    schema_version: str = PARENT_MISSION_APPROVAL_SCHEMA_VERSION

    def to_material(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def approval_binding_sha256(self) -> str:
        return canonical_sha256(self.to_material())


@dataclass(frozen=True)
class ParentMissionStageResult:
    """A child predicate result bound to its exact parent stage."""

    parent_mission_sha256: str
    stage_index: int
    stage_ref: str
    stage_binding_sha256: str
    child_contract_sha256: str
    predicate_package_sha256: str
    predicate_evaluation_sha256: str
    predicate_status: str
    evaluated_outcome_claim: bool
    actual_verification_basis: VerificationBasis
    predicate_package_evaluated: bool
    lineage_verified: bool
    reasons: tuple[str, ...]
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    runtime_effect_requested: Literal[False] = False
    operational_closure_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    schema_version: str = PARENT_MISSION_STAGE_RESULT_SCHEMA_VERSION

    @property
    def predicate_satisfied(self) -> bool:
        return (
            self.lineage_verified
            and self.predicate_status == "satisfied"
            and self.evaluated_outcome_claim is True
            and self.predicate_package_evaluated is True
            and not self.reasons
        )

    def to_material(self) -> dict[str, Any]:
        material = asdict(self)
        material["actual_verification_basis"] = (
            self.actual_verification_basis.value
            if isinstance(self.actual_verification_basis, VerificationBasis)
            else str(self.actual_verification_basis)
        )
        material["predicate_satisfied"] = self.predicate_satisfied
        return material

    @property
    def stage_result_sha256(self) -> str:
        return canonical_sha256(self.to_material())


@dataclass(frozen=True)
class ParentMissionTransitionAuthority:
    """Authority check for one stage; never an authority mint."""

    parent_mission_sha256: str
    approval_binding_sha256: str
    target_stage_index: int
    target_stage_ref: str
    target_stage_binding_sha256: str | None
    prerequisite_stage_ref: str | None
    prerequisite_stage_result_sha256: str | None
    prerequisite_predicate_satisfied: bool | None
    transition_status: str
    dispatch_authority_present: bool
    dispatch_authority_source: str | None
    blocking_reasons: tuple[str, ...]
    mission_completion_claimed: Literal[False] = False
    mission_completion_status: Literal["unverified"] = "unverified"
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    runtime_effect_requested: Literal[False] = False
    operational_closure_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    schema_version: str = PARENT_MISSION_TRANSITION_AUTHORITY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_parent_mission_stage_binding(
    *,
    stage_index: int,
    stage_ref: str,
    executor_ref: str,
    child_contract: FrozenMissionContract,
    evaluation_clock_domain_ref: str = UTC_WALL_CLOCK_DOMAIN_REF,
) -> ParentMissionStageBinding:
    """Project immutable child material into one parent stage binding."""

    child_reasons = validate_frozen_mission_contract(child_contract)
    if child_reasons:
        raise ValueError(
            "child contract is invalid: " + ",".join(child_reasons)
        )
    return ParentMissionStageBinding(
        stage_index=stage_index,
        stage_ref=stage_ref,
        executor_ref=executor_ref,
        execution_scope=child_contract.execution_scope,
        child_contract_id=child_contract.contract_id,
        child_contract_sha256=child_contract.contract_sha256,
        predicate_package=child_contract.predicate_package,
        required_verification_basis=(
            child_contract.required_verification_basis
        ),
        observation_clock_bindings=tuple(
            ParentMissionObservationClockBinding(
                requirement_id=requirement.requirement_id,
                freshness_basis=requirement.freshness_basis,
                source_clock_domain_ref=requirement.source_clock_domain_ref,
                receipt_clock_domain_ref=requirement.receipt_clock_domain_ref,
                require_source_receipt_binding=(
                    requirement.require_source_receipt_binding
                ),
            )
            for requirement in child_contract.observation_requirements
        ),
        evaluation_clock_domain_ref=evaluation_clock_domain_ref,
    )


def validate_frozen_parent_mission_contract(
    contract: FrozenParentMissionContract,
) -> tuple[str, ...]:
    """Return structural refusal reasons for a parent contract."""

    reasons: list[str] = []
    if contract.schema_version != PARENT_MISSION_CONTRACT_SCHEMA_VERSION:
        reasons.append("parent_mission_schema_not_supported")
    if not str(contract.parent_mission_id or "").strip():
        reasons.append("parent_mission_id_missing")
    if not str(contract.parent_mission_version or "").strip():
        reasons.append("parent_mission_version_missing")
    if not _sha256_is_valid(contract.shared_target_descriptor_sha256):
        reasons.append("parent_mission_shared_target_descriptor_invalid")
    if contract.identity_continuity_claimed is not False:
        reasons.append("parent_mission_identity_continuity_claim_forbidden")
    if contract.shared_world_claimed is not False:
        reasons.append("parent_mission_shared_world_claim_forbidden")

    scope = contract.quantification_scope
    if not isinstance(scope, QuantificationScope):
        reasons.append("parent_mission_quantification_scope_missing")
    elif not isinstance(scope.kind, QuantificationScopeKind):
        reasons.append("parent_mission_quantification_scope_kind_invalid")
    elif scope.kind is QuantificationScopeKind.NONE:
        if not str(scope.reason or "").strip():
            reasons.append("parent_mission_quantification_scope_reason_missing")
        if scope.member_ids or scope.scope_ref is not None:
            reasons.append("parent_mission_quantification_scope_none_not_empty")
    elif scope.scope_ref != SHARED_TARGET_DESCRIPTOR_REF:
        reasons.append("parent_mission_quantification_scope_ref_invalid")

    if len(contract.stages) < 2:
        reasons.append("parent_mission_stage_count_insufficient")
    seen_refs: set[str] = set()
    seen_contracts: set[str] = set()
    for expected_index, stage in enumerate(contract.stages, start=1):
        prefix = f"parent_mission_stage:{expected_index}"
        if not isinstance(stage, ParentMissionStageBinding):
            reasons.append(f"{prefix}:binding_invalid")
            continue
        if (
            isinstance(stage.stage_index, bool)
            or stage.stage_index != expected_index
        ):
            reasons.append(f"{prefix}:index_invalid")
        if not str(stage.stage_ref or "").strip():
            reasons.append(f"{prefix}:ref_missing")
        elif stage.stage_ref in seen_refs:
            reasons.append(f"{prefix}:ref_duplicate")
        seen_refs.add(stage.stage_ref)
        if not str(stage.executor_ref or "").strip():
            reasons.append(f"{prefix}:executor_ref_missing")
        if parse_hardware_execution_mode(stage.execution_scope) is None:
            reasons.append(f"{prefix}:execution_scope_invalid")
        if not str(stage.child_contract_id or "").strip():
            reasons.append(f"{prefix}:child_contract_id_missing")
        elif stage.child_contract_id in seen_contracts:
            reasons.append(f"{prefix}:child_contract_reused")
        seen_contracts.add(stage.child_contract_id)
        if not _sha256_is_valid(stage.child_contract_sha256):
            reasons.append(f"{prefix}:child_contract_digest_invalid")
        package = stage.predicate_package
        if not isinstance(package, PredicatePackageBinding):
            reasons.append(f"{prefix}:predicate_package_invalid")
        else:
            if not str(package.package_id or "").strip():
                reasons.append(f"{prefix}:predicate_package_id_missing")
            if not str(package.package_version or "").strip():
                reasons.append(f"{prefix}:predicate_package_version_missing")
            if not _sha256_is_valid(package.content_sha256):
                reasons.append(f"{prefix}:predicate_package_digest_invalid")
        if not isinstance(
            stage.required_verification_basis, VerificationBasis
        ):
            reasons.append(f"{prefix}:required_verification_basis_invalid")
        if not str(stage.evaluation_clock_domain_ref or "").strip():
            reasons.append(f"{prefix}:evaluation_clock_domain_missing")
        for clock in stage.observation_clock_bindings:
            if not isinstance(clock, ParentMissionObservationClockBinding):
                reasons.append(f"{prefix}:observation_clock_binding_invalid")
                continue
            if not str(clock.requirement_id or "").strip():
                reasons.append(f"{prefix}:observation_requirement_id_missing")
            if not isinstance(
                clock.freshness_basis, ObservationFreshnessBasis
            ):
                reasons.append(f"{prefix}:freshness_basis_invalid")
            if not str(clock.source_clock_domain_ref or "").strip():
                reasons.append(f"{prefix}:source_clock_domain_missing")
            if (
                clock.require_source_receipt_binding
                and not str(clock.receipt_clock_domain_ref or "").strip()
            ):
                reasons.append(f"{prefix}:receipt_clock_domain_missing")
    return tuple(dict.fromkeys(reasons))


def build_parent_mission_approval_binding(
    *,
    contract: FrozenParentMissionContract,
    operator_approval_ref: str,
    authority_bundle_ref: str,
) -> ParentMissionApprovalBinding:
    """Bind a pre-existing approval reference to exact parent material."""

    reasons = validate_frozen_parent_mission_contract(contract)
    if reasons:
        raise ValueError("parent mission is invalid: " + ",".join(reasons))
    if not str(operator_approval_ref or "").strip():
        raise ValueError("operator approval ref is required")
    if not str(authority_bundle_ref or "").strip():
        raise ValueError("authority bundle ref is required")
    return ParentMissionApprovalBinding(
        parent_mission_sha256=contract.parent_mission_sha256,
        operator_approval_ref=operator_approval_ref,
        authority_bundle_ref=authority_bundle_ref,
        shared_target_descriptor_sha256=(
            contract.shared_target_descriptor_sha256
        ),
        quantification_scope_sha256=canonical_sha256(
            contract.quantification_scope.to_material()
        ),
        approved_stage_binding_sha256s=tuple(
            stage.stage_binding_sha256 for stage in contract.stages
        ),
    )


def validate_parent_mission_approval_binding(
    *,
    contract: FrozenParentMissionContract,
    approval: ParentMissionApprovalBinding | None,
) -> tuple[str, ...]:
    """Validate stored approval material against the current parent."""

    reasons = list(validate_frozen_parent_mission_contract(contract))
    if not isinstance(approval, ParentMissionApprovalBinding):
        reasons.append("parent_mission_approval_binding_missing")
        return tuple(dict.fromkeys(reasons))
    if approval.schema_version != PARENT_MISSION_APPROVAL_SCHEMA_VERSION:
        reasons.append("parent_mission_approval_schema_not_supported")
    if approval.parent_mission_sha256 != contract.parent_mission_sha256:
        reasons.append("parent_mission_approval_contract_digest_mismatch")
    if not str(approval.operator_approval_ref or "").strip():
        reasons.append("parent_mission_operator_approval_ref_missing")
    if not str(approval.authority_bundle_ref or "").strip():
        reasons.append("parent_mission_authority_bundle_ref_missing")
    if (
        approval.shared_target_descriptor_sha256
        != contract.shared_target_descriptor_sha256
    ):
        reasons.append(
            "parent_mission_approval_shared_target_descriptor_mismatch"
        )
    if approval.quantification_scope_sha256 != canonical_sha256(
        contract.quantification_scope.to_material()
    ):
        reasons.append("parent_mission_approval_quantification_scope_mismatch")
    if approval.approved_stage_binding_sha256s != tuple(
        stage.stage_binding_sha256 for stage in contract.stages
    ):
        reasons.append("parent_mission_approval_stage_bindings_mismatch")
    for field in (
        "approval_created",
        "dispatch_authority_created",
        "runtime_effect_requested",
        "physical_execution_invoked",
    ):
        if getattr(approval, field) is not False:
            reasons.append(f"parent_mission_approval_{field}_forbidden")
    return tuple(dict.fromkeys(reasons))


def bind_parent_mission_stage_result(
    *,
    contract: FrozenParentMissionContract,
    stage_index: int,
    predicate_evaluation: Mapping[str, Any],
) -> ParentMissionStageResult:
    """Bind one package-specific evaluation to its exact parent stage."""

    reasons = list(validate_frozen_parent_mission_contract(contract))
    stage: ParentMissionStageBinding | None = None
    if (
        isinstance(stage_index, bool)
        or not isinstance(stage_index, int)
        or stage_index < 1
        or stage_index > len(contract.stages)
    ):
        reasons.append("parent_mission_stage_result_index_invalid")
    else:
        stage = contract.stages[stage_index - 1]
    evaluation = dict(predicate_evaluation)
    package = stage.predicate_package if stage is not None else None
    expected = {
        "contract_id": stage.child_contract_id if stage else None,
        "contract_sha256": stage.child_contract_sha256 if stage else None,
        "predicate_package_id": package.package_id if package else None,
        "predicate_package_version": (
            package.package_version if package else None
        ),
        "predicate_package_sha256": package.content_sha256 if package else None,
    }
    for field, value in expected.items():
        if evaluation.get(field) != value:
            reasons.append(f"parent_mission_stage_result_{field}_mismatch")
    actual_basis = evaluation.get("actual_verification_basis")
    try:
        basis = (
            actual_basis
            if isinstance(actual_basis, VerificationBasis)
            else VerificationBasis(str(actual_basis))
        )
    except ValueError:
        basis = VerificationBasis.UNVERIFIED
        reasons.append("parent_mission_stage_result_basis_invalid")
    status = str(evaluation.get("status") or "")
    outcome = evaluation.get("evaluated_outcome_claim") is True
    package_evaluated = evaluation.get("predicate_package_evaluated") is True
    if status == "satisfied":
        if not outcome:
            reasons.append("parent_mission_stage_result_outcome_not_claimed")
        if not package_evaluated:
            reasons.append("parent_mission_stage_result_package_not_evaluated")
        if (
            stage is not None
            and basis is not stage.required_verification_basis
        ):
            reasons.append("parent_mission_stage_result_basis_insufficient")
    for field in (
        "approval_created",
        "dispatch_authority_created",
        "runtime_effect_requested",
        "operational_closure_created",
        "physical_execution_invoked",
    ):
        if evaluation.get(field) is not False:
            reasons.append(f"parent_mission_stage_result_{field}_forbidden")
    return ParentMissionStageResult(
        parent_mission_sha256=contract.parent_mission_sha256,
        stage_index=stage_index,
        stage_ref=stage.stage_ref if stage else "",
        stage_binding_sha256=stage.stage_binding_sha256 if stage else "",
        child_contract_sha256=(
            stage.child_contract_sha256 if stage else ""
        ),
        predicate_package_sha256=(
            package.content_sha256 if package else ""
        ),
        predicate_evaluation_sha256=canonical_sha256(evaluation),
        predicate_status=status,
        evaluated_outcome_claim=outcome,
        actual_verification_basis=basis,
        predicate_package_evaluated=package_evaluated,
        lineage_verified=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def evaluate_parent_mission_transition_authority(
    *,
    contract: FrozenParentMissionContract,
    approval: ParentMissionApprovalBinding | None,
    target_stage_index: int,
    target_stage_ref: str,
    previous_stage_result: ParentMissionStageResult | None,
    previous_predicate_evaluation: Mapping[str, Any] | None,
) -> ParentMissionTransitionAuthority:
    """Check existing authority for one ordered stage before dispatch."""

    reasons = list(
        validate_parent_mission_approval_binding(
            contract=contract,
            approval=approval,
        )
    )
    target: ParentMissionStageBinding | None = None
    if (
        isinstance(target_stage_index, bool)
        or not isinstance(target_stage_index, int)
        or target_stage_index < 1
        or target_stage_index > len(contract.stages)
    ):
        reasons.append("parent_mission_transition_target_index_invalid")
    else:
        target = contract.stages[target_stage_index - 1]
        if target_stage_ref != target.stage_ref:
            reasons.append("parent_mission_transition_target_ref_mismatch")

    prerequisite_stage_ref: str | None = None
    prerequisite_result_sha256: str | None = None
    prerequisite_satisfied: bool | None = None
    if target_stage_index == 1:
        if (
            previous_stage_result is not None
            or previous_predicate_evaluation is not None
        ):
            reasons.append(
                "parent_mission_transition_initial_prerequisite_present"
            )
    elif target is not None:
        expected_previous = contract.stages[target_stage_index - 2]
        prerequisite_stage_ref = expected_previous.stage_ref
        if not isinstance(previous_stage_result, ParentMissionStageResult):
            reasons.append(
                "parent_mission_transition_prerequisite_result_missing"
            )
        elif not isinstance(previous_predicate_evaluation, Mapping):
            reasons.append(
                "parent_mission_transition_prerequisite_evaluation_missing"
            )
        else:
            prerequisite_result_sha256 = (
                previous_stage_result.stage_result_sha256
            )
            prerequisite_satisfied = (
                previous_stage_result.predicate_satisfied
            )
            if (
                previous_stage_result.parent_mission_sha256
                != contract.parent_mission_sha256
            ):
                reasons.append(
                    "parent_mission_transition_prerequisite_parent_mismatch"
                )
            if (
                previous_stage_result.stage_index
                != expected_previous.stage_index
                or previous_stage_result.stage_ref
                != expected_previous.stage_ref
                or previous_stage_result.stage_binding_sha256
                != expected_previous.stage_binding_sha256
            ):
                reasons.append(
                    "parent_mission_transition_prerequisite_stage_mismatch"
                )
            if not prerequisite_satisfied:
                reasons.append(
                    "parent_mission_transition_prerequisite_not_satisfied"
                )
            rebound = bind_parent_mission_stage_result(
                contract=contract,
                stage_index=expected_previous.stage_index,
                predicate_evaluation=previous_predicate_evaluation,
            )
            if rebound != previous_stage_result:
                reasons.append(
                    "parent_mission_transition_prerequisite_binding_mismatch"
                )

    reasons = list(dict.fromkeys(reasons))
    authorized = not reasons
    return ParentMissionTransitionAuthority(
        parent_mission_sha256=contract.parent_mission_sha256,
        approval_binding_sha256=(
            approval.approval_binding_sha256
            if isinstance(approval, ParentMissionApprovalBinding)
            else ""
        ),
        target_stage_index=target_stage_index,
        target_stage_ref=target_stage_ref,
        target_stage_binding_sha256=(
            target.stage_binding_sha256 if target else None
        ),
        prerequisite_stage_ref=prerequisite_stage_ref,
        prerequisite_stage_result_sha256=prerequisite_result_sha256,
        prerequisite_predicate_satisfied=prerequisite_satisfied,
        transition_status="authorized" if authorized else "blocked",
        dispatch_authority_present=authorized,
        dispatch_authority_source=(
            "preexisting_mission_approval" if authorized else None
        ),
        blocking_reasons=tuple(reasons),
    )


__all__ = [
    "PARENT_MISSION_APPROVAL_SCHEMA_VERSION",
    "PARENT_MISSION_CONTRACT_SCHEMA_VERSION",
    "PARENT_MISSION_STAGE_RESULT_SCHEMA_VERSION",
    "PARENT_MISSION_TRANSITION_AUTHORITY_SCHEMA_VERSION",
    "SHARED_TARGET_DESCRIPTOR_REF",
    "FrozenParentMissionContract",
    "ParentMissionApprovalBinding",
    "ParentMissionObservationClockBinding",
    "ParentMissionStageBinding",
    "ParentMissionStageResult",
    "ParentMissionTransitionAuthority",
    "bind_parent_mission_stage_result",
    "build_parent_mission_approval_binding",
    "build_parent_mission_stage_binding",
    "evaluate_parent_mission_transition_authority",
    "validate_frozen_parent_mission_contract",
    "validate_parent_mission_approval_binding",
]
