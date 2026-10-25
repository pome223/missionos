"""Same-build v7 composition; scripted NumPy snapshots, zero real integration."""
from copy import deepcopy
from dataclasses import asdict, FrozenInstanceError, replace
import json
import math
from pathlib import Path

import numpy as np
import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime import starship_state_mission as seam
from src.runtime.starship_ascent import (AscentControlOptions, AscentCoastOrbitObjectives,
    AscentScreenSettings, AscentShutdown, ExperimentalMissionAscentSettings, StateInsertionOptions)
from src.runtime.starship_experimental_continuation import development_settings
from src.runtime.starship_experimental_mission import validate_physical_mission
from src.runtime.starship_retained_return import TRIMMED_POLICY_ID
from src.runtime.starship_return_sites import ReturnSite, ReturnSites
from src.runtime.starship_splashdown import load_goal

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_physics_or_qualification(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("state mission contracts cannot integrate or borrow qualification")
    for owner, name in ((dyn, "step"), (dyn, "_rhs"), (dyn, "_rk4"), (env, "step"),
                        (mission, "advance_plant")):
        monkeypatch.setattr(owner, name, forbidden)
    from src.runtime import starship_return_feasibility as qualification
    monkeypatch.setattr(qualification, "certificate_ready", forbidden)


@pytest.fixture
def setup():
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_bytes())
    settings = seam.ExperimentalStateMissionAscentSettings(AscentScreenSettings(policy="state_insertion_v7",
        objectives=AscentCoastOrbitObjectives(-20000., 300000., 275000., 220000., 50000.),
        control_options=AscentControlOptions(True, False), insertion_options=StateInsertionOptions()))
    raw = json.loads((ROOT/"examples/spaceflight/starship-return-sites-model-test.json").read_bytes())
    sites = ReturnSites(raw["profile_id"], *(ReturnSite.from_dict(row) for row in raw["sites"]))
    return profile, settings, sites, load_goal()


def arguments(settings):
    return {"duration_s": 7500., "return_policy": TRIMMED_POLICY_ID,
        "_development_return_qualification": development_settings(), "_defer_booster": True,
        "_defer_director_finish": True, "_experimental_mission_ascent": settings}


def test_separate_exact_frozen_settings_admit_only_two_registered_configurations(setup):
    profile, settings, sites, goal = setup
    seam.validate_state_physical_mission(profile, settings, 7500., sites, goal)
    outage = replace(settings, ascent=replace(settings.ascent, shutdowns=(
        AscentShutdown("booster_main_32", 76.), AscentShutdown("ship_main_3", 437.))))
    outage.validate_profile(profile)
    assert settings.record()["schema"] == "missionos.experimental_state_mission_ascent_settings.v1"
    assert settings.record()["qualified_return"] is False
    with pytest.raises(FrozenInstanceError):
        settings.ascent = None
    with pytest.raises(ValueError):
        validate_physical_mission(profile, settings, 7500., sites, goal)
    old = ExperimentalMissionAscentSettings(AscentScreenSettings(policy="coast_orbit_v5",
        objectives=settings.ascent.objectives))
    with pytest.raises(ValueError):
        seam.validate_state_physical_mission(profile, old, 7500., sites, goal)


@pytest.mark.parametrize("change", ["offset", "moving", "options", "one_outage", "outage_time", "objectives", "v5"])
def test_explicit_new_version_does_not_silently_expand_scope(setup, change):
    _, settings, _, _ = setup
    value = settings.ascent
    if change == "offset":
        value = replace(value, stack_pitch_offset_deg=-10.)
    elif change == "moving":
        value = replace(value, control_options=AscentControlOptions(True, True))
    elif change == "options":
        value = replace(value, insertion_options=replace(value.insertion_options, shutdown_settle_s=4.))
    elif change == "one_outage":
        value = replace(value, shutdowns=(AscentShutdown("ship_main_3", 437.),))
    elif change == "outage_time":
        value = replace(value, shutdowns=(AscentShutdown("booster_main_32", 76.), AscentShutdown("ship_main_3", 438.)))
    elif change == "objectives":
        value = replace(value, objectives=replace(value.objectives, first_burn_apogee_m=310000.))
    else:
        value = AscentScreenSettings(policy="coast_orbit_v5", objectives=value.objectives)
    with pytest.raises(ValueError, match="frozen_v7"):
        seam.ExperimentalStateMissionAscentSettings(value)


@pytest.mark.parametrize("path,value", [(('guidance', 'target_perigee_m'), 219999.),
    (('guidance', 'orbit_release_delay_s'), 44.), (('guidance', 'coast_before_return_s'), 1801.),
    (('payload', 'count'), 25), (('ship', 'return_reserve_kg'), 27999.),
    (('ship', 'ascent_reserve_kg'), 74999.)])
