"""Position, command confirmation and force evidence are separate gates."""

import copy
import math

import pytest

from src.runtime.yokohama_wind_profile import make_profile, requested_wind, verify_profile, zone_at


def world():
    return dict(
        world_sha256="test-world",
        points=[dict(world_xyz_m=[0, 0, 15]), dict(world_xyz_m=[150, -150, 15])],
        sea_extension=dict(
            ship_hold_world_xyz_m=[1400, 0, 15],
            coast_to_city_entry_m=400,
            offshore_distance_m=1000,
        ),
    )


def pose(x, t):
    return dict(xyz=[x, 0, 15], id=17, sensor_sim_s=t, age_s=0)


def test_regions_and_hysteresis_cover_return_and_city_turns():
    spec = make_profile(world(), "harbor-nominal")
    previous = None
    for x, expected in [
        (1400, "offshore"),
        (898, "offshore"),
        (890, "harbor"),
        (398, "harbor"),
        (390, "coast"),
        (0, "city"),
        (150, "city"),
        (169, "city"),
        (171, "coast"),
        (410, "harbor"),
        (910, "offshore"),
    ]:
        previous = zone_at(spec, [x, 0, 15], previous)
        assert previous == expected
    assert zone_at(spec, [150, -145, 15]) == "city"
    assert make_profile(world(), "harbor-upper")["speeds_mps"] == dict(
        offshore=8, harbor=7, coast=6, city=4
    )


@pytest.mark.parametrize("fault", ["stale", "future", "nan"])
def test_zone_rejects_bad_pose(fault):
    p = pose(1400, 1)
    if fault == "stale":
        p["age_s"] = 3
    elif fault == "future":
        p["sensor_sim_s"] = 3
    else:
        p["xyz"][0] = math.nan
    with pytest.raises(ValueError):
        requested_wind(make_profile(world(), "harbor-nominal"), p, 1)


def evidence():
    w = world()
    spec = make_profile(w, "harbor-nominal")
    w["wind"] = dict(profile=spec, control_start_xyz_m=[0, 3010, 100])
    config = dict(world=w, run_id="test-run")
    receipts, rows = [], []
    for i, (x, zone) in enumerate(
        [(1400, "offshore"), (700, "harbor"), (300, "coast"), (0, "city")]
    ):
        t = 30 * i + 1
        receipts.append(
            dict(
                run_id="test-run",
                world_sha256="test-world",
                sequence=i,
                previous_zone=receipts[-1]["zone"] if receipts else None,
                zone=zone,
                phase="SEA-TAKEOFF" if not i else "test",
                observation_sim_s=t,
                start_sim_s=t,
                end_sim_s=t,
                confirmed=True,
                vehicle=pose(x, t),
                requested_enu_mps=[spec["speeds_mps"][zone], 0, 0],
            )
        )
        for dt in [0, 9, 10, 11]:
            now = t + dt
            position, old = 0.0, 0.0
            for r in receipts:
                s = now - r["start_sim_s"]
                speed = r["requested_enu_mps"][0]
                position += (speed - old) * (s - 2 + (s + 2) * math.exp(-s))
                old = speed
            rows.append(
                dict(
                    sim_s=now,
                    vehicle=pose(x, now),
                    wind_probe=dict(
                        wind_witness=dict(xyz=[position, 3000, 100], sensor_sim_s=now, age_s=0),
                        wind_control=dict(xyz=[0, 3010, 100], sensor_sim_s=now, age_s=0),
                    ),
                )
            )
    return config, rows, receipts


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "wrong_zone",
        "unconfirmed",
        "other_run",
        "future_pose",
        "force_not_changed",
        "missing_transitions",
        "mutated_preset",
    ],
)
def test_multi_zone_evidence_rejects_wrong_position_seed_or_force(fault):
    config, rows, receipts = copy.deepcopy(evidence())
    if fault == "wrong_zone":
        receipts[1]["zone"] = "city"
    elif fault == "unconfirmed":
        receipts[1]["confirmed"] = False
    elif fault == "other_run":
        receipts[1]["run_id"] = "other"
    elif fault == "future_pose":
        receipts[1]["vehicle"]["sensor_sim_s"] += 10
    elif fault == "force_not_changed":
        rows[-1]["wind_probe"]["wind_witness"]["xyz"][0] += 3
    elif fault == "missing_transitions":
        receipts = []
    elif fault == "mutated_preset":
        config["world"]["wind"]["profile"]["speeds_mps"]["offshore"] = 2
    result = verify_profile(config, rows, receipts)
    assert (result["status"] == "passed") == (fault is None)


def test_partial_force_check_does_not_claim_all_zones():
    config, rows, receipts = evidence()
    result = verify_profile(config, rows[:4], receipts[:1])
    assert result["status"] == "passed"
    assert result["all_zones_exercised"] is False


def test_same_clock_tick_binds_the_matching_observed_pose():
    config, rows, receipts = evidence()
    before = copy.deepcopy(rows[4])
    before["vehicle"]["xyz"][0] = 1400
    rows.insert(4, before)
    assert verify_profile(config, rows, receipts)["status"] == "passed"


def test_transport_marker_is_not_an_airborne_activation_receipt():
    config, rows, receipts = evidence()
    receipts[0]["phase"] = "transport-marker-only"
    assert verify_profile(config, rows, receipts)["status"] == "failed"
    config["wind_validation_scope"] = "transport-marker-only"
    assert verify_profile(config, rows, receipts)["status"] == "passed"
