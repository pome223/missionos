from __future__ import annotations

import re
from pathlib import Path
import shlex
import sys
from typing import Any, Mapping

import src.gateway.server as gateway_server
from scripts import smoke_missionos_chat_turtlebot3_home_mission as tb3_chat_smoke
from src.runtime.task_store import TaskStore
from src.runtime.ros2_nav2_dispatch_bridge import (
    ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV,
    ROS2_NAV2_BRIDGE_COMMAND_ENV,
)


JAPANESE_TEXT = re.compile(r"[ぁ-んァ-ン一-龥]")


def test_turtlebot3_e2e_smoke_uses_explicit_authority_route_hints(
    monkeypatch: Any,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_post_conversation(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"mission_designer": {"context_ref": len(calls)}}

    monkeypatch.setattr(
        tb3_chat_smoke,
        "_post_conversation",
        fake_post_conversation,
    )

    tb3_chat_smoke._run_chat_flow(
        base_url="http://127.0.0.1:18791",
        session_id="contract-e2e-route-hints",
        instruction="TurtleBot3で家の中を一周して",
    )

    assert [call.get("route_hint") for call in calls] == [
        None,
        "approve",
        "execute",
    ]


def _install_quiet_conversation_dependencies(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        gateway_server,
        "run_missionos_agent_runtime",
        lambda **_kwargs: {
            "runtime_status": "not_configured",
            "agent_invocations": [],
            "monitoring_observations": [],
        },
    )
    monkeypatch.setattr(
        gateway_server,
        "run_llm_dialogue_router",
        lambda *_args, **_kwargs: {"router_status": "not_configured"},
    )
    monkeypatch.setattr(gateway_server, "build_form2a_response_selection_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_form2a_operator_review_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_form2a_action_consumption_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_llm_repair_planner_summary", lambda: {})


def _registered_plan_context(session_id: str) -> dict[str, Any]:
    return gateway_server._missionos_register_mission_designer_context(
        {
            "scenario_proposal": {
                "proposal_id": "proposal_language_test",
                "mission_objective": "Source-backed route language test",
            },
            "validation_result": {"validation_status": "accepted"},
            "summary": {"mission_objective": "Source-backed route language test"},
        },
        session_id=session_id,
    )


def _fake_approval_result(
    *,
    proposal: Mapping[str, Any],
    validation: Mapping[str, Any],
    now: Any | None = None,
) -> dict[str, Any]:
    del proposal, validation, now
    return {
        "scenario_approval": {"approval_status": "approved"},
        "scenario_compile_result": {"compile_status": "compiled"},
        "bounded_simulation_request": {"request_status": "prepared_for_operator"},
        "summary": {
            "approval_status": "approved",
            "operator_approved": True,
            "approved_for_bounded_simulation": True,
            "gazebo_execution_invoked": False,
            "progress_counted": False,
        },
    }


def _write_turtlebot3_success_bridge(
    path: Path,
    *,
    obstacle_avoidance_observed: bool = False,
) -> None:
    obstacle = "True" if obstacle_avoidance_observed else "False"
    path.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "payload = request.get('payload') or {}\n"
        "assert request.get('physical_execution_invoked') is False\n"
        "assert request.get('raw_velocity_allowed') is False\n"
        "assert request.get('raw_ros_topic_publication_allowed') is False\n"
        "print(json.dumps({\n"
        "    'physical_execution_invoked': False,\n"
        "    'raw_velocity_published': False,\n"
        "    'raw_ros_topic_published': False,\n"
        "    'cmd_vel_published_by_missionos': False,\n"
        "    'ack_status': 'accepted',\n"
        "    'ack_source': 'fixture_nav2_navigate_to_pose',\n"
        "    'goal_x_m': payload.get('x_m'),\n"
        "    'runtime_progress_observed': True,\n"
        "    'completion_observed': True,\n"
        "    'nav2_status': 'succeeded',\n"
        "    'state_result': {\n"
        "        'pose_observed': True,\n"
        "        'robot_motion_observed': True,\n"
        "        'odom_topic': '/odom',\n"
        "        'odom_delta_m': 0.26,\n"
        "        'costmap_obstacle_observed': "
        + obstacle
        + ",\n"
        "        'obstacle_avoidance_observed': "
        + obstacle
        + ",\n"
        "        'trajectory_result': {\n"
        "            'trajectory_lateral_deviation_observed': "
        + obstacle
        + ",\n"
        "            'max_lateral_deviation_m': 0.12 if "
        + obstacle
        + " else None,\n"
        "            'trajectory_samples': [\n"
        "                {'x_m': -2.0, 'y_m': -0.5, 'sample_index': 0},\n"
        "                {'x_m': payload.get('x_m') / 2.0, 'y_m': payload.get('y_m') or 0.0, 'sample_index': 1},\n"
        "                {'x_m': payload.get('x_m'), 'y_m': payload.get('y_m') or 0.0, 'sample_index': 2},\n"
        "            ],\n"
        "        },\n"
        "    },\n"
        "    'progress_result': {\n"
        "        'runtime_progress_observed': True,\n"
        "        'completion_observed': True,\n"
        "        'robot_motion_observed': True,\n"
        "        'nav2_status': 'succeeded',\n"
        "        'costmap_obstacle_observed': "
        + obstacle
        + ",\n"
        "        'obstacle_avoidance_observed': "
        + obstacle
        + ",\n"
        "    },\n"
        "}))\n",
        encoding="utf-8",
    )