def test_inventory_reserves_and_relative_schedule_remain_frozen(setup, path, value):
    profile, settings, _, _ = deepcopy(setup)
    profile[path[0]][path[1]] = value
    with pytest.raises(ValueError):
        settings.validate_profile(profile)


@pytest.mark.parametrize("duration", [None, True, 3336., 7500.1, math.inf, math.nan])
def test_bounded_horizon_cannot_be_borrowed_or_extended(setup, duration):
    profile, settings, sites, goal = setup
    with pytest.raises(ValueError):
        seam.validate_state_physical_mission(profile, settings, duration, sites, goal)


def test_exact_new_type_and_site_binding_are_required(setup):
    profile, settings, sites, goal = setup
    class Derived(seam.ExperimentalStateMissionAscentSettings):
        pass
    with pytest.raises(ValueError):
        seam.validate_state_physical_mission(profile, Derived(settings.ascent), 7500., sites, goal)
    with pytest.raises(ValueError):
        seam.validate_state_physical_mission(profile, settings, 7500., object(), goal)
    with pytest.raises(ValueError, match="splashdown_site_binding"):
        seam.validate_state_physical_mission(profile, settings, 7500., sites,
            replace(goal, reference_site_sha256="0"*64))
    with pytest.raises(ValueError, match="tick_observer"):
        seam.state_physical_mission(profile, settings, return_sites=sites, splashdown_goal=goal, on_tick=object())


@pytest.mark.parametrize("bad", [None, "perigee", "apogee", "fuel", "rate", "main_unavailable", "escape"])
def test_settled_gate_reads_actual_envelope_and_preserves_vacuum_outage(setup, bad):
    profile, settings, _, _ = setup
    craft = mission.vehicle(profile)
    radius = env.EARTH_EQUATORIAL_RADIUS_M+(219000. if bad == "perigee" else 601000. if bad == "apogee" else 350000.)
    engines = tuple(dyn.EngineState(.2 if i == 0 else 0., available=i != 3 and not (bad == "main_unavailable" and i == 0))
        for i in range(len(craft.engines)))
    state = dyn.State6DOF(2000., (np.float64(radius), 0., 0.),
        (0., np.float64(12000. if bad == "escape" else math.sqrt(env.EARTH_MU_M3_S2/radius)), 0.),
        dyn.IDENTITY, (5.1 if bad == "rate" else 0., 0., 0.),
        74999. if bad == "fuel" else 100000., engines, tuple(0. for _ in craft.aero_panels))
    settings = replace(settings, ascent=replace(settings.ascent, shutdowns=(
        AscentShutdown("booster_main_32", 76.), AscentShutdown("ship_main_3", 437.))))
    before = asdict(state)
    measurement = seam.settled_insertion_measurement(state, craft, profile, settings)
    assert measurement["gate_reached"] is (bad is None)
    assert asdict(state) == before == measurement["state"]
    assert measurement["state"]["engine_states"][3]["available"] is False
    assert type(measurement["gate_reached"]) is bool


@pytest.mark.parametrize("extra", [{"mission_director": object()}, {"visual_runtime": object()},
    {"_trouble_engine": object()}, {"deployment_permission": lambda: True}, {"supervision": object()},
    {"_ascent_screen": AscentScreenSettings(policy="guidance_outcome_v3")}, {"_experimental_ascent_origin": object()},
    {"_defer_booster": False}, {"_defer_director_finish": False}, {"return_policy": "fixed_v1"},
    {"_development_return_qualification": None}, {"scenario": "engine_out"}, {"dt_scale": .5}])
def test_inner_scope_rejects_other_hooks_before_constructing_any_vehicle(setup, monkeypatch, extra):
    profile, settings, _, _ = setup
    monkeypatch.setattr(mission, "vehicle", lambda *_a, **_k: pytest.fail("scope must fail before construction"))
    with pytest.raises(ValueError):
        next(mission._simulate_steps(profile, **{**arguments(settings), **extra}))


@pytest.mark.parametrize("extra", [{"mission_director": object()}, {"visual_runtime": object()},
    {"trouble_manifest": object()}, {"trouble_control": object()}, {"deployment_permission": lambda: True},
    {"development_booster_coast_control": "stopping_distance_v1"}, {"return_policy": "fixed_v1"},
    {"_development_return_qualification": None}])
def test_concurrent_scope_rejects_other_hooks(setup, extra):
    profile, settings, sites, goal = setup
    with pytest.raises(ValueError):
        mission.simulate_concurrent(profile, **{"duration_s": 7500., "return_policy": TRIMMED_POLICY_ID,
            "_development_return_qualification": development_settings(), "experimental_ascent": settings,
            "return_sites": sites, "splashdown_goal": goal, **extra})


