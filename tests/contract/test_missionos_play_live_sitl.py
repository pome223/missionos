"""Contract tests for the live ``missionos play`` SITL runner."""

import pytest

from src.runtime.missionos_play_live_sitl import (
    DEFAULT_CONTAINER,
    PX4_BIN,
    CommandResult,
    prepare_and_takeoff,
    start_play_sitl_container,
)
from src.runtime.missionos_play_scenario import load_scenario

pytestmark = pytest.mark.contract


class FakeRunner:
    def __init__(self, logs: str = "") -> None:
        self.calls: list[list[str]] = []
        self.logs = logs

    def __call__(self, args: list[str]) -> CommandResult:
        self.calls.append(args)
        if args[:2] == ["docker", "logs"]:
            return CommandResult(tuple(args), 0, stdout=self.logs)
        if args[:2] == ["docker", "run"]:
            return CommandResult(tuple(args), 0, stdout="container-id")
        if args[-2:] == ["commander", "status"] or args[-1:] == ["status"]:
            return CommandResult(tuple(args), 0, stdout="INFO  [commander] Armed\n")
        return CommandResult(tuple(args), 0)


def test_start_container_uses_official_px4_image_and_fuji_home() -> None:
    scenario = load_scenario()
    logs = (
        "Gazebo world is ready\n"
        "gz_bridge] world: default, model: x500_0\n"
        "Startup script returned successfully\n"
    )
    runner = FakeRunner(logs)

    ready, _ = start_play_sitl_container(
        scenario,
        runner=runner,
        sleep=lambda _s: None,
        clock=lambda: 0.0,
    )

    assert ready is True
    run_call = next(call for call in runner.calls if call[:2] == ["docker", "run"])
    assert "px4io/px4-sitl-gazebo:latest" in run_call
    assert f"PX4_HOME_LAT={scenario.takeoff_lat}" in run_call
    assert f"PX4_HOME_LON={scenario.takeoff_lon}" in run_call
    assert f"PX4_HOME_ALT={scenario.takeoff_elevation_m}" in run_call
    assert "14540:14540/udp" in run_call


def test_prepare_and_takeoff_uses_full_px4_commander_path() -> None:
    runner = FakeRunner()

    observed = prepare_and_takeoff(
        container=DEFAULT_CONTAINER,
        runner=runner,
        sleep=lambda _s: None,
    )

    assert observed is True
    command_strings = [" ".join(call) for call in runner.calls]
    assert any(f"{PX4_BIN}/px4-commander arm" in call for call in command_strings)
    assert any(f"{PX4_BIN}/px4-commander takeoff" in call for call in command_strings)
    assert not any(" px4-commander " in call for call in command_strings)
