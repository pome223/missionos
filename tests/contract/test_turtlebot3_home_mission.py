from __future__ import annotations

import pytest

import json
from pathlib import Path
import shlex
import sys

from src.intelligence.turtlebot3_recovery_planner import (
    TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV,
    TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV,
    TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV,
)
from src.runtime.ros2_nav2_dispatch_bridge import (
    ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV,
    ROS2_NAV2_BRIDGE_COMMAND_ENV,
)
from src.runtime.turtlebot3_log_collector import TURTLEBOT3_LOG_BUNDLE_PATHS_ENV
from src.runtime.turtlebot3_home_mission import (
    approve_turtlebot3_home_mission_plan,
    build_turtlebot3_home_mission_plan,
    infer_turtlebot3_home_mission_kind,
    instruction_requests_turtlebot3_home_mission,
    run_turtlebot3_home_mission_dispatch,
)
from src.runtime.turtlebot3_telemetry_sidecar import (
    TURTLEBOT3_TELEMETRY_SIDECAR_JSONL_ENV,
)

@pytest.fixture(autouse=True)
def _default_arena_world_profile(monkeypatch):
    """Pin the historical arena world for this module's regression corpus.

    The runtime default is the turtlebot3_house profile; these tests assert
    arena-era routes and geometry, and house-specific tests override the env.
    """

    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "arena")



def _write_success_bridge(
    path: Path,
    *,
    obstacle_avoidance_observed: bool = False,
) -> None:
    obstacle = "True" if obstacle_avoidance_observed else "False"
    path.write_text(
        "import json, math, sys\n"
        "from pathlib import Path\n"
        "request = json.loads(sys.stdin.read())\n"
        "payload = request.get('payload') or {}\n"
        "state_path = Path(__file__).with_suffix('.state.json')\n"
        "if state_path.exists():\n"
        "    state = json.loads(state_path.read_text())\n"
        "else:\n"
        "    state = {'x_m': -2.0, 'y_m': -0.5}\n"
        "start_x = float(state.get('x_m', -2.0))\n"
        "start_y = float(state.get('y_m', -0.5))\n"
        "goal_x = float(payload.get('x_m') or 0.0)\n"
        "goal_y = float(payload.get('y_m') or 0.0)\n"
        "mid_x = (start_x + goal_x) / 2.0\n"
        "mid_y = (start_y + goal_y) / 2.0\n"
        "if "
        + obstacle
        + " and payload.get('label') == 'turtlebot3_validated_home_patrol_leg':\n"
        "    samples = [\n"
        "        {'x_m': start_x, 'y_m': start_y},\n"
        "        {'x_m': -1.15, 'y_m': -0.85},\n"
        "        {'x_m': -0.35, 'y_m': -0.85},\n"
        "        {'x_m': 0.35, 'y_m': -0.55},\n"
        "        {'x_m': goal_x, 'y_m': goal_y},\n"
        "    ]\n"
        "else:\n"
        "    samples = [\n"
        "        {'x_m': start_x, 'y_m': start_y},\n"
        "        {'x_m': mid_x, 'y_m': mid_y},\n"
        "        {'x_m': goal_x, 'y_m': goal_y},\n"
        "    ]\n"
        "for sample_index, sample in enumerate(samples):\n"
        "    sample['sample_index'] = sample_index\n"
        "state_path.write_text(json.dumps({'x_m': goal_x, 'y_m': goal_y}))\n"
        "odom_delta_m = max(math.hypot(goal_x - start_x, goal_y - start_y), 0.05)\n"
        "trajectory = {\n"
        "    'trajectory_lateral_deviation_observed': "
        + obstacle
        + ",\n"
        "    'max_lateral_deviation_m': 0.12 if "
        + obstacle
        + " else None,\n"
        "    'trajectory_samples': samples,\n"
        "}\n"
        "assert request.get('physical_execution_invoked') is False\n"
        "assert request.get('raw_velocity_allowed') is False\n"
        "assert request.get('raw_ros_topic_publication_allowed') is False\n"
        "response = {\n"
        "    'physical_execution_invoked': False,\n"
        "    'raw_velocity_published': False,\n"
        "    'raw_ros_topic_published': False,\n"
        "    'cmd_vel_published_by_missionos': False,\n"
        "    'ack_status': 'accepted',\n"
        "    'ack_source': 'fixture_nav2_navigate_to_pose',\n"
        "    'goal_x_m': goal_x,\n"
        "    'runtime_progress_observed': True,\n"
        "    'completion_observed': True,\n"
        "    'nav2_status': 'succeeded',\n"
        "    'state_result': {\n"
        "        'nav2_action_server_available': True,\n"
        "        'pose_observed': True,\n"
        "        'robot_motion_observed': True,\n"
        "        'odom_topic': '/odom',\n"
        "        'odom_delta_m': odom_delta_m,\n"
        "        'costmap_obstacle_observed': "
        + obstacle
        + ",\n"
        "        'obstacle_avoidance_observed': "
        + obstacle
        + ",\n"
        "        'trajectory_result': trajectory,\n"
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
        "        'trajectory_result': trajectory,\n"
        "    },\n"
        "}\n"
        "print(json.dumps(response))\n",
        encoding="utf-8",
    )


def _write_nav2_failure_bridge(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "payload = request.get('payload') or {}\n"
        "assert request.get('physical_execution_invoked') is False\n"
        "response = {\n"
        "    'physical_execution_invoked': False,\n"
        "    'raw_velocity_published': False,\n"
        "    'raw_ros_topic_published': False,\n"
        "    'cmd_vel_published_by_missionos': False,\n"
        "    'ack_status': 'accepted',\n"
        "    'ack_source': 'fixture_nav2_navigate_to_pose',\n"
        "    'goal_x_m': payload.get('x_m'),\n"
        "    'runtime_progress_observed': True,\n"
        "    'completion_observed': False,\n"
        "    'nav2_status': 'aborted',\n"
        "    'blocking_reasons': ['nav2_goal_result_not_succeeded'],\n"
        "    'state_result': {\n"
        "        'nav2_action_server_available': True,\n"
        "        'pose_observed': True,\n"
        "        'robot_motion_observed': False,\n"
        "        'odom_topic': '/odom',\n"
        "        'odom_delta_m': 0.0,\n"
        "    },\n"
        "    'progress_result': {\n"
        "        'runtime_progress_observed': True,\n"
        "        'completion_observed': False,\n"
        "        'robot_motion_observed': False,\n"
        "        'nav2_status': 'aborted',\n"
        "        'blocking_reasons': ['nav2_goal_result_not_succeeded'],\n"
        "    },\n"
        "}\n"
        "print(json.dumps(response))\n",
        encoding="utf-8",
    )


