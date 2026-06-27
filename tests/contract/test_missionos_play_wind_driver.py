"""Contract tests for the real time/altitude-varying wind driver (offline logic)."""

import json
import math

import pytest

from src.runtime.missionos_play_scenario import load_scenario
from src.runtime.missionos_play_weather import fetch_weather_forecast
from src.runtime.missionos_play_wind_driver import (
    resolve_wind_at,
    run_wind_driver,
    wind_drag_force,
)

pytestmark = pytest.mark.contract

LAT, LON = 35.3606, 138.7274


def _forecast(speeds, dirs):
    n = len(speeds)
    payload = json.dumps(
        {
            "current": {
                "time": "2026-06-22T00:00",
                "wind_speed_10m": speeds[0],
                "wind_gusts_10m": speeds[0] * 1.5,
                "wind_direction_10m": dirs[0],
                "precipitation": 0,
            },
            "hourly": {
                "time": [f"2026-06-22T0{i}:00" for i in range(n)],
                "wind_speed_10m": list(speeds),
                "wind_gusts_10m": [s * 1.5 for s in speeds],
                "wind_direction_10m": list(dirs),
                "precipitation": [0.0] * n,
            },
        }
    )
    return fetch_weather_forecast(LAT, LON, fetcher=lambda _u: ("http_200", payload))


def test_drag_force_grows_with_wind_squared():
    _, f5 = wind_drag_force(5.0, 0.0)
    _, f10 = wind_drag_force(10.0, 0.0)
    # quadratic: doubling wind quadruples force.
    assert f10 == pytest.approx(f5 * 4.0, rel=1e-3)


def test_drag_force_direction_pushes_downwind():
    # Wind FROM north (0deg) pushes the vehicle toward south -> negative north.
    fe, fn = wind_drag_force(8.0, 0.0)
    assert fn < 0
    assert abs(fe) < 1e-6
    # Wind FROM east (90deg) pushes toward west -> negative east.
    fe2, fn2 = wind_drag_force(8.0, 90.0)
    assert fe2 < 0
    assert abs(fn2) < 1e-6


def test_resolve_wind_increases_with_altitude():
    scenario = load_scenario()
    forecast = _forecast([18.0], [270])  # 5 m/s surface
    low, _, agl_low = resolve_wind_at(
        forecast, scenario, elapsed_s=0, altitude_msl_m=scenario.takeoff_elevation_m + 20
    )
    high, _, agl_high = resolve_wind_at(
        forecast, scenario, elapsed_s=0, altitude_msl_m=scenario.takeoff_elevation_m + 300
    )
    assert agl_high > agl_low
    assert high > low  # altitude-varying wind is real in the resolved value


def test_resolve_wind_changes_over_mission_time():
    scenario = load_scenario()
    forecast = _forecast([18.0, 54.0], [270, 270])  # 5 -> 15 m/s across an hour
    alt = scenario.takeoff_elevation_m + 50
    early, _, _ = resolve_wind_at(forecast, scenario, elapsed_s=0, altitude_msl_m=alt)
    late, _, _ = resolve_wind_at(forecast, scenario, elapsed_s=3600, altitude_msl_m=alt)
    assert late > early  # time-varying wind is real in the resolved value


def test_driver_loop_applies_time_and_altitude_varying_force():
    scenario = load_scenario()
    forecast = _forecast([18.0, 54.0], [270, 270])

    # Fake clock advancing 30 min per step; drone climbs each step.
    times = iter([0.0, 0.0, 1800.0, 3600.0, 4000.0])
    altitudes = iter(
        [
            scenario.takeoff_elevation_m + 20,
            scenario.takeoff_elevation_m + 200,
            scenario.takeoff_elevation_m + 400,
        ]
    )
    applied: list[tuple[float, float]] = []

    steps = run_wind_driver(
        forecast=forecast,
        scenario=scenario,
        read_altitude_msl=lambda: next(altitudes),
        publish_force=lambda e, n: applied.append((e, n)),
        duration_s=3600,
        step_s=1800,
        clock=lambda: next(times),
        sleep=lambda _s: None,
    )

    assert len(steps) == 3
    # wind magnitude strictly increases (altitude climb + forecast ramp).
    winds = [s.wind_mps for s in steps]
    assert winds[0] < winds[1] < winds[2]
    assert len(applied) == 3
