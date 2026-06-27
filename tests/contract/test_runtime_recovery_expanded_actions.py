from __future__ import annotations

from datetime import datetime, timezone

import pytest
import click
from rich.console import Console

from missionos_cli import cli as missionos_cli
from scripts import smoke_missionos_auto_mission_full_runtime_probe as auto_probe
from src.gateway import server as gateway_server
from src.intelligence import missionos_agent_runtime
from src.runtime import px4_gazebo_mission_designer_sitl_live_flight_run as live_run
from src.runtime.recovery_window_summary import build_recovery_window_summary

pytestmark = pytest.mark.contract


def _assessment(
    action: str,
    *,
    parameters: dict | None = None,
    telemetry: dict | None = None,
) -> dict:
    return missionos_agent_runtime._validate_runtime_recovery_output(
        agent_output={
            "selected_bounded_action": action,
            "trigger_level": "advisory",
            "requires_human_approval": True,
            "proposed_parameters": parameters or {},
        },
        telemetry_snapshot=telemetry or {},
        recovery_policy={
            "preauthorized_actions": [
                "return_to_launch",
                "land",
                "adjust_altitude",
                "adjust_speed",
                "reroute",
                "avoid_obstacle",
            ]
        },
    )


def _planner_tool_telemetry() -> dict:
    return {
        "position": {
            "local_x_m": 0.0,
            "local_y_m": 0.0,
            "altitude_above_home_m": 25.0,
        },
        "terrain": {
            "terrain_clearance_m": 18.0,
            "terrain_clearance_target_m": 30.0,
            "terrain_clearance_margin_m": -12.0,
            "terrain_clearance_below_minimum": True,
        },
        "route": {
            "active_leg": {
                "from_x_m": 0.0,
                "from_y_m": 0.0,
                "to_x_m": 200.0,
                "to_y_m": 0.0,
            }
        },
        "obstacle": {
            "obstacle_detected": True,
            "building_risk_detected": True,
            "obstacle_manifest": {
                "obstacles": [
                    {
                        "name": "missionos_landing_zone_blocker",
                        "kind": "building_box",
                        "source": "gazebo_pose_readback",
                        "x_m": 100.0,
                        "y_m": 0.0,
                        "size_x_m": 20.0,
                        "size_y_m": 20.0,
                    }
                ]
            },
        },
    }


def _planner_policy() -> dict:
    return {
        "policy_ref": "test_recovery_tool_policy",
        "preauthorized_actions": [
            "adjust_altitude",
            "reroute",
            "avoid_obstacle",
        ],
        "min_terrain_clearance_m": 30.0,
        "max_adjust_altitude_m": 120.0,
        "max_reroute_target_abs_m": 5000.0,
    }


def test_runtime_recovery_prompt_advertises_planner_function_tool() -> None:
    payload = missionos_agent_runtime._runtime_recovery_prompt_payload(
        telemetry_snapshot={},
        mission_context={},
        recovery_policy={},
    )

    tool = payload["role_contract"]["function_tools"][0]
    assert tool["name"] == missionos_agent_runtime.MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_NAME
    assert tool["must_use_before_actions"] == [
        "adjust_altitude",
        "reroute",
        "avoid_obstacle",
    ]
    assert tool["copy_tool_proposed_parameters_exactly"] is True


def test_runtime_recovery_planner_tool_computes_altitude_and_obstacle_targets() -> None:
    avoid_result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=_planner_tool_telemetry(),
        mission_context={},
        recovery_policy=_planner_policy(),
        requested_action="avoid_obstacle",
        request_reason="source-backed building risk near original route",
    )
    altitude_result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=_planner_tool_telemetry(),
        mission_context={},
        recovery_policy=_planner_policy(),
        requested_action="adjust_altitude",
    )

    assert avoid_result["tool_status"] == "computed"
    avoid_candidate = avoid_result["recommended_candidate"]
    assert avoid_candidate["selected_bounded_action"] == "avoid_obstacle"
    assert avoid_candidate["proposed_parameters"] == {
        "target_x_m": 30.0,
        "target_y_m": 30.0,
        "target_altitude_m": 45.0,
    }
    assert (
        avoid_candidate["basis"]["route_vector_source_ref"]
        == "telemetry_snapshot.route.active_leg"
    )
    assert "avoid_obstacle" in avoid_result["candidate_actions"]
    assert "reroute" not in avoid_result["candidate_actions"]
    assert avoid_result["dispatch_authority_created"] is False
    assert avoid_result["physical_execution_invoked"] is False
    assert avoid_result["progress_counted"] is False

    assert altitude_result["tool_status"] == "computed"
    assert altitude_result["recommended_candidate"]["proposed_parameters"] == {
        "target_altitude_m": 42.0
    }