def _bridge_command(path: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"


def test_chat_approve_and_prepare_messages_are_english(monkeypatch: Any) -> None:
    _install_quiet_conversation_dependencies(monkeypatch)
    monkeypatch.setattr(
        gateway_server,
        "approve_px4_gazebo_mission_scenario_for_bounded_simulation",
        _fake_approval_result,
    )

    session_id = "chat-language-session"
    plan_context = _registered_plan_context(session_id)

    approval = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "approve",
            "missionos_route_hint": "approve",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": plan_context,
        }
    )

    assert approval["routed_action"] == "approve"
    assert "Approval recorded." in approval["message"]
    assert not JAPANESE_TEXT.search(approval["message"])

    def fake_prepare(context: Mapping[str, Any]) -> dict[str, Any]:
        summary = context.get("summary") if isinstance(context.get("summary"), Mapping) else {}
        return gateway_server._missionos_register_mission_designer_context(
            {
                **dict(context),
                "sitl_execution_request": {"request_status": "prepared"},
                "summary": {
                    **dict(summary),
                    "sitl_execution_task_id": "task_chat_language",
                    "progress_counted": False,
                },
            },
            session_id=session_id,
        )

    monkeypatch.setattr(
        gateway_server,
        "_missionos_prepare_mission_designer_sitl_context",
        fake_prepare,
    )

    prepared = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "execute",
            "missionos_route_hint": "execute",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": approval["mission_designer"],
        }
    )

    assert prepared["routed_action"] == "execute"
    assert "SITL execution request prepared" in prepared["message"]
    assert not JAPANESE_TEXT.search(prepared["message"])


