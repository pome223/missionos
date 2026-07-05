#!/usr/bin/env python3
"""Smoke for the TurtleBot3/Nav2 simulator process runner."""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.runtime.turtlebot3_sim_process_runner import (
    TURTLEBOT3_SIM_PROCESS_RUNNER_SCENARIO_ENV,
    TurtleBot3SimProcessRun,
    run_turtlebot3_sim_process_scenario,
)

FULL_OUTPUT_ENV = "MISSIONOS_TURTLEBOT3_SIM_PROCESS_RUNNER_FULL_OUTPUT"


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _compact_payload(run: TurtleBot3SimProcessRun) -> dict[str, object]:
    parsed = run.parsed_result
    mid = parsed.get("mid_mission_recovery")
    mid = mid if isinstance(mid, dict) else {}
    return {
        "schema_version": run.schema_version,
        "process_run_id": run.process_run_id,
        "scenario": run.scenario,
        "process_launch_attempted": run.process_launch_attempted,
        "docker_lifecycle_invoked": run.docker_lifecycle_invoked,
        "exit_status": run.exit_status,
        "exit_code": run.exit_code,
        "scenario_passed": run.scenario_passed,
        "scenario_assertions": list(run.scenario_assertions),
        "blocked_reasons": list(run.blocked_reasons),
        "stdout_ref": run.stdout_ref,
        "stderr_ref": run.stderr_ref,
        "stdout_sha256": run.stdout_sha256,
        "stderr_sha256": run.stderr_sha256,
        "physical_execution_invoked": run.physical_execution_invoked,
        "mission_delivery_completion_claimed": run.mission_delivery_completion_claimed,
        "parsed": {
            "status": run.parsed_status,
            "completion_claimed": run.parsed_completion_claimed,
            "completion_scope": run.parsed_completion_scope,
            "robot_motion_observed": parsed.get("robot_motion_observed"),
            "odom_delta_m": parsed.get("odom_delta_m"),
            "mission_episode_review_status": parsed.get(
                "mission_episode_review_status"
            ),
            "mission_episode_review_passed": parsed.get(
                "mission_episode_review_passed"
            ),
            "localization_drift_fault_injection_enabled": (
                run.parsed_fault_injection_enabled
            ),
        },
        "mid_mission_recovery": {
            "status": run.parsed_mid_recovery_status,
            "runtime_recovery_triggered": mid.get("runtime_recovery_triggered"),
            "recovery_dispatch_request_sent": mid.get(
                "recovery_dispatch_request_sent"
            ),
            "recovery_completion_claimed": mid.get("recovery_completion_claimed"),
            "completion_claimed": mid.get("completion_claimed"),
            "mission_episode_review_status": mid.get("mission_episode_review_status"),
            "mission_episode_review_passed": mid.get("mission_episode_review_passed"),
            "blocking_reasons": mid.get("blocking_reasons"),
            "nav2_log_diagnostics_status": mid.get("nav2_log_diagnostics_status"),
        },
    }


def main() -> int:
    scenario = os.environ.get(
        TURTLEBOT3_SIM_PROCESS_RUNNER_SCENARIO_ENV,
        "obstacle_delivery",
    )
    if scenario not in {
        "obstacle_delivery",
        "mid_recovery",
        "localization_drift_fault",
    }:
        raise SystemExit(f"unsupported TurtleBot3 sim process scenario: {scenario}")
    run = run_turtlebot3_sim_process_scenario(
        scenario=scenario,  # type: ignore[arg-type]
        repo_root=Path(__file__).resolve().parents[1],
    )
    payload = (
        run.model_dump(mode="json")
        if _truthy_env(FULL_OUTPUT_ENV)
        else _compact_payload(run)
    )
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    if run.process_launch_attempted is False:
        if run.physical_execution_invoked is not False:
            raise SystemExit("process runner claimed physical execution")
        if run.mission_delivery_completion_claimed is not False:
            raise SystemExit("process runner claimed mission delivery completion")
        return 0
    return 0 if run.scenario_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