def test_runtime_recovery_planner_honors_operator_requested_altitude() -> None:
    telemetry = _planner_tool_telemetry()
    telemetry["terrain"] = {
        "terrain_clearance_m": 40.0,
        "terrain_clearance_target_m": 30.0,
        "terrain_clearance_margin_m": 10.0,
        "terrain_clearance_below_minimum": False,
    }

    result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        mission_context={
            "operator_recovery_request": {
                "requested_action": "adjust_altitude",
                "target_altitude_m": 50.0,
            }
        },
        recovery_policy=_planner_policy(),
        requested_action="adjust_altitude",
        request_reason="operator asked to climb to 50m",
    )

    assert result["tool_status"] == "computed"
    assert result["recommended_candidate"]["selected_bounded_action"] == (
        "adjust_altitude"
    )
    assert result["recommended_candidate"]["proposed_parameters"] == {
        "target_altitude_m": 50.0
    }
    assert (
        "mission_context.operator_recovery_request"
        in result["recommended_candidate"]["source_refs"]
    )
    assert result["dispatch_authority_created"] is False
    assert result["physical_execution_invoked"] is False


def test_runtime_recovery_planner_treats_altitude_delta_as_signed() -> None:
    telemetry = _planner_tool_telemetry()
    telemetry["terrain"] = {
        "terrain_clearance_m": 40.0,
        "terrain_clearance_target_m": 30.0,
        "terrain_clearance_margin_m": 10.0,
        "terrain_clearance_below_minimum": False,
    }

    result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        mission_context={
            "operator_recovery_request": {
                "requested_action": "adjust_altitude",
                "altitude_delta_m": -15.0,
            }
        },
        recovery_policy=_planner_policy(),
        requested_action="adjust_altitude",
        request_reason="operator asked to descend by 15m",
    )

    assert result["tool_status"] == "computed"
    candidate = result["recommended_candidate"]
    assert candidate["selected_bounded_action"] == "adjust_altitude"
    assert candidate["proposed_parameters"] == {"target_altitude_m": 10.0}
    assert candidate["basis"]["requested_delta_m"] == -15.0
    assert candidate["basis"]["adjustment_m"] == -15.0
    assert result["dispatch_authority_created"] is False
    assert result["physical_execution_invoked"] is False


def test_runtime_recovery_planner_derives_requested_reroute_without_coordinates() -> None:
    telemetry = _planner_tool_telemetry()
    telemetry["obstacle"] = {}

    result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        mission_context={
            "operator_recovery_request": {
                "requested_action": "reroute",
            }
        },
        recovery_policy={
            **_planner_policy(),
            "operator_reroute_forward_m": 80.0,
            "operator_reroute_lateral_m": 30.0,
        },
        requested_action="reroute",
        request_reason="operator asked for a route change",
    )

    assert result["tool_status"] == "computed"
    assert result["recommended_candidate"]["selected_bounded_action"] == "reroute"
    assert result["recommended_candidate"]["proposed_parameters"] == {
        "target_x_m": 80.0,
        "target_y_m": 30.0,
    }
    assert result["dispatch_authority_created"] is False
    assert result["physical_execution_invoked"] is False


def test_runtime_recovery_guard_requires_tool_match_for_parameterized_agent_actions() -> None:
    telemetry = _planner_tool_telemetry()
    policy = _planner_policy()
    tool_result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
        requested_action="avoid_obstacle",
    )
    tool_parameters = tool_result["recommended_candidate"]["proposed_parameters"]

    matching = missionos_agent_runtime._validate_runtime_recovery_output(
        agent_output={
            "selected_bounded_action": "avoid_obstacle",
            "trigger_level": "advisory",
            "requires_human_approval": True,
            "proposed_parameters": tool_parameters,
        },
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
        planner_tool_results=[tool_result],
        require_parameter_tool_call=True,
        parameter_tool_called=True,
    )
    no_tool_call = missionos_agent_runtime._validate_runtime_recovery_output(
        agent_output={
            "selected_bounded_action": "avoid_obstacle",
            "trigger_level": "advisory",
            "requires_human_approval": True,
            "proposed_parameters": tool_parameters,
        },
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
        planner_tool_results=[],
        require_parameter_tool_call=True,
        parameter_tool_called=False,
    )
    invented_parameters = dict(tool_parameters)
    invented_parameters["target_y_m"] = 75.0
    invented = missionos_agent_runtime._validate_runtime_recovery_output(
        agent_output={
            "selected_bounded_action": "avoid_obstacle",
            "trigger_level": "advisory",
            "requires_human_approval": True,
            "proposed_parameters": invented_parameters,
        },
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
        planner_tool_results=[tool_result],
        require_parameter_tool_call=True,
        parameter_tool_called=True,
    )
    mismatched_action = missionos_agent_runtime._validate_runtime_recovery_output(
        agent_output={
            "selected_bounded_action": "reroute",
            "trigger_level": "advisory",
            "requires_human_approval": True,
            "proposed_parameters": tool_parameters,
        },
        telemetry_snapshot=telemetry,
        recovery_policy=policy,
        planner_tool_results=[tool_result],
        require_parameter_tool_call=True,
        parameter_tool_called=True,
    )

    assert matching["assessment_status"] == "proposal_guardrail_passed"
    assert matching["proposed_parameters_source"] == (
        "runtime_recovery_planner_function_tool"
    )
    assert matching["recovery_planner_tool_called"] is True
    assert no_tool_call["selected_bounded_action"] == "operator_review"
    assert (
        "parameterized_recovery_requires_runtime_recovery_planner_tool_call"
        in no_tool_call["blocking_reasons"]
    )
    assert invented["selected_bounded_action"] == "operator_review"
    assert (
        "parameterized_recovery_parameters_must_match_runtime_recovery_planner_tool_candidate"
        in invented["blocking_reasons"]
    )
    assert mismatched_action["selected_bounded_action"] == "operator_review"
    assert (
        "parameterized_recovery_action_must_match_runtime_recovery_planner_"
        "recommendation"
        in mismatched_action["blocking_reasons"]
    )