def test_free_form_sensitive_words_cannot_create_authority(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        gateway_server,
        "run_missionos_agent_runtime",
        lambda **kwargs: {
            "runtime_status": "proposal_guardrail_passed",
            "proposal": {
                "intent": (
                    "execute"
                    if "実行" in str(kwargs.get("utterance") or "")
                    else "approve"
                ),
                "operator_instruction": kwargs.get("utterance"),
                "specialist_agent": "missionos_response_planner_agent",
            },
            "agent_invocations": [],
            "monitoring_observations": [],
        },
    )
    monkeypatch.setattr(gateway_server, "build_form2a_response_selection_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_form2a_operator_review_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_form2a_action_consumption_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_llm_repair_planner_summary", lambda: {})
    monkeypatch.setattr(
        gateway_server,
        "approve_px4_gazebo_mission_scenario_for_bounded_simulation",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("free-form text must not reach approval")
        ),
    )

    plan_context = _registered_plan_context("negative-sensitive-intent")
    for instruction in (
        "まだ承認しないで",
        "実行しないでください",
        "please don't approve this",
        "承認って何ですか？",
    ):
        response = gateway_server.run_missionos_autonomy_conversation(
            {
                "operator_instruction": instruction,
                "missionos_client_surface": "chat",
                "session_id": "negative-sensitive-intent",
                "mission_designer_context": plan_context,
            }
        )

        assert response["routed_action"] == "clarification"
        assert response["progress_counted"] is False


def test_chat_turtlebot3_home_mission_reaches_nav2_bridge_without_false_claims(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _install_quiet_conversation_dependencies(monkeypatch)
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(gateway_server, "get_task_store", lambda: task_store)
    bridge = tmp_path / "turtlebot3_bridge.py"
    _write_turtlebot3_success_bridge(bridge)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))

    session_id = "chat-turtlebot3-session"
    plan = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "TurtleBot3で家の中を一周して",
            "missionos_client_surface": "chat",
            "session_id": session_id,
        }
    )

    assert plan["routed_action"] == "mission_designer_plan"
    assert "TurtleBot3 home mission proposal" in plan["message"]
    plan_context = plan["mission_designer"]
    assert plan_context["turtlebot3_home_mission_plan"]["mission_kind"] == (
        "indoor_patrol_leg"
    )
    assert plan_context["summary"]["llm_recovery_proposals_allowed"] is True
    assert plan_context["summary"]["proposal_first_classification"] is True
    assert plan_context["summary"]["dispatch_request_sent"] is False
    assert "cleaning_completion" in plan_context["summary"]["blocked_claims"]

    approved = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "approve",
            "missionos_route_hint": "approve",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": plan_context,
        }
    )

    assert approved["routed_action"] == "approve"
    assert "Approval recorded for the TurtleBot3 home mission leg" in approved[
        "message"
    ]

    executed = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "run",
            "missionos_route_hint": "execute",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": approved["mission_designer"],
        }
    )

    summary = executed["operation_result"]["summary"]
    execution = executed["operation_result"]["turtlebot3_home_mission_execution"]
    task_id = summary["task_id"]
    assert executed["routed_action"] == "execute"
    assert summary["status"] == "completed"
    assert task_id.startswith("task_")
    assert summary["turtlebot3_home_mission_task_id"] == task_id
    assert summary["dispatch_request_sent"] is True
    assert summary["completion_claimed"] is True
    assert summary["completion_scope"] == "sim_action"
    assert summary["robot_motion_observed"] is True
    assert summary["turtlebot3_indoor_map_model"]["map_kind"] == "indoor_local_xy"
    assert summary["physical_execution_invoked"] is False
    assert execution["whole_home_loop_completion_claimed"] is False
    assert execution["cleaning_completion_claimed"] is False
    assert execution["payload_delivery_completion_claimed"] is False
    task = task_store.get(task_id)
    assert task is not None
    assert task["kind"] == "turtlebot3_home_mission_execution"
    assert task["artifacts"]["turtlebot3_indoor_map_model"]["map_kind"] == (
        "indoor_local_xy"
    )
    decision_summary = task["artifacts"]["turtlebot3_recovery_decision_summary"]
    assert decision_summary["schema_version"] == (
        "missionos_turtlebot3_recovery_decision_summary.v1"
    )
    assert decision_summary["read_only"] is True
    assert decision_summary["judgment_required"] is False
    assert decision_summary["llm_recovery_judgment_count"] == 0
    assert decision_summary["fresh_recovery_operator_approval_count"] == 0
    assert decision_summary["decision_summary_creates_dispatch_authority"] is False
    assert decision_summary["physical_execution_invoked"] is False
    assert task["artifacts"]["summary"]["turtlebot3_recovery_decision_summary_ref"] == (
        decision_summary["decision_summary_ref"]
    )


