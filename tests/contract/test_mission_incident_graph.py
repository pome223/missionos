from __future__ import annotations

from typing import Any

import pytest

from src.intelligence.mission_assurance_agent import (
    MissionAssuranceAgent,
    ModelJudgment,
)
from src.intelligence.missionos_mission_incident_graph import (
    MISSION_INCIDENT_GRAPH_WORKFLOW_NAME,
    run_missionos_mission_incident_graph,
)


class _Judge:
    def __init__(self, response_kind: str, *, expected_action: str = "avoid_obstacle") -> None:
        self.response_kind = response_kind
        self.expected_action = expected_action
        self.called = False
        self.prompt: dict[str, Any] = {}

    def judge(self, prompt: dict[str, Any]) -> ModelJudgment:
        self.called = True
        self.prompt = prompt
        assert prompt["mission_situation"]["observations"][
            "runtime_recovery_agent_result"
        ]["assessment"]["selected_bounded_action"] == self.expected_action
        return ModelJudgment(
            output={
                "proposed_response_kind": self.response_kind,
                "parameters": {},
                "rationale": "The mission-level response follows the evidence.",
                "expected_outcome": "The declared mission remains governed.",
                "uncertainty": "Bounded fixture judgment.",
                "operator_question": "Approve the bounded Recovery proposal?",
            },
            invocation_evidence={
                "invocation_kind": "fixture",
                "model_id": "fixture-mission-assurance",
            },
        )


