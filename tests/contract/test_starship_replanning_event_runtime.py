"""Event-driven actor integration with a finite fake plant and no model/flight calls.

These tests execute the production waiting, selection, admission and orbital
loop boundaries. Forecast worker doubles represent bounded asynchronous jobs;
they do not establish physical return feasibility.
"""

from copy import deepcopy
from dataclasses import replace
import math
from types import SimpleNamespace

import pytest

from src.runtime import starship_replanning_executor as runtime
from src.runtime import starship_sixdof as dyn
from src.runtime.starship_replanning import contract, digest, notices
from src.runtime.starship_replanning_events import EventMonitor
from src.runtime.starship_replanning_verifier import admit


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, _duration):
        self.now += 0.5


def actor_harness(monkeypatch, tmp_path, *, now=1448.0, case="event_normal"):
    actor = runtime.ReplanningExecutor.__new__(runtime.ReplanningExecutor)
    actor.envelope = contract("fixture", case=case)
    actor.case, actor.output, actor.mailbox = case, tmp_path, None
    actor.run_id, actor.sources = "event-runtime-test", {}
    actor.scheduled = 2280.0
    actor.times = {"nominal": 2280.0, "next_orbit": 7730.0}
    actor.s = dyn.State6DOF(
        now,
        (6678137.0, 0.0, 0.0),
        (0.0, 7725.0, 0.0),
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        100000.0,
        (dyn.EngineState(),),
        (),
    )
    actor.p = {"integration": {"coast_dt_s": 2.0, "powered_dt_s": 0.1}}
    actor.controller = SimpleNamespace(return_time_s=1e9, phase="orbital_coast")
    actor.pending, actor.events, actor.decisions, actor.revisions = [], [], [], []
    actor.candidates, actor.ground_observations = {}, []
    actor.revision, actor.forecast_count, actor.selected = 0, 0, None
    actor.service_failed = False
    actor.dispatch, actor.contact, actor.termination = None, None, "return_unresolved"
    actor.monitor = EventMonitor(actor.envelope, actor.times)
    actor.monitor.poll(actor.observation(), notices(case, now), actor.selected)

    def plant_step():
        actor.s = replace(actor.s, time_s=actor.s.time_s + 2.0)
        if actor.controller.return_time_s <= actor.s.time_s:
            actor.contact = {"contact": True}
            actor.termination = "fake_plant_contact"
        else:
            actor.monitor.poll(actor.observation(), notices(case, actor.s.time_s), actor.selected)

    actor.step = plant_step
    actor.finish = lambda: actor.dispatch
    clock = Clock()
    monkeypatch.setattr(runtime, "time", clock)
    monkeypatch.setattr(runtime, "source_hashes", lambda: {})
    return actor, clock


def make_candidate(actor, name):
    observation = actor.observation()
    contact_time = 5000.0 if name == "nominal" else 10400.0
    area = actor.envelope["operations"]["areas"]["east" if name == "nominal" else "west"]
    latitude, longitude = map(math.radians, (area["latitude_deg"], area["longitude_deg"]))
    angle = longitude + 7.292115e-5 * contact_time
    normal = [
        math.cos(latitude) * math.cos(angle),
        math.cos(latitude) * math.sin(angle),
        math.sin(latitude),
    ]
    # Quaternion rotating body +Z onto the contact normal.
    qscale = math.sqrt(2.0 * (1.0 + normal[2]))
    q = [(1.0 + normal[2]) / qscale, -normal[1] / qscale, normal[0] / qscale, 0.0]
    receipt = {
        "event_time_s": contact_time,
        "surface_relative_speed_mps": 4.0,
        "q_body_to_eci": q,
        "surface_normal_eci": normal,
        "point_eci_m": [6378137.0 * x for x in normal],
        "propellant_kg": 60000.0,
    }
    trial = {
        "origin": observation,
        "final_state": {"omega_body_rad_s": [0.0, 0.0, 0.0]},
        "outcome": {"contact_receipt": receipt},
        "coast": [deepcopy(observation["state"])],
        "wall_time_s": 1.0,
    }
    return {
        "id": name,
        "return_time_s": actor.times[name],
        "scheduled_s": actor.scheduled,
        "envelope_sha256": digest(actor.envelope),
        "source_sha256": {},
        "notice_sequence": notices(actor.case, actor.s.time_s)["sequence"],
        "signs": [0, -1, 1],
        "trials": [
            {**deepcopy(trial), "integration_scale": scale} for scale in (1.0, 1.0, 1.0, 0.5)
        ],
    }


def asynchronous_pool(monkeypatch, clock, *, completion_s=3.0):
    pools = []

    class Pool:
        def __init__(self, **kwargs):
            self.terminated = False
            self.closed = False
            self._processes = {1: SimpleNamespace(terminate=self.terminate)}
            pools.append(self)

        def terminate(self):
            self.terminated = True

        def submit(self, _function, job):
            origin, _, when, _, _, scale = job
            return SimpleNamespace(
                done=lambda: clock.now >= completion_s,
                result=lambda: {
                    "origin": origin,
                    "return_time_s": when,
                    "wall_time_s": completion_s,
                    "integration_scale": scale,
                },
            )

        def shutdown(self, **kwargs):
            self.closed = True

    monkeypatch.setattr(runtime, "ProcessPoolExecutor", Pool)
    return pools


def test_fast_response_still_advances_plant_and_rejects_notice_stale_action(monkeypatch, tmp_path):
    actor, _ = actor_harness(monkeypatch, tmp_path)
    # The synthetic answer is ready before wait() observes a later plant state.
    action = actor.choose("selected_plan_monitoring", ["keep_plan"])
    assert action is None
    assert actor.s.time_s == 1450.0
    record = actor.decisions[-1]
    assert record["response"]["action"] == "keep_plan"
    assert record["interrupted_by_events"] is True
    assert record["later_generation"] > record["request"]["context_generation"]
    assert actor.monitor.pending[0]["kind"] == "notice_updated"
    assert actor.pending[-1]["end_time_s"] > actor.pending[-1]["start_time_s"]


