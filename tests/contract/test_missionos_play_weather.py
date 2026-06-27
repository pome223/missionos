"""Contract tests for the play lab's real (hourly) Open-Meteo weather ingestion."""

import json

import pytest

from src.runtime.missionos_play_weather import (
    PlayWeatherError,
    build_open_meteo_forecast_url,
    fetch_weather_forecast,
    sample_at_elapsed,
)

pytestmark = pytest.mark.contract

# Fuji-area coordinates, matching the bundled play scenario.
LAT, LON = 35.3606, 138.7274


def _stub_payload() -> str:
    return json.dumps(
        {
            "current": {
                "time": "2026-06-22T00:00",
                "temperature_2m": 12,
                "precipitation": 0,
                "wind_speed_10m": 18.0,  # km/h -> 5.0 m/s
                "wind_direction_10m": 245,
                "wind_gusts_10m": 36.0,  # km/h -> 10.0 m/s
                "surface_pressure": 900,
            },
            "hourly": {
                "time": ["2026-06-22T00:00", "2026-06-22T01:00", "2026-06-22T02:00"],
                "wind_speed_10m": [18.0, 36.0, 54.0],  # 5, 10, 15 m/s
                "wind_gusts_10m": [36.0, 54.0, 72.0],
                "wind_direction_10m": [245, 250, 255],
                "precipitation": [0.0, 0.5, 1.0],
            },
        }
    )


def test_forecast_url_is_open_meteo_jma_with_hourly():
    url = build_open_meteo_forecast_url(LAT, LON, forecast_hours=6)
    assert url.startswith("https://api.open-meteo.com/v1/jma?")
    assert "hourly=wind_speed_10m" in url
    assert "forecast_hours=6" in url


def test_fetch_parses_current_and_hourly_kmh_to_mps():
    forecast = fetch_weather_forecast(LAT, LON, fetcher=lambda _u: ("http_200", _stub_payload()))
    assert forecast.source_unavailable is False
    assert forecast.current is not None
    assert forecast.current.wind_speed_mps == 5.0  # 18 km/h
    assert forecast.current.wind_gust_mps == 10.0  # 36 km/h
    assert len(forecast.hourly) == 3
    assert [s.wind_speed_mps for s in forecast.hourly] == [5.0, 10.0, 15.0]


def test_time_varying_sampling_interpolates_between_hours():
    forecast = fetch_weather_forecast(LAT, LON, fetcher=lambda _u: ("http_200", _stub_payload()))
    # 30 minutes in: halfway between hour0 (5 m/s) and hour1 (10 m/s).
    sample = sample_at_elapsed(forecast, 30.0)
    assert sample is not None
    assert sample.wind_speed_mps == pytest.approx(7.5, abs=0.01)
    # 90 minutes in: halfway between hour1 (10) and hour2 (15).
    assert sample_at_elapsed(forecast, 90.0).wind_speed_mps == pytest.approx(12.5, abs=0.01)


def test_source_failure_marks_unavailable_not_fabricated():
    def boom(_url):
        raise RuntimeError("network down")

    forecast = fetch_weather_forecast(LAT, LON, fetcher=boom)
    assert forecast.source_unavailable is True
    assert forecast.current is None
    assert forecast.hourly == ()
    assert forecast.provider_response_status.startswith("unavailable:")


def test_off_contract_host_is_rejected():
    with pytest.raises(PlayWeatherError):
        from src.runtime.missionos_play_weather import _validate_open_meteo_url

        _validate_open_meteo_url("https://evil.example.com/v1/jma?x=1")
