#!/usr/bin/env python3
"""Runtime smoke for redacted Mission Control review reports."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

from src.runtime.px4_gazebo_delivery_mission_control import (
    PX4GazeboDeliveryMissionFailureType,
    PX4GazeboDeliveryMissionPhase,
    build_px4_gazebo_delivery_mission_contract,
    run_px4_gazebo_delivery_mission_v1,
)
from src.runtime.px4_gazebo_fleet_memory import (
    run_px4_gazebo_fleet_memory_feedback_simulation,
)
from src.runtime.px4_gazebo_mission_review import (
    run_px4_gazebo_mission_control_review_report,
    write_px4_gazebo_mission_review_archive,
)

OPT_IN_ENV = "RUN_PX4_GAZEBO_MISSION_CONTROL_REVIEW_SMOKE"
ARTIFACT_ROOT_ENV = "PX4_GAZEBO_MISSION_REVIEW_ARTIFACT_ROOT"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the Mission Control review report smoke."
        )


def main() -> int:
    _require_opt_in()
    contract = build_px4_gazebo_delivery_mission_contract(
        route_plan_refs=(
            "px4_gazebo_pickup_dropoff_route_plan:pickup_to_waypoint",
            "px4_gazebo_pickup_dropoff_route_plan:waypoint_alpha_to_bravo",
            "px4_gazebo_pickup_dropoff_route_plan:waypoint_to_dropoff",
        ),
        waypoint_refs=(
            "gazebo_waypoint:alpha",
            "gazebo_waypoint:bravo",
            "gazebo_waypoint:charlie",
        ),
        now=NOW,
    )
    happy = run_px4_gazebo_delivery_mission_v1(
        mission_contract=contract,
        route_dispatch_refs=(
            "px4_gazebo_route_command_dispatch_result:leg_pickup_to_waypoint",
            "px4_gazebo_route_command_dispatch_result:leg_waypoint_alpha_to_bravo",
            "px4_gazebo_route_command_dispatch_result:leg_waypoint_to_dropoff",
        ),
        route_completion_gate_refs=(
            "px4_gazebo_route_delivery_completion_gate:leg_pickup_to_waypoint",
            "px4_gazebo_route_delivery_completion_gate:leg_waypoint_alpha_to_bravo",
            "px4_gazebo_route_delivery_completion_gate:leg_waypoint_to_dropoff",
        ),
        now=NOW,
    )
    blocked = run_px4_gazebo_delivery_mission_v1(
        mission_contract=contract,
        failure_phase=PX4GazeboDeliveryMissionPhase.DELIVERY_ROUTE,
        failure_type=PX4GazeboDeliveryMissionFailureType.POSE_DEVIATION,
        now=NOW,
    )
    fleet_memory = run_px4_gazebo_fleet_memory_feedback_simulation(
        happy_runner_result=happy["runner_result"],
        happy_replay_timeline=happy["replay_timeline"],
        blocked_runner_result=blocked["runner_result"],
        blocked_replay_timeline=blocked["replay_timeline"],
        mission_contract_ref=(
            f"px4_gazebo_delivery_mission_contract:{contract.mission_contract_id}"
        ),
        now=NOW,
    )
    report_artifacts = run_px4_gazebo_mission_control_review_report(
        runner_result=happy["runner_result"],
        replay_timeline=happy["replay_timeline"],
        fleet_memory_artifacts=fleet_memory,
        now=NOW,
    )
    report = report_artifacts["evidence_report"]
    replay_index = report_artifacts["replay_index"]
    safety = report_artifacts["safety_boundary_summary"]
    provenance = report_artifacts["fleet_memory_provenance_summary"]
    markdown = report_artifacts["redacted_markdown"]
    html = report_artifacts["redacted_html"]
    artifact_root = Path(
        os.getenv(ARTIFACT_ROOT_ENV, "output/mission_control_review_reports")
    )
    run_dir = artifact_root / "review_report_20260101T120000Z"
    archive_paths = write_px4_gazebo_mission_review_archive(
        output_dir=run_dir,
        report=report,
        replay_index=replay_index,
        safety_boundary_summary=safety,
        fleet_memory_provenance=provenance,
    )
    summary = {
        "schema_version": "px4_gazebo_mission_control_review_report_smoke.v1",
        "report_schema_version": report.schema_version,
        "replay_index_schema_version": replay_index.schema_version,
        "safety_boundary_summary_schema_version": safety.schema_version,
        "fleet_memory_provenance_summary_schema_version": provenance.schema_version,
        "final_status": report.final_status,
        "why_completed_or_blocked": list(report.why_completed_or_blocked),
        "replay_event_count": replay_index.event_count,
        "evidence_chain_count": len(report.evidence_chain),
        "fleet_memory_provenance_ref_present": (
            report.fleet_memory_provenance_ref is not None
        ),
        "redacted_markdown_line_count": len(markdown.splitlines()),
        "redacted_html_contains_timeline": "Replay Timeline" in html,
        "archive_dir": str(run_dir),
        "report_json": archive_paths["report_json"],
        "report_markdown": archive_paths["report_markdown"],
        "report_html": archive_paths["report_html"],
        "raw_logs_included": report.raw_logs_included,
        "sqlite_included": report.sqlite_included,
        "full_telemetry_included": report.full_telemetry_included,
        "reproduction_steps_included": report.reproduction_steps_included,
        "runtime_script_names_included": report.runtime_script_names_included,
        "transport_details_included": report.transport_details_included,
        "low_level_command_details_included": (
            report.low_level_command_details_included
        ),
        "hardware_target_allowed": safety.hardware_target_allowed,
        "physical_execution_invoked": safety.physical_execution_invoked,
        "px4_mission_upload_allowed": safety.px4_mission_upload_allowed,
        "unbounded_setpoint_stream_allowed": (safety.unbounded_setpoint_stream_allowed),
        "memory_direct_command_authority_allowed": (
            provenance.memory_direct_command_authority_allowed
        ),
        "memory_grants_dispatch_authority": provenance.memory_grants_dispatch_authority,
        "memory_used_for_planning_only": provenance.memory_used_for_planning_only,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    assert summary["final_status"] == "completed"
    assert "all_required_phases_observed" in summary["why_completed_or_blocked"]
    assert summary["replay_event_count"] > 0
    assert summary["evidence_chain_count"] >= 4
    assert summary["fleet_memory_provenance_ref_present"] is True
    assert summary["redacted_html_contains_timeline"] is True
    assert Path(summary["report_json"]).exists()
    assert Path(summary["report_markdown"]).exists()
    assert Path(summary["report_html"]).exists()
    assert summary["raw_logs_included"] is False
    assert summary["sqlite_included"] is False
    assert summary["full_telemetry_included"] is False
    assert summary["reproduction_steps_included"] is False
    assert summary["runtime_script_names_included"] is False
    assert summary["transport_details_included"] is False
    assert summary["low_level_command_details_included"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["px4_mission_upload_allowed"] is False
    assert summary["unbounded_setpoint_stream_allowed"] is False
    assert summary["memory_direct_command_authority_allowed"] is False
    assert summary["memory_grants_dispatch_authority"] is False
    assert summary["memory_used_for_planning_only"] is True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