def test_turtlebot3_recovery_decision_summary_records_fresh_operator_approval() -> None:
    execution_result = {
        "summary": {
            "runtime_recovery_triggered": True,
            "runtime_recovery_action_kind": "avoid_obstacle",
            "recovery_dispatch_request_sent": True,
            "recovery_completion_claimed": True,
            "route_resumed_after_recovery": True,
            "route_completed_after_recovery": True,
            "runtime_recovery_motion_context": {"odom_delta_m": 1.2},
            "recovery_planner_status": "proposal_guardrail_passed",
            "completion_scope": "sim_action",
            "completion_claimed": True,
            "mission_delivery_completion_claimed": False,
            "physical_execution_invoked": False,
            "fresh_recovery_operator_approval_count": 1,
            "fresh_recovery_operator_approvals": [
                {
                    "operator_approval_ref": "operator_approval:codex_e2e_recovery",
                    "approval_actor": "codex_e2e_operator",
                    "approved_action": "avoid_obstacle",
                }
            ],
            "recovery_execution_permitted_by_operator_approval": True,
            "recovery_dispatch_authority_source": "fresh_operator_approval",
            "recovery_proposal_classifications": [
                {
                    "execution_class": "requires_human_approval",
                    "requires_new_human_approval": True,
                    "execution_permitted_by_envelope": False,
                    "proposal_allowed": True,
                }
            ],
            "recovery_proposals": [
                {
                    "proposal_source": "llm",
                    "approval_created": False,
                    "input_observations": {
                        "runtime_obstacle_observed": True,
                        "recommended_recovery_action": "avoid_obstacle",
                        "odom_delta_m": 1.2,
                    },
                }
            ],
        }
    }

    result = gateway_server._missionos_attach_turtlebot3_recovery_decision_summary(
        execution_result,
        mission_operator_approval_count=1,
    )

    decision_summary = result["turtlebot3_recovery_decision_summary"]
    assert decision_summary["judgment_required"] is True
    assert decision_summary["trigger"] == "runtime_obstacle"
    assert decision_summary["llm_recovery_judgment_count"] == 1
    assert decision_summary["mission_operator_approval_count"] == 1
    assert decision_summary["fresh_recovery_operator_approval_count"] == 1
    assert decision_summary["operator_approval_created_for_recovery"] is True
    assert decision_summary["operator_approval_reused_for_recovery"] is False
    assert decision_summary["rules_execution_class"] == "requires_human_approval"
    assert decision_summary["requires_new_human_approval"] is True
    assert decision_summary["execution_permitted_by_envelope"] is False
    assert decision_summary["recovery_execution_permitted_by_operator_approval"] is True
    assert decision_summary["recovery_dispatch_authority_source"] == (
        "fresh_operator_approval"
    )
    assert decision_summary["decision_summary_creates_dispatch_authority"] is False
    assert decision_summary["mission_delivery_completion_claimed"] is False
    assert decision_summary["physical_execution_invoked"] is False
    assert result["summary"]["turtlebot3_recovery_decision_summary_ref"] == (
        decision_summary["decision_summary_ref"]
    )


