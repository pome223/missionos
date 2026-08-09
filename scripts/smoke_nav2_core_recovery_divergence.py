#!/usr/bin/env python3
"""Explain a publication-safe Nav2 Recovery candidate divergence through Core."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from src.runtime.nav2_core_action_feasibility_adapter import (
    evaluate_nav2_recovery_candidates_through_core,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CASE_ROOT = REPO_ROOT / "tests/golden/action_feasibility/nav2_v1/cases"
BLOCKED_CASE = CASE_ROOT / "nav2-refusal-missing-obstacle-geometry.json"
REFERENCE_CASE = CASE_ROOT / "nav2-positive-verified-bypass.json"
AUTHORITY_FLAGS = (
    "approval_created",
    "dispatch_authority_created",
    "dispatch_request_sent",
    "physical_execution_invoked",
    "completion_claimed",
    "delivery_completion_claimed",
    "progress_claimed",
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _case_verdict(path: Path) -> dict[str, Any]:
    case = json.loads(path.read_text(encoding="utf-8"))
    verdict = evaluate_nav2_recovery_candidates_through_core(
        evaluation=case["evaluation"],
        obstacle=case["obstacle"],
        robot_collision_envelope=case["robot_collision_envelope"],
        active_policy=case["active_policy"],
        evaluated_at=case["evaluated_at"],
    )
    candidates: list[dict[str, Any]] = []
    for item in verdict["candidate_evaluations"]:
        artifact = _mapping(item.get("core_action_feasibility"))
        feasibility = _mapping(artifact.get("action_feasibility"))
        extension_verdicts = [
            _mapping(value) for value in feasibility.get("extension_verdicts") or []
        ]
        verification_items = [
            json.loads(json.dumps(verification_item))
            for extension in extension_verdicts
            for verification_item in extension.get("verification_items") or []
            if isinstance(verification_item, Mapping)
        ]
        candidate = _mapping(artifact.get("action_candidate"))
        hazard_state = _mapping(artifact.get("hazard_state"))
        observed_facts = [
            {
                "name": fact.get("name"),
                "status": fact.get("status"),
                "source": dict(_mapping(fact.get("source"))),
                "value": dict(_mapping(fact.get("value"))),
            }
            for fact in hazard_state.get("observed_facts") or []
            if isinstance(fact, Mapping)
        ]
        candidates.append(
            {
                "candidate_id": item.get("candidate_id"),
                "path_valid": item.get("path_valid"),
                "core_action_feasibility_status": item.get(
                    "core_action_feasibility_status"
                ),
                "blocked_reasons": list(feasibility.get("blocked_reasons") or []),
                "unverified_reasons": list(
                    feasibility.get("unverified_reasons") or []
                ),
                "verification_items": verification_items,
                "evidence_refs": list(candidate.get("evidence_refs") or []),
                "observed_fact_provenance": observed_facts,
                "authority": {
                    key: bool(artifact.get(key)) for key in AUTHORITY_FLAGS
                },
            }
        )
    return {
        "case_id": case["case_id"],
        "case_sha256": case["case_sha256"],
        "evaluation_status": verdict["evaluation_status"],
        "blocking_reasons": list(verdict.get("blocking_reasons") or []),
        "selected_candidate_id": _mapping(verdict.get("selected_candidate")).get(
            "candidate_id"
        ),
        "policy_binding": dict(_mapping(verdict.get("core_policy_binding"))),
        "obstacle_input": dict(case["obstacle"]),
        "candidates": candidates,
        "authority": {key: bool(verdict.get(key)) for key in AUTHORITY_FLAGS},
    }


def build_report() -> dict[str, Any]:
    blocked = _case_verdict(BLOCKED_CASE)
    reference = _case_verdict(REFERENCE_CASE)
    blocked_obstacle = blocked["obstacle_input"]
    reference_obstacle = reference["obstacle_input"]
    missing_reference_inputs = sorted(
        key
        for key, value in reference_obstacle.items()
        if value is not None and blocked_obstacle.get(key) is None
    )
    return {
        "schema_version": "missionos_nav2_core_recovery_divergence_report.v1",
        "comparison_scope": "publication_safe_fixture_pair",
        "historical_internal_route_artifact_compared": False,
        "root_cause_classification": "missing_evidence",
        "policy_binding_equal": (
            blocked["policy_binding"] == reference["policy_binding"]
        ),
        "missing_reference_inputs": missing_reference_inputs,
        "blocked_case": blocked,
        "verified_reference_case": reference,
        "authority_boundary": {
            "proposal_is_approval": False,
            "candidate_selection_is_dispatch": False,
            "core_verification_is_execution": False,
            "physical_execution_invoked": False,
        },
        "conclusion": (
            "The blocked fixture lacks source-backed obstacle height evidence. "
            "Core therefore leaves every candidate unverified and exposes no "
            "selectable Recovery candidate. The historical private route is not "
            "part of this public comparison."
        ),
    }


def _report_is_expected(report: Mapping[str, Any]) -> bool:
    blocked = _mapping(report.get("blocked_case"))
    reference = _mapping(report.get("verified_reference_case"))
    blocked_candidates = blocked.get("candidates") or []
    reference_candidates = reference.get("candidates") or []
    return bool(
        report.get("root_cause_classification") == "missing_evidence"
        and report.get("policy_binding_equal") is True
        and report.get("missing_reference_inputs")
        == ["runtime_obstacle_size_z_m"]
        and blocked.get("evaluation_status") == "blocked"
        and "no_core_verified_recovery_candidate"
        in (blocked.get("blocking_reasons") or [])
        and blocked_candidates
        and all(
            item.get("core_action_feasibility_status") == "unverified"
            for item in blocked_candidates
        )
        and reference.get("evaluation_status") == "validated"
        and reference_candidates
        and all(
            item.get("core_action_feasibility_status") == "verified_feasible"
            for item in reference_candidates
        )
        and all(
            value is False
            for value in _mapping(report.get("authority_boundary")).values()
        )
    )


def main() -> int:
    report = build_report()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if _report_is_expected(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
