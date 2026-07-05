from __future__ import annotations

import json
from pathlib import Path

from src.runtime.turtlebot3_log_collector import (
    build_turtlebot3_nav2_log_diagnostics,
    collect_turtlebot3_log_bundle_from_paths,
    parse_turtlebot3_log_bundle_paths_env,
)


def _write_log(path: Path, *lines: str) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_turtlebot3_log_collector_builds_read_only_bundle(tmp_path: Path) -> None:
    gazebo = tmp_path / "gazebo.log"
    nav2 = tmp_path / "nav2.log"
    relay = tmp_path / "relay.log"
    sidecar = tmp_path / "sidecar.log"
    _write_log(gazebo, "Gazebo started", "world turtlebot3_world loaded")
    _write_log(nav2, "Nav2 lifecycle active", "NavigateToPose action available")
    _write_log(relay, "relay started", "velocity_generated_by_missionos=false")
    _write_log(sidecar, "telemetry sidecar started", "sample_counts odom=42")

    bundle = collect_turtlebot3_log_bundle_from_paths(
        {
            "gazebo": gazebo,
            "nav2": nav2,
            "relay": relay,
            "telemetry_sidecar": sidecar,
        },
    )

    assert bundle.schema_version == "missionos_turtlebot3_log_bundle.v1"
    assert bundle.bundle_status == "ready"
    assert bundle.raw_logs_ref.startswith("turtlebot3_process_log_bundle:")
    assert bundle.source_count == 4
    assert bundle.observed_source_count == 4
    assert bundle.raw_logs_included is False
    assert bundle.read_only is True
    assert bundle.command_payload_allowed is False
    assert bundle.physical_execution_invoked is False
    assert bundle.mission_delivery_completion_claimed is False
    assert all(record.raw_log_ref for record in bundle.records)
    assert all(record.sha256 for record in bundle.records)


def test_turtlebot3_nav2_log_diagnostics_classifies_abort_signatures(
    tmp_path: Path,
) -> None:
    gazebo = tmp_path / "gazebo.log"
    nav2 = tmp_path / "nav2.log"
    relay = tmp_path / "relay.log"
    sidecar = tmp_path / "sidecar.log"
    _write_log(gazebo, "Gazebo started", "world turtlebot3_world loaded")
    _write_log(
        nav2,
        "[controller_server]: Received a goal, begin computing control effort.",
        "[controller_server]: Failed to make progress",
        "[controller_server]: [follow_path] [ActionServer] Aborting handle.",
        "[local_costmap.local_costmap]: Received request to clear entirely the local_costmap",
        "[behavior_server]: Running spin",
        "[behavior_server]: spin completed successfully",
    )
    _write_log(relay, "relay started", "velocity_generated_by_missionos=false")
    _write_log(sidecar, "telemetry sidecar started", "sample_counts odom=42")
    bundle = collect_turtlebot3_log_bundle_from_paths(
        {
            "gazebo": gazebo,
            "nav2": nav2,
            "relay": relay,
            "telemetry_sidecar": sidecar,
        },
    )

    diagnostics = build_turtlebot3_nav2_log_diagnostics(bundle)

    assert diagnostics.schema_version == "missionos_turtlebot3_nav2_log_diagnostics.v1"
    assert diagnostics.diagnostic_status == "ready"
    assert diagnostics.read_only is True
    assert diagnostics.raw_logs_included is False
    assert diagnostics.physical_execution_invoked is False
    assert diagnostics.mission_delivery_completion_claimed is False
    assert diagnostics.goal_received_count == 1
    assert diagnostics.failed_to_make_progress_count == 1
    assert diagnostics.follow_path_abort_count == 1
    assert diagnostics.costmap_clear_count == 1
    assert diagnostics.spin_recovery_count == 2
    assert "controller_failed_to_make_progress" in diagnostics.observed_patterns
    assert "follow_path_action_aborted" in diagnostics.observed_patterns
    assert "costmap_clear_recovery_observed" in diagnostics.observed_patterns
    assert (
        "controller_progress_blocked_or_goal_inside_constrained_costmap"
        in diagnostics.failure_hypotheses
    )
    assert "recovery_goal_stalled_after_costmap_clear" in diagnostics.failure_hypotheses


def test_turtlebot3_log_collector_blocks_missing_required_source(
    tmp_path: Path,
) -> None:
    gazebo = tmp_path / "gazebo.log"
    nav2 = tmp_path / "nav2.log"
    relay = tmp_path / "relay.log"
    _write_log(gazebo, "Gazebo started")
    _write_log(nav2, "Nav2 active")
    _write_log(relay, "relay active")

    bundle = collect_turtlebot3_log_bundle_from_paths(
        {"gazebo": gazebo, "nav2": nav2, "relay": relay},
    )

    assert bundle.bundle_status == "blocked"
    assert "telemetry_sidecar" in bundle.missing_required_sources
    assert "required_turtlebot3_process_logs_missing" in bundle.blocked_reasons
    assert bundle.physical_execution_invoked is False


def test_turtlebot3_log_collector_parses_env_mapping(tmp_path: Path) -> None:
    gazebo = tmp_path / "gazebo.log"
    env = {
        "MISSIONOS_TURTLEBOT3_LOG_BUNDLE_PATHS": json.dumps(
            {"Gazebo": str(gazebo)}
        )
    }

    parsed = parse_turtlebot3_log_bundle_paths_env(env)

    assert parsed == {"gazebo": gazebo}