def test_chat_turtlebot3_cleaning_request_plans_inspection_not_cleaning(
    monkeypatch: Any,
) -> None:
    _install_quiet_conversation_dependencies(monkeypatch)

    response = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "家の中を掃除して",
            "missionos_client_surface": "chat",
            "session_id": "chat-turtlebot3-cleaning",
        }
    )

    mission = response["mission_designer"]["turtlebot3_home_mission_plan"]
    summary = response["mission_designer"]["summary"]
    assert response["routed_action"] == "mission_designer_plan"
    assert mission["mission_kind"] == "cleaning_inspection_leg"
    assert "cleaning_completion" in mission["blocked_claims"]
    assert "vacuum_or_brush_actuator_invoked" in mission["blocked_claims"]
    assert summary["completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False


def test_chat_nova_carter_profile_records_isaac_execution_target(
    monkeypatch: Any,
) -> None:
    _install_quiet_conversation_dependencies(monkeypatch)

    response = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "短いNav2ルートを走って",
            "robot_profile": "nova_carter",
            "missionos_client_surface": "chat",
            "session_id": "chat-nova-carter-profile",
        }
    )

    mission = response["mission_designer"]["turtlebot3_home_mission_plan"]
    summary = response["mission_designer"]["summary"]
    assert response["routed_action"] == "mission_designer_plan"
    assert "Nova Carter home mission proposal" in response["message"]
    assert mission["robot_profile"] == "nova_carter"
    assert mission["robot_label"] == "Nova Carter"
    assert mission["execution_target"] == "isaac_ros_nav2_nova_carter_sim"
    assert summary["runtime_substrate"] == "NVIDIA Isaac Sim + Isaac ROS/Nav2"
    assert summary["completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False


def test_chat_turtlebot4_request_builds_turtlebot4_nav2_plan(
    monkeypatch: Any,
) -> None:
    _install_quiet_conversation_dependencies(monkeypatch)

    response = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "TurtleBot4で家の中を一周して",
            "missionos_client_surface": "chat",
            "session_id": "chat-turtlebot4-plan",
        }
    )

    mission = response["mission_designer"]["turtlebot3_home_mission_plan"]
    summary = response["mission_designer"]["summary"]
    assert response["routed_action"] == "mission_designer_plan"
    assert mission["robot_profile"] == "turtlebot4"
    assert mission["robot_model"] == "turtlebot4_lite"
    assert mission["execution_target"] == "ros2_nav2_turtlebot4_sim"
    assert summary["robot_profile"] == "turtlebot4"
    assert "TurtleBot4" in response["message"]
    assert summary["dispatch_request_sent"] is False
    assert summary["physical_execution_invoked"] is False


def test_chat_turtlebot3_low_battery_blocks_dispatch(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _install_quiet_conversation_dependencies(monkeypatch)
    bridge = tmp_path / "bridge_should_not_run.py"
    bridge.write_text("raise SystemExit('bridge should not run')\n", encoding="utf-8")
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))

    session_id = "chat-turtlebot3-low-battery"
    plan = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "TurtleBot3で家の中を一周して。バッテリーが足りない",
            "missionos_client_surface": "chat",
            "session_id": session_id,
        }
    )
    approved = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "approve",
            "missionos_route_hint": "approve",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": plan["mission_designer"],
        }
    )
    executed = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "run",
            "missionos_route_hint": "execute",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": approved["mission_designer"],
        }
    )

    summary = executed["operation_result"]["summary"]
    assert summary["dispatch_request_sent"] is False
    assert summary["completion_claimed"] is False
    assert summary["recovery_action_suggested"] == "return_home"
    assert summary["recovery_execution_permitted_by_envelope"] is True
    assert summary["recovery_dispatch_request_sent"] is False
    assert summary["recovery_proposal_classifications"][0]["proposal_allowed"] is True
    assert summary["recovery_proposal_classifications"][0]["execution_class"] == (
        "auto_executable"
    )
    assert "battery_below_minimum_required" in summary["blocking_reasons"]
    assert summary["physical_execution_invoked"] is False