def test_notice_during_forecast_discards_batch_and_stops_workers(monkeypatch, tmp_path):
    actor, clock = actor_harness(monkeypatch, tmp_path)
    pools = asynchronous_pool(monkeypatch, clock)
    assert actor.calculate(["nominal", "next_orbit"]) is False
    assert actor.candidates == {}
    assert actor.forecast_count == 8  # Canceled work still consumes the approved budget.
    assert pools[0].terminated and pools[0].closed
    assert actor.pending[-1]["expired"] is True
    assert any(e["kind"] == "notice_updated" for e in actor.monitor.pending)


def test_candidate_deadline_does_not_cancel_already_running_forecast(monkeypatch, tmp_path):
    actor, clock = actor_harness(monkeypatch, tmp_path, now=1584.0)
    pools = asynchronous_pool(monkeypatch, clock)
    assert actor.calculate(["nominal"]) is True
    assert set(actor.candidates) == {"nominal"}
    assert pools[0].closed and not pools[0].terminated
    assert actor.pending[-1]["expired"] is False
    assert [e["kind"] for e in actor.monitor.pending] == ["candidate_deadline"]
    assert actor.s.time_s > 1584.0


@pytest.mark.parametrize("fault", ["engine", "rate"])
def test_new_health_violation_revokes_forecast_admission_without_model_wait(
    monkeypatch, tmp_path, fault
):
    actor, _ = actor_harness(monkeypatch, tmp_path, now=1500.0)
    candidate = make_candidate(actor, "nominal")
    notice = notices(actor.case, actor.s.time_s)
    assert admit(candidate, actor.envelope, actor.observation(), notice, actor.scheduled)[
        "accepted"
    ]
    if fault == "engine":
        actor.s = replace(
            actor.s, engine_states=(replace(actor.s.engine_states[0], available=False),)
        )
    else:
        actor.s = replace(actor.s, omega_body_rad_s=(0.021, 0.0, 0.0))
    actor.monitor.poll(actor.observation(), notice, actor.selected)
    result = admit(candidate, actor.envelope, actor.observation(), notice, actor.scheduled)
    assert not result["accepted"]
    assert "observed_health_outside_forecast_domain" in result["reasons"]
    actor.run_event_supervision()
    assert actor.dispatch is None and actor.decisions == []
    assert actor.termination == "health_inhibited_unresolved"


@pytest.mark.parametrize("name", ["nominal", "next_orbit"])
def test_both_admissible_choices_can_be_selected_without_fixture_policy(
    monkeypatch, tmp_path, name
):
    actor, _ = actor_harness(monkeypatch, tmp_path, now=1500.0, case="event_tradeoff")
    actor.candidates = {n: make_candidate(actor, n) for n in actor.times}
    assert all(c["accepted"] for c in actor.checks(notices(actor.case, actor.s.time_s)).values())
    # Selection receives an already bound model decision; no policy helper picks
    # the answer for this test. Both actions face the same real admission checks.
    actor.decisions = [{"request": {"request_id": "independent-choice"}}]
    assert actor.select(name, "ai") is True
    assert actor.selected == name
    assert actor.revisions[-1]["basis"] == "ai"
    assert actor.revisions[-1]["candidate_id"] == name
    assert actor.revisions[-1]["check"]["accepted"] is True


def test_benign_notice_event_preserves_nominal_dispatch_with_bounded_decisions(
    monkeypatch, tmp_path
):
    actor, _ = actor_harness(monkeypatch, tmp_path, now=1000.0)

    def calculated(names):
        actor.forecast_count += 4 * len(names)
        actor.step()
        actor.candidates.update({n: make_candidate(actor, n) for n in names})
        return True

    def checked(notice):
        # Forecast doubles stand in for physical trajectories; actual scalar
        # admission is covered independently above using contact measurements.
        return {
            n: {
                "accepted": c["notice_sequence"] == notice["sequence"],
                "reasons": []
                if c["notice_sequence"] == notice["sequence"]
                else ["forecast_notice_binding"],
                "areas": ["east" if n == "nominal" else "west"],
            }
            for n, c in actor.candidates.items()
        }

    actor.calculate, actor.checks = calculated, checked
    result = actor.run_event_supervision()
    assert result["candidate_id"] == "nominal"
    assert result["time_s"] == actor.times["nominal"]
    assert actor.contact == {"contact": True}
    assert 2 <= len(actor.decisions) <= 8
    assert actor.forecast_count <= actor.envelope["maximum_forecasts"]
    assert all(r["candidate_id"] == "nominal" for r in actor.revisions)
    events = actor.monitor.events
    updated = next(e for e in events if e["kind"] == "notice_updated")
    assert updated["detected_at_s"] == 1450.0
    response = next(
        r for r in actor.decisions if updated["id"] in r["request"]["trigger_event_ids"]
    )
    assert response["request"]["time_s"] - updated["detected_at_s"] <= 2.0


def test_fast_forecast_result_is_discarded_if_confirmation_step_observes_new_notice(
    monkeypatch, tmp_path
):
    actor, clock = actor_harness(monkeypatch, tmp_path)
    pools = asynchronous_pool(monkeypatch, clock, completion_s=0.0)
    assert actor.calculate(["nominal"]) is False
    assert actor.candidates == {}
    assert pools[0].closed
    assert actor.s.time_s == 1450.0
    assert any(e["kind"] == "notice_updated" for e in actor.monitor.pending)
