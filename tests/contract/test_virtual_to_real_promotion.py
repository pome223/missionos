from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from missionos_core import (
    EvidenceOrigin,
    EvidenceSourceRef,
    HardwareExecutionMode,
    PromotionComponentBinding,
    PromotionComponentKind,
    PromotionEvidenceScope,
    PromotionGap,
    PromotionGapRequirement,
    PromotionGapStatus,
    PromotionReceiptValidationStatus,
    PromotionSourceEvidenceBinding,
    UTC_WALL_CLOCK_DOMAIN_REF,
    VerificationBasis,
    VirtualToRealPromotionPolicy,
    VirtualToRealPromotionReceipt,
    VirtualToRealPromotionValidationContext,
    promotion_approval_artifact_sha256,
    validate_virtual_to_real_promotion_receipt,
)


SOURCE_SHA = "1" * 64
CONTRACT_SHA = "2" * 64
PREDICATE_SHA = "3" * 64
SIMULATOR_SHA = "4" * 64
CONTROLLER_SHA = "5" * 64
TARGET_EXECUTOR_SHA = "6" * 64
TARGET_CONTROLLER_SHA = "7" * 64
SAFE_STOP_SHA = "9" * 64
ENFORCEMENT_SHA = "a" * 64
ATTESTATION_SHA = "b" * 64


def _policy() -> VirtualToRealPromotionPolicy:
    return VirtualToRealPromotionPolicy(
        policy_id="policy:v2r:bench:v1",
        policy_version="1",
        source_scope=HardwareExecutionMode.SIM,
        allowed_target_scopes=(HardwareExecutionMode.BENCH,),
        required_source_verification_basis=VerificationBasis.DETERMINISTIC,
        approval_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
        required_component_kinds=(
            PromotionComponentKind.SIMULATOR,
            PromotionComponentKind.CONTROLLER_PROFILE,
        ),
        required_gaps=(
            PromotionGapRequirement(
                gap_id="safe_stop",
                required_evidence_kind="safe_stop_receipt",
                required_origin=EvidenceOrigin.MACHINE_OBSERVED,
                evidence_scope=PromotionEvidenceScope.TARGET,
            ),
            PromotionGapRequirement(
                gap_id="controller_enforcement",
                required_evidence_kind="controller_enforcement_readback",
                required_origin=EvidenceOrigin.MACHINE_OBSERVED,
                evidence_scope=PromotionEvidenceScope.TARGET,
            ),
            PromotionGapRequirement(
                gap_id="hardware_attestation",
                required_evidence_kind="hardware_attestation",
                required_origin=EvidenceOrigin.MACHINE_OBSERVED,
                evidence_scope=PromotionEvidenceScope.TARGET,
            ),
        ),
        required_target_evidence_kinds=(
            "safe_stop_receipt",
            "controller_enforcement_readback",
            "hardware_attestation",
        ),
        required_rollback_condition_ids=("rollback:disable-profile",),
        required_disable_condition_ids=("disable:on-evidence-drift",),
        maximum_age_seconds=3600.0,
    )


def _source(
    source_id: str,
    *,
    kind: str,
    digest: str,
    scope: HardwareExecutionMode,
    origin: EvidenceOrigin,
) -> EvidenceSourceRef:
    return EvidenceSourceRef(
        source_id=source_id,
        evidence_kind=kind,
        observed_at="2026-08-01T00:00:00+00:00",
        content_sha256=digest,
        execution_scope=scope,
        origin=origin,
    )


