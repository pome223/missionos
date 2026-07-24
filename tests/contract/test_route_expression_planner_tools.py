from __future__ import annotations

from typing import Any
from urllib.parse import unquote_plus

from src.gateway.server import _missionos_instruction_requests_designer_plan
from src.intelligence import missionos_chief_planner_tools as chief_planner_tools
from src.intelligence.missionos_chief_planner_tools import (
    _normalize_semantic_route_request,
    resolve_chief_planner_internal_tools,
)
from src.runtime import px4_gazebo_mission_scenario_designer as scenario_designer


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


def test_chief_semantic_missing_deepseek_key_names_deepseek(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("MISSIONOS_AGENT_RUNTIME_ADK_ENABLED", "1")
    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    result = chief_planner_tools._resolve_chief_route_via_function_tool(
        utterance="Tokyo Station to Akihabara",
        now=None,
        weather_fetcher=None,
        postal_fetcher=None,
        geocode_fetcher=None,
        terrain_fetcher=None,
        weather_timeout_seconds=1.0,
        place_timeout_seconds=1.0,
        terrain_timeout_seconds=1.0,
    )

    invocation = result["chief_route_function_tool_invocation"]
    assert result["tool_status"] == "not_configured"
    assert invocation["blocking_reasons"] == [
        "DEEPSEEK_API_KEY_not_configured"
    ]


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


def test_japanese_route_expression_keeps_followup_recovery_sentence_out_of_place_query(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("MISSIONOS_AGENT_RUNTIME_ADK_ENABLED", raising=False)
    monkeypatch.delenv("MISSIONOS_CHIEF_ROUTE_SEMANTIC_ADK_ENABLED", raising=False)

    result = resolve_chief_planner_internal_tools(
        utterance=(
            "東京駅から日本橋まで、PX4/Gazeboのドローンで飛行するミッションを"
            "計画してください。飛行経路上の障害物を検出した場合は、安全にHOLDして"
            "Recovery Agentが回避案を提案してください。"
        ),
        weather_fetcher=_weather_fetcher,
        terrain_fetcher=_terrain_fetcher,
    )

    route = result["coordinate_route"]
    assert result["tool_status"] == "resolved"
    assert route["takeoff_label"] == "Tokyo Station"
    assert route["dropoff_label"].startswith("Nihonbashi")
    assert route["landing_zone_blocked"] is False
    assert route["obstacle_route_fraction"] == 0.5
    assert route["gazebo_obstacle_model_spawn_requested"] is True
    assert route["obstacle_scenario_source"] == (
        "operator_instruction_mid_route_bounded_sitl_scenario"
    )
    compiled_route = scenario_designer._coordinate_route_from_payload(route)
    assert compiled_route["landing_zone_blocked"] is False
    assert compiled_route["obstacle_route_fraction"] == 0.5
    assert compiled_route["obstacle_size_x_m"] == 18.0
    assert compiled_route["obstacle_size_y_m"] == 18.0
    assert compiled_route["obstacle_size_z_m"] == 20.0
    assert compiled_route["obstacle_scenario_source"] == (
        "operator_instruction_mid_route_bounded_sitl_scenario"
    )


def test_japanese_route_expression_treats_fifty_percent_as_mid_route_obstacle(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("MISSIONOS_AGENT_RUNTIME_ADK_ENABLED", raising=False)
    monkeypatch.delenv("MISSIONOS_CHIEF_ROUTE_SEMANTIC_ADK_ENABLED", raising=False)

    result = resolve_chief_planner_internal_tools(
        utterance=(
            "東京駅から日本橋まで飛行し、経路の50%地点に衝突判定付き障害物を"
            "配置するPX4/Gazeboミッションを計画してください。"
        ),
        weather_fetcher=_weather_fetcher,
        terrain_fetcher=_terrain_fetcher,
    )

    route = result["coordinate_route"]
    assert route["landing_zone_blocked"] is False
    assert route["obstacle_route_fraction"] == 0.5
    assert route["obstacle_scenario_source"] == (
        "operator_instruction_mid_route_bounded_sitl_scenario"
    )


def test_chief_semantic_request_applies_colloquial_multi_hazard_values(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("MISSIONOS_AGENT_RUNTIME_ADK_ENABLED", raising=False)
    monkeypatch.delenv("MISSIONOS_CHIEF_ROUTE_SEMANTIC_ADK_ENABLED", raising=False)
    semantic_request = _normalize_semantic_route_request(
        {
            "mission_designer_request": {
                "origin_query": "New York Public Library",
                "destination_query": "Brooklyn Bridge",
                "payload_weight_kg": 0.5,
                "wind_speed_mps": 3.0,
                "temperature_c": 5.0,
                "obstacle_route_fractions": [0.5],
            }
        }
    )

    result = resolve_chief_planner_internal_tools(
        utterance=(
            "Send a half-kilo package to Brooklyn Bridge. The wind is three "
            "meters per second, it is five degrees, and put an obstacle at halfway."
        ),
        semantic_route_request=semantic_request,
        geocode_fetcher=_geocode_fetcher,
        weather_fetcher=_weather_fetcher,
        terrain_fetcher=_terrain_fetcher,
    )

    route = result["coordinate_route"]
    assert route["wind_speed_mps"] == 3.0
    assert route["wind_speed_source"] == "operator_instruction"
    assert route["temperature_c"] == 5.0
    assert route["temperature_source"] == "operator_instruction"
    assert route["landing_zone_blocked"] is False
    assert route["obstacle_route_fraction"] == 0.5
    assert route["obstacle_scenario_source"] == "chief_semantic_route_request"


def test_chief_semantic_request_normalizes_percentage_obstacle_values() -> None:
    semantic_request = _normalize_semantic_route_request(
        {
            "mission_designer_request": {
                "origin_query": "Tokyo",
                "destination_query": "Akihabara",
                "obstacle_route_fractions": [50, 75, 101, "invalid"],
            }
        }
    )

    assert semantic_request["obstacle_route_fractions"] == [0.5, 0.75]


def test_compound_route_keeps_explicit_thermal_factors_separate_from_nearby_numbers(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("MISSIONOS_AGENT_RUNTIME_ADK_ENABLED", raising=False)
    monkeypatch.delenv("MISSIONOS_CHIEF_ROUTE_SEMANTIC_ADK_ENABLED", raising=False)

    result = resolve_chief_planner_internal_tools(
        utterance=(
            "東京駅から日本橋までPX4/Gazeboで飛行してください。"
            "風速4.0m/s、突風4.8m/s、風向270度、気温38度、"
            "thermal battery drain 1.15倍、thermal motor derate 0.90、"
            "0.5kgの荷物、地形クリアランス30mを条件にし、"
            "経路の50%地点に衝突判定付き18x18x20mの障害物を配置してください。"
        ),
        weather_fetcher=_weather_fetcher,
        terrain_fetcher=_terrain_fetcher,
    )

    route = result["coordinate_route"]
    assert route["temperature_c"] == 38.0
    assert route["thermal_battery_drain_factor"] == 1.15
    assert route["thermal_motor_derate_factor"] == 0.9
    assert route["payload_weight_kg"] == 0.5
    assert route["obstacle_route_fraction"] == 0.5
    assert route["obstacle_size_x_m"] == 18.0
    assert route["obstacle_size_y_m"] == 18.0
    assert route["obstacle_size_z_m"] == 20.0


def test_thermal_factor_phrases_do_not_borrow_later_payload_or_obstacle_numbers(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("MISSIONOS_AGENT_RUNTIME_ADK_ENABLED", raising=False)
    monkeypatch.delenv("MISSIONOS_CHIEF_ROUTE_SEMANTIC_ADK_ENABLED", raising=False)

    result = resolve_chief_planner_internal_tools(
        utterance=(
            "東京駅から日本橋まで、気温38度、thermal battery drainを設定し、"
            "thermal motor derateも設定して、0.5kgの荷物を運び、"
            "経路の50%地点に18x18x20mの障害物を配置してください。"
        ),
        weather_fetcher=_weather_fetcher,
        terrain_fetcher=_terrain_fetcher,
    )

    route = result["coordinate_route"]
    assert "thermal_battery_drain_factor" not in route
    assert "thermal_motor_derate_factor" not in route
    assert route["payload_weight_kg"] == 0.5
    assert route["obstacle_route_fraction"] == 0.5


def test_japanese_route_expression_preserves_two_route_obstacles(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("MISSIONOS_AGENT_RUNTIME_ADK_ENABLED", raising=False)
    monkeypatch.delenv("MISSIONOS_CHIEF_ROUTE_SEMANTIC_ADK_ENABLED", raising=False)

    result = resolve_chief_planner_internal_tools(
        utterance=(
            "東京駅から日本橋まで飛行し、経路の50%と75%進行時点に"
            "衝突判定付き障害物を置いてください。"
        ),
        weather_fetcher=_weather_fetcher,
        terrain_fetcher=_terrain_fetcher,
    )

    route = result["coordinate_route"]
    assert route["landing_zone_blocked"] is False
    assert [item["route_fraction"] for item in route["obstacles"]] == [0.5, 0.75]
    assert route["obstacle_scenario_source"] == (
        "operator_instruction_multi_route_bounded_sitl_scenario"
    )
    compiled_route = scenario_designer._coordinate_route_from_payload(route)
    assert compiled_route is not None
    assert [item["name"] for item in compiled_route["obstacles"]] == [
        "missionos_route_obstacle_50pct",
        "missionos_route_obstacle_75pct",
    ]
    assert [item["route_fraction"] for item in compiled_route["obstacles"]] == [
        0.5,
        0.75,
    ]


def test_arrow_route_expression_is_mission_designer_intent() -> None:
    assert _missionos_instruction_requests_designer_plan(
        "New York Public Library -> Brooklyn Bridge"
    )
    assert _missionos_instruction_requests_designer_plan(
        "from New York Public Library to Brooklyn Bridge"
    )
    assert _missionos_instruction_requests_designer_plan("Tokyo Station to Kawasaki Station")
    assert _missionos_instruction_requests_designer_plan("東京駅から秋葉原駅まで。障害物あり")
    assert not _missionos_instruction_requests_designer_plan("I want to fly")
    assert not _missionos_instruction_requests_designer_plan(
        "Can you fly from New York Public Library to Brooklyn Bridge?"
    )
