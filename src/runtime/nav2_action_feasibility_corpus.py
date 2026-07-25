"""Publication-safe Nav2 Action Feasibility conformance replay.

Cases contain sealed, deterministic planning evidence.  Replay invokes neither
ROS/Nav2 nor an LLM and creates no approval, dispatch, execution, progress, or
completion authority.
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
from src.runtime.nav2_core_action_feasibility_adapter import (
    verify_nav2_core_action_candidate,
)


NAV2_CORPUS_SCHEMA = "missionos_nav2_action_feasibility_corpus.v1"
NAV2_CORPUS_CASE_SCHEMA = "missionos_nav2_action_feasibility_case.v1"
NAV2_CORPUS_VERDICT_SCHEMA = "missionos_nav2_action_feasibility_verdict.v1"
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


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
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


def seal_nav2_corpus_case(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    material = {
        key: value
        for key, value in dict(payload).items()
        if key != "case_sha256"
    }
    return {**material, "case_sha256": _sha256(material)}


def _publication_safe(value: Any) -> bool:
    return not publication_findings(value)


def _authority_chain_valid(
    value: Mapping[str, Any],
    *,
    scenario_class: str,
) -> bool:
    chain = _mapping(value)
    proposal = _mapping(chain.get("proposal"))
    approval = _mapping(chain.get("human_approval"))
    revalidation = _mapping(chain.get("dispatch_revalidation"))
    authority = _mapping(chain.get("dispatch_authority"))
    ack = _mapping(chain.get("runner_ack"))
    effect = _mapping(chain.get("observed_effect"))
    completion = _mapping(chain.get("completion"))
    if proposal.get("approval_created") is not False:
        return False
    if proposal.get("dispatch_authority_created") is not False:
        return False
    if ack.get("ack_is_execution_effect") is not False:
        return False
    if completion.get("delivery_completion_claimed") is not False:
        return False
    if completion.get("physical_execution_invoked") is not False:
        return False
    if scenario_class == "positive":
        return (
            proposal.get("status") == "created"
            and approval.get("status") == "approved"
            and approval.get("human_approval_performed") is True
            and revalidation.get("status") == "valid"
            and authority.get("created") is True
            and ack.get("observed") is True
            and effect.get("target_reached") is True
            and effect.get("resume_status") == "resumed_route"
            and completion.get("sim_route_terminal_observed") is True
        )
    return (
        proposal.get("status")
        in {"rejected_before_proposal", "operator_review_only"}
        and approval.get("human_approval_performed") is False
        and revalidation.get("status") != "valid"
        and authority.get("created") is False
        and ack.get("observed") is False
        and effect.get("target_reached") is False
        and effect.get("resume_status") == "not_attempted"
        and completion.get("mission_completion_claimed") is False
    )


def verify_nav2_corpus_case(
    case: Mapping[str, Any],
) -> dict[str, Any]:
    """Replay one sealed Nav2 case through the Core-backed adapter."""

    reasons: list[str] = []
    case_id = str(case.get("case_id") or "")
    if case.get("schema_version") != NAV2_CORPUS_CASE_SCHEMA:
        reasons.append("nav2_corpus_case_schema_not_supported")
    if not _CASE_ID.fullmatch(case_id):
        reasons.append("nav2_corpus_case_id_invalid")
    material = {
        key: value for key, value in case.items() if key != "case_sha256"
    }
    if case.get("case_sha256") != _sha256(material):
        reasons.append("nav2_corpus_case_hash_mismatch")
    if not _publication_safe(case):
        reasons.append("nav2_corpus_publication_boundary_violated")
    scenario_class = str(case.get("scenario_class") or "")
    if scenario_class not in {"positive", "refusal"}:
        reasons.append("nav2_corpus_scenario_class_invalid")

    artifact = verify_nav2_core_action_candidate(
        evaluation=_mapping(case.get("evaluation")),
        candidate_evaluation=_mapping(case.get("candidate")),
        obstacle=_mapping(case.get("obstacle")),
        robot_collision_envelope=_mapping(
            case.get("robot_collision_envelope")
        ),
        active_policy=_mapping(case.get("active_policy")),
        evaluated_at=str(case.get("evaluated_at") or ""),
        previous_hazard_state=(
            _mapping(case.get("previous_hazard_state")) or None
        ),
    )
    result = _mapping(artifact.get("action_feasibility"))
    status = result.get("status")
    status = status.value if hasattr(status, "value") else str(status)
    blocked = list(result.get("blocked_reasons") or [])
    unverified = list(result.get("unverified_reasons") or [])
    expected = _mapping(case.get("expected"))
    if status != expected.get("status"):
        reasons.append("nav2_corpus_status_changed")
    required_reason = str(expected.get("required_reason") or "")
    if required_reason and required_reason not in [*blocked, *unverified]:
        reasons.append("nav2_corpus_required_reason_missing")
    if scenario_class == "positive" and status != "verified_feasible":
        reasons.append("nav2_corpus_positive_not_verified")
    if scenario_class == "refusal" and status == "verified_feasible":
        reasons.append("nav2_corpus_refusal_became_feasible")
    if not _authority_chain_valid(
        _mapping(case.get("authority_chain")),
        scenario_class=scenario_class,
    ):
        reasons.append("nav2_corpus_authority_chain_invalid")

    reasons = list(dict.fromkeys(reasons))
    return {
        "schema_version": NAV2_CORPUS_VERDICT_SCHEMA,
        "case_id": case_id,
        "scenario_class": scenario_class,
        "status": status,
        "passed": not reasons,
        "reasons": reasons,
        "source_runtime_reexecuted": False,
        "output_flags": dict(_AUTHORITY_FLAGS),
    }


def verify_nav2_corpus_through_core(
    manifest_path: Path,
) -> dict[str, Any]:
    """Run sealed Nav2 cases through the same Core corpus runner as PX4."""

    return run_conformance_corpus(
        manifest_path,
        execute_case=verify_nav2_corpus_case,
    )


def verify_nav2_corpus(
    manifest_path: Path,
) -> dict[str, Any]:
    """Validate Nav2 manifest metadata in addition to Core invariants."""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reasons: list[str] = []
    if manifest.get("schema_version") != NAV2_CORPUS_SCHEMA:
        reasons.append("nav2_corpus_manifest_schema_not_supported")
    if manifest.get("manifest_sha256") != _sha256(
        {
            key: value
            for key, value in manifest.items()
            if key != "manifest_sha256"
        }
    ):
        reasons.append("nav2_corpus_manifest_hash_mismatch")
    if not _publication_safe(manifest):
        reasons.append("nav2_corpus_manifest_publication_boundary_violated")
    core_result = verify_nav2_corpus_through_core(manifest_path)
    reasons.extend(core_result.get("reasons") or [])
    verdicts = _sequence(core_result.get("case_verdicts"))
    positive = sum(
        _mapping(item.get("adapter_result")).get("scenario_class")
        == "positive"
        for item in verdicts
        if isinstance(item, Mapping)
    )
    refusal = sum(
        _mapping(item.get("adapter_result")).get("scenario_class")
        == "refusal"
        for item in verdicts
        if isinstance(item, Mapping)
    )
    if positive < 1:
        reasons.append("nav2_corpus_positive_case_missing")
    if refusal < 1:
        reasons.append("nav2_corpus_refusal_case_missing")
    return {
        **core_result,
        "status": "verified" if not reasons else "failed",
        "reasons": list(dict.fromkeys(reasons)),
        "positive_case_count": positive,
        "refusal_case_count": refusal,
    }


__all__ = [
    "NAV2_CORPUS_CASE_SCHEMA",
    "NAV2_CORPUS_SCHEMA",
    "NAV2_CORPUS_VERDICT_SCHEMA",
    "seal_nav2_corpus_case",
    "verify_nav2_corpus",
    "verify_nav2_corpus_case",
    "verify_nav2_corpus_through_core",
]
