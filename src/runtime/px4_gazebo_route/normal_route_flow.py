"""Coordinator for the ordinary PX4/Gazebo route branch.

The coordinator sequences an already-approved route through target compilation,
bounded streaming, terminal observation, and final report projection.  It does
not create approval, dispatch authority, simulator opt-in, or physical-execution
claims.  Every executor-facing operation is supplied by the caller, and an ACK
is accepted only as dispatch evidence; completion still comes from correlated
route progress and terminal observations.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.runtime.px4_gazebo_route.finalization import (
    RouteFinalizationInputs,
    RouteFinalizationResult,
    finalize_route_observation,
)
from src.runtime.px4_gazebo_route.observation import (
    pose_rows,
    terminal_pose_summary_fields,
)
from src.runtime.px4_gazebo_route.reporting import (
    RouteSummaryInputs,
    build_route_summary,
)
from src.runtime.px4_gazebo_route.route_decision import (
    RouteBlockingDecision,
    observe_route_blocking_decision,
)
from src.runtime.px4_gazebo_route.terminal_action import (
    RouteTerminalActionResult,
    execute_route_terminal_action,
)
from src.runtime.px4_gazebo_route_dispatcher import derive_px4_gazebo_route_target_ned
from src.runtime.px4_gazebo_route_plan import PX4GazeboPickupDropoffRoutePlan
from src.runtime.task_store import TaskStore


@dataclass(frozen=True)
class NormalRouteTarget:
    route_delta_x_m: float
    route_delta_y_m: float
    target_z_m: float
    uncompensated_target_x_m: float
    uncompensated_target_y_m: float
    sent_target_x_m: float
    sent_target_y_m: float
    compensation_offset_x_m: float
    compensation_offset_y_m: float
    feed_forward_velocity_x_mps: float
    feed_forward_velocity_y_mps: float
    wind_compensation: dict[str, Any]


@dataclass(frozen=True)
class NormalRouteTerminalProjection:
    payload_release_summary: dict[str, Any] | None
    telemetry_summary: dict[str, Any]
    vehicle_summary: dict[str, Any]
    obstacle_supervisor_recovery_loop: dict[str, Any] | None
    wind_artifacts: dict[str, Any]
    vehicle_artifacts: dict[str, Any]


@dataclass(frozen=True)
class NormalRouteFlowInputs:
    store: TaskStore
    task_id: str
    task_db_path: Path
    artifact_dir: Path
    route: PX4GazeboPickupDropoffRoutePlan
    approval: Any
    route_allowlist: Any
    endpoint_port: int
    pickup_pose: Mapping[str, float]
    climb_samples: Sequence[Mapping[str, float]]
    inject_target_offset_m: float
    collision_observation_attempts: int
    rth_requested: bool
    observed_at: datetime
    preupload_summary: Mapping[str, Any] | None
    live_pose_trace_populated: bool


@dataclass(frozen=True)
class NormalRouteRuntime:
    terrain_relative_xy_origin: Callable[[Mapping[str, float]], tuple[float, float]]
    wind_compensation_request: Callable[[], Mapping[str, Any]]
    wind_compensation_xy_offset: Callable[[Mapping[str, Any]], tuple[float, float]]
    wind_feed_forward_xy_mps: Callable[[Mapping[str, Any]], tuple[float, float]]
    assert_stream_budget: Callable[..., None]
    send_route_with_monitor: Callable[..., Mapping[str, Any]]
    dispatch_recovery: Callable[[str], tuple[Any, Any, Any]]
    handle_route_deviation: Callable[..., Any]
    pose_sample: Callable[[], Mapping[str, float]]
    append_pose_row: Callable[..., None]
    collect_route_blocking_observation: Callable[..., Mapping[str, Mapping[str, Any]]]
    record_wait_observation: Callable[[int], None]
    dispatch_rth: Callable[[], tuple[Any, Any, Any]]
    observe_recovery_state: Callable[..., tuple[Any, ...]]
    upload_alternate_mission: Callable[[], Mapping[str, Any]]
    execute_alternate_route: Callable[..., Mapping[str, Any]]
    dispatch_alternate_landing: Callable[[], tuple[Any, Any, Any]]
    send_standard_land: Callable[[], None]
    wait_for_landing: Callable[[str], tuple[Mapping[str, float], Sequence[Mapping[str, float]]]]
    project_terminal_realism: Callable[..., NormalRouteTerminalProjection]
    snapshot_task_database: Callable[..., None]
    recorded_at: Callable[[], datetime]


@dataclass(frozen=True)
class NormalRouteFlowResult:
    branch: str
    target: NormalRouteTarget
    route_stream: dict[str, Any]
    deviation_result: Any | None = None
    summary: dict[str, Any] | None = None
    fallback_pose_rows: tuple[dict[str, Any], ...] | None = None
    updated_task: dict[str, Any] | None = None
    route_pose: dict[str, float] | None = None
    terminal_action: RouteTerminalActionResult | None = None
    blocking_decision: RouteBlockingDecision | None = None
    terminal_projection: NormalRouteTerminalProjection | None = None
    finalization: RouteFinalizationResult | None = None
    recorded_at: datetime | None = None


def _target(
    inputs: NormalRouteFlowInputs,
    runtime: NormalRouteRuntime,
) -> NormalRouteTarget:
    route_delta_x, route_delta_y, target_z = derive_px4_gazebo_route_target_ned(inputs.route)
    route_origin_x, route_origin_y = runtime.terrain_relative_xy_origin(inputs.pickup_pose)
    target_x = route_origin_x + route_delta_x
    target_y = route_origin_y + route_delta_y
    wind_compensation = dict(runtime.wind_compensation_request())
    offset_x, offset_y = runtime.wind_compensation_xy_offset(wind_compensation)
    feed_forward_x, feed_forward_y = runtime.wind_feed_forward_xy_mps(wind_compensation)
    injected_offset = float(inputs.inject_target_offset_m)
    return NormalRouteTarget(
        route_delta_x_m=float(route_delta_x),
        route_delta_y_m=float(route_delta_y),
        target_z_m=float(target_z),
        uncompensated_target_x_m=float(target_x),
        uncompensated_target_y_m=float(target_y),
        sent_target_x_m=float(target_x + injected_offset + offset_x),
        sent_target_y_m=float(target_y + injected_offset + offset_y),
        compensation_offset_x_m=float(offset_x),
        compensation_offset_y_m=float(offset_y),
        feed_forward_velocity_x_mps=float(feed_forward_x),
        feed_forward_velocity_y_mps=float(feed_forward_y),
        wind_compensation=wind_compensation,
    )


def _require_observed_offboard_ack(route_stream: Mapping[str, Any]) -> None:
    required = {
        "offboard_mode_switch_allowed": True,
        "offboard_mode_switch_command_id": 176,
        "offboard_mode_switch_frame_sent": True,
        "offboard_mode_switch_ack_required": True,
        "offboard_mode_switch_ack_command_id": 176,
        "offboard_mode_switch_ack_observed": True,
        "offboard_mode_switch_ack_result_code": 0,
    }
    invalid = [key for key, expected in required.items() if route_stream.get(key) != expected]
    if invalid:
        raise RuntimeError(
            "normal route cannot continue without the required observed OFFBOARD ACK: "
            + ", ".join(invalid)
        )


def run_normal_route_flow(
    inputs: NormalRouteFlowInputs,
    *,
    runtime: NormalRouteRuntime,
) -> NormalRouteFlowResult:
    """Run the ordinary route branch without collapsing authority or evidence."""

    target = _target(inputs, runtime)
    route_duration_seconds = 25.0
    runtime.assert_stream_budget(duration_seconds=route_duration_seconds)
    recovery_approval = None
    recovery_allowlist = None
    recovery_dispatch = None

    def on_deviation() -> dict[str, Any]:
        nonlocal recovery_approval, recovery_allowlist, recovery_dispatch
        if inputs.route.on_deviation_action == "abort_only":
            return {"recovery_action_taken": None}
        recovery_approval, recovery_allowlist, recovery_dispatch = runtime.dispatch_recovery(
            inputs.route.on_deviation_action
        )
        return {
            "recovery_action_taken": inputs.route.on_deviation_action,
            "recovery_dispatch_status": recovery_dispatch.dispatch_status,
            "recovery_command_ack_observed": recovery_dispatch.command_ack_observed,
            "recovery_command_ack_result_name": recovery_dispatch.command_ack_result_name,
        }

    route_stream = dict(
        runtime.send_route_with_monitor(
            target_x=target.sent_target_x_m,
            target_y=target.sent_target_y_m,
            target_z=target.target_z_m,
            feed_forward_vx_mps=target.feed_forward_velocity_x_mps,
            feed_forward_vy_mps=target.feed_forward_velocity_y_mps,
            feed_forward_ramp_start_fraction=float(
                target.wind_compensation["feed_forward_ramp_start_fraction"]
            ),
            feed_forward_ramp_end_fraction=float(
                target.wind_compensation["feed_forward_ramp_end_fraction"]
            ),
            expected_target_x=target.uncompensated_target_x_m,
            expected_target_y=target.uncompensated_target_y_m,
            pickup_pose=inputs.pickup_pose,
            altitude_max_m=inputs.route.altitude_max_m,
            max_pose_deviation_xy_m=inputs.route.max_pose_deviation_xy_m,
            max_pose_deviation_z_m=inputs.route.max_pose_deviation_z_m,
            duration_seconds=route_duration_seconds,
            timeout=40,
            on_deviation=on_deviation,
        )
    )
    if route_stream.get("pose_deviation_aborted") is True:
        deviation = runtime.handle_route_deviation(
            route_stream=route_stream,
            recovery_approval=recovery_approval,
            recovery_allowlist=recovery_allowlist,
            recovery_dispatch=recovery_dispatch,
            target=target,
        )
        return NormalRouteFlowResult(
            branch="route_deviation",
            target=target,
            route_stream=route_stream,
            deviation_result=deviation,
        )

    _require_observed_offboard_ack(route_stream)
    route_pose = {key: float(value) for key, value in runtime.pose_sample().items()}
    runtime.append_pose_row("route", route_pose)
    blocking_decision = observe_route_blocking_decision(
        observation_attempts=inputs.collision_observation_attempts,
        rth_requested=inputs.rth_requested,
        observe_once=lambda _attempt: runtime.collect_route_blocking_observation(
            route_start_xy_m=(float(inputs.pickup_pose["x"]), float(inputs.pickup_pose["y"])),
            route_dropoff_xy_m=(
                target.uncompensated_target_x_m,
                target.uncompensated_target_y_m,
            ),
        ),
        record_wait_observation=runtime.record_wait_observation,
    )
    terminal = execute_route_terminal_action(
        rth_behavior_requested=blocking_decision.rth_behavior_requested,
        alternate_landing_requested=blocking_decision.alternate_landing_requested,
        pickup_pose=inputs.pickup_pose,
        target_z=target.target_z_m,
        altitude_max_m=inputs.route.altitude_max_m,
        route_approval=inputs.approval,
        route_allowlist=inputs.route_allowlist,
        dispatch_rth=runtime.dispatch_rth,
        observe_recovery_state=runtime.observe_recovery_state,
        current_pose=runtime.pose_sample,
        upload_alternate_mission=runtime.upload_alternate_mission,
        execute_alternate_route=runtime.execute_alternate_route,
        dispatch_alternate_landing=runtime.dispatch_alternate_landing,
        send_standard_land=runtime.send_standard_land,
        wait_for_landing=runtime.wait_for_landing,
    )
    runtime.append_pose_row(
        "rth_completed" if blocking_decision.rth_behavior_requested else "completed",
        terminal.completed_pose,
    )
    projection = runtime.project_terminal_realism(
        blocking_decision=blocking_decision,
        terminal_action=terminal,
        target=target,
    )
    finalization = finalize_route_observation(
        RouteFinalizationInputs(
            store=inputs.store,
            task_id=inputs.task_id,
            route=inputs.route,
            route_allowlist=inputs.route_allowlist,
            approval=inputs.approval,
            endpoint_port=inputs.endpoint_port,
            target_x_m=target.route_delta_x_m,
            target_y_m=target.route_delta_y_m,
            target_z_m=target.target_z_m,
            route_stream=route_stream,
            pickup_pose_xy_m=(float(inputs.pickup_pose["x"]), float(inputs.pickup_pose["y"])),
            observed_pose_xy_m=(
                terminal.completed_pose["x"],
                terminal.completed_pose["y"],
            ),
            horizontal_route_motion_observed=True,
            px4_telemetry_correlated=True,
            gazebo_pose_correlated=True,
            actual_px4_gazebo_horizontal_smoke_observed=True,
            now=inputs.observed_at,
        )
    )
    runtime.snapshot_task_database(
        task_db_path=inputs.task_db_path,
        run_dir=inputs.artifact_dir,
    )
    runner = finalization.updated_task["artifacts"]["px4_gazebo_route_delivery_runner_result"]
    recorded_at_value = runtime.recorded_at()
    delivery_completion_claimed = (
        runner["final_status"] == "completed"
        and finalization.gate.dropoff_region_reached
        and not finalization.gate.blocked_reasons
    )
    terminal_fields = terminal_pose_summary_fields(
        route_pose=route_pose,
        completed_pose=terminal.completed_pose,
        landing_samples=terminal.landing_samples,
        route_terminal_progress_m=finalization.gate.horizontal_progress_m,
    )
    summary = build_route_summary(
        RouteSummaryInputs(
            artifact_dir=inputs.artifact_dir,
            recorded_at=recorded_at_value.isoformat(),
            task_status=finalization.updated_task["status"],
            existing_artifacts_retained=(
                finalization.updated_task["artifacts"]["existing"]["kept"]
            ),
            route_plan_schema_version=inputs.route.schema_version,
            route_allowlist_schema_version=inputs.route_allowlist.schema_version,
            dispatch=finalization.dispatch,
            progress=finalization.progress,
            gate=finalization.gate,
            runner=runner,
            pickup_pose=inputs.pickup_pose,
            route_pose=route_pose,
            completed_pose=terminal.completed_pose,
            delivery_completion_claimed=delivery_completion_claimed,
            terminal_pose_fields=terminal_fields,
            route_send=route_stream,
            sent_target_x_m=target.sent_target_x_m,
            sent_target_y_m=target.sent_target_y_m,
            uncompensated_target_x_m=target.uncompensated_target_x_m,
            uncompensated_target_y_m=target.uncompensated_target_y_m,
            form2a_wind_compensation=target.wind_compensation,
            compensation_offset_x_m=target.compensation_offset_x_m,
            compensation_offset_y_m=target.compensation_offset_y_m,
            climb_sample_count=len(inputs.climb_samples),
            landing_sample_count=len(terminal.landing_samples),
            preupload_summary=inputs.preupload_summary,
            payload_release_summary=projection.payload_release_summary,
            obstacle_supervisor_recovery_loop=(
                projection.obstacle_supervisor_recovery_loop
            ),
            wind_realism_artifacts=projection.wind_artifacts,
            vehicle_realism_artifacts=projection.vehicle_artifacts,
        )
    )
    fallback_rows = None
    if not inputs.live_pose_trace_populated:
        fallback_rows = tuple(
            pose_rows(
                pickup_pose=inputs.pickup_pose,
                climb_samples=inputs.climb_samples,
                route_pose=route_pose,
                completed_pose=terminal.completed_pose,
                landing_samples=terminal.landing_samples,
            )
        )
    return NormalRouteFlowResult(
        branch="normal_route",
        target=target,
        route_stream=route_stream,
        summary=summary,
        fallback_pose_rows=fallback_rows,
        updated_task=finalization.updated_task,
        route_pose=route_pose,
        terminal_action=terminal,
        blocking_decision=blocking_decision,
        terminal_projection=projection,
        finalization=finalization,
        recorded_at=recorded_at_value,
    )


__all__ = [
    "NormalRouteFlowInputs",
    "NormalRouteFlowResult",
    "NormalRouteRuntime",
    "NormalRouteTarget",
    "NormalRouteTerminalProjection",
    "run_normal_route_flow",
]