def test_chat_turtlebot3_obstacle_mission_requires_avoidance_observation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _install_quiet_conversation_dependencies(monkeypatch)
    bridge = tmp_path / "turtlebot3_bridge.py"
    _write_turtlebot3_success_bridge(bridge, obstacle_avoidance_observed=False)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))

    session_id = "chat-turtlebot3-obstacle"
    plan = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "TurtleBot3で家の中の障害物を避けて",
            "missionos_client_surface": "chat",
            "session_id": session_id,
        }
    )
    approved = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "approve",
            "missionos_route_hint": "approve",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": plan["mission_designer"],
        }
    )
    executed = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "run",
            "missionos_route_hint": "execute",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": approved["mission_designer"],
        }
    )

    summary = executed["operation_result"]["summary"]
    assert summary["nav2_action_completion_claimed"] is True
    assert summary["completion_claimed"] is False
    assert summary["obstacle_challenge_required"] is True
    assert summary["obstacle_avoidance_completion_claimed"] is False
    assert "obstacle_avoidance_not_observed" in summary["blocking_reasons"]


def test_chat_turtlebot3_followup_adds_obstacle_judgment_without_repeating_robot(
    monkeypatch: Any,
) -> None:
    _install_quiet_conversation_dependencies(monkeypatch)

    session_id = "chat-turtlebot3-followup"
    plan = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "TurtleBot3で家の中を一周して",
            "missionos_client_surface": "chat",
            "session_id": session_id,
        }
    )
    revised = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "障害物を登場させて避ける判断ポイントも入れて",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": plan["mission_designer"],
        }
    )

    summary = revised["mission_designer"]["summary"]
    assert revised["routed_action"] == "mission_designer_plan"
    assert summary["home_robot_mission_kind"] == "obstacle_avoidance_patrol_leg"
    assert summary["obstacle_scenario"]["obstacle_challenge_requested"] is True
    assert summary["ai_judgment_points"][1]["judgment_kind"] == "obstacle_avoidance"


def test_chat_status_prompts_are_english(monkeypatch: Any) -> None:
    _install_quiet_conversation_dependencies(monkeypatch)
    monkeypatch.setattr(
        gateway_server,
        "approve_px4_gazebo_mission_scenario_for_bounded_simulation",
        _fake_approval_result,
    )

    session_id = "chat-language-status-session"
    plan_context = _registered_plan_context(session_id)

    waiting_for_approval = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "status",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": plan_context,
        }
    )

    assert "Type `/approve`" in waiting_for_approval["message"]
    assert not JAPANESE_TEXT.search(waiting_for_approval["message"])

    approved = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "approve",
            "missionos_route_hint": "approve",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": plan_context,
        }
    )
    waiting_for_prepare = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "status",
            "missionos_client_surface": "chat",
            "session_id": session_id,
            "mission_designer_context": approved["mission_designer"],
        }
    )

    assert "Type `/run`" in waiting_for_prepare["message"]
    assert not JAPANESE_TEXT.search(waiting_for_prepare["message"])