def scripted_record(setup, monkeypatch, *, settled_bad=False):
    """Drive the production generator with saved-style snapshots, not dynamics."""
    profile, settings, _, _ = setup
    calls, ticks, ascent_calls = [], [], []
    before_step, limit_step = mission.AscentController.before_step, mission.AscentController.limit_step

    def before(self, state, craft):
        ascent_calls.append(("before", iterator.gi_frame.f_locals["phase"]))
        return before_step(self, state, craft)

    def limit(self, time, interval):
        ascent_calls.append(("limit", iterator.gi_frame.f_locals["phase"]))
        return limit_step(self, time, interval)

    def fake_advance(state, craft, command, interval, *unused):
        phase = iterator.gi_frame.f_locals["phase"]
        calls.append((phase, state, command, interval))
        after = replace(state, time_s=state.time_s+interval)
        if len(calls) == 1:
            after = replace(after, propellant_kg=profile["booster"]["separation_reserve_kg"])
        elif len(calls) == 2:
            rp, ra = env.EARTH_EQUATORIAL_RADIUS_M-19000., env.EARTH_EQUATORIAL_RADIUS_M+301000.
            radius = env.EARTH_EQUATORIAL_RADIUS_M+200000.
            h2 = 2*env.EARTH_MU_M3_S2*rp*ra/(rp+ra)
            vr = math.sqrt(env.EARTH_MU_M3_S2*(2/radius-2/(rp+ra))-h2/radius**2)
            after = replace(after, r_eci_m=(radius, 0., 0.), v_eci_mps=(vr, math.sqrt(h2)/radius, 0.),
                propellant_kg=100000.)
        elif len(calls) == 3:
            radius, rp = env.EARTH_EQUATORIAL_RADIUS_M+350000., env.EARTH_EQUATORIAL_RADIUS_M+150000.
            vt = math.sqrt(2*env.EARTH_MU_M3_S2*rp/(radius*(rp+radius)))
            after = replace(after, r_eci_m=(radius, 0., 0.), v_eci_mps=(0., vt, 0.),
                q_body_to_eci=mission._attitude((0., 1., 0.), (0., 0., 1.)), omega_body_rad_s=dyn.ZERO)
        elif len(calls) == 4:
            radius = env.norm(after.r_eci_m)
            after = replace(after, v_eci_mps=(0., math.sqrt(env.EARTH_MU_M3_S2/radius), 0.),
                engine_states=tuple(replace(row, throttle=.2 if i == 0 else row.throttle)
                    for i, row in enumerate(after.engine_states)))
        elif phase == "ascent_screen_shutdown":
            # A finite inherited state persists; production may request zero
            # but must never overwrite these snapshot actuators itself.
            if settled_bad:
                after = replace(after, propellant_kg=74999.)
        elif phase == "orbital_coast":
            after = replace(after, omega_body_rad_s=(5.1, 0., 0.))
        return replace(after, r_eci_m=tuple(np.float64(x) for x in after.r_eci_m),
            v_eci_mps=tuple(np.float64(x) for x in after.v_eci_mps)), None

    monkeypatch.setattr(mission.AscentController, "before_step", before)
    monkeypatch.setattr(mission.AscentController, "limit_step", limit)
    monkeypatch.setattr(mission, "advance_plant", fake_advance)
    iterator = mission._simulate_steps(profile, **arguments(settings))
    while True:
        try:
            ticks.append(next(iterator))
        except StopIteration as done:
            record = done.value
            break
    return record, calls, ticks, ascent_calls


def test_real_generator_settles_actual_state_then_freezes_trace_and_uses_shared_loop(setup, monkeypatch):
    record, calls, ticks, ascent_calls = scripted_record(setup, monkeypatch)
    metadata = record["experimental_mission_ascent"]
    cutoff, settled = metadata["cutoff_measurement"], metadata["settled_measurement"]
    assert ticks[0].time_s == 0. and ticks[0].phase == "stack_ascent"
    assert metadata["schema"] == "missionos.experimental_state_mission_ascent.v1"
    assert settled["time_s"] == metadata["insertion_shutdown_until_s"] == cutoff["time_s"]+5.
    assert settled["gate_reached"] is True
    assert metadata["requested_absolute_duration_s"] == 7500.
    assert metadata["schedule"] == {"final_gate_time_s": cutoff["time_s"],
        "first_release_time_s": cutoff["time_s"]+45., "return_request_time_s": cutoff["time_s"]+1800.}
    tail = [row for row in calls if row[0] == "ascent_screen_shutdown"]
    assert sum(row[3] for row in tail) == pytest.approx(5.)
    assert tail[0][1].engine_states[0].throttle == settled["state"]["engine_states"][0]["throttle"] == .2
    assert all(not row[2].engines[0].enabled for row in tail)
    handoff = next(row for row in metadata["transition_witnesses"] if row["to_phase"] == "orbital_coast")
    assert handoff["state_before"] == handoff["state_after"] == settled["state"]
    assert next(row for row in record["events"] if row["event"] == "state_insertion_settled_handoff")["state_reset"] is False
    assert metadata["insertion_vehicle"] == settled["vehicle"]
    rows = record["ascent_control_trace"]["rows"]
    assert len(rows) == metadata["ascent_trace_steps"] == record["outcome"]["integration_steps"]-1
    assert rows[-1]["state_returned_by_plant"] == settled["state"]
    assert all(row["phase"] != "orbital_coast" for row in rows)
    assert all(phase != "orbital_coast" for _, phase in ascent_calls)
    assert record["ascent_control_trace"]["terminal_commands"] == []
    assert record["outcome"]["payload_released_count"] == 0 and record["satellites"] == []
    assert metadata["qualified_return"] is metadata["all_features_completed"] is False
    encoded = json.dumps(record, allow_nan=False, sort_keys=True, separators=(",", ":"))
    assert json.dumps(json.loads(encoded), allow_nan=False, sort_keys=True, separators=(",", ":")) == encoded


