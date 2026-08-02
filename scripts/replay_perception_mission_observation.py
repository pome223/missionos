#!/usr/bin/env python3
"""Replay a stored live perception claim through Mission Contract readiness."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from missionos_core import (
    EvidenceOrigin,
    FrozenMissionContract,
    MissionRuntimeEvidence,
    ObservationFreshnessBasis,
    ObservationRequirement,
    OutcomeClaimSpec,
    PredicatePackageBinding,
    QuantificationScope,
    QuantificationScopeKind,
    TerminationPolicy,
    TerminationReason,
    VerificationBasis,
    canonical_sha256,
    check_mission_evidence_readiness,
)
from src.runtime.perception_mission_observation import (
    PERCEPTION_MISSION_OBSERVATION_EVIDENCE_KIND,
    project_perception_claim_to_mission_observation,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--execution-scope", required=True)
    parser.add_argument("--source-clock-domain-ref", required=True)
    parser.add_argument("--receipt-clock-domain-ref", required=True)
    parser.add_argument("--maximum-receipt-age-seconds", type=float, required=True)
    return parser.parse_args()


def _load(path: Path) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("artifact root must be an object")
    return value, sha256(payload).hexdigest()


def main() -> int:
    args = _arguments()
    artifact, artifact_sha256 = _load(args.artifact)
    claim = artifact.get("perception_claim")
    if not isinstance(claim, dict):
        raise ValueError("artifact perception_claim must be an object")

    projection = project_perception_claim_to_mission_observation(
        claim=claim,
        requirement_id="model_inferred_visual_claim",
        execution_scope=args.execution_scope,
        source_clock_domain_ref=args.source_clock_domain_ref,
        receipt_clock_domain_ref=args.receipt_clock_domain_ref,
    )
    binding = claim.get("corroboration_binding")
    evaluated_at = (
        str(binding.get("vlm_invocation_started_at") or "")
        if isinstance(binding, dict)
        else ""
    )
    report: dict[str, Any] = {
        "artifact_sha256": artifact_sha256,
        "projection": projection.to_dict(),
        "evaluated_at": evaluated_at,
        "claim_boundary": {
            "semantic_claim_origin": "model_inferred",
            "source_freshness_claimed": False,
            "receipt_freshness_evaluated": True,
            "outcome_claimed": False,
            "approval_created": False,
            "dispatch_authority_created": False,
            "runtime_effect_requested": False,
            "physical_execution_invoked": False,
        },
    }
    if projection.observation is None:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2

    contract = FrozenMissionContract(
        contract_id="perception-observation-replay",
        contract_version="1",
        execution_scope=projection.observation.execution_scope,
        reference_inputs=(),
        observation_requirements=(
            ObservationRequirement(
                requirement_id="model_inferred_visual_claim",
                evidence_kind=PERCEPTION_MISSION_OBSERVATION_EVIDENCE_KIND,
                required_origin=EvidenceOrigin.MODEL_INFERRED,
                maximum_age_seconds=args.maximum_receipt_age_seconds,
                freshness_basis=(
                    ObservationFreshnessBasis.MISSION_RECEIVED_AT
                ),
                source_clock_domain_ref=args.source_clock_domain_ref,
                receipt_clock_domain_ref=args.receipt_clock_domain_ref,
                require_source_receipt_binding=True,
            ),
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason="one stored perception claim is replayed",
        ),
        outcome_claim_spec=OutcomeClaimSpec(
            claim_id="perception-observation-ready",
            statement=(
                "The stored model-inferred perception claim is ready for a "
                "later approved predicate."
            ),
            claim_scope="one stored perception claim",
        ),
        predicate_package=PredicatePackageBinding(
            package_id="perception-readiness-only",
            package_version="1",
            content_sha256=canonical_sha256(
                {
                    "package_id": "perception-readiness-only",
                    "outcome_evaluation_implemented": False,
                }
            ),
        ),
        termination_policy=TerminationPolicy(
            allowed_reasons=(TerminationReason.EXPIRY,)
        ),
        required_verification_basis=VerificationBasis.MODEL_INFERRED,
    )
    evaluation = check_mission_evidence_readiness(
        contract=contract,
        evidence=MissionRuntimeEvidence(
            contract_sha256=contract.contract_sha256,
            observations=(projection.observation,),
        ),
        evaluated_at=evaluated_at,
        evaluation_clock_domain_ref=args.receipt_clock_domain_ref,
    )
    report["contract_sha256"] = contract.contract_sha256
    report["evaluation"] = evaluation.to_dict()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if evaluation.evidence_readiness.value == "ready" else 3


if __name__ == "__main__":
    raise SystemExit(main())
