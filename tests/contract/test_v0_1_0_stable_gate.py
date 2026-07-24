from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_v0_1_0_stable_gate import (
    EVIDENCE_PATH,
    EXPECTED_STATUSES,
    evaluate_stable_gate,
)


pytestmark = pytest.mark.contract


def test_stable_gate_requires_two_complete_tri_state_backends() -> None:
    verdict = evaluate_stable_gate()

    assert verdict["status"] == "ready"
    assert verdict["blocking_reasons"] == []
    assert set(verdict["backend_results"]) == {"px4", "nav2"}
    assert all(
        set(result["statuses"]) == EXPECTED_STATUSES
        for result in verdict["backend_results"].values()
    )
    assert verdict["llm_invoked"] is False
    assert verdict["approval_created"] is False
    assert verdict["dispatch_authority_created"] is False
    assert verdict["execution_invoked"] is False
    assert verdict["completion_claimed"] is False


def test_stable_gate_rejects_incomplete_same_task_surfaces(
    tmp_path: Path,
) -> None:
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    evidence["backends"]["nav2"]["operator_surfaces"].remove("job-status")
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")

    verdict = evaluate_stable_gate(evidence_path=path)

    assert verdict["status"] == "blocked"
    assert "nav2_operator_surfaces_incomplete" in verdict[
        "blocking_reasons"
    ]


def test_stable_gate_rejects_raw_task_id_in_release_evidence(
    tmp_path: Path,
) -> None:
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    evidence["backends"]["px4"]["unsafe"] = "task_deadbeefcafebabe"
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")

    verdict = evaluate_stable_gate(evidence_path=path)

    assert verdict["status"] == "blocked"
    assert "stable_runtime_evidence_contains_raw_task_id" in verdict[
        "blocking_reasons"
    ]


def test_stable_gate_rejects_missing_source_evidence(
    tmp_path: Path,
) -> None:
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    evidence["backends"]["nav2"]["source_evidence_ref"] = (
        "docs/agents/evidence/missing.md"
    )
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")

    verdict = evaluate_stable_gate(evidence_path=path)

    assert verdict["status"] == "blocked"
    assert "nav2_source_evidence_ref_invalid" in verdict[
        "blocking_reasons"
    ]
