from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from src.runtime.turtlebot3_sim_process_runner import (
    TurtleBot3ProcessCommandResult,
    run_turtlebot3_sim_process_scenario,
    turtlebot3_sim_process_run_ref,
)


def _base_smoke_payload() -> dict[str, object]:
    return {
        "smoke": "missionos_chat_turtlebot3_home_mission",
        "status": "completed",
        "completion_claimed": True,
        "completion_scope": "sim_action",
        "robot_motion_observed": True,
        "dispatch_request_sent": True,
        "physical_execution_invoked": False,
        "mission_delivery_completion_claimed": False,
        "localization_drift_fault_injection_enabled": False,
        "mid_mission_recovery": {
            "status": None,
            "runtime_recovery_triggered": None,
            "recovery_dispatch_request_sent": None,
            "recovery_completion_claimed": None,
            "completion_claimed": None,
            "physical_execution_invoked": None,
            "mission_delivery_completion_claimed": None,
        },
        "dynamic_obstacle_recovery": {
            "status": None,
            "runtime_recovery_triggered": None,
            "runtime_recovery_action_kind": None,
            "route_resumed_after_recovery": None,
            "route_completed_after_recovery": None,
            "recovery_planner_status": None,
            "recovery_proposal_source": None,
            "recovery_dispatch_request_sent": None,
            "recovery_completion_claimed": None,
            "physical_execution_invoked": None,
        },
        "recovery_guardrail_fallback_injection_enabled": False,
    }


def _runner_for(payload: dict[str, object]):
    def _runner(
        command: tuple[str, ...],
        env: Mapping[str, str],
        timeout_s: float,
        cwd: Path,
    ) -> TurtleBot3ProcessCommandResult:
        assert command == ("fake-turtlebot3-smoke",)
        assert timeout_s > 0
        assert cwd.exists()
        if payload.get("localization_drift_fault_injection_enabled") is True:
            assert env["MISSIONOS_CHAT_TURTLEBOT3_LOCALIZATION_DRIFT_FAULT_SMOKE"] == "1"
        if payload.get("recovery_guardrail_fallback_injection_enabled") is True:
            assert env["MISSIONOS_CHAT_TURTLEBOT3_DYNAMIC_OBSTACLE_RECOVERY_SMOKE"] == "1"
            assert (
                env["MISSIONOS_CHAT_TURTLEBOT3_RECOVERY_GUARDRAIL_FALLBACK_SMOKE"]
                == "1"
            )
        return TurtleBot3ProcessCommandResult(
            exit_code=0,
            stdout="sim_obstacle_spawn_status=0\n"
            + json.dumps(payload, sort_keys=True)
            + "\n",
        )

    return _runner


def test_turtlebot3_sim_process_runner_blocks_without_opt_in() -> None:
    run = run_turtlebot3_sim_process_scenario(
        command=("fake-turtlebot3-smoke",),
        opt_in=False,
    )

    assert run.schema_version == "missionos_turtlebot3_sim_process_run.v1"
    assert run.process_launch_attempted is False
    assert run.docker_lifecycle_invoked is False
    assert run.exit_status == "not_attempted"
    assert run.scenario_passed is False
    assert "RUN_MISSIONOS_TURTLEBOT3_SIM_PROCESS_RUNNER_not_enabled" in run.blocked_reasons
    assert run.physical_execution_invoked is False
    assert run.mission_delivery_completion_claimed is False


def test_turtlebot3_sim_process_runner_passes_obstacle_delivery() -> None:
    run = run_turtlebot3_sim_process_scenario(
        scenario="obstacle_delivery",
        command=("fake-turtlebot3-smoke",),
        opt_in=True,
        runner=_runner_for(_base_smoke_payload()),
    )

    assert run.process_launch_attempted is True
    assert run.docker_lifecycle_invoked is False
    assert run.exit_status == "completed"
    assert run.scenario_passed is True
    assert run.parsed_status == "completed"
    assert run.parsed_completion_scope == "sim_action"
    assert not run.blocked_reasons
    assert "normal_delivery_completed" in run.scenario_assertions
    assert turtlebot3_sim_process_run_ref(run).startswith(
        "turtlebot3_sim_process_run:"
    )


def test_turtlebot3_sim_process_runner_passes_mid_recovery() -> None:
    payload = _base_smoke_payload()
    payload["mid_mission_recovery"] = {
        "status": "recovered",
        "runtime_recovery_triggered": True,
        "recovery_dispatch_request_sent": True,
        "recovery_completion_claimed": True,
        "completion_claimed": False,
        "physical_execution_invoked": False,
        "mission_delivery_completion_claimed": False,
    }

    run = run_turtlebot3_sim_process_scenario(
        scenario="mid_recovery",
        command=("fake-turtlebot3-smoke",),
        opt_in=True,
        runner=_runner_for(payload),
    )

    assert run.scenario_passed is True
    assert run.parsed_mid_recovery_status == "recovered"
    assert "return_home_recovery_completed" in run.scenario_assertions


