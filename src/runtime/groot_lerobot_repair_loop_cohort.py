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
LOOP_RESULT_SCHEMA_VERSION = "missionos_groot_lerobot_repair_loop_result.v1"
PRESERVATION_RESULT_FIELDS = (
    "preservation_violation_observed",
    "preservation_invariant_breach_observed",
)
LOOP_STATUS_TO_FINAL_ATTEMPT_STATUS = {
    "satisfied": "satisfied",
    "loop_budget_exhausted_without_improvement": "budget_exhausted_without_improvement",
    "stopped_without_next_human_approval": "budget_exhausted_without_improvement",
    "predicate_improved_without_full_completion": "budget_exhausted_without_improvement",
    "stopped_on_preservation_invariant": "stopped_on_preservation_invariant",
    "stopped_on_preservation_violation": "stopped_on_preservation_violation",
}
PRESERVATION_STOP_STATUSES = frozenset(
    {"stopped_on_preservation_invariant", "stopped_on_preservation_violation"}
)


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
        if raw.get("schema_version") != LOOP_RESULT_SCHEMA_VERSION:
            raise ValueError("repair_loop_cohort_loop_schema_mismatch")
        loop = raw.get("repair_loop")
        if not isinstance(loop, Mapping):
            raise TypeError("repair_loop_cohort_loop_policy_required")
        loop_id = str(loop.get("repair_loop_id") or "").strip()
        if not loop_id:
            raise ValueError("repair_loop_cohort_loop_id_required")
        if loop_id in loop_ids:
            raise ValueError("repair_loop_cohort_loop_id_not_unique")
        loop_ids.add(loop_id)
        configured_max_attempts = loop.get("max_attempts")
        if (
            isinstance(configured_max_attempts, bool)
            or not isinstance(configured_max_attempts, int)
            or configured_max_attempts != max_attempts_per_loop
        ):
            raise ValueError("repair_loop_cohort_max_attempts_mismatch")
        attempts = raw.get("attempts")
        if not isinstance(attempts, list):
            raise TypeError("repair_loop_cohort_attempts_required")
        if not attempts:
            raise ValueError("repair_loop_cohort_attempts_empty")
        if len(attempts) > max_attempts_per_loop:
            raise ValueError("repair_loop_cohort_attempt_count_exceeded")
        configured_attempt_count = raw.get("attempt_count")
        if (
            isinstance(configured_attempt_count, bool)
            or not isinstance(configured_attempt_count, int)
            or configured_attempt_count != len(attempts)
        ):
            raise ValueError("repair_loop_cohort_attempt_count_mismatch")
        if raw.get("automatic_retry_performed") is not False:
            raise ValueError("repair_loop_cohort_automatic_retry_forbidden")
        if raw.get("physical_execution_invoked") is not False:
            raise ValueError("repair_loop_cohort_physical_execution_forbidden")
        semantic_repair_established = raw.get("semantic_repair_established")
        if not isinstance(semantic_repair_established, bool):
            raise TypeError("repair_loop_cohort_semantic_repair_boolean_required")
        status = raw.get("status")
        if not isinstance(status, str) or not status:
            raise ValueError("repair_loop_cohort_loop_status_required")
        if status not in LOOP_STATUS_TO_FINAL_ATTEMPT_STATUS:
            raise ValueError("repair_loop_cohort_loop_status_invalid")

        attempt_statuses: list[str] = []
        attempt_preservation_violations: list[bool] = []
        for attempt_index, attempt in enumerate(attempts):
            if not isinstance(attempt, Mapping):
                raise TypeError("repair_loop_cohort_attempt_record_required")
            recorded_attempt_index = attempt.get("attempt_index")
            if (
                isinstance(recorded_attempt_index, bool)
                or not isinstance(recorded_attempt_index, int)
                or recorded_attempt_index != attempt_index
            ):
                raise ValueError("repair_loop_cohort_attempt_index_mismatch")
            result = attempt.get("result")
            if not isinstance(result, Mapping):
                raise TypeError("repair_loop_cohort_attempt_result_required")
            result_status = result.get("status")
            if not isinstance(result_status, str) or not result_status:
                raise ValueError("repair_loop_cohort_attempt_result_status_required")
            preservation_values: list[bool] = []
            for field in PRESERVATION_RESULT_FIELDS:
                if field not in result:
                    continue
                value = result[field]
                if not isinstance(value, bool):
                    raise TypeError("repair_loop_cohort_preservation_boolean_required")
                preservation_values.append(value)
            if not preservation_values:
                raise ValueError("repair_loop_cohort_preservation_result_required")
            attempt_statuses.append(result_status)
            attempt_preservation_violations.append(any(preservation_values))

        if any(
            attempt_status != "budget_exhausted_without_improvement"
            or attempt_preservation_violations[attempt_index]
            for attempt_index, attempt_status in enumerate(attempt_statuses[:-1])
        ):
            raise ValueError("repair_loop_cohort_intermediate_attempt_inconsistent")
        if attempt_statuses[-1] != LOOP_STATUS_TO_FINAL_ATTEMPT_STATUS[status]:
            raise ValueError("repair_loop_cohort_final_attempt_status_inconsistent")
        if semantic_repair_established is not (status == "satisfied"):
            raise ValueError("repair_loop_cohort_semantic_repair_status_inconsistent")
        preservation_violation_observed = any(attempt_preservation_violations)
        if (status in PRESERVATION_STOP_STATUSES) is not preservation_violation_observed:
            raise ValueError("repair_loop_cohort_preservation_status_inconsistent")
        normalized.append(
            {
                "cohort_index": index,
                "repair_loop_id": loop_id,
                "status": status,
                "attempt_count": len(attempts),
                "semantic_repair_established": semantic_repair_established,
                "preservation_violation_observed": preservation_violation_observed,
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
    "LOOP_RESULT_SCHEMA_VERSION",
    "build_repair_loop_cohort",
]
