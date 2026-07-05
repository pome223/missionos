from __future__ import annotations

from pathlib import Path
import shlex
import sys

from src.intelligence.turtlebot3_recovery_planner import (
    TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV,
    TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV,
    TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV,
    _ollama_base_url,
    build_turtlebot3_recovery_planner_prompt,
    guard_turtlebot3_recovery_planner_output,
    run_turtlebot3_recovery_planner,
)


def _command(path: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"


def _battery_envelope() -> dict[str, object]:
    return {
        "schema_version": "missionos_turtlebot3_battery_envelope.v1",
        "battery_start_pct": 18.0,
        "minimum_required_pct": 28.0,
        "dispatch_allowed": False,
        "blocking_reasons": ["battery_below_minimum_required"],
    }


def _autonomy_envelope() -> dict[str, object]:
    return {
        "schema_version": "missionos_mission_autonomy_envelope.v1",
        "envelope_id": "envelope_test",
        "mission_ref": "mission_test",
        "operator_approved": False,
        "llm_recovery_proposals_allowed": True,
        "proposal_first_classification": True,
        "preapproved_recovery_actions": ["return_home", "hold"],
        "requires_human_approval_for": [
            "avoid_obstacle",
            "reroute",
            "safe_stop",
            "ask_human",
        ],
        "blocked_actions": [
            "raw_velocity",
            "unbounded_move",
            "physical_execution",
            "payload_delivery_completion",
        ],
    }


def _home_distance_envelope() -> dict[str, object]:
    return {
        "schema_version": "missionos_turtlebot3_home_distance_envelope.v1",
        "distance_to_home_m": 1.4,
        "distance_to_home_source": "planned_nav2_goal_projection",
        "distance_to_home_source_backed": True,
        "projected_return_battery_required_pct": 2.8,
        "runtime_observed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _runtime_failure_context() -> dict[str, object]:
    return {
        "schema_version": "missionos_turtlebot3_runtime_segment_failure.v1",
        "runtime_failure_observed": True,
        "failed_segment_index": 2,
        "failed_segment_label": "simulated_hallway_checkpoint",
        "runtime_failure_source": "ros2_nav2_bridge_segment_result",
        "failed_segment_completion_claimed": False,
        "failed_segment_blocking_reason_count": 1,
        "recommended_recovery_action": "return_home",
        "source": "ros2_nav2_bridge_segment_result",
    }


def _runtime_motion_context() -> dict[str, object]:
    return {
        "schema_version": "missionos_turtlebot3_runtime_motion_context.v1",
        "robot_motion_observed": False,
        "odom_delta_m": 0.0,
        "odom_topic": "/odom",
        "motion_observation_source": "ros2_nav2_bridge_receipt",
        "route_progress_delta_m": 0.0,
        "completed_route_distance_m": 0.0,
        "planned_segment_distance_m": 1.4,
        "stalled_after_dispatch": True,
        "motion_stall_threshold_m": 0.05,
        "telemetry_window_ref": "not_available",
    }


def test_turtlebot3_recovery_planner_command_override_records_llm_proposal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = tmp_path / "planner.py"
    script.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "assert prompt['role_contract']['llm_must_not_dispatch'] is True\n"
        "print(json.dumps({\n"
        "  'selected_action': 'return_home',\n"
        "  'reason': 'Battery is below reserve and return home is bounded.',\n"
        "  'input_observations': {\n"
        "    'battery_start_pct': prompt['battery_envelope']['battery_start_pct'],\n"
        "    'minimum_required_pct': prompt['battery_envelope']['minimum_required_pct'],\n"
        "    'distance_to_home_m': prompt['home_distance_envelope']['distance_to_home_m'],\n"
        "    'projected_return_battery_required_pct': prompt['home_distance_envelope']['projected_return_battery_required_pct']\n"
        "  }\n"
        "}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _command(script))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)

    result = run_turtlebot3_recovery_planner(
        mission_ref="mission_test",
        operator_instruction="TurtleBot3で配送して。バッテリーが足りない",
        battery_envelope=_battery_envelope(),
        home_distance_envelope=_home_distance_envelope(),
        autonomy_envelope=_autonomy_envelope(),
    )

    proposal = result["proposal"]
    evidence = result["llm_invocation_evidence"]
    assert result["planner_status"] == "proposal_guardrail_passed"
    assert proposal["proposal_source"] == "llm"
    assert proposal["selected_action"] == "return_home"
    assert proposal["llm_judgment_recorded"] is True
    assert proposal["dispatch_authority_created"] is False
    assert proposal["physical_execution_invoked"] is False
    assert proposal["llm_invocation_evidence"]["provider"] == "command_override"
    assert evidence["invocation_kind"] == "subprocess"
    assert result["dispatch_authority_created"] is False
    assert result["physical_execution_invoked"] is False


def test_turtlebot3_recovery_planner_command_override_accepts_runtime_failure_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = tmp_path / "planner.py"
    script.write_text(
        "import json, sys\n"
        "prompt = json.loads(sys.stdin.read())\n"
        "failure = prompt['runtime_failure_context']\n"
        "assert failure['runtime_failure_observed'] is True\n"
        "print(json.dumps({\n"
        "  'selected_action': 'return_home',\n"
        "  'reason': 'Source-backed Nav2 segment failure; return home is bounded.',\n"
        "  'input_observations': {\n"
        "    'runtime_failure_observed': failure['runtime_failure_observed'],\n"
        "    'failed_segment_index': failure['failed_segment_index'],\n"
        "    'failed_segment_label': failure['failed_segment_label'],\n"
        "    'runtime_failure_source': failure['runtime_failure_source'],\n"
        "    'failed_segment_completion_claimed': failure['failed_segment_completion_claimed'],\n"
        "    'failed_segment_blocking_reason_count': failure['failed_segment_blocking_reason_count'],\n"
        "    'recommended_recovery_action': failure['recommended_recovery_action'],\n"
        "    'distance_to_home_m': prompt['home_distance_envelope']['distance_to_home_m'],\n"
        "    'odom_delta_m': prompt['runtime_motion_context']['odom_delta_m'],\n"
        "    'stalled_after_dispatch': prompt['runtime_motion_context']['stalled_after_dispatch'],\n"
        "    'motion_observation_source': prompt['runtime_motion_context']['motion_observation_source']\n"
        "  }\n"
        "}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, _command(script))
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, "1")
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)

    result = run_turtlebot3_recovery_planner(
        mission_ref="mission_test",
        operator_instruction="TurtleBot3で配送して。経路が失敗したら回復判断して。",
        battery_envelope={**_battery_envelope(), "dispatch_allowed": True},
        home_distance_envelope=_home_distance_envelope(),
        autonomy_envelope=_autonomy_envelope(),
        runtime_failure_context=_runtime_failure_context(),
        runtime_motion_context=_runtime_motion_context(),
    )

    proposal = result["proposal"]
    assert result["planner_status"] == "proposal_guardrail_passed"
    assert proposal["proposal_source"] == "llm"
    assert proposal["selected_action"] == "return_home"
    assert proposal["input_observations"]["runtime_failure_observed"] is True
    assert proposal["input_observations"]["odom_delta_m"] == 0.0
    assert proposal["input_observations"]["stalled_after_dispatch"] is True
    assert proposal["dispatch_authority_created"] is False
    assert proposal["physical_execution_invoked"] is False


def test_turtlebot3_recovery_planner_blocks_forbidden_authority_keys() -> None:
    guardrail = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "return_home",
            "reason": "Battery is below reserve.",
            "input_observations": {"battery_pct": 18.0},
            "approved": True,
        },
        source_observations={"battery_pct": 18.0},
    )

    assert guardrail["guardrail_passed"] is False
    assert "raw_llm_output_forbidden_authority_key:approved" in guardrail[
        "blocking_reasons"
    ]