def test_runtime_recovery_direct_planner_result_uses_shared_guardrail() -> None:
    telemetry = _planner_tool_telemetry()
    telemetry["obstacle"] = {}
    malicious_candidate = {
        "selected_bounded_action": "avoid_obstacle",
        "proposed_parameters": {"target_x_m": 40.0, "target_y_m": 20.0},
        "source_refs": ["test.malicious_candidate"],
    }
    planner_result = {
        "schema_version": (
            missionos_agent_runtime.MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_SCHEMA_VERSION
        ),
        "tool_name": missionos_agent_runtime.MISSIONOS_RUNTIME_RECOVERY_PLANNER_TOOL_NAME,
        "tool_status": "computed",
        "requested_action": "avoid_obstacle",
        "request_reason": "operator asked to avoid an obstacle",
        "recommended_candidate": malicious_candidate,
        "candidates": [malicious_candidate],
        "candidate_actions": ["avoid_obstacle"],
        "dispatch_authority_created": False,
        "operator_approval_required": True,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }

    guarded = missionos_agent_runtime.guard_runtime_recovery_planner_result(
        planner_result=planner_result,
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
    )

    assert guarded["tool_status"] == "guardrail_blocked"
    assert guarded["recommended_candidate"]["selected_bounded_action"] == (
        "operator_review"
    )
    assert guarded["recommended_candidate"]["proposed_parameters"] == {}
    assert (
        guarded["recovery_guardrail_assessment"]["selected_bounded_action"]
        == "operator_review"
    )
    assert (
        "avoid_obstacle_requires_source_backed_obstacle_or_building_risk"
        in guarded["recovery_guardrail_assessment"]["blocking_reasons"]
    )
    assert guarded["dispatch_authority_created"] is False
    assert guarded["physical_execution_invoked"] is False


def test_runtime_recovery_direct_planner_guard_preserves_valid_candidate() -> None:
    telemetry = _planner_tool_telemetry()
    planner_result = missionos_agent_runtime.plan_runtime_recovery_maneuver(
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
        requested_action="avoid_obstacle",
    )

    guarded = missionos_agent_runtime.guard_runtime_recovery_planner_result(
        planner_result=planner_result,
        telemetry_snapshot=telemetry,
        recovery_policy=_planner_policy(),
    )

    assert guarded["tool_status"] == "computed"
    assert guarded["guardrail_status"] == "proposal_guardrail_passed"
    assert guarded["recommended_candidate"] == planner_result["recommended_candidate"]
    assert guarded["recovery_guardrail_assessment"]["assessment_status"] == (
        "proposal_guardrail_passed"
    )


def test_runtime_recovery_accepts_bounded_altitude_speed_and_reroute_parameters() -> None:
    altitude = _assessment("adjust_altitude", parameters={"target_altitude_m": 45.0})
    speed = _assessment("adjust_speed", parameters={"target_speed_mps": 8.0})
    reroute = _assessment(
        "reroute",
        parameters={"target_x_m": 120.0, "target_y_m": -20.0, "target_altitude_m": 35.0},
    )

    assert altitude["assessment_status"] == "proposal_guardrail_passed"
    assert altitude["selected_bounded_action"] == "adjust_altitude"
    assert speed["assessment_status"] == "proposal_guardrail_passed"
    assert speed["selected_bounded_action"] == "adjust_speed"
    assert reroute["assessment_status"] == "proposal_guardrail_passed"
    assert reroute["selected_bounded_action"] == "reroute"


