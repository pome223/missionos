"""Contract tests: real-gust-driven turbulence on the wind driver."""

import json
import random

import pytest

from src.runtime.missionos_play_scenario import load_scenario
from src.runtime.missionos_play_weather import fetch_weather_forecast
from src.runtime.missionos_play_wind_driver import (
    resolve_gust_at,
    resolve_wind_at,
    run_wind_driver,
)

pytestmark = pytest.mark.contract

LAT, LON = 35.3606, 138.7274


def _forecast(mean, gust):
    payload = json.dumps(
        {
            "current": {"time": "2026-06-22T00:00", "wind_speed_10m": mean,
                        "wind_gusts_10m": gust, "wind_direction_10m": 270, "precipitation": 0},
            "hourly": {"time": ["2026-06-22T00:00"], "wind_speed_10m": [mean],
                       "wind_gusts_10m": [gust], "wind_direction_10m": [270], "precipitation": [0.0]},
        }
    )
    return fetch_weather_forecast(LAT, LON, fetcher=lambda _u: ("http_200", payload))


def _drive(forecast, *, turbulence, seed, msl):
    scenario = load_scenario()
    forces: list[float] = []
    times = iter([float(i) for i in range(0, 80, 2)])
    run_wind_driver(
        forecast=forecast, scenario=scenario,
        read_altitude_msl=lambda: msl,
        publish_force=lambda e, n: forces.append(round(e, 3)),
        duration_s=18.0, step_s=2.0,
        clock=lambda: next(times), sleep=lambda _s: None,
        turbulence=turbulence, rng=random.Random(seed),
    )
    return forces


def test_resolve_gust_is_at_least_the_mean_wind():
    scenario = load_scenario()
    fc = _forecast(18.0, 36.0)  # 5 m/s mean, 10 m/s gust (surface, km/h)
    msl = scenario.takeoff_elevation_m + 60.0
    gust = resolve_gust_at(fc, scenario, elapsed_s=0, altitude_msl_m=msl)
    mean, _, _ = resolve_wind_at(fc, scenario, elapsed_s=0, altitude_msl_m=msl)
    assert gust >= mean  # gust scales the mean up by the real gust/mean ratio


def test_turbulence_makes_injected_force_vary_with_real_gust_spread():
    scenario = load_scenario()
    fc = _forecast(18.0, 54.0)  # strong gust spread
    msl = scenario.takeoff_elevation_m + 60.0
    forces = _drive(fc, turbulence=True, seed=3, msl=msl)
    assert len(set(forces)) > 1  # gusts make the injected force vary


def test_no_gust_spread_means_steady_force():
    scenario = load_scenario()
    fc = _forecast(18.0, 18.0)  # mean == gust -> amplitude 0
    msl = scenario.takeoff_elevation_m + 60.0
    forces = _drive(fc, turbulence=True, seed=1, msl=msl)
    assert len(set(forces)) == 1  # no gust spread -> steady force


def test_turbulence_off_is_steady_even_with_gust_spread():
    scenario = load_scenario()
    fc = _forecast(18.0, 54.0)
    msl = scenario.takeoff_elevation_m + 60.0
    forces = _drive(fc, turbulence=False, seed=3, msl=msl)
    assert len(set(forces)) == 1  # turbulence disabled -> mean wind only