def _sources(*, approval_sha256: str) -> dict[str, EvidenceSourceRef]:
    return {
        "evidence:sim-result": _source(
            "evidence:sim-result",
            kind="bounded_simulator_result",
            digest=SOURCE_SHA,
            scope=HardwareExecutionMode.SIM,
            origin=EvidenceOrigin.STORED_ARTIFACT,
        ),
        "approval:v2r": _source(
            "approval:v2r",
            kind="v2r_promotion_approval",
            digest=approval_sha256,
            scope=HardwareExecutionMode.BENCH,
            origin=EvidenceOrigin.AUTHORITY_ARTIFACT,
        ),
        "evidence:safe-stop": _source(
            "evidence:safe-stop",
            kind="safe_stop_receipt",
            digest=SAFE_STOP_SHA,
            scope=HardwareExecutionMode.BENCH,
            origin=EvidenceOrigin.MACHINE_OBSERVED,
        ),
        "evidence:enforcement": _source(
            "evidence:enforcement",
            kind="controller_enforcement_readback",
            digest=ENFORCEMENT_SHA,
            scope=HardwareExecutionMode.BENCH,
            origin=EvidenceOrigin.MACHINE_OBSERVED,
        ),
        "evidence:attestation": _source(
            "evidence:attestation",
            kind="hardware_attestation",
            digest=ATTESTATION_SHA,
            scope=HardwareExecutionMode.BENCH,
            origin=EvidenceOrigin.MACHINE_OBSERVED,
        ),
    }


def _receipt(
    policy: VirtualToRealPromotionPolicy,
) -> VirtualToRealPromotionReceipt:
    receipt = VirtualToRealPromotionReceipt(
        receipt_id="promotion-receipt:sim-to-bench:1",
        source_scope=HardwareExecutionMode.SIM,
        target_scope=HardwareExecutionMode.BENCH,
        source_contract_sha256=CONTRACT_SHA,
        source_predicate_package_id="predicate:bounded-sim-result",
        source_predicate_package_version="1",
        source_predicate_package_sha256=PREDICATE_SHA,
        source_outcome_claim_scope="exact_simulator_run",
        source_outcome_evidence_ref="evidence:sim-result",
        source_outcome_claim_satisfied=True,
        source_verification_basis=VerificationBasis.DETERMINISTIC,
        source_evidence_bindings=(
            PromotionSourceEvidenceBinding(
                evidence_ref="evidence:sim-result",
                evidence_kind="bounded_simulator_result",
                content_sha256=SOURCE_SHA,
                execution_scope=HardwareExecutionMode.SIM,
                origin=EvidenceOrigin.STORED_ARTIFACT,
            ),
        ),
        component_bindings=(
            PromotionComponentBinding(
                kind=PromotionComponentKind.SIMULATOR,
                component_id="simulator:approved",
                component_version="1",
                content_sha256=SIMULATOR_SHA,
            ),
            PromotionComponentBinding(
                kind=PromotionComponentKind.CONTROLLER_PROFILE,
                component_id="controller:target",
                component_version="1",
                content_sha256=CONTROLLER_SHA,
            ),
        ),
        target_executor_profile_sha256=TARGET_EXECUTOR_SHA,
        target_controller_profile_sha256=TARGET_CONTROLLER_SHA,
        gaps=(
            PromotionGap(
                gap_id="safe_stop",
                status=PromotionGapStatus.RESOLVED,
                resolution_evidence_ref="evidence:safe-stop",
                resolution_evidence_sha256=SAFE_STOP_SHA,
                resolution_evidence_kind="safe_stop_receipt",
                resolution_evidence_origin=EvidenceOrigin.MACHINE_OBSERVED,
                resolution_execution_scope=HardwareExecutionMode.BENCH,
            ),
            PromotionGap(
                gap_id="controller_enforcement",
                status=PromotionGapStatus.RESOLVED,
                resolution_evidence_ref="evidence:enforcement",
                resolution_evidence_sha256=ENFORCEMENT_SHA,
                resolution_evidence_kind="controller_enforcement_readback",
                resolution_evidence_origin=EvidenceOrigin.MACHINE_OBSERVED,
                resolution_execution_scope=HardwareExecutionMode.BENCH,
            ),
            PromotionGap(
                gap_id="hardware_attestation",
                status=PromotionGapStatus.RESOLVED,
                resolution_evidence_ref="evidence:attestation",
                resolution_evidence_sha256=ATTESTATION_SHA,
                resolution_evidence_kind="hardware_attestation",
                resolution_evidence_origin=EvidenceOrigin.MACHINE_OBSERVED,
                resolution_execution_scope=HardwareExecutionMode.BENCH,
            ),
        ),
        target_required_evidence_kinds=policy.required_target_evidence_kinds,
        approved_by="human:operator",
        approval_artifact_ref="approval:v2r",
        approval_artifact_sha256="0" * 64,
        approved_at="2026-08-01T00:00:00+00:00",
        expires_at="2026-08-01T01:00:00+00:00",
        approval_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
        policy_binding=policy.binding,
        rollback_condition_ids=policy.required_rollback_condition_ids,
        disable_condition_ids=policy.required_disable_condition_ids,
    )
    return replace(
        receipt,
        approval_artifact_sha256=promotion_approval_artifact_sha256(receipt),
    )


