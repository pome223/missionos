"""The pad mailbox must never turn stale/occupied observations into entry authority."""

import copy
import json
import time

import pytest

from src.runtime.yokohama_pad_queue import (
    PadSupervisor,
    atomic_json,
    clear_window,
    make_request,
    propose,
    require_response,
)


def config():
    return dict(
        run_id="pad-test",
        operator_approval="explicit fixture",
        world=dict(
            world_sha256="abc",
            payload_delivery=dict(hover_world_xyz_m=[0, 0, 3]),
            pad_queue=dict(
                wait_xyz_m=[-20, 0, 15],
                approach_xyz_m=[0, 0, 15],
                pad_xyz_m=[0, 0, 0],
                trigger_phase="02-D3",
                maximum_observation_age_s=1,
                minimum_battery_fraction=0.2,
                stable_clear_sim_s=5,
                maximum_sample_gap_sim_s=2,
                response_max_age_s=2,
                pad_exclusion_radius_m=6,
                approach_exclusion_radius_m=3,
                approved_actions=["wait_at_current_hold", "enter_delivery_approach"],
            ),
        ),
    )


def observation(t, occupied=False):
    def pose(ident, xyz):
        return dict(id=ident, xyz=xyz, age_s=0.01, sensor_sim_s=t)

    return dict(
        run_id="pad-test",
        world_sha256="abc",
        sim_s=t,
        wall_s=t,
        phase="02-D3",
        nav_state=4,
        arming_state=2,
        landed=False,
        position_valid=True,
        velocity_ned=[0, 0, 0],
        battery_fraction=0.8,
        reset_counters=[0, 0, 0],
        vehicle=pose(1, [-20, 0, 15]),
        queue_lead=pose(2, [0, 0, 1] if occupied else [12, 12, 7]),
    )


def test_real_host_mailbox_wait_then_observed_clearance(tmp_path):
    c = config()
    host = PadSupervisor(tmp_path, c)
    try:
        for seq, rows, expected in [
            (0, [observation(0, True)], "wait_at_current_hold"),
            (1, [observation(t) for t in range(1, 7)], "enter_delivery_approach"),
        ]:
            folder = tmp_path / "pad-decisions" / f"{seq:03d}"
            folder.mkdir(parents=True)
            req = make_request(c, seq, rows)
            atomic_json(folder / "request.json", req)
            deadline = time.monotonic() + 3
            while not (folder / "response.json").exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            response = json.loads((folder / "response.json").read_text())
            assert response["approval_granted"] is False
            assert require_response(c, req, response, rows[-1]) == expected
    finally:
        host.close()


@pytest.mark.parametrize(
    "fault", ["reoccupied", "stale", "foreign", "clock", "reset", "battery", "mode", "identity"]
)
def test_late_response_cannot_grant_entry(fault):
    c = config()
    rows = [observation(t) for t in range(6)]
    req = make_request(c, 1, rows)
    response = propose(c, req)
    current = observation(6)
    if fault == "reoccupied":
        current = observation(6, True)
    if fault == "stale":
        current["queue_lead"]["age_s"] = 2
    if fault == "foreign":
        response["request_id"] = "other"
    if fault == "clock":
        current["wall_s"] = 9
    if fault == "reset":
        current["reset_counters"][0] = 1
    if fault == "battery":
        current["battery_fraction"] = 0.1
    if fault == "mode":
        current["nav_state"] = 3
    if fault == "identity":
        current["queue_lead"]["id"] = 3
    with pytest.raises(ValueError):
        require_response(c, req, response, current)


@pytest.mark.parametrize("fault", ["gap", "repeat", "reoccupied", "short", "reset"])
def test_clearance_needs_continuous_observed_clear_window(fault):
    rows = [observation(t) for t in range(6)]
    if fault == "gap":
        rows = rows[:2] + rows[4:]
    if fault == "repeat":
        rows[3] = copy.deepcopy(rows[2])
    if fault == "reoccupied":
        rows[3] = observation(3, True)
    if fault == "short":
        rows = rows[:-1]
    if fault == "reset":
        rows[3]["reset_counters"][0] = 1
    assert not clear_window(config(), rows)


@pytest.mark.parametrize("applied", [True, False])
def test_pose_transport_ack_cannot_substitute_for_observed_exit(monkeypatch, tmp_path, applied):
    import importlib
    import sys
    from types import SimpleNamespace
    from src.runtime import yokohama_pad_queue

    monkeypatch.setitem(sys.modules, "yokohama_pad_queue", yokohama_pad_queue)

    class Pose:
        def __init__(self):
            self.position = SimpleNamespace(x=0, y=0, z=0)
            self.orientation = SimpleNamespace(w=0)

    monkeypatch.setitem(sys.modules, "gz.msgs10.pose_pb2", SimpleNamespace(Pose=Pose))
    monkeypatch.setitem(sys.modules, "gz.msgs10.boolean_pb2", SimpleNamespace(Boolean=object))
    worker = importlib.import_module("scripts.yokohama_pad_worker")
    c = config()
    p = c["world"]["pad_queue"]
    p.update(
        lead_entity="queue_lead",
        parcel_entity="queue_parcel",
        lead_start_xyz_m=[0, 0, 1],
        lead_up_xyz_m=[0, 0, 7],
        lead_end_xyz_m=[12, 12, 7],
        occupied_duration_sim_s=15,
        ascent_duration_sim_s=6,
        departure_duration_sim_s=12,
        unload_duration_sim_s=5,
    )
    row = observation(40, True)
    snap = dict(
        sim_s=40,
        poses=dict(queue_lead=row["queue_lead"], queue_parcel=dict(xyz=[0, 0, 0.5], age_s=0.01)),
    )

    class Node:
        def request(self, service, msg, *args):
            if applied:
                snap["poses"][msg.name]["xyz"] = [msg.position.x, msg.position.y, msg.position.z]
            return False, SimpleNamespace(data=False)

    obs = SimpleNamespace(snapshot=lambda: snap, node=Node())
    q = worker.PadQueue(tmp_path, c, obs, lambda *args, **kw: None)
    q.epoch = 0
    q.update_actor()
    from src.runtime.yokohama_pad_queue import clearance

    assert clearance(c, row)["pad_clear"] is applied
