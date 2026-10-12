"""Approved event margins must not strand an already booked next-orbit return.

Uses the finite fake plant harness, production orbital loop and real admission;
no numerical forecasts, API calls or full flights are performed here.
"""

import importlib.util
from pathlib import Path

import pytest

from src.intelligence import starship_replanning as intelligence
from src.runtime.starship_replanning import notices
from src.runtime.starship_replanning_events import EventMonitor


@pytest.fixture
def event_runtime_harness():
    path = Path(__file__).with_name("test_starship_replanning_event_runtime.py")
    spec = importlib.util.spec_from_file_location("_event_margin_runtime_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("margin", [0.0, 15.0, 120.0])
def test_booked_next_orbit_dispatches_for_each_approved_margin(
    monkeypatch, tmp_path, event_runtime_harness, margin
):
    actor, _ = event_runtime_harness.actor_harness(monkeypatch, tmp_path, now=1000.0)
    actor.envelope["event_supervision"]["deadline_margin_s"] = margin
    actor.monitor = EventMonitor(actor.envelope, actor.times)
    actor.monitor.poll(actor.observation(), notices(actor.case, actor.s.time_s), None)
    original_fixture = intelligence.fixture_decision

    def choose_next_then_preserve(request, envelope):
        response = original_fixture(request, envelope)
        if request["selected"] == "next_orbit" and "keep_plan" in request["allowed_actions"]:
            response["action"] = "keep_plan"
        elif "select_next_orbit" in request["allowed_actions"]:
            response["action"] = "select_next_orbit"
        return response

    monkeypatch.setattr(intelligence, "fixture_decision", choose_next_then_preserve)

    def calculated(names):
        actor.forecast_count += 4 * len(names)
        actor.step()
        actor.candidates.update(
            {name: event_runtime_harness.make_candidate(actor, name) for name in names}
        )
        return True

    actor.calculate = calculated
    result = actor.run_event_supervision()
    assert result is not None
    assert result["candidate_id"] == "next_orbit"
    assert result["time_s"] == actor.times["next_orbit"]
    assert actor.contact == {"contact": True}
    assert actor.forecast_count == 8
    assert len(actor.revisions) == 1
    horizon = actor.envelope["decision_timeout_s"] + actor.p["integration"]["coast_dt_s"]
    near_burn = [
        record
        for record in actor.decisions
        if record["request"]["time_s"] >= actor.times["next_orbit"] - horizon
    ]
    assert all(record["request_disposition"] == "deadline_fallback" for record in near_burn)
    assert all(record["request"]["time_s"] == record["later_time_s"] for record in near_burn)
    if margin <= horizon:
        assert near_burn
    assert all(
        not (
            actor.times["next_orbit"] - horizon
            <= operation["start_time_s"]
            <= actor.times["next_orbit"]
        )
        for operation in actor.pending
        if operation["operation"].startswith("model:")
    )


@pytest.mark.parametrize("cause", ["health", "deadline"])
def test_production_finish_emits_auditable_unresolved_event_disposition(
    monkeypatch, tmp_path, event_runtime_harness, cause
):
    from dataclasses import asdict, replace

    from src.runtime.starship_replanning_events import verify_events

    now = 1500.0 if cause == "health" else 7730.0
    actor, _ = event_runtime_harness.actor_harness(monkeypatch, tmp_path, now=now)
    actor.origin = asdict(actor.s)
    if cause == "health":
        actor.s = replace(
            actor.s, engine_states=(replace(actor.s.engine_states[0], available=False),)
        )
        actor.monitor.poll(actor.observation(), notices(actor.case, now), actor.selected)
        # The real orbital loop emits its urgent witness, without a model call.
        actor.run_event_supervision()
        assert actor.decisions == []
    actor.samples, actor.launch, actor.v = [], {}, None
    actor.controller.to_dict = lambda: {"phase": "orbital_coast"}
    monkeypatch.setattr(event_runtime_harness.runtime, "_sample", lambda *args: {})
    study = event_runtime_harness.runtime.ReplanningExecutor.finish(actor)
    assert study["dispatch"] is None
    assert study["execution"]["outcome"]["contact_receipt"] is None
    marker = next(
        event
        for event in study["execution"]["events"]
        if event["event"] == "m1_supervision_terminated"
    )
    assert marker["event_ids"] == [event["id"] for event in actor.monitor.pending]
    assert verify_events(study, actor.envelope, expected_case="event_normal") == []
