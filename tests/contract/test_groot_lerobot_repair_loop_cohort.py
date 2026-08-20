from __future__ import annotations

import json
from pathlib import Path

import pytest
from missionos_core import canonical_sha256

from src.runtime.groot_lerobot_repair_loop_cohort import build_repair_loop_cohort


def _loop(
    loop_id: str,
    *,
    status: str,
    attempts: int = 1,
    max_attempts: int = 2,
    success: bool = False,
    preservation_breach: bool = False,
) -> dict:
    return {
        "repair_loop": {"repair_loop_id": loop_id, "max_attempts": max_attempts},
        "status": status,
        "attempts": [
            {
                "result": {
                    "status": "satisfied" if success else "budget_exhausted_without_improvement",
                    "preservation_violation_observed": False,
                    "preservation_invariant_breach_observed": preservation_breach,
                }
            }
            for _ in range(attempts)
        ],
        "semantic_repair_established": success,
        "automatic_retry_performed": False,
        "physical_execution_invoked": False,
    }


def test_cohort_counts_loops_not_attempts() -> None:
    report = build_repair_loop_cohort(
        [
            _loop("loop-0", status="satisfied", attempts=2, success=True),
            _loop("loop-1", status="loop_budget_exhausted_without_improvement", attempts=2),
            _loop("loop-2", status="satisfied", attempts=1, success=True),
            _loop("loop-3", status="stopped_without_next_human_approval"),
            _loop("loop-4", status="satisfied", attempts=2, success=True),
        ]
    )

    assert report["cohort_status"] == "cohort_complete"
    assert report["completed_loop_count"] == 5
    assert report["successful_loop_count"] == 3
    assert report["primary_measurement"] == "3/5 loops"
    assert report["attempt_count_total"] == 8
    assert report["attempts_are_independent_observations"] is False


def test_single_attempt_cohort_reports_zero_of_five_only_when_complete() -> None:
    loops = [
        _loop(
            f"loop-{index}",
            status="loop_budget_exhausted_without_improvement",
            max_attempts=1,
        )
        for index in range(5)
    ]

    in_progress = build_repair_loop_cohort(
        loops[:4], planned_loop_count=5, max_attempts_per_loop=1
    )
    complete = build_repair_loop_cohort(
        loops, planned_loop_count=5, max_attempts_per_loop=1
    )

    assert in_progress["primary_measurement"] is None
    assert complete["primary_measurement"] == "0/5 loops"
    assert complete["attempt_count_total"] == 5
    assert complete["preservation_violation_loop_count"] == 0


def test_published_native_cohort_result_digest_and_boundary() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "docs/agents/evidence"
        / "20260820-groot-n17-lerobot-native-single-attempt-cohort-result.json"
    )
    published = json.loads(path.read_text(encoding="utf-8"))
    stated_digest = published.pop("result_sha256")

    assert canonical_sha256(published) == stated_digest
    assert published["cohort_status"] == "cohort_complete"
    assert published["primary_measurement"] == "0/5 loops"
    assert published["preservation_violation_loop_count"] == 0
    assert published["attempt_count_total"] == 5
    assert published["automatic_retry_performed"] is False
    assert published["physical_execution_invoked"] is False


def test_cohort_rejects_duplicate_loop_ids() -> None:
    with pytest.raises(ValueError, match="loop_id_not_unique"):
        build_repair_loop_cohort(
            [_loop("loop-0", status="satisfied"), _loop("loop-0", status="satisfied")]
        )


def test_cohort_rejects_automatic_retry_claim() -> None:
    artifact = _loop("loop-0", status="satisfied", success=True)
    artifact["automatic_retry_performed"] = True
    with pytest.raises(ValueError, match="automatic_retry_forbidden"):
        build_repair_loop_cohort([artifact])


def test_cohort_counts_preservation_invariant_breach() -> None:
    report = build_repair_loop_cohort(
        [
            _loop(
                "loop-0",
                status="stopped_on_preservation_invariant",
                preservation_breach=True,
            )
        ]
    )

    assert report["preservation_violation_loop_count"] == 1


def test_cohort_rejects_more_than_two_attempts() -> None:
    with pytest.raises(ValueError, match="attempt_count_exceeded"):
        build_repair_loop_cohort(
            [_loop("loop-0", status="loop_budget_exhausted_without_improvement", attempts=3)]
        )