def test_failed_settled_measurement_is_retained_without_dispatch(setup, monkeypatch):
    record, calls, _, _ = scripted_record(setup, monkeypatch, settled_bad=True)
    metadata = record["experimental_mission_ascent"]
    assert record["outcome"]["termination"] == "state_insertion_settled_gate_unreached"
    assert record["outcome"]["phase"] == "ascent_screen_shutdown"
    assert record["outcome"]["orbit_gate_reached"] is False
    assert metadata["schedule"] is None and metadata["settled_measurement"]["gate_reached"] is False
    assert metadata["insertion_vehicle"] is None
    assert metadata["settled_measurement"]["state"] == record["final_state"]
    assert all(phase != "orbital_coast" for phase, *_ in calls)
    assert not any(row["event"] == "orbit_cutoff_command" for row in record["events"])
    assert metadata["ascent_trace_steps"] == record["outcome"]["integration_steps"]


def test_pending_outage_cannot_be_lost_by_early_handoff(setup, monkeypatch):
    profile, settings, sites, goal = setup
    settings = replace(settings, ascent=replace(settings.ascent, shutdowns=(
        AscentShutdown("booster_main_32", 76.), AscentShutdown("ship_main_3", 437.))))
    record, _, _, _ = scripted_record((profile, settings, sites, goal), monkeypatch)
    metadata = record["experimental_mission_ascent"]
    assert metadata["shutdown_receipts"] == []
    assert metadata["settled_measurement"]["state"]["time_s"] < 76.
    assert metadata["settled_measurement"]["gate_reached"] is False
    assert metadata["schedule"] is None
    assert record["outcome"]["termination"] == "state_insertion_settled_gate_unreached"


def test_factory_preserves_actual_concurrent_body_routing_and_peak_witness(setup, monkeypatch):
    from src.runtime import starship_concurrent as concurrent, starship_sixdof_booster as booster
    from src.runtime.starship_concurrent import PhysicsTick
    profile, settings, sites, goal = setup
    state = next(mission._simulate_steps(profile, **arguments(settings))).state
    calls, observed = {}, []
    def ship_loop(_p, **kwargs):
        calls["ship"] = kwargs
        return iter(())
    def booster_loop(_p, actual, **kwargs):
        assert actual == asdict(state)
        calls["booster"] = kwargs
        return iter(())
    def coordinate(_ship, factory, **kwargs):
        factory(state)
        tick = PhysicsTick("booster", state.time_s, "booster_entry_coast", state)
        kwargs["on_tick"](tick)
        return {"booster_run": {"outcome": {"termination": "surface_contact"}, "final_state": asdict(state)},
            "concurrent_execution": {}, "experimental_mission_ascent": {}}
    monkeypatch.setattr(mission, "_simulate_steps", ship_loop)
    monkeypatch.setattr(booster, "_simulate_booster_steps", booster_loop)
    monkeypatch.setattr(concurrent, "coordinate", coordinate)
    record = seam.state_physical_mission(profile, settings, return_sites=sites, splashdown_goal=goal,
        on_tick=observed.append)
    assert calls["ship"]["_experimental_mission_ascent"] is settings
    assert calls["ship"]["return_sites"] is calls["ship"]["splashdown_goal"] is None
    assert calls["booster"]["return_sites"] is sites and calls["booster"]["splashdown_goal"] is goal
    assert calls["booster"]["development_coast_control"] == "fixed_v1"
    assert calls["booster"]["_trouble_engine"] is calls["booster"]["_trouble_control"] is None
    assert len(observed) == 1
    assert record["booster_run"]["runner_booster_peak_witness"]["state"] == asdict(state)
    assert record["experimental_mission_ascent"]["booster_policy"] == "fixed_v1"
