from __future__ import annotations

from copy import deepcopy

from src.gateway.mission_assurance_px4_e2e import (
    build_mission_assurance_gateway_px4_e2e,
)


def _approval() -> dict:
    return {
        "approval_id": "approval_e2e",
        "operator_approved": True,
        "approval_status": "consumed_in_runtime",
        "consumed_in_runtime": True,
        "mission_assurance_on_deviation_approved": True,
        "missionos_client_surface": "missionos_cli",
    }


def _summary() -> dict:
    return {
        "mission_designer_task_id": "task_e2e",
        "same_gateway_execution_run_observed": True,
        "actual_px4_gazebo_horizontal_smoke_observed": True,
        "actual_sitl_flight_evidence_observed": True,
        "decision_loop_driver": "runtime_recovery_agent_then_mission_assurance_agent",
        "runtime_recovery_agent_invoked": True,
        "mission_assurance_agent_invoked": True,
        "recovery_command_ack_observed": False,
        "recovery_state_observed": False,
        "recovery_state_label": None,
        "final_status": "aborted_pose_deviation",
        "task_status": "blocked",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "artifact_dir": "contract-artifact-dir",
        "mission_assurance_live_guard": {
            "guard_status": "awaiting_operator_approval",
            "selected_recovery_action": None,
            "proposed_recovery_action": "rtl",
            "operator_recovery_approval_request": {
                "request_status": "awaiting_operator_approval",
                "recovery_action": "rtl",
                "requires_new_human_approval": True,
                "route_execution_approval_is_not_recovery_approval": True,
            },
            "approval_recorded": False,
            "dispatch_authority_created": False,
            "dispatch_request_sent": False,
            "physical_execution_invoked": False,
            "runtime_recovery_agent_invoked": True,
            "recovery_agent_invoked_before_mission_assurance": True,
            "decision_sequence": [
                "missionos_runtime_recovery_agent",
                "mission_assurance_agent",
                "action_feasibility_revalidation",
                "fresh_operator_recovery_approval_boundary",
            ],
            "runtime_recovery_agent_proposal": {
                "proposal_ref": "runtime_recovery_agent_proposal:e2e",
                "runtime_status": "proposal_guardrail_passed",
                "selected_bounded_action": "return_to_launch",
                "model_inference_invoked": True,
                "approval_recorded": False,
                "dispatch_authority_created": False,
                "dispatch_request_sent": False,
                "physical_execution_invoked": False,
                "model_invocation_evidence": {
                    "model_id": "ollama_chat/llama3:latest"
                },
            },
            "mission_assurance_evaluation": {
                "proposal": {
                    "proposal_id": "proposal_e2e",
                    "proposed_response_kind": "return",
                    "judgment_status": "proposal_guardrail_passed",
                    "model_inference_invoked": True,
                    "approval_recorded": False,
                    "dispatch_authority_created": False,
                    "dispatch_request_sent": False,
                    "physical_execution_invoked": False,
                    "model_invocation_evidence": {
                        "model_id": "ollama_chat/llama3:latest"
                    },
                }
            },
            "original_action_feasibility": {
                "feasibility_status": "verified_feasible"
            },
            "current_action_feasibility": {},
            "action_revalidation": {},
        },
    }


def test_complete_gateway_cli_agent_px4_loop_is_observed_without_ack_claim() -> None:
    result = build_mission_assurance_gateway_px4_e2e(
        task_id="task_e2e",
        client_surface="missionos_cli",
        mission_assurance_requested=True,
        execution_approval=_approval(),
        live_summary=_summary(),
    )

    assert result["full_gateway_runtime_loop"] is True
    assert result["e2e_status"] == "completed"
    assert result["blocking_reasons"] == []
    assert result["model_inference_invoked"] is True
    assert result["runtime_recovery_agent_invoked"] is True
    assert result["recovery_agent_invoked_before_mission_assurance"] is True
    assert result["recovery_proposed_action"] == "return_to_launch"
    assert result["e2e_disposition"] == "return_awaiting_operator_approval"
    assert result["human_execution_approval_consumed"] is True
    assert result["simulator_execution_observed"] is True
    assert result["command_ack_observed"] is False
    assert result["runtime_state_observed"] is False
    assert result["agent_created_approval"] is False
    assert result["agent_created_dispatch_authority"] is False
    assert result["physical_execution_invoked"] is False


