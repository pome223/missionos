"""Bounded recovery, revocation and live CPU mailbox regression checks."""

import json
import time
import threading
from copy import deepcopy

import pytest

from scripts.yokohama_decision_host import DecisionHost
from scripts.yokohama_decision_worker import CityDecisions, HoldInterrupted


def observation(**fields):
    return dict(
        phase="00-D1",
        sim_s=10.0,
        wall_s=0.0,
        nav_state=4,
        arming_state=2,
        landed=False,
        position_valid=True,
        battery_fraction=0.8,
        vehicle={"xyz": [0, 0, 15], "quat_wxyz": [1, 0, 0, 0]},
        velocity_ned=fields.pop("velocity_ned", [0, 0, 0]),
        heading_ned_rad=1.57,
        reset_counters=[0, 0, 0],
        **fields,
    )


def config():
    return dict(
        run_id="cpu-recovery",
        flight_stages=[dict(name="00-D1", target_world_xyz_m=[0, 0, 15])],
        decisions=dict(
            wam_profile="motion-v4",
            startup_timeout_s=2,
            hold_recovery=dict(max_attempts=3, stable_sim_s=5, timeout_wall_s=1),
        ),
    )


def worker(root, cfg, sample):
    events = []
    return CityDecisions(
        root, cfg, sample, lambda name, **kw: events.append(dict(event=name, **kw)), lambda: 0
    ), events


@pytest.mark.parametrize(
    "field,value",
    [
        ("nav_state", 3),
        ("battery_fraction", 0.19),
        ("battery_fraction", float("nan")),
        ("position_valid", False),
        ("reset_counters", [1, 0, 0]),
    ],
)
def test_hard_failure_is_never_recoverable(tmp_path, field, value):
    anchor = observation()
    bad = dict(anchor, **{field: value})
    control, _ = worker(tmp_path, config(), lambda: bad)
    with pytest.raises(ValueError) as failure:
        control.held(anchor)
    assert not isinstance(failure.value, HoldInterrupted)


def test_live_mailbox_revokes_delayed_start_then_resumes_and_stops_while_moving(tmp_path):
    cfg = config()
    cfg["decisions"]["fixture_delay_s"] = {"start": 0.2}
    host = DecisionHost(tmp_path, cfg, tmp_path, "fixture")
    row = observation(velocity_ned=[0.4, 0, 0])
    control, events = worker(tmp_path, cfg, lambda: deepcopy(row))
    control.cycle, control.attempt, control.started = 1, 1, True
    try:
        with pytest.raises(HoldInterrupted):
            control.exchange("start", observation())
        control.revoke_attempt()
        row["velocity_ned"] = [0, 0, 0]
        control.attempt = 2
        value = control.exchange("resume", row)
        assert value == {"backend": "fixture", "models_invoked": False}
        late = json.loads((tmp_path / "decisions/001-response.json").read_text())
        assert late["attempt_revoked"] and "Late result" in late["error"]
        control.active = True
        row["velocity_ned"] = [1, 0, 0]
        control.stop()
        assert control.closed and not control.active and host.closed
        assert any(e["event"] == "city_late_response_rejected" for e in events)
        assert not host.pending
    finally:
        host.close()


def test_recovery_requires_five_uninterrupted_sim_seconds(tmp_path, monkeypatch):
    rows = [observation() for _ in range(10)]
    for index, row in enumerate(rows):
        row["sim_s"] = 11 + index
    rows[2]["velocity_ned"] = [0.4, 0, 0]
    stream = iter(rows)
    control, events = worker(tmp_path, config(), lambda: next(stream))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    control.recover_hold(observation())
    receipt = events[-1]
    assert receipt["event"] == "city_recovery_held"
    assert receipt["stable_since_sim_s"] == 14
    assert receipt["observation"]["sim_s"] == 19


def test_repeated_clock_snapshot_does_not_fail_or_earn_stability(tmp_path, monkeypatch):
    rows = [observation() for _ in range(9)]
    for row, stamp in zip(rows, [10, 10, 11, 12, 12, 13, 14, 15, 16]):
        row["sim_s"] = stamp
    stream = iter(rows)
    control, events = worker(tmp_path, config(), lambda: next(stream))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    control.recover_hold(observation())
    assert events[-1]["observation"]["sim_s"] == 15
    assert next(stream)["sim_s"] == 16


