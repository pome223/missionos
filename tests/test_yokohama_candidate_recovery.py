"""Recovery bounds, terminal evidence, and irrevocable authority regressions."""

import copy
import json
import math
from types import SimpleNamespace

import pytest

from scripts.yokohama_candidate_recovery import LIMITS, deadline_for, recover, validate_sample
from scripts.verify_yokohama_candidate_recovery import (
    stable_observed,
    verify,
    verify_observations,
)
from scripts.yokohama_decision_worker import CityDecisions
from src.runtime.yokohama_execution_service import (
    admit_recovery_request,
    proposal_digest,
    recovery_arguments,
)


def config():
    return {
        "run_id": "test",
        "timeout_s": 900,
        "world": {"world_sha256": "a" * 64},
        "candidate_recovery": dict(LIMITS),
        "fixture_reject_second_candidate": True,
        "operator_approval_manifest_sha256": "b" * 64,
        "decisions": {
            "backend": "fixture",
            "endpoint_feedback": {
                "entry_world_xyz_m": [0.0, 0.0, 15.0],
                "exit_world_xyz_m": [0.0, 0.0, 15.0],
                "goal_world_xyz_m": [0.0, 4.0, 15.0],
                "corridor_half_width_m": 0.5,
                "tracking_tube_m": 1.0,
            },
        },
    }


def row(t, xyz, *, land=False):
    return {
        "run_id": "test",
        "world_sha256": "a" * 64,
        "sim_s": t,
        "wall_s": t,
        "vehicle": {"xyz": xyz, "sensor_sim_s": t, "age_s": 0.01},
        "velocity_ned": [0, 0, 0],
        "reset_counters": [0, 1, 0],
        "position_valid": True,
        "battery_fraction": 0.9,
        "arming_state": 1 if land else 2,
        "landed": land,
        "phase": "recovery_land" if land else "recovery_return",
        "raw_px4": {
            "vehicle_status": "timestamp: 1000000 (0.01 seconds ago)\n arming_state: "
            + ("1" if land else "2"),
            "vehicle_land_detected": "timestamp: 1000000 (0.01 seconds ago)\n landed: " + str(land),
            "vehicle_local_position": "timestamp: 1000000 (0.01 seconds ago)\n xy_valid: True",
        },
    }


def observations():
    trajectory = [
        row(10, [0, 2, 15]),
        *[row(t, [0, 0, 15]) for t in (11, 12, 13)],
        *[row(t, [0, 0, 0.2], land=True) for t in (14, 15, 16)],
    ]
    poses = [
        {
            "run_id": "test",
            "sensor_sim_s": r["sim_s"],
            "xyz": r["vehicle"]["xyz"],
            "source_topic": "/world/default/pose/info",
        }
        for r in trajectory
    ]
    contact = [
        {
            "topic": "launch_pad",
            "sensor_sim_s": 16,
            "collision1": "launch_pad",
            "collision2": "x500_0",
        }
    ]
    rejected = {
        "recovery_begin_wall_s": 10,
        "recovery_deadline_wall_s": 230,
        "observation": trajectory[0],
    }
    return config(), trajectory, poses, contact, rejected, {"wall_s": 13}, {"wall_s": 16}


@pytest.mark.parametrize("begin", [0.1, 10.3, 88.123456789])
def test_decimal_reserve_inclusive_and_one_ulp_short_rejected(begin):
    deadline = begin + 220
    assert deadline_for(begin, deadline) == deadline
    with pytest.raises(ValueError):
        deadline_for(begin, math.nextafter(deadline, -math.inf))


def test_terminal_measurements_pass_without_worker_verdict():
    result = verify_observations(*observations())
    assert result["disarm_observed"] and result["recovery_duration_wall_s"] == 6