def _reapprove(
    receipt: VirtualToRealPromotionReceipt,
) -> VirtualToRealPromotionReceipt:
    provisional = replace(receipt, approval_artifact_sha256="0" * 64)
    return replace(
        provisional,
        approval_artifact_sha256=promotion_approval_artifact_sha256(provisional),
    )


def _context(
    policy: VirtualToRealPromotionPolicy,
    *,
    receipt: VirtualToRealPromotionReceipt | None = None,
) -> VirtualToRealPromotionValidationContext:
    bound_receipt = receipt or _receipt(policy)
    return VirtualToRealPromotionValidationContext(
        expected_source_scope=HardwareExecutionMode.SIM,
        expected_target_scope=HardwareExecutionMode.BENCH,
        observed_source_outcome_evidence_ref="evidence:sim-result",
        observed_source_outcome_claim_satisfied=True,
        observed_source_verification_basis=VerificationBasis.DETERMINISTIC,
        expected_target_executor_profile_sha256=TARGET_EXECUTOR_SHA,
        expected_target_controller_profile_sha256=TARGET_CONTROLLER_SHA,
        active_policy=policy,
        evidence_sources=_sources(approval_sha256=bound_receipt.approval_artifact_sha256),
        evaluated_at="2026-08-01T00:30:00+00:00",
    )


def test_verified_receipt_is_only_a_promotion_prerequisite() -> None:
    policy = _policy()

    result = validate_virtual_to_real_promotion_receipt(
        _receipt(policy),
        context=_context(policy),
    )

    assert result.status is PromotionReceiptValidationStatus.VERIFIED_PREREQUISITE
    assert result.verification_basis is VerificationBasis.DETERMINISTIC
    assert result.promotion_prerequisite_satisfied is True
    assert result.approval_created is False
    assert result.dispatch_authority_created is False
    assert result.runtime_effect_requested is False
    assert result.task_completion_claimed is False
    assert result.physical_safety_claimed is False
    assert result.physical_execution_invoked is False


def test_missing_receipt_blocks_physical_promotion() -> None:
    policy = _policy()

    result = validate_virtual_to_real_promotion_receipt(
        None,
        context=_context(policy),
    )

    assert result.status is PromotionReceiptValidationStatus.BLOCKED
    assert result.reasons == ("v2r_promotion_receipt_missing",)


def test_observed_failed_source_outcome_cannot_be_promoted() -> None:
    policy = _policy()

    result = validate_virtual_to_real_promotion_receipt(
        _receipt(policy),
        context=replace(
            _context(policy),
            observed_source_outcome_claim_satisfied=False,
        ),
    )

    assert result.status is PromotionReceiptValidationStatus.BLOCKED
    assert "v2r_promotion_observed_source_outcome_not_satisfied" in result.reasons


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda receipt: replace(
                receipt,
                target_controller_profile_sha256="c" * 64,
            ),
            "v2r_promotion_target_controller_profile_mismatch",
        ),
        (
            lambda receipt: replace(
                receipt,
                target_scope=HardwareExecutionMode.SIM,
            ),
            "v2r_promotion_target_scope_not_physical",
        ),
        (
            lambda receipt: replace(
                receipt,
                target_required_evidence_kinds=("safe_stop_receipt",),
            ),
            "v2r_promotion_target_evidence_requirements_mismatch",
        ),
        (
            lambda receipt: replace(
                receipt,
                expires_at="2026-08-01T02:00:00+00:00",
            ),
            "v2r_promotion_expiry_not_policy_derived",
        ),
        (
            lambda receipt: replace(
                receipt,
                physical_execution_invoked=True,
            ),
            "v2r_promotion_authority_or_physical_claim_forbidden",
        ),
        (
            lambda receipt: replace(
                receipt,
                source_outcome_claim_satisfied=False,
            ),
            "v2r_promotion_source_outcome_not_satisfied",
        ),
        (
            lambda receipt: replace(
                receipt,
                source_verification_basis=VerificationBasis.MODEL_INFERRED,
            ),
            "v2r_promotion_source_verification_basis_mismatch",
        ),
        (
            lambda receipt: replace(
                receipt,
                approval_clock_domain_ref="clock:sim_time",
            ),
            "v2r_promotion_clock_domain_mismatch",
        ),
    ],
)
def test_target_or_authority_mutation_is_blocked(mutation, reason: str) -> None:
    policy = _policy()

    result = validate_virtual_to_real_promotion_receipt(
        mutation(_receipt(policy)),
        context=_context(policy),
    )

    assert result.status is PromotionReceiptValidationStatus.BLOCKED
    assert reason in result.reasons


