"""Contract tests for the live ``missionos play`` SITL runner."""

from types import SimpleNamespace

import pytest

from src.runtime import missionos_play_battery, missionos_play_sensors
from src.runtime import missionos_play_live_sitl as live_sitl
from src.runtime.missionos_play_live_sitl import (
    DEFAULT_CONTAINER,
    PX4_BIN,
    CommandResult,
    prepare_and_takeoff,
    start_play_sitl_container,
)
from src.runtime.missionos_play_scenario import load_scenario
from src.runtime.missionos_play_weather import WeatherForecast

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


def test_battery_and_gps_observations_enter_shared_incident_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph_calls: list[dict] = []
    monkeypatch.setattr(
        live_sitl,
        "start_play_sitl_container",
        lambda *_args, **_kwargs: (True, "ready"),
    )
    monkeypatch.setattr(
        live_sitl,
        "prepare_and_takeoff",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        live_sitl,
        "run_wind_driver",
        lambda **_kwargs: (),
    )
    monkeypatch.setattr(
        live_sitl,
        "_read_local_drift_xy",
        lambda *_args, **_kwargs: 2.0,
    )
    monkeypatch.setattr(live_sitl, "_logs", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        live_sitl,
        "stop_play_sitl_container",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        missionos_play_battery,
        "prepare_battery_mounts",
        lambda: ["-v", "fixture-battery-mount"],
    )
    monkeypatch.setattr(
        missionos_play_battery,
        "read_battery_state",
        lambda _container: SimpleNamespace(
            percentage=0.72,
            current_a=8.4,
            voltage_v=24.1,
        ),
    )
    monkeypatch.setattr(
        missionos_play_sensors,
        "set_gps_denied",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        missionos_play_sensors,
        "read_gps_status",
        lambda _container: SimpleNamespace(
            xy_position_valid=False,
            gnss_fused=False,
        ),
    )
    monkeypatch.setattr(
        missionos_play_sensors,
        "gps_health_snapshot",
        lambda _status: {
            "gps_denied": True,
            "position_trustworthy": False,
        },
    )

    def run_graph(**kwargs):
        graph_calls.append(kwargs)
        return {
            "schema_version": (
                "missionos_adk_v2_mission_incident_graph_result.v1"
            ),
            "workflow_name": "missionos_mission_incident_v2",
            "graph_runtime_status": "proposal_guardrail_passed",
            "decision_status": "no_dispatch",
            "recovery_result": {
                "runtime_status": "proposal_guardrail_passed",
                "assessment": {"selected_bounded_action": "continue"},
            },
            "approval_created": False,
            "dispatch_authority_created": False,
        }

    monkeypatch.setattr(
        live_sitl,
        "run_missionos_mission_incident_graph",
        run_graph,
    )
    scenario = load_scenario()
    forecast = WeatherForecast(
        latitude=scenario.takeoff_lat,
        longitude=scenario.takeoff_lon,
        source_url="fixture://weather",
        provider_response_status="fixture",
        source_unavailable=False,
        captured_at="2026-09-04T00:00:00+00:00",
    )

    result = live_sitl.run_play_live_sitl(
        scenario=scenario,
        forecast=forecast,
        duration_s=0.0,
        cleanup=True,
        battery_coupling=True,
        gps_denied=True,
    )

    assert len(graph_calls) == 1
    graph_input = graph_calls[0]
    assert graph_input["telemetry_snapshot"]["battery"]["percentage"] == 0.72
    assert graph_input["telemetry_snapshot"]["gps"]["gps_denied"] is True
    assert graph_input["mission_context"]["execution_scope"] == "simulator"
    assert graph_input["recovery_runner"] is (
        live_sitl.run_missionos_runtime_recovery_agent
    )
    assert result.missionos_mission_incident_graph["workflow_name"] == (
        "missionos_mission_incident_v2"
    )
    assert result.recovery_agent_result["assessment"][
        "selected_bounded_action"
    ] == "continue"
