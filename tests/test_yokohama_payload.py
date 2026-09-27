"""Cargo receipt negative cases and the real host mailbox boundary, without GPU."""

import copy
import json
import time

import pytest

from src.runtime.yokohama_payload import (
    PayloadReceiver,
    assess_delivery,
    atomic_json,
    digest,
    make_receipt,
    require_receipt,
    require_release,
)
from src.runtime.yokohama_sea import flight_stops


def scenario():
    config = dict(
        run_id="cargo-test",
        operator_approval="explicit opt-in simulator approval",
        world=dict(
            world_sha256="world",
            payload_delivery=dict(
                hover_world_xyz_m=[50, 0, 3],
                pad_world_xyz_m=[50, 0, 0],
                pad_acceptance_radius_m=1.5,
                size_m=[0.12, 0.12, 0.08],
                rest_height_tolerance_m=0.08,
                maximum_contact_age_sim_s=0.5,
                maximum_sample_gap_sim_s=1,
                maximum_speed_mps=0.1,
                maximum_motion_m=0.05,
                stability_sim_s=3,
                receipt_max_age_worker_s=5,
            ),
        ),
    )

    def row(t, phase, vehicle, payload):
        return dict(
            run_id="cargo-test",
            world_sha256="world",
            sim_s=t,
            wall_s=t,
            phase=phase,
            nav_state=4,
            arming_state=2,
            landed=False,
            velocity_ned=[0, 0, 0],
            vehicle=dict(id=101, xyz=vehicle, age_s=0.02, sensor_sim_s=t),
            payload=dict(id=102, xyz=payload, age_s=0.02, sensor_sim_s=t),
        )

    rows = [row(t, "SEA-INBOUND-COAST", [t * 320, 0, 15], [t * 320, 0, 14.7]) for t in range(4)]
    anchor = row(10, "PAYLOAD-LOW", [50, 0, 3], [50, 0, 2.7])
    rows.append(anchor)
    request = require_release(config, anchor)
    rows += [row(11 + i * 0.5, "PAYLOAD-VERIFY", [50, 0, 3], [50, 0, 0.04]) for i in range(8)]
    contacts = [
        dict(
            topic="delivery_pad",
            sensor_sim_s=r["sim_s"],
            collision1="delivery_pad::link::collision",
            collision2="delivery_payload::payload_link::payload_collision",
        )
        for r in rows[5:]
    ]
    joints = [dict(state="detached", observed_sim_s=10.1)]
    return config, request, rows, contacts, joints


def test_receipt_requires_observed_carriage_separation_contact_and_rest():
    config, request, rows, contacts, joints = scenario()
    result = assess_delivery(config, request, rows, contacts, joints)
    assert result["verified"] and all(result["checks"].values())
    receipt = make_receipt(config, request, result)
    require_receipt(config, request, receipt, rows[-1])
    assert receipt["physical_receipt_verified"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "no_contact",
        "stale_contact",
        "wrong_pad",
        "still_attached",
        "moving_cargo",
        "short_hold",
        "stale_pose",
        "wrong_entity",
        "wrong_run",
        "no_carriage",
        "no_joint_event",
        "old_detach",
        "foreign_request",
        "missing_anchor",
        "reversed_clock",
    ],
)
def test_incomplete_delivery_never_produces_receipt(fault):
    config, request, rows, contacts, joints = scenario()
    if fault == "no_contact":
        contacts = []
    elif fault == "stale_contact":
        for c in contacts:
            c["sensor_sim_s"] -= 100
    elif fault == "wrong_pad":
        for c in contacts:
            c["collision1"] = "other_pad::link::collision"
    elif fault == "still_attached":
        for r in rows[5:]:
            r["payload"]["xyz"][2] = 2.7
    elif fault == "moving_cargo":
        for i, r in enumerate(rows[5:]):
            r["payload"]["xyz"][0] += i * 0.1
    elif fault == "short_hold":
        rows = rows[:-2]
    elif fault == "stale_pose":
        for r in rows[5:]:
            r["payload"]["age_s"] = 2
    elif fault == "wrong_entity":
        rows[-1]["payload"]["id"] = 103
    elif fault == "wrong_run":
        rows[-1]["run_id"] = "foreign"
    elif fault == "no_carriage":
        rows = rows[4:]
    elif fault == "no_joint_event":
        joints = []
    elif fault == "old_detach":
        joints[0]["observed_sim_s"] = 2
    elif fault == "foreign_request":
        request["config_sha256"] = "foreign"
    elif fault == "missing_anchor":
        rows = rows[:4] + rows[5:]
    elif fault == "reversed_clock":
        rows.reverse()
    result = assess_delivery(config, request, rows, contacts, joints)
    assert not result["verified"]
    with pytest.raises(ValueError, match="requires observed"):
        make_receipt(config, request, result)


