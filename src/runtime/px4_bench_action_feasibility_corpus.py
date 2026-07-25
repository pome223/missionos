"""Publication-safe PX4 bench Action Feasibility conformance replay.

Cases are sealed, deterministic extracts of the bench physical-safety boundary.
Replay opens no serial port, touches no airframe, invokes no LLM, and creates no
approval, dispatch, execution, progress, or completion authority.

`physical_execution_invoked` is false for every case. That flag describes what
*this replay* did, not what the source bench run did. A physical claim is made
only by a live bench E2E record and the stable-readiness summary, never here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from missionos_core import run_conformance_corpus

from src.runtime.corpus_publication_sanitation import publication_findings
from src.runtime.px4_bench_core_action_feasibility_adapter import (
    verify_px4_bench_core_action_candidate,
)


BENCH_CORPUS_SCHEMA = "missionos_px4_bench_action_feasibility_corpus.v1"
BENCH_CORPUS_CASE_SCHEMA = "missionos_px4_bench_action_feasibility_case.v1"
BENCH_CORPUS_VERDICT_SCHEMA = (
    "missionos_px4_bench_action_feasibility_verdict.v1"
)

_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,95}$")
_AUTHORITY_FLAGS = {
    "llm_invoked": False,
    "approval_created": False,
    "dispatch_authority_created": False,
    "dispatch_request_sent": False,
    "physical_execution_invoked": False,
    "progress_claimed": False,
    "completion_claimed": False,
    "delivery_completion_claimed": False,
}
_AUTHORITY_STAGES = (
    "proposal",
    "human_approval",
    "dispatch_revalidation",
    "dispatch_authority",
    "adapter_ack",
    "observed_effect",
    "completion",
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return list(value)
    return []


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def seal_px4_bench_corpus_case(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a bench case with a canonical integrity digest."""

    material = {
        key: value
        for key, value in dict(payload).items()
        if key != "case_sha256"
    }
    return {**material, "case_sha256": _sha256(material)}


def _publication_safe(value: Any) -> bool:
    return not publication_findings(value)


def _truth_boundary_reasons(case: Mapping[str, Any]) -> list[str]:
    """A bench case must never promote replay into physical evidence."""

    reasons: list[str] = []
    boundary = _mapping(case.get("truth_boundary"))
    runtime_truth = _mapping(boundary.get("runtime_truth"))
    if not _mapping(boundary.get("artifact_truth")):
        reasons.append("bench_corpus_artifact_truth_missing")
    if not runtime_truth:
        reasons.append("bench_corpus_runtime_truth_missing")
        return reasons
    if runtime_truth.get("runtime_invoked_by_this_replay") is not False:
        reasons.append("bench_corpus_replay_claimed_runtime")
    if runtime_truth.get("source_runtime_reexecuted") is not False:
        reasons.append("bench_corpus_replay_claimed_reexecution")
    if runtime_truth.get("physical_execution_invoked_by_this_replay") is not False:
        reasons.append("bench_corpus_replay_claimed_physical_execution")
    # Live bench evidence must name its execution mode; a contract-only case
    # must not claim live runtime evidence it does not have.
    if runtime_truth.get("source_runtime_evidence_available") is True:
        if str(runtime_truth.get("source_execution_mode") or "") != "bench":
            reasons.append("bench_corpus_source_execution_mode_invalid")
        if not _sequence(runtime_truth.get("source_runtime_evidence_refs")):
            reasons.append("bench_corpus_source_runtime_evidence_refs_missing")
    else:
        if _sequence(runtime_truth.get("source_runtime_evidence_refs")):
            reasons.append("bench_corpus_source_runtime_evidence_overclaimed")
        if not _sequence(runtime_truth.get("source_contract_evidence_refs")):
            reasons.append("bench_corpus_source_contract_evidence_refs_missing")
    return reasons


def _verifier_assumption_reasons(case: Mapping[str, Any]) -> list[str]:
    """Every observed fact must be classified as machine- or operator-sourced.

    Nothing verifies that a physical E-stop exists, that the airframe is
    restrained, or that the propellers are off. Those facts hold because a named
    operator asserted them. Leaving that implicit is how an attestation quietly
    acquires the weight of a measurement, so the classification is required and
    checked rather than written in prose.
    """

    reasons: list[str] = []
    assumptions = _mapping(case.get("verifier_assumptions"))
    if not assumptions:
        return ["bench_corpus_verifier_assumptions_missing"]
    machine = [str(name) for name in _sequence(assumptions.get("machine_observed_facts"))]
    operator = [
        str(name) for name in _sequence(assumptions.get("operator_declared_facts"))
    ]
    if not operator:
        reasons.append("bench_corpus_operator_declared_facts_missing")
    if assumptions.get("operator_declared_facts_are_machine_verified") is not False:
        reasons.append("bench_corpus_operator_declaration_overclaimed")
    overlap = set(machine) & set(operator)
    if overlap:
        reasons.append("bench_corpus_fact_classified_twice")

    classified = set(machine) | set(operator)
    observed = {
        str(fact.get("name"))
        for fact in _sequence(_mapping(case.get("hazard_state")).get("observed_facts"))
        if isinstance(fact, Mapping)
    }
    if observed - classified:
        reasons.append("bench_corpus_fact_unclassified")
    return reasons


