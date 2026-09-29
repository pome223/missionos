"""A D3 model step may follow a cleared pad only through fresh, bounded authority."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

from scripts.ship_anwm import action_pose
from scripts.yokohama_decision_host import DecisionHost
from scripts.yokohama_decision_worker import decision_phases, require_city_request
from src.runtime.yokohama_native import (
    forecast_consistency,
    pad_structure_consistency,
    past_view,
)
from src.runtime.yokohama_pad_queue import (
    approved_hold,
    atomic_json,
    make_request,
    propose,
    require_response,
    require_wait,
)
from tests.test_yokohama_pad_queue import config as queue_config, observation

REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "docs/examples/yokohama-pad-native-probe/inputs"


def approach_config():
    c = queue_config()
    c["world"]["pad_queue"].update(model_hold_max_offset_m=5.01, reconfirm_timeout_wall_s=30)
    c["decisions"] = dict(
        points=["D1", "D2", "D3"],
        pad_approach=dict(corridor_lateral_max_m=1.0),
    )
    return c


def at(row, xyz):
    row = copy.deepcopy(row)
    row["vehicle"]["xyz"] = xyz
    return row


def test_moved_hold_is_bounded_and_bound_into_the_request():
    c = approach_config()
    hold = [-16, 0, 15]
    assert approved_hold(c, hold) == hold
    with pytest.raises(ValueError, match="model-approach bound"):
        approved_hold(c, [-14, 0, 15])
    with pytest.raises(ValueError, match="model-approach bound"):
        approved_hold(queue_config(), hold)
    rows = [at(observation(t), hold) for t in range(1, 7)]
    with pytest.raises(ValueError, match="approved pad hold"):
        require_wait(c, rows[-1])
    request = make_request(c, 3, rows, hold=hold)
    assert request["hold_xyz_m"] == hold
    response = propose(c, request)
    assert response["proposed_action"] == "enter_delivery_approach"
    assert require_response(c, request, response, rows[-1]) == "enter_delivery_approach"
    forged = dict(request, hold_xyz_m=[-20, 0, 15])
    with pytest.raises(ValueError, match="Unbound"):
        propose(c, forged)


def test_d3_decision_exists_only_for_the_pad_approach():
    c = approach_config()
    assert decision_phases(c) == {1: "00-D1", 2: "01-D2", 3: "02-D3"}
    bare = copy.deepcopy(c)
    del bare["decisions"]["pad_approach"]
    with pytest.raises(ValueError, match="Unapproved"):
        decision_phases(bare)
    assert decision_phases({}) == {1: "00-D1", 2: "01-D2"}
    stage = dict(name="02-D3", target_world_xyz_m=[-20, 0, 15])
    c["flight_stages"] = [stage]
    row = dict(observation(0), vehicle=dict(xyz=[-20, 0, 15]))
    require_city_request(c, dict(operation="vla", cycle=3, observation=row))
    with pytest.raises(ValueError, match="outside approved"):
        require_city_request(
            c, dict(operation="vla", cycle=3, observation=dict(row, phase="01-D2"))
        )


class _Host:
    pad_cycle = DecisionHost.pad_cycle
    require_pad_approach = DecisionHost.require_pad_approach

    def __init__(self, config):
        self.config = config


def test_host_rules_require_clear_pad_and_the_wait_to_approach_corridor():
    host = _Host(approach_config())
    assert host.pad_cycle(3) and not host.pad_cycle(2)
    clear = observation(0)
    assert host.require_pad_approach(clear, [-17, 0.5, 15])["pad_clear"] is True
    with pytest.raises(ValueError, match="not clear"):
        host.require_pad_approach(observation(0, occupied=True), [-17, 0, 15])
    with pytest.raises(ValueError, match="corridor"):
        host.require_pad_approach(clear, [-17, 1.5, 15])
    with pytest.raises(ValueError, match="corridor"):
        host.require_pad_approach(clear, [-23, 0, 15])


def _probe(case):
    with np.load(PROBE / case / "history.npz", allow_pickle=False) as saved:
        return {k: saved[k] for k in saved.files}


@pytest.mark.parametrize("forward", [0, 1.0204, 2.9592, 5.0])
def test_pad_view_gate_admits_perfect_far_view_and_rejects_controls(forward):
    arrays = _probe("far-busy")
    pose = arrays["poses"][-1]
    _, hold_mask = past_view(arrays, action_pose(pose, [0, 0, 0, 0]))
    reference, mask = past_view(arrays, action_pose(pose, [forward, 0, 0, 0]))
    # The unchanged full-frame gate cannot admit even a perfect sky-dominated view.
    assert forecast_consistency(reference, reference, mask)["passed"] is False
    perfect = pad_structure_consistency(reference, reference, mask, hold_mask)
    assert perfect["passed"] and perfect["view_discriminative"]
    assert perfect["structure_fraction"] >= 0.9
    for wrong in (
        np.ascontiguousarray(reference[:, ::-1]),
        np.full_like(reference, 128),
        np.zeros_like(reference),
    ):
        assert pad_structure_consistency(wrong, reference, mask, hold_mask)["passed"] is False


def test_pad_view_gate_fails_closed_where_a_mirror_would_pass():
    arrays = _probe("near-busy")
    pose = arrays["poses"][-1]
    _, hold_mask = past_view(arrays, action_pose(pose, [0, 0, 0, 0]))
    reference, mask = past_view(arrays, action_pose(pose, [2.9592, 0, 0, 0]))
    check = pad_structure_consistency(reference, reference, mask, hold_mask)
    assert check["control_passed"]["mirrored"] is True
    assert check["view_discriminative"] is False and check["passed"] is False


def _worker_queue(tmp_path, config, rows):
    sys.path.insert(0, str(REPO / "src/runtime"))
    sys.path.insert(0, str(REPO / "scripts"))
    from yokohama_pad_worker import PadQueue

    events = []
    queue = PadQueue(tmp_path, config, obs=None, event=lambda name, **d: events.append(name))
    queue.entered = True
    feed = iter(rows)
    last = []

    def sample():  # the recorded clock stops after the last row; it never advances
        last[:] = [next(feed, last[0] if last else rows[-1])]
        return last[0]

    return queue, sample, events


def test_reconfirmation_needs_a_new_clear_window_and_host_response(tmp_path, monkeypatch):
    c = approach_config()
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    hold = [-16, 0, 15]
    rows = [at(observation(t / 2), hold) for t in range(0, 12)]
    queue, sample, events = _worker_queue(tmp_path, c, rows)
    queue.hold, queue.sequence = hold, 2
    stop = threading.Event()

    def host():  # the real MissionOS fixture proposal, at the worker's sequence
        folder = tmp_path / "pad-decisions" / "002"
        while not stop.wait(0.01):
            if (folder / "request.json").exists() and not (folder / "response.json").exists():
                request = json.loads((folder / "request.json").read_text())
                atomic_json(folder / "response.json", propose(c, request))

    thread = threading.Thread(target=host, daemon=True)
    thread.start()
    try:
        permit = queue.reconfirm(sample, "model_endpoint_before_delivery_connector")
    finally:
        stop.set()
        thread.join(timeout=2)
    request = json.loads((tmp_path / "pad-decisions/002/request.json").read_text())
    assert request["hold_xyz_m"] == hold and permit["hold_xyz_m"] == hold
    assert request["observations"][-1]["sim_s"] - request["observations"][0]["sim_s"] >= 5
    assert permit["request_id"] == request["request_id"] and queue.sequence == 3
    assert "pad_entry_reconfirmed" in events


def test_reoccupation_during_reconfirmation_grants_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    rows = [observation(0), observation(0.5), observation(1, occupied=True)]
    queue, sample, events = _worker_queue(tmp_path, approach_config(), rows)
    with pytest.raises(ValueError, match="reoccupied"):
        queue.reconfirm(sample, "before_model_approach_authority")
    assert queue.permission is None and not (tmp_path / "pad-decisions").exists()


@pytest.mark.parametrize(
    "extra,message",
    [
        (["--pad-approach-decision"], "Pad approach decision requires"),
        (["--occupied-pad", "--decision-backend", "fixture"], "only through --pad-approach"),
        (
            ["--occupied-pad", "--decision-backend", "fixture", "--pad-approach-decision"],
            "motion-v4",
        ),
        (["--fault-lead-return-on-d3-wam"], "Lead-return fault needs"),
    ],
)
def test_cli_refuses_unqualified_pad_and_model_combinations(extra, message, tmp_path):
    command = [
        sys.executable,
        str(REPO / "scripts/yokohama_sitl.py"),
        "--phase",
        "flight",
        "--approve-sitl",
        "--output-dir",
        str(tmp_path / "run"),
        "--sea-round-trip",
        "--deliver-payload",
        *extra,
    ]
    done = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and message in done.stderr
    assert not (tmp_path / "run").exists()


def test_fault_return_starts_only_at_the_d3_wam_request(tmp_path):
    c = approach_config()
    c["world"]["pad_queue"]["fault_lead_return"] = "on_d3_wam_request"
    queue, _, events = _worker_queue(tmp_path, c, [observation(0)])
    queue.obs = type("Obs", (), {"snapshot": lambda self: {"sim_s": 42.0}})()
    for operation, cycle in [("vla", 3), ("wam", 2), ("authorize", 3)]:
        queue.trigger_fault(operation, cycle)
    assert queue.fault_started_sim_s is None and not events
    queue.trigger_fault("wam", 3)
    queue.trigger_fault("wam", 3)
    assert queue.fault_started_sim_s == 42.0
    assert events == ["fault_lead_return_triggered"]