def _recovery_result(
    *,
    feasible: bool = True,
    action: str = "avoid_obstacle",
) -> dict[str, Any]:
    return {
        "schema_version": "missionos_runtime_recovery_agent_result.v1",
        "runtime_status": "proposal_guardrail_passed",
        "blocking_reasons": [],
        "assessment": {
            "selected_bounded_action": action,
            "proposed_parameters": {
                "target_x_m": 40.0,
                "target_y_m": 10.0,
            },
            "action_feasibility": {
                "action": action,
                "feasibility_status": (
                    "verified_feasible" if feasible else "blocked"
                ),
            },
        },
        "agent_invocations": [
            {
                "agent_name": "missionos_runtime_recovery_agent",
                "model_id": "fixture-recovery",
            }
        ],
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _run(
    response_kind: str,
    *,
    feasible: bool = True,
    recovery_action: str = "avoid_obstacle",
):
    judge = _Judge(response_kind, expected_action=recovery_action)

    def recovery_runner(**_: Any) -> dict[str, Any]:
        return _recovery_result(feasible=feasible, action=recovery_action)

    result = run_missionos_mission_incident_graph(
        telemetry_snapshot={
            "observed_at": "2026-09-03T00:00:00+00:00",
            "sample_index": 25,
            "obstacle": {"local_avoidance_required": True},
        },
        mission_context={
            "task_id": "task_fixture",
            "mission_phase": "safety_hold",
            "execution_scope": "simulator",
        },
        recovery_policy={"policy_ref": "fixture-policy"},
        recovery_runner=recovery_runner,
        mission_assurance_agent=MissionAssuranceAgent(judge),
    )
    return result, judge


def test_one_graph_accepts_feasible_obstacle_recovery_for_human_checkpoint() -> None:
    result, judge = _run("replan")

    assert judge.called is True
    assert result["workflow_name"] == MISSION_INCIDENT_GRAPH_WORKFLOW_NAME
    assert result["graph_runtime_status"] == "proposal_guardrail_passed"
    assert result["decision_status"] == "awaiting_operator_approval"
    assert result["alignment_status"] == "accepted"
    assert result["recovery_proposed_action"] == "avoid_obstacle"
    assert result["mission_assurance_response_kind"] == "replan"
    assert result["recovery_agent_invoked_before_mission_assurance"] is True
    assert result["operator_approval_required"] is True
    assert result["approval_created"] is False
    assert result["dispatch_authority_created"] is False
    assert result["executor_invoked"] is False
    assert result["verifier_status"] == "not_started"
    assert "downstream_required_sequence" not in result
    assert result["declared_next_sequence"] == [
        "operator_recovery_approval_boundary",
        "dispatch_time_action_feasibility_revalidation",
        "executor",
        "verifier",
        "next_mission_situation",
    ]
    assert any("invoke_runtime_recovery_agent" in path for path in result["workflow_node_paths"])
    assert any("invoke_mission_assurance_agent" in path for path in result["workflow_node_paths"])


def test_one_graph_attributes_feasible_recovery_suppression_to_assurance() -> None:
    result, judge = _run("hold")

    assert judge.called is True
    assert result["decision_status"] == "no_dispatch"
    assert result["alignment_status"] == "suppressed_by_mission_assurance"
    assert result["dispatch_prevented_by_mission_assurance"] is True
    assert result["suppression_source"] == "mission_assurance_agent"
    assert result["dispatch_request_sent"] is False


@pytest.mark.parametrize("response_kind", ["hold", "operator_escalation"])
def test_one_graph_records_assurance_preventing_recovery_continue(
    response_kind: str,
) -> None:
    result, judge = _run(response_kind, recovery_action="continue")

    assert judge.called is True
    assert result["decision_status"] == "no_dispatch"
    assert result["alignment_status"] == (
        "mission_continuation_suppressed_by_mission_assurance"
    )
    assert result["mission_continuation_prevented_by_mission_assurance"] is True
    assert result["dispatch_prevented_by_mission_assurance"] is False
    assert result["suppression_source"] == "mission_assurance_agent"
    assert result["dispatch_request_sent"] is False


def test_one_graph_keeps_reverse_no_action_disagreement_for_operator() -> None:
    result, judge = _run("continue", recovery_action="hold")

    assert judge.called is True
    assert result["decision_status"] == "operator_escalation"
    assert result["alignment_status"] == "agent_disagreement"
    assert result["mission_continuation_prevented_by_mission_assurance"] is False
    assert result["dispatch_prevented_by_mission_assurance"] is False
    assert result["suppression_source"] is None


def test_one_graph_fails_closed_before_assurance_when_source_rules_block() -> None:
    result, judge = _run("replan", feasible=False)

    assert judge.called is False
    assert result["graph_runtime_status"] == "guardrail_blocked"
    assert result["decision_status"] == "operator_escalation"
    assert result["mission_assurance_agent_invoked"] is False
    assert result["dispatch_authority_created"] is False
    assert result["blocking_reasons"] == [
        "source_action_feasibility_not_verified_for_recovery_action"
    ]


def test_one_graph_excludes_abort_for_non_land_recovery() -> None:
    result, judge = _run("replan")

    assert result["decision_status"] == "awaiting_operator_approval"
    assert judge.prompt["mission_situation"]["allowed_response_kinds"] == (
        "continue",
        "hold",
        "operator_escalation",
        "replan",
        "return",
    )
    assert "abort" not in judge.prompt["response_semantics"]


def test_one_graph_keeps_unsupported_action_guardrail_blocked() -> None:
    judge = _Judge("replan", expected_action="teleport")
    recovery_result = _recovery_result()
    recovery_result["assessment"]["selected_bounded_action"] = "teleport"
    recovery_result["assessment"]["action_feasibility"] = {
        "action": "teleport",
        "feasibility_status": "verified_feasible",
    }

    result = run_missionos_mission_incident_graph(
        telemetry_snapshot={"observed_at": "2026-09-03T00:00:00+00:00"},
        mission_context={"task_id": "task_unsupported"},
        recovery_policy={"policy_ref": "fixture-policy"},
        recovery_runner=lambda **_: recovery_result,
        mission_assurance_agent=MissionAssuranceAgent(judge),
    )

    assert judge.called is True
    assert result["graph_runtime_status"] == "guardrail_blocked"
    assert result["decision_status"] == "operator_escalation"
    assert result["alignment_status"] == "unsupported_recovery_action"
    assert result["blocking_reasons"] == [
        "recovery_action_has_no_mission_response_alignment"
    ]


def test_one_graph_rejects_unbound_deterministic_recovery_before_assurance() -> None:
    judge = _Judge("replan")
    recovery_result = _recovery_result()
    recovery_result["agent_invocations"] = []

    result = run_missionos_mission_incident_graph(
        telemetry_snapshot={"observed_at": "2026-09-04T00:00:00+00:00"},
        mission_context={"task_id": "task_unbound_recovery"},
        recovery_policy={"policy_ref": "fixture-policy"},
        recovery_runner=lambda **_: recovery_result,
        mission_assurance_agent=MissionAssuranceAgent(judge),
    )

    assert judge.called is False
    assert result["graph_runtime_status"] == "guardrail_blocked"
    assert result["decision_status"] == "operator_escalation"
    assert result["recovery_agent_invoked"] is False
    assert result["recovery_judgment_inherited"] is False


def test_one_graph_rejudges_assurance_for_bound_recompiled_recovery() -> None:
    source_graph, _ = _run("replan")
    judge = _Judge("replan")
    recovery_result = _recovery_result()
    recovery_result["agent_invocations"] = [
        {
            "agent_name": "missionos_runtime_recovery_agent_fallback",
            "provider": "deterministic",
            "invocation_kind": "deterministic_guardrail_fallback",
        }
    ]

    result = run_missionos_mission_incident_graph(
        telemetry_snapshot={
            "observed_at": "2026-09-04T00:01:00+00:00",
            "sample_index": 26,
        },
        mission_context={
            "task_id": "task_bound_recompile",
            "recovery_judgment_binding": {
                "binding_mode": "deterministic_recompile_of_prior_judgment",
                "source_proposal_id": "runtime_recovery_proposal_source",
                "source_mission_incident_graph": source_graph,
            },
        },
        recovery_policy={"policy_ref": "fixture-policy"},
        recovery_runner=lambda **_: recovery_result,
        mission_assurance_agent=MissionAssuranceAgent(judge),
    )

    assert judge.called is True
    assert result["graph_runtime_status"] == "proposal_guardrail_passed"
    assert result["decision_status"] == "awaiting_operator_approval"
    assert result["recovery_agent_invoked"] is False
    assert result["recovery_judgment_inherited"] is True
    assert result["recovery_judgment_available_before_mission_assurance"] is True
    assert result["mission_assurance_agent_invoked"] is True
    assert result["recovery_judgment_binding"]["binding_status"] == "verified"
    assert result["mission_situation"]["observed_at"] == (
        "2026-09-04T00:01:00+00:00"
    )
