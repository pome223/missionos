"""Coordinator for the explicit payload-recovery terminal branch.

This flow starts only after the caller has selected the opt-in payload-recovery
scenario.  It may invoke caller-supplied dispatch functions, but it cannot mint
authority by itself: each dispatched cycle must return its own approval,
allowlist, and dispatch artifacts.  Completion is derived from observed cycle
outcomes, never from command ACK alone, and never upgrades payload recovery to
delivery or physical-execution completion.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.runtime.px4_gazebo_route import supervision
from src.runtime.px4_gazebo_route.recovery_execution import (
    ObservedRecoveryCycle,
    observe_dispatched_recovery,
)
from src.runtime.px4_gazebo_route.recovery_outcomes import (
    PAYLOAD_RECOVERY_ACTION_REF,
    RecoveryCycleOutcome,
    build_payload_post_recovery_action,
    build_payload_recovery_action,
    payload_recovery_terminal_status,
    recovery_task_artifacts,
)
from src.runtime.px4_gazebo_route.recovery_reporting import (
    PayloadRecoverySummaryInputs,
    build_payload_recovery_summary,
    recovery_pose_rows,
)
from src.runtime.task_store import TaskStore


@dataclass(frozen=True)
class PayloadRecoveryRealismRefresh:
    telemetry_summary: dict[str, Any]
    vehicle_summary: dict[str, Any]
    wind_artifacts: dict[str, Any]
    vehicle_artifacts: dict[str, Any]


@dataclass(frozen=True)
class PayloadRecoveryFlowInputs:
    store: TaskStore
    task_id: str
    artifact_dir: Path
    pickup_pose: Mapping[str, float]
    route_target_ned: tuple[float, float, float]
    route_altitude_max_m: float
    payload_action: str
    advisory_ref: str
    supervisor_advisory_ref: str
    supervisor_loop_requested: bool
    post_recovery_action: str
    vehicle_realism_summary: Mapping[str, Any]
    battery_realism_summary: Mapping[str, Any]
    telemetry_realism_summary: Mapping[str, Any]


@dataclass(frozen=True)
class PayloadRecoveryRuntime:
    terrain_relative_xy_origin: Callable[[Mapping[str, float]], tuple[float, float]]
    assert_stream_budget: Callable[..., None]
    send_route_with_monitor: Callable[..., Mapping[str, Any]]
    pose_sample: Callable[[], Mapping[str, float]]
    append_pose_row: Callable[..., None]
    dispatch_recovery: Callable[[str], tuple[Any, Any, Any]]
    observe_recovery_state: Callable[..., tuple[Any, ...]]
    refresh_realism: Callable[[], PayloadRecoveryRealismRefresh]
    observed_at_iso: Callable[[], str]


@dataclass(frozen=True)
class PayloadRecoveryFlowResult:
    summary: dict[str, Any]
    pose_rows: tuple[dict[str, Any], ...]
    updated_task: dict[str, Any]
    telemetry_summary: dict[str, Any]
    vehicle_summary: dict[str, Any]
    primary_cycle: ObservedRecoveryCycle
    post_cycle: ObservedRecoveryCycle | None


def _distance_xy(
    first: Mapping[str, float],
    second: Mapping[str, float],
) -> float:
    dx = float(first["x"]) - float(second["x"])
    dy = float(first["y"]) - float(second["y"])
    return (dx * dx + dy * dy) ** 0.5


def run_payload_recovery_flow(
    inputs: PayloadRecoveryFlowInputs,
    *,
    runtime: PayloadRecoveryRuntime,
) -> PayloadRecoveryFlowResult:
    """Run the separately-approved payload branch and project observed truth."""

    payload_route_progress: Mapping[str, Any] | None = None
    payload_route_pose: dict[str, float] | None = None
    pre_recovery_distance_m: float | None = None
    progress_away_from_pickup = False

    if inputs.supervisor_loop_requested:
        route_delta_x, route_delta_y, target_z = inputs.route_target_ned
        route_origin_x, route_origin_y = runtime.terrain_relative_xy_origin(inputs.pickup_pose)
        target_x = route_origin_x + route_delta_x
        target_y = route_origin_y + route_delta_y
        runtime.assert_stream_budget(duration_seconds=12.0)
        payload_route_progress = runtime.send_route_with_monitor(
            target_x=target_x,
            target_y=target_y,
            target_z=target_z,
            expected_target_x=target_x,
            expected_target_y=target_y,
            pickup_pose=inputs.pickup_pose,
            altitude_max_m=inputs.route_altitude_max_m,
            max_pose_deviation_xy_m=10.0,
            max_pose_deviation_z_m=3.0,
            duration_seconds=12.0,
            timeout=25,
        )
        payload_route_pose = dict(runtime.pose_sample())
        runtime.append_pose_row("payload_pre_recovery_route", payload_route_pose)
        pre_recovery_distance_m = _distance_xy(
            payload_route_pose,
            inputs.pickup_pose,
        )
        progress_away_from_pickup = pre_recovery_distance_m >= 2.5
        if not progress_away_from_pickup:
            raise RuntimeError(
                "payload supervisor Form 3 requires route progress "
                "away from pickup before bounded RTL"
            )

    primary_approval, primary_allowlist, primary_dispatch = runtime.dispatch_recovery(
        inputs.payload_action
    )
    primary_cycle = observe_dispatched_recovery(
        action=inputs.payload_action,
        approval=primary_approval,
        dispatch=primary_dispatch,
        pickup_pose=inputs.pickup_pose,
        observe_state=runtime.observe_recovery_state,
    )
    primary_pose = primary_cycle.pose
    recovery_distance_m = (
        None if primary_pose is None else _distance_xy(primary_pose, inputs.pickup_pose)
    )

    post_approval = None
    post_allowlist = None
    post_dispatch = None
    post_cycle: ObservedRecoveryCycle | None = None
    post_outcome = RecoveryCycleOutcome(action=None)
    post_action_ref = None
    post_action_artifact = None
    supervisor_loop = None
    if (
        inputs.supervisor_loop_requested
        and primary_cycle.outcome.completed
        and inputs.post_recovery_action != "none"
    ):
        post_approval, post_allowlist, post_dispatch = runtime.dispatch_recovery(
            inputs.post_recovery_action
        )
        post_cycle = observe_dispatched_recovery(
            action=inputs.post_recovery_action,
            approval=post_approval,
            dispatch=post_dispatch,
            pickup_pose=inputs.pickup_pose,
            observe_state=runtime.observe_recovery_state,
        )
        post_outcome = post_cycle.outcome
        post_action_ref = "payload_supervisor_post_recovery_action:mission_designer_payload_mass"
        post_action_artifact = build_payload_post_recovery_action(
            advisory_ref=inputs.advisory_ref,
            source_cycle1_outcome_ref=PAYLOAD_RECOVERY_ACTION_REF,
            outcome=post_outcome,
            observed_at=runtime.observed_at_iso(),
            action_ref=post_action_ref,
        )
        supervisor_loop = supervision.build_payload_recovery_loop_from_outcomes(
            payload_feasibility_advisory_ref=inputs.supervisor_advisory_ref,
            primary_outcome=primary_cycle.outcome,
            primary_outcome_ref=PAYLOAD_RECOVERY_ACTION_REF,
            post_outcome=post_outcome,
            post_outcome_ref=post_action_ref,
            vehicle_realism_summary=inputs.vehicle_realism_summary,
            battery_realism_summary=inputs.battery_realism_summary,
            telemetry_realism_summary=inputs.telemetry_realism_summary,
        )

    final_status, task_status = payload_recovery_terminal_status(
        payload_action=inputs.payload_action,
        payload_outcome=primary_cycle.outcome,
        supervisor_loop_requested=inputs.supervisor_loop_requested,
        post_recovery_outcome=post_outcome,
    )
    payload_action_artifact = build_payload_recovery_action(
        advisory_ref=inputs.advisory_ref,
        outcome=primary_cycle.outcome,
        observed_at=runtime.observed_at_iso(),
    )
    updated = inputs.store.update(
        inputs.task_id,
        status=task_status,
        artifacts=recovery_task_artifacts(
            approval=primary_approval,
            allowlist=primary_allowlist,
            dispatch=primary_dispatch,
            post_approval=post_approval,
            post_allowlist=post_allowlist,
            post_dispatch=post_dispatch,
            payload_recovery_action=payload_action_artifact,
            payload_post_recovery_action=post_action_artifact,
            supervisor_loop=supervisor_loop,
        ),
    )
    if updated is None:
        raise RuntimeError(f"task {inputs.task_id} disappeared during payload recovery persistence")

    refreshed = runtime.refresh_realism()
    summary = build_payload_recovery_summary(
        PayloadRecoverySummaryInputs(
            artifact_dir=inputs.artifact_dir,
            task_status=updated["status"],
            existing_artifacts_retained=updated["artifacts"]["existing"]["kept"],
            final_status=final_status,
            advisory_ref=inputs.advisory_ref,
            payload_action_ref=PAYLOAD_RECOVERY_ACTION_REF,
            payload_outcome=primary_cycle.outcome,
            payload_action_artifact=payload_action_artifact,
            payload_route_progress_payload=payload_route_progress,
            payload_route_progress_away_from_pickup_observed=(progress_away_from_pickup),
            payload_pre_recovery_distance_to_pickup_m=pre_recovery_distance_m,
            payload_recovery_distance_to_pickup_m=recovery_distance_m,
            post_recovery_outcome=post_outcome,
            payload_post_recovery_action_ref=post_action_ref,
            payload_post_recovery_action_artifact=post_action_artifact,
            supervisor_loop=supervisor_loop,
            wind_realism_artifacts=refreshed.wind_artifacts,
            vehicle_realism_artifacts=refreshed.vehicle_artifacts,
        )
    )
    rows = recovery_pose_rows(
        pre_phase="payload_pre_recovery_route",
        pre_pose=payload_route_pose,
        primary_phase=f"payload_recovery_{inputs.payload_action}",
        primary_samples=primary_cycle.samples,
        primary_pose=primary_pose,
        primary_completed_phase="payload_recovery_completed",
        post_phase=f"payload_post_recovery_{inputs.post_recovery_action}",
        post_samples=() if post_cycle is None else post_cycle.samples,
        post_pose=None if post_cycle is None else post_cycle.pose,
        post_completed_phase="payload_post_recovery_completed",
    )
    return PayloadRecoveryFlowResult(
        summary=summary,
        pose_rows=tuple(rows),
        updated_task=updated,
        telemetry_summary=refreshed.telemetry_summary,
        vehicle_summary=refreshed.vehicle_summary,
        primary_cycle=primary_cycle,
        post_cycle=post_cycle,
    )


__all__ = [
    "PayloadRecoveryFlowInputs",
    "PayloadRecoveryFlowResult",
    "PayloadRecoveryRealismRefresh",
    "PayloadRecoveryRuntime",
    "run_payload_recovery_flow",
]
