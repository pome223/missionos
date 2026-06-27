"""Contract tests for the play delivery-mission builder + cross-track logic."""

import pytest

from src.runtime.missionos_play_scenario import load_scenario
from src.runtime.missionos_play_delivery import (
    _cross_track_m,
    build_delivery_mission_items,
)

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
