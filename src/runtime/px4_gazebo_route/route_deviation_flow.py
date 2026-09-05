"""Coordinator for the route-deviation recovery terminal branch.

The route monitor supplies any already-created primary approval, allowlist, and
dispatch artifacts.  This flow observes that dispatch, optionally requests one
new separately approved post-recovery action, persists the exact artifacts, and
projects a report.  ACK remains distinct from observed recovery completion,
and no route-recovery result is promoted to delivery or physical execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.runtime.px4_gazebo_route import supervision
from src.runtime.px4_gazebo_route.recovery_execution import ObservedRecoveryCycle
from src.runtime.px4_gazebo_route.recovery_persistence import (
    RouteDeviationRecoveryPersistenceInputs,
    persist_route_deviation_recovery,
)
from src.runtime.px4_gazebo_route.recovery_reporting import (
    RouteDeviationRecoverySummaryInputs,
    build_route_deviation_recovery_summary,
    recovery_pose_rows,
)
from src.runtime.px4_gazebo_route.recovery_workflow import (
    RouteDeviationRecoveryWorkflow,
    assemble_route_deviation_recovery,
)
from src.runtime.task_store import TaskStore


@dataclass(frozen=True)
class RouteDeviationRealismRefresh:
    route_blocking_verification_summary: dict[str, Any]
    vehicle_summary: dict[str, Any]
    battery_summary: dict[str, Any]
    telemetry_summary: dict[str, Any]
    wind_artifacts: dict[str, Any]
    vehicle_artifacts: dict[str, Any]


@dataclass(frozen=True)
class RouteDeviationFlowInputs:
    store: TaskStore
    task_id: str
    artifact_dir: Path
    route: Any
    route_allowlist: Any
    route_stream: Mapping[str, Any]
    pickup_pose: Mapping[str, float]
    post_recovery_action: str
    recovery_approval: Any | None
    recovery_allowlist: Any | None
    recovery_dispatch: Any | None
    supervisor_loop_requested: bool
    multi_condition_supervisor_requested: bool
    wind_requested_profile: Mapping[str, Any]
    observed_at: datetime


@dataclass(frozen=True)
class RouteDeviationRuntime:
    build_deviation_abort: Callable[..., Any]
    observe_dispatched_recovery: Callable[..., ObservedRecoveryCycle]
    build_recovery_completion: Callable[..., Any]
    observe_recovery_state: Callable[..., tuple[Any, ...]]
    dispatch_recovery: Callable[[str], tuple[Any, Any, Any]]
    refresh_realism: Callable[[], RouteDeviationRealismRefresh]


@dataclass(frozen=True)
class RouteDeviationFlowResult:
    summary: dict[str, Any]
    pose_rows: tuple[dict[str, Any], ...]
    updated_task: dict[str, Any]
    workflow: RouteDeviationRecoveryWorkflow
    refresh: RouteDeviationRealismRefresh
    deviation_abort: Any


def run_route_deviation_flow(
    inputs: RouteDeviationFlowInputs,
    *,
    runtime: RouteDeviationRuntime,
) -> RouteDeviationFlowResult:
    """Observe and persist one already-triggered route-deviation branch."""

    route_stream = inputs.route_stream
    abort = runtime.build_deviation_abort(
        route_plan=inputs.route,
        route_allowlist=inputs.route_allowlist,
        deviation_samples=route_stream["deviation_samples"],
        route_monitor_sample_count=int(route_stream["route_monitor_sample_count"]),
        now=inputs.observed_at,
    )

    workflow = assemble_route_deviation_recovery()
    post_approval = None
    post_allowlist = None
    post_dispatch = None
    if inputs.recovery_dispatch is not None:
        primary_cycle = runtime.observe_dispatched_recovery(
            action=inputs.route.on_deviation_action,
            approval=inputs.recovery_approval,
            dispatch=inputs.recovery_dispatch,
            pickup_pose=inputs.pickup_pose,
            observe_state=runtime.observe_recovery_state,
            deviation_abort=abort,
            build_completion=runtime.build_recovery_completion,
            observed_at=inputs.observed_at,
        )
        workflow = assemble_route_deviation_recovery(primary=primary_cycle)
        if workflow.primary.outcome.completed and inputs.post_recovery_action != "none":
            post_approval, post_allowlist, post_dispatch = runtime.dispatch_recovery(
                inputs.post_recovery_action
            )
            post_cycle = runtime.observe_dispatched_recovery(
                action=inputs.post_recovery_action,
                approval=post_approval,
                dispatch=post_dispatch,
                pickup_pose=inputs.pickup_pose,
                observe_state=runtime.observe_recovery_state,
                deviation_abort=abort,
                build_completion=runtime.build_recovery_completion,
                observed_at=inputs.observed_at,
            )
            workflow = assemble_route_deviation_recovery(
                primary=workflow.primary,
                post=post_cycle,
            )

    updated = persist_route_deviation_recovery(
        RouteDeviationRecoveryPersistenceInputs(
            store=inputs.store,
            task_id=inputs.task_id,
            workflow=workflow,
            deviation_abort=abort,
            approval=inputs.recovery_approval,
            allowlist=inputs.recovery_allowlist,
            dispatch=inputs.recovery_dispatch,
            post_approval=post_approval,
            post_allowlist=post_allowlist,
            post_dispatch=post_dispatch,
            mission_assurance_live_guard=(
                route_stream.get("mission_assurance_live_guard")
                if isinstance(route_stream.get("mission_assurance_live_guard"), Mapping)
                else None
            ),
        )
    )
    refreshed = runtime.refresh_realism()
    supervisor_loop = None
    if inputs.recovery_dispatch is not None and (
        inputs.supervisor_loop_requested or inputs.multi_condition_supervisor_requested
    ):
        supervisor_loop = supervision.build_wind_recovery_loop_from_outcomes(
            deviation_samples=route_stream["deviation_samples"],
            primary_outcome=workflow.primary.outcome,
            post_outcome=workflow.post.outcome,
            wind_requested_profile=inputs.wind_requested_profile,
            route_blocking_verification_summary=(refreshed.route_blocking_verification_summary),
            vehicle_realism_summary=refreshed.vehicle_summary,
            battery_realism_summary=refreshed.battery_summary,
            telemetry_realism_summary=refreshed.telemetry_summary,
            supervisor_scope=(
                supervision.MULTI_CONDITION_SUPERVISOR_SCOPE
                if inputs.multi_condition_supervisor_requested
                else supervision.WIND_SUPERVISOR_SCOPE
            ),
        )

    summary = build_route_deviation_recovery_summary(
        RouteDeviationRecoverySummaryInputs(
            artifact_dir=inputs.artifact_dir,
            task_status=updated["status"],
            existing_artifacts_retained=updated["artifacts"]["existing"]["kept"],
            final_status=workflow.final_status,
            deviation_abort=abort,
            route=inputs.route,
            route_stream=route_stream,
            recovery_outcome=workflow.primary.outcome,
            recovery_completion=workflow.primary.completion,
            post_recovery_outcome=workflow.post.outcome,
            supervisor_loop=supervisor_loop,
            wind_realism_artifacts=refreshed.wind_artifacts,
            vehicle_realism_artifacts=refreshed.vehicle_artifacts,
        )
    )
    rows = recovery_pose_rows(
        primary_phase=f"recovery_{inputs.route.on_deviation_action}",
        primary_samples=workflow.primary.samples,
        primary_pose=workflow.primary.pose,
        primary_completed_phase="recovery_completed",
        post_phase=f"post_recovery_{inputs.post_recovery_action}",
        post_samples=workflow.post.samples,
        post_pose=workflow.post.pose,
        post_completed_phase="post_recovery_completed",
    )
    return RouteDeviationFlowResult(
        summary=summary,
        pose_rows=tuple(rows),
        updated_task=updated,
        workflow=workflow,
        refresh=refreshed,
        deviation_abort=abort,
    )


__all__ = [
    "RouteDeviationFlowInputs",
    "RouteDeviationFlowResult",
    "RouteDeviationRealismRefresh",
    "RouteDeviationRuntime",
    "run_route_deviation_flow",
]