def test_runtime_recovery_blocks_unbounded_or_unsourced_obstacle_maneuvers() -> None:
    missing_parameter = _assessment("adjust_altitude")
    unsourced_obstacle = _assessment(
        "avoid_obstacle",
        parameters={"target_x_m": 40.0, "target_y_m": 20.0},
    )
    sourced_obstacle = _assessment(
        "avoid_obstacle",
        parameters={"target_x_m": 40.0, "target_y_m": 20.0},
        telemetry={"obstacle": {"obstacle_detected": True}},
    )

    assert missing_parameter["selected_bounded_action"] == "operator_review"
    assert "adjust_altitude_requires_target_altitude_m" in missing_parameter["blocking_reasons"]
    assert unsourced_obstacle["selected_bounded_action"] == "operator_review"
    assert (
        "avoid_obstacle_requires_source_backed_obstacle_or_building_risk"
        in unsourced_obstacle["blocking_reasons"]
    )
    assert sourced_obstacle["assessment_status"] == "proposal_guardrail_passed"
    assert sourced_obstacle["selected_bounded_action"] == "avoid_obstacle"


def test_gateway_bounds_parameterized_recovery_requests_and_marks_maneuver_approval() -> None:
    params = gateway_server._bounded_operator_recovery_parameters(
        recovery_action="reroute",
        body={
            "recovery_parameters": {
                "target_x_m": "150",
                "target_y_m": "-25",
                "target_altitude_m": "40",
            }
        },
    )
    approval, allowlist = gateway_server._operator_recovery_approval_payload(
        recovery_action="reroute",
        task_id="task_expanded_recovery",
        parameters=params,
        now=datetime(2026, 6, 23, tzinfo=timezone.utc),
    )

    assert params == {
        "target_x_m": 150.0,
        "target_y_m": -25.0,
        "target_altitude_m": 40.0,
    }
    assert approval["operator_approval_performed"] is True
    assert approval["approved_recovery_action"] == "reroute"
    assert approval["physical_execution_invoked"] is False
    assert allowlist["active_runner_required"] is True
    assert "SET_POSITION_TARGET_LOCAL_NED" in allowlist["allowed_mavlink_message_ids"]


def test_operate_action_panel_surfaces_parameterized_recovery_without_rtl_land_keys() -> None:
    panel = missionos_cli._render_action_panel(
        {
            "action": "adjust_altitude",
            "status": "proposal_guardrail_passed",
            "risks": ["terrain_clearance_below_minimum"],
            "parameters": {"target_altitude_m": 45.0},
        },
        confirming=None,
    )
    rendered = str(panel.renderable)

    assert "params = target_altitude_m=45.0" in rendered
    assert "climb <m>" in rendered
    assert "speed <m/s>" in rendered
    assert "reroute <x> <y> (alt)" in rendered
    assert "approve RTL" not in rendered
    assert "approve LAND" not in rendered


def test_operate_console_parses_direct_recovery_commands() -> None:
    climb = missionos_cli._parse_operate_console_command("climb 45")
    speed = missionos_cli._parse_operate_console_command("speed 7")
    reroute = missionos_cli._parse_operate_console_command("reroute 120 -20 35")
    avoid = missionos_cli._parse_operate_console_command("avoid x=40 y=20 alt=45")
    rtl = missionos_cli._parse_operate_console_command("rtl")

    assert climb.kind == "dispatch"
    assert climb.action == "adjust_altitude"
    assert climb.parameters == {"target_altitude_m": 45.0}
    assert speed.action == "adjust_speed"
    assert speed.parameters == {"target_speed_mps": 7.0}
    assert reroute.action == "reroute"
    assert reroute.parameters == {
        "target_x_m": 120.0,
        "target_y_m": -20.0,
        "target_altitude_m": 35.0,
    }
    assert avoid.action == "avoid_obstacle"
    assert avoid.parameters == {
        "target_x_m": 40.0,
        "target_y_m": 20.0,
        "target_altitude_m": 45.0,
    }
    assert rtl.action == "return_to_launch"
    assert rtl.parameters == {}


def test_operate_console_rejects_missing_direct_command_parameters() -> None:
    with pytest.raises(click.ClickException, match="usage: climb"):
        missionos_cli._parse_operate_console_command("climb")
    with pytest.raises(click.ClickException, match="usage: reroute"):
        missionos_cli._parse_operate_console_command("reroute 120")


def test_gazebo_obstacle_manifest_materializes_landing_zone_blocker() -> None:
    manifest = auto_probe._gazebo_obstacle_manifest_from_route(
        {
            "takeoff_latitude": 35.681236,
            "takeoff_longitude": 139.767125,
            "dropoff_latitude": 35.6984,
            "dropoff_longitude": 139.773,
            "landing_zone_blocked": True,
        }
    )

    assert manifest["manifest_status"] == "configured"
    assert manifest["landing_zone_blocked"] is True
    assert manifest["building_risk_detected"] is True
    assert manifest["gazebo_obstacle_model_spawn_requested"] is True
    assert manifest["gazebo_obstacle_model_spawned"] is False
    assert manifest["obstacles"][0]["name"] == "missionos_landing_zone_blocker"
    assert manifest["obstacles"][0]["frame"] == "gazebo_world_local_ned"


