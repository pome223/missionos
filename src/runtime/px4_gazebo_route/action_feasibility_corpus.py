"""Publication-safe PX4 Action Feasibility conformance replay.

The corpus freezes deterministic inputs and expected verifier results extracted
from reviewed simulator evidence. Replaying a case does not invoke an LLM, PX4,
Gazebo, a runner, or any execution authority. Runtime truth from the source run
and artifact truth produced by this replay remain explicitly separate.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from missionos_core import run_conformance_corpus

from src.runtime.corpus_publication_sanitation import (
    CASE_ID_PATTERN as _CASE_ID_PATTERN,
    publication_findings as _publication_findings,
)
from src.runtime.px4_gazebo_route.action_feasibility import (
    action_feasibility_hash_matches,
)
from src.runtime.px4_gazebo_route.core_action_feasibility_adapter import (
    attach_core_hazard_state,
    verify_runtime_recovery_action_feasibility,
)
from src.runtime.px4_gazebo_route.hazard_state import (
    hazard_state_hash_matches,
)


CORPUS_MANIFEST_SCHEMA_VERSION = (
    "missionos_action_feasibility_conformance_corpus.v1"
)
CORPUS_CASE_SCHEMA_VERSION = "missionos_action_feasibility_conformance_case.v1"
CORPUS_VERDICT_SCHEMA_VERSION = (
    "missionos_action_feasibility_conformance_verdict.v1"
)

_AUTHORITY_STAGES = (
    "proposal",
    "human_approval",
    "dispatch_revalidation",
    "dispatch_authority",
    "runner_ack",
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


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def seal_action_feasibility_corpus_case(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a case with a canonical integrity digest."""

    material = {
        key: value
        for key, value in dict(payload).items()
        if key != "case_sha256"
    }
    return {**material, "case_sha256": _canonical_sha256(material)}


def _authority_chain_reasons(
    authority_chain: Mapping[str, Any],
    *,
    scenario_class: str,
) -> list[str]:
    reasons: list[str] = []
    stages = {
        name: _mapping(authority_chain.get(name))
        for name in _AUTHORITY_STAGES
    }
    if any(not stages[name] for name in _AUTHORITY_STAGES):
        reasons.append("authority_chain_stage_missing")
        return reasons
    artifact_refs = [
        str(stages[name].get("artifact_ref") or "").strip()
        for name in _AUTHORITY_STAGES
    ]
    if any(not item for item in artifact_refs):
        reasons.append("authority_chain_artifact_ref_missing")
    elif len(set(artifact_refs)) != len(artifact_refs):
        reasons.append("authority_chain_artifact_refs_not_distinct")

    proposal = stages["proposal"]
    approval = stages["human_approval"]
    revalidation = stages["dispatch_revalidation"]
    authority = stages["dispatch_authority"]
    ack = stages["runner_ack"]
    effect = stages["observed_effect"]
    completion = stages["completion"]

    if proposal.get("approval_created") is not False:
        reasons.append("proposal_created_approval")
    if proposal.get("dispatch_authority_created") is not False:
        reasons.append("proposal_created_dispatch_authority")
    if proposal.get("physical_execution_invoked") is not False:
        reasons.append("proposal_claimed_physical_execution")
    if ack.get("ack_is_execution_effect") is not False:
        reasons.append("runner_ack_collapsed_with_execution_effect")
    if completion.get("delivery_completion_claimed") is not False:
        reasons.append("delivery_completion_overclaimed")
    if completion.get("physical_execution_invoked") is not False:
        reasons.append("physical_execution_overclaimed")

    if scenario_class == "positive":
        checks = {
            "positive_proposal_missing": (
                proposal.get("status") == "created"
                and proposal.get("llm_judgment_observed") is True
            ),
            "positive_human_approval_missing": (
                approval.get("status") == "approved"
                and approval.get("human_approval_performed") is True
            ),
            "positive_dispatch_revalidation_missing": (
                revalidation.get("status") == "valid"
            ),
            "positive_dispatch_authority_missing": (
                authority.get("created") is True
            ),
            "positive_runner_ack_missing": ack.get("observed") is True,
            "positive_target_effect_missing": (
                effect.get("target_reached") is True
                and effect.get("resume_status") == "resumed_auto_mission"
            ),
            "positive_terminal_observation_missing": (
                completion.get("landed") is True
                and completion.get("disarmed") is True
            ),
        }
    else:
        checks = {
            "refusal_proposal_not_rejected": proposal.get("status")
            in {"rejected_before_proposal", "operator_review_only"},
            "refusal_human_approval_present": (
                approval.get("human_approval_performed") is False
            ),
            "refusal_dispatch_revalidation_valid": (
                revalidation.get("status") != "valid"
            ),
            "refusal_dispatch_authority_present": (
                authority.get("created") is False
            ),
            "refusal_runner_ack_present": ack.get("observed") is False,
            "refusal_execution_effect_present": (
                effect.get("target_reached") is False
                and effect.get("resume_status") == "not_attempted"
            ),
            "refusal_completion_claimed": (
                completion.get("mission_completion_claimed") is False
            ),
        }
    reasons.extend(reason for reason, passed in checks.items() if not passed)
    return reasons