def test_inflight_result_and_old_authorization_cannot_cross_attempts(tmp_path):
    entered, finish = threading.Event(), threading.Event()

    class DelayedFixtureHost(DecisionHost):
        def vla(self, message, output):
            entered.set()
            assert finish.wait(2)
            self.pending[message["cycle"]] = {"test_proposal": True}
            return {"test_proposal": True}

    cfg = config()
    host = DelayedFixtureHost(tmp_path, cfg, tmp_path, "fixture")
    moving = True

    def sample():
        return observation(velocity_ned=[0.4 if entered.is_set() and moving else 0, 0, 0])

    control, _ = worker(tmp_path, cfg, sample)
    control.cycle, control.attempt, control.started = 1, 1, True
    try:
        control.exchange("start", observation())
        control.active = True
        with pytest.raises(HoldInterrupted):
            control.exchange("vla", observation())
        control.revoke_attempt()
        moving = False
        finish.set()
        control.attempt = 2
        control.exchange("resume", observation())
        assert not host.pending
        late = json.loads((tmp_path / "decisions/002-response.json").read_text())
        assert late["attempt_revoked"] and "error" in late
        control.active, control.attempt = True, 1
        with pytest.raises(ValueError, match="Obsolete"):
            control.exchange("authorize", observation())
        control.attempt = 2
        control.stop()
    finally:
        finish.set()
        host.close()


@pytest.mark.parametrize("fault", ["drift", "gap", "reset"])
def test_recovery_cannot_follow_drift_or_bridge_missing_evidence(tmp_path, fault, monkeypatch):
    rows = [observation(), observation()]
    rows[1]["sim_s"] = 11
    if fault == "drift":
        rows[1]["vehicle"]["xyz"][0] = 1.01
    elif fault == "gap":
        rows[1]["sim_s"] = 12.01
    else:
        rows[1]["reset_counters"] = [1, 0, 0]
    stream = iter(rows)
    control, _ = worker(tmp_path, config(), lambda: next(stream))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    with pytest.raises(ValueError, match="recovery left"):
        control.recover_hold(observation())


def test_retry_keeps_cycle_but_reobserves_before_upload(tmp_path, monkeypatch):
    control, events = worker(tmp_path, config(), observation)
    (tmp_path / "decisions").mkdir()
    attempts, uploads = [], []

    def decide(obs, target, **kw):
        attempts.append((control.cycle, control.attempt))
        if control.attempt == 1:
            raise HoldInterrupted("gust")
        return dict(upload_name="fresh")

    monkeypatch.setattr(control, "decide", decide)
    monkeypatch.setattr(
        control, "recover_hold", lambda anchor: events.append({"event": "recovered"})
    )
    monkeypatch.setattr(control, "activation_permit", lambda p: p)
    assert control.prepare_segment(None, [1, 0, 15], uploads.append) == {"upload_name": "fresh"}
    assert attempts == [(1, 1), (1, 2)] and uploads == ["fresh"]
    assert [e["event"] for e in events] == ["city_attempt_revoked", "recovered"]


def test_recovery_anchor_must_be_inside_approved_city_volume(tmp_path):
    row = observation()
    row["vehicle"]["xyz"][0] = 1.01
    control, events = worker(tmp_path, config(), lambda: row)
    with pytest.raises(ValueError, match="outside approved"):
        control.prepare_segment(None, [1, 0, 15], lambda _: pytest.fail("unauthorized upload"))
    assert not events


@pytest.mark.parametrize("error", [ValueError("WAM rejected"), HoldInterrupted("gust")])
def test_hard_failure_or_attempt_exhaustion_never_uploads(tmp_path, monkeypatch, error):
    control, _ = worker(tmp_path, config(), observation)
    (tmp_path / "decisions").mkdir()
    attempts, uploads = [], []

    def fail(*a, **kw):
        attempts.append(control.attempt)
        raise error

    monkeypatch.setattr(control, "decide", fail)
    monkeypatch.setattr(control, "recover_hold", lambda anchor: None)
    with pytest.raises(type(error)):
        control.prepare_segment(None, [1, 0, 15], uploads.append)
    assert attempts == ([1, 2, 3] if isinstance(error, HoldInterrupted) else [1])
    assert uploads == []


def test_recovery_volume_checks_whole_envelope_against_map(tmp_path):
    pytest.importorskip("shapely")
    from src.runtime.yokohama_native import recovery_envelopes

    cfg = dict(
        world=dict(
            frame=dict(
                source_origin_xyz_m=[0, 0, 0],
                source_to_world_matrix=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            )
        ),
        flight_stages=[
            dict(name=name, target_world_xyz_m=[0, 0, 15]) for name in ("00-D1", "01-D2")
        ],
    )

    def write_building(x):
        (tmp_path / "collision-footprints.geojson").write_text(
            json.dumps(
                dict(
                    features=[
                        dict(
                            properties=dict(zmin=0, zmax=30),
                            geometry=dict(
                                type="Polygon",
                                coordinates=[[[x, -1], [x + 1, -1], [x + 1, 1], [x, 1], [x, -1]]],
                            ),
                        )
                    ]
                )
            )
        )

    write_building(10)
    assert recovery_envelopes(cfg, tmp_path, 3)[0]["minimum_envelope_clearance_m"] == 6
    write_building(6)
    with pytest.raises(ValueError, match="2 m mapped clearance"):
        recovery_envelopes(cfg, tmp_path, 3)
    for invalid in (0, 3.01, float("nan")):
        with pytest.raises(ValueError):
            recovery_envelopes(cfg, tmp_path, invalid)