def _write_intersecting_obstacle_bridge(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "payload = request.get('payload') or {}\n"
        "trajectory = {\n"
        "    'trajectory_lateral_deviation_observed': True,\n"
        "    'max_lateral_deviation_m': 0.12,\n"
        "    'trajectory_samples': [\n"
        "        {'x_m': -2.0, 'y_m': -0.5, 'sample_index': 0},\n"
        "        {'x_m': -1.15, 'y_m': -0.5, 'sample_index': 1},\n"
        "        {'x_m': payload.get('x_m'), 'y_m': payload.get('y_m') or 0.0, 'sample_index': 2},\n"
        "    ],\n"
        "}\n"
        "print(json.dumps({\n"
        "    'physical_execution_invoked': False,\n"
        "    'raw_velocity_published': False,\n"
        "    'raw_ros_topic_published': False,\n"
        "    'cmd_vel_published_by_missionos': False,\n"
        "    'ack_status': 'accepted',\n"
        "    'ack_source': 'fixture_nav2_navigate_to_pose',\n"
        "    'runtime_progress_observed': True,\n"
        "    'completion_observed': True,\n"
        "    'nav2_status': 'succeeded',\n"
        "    'state_result': {\n"
        "        'nav2_action_server_available': True,\n"
        "        'pose_observed': True,\n"
        "        'robot_motion_observed': True,\n"
        "        'odom_topic': '/odom',\n"
        "        'odom_delta_m': 0.26,\n"
        "        'costmap_obstacle_observed': True,\n"
        "        'obstacle_avoidance_observed': True,\n"
        "        'trajectory_result': trajectory,\n"
        "    },\n"
        "    'progress_result': {\n"
        "        'runtime_progress_observed': True,\n"
        "        'completion_observed': True,\n"
        "        'robot_motion_observed': True,\n"
        "        'nav2_status': 'succeeded',\n"
        "        'costmap_obstacle_observed': True,\n"
        "        'obstacle_avoidance_observed': True,\n"
        "        'trajectory_result': trajectory,\n"
        "    },\n"
        "}))\n",
        encoding="utf-8",
    )


