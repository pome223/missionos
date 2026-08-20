from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from missionos_core import canonical_sha256

from src.runtime.groot_lerobot_repair_loop_cohort import (
    LOOP_RESULT_SCHEMA_VERSION,
    build_repair_loop_cohort,
)


def _loop(
    loop_id: str,
    *,
    status: str,
    attempts: int = 1,
    max_attempts: int = 2,
    success: bool = False,
    preservation_breach: bool = False,
) -> dict:
    if status == "satisfied":
        result_status = "satisfied"
    elif status in {
        "stopped_on_preservation_invariant",
        "stopped_on_preservation_violation",
    }:
        result_status = status
    else:
        result_status = "budget_exhausted_without_improvement"
    return {
        "schema_version": LOOP_RESULT_SCHEMA_VERSION,
        "repair_loop": {"repair_loop_id": loop_id, "max_attempts": max_attempts},
        "status": status,
        "attempts": [
            {
                "attempt_index": attempt_index,
                "result": {
                    "status": (
                        result_status
                        if attempt_index == attempts - 1
                        else "budget_exhausted_without_improvement"
                    ),
                    "preservation_violation_observed": False,
                    "preservation_invariant_breach_observed": (
                        preservation_breach if attempt_index == attempts - 1 else False
                    ),
                }
            }
            for attempt_index in range(attempts)
        ],
        "attempt_count": attempts,
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


def test_publication_companion_derives_combined_direction_and_digests() -> None:
    evidence_root = Path(__file__).resolve().parents[2] / "docs/agents/evidence"
    result_path = (
        evidence_root
        / "20260820-groot-n17-lerobot-native-single-attempt-cohort-result.json"
    )
    publication = json.loads(
        (
            evidence_root
            / "20260820-groot-n17-lerobot-native-single-attempt-cohort-publication.json"
        ).read_text(encoding="utf-8")
    )
    previous = json.loads(
        (
            evidence_root
            / "20260817-groot-n17-lerobot-natural-repair-diagnostic-replay.json"
        ).read_text(encoding="utf-8")
    )
    published_result = json.loads(result_path.read_text(encoding="utf-8"))

    cohort_result = publication["cohort_result"]
    assert sha256(result_path.read_bytes()).hexdigest() == cohort_result["source_record_sha256"]
    stated_result_digest = published_result.pop("result_sha256")
    assert canonical_sha256(published_result) == stated_result_digest
    assert stated_result_digest == cohort_result["result_sha256"]

    native_direction_counts = {0: 0, 1: 0}
    for loop in publication["native_loop_context"]:
        native_direction_counts[loop["target_predicate_index"]] += 1
    previous_vector = previous["candidate_specification_match"]["observed_source_vector"]
    previous_target_indices = [
        index for index, satisfied in enumerate(previous_vector) if satisfied is False
    ]
    assert previous_target_indices == [0]
    previous_attempt_count = previous["preservation_observation"]["attempt_count"]
    expected_direction_counts = {
        0: native_direction_counts[0] + previous_attempt_count,
        1: native_direction_counts[1],
    }
    breakdown = publication["combined_preservation_accounting"][
        "target_direction_breakdown"
    ]
    assert breakdown == {
        "predicate_0_target_execution_count": expected_direction_counts[0],
        "predicate_1_target_execution_count": expected_direction_counts[1],
    }

    combined = publication["combined_preservation_accounting"]
    assert previous_attempt_count + publication["cohort_result"]["attempt_count_total"] == 16
    assert sum(expected_direction_counts.values()) == combined["execution_count"] == 16
    assert combined["preserve_predicates_maintained_count"] == 16


def test_cohort_rejects_empty_attempts() -> None:
    artifact = _loop("loop-0", status="loop_budget_exhausted_without_improvement")
    artifact["attempts"] = []
    artifact["attempt_count"] = 0

    with pytest.raises(ValueError, match="attempts_empty"):
        build_repair_loop_cohort([artifact])


def test_cohort_rejects_missing_attempt_result() -> None:
    artifact = _loop("loop-0", status="loop_budget_exhausted_without_improvement")
    artifact["attempts"] = [{"attempt_index": 0}]

    with pytest.raises(TypeError, match="attempt_result_required"):
        build_repair_loop_cohort([artifact])


@pytest.mark.parametrize("value", [None, 0, "false"])
def test_cohort_rejects_non_boolean_semantic_repair(value: object) -> None:
    artifact = _loop("loop-0", status="loop_budget_exhausted_without_improvement")
    artifact["semantic_repair_established"] = value

    with pytest.raises(TypeError, match="semantic_repair_boolean_required"):
        build_repair_loop_cohort([artifact])


def test_cohort_rejects_missing_preservation_result() -> None:
    artifact = _loop("loop-0", status="loop_budget_exhausted_without_improvement")
    result = artifact["attempts"][0]["result"]
    result.pop("preservation_violation_observed")
    result.pop("preservation_invariant_breach_observed")

    with pytest.raises(ValueError, match="preservation_result_required"):
        build_repair_loop_cohort([artifact])


def test_cohort_rejects_non_boolean_preservation_result() -> None:
    artifact = _loop("loop-0", status="loop_budget_exhausted_without_improvement")
    artifact["attempts"][0]["result"]["preservation_invariant_breach_observed"] = None

    with pytest.raises(TypeError, match="preservation_boolean_required"):
        build_repair_loop_cohort([artifact])


@pytest.mark.parametrize(
    ("status", "success", "result_status"),
    [
        ("satisfied", False, "satisfied"),
        ("loop_budget_exhausted_without_improvement", True, "satisfied"),
        ("loop_budget_exhausted_without_improvement", False, "satisfied"),
    ],
)
def test_cohort_rejects_contradictory_terminal_status(
    status: str, success: bool, result_status: str
) -> None:
    artifact = _loop("loop-0", status=status, success=success)
    artifact["attempts"][-1]["result"]["status"] = result_status

    with pytest.raises(ValueError, match="status_inconsistent"):
        build_repair_loop_cohort([artifact])


def test_cohort_rejects_duplicate_loop_ids() -> None:
    with pytest.raises(ValueError, match="loop_id_not_unique"):
        build_repair_loop_cohort(
            [
                _loop("loop-0", status="satisfied", success=True),
                _loop("loop-0", status="satisfied", success=True),
            ]
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
