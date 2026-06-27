"""Contract tests: real weather -> SITL realism env, with honest capability record."""

import json

import pytest

from src.runtime.missionos_play_weather import fetch_weather_forecast
from src.runtime.missionos_play_sitl_conditions import (
    build_sitl_conditions,
    wind_at_altitude,
)

pytestmark = pytest.mark.contract

LAT, LON = 35.3606, 138.7274


def _forecast(fetcher_payload: str):
    return fetch_weather_forecast(LAT, LON, fetcher=lambda _u: ("http_200", fetcher_payload))


def _payload(speeds, gusts) -> str:
    n = len(speeds)
    return json.dumps(
        {
            "current": {
                "time": "2026-06-22T00:00",
                "wind_speed_10m": speeds[0],
                "wind_gusts_10m": gusts[0],
                "wind_direction_10m": 245,
                "precipitation": 0,
            },
            "hourly": {
                "time": [f"2026-06-22T0{i}:00" for i in range(n)],
                "wind_speed_10m": list(speeds),
                "wind_gusts_10m": list(gusts),
                "wind_direction_10m": [245] * n,
                "precipitation": [0.0] * n,
            },
        }
    )


def test_wind_increases_with_altitude_power_law():
    assert wind_at_altitude(5.0, 10.0) == 5.0  # at reference height, unchanged
    high = wind_at_altitude(5.0, 100.0)
    assert high > 5.0  # higher AGL -> stronger wind (modelled)
    assert wind_at_altitude(5.0, 300.0) > high


def test_realism_env_carries_real_wind_to_sitl_knobs():
    # 18 km/h -> 5 m/s surface; gust 36 km/h -> 10 m/s.
    forecast = _forecast(_payload([18.0, 18.0], [36.0, 36.0]))
    conditions = build_sitl_conditions(forecast, flight_agl_m=60.0, payload_kg=2.0)
    env = conditions.realism_env
    assert "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS" in env
    assert "MISSION_DESIGNER_REALISM_WIND_GUST_MPS" in env
    assert env["MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG"] == "2.000"
    # mean wind at 60 m AGL is the 5 m/s surface scaled up by the profile.
    assert float(env["MISSION_DESIGNER_REALISM_WIND_MEAN_MPS"]) > 5.0


def test_capability_matrix_is_honest_about_real_vs_modelled():
    forecast = _forecast(_payload([18.0, 36.0, 54.0], [36.0, 54.0, 72.0]))
    conditions = build_sitl_conditions(
        forecast, flight_agl_m=60.0, payload_kg=1.0, mission_minutes=120, step_minutes=30
    )
    matrix = conditions.capability_matrix
    # real surface values are forwarded; wind direction IS a supported SITL knob.
    assert matrix["wind_mean"] == "forwarded_real_surface"
    assert matrix["wind_direction"] == "forwarded_real_surface"
    assert conditions.realism_env["MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG"] == "245.000"
    # altitude profile and variance are explicitly modelled/derived, not measured.
    assert matrix["altitude_profile"] == "modelled_power_law"
    assert matrix["wind_variance"] == "derived"
    assert "wind_at_altitude_power_law_modelled" in matrix["approximation_reasons"]
    # forecast rises over the mission -> forwarded as a single launch value today.
    assert "time_varying_wind_forwarded_as_launch_value" in matrix["approximation_reasons"]
    assert conditions.condition_application["application_status"] == "forwarded_with_approximations"
    assert conditions.condition_application["physical_execution_invoked"] is False


def test_source_unavailable_does_not_fabricate_wind():
    def boom(_u):
        raise RuntimeError("down")

    forecast = fetch_weather_forecast(LAT, LON, fetcher=boom)
    conditions = build_sitl_conditions(forecast, flight_agl_m=60.0, payload_kg=1.0)
    assert conditions.source_unavailable is True
    assert "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS" not in conditions.realism_env
    assert conditions.capability_matrix["wind_mean"] == "source_unavailable"


def test_time_series_spans_the_mission():
    forecast = _forecast(_payload([18.0, 36.0, 54.0], [36.0, 54.0, 72.0]))
    conditions = build_sitl_conditions(
        forecast, flight_agl_m=60.0, payload_kg=1.0, mission_minutes=60, step_minutes=20
    )
    elapsed = [p.elapsed_minutes for p in conditions.wind_time_series]
    assert elapsed == [0.0, 20.0, 40.0, 60.0]
    # wind at altitude is stronger than the surface sample at each point.
    for point in conditions.wind_time_series:
        if point.surface_wind_mps:
            assert point.altitude_wind_mps >= point.surface_wind_mps
