"""Cohort accounting for governed LeRobot Repair loops.

The experimental unit is one naturally observed failure world and its bounded
human-approved loop. Individual attempts are retained as secondary evidence;
they must not be counted as independent observations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from missionos_core import canonical_sha256

DEFAULT_PLANNED_LOOP_COUNT = 5
DEFAULT_MAX_ATTEMPTS_PER_LOOP = 2


def build_repair_loop_cohort(
    loop_results: Sequence[Mapping[str, Any]],
    *,
    planned_loop_count: int = DEFAULT_PLANNED_LOOP_COUNT,
    max_attempts_per_loop: int = DEFAULT_MAX_ATTEMPTS_PER_LOOP,
) -> dict[str, Any]:
    """Summarize loop artifacts without treating attempts as observations."""

    if isinstance(planned_loop_count, bool) or planned_loop_count <= 0:
        raise ValueError("repair_loop_cohort_planned_loop_count_invalid")
    if isinstance(max_attempts_per_loop, bool) or max_attempts_per_loop <= 0:
        raise ValueError("repair_loop_cohort_max_attempts_invalid")
    if len(loop_results) > planned_loop_count:
        raise ValueError("repair_loop_cohort_contains_too_many_loops")

    normalized: list[dict[str, Any]] = []
    loop_ids: set[str] = set()
    for index, raw in enumerate(loop_results):
        if not isinstance(raw, Mapping):
            raise TypeError("repair_loop_cohort_loop_result_required")
        loop = raw.get("repair_loop")
        if not isinstance(loop, Mapping):
            raise TypeError("repair_loop_cohort_loop_policy_required")
        loop_id = str(loop.get("repair_loop_id") or "").strip()
        if not loop_id:
            raise ValueError("repair_loop_cohort_loop_id_required")
        if loop_id in loop_ids:
            raise ValueError("repair_loop_cohort_loop_id_not_unique")
        loop_ids.add(loop_id)
        if loop.get("max_attempts") != max_attempts_per_loop:
            raise ValueError("repair_loop_cohort_max_attempts_mismatch")
        attempts = raw.get("attempts")
        if not isinstance(attempts, list):
            raise TypeError("repair_loop_cohort_attempts_required")
        if len(attempts) > max_attempts_per_loop:
            raise ValueError("repair_loop_cohort_attempt_count_exceeded")
        if raw.get("automatic_retry_performed") is not False:
            raise ValueError("repair_loop_cohort_automatic_retry_forbidden")
        if raw.get("physical_execution_invoked") is not False:
            raise ValueError("repair_loop_cohort_physical_execution_forbidden")
        normalized.append(
            {
                "cohort_index": index,
                "repair_loop_id": loop_id,
                "status": raw.get("status"),
                "attempt_count": len(attempts),
                "semantic_repair_established": raw.get("semantic_repair_established") is True,
                "preservation_violation_observed": any(
                    isinstance(attempt, Mapping)
                    and isinstance(attempt.get("result"), Mapping)
                    and (
                        attempt["result"].get("preservation_violation_observed") is True
                        or attempt["result"].get("preservation_invariant_breach_observed") is True
                        or str(attempt["result"].get("status") or "")
                        in {
                            "stopped_on_preservation_invariant",
                            "stopped_on_preservation_violation",
                        }
                    )
                    for attempt in attempts
                ),
            }
        )

    successful = sum(item["semantic_repair_established"] for item in normalized)
    preservation_violations = sum(
        item["preservation_violation_observed"] for item in normalized
    )
    completed = len(normalized)
    cohort_status = "cohort_complete" if completed == planned_loop_count else "cohort_in_progress"
    primary_measurement = (
        f"{successful}/{planned_loop_count} loops" if cohort_status == "cohort_complete" else None
    )
    report_without_digest = {
        "schema_version": "missionos_groot_lerobot_repair_loop_cohort.v1",
        "cohort_status": cohort_status,
        "planned_loop_count": planned_loop_count,
        "completed_loop_count": completed,
        "successful_loop_count": successful,
        "primary_measurement": primary_measurement,
        "attempt_count_total": sum(item["attempt_count"] for item in normalized),
        "preservation_violation_loop_count": preservation_violations,
        "loops": deepcopy(normalized),
        "independence_claimed": False,
        "attempts_are_independent_observations": False,
        "automatic_retry_performed": False,
        "physical_execution_invoked": False,
    }
    return {
        **report_without_digest,
        "result_sha256": canonical_sha256(report_without_digest),
    }


__all__ = [
    "DEFAULT_MAX_ATTEMPTS_PER_LOOP",
    "DEFAULT_PLANNED_LOOP_COUNT",
    "build_repair_loop_cohort",
]
