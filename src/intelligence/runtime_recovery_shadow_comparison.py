"""Read-only contribution comparison for the Runtime Recovery planner stage."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = "missionos_runtime_recovery_shadow_comparison.v1"
COMPARISON_SCOPE = "runtime_recovery_planner_stage_only"
PLANNER_TOOL_NAME = "missionos_plan_bounded_recovery_maneuver"
_PARAMETER_KEYS = ("target_x_m", "target_y_m", "target_altitude_m")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _planner_tool_result(result: Mapping[str, Any]) -> dict[str, Any]:
    for invocation in _sequence(result.get("agent_invocations")):
        if invocation.get("agent_name") != "missionos_runtime_recovery_agent":
            continue
        for tool_result in _sequence(invocation.get("function_tool_results")):
            if tool_result.get("tool_name") == PLANNER_TOOL_NAME:
                return tool_result
    return {}


def _candidate_action(candidate: Mapping[str, Any]) -> str:
    return str(candidate.get("selected_bounded_action") or "")


def _candidate_parameters(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(candidate.get("proposed_parameters"))


def _parameters_match(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    tolerance: float = 0.05,
) -> bool:
    comparable = False
    for key in _PARAMETER_KEYS:
        left_number = _number(left.get(key))
        right_number = _number(right.get(key))
        if left_number is None and right_number is None:
            continue
        comparable = True
        if left_number is None or right_number is None:
            return False
        if abs(left_number - right_number) > tolerance:
            return False
    return comparable or dict(left) == dict(right)


def _parameter_delta(
    llm_parameters: Mapping[str, Any],
    baseline_parameters: Mapping[str, Any],
) -> dict[str, float | None]:
    delta: dict[str, float | None] = {}
    for key in _PARAMETER_KEYS:
        llm_value = _number(llm_parameters.get(key))
        baseline_value = _number(baseline_parameters.get(key))
        delta[key] = (
            round(llm_value - baseline_value, 6)
            if llm_value is not None and baseline_value is not None
            else None
        )
    return delta


def _base_result(*, task_id: str, proposal_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "comparison_status": "unavailable",
        "comparison_scope": COMPARISON_SCOPE,
        "comparison_method": "retained_function_tool_result_replay",
        "task_id": task_id,
        "proposal_id": proposal_id,
        "chief_stage_compared": False,
        "safety_critic_stage_compared": False,
        "candidate_set_size": None,
        "decision_difference_observable": None,
        "llm_changed_decision": None,
        "llm_selected_non_top_ranked": None,
        "selected_action_changed": None,
        "candidate_rank_changed": None,
        "parameter_delta": {key: None for key in _PARAMETER_KEYS},
        "parameter_delta_semantics": "llm_minus_deterministic_baseline",
        "deterministic_baseline_feasible": None,
        "counterfactual_execution_observed": False,
        "causal_outcome_effect": "unverified",
        "approval_created": False,
        "dispatch_authority_created": False,
        "progress_counted": False,
        "completion_claimed": False,
        "physical_execution_invoked": False,
    }


def build_runtime_recovery_shadow_comparison(
    *,
    task_id: str,
    proposal_id: str,
    runtime_recovery_agent_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare the LLM-selected planner candidate with the retained baseline.

    The FunctionTool result already contains the source-bound deterministic
    candidate ordering that the hosted Agent saw. Reading it avoids rebuilding
    a counterfactual from later telemetry. This function never invokes an
    executor or creates approval, dispatch, verifier, or completion facts.
    """

    comparison = _base_result(task_id=task_id, proposal_id=proposal_id)
    result = _mapping(runtime_recovery_agent_result)
    pipeline = _mapping(result.get("agent_pipeline"))
    if pipeline.get("decision_source") != "llm":
        comparison["blocking_reasons"] = ["hosted_llm_planner_result_required"]
        return comparison

    assessment = _mapping(result.get("assessment"))
    llm_action = str(assessment.get("selected_bounded_action") or "")
    llm_parameters = _mapping(assessment.get("proposed_parameters"))
    tool_result = _planner_tool_result(result)
    candidates = _sequence(tool_result.get("candidates"))
    if not llm_action or not tool_result:
        comparison["blocking_reasons"] = [
            "llm_assessment_or_retained_planner_tool_result_missing"
        ]
        return comparison
    if not candidates:
        comparison.update(
            {
                "candidate_set_size": 0,
                "decision_difference_observable": False,
                "deterministic_baseline_feasible": False,
                "blocking_reasons": ["deterministic_baseline_has_no_feasible_candidate"],
                "source": {
                    "planner_tool_result_sha256": _sha256_json(tool_result),
                    "assessment_sha256": _sha256_json(assessment),
                },
            }
        )
        return comparison

    comparison["candidate_set_size"] = len(candidates)
    if len(candidates) == 1:
        baseline = candidates[0]
        baseline_parameters = _candidate_parameters(baseline)
        selected_matches_only_candidate = (
            _candidate_action(baseline) == llm_action
            and _parameters_match(llm_parameters, baseline_parameters)
        )
        if not selected_matches_only_candidate:
            comparison.update(
                {
                    "comparison_status": "unavailable",
                    "decision_difference_observable": False,
                    "deterministic_baseline_feasible": True,
                    "blocking_reasons": [
                        "llm_selected_candidate_not_comparable_to_singleton_ordering"
                    ],
                    "source": {
                        "planner_tool_result_sha256": _sha256_json(tool_result),
                        "assessment_sha256": _sha256_json(assessment),
                    },
                }
            )
            return comparison
        comparison.update(
            {
                "comparison_status": "not_observable",
                "decision_difference_observable": False,
                "deterministic_baseline_feasible": True,
                "deterministic_rank_order": [_candidate_action(baseline)],
                "deterministic_baseline": {
                    "selected_bounded_action": _candidate_action(baseline),
                    "proposed_parameters": baseline_parameters,
                    "candidate_rank": 0,
                },
                "llm_decision": {
                    "selected_bounded_action": llm_action,
                    "proposed_parameters": llm_parameters,
                    "candidate_rank_in_deterministic_ordering": 0,
                },
                "blocking_reasons": [
                    "planner_candidate_set_has_no_selection_freedom"
                ],
                "source": {
                    "planner_tool_result_sha256": _sha256_json(tool_result),
                    "assessment_sha256": _sha256_json(assessment),
                    "telemetry_cursor": _mapping(
                        _mapping(tool_result.get("hazard_state")).get(
                            "telemetry_cursor"
                        )
                    ),
                    "policy_sha256": str(
                        _mapping(tool_result.get("hazard_state")).get(
                            "policy_sha256"
                        )
                        or ""
                    ),
                },
            }
        )
        comparison["comparison_sha256"] = _sha256_json(comparison)
        return comparison

    baseline = candidates[0]
    baseline_action = _candidate_action(baseline)
    baseline_parameters = _candidate_parameters(baseline)
    llm_rank: int | None = None
    for index, candidate in enumerate(candidates):
        if _candidate_action(candidate) != llm_action:
            continue
        if _parameters_match(
            llm_parameters,
            _candidate_parameters(candidate),
        ):
            llm_rank = index
            break
    if llm_rank is None:
        comparison.update(
            {
                "decision_difference_observable": True,
                "deterministic_baseline_feasible": True,
                "blocking_reasons": [
                    "llm_selected_candidate_not_comparable_to_retained_ordering"
                ],
                "source": {
                    "planner_tool_result_sha256": _sha256_json(tool_result),
                    "assessment_sha256": _sha256_json(assessment),
                },
            }
        )
        return comparison

    selected_action_changed = llm_action != baseline_action
    candidate_rank_changed = llm_rank != 0
    comparison.update(
        {
            "comparison_status": "computed",
            "decision_difference_observable": True,
            "llm_changed_decision": (
                selected_action_changed or candidate_rank_changed
            ),
            "llm_selected_non_top_ranked": candidate_rank_changed,
            "selected_action_changed": selected_action_changed,
            "candidate_rank_changed": candidate_rank_changed,
            "parameter_delta": _parameter_delta(
                llm_parameters,
                baseline_parameters,
            ),
            "deterministic_baseline_feasible": True,
            "deterministic_baseline": {
                "selected_bounded_action": baseline_action,
                "proposed_parameters": baseline_parameters,
                "candidate_rank": 0,
            },
            "llm_decision": {
                "selected_bounded_action": llm_action,
                "proposed_parameters": llm_parameters,
                "candidate_rank_in_deterministic_ordering": llm_rank,
            },
            "candidate_actions": [
                _candidate_action(candidate) for candidate in candidates
            ],
            "deterministic_rank_order": [
                _candidate_action(candidate) for candidate in candidates
            ],
            "blocking_reasons": [],
            "source": {
                "planner_tool_result_sha256": _sha256_json(tool_result),
                "assessment_sha256": _sha256_json(assessment),
                "telemetry_cursor": _mapping(
                    _mapping(tool_result.get("hazard_state")).get(
                        "telemetry_cursor"
                    )
                ),
                "policy_sha256": str(
                    _mapping(tool_result.get("hazard_state")).get(
                        "policy_sha256"
                    )
                    or ""
                ),
            },
        }
    )
    comparison["comparison_sha256"] = _sha256_json(comparison)
    return comparison


__all__ = [
    "COMPARISON_SCOPE",
    "SCHEMA_VERSION",
    "build_runtime_recovery_shadow_comparison",
]