def test_complete_gateway_cli_hold_loop_prevents_rtl_dispatch() -> None:
    summary = deepcopy(_summary())
    guard = summary["mission_assurance_live_guard"]
    guard["guard_status"] = "no_dispatch"
    guard["selected_recovery_action"] = None
    guard["dispatch_prevented_by_mission_assurance"] = True
    guard["suppression_source"] = "mission_assurance_agent"
    guard["suppression_reason"] = (
        "mission_assurance_hold_suppressed_feasible_recovery_proposal"
    )
    guard["post_suppression_observation"] = {
        "observation_kind": "mission_assurance_post_suppression_reobservation"
    }
    guard["recovery_proposed_action_feasibility"] = {
        "feasibility_status": "verified_feasible"
    }
    guard["response_compilation"] = {"compile_status": "no_action_required"}
    guard["mission_assurance_evaluation"]["proposal"][
        "proposed_response_kind"
    ] = "hold"
    summary["recovery_state_observed"] = False
    summary["recovery_state_label"] = None
    summary["final_status"] = "aborted_pose_deviation"
    summary["task_status"] = "blocked"

    result = build_mission_assurance_gateway_px4_e2e(
        task_id="task_e2e",
        client_surface="missionos_cli",
        mission_assurance_requested=True,
        execution_approval=_approval(),
        live_summary=summary,
    )

    assert result["full_gateway_runtime_loop"] is True
    assert result["e2e_status"] == "completed"
    assert result["e2e_disposition"] == "hold_prevented_recovery_dispatch"
    assert result["recovery_proposed_action"] == "return_to_launch"
    assert result["recovery_proposed_action_feasibility_status"] == (
        "verified_feasible"
    )
    assert result["proposed_response_kind"] == "hold"
    assert result["dispatch_prevented_by_mission_assurance"] is True
    assert result["suppression_source"] == "mission_assurance_agent"
    assert result["post_suppression_reobservation_observed"] is True
    assert result["selected_recovery_action"] is None
    assert result["command_ack_observed"] is False


def test_complete_gateway_cli_accepts_independent_recovery_continue() -> None:
    summary = deepcopy(_summary())
    guard = summary["mission_assurance_live_guard"]
    guard["guard_status"] = "no_dispatch"
    guard["selected_recovery_action"] = None
    guard["recovery_proposal_accepted"] = True
    guard["recovery_no_dispatch_response_accepted"] = True
    guard["runtime_recovery_agent_proposal"]["selected_bounded_action"] = "continue"
    guard["runtime_recovery_agent_proposal"]["source_action_feasibility"] = {}
    guard["response_compilation"] = {"compile_status": "no_action_required"}
    guard["mission_assurance_evaluation"]["proposal"][
        "proposed_response_kind"
    ] = "continue"
    summary["recovery_state_observed"] = False
    summary["recovery_state_label"] = None
    summary["mission_assurance_continue_execution_invoked"] = True
    summary["mission_assurance_continue_effect_observed"] = True
    summary["mission_assurance_continue_route_completion_observed"] = True
    summary["mission_assurance_continue_dropoff_approach_observed"] = True
    summary["mission_assurance_continue_execution"] = {
        "existing_route_approval_consumed": True,
        "simulator_route_resume_invoked": True,
        "offboard_mode_switch_ack_observed": True,
        "offboard_mode_switch_ack_result_code": 0,
        "setpoint_frames_sent": 50,
        "route_resume_effect_observed": True,
    }
    summary["final_status"] = "completed"
    summary["task_status"] = "completed"
    summary["dropoff_region_reached"] = True
    summary["payload_release_observed"] = True
    summary["delivery_completion_claimed"] = True

    result = build_mission_assurance_gateway_px4_e2e(
        task_id="task_e2e",
        client_surface="missionos_cli",
        mission_assurance_requested=True,
        execution_approval=_approval(),
        live_summary=summary,
    )

    assert result["full_gateway_runtime_loop"] is True
    assert result["e2e_status"] == "completed"
    assert result["e2e_disposition"] == "continue_completed_route_delivery"
    assert result["recovery_proposed_action"] == "continue"
    assert result["proposed_response_kind"] == "continue"
    assert result["guard_status"] == "no_dispatch"
    assert result["selected_recovery_action"] is None
    assert result["command_ack_observed"] is False
    assert result["mission_assurance_continue_execution_invoked"] is True
    assert result["mission_assurance_continue_effect_observed"] is True
    assert result["mission_assurance_continue_route_completion_observed"] is True
    assert result["mission_assurance_continue_dropoff_approach_observed"] is True
    assert result["delivery_completion_claimed"] is True