@pytest.mark.parametrize("fault", ["sea", "moving", "no_cargo", "off_pad", "ground", "stale"])
def test_release_rules_fail_closed(fault):
    config, request, *_ = scenario()
    row = copy.deepcopy(request["observation"])
    if fault == "sea":
        row["phase"] = "SEA-RETURN"
    elif fault == "moving":
        row["velocity_ned"] = [1, 0, 0]
    elif fault == "no_cargo":
        row["payload"] = None
    elif fault == "off_pad":
        row["vehicle"]["xyz"][0] += 1
    elif fault == "ground":
        row["landed"] = True
    elif fault == "stale":
        row["vehicle"]["sensor_sim_s"] -= 1
    with pytest.raises(ValueError, match="approved"):
        require_release(config, row)


@pytest.mark.parametrize(
    "fault", ["expired", "foreign_config", "foreign_request", "changed_receipt"]
)
def test_return_rejects_stale_or_changed_receipts(fault):
    config, request, rows, contacts, joints = scenario()
    receipt = make_receipt(
        config, request, assess_delivery(config, request, rows, contacts, joints)
    )
    row = copy.deepcopy(rows[-1])
    if fault == "expired":
        row["wall_s"] += 6
    elif fault == "foreign_config":
        config["run_id"] = "other"
    elif fault == "foreign_request":
        request["approved_by"] = "other"
    else:
        receipt["assessment"]["observed_through_wall_s"] -= 1
    with pytest.raises(ValueError, match="return denied"):
        require_receipt(config, request, receipt, row)


@pytest.mark.parametrize("valid", [False, True])
def test_receiver_mailbox_writes_only_a_verified_receipt(tmp_path, valid):
    config, request, rows, contacts, joints = scenario()
    if not valid:
        contacts = []
    for name, records in [
        ("flight-trajectory.jsonl", rows),
        ("sensor-events.jsonl", contacts),
        ("payload-joint-events.jsonl", joints),
    ]:
        (tmp_path / name).write_text("".join(json.dumps(r) + "\n" for r in records))
    atomic_json(tmp_path / "payload-request.json", request)
    receiver = PayloadReceiver(tmp_path, config)
    try:
        deadline = time.monotonic() + 3
        target = tmp_path / ("payload-receipt.json" if valid else "payload-assessment.json")
        while not target.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert target.exists()
        if valid:
            receipt = json.loads(target.read_text())
            assert receipt["request_sha256"] == digest(request)
            require_receipt(config, request, receipt, rows[-1])
        else:
            assert not json.loads(target.read_text())["verified"]
            assert not (tmp_path / "payload-receipt.json").exists()
    finally:
        receiver.close()


def test_cargo_route_descends_then_climbs_without_moving_city_decision_points():
    world = dict(
        points=[
            dict(id=p, world_xyz_m=[i, 0, 15]) for i, p in enumerate(["D1", "D2", "D3", "DELIVERY"])
        ],
        sea_extension=dict(ship_hold_world_xyz_m=[1400, 0, 15], coast_world_xyz_m=[400, 0, 15]),
        payload_delivery=dict(hover_world_xyz_m=[3, 0, 3]),
    )
    stops = flight_stops(world)
    assert len(stops) == 13
    assert [s["name"] for s in stops[5:9]] == [
        "03-DELIVERY",
        "PAYLOAD-LOW",
        "PAYLOAD-CLIMB",
        "04-D3",
    ]
    assert stops[6]["target_world_xyz_m"] == [3, 0, 3]
    assert stops[7]["target_world_xyz_m"] == [3, 0, 15]