def _authority_chain_reasons(
    authority_chain: Mapping[str, Any],
    *,
    scenario_class: str,
) -> list[str]:
    reasons: list[str] = []
    stages = {
        name: _mapping(authority_chain.get(name)) for name in _AUTHORITY_STAGES
    }
    if any(not stages[name] for name in _AUTHORITY_STAGES):
        return ["bench_corpus_authority_chain_stage_missing"]
    refs = [
        str(stages[name].get("artifact_ref") or "").strip()
        for name in _AUTHORITY_STAGES
    ]
    if any(not item for item in refs):
        reasons.append("bench_corpus_authority_chain_artifact_ref_missing")
    elif len(set(refs)) != len(refs):
        reasons.append("bench_corpus_authority_chain_artifact_refs_not_distinct")

    proposal = stages["proposal"]
    approval = stages["human_approval"]
    revalidation = stages["dispatch_revalidation"]
    authority = stages["dispatch_authority"]
    ack = stages["adapter_ack"]
    effect = stages["observed_effect"]
    completion = stages["completion"]

    if proposal.get("approval_created") is not False:
        reasons.append("bench_corpus_proposal_created_approval")
    if proposal.get("dispatch_authority_created") is not False:
        reasons.append("bench_corpus_proposal_created_dispatch_authority")
    # ACK is a transport fact. It is never the physical effect.
    if ack.get("ack_is_execution_effect") is not False:
        reasons.append("bench_corpus_ack_collapsed_with_execution_effect")
    # The bench slice never claims flight, mission, or delivery.
    if completion.get("mission_completion_claimed") is not False:
        reasons.append("bench_corpus_mission_completion_overclaimed")
    if completion.get("delivery_completion_claimed") is not False:
        reasons.append("bench_corpus_delivery_completion_overclaimed")
    if completion.get("flight_claimed") is not False:
        reasons.append("bench_corpus_flight_overclaimed")

    if scenario_class == "positive":
        if completion.get("completion_scope") != "adapter_action":
            reasons.append("bench_corpus_positive_completion_scope_invalid")
        checks = {
            "bench_corpus_positive_proposal_missing": (
                proposal.get("status") == "created"
            ),
            "bench_corpus_positive_approval_missing": (
                approval.get("status") == "approved"
                and approval.get("human_approval_performed") is True
            ),
            "bench_corpus_positive_revalidation_missing": (
                revalidation.get("status") == "valid"
            ),
            "bench_corpus_positive_authority_missing": (
                authority.get("created") is True
            ),
            "bench_corpus_positive_ack_missing": ack.get("observed") is True,
            "bench_corpus_positive_state_readback_missing": (
                effect.get("armed_state_readback_observed") is True
                and effect.get("disarm_observed") is True
            ),
        }
        reasons.extend(name for name, ok in checks.items() if not ok)
        return reasons

    if completion.get("completion_scope") != "none":
        reasons.append("bench_corpus_refusal_completion_scope_invalid")
    checks = {
        "bench_corpus_refusal_proposal_invalid": (
            proposal.get("status")
            in {"rejected_before_proposal", "operator_review_only"}
        ),
        "bench_corpus_refusal_approval_invalid": (
            approval.get("human_approval_performed") is False
        ),
        "bench_corpus_refusal_revalidation_invalid": (
            revalidation.get("status") != "valid"
        ),
        "bench_corpus_refusal_authority_invalid": (
            authority.get("created") is False
        ),
        "bench_corpus_refusal_ack_invalid": ack.get("observed") is False,
        "bench_corpus_refusal_effect_invalid": (
            effect.get("armed_state_readback_observed") is False
            and effect.get("disarm_observed") is False
        ),
    }
    reasons.extend(name for name, ok in checks.items() if not ok)
    return reasons


