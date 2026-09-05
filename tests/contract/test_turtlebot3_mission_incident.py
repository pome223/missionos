import json
from copy import deepcopy

import pytest

from src.intelligence.mission_assurance_agent import MissionAssuranceAgent, ModelJudgment
from src.runtime.turtlebot3_mission_incident import (
    continue_turtlebot3_incident,
    judge_turtlebot3_checkpoint,
    turtlebot3_incident_dispatch_reasons,
)


class Judge:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def judge(self, prompt):
        self.calls += 1
        return ModelJudgment(
            output={
                "proposed_response_kind": self.response,
                "parameters": {},
                "rationale": "Bounded fixture mission judgment.",
                "expected_outcome": "Preserve route objective.",
                "uncertainty": "Fixture only.",
                "operator_question": "Review candidate.",
            },
            invocation_evidence={"invocation_kind": "fixture", "model_id": "fixture"},
        )


def input_case():
    checkpoint = {
        "proposal_id": "mission-fixture",
        "recovery_proposal_id": "recovery-fixture",
        "selected_action": "avoid_obstacle",
        "approved_parameters": {"target_x_m": 2.0, "target_y_m": 1.0},
        "checkpoint_id": "checkpoint-fixture",
        "completed_segment_count": 1,
        "planned_segments_sha256": "route-fixture",
        "resume_state_hash": "resume-fixture",
    }
    return dict(
        checkpoint=checkpoint,
        proposal={"operator_instruction": "Deliver via safe bypass"},
        recovery_proposal={"proposal_source": "llm", "selected_action": "avoid_obstacle"},
        planner_result={
            "planner_status": "proposal_guardrail_passed",
            "llm_invocation_evidence": {
                "provider": "google_adk_fixture",
                "invocation_kind": "fixture",
                "model_id": "fixture-recovery",
                "prompt_sha256": "p" * 64,
                "response_sha256": "r" * 64,
                "invocation_exit_code": 0,
            },
        },
        motion={"robot_motion_observed": True, "telemetry_window_ref": "window-fixture"},
        obstacle={
            "recovery_candidate_resolution": {
                "resolution_status": "validated",
                "dual_costmap_validated": True,
                "selected_candidate": {
                    "path_valid": True,
                    "core_action_feasibility_status": "verified_feasible",
                    "core_action_feasibility": {"artifact_id": "feasible-fixture"},
                },
            }
        },
    )


def bind_checkpoint_graph(checkpoint):
    """Model-only fixture for Gateway tests; keep the actual shared graph."""
    case = input_case()
    case["checkpoint"] = deepcopy(checkpoint)
    case["recovery_proposal"]["selected_action"] = checkpoint["selected_action"]
    response = "return" if checkpoint["selected_action"] == "return_home" else "replan"
    checkpoint["missionos_mission_incident_graph"] = json.loads(
        json.dumps(
            judge_turtlebot3_checkpoint(
                **case, mission_assurance_agent=MissionAssuranceAgent(Judge(response))
            )
        )
    )


@pytest.mark.parametrize(
    "response,accepted", [("replan", True), ("hold", False), ("operator_escalation", False)]
)
def test_same_nav2_candidate_is_accepted_or_suppressed_only_by_assurance(response, accepted):
    case = input_case()
    judge = Judge(response)
    graph = judge_turtlebot3_checkpoint(
        **case, mission_assurance_agent=MissionAssuranceAgent(judge)
    )
    case["checkpoint"]["missionos_mission_incident_graph"] = graph
    assert judge.calls == 1
    assert graph["recovery_proposed_action"] == "avoid_obstacle"
    assert graph["source_action_feasibility"]["feasibility_status"] == "verified_feasible"
    assert graph["operator_approval_required"] is accepted
    assert bool(turtlebot3_incident_dispatch_reasons(case["checkpoint"])) is not accepted
    assert graph["dispatch_request_sent"] is False
    assert graph["dispatch_prevented_by_mission_assurance"] is not accepted


def test_missing_inference_cannot_reach_assurance_or_approval():
    case = input_case()
    case["planner_result"]["llm_invocation_evidence"] = {}
    judge = Judge("replan")
    graph = judge_turtlebot3_checkpoint(
        **case, mission_assurance_agent=MissionAssuranceAgent(judge)
    )
    assert judge.calls == 0
    assert graph["decision_status"] == "operator_escalation"
    assert "nav2_recovery_llm_inference_required" in graph["blocking_reasons"]