def test_turtlebot3_recovery_planner_blocks_dispatch_authority_key_alone() -> None:
    guardrail = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "avoid_obstacle",
            "reason": "Runtime obstacle is source-backed.",
            "dispatch_request_sent": True,
            "input_observations": {
                "runtime_obstacle_observed": True,
                "runtime_obstacle_source": "ros2_nav2_bridge_costmap",
            },
        },
        source_observations={
            "runtime_obstacle_observed": True,
            "runtime_obstacle_source": "ros2_nav2_bridge_costmap",
        },
    )

    assert guardrail["guardrail_passed"] is False
    assert guardrail["checks"]["forbidden_authority_keys_absent"] is False
    assert guardrail["checks"]["input_observations_source_backed"] is True
    assert guardrail["blocking_reasons"] == [
        "raw_llm_output_forbidden_authority_key:dispatch_request_sent"
    ]


def test_turtlebot3_recovery_planner_blocks_unsupported_home_distance_claim() -> None:
    guardrail = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "return_home",
            "reason": "Battery is below reserve.",
            "input_observations": {"distance_to_home_m": 7.0},
        },
        source_observations={"distance_to_home_m": 1.4},
    )

    assert guardrail["guardrail_passed"] is False
    assert "observation_not_source_backed:distance_to_home_m" in guardrail[
        "blocking_reasons"
    ]