@pytest.mark.parametrize(
    "fault",
    [
        "contact",
        "arming",
        "raw_arming",
        "raw_landed",
        "pose",
        "time",
        "altitude",
        "corridor",
        "reset",
        "battery",
        "nan",
        "nan_sensor_stamp",
        "deadline",
        "duplicate_pose_stamp",
        "gap",
        "stale",
    ],
)
def test_terminal_evidence_fails_closed(fault):
    args = list(observations())
    cfg, trajectory, poses, contacts, rejected, returned, landed = args
    if fault == "contact":
        contacts.clear()
    if fault == "arming":
        for r in trajectory[4:]:
            r["arming_state"] = 2
    if fault == "raw_arming":
        trajectory[-1]["raw_px4"]["vehicle_status"] = "arming_state: 2"
    if fault == "raw_landed":
        trajectory[-1]["raw_px4"]["vehicle_land_detected"] = "landed: False"
    if fault == "pose":
        poses[-1]["xyz"] = [100, 0, 0]
    if fault == "time":
        trajectory[-1]["wall_s"] = 15
    if fault == "altitude":
        trajectory[2]["vehicle"]["xyz"][2] = 17
    if fault == "corridor":
        trajectory[2]["vehicle"]["xyz"][0] = 2
    if fault == "reset":
        trajectory[2]["reset_counters"] = [1, 1, 0]
    if fault == "battery":
        trajectory[2]["battery_fraction"] = 0.1
    if fault == "nan":
        trajectory[2]["velocity_ned"][0] = math.nan
    if fault == "nan_sensor_stamp":
        trajectory[2]["vehicle"]["sensor_sim_s"] = math.nan
    if fault == "deadline":
        rejected["recovery_deadline_wall_s"] = 231
    if fault == "duplicate_pose_stamp":
        for r in trajectory[4:]:
            r["vehicle"]["sensor_sim_s"] = 14
    if fault == "gap":
        trajectory[2]["wall_s"] = 16
    if fault == "stale":
        trajectory[2]["raw_px4"]["vehicle_status"] = (
            "timestamp: 1000000 (3.01 seconds ago)\n arming_state: 2"
        )
    with pytest.raises(ValueError):
        verify_observations(*args)


@pytest.mark.parametrize("remaining", [0.79, 1.05, 1.30])
def test_second_fixture_level_candidate_is_rejected_by_progress_rules(remaining):
    from scripts.yokohama_endpoint_feedback import FIXED, validate_feedback_candidate
    from src.runtime.yokohama_native import vla_candidate

    cfg = config()
    cfg["decisions"]["endpoint_feedback"] = dict(
        FIXED,
        entry_world_xyz_m=[0, 0, 15],
        goal_world_xyz_m=[0, 4, 15],
        exit_world_xyz_m=[0, 0, 15],
        map_sha256="c" * 64,
    )
    cfg["world"]["source_sha256"] = {"collision-footprints.geojson": "c" * 64}
    cfg["flight_stages"] = [
        dict(name=n, target_world_xyz_m=[0, 0, 15]) for n in ("00-D1", "01-FEEDBACK-EXIT")
    ]
    cfg["decisions"].update(
        wam_profile="motion-v4", points=["D1"], sea_leg_present=False, payload_release_present=False
    )
    p = cfg["decisions"]["endpoint_feedback"]
    home, goal = p["entry_world_xyz_m"], p["goal_world_xyz_m"]
    dx, dy = [(goal[i] - home[i]) / 4 for i in range(2)]
    yaw = math.atan2(dy, dx)
    r = {
        "vehicle": {
            "xyz": [goal[0] - dx * remaining, goal[1] - dy * remaining, home[2]],
            "quat_wxyz": [math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)],
        },
        "heading_ned_rad": math.atan2(dx, dy),
    }
    candidate = vla_candidate("58 49 49", r)
    assert candidate["bins"] == [58, 49, 49]
    with pytest.raises(ValueError, match="goal progress or corridor"):
        validate_feedback_candidate(cfg, r["vehicle"]["xyz"], candidate["target_world_xyz_m"])


def test_container_cleanup_alone_never_proves_landing(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"cleanup": True, "status": "passed"}))
    assert verify(tmp_path)["status"] == "failed"


def test_contact_before_automatic_disarm_is_transitional_not_terminal_success():
    r = row(20, [0, 0, 0.2], land=True)
    r["arming_state"] = 2
    validate_sample(config(), r, r, landing=True)
    cfg, trajectory, poses, contacts, rejected, returned, landed = observations()
    for sample in trajectory[4:]:
        sample["arming_state"] = 2
        sample["raw_px4"]["vehicle_status"] = (
            "timestamp: 1000000 (0.01 seconds ago)\n arming_state: 2"
        )
    with pytest.raises(ValueError, match="stable_terminal"):
        verify_observations(cfg, trajectory, poses, contacts, rejected, returned, landed)


def test_duplicate_samples_do_not_earn_stable_time():
    duplicate = [row(1, [0, 0, 15]) for _ in range(20)]
    with pytest.raises(ValueError):
        stable_observed(duplicate, lambda r: True)


