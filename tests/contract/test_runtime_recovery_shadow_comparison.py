from __future__ import annotations

from typing import Any

from src.intelligence.runtime_recovery_shadow_comparison import (
    build_runtime_recovery_shadow_comparison,
)


def _candidate(action: str, *, x: float, y: float, altitude: float) -> dict[str, Any]:
    return {
        "selected_bounded_action": action,
        "proposed_parameters": {
            "target_x_m": x,
            "target_y_m": y,
            "target_altitude_m": altitude,
        },
    }


def _agent_result(
    *,
    selected: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "runtime_status": "proposal_guardrail_passed",
        "assessment": {
            "selected_bounded_action": selected["selected_bounded_action"],
            "proposed_parameters": selected["proposed_parameters"],
        },
        "agent_pipeline": {"decision_source": "llm"},
        "agent_invocations": [
            {
                "agent_name": "missionos_runtime_recovery_agent",
                "function_tool_results": [
                    {
                        "tool_name": "missionos_plan_bounded_recovery_maneuver",
                        "tool_status": "computed",
                        "candidates": candidates,
                        "hazard_state": {
                            "telemetry_cursor": {
                                "sample_index": 53,
                                "elapsed_seconds": 65.171,
                            },
                            "policy_sha256": "policy-sha",
                        },
                    }
                ],
            }
        ],
    }


def _assert_no_authority(comparison: dict[str, Any]) -> None:
    assert comparison["approval_created"] is False
    assert comparison["dispatch_authority_created"] is False
    assert comparison["progress_counted"] is False
    assert comparison["completion_claimed"] is False
    assert comparison["physical_execution_invoked"] is False
    assert comparison["counterfactual_execution_observed"] is False
    assert comparison["causal_outcome_effect"] == "unverified"
    assert comparison["chief_stage_compared"] is False
    assert comparison["safety_critic_stage_compared"] is False


def test_shadow_comparison_marks_single_candidate_decision_unobservable() -> None:
    baseline = _candidate("avoid_obstacle", x=-48.0, y=300.0, altitude=45.0)

    comparison = build_runtime_recovery_shadow_comparison(
        task_id="task_same",
        proposal_id="proposal_same",
        runtime_recovery_agent_result=_agent_result(
            selected=baseline,
            candidates=[baseline],
        ),
    )

    assert comparison["comparison_status"] == "not_observable"
    assert comparison["comparison_scope"] == "runtime_recovery_planner_stage_only"
    assert comparison["candidate_set_size"] == 1
    assert comparison["decision_difference_observable"] is False
    assert comparison["llm_changed_decision"] is None
    assert comparison["llm_selected_non_top_ranked"] is None
    assert comparison["selected_action_changed"] is None
    assert comparison["candidate_rank_changed"] is None
    assert comparison["deterministic_baseline_feasible"] is True
    assert comparison["parameter_delta"] == {
        "target_x_m": None,
        "target_y_m": None,
        "target_altitude_m": None,
    }
    assert comparison["blocking_reasons"] == [
        "planner_candidate_set_has_no_selection_freedom"
    ]
    _assert_no_authority(comparison)


def test_shadow_comparison_records_changed_action_and_rank() -> None:
    baseline = _candidate("avoid_obstacle", x=-48.0, y=300.0, altitude=45.0)
    selected = _candidate("reroute", x=20.0, y=320.0, altitude=40.0)

    comparison = build_runtime_recovery_shadow_comparison(
        task_id="task_action_changed",
        proposal_id="proposal_action_changed",
        runtime_recovery_agent_result=_agent_result(
            selected=selected,
            candidates=[baseline, selected],
        ),
    )

    assert comparison["comparison_status"] == "computed"
    assert comparison["candidate_set_size"] == 2
    assert comparison["decision_difference_observable"] is True
    assert comparison["llm_changed_decision"] is True
    assert comparison["llm_selected_non_top_ranked"] is True
    assert comparison["selected_action_changed"] is True
    assert comparison["candidate_rank_changed"] is True
    assert comparison["llm_decision"]["candidate_rank_in_deterministic_ordering"] == 1
    _assert_no_authority(comparison)


def test_shadow_comparison_records_changed_rank_with_same_action() -> None:
    baseline = _candidate("avoid_obstacle", x=-48.0, y=300.0, altitude=45.0)
    selected = _candidate("avoid_obstacle", x=48.0, y=300.0, altitude=45.0)

    comparison = build_runtime_recovery_shadow_comparison(
        task_id="task_rank_changed",
        proposal_id="proposal_rank_changed",
        runtime_recovery_agent_result=_agent_result(
            selected=selected,
            candidates=[baseline, selected],
        ),
    )

    assert comparison["llm_changed_decision"] is True
    assert comparison["llm_selected_non_top_ranked"] is True
    assert comparison["selected_action_changed"] is False
    assert comparison["candidate_rank_changed"] is True
    assert comparison["parameter_delta"]["target_x_m"] == 96.0
    _assert_no_authority(comparison)


def test_shadow_comparison_keeps_missing_evidence_unavailable() -> None:
    comparison = build_runtime_recovery_shadow_comparison(
        task_id="task_unavailable",
        proposal_id="proposal_unavailable",
        runtime_recovery_agent_result={
            "assessment": {
                "selected_bounded_action": "avoid_obstacle",
                "proposed_parameters": {},
            },
            "agent_pipeline": {"decision_source": "llm"},
            "agent_invocations": [],
        },
    )

    assert comparison["comparison_status"] == "unavailable"
    assert comparison["candidate_set_size"] is None
    assert comparison["decision_difference_observable"] is None
    assert comparison["llm_changed_decision"] is None
    assert comparison["selected_action_changed"] is None
    assert comparison["candidate_rank_changed"] is None
    assert comparison["deterministic_baseline_feasible"] is None
    assert comparison["parameter_delta"] == {
        "target_x_m": None,
        "target_y_m": None,
        "target_altitude_m": None,
    }
    _assert_no_authority(comparison)