def test_chat_repair_intent_hands_off_to_repair_capability(monkeypatch: Any) -> None:
    repair_contexts: list[Mapping[str, Any]] = []

    monkeypatch.setattr(
        gateway_server,
        "run_missionos_agent_runtime",
        lambda **_kwargs: {
            "runtime_status": "proposal_guardrail_passed",
            "proposal": {
                "intent": "repair",
                "operator_instruction": "diagnose the blocked mission",
                "specialist_agent": "missionos_repair_planner_agent",
            },
            "agent_invocations": [
                {
                    "agent_name": "missionos_chief_agent",
                    "provider": "google_adk_gemini",
                    "artifact_path": "missionos_agent_invocations/chief.json",
                },
                {
                    "agent_name": "missionos_repair_planner_agent",
                    "provider": "google_adk_gemini",
                    "artifact_path": "missionos_agent_invocations/repair.json",
                },
                {
                    "agent_name": "missionos_safety_critic_agent",
                    "provider": "google_adk_gemini",
                    "artifact_path": "missionos_agent_invocations/critic.json",
                },
            ],
            "monitoring_observations": [],
        },
    )
    monkeypatch.setattr(
        gateway_server,
        "run_llm_dialogue_router",
        lambda *_args, **_kwargs: {"router_status": "not_configured"},
    )
    monkeypatch.setattr(gateway_server, "build_form2a_response_selection_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_form2a_operator_review_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_form2a_action_consumption_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_llm_repair_planner_summary", lambda: {})

    def fake_repair_planner(
        *,
        capability_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        repair_contexts.append(capability_context)
        return {
            "summary_status": "proposal_guardrail_passed",
            "planner_status": "proposal_guardrail_passed",
            "proposal": {"repair_target": "collect_more_runtime_evidence"},
        }

    monkeypatch.setattr(
        gateway_server,
        "run_llm_repair_planner_from_latest_evidence",
        fake_repair_planner,
    )

    response = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "repair this blocked mission",
            "missionos_client_surface": "chat",
            "session_id": "repair-handoff-session",
        }
    )

    assert response["routed_action"] == "repair"
    assert "Repair Agent" in response["message"]
    assert response["operation_result"]["capability_surface"]["capability_id"] == (
        "llm_repair_planning"
    )
    assert response["operation_result"]["capability_surface"]["coordinating_agent"] == (
        "missionos_repair_planner_agent"
    )
    assert response["operation_result"]["repair_agent_handoff"]["repair_phase"] == (
        "post_block_or_next_run_planning"
    )
    assert response["operation_result"]["repair_agent_handoff"]["input_scope"] == (
        "latest_blocked_or_failed_evidence"
    )
    assert response["missionos_repair_agent_capability_handoff"]["capability_id"] == (
        "llm_repair_planning"
    )
    assert response["repair"]["repair_agent_handoff"]["dispatch_authority_created"] is False

    assert len(repair_contexts) == 1
    context = repair_contexts[0]
    assert context["requested_by"] == "missionos_chief_agent"
    assert context["specialist_agent_invocation_ref"].endswith(
        "missionos_agent_invocations/repair.json"
    )


