from __future__ import annotations

from copy import deepcopy

from missionos_cli.flight_map_html import _mission_map_html
from missionos_cli.map_model import _mission_map_model
from missionos_cli.mission_assurance_projection import (
    mission_assurance_projection,
)
from missionos_cli.operate_view import _build_operate_status_group
from rich.console import Console


def _task_payload() -> dict:
    e2e = {
        "e2e_status": "completed",
        "decision_sequence": [
            "missionos_runtime_recovery_agent",
            "mission_assurance_agent",
            "source_action_feasibility",
            "fresh_operator_recovery_approval_boundary",
        ],
        "runtime_recovery_agent_invoked": True,
        "recovery_agent_invoked_before_mission_assurance": True,
        "recovery_agent_model_id": "recovery-model",
        "recovery_proposed_action": "return_to_launch",
        "mission_assurance_agent_invoked": True,
        "mission_assurance_agent_model_id": "assurance-model",
        "proposed_response_kind": "return",
        "original_action_feasibility_status": "verified_feasible",
        "current_action_feasibility_status": None,
        "action_revalidation_status": None,
        "guard_status": "awaiting_operator_approval",
        "selected_recovery_action": None,
        "proposed_recovery_action": "rtl",
        "fresh_operator_approval_required": True,
        "recovery_approval_status": "awaiting_operator_approval",
        "human_execution_approval_consumed": True,
        "runtime_state_observed": False,
        "runtime_state_label": None,
        "command_ack_observed": False,
        "final_status": "aborted_pose_deviation",
        "agent_created_approval": False,
        "agent_created_dispatch_authority": False,
        "physical_execution_invoked": False,
    }
    return {
        "task": {
            "task_id": "task_assurance_operator",
            "status": "blocked",
            "artifacts": {
                "missionos_mission_assurance_gateway_px4_e2e": e2e,
                "missionos_mission_assurance_px4_horizontal_summary": {
                    "route_target_x_m": 4.95,
                    "route_target_y_m": 5.0,
                    "route_target_z_m": -2.5,
                    "recovery_action_taken": "rtl",
                    "recovery_state_observed": True,
                    "recovery_state_label": "return_to_launch_state_observed",
                    "recovery_command_ack_observed": False,
                },
                "mission_designer_live_telemetry_snapshot": {
                    "sample_count": 3,
                    "latest_sample": {
                        "sample_index": 2,
                        "phase": "deviation",
                        "local_x_m": 3.2,
                        "local_y_m": 2.1,
                        "local_z_m": 2.45,
                        "battery_remaining_percent": 87.0,
                    },
                    "flight_path_profile": [
                        {
                            "sample_index": 0,
                            "phase": "takeoff",
                            "local_x_m": 0.0,
                            "local_y_m": 0.0,
                            "local_z_m": 0.0,
                        },
                        {
                            "sample_index": 1,
                            "phase": "route",
                            "local_x_m": 2.0,
                            "local_y_m": 1.0,
                            "local_z_m": 2.4,
                        },
                        {
                            "sample_index": 2,
                            "phase": "deviation",
                            "local_x_m": 3.2,
                            "local_y_m": 2.1,
                            "local_z_m": 2.45,
                            "battery_remaining_percent": 87.0,
                        },
                    ],
                },
            },
        }
    }


def test_operate_task_shows_fresh_recovery_approval_boundary() -> None:
    group, fingerprint = _build_operate_status_group(
        _task_payload(),
        proposal=None,
        pending=None,
        status="blocked",
        task_id="task_assurance_operator",
    )
    console = Console(record=True, width=180)
    console.print(group)
    rendered = console.export_text()

    assert "Recovery Agent proposes return_to_launch" in rendered
    assert "MissionAssuranceAgent judges return" in rendered
    assert "route_execution_approval_consumed=yes" in rendered
    assert "recovery_approval_recorded=no" in rendered
    assert "Operator approval required: fresh approval for rtl" in rendered
    assert "The route execution approval does not authorize this Recovery action" in rendered
    assert "command_ACK=no" in rendered
    assert "agents created no approval or dispatch authority" in rendered
    assert "x=3.20m y=2.10m z=2.45m" in rendered
    assert "observed_samples=3" in rendered
    assert "mission_assurance" in fingerprint


