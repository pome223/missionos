"""Contract tests for the play delivery-mission builder + cross-track logic."""

import pytest

from src.runtime import missionos_play_delivery as delivery
from src.runtime.missionos_play_live_sitl import CommandResult
from src.runtime.missionos_play_scenario import load_scenario
from src.runtime.missionos_play_delivery import (
    _cross_track_m,
    build_delivery_mission_items,
)
from src.runtime.missionos_play_weather import WeatherForecast

pytestmark = pytest.mark.contract


def test_delivery_mission_is_takeoff_dropoff_return_land():
    items = build_delivery_mission_items(load_scenario())
    commands = [it["command"] for it in items]
    assert commands == [22, 16, 16, 21]  # TAKEOFF, WAYPOINT, WAYPOINT, LAND
    # dropoff waypoint is offset from home; return waypoint is back at home.
    assert (items[1]["latitude_deg"], items[1]["longitude_deg"]) != (
        items[0]["latitude_deg"],
        items[0]["longitude_deg"],
    )
    assert (items[2]["latitude_deg"], items[2]["longitude_deg"]) == (
        items[0]["latitude_deg"],
        items[0]["longitude_deg"],
    )


def test_cross_track_is_perpendicular_distance_not_route_distance():
    # Route leg home(0,0) -> drop(200,180). A point ON the line has ~0 cross-track
    # even though it is far from home.
    on_line = _cross_track_m(100.0, 90.0, 0.0, 0.0, 200.0, 180.0)
    assert on_line == pytest.approx(0.0, abs=0.5)
    # A point pushed sideways off the line has a real cross-track error.
    off_line = _cross_track_m(0.0, 50.0, 0.0, 0.0, 200.0, 180.0)
    assert off_line > 20.0


def test_cross_track_degenerate_leg_falls_back_to_point_distance():
    assert _cross_track_m(3.0, 4.0, 0.0, 0.0, 0.0, 0.0) == pytest.approx(5.0)


def test_delivery_deviation_enters_shared_incident_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph_calls: list[dict] = []
    clock_values = iter((0.0, 0.0, 1.0, 20.0))

    def runner(args: list[str]) -> CommandResult:
        if "vehicle_local_position" in args:
            return CommandResult(
                tuple(args),
                0,
                stdout="x: 0.0\ny: 50.0\nz: -30.0\nvx: 0.0\nvy: 0.0\n",
            )
        return CommandResult(tuple(args), 0)

    monkeypatch.setattr(
        delivery,
        "start_play_sitl_container",
        lambda *_args, **_kwargs: (True, "ready"),
    )
    monkeypatch.setattr(
        delivery,
        "upload_mission",
        lambda *_args, **_kwargs: {"mission_ack_observed": True},
    )
    monkeypatch.setattr(delivery, "start_mission", lambda *_args: None)
    monkeypatch.setattr(
        delivery,
        "stop_play_sitl_container",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        delivery,
        "docker_exec_publish_force",
        lambda _container: lambda _east, _north: None,
    )
    monkeypatch.setattr(
        delivery,
        "resolve_wind_at",
        lambda *_args, **_kwargs: (9.0, 90.0, {}),
    )
    monkeypatch.setattr(
        delivery,
        "resolve_gust_at",
        lambda *_args, **_kwargs: 9.0,
    )
    monkeypatch.setattr(
        delivery,
        "relative_wind_drag_force",
        lambda *_args, **_kwargs: (0.0, 0.0),
    )

    def run_graph(**kwargs):
        graph_calls.append(kwargs)
        return {
            "schema_version": (
                "missionos_adk_v2_mission_incident_graph_result.v1"
            ),
            "workflow_name": "missionos_mission_incident_v2",
            "graph_runtime_status": "proposal_guardrail_passed",
            "decision_status": "awaiting_operator_approval",
            "recovery_result": {
                "runtime_status": "proposal_guardrail_passed",
                "assessment": {
                    "selected_bounded_action": "return_to_launch"
                },
            },
            "approval_created": False,
            "dispatch_authority_created": False,
        }

    monkeypatch.setattr(
        delivery,
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

    result = delivery.run_play_delivery(
        scenario=scenario,
        forecast=forecast,
        duration_s=10.0,
        deviation_limit_m=15.0,
        runner=runner,
        sleep=lambda _seconds: None,
        clock=lambda: next(clock_values),
    )

    assert len(graph_calls) == 1
    graph_input = graph_calls[0]
    assert graph_input["mission_context"]["mission_kind"] == (
        "pickup_dropoff_delivery"
    )
    assert graph_input["mission_context"]["execution_scope"] == "simulator"
    assert graph_input["telemetry_snapshot"]["wind"]["speed_mps"] == 9.0
    assert graph_input["recovery_runner"] is (
        delivery.run_missionos_runtime_recovery_agent
    )
    assert result.missionos_mission_incident_graph["workflow_name"] == (
        "missionos_mission_incident_v2"
    )
    assert result.recovery_agent_result["assessment"][
        "selected_bounded_action"
    ] == "return_to_launch"