def _bridge_command(path: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"


def _write_recovery_planner(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "assert prompt['role_contract']['rules_classify_execution_after_proposal'] is True\n"
        "print(json.dumps({\n"
        "    'selected_action': 'return_home',\n"
        "    'reason': 'LLM judges battery reserve and proposes return_home.',\n"
        "    'input_observations': {\n"
        "        'battery_start_pct': prompt['battery_envelope']['battery_start_pct'],\n"
        "        'minimum_required_pct': prompt['battery_envelope']['minimum_required_pct'],\n"
        "        'distance_to_home_m': prompt['home_distance_envelope']['distance_to_home_m'],\n"
        "        'projected_return_battery_required_pct': prompt['home_distance_envelope']['projected_return_battery_required_pct'],\n"
        "    },\n"
        "}))\n",
        encoding="utf-8",
    )


def _write_failure_recovery_planner(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "failure = prompt['runtime_failure_context']\n"
        "motion = prompt['runtime_motion_context']\n"
        "assert prompt['role_contract']['llm_must_not_dispatch'] is True\n"
        "assert failure['runtime_failure_observed'] is True\n"
        "assert failure['runtime_failure_source'] == 'ros2_nav2_bridge_segment_result'\n"
        "assert motion['odom_delta_m'] == 0.0\n"
        "assert motion['stalled_after_dispatch'] is True\n"
        "print(json.dumps({\n"
        "    'selected_action': 'return_home',\n"
        "    'reason': 'LLM judges source-backed Nav2 segment failure and odom stall.',\n"
        "    'input_observations': {\n"
        "        'runtime_failure_observed': failure['runtime_failure_observed'],\n"
        "        'failed_segment_index': failure['failed_segment_index'],\n"
        "        'failed_segment_label': failure['failed_segment_label'],\n"
        "        'runtime_failure_source': failure['runtime_failure_source'],\n"
        "        'failed_segment_completion_claimed': failure['failed_segment_completion_claimed'],\n"
        "        'failed_segment_blocking_reason_count': failure['failed_segment_blocking_reason_count'],\n"
        "        'recommended_recovery_action': failure['recommended_recovery_action'],\n"
        "        'odom_delta_m': motion['odom_delta_m'],\n"
        "        'robot_motion_observed': motion['robot_motion_observed'],\n"
        "        'stalled_after_dispatch': motion['stalled_after_dispatch'],\n"
        "        'motion_observation_source': motion['motion_observation_source'],\n"
        "        'route_progress_delta_m': motion['route_progress_delta_m'],\n"
        "        'distance_to_home_m': prompt['home_distance_envelope']['distance_to_home_m'],\n"
        "        'distance_to_home_source': prompt['home_distance_envelope']['distance_to_home_source'],\n"
        "    },\n"
        "}))\n",
        encoding="utf-8",
    )


def _write_obstacle_recovery_planner(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "assert prompt['role_contract']['llm_must_not_dispatch'] is True\n"
        "assert prompt['obstacle_scenario']['runtime_obstacle_observed'] is True\n"
        "print(json.dumps({\n"
        "    'selected_action': 'avoid_obstacle',\n"
        "    'reason': 'LLM judges runtime obstacle evidence and proposes avoid_obstacle.',\n"
        "    'input_observations': {\n"
        "        'runtime_obstacle_observed': prompt['obstacle_scenario']['runtime_obstacle_observed'],\n"
        "        'costmap_obstacle_observed': prompt['obstacle_scenario']['costmap_obstacle_observed'],\n"
        "        'runtime_obstacle_source': prompt['obstacle_scenario']['runtime_obstacle_source'],\n"
        "        'recommended_recovery_action': prompt['obstacle_scenario']['recommended_recovery_action'],\n"
        "        'recommended_avoidance_target_x_m': prompt['obstacle_scenario']['recommended_avoidance_target_x_m'],\n"
        "        'recommended_avoidance_target_y_m': prompt['obstacle_scenario']['recommended_avoidance_target_y_m'],\n"
        "    },\n"
        "}))\n",
        encoding="utf-8",
    )


def _write_guardrail_fault_recovery_planner(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "assert prompt['obstacle_scenario']['runtime_obstacle_observed'] is True\n"
        "print(json.dumps({\n"
        "    'selected_action': 'avoid_obstacle',\n"
        "    'reason': 'Malformed fixture claims dispatch authority.',\n"
        "    'dispatch_request_sent': True,\n"
        "    'input_observations': {\n"
        "        'runtime_obstacle_observed': prompt['obstacle_scenario']['runtime_obstacle_observed'],\n"
        "        'fabricated_distance_to_home_m': 123.456,\n"
        "    },\n"
        "}))\n",
        encoding="utf-8",
    )


def _write_sidecar_jsonl(path: Path, *, moved: bool) -> None:
    end_x = 0.35 if moved else 0.0
    path.write_text(
        "\n".join(
            json.dumps(row, sort_keys=True)
            for row in [
                {
                    "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                    "sample_kind": "odom",
                    "captured_at": "2026-07-02T00:00:00+00:00",
                    "topic": "/odom",
                    "position": {"x_m": 0.0, "y_m": 0.0},
                },
                {
                    "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                    "sample_kind": "scan",
                    "captured_at": "2026-07-02T00:00:01+00:00",
                    "topic": "/scan",
                    "min_range_m": 0.45,
                },
                {
                    "schema_version": "missionos_turtlebot3_telemetry_sample.v1",
                    "sample_kind": "odom",
                    "captured_at": "2026-07-02T00:00:02+00:00",
                    "topic": "/odom",
                    "position": {"x_m": end_x, "y_m": 0.0},
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_process_logs(tmp_path: Path) -> dict[str, str]:
    paths = {
        "gazebo": tmp_path / "gazebo.log",
        "nav2": tmp_path / "nav2.log",
        "relay": tmp_path / "relay.log",
        "telemetry_sidecar": tmp_path / "telemetry_sidecar.log",
    }
    paths["gazebo"].write_text("Gazebo started\nworld loaded\n", encoding="utf-8")
    paths["nav2"].write_text(
        "Nav2 active\n"
        "[controller_server]: Received a goal, begin computing control effort.\n"
        "[bt_navigator]: Navigation succeeded\n",
        encoding="utf-8",
    )
    paths["relay"].write_text(
        "relay active\nvelocity_generated_by_missionos=false\n",
        encoding="utf-8",
    )
    paths["telemetry_sidecar"].write_text(
        "telemetry sidecar active\nread_only=true\n",
        encoding="utf-8",
    )
    return {name: str(path) for name, path in paths.items()}


def _write_nav2_failure_process_logs(tmp_path: Path) -> dict[str, str]:
    paths = {
        "gazebo": tmp_path / "gazebo.log",
        "nav2": tmp_path / "nav2.log",
        "relay": tmp_path / "relay.log",
        "telemetry_sidecar": tmp_path / "telemetry_sidecar.log",
    }
    paths["gazebo"].write_text("Gazebo started\nworld loaded\n", encoding="utf-8")
    paths["nav2"].write_text(
        "Nav2 active\n"
        "[controller_server]: Received a goal, begin computing control effort.\n"
        "[controller_server]: Failed to make progress\n"
        "[controller_server]: [follow_path] [ActionServer] Aborting handle.\n"
        "[local_costmap.local_costmap]: Received request to clear entirely the local_costmap\n",
        encoding="utf-8",
    )
    paths["relay"].write_text(
        "relay active\nvelocity_generated_by_missionos=false\n",
        encoding="utf-8",
    )
    paths["telemetry_sidecar"].write_text(
        "telemetry sidecar active\nread_only=true\n",
        encoding="utf-8",
    )
    return {name: str(path) for name, path in paths.items()}


def test_turtlebot3_home_mission_classifier_keeps_questions_non_dispatchable() -> None:
    assert instruction_requests_turtlebot3_home_mission("TurtleBot3で家を一周して")
    assert instruction_requests_turtlebot3_home_mission("TurtleBot4で家を一周して")
    assert instruction_requests_turtlebot3_home_mission("TB4で屋内配送して")
    assert instruction_requests_turtlebot3_home_mission("家の中を掃除して")
    assert instruction_requests_turtlebot3_home_mission("家の中で障害物を避けて")
    assert instruction_requests_turtlebot3_home_mission("家の中で屋内配送して")
    assert instruction_requests_turtlebot3_home_mission("TurtleBot3でバッテリー不足を判断して")
    assert infer_turtlebot3_home_mission_kind("家の中で障害物を避けながら屋内配送して") == (
        "indoor_delivery_route_leg"
    )
    assert infer_turtlebot3_home_mission_kind("家の中で障害物を避けて") == (
        "obstacle_avoidance_patrol_leg"
    )
    assert infer_turtlebot3_home_mission_kind("家の中を掃除して") == (
        "cleaning_inspection_leg"
    )
    assert infer_turtlebot3_home_mission_kind("荷物を運んで") == (
        "payload_transport_rehearsal_leg"
    )
    assert not instruction_requests_turtlebot3_home_mission("今日の天気を教えて")


def test_turtlebot4_plan_keeps_nav2_profile_without_physical_claims() -> None:
    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot4で家の中を一周して",
    )

    proposal = result["scenario_proposal"]
    summary = result["summary"]

    assert proposal["robot_profile"] == "turtlebot4"
    assert proposal["robot_model"] == "turtlebot4_lite"
    assert proposal["execution_target"] == "ros2_nav2_turtlebot4_sim"
    assert proposal["proposal_id"].startswith("turtlebot4_home_")
    assert "TurtleBot4 indoor patrol" in proposal["mission_objective"]
    assert summary["robot_profile"] == "turtlebot4"
    assert summary["dispatch_request_sent"] is False
    assert summary["completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False


def test_turtlebot3_plan_blocks_cleaning_payload_loop_and_physical_claims() -> None:
    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して",
    )

    proposal = result["scenario_proposal"]
    summary = result["summary"]

    assert proposal["mission_kind"] == "indoor_patrol_leg"
    assert proposal["requires_operator_approval"] is True
    assert proposal["physical_execution_invoked"] is False
    assert proposal["autonomy_envelope"]["operator_approved"] is False
    assert proposal["autonomy_envelope"]["llm_recovery_proposals_allowed"] is True
    assert proposal["autonomy_envelope"]["proposal_first_classification"] is True
    assert proposal["recovery_proposals"] == []
    assert "whole_home_loop_completion" in proposal["blocked_claims"]
    assert "cleaning_completion" in proposal["blocked_claims"]
    assert "payload_delivery_completion" in proposal["blocked_claims"]
    assert summary["dispatch_request_sent"] is False
    assert summary["completion_claimed"] is False
    assert summary["indoor_delivery_route_completion_claimed"] is False
    assert summary["mission_delivery_completion_claimed"] is False


def test_turtlebot3_plan_adds_obstacle_and_battery_judgment_points() -> None:
    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けて。バッテリー80%",
    )

    proposal = result["scenario_proposal"]
    summary = result["summary"]
    points = proposal["ai_judgment_points"]

    assert proposal["mission_kind"] == "obstacle_avoidance_patrol_leg"
    assert proposal["obstacle_scenario"]["obstacle_challenge_requested"] is True
    assert proposal["battery_envelope"]["battery_start_pct"] == 80.0
    assert proposal["battery_envelope"]["dispatch_allowed"] is True
    assert proposal["home_distance_envelope"]["distance_to_home_source_backed"] is True
    assert proposal["autonomy_envelope"]["blocked_actions"] == [
        "raw_velocity",
        "unbounded_move",
        "physical_execution",
        "payload_delivery_completion",
    ]
    assert [point["judgment_kind"] for point in points] == [
        "battery_envelope",
        "obstacle_avoidance",
    ]
    assert points[0]["decision"] == "allow"
    assert points[1]["decision"] == "observe_required"
    assert summary["ai_judgment_points"][1]["required_runtime_observation"] == (
        "obstacle_avoidance_observed"
    )
    assert summary["dispatch_request_sent"] is False


def test_turtlebot3_approval_classifies_low_battery_return_home_recovery() -> None:
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して。バッテリーが足りない",
    )
    proposal = plan["scenario_proposal"]
    assert proposal["recovery_proposals"][0]["proposal_source"] == (
        "deterministic_fallback"
    )
    assert proposal["recovery_proposals"][0]["selected_action"] == "return_home"
    assert proposal["recovery_proposal_classifications"][0]["proposal_allowed"] is True
    assert proposal["recovery_proposal_classifications"][0]["execution_class"] == (
        "requires_human_approval"
    )

    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )

    approved = approval["turtlebot3_home_mission_approval"]
    classification = approved["recovery_proposal_classifications"][0]
    assert approved["autonomy_envelope"]["operator_approved"] is True
    assert approved["llm_recovery_proposals_allowed"] is True
    assert approved["proposal_first_classification"] is True
    assert classification["proposal_allowed"] is True
    assert classification["execution_class"] == "auto_executable"
    assert classification["execution_permitted_by_envelope"] is True
    assert classification["dispatch_authority_created"] is False
    assert classification["physical_execution_invoked"] is False


def test_turtlebot3_plan_uses_llm_recovery_planner_when_configured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    planner = tmp_path / "recovery_planner.py"
    _write_recovery_planner(planner)
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中に配送して。バッテリーが足りない",
    )

    proposal = result["scenario_proposal"]
    recovery = proposal["recovery_proposals"][0]
    classification = proposal["recovery_proposal_classifications"][0]
    assert proposal["home_distance_envelope"]["distance_to_home_m"] == 2.599
    assert proposal["recovery_planner_result"]["planner_status"] == (
        "proposal_guardrail_passed"
    )
    assert recovery["proposal_source"] == "llm"
    assert recovery["llm_judgment_recorded"] is True
    assert recovery["selected_action"] == "return_home"
    assert recovery["input_observations"]["distance_to_home_m"] == 2.599
    assert recovery["llm_invocation_evidence"]["provider"] == "command_override"
    assert classification["proposal_allowed"] is True
    assert classification["execution_class"] == "requires_human_approval"
    assert classification["dispatch_authority_created"] is False


