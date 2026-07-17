"""Terminal route-action orchestration for the PX4/Gazebo runtime harness.

The caller retains every authority-producing and executor-facing operation.
This module selects the previously justified branch and invokes only the
callbacks supplied for that branch.  It never treats dispatch ACK as landing,
return-to-home, delivery completion, or physical-execution proof.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RouteTerminalActionResult:
    terminal_action: str
    completed_pose: dict[str, float]
    landing_samples: tuple[dict[str, float], ...]
    landing_phase: str
    alternate_approval: Any | None = None
    alternate_allowlist: Any | None = None
    alternate_dispatch: Any | None = None
    alternate_mission_upload_result: dict[str, Any] | None = None
    alternate_route_execution_result: dict[str, Any] | None = None
    rth_approval: Any | None = None
    rth_allowlist: Any | None = None
    rth_dispatch: Any | None = None
    rth_state_observed: bool = False
    rth_state_label: str | None = None
    rth_pose: dict[str, float] | None = None
    rth_samples: tuple[dict[str, float], ...] = ()


def _pose(value: Mapping[str, float]) -> dict[str, float]:
    return {str(key): float(item) for key, item in value.items()}


def _samples(
    values: Sequence[Mapping[str, float]],
) -> tuple[dict[str, float], ...]:
    return tuple(_pose(value) for value in values)


def execute_route_terminal_action(
    *,
    rth_behavior_requested: bool,
    alternate_landing_requested: bool,
    pickup_pose: Mapping[str, float],
    target_z: float,
    altitude_max_m: float,
    route_approval: Any,
    route_allowlist: Any,
    dispatch_rth: Callable[[], tuple[Any, Any, Any]],
    observe_recovery_state: Callable[
        ..., tuple[bool, str | None, Mapping[str, float] | None, Sequence[Mapping[str, float]]]
    ],
    current_pose: Callable[[], Mapping[str, float]],
    upload_alternate_mission: Callable[[], Mapping[str, Any]],
    execute_alternate_route: Callable[..., Mapping[str, Any]],
    dispatch_alternate_landing: Callable[[], tuple[Any, Any, Any]],
    send_standard_land: Callable[[], None],
    wait_for_landing: Callable[[str], tuple[Mapping[str, float], Sequence[Mapping[str, float]]]],
) -> RouteTerminalActionResult:
    """Execute exactly one requested terminal branch and observe its outcome."""

    if rth_behavior_requested:
        rth_approval, rth_allowlist, rth_dispatch = dispatch_rth()
        state_observed, state_label, raw_pose, raw_samples = observe_recovery_state(
            action="rtl",
            pickup_pose=pickup_pose,
            dispatch_frame_sent=(rth_dispatch is not None and rth_dispatch.frame_sent is True),
        )
        rth_pose = None if raw_pose is None else _pose(raw_pose)
        rth_samples = _samples(raw_samples)
        completed_pose = rth_pose or _pose(current_pose())
        return RouteTerminalActionResult(
            terminal_action="rtl",
            completed_pose=completed_pose,
            landing_samples=rth_samples,
            landing_phase="rth",
            rth_approval=rth_approval,
            rth_allowlist=rth_allowlist,
            rth_dispatch=rth_dispatch,
            rth_state_observed=bool(state_observed),
            rth_state_label=state_label,
            rth_pose=rth_pose,
            rth_samples=rth_samples,
        )

    alternate_approval = None
    alternate_allowlist = None
    alternate_dispatch = None
    upload_result = None
    route_execution_result = None
    landing_phase = "landing"
    terminal_action = "land"
    if alternate_landing_requested:
        terminal_action = "alternate_land"
        landing_phase = "alternate_landing"
        upload_result = dict(upload_alternate_mission())
        route_execution_result = dict(
            execute_alternate_route(
                target_z=target_z,
                altitude_max_m=altitude_max_m,
                upload_result=upload_result,
                approval=route_approval,
                route_allowlist=route_allowlist,
            )
        )
        (
            alternate_approval,
            alternate_allowlist,
            alternate_dispatch,
        ) = dispatch_alternate_landing()
    else:
        send_standard_land()

    raw_completed_pose, raw_landing_samples = wait_for_landing(landing_phase)
    return RouteTerminalActionResult(
        terminal_action=terminal_action,
        completed_pose=_pose(raw_completed_pose),
        landing_samples=_samples(raw_landing_samples),
        landing_phase=landing_phase,
        alternate_approval=alternate_approval,
        alternate_allowlist=alternate_allowlist,
        alternate_dispatch=alternate_dispatch,
        alternate_mission_upload_result=upload_result,
        alternate_route_execution_result=route_execution_result,
    )


__all__ = [
    "RouteTerminalActionResult",
    "execute_route_terminal_action",
]