def test_source_evidence_digest_mutation_is_blocked() -> None:
    policy = _policy()
    receipt = _receipt(policy)
    mutated_binding = replace(
        receipt.source_evidence_bindings[0],
        content_sha256="d" * 64,
    )

    result = validate_virtual_to_real_promotion_receipt(
        replace(receipt, source_evidence_bindings=(mutated_binding,)),
        context=_context(policy),
    )

    assert result.status is PromotionReceiptValidationStatus.BLOCKED
    assert any("source_evidence_digest_mismatch" in reason for reason in result.reasons)


def test_source_evidence_origin_is_content_bound() -> None:
    policy = _policy()
    receipt = _receipt(policy)
    mutated_binding = replace(
        receipt.source_evidence_bindings[0],
        origin=EvidenceOrigin.MODEL_INFERRED,
    )

    modified = _reapprove(replace(receipt, source_evidence_bindings=(mutated_binding,)))
    result = validate_virtual_to_real_promotion_receipt(
        modified,
        context=_context(policy, receipt=modified),
    )

    assert result.status is PromotionReceiptValidationStatus.BLOCKED
    assert any("source_evidence_origin_mismatch" in reason for reason in result.reasons)


def test_gap_cannot_be_resolved_with_another_evidence_kind() -> None:
    policy = _policy()
    receipt = _receipt(policy)
    gap = replace(
        receipt.gaps[0],
        resolution_evidence_ref="evidence:attestation",
        resolution_evidence_sha256=ATTESTATION_SHA,
        resolution_evidence_kind="hardware_attestation",
    )

    modified = _reapprove(replace(receipt, gaps=(gap, *receipt.gaps[1:])))
    result = validate_virtual_to_real_promotion_receipt(
        modified,
        context=_context(policy, receipt=modified),
    )

    assert result.status is PromotionReceiptValidationStatus.BLOCKED
    assert "v2r_promotion_gap_resolution_kind_mismatch:safe_stop" in result.reasons


@pytest.mark.parametrize(
    ("policy_mutation", "reason"),
    [
        (
            {"required_component_kinds": ("simulator",)},
            "v2r_promotion_policy_component_requirements_invalid",
        ),
        (
            {"maximum_age_seconds": True},
            "v2r_promotion_policy_maximum_age_invalid",
        ),
        (
            {"allowed_target_scopes": (HardwareExecutionMode.BENCH, "bogus")},
            "v2r_promotion_policy_target_scope_invalid",
        ),
        (
            {"required_source_verification_basis": "deterministic"},
            "v2r_promotion_policy_source_basis_invalid",
        ),
        (
            {"approval_clock_domain_ref": "clock:sim_time"},
            "v2r_promotion_policy_clock_domain_invalid",
        ),
    ],
)
def test_malformed_policy_is_blocked_without_throwing(
    policy_mutation: dict[str, object],
    reason: str,
) -> None:
    policy = _policy()
    malformed = replace(policy, **policy_mutation)

    result = validate_virtual_to_real_promotion_receipt(
        _receipt(policy),
        context=replace(_context(policy), active_policy=malformed),
    )

    assert result.status is PromotionReceiptValidationStatus.BLOCKED
    assert reason in result.reasons