def test_chat_repair_uses_current_mission_designer_context(monkeypatch: Any) -> None:
    evidence_payloads: list[Mapping[str, Any]] = []

    monkeypatch.setattr(
        gateway_server,
        "run_missionos_agent_runtime",
        lambda **_kwargs: {
            "runtime_status": "proposal_guardrail_passed",
            "proposal": {
                "intent": "repair",
                "operator_instruction": "repair the heavy wind plan",
                "specialist_agent": "missionos_repair_planner_agent",
            },
            "agent_invocations": [
                {
                    "agent_name": "missionos_chief_agent",
                    "provider": "google_adk_gemini",
                    "artifact_path": "missionos_agent_invocations/chief.json",
                },
                {
                    "agent_name": "missionos_repair_planner_agent",
                    "provider": "google_adk_gemini",
                    "artifact_path": "missionos_agent_invocations/repair.json",
                },
                {
                    "agent_name": "missionos_safety_critic_agent",
                    "provider": "google_adk_gemini",
                    "artifact_path": "missionos_agent_invocations/critic.json",
                },
            ],
            "monitoring_observations": [],
        },
    )
    monkeypatch.setattr(
        gateway_server,
        "run_llm_dialogue_router",
        lambda *_args, **_kwargs: {"router_status": "not_configured"},
    )
    monkeypatch.setattr(gateway_server, "build_form2a_response_selection_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_form2a_operator_review_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_form2a_action_consumption_summary", lambda: {})
    monkeypatch.setattr(gateway_server, "build_llm_repair_planner_summary", lambda: {})
    monkeypatch.setattr(
        gateway_server,
        "run_llm_repair_planner_from_latest_evidence",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("latest evidence path should not be used")
        ),
    )

    def fake_context_repair_planner(
        *,
        evidence_artifact: Mapping[str, Any],
        evidence_label: str,
        capability_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        del capability_context
        evidence_payloads.append(evidence_artifact)
        return {
            "summary_status": "blocked",
            "repair_proposal": {
                "planner_status": "not_configured",
                "repair_target": "",
            },
            "input_evidence": {"artifact_path": "missionos_repair_input_evidence.json"},
            "evidence_label": evidence_label,
        }

    monkeypatch.setattr(
        gateway_server,
        "run_llm_repair_planner_from_evidence_payload",
        fake_context_repair_planner,
    )

    registered_context = gateway_server._missionos_register_mission_designer_context(
        {
            "scenario_proposal": {"proposal_id": "proposal_heavy_wind"},
            "validation_result": {"validation_status": "accepted"},
            "mission_designer_coordinate_pair_route": {
                "takeoff_label": "Tokyo Station",
                "dropoff_label": "Akihabara",
                "wind_speed_mps": 14.0,
                "payload_weight_kg": 4.0,
                "requested_total_payload_weight_kg": 8.0,
            },
            "missionos_payload_split_plan": {
                "plan_status": "split_required",
                "requested_payload_weight_kg": 8.0,
                "sortie_count": 2,
            },
            "summary": {"sitl_execution_task_id": "task_heavy_wind"},
        },
        session_id="repair-context-session",
    )

    response = gateway_server.run_missionos_autonomy_conversation(
        {
            "operator_instruction": "repair this heavy payload and wind case",
            "missionos_client_surface": "chat",
            "session_id": "repair-context-session",
            "mission_designer_context": registered_context,
        }
    )

    assert response["routed_action"] == "repair"
    assert "current Mission Designer evidence" in response["message"]
    assert response["operation_result"]["repair_agent_handoff"]["input_scope"] == (
        "mission_designer_context"
    )
    assert len(evidence_payloads) == 1
    evidence = evidence_payloads[0]
    assert evidence["evidence_label"] == "mission_designer_context"
    assert evidence["task_id"] == "task_heavy_wind"
    assert "payload_split_required" in evidence["blocking_reasons"]
    assert "wind_over_live_sitl_contract" in evidence["blocking_reasons"]
    assert evidence["source_boundary"]["context_ref_verified_server_side"] is True
    repair_warnings = response["operation_result"]["repair_followup_warnings"]
    assert any("Live SITL remains blocked" in warning for warning in repair_warnings)
    assert any("Payload split remains" in warning for warning in repair_warnings)


def test_blocked_mission_designer_context_offers_repair_prompt() -> None:
    context = gateway_server._missionos_register_mission_designer_context(
        {
            "scenario_proposal": {"proposal_id": "proposal_repair_prompt"},
            "validation_result": {"validation_status": "accepted"},
            "mission_designer_coordinate_pair_route": {
                "takeoff_label": "Tokyo Station",
                "dropoff_label": "Akihabara",
                "wind_speed_mps": 14.0,
                "payload_weight_kg": 4.0,
                "requested_total_payload_weight_kg": 8.0,
            },
            "missionos_payload_split_plan": {
                "plan_status": "split_required",
                "requested_payload_weight_kg": 8.0,
                "sortie_count": 2,
            },
        },
        session_id="repair-prompt-session",
    )

    evidence = gateway_server._missionos_repair_evidence_from_mission_designer_context(
        context,
        operator_instruction="Tokyo Station -> Akihabara with heavy payload and strong wind",
    )
    prompt = gateway_server._missionos_repair_prompt_from_evidence(evidence)

    assert evidence["summary_status"] == "blocked"
    assert evidence["blocking_reasons"] == [
        "wind_over_live_sitl_contract",
        "payload_split_required",
    ]
    assert prompt["prompt_status"] == "repair_available"
    assert prompt["suggested_command"] == "/repair"
    assert "Type `/repair`" in prompt["operator_prompt"]
    assert prompt["dispatch_authority_created"] is False
    assert prompt["progress_counted"] is False
