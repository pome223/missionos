"""Synthetic actual-state seam tests; no plant integration or provider calls."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import socket
import subprocess

import numpy as np
import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime import starship_restored_prefix as prefix
from src.runtime import starship_restored_contract as contract
from src.runtime.starship_ascent import (AscentControlOptions, AscentCoastOrbitObjectives,
    AscentScreenSettings, StateInsertionOptions)
from src.runtime.starship_state_mission import ExperimentalStateMissionAscentSettings
from src.runtime.starship_booster_terminal_control import BoosterTerminalRecoverySettings
from src.runtime.starship_experimental_continuation import development_settings
from src.runtime.starship_retained_return import TRIMMED_POLICY_ID
from src.runtime.starship_return_sites import ReturnSite, ReturnSites
from src.runtime.starship_splashdown import load_goal
from src.runtime.starship_sixdof_separation import release_payload

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_flight_or_external_calls(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("prefix tests cannot run physics, qualifications or external calls")
    for owner,name in ((dyn,"step"),(dyn,"_rhs"),(dyn,"_rk4"),(env,"step"),
            (mission,"advance_plant"),(socket,"socket"),(socket,"create_connection"),(subprocess,"Popen")):
        monkeypatch.setattr(owner,name,forbidden)
    from src.runtime import starship_return_feasibility as qualification
    monkeypatch.setattr(qualification,"certificate_ready",forbidden)


@pytest.fixture
def setup():
    raw = (ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_bytes()
    profile = json.loads(raw)
    settings = ExperimentalStateMissionAscentSettings(AscentScreenSettings(policy="state_insertion_v7",
        objectives=AscentCoastOrbitObjectives(-20000.,300000.,275000.,220000.,50000.),
        control_options=AscentControlOptions(True,False),insertion_options=StateInsertionOptions()))
    controls = BoosterTerminalRecoverySettings(powered_terminal_alignment=False,shutdown_tail_only=True,
        low_pressure_reference_capture=True,powered_reference_capture=True,residual_authority_capture=True,
        energy_terminal_descent=True,planned_capture_impulse_compensation=True,terminal_horizontal_braking=True)
    source = {name:"1"*64 for name in contract.CRITICAL_SOURCES | {"src/runtime/starship_restored_prefix.py"}}
    source["examples/spaceflight/starship-sixdof-profile.json"] = contract.PROFILE_SHA256
    sites_raw = json.loads((ROOT/"examples/spaceflight/starship-return-sites-model-test.json").read_bytes())
    sites = ReturnSites(sites_raw["profile_id"],*(ReturnSite.from_dict(row) for row in sites_raw["sites"]))
    return raw,profile,settings,controls,source,sites,load_goal()


def runtime(setup, callback=lambda _summary:"continue_deployment", **changes):
    raw,_,settings,controls,source,*_ = setup
    args = {"profile_raw_bytes":raw,"expected_source_sha256":source,"ascent_settings":settings,
        "booster_control_settings":controls,"maximum_simulation_time_s":7500.,"deployment_decision":callback}
    return prefix.RestoredPrefixRuntime(**{**args,**changes})


def state_at(profile,time_s):
    craft = mission.vehicle(profile)
    radius = env.EARTH_EQUATORIAL_RADIUS_M+300000.
    return dyn.State6DOF(time_s,(np.float64(radius),0.,0.),
        (0.,np.float64(math.sqrt(env.EARTH_MU_M3_S2/radius)),0.),dyn.IDENTITY,(.0001,0.,0.),80000.,
        tuple(dyn.EngineState(throttle=.0001 if i==0 else 0.,gimbal_x_rad=.001 if i==0 else 0.)
            for i in range(len(craft.engines))),tuple(0. for _ in craft.aero_panels))


def completed_fixture(setup, retained, callback=None):
    _,profile,settings,_,_,*_=setup
    driver = runtime(setup,callback or (lambda row:"withhold_deployment"
        if row["retained_count"] == retained else "continue_deployment"))
    driver.begin(profile,settings,7500.)
    cutoff,settled = state_at(profile,1800.),state_at(profile,1805.)
    craft = mission.vehicle(profile)
    schedule = {"final_gate_time_s":1800.,"first_release_time_s":1845.,"return_request_time_s":3600.}
    driver.settled(settled,schedule)
    state = settled
    for i in range(27-retained):
        state = replace(state,time_s=1845.+i*profile["payload"]["interval_s"])
        if driver.needs_decision(i):
            driver.decide(state,i,release_ready=True,sequencer_state="running")
        if driver.closed:
            break
        after = mission.vehicle(profile,payload_count=25-i)
        cmass = profile["payload"]["mass_each_kg"]
        pmass = dyn.mass_properties(after,state.propellant_kg).mass_kg
        state,_,_,receipt = release_payload(state,craft,after,cmass,
            (0.,0.,profile["payload"]["center_z_m"]),mission.payload_inertia(profile),
            (pmass*cmass/(pmass+cmass)*profile["payload"]["release_speed_mps"],0.,0.))
        driver.released(receipt)
        craft = after
        if driver.closed:
            break
    result = {"outcome":{"termination":"restored_deployment_handoff","phase":"orbital_coast"},
        "final_state":asdict(state),"final_vehicle":asdict(craft),
        "experimental_mission_ascent":{"schema":"missionos.experimental_state_mission_ascent.v1",
            "settings":settings.record(),"cutoff_measurement":{"time_s":1800.,"state":asdict(cutoff),"gate_reached":True},
            "insertion_shutdown_until_s":1805.,"schedule":schedule,
            "settled_measurement":{"time_s":1805.,"state":asdict(settled),"vehicle":asdict(mission.vehicle(profile)),
                "gate_reached":True,"orbit":env.orbital_elements(mission.point_state(settled))}}}
    return driver,result


@pytest.mark.parametrize("retained",[0,13,26])
def test_receipts_determine_closed_inventory_and_exact_immutable_actual_origin(setup,retained):
    driver,result = completed_fixture(setup,retained)
    final = deepcopy(result["final_state"])
    evidence = driver.finish(result)
    assert evidence["closed"] is True and evidence["release_generation"] == 26-retained
    assert evidence["origin"]["state"] == prefix._native(final)
    assert evidence["origin"]["retained_count"] == retained
    assert evidence["origin"]["old_certificate_admitted"] is False
    assert driver.origin().record() == evidence["origin"]
    history = evidence["handoff_record"]["state_history"]
    assert len([1 for a,b in zip(history,history[1:]) if b["retained_count"] < a["retained_count"]]) == 26-retained
    assert all(row["state"]["engine_states"][0]["gimbal_x_rad"] == .001 for row in history)
    assert result["final_state"] == final
    evidence["origin"]["state"]["propellant_kg"] = -1
    assert driver.origin().record()["state"]["propellant_kg"] == 80000.
    with pytest.raises(ValueError,match="already_finished"):
        driver.finish(result)


@pytest.mark.parametrize("bad",[None,True,"continue",{"action":"continue_deployment"}])
def test_bad_callback_result_is_sticky_withhold_and_never_retried(setup,bad):
    seen=[]
    def callback(summary):
        seen.append(dict(summary))
        with pytest.raises(TypeError):
            summary["retained_count"] = 0
        assert set(summary) == {"time_s","released_count","retained_count","release_ready","sequencer_state","notice",
            "decision_started_s","decision_deadline_s"}
        return bad
    driver,result=completed_fixture(setup,26,callback)
    evidence=driver.finish(result)
    assert len(seen)==1 and evidence["origin"]["retained_count"] == 26
    assert evidence["decisions"][0]["callback_error_type"] == "ValueError"
    assert not driver.needs_decision(0)
    with pytest.raises(ValueError):
        driver.decide(state_at(setup[1],1850.),0,release_ready=True,sequencer_state="skipped")


def test_callback_exception_fails_closed_without_provider_retry(setup):
    calls=[]
    def broken(summary):
        calls.append(summary["time_s"])
        raise TimeoutError("must not persist callback contents")
    driver,result=completed_fixture(setup,26,broken)
    evidence=driver.finish(result)
    assert len(calls)==1 and evidence["decisions"][0]["callback_error_type"]=="TimeoutError"
    assert "must not persist" not in json.dumps(evidence)


def test_pending_poll_advances_actual_clock_and_deadline_closes_without_extra_callback(setup):
    seen=[]
    driver=runtime(setup,lambda summary:seen.append(dict(summary)) or prefix.PENDING_DEPLOYMENT_DECISION)
    driver.begin(setup[1],setup[2],7500.)
    driver.settled(state_at(setup[1],1805.),
        {"final_gate_time_s":1800.,"first_release_time_s":1845.,"return_request_time_s":3600.})
    assert driver.decide(state_at(setup[1],1845.),0,release_ready=True,sequencer_state="running") is prefix.PENDING_DEPLOYMENT_DECISION
    assert driver.decide(state_at(setup[1],1846.),0,release_ready=True,sequencer_state="running") is prefix.PENDING_DEPLOYMENT_DECISION
    assert seen[0]["decision_deadline_s"] is None
    assert seen[1]["decision_started_s"]==1845. and seen[1]["decision_deadline_s"]==1880.
    assert driver.decide(state_at(setup[1],1880.),0,release_ready=True,sequencer_state="running")=="withhold_deployment"
    assert driver.closed and len(seen)==2


def test_pending_resolution_is_one_inventory_decision_not_a_new_release_generation(setup):
    actions=iter((prefix.PENDING_DEPLOYMENT_DECISION,"continue_deployment"))
    driver=runtime(setup,lambda summary:next(actions))
    driver.begin(setup[1],setup[2],7500.)
    driver.settled(state_at(setup[1],1805.),
        {"final_gate_time_s":1800.,"first_release_time_s":1845.,"return_request_time_s":3600.})
    driver.decide(state_at(setup[1],1845.),0,release_ready=True,sequencer_state="running")
    assert driver.decide(state_at(setup[1],1845.25),0,release_ready=True,sequencer_state="running")=="continue_deployment"
    assert not driver.needs_decision(0) and not driver.closed
    evidence=driver.finish({})
    assert evidence["release_generation"]==0 and evidence["decisions"][0]["pending"]["poll_count"]==1


@pytest.mark.parametrize("change",[{"profile_raw_bytes":b"{}"},{"expected_source_sha256":{}},
    {"ascent_settings":object()},{"booster_control_settings":BoosterTerminalRecoverySettings()},
    {"maximum_simulation_time_s":True},{"maximum_simulation_time_s":16000.},{"deployment_decision":None}])
def test_scope_is_exact_frozen_and_cannot_borrow_legacy_certificate(setup,change):
    with pytest.raises(ValueError):
        runtime(setup,**change)


def test_configuration_copied_single_use_no_origin_without_closed_actual_evidence(setup):
    driver=runtime(setup)
    profile,settings=setup[1:3]
    driver.begin(profile,settings,7500.)
    with pytest.raises(ValueError,match="single_use"):
        driver.begin(profile,settings,7500.)
    with pytest.raises(ValueError,match="no_validated"):
        driver.origin()
    altered=deepcopy(profile)
    altered["payload"]["mass_each_kg"]+=1.
    with pytest.raises(ValueError,match="mismatch"):
        driver.validate_scope(altered,settings,setup[3],7500.)
    assert driver.finish({})["closed"] is False


def test_invalid_origin_preserves_actual_prefix_for_save_then_refuses_continuation(setup):
    driver,result=completed_fixture(setup,26)
    result["final_state"]["propellant_kg"]+=1000.
    before=deepcopy(result)
    evidence=driver.finish(result)
    assert evidence["closed"] is True and evidence["origin"] is None
    assert evidence["origin_validation_issue"]=="restored_history_clock_or_added_fuel"
    assert evidence["handoff_record"]["final_state"]==prefix._native(result["final_state"])
    assert evidence["handoff_record_sha256"] and result==before
    assert evidence["mission_authority_granted"] is False
    with pytest.raises(ValueError,match="no_validated_closed_actual_origin"):
        driver.origin()


@pytest.mark.parametrize("extra",[{"mission_director":object()},{"visual_runtime":object()},
    {"deployment_permission":lambda:True},{"trouble_manifest":object()},{"supervision":object()},
    {"booster_outage_control":object()},{"booster_terminal_recovery":None},{"experimental_ascent":None},
    {"duration_s":7400.},{"restored_runtime":object()}])
def test_shared_api_rejects_old_actor_mix_before_any_plant(setup,extra):
    _,profile,settings,controls,_,sites,goal=setup
    args={"duration_s":7500.,"return_policy":TRIMMED_POLICY_ID,"_development_return_qualification":development_settings(),
        "return_sites":sites,"splashdown_goal":goal,"experimental_ascent":settings,
        "booster_terminal_recovery":controls,"restored_runtime":runtime(setup)}
    with pytest.raises(ValueError):
        mission.simulate_concurrent(profile,**{**args,**extra})


def test_factory_routes_exact_actual_composition_and_unchanged_default_parameter(setup,monkeypatch):
    _,profile,settings,controls,_,sites,goal=setup
    driver=runtime(setup)
    seen={}
    def concurrent(actual,**kwargs):
        seen.update(kwargs)
        assert actual is profile
        return {"boundary":"common_concurrent"}
    monkeypatch.setattr(mission,"simulate_concurrent",concurrent)
    assert prefix.restored_prefix_mission(profile,driver,return_sites=sites,splashdown_goal=goal)=={"boundary":"common_concurrent"}
    assert seen["restored_runtime"] is driver and seen["experimental_ascent"] is settings
    assert seen["booster_terminal_recovery"] is controls and seen["duration_s"]==7500.
    assert "mission_director" not in seen and "deployment_permission" not in seen


def test_common_concurrent_finalizes_origin_after_actual_booster_routing(setup,monkeypatch):
    from src.runtime import starship_concurrent as concurrent, starship_sixdof_booster as booster
    from src.runtime.starship_booster_terminal_control import POLICY_ID
    _,profile,settings,controls,_,sites,goal=setup
    driver,result=completed_fixture(setup,26)
    seen={}
    separation=state_at(profile,135.)
    def ship_loop(actual,**kwargs):
        assert actual is profile
        seen["ship"]=kwargs
        return iter(())
    def booster_loop(actual,state,**kwargs):
        assert actual is profile and state==asdict(separation)
        seen["booster"]=kwargs
        return iter(())
    def coordinated(ship,factory,**kwargs):
        factory(separation)
        result.update(booster_run={"outcome":{"termination":"surface_contact"},"final_state":asdict(separation)},
            concurrent_execution={})
        return result
    monkeypatch.setattr(mission,"_simulate_steps",ship_loop)
    monkeypatch.setattr(booster,"_simulate_booster_steps",booster_loop)
    monkeypatch.setattr(concurrent,"coordinate",coordinated)
    record=prefix.restored_prefix_mission(profile,driver,return_sites=sites,splashdown_goal=goal)
    assert seen["ship"]["restored_runtime"] is driver
    assert seen["ship"]["_experimental_mission_ascent"] is settings
    assert seen["ship"]["return_sites"] is seen["ship"]["splashdown_goal"] is None
    assert seen["booster"]["guidance_policy"]==POLICY_ID
    assert seen["booster"]["terminal_recovery_settings"] is controls
    assert seen["booster"]["return_sites"] is sites and seen["booster"]["splashdown_goal"] is goal
    assert record["restored_prefix"]["origin"]==driver.origin().record()
    assert record["experimental_mission_ascent"]["booster_policy"]==POLICY_ID
    assert record["booster_run"]["shared_clock_checkpoint"]["state_reset"] is False


def test_real_ground_generator_starts_before_callbacks_and_preserves_actual_first_tick(setup):
    _,profile,settings,*_=setup
    seen=[]
    driver=runtime(setup,lambda summary:seen.append(summary) or "withhold_deployment")
    args={"duration_s":7500.,"return_policy":TRIMMED_POLICY_ID,"_development_return_qualification":development_settings(),
        "_defer_booster":True,"_defer_director_finish":True,"_experimental_mission_ascent":settings}
    old=mission._simulate_steps(profile,**args)
    new=mission._simulate_steps(profile,**args,restored_runtime=driver)
    try:
        old_tick,new_tick=next(old),next(new)
        assert asdict(old_tick)==asdict(new_tick) and new_tick.time_s==0.
        assert seen==[] and not driver.closed
    finally:
        old.close()
        new.close()


@pytest.mark.parametrize("decision_mode",["withhold","pending","all_released"])
def test_real_generator_closes_after_settle_without_return_or_physics(setup,monkeypatch,decision_mode):
    _,profile,settings,*_=setup
    calls=[]
    def decision(summary):
        calls.append(dict(summary))
        if decision_mode=="all_released":
            return "continue_deployment"
        return prefix.PENDING_DEPLOYMENT_DECISION if decision_mode=="pending" and len(calls)==1 else "withhold_deployment"
    driver=runtime(setup,decision)
    advanced=[]
    def scripted(state,craft,command,interval,*unused):
        phase=iterator.gi_frame.f_locals["phase"]
        advanced.append(phase)
        after=replace(state,time_s=state.time_s+interval)
        if len(advanced)==1:
            after=replace(after,propellant_kg=profile["booster"]["separation_reserve_kg"])
        elif len(advanced)==2:
            rp,ra=env.EARTH_EQUATORIAL_RADIUS_M-19000.,env.EARTH_EQUATORIAL_RADIUS_M+301000.
            radius=env.EARTH_EQUATORIAL_RADIUS_M+200000.
            h2=2*env.EARTH_MU_M3_S2*rp*ra/(rp+ra)
            vr=math.sqrt(env.EARTH_MU_M3_S2*(2/radius-2/(rp+ra))-h2/radius**2)
            after=replace(after,r_eci_m=(radius,0.,0.),v_eci_mps=(vr,math.sqrt(h2)/radius,0.),propellant_kg=100000.)
        elif len(advanced)==3:
            radius,rp=env.EARTH_EQUATORIAL_RADIUS_M+350000.,env.EARTH_EQUATORIAL_RADIUS_M+150000.
            vt=math.sqrt(2*env.EARTH_MU_M3_S2*rp/(radius*(rp+radius)))
            after=replace(after,r_eci_m=(radius,0.,0.),v_eci_mps=(0.,vt,0.),
                q_body_to_eci=mission._attitude((0.,1.,0.),(0.,0.,1.)),omega_body_rad_s=dyn.ZERO)
        elif len(advanced)==4:
            after=replace(after,v_eci_mps=(0.,math.sqrt(env.EARTH_MU_M3_S2/env.norm(after.r_eci_m)),0.))
        return after,None
    monkeypatch.setattr(mission,"advance_plant",scripted)
    if decision_mode=="all_released":
        # This is an invented child-coast snapshot boundary, never a dyn.step
        # invocation. Parent coast commands still reach the scripted boundary.
        monkeypatch.setattr(dyn,"step",lambda state,_craft,_command,dt,**_k:replace(state,time_s=state.time_s+dt))
    monkeypatch.setattr(mission,"ReturnController",lambda *_a,**_k:pytest.fail("prefix must not dispatch return"))
    iterator=mission._simulate_steps(profile,duration_s=7500.,return_policy=TRIMMED_POLICY_ID,
        _development_return_qualification=development_settings(),_defer_booster=True,_defer_director_finish=True,
        _experimental_mission_ascent=settings,restored_runtime=driver)
    while True:
        try:
            next(iterator)
        except StopIteration as done:
            result=done.value
            break
    evidence=driver.finish(result)
    schedule=result["experimental_mission_ascent"]["schedule"]
    assert result["outcome"]["termination"]=="restored_deployment_handoff"
    assert result["outcome"]["phase"]=="orbital_coast"
    assert len(calls)==(1 if decision_mode=="withhold" else 2) and calls[0]["time_s"]>=schedule["first_release_time_s"]
    if decision_mode=="pending":
        assert calls[1]["time_s"]==pytest.approx(calls[0]["time_s"]+profile["integration"]["coast_dt_s"])
    assert result["final_state"]["time_s"]<schedule["return_request_time_s"]
    assert evidence["origin"]["retained_count"]==(0 if decision_mode=="all_released" else 26)
    assert evidence["origin"]["state"]==prefix._native(result["final_state"])
    if decision_mode=="all_released":
        assert len(result["satellites"])==26
        last_release=result["satellites"][-1]["receipt"]["time_s"]
        # Complete the already-selected coast command/finite plant interval
        # after the26th release, as the legacy production loop does.
        assert result["final_state"]["time_s"]==pytest.approx(last_release+profile["integration"]["coast_dt_s"])
        assert advanced[-1]=="orbital_coast"
    else:
        assert result["satellites"]==[]
    assert "deorbit_burn" not in advanced