def test_unverified_costmap_cannot_reach_assurance():
    case = input_case()
    case["obstacle"]["recovery_candidate_resolution"]["dual_costmap_validated"] = False
    judge = Judge("replan")
    graph = judge_turtlebot3_checkpoint(
        **case, mission_assurance_agent=MissionAssuranceAgent(judge)
    )
    assert judge.calls == 0
    assert graph["operator_approval_required"] is False


def test_changed_target_does_not_inherit_assurance():
    case = input_case()
    graph = judge_turtlebot3_checkpoint(
        **case, mission_assurance_agent=MissionAssuranceAgent(Judge("replan"))
    )
    checkpoint = deepcopy(case["checkpoint"])
    checkpoint["missionos_mission_incident_graph"] = graph
    checkpoint["approved_parameters"]["target_x_m"] += 0.5
    assert "nav2_assurance_compiled_candidate_changed" in turtlebot3_incident_dispatch_reasons(
        checkpoint
    )


def test_graphless_checkpoint_is_rejected_before_executor():
    called = []
    result = continue_turtlebot3_incident(
        checkpoint=input_case()["checkpoint"],
        approval={"explicit_recovery_dispatch_approval": True},
        validate=lambda: [],
        execute=lambda: called.append(True),
    )
    assert not called
    assert result["summary"]["recovery_dispatch_request_sent"] is False


def test_runtime_entrypoint_cannot_resume_a_legacy_graphless_checkpoint(monkeypatch):
    from src.runtime import turtlebot3_home_mission as runtime

    called = []
    monkeypatch.setattr(
        runtime, "_execute_turtlebot3_home_mission", lambda **_: called.append(True)
    )
    result = runtime.run_turtlebot3_home_mission_dispatch(
        proposal={"nav2_goal_pose": {"x_m": 2.0, "y_m": 1.0}},
        approval={},
        resume_execution={"turtlebot3_recovery_checkpoint": input_case()["checkpoint"]},
        recovery_operator_approval={"explicit_recovery_dispatch_approval": True},
    )
    assert called == []
    assert result["summary"]["status"] == "blocked"
    assert result["summary"]["recovery_dispatch_request_sent"] is False


def test_chat_review_displays_assurance_without_calling_it_approval():
    from missionos_cli.cli import _render_chat_recovery_review

    panel = _render_chat_recovery_review(
        {
            "mission_assurance": {
                "mission_assurance_response_kind": "replan",
                "mission_assurance_proposal": {
                    "rationale": "Bypass preserves the delivery objective."
                },
            },
            "checkpoint_approval_supported": True,
        }
    )
    text = str(panel.renderable)
    assert "MissionAssuranceAgent=replan" in text
    assert "Bypass preserves the delivery objective." in text
    assert "approve exact checkpoint" in text
    assert "dispatch_authority=False" in text


def test_approved_continuation_runs_once_and_projects_real_outcome():
    case = input_case()
    judge = Judge("replan")
    graph = judge_turtlebot3_checkpoint(
        **case, mission_assurance_agent=MissionAssuranceAgent(judge)
    )
    checkpoint = case["checkpoint"]
    checkpoint["missionos_mission_incident_graph"] = graph
    called = []

    def execute():
        called.append(True)
        return {
            "summary": {
                "recovery_dispatch_request_sent": True,
                "recovery_execution_permitted_by_operator_approval": True,
                "recovery_closed_loop_cycles": [
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "reobservation_sha256": "observed-fixture",
                        "outcome_verification": {
                            "verification_status": "verified",
                            "command_ack_observed": True,
                            "executor_effect_observed": True,
                            "recovery_success_verified": True,
                        },
                    }
                ],
            }
        }

    result = continue_turtlebot3_incident(
        checkpoint=checkpoint,
        approval={"explicit_recovery_dispatch_approval": True},
        validate=lambda: [],
        execute=execute,
    )
    continuation = result["missionos_mission_incident_continuation_graph"]
    assert len(called) == 1 and judge.calls == 1
    assert continuation["verifier_status"] == "verified"
    assert continuation["next_mission_situation_created"] is True
    assert continuation["recovery_agent_rerun"] is False
    assert continuation["mission_assurance_agent_rerun"] is False
