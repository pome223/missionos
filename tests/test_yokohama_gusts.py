"""A frozen pulse must change physical evidence and cannot disappear from receipts."""

import copy
import math

import pytest

from src.runtime.yokohama_wind_profile import (
    check_gust_deadlines,
    gust_schedule,
    make_profile,
    verify_profile,
    wind_state,
)
from tests.test_yokohama_wind_profile import pose, world


def test_seeded_schedule_is_bounded_reproducible_and_zone_aware():
    a = gust_schedule(20260928)
    assert a == gust_schedule(20260928)
    assert a != gust_schedule(20260929)
    assert 20 <= a["events"][0]["start_s"] <= 35
    for i, e in enumerate(a["events"]):
        assert 6 <= e["end_s"] - e["start_s"] <= 10
        assert e["end_s"] <= 3600
        if i:
            assert 45 <= e["start_s"] - a["events"][i - 1]["end_s"] <= 75
    spec = make_profile(world(), "harbor-nominal", 20260928)
    t = a["events"][0]["start_s"] + 1
    assert 7 <= wind_state(spec, pose(1400, t), t, epoch_sim_s=0)["requested_enu_mps"][0] <= 8
    assert 8 <= wind_state(spec, pose(0, t), t, epoch_sim_s=0)["requested_enu_mps"][0] <= 12
    for epoch in [None, math.nan, t + 1, t - 3601]:
        with pytest.raises(ValueError):
            wind_state(spec, pose(0, t), t, epoch_sim_s=epoch)
    for seed in [-1, 2**32, True, 1.5]:
        with pytest.raises(ValueError):
            gust_schedule(seed)
    with pytest.raises(ValueError, match="harbor-nominal"):
        make_profile(world(), "harbor-upper", 20260928)


def evidence(cross_zone=False):
    w = world()
    spec = make_profile(w, "harbor-nominal", 20260928)
    w["wind"] = dict(profile=spec, control_start_xyz_m=[0, 3010, 100])
    config = dict(world=w, run_id="gust-test")
    rows, receipts, applied = [], [], None
    for i in range(241):
        t = 1 + i / 4
        switch = spec["gust_schedule"]["events"][0]["start_s"] + 1.5
        x_vehicle = 700 if cross_zone and t >= switch else 1400
        vehicle = pose(x_vehicle, t)
        previous_zone = applied["zone"] if applied else None
        state = wind_state(spec, vehicle, t, previous_zone, 1)
        if state != applied:
            receipts.append(
                dict(
                    run_id="gust-test",
                    world_sha256="test-world",
                    sequence=len(receipts),
                    previous_zone=previous_zone,
                    phase="SEA-TAKEOFF" if not applied else "SEA-INBOUND-COAST",
                    observation_sim_s=t,
                    start_sim_s=t,
                    end_sim_s=t,
                    confirmed=True,
                    vehicle=vehicle,
                    **state,
                )
            )
            applied = state
        x, old = 0, 0
        for r in receipts:
            dt = t - r["start_sim_s"]
            speed = r["requested_enu_mps"][0]
            x += (speed - old) * (dt - 2 + (dt + 2) * math.exp(-dt))
            old = speed
        rows.append(
            dict(
                sim_s=t,
                vehicle=vehicle,
                wind_probe=dict(
                    wind_witness=dict(xyz=[x, 3000, 100], sensor_sim_s=t, age_s=0),
                    wind_control=dict(xyz=[0, 3010, 100], sensor_sim_s=t, age_s=0),
                ),
            )
        )
    return config, rows, receipts


@pytest.mark.parametrize(
    "fault",
    [None, "omitted_pulse", "wrong_epoch", "wrong_seed", "wrong_peak", "no_force", "missing_end"],
)
def test_gust_receipts_and_transient_force_are_required(fault):
    config, rows, receipts = copy.deepcopy(evidence())
    if fault == "omitted_pulse":
        receipts = receipts[:1]
    elif fault == "wrong_epoch":
        receipts[1]["gust_epoch_sim_s"] += 1
    elif fault == "wrong_seed":
        config["world"]["wind"]["profile"]["gust_schedule"]["seed"] += 1
    elif fault == "wrong_peak":
        receipts[1]["requested_enu_mps"][0] += 1
    elif fault == "no_force":
        for r in rows:
            t = r["sim_s"] - 1
            r["wind_probe"]["wind_witness"]["xyz"][0] = 6 * (t - 2 + (t + 2) * math.exp(-t))
    elif fault == "missing_end":
        receipts = receipts[:2]
    result = verify_profile(config, rows, receipts)
    assert (result["status"] == "passed") == (fault is None)
    assert result["all_zones_exercised"] is False
    if fault is None:
        assert result["observed_gust_ids"] == result["expected_gust_ids"] == [0]


def test_late_or_unobserved_gust_is_not_credited():
    config, rows, receipts = evidence()
    receipts[1]["end_sim_s"] += 2
    assert verify_profile(config, rows, receipts)["checks"]["scheduled_gusts_observed"] is False


def test_blocking_across_a_complete_pulse_fails_closed():
    config, _, _ = evidence()
    spec = config["world"]["wind"]["profile"]
    e = spec["gust_schedule"]["events"][0]
    check_gust_deadlines(spec, 1, 1, e["start_s"] + 1.5)
    with pytest.raises(RuntimeError, match="deadline"):
        check_gust_deadlines(spec, 1, 1, e["end_s"] + 2)


def test_gust_and_zone_change_on_adjacent_ticks_use_combined_transient():
    config, rows, receipts = evidence(cross_zone=True)
    assert receipts[1]["gust_id"] == receipts[2]["gust_id"] == 0
    receipts[1]["end_sim_s"] = receipts[2]["start_sim_s"]
    result = verify_profile(config, rows, receipts)
    assert result["status"] == "passed"
    assert all(n >= 3 for n in result["force_samples_per_transition"])