def test_turtlebot3_recovery_planner_blocks_unsupported_observation_alias() -> None:
    guardrail = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "return_home",
            "reason": "Battery is below reserve.",
            "input_observations": {"battery_pct": 18.0},
        },
        source_observations={"battery_start_pct": 18.0},
    )

    assert guardrail["guardrail_passed"] is False
    assert "unsupported_observation_claim:battery_pct" in guardrail[
        "blocking_reasons"
    ]


def test_turtlebot3_recovery_planner_blocks_fabricated_observation_alone() -> None:
    guardrail = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "avoid_obstacle",
            "reason": "Runtime obstacle is source-backed.",
            "input_observations": {
                "runtime_obstacle_observed": True,
                "fabricated_distance_to_home_m": 123.456,
            },
        },
        source_observations={
            "runtime_obstacle_observed": True,
        },
    )

    assert guardrail["guardrail_passed"] is False
    assert guardrail["checks"]["forbidden_authority_keys_absent"] is True
    assert guardrail["checks"]["input_observations_source_backed"] is False
    assert guardrail["blocking_reasons"] == [
        "unsupported_observation_claim:fabricated_distance_to_home_m"
    ]


def test_turtlebot3_recovery_planner_accepts_source_backed_avoid_obstacle() -> None:
    guardrail = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "avoid_obstacle",
            "reason": "Runtime obstacle is source-backed.",
            "input_observations": {
                "runtime_obstacle_observed": True,
                "runtime_obstacle_source": "ros2_nav2_bridge_costmap",
                "recommended_avoidance_target_x_m": -1.15,
                "recommended_avoidance_target_y_m": -0.85,
            },
        },
        source_observations={
            "runtime_obstacle_observed": True,
            "runtime_obstacle_source": "ros2_nav2_bridge_costmap",
            "recommended_avoidance_target_x_m": -1.15,
            "recommended_avoidance_target_y_m": -0.85,
        },
    )

    assert guardrail["guardrail_passed"] is True
    assert guardrail["validated_proposal"]["selected_action"] == "avoid_obstacle"
    assert guardrail["dispatch_authority_created"] is False
    assert guardrail["physical_execution_invoked"] is False


def test_turtlebot3_recovery_planner_accepts_source_backed_runtime_failure() -> None:
    guardrail = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "return_home",
            "reason": "Runtime Nav2 segment failure is source-backed.",
            "input_observations": {
                "runtime_failure_observed": True,
                "failed_segment_index": 2,
                "failed_segment_label": "simulated_hallway_checkpoint",
                "runtime_failure_source": "ros2_nav2_bridge_segment_result",
                "failed_segment_completion_claimed": False,
                "failed_segment_blocking_reason_count": 1,
                "recommended_recovery_action": "return_home",
            },
        },
        source_observations={
            "runtime_failure_observed": True,
            "failed_segment_index": 2,
            "failed_segment_label": "simulated_hallway_checkpoint",
            "runtime_failure_source": "ros2_nav2_bridge_segment_result",
            "failed_segment_completion_claimed": False,
            "failed_segment_blocking_reason_count": 1,
            "recommended_recovery_action": "return_home",
        },
    )

    assert guardrail["guardrail_passed"] is True
    assert guardrail["validated_proposal"]["selected_action"] == "return_home"
    assert guardrail["dispatch_authority_created"] is False
    assert guardrail["physical_execution_invoked"] is False