def test_turtlebot3_plan_uses_ollama_recovery_planner_when_configured(
    monkeypatch,
) -> None:
    from src.intelligence import turtlebot3_recovery_planner

    def fake_ollama_response(
        *,
        prompt_text: str,
        model_id: str,
        timeout_seconds: int,
    ) -> str:
        prompt = json.loads(prompt_text)
        assert model_id == "ollama_chat/gemma4:26b"
        assert timeout_seconds == 240
        return json.dumps(
            {
                "selected_action": "return_home",
                "reason": "Gemma judges battery reserve and proposes return_home.",
                "input_observations": {
                    "battery_start_pct": prompt["battery_envelope"][
                        "battery_start_pct"
                    ],
                    "planned_route_distance_m": prompt["battery_envelope"][
                        "planned_route_distance_m"
                    ],
                    "estimated_consumption_pct": prompt["battery_envelope"][
                        "estimated_consumption_pct"
                    ],
                    "minimum_required_pct": prompt["battery_envelope"][
                        "minimum_required_pct"
                    ],
                    "distance_to_home_m": prompt["home_distance_envelope"][
                        "distance_to_home_m"
                    ],
                    "distance_to_home_source": prompt["home_distance_envelope"][
                        "distance_to_home_source"
                    ],
                },
            }
        )

    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, "1")
    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "ollama")
    monkeypatch.setenv("MISSIONOS_OLLAMA_MODEL", "gemma4:26b")
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_TIMEOUT_SECONDS", "240")
    monkeypatch.setattr(
        turtlebot3_recovery_planner,
        "_invoke_ollama_response_text",
        fake_ollama_response,
    )

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中に配送して。バッテリーが足りない",
    )

    proposal = result["scenario_proposal"]
    recovery = proposal["recovery_proposals"][0]
    planner_result = proposal["recovery_planner_result"]
    assert planner_result["planner_status"] == "proposal_guardrail_passed"
    assert planner_result["llm_invocation_evidence"]["provider"] == "ollama_native"
    assert planner_result["llm_invocation_evidence"]["invocation_kind"] == (
        "ollama_chat_json"
    )
    assert recovery["proposal_source"] == "llm"
    assert recovery["llm_judgment_recorded"] is True
    assert recovery["selected_action"] == "return_home"
    assert 7.6 < recovery["input_observations"]["planned_route_distance_m"] < 8.4
    assert 14.0 < recovery["input_observations"]["estimated_consumption_pct"] < 17.0