def test_gateway_records_reverse_agent_disagreement_as_operator_escalation() -> None:
    summary = deepcopy(_summary())
    guard = summary["mission_assurance_live_guard"]
    guard.update(
        {
            "guard_status": "operator_escalation",
            "selected_recovery_action": None,
            "proposed_recovery_action": None,
            "operator_recovery_approval_request": {},
            "agent_disagreement_observed": True,
            "agent_disagreement_kind": (
                "assurance_action_without_recovery_action_candidate"
            ),
            "agent_disagreement_resolution": "operator_escalation",
            "assurance_requested_action": "return_to_launch",
            "recovery_no_action_response": "continue",
        }
    )
    guard["runtime_recovery_agent_proposal"][
        "selected_bounded_action"
    ] = "continue"
    guard["mission_assurance_evaluation"]["proposal"][
        "proposed_response_kind"
    ] = "return"
    guard["original_action_feasibility"] = {}
    guard["current_action_feasibility"] = {}
    guard["action_revalidation"] = {}
    summary["recovery_state_observed"] = False
    summary["recovery_state_label"] = None
    summary["final_status"] = "aborted_pose_deviation"
    summary["task_status"] = "blocked"

    result = build_mission_assurance_gateway_px4_e2e(
        task_id="task_e2e",
        client_surface="missionos_cli",
        mission_assurance_requested=True,
        execution_approval=_approval(),
        live_summary=summary,
    )

    assert result["full_gateway_runtime_loop"] is True
    assert result["e2e_disposition"] == (
        "agent_disagreement_operator_escalation"
    )
    assert result["agent_disagreement_observed"] is True
    assert result["agent_disagreement_resolution"] == "operator_escalation"
    assert result["selected_recovery_action"] is None
    assert result["command_ack_observed"] is False


def test_complete_loop_fails_closed_without_model_inference() -> None:
    summary = deepcopy(_summary())
    summary["mission_assurance_live_guard"]["mission_assurance_evaluation"][
        "proposal"
    ]["model_inference_invoked"] = False

    result = build_mission_assurance_gateway_px4_e2e(
        task_id="task_e2e",
        client_surface="missionos_cli",
        mission_assurance_requested=True,
        execution_approval=_approval(),
        live_summary=summary,
    )

    assert result["full_gateway_runtime_loop"] is False
    assert result["e2e_status"] == "blocked"
    assert result["blocking_reasons"] == [
        "mission_assurance_model_inference_not_observed"
    ]


def test_complete_loop_fails_closed_without_bound_cli_approval() -> None:
    approval = _approval()
    approval["mission_assurance_on_deviation_approved"] = False

    result = build_mission_assurance_gateway_px4_e2e(
        task_id="task_e2e",
        client_surface="missionos_cli",
        mission_assurance_requested=True,
        execution_approval=approval,
        live_summary=_summary(),
    )

    assert result["full_gateway_runtime_loop"] is False
    assert result["blocking_reasons"] == [
        "mission_assurance_execution_approval_binding_missing"
    ]


def test_gateway_binds_mission_assurance_request_to_execution_approval(
    monkeypatch,
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    from src.config.settings import reset_settings
    from src.gateway.server import create_missionos_gateway
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    reset_settings()
    reset_task_store()
    audit._audit_logger = None
    gateway = create_missionos_gateway()
    client = TestClient(gateway.app)
    proposed_response = client.post(
        "/px4-gazebo/mission-scenarios/propose",
        json={"prompt": "高度30mで風速4m/sのbounded simulator test"},
    )
    assert proposed_response.status_code == 200
    proposed = proposed_response.json()
    approved_response = client.post(
        "/px4-gazebo/mission-scenarios/approve",
        json={
            "scenario_proposal": proposed["scenario_proposal"],
            "validation_result": proposed["validation_result"],
        },
    )
    assert approved_response.status_code == 200
    approved = approved_response.json()
    prepared_response = client.post(
        "/px4-gazebo/mission-scenarios/prepare-sitl-execution",
        json={
            "scenario_proposal": proposed["scenario_proposal"],
            "validation_result": proposed["validation_result"],
            "scenario_approval": approved["scenario_approval"],
            "scenario_compile_result": approved["scenario_compile_result"],
            "bounded_simulation_request": approved["bounded_simulation_request"],
        },
    )
    assert prepared_response.status_code == 200
    task_id = prepared_response.json()["summary"]["task_id"]
    approval_response = client.post(
        "/px4-gazebo/mission-scenarios/approve-sitl-execution",
        json={
            "task_id": task_id,
            "explicit_execution_approval": True,
            "mission_assurance_on_deviation": True,
            "missionos_client_surface": "missionos_cli",
        },
    )
    assert approval_response.status_code == 200
    approval = approval_response.json()["execution_operator_approval"]
    assert approval["mission_assurance_on_deviation_approved"] is True
    assert approval["missionos_client_surface"] == "missionos_cli"

    mismatch = client.post(
        "/px4-gazebo/mission-scenarios/execute-sitl",
        json={
            "task_id": task_id,
            "execution_approval_id": approval["approval_id"],
            "live_flight_mode": True,
            "mission_assurance_on_deviation": False,
        },
    )

    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == (
        "mission_assurance_on_deviation does not match the stored execution approval"
    )
