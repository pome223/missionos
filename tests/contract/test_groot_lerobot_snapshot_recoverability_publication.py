from __future__ import annotations

import json
from pathlib import Path


def _publication() -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / "docs/agents/evidence"
        / "20260822-groot-n17-lerobot-snapshot-recoverability-publication.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_snapshot_recoverability_publication_is_internally_consistent() -> None:
    publication = _publication()
    groot = publication["groot_diagnostic_control"]
    groot_result = groot["result"]
    oracle = publication["privileged_recoverability_control"]
    oracle_result = oracle["result"]

    assert groot_result["short_target_trial_count"] == 5
    assert groot_result["benchmark_standard_trial_count"] == 5
    assert groot_result["completed_trial_count"] == 10
    assert groot_result["short_target_success_count"] == 0
    assert groot_result["benchmark_standard_success_count"] == 0
    assert groot_result["terminal_goal_predicate_vector"] == [True, False, True]
    assert groot_result["preservation_violation_observed"] is False

    assert oracle["same_7d_simulator_action_interface_used"] is True
    assert oracle_result["predicate_success_first_observed_after_action"] == 61
    assert oracle_result["settling_action_count"] == 20
    assert oracle_result["total_action_count"] == 81
    assert oracle_result["total_action_count"] <= oracle_result["maximum_action_budget"]
    assert oracle_result["terminal_goal_predicate_vector"] == [True, True, True]
    assert oracle_result["preservation_violation_observed"] is False
    assert oracle_result["stable_success_after_settle"] is True


def test_snapshot_recoverability_publication_preserves_authority_boundaries() -> None:
    publication = _publication()
    separation = publication["evidence_separation"]
    boundary = publication["claim_boundary"]

    assert separation["human_approval_created"] is False
    assert separation["governed_dispatch_created"] is False
    assert separation["controller_ack_observed"] is False
    assert separation["semantic_repair_established"] is False
    assert separation["physical_execution_invoked"] is False
    assert boundary["general_groot_recovery_failure_rate_established"] is False
    assert boundary["autonomous_oracle_recovery_capability_established"] is False
    assert boundary["observation_grounded_fallback_established"] is False
    assert boundary["semantic_repair_established"] is False
    assert boundary["physical_execution_established"] is False


def test_snapshot_recoverability_source_digests_are_sha256() -> None:
    publication = _publication()
    digests = [publication["snapshot_artifact_sha256"]]
    for section_name in (
        "groot_diagnostic_control",
        "privileged_recoverability_control",
    ):
        section = publication[section_name]
        digests.extend(
            value
            for key, value in section.items()
            if key.endswith("_sha256")
        )

    assert digests
    assert all(len(digest) == 64 for digest in digests)
    assert all(set(digest) <= set("0123456789abcdef") for digest in digests)