def test_gazebo_obstacle_artifacts_require_pose_readback_for_spawn_claim() -> None:
    route = {
        "takeoff_latitude": 35.681236,
        "takeoff_longitude": 139.767125,
        "dropoff_latitude": 35.6984,
        "dropoff_longitude": 139.773,
        "landing_zone_blocked": True,
    }
    manifest = auto_probe._gazebo_obstacle_manifest_from_route(route)
    artifacts = auto_probe._gazebo_obstacle_runtime_artifacts(
        route=route,
        probe_observed={
            "gazebo_obstacle_application": {
                "application_status": "applied",
                "gazebo_obstacle_model_spawn_requested": True,
                "gazebo_obstacle_model_spawned": True,
                "requested_model_count": 1,
                "spawned_model_count": 1,
                "obstacle_manifest": {
                    **manifest,
                    "gazebo_obstacle_model_spawned": True,
                },
                "models": [
                    {
                        "name": "missionos_landing_zone_blocker",
                        "pose_readback_observed": True,
                        "pose_readback": {"x": 10.0, "y": 2.0, "z": 10.0},
                    }
                ],
            }
        },
    )

    assert artifacts["gazebo_world_application"]["application_status"] == "applied"
    assert artifacts["obstacle_manifest"]["gazebo_obstacle_model_spawned"] is True
    assert artifacts["observed_world_condition_evidence"]["observation_status"] == (
        "gazebo_obstacle_pose_readback_observed"
    )


def test_running_gazebo_obstacle_spawn_reaches_recovery_projection() -> None:
    marker = {
        "sample_index": 3,
        "gazebo_obstacle_model_spawned": True,
        "gazebo_obstacle_model_spawn_requested": True,
        "gazebo_obstacle_application_status": "applied",
        "obstacle_manifest": {
            "schema_version": "missionos_gazebo_obstacle_manifest.v1",
            "manifest_status": "configured",
            "building_risk_detected": True,
            "landing_zone_blocked": True,
            "gazebo_obstacle_model_spawned": True,
            "obstacles": [{"name": "missionos_landing_zone_blocker"}],
        },
        "gazebo_obstacle_application": {"application_status": "applied"},
    }
    snapshot = auto_probe._build_running_snapshot(marker, waypoint_total=4)
    projection = live_run._auto_runtime_obstacle_projection(
        artifacts={"missionos_auto_mission_runtime_snapshot": snapshot}
    )

    assert projection["projection_status"] == "source_backed"
    assert projection["obstacle_detected"] is True
    assert projection["building_risk_detected"] is True
    assert projection["gazebo_obstacle_model_spawned"] is True


def test_recovery_window_treats_source_backed_obstacle_as_hard_news() -> None:
    summary = build_recovery_window_summary(
        [
            {
                "sample_index": 1,
                "elapsed_seconds": 1.0,
                "battery_remaining_percent": 98.0,
                "terrain_clearance_m": 30.0,
                "obstacle": {
                    "projection_status": "source_backed",
                    "obstacle_detected": True,
                    "building_risk_detected": True,
                    "gazebo_obstacle_model_spawned": True,
                    "obstacle_manifest": {
                        "building_risk_detected": True,
                        "gazebo_obstacle_model_spawned": True,
                    },
                },
            }
        ]
    )

    assert summary["hard_breaches"]["obstacle_or_building_risk"] is True
    assert summary["hard_breaches"]["any"] is True
    assert summary["overall"]["obstacle_or_building_risk_count"] == 1
    assert summary["latest"]["obstacle_or_building_risk"] is True


def test_live_recovery_agent_timeout_falls_back_to_guarded_planner() -> None:
    result = live_run._runtime_recovery_agent_fallback_result(
        telemetry_snapshot=_planner_tool_telemetry(),
        task_id="task_timeout_fallback",
        reason="runtime_recovery_agent_timeout",
        detail="timeout_seconds=0.001",
    )

    assert result["runtime_status"] == "proposal_guardrail_passed"
    assert result["assessment"]["selected_bounded_action"] == "avoid_obstacle"
    assert result["assessment"]["proposed_parameters"]["target_altitude_m"] == 45.0
    assert result["agent_invocations"][0]["function_tool_called"] is True
    assert result["dispatch_authority_created"] is False
    assert result["progress_counted"] is False