def test_turtlebot3_recovery_planner_accepts_source_backed_motion_delta() -> None:
    guardrail = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "return_home",
            "reason": "Runtime segment stalled with no odom delta.",
            "input_observations": {
                "runtime_failure_observed": True,
                "odom_delta_m": 0.0,
                "robot_motion_observed": False,
                "stalled_after_dispatch": True,
                "motion_observation_source": "ros2_nav2_bridge_receipt",
                "route_progress_delta_m": 0.0,
                "motion_stall_threshold_m": 0.05,
            },
        },
        source_observations={
            "runtime_failure_observed": True,
            "odom_delta_m": 0.0,
            "robot_motion_observed": False,
            "stalled_after_dispatch": True,
            "motion_observation_source": "ros2_nav2_bridge_receipt",
            "route_progress_delta_m": 0.0,
            "motion_stall_threshold_m": 0.05,
        },
    )

    assert guardrail["guardrail_passed"] is True
    assert guardrail["validated_proposal"]["input_observations"]["odom_delta_m"] == 0.0
    assert guardrail["dispatch_authority_created"] is False
    assert guardrail["physical_execution_invoked"] is False


def test_turtlebot3_recovery_planner_blocks_fabricated_motion_delta() -> None:
    guardrail = guard_turtlebot3_recovery_planner_output(
        {
            "selected_action": "continue",
            "reason": "Nearly complete progress.",
            "input_observations": {
                "runtime_failure_observed": True,
                "odom_delta_m": 1.8,
            },
        },
        source_observations={
            "runtime_failure_observed": True,
            "odom_delta_m": 0.0,
        },
    )

    assert guardrail["guardrail_passed"] is False
    assert "observation_not_source_backed:odom_delta_m" in guardrail[
        "blocking_reasons"
    ]
    assert guardrail["dispatch_authority_created"] is False
    assert guardrail["physical_execution_invoked"] is False


def test_turtlebot3_recovery_planner_prompt_carries_runtime_failure_context() -> None:
    prompt = build_turtlebot3_recovery_planner_prompt(
        mission_ref="mission_test",
        operator_instruction="TurtleBot3で配送して。経路が失敗したら回復判断して。",
        battery_envelope=_battery_envelope(),
        home_distance_envelope=_home_distance_envelope(),
        autonomy_envelope=_autonomy_envelope(),
        runtime_failure_context=_runtime_failure_context(),
        runtime_motion_context=_runtime_motion_context(),
    )

    assert prompt["runtime_failure_context"]["runtime_failure_observed"] is True
    assert prompt["runtime_motion_context"]["odom_delta_m"] == 0.0
    assert "odom_delta_m" in prompt["allowed_input_observation_keys"]
    assert "runtime_motion_context" not in prompt["allowed_input_observation_keys"]
    assert "allowed_input_observation_keys" in prompt["strict_output_contract"]
    assert "runtime_failure_context" in prompt["strict_output_contract"]
    assert "runtime_motion_context" in prompt["strict_output_contract"]
    assert prompt["role_contract"]["llm_must_not_dispatch"] is True


def test_turtlebot3_recovery_planner_ollama_base_url_prefers_agent_env(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MISSIONOS_OLLAMA_BASE_URL", "http://localhost:11434/")
    monkeypatch.setenv(
        "MISSIONOS_AGENT_MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_AGENT_OLLAMA_BASE_URL",
        "http://host.docker.internal:11434/",
    )

    assert _ollama_base_url() == "http://host.docker.internal:11434"


def test_turtlebot3_recovery_planner_reports_not_configured(monkeypatch) -> None:
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, raising=False)
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, raising=False)

    result = run_turtlebot3_recovery_planner(
        mission_ref="mission_test",
        operator_instruction="TurtleBot3で配送して。バッテリーが足りない",
        battery_envelope=_battery_envelope(),
        home_distance_envelope=_home_distance_envelope(),
        autonomy_envelope=_autonomy_envelope(),
    )

    assert result["planner_status"] == "not_configured"
    assert result["proposal"] == {}
    assert result["dispatch_authority_created"] is False
    assert result["physical_execution_invoked"] is False