def test_turtlebot3_execution_blocks_without_bridge_opt_in(monkeypatch) -> None:
    monkeypatch.delenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, raising=False)
    monkeypatch.delenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, raising=False)
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    assert summary["status"] == "blocked"
    assert summary["dispatch_request_sent"] is False
    assert summary["completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False
    assert "ros2_nav2_bridge_command_missing" in summary["blocking_reasons"]
    assert f"{ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV}_not_enabled" in summary[
        "blocking_reasons"
    ]


def test_turtlebot3_low_battery_judgment_blocks_before_bridge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge_should_not_run.py"
    bridge.write_text("raise SystemExit('bridge should not run')\n", encoding="utf-8")
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して。バッテリーが足りない",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    assert summary["status"] == "blocked"
    assert summary["dispatch_request_sent"] is False
    assert summary["completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["recovery_action_suggested"] == "return_home"
    assert summary["recovery_execution_permitted_by_envelope"] is True
    assert summary["recovery_dispatch_request_sent"] is False
    assert summary["recovery_proposal_classifications"][0]["proposal_allowed"] is True
    assert summary["recovery_proposal_classifications"][0]["execution_class"] == (
        "auto_executable"
    )
    assert "battery_below_minimum_required" in summary["blocking_reasons"]


def test_turtlebot3_execution_claims_only_sim_action_with_motion_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(bridge)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    evidence = result["ros2_nav2_hardware_adapter_evidence"]
    review = result["mission_episode_review"]
    assert summary["status"] == "completed"
    assert summary["dispatch_request_sent"] is True
    assert summary["completion_claimed"] is True
    assert summary["completion_scope"] == "sim_action"
    assert summary["mission_episode_review_status"] == "passed"
    assert summary["mission_episode_review_passed"] is True
    assert result["mission_episode_review_ref"].startswith("mission_episode_review:")
    assert execution["mission_episode_review_ref"] == result["mission_episode_review_ref"]
    assert review["status"] == "passed"
    assert review["source_completion_scope"] == "sim_action"
    assert summary["robot_motion_observed"] is True
    assert 2.4 < summary["odom_delta_m"] < 2.8
    assert summary["physical_execution_invoked"] is False
    assert summary["llm_recovery_proposals_allowed"] is True
    assert summary["proposal_first_classification"] is True
    assert summary["mission_delivery_completion_claimed"] is False
    assert execution["whole_home_loop_completion_claimed"] is False
    assert execution["cleaning_completion_claimed"] is False
    assert execution["payload_delivery_completion_claimed"] is False
    assert evidence["completion_scope"] == "sim_action"
    assert "sim_action_completion_not_physical" in evidence["unproven_claims"]


def test_turtlebot3_execution_attaches_process_log_bundle_ref(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(bridge)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(
        TURTLEBOT3_LOG_BUNDLE_PATHS_ENV,
        json.dumps(_write_process_logs(tmp_path)),
    )
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    evidence = result["ros2_nav2_hardware_adapter_evidence"]
    bundle = result["log_bundle_artifacts"]["turtlebot3_log_bundle"]
    assert summary["status"] == "completed"
    assert summary["raw_logs_ref"].startswith("turtlebot3_process_log_bundle:")
    assert summary["raw_logs_ref"] == evidence["raw_logs_ref"]
    assert summary["log_bundle_status"] == "ready"
    assert summary["log_bundle_observed_source_count"] == 4
    assert summary["nav2_log_diagnostics_status"] == "ready"
    assert summary["nav2_log_observed_patterns"] == ["nav2_goal_received"]
    assert summary["nav2_log_failure_hypotheses"] == []
    assert bundle["bundle_status"] == "ready"
    assert bundle["raw_logs_ref"] == summary["raw_logs_ref"]
    assert bundle["raw_logs_included"] is False
    assert bundle["physical_execution_invoked"] is False
    assert (
        result["log_bundle_artifacts"]["nav2_log_diagnostics"][
            "physical_execution_invoked"
        ]
        is False
    )
    assert execution["raw_logs_ref"] == summary["raw_logs_ref"]


def test_turtlebot3_execution_uses_sidecar_motion_when_available(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    sidecar = tmp_path / "telemetry.jsonl"
    _write_success_bridge(bridge)
    _write_sidecar_jsonl(sidecar, moved=True)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_TELEMETRY_SIDECAR_JSONL_ENV, str(sidecar))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    artifacts = result["telemetry_sidecar_artifacts"]
    assert summary["status"] == "completed"
    assert summary["completion_claimed"] is True
    assert summary["robot_motion_observed"] is True
    assert summary["odom_delta_m"] == 0.35
    assert summary["robot_motion_observation_source"] == (
        "ros2_telemetry_sidecar_jsonl"
    )
    assert summary["telemetry_sidecar_required"] is True
    assert summary["telemetry_sidecar_motion_correlation_confirmed"] is True
    assert artifacts["telemetry_sidecar_status"] == "ready"
    assert artifacts["turtlebot3_telemetry_window"]["odom_motion_observed"] is True
    assert artifacts["turtlebot3_state_correlation"][
        "motion_correlation_confirmed"
    ] is True
    assert execution["telemetry_sidecar_artifacts"]["raw_logs_ref"].startswith(
        "turtlebot3_telemetry_sidecar_jsonl:"
    )
    assert summary["physical_execution_invoked"] is False


def test_turtlebot3_execution_does_not_complete_when_sidecar_motion_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    sidecar = tmp_path / "telemetry.jsonl"
    _write_success_bridge(bridge)
    _write_sidecar_jsonl(sidecar, moved=False)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_TELEMETRY_SIDECAR_JSONL_ENV, str(sidecar))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中を一周して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    artifacts = result["telemetry_sidecar_artifacts"]
    assert summary["nav2_action_completion_claimed"] is True
    assert summary["status"] == "blocked"
    assert summary["completion_claimed"] is False
    assert summary["completion_scope"] == "none"
    assert summary["robot_motion_observed"] is False
    assert summary["robot_motion_observation_source"] == (
        "ros2_telemetry_sidecar_jsonl"
    )
    assert summary["telemetry_sidecar_required"] is True
    assert summary["telemetry_sidecar_motion_correlation_confirmed"] is False
    assert "telemetry_sidecar_motion_correlation_not_confirmed" in summary[
        "blocking_reasons"
    ]
    assert "sidecar_motion_not_observed" in summary["blocking_reasons"]
    assert artifacts["telemetry_sidecar_status"] == "blocked"
    assert artifacts["turtlebot3_state_correlation"]["correlation_status"] == (
        "blocked"
    )


def test_turtlebot3_obstacle_mission_needs_avoidance_observation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=False)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中の障害物を避けて",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    assert summary["nav2_action_completion_claimed"] is True
    assert summary["completion_claimed"] is False
    assert summary["completion_scope"] == "none"
    assert summary["obstacle_challenge_required"] is True
    assert summary["obstacle_avoidance_completion_claimed"] is False
    assert "obstacle_avoidance_not_observed" in summary["blocking_reasons"]


def test_turtlebot3_obstacle_mission_claims_completion_with_avoidance_observation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中の障害物を避けて",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    assert summary["status"] == "completed"
    assert summary["completion_claimed"] is True
    assert summary["completion_scope"] == "sim_action"
    assert summary["obstacle_detected"] is True
    assert summary["obstacle_avoidance_observed"] is True
    assert summary["obstacle_avoidance_completion_claimed"] is True
    assert summary["trajectory_lateral_deviation_observed"] is True
    assert summary["max_lateral_deviation_m"] == 0.12
    assert summary["obstacle_trajectory_clearance_observed"] is True
    assert summary["obstacle_trajectory_intersects_obstacle"] is False


def test_turtlebot3_obstacle_mission_blocks_when_trajectory_crosses_obstacle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_intersecting_obstacle_bridge(bridge)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で家の中の障害物を避けて",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    assert summary["nav2_action_completion_claimed"] is True
    assert summary["status"] == "blocked"
    assert summary["completion_claimed"] is False
    assert summary["completion_scope"] == "none"
    assert summary["bridge_obstacle_avoidance_observed"] is True
    assert summary["obstacle_avoidance_observed"] is False
    assert summary["obstacle_avoidance_completion_claimed"] is False
    assert summary["obstacle_trajectory_clearance_observed"] is False
    assert summary["obstacle_trajectory_intersects_obstacle"] is True
    assert summary["obstacle_intersection_point_count"] > 0
    assert "obstacle_trajectory_intersects_obstacle" in summary["blocking_reasons"]


def test_turtlebot3_delivery_route_claims_dropoff_arrival_not_payload_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    proposal = plan["scenario_proposal"]
    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    assert proposal["mission_kind"] == "indoor_delivery_route_leg"
    assert len(proposal["planned_segments"]) == 10
    assert 7.6 < proposal["planned_route_distance_m"] < 8.4
    assert proposal["indoor_delivery_route"]["route_layout"] == (
        "simulated_home_loop_with_closed_door_person_and_pet_detours"
    )
    assert [
        blocker["label"]
        for blocker in proposal["indoor_delivery_route"]["simulated_blockers"]
    ] == ["closed door", "person", "dog"]
    assert proposal["obstacle_scenario"]["obstacle_challenge_requested"] is True
    assert proposal["indoor_delivery_route"]["route_requested"] is True
    assert summary["status"] == "completed"
    assert summary["completion_claimed"] is True
    assert summary["planned_segment_count"] == 10
    assert 7.6 < summary["planned_route_distance_m"] < 8.4
    assert summary["segment_dispatch_count"] == 10
    assert summary["segment_completion_count"] == 10
    assert summary["multi_segment_mission_claimed"] is True
    assert summary["obstacle_avoidance_completion_claimed"] is True
    assert summary["indoor_delivery_route_completion_claimed"] is True
    assert summary["dropoff_arrival_claimed"] is True
    assert summary["mission_delivery_completion_claimed"] is False
    assert execution["planned_route_distance_m"] == summary["planned_route_distance_m"]
    assert execution["payload_delivery_completion_claimed"] is False


def test_turtlebot3_mid_mission_low_battery_dispatches_return_home_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で障害物を避けながら屋内配送して。"
            "途中でバッテリーが足りなくなったら帰還"
        ),
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    assert summary["status"] == "recovered"
    assert summary["completion_claimed"] is False
    assert summary["indoor_delivery_route_completion_claimed"] is False
    assert summary["dropoff_arrival_claimed"] is False
    assert summary["runtime_recovery_triggered"] is True
    assert summary["route_interrupted_for_recovery"] is True
    assert summary["planned_segment_count"] == 10
    assert 7.6 < summary["planned_route_distance_m"] < 8.4
    assert summary["segment_dispatch_count"] == 1
    assert summary["segment_completion_count"] == 1
    assert summary["recovery_action_suggested"] == "return_home"
    assert summary["recovery_dispatch_request_sent"] is True
    assert summary["recovery_completion_claimed"] is True
    assert summary["recovery_segment_result"]["goal_pose"]["label"] == (
        "simulated_home_origin"
    )
    assert summary["physical_execution_invoked"] is False
    assert summary["mission_delivery_completion_claimed"] is False
    assert execution["recovery_completion_claimed"] is True


