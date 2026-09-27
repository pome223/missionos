"""Phase/position denial before model lifecycle or inference, and route continuity."""

import copy
import json
import math
import time

import pytest

from scripts.yokohama_decision_host import DecisionHost
from scripts.yokohama_decision_worker import digest, require_city_request
from src.runtime.yokohama_sea import flight_stops
from src.runtime.yokohama_native import executor_altitude


def test_offshore_altitude_mapping_preserves_world_goal_without_relaxing_arrival():
    row = dict(vehicle=dict(xyz=[-35, -60, 15.4]), px4_relative_altitude_m=15.2)
    assert executor_altitude(15.3, row) == pytest.approx(15.1)
    assert executor_altitude(0, row) == pytest.approx(-0.2)
    for missing in [None, math.nan, math.inf]:
        row["px4_relative_altitude_m"] = missing
        with pytest.raises(ValueError, match="altitude"):
            executor_altitude(15.3, row)


def contract():
    config = dict(
        run_id="sea-city",
        decisions={},
        flight_stages=[
            dict(name="00-D1", target_world_xyz_m=[0, 0, 15]),
            dict(name="01-D2", target_world_xyz_m=[-35, -60, 15]),
        ],
    )
    request = dict(
        run_id="sea-city",
        config_sha256=digest(config),
        cycle=1,
        operation="start",
        observation=dict(
            phase="00-D1", nav_state=4, arming_state=2, landed=False, vehicle=dict(xyz=[0, 0, 15])
        ),
    )
    return config, request


@pytest.mark.parametrize(
    "fault", ["sea_phase", "sea_position", "wrong_cycle", "nan", "not_holding"]
)
def test_city_gate_rejects_unsafe_model_start(fault):
    config, request = contract()
    require_city_request(config, request)
    bad = copy.deepcopy(request)
    if fault == "sea_phase":
        bad["observation"]["phase"] = "SEA-INBOUND-COAST"
    elif fault == "sea_position":
        bad["observation"]["vehicle"]["xyz"] = [1000, 0, 15]
    elif fault == "wrong_cycle":
        bad["cycle"] = 2
    elif fault == "nan":
        bad["observation"]["vehicle"]["xyz"][0] = math.nan
    else:
        bad["observation"]["nav_state"] = 3
    with pytest.raises(ValueError, match="inland"):
        require_city_request(config, bad)
    # Cleanup remains available even if the aircraft leaves its hold.
    bad["operation"] = "stop"
    require_city_request(config, bad)


def test_host_mailbox_denies_offshore_start_before_lifecycle(tmp_path):
    config, request = contract()
    request["observation"]["vehicle"]["xyz"] = [1400, 0, 15]
    host = DecisionHost(tmp_path, config, tmp_path, "fixture")
    called = []
    host.start = lambda: called.append("unexpected model startup")
    try:
        folder = tmp_path / "decisions"
        (folder / "001-request.json").write_text(json.dumps(request))
        response = folder / "001-response.json"
        deadline = time.monotonic() + 3
        while not response.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "inland" in json.loads(response.read_text())["error"]
        assert called == [] and not host.active
    finally:
        host.close()


def test_sea_route_keeps_two_city_decisions_and_returns_to_ship():
    points = [
        dict(id=name, world_xyz_m=xyz)
        for name, xyz in [
            ("D1", [0, 0, 15]),
            ("D2", [10, 0, 15]),
            ("D3", [10, 10, 15]),
            ("DELIVERY", [20, 10, 15]),
        ]
    ]
    world = dict(
        points=points,
        sea_extension=dict(ship_hold_world_xyz_m=[1400, 0, 15], coast_world_xyz_m=[400, 0, 15]),
    )
    route = flight_stops(world)
    assert route[0]["target_world_xyz_m"] == route[-1]["target_world_xyz_m"]
    assert math.dist(route[0]["target_world_xyz_m"], route[1]["target_world_xyz_m"]) == 1000
    assert [s["name"] for s in route[2:-2]] == [
        "00-D1",
        "01-D2",
        "02-D3",
        "03-DELIVERY",
        "04-D3",
        "05-D2",
        "06-D1",
    ]
