"""Saved-only restoration checks; invented contact fixtures are not flights."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import math
import socket
import subprocess

import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime import starship_sixdof_contact as contact
from src.runtime import starship_restored_contract as contract
from src.runtime import starship_restored_verifier as verifier
from src.runtime import starship_restored_return as returning
from src.runtime.starship_retained_return import TRIMMED_POLICY_ID, new_record
from tests.contract.test_starship_state_mission_metrics import evidence as negative_evidence
from tests.contract.test_starship_restored_prefix import setup as prefix_setup, completed_fixture


def wire(value):
    return json.loads(json.dumps(value,allow_nan=False))


@pytest.fixture(autouse=True)
def no_integration_or_external_calls(monkeypatch):
    def forbidden(*_args,**_kwargs):
        raise AssertionError("saved verifier cannot propagate, forecast, or call external providers")
    for owner,name in ((dyn,"step"),(dyn,"_rhs"),(dyn,"_rk4"),(env,"step"),
            (mission,"advance_plant"),(mission,"simulate_concurrent"),(contact,"find_contact"),
            (socket,"socket"),(socket,"create_connection"),(subprocess,"Popen")):
        monkeypatch.setattr(owner,name,forbidden)


def scope(setup):
    _,_,settings,controls,source,sites,goal=setup
    source=deepcopy(source)
    source.update({name:"2"*64 for name in ("src/runtime/starship_restored_return.py","src/runtime/starship_restored_verifier.py")})
    return {"expected_source_sha256":source,"expected_settings":settings.record(),"expected_control_settings":controls.record(),
        "expected_site":sites.divert.to_dict(),"expected_goal":goal.to_dict(),"expected_return_sites":sites.to_dict(),
        "expected_prefix_horizon_s":7500.,"expected_total_horizon_s":16000.,"expected_return_candidate_id":"nominal",
        "expected_plan_sha256":"a"*64,"expected_case_id":"synthetic"}


def execution(expected):
    return {"schema":verifier.EXECUTION_SCHEMA,"plan_sha256":expected["expected_plan_sha256"],"case_id":expected["expected_case_id"],
        "source_sha256":expected["expected_source_sha256"],"profile_sha256":contract.PROFILE_SHA256,
        "settings_sha256":verifier.digest(expected["expected_settings"]),"control_settings_sha256":verifier.digest(expected["expected_control_settings"]),
        "prefix_horizon_s":7500.,"total_horizon_s":16000.,"return_candidate_id":expected["expected_return_candidate_id"]}


@pytest.fixture
def valid_negative(monkeypatch):
    setup=prefix_setup.__wrapped__()
    launch,profile,_=negative_evidence.__wrapped__()
    expected=scope(setup)
    launch.pop("experimental_mission_execution")
    launch["experimental_mission_ascent"].update(booster_policy="availability_terminal_recovery_v1",
        booster_terminal_recovery=expected["expected_control_settings"])
    launch["restored_prefix"]={"schema":"missionos.starship_restored_prefix.v1","closed":False,
        "decisions":[],"release_generation":0,"pending_decision":None,"decision_window_s":35.,"state_reset":False,
        "old_certificate_admitted":False,"mission_authority_granted":False,"model_calls_inferred":False,
        "handoff_record":None,"handoff_record_sha256":None,"origin":None}
    def forbidden(*_a,**_k):
        raise AssertionError("Verifier reused a controller or producer")
    from src.runtime import starship_ascent as ascent
    monkeypatch.setattr(mission,"control",forbidden)
    monkeypatch.setattr(ascent.AscentController,"request",forbidden)
    monkeypatch.setattr(returning,"continue_restored_return",forbidden)
    monkeypatch.setattr(returning,"forecast_restored_return",forbidden)
    return {"schema":verifier.SCHEMA,"execution":execution(expected),"prefix":launch,"return":None},profile,expected


def test_full_actual_ascent_negative_is_valid_without_inventing_origin_or_return(valid_negative):
    record,profile,expected=valid_negative
    before=deepcopy(record)
    result=verifier.verify_restored_mission(record,profile,**expected)
    assert result["passed"],result
    assert result["physical_success"] is result["ship_physical_success"] is result["ai_control_completed"] is False
    assert result["termination"]=="angular_rate_envelope_exceeded"
    assert result["verified_ascent_command_samples"]>0 and record==before


@pytest.mark.parametrize("path,value",[
    (("prefix","restored_prefix","origin_validation_issue"),"restored_invalid_actual_origin"),
    (("schema",),"missionos.experimental_state_physical_mission.v1"),
    (("execution","plan_sha256"),"b"*64),(("execution","profile_sha256"),"b"*64),
    (("execution","case_id"),"other"),(("execution","return_candidate_id"),"next_orbit"),
    (("prefix","ascent_control_trace","rows",0,"command","engines",0,"throttle"),.4),
    (("prefix","concurrent_execution","tick_order",0,"next_time_s"),1.),
    (("prefix","samples",0,"engine_states",0,"available"),False),
    (("prefix","restored_prefix","old_certificate_admitted"),True),
    (("prefix","restored_prefix","closed"),True),(("return",),{}),
    (("prefix","outcome","termination"),"numerical_failure"),
])
def test_source_actual_control_clock_availability_scope_and_unclosed_dispatch_tamper_rejected(valid_negative,path,value):
    record,profile,expected=valid_negative
    target=record
    for key in path[:-1]:
        target=target[key]
    target[path[-1]]=value
    assert not verifier.verify_restored_mission(record,profile,**expected)["passed"]


@pytest.mark.parametrize("field",verifier.FORBIDDEN)
def test_old_actor_certificate_and_diagnostic_scope_stays_forbidden(valid_negative,field):
    record,profile,expected=valid_negative
    record["prefix"][field]={}
    assert not verifier.verify_restored_mission(record,profile,**expected)["passed"]


@pytest.mark.parametrize("kind",["request","activation"])
def test_all_negative_prefix_terminations_still_reject_an_already_dispatched_return(valid_negative,kind):
    record,profile,expected=valid_negative
    if kind=="request":
        record["prefix"]["events"].insert(0,{"event":"return_requested","time_s":.1})
    else:
        record["prefix"]["retained_return"]["activation"]={"time_s":.1}
    checked=verifier.verify_restored_mission(record,profile,**expected)
    assert not checked["passed"] and "before any return" in checked["issues"][0]["detail"]


def contact_fixture(*,retained=13,speed=1.,fuel=30000.,candidate="nominal"):
    setup=prefix_setup.__wrapped__()
    expected=scope(setup)
    setup=(*setup[:4],expected["expected_source_sha256"],*setup[5:])
    driver,prefix_record=completed_fixture(setup,retained)
    evidence=driver.finish(prefix_record)
    prefix_record=wire(prefix_record)
    prefix_record["restored_prefix"]=evidence
    prefix_record["satellites"]=[{"receipt":receipt} for receipt in evidence["handoff_record"]["inventory"]["release_receipts"]]
    origin=driver.origin()
    profile=setup[1]
    typed=setup[2]
    request=returning.RestoredReturnRequest(origin,contract.RestoredCandidateSchedule(origin),candidate,
        setup[0],expected["expected_source_sha256"])
    # Analytic equatorial material-point contact. No return trajectory was run.
    craft=dyn.vehicle_from_dict(origin.record()["vehicle"])
    com=dyn.mass_properties(craft,fuel).com_body_m[2]
    time=request.record()["return_time_s"]+1000.
    q=dyn.axis_angle((0.,1.,0.),math.pi/2)
    r=(env.EARTH_EQUATORIAL_RADIUS_M+com,0.,0.)
    velocity=env.add(env.cross((0.,0.,env.EARTH_ROTATION_RAD_S),r),(-speed,0.,0.))
    final=dyn.State6DOF(time,r,velocity,q,dyn.inverse_rotate(q,(0.,0.,env.EARTH_ROTATION_RAD_S)),fuel,
        tuple(dyn.EngineState(available=row["available"]) for row in origin.record()["state"]["engine_states"]),
        tuple(0. for _ in craft.aero_panels))
    command=dyn.Command6DOF(tuple(dyn.EngineCommand() for _ in craft.engines),final.flap_angles_rad)
    geometry=contact.hull_clearance(final,craft,profile["geometry"]["ship_length_m"],profile["geometry"]["radius_m"])
    receipt=wire(contact._receipt(final,craft,command,geometry,contact=True,bracket=[time-.01,time]))
    before=dyn.state_from_dict(origin.record()["state"])
    start=replace(before,time_s=request.record()["return_time_s"])
    first_sample=mission._sample(before,craft,"orbital_coast")
    activation=mission._sample(start,craft,"orbital_coast")
    last_sample=mission._sample(final,craft,"landing_burn")
    last_sample["contact"]=True
    policy=new_record(TRIMMED_POLICY_ID)
    policy.update(status="triggered",activation={"time_s":start.time_s,"payload_retained_count":retained,
        "payload_retained_mass_kg":retained*profile["payload"]["mass_each_kg"],"state":activation})
    controller={"schema":"missionos.ship_return_controller.v1","policy":TRIMMED_POLICY_ID,
        "return_time_s":start.time_s,"released_count":26-retained,"allocation_enabled":True,"registered":False,
        "terminal_engine_out":False,"phase":"landing_burn","record":policy}
    term="low_speed_surface_contact" if speed<=5. else "surface_impact"
    result={"schema":"missionos.starship_restored_return_continuation.v1","prediction_only":False,
        "binding":request.record(),"initial_binding":request.record(),"initial_state":origin.record()["state"],
        "vehicle":origin.record()["vehicle"],"final_state":asdict(final),"final_vehicle":asdict(craft),
        "samples":[first_sample,activation,last_sample],"events":[
            {"event":"restored_return_continuation_start","time_s":before.time_s},
            {"event":"return_requested","time_s":start.time_s},
            {"event":term,"time_s":time,"contact":receipt}],"return_controller":controller,
        "reservation_changes":[],"retained_count":retained,"payload_releases_after_origin":0,
        "actual_state_initialized_from_prediction":False,"source_provenance_authenticated":False,
        "old_certificate_admitted":False,"qualification_refreshed":False,"mission_authority_granted":False,
        "dispatch_authorized":False,"physical_execution":False,"independent_physical_model":False,"model_calls":0,
        "outcome":{"termination":term,"phase":"landing_burn","integration_steps":2,"control_ticks":3,
            "contact_receipt":receipt,"contact_metrics":returning._contact_metrics(final,receipt,profile)}}
    return wire(result),origin,profile,typed,prefix_record


@pytest.mark.parametrize("retained",[0,13,26])
@pytest.mark.parametrize("candidate",["nominal","next_orbit"])
def test_independent_same_actual_origin_and_contact_geometry_retained_mass(retained,candidate,monkeypatch):
    record,origin,profile,settings,_=contact_fixture(retained=retained,candidate=candidate)
    before=deepcopy(record)
    monkeypatch.setattr(returning,"_contact_metrics",lambda *_a:pytest.fail("cannot reuse contact result producer"))
    measured=verifier._return(record,origin,profile,settings,candidate,16000)
    assert measured["contact_envelope_met"] is True and measured["point_speed_mps"]==pytest.approx(1.)
    assert record==before


@pytest.mark.parametrize("speed,fuel",[(9.,30000.),(1.,27000.)])
def test_valid_contact_record_preserves_physical_speed_or_reserve_negative(speed,fuel):
    record,origin,profile,settings,_=contact_fixture(speed=speed,fuel=fuel)
    measured=verifier._return(record,origin,profile,settings,"nominal",16000.)
    assert measured["contact_envelope_met"] is False


@pytest.mark.parametrize("mutation",["prediction","initial_state","mass","unavailable","fuelfill","request",
    "contactpoint","contactspeed","gatetrue","gate_int","certificate","late","model_bool"])
def test_return_tamper_fails_without_using_any_controller_or_dynamics(mutation):
    record,origin,profile,settings,_=contact_fixture()
    if mutation=="prediction":
        record["schema"]="missionos.starship_restored_return_prediction.v1"
        record["prediction_only"]=True
    elif mutation=="initial_state":
        record["initial_state"]["engine_states"][0]["throttle"]=0.
    elif mutation=="mass":
        record["vehicle"]["dry_mass_kg"]+=1.
    elif mutation=="unavailable":
        record["samples"][-1]["engine_states"][0]["available"]=False
    elif mutation=="fuelfill":
        record["samples"][-1]["propellant_kg"]=100000.
    elif mutation=="request":
        record["binding"]["return_time_s"]+=5.
    elif mutation=="contactpoint":
        record["outcome"]["contact_receipt"]["point_eci_m"][0]+=10.
    elif mutation=="contactspeed":
        record["outcome"]["contact_receipt"]["surface_relative_speed_mps"]=0.
    elif mutation=="gatetrue":
        record["outcome"]["contact_metrics"]["gates"]["reserve"]=False
    elif mutation=="gate_int":
        record["outcome"]["contact_metrics"]["gates"]["reserve"]=1
    elif mutation=="certificate":
        record["return_qualification_sha256"]="a"*64
    elif mutation=="late":
        record["events"][1]["time_s"]+=5.
    else:
        record["model_calls"]=False
    with pytest.raises((ValueError,saved_invalid())):
        verifier._return(record,origin,profile,settings,"nominal",16000.)


def saved_invalid():
    from src.runtime.starship_sixdof_verifier import _Invalid
    return _Invalid


def test_handoff_cannot_substitute_another_saved_origin_or_receipt_generation():
    _,origin,profile,_,launch=contact_fixture()
    raw=(verifier.Path(__file__).resolve().parents[2]/"examples/spaceflight/starship-sixdof-profile.json").read_bytes()
    expected=origin.record()["source_sha256"]
    assert verifier._origin(launch,raw,expected).record()==origin.record()
    launch["restored_prefix"]["origin"]["inventory_generation"]+=1
    with pytest.raises(saved_invalid()):
        verifier._origin(launch,raw,expected)


def test_false_availability_helper_return_is_rejected_explicitly(monkeypatch):
    record,origin,profile,settings,_=contact_fixture()
    monkeypatch.setattr(verifier.state_checks,"_check_availability",lambda *_a:False)
    with pytest.raises(saved_invalid(),match="availability"):
        verifier._return(record,origin,profile,settings,"nominal",16000.)


@pytest.mark.parametrize("retained",[0,13,26])
def test_decisions_bind_actual_slot_inventory_and_independent_release_gate(retained):
    _,_,profile,_,launch=contact_fixture(retained=retained)
    handoff=launch["restored_prefix"]["handoff_record"]
    launch["samples"]=[wire(mission._sample(dyn.state_from_dict(row["state"]),
        mission.vehicle(profile,payload_count=row["retained_count"]),"orbital_coast")) for row in handoff["state_history"]]
    verifier._decisions(launch,handoff,profile)
    launch["restored_prefix"]["decisions"][0]["summary"]["release_ready"]=False
    with pytest.raises(saved_invalid(),match="readiness"):
        verifier._decisions(launch,handoff,profile)


def test_pending_deadline_uses_observed_actual_state_and_cannot_reset_or_extend_clock():
    _,_,profile,_,launch=contact_fixture(retained=26)
    handoff=launch["restored_prefix"]["handoff_record"]
    decision=launch["restored_prefix"]["decisions"][0]
    decision["pending"]={"released_count":0,"started_s":1845.,"deadline_s":1880.,"poll_count":8}
    decision["summary"].update(time_s=1847.,decision_started_s=1845.,decision_deadline_s=1880.)
    handoff["final_state"]["time_s"]=1847.
    state=dyn.state_from_dict(handoff["final_state"])
    craft=mission.vehicle(profile)
    launch["samples"]=[wire(mission._sample(replace(state,time_s=t),craft,"orbital_coast")) for t in (1845.,1847.)]
    verifier._decisions(launch,handoff,profile)
    decision["pending"]["deadline_s"]+=1.
    with pytest.raises(saved_invalid(),match="Pending"):
        verifier._decisions(launch,handoff,profile)
