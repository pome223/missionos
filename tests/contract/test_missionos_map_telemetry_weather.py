from __future__ import annotations

import pytest

from missionos_cli import cli as missionos_cli

pytestmark = pytest.mark.contract


def _task_payload() -> dict:
    return {
        "task_id": "task_map_weather_altitude",
        "status": "running",
        "artifacts": {
            "mission_designer_coordinate_pair_route": {
                "takeoff_latitude": 35.0,
                "takeoff_longitude": 139.0,
                "dropoff_latitude": 35.01,
                "dropoff_longitude": 139.02,
                "wind_speed_mps": 8.2,
                "wind_direction_deg": 270.0,
                "wind_gust_mps": 12.5,
                "temperature_c": 18.4,
                "pressure_hpa": 1008.0,
                "precipitation_mm_per_hour": 0.3,
            },
            "missionos_auto_mission_compilation": {
                "planned_route_m": 1000.0,
                "terrain_clearance_target_m": 30.0,
                "terrain_clearance_profile": [
                    {
                        "fraction": 0.0,
                        "distance_m": 0.0,
                        "terrain_elevation_m": 570.0,
                        "target_clearance_m": 30.0,
                        "mission_altitude_m": 30.0,
                    },
                    {
                        "fraction": 1.0,
                        "distance_m": 1000.0,
                        "terrain_elevation_m": 3700.0,
                        "target_clearance_m": 30.0,
                        "mission_altitude_m": 3160.0,
                    },
                ],
            },
            "missionos_auto_mission_runtime_snapshot": {
                "local_x_m": 100.0,
                "local_y_m": 20.0,
                "battery_remaining_percent": 80.0,
                "altitude_above_home_m": 30.0,
                "terrain_elevation_m": 570.0,
                "terrain_clearance_m": 30.0,
                "terrain_clearance_target_m": 30.0,
                "terrain_clearance_margin_m": 0.0,
                "terrain_clearance_status": "ok",
            },
        },
    }


def test_mission_map_model_includes_altitude_references_and_weather() -> None:
    model = missionos_cli._mission_map_model(
        task_payload=_task_payload(),
        provider="osm",
        live_task_url=None,
    )

    assert model["telemetry"]["altitude_amsl_m"] == 600.0
    assert model["telemetry"]["home_relative_altitude_m"] == 30.0
    assert model["telemetry"]["agl_m"] == 30.0
    assert model["telemetry"]["agl_target_m"] == 30.0
    assert model["telemetry"]["destination_target_amsl_m"] == 3730.0
    assert model["telemetry"]["climb_to_destination_m"] == 3130.0
    assert model["weather"]["wind_speed_mps"] == 8.2
    assert model["weather"]["wind_direction_deg"] == 270.0
    assert model["weather"]["wind_gust_mps"] == 12.5
    assert model["weather"]["temperature_c"] == 18.4
    assert model["weather"]["pressure_hpa"] == 1008.0
    assert model["weather"]["precipitation_mm_per_hour"] == 0.3


def test_mission_map_html_renders_altitude_and_weather_panels() -> None:
    model = missionos_cli._mission_map_model(
        task_payload=_task_payload(),
        provider="osm",
        live_task_url=None,
    )
    html = missionos_cli._mission_map_html(model)

    assert "altitudeSummary" in html
    assert "weatherSummary" in html
    assert "alt(home)=" in html
    assert "AMSL" in html
    assert "AGL" in html
    assert "wind=" in html
    assert "temp=" in html