@pytest.mark.parametrize(
    "receipt_mutation",
    [
        {"source_evidence_bindings": ("not-a-binding",)},
        {"component_bindings": ("not-a-component",)},
        {"gaps": ("not-a-gap",)},
    ],
)
def test_malformed_nested_receipt_is_structurally_blocked(
    receipt_mutation: dict[str, object],
) -> None:
    policy = _policy()

    result = validate_virtual_to_real_promotion_receipt(
        replace(_receipt(policy), **receipt_mutation),
        context=_context(policy),
    )

    assert result.status is PromotionReceiptValidationStatus.BLOCKED
    assert result.reasons == ("v2r_promotion_receipt_structure_invalid",)
    assert result.dispatch_authority_created is False
    assert result.physical_execution_invoked is False


@pytest.mark.parametrize("status", [PromotionGapStatus.UNRESOLVED, PromotionGapStatus.UNKNOWN])
def test_unresolved_or_unknown_gap_is_unverified(status: PromotionGapStatus) -> None:
    policy = _policy()
    receipt = _receipt(policy)
    gap = replace(
        receipt.gaps[0],
        status=status,
        resolution_evidence_ref=None,
        resolution_evidence_sha256=None,
        resolution_evidence_kind=None,
        resolution_evidence_origin=None,
        resolution_execution_scope=None,
    )

    modified = _reapprove(replace(receipt, gaps=(gap, *receipt.gaps[1:])))
    result = validate_virtual_to_real_promotion_receipt(
        modified,
        context=_context(policy, receipt=modified),
    )

    assert result.status is PromotionReceiptValidationStatus.UNVERIFIED
    assert f"v2r_promotion_gap_{status.value}:safe_stop" in result.reasons
    assert result.promotion_prerequisite_satisfied is False


def test_missing_policy_required_component_is_unverified() -> None:
    policy = _policy()
    receipt = _receipt(policy)

    modified = _reapprove(replace(receipt, component_bindings=receipt.component_bindings[:1]))
    result = validate_virtual_to_real_promotion_receipt(
        modified,
        context=_context(policy, receipt=modified),
    )

    assert result.status is PromotionReceiptValidationStatus.UNVERIFIED
    assert "v2r_promotion_required_component_missing" in result.reasons


def test_expired_receipt_is_unverified() -> None:
    policy = _policy()
    context = replace(_context(policy), evaluated_at="2026-08-01T01:00:01+00:00")

    result = validate_virtual_to_real_promotion_receipt(
        _receipt(policy),
        context=context,
    )

    assert result.status is PromotionReceiptValidationStatus.UNVERIFIED
    assert "v2r_promotion_receipt_expired" in result.reasons


def test_approval_must_be_authority_artifact_in_target_scope() -> None:
    policy = _policy()
    context = _context(policy)
    sources = dict(context.evidence_sources)
    sources["approval:v2r"] = replace(
        sources["approval:v2r"],
        origin=EvidenceOrigin.OPERATOR_DECLARED,
        execution_scope=HardwareExecutionMode.SIM,
    )

    result = validate_virtual_to_real_promotion_receipt(
        _receipt(policy),
        context=replace(context, evidence_sources=sources),
    )

    assert result.status is PromotionReceiptValidationStatus.BLOCKED
    assert "v2r_promotion_approval_evidence_origin_invalid" in result.reasons
    assert "v2r_promotion_approval_evidence_scope_mismatch" in result.reasons


def test_tracked_simulator_evidence_is_not_promotion_ready_without_target() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "agents"
        / "evidence"
        / "20260801-v2r-promotion-source-audit.json"
    )
    audit = json.loads(path.read_text(encoding="utf-8"))

    assert len(audit["cases"]) == 3
    assert all(case["source_binding"]["execution_scope"] == "sim" for case in audit["cases"])
    assert all(case["promotion_readiness"] == "unverified" for case in audit["cases"])
    assert all(
        case["target_status"]["physical_execution_scope"] == "not_provided"
        for case in audit["cases"]
    )
    assert audit["claim_boundary"]["promotion_receipt_created"] is False
    assert audit["claim_boundary"]["physical_execution_invoked"] is False