def test_turtlebot3_mid_mission_obstacle_recovery_resumes_delivery_with_llm_proposal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "obstacle_recovery_planner.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    _write_obstacle_recovery_planner(planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    assert proposal["mission_kind"] == "indoor_delivery_route_leg"
    assert proposal["obstacle_scenario"]["runtime_obstacle_recovery_requested"] is True
    assert proposal["planned_segments"][0]["label"] == (
        "simulated_dynamic_obstacle_approach"
    )
    assert len(proposal["planned_segments"]) == 10
    assert 7.6 < proposal["planned_route_distance_m"] < 8.4
    assert proposal["autonomy_envelope"]["preapproved_recovery_actions"] == [
        "return_home",
        "hold",
        "avoid_obstacle",
    ]
    assert summary["status"] == "completed"
    assert summary["completion_claimed"] is True
    assert summary["runtime_recovery_triggered"] is True
    assert summary["runtime_recovery_action_kind"] == "avoid_obstacle"
    assert summary["route_interrupted_for_recovery"] is True
    assert summary["route_resumed_after_recovery"] is True
    assert summary["route_completed_after_recovery"] is True
    assert summary["recovery_action_suggested"] == "avoid_obstacle"
    assert summary["recovery_proposals"][0]["proposal_source"] == "llm"
    assert summary["recovery_proposals"][0]["llm_judgment_recorded"] is True
    assert summary["recovery_proposal_classifications"][0]["execution_class"] == (
        "auto_executable"
    )
    assert summary["recovery_dispatch_request_sent"] is True
    assert summary["recovery_completion_claimed"] is True
    assert 7.6 < summary["planned_route_distance_m"] < 8.4
    assert summary["segment_dispatch_count"] == summary["planned_segment_count"]
    assert summary["segment_completion_count"] == summary["planned_segment_count"]
    assert summary["obstacle_avoidance_completion_claimed"] is True
    assert summary["obstacle_trajectory_clearance_observed"] is True
    assert summary["obstacle_trajectory_intersects_obstacle"] is False
    assert summary["indoor_delivery_route_completion_claimed"] is True
    assert summary["mission_delivery_completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False
    assert execution["runtime_recovery_obstacle_scenario"][
        "runtime_obstacle_observed"
    ] is True
    assert execution["recovery_segment_result"]["goal_pose"]["label"] == (
        "runtime_recovery_avoid_obstacle_waypoint"
    )


def test_turtlebot3_obstacle_recovery_requires_fresh_operator_approval_when_configured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "obstacle_recovery_planner.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    _write_obstacle_recovery_planner(planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    monkeypatch.setenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL",
        "1",
    )
    monkeypatch.setenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_REF",
        "operator_approval:test_fresh_recovery",
    )
    monkeypatch.setenv(
        "MISSIONOS_TURTLEBOT3_RECOVERY_OPERATOR_APPROVAL_ACTOR",
        "codex_e2e_operator",
    )
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    execution = result["turtlebot3_home_mission_execution"]
    assert proposal["autonomy_envelope"]["preapproved_recovery_actions"] == [
        "return_home",
        "hold",
    ]
    assert "avoid_obstacle" in proposal["autonomy_envelope"][
        "requires_human_approval_for"
    ]
    assert summary["status"] == "completed"
    assert summary["runtime_recovery_triggered"] is True
    assert summary["recovery_action_suggested"] == "avoid_obstacle"
    assert summary["recovery_proposal_classifications"][0]["execution_class"] == (
        "requires_human_approval"
    )
    assert summary["recovery_proposal_classifications"][0][
        "execution_permitted_by_envelope"
    ] is False
    assert summary["recovery_proposal_classifications"][0][
        "requires_new_human_approval"
    ] is True
    assert summary["recovery_execution_permitted_by_operator_approval"] is True
    assert summary["recovery_dispatch_authority_source"] == "fresh_operator_approval"
    assert summary["fresh_recovery_operator_approval_count"] == 1
    assert summary["fresh_recovery_operator_approvals"][0]["operator_approval_ref"] == (
        "operator_approval:test_fresh_recovery"
    )
    assert summary["fresh_recovery_operator_approvals"][0]["approval_actor"] == (
        "codex_e2e_operator"
    )
    assert summary["recovery_dispatch_request_sent"] is True
    assert summary["recovery_completion_claimed"] is True
    assert summary["route_completed_after_recovery"] is True
    assert summary["mission_delivery_completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False
    assert execution["recovery_segment_result"]["adapter_evidence"][
        "operator_approval_ref"
    ] == "operator_approval:test_fresh_recovery"


def test_turtlebot3_mid_mission_obstacle_recovery_uses_fallback_when_llm_guardrail_blocks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "guardrail_fault_recovery_planner.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    _write_guardrail_fault_recovery_planner(planner)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction=(
            "TurtleBot3で屋内配送して。走行中に障害物が出たら"
            "Recovery Agentが避ける提案をして、承認後に継続して"
        ),
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=proposal,
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    planner_result = summary["recovery_planner_result"]
    guardrail = planner_result["guardrail"]
    assert planner_result["planner_status"] == "guardrail_blocked"
    assert (
        "raw_llm_output_forbidden_authority_key:dispatch_request_sent"
        in planner_result["blocking_reasons"]
    )
    assert (
        "unsupported_observation_claim:fabricated_distance_to_home_m"
        in planner_result["blocking_reasons"]
    )
    assert guardrail["checks"]["forbidden_authority_keys_absent"] is False
    assert summary["recovery_proposals"][0]["proposal_source"] == (
        "deterministic_fallback"
    )
    assert summary["recovery_proposals"][0]["llm_judgment_recorded"] is False
    assert summary["recovery_action_suggested"] == "avoid_obstacle"
    assert summary["recovery_dispatch_request_sent"] is True
    assert summary["recovery_completion_claimed"] is True
    assert summary["route_resumed_after_recovery"] is True
    assert summary["route_completed_after_recovery"] is True
    assert summary["completion_claimed"] is True
    assert summary["mission_delivery_completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False


def test_turtlebot3_mid_mission_nav2_failure_convenes_recovery_and_stays_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    planner = tmp_path / "planner.py"
    _write_nav2_failure_bridge(bridge)
    _write_failure_recovery_planner(planner)
    process_logs = _write_nav2_failure_process_logs(tmp_path)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    monkeypatch.setenv(TURTLEBOT3_LOG_BUNDLE_PATHS_ENV, json.dumps(process_logs))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _bridge_command(planner))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して。",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
    )

    summary = result["summary"]
    assert summary["status"] == "blocked"
    assert summary["mission_episode_review_status"] == "blocked"
    assert "episode_blocked" in summary["mission_episode_review_blocked_buckets"]
    assert summary["dispatch_request_sent"] is True
    assert summary["completion_claimed"] is False
    assert summary["completion_scope"] == "none"
    # An unplanned segment failure now convenes the recovery machinery:
    # the planner proposes, the envelope classifies, and a permitted
    # return_home is attempted under the existing mission approval. The
    # failing bridge also fails the recovery dispatch, so nothing is claimed.
    assert summary["runtime_recovery_triggered"] is True
    assert summary["runtime_failure_recovery_triggered"] is True
    failure_context = summary["runtime_failure_context"]
    assert failure_context["failed_segment_index"] == 1
    assert failure_context["source"] == "ros2_nav2_bridge_segment_result"
    assert failure_context["runtime_failure_observed"] is True
    assert failure_context["recommended_recovery_action"] == "return_home"
    motion_context = summary["runtime_recovery_motion_context"]
    assert motion_context["odom_delta_m"] == 0.0
    assert motion_context["stalled_after_dispatch"] is True
    assert motion_context["motion_observation_source"] == "ros2_nav2_bridge_receipt"
    assert summary["recovery_proposals"]
    assert summary["recovery_proposals"][0]["proposal_source"] == "llm"
    assert summary["recovery_proposals"][0]["llm_judgment_recorded"] is True
    assert (
        summary["recovery_proposals"][0]["input_observations"][
            "runtime_failure_source"
        ]
        == "ros2_nav2_bridge_segment_result"
    )
    assert (
        summary["recovery_proposals"][0]["input_observations"]["odom_delta_m"]
        == 0.0
    )
    assert (
        summary["recovery_proposals"][0]["input_observations"][
            "stalled_after_dispatch"
        ]
        is True
    )
    assert summary["recovery_planner_status"] == "proposal_guardrail_passed"
    assert summary["runtime_recovery_action_kind"] == "return_home"
    assert summary["recovery_dispatch_request_sent"] is True
    assert summary["recovery_completion_claimed"] is False
    assert summary["mission_delivery_completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["segment_dispatch_count"] == 1
    assert summary["segment_completion_count"] == 0
    assert "nav2_goal_result_not_succeeded" in summary["blocking_reasons"]
    assert summary["nav2_log_diagnostics_status"] == "ready"
    assert "controller_failed_to_make_progress" in summary["nav2_log_observed_patterns"]
    assert "follow_path_action_aborted" in summary["nav2_log_observed_patterns"]
    assert "costmap_clear_recovery_observed" in summary["nav2_log_observed_patterns"]
    assert (
        "recovery_goal_stalled_after_costmap_clear"
        in summary["nav2_log_failure_hypotheses"]
    )


def test_turtlebot3_house_profile_routes_through_front_door(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "house")

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して",
    )

    proposal = result["scenario_proposal"]
    segments = proposal["planned_segments"]
    labels = [segment["label"] for segment in segments]
    assert len(segments) == 6
    assert "simulated_front_door_passage_checkpoint" in labels
    assert labels[-1] == "simulated_living_room_dropoff_waypoint"
    assert 9.7 < proposal["planned_route_distance_m"] < 10.7
    route = proposal["indoor_delivery_route"]
    assert route["planned_room_sequence"][0] == "home"
    assert "front door" in route["planned_room_sequence"]

    from src.runtime.turtlebot3_home_mission import _turtlebot3_home_floor_plan

    floor_plan = _turtlebot3_home_floor_plan()
    assert floor_plan["floor_plan_id"] == "turtlebot3_house.v1"
    assert floor_plan["source"] == "turtlebot3_house_model_sdf_collision"
    assert len(floor_plan["walls"]) >= 30
    assert floor_plan["pillars"] == []
    furniture_labels = {item["label"] for item in floor_plan["furniture"]}
    assert {"table", "bookshelf", "mailbox"} <= furniture_labels
    rooms = floor_plan["rooms"]
    for index, room_a in enumerate(rooms):
        for room_b in rooms[index + 1:]:
            overlap_x = min(room_a["max_x_m"], room_b["max_x_m"]) - max(
                room_a["min_x_m"], room_b["min_x_m"]
            )
            overlap_y = min(room_a["max_y_m"], room_b["max_y_m"]) - max(
                room_a["min_y_m"], room_b["min_y_m"]
            )
            assert not (overlap_x > 1e-9 and overlap_y > 1e-9), (
                room_a["room_id"],
                room_b["room_id"],
            )

    obstacle_scenario = proposal["obstacle_scenario"]
    assert obstacle_scenario["physical_execution_invoked"] is False