def test_unified_incident_graph_is_visible_without_legacy_rtl_artifacts() -> None:
    projection = mission_assurance_projection(
        {
            "missionos_mission_incident_graph": {
                "schema_version": (
                    "missionos_adk_v2_mission_incident_graph_result.v1"
                ),
                "workflow_name": "missionos_mission_incident_v2",
                "decision_sequence": [
                    "runtime_recovery_agent",
                    "source_action_feasibility",
                    "mission_assurance_agent",
                    "operator_recovery_approval_boundary",
                ],
                "decision_status": "awaiting_operator_approval",
                "operator_approval_required": True,
                "recovery_agent_invoked": True,
                "recovery_agent_invoked_before_mission_assurance": True,
                "recovery_proposed_action": "avoid_obstacle",
                "recovery_result": {
                    "runtime_status": "proposal_guardrail_passed",
                    "assessment": {
                        "selected_bounded_action": "avoid_obstacle"
                    },
                    "agent_invocations": [
                        {
                            "agent_name": "missionos_runtime_recovery_agent",
                            "model_id": "deepseek-v4-flash",
                        }
                    ],
                },
                "source_action_feasibility": {
                    "action": "avoid_obstacle",
                    "feasibility_status": "verified_feasible",
                },
                "mission_assurance_agent_invoked": True,
                "mission_assurance_proposal": {
                    "proposed_response_kind": "replan",
                    "judgment_status": "proposal_guardrail_passed",
                    "model_inference_invoked": True,
                    "model_invocation_evidence": {
                        "model_id": "deepseek-v4-flash"
                    },
                },
                "approval_created": False,
                "dispatch_authority_created": False,
                "physical_execution_invoked": False,
            }
        }
    )

    assert projection["source_artifact"] == "missionos_mission_incident_graph"
    assert projection["recovery_proposed_action"] == "avoid_obstacle"
    assert projection["mission_assurance_response"] == "replan"
    assert projection["original_feasibility"] == "verified_feasible"
    assert projection["fresh_operator_approval_required"] is True
    assert projection["guard_status"] == "awaiting_operator_approval"


def test_map_uses_persisted_px4_local_coordinates_without_inventing_rtl_home() -> None:
    model = _mission_map_model(task_payload=_task_payload(), provider="osm", live_task_url=None)

    assert model["map_kind"] == "px4_gazebo_local_xy"
    assert model["planned_points"][1] == {
        "role": "route_target",
        "x_m": 4.95,
        "y_m": 5.0,
        "altitude_up_m": 2.5,
    }
    assert model["observed_points"][-1]["x_m"] == 3.2
    assert model["observed_points"][-1]["y_m"] == 2.1
    assert model["recovery_event"]["final_xy_observed"] is False
    assert model["recovery_event"]["x_m"] == 3.2
    assert model["mission_assurance"]["command_ack_observed"] is False

    page = _mission_map_html(model)
    assert "PX4/Gazebo local XY" in page
    assert "Recovery Agent" in page
    assert "MissionAssuranceAgent" in page
    assert "final RTL/home XY was not observed" in page
    assert "no basemap/WGS84 conversion" in page