def test_turtlebot3_sim_process_runner_passes_localization_drift_fault_gate() -> None:
    payload = _base_smoke_payload()
    payload["localization_drift_fault_injection_enabled"] = True
    payload["mid_mission_recovery"] = {
        "status": "blocked",
        "completion_claimed": False,
        "runtime_recovery_triggered": True,
        "runtime_failure_recovery_triggered": True,
        "recovery_proposal_count": 1,
        "llm_recovery_proposal_count": 0,
        "runtime_recovery_motion_context": {
            "odom_delta_m": 0.0,
            "robot_motion_observed": False,
            "motion_observation_source": "ros2_nav2_bridge_receipt",
            "stalled_after_dispatch": True,
        },
        "recovery_approval_created_count": 0,
        "recovery_dispatch_request_sent": False,
        "recovery_completion_claimed": False,
        "mission_delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "nav2_log_diagnostics_status": "ready",
        "blocking_reasons": ["nav2_goal_result_not_succeeded"],
    }

    run = run_turtlebot3_sim_process_scenario(
        scenario="localization_drift_fault",
        command=("fake-turtlebot3-smoke",),
        opt_in=True,
        runner=_runner_for(payload),
    )

    assert run.scenario_passed is True
    assert run.parsed_fault_injection_enabled is True
    assert run.parsed_mid_recovery_status == "blocked"
    assert "localization_drift_fault_blocks_or_recovers" in run.scenario_assertions
    assert "failure_recovery_convened" in run.scenario_assertions


def test_turtlebot3_sim_process_runner_accepts_localization_drift_recovered() -> None:
    payload = _base_smoke_payload()
    payload["localization_drift_fault_injection_enabled"] = True
    payload["mid_mission_recovery"] = {
        "status": "recovered",
        "completion_claimed": False,
        "runtime_recovery_triggered": True,
        "runtime_failure_recovery_triggered": True,
        "recovery_proposal_count": 1,
        "llm_recovery_proposal_count": 1,
        "runtime_recovery_motion_context": {
            "odom_delta_m": 0.03,
            "robot_motion_observed": True,
            "motion_observation_source": "ros2_nav2_bridge_receipt",
            "stalled_after_dispatch": True,
        },
        "recovery_approval_created_count": 0,
        "recovery_dispatch_request_sent": True,
        "recovery_completion_claimed": True,
        "mission_delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "nav2_log_diagnostics_status": "ready",
        "blocking_reasons": [],
    }

    run = run_turtlebot3_sim_process_scenario(
        scenario="localization_drift_fault",
        command=("fake-turtlebot3-smoke",),
        opt_in=True,
        runner=_runner_for(payload),
    )

    assert run.scenario_passed is True
    assert run.parsed_mid_recovery_status == "recovered"
    assert "recovery_completion_bounded" in run.scenario_assertions
    assert "motion_delta_supplied" in run.scenario_assertions


def test_turtlebot3_sim_process_runner_passes_recovery_guardrail_fallback() -> None:
    payload = _base_smoke_payload()
    payload["recovery_guardrail_fallback_injection_enabled"] = True
    payload["dynamic_obstacle_recovery"] = {
        "status": "completed",
        "completion_claimed": True,
        "runtime_recovery_triggered": True,
        "runtime_recovery_action_kind": "avoid_obstacle",
        "route_resumed_after_recovery": True,
        "route_completed_after_recovery": True,
        "recovery_planner_status": "guardrail_blocked",
        "recovery_proposal_source": "deterministic_fallback",
        "recovery_dispatch_request_sent": True,
        "recovery_completion_claimed": True,
        "physical_execution_invoked": False,
    }

    run = run_turtlebot3_sim_process_scenario(
        scenario="recovery_guardrail_fallback",
        command=("fake-turtlebot3-smoke",),
        opt_in=True,
        runner=_runner_for(payload),
    )

    assert run.scenario_passed is True
    assert "llm_output_guardrail_blocked" in run.scenario_assertions
    assert "deterministic_fallback_used" in run.scenario_assertions
    assert "route_completed_after_fallback_recovery" in run.scenario_assertions


def test_turtlebot3_sim_process_runner_passes_recovered_guardrail_fallback_without_completion() -> None:
    payload = _base_smoke_payload()
    payload["completion_claimed"] = False
    payload["completion_scope"] = "none"
    payload["status"] = "recovered"
    payload["recovery_guardrail_fallback_injection_enabled"] = True
    payload["dynamic_obstacle_recovery"] = {
        "status": "recovered",
        "completion_claimed": False,
        "runtime_recovery_triggered": True,
        "runtime_recovery_action_kind": "avoid_obstacle",
        "route_resumed_after_recovery": True,
        "route_completed_after_recovery": False,
        "recovery_planner_status": "guardrail_blocked",
        "recovery_proposal_source": "deterministic_fallback",
        "recovery_dispatch_request_sent": True,
        "recovery_completion_claimed": True,
        "physical_execution_invoked": False,
    }

    run = run_turtlebot3_sim_process_scenario(
        scenario="recovery_guardrail_fallback",
        command=("fake-turtlebot3-smoke",),
        opt_in=True,
        runner=_runner_for(payload),
    )

    assert run.scenario_passed is True
    assert "fallback_recovery_completed" in run.scenario_assertions
    assert "route_completed_after_fallback_recovery" not in run.scenario_assertions


def test_turtlebot3_sim_process_runner_blocks_physical_claim() -> None:
    payload = _base_smoke_payload()
    payload["physical_execution_invoked"] = True

    run = run_turtlebot3_sim_process_scenario(
        scenario="obstacle_delivery",
        command=("fake-turtlebot3-smoke",),
        opt_in=True,
        runner=_runner_for(payload),
    )

    assert run.scenario_passed is False
    assert "physical_execution_claimed" in run.blocked_reasons
