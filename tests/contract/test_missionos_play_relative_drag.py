"""Contract tests for relative-airflow (velocity-aware) wind drag."""

import math

import pytest

from src.runtime.missionos_play_wind_driver import (
    MAX_DRAG_FORCE_N,
    relative_wind_drag_force,
    wind_drag_force,
)

pytestmark = pytest.mark.contract


def test_force_is_clamped_to_a_physical_max():
    # a diverged velocity read must not blow the relative drag up unphysically.
    fe, fn = relative_wind_drag_force(
        10.0, 270.0, vel_north_mps=0.0, vel_east_mps=-200.0
    )
    assert math.hypot(fe, fn) <= MAX_DRAG_FORCE_N + 1e-6
    # plain wind drag is capped too.
    fe2, fn2 = wind_drag_force(120.0, 270.0)
    assert math.hypot(fe2, fn2) <= MAX_DRAG_FORCE_N + 1e-6


def test_moving_with_the_wind_feels_almost_no_force():
    # wind 10 m/s from west (270) blows toward east (+x). A drone already moving
    # east at 10 m/s sees ~no relative airflow -> ~no force.
    fe, fn = relative_wind_drag_force(
        10.0, 270.0, vel_north_mps=0.0, vel_east_mps=10.0
    )
    assert abs(fe) < 1e-6 and abs(fn) < 1e-6


def test_stationary_matches_the_plain_wind_drag():
    # zero vehicle velocity -> relative airflow == wind -> same as wind_drag_force.
    rel = relative_wind_drag_force(8.0, 270.0, vel_north_mps=0.0, vel_east_mps=0.0)
    plain = wind_drag_force(8.0, 270.0)
    assert rel[0] == pytest.approx(plain[0], abs=1e-3)
    assert rel[1] == pytest.approx(plain[1], abs=1e-3)


def test_fighting_the_wind_feels_more_force_than_drifting_with_it():
    wind, bearing = 10.0, 270.0  # toward east
    fighting = relative_wind_drag_force(
        wind, bearing, vel_north_mps=0.0, vel_east_mps=-4.0  # flying west into it
    )
    drifting = relative_wind_drag_force(
        wind, bearing, vel_north_mps=0.0, vel_east_mps=4.0  # flying east with it
    )
    assert abs(fighting[0]) > abs(drifting[0])


def test_force_direction_is_along_relative_airflow():
    # wind toward east, drone moving north -> relative airflow has +east and
    # -north components, so the force is east-and-south.
    fe, fn = relative_wind_drag_force(
        10.0, 270.0, vel_north_mps=5.0, vel_east_mps=0.0
    )
    assert fe > 0  # pushed east
    assert fn < 0  # and back south (opposing its northward motion)