def test_running_snapshot_preserves_operator_maneuver_observation() -> None:
    marker = {
        "sample_index": 9,
        "operator_recovery_request_observed": True,
        "operator_recovery_action": "avoid_obstacle",
        "operator_recovery_parameters": {
            "target_x_m": 150.0,
            "target_y_m": -25.0,
            "target_altitude_m": 45.0,
        },
        "operator_recovery_command_ack_observed": True,
        "operator_recovery_command_ack_result": 0,
        "operator_recovery_path": "SET_POSITION_TARGET_LOCAL_NED:avoid_obstacle",
        "operator_recovery_target": {
            "assist_kind": "bounded_offboard_obstacle_avoidance_reroute",
            "target_x_m": 150.0,
            "target_y_m": -25.0,
            "target_z_m": -45.0,
        },
        "operator_recovery_assist_status": "target_reached",
        "operator_recovery_assist_kind": "bounded_offboard_obstacle_avoidance_reroute",
        "operator_recovery_assist_setpoint_frames_sent": 42,
        "operator_recovery_target_reached": True,
        "operator_recovery_target_distance_m": 2.5,
        "operator_recovery_target_altitude_m": 45.0,
        "operator_recovery_altitude_delta_m": 14.2,
        "operator_recovery_local_delta_x_m": 18.0,
        "operator_recovery_local_delta_y_m": -4.5,
        "operator_recovery_terminal": False,
        "operator_recovery_resume_auto_attempted": True,
        "operator_recovery_resume_auto_ack_observed": True,
        "operator_recovery_resume_auto_ack_result": 0,
        "operator_recovery_resume_auto_nav_state_observed": True,
        "operator_recovery_resume_auto_nav_state": 3,
        "operator_recovery_resume_auto_status": "resumed_auto_mission",
    }

    snapshot = auto_probe._build_running_snapshot(marker, waypoint_total=4)

    assert snapshot["operator_recovery_action"] == "avoid_obstacle"
    assert snapshot["operator_recovery_path"] == "SET_POSITION_TARGET_LOCAL_NED:avoid_obstacle"
    assert snapshot["operator_recovery_target"]["target_z_m"] == -45.0
    assert snapshot["operator_recovery_assist_status"] == "target_reached"
    assert snapshot["operator_recovery_target_reached"] is True
    assert snapshot["operator_recovery_target_distance_m"] == 2.5
    assert snapshot["operator_recovery_altitude_delta_m"] == 14.2
    assert snapshot["operator_recovery_terminal"] is False
    assert snapshot["operator_recovery_resume_auto_status"] == "resumed_auto_mission"
    assert snapshot["operator_recovery_resume_auto_nav_state_observed"] is True

    lines = missionos_cli._recovery_runner_observation_lines(
        {"artifacts": {"missionos_auto_mission_runtime_snapshot": snapshot}}
    )
    assist_line = "\n".join(lines)
    assert "assist=target_reached" in assist_line
    assert "kind=bounded_offboard_obstacle_avoidance_reroute" in assist_line
    assert "target=True" in assist_line
    assert "resume=resumed_auto_mission" in assist_line


def test_operator_recovery_wait_returns_on_maneuver_assist_status() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, path: str) -> dict:
            self.calls += 1
            assert path == "/tasks/task_maneuver_wait"
            return {
                "task": {
                    "task_id": "task_maneuver_wait",
                    "status": "running",
                    "artifacts": {
                        "missionos_auto_mission_runtime_snapshot": {
                            "operator_recovery_request_observed": True,
                            "operator_recovery_command_ack_observed": True,
                            "operator_recovery_command_ack_result": 0,
                            "operator_recovery_assist_status": "target_reached",
                            "operator_recovery_target_reached": True,
                            "operator_recovery_resume_auto_status": "resumed_auto_mission",
                        }
                    },
                }
            }

    client = Client()
    observed = missionos_cli._wait_for_active_runner_recovery_observation(
        client,  # type: ignore[arg-type]
        {
            "summary": {
                "task_id": "task_maneuver_wait",
                "recovery_action": "avoid_obstacle",
                "active_runner_request_queued": True,
            }
        },
        timeout_seconds=5.0,
        poll_interval=0.01,
    )

    assert observed is not None
    assert client.calls == 1


