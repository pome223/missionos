#!/usr/bin/env python3
"""Opt-in smoke for launching a generated Digital Twin Gazebo world."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

from src.runtime.digital_twin_mission_environment import (
    build_digital_twin_stage1_environment,
)
from src.runtime.digital_twin_sitl_process_runner import (
    DIGITAL_TWIN_SITL_PROCESS_RUN_SCHEMA_VERSION,
    digital_twin_sitl_process_run_ref,
    run_digital_twin_sitl_process,
)


OPT_IN_ENV = "RUN_DIGITAL_TWIN_GAZEBO_WORLD_LAUNCH_SMOKE"
PROMPT = "10km先の3000mの山小屋に水3kgを届ける"
PROMPT_REF = "px4_gazebo_mission_prompt_request:digital_twin_world_launch_smoke"
NOW = datetime(2026, 5, 8, 0, 0, tzinfo=timezone.utc)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to launch the generated Digital Twin Gazebo world."
        )


def run_smoke() -> dict:
    _require_opt_in()
    digital_twin = build_digital_twin_stage1_environment(
        prompt=PROMPT,
        prompt_request_ref=PROMPT_REF,
        altitude_target_m=3000,
        payload_weight_kg=3,
        weather_hazard_labels=(),
        now=NOW,
    )
    world_artifact = digital_twin["gazebo_world_artifact"]
    process_run = run_digital_twin_sitl_process(
        gazebo_world_artifact=world_artifact,
        repo_root=Path(__file__).resolve().parents[1],
        startup_window_seconds=float(
            os.getenv("DIGITAL_TWIN_GAZEBO_STARTUP_WINDOW_SECONDS", "8")
        ),
        cleanup_timeout_seconds=float(
            os.getenv("DIGITAL_TWIN_GAZEBO_CLEANUP_TIMEOUT_SECONDS", "5")
        ),
        now=NOW,
    )
    summary = {
        "digital_twin_gazebo_world_launch_smoke_passed": True,
        "schema_version": process_run.schema_version,
        "schema_version_expected": DIGITAL_TWIN_SITL_PROCESS_RUN_SCHEMA_VERSION,
        "process_run_ref": digital_twin_sitl_process_run_ref(process_run),
        "gazebo_world_artifact_ref": process_run.gazebo_world_artifact_ref,
        "world_file_path_or_artifact_uri": (
            process_run.world_file_path_or_artifact_uri
        ),
        "world_file_sha256": process_run.world_file_sha256,
        "process_launch_attempted": process_run.process_launch_attempted,
        "gazebo_execution_invoked": process_run.gazebo_execution_invoked,
        "px4_process_invoked": process_run.px4_process_invoked,
        "process_pids": list(process_run.process_pids),
        "command": list(process_run.command),
        "exit_status": process_run.exit_status,
        "exit_code": process_run.exit_code,
        "stdout_ref": process_run.stdout_ref,
        "stderr_ref": process_run.stderr_ref,
        "startup_error_observed": process_run.startup_error_observed,
        "simulation_only": process_run.simulation_only,
        "hardware_target_allowed": process_run.hardware_target_allowed,
        "physical_execution_invoked": process_run.physical_execution_invoked,
        "approval_free_stronger_execution_allowed": (
            process_run.approval_free_stronger_execution_allowed
        ),
        "run_hash_equals_sha256": process_run.run_hash == process_run.sha256,
        "blocked_reasons": list(process_run.blocked_reasons),
    }
    assert process_run.schema_version == DIGITAL_TWIN_SITL_PROCESS_RUN_SCHEMA_VERSION
    assert process_run.process_launch_attempted is True
    assert process_run.gazebo_execution_invoked is True
    assert process_run.process_pids
    assert process_run.world_file_path_or_artifact_uri.startswith(
        "output/digital_twin/worlds/"
    )
    assert process_run.world_file_path_or_artifact_uri.endswith(".world.sdf")
    assert process_run.simulation_only is True
    assert process_run.hardware_target_allowed is False
    assert process_run.physical_execution_invoked is False
    assert process_run.approval_free_stronger_execution_allowed is False
    assert process_run.run_hash == process_run.sha256
    return summary


def main() -> int:
    summary = run_smoke()
    print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    print(
        "SMOKE_SUMMARY_JSON "
        + json.dumps(summary, sort_keys=True, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
