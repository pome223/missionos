from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import reporting


def _inputs(tmp_path: Path, **overrides: Any) -> reporting.RouteSummaryInputs:
    dispatch = SimpleNamespace(
        schema_version="px4_gazebo_route_command_dispatch.v1",
        setpoint_frames_sent=42,
        setpoint_stream_duration_seconds=3.0,
        route_primitive="bounded_position_setpoint_stream",
        target_x_m=4.0,
        target_y_m=3.0,
        target_z_m=-2.5,
        bounded_setpoint_stream_allowed=True,
        unbounded_setpoint_stream_allowed=False,
        offboard_mode_switch_allowed=True,
        offboard_mode_switch_command_id=176,
        offboard_mode_switch_frame_sent=True,
        offboard_mode_switch_ack_required=True,
        offboard_mode_switch_ack_command_id=176,
        offboard_mode_switch_ack_timeout_seconds=5.0,
        offboard_mode_switch_ack_observed=True,
        offboard_mode_switch_ack_result_code=0,
        offboard_mode_switch_ack_result_name="ACCEPTED",
    )
    progress = SimpleNamespace(
        schema_version="px4_gazebo_route_progress_evidence.v1",
        deviation_samples=(),
    )
    gate = SimpleNamespace(
        schema_version="px4_gazebo_route_completion_gate.v1",
        actual_px4_gazebo_horizontal_smoke_observed=True,
        horizontal_progress_m=6.0,
        dropoff_region_reached=True,
        route_geofence_violation=False,
        blocked_reasons=(),
        hardware_target_allowed=False,
        physical_execution_invoked=False,
        px4_mission_upload_allowed=False,
    )
    values = {
        "artifact_dir": tmp_path,
        "recorded_at": "2026-07-15T13:00:00+00:00",
        "task_status": "completed",
        "existing_artifacts_retained": True,
        "route_plan_schema_version": "px4_gazebo_route_plan.v1",
        "route_allowlist_schema_version": "px4_gazebo_route_allowlist.v1",
        "dispatch": dispatch,
        "progress": progress,
        "gate": gate,
        "runner": {
            "schema_version": "px4_gazebo_route_delivery_runner_result.v1",
            "final_status": "completed",
        },
        "pickup_pose": {"x": 0.0, "y": 0.0, "z": 0.0},
        "route_pose": {"x": 3.5, "y": 2.5, "z": -2.5},
        "completed_pose": {"x": 4.0, "y": 3.0, "z": 0.1},
        "delivery_completion_claimed": False,
        "terminal_pose_fields": {
            "route_terminal_pose": {"phase": "route", "observed": True},
            "landing_terminal_pose": {"phase": "landing"},
            "completed_terminal_pose": {"phase": "completed"},
            "route_terminal_progress_m": 6.0,
        },
        "route_send": {
            "route_monitor_sample_count": 3,
            "feed_forward_velocity_x_mps": 0.0,
            "feed_forward_velocity_y_mps": 0.0,
            "feed_forward_phase_schedule": "full_then_linear_ramp_down",
            "feed_forward_ramp_start_fraction": 0.65,
            "feed_forward_ramp_end_fraction": 0.9,
            "feed_forward_scale_min": 0.0,
            "feed_forward_scale_max": 1.0,
            "feed_forward_scale_sample_count": 3,
        },
        "sent_target_x_m": 4.0,
        "sent_target_y_m": 3.0,
        "uncompensated_target_x_m": 4.0,
        "uncompensated_target_y_m": 3.0,
        "form2a_wind_compensation": {"route_geometry_compensation_applied": False},
        "compensation_offset_x_m": 0.0,
        "compensation_offset_y_m": 0.0,
        "climb_sample_count": 2,
        "landing_sample_count": 4,
    }
    values.update(overrides)
    return reporting.RouteSummaryInputs(**values)


def test_legacy_entrypoint_delegates_summary_projection_to_package() -> None:
    assert route_entrypoint._RouteSummaryInputs is reporting.RouteSummaryInputs
    assert route_entrypoint._build_route_summary is reporting.build_route_summary


def test_summary_projection_keeps_caller_claim_and_authority_values(
    tmp_path: Path,
) -> None:
    summary = reporting.build_route_summary(_inputs(tmp_path))

    assert summary["delivery_completion_claimed"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["route_target_x_m"] == 4.0
    assert summary["payload_release_observed"] is False
    assert summary["decision_loop_driver"] == "scripted_horizontal_route_smoke"


def test_summary_projection_includes_explicit_receipts_and_supervisor_loop(
    tmp_path: Path,
) -> None:
    payload = {
        "payload_release_observed": True,
        "payload_release_event_source": "gazebo_detachable_joint_detach_event",
        "payload_release_observed_at": "2026-07-15T13:00:01+00:00",
        "payload_release_position_x_m": 4.0,
        "payload_release_position_y_m": 3.0,
        "payload_release_position_z_m": 0.1,
    }
    supervisor = {"schema_version": "mission_os_supervisor_recovery_loop.v1"}
    summary = reporting.build_route_summary(
        _inputs(
            tmp_path,
            preupload_summary={
                "mission_ack_observed": True,
                "mission_ack_type": 0,
                "mission_request_sequences": [0, 1, 2, 3],
            },
            payload_release_summary=payload,
            obstacle_supervisor_recovery_loop=supervisor,
        )
    )

    assert summary["preupload_mission_performed"] is True
    assert summary["payload_release_summary"] == payload
    assert summary["decision_loop_driver"] == "mission_os_supervisor"
    assert summary["primary_trigger"] == "route_blocking_obstacle_verified"
    assert summary["full_gateway_runtime_loop"] is False


def test_summary_projection_merges_disjoint_realism_artifacts(tmp_path: Path) -> None:
    summary = reporting.build_route_summary(
        _inputs(
            tmp_path,
            wind_realism_artifacts={"wind_profile": {"requested": True}},
            vehicle_realism_artifacts={"vehicle_profile": {"requested": True}},
        )
    )

    assert summary["wind_profile"] == {"requested": True}
    assert summary["vehicle_profile"] == {"requested": True}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("delivery_completion_claimed", True),
        ("physical_execution_invoked", True),
        ("route_target_x_m", 99.0),
    ],
)
def test_realism_artifacts_cannot_overwrite_core_summary_fields(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    with pytest.raises(ValueError, match="cannot overwrite"):
        reporting.build_route_summary(_inputs(tmp_path, wind_realism_artifacts={field: value}))