def test_operator_recovery_wait_ignores_stale_maneuver_assist_parameters() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, path: str) -> dict:
            self.calls += 1
            assert path == "/tasks/task_maneuver_wait"
            target_x = 12.0 if self.calls == 1 else 40.0
            return {
                "task": {
                    "task_id": "task_maneuver_wait",
                    "status": "running",
                    "artifacts": {
                        "missionos_auto_mission_runtime_snapshot": {
                            "operator_recovery_request_observed": True,
                            "operator_recovery_command_ack_observed": True,
                            "operator_recovery_command_ack_result": 0,
                            "operator_recovery_parameters": {
                                "target_x_m": target_x,
                                "target_y_m": 20.0,
                                "target_altitude_m": 45.0,
                            },
                            "operator_recovery_assist_status": "target_reached",
                            "operator_recovery_target_reached": True,
                        }
                    },
                }
            }

    client = Client()
    observed = missionos_cli._wait_for_active_runner_recovery_observation(
        client,  # type: ignore[arg-type]
        {
            "summary": {
                "task_id": "task_maneuver_wait",
                "recovery_action": "avoid_obstacle",
                "active_runner_request_queued": True,
                "recovery_parameters": {
                    "target_x_m": 40.0,
                    "target_y_m": 20.0,
                    "target_altitude_m": 45.0,
                },
            }
        },
        timeout_seconds=5.0,
        poll_interval=0.01,
    )

    assert observed is not None
    assert client.calls == 2


def test_operator_dispatch_summary_keeps_receipt_action_over_terminal_return_snapshot() -> None:
    line = missionos_cli._operator_recovery_dispatch_status_text(
        artifacts={
            "missionos_runtime_recovery_dispatch_receipt": {
                "dispatch_status": "queued_for_active_runner",
                "recovery_action": "avoid_obstacle",
                "active_runner_request_queued": True,
                "recovery_parameters": {
                    "target_x_m": 40.0,
                    "target_y_m": 20.0,
                    "target_altitude_m": 45.0,
                },
            }
        },
        snapshot={
            "operator_recovery_request_observed": True,
            "operator_recovery_action": "return_to_launch",
            "operator_recovery_command_ack_observed": True,
            "operator_recovery_command_ack_result": 0,
            "post_abort_tracking": True,
            "post_abort_outcome_status": "return_progress_observed",
        },
    )

    assert line is not None
    assert "action=avoid_obstacle" in line
    assert "target_altitude_m=45.0" in line
    assert "outcome=return_progress_observed" in line


def test_operator_dispatch_summary_surfaces_probe_maneuver_evidence_after_return() -> None:
    line = missionos_cli._operator_recovery_dispatch_status_text(
        artifacts={
            "missionos_runtime_recovery_dispatch_receipt": {
                "dispatch_status": "queued_for_active_runner",
                "recovery_action": "avoid_obstacle",
                "active_runner_request_queued": True,
                "recovery_parameters": {
                    "target_x_m": 99.929,
                    "target_y_m": 72.863,
                    "target_altitude_m": 45.0,
                },
            },
            "missionos_auto_mission_probe_observed": {
                "monitor": {
                    "terminal_snapshot": {
                        "operator_recovery_action": "avoid_obstacle",
                        "operator_recovery_assist_status": "target_reached",
                        "operator_recovery_target_reached": True,
                        "operator_recovery_resume_auto_status": "resumed_auto_mission",
                    }
                }
            },
        },
        snapshot={
            "operator_recovery_request_observed": True,
            "operator_recovery_action": "return_to_launch",
            "operator_recovery_command_ack_observed": True,
            "operator_recovery_command_ack_result": 0,
            "post_abort_tracking": True,
            "post_abort_outcome_status": "return_progress_observed",
        },
    )

    assert line is not None
    assert "action=avoid_obstacle" in line
    assert "maneuver=avoid_obstacle" in line
    assert "assist=target_reached" in line
    assert "target=True" in line
    assert "resume=resumed_auto_mission" in line


def test_operator_maneuver_window_handles_stale_approved_setpoints() -> None:
    assert auto_probe.OPERATOR_RECOVERY_ASSIST_MAX_SECONDS >= 30.0


