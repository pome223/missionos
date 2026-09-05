"""Serializable report projection for a PX4/Gazebo route smoke run.

The caller supplies already-decided claim, authority, observation, and status
values.  This module only projects them into the stable summary shape; it does
not approve, dispatch, verify, or promote a completion claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class RouteSummaryInputs:
    artifact_dir: Path
    recorded_at: str
    task_status: str
    existing_artifacts_retained: bool
    route_plan_schema_version: str
    route_allowlist_schema_version: str
    dispatch: Any
    progress: Any
    gate: Any
    runner: Mapping[str, Any]
    pickup_pose: Mapping[str, float]
    route_pose: Mapping[str, float]
    completed_pose: Mapping[str, float]
    delivery_completion_claimed: bool
    terminal_pose_fields: Mapping[str, Any]
    route_send: Mapping[str, Any]
    sent_target_x_m: float
    sent_target_y_m: float
    uncompensated_target_x_m: float
    uncompensated_target_y_m: float
    form2a_wind_compensation: Mapping[str, Any]
    compensation_offset_x_m: float
    compensation_offset_y_m: float
    climb_sample_count: int
    landing_sample_count: int
    preupload_summary: Mapping[str, Any] | None = None
    payload_release_summary: Mapping[str, Any] | None = None
    obstacle_supervisor_recovery_loop: Mapping[str, Any] | None = None
    wind_realism_artifacts: Mapping[str, Any] | None = None
    vehicle_realism_artifacts: Mapping[str, Any] | None = None


def build_route_summary(inputs: RouteSummaryInputs) -> dict[str, Any]:
    preupload = inputs.preupload_summary
    payload = inputs.payload_release_summary
    dispatch = inputs.dispatch
    progress = inputs.progress
    gate = inputs.gate
    route_send = inputs.route_send
    supervisor_loop = inputs.obstacle_supervisor_recovery_loop
    mission_assurance_guard = (
        dict(route_send.get("mission_assurance_live_guard"))
        if isinstance(route_send.get("mission_assurance_live_guard"), Mapping)
        else None
    )
    mission_assurance_evaluation = (
        mission_assurance_guard.get("mission_assurance_evaluation", {})
        if mission_assurance_guard is not None
        else {}
    )
    mission_assurance_evaluation = (
        mission_assurance_evaluation
        if isinstance(mission_assurance_evaluation, Mapping)
        else {}
    )
    mission_assurance_proposal = mission_assurance_evaluation.get("proposal", {})
    mission_assurance_proposal = (
        mission_assurance_proposal
        if isinstance(mission_assurance_proposal, Mapping)
        else {}
    )
    continue_execution = (
        dict(route_send.get("mission_assurance_continue_execution"))
        if isinstance(
            route_send.get("mission_assurance_continue_execution"), Mapping
        )
        else {}
    )
    continue_dropoff_approach = (
        dict(route_send.get("mission_assurance_continue_dropoff_approach"))
        if isinstance(
            route_send.get("mission_assurance_continue_dropoff_approach"), Mapping
        )
        else {}
    )
    summary = {
        "artifact_dir": str(inputs.artifact_dir),
        "recorded_at": inputs.recorded_at,
        "frozen_for_test": False,
        "task_status": inputs.task_status,
        "existing_artifacts_retained": inputs.existing_artifacts_retained,
        "route_plan_schema_version": inputs.route_plan_schema_version,
        "preupload_mission_performed": preupload is not None,
        "preupload_mission_ack_observed": (
            preupload["mission_ack_observed"] if preupload else False
        ),
        "preupload_mission_ack_type": (preupload["mission_ack_type"] if preupload else None),
        "preupload_mission_request_sequences": (
            preupload["mission_request_sequences"] if preupload else []
        ),
        "route_allowlist_schema_version": inputs.route_allowlist_schema_version,
        "dispatch_schema_version": dispatch.schema_version,
        "progress_schema_version": progress.schema_version,
        "completion_gate_schema_version": gate.schema_version,
        "runner_schema_version": inputs.runner["schema_version"],
        "final_status": inputs.runner["final_status"],
        "actual_px4_gazebo_horizontal_smoke_observed": (
            gate.actual_px4_gazebo_horizontal_smoke_observed
        ),
        "pickup_pose_xy_m": [inputs.pickup_pose["x"], inputs.pickup_pose["y"]],
        "route_pose_xy_m": [inputs.route_pose["x"], inputs.route_pose["y"]],
        "completed_pose_xy_m": [
            inputs.completed_pose["x"],
            inputs.completed_pose["y"],
        ],
        "completed_pose_z_m": inputs.completed_pose["z"],
        "horizontal_progress_m": gate.horizontal_progress_m,
        "dropoff_region_reached": gate.dropoff_region_reached,
        "delivery_completion_claimed": inputs.delivery_completion_claimed,
        "mission_assurance_live_guard": mission_assurance_guard,
        "mission_assurance_agent_invoked": (
            mission_assurance_guard is not None
            and mission_assurance_proposal.get("model_inference_invoked") is True
        ),
        "runtime_recovery_agent_invoked": (
            mission_assurance_guard is not None
            and mission_assurance_guard.get("runtime_recovery_agent_invoked")
            is True
        ),
        "mission_assurance_continue_execution": continue_execution,
        "mission_assurance_continue_dropoff_approach": continue_dropoff_approach,
        "mission_assurance_continue_dropoff_approach_observed": (
            continue_dropoff_approach.get("dropoff_approach_effect_observed")
            is True
        ),
        "dropoff_region_observed_at": continue_dropoff_approach.get(
            "dropoff_region_observed_at"
        ),
        "dropoff_region_observed_pose_x_m": (
            continue_dropoff_approach.get("approach_observed_pose", {}).get("x")
            if isinstance(
                continue_dropoff_approach.get("approach_observed_pose"), Mapping
            )
            else None
        ),
        "dropoff_region_observed_pose_y_m": (
            continue_dropoff_approach.get("approach_observed_pose", {}).get("y")
            if isinstance(
                continue_dropoff_approach.get("approach_observed_pose"), Mapping
            )
            else None
        ),
        "dropoff_region_observed_pose_z_m": (
            continue_dropoff_approach.get("approach_observed_pose", {}).get("z")
            if isinstance(
                continue_dropoff_approach.get("approach_observed_pose"), Mapping
            )
            else None
        ),
        "mission_assurance_continue_execution_invoked": (
            continue_execution.get("simulator_route_resume_invoked") is True
        ),
        "mission_assurance_continue_effect_observed": (
            continue_execution.get("route_resume_effect_observed") is True
        ),
        "mission_assurance_continue_route_completion_observed": (
            continue_execution.get("route_completion_claimed") is True
            and gate.dropoff_region_reached
            and not gate.blocked_reasons
        ),
        "recovery_command_ack_observed": False,
        "recovery_state_observed": False,
        "recovery_state_label": None,
        **inputs.terminal_pose_fields,
        "route_geofence_violation": gate.route_geofence_violation,
        "blocked_reasons": list(gate.blocked_reasons),
        "pose_deviation_gate_active": True,
        "pose_deviation_aborted": False,
        "initial_pose_deviation_aborted": route_send.get(
            "initial_pose_deviation_aborted"
        )
        is True,
        "route_stream_resumed_after_mission_assurance_continue": route_send.get(
            "route_stream_resumed_after_mission_assurance_continue"
        )
        is True,
        "deviation_samples": list(progress.deviation_samples),
        "route_monitor_sample_count": route_send["route_monitor_sample_count"],
        "setpoint_frames_sent": dispatch.setpoint_frames_sent,
        "setpoint_stream_duration_seconds": dispatch.setpoint_stream_duration_seconds,
        "route_primitive": dispatch.route_primitive,
        "route_target_x_m": dispatch.target_x_m,
        "route_target_y_m": dispatch.target_y_m,
        "route_target_z_m": dispatch.target_z_m,
        "sent_route_target_x_m": inputs.sent_target_x_m,
        "sent_route_target_y_m": inputs.sent_target_y_m,
        "uncompensated_route_target_x_m": inputs.uncompensated_target_x_m,
        "uncompensated_route_target_y_m": inputs.uncompensated_target_y_m,
        "form2a_wind_compensation": inputs.form2a_wind_compensation,
        "form2a_wind_compensation_applied": inputs.form2a_wind_compensation[
            "route_geometry_compensation_applied"
        ],
        "form2a_wind_preemptive_offset_x_m": inputs.compensation_offset_x_m,
        "form2a_wind_preemptive_offset_y_m": inputs.compensation_offset_y_m,
        "form2a_wind_feed_forward_velocity_x_mps": route_send["feed_forward_velocity_x_mps"],
        "form2a_wind_feed_forward_velocity_y_mps": route_send["feed_forward_velocity_y_mps"],
        "form2a_wind_feed_forward_phase_schedule": route_send["feed_forward_phase_schedule"],
        "form2a_wind_feed_forward_ramp_start_fraction": route_send[
            "feed_forward_ramp_start_fraction"
        ],
        "form2a_wind_feed_forward_ramp_end_fraction": route_send["feed_forward_ramp_end_fraction"],
        "form2a_wind_feed_forward_scale_min": route_send["feed_forward_scale_min"],
        "form2a_wind_feed_forward_scale_max": route_send["feed_forward_scale_max"],
        "form2a_wind_feed_forward_scale_sample_count": route_send[
            "feed_forward_scale_sample_count"
        ],
        "bounded_setpoint_stream_allowed": dispatch.bounded_setpoint_stream_allowed,
        "unbounded_setpoint_stream_allowed": dispatch.unbounded_setpoint_stream_allowed,
        "offboard_mode_switch_allowed": dispatch.offboard_mode_switch_allowed,
        "offboard_mode_switch_command_id": dispatch.offboard_mode_switch_command_id,
        "offboard_mode_switch_frame_sent": dispatch.offboard_mode_switch_frame_sent,
        "offboard_mode_switch_ack_required": dispatch.offboard_mode_switch_ack_required,
        "offboard_mode_switch_ack_command_id": dispatch.offboard_mode_switch_ack_command_id,
        "offboard_mode_switch_ack_timeout_seconds": (
            dispatch.offboard_mode_switch_ack_timeout_seconds
        ),
        "offboard_mode_switch_ack_observed": dispatch.offboard_mode_switch_ack_observed,
        "offboard_mode_switch_ack_result_code": dispatch.offboard_mode_switch_ack_result_code,
        "offboard_mode_switch_ack_result_name": dispatch.offboard_mode_switch_ack_result_name,
        "hardware_target_allowed": gate.hardware_target_allowed,
        "physical_execution_invoked": gate.physical_execution_invoked,
        "px4_mission_upload_allowed": gate.px4_mission_upload_allowed,
        "climb_sample_count": inputs.climb_sample_count,
        "landing_sample_count": inputs.landing_sample_count,
        "payload_release_observed": bool(payload and payload["payload_release_observed"]),
        "payload_release_event_source": (
            payload["payload_release_event_source"] if payload else ""
        ),
        "payload_release_observed_at": (payload["payload_release_observed_at"] if payload else ""),
        "payload_release_position_x_m": (
            payload["payload_release_position_x_m"] if payload else None
        ),
        "payload_release_position_y_m": (
            payload["payload_release_position_y_m"] if payload else None
        ),
        "payload_release_position_z_m": (
            payload["payload_release_position_z_m"] if payload else None
        ),
        "payload_release_summary": payload or {},
        "decision_loop_driver": (
            "runtime_recovery_agent_then_mission_assurance_agent"
            if mission_assurance_guard is not None
            else (
                "mission_os_supervisor"
                if supervisor_loop is not None
                else "scripted_horizontal_route_smoke"
            )
        ),
        "primary_trigger": (
            "route_blocking_obstacle_verified" if supervisor_loop is not None else None
        ),
        "supervisor_scope": ("obstacle_form3_sitl_only" if supervisor_loop is not None else None),
        "full_gateway_runtime_loop": False,
        "mission_os_supervisor_recovery_loop": supervisor_loop,
    }
    for label, extra in (
        ("wind_realism_artifacts", inputs.wind_realism_artifacts or {}),
        ("vehicle_realism_artifacts", inputs.vehicle_realism_artifacts or {}),
    ):
        overlapping_keys = sorted(set(summary).intersection(extra))
        if overlapping_keys:
            raise ValueError(
                f"{label} cannot overwrite route summary fields: " + ", ".join(overlapping_keys)
            )
        summary.update(extra)
    return summary


__all__ = ["RouteSummaryInputs", "build_route_summary"]
