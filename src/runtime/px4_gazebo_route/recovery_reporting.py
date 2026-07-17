"""Serializable report projections for already-observed recovery outcomes.

These builders do not approve, dispatch, observe, verify, or promote recovery
or delivery completion.  They retain the distinction between command ACK,
observed state, bounded recovery completion, and delivery completion.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.runtime.px4_gazebo_route.recovery_outcomes import RecoveryCycleOutcome


@dataclass(frozen=True)
class PayloadRecoverySummaryInputs:
    artifact_dir: Path
    task_status: str
    existing_artifacts_retained: bool
    final_status: str
    advisory_ref: str
    payload_action_ref: str
    payload_outcome: RecoveryCycleOutcome
    payload_action_artifact: Mapping[str, Any]
    payload_route_progress_payload: Mapping[str, Any] | None
    payload_route_progress_away_from_pickup_observed: bool
    payload_pre_recovery_distance_to_pickup_m: float | None
    payload_recovery_distance_to_pickup_m: float | None
    post_recovery_outcome: RecoveryCycleOutcome
    payload_post_recovery_action_ref: str | None
    payload_post_recovery_action_artifact: Mapping[str, Any] | None
    supervisor_loop: Mapping[str, Any] | None
    wind_realism_artifacts: Mapping[str, Any]
    vehicle_realism_artifacts: Mapping[str, Any]


@dataclass(frozen=True)
class RouteDeviationRecoverySummaryInputs:
    artifact_dir: Path
    task_status: str
    existing_artifacts_retained: bool
    final_status: str
    deviation_abort: Any
    route: Any
    route_stream: Mapping[str, Any]
    recovery_outcome: RecoveryCycleOutcome
    recovery_completion: Any | None
    post_recovery_outcome: RecoveryCycleOutcome
    supervisor_loop: Mapping[str, Any] | None
    wind_realism_artifacts: Mapping[str, Any]
    vehicle_realism_artifacts: Mapping[str, Any]


def _merge_non_overlapping(
    summary: dict[str, Any],
    *,
    label: str,
    values: Mapping[str, Any],
) -> None:
    overlapping = sorted(set(summary).intersection(values))
    if overlapping:
        raise ValueError(
            f"{label} cannot overwrite recovery summary fields: "
            + ", ".join(overlapping)
        )
    summary.update(values)


def build_payload_recovery_summary(
    inputs: PayloadRecoverySummaryInputs,
) -> dict[str, Any]:
    payload = inputs.payload_outcome
    post = inputs.post_recovery_outcome
    loop = inputs.supervisor_loop
    summary = {
        "artifact_dir": str(inputs.artifact_dir),
        "task_status": inputs.task_status,
        "existing_artifacts_retained": inputs.existing_artifacts_retained,
        "final_status": inputs.final_status,
        "actual_px4_gazebo_horizontal_smoke_observed": True,
        "dropoff_region_reached": False,
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
        "payload_feasibility_advisory_ref": inputs.advisory_ref,
        "payload_recovery_action_ref": inputs.payload_action_ref,
        "payload_advisory_consumed_by_ref": inputs.payload_action_ref,
        "payload_recovery_action": payload.action,
        "payload_recovery_approval_ref": payload.approval_ref,
        "payload_recovery_dispatch_ref": payload.dispatch_ref,
        "payload_recovery_dispatch_status": payload.dispatch_status,
        "payload_recovery_command_ack_observed": payload.command_ack_observed,
        "payload_recovery_command_ack_result_name": (
            payload.command_ack_result_name
        ),
        "payload_recovery_state_observed": payload.state_observed,
        "payload_recovery_state_label": payload.state_label,
        "payload_recovery_completed": payload.completed,
        "payload_recovery_pose_z_m": payload.pose_z_m,
        "payload_route_progress_payload": inputs.payload_route_progress_payload,
        "payload_route_progress_away_from_pickup_observed": (
            inputs.payload_route_progress_away_from_pickup_observed
        ),
        "payload_pre_recovery_distance_to_pickup_m": (
            inputs.payload_pre_recovery_distance_to_pickup_m
        ),
        "payload_recovery_distance_to_pickup_m": (
            inputs.payload_recovery_distance_to_pickup_m
        ),
        "payload_recovery_action_artifact": dict(
            inputs.payload_action_artifact
        ),
        "post_recovery_action_taken": post.action,
        "post_recovery_dispatch_ref": post.dispatch_ref,
        "post_recovery_dispatch_status": post.dispatch_status,
        "post_recovery_command_ack_observed": post.command_ack_observed,
        "post_recovery_command_ack_result_name": post.command_ack_result_name,
        "post_recovery_state_observed": post.state_observed,
        "post_recovery_state_label": post.state_label,
        "post_recovery_completed": post.completed,
        "post_recovery_pose_z_m": post.pose_z_m,
        "payload_supervisor_post_recovery_action_ref": (
            inputs.payload_post_recovery_action_ref
        ),
        "payload_supervisor_post_recovery_action_artifact": (
            None
            if inputs.payload_post_recovery_action_artifact is None
            else dict(inputs.payload_post_recovery_action_artifact)
        ),
        "decision_loop_driver": (
            "mission_os_supervisor"
            if loop is not None
            else "scripted_payload_recovery_smoke"
        ),
        "supervisor_scope": (
            "payload_form3_sitl_only" if loop is not None else None
        ),
        "full_gateway_runtime_loop": False,
        "supervisor_loop_claim_supported": (
            None if loop is None else loop["supervisor_loop_claim_supported"]
        ),
        "mission_os_supervisor_recovery_loop": (
            None if loop is None else dict(loop)
        ),
        "setpoint_frames_sent": 0,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "px4_mission_upload_allowed": False,
    }
    _merge_non_overlapping(
        summary,
        label="wind_realism_artifacts",
        values=inputs.wind_realism_artifacts,
    )
    _merge_non_overlapping(
        summary,
        label="vehicle_realism_artifacts",
        values=inputs.vehicle_realism_artifacts,
    )
    return summary


def build_route_deviation_recovery_summary(
    inputs: RouteDeviationRecoverySummaryInputs,
) -> dict[str, Any]:
    recovery = inputs.recovery_outcome
    post = inputs.post_recovery_outcome
    stream = inputs.route_stream
    loop = inputs.supervisor_loop
    completion = inputs.recovery_completion
    summary = {
        "artifact_dir": str(inputs.artifact_dir),
        "task_status": inputs.task_status,
        "existing_artifacts_retained": inputs.existing_artifacts_retained,
        "final_status": inputs.final_status,
        "actual_px4_gazebo_horizontal_smoke_observed": True,
        "dropoff_region_reached": False,
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
        "deviation_abort_schema_version": inputs.deviation_abort.schema_version,
        "deviation_abort_ref": (
            "px4_gazebo_route_deviation_abort:"
            f"{inputs.deviation_abort.abort_id}"
        ),
        "route_plan_schema_version": inputs.route.schema_version,
        "on_deviation_action": inputs.route.on_deviation_action,
        "pose_deviation_gate_active": True,
        "pose_deviation_aborted": True,
        "deviation_samples": stream["deviation_samples"],
        "route_monitor_sample_count": stream["route_monitor_sample_count"],
        "route_stream_terminated_before_recovery_dispatch": stream[
            "route_stream_terminated_before_recovery_dispatch"
        ],
        "route_stream_process_returncode": stream[
            "route_stream_process_returncode"
        ],
        "route_stream_stop_reason": stream["route_stream_stop_reason"],
        "route_stream_forced_kill": stream["route_stream_forced_kill"],
        "recovery_action_taken": recovery.action,
        "recovery_dispatch_ref": recovery.dispatch_ref,
        "recovery_approval_ref": recovery.approval_ref,
        "recovery_completion_ref": recovery.completion_ref,
        "recovery_completion_schema_version": (
            None if completion is None else completion.schema_version
        ),
        "recovery_completed": recovery.completed,
        "recovery_completion_basis": recovery.completion_basis,
        "recovery_ack_complete": recovery.ack_complete,
        "recovery_state_observed": recovery.state_observed,
        "recovery_state_label": recovery.state_label,
        "recovery_dispatch_status": recovery.dispatch_status,
        "recovery_command_ack_observed": recovery.command_ack_observed,
        "recovery_command_ack_result_name": recovery.command_ack_result_name,
        "recovery_pose_z_m": recovery.pose_z_m,
        "post_recovery_action_taken": post.action,
        "post_recovery_dispatch_ref": post.dispatch_ref,
        "post_recovery_approval_ref": post.approval_ref,
        "post_recovery_completion_ref": post.completion_ref,
        "post_recovery_completed": post.completed,
        "post_recovery_completion_basis": post.completion_basis,
        "post_recovery_ack_complete": post.ack_complete,
        "post_recovery_state_observed": post.state_observed,
        "post_recovery_state_label": post.state_label,
        "post_recovery_dispatch_status": post.dispatch_status,
        "post_recovery_command_ack_observed": post.command_ack_observed,
        "post_recovery_command_ack_result_name": post.command_ack_result_name,
        "post_recovery_pose_z_m": post.pose_z_m,
        "setpoint_frames_sent": 0,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "px4_mission_upload_allowed": False,
        "decision_loop_driver": (
            "mission_os_supervisor"
            if loop is not None
            else "scripted_horizontal_route_smoke"
        ),
        "supervisor_scope": (
            loop.get("supervisor_scope") if loop is not None else None
        ),
        "full_gateway_runtime_loop": False,
        "mission_os_supervisor_recovery_loop": (
            None if loop is None else dict(loop)
        ),
    }
    _merge_non_overlapping(
        summary,
        label="wind_realism_artifacts",
        values=inputs.wind_realism_artifacts,
    )
    _merge_non_overlapping(
        summary,
        label="vehicle_realism_artifacts",
        values=inputs.vehicle_realism_artifacts,
    )
    return summary


def recovery_pose_rows(
    *,
    primary_phase: str,
    primary_samples: Sequence[Mapping[str, float]],
    primary_pose: Mapping[str, float] | None,
    primary_completed_phase: str,
    post_phase: str,
    post_samples: Sequence[Mapping[str, float]],
    post_pose: Mapping[str, float] | None,
    post_completed_phase: str,
    pre_phase: str | None = None,
    pre_pose: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if pre_phase is not None and pre_pose is not None:
        rows.append({"phase": pre_phase, "sample": dict(pre_pose)})
    rows.extend(
        {
            "phase": primary_phase,
            "sample_index": index,
            "sample": dict(sample),
        }
        for index, sample in enumerate(primary_samples)
    )
    if primary_pose is not None:
        rows.append(
            {"phase": primary_completed_phase, "sample": dict(primary_pose)}
        )
    rows.extend(
        {
            "phase": post_phase,
            "sample_index": index,
            "sample": dict(sample),
        }
        for index, sample in enumerate(post_samples)
    )
    if post_pose is not None:
        rows.append({"phase": post_completed_phase, "sample": dict(post_pose)})
    return rows


__all__ = [
    "PayloadRecoverySummaryInputs",
    "RouteDeviationRecoverySummaryInputs",
    "build_payload_recovery_summary",
    "build_route_deviation_recovery_summary",
    "recovery_pose_rows",
]