def _source_ref_reasons(
    hazard_state: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    cursor = _mapping(hazard_state.get("telemetry_cursor"))
    if not {
        "cursor_status",
        "sample_index",
        "elapsed_seconds",
    }.issubset(cursor):
        reasons.append("hazard_state_cursor_fields_missing")
    if not str(hazard_state.get("policy_sha256") or "").strip():
        reasons.append("hazard_state_policy_digest_missing")
    if not [
        item
        for item in candidate.get("source_refs") or []
        if str(item).strip()
    ]:
        reasons.append("candidate_source_refs_missing")
    for fact_name, value in _mapping(
        hazard_state.get("observed_facts")
    ).items():
        fact = _mapping(value)
        if fact.get("fact_status") == "not_applicable":
            continue
        if not [
            item for item in fact.get("source_refs") or [] if str(item).strip()
        ]:
            reasons.append(f"hazard_fact_source_refs_missing:{fact_name}")
    return reasons


def verify_action_feasibility_corpus_case(
    case: Mapping[str, Any],
) -> dict[str, Any]:
    """Replay one case through the deterministic PX4 verifier."""

    blocking_reasons: list[str] = []
    if case.get("schema_version") != CORPUS_CASE_SCHEMA_VERSION:
        blocking_reasons.append("corpus_case_schema_not_supported")
    case_id = str(case.get("case_id") or "")
    if not _CASE_ID_PATTERN.fullmatch(case_id):
        blocking_reasons.append("corpus_case_id_invalid")
    expected_digest = str(case.get("case_sha256") or "")
    material = {
        key: value for key, value in case.items() if key != "case_sha256"
    }
    if not expected_digest or expected_digest != _canonical_sha256(material):
        blocking_reasons.append("corpus_case_hash_mismatch")
    if _publication_findings(case):
        blocking_reasons.append("corpus_case_publication_boundary_violated")

    scenario_class = str(case.get("scenario_class") or "")
    if scenario_class not in {"positive", "refusal"}:
        blocking_reasons.append("corpus_case_scenario_class_invalid")

    truth_boundary = _mapping(case.get("truth_boundary"))
    artifact_truth = _mapping(truth_boundary.get("artifact_truth"))
    runtime_truth = _mapping(truth_boundary.get("runtime_truth"))
    if artifact_truth.get("case_is_replay_fixture") is not True:
        blocking_reasons.append("corpus_case_artifact_truth_missing")
    if runtime_truth.get("runtime_invoked_by_this_replay") is not False:
        blocking_reasons.append("corpus_case_runtime_truth_overclaimed")
    runtime_evidence_available = (
        runtime_truth.get("source_runtime_evidence_available") is True
    )
    contract_evidence_available = (
        runtime_truth.get("source_contract_evidence_available") is True
    )
    runtime_refs = [
        item
        for item in runtime_truth.get("source_runtime_evidence_refs") or []
        if str(item).strip()
    ]
    contract_refs = [
        item
        for item in runtime_truth.get("source_contract_evidence_refs") or []
        if str(item).strip()
    ]
    if not runtime_evidence_available and not contract_evidence_available:
        blocking_reasons.append("corpus_case_source_evidence_missing")
    if runtime_evidence_available and not runtime_refs:
        blocking_reasons.append("corpus_case_runtime_evidence_refs_missing")
    if contract_evidence_available and not contract_refs:
        blocking_reasons.append("corpus_case_contract_evidence_refs_missing")

    hazard_state = attach_core_hazard_state(
        _mapping(case.get("hazard_state"))
    )
    candidate = _mapping(case.get("candidate"))
    recovery_policy = _mapping(case.get("recovery_policy"))
    if not hazard_state_hash_matches(hazard_state):
        blocking_reasons.append("corpus_case_hazard_state_hash_mismatch")
    blocking_reasons.extend(_source_ref_reasons(hazard_state, candidate))

    evaluation = verify_runtime_recovery_action_feasibility(
        candidate=candidate,
        hazard_state=hazard_state,
        recovery_policy=recovery_policy,
    )
    if not action_feasibility_hash_matches(evaluation):
        blocking_reasons.append("corpus_case_evaluation_hash_mismatch")

    expected = _mapping(case.get("expected"))
    if evaluation.get("feasibility_status") != expected.get(
        "feasibility_status"
    ):
        blocking_reasons.append("corpus_case_feasibility_status_changed")
    if evaluation.get("blocking_reasons") != list(
        expected.get("blocking_reasons") or []
    ):
        blocking_reasons.append("corpus_case_blocking_reasons_changed")
    if evaluation.get("unverified_reasons") != list(
        expected.get("unverified_reasons") or []
    ):
        blocking_reasons.append("corpus_case_unverified_reasons_changed")
    required_assumptions = list(expected.get("required_assumptions") or [])
    if any(
        item not in list(evaluation.get("assumptions") or [])
        for item in required_assumptions
    ):
        blocking_reasons.append("corpus_case_required_assumptions_changed")

    blocking_reasons.extend(
        _authority_chain_reasons(
            _mapping(case.get("authority_chain")),
            scenario_class=scenario_class,
        )
    )
    if scenario_class == "positive" and evaluation.get(
        "feasibility_status"
    ) != "verified_feasible":
        blocking_reasons.append("positive_case_not_verified_feasible")
    if scenario_class == "refusal" and evaluation.get(
        "feasibility_status"
    ) == "verified_feasible":
        blocking_reasons.append("refusal_case_became_verified_feasible")

    unique_reasons = list(dict.fromkeys(blocking_reasons))
    return {
        "schema_version": CORPUS_VERDICT_SCHEMA_VERSION,
        "case_id": case_id,
        "verification_status": "verified" if not unique_reasons else "failed",
        "scenario_class": scenario_class,
        "feasibility_status": evaluation.get("feasibility_status"),
        "blocking_reasons": unique_reasons,
        "source_runtime_reexecuted": False,
        "llm_invoked": False,
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "completion_claimed": False,
        "progress_counted": False,
    }


def verify_action_feasibility_corpus(
    manifest_path: Path,
) -> dict[str, Any]:
    """Load and verify every case named by a corpus manifest."""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("corpus manifest must contain one JSON object")
    reasons: list[str] = []
    if manifest.get("schema_version") != CORPUS_MANIFEST_SCHEMA_VERSION:
        reasons.append("corpus_manifest_schema_not_supported")
    expected_manifest_hash = str(manifest.get("manifest_sha256") or "")
    unhashed_manifest = {
        key: value
        for key, value in manifest.items()
        if key != "manifest_sha256"
    }
    if (
        not expected_manifest_hash
        or expected_manifest_hash != _canonical_sha256(unhashed_manifest)
    ):
        reasons.append("corpus_manifest_hash_mismatch")
    if _publication_findings(manifest):
        reasons.append("corpus_manifest_publication_boundary_violated")
    cases: list[dict[str, Any]] = []
    root = manifest_path.parent.resolve()
    for entry in _sequence(manifest.get("cases")):
        entry = _mapping(entry)
        relative_path = Path(str(entry.get("path") or ""))
        if (
            not relative_path.name
            or relative_path.is_absolute()
            or ".." in relative_path.parts
        ):
            reasons.append("corpus_manifest_case_path_invalid")
            continue
        case_path = (root / relative_path).resolve()
        if root not in case_path.parents:
            reasons.append("corpus_manifest_case_path_escaped")
            continue
        try:
            case = json.loads(case_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            reasons.append(f"corpus_case_unreadable:{relative_path}")
            continue
        if not isinstance(case, dict):
            reasons.append(f"corpus_case_not_object:{relative_path}")
            continue
        if str(entry.get("sha256") or "") != _canonical_sha256(case):
            reasons.append(f"corpus_manifest_case_hash_mismatch:{relative_path}")
        verdict = verify_action_feasibility_corpus_case(case)
        cases.append(verdict)
        if verdict["verification_status"] != "verified":
            reasons.append(f"corpus_case_failed:{verdict['case_id']}")

    positive_count = sum(
        item.get("scenario_class") == "positive" for item in cases
    )
    refusal_count = sum(
        item.get("scenario_class") == "refusal" for item in cases
    )
    if positive_count < 1:
        reasons.append("corpus_positive_case_missing")
    if refusal_count < 1:
        reasons.append("corpus_refusal_case_missing")
    if manifest.get("case_count") != len(cases):
        reasons.append("corpus_manifest_case_count_mismatch")
    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "schema_version": CORPUS_VERDICT_SCHEMA_VERSION,
        "verification_status": "verified" if not unique_reasons else "failed",
        "case_count": len(cases),
        "positive_case_count": positive_count,
        "refusal_case_count": refusal_count,
        "case_results": cases,
        "blocking_reasons": unique_reasons,
        "source_runtime_reexecuted": False,
        "llm_invoked": False,
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "completion_claimed": False,
        "progress_counted": False,
    }


def _core_case_adapter(case: Mapping[str, Any]) -> dict[str, Any]:
    """Expose the existing verifier to the backend-neutral Core suite."""

    verdict = verify_action_feasibility_corpus_case(case)
    return {
        "passed": verdict["verification_status"] == "verified",
        "feasibility_status": verdict["feasibility_status"],
        "reasons": verdict["blocking_reasons"],
        "output_flags": {
            "llm_invoked": verdict["llm_invoked"],
            "approval_created": verdict["approval_created"],
            "dispatch_authority_created": verdict[
                "dispatch_authority_created"
            ],
            "dispatch_request_sent": False,
            "physical_execution_invoked": verdict[
                "physical_execution_invoked"
            ],
            "progress_claimed": verdict["progress_counted"],
            "completion_claimed": verdict["completion_claimed"],
            "delivery_completion_claimed": False,
        },
    }


def verify_action_feasibility_corpus_through_core(
    manifest_path: Path,
) -> dict[str, Any]:
    """Run the corpus through Core while retaining backend calculations."""

    return run_conformance_corpus(
        manifest_path,
        execute_case=_core_case_adapter,
    )


__all__ = [
    "CORPUS_CASE_SCHEMA_VERSION",
    "CORPUS_MANIFEST_SCHEMA_VERSION",
    "CORPUS_VERDICT_SCHEMA_VERSION",
    "seal_action_feasibility_corpus_case",
    "verify_action_feasibility_corpus",
    "verify_action_feasibility_corpus_case",
    "verify_action_feasibility_corpus_through_core",
]
