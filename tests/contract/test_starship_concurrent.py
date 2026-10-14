"""Shared-clock ordering and legacy drains, with every physics step stubbed."""
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path

import pytest

from src.runtime import starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime import starship_sixdof_booster as booster
from src.runtime import starship_booster_control as control
from src.runtime.starship_concurrent import PhysicsTick, coordinate, drain


@dataclass(frozen=True)
class ClockState:
    time_s: float
    inherited_identity: str = "exact-separation"


def clock_fixture(events):
    separation = ClockState(.1)
    def ship():
        yield PhysicsTick("ship", 0., "stack_ascent", ClockState(0.))
        events.append(("ship", 0.))
        yield PhysicsTick("ship", .1, "ship_ascent", ClockState(.1), separation)
        events.append(("ship", .1))
        yield PhysicsTick("ship", .2, "ship_ascent", ClockState(.2))
        events.append(("ship", .2))
        return {"final_state": {"time_s": .3}, "booster_separation_state": asdict(separation), "outcome": {}}
    def separated(state):
        assert state is separation
        for t in [.1, .15, .2]:
            yield PhysicsTick("booster", t, "booster_return", ClockState(t))
            events.append(("booster", t))
        return {"final_state": {"time_s": .25}, "booster_separation_state": asdict(state)}
    return ship, separated


def test_scheduler_runs_observers_and_bodies_in_time_order_without_sorting():
    events, observed = [], []
    ship, separated = clock_fixture(events)
    result = coordinate(ship(), separated, maximum_tick_s=.1, on_tick=lambda tick: observed.append((tick.body_id,tick.time_s)))
    expected = [("ship",0.),("booster",.1),("ship",.1),("booster",.15),("booster",.2),("ship",.2)]
    assert events == observed == expected
    trace = result["concurrent_execution"]["tick_order"]
    assert [(r["body_id"],r["time_s"]) for r in trace] == expected
    assert result["concurrent_execution"]["maximum_observed_body_lead_s"] <= .1
    assert result["concurrent_execution"]["posthoc_ordering"] is False
    assert result["booster_run"]["final_state"]["time_s"] == .25


def test_shared_clock_rejects_later_booster_start_not_hidden_by_sorting():
    ship, _ = clock_fixture([])
    def wrong_booster(state):
        yield PhysicsTick("booster", .15, "booster_return", ClockState(.15))
    with pytest.raises(ValueError, match="exact_separation_time"):
        coordinate(ship(), wrong_booster, maximum_tick_s=.1)


@pytest.mark.parametrize("next_time", [-.1, .21])
def test_clock_rejects_rewind_or_multiple_unobserved_steps(next_time):
    def wrong_ship():
        yield PhysicsTick("ship", 0., "stack_ascent", ClockState(0.))
        yield PhysicsTick("ship", next_time, "stack_ascent", ClockState(next_time))
    with pytest.raises(ValueError, match="body_exceeded"):
        coordinate(wrong_ship(), None, maximum_tick_s=.1)


def test_simulate_concurrent_finishes_director_once_after_both_bodies(monkeypatch):
    events = []
    ship, separated = clock_fixture(events)
    def ship_loop(profile, **kwargs):
        assert kwargs["_defer_booster"] is True
        assert kwargs["_defer_director_finish"] is True
        return ship()
    # Identity is checked by the pure coordinator test; wrapper round-trip is exact data.
    def relaxed_booster(profile, state, **kwargs):
        assert kwargs["development_coast_from_separation"] is True
        assert kwargs["duration_s"] == pytest.approx(.2)
        for t in [.1,.2]:
            yield PhysicsTick("booster",t,"booster_return",ClockState(t))
            events.append(("booster",t))
        return {"final_state":{"time_s":.3},"booster_separation_state":state,
                "outcome":{"termination":"time_limit"}}
    class Director:
        calls = 0
        def finish(self):
            self.calls += 1
            assert ("ship",.2) in events and ("booster",.2) in events
            return {"finished":True}
    director = Director()
    monkeypatch.setattr(mission,"_simulate_steps",ship_loop)
    monkeypatch.setattr(booster,"_simulate_booster_steps",relaxed_booster)
    result = mission.simulate_concurrent({"integration":{"powered_dt_s":.1,"coast_dt_s":.1}, "booster":{"engine_count":33}},
        duration_s=.3, mission_director=director, development_booster_coast_control="stopping_distance_v1")
    assert director.calls == 1
    assert result["mission_director"] == {"finished":True}
    assert result["concurrent_execution"]["absolute_deadline_s"] == .3
    assert result["booster_run"]["shared_clock_checkpoint"] == {
        "absolute_deadline_s":.3, "unfinished":True, "state_reset":False}


@pytest.fixture
def profile():
    return json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())


def booster_state(profile, fuel=87_000.):
    body = mission.vehicle(profile,"booster")
    return dyn.State6DOF(12.,(6_578_137.,0.,0.),(0.,7800.,0.),(1.,0.,0.,0.),(.01,0.,0.),fuel,
        tuple(dyn.EngineState() for _ in body.engines),tuple(0. for _ in body.aero_panels))


def test_ship_legacy_wrapper_equals_drain_without_real_integration(profile, monkeypatch):
    steps = []
    def advance(state, body, command, dt, *args):
        steps.append((state.time_s,dt))
        return replace(state,time_s=state.time_s+dt),None
    monkeypatch.setattr(mission,"advance_plant",advance)
    iterator = mission._simulate_steps(profile,scenario="gimbal_step",duration_s=.1)
    first = next(iterator)
    assert first.body_id == "ship" and first.time_s == 0.
    assert steps == []
    manually_drained = drain(iterator)
    wrapped = mission.simulate(profile,scenario="gimbal_step",duration_s=.1)
    assert manually_drained == wrapped
    assert "concurrent_execution" not in wrapped
    assert steps == [(0.,.1),(0.,.1)]


