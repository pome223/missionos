from __future__ import annotations

from typing import Any
from urllib.parse import unquote_plus

from src.gateway.server import _missionos_instruction_requests_designer_plan
from src.intelligence.missionos_chief_planner_tools import (
    resolve_chief_planner_internal_tools,
)


def _geocode_fetcher(url: str) -> Any:
    decoded = unquote_plus(url).lower()
    if "new york public library" in decoded:
        return "fixture_nominatim", [
            {
                "lat": "40.753182",
                "lon": "-73.982253",
                "display_name": "New York Public Library, Manhattan, New York",
                "place_id": "nypl",
                "type": "library",
            }
        ]
    if "brooklyn bridge" in decoded:
        return "fixture_nominatim", [
            {
                "lat": "40.706086",
                "lon": "-73.996864",
                "display_name": "Brooklyn Bridge, New York",
                "place_id": "brooklyn-bridge",
                "type": "bridge",
            }
        ]
    raise AssertionError(f"unexpected geocode URL: {url}")


def _weather_fetcher(_url: str) -> Any:
    return "fixture_weather", {
        "current": {
            "time": "2026-06-21T00:00",
            "precipitation": 0.0,
            "wind_speed_10m": 18.0,
            "wind_gusts_10m": 22.0,
            "wind_direction_10m": 180.0,
            "temperature_2m": 22.0,
            "surface_pressure": 1012.0,
        }
    }


def _terrain_fetcher(url: str) -> Any:
    if "api.open-meteo.com" in url:
        return "fixture_elevation", [
            {"elevation": 8.0},
            {"elevation": 9.0},
            {"elevation": 10.0},
            {"elevation": 11.0},
            {"elevation": 12.0},
        ]
    return "fixture_gsi_unavailable", []


def test_arrow_route_expression_uses_source_geocoder(monkeypatch: Any) -> None:
    monkeypatch.delenv("MISSIONOS_AGENT_RUNTIME_ADK_ENABLED", raising=False)
    monkeypatch.delenv("MISSIONOS_CHIEF_ROUTE_SEMANTIC_ADK_ENABLED", raising=False)

    result = resolve_chief_planner_internal_tools(
        utterance="New York Public Library -> Brooklyn Bridge",
        geocode_fetcher=_geocode_fetcher,
        weather_fetcher=_weather_fetcher,
        terrain_fetcher=_terrain_fetcher,
    )

    route = result["coordinate_route"]
    assert result["tool_status"] == "resolved"
    assert "missionos_place_geocoder_tool" in result["internal_tool_names"]
    assert route["takeoff_label"].startswith("New York Public Library")
    assert route["dropoff_label"].startswith("Brooklyn Bridge")
    assert route["source_refs"]
    assert route["dispatch_authority_created"] is False
    assert route["progress_counted"] is False


def test_obstacle_instruction_sets_bounded_sitl_obstacle_flags(monkeypatch: Any) -> None:
    monkeypatch.delenv("MISSIONOS_AGENT_RUNTIME_ADK_ENABLED", raising=False)
    monkeypatch.delenv("MISSIONOS_CHIEF_ROUTE_SEMANTIC_ADK_ENABLED", raising=False)

    result = resolve_chief_planner_internal_tools(
        utterance="New York Public Library -> Brooklyn Bridge with obstacle and building risk",
        geocode_fetcher=_geocode_fetcher,
        weather_fetcher=_weather_fetcher,
        terrain_fetcher=_terrain_fetcher,
    )

    route = result["coordinate_route"]
    assert route["landing_zone_blocked"] is True
    assert route["building_risk_detected"] is True
    assert route["gazebo_obstacle_model_spawn_requested"] is True
    assert route["obstacle_scenario_source"] == "operator_instruction_bounded_sitl_scenario"
    assert "operator_instruction_obstacle_scenario:bounded_sitl" in route["source_refs"]
    assert route["dispatch_authority_created"] is False
    assert route["progress_counted"] is False


def test_arrow_route_expression_is_mission_designer_intent() -> None:
    assert _missionos_instruction_requests_designer_plan(
        "New York Public Library -> Brooklyn Bridge"
    )
    assert _missionos_instruction_requests_designer_plan(
        "from New York Public Library to Brooklyn Bridge"
    )
    assert _missionos_instruction_requests_designer_plan(
        "Tokyo Station to Kawasaki Station"
    )
    assert _missionos_instruction_requests_designer_plan(
        "東京駅から秋葉原駅まで。障害物あり"
    )
    assert not _missionos_instruction_requests_designer_plan("I want to fly")
    assert not _missionos_instruction_requests_designer_plan(
        "Can you fly from New York Public Library to Brooklyn Bridge?"
    )
