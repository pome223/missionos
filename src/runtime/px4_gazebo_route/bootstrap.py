"""Authority-explicit bootstrap for an opt-in PX4/Gazebo route smoke run.

This boundary creates the initial plan, approval, and bounded allowlists only
when the caller supplies a fresh operator-approval fact.  It does not infer
approval from simulator opt-in, dispatch a command, observe motion, or claim
completion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.runtime.px4_gazebo_coupled_delivery import (
    PX4GazeboCoupledCommandAllowlist,
    PX4GazeboCoupledCommandApproval,
    build_px4_gazebo_coupled_command_allowlist,
    build_px4_gazebo_coupled_command_approval,
)
from src.runtime.px4_gazebo_route_dispatcher import (
    PX4GazeboRouteCommandAllowlist,
    build_px4_gazebo_route_command_allowlist,
)
from src.runtime.px4_gazebo_route_plan import (
    PX4GazeboPickupDropoffRoutePlan,
    build_px4_gazebo_pickup_dropoff_route_plan,
)
from src.runtime.task_store import TaskStore


@dataclass(frozen=True)
class RouteBootstrapResult:
    task: dict
    route: PX4GazeboPickupDropoffRoutePlan
    approval: PX4GazeboCoupledCommandApproval
    coupled_allowlist: PX4GazeboCoupledCommandAllowlist
    route_allowlist: PX4GazeboRouteCommandAllowlist


def bootstrap_route_task(
    *,
    store: TaskStore,
    max_pose_deviation_xy_m: float,
    on_deviation_action: str,
    operator_approval_performed: bool,
    now: datetime,
) -> RouteBootstrapResult:
    if operator_approval_performed is not True:
        raise PermissionError("PX4/Gazebo route bootstrap requires a fresh operator approval fact")

    route = build_px4_gazebo_pickup_dropoff_route_plan(
        pickup_pad_ref="gazebo_pad:pickup",
        dropoff_pad_ref="gazebo_pad:dropoff",
        route_waypoint_refs=["gazebo_waypoint:mid"],
        geofence_polygon=[
            (-2.0, -2.0),
            (5.75, -2.0),
            (5.75, 10.0),
            (-2.0, 10.0),
        ],
        altitude_min_m=1.0,
        altitude_max_m=2.5,
        min_battery_margin_pct=25.0,
        route_completion_radius_m=0.8,
        max_pose_deviation_xy_m=max_pose_deviation_xy_m,
        on_deviation_action=on_deviation_action,
        now=now,
    )
    approval = build_px4_gazebo_coupled_command_approval(
        operator_approval_performed=operator_approval_performed,
        now=now,
    )
    coupled_allowlist = build_px4_gazebo_coupled_command_allowlist(
        approval=approval,
        now=now,
    )
    route_allowlist = build_px4_gazebo_route_command_allowlist(
        route_plan=route,
        approval=approval,
        now=now,
    )
    task = store.create(
        kind="px4_gazebo_horizontal_route_delivery",
        title="PX4/Gazebo horizontal route delivery smoke",
        status="running",
        artifacts={
            "existing": {
                "case_id": "actual-px4-gazebo-horizontal-route",
                "kept": True,
            }
        },
    )
    persisted = store.update(
        task["task_id"],
        artifacts={
            "px4_gazebo_pickup_dropoff_route_plan": route.model_dump(mode="json"),
            "px4_gazebo_coupled_command_approval": approval.model_dump(mode="json"),
            "px4_gazebo_coupled_command_allowlist": coupled_allowlist.model_dump(mode="json"),
            "px4_gazebo_route_command_allowlist": route_allowlist.model_dump(mode="json"),
        },
    )
    if persisted is None:
        raise RuntimeError("PX4/Gazebo route bootstrap artifact persistence failed")
    return RouteBootstrapResult(
        task=task,
        route=route,
        approval=approval,
        coupled_allowlist=coupled_allowlist,
        route_allowlist=route_allowlist,
    )


__all__ = ["RouteBootstrapResult", "bootstrap_route_task"]
