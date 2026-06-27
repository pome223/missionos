"""Contract tests for real multi-height wind profile (replaces power-law model)."""

import json

import pytest

from src.runtime.missionos_play_scenario import load_scenario
from src.runtime.missionos_play_weather import (
    WeatherForecast,
    WindProfileAnchor,
    build_open_meteo_profile_url,
    fetch_weather_forecast,
    fetch_wind_profile,
    profile_wind_at,
)
from src.runtime.missionos_play_wind_driver import resolve_wind_at

pytestmark = pytest.mark.contract

LAT, LON = 35.3606, 138.7274


def _profile_payload() -> str:
    # m/s already (wind_speed_unit=ms) -> no km/h conversion.
    return json.dumps(
        {
            "hourly": {
                "time": ["2026-06-22T00:00"],
                "wind_speed_10m": [3.0],
                "wind_speed_80m": [6.0],
                "wind_speed_120m": [8.0],
                "wind_speed_180m": [11.0],
                "wind_direction_10m": [270],
                "wind_direction_80m": [275],
                "wind_direction_120m": [280],
                "wind_direction_180m": [285],
            }
        }
    )


def test_profile_url_targets_open_meteo_forecast_with_height_winds():
    url = build_open_meteo_profile_url(LAT, LON)
    assert url.startswith("https://api.open-meteo.com/v1/forecast?")
    assert "wind_speed_80m" in url and "wind_speed_180m" in url
    assert "wind_speed_unit=ms" in url


def test_fetch_wind_profile_parses_real_height_anchors():
    anchors, url = fetch_wind_profile(LAT, LON, fetcher=lambda _u: ("http_200", _profile_payload()))
    heights = [a.height_agl_m for a in anchors]
    assert heights == [10.0, 80.0, 120.0, 180.0]
    assert [a.wind_mps for a in anchors] == [3.0, 6.0, 8.0, 11.0]  # no conversion


def test_profile_wind_at_interpolates_real_anchors():
    forecast = WeatherForecast(
        latitude=LAT, longitude=LON, source_url="", provider_response_status="",
        source_unavailable=False, captured_at="",
        wind_profile=(
            WindProfileAnchor(10.0, 3.0, 270),
            WindProfileAnchor(80.0, 6.0, 275),
        ),
    )
    # halfway between 10 m (3) and 80 m (6) -> ~4.5
    assert profile_wind_at(forecast, 45.0) == pytest.approx(4.5, abs=0.05)
    # clamps below/above the measured band
    assert profile_wind_at(forecast, 0.0) == 3.0
    assert profile_wind_at(forecast, 500.0) == 6.0


def test_no_profile_returns_none_so_caller_falls_back():
    forecast = WeatherForecast(
        latitude=LAT, longitude=LON, source_url="", provider_response_status="",
        source_unavailable=False, captured_at="",
    )
    assert profile_wind_at(forecast, 60.0) is None


def test_wind_driver_uses_real_profile_over_power_law():
    scenario = load_scenario()
    # surface (JMA) forecast says calm, but the real profile says strong aloft.
    base = fetch_weather_forecast(
        LAT, LON, fetcher=lambda _u: ("http_200", json.dumps(
            {"current": {"time": "2026-06-22T00:00", "wind_speed_10m": 3.6,
                         "wind_direction_10m": 270, "precipitation": 0}}
        ))
    )
    anchors, _ = fetch_wind_profile(LAT, LON, fetcher=lambda _u: ("http_200", _profile_payload()))
    forecast = WeatherForecast(
        latitude=base.latitude, longitude=base.longitude, source_url=base.source_url,
        provider_response_status=base.provider_response_status, source_unavailable=False,
        captured_at=base.captured_at, current=base.current, hourly=base.hourly,
        wind_profile=anchors,
    )
    agl = 120.0
    msl = scenario.takeoff_elevation_m + agl
    wind, _bearing, _ = resolve_wind_at(forecast, scenario, elapsed_s=0, altitude_msl_m=msl)
    # 120 m profile anchor is 8.0 m/s real — that's what should drive the sim.
    assert wind == pytest.approx(8.0, abs=0.05)