def _obstacle_recovery_map_payload() -> dict:
    return {
        "task_id": "task_obstacle_map_layers",
        "status": "running",
        "artifacts": {
            "mission_designer_coordinate_pair_route": {
                "takeoff_latitude": 35.681236,
                "takeoff_longitude": 139.767125,
                "dropoff_latitude": 35.6979189,
                "dropoff_longitude": 139.7754511,
                "landing_zone_blocked": True,
            },
            "missionos_auto_mission_compilation": {
                "planned_route_m": 2001.0,
                "mission_items": [
                    {
                        "seq": 0,
                        "command": 22,
                        "latitude_deg": 35.681236,
                        "longitude_deg": 139.767125,
                        "altitude_m": 30.0,
                    },
                    {
                        "seq": 1,
                        "command": 16,
                        "latitude_deg": 35.6895,
                        "longitude_deg": 139.7710,
                        "altitude_m": 30.0,
                    },
                    {
                        "seq": 2,
                        "command": 19,
                        "latitude_deg": 35.6979189,
                        "longitude_deg": 139.7754511,
                        "altitude_m": 30.0,
                    },
                    {
                        "seq": 3,
                        "command": 21,
                        "latitude_deg": 35.6979189,
                        "longitude_deg": 139.7754511,
                        "altitude_m": 0.0,
                    },
                ],
            },
            "missionos_auto_mission_runtime_snapshot": {
                "local_x_m": 80.0,
                "local_y_m": 40.0,
                "altitude_above_home_m": 45.0,
                "operator_recovery_action": "avoid_obstacle",
                "operator_recovery_target_reached": True,
                "operator_recovery_resume_auto_status": "resumed_auto_mission",
            },
            "missionos_auto_mission_runtime_replay": {
                "flight_path_profile": [
                    {
                        "sample_index": 0,
                        "phase": "prepared",
                        "local_x_m": 0.0,
                        "local_y_m": 0.0,
                        "relative_alt_m": 0.0,
                    },
                    {
                        "sample_index": 1,
                        "phase": "operator_recovery",
                        "local_x_m": 40.0,
                        "local_y_m": 20.0,
                        "relative_alt_m": 45.0,
                    },
                    {
                        "sample_index": 2,
                        "phase": "auto_mission_resumed",
                        "local_x_m": 80.0,
                        "local_y_m": 40.0,
                        "relative_alt_m": 45.0,
                    },
                ],
            },
            "missionos_auto_mission_probe_observed": {
                "gazebo_obstacle_application": {
                    "gazebo_obstacle_model_spawned": True,
                    "obstacle_manifest": {
                        "gazebo_obstacle_model_spawned": True,
                        "obstacles": [
                            {
                                "name": "missionos_landing_zone_blocker",
                                "kind": "building_box",
                                "source": "landing_zone_blocked",
                                "x_m": 1855.054,
                                "y_m": 752.02,
                                "z_m": 10.0,
                                "size_x_m": 18.0,
                                "size_y_m": 18.0,
                                "size_z_m": 20.0,
                            }
                        ],
                    },
                },
                "monitor": {
                    "operator_recovery": {
                        "command": {
                            "status": "target_reached",
                            "recovery_path": "SET_POSITION_TARGET_LOCAL_NED:avoid_obstacle",
                            "target": {
                                "target_x_m": 40.0,
                                "target_y_m": 20.0,
                                "target_z_m": -45.0,
                            },
                            "target_reached": True,
                            "target_distance_m": 0.7,
                            "resume_auto_status": "resumed_auto_mission",
                            "maneuver_observation_samples": [
                                {
                                    "x_m": 30.0,
                                    "y_m": 15.0,
                                    "altitude_above_home_m": 35.0,
                                    "distance_to_target_m": 11.0,
                                },
                                {
                                    "x_m": 40.0,
                                    "y_m": 20.0,
                                    "altitude_above_home_m": 45.0,
                                    "distance_to_target_m": 0.7,
                                },
                            ],
                        }
                    }
                },
            },
        },
    }


def test_mission_map_model_separates_plan_observed_avoidance_and_obstacles() -> None:
    model = missionos_cli._mission_map_model(
        task_payload=_obstacle_recovery_map_payload(),
        provider="osm",
        live_task_url=None,
    )

    assert len(model["planned_points"]) == 4
    assert len(model["observed_points"]) == 3
    assert model["latest"]["phase"] == "auto_mission_resumed"
    assert model["obstacles"][0]["name"] == "missionos_landing_zone_blocker"
    assert model["obstacles"][0]["spawned"] is True
    assert model["avoidance"]["action"] == "avoid_obstacle"
    assert model["avoidance"]["target"]["x_m"] == 40.0
    assert len(model["avoidance"]["samples"]) == 2
    assert model["avoidance"]["target_reached"] is True
    assert model["avoidance"]["resume_auto_status"] == "resumed_auto_mission"


def test_mission_map_html_and_watch_surface_obstacle_layers() -> None:
    payload = _obstacle_recovery_map_payload()
    artifacts = payload["artifacts"]
    model = missionos_cli._mission_map_model(
        task_payload=payload,
        provider="osm",
        live_task_url=None,
    )
    html = missionos_cli._mission_map_html(model)

    assert "planned-path" in html
    assert "observed-path" in html
    assert "avoidance-path" in html
    assert "marker-obstacle" in html
    assert "initial plan" in html
    assert "observed trajectory" in html

    console = Console(record=True, color_system=None, width=120)
    console.print(
        missionos_cli._render_flight_map(
            trail=[(0.0, 0.0), (40.0, 20.0), (80.0, 40.0)],
            snapshot=artifacts["missionos_auto_mission_runtime_snapshot"],
            artifacts=artifacts,
            status="running",
            task_id="task_obstacle_map_layers",
        )
    )
    rendered = console.export_text()
    assert "p=initial plan" in rendered
    assert "O=obstacle" in rendered
    assert "avoid=target_reached" in rendered
    assert "obstacles=1(spawned)" in rendered