def test_turtlebot3_world_profile_defaults_to_house_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", raising=False)

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して",
    )

    from src.runtime.turtlebot3_home_mission import _turtlebot3_home_floor_plan

    proposal = result["scenario_proposal"]
    assert _turtlebot3_home_floor_plan()["floor_plan_id"] == "turtlebot3_house.v1"
    assert len(proposal["planned_segments"]) == 6


def test_turtlebot3_arena_profile_stays_available_via_env(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "arena")

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して",
    )

    from src.runtime.turtlebot3_home_mission import _turtlebot3_home_floor_plan

    proposal = result["scenario_proposal"]
    assert _turtlebot3_home_floor_plan()["floor_plan_id"] == (
        "turtlebot3_simulated_home_loop.v1"
    )
    assert len(proposal["planned_segments"]) == 10


def test_turtlebot3_house_delivery_to_named_bedroom(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "house")

    result = build_turtlebot3_home_mission_plan(
        operator_instruction="亀さん、bedroomへ荷物を届けて",
    )

    proposal = result["scenario_proposal"]
    labels = [segment["label"] for segment in proposal["planned_segments"]]
    assert labels[-1] == "simulated_bedroom_dropoff_waypoint"
    assert "simulated_front_door_passage_checkpoint" in labels
    assert "simulated_study_door_checkpoint" in labels
    assert "simulated_bedroom_door_checkpoint" in labels
    route = proposal["indoor_delivery_route"]
    assert route["destination_room_id"] == "bedroom"
    assert route["destination_room_label"] == "Bedroom"
    assert "bedroom" in route["planned_room_sequence"]


