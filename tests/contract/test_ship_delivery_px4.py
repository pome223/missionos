"""AUTO export is a typed route artifact, never live-flight evidence."""

import json
import math

import pytest

from src.runtime.px4_gazebo_sitl_mission_upload import PX4GazeboSITLMissionItem
from src.runtime.ship_delivery_px4 import build_stationary_ship_px4_plan


pytestmark = pytest.mark.contract


def test_round_trip_retraces_route_and_lands_at_ship():
    plan = build_stationary_ship_px4_plan()
    items = plan["mission_items"]
    assert plan["round_trip_distance_m"] == 2400
    assert [item["seq"] for item in items] == list(range(len(items)))
    assert items[0]["command"] == 22
    assert items[-1]["command"] == 21
    assert items[-1]["latitude_deg"] == items[0]["latitude_deg"]
    assert items[-1]["longitude_deg"] == items[0]["longitude_deg"]
    assert items[-1]["altitude_m"] == 0
    dwell_seq = plan["dropoff_dwell_mission_seq"]
    assert items[dwell_seq]["command"] == 19
    outbound_latitudes = [item["latitude_deg"] for item in items[:dwell_seq]]
    return_latitudes = [item["latitude_deg"] for item in items[dwell_seq + 1 : -1]]
    assert return_latitudes == list(reversed(outbound_latitudes[:-1]))
    for item in items:
        PX4GazeboSITLMissionItem.model_validate(item)
    json.dumps(plan, allow_nan=False)


def test_non_grid_shoreline_is_explicit_in_both_directions():
    plan = build_stationary_ship_px4_plan(offshore_distance_m=1055, urban_distance_m=233)
    items = plan["mission_items"]
    shore = items[plan["shoreline_outbound_seq"]]
    expected_lat = round(35.3195 + math.degrees(1055 / 6_371_000), 7)
    assert shore["latitude_deg"] == expected_lat
    assert sum(item["latitude_deg"] == expected_lat for item in items) == 2
    assert plan["round_trip_distance_m"] == 2576


def test_export_blocks_release_return_and_runtime_completion_claims():
    plan = build_stationary_ship_px4_plan()
    assert plan["status"] == "blocked_pending_runtime_adapter"
    assert "payload_release_observation_not_bound_to_return_departure" in plan["blocked_reasons"]
    assert all(item["command"] != 211 for item in plan["mission_items"])
    assert (
        plan["outbound_compilation"]["mission_items"][-1]["latitude_deg"]
        != plan["mission_items"][-1]["latitude_deg"]
    )
    for field in (
        "live_execution_supported",
        "runtime_invoked",
        "mavlink_dispatch_performed",
        "payload_release_observed",
        "delivery_completion_claimed",
        "ship_recovery_verified",
        "vla_invoked",
        "wam_invoked",
        "hardware_target_allowed",
        "physical_execution_invoked",
    ):
        assert plan[field] is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"offshore_distance_m": float("nan")},
        {"urban_distance_m": float("inf")},
        {"cruise_altitude_m": 121},
        {"cruise_altitude_m": 9},
        {"offshore_distance_m": -1},
        {"urban_distance_m": True},
        {"ship_latitude": 90},
        {"ship_longitude": 181},
        {"offshore_distance_m": 3000},
    ],
)
def test_invalid_or_out_of_envelope_inputs_fail_closed(kwargs):
    with pytest.raises(ValueError):
        build_stationary_ship_px4_plan(**kwargs)