def verify_px4_bench_corpus_case(
    case: Mapping[str, Any],
) -> dict[str, Any]:
    """Replay one sealed bench case through the Core-backed adapter."""

    reasons: list[str] = []
    case_id = str(case.get("case_id") or "")
    if case.get("schema_version") != BENCH_CORPUS_CASE_SCHEMA:
        reasons.append("bench_corpus_case_schema_not_supported")
    if not _CASE_ID.fullmatch(case_id):
        reasons.append("bench_corpus_case_id_invalid")
    material = {
        key: value for key, value in case.items() if key != "case_sha256"
    }
    if case.get("case_sha256") != _sha256(material):
        reasons.append("bench_corpus_case_hash_mismatch")
    if not _publication_safe(case):
        reasons.append("bench_corpus_publication_boundary_violated")
    scenario_class = str(case.get("scenario_class") or "")
    if scenario_class not in {"positive", "refusal"}:
        reasons.append("bench_corpus_scenario_class_invalid")
    reasons.extend(_truth_boundary_reasons(case))
    reasons.extend(_verifier_assumption_reasons(case))

    artifact = verify_px4_bench_core_action_candidate(
        hazard_state=_mapping(case.get("hazard_state")),
        candidate=_mapping(case.get("candidate")),
        active_policy=_mapping(case.get("active_policy")),
        evaluated_at=str(case.get("evaluated_at") or ""),
    )
    result = _mapping(artifact.get("action_feasibility"))
    status = result.get("status")
    status = status.value if hasattr(status, "value") else str(status)
    blocked = list(result.get("blocked_reasons") or [])
    unverified = list(result.get("unverified_reasons") or [])

    expected = _mapping(case.get("expected"))
    if status != expected.get("status"):
        reasons.append("bench_corpus_status_changed")
    required_reason = str(expected.get("required_reason") or "")
    if required_reason and required_reason not in [*blocked, *unverified]:
        reasons.append("bench_corpus_required_reason_missing")
    if scenario_class == "positive" and status != "verified_feasible":
        reasons.append("bench_corpus_positive_not_verified")
    if scenario_class == "refusal" and status == "verified_feasible":
        reasons.append("bench_corpus_refusal_became_feasible")
    reasons.extend(
        _authority_chain_reasons(
            _mapping(case.get("authority_chain")),
            scenario_class=scenario_class,
        )
    )

    reasons = list(dict.fromkeys(reasons))
    return {
        "schema_version": BENCH_CORPUS_VERDICT_SCHEMA,
        "case_id": case_id,
        "scenario_class": scenario_class,
        "status": status,
        "blocked_reasons": blocked,
        "unverified_reasons": unverified,
        "passed": not reasons,
        "reasons": reasons,
        "source_runtime_reexecuted": False,
        "output_flags": dict(_AUTHORITY_FLAGS),
    }


def verify_px4_bench_corpus_through_core(
    manifest_path: Path,
) -> dict[str, Any]:
    """Run sealed bench cases through the same Core runner as PX4 and Nav2."""

    return run_conformance_corpus(
        manifest_path,
        execute_case=verify_px4_bench_corpus_case,
    )


def verify_px4_bench_corpus(manifest_path: Path) -> dict[str, Any]:
    """Validate bench manifest metadata in addition to Core invariants."""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reasons: list[str] = []
    if manifest.get("schema_version") != BENCH_CORPUS_SCHEMA:
        reasons.append("bench_corpus_manifest_schema_not_supported")
    if manifest.get("manifest_sha256") != _sha256(
        {
            key: value
            for key, value in manifest.items()
            if key != "manifest_sha256"
        }
    ):
        reasons.append("bench_corpus_manifest_hash_mismatch")
    if not _publication_safe(manifest):
        reasons.append("bench_corpus_manifest_publication_boundary_violated")
    core_result = verify_px4_bench_corpus_through_core(manifest_path)
    reasons.extend(core_result.get("reasons") or [])
    verdicts = [
        item
        for item in _sequence(core_result.get("case_verdicts"))
        if isinstance(item, Mapping)
    ]
    positive = sum(
        _mapping(item.get("adapter_result")).get("scenario_class") == "positive"
        for item in verdicts
    )
    refusal = sum(
        _mapping(item.get("adapter_result")).get("scenario_class") == "refusal"
        for item in verdicts
    )
    if positive < 1:
        reasons.append("bench_corpus_positive_case_missing")
    if refusal < 1:
        reasons.append("bench_corpus_refusal_case_missing")
    reasons = list(dict.fromkeys(reasons))
    return {
        **core_result,
        "status": "verified" if not reasons else "failed",
        "reasons": reasons,
        "positive_case_count": positive,
        "refusal_case_count": refusal,
    }


__all__ = [
    "BENCH_CORPUS_CASE_SCHEMA",
    "BENCH_CORPUS_SCHEMA",
    "BENCH_CORPUS_VERDICT_SCHEMA",
    "seal_px4_bench_corpus_case",
    "verify_px4_bench_corpus",
    "verify_px4_bench_corpus_case",
    "verify_px4_bench_corpus_through_core",
]