@pytest.mark.parametrize("change", ["altitude", "corridor", "reset", "nan"])
def test_runtime_envelope_rejected(change):
    anchor = row(10, [0, 2, 15])
    altered = copy.deepcopy(anchor)
    if change == "altitude":
        altered["vehicle"]["xyz"][2] = 16
    if change == "corridor":
        altered["vehicle"]["xyz"][0] = 2
    if change == "reset":
        altered["reset_counters"] = [1, 1, 0]
    if change == "nan":
        altered["sim_s"] = math.nan
    with pytest.raises(ValueError):
        validate_sample(config(), altered, anchor)


def test_closed_worker_cannot_reauthorize_start_or_model(tmp_path):
    worker = CityDecisions(tmp_path, {}, lambda: {}, lambda *a, **k: None, lambda: 0)
    worker.closed = True
    for operation in ("start", "vla", "wam", "authorize", "activate", "resume"):
        with pytest.raises(ValueError, match="inactive"):
            worker.exchange(operation, {})


def test_recovery_waits_for_revocation_and_observed_return_before_land():
    cfg = config()
    now = [10.0]
    calls = []
    phase = ["00-D1"]
    decisions = SimpleNamespace(closed=False, active=True, cycle=2)

    def stop():
        calls.append("stop")
        decisions.closed = True
        decisions.active = False
        return {"session_revoked": True}

    decisions.stop = stop

    def wait(predicate, timeout):
        assert timeout <= 220
        for _ in range(10):
            now[0] += 1
            r = row(
                now[0],
                [0, 0, 0.2] if phase[0] == "recovery_land" else [0, 0, 15],
                land=phase[0] == "recovery_land",
            )
            if predicate(r):
                return r
        raise TimeoutError("test wait failed")

    result = recover(
        cfg,
        decisions,
        ValueError("candidate"),
        sample=lambda: row(10, [0, 2, 15]),
        upload=lambda name: calls.append("upload"),
        activate=lambda **kw: calls.append("activate"),
        wait_for=wait,
        land=lambda: calls.append("land"),
        contacts=lambda: [
            {
                "topic": "launch_pad",
                "collision1": "launch_pad",
                "collision2": "x500_0",
                "sensor_sim_s": now[0],
            }
        ],
        event=lambda *a, **kw: None,
        clock=lambda: now[0],
        set_phase=lambda name: phase.__setitem__(0, name),
    )
    assert calls == ["stop", "upload", "activate", "land"]
    assert result["mission_outcome"] == "failed" and result["status"] == "failed_recovered"


def test_no_dispatch_when_revocation_not_confirmed():
    calls = []
    decisions = SimpleNamespace(closed=False, active=True, cycle=2, stop=lambda: {})
    with pytest.raises(ValueError, match="revocation"):
        recover(
            config(),
            decisions,
            ValueError("candidate"),
            sample=lambda: row(10, [0, 2, 15]),
            upload=lambda n: calls.append(n),
            activate=None,
            wait_for=None,
            land=None,
            contacts=None,
            event=lambda *a, **kw: None,
            clock=lambda: 10,
            set_phase=None,
        )
    assert calls == []


def test_source_bound_recovery_admission(monkeypatch, tmp_path):
    import src.runtime.yokohama_execution_service as service

    hashes = {"script": "hash"}
    monkeypatch.setattr(service, "recovery_input_hashes", lambda root: hashes)
    image = "sha256:" + "a" * 64
    proposal = {
        "schema": "yokohama.candidate-recovery-proposal.v1",
        "limits": dict(LIMITS),
        "city_models": "fixture",
        "physical_execution_invoked": False,
        "proposal_id": "test",
        "image_id": image,
        "simulator_arguments": recovery_arguments(image),
        "input_sha256": hashes,
    }
    approval = {
        "approved_proposal_sha256": proposal_digest(proposal),
        "maximum_actual_flight_trials": 1,
        "operator_approval_ref": "test",
        "actor_session_id": "test",
        "approved_at": "test",
    }
    raw = json.dumps({"proposal": proposal, "approval": approval}).encode()
    admit_recovery_request(raw, tmp_path, proposal["simulator_arguments"])
    hashes["script"] = "changed"
    with pytest.raises(ValueError):
        admit_recovery_request(raw, tmp_path, proposal["simulator_arguments"])
