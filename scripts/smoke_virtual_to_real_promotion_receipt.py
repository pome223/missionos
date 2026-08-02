#!/usr/bin/env python3
"""Replay tracked simulator sources through the no-receipt promotion gate."""

from __future__ import annotations

import json
from pathlib import Path

from missionos_core import (
    EvidenceOrigin,
    HardwareExecutionMode,
    PromotionComponentKind,
    PromotionEvidenceScope,
    PromotionGapRequirement,
    UTC_WALL_CLOCK_DOMAIN_REF,
    VerificationBasis,
    VirtualToRealPromotionPolicy,
    VirtualToRealPromotionValidationContext,
    validate_virtual_to_real_promotion_receipt,
)


ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "docs" / "agents" / "evidence" / "20260801-v2r-promotion-source-audit.json"


def _policy() -> VirtualToRealPromotionPolicy:
    return VirtualToRealPromotionPolicy(
        policy_id="policy:v2r:tracked-simulator-replay:v1",
        policy_version="1",
        source_scope=HardwareExecutionMode.SIM,
        allowed_target_scopes=(HardwareExecutionMode.BENCH,),
        required_source_verification_basis=VerificationBasis.DETERMINISTIC,
        approval_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
        required_component_kinds=(PromotionComponentKind.CONTROLLER_PROFILE,),
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


def main() -> int:
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    policy = _policy()
    results: list[dict[str, object]] = []
    for case in audit["cases"]:
        if case["source_binding"]["execution_scope"] != "sim":
            raise SystemExit("tracked source scope drifted from sim")
        if case["promotion_readiness"] != "unverified":
            raise SystemExit("tracked source was unexpectedly promoted")
        result = validate_virtual_to_real_promotion_receipt(
            None,
            context=VirtualToRealPromotionValidationContext(
                expected_source_scope=HardwareExecutionMode.SIM,
                expected_target_scope=HardwareExecutionMode.BENCH,
                observed_source_outcome_evidence_ref=(f"evidence:{case['case_id']}"),
                observed_source_outcome_claim_satisfied=True,
                observed_source_verification_basis=(VerificationBasis.DETERMINISTIC),
                expected_target_executor_profile_sha256="0" * 64,
                expected_target_controller_profile_sha256="0" * 64,
                active_policy=policy,
                evidence_sources={},
                evaluated_at="2026-08-01T00:00:00+00:00",
            ),
        )
        results.append(
            {
                "case_id": case["case_id"],
                "promotion_status": result.status.value,
                "reasons": list(result.reasons),
                "dispatch_authority_created": result.dispatch_authority_created,
                "physical_execution_invoked": result.physical_execution_invoked,
            }
        )

    output = {
        "schema_version": "missionos_v2r_promotion_replay_smoke.v1",
        "source_case_count": len(results),
        "results": results,
        "promotion_receipt_created": False,
        "physical_safety_claimed": False,
        "physical_execution_invoked": False,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