def test_operate_and_map_distinguish_assurance_hold_from_rules_block() -> None:
    payload = deepcopy(_task_payload())
    task = payload["task"]
    task["status"] = "blocked"
    e2e = task["artifacts"]["missionos_mission_assurance_gateway_px4_e2e"]
    e2e.update(
        {
            "decision_sequence": [
                "missionos_runtime_recovery_agent",
                "source_action_feasibility",
                "mission_assurance_agent",
                "mission_assurance_no_dispatch_boundary",
                "post_suppression_reobservation",
            ],
            "proposed_response_kind": "hold",
            "original_feasibility_status": "verified_feasible",
            "current_action_feasibility_status": None,
            "action_revalidation_status": None,
            "guard_status": "no_dispatch",
            "selected_recovery_action": None,
            "dispatch_prevented_by_mission_assurance": True,
            "suppression_source": "mission_assurance_agent",
            "suppression_reason": (
                "mission_assurance_hold_suppressed_feasible_recovery_proposal"
            ),
            "post_suppression_reobservation_observed": True,
            "runtime_state_observed": False,
            "runtime_state_label": None,
            "final_status": "aborted_pose_deviation",
        }
    )

    group, _ = _build_operate_status_group(
        payload,
        proposal=None,
        pending=None,
        status="blocked",
        task_id="task_assurance_operator",
    )
    console = Console(record=True, width=180)
    console.print(group)
    rendered = console.export_text()
    model = _mission_map_model(task_payload=payload, provider="osm", live_task_url=None)
    page = _mission_map_html(model)

    assert "MissionAssuranceAgent judges hold" in rendered
    assert "original=verified_feasible" in rendered
    assert "feasible recovery proposal suppressed by MissionAssuranceAgent" in rendered
    assert "dispatch=no" in rendered
    assert "post-decision re-observation=yes" in rendered
    assert model["mission_assurance"]["dispatch_prevented_by_mission_assurance"] is True
    assert model["mission_assurance"]["suppression_source"] == (
        "mission_assurance_agent"
    )
    assert "feasible recovery suppressed" in page


def test_operate_and_map_show_reverse_agent_disagreement_escalation() -> None:
    payload = deepcopy(_task_payload())
    task = payload["task"]
    task["status"] = "blocked"
    e2e = task["artifacts"]["missionos_mission_assurance_gateway_px4_e2e"]
    e2e.update(
        {
            "decision_sequence": [
                "missionos_runtime_recovery_agent",
                "mission_assurance_agent",
                "agent_disagreement_operator_escalation_boundary",
            ],
            "recovery_proposed_action": "continue",
            "proposed_response_kind": "return",
            "guard_status": "operator_escalation",
            "selected_recovery_action": None,
            "proposed_recovery_action": None,
            "fresh_operator_approval_required": False,
            "recovery_approval_status": None,
            "agent_disagreement_observed": True,
            "agent_disagreement_kind": (
                "assurance_action_without_recovery_action_candidate"
            ),
            "agent_disagreement_resolution": "operator_escalation",
            "assurance_requested_action": "return_to_launch",
            "recovery_no_action_response": "continue",
            "runtime_state_observed": False,
            "runtime_state_label": None,
            "final_status": "aborted_pose_deviation",
        }
    )
    summary = task["artifacts"]["missionos_mission_assurance_px4_horizontal_summary"]
    summary["recovery_action_taken"] = None
    summary["recovery_state_observed"] = False
    summary["recovery_state_label"] = None

    group, _ = _build_operate_status_group(
        payload,
        proposal=None,
        pending=None,
        status="blocked",
        task_id="task_assurance_operator",
    )
    console = Console(record=True, width=180)
    console.print(group)
    rendered = console.export_text()
    model = _mission_map_model(task_payload=payload, provider="osm", live_task_url=None)
    page = _mission_map_html(model)

    assert "Recovery Agent proposes continue" in rendered
    assert "MissionAssuranceAgent judges return" in rendered
    assert "Agent disagreement: Recovery proposed continue" in rendered
    assert "No action was invented; resolution=operator_escalation" in rendered
    assert model["mission_assurance"]["agent_disagreement_observed"] is True
    assert model["mission_assurance"]["agent_disagreement_resolution"] == (
        "operator_escalation"
    )
    assert model["mission_assurance"]["assurance_requested_action"] == (
        "return_to_launch"
    )
    assert "Agent disagreement" in page
    assert "assurance_requested_action" in page
