"""Observed-stream persistence and completion gating for a PX4 route run.

The caller must supply each runtime observation explicitly.  This boundary
records an already-sent bounded stream, derives progress from the supplied
poses, and runs the existing completion gate.  It does not approve a route,
send a frame, infer motion from an ACK, or grant physical execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from src.runtime.px4_gazebo_coupled_delivery import (
    PX4GazeboCoupledCommandApproval,
)
from src.runtime.px4_gazebo_route_delivery import (
    PX4GazeboRouteDeliveryCompletionGate,
    build_px4_gazebo_route_delivery_completion_gate,
    run_px4_gazebo_route_delivery_task,
)
from src.runtime.px4_gazebo_route_dispatcher import (
    PX4GazeboRouteCommandAllowlist,
    PX4GazeboRouteCommandDispatchResult,
    PX4GazeboRouteProgressEvidence,
    build_px4_gazebo_route_command_dispatch_result_from_observed_stream,
    build_px4_gazebo_route_progress_evidence,
)
from src.runtime.px4_gazebo_route_plan import PX4GazeboPickupDropoffRoutePlan
from src.runtime.task_store import TaskStore


class RouteFinalizationError(RuntimeError):
    """Raised when observed route artifacts cannot be persisted safely."""


@dataclass(frozen=True)
class RouteFinalizationInputs:
    store: TaskStore
    task_id: str
    route: PX4GazeboPickupDropoffRoutePlan
    route_allowlist: PX4GazeboRouteCommandAllowlist
    approval: PX4GazeboCoupledCommandApproval
    endpoint_port: int
    target_x_m: float
    target_y_m: float
    target_z_m: float
    route_stream: Mapping[str, Any]
    pickup_pose_xy_m: tuple[float, float]
    observed_pose_xy_m: tuple[float, float]
    horizontal_route_motion_observed: bool
    px4_telemetry_correlated: bool
    gazebo_pose_correlated: bool
    actual_px4_gazebo_horizontal_smoke_observed: bool
    now: datetime
    route_progress_age_seconds: float = 0.0
    max_route_progress_age_seconds: float = 5.0
    pose_observed: bool = True
    expected_vehicle_ref: str = "gazebo_vehicle:x500_0"
    observed_vehicle_ref: str | None = "gazebo_vehicle:x500_0"


@dataclass(frozen=True)
class RouteFinalizationResult:
    dispatch: PX4GazeboRouteCommandDispatchResult
    progress: PX4GazeboRouteProgressEvidence
    gate: PX4GazeboRouteDeliveryCompletionGate
    updated_task: dict[str, Any]


def finalize_route_observation(
    inputs: RouteFinalizationInputs,
) -> RouteFinalizationResult:
    if inputs.store.get(inputs.task_id) is None:
        raise RouteFinalizationError(
            f"task {inputs.task_id} not found; cannot finalize PX4 route observation"
        )

    stream = inputs.route_stream
    dispatch = build_px4_gazebo_route_command_dispatch_result_from_observed_stream(
        route_plan=inputs.route,
        route_allowlist=inputs.route_allowlist,
        approval=inputs.approval,
        endpoint_port=inputs.endpoint_port,
        target_x_m=inputs.target_x_m,
        target_y_m=inputs.target_y_m,
        target_z_m=inputs.target_z_m,
        setpoint_frames_sent=int(stream["setpoint_frames_sent"]),
        setpoint_stream_duration_seconds=float(stream["setpoint_stream_duration_seconds"]),
        offboard_mode_switch_frame_sent=bool(stream["offboard_mode_switch_frame_sent"]),
        offboard_mode_switch_ack_observed=bool(stream["offboard_mode_switch_ack_observed"]),
        offboard_mode_switch_ack_result_code=stream["offboard_mode_switch_ack_result_code"],
        offboard_mode_switch_ack_result_name=stream["offboard_mode_switch_ack_result_name"],
        offboard_mode_switch_ack_timeout_seconds=float(
            stream["offboard_mode_switch_ack_timeout_seconds"]
        ),
        now=inputs.now,
    )
    progress = build_px4_gazebo_route_progress_evidence(
        route_plan=inputs.route,
        route_dispatch_result=dispatch,
        pickup_pose_xy_m=inputs.pickup_pose_xy_m,
        observed_pose_xy_m=inputs.observed_pose_xy_m,
        deviation_samples=stream["deviation_samples"],
        now=inputs.now,
    )
    persisted = inputs.store.update(
        inputs.task_id,
        artifacts={
            "px4_gazebo_route_command_dispatch_result": dispatch.model_dump(mode="json"),
            "px4_gazebo_route_progress_evidence": progress.model_dump(mode="json"),
        },
    )
    if persisted is None:
        raise RouteFinalizationError(
            f"task {inputs.task_id} disappeared while persisting route evidence"
        )

    gate = build_px4_gazebo_route_delivery_completion_gate(
        route_plan=inputs.route,
        route_dispatch_result=dispatch,
        route_progress_evidence=progress,
        horizontal_route_motion_observed=inputs.horizontal_route_motion_observed,
        px4_telemetry_correlated=inputs.px4_telemetry_correlated,
        gazebo_pose_correlated=inputs.gazebo_pose_correlated,
        route_progress_age_seconds=inputs.route_progress_age_seconds,
        max_route_progress_age_seconds=inputs.max_route_progress_age_seconds,
        pose_observed=inputs.pose_observed,
        expected_vehicle_ref=inputs.expected_vehicle_ref,
        observed_vehicle_ref=inputs.observed_vehicle_ref,
        actual_px4_gazebo_horizontal_smoke_observed=(
            inputs.actual_px4_gazebo_horizontal_smoke_observed
        ),
        now=inputs.now,
    )
    updated = run_px4_gazebo_route_delivery_task(
        inputs.task_id,
        completion_gate=gate,
        now=inputs.now,
        task_store_factory=lambda: inputs.store,
    )
    return RouteFinalizationResult(
        dispatch=dispatch,
        progress=progress,
        gate=gate,
        updated_task=updated,
    )


__all__ = [
    "RouteFinalizationError",
    "RouteFinalizationInputs",
    "RouteFinalizationResult",
    "finalize_route_observation",
]