def test_booster_legacy_wrapper_equals_drain_without_real_integration(profile, monkeypatch):
    steps = []
    def step(state, body, command, dt):
        steps.append((state.time_s,dt))
        return replace(state,time_s=state.time_s+dt)
    monkeypatch.setattr(dyn,"step",step)
    original = asdict(booster_state(profile))
    iterator = booster._simulate_booster_steps(profile,original,duration_s=.1)
    assert next(iterator).time_s == 12.
    assert steps == []
    manually_drained = drain(iterator)
    wrapped = booster.simulate_booster(profile,original,duration_s=.1)
    assert manually_drained == wrapped
    assert "development_coast_from_separation" not in wrapped
    assert [t for t, _ in steps] == [12.,12.]
    assert [dt for _, dt in steps] == pytest.approx([.1,.1])


def test_full_continuation_candidate_is_explicit_and_changes_only_coast_command(profile, monkeypatch):
    calls = []
    original_control = control.control_coast_stopping_distance
    def candidate(*args, **kwargs):
        calls.append(args[0].time_s)
        return original_control(*args, **kwargs)
    monkeypatch.setattr(control,"control_coast_stopping_distance",candidate)
    monkeypatch.setattr(dyn,"step",lambda state,body,command,dt:replace(state,time_s=state.time_s+dt))
    # Low-fuel initialized fixture reaches the existing reserve cutoff immediately.
    original = asdict(booster_state(profile,fuel=50_000.))
    result = booster.simulate_booster(profile,original,duration_s=.25,
        development_coast_control="stopping_distance_v1",development_coast_from_separation=True)
    assert calls == [12.]
    assert result["development_coast_from_separation"]["terminal_alignment_gate_changed"] is False
    assert result["booster_separation_state"] == original
    assert result["outcome"]["termination"] == "time_limit"
    assert result["guidance_configuration"] == booster._configuration(profile)


def test_drain_returns_original_exception():
    def failed():
        yield None
        raise RuntimeError("original failure")
    with pytest.raises(RuntimeError,match="original failure"):
        drain(failed())


@pytest.mark.parametrize("permitted,expected_released", [(True,1),(False,0)])
def test_permission_reaches_actual_release_sequencer_with_stub_dynamics(profile, monkeypatch, permitted, expected_released):
    # Arrange instantaneous phase transitions solely to exercise the sequencer.
    profile["booster"]["separation_reserve_kg"] = 1e20
    profile["guidance"]["orbit_release_delay_s"] = 0.
    monkeypatch.setattr(mission.env,"orbital_elements",lambda *args:{"status":"bound","perigee_altitude_m":300_000.})
    monkeypatch.setattr(mission,"advance_plant",lambda state,body,command,dt,*args:(replace(state,time_s=state.time_s+dt),None))
    monkeypatch.setattr(dyn,"step",lambda state,body,command,dt,**kwargs:replace(state,time_s=state.time_s+dt))
    permission_calls = []
    def permission():
        permission_calls.append(1)
        # A later True must not clear the first False.
        return permitted if len(permission_calls)==1 else True
    result = drain(mission._simulate_steps(profile,duration_s=.1,_defer_booster=True,deployment_permission=permission))
    assert result["outcome"]["payload_released_count"] == expected_released
    assert result["deployment_permission"]["withheld"] is (not permitted)
    if not permitted:
        assert result["deployment_permission"]["sequencer_state"] == "skipped"
        assert len([e for e in result["events"] if e["event"]=="supervised_deployment_withheld"]) == 1
        assert not any(e["event"]=="payload_released" for e in result["events"])


def test_permission_rejects_truthy_model_strings_without_acting(profile, monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError("invalid permission may not advance physics")
    monkeypatch.setattr(mission,"advance_plant",forbidden)
    with pytest.raises(ValueError,match="must_return_boolean"):
        mission.simulate(profile,duration_s=.1,deployment_permission=lambda:"continue_mission")


def test_partial_executed_clock_survives_plant_exception():
    def ship():
        yield PhysicsTick("ship", 0., "stack_ascent", ClockState(0.))
        raise RuntimeError("plant failed after first tick")
    with pytest.raises(RuntimeError, match="plant failed") as caught:
        coordinate(ship(), None, maximum_tick_s=.1)
    evidence = caught.value.concurrent_execution
    assert evidence["incomplete"] is True and evidence["posthoc_ordering"] is False
    assert evidence["tick_order"] == [{"sequence":0,"body_id":"ship","time_s":0.,"phase":"stack_ascent",
        "sequencer_state":None,"released_count":0,"deployment_withheld":False}]


def test_json_separation_identity_accepts_tuple_list_round_trip():
    @dataclass(frozen=True)
    class VectorState:
        time_s: float
        vector: tuple = (1.,2.,3.)
    separation = VectorState(.1)
    def ship():
        yield PhysicsTick("ship", .1, "ship_ascent", separation, separation)
        return {"final_state":{"time_s":.2}, "booster_separation_state":json.loads(json.dumps(asdict(separation))), "outcome":{}}
    def booster(state):
        yield PhysicsTick("booster", .1, "booster_return", state)
        return {"final_state":{"time_s":.2}, "booster_separation_state":asdict(state)}
    assert coordinate(ship(), booster, maximum_tick_s=.1)["concurrent_execution"]["separation_state_preserved"]
