from __future__ import annotations

from dataclasses import dataclass

from src.runtime.px4_gazebo_route.terminal_action import (
    execute_route_terminal_action,
)


@dataclass
class Dispatch:
    frame_sent: bool = True


def _defaults(events: list[str]) -> dict:
    return {
        "pickup_pose": {"x": 1.0, "y": 2.0, "z": -3.0},
        "target_z": -12.0,
        "altitude_max_m": 50.0,
        "route_approval": "route-approval",
        "route_allowlist": "route-allowlist",
        "dispatch_rth": lambda: (_ for _ in ()).throw(AssertionError("unexpected RTH")),
        "observe_recovery_state": lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected recovery observation")
        ),
        "current_pose": lambda: {"x": 9.0, "y": 8.0, "z": -1.0},
        "upload_alternate_mission": lambda: (_ for _ in ()).throw(
            AssertionError("unexpected upload")
        ),
        "execute_alternate_route": lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected alternate route")
        ),
        "dispatch_alternate_landing": lambda: (_ for _ in ()).throw(
            AssertionError("unexpected alternate dispatch")
        ),
        "send_standard_land": lambda: events.append("land"),
        "wait_for_landing": lambda phase: (
            events.append(f"wait:{phase}") or {"x": 3.0, "y": 4.0, "z": 0.0},
            [{"x": 3.0, "y": 4.0, "z": 0.0}],
        ),
    }


def test_standard_land_uses_existing_route_authority_callback() -> None:
    events: list[str] = []
    result = execute_route_terminal_action(
        rth_behavior_requested=False,
        alternate_landing_requested=False,
        **_defaults(events),
    )

    assert events == ["land", "wait:landing"]
    assert result.terminal_action == "land"
    assert result.completed_pose["z"] == 0.0


def test_alternate_landing_preserves_upload_route_dispatch_order() -> None:
    events: list[str] = []
    values = _defaults(events)

    def upload() -> dict:
        events.append("upload")
        return {"mission": "uploaded"}

    def execute(**kwargs: object) -> dict:
        events.append("route")
        assert kwargs["approval"] == "route-approval"
        assert kwargs["route_allowlist"] == "route-allowlist"
        assert kwargs["upload_result"] == {"mission": "uploaded"}
        return {"route": "observed"}

    def dispatch() -> tuple[str, str, Dispatch]:
        events.append("dispatch")
        return "fresh-approval", "fresh-allowlist", Dispatch()

    values.update(
        upload_alternate_mission=upload,
        execute_alternate_route=execute,
        dispatch_alternate_landing=dispatch,
    )
    result = execute_route_terminal_action(
        rth_behavior_requested=False,
        alternate_landing_requested=True,
        **values,
    )

    assert events == ["upload", "route", "dispatch", "wait:alternate_landing"]
    assert result.terminal_action == "alternate_land"
    assert result.alternate_approval == "fresh-approval"
    assert result.alternate_route_execution_result == {"route": "observed"}


def test_rth_observes_dispatch_and_never_runs_land_wait() -> None:
    events: list[str] = []
    values = _defaults(events)

    def dispatch_rth() -> tuple[str, str, Dispatch]:
        events.append("dispatch-rtl")
        return "rtl-approval", "rtl-allowlist", Dispatch()

    def observe(**kwargs: object) -> tuple:
        events.append("observe-rtl")
        assert kwargs["action"] == "rtl"
        assert kwargs["dispatch_frame_sent"] is True
        return True, "return_to_launch_state_observed", {"x": 1, "y": 2, "z": 0}, []

    values.update(dispatch_rth=dispatch_rth, observe_recovery_state=observe)
    result = execute_route_terminal_action(
        rth_behavior_requested=True,
        alternate_landing_requested=True,
        **values,
    )

    assert events == ["dispatch-rtl", "observe-rtl"]
    assert result.terminal_action == "rtl"
    assert result.rth_state_observed is True
    assert result.completed_pose == {"x": 1.0, "y": 2.0, "z": 0.0}
