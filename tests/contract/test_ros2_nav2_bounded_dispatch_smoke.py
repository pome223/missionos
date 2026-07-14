from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts import ros2_nav2_bounded_dispatch_smoke as smoke_runtime
from src.runtime.ros2_nav2_dispatch_bridge import (
    ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV,
    ROS2_NAV2_BRIDGE_COMMAND_ENV,
)


def test_shared_nav2_smoke_profiles_preserve_robot_specific_contracts() -> None:
    assert smoke_runtime.DEFAULT_GOAL_X_M == {
        "turtlebot3": 0.75,
        "turtlebot4": 0.25,
    }


@pytest.mark.parametrize("robot_profile", ["turtlebot3", "turtlebot4"])
def test_shared_nav2_smoke_remains_fail_closed_without_opt_in(
    robot_profile: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, raising=False)
    monkeypatch.delenv(ROS2_NAV2_BRIDGE_COMMAND_ENV, raising=False)

    assert smoke_runtime.run_bounded_dispatch_smoke(robot_profile) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["smoke"] == f"ros2_nav2_{robot_profile}_bounded_dispatch"
    assert payload["ran"] is False
    assert payload["dispatch_request_sent"] is False
    assert payload["completion_claimed"] is False
    assert payload["physical_execution_invoked"] is False


@pytest.mark.parametrize(
    ("robot_profile", "goal_x_m"),
    [("turtlebot3", 0.75), ("turtlebot4", 0.25)],
)
def test_shared_nav2_smoke_binds_profile_to_exact_dispatch_contract(
    robot_profile: str,
    goal_x_m: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def collect_responses(self) -> tuple[dict[str, object], ...]:
            return ({"action": "send_goal_pose"},)

    class FakeAdapter:
        def __init__(self, *, config: object, client: object) -> None:
            captured["config"] = config
            captured["client"] = client

        def dispatch_approved_action(self) -> SimpleNamespace:
            return SimpleNamespace(
                physical_execution_invoked=False,
                dispatch_request_sent=True,
                completion_scope="sim_action",
                completion_claimed=True,
                model_dump=lambda **_: {"completion_scope": "sim_action"},
            )

    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setattr(smoke_runtime, "Ros2Nav2BridgeCommandClient", FakeClient)
    monkeypatch.setattr(smoke_runtime, "Ros2Nav2HardwareAdapter", FakeAdapter)

    assert smoke_runtime.run_bounded_dispatch_smoke(robot_profile) == 0

    config = captured["config"]
    assert config.missionos_action_ref == (
        f"missionos_plan_{robot_profile}_bounded_nav2_goal"
    )
    assert config.operator_approval_ref.endswith(f"{robot_profile}_001")
    assert config.goal_pose.x_m == goal_x_m
    assert config.goal_pose.label == f"{robot_profile}_short_nav2_goal"
