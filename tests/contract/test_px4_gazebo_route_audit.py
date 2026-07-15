from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import audit


def _summary(tmp_path: Path) -> dict[str, Any]:
    for name in (
        "summary.json",
        "tasks.db",
        "px4_docker.log",
        "pose_samples.jsonl",
        "mission_artifacts.json",
    ):
        (tmp_path / name).write_text("fixture")
    return {
        "artifact_dir": str(tmp_path),
        "existing_artifacts_retained": True,
        "task_status": "completed",
        "final_status": "completed",
        "dropoff_region_reached": True,
        "blocked_reasons": [],
        "actual_px4_gazebo_horizontal_smoke_observed": True,
        "delivery_completion_claimed": True,
        "route_terminal_pose": {"phase": "route", "observed": True},
        "landing_terminal_pose": {"phase": "landing"},
        "completed_terminal_pose": {"phase": "completed"},
        "route_terminal_progress_m": 6.0,
        "horizontal_progress_m": 6.0,
        "route_geofence_violation": False,
        "pose_deviation_gate_active": True,
        "pose_deviation_aborted": False,
        "deviation_samples": [],
        "route_primitive": "bounded_position_setpoint_stream",
        "bounded_setpoint_stream_allowed": True,
        "unbounded_setpoint_stream_allowed": False,
        "offboard_mode_switch_allowed": True,
        "offboard_mode_switch_command_id": 176,
        "offboard_mode_switch_frame_sent": True,
        "offboard_mode_switch_ack_required": True,
        "offboard_mode_switch_ack_command_id": 176,
        "offboard_mode_switch_ack_observed": True,
        "offboard_mode_switch_ack_result_code": 0,
        "offboard_mode_switch_ack_result_name": "ACCEPTED",
        "route_target_x_m": 4.0,
        "route_target_y_m": 3.0,
        "route_target_z_m": -2.5,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "px4_mission_upload_allowed": False,
        "completed_pose_z_m": 0.1,
    }


def _expectations(**overrides: Any) -> audit.RouteAuditExpectations:
    values = {
        "route_target_x_m": 4.0,
        "route_target_y_m": 3.0,
        "route_target_z_m": -2.5,
        "landing_z_threshold_m": 0.15,
    }
    values.update(overrides)
    return audit.RouteAuditExpectations(**values)


def test_legacy_entrypoint_delegates_summary_audit_to_package() -> None:
    assert route_entrypoint._RouteAuditExpectations is audit.RouteAuditExpectations
    assert route_entrypoint._audit_route_summary is audit.audit_route_summary


def test_completed_summary_passes_without_promoting_new_claims(tmp_path: Path) -> None:
    summary = _summary(tmp_path)

    audit.audit_route_summary(summary, expectations=_expectations())

    assert summary["delivery_completion_claimed"] is True
    assert summary["physical_execution_invoked"] is False


def test_summary_audit_rejects_hardware_or_physical_claim(tmp_path: Path) -> None:
    summary = _summary(tmp_path)
    summary["hardware_target_allowed"] = True

    with pytest.raises(AssertionError):
        audit.audit_route_summary(summary, expectations=_expectations())


def test_route_blocking_summary_requires_blocked_truth(tmp_path: Path) -> None:
    summary = _summary(tmp_path)
    summary.update(
        {
            "task_status": "blocked",
            "final_status": "blocked",
            "dropoff_region_reached": False,
            "blocked_reasons": ["dropoff_region_not_reached"],
            "delivery_completion_claimed": False,
            "route_terminal_progress_m": 0.0,
            "horizontal_progress_m": 0.0,
            "route_blocking_verification": {
                "verification_status": "route_blocking_verified",
                "observed": {"route_blocking_verified": True},
            },
            "gazebo_route_corridor_obstacle_spawn_application": {"application_status": "applied"},
        }
    )

    audit.audit_route_summary(summary, expectations=_expectations())
    summary["final_status"] = "completed"
    with pytest.raises(AssertionError):
        audit.audit_route_summary(summary, expectations=_expectations())


def test_rth_observation_is_bounded_and_does_not_claim_delivery(tmp_path: Path) -> None:
    summary = _summary(tmp_path)
    summary.update(
        {
            "task_status": "blocked",
            "final_status": "blocked",
            "dropoff_region_reached": False,
            "blocked_reasons": ["dropoff_region_not_reached"],
            "delivery_completion_claimed": False,
            "route_terminal_progress_m": 0.0,
            "horizontal_progress_m": 0.0,
            "completed_pose_z_m": 5.0,
            "rth_behavior_observation": {
                "return_to_home_behavior_observed": True,
                "rth_commanded": True,
                "rth_state_observed": True,
                "delivery_completion_claimed": False,
            },
            "rth_execution_request": {"request_status": "approved_for_sitl_rth"},
            "rth_command_dispatch": {"mavlink_dispatch_performed": True},
        }
    )

    audit.audit_route_summary(summary, expectations=_expectations())


def test_requested_preupload_and_payload_require_observed_receipts(
    tmp_path: Path,
) -> None:
    summary = _summary(tmp_path)
    expectations = _expectations(
        preupload_requested=True,
        payload_release_requested=True,
    )
    summary.update(
        {
            "preupload_mission_performed": True,
            "preupload_mission_ack_observed": True,
            "preupload_mission_ack_type": 0,
            "preupload_mission_request_sequences": [0, 1, 2, 3],
            "payload_release_observed": True,
            "payload_release_event_source": "gazebo_detachable_joint_detach_event",
            "payload_release_position_x_m": 4.0,
            "payload_release_position_y_m": 3.0,
            "payload_release_position_z_m": 0.1,
        }
    )

    audit.audit_route_summary(summary, expectations=expectations)
    summary["payload_release_observed"] = False
    with pytest.raises(AssertionError):
        audit.audit_route_summary(summary, expectations=expectations)