def test_turtlebot3_house_delivery_room_terms_japanese(monkeypatch) -> None:
    monkeypatch.setenv("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "house")

    for text, expected in (
        ("寝室まで配送して", "bedroom"),
        ("書斎に届けて", "study"),
        ("ダイニングへ配送して", "dining"),
        ("パントリーに配送して", "pantry"),
        ("ラウンジまで届けて", "lounge"),
        ("屋内配送して", "living"),
    ):
        result = build_turtlebot3_home_mission_plan(
            operator_instruction=f"TurtleBot3で{text}",
        )
        route = result["scenario_proposal"]["indoor_delivery_route"]
        assert route["destination_room_id"] == expected, text


def test_turtlebot3_dispatch_emits_claim_safe_live_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "bridge.py"
    _write_success_bridge(bridge, obstacle_avoidance_observed=True)
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, _bridge_command(bridge))
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して",
    )
    approval = approve_turtlebot3_home_mission_plan(
        proposal=plan["scenario_proposal"],
        validation=plan["validation_result"],
    )

    partials: list[dict] = []
    result = run_turtlebot3_home_mission_dispatch(
        proposal=plan["scenario_proposal"],
        approval=approval["turtlebot3_home_mission_approval"],
        progress_callback=partials.append,
    )

    assert result["summary"]["status"] == "completed"
    segment_count = len(plan["scenario_proposal"]["planned_segments"])
    assert len(partials) == segment_count
    dispatch_counts = [
        partial["summary"]["segment_dispatch_count"] for partial in partials
    ]
    assert dispatch_counts == sorted(dispatch_counts)
    assert dispatch_counts[-1] == segment_count
    for partial in partials:
        summary = partial["summary"]
        assert summary["status"] == "running"
        assert summary["completion_claimed"] is False
        assert summary["physical_execution_invoked"] is False
        assert summary["mission_delivery_completion_claimed"] is False
        model = summary["turtlebot3_indoor_map_model"]
        assert model["mission_status"] == "running"
        assert model["physical_execution_invoked"] is False


def test_turtlebot3_display_sanitize_drops_frame_leaks_and_spikes() -> None:
    from src.runtime.turtlebot3_home_mission import (
        _sanitize_observed_display_points,
    )

    points = [
        {"x_m": 5.0, "y_m": 5.0, "frame_id": "odom"},
        {"x_m": -2.0, "y_m": -0.5, "frame_id": "map"},
        {"x_m": -1.9, "y_m": -0.5, "frame_id": "map"},
        {"x_m": 3.0, "y_m": 2.0, "frame_id": "map"},
        {"x_m": -1.8, "y_m": -0.45, "frame_id": "map"},
        {"x_m": -0.9, "y_m": -0.4, "frame_id": "map"},
        {"x_m": -0.85, "y_m": -0.35, "frame_id": "map"},
    ]

    kept, meta = _sanitize_observed_display_points(points)

    xs = [point["x_m"] for point in kept]
    assert 5.0 not in xs
    assert 3.0 not in xs
    assert meta["odom_fallback_dropped"] == 1
    assert meta["spike_dropped"] == 1
    sustained = [
        {"x_m": -2.0, "y_m": -0.5, "frame_id": "map"},
        {"x_m": -0.9, "y_m": -0.4, "frame_id": "map"},
        {"x_m": -0.88, "y_m": -0.38, "frame_id": "map"},
    ]
    kept_sustained, _ = _sanitize_observed_display_points(sustained)
    assert len(kept_sustained) == 3
