"""Analytic saved contacts/driver snapshots only; no flight, forecast or model."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
import math
from pathlib import Path
import socket
import subprocess

import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime import starship_restored_admission as admission
from src.runtime import starship_restored_contract as contract
from src.runtime import starship_restored_return as returning
from tests.contract.test_starship_restored_verifier import contact_fixture


@pytest.fixture(autouse=True)
def no_integration_models_or_external_calls(monkeypatch):
    def forbidden(*_a,**_k):
        raise AssertionError("admission tests cannot advance actual/predicted physics or call providers")
    for owner,name in ((dyn,"step"),(dyn,"_rhs"),(dyn,"_rk4"),(env,"step"),(mission,"advance_plant"),
        (returning,"advance_plant"),(returning,"control"),(returning,"forecast_restored_return"),
        (returning,"continue_restored_return"),(socket,"socket"),(socket,"create_connection"),(subprocess,"Popen")):
        monkeypatch.setattr(owner,name,forbidden)


def raw(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()


def case(*,candidate_id="nominal",speed=1.,fuel=30000.,retained=13,mutate=None):
    value,_,profile,_,launch=contact_fixture(candidate=candidate_id,speed=speed,fuel=fuel,retained=retained)
    handoff=deepcopy(launch["restored_prefix"]["handoff_record"])
    sources=handoff["source_sha256"]
    sources["src/runtime/starship_restored_admission.py"]="3"*64
    profile_raw=(Path(admission.__file__).resolve().parents[2]/"examples/spaceflight/starship-sixdof-profile.json").read_bytes()
    encoded=raw(handoff)
    origin=contract.ActualRestoredOrigin(sha256(encoded).hexdigest(),encoded,profile_raw,sources)
    anchor=contract.RestoredCandidateSchedule(origin)
    request=returning.RestoredReturnRequest(origin,anchor,candidate_id,profile_raw,sources)
    value.update(schema="missionos.starship_restored_return_prediction.v1",prediction_only=True,
        binding=request.record(),initial_binding=request.record())
    value["return_start_state"]={key:value["return_controller"]["record"]["activation"]["state"][key] for key in contract.STATE_KEYS}
    # Add one continuous coast bracket; the later contact remains an explicitly
    # invented analytic fixture, not an integrated trajectory or proof.
    s=dyn.state_from_dict(origin.record()["state"])
    craft=dyn.vehicle_from_dict(origin.record()["vehicle"])
    right=mission._sample(replace(s,time_s=s.time_s+2.),craft,"orbital_coast")
    value["samples"].insert(1,json.loads(raw(right)))
    when=value["outcome"]["contact_receipt"]["event_time_s"]
    if mutate:
        mutate(value)
    prediction=returning.RestoredReturnResult(raw(value))
    sites=(admission.RestoredSitePolicy("synth",0.,math.degrees(-env.EARTH_ROTATION_RAD_S*when),100000.,0.,16000.),)
    notice=admission.RestoredOperationalNotice(0,0.,16000.,("synth",))
    resources=admission.RestoredResourceState(0,origin.time_s)
    policy=admission.RestoredAdmissionPolicy()
    return prediction,origin,anchor,profile_raw,sources,policy,sites,notice,resources,request,profile


def candidate(values,**changes):
    prediction,origin,anchor,profile_raw,sources,policy,sites,notice,resources,*_=values
    args={"prediction":prediction,"forecast_origin":origin,"opportunity_anchor":anchor,
        "profile_raw_bytes":profile_raw,"expected_source_sha256":sources,"policy":policy,"sites":sites,
        "notice":notice,"resources":resources}
    return admission.RestoredCandidate(**{**args,**changes})


def current(values,*,time_s=None,state_change=None,binding_change=None):
    _,origin,_,_,_,_,_,_,_,request,_=values
    execution=returning.RestoredReturnExecution(request)
    try:
        # Scripted actual-driver observation only. No advance method is called.
        snapshot=execution.snapshot()
        if time_s is not None:
            snapshot["state"]["time_s"]=time_s
        if state_change:
            state_change(snapshot)
        if binding_change:
            binding_change(snapshot["binding"])
        execution._context["snapshot"]=snapshot
        return admission.RestoredCurrentObservation(origin,execution)
    finally:
        execution._iterator.close()


def check(values,selected=None,observation=None,*,stage="selection",notice=None,resources=None):
    return admission.admit_restored_candidate(selected or candidate(values),observation or current(values),
        notice=notice or values[7],resources=resources or values[8],stage=stage)


def test_candidate_is_compact_immutable_and_admits_only_independent_contact_and_context():
    values=case()
    selected=candidate(values)
    row=selected.record()
    result=check(values,selected)
    assert result["accepted"],result
    assert row["numerical_contact"]["capability_met"] is True
    assert "forecast_origin" not in row and "samples" not in row
    assert row["prediction_record_sha256"]==values[0].sha256
    assert result["mission_authority_granted"] is result["dispatch_authorized"] is False
    assert result["uncertainty_envelope_verified"] is False
    row["numerical_contact"]["capability_met"]=False
    assert selected.record()["numerical_contact"]["capability_met"] is True
    with pytest.raises(FrozenInstanceError):
        selected._json=b"{}"


@pytest.mark.parametrize("speed,fuel,reason",[(9.,30000.,"numerical_contact_capability_unmet"),(1.,27000.,"numerical_contact_capability_unmet")])
def test_claimed_metrics_cannot_turn_real_saved_geometry_negative_into_capability(speed,fuel,reason):
    values=case(speed=speed,fuel=fuel,mutate=lambda r:r["outcome"].update(contact_metrics={"all_passed":True}))
    result=check(values)
    assert not result["accepted"] and reason in result["reasons"]


def test_valid_noncontact_negative_is_preserved_as_ineligible_not_a_missing_contact_crash():
    def stopped(record):
        first=record["samples"][0]
        last=deepcopy(first)
        last.update(time_s=first["time_s"]+.25,contact=False)
        record.update(samples=[first,last],final_state={key:last[key] for key in contract.STATE_KEYS},
            return_start_state=None,return_controller=None,events=[record["events"][0],
                {"event":"control_tick_limit","time_s":last["time_s"]}])
        record["binding"]["maximum_control_ticks"]=1
        record["initial_binding"]["maximum_control_ticks"]=1
        record["outcome"]={"termination":"control_tick_limit","phase":"orbital_coast","integration_steps":1,
            "control_ticks":1,"contact_receipt":None,"contact_metrics":{"pretend_success":True}}
    values=case(mutate=stopped)
    selected=candidate(values)
    assert selected.record()["numerical_contact"]["contact_observed"] is False
    result=check(values,selected)
    assert not result["accepted"] and "numerical_contact_capability_unmet" in result["reasons"]


@pytest.mark.parametrize("change",["profile","source","actual_result","raw_origin","anchor","site_type","policy_type"])
def test_untyped_stale_or_retimed_tool_evidence_rejected(change):
    values=case()
    changes={}
    if change=="profile":
        changes["profile_raw_bytes"]=values[3]+b" "
    elif change=="source":
        source=deepcopy(values[4])
        source["src/runtime/starship_sixdof.py"]="4"*64
        changes["expected_source_sha256"]=source
    elif change=="actual_result":
        r=values[0].record()
        r.update(schema="missionos.starship_restored_return_continuation.v1",prediction_only=False)
        changes["prediction"]=returning.RestoredReturnResult(raw(r))
    elif change=="raw_origin":
        changes["forecast_origin"]=values[1].record()
    elif change=="anchor":
        changes["opportunity_anchor"]=values[2].record()
    elif change=="site_type":
        changes["sites"]=(values[6][0].record(),)
    else:
        changes["policy"]={"qualified":True}
    with pytest.raises(ValueError):
        candidate(values,**changes)


@pytest.mark.parametrize("field,offset,reason",[("r_eci_m",51.,"fresh_position_m"),("v_eci_mps",1.1,"fresh_velocity_mps"),
    ("omega_body_rad_s",.0011,"fresh_body_rate_rad_s")])
def test_each_choice_rechecks_fresh_current_state(field,offset,reason):
    values=case()
    observed=current(values,time_s=values[1].time_s+1.,state_change=lambda r:r["state"][field].__setitem__(0,r["state"][field][0]+offset))
    result=check(values,observation=observed)
    assert not result["accepted"] and reason in result["reasons"]


def test_fuel_and_attitude_match_are_not_inferred_from_inventory_or_capability():
    values=case()
    observed=current(values,time_s=values[1].time_s+1.,state_change=lambda r:r["state"].update(propellant_kg=r["state"]["propellant_kg"]-1.1))
    assert "fresh_fuel_kg" in check(values,observation=observed)["reasons"]
    def turn(row):
        row["state"]["q_body_to_eci"]=list(dyn.quaternion_multiply(tuple(row["state"]["q_body_to_eci"]),dyn.axis_angle((1.,0.,0.),math.radians(1.1))))
    observed=current(values,time_s=values[1].time_s+1.,state_change=turn)
    assert "fresh_attitude_deg" in check(values,observation=observed)["reasons"]


def test_quaternion_antipodes_interpolate_same_orientation_without_zero_quaternion():
    def flip(r):
        r["samples"][1]["q_body_to_eci"]=[-x for x in r["samples"][1]["q_body_to_eci"]]
    values=case(mutate=flip)
    result=check(values,observation=current(values,time_s=values[1].time_s+1.))
    assert result["accepted"],result
    assert result["fresh_state_match"]["errors"]["attitude_deg"]==pytest.approx(0.)


@pytest.mark.parametrize("kind",["gap","event","phase"])
def test_fresh_match_never_bridges_a_gap_event_or_return_transition(kind):
    def mutate(r):
        if kind=="gap":
            r["samples"][1]["time_s"]+=1.
        elif kind=="event":
            r["events"].insert(1,{"time_s":r["samples"][0]["time_s"]+1.,"event":"material_update"})
        else:
            r["samples"][1]["phase"]="deorbit_slew"
    values=case(mutate=mutate)
    if kind=="phase":
        with pytest.raises(ValueError,match="phase_order"):
            candidate(values)
        return
    result=check(values,observation=current(values,time_s=values[1].time_s+1.))
    assert not result["accepted"]
    assert any("coast" in reason for reason in result["reasons"])


def test_no_engine_healing_or_actual_state_from_forecast_is_accepted():
    values=case()
    with pytest.raises(ValueError,match="availability"):
        current(values,state_change=lambda r:r["state"]["engine_states"][0].update(available=False))
    with pytest.raises(ValueError):
        admission.RestoredCurrentObservation(values[1],values[0])
    with pytest.raises(ValueError):
        admission.RestoredCurrentObservation(values[1],{"actual":True})


@pytest.mark.parametrize("kind,reason",[("throttle","fresh_throttle"),("gimbal","fresh_gimbal_rad"),("flap","fresh_flap_rad")])
def test_finite_actuator_state_is_rechecked_not_only_pose_and_fuel(kind,reason):
    values=case()
    def change(snapshot):
        if kind=="throttle":
            snapshot["state"]["engine_states"][0]["throttle"]+=.0011
        elif kind=="gimbal":
            snapshot["state"]["engine_states"][0]["gimbal_y_rad"]+=.00011
        else:
            snapshot["state"]["flap_angles_rad"][3]+=.00011
    result=check(values,observation=current(values,state_change=change))
    assert not result["accepted"] and reason in result["reasons"]


def test_notice_and_resource_generation_change_rebinds_proof_without_changing_approved_proposal():
    values=case()
    old=candidate(values)
    notice=replace(values[7],generation=1)
    resources=replace(values[8],generation=1,energy_kwh=21.9)
    stale=check(values,old,notice=notice,resources=resources)
    assert not stale["accepted"] and any("rebind_saved_prediction" in r for r in stale["reasons"])
    new=candidate(values,notice=notice,resources=resources)
    assert old.record()["proposal_sha256"]==new.record()["proposal_sha256"]
    assert old.record()["forecast_proof_sha256"]!=new.record()["forecast_proof_sha256"]
    assert check(values,new,notice=notice,resources=resources)["accepted"]


def test_fresh_forecast_from_later_actual_origin_keeps_original_proposal_and_opportunity():
    values=case(candidate_id="next_orbit")
    old=candidate(values)
    # New invented actual coast witness, not a forecast checkpoint or flight.
    _,_,_,_,launch=contact_fixture(candidate="next_orbit",retained=13)
    handoff=deepcopy(launch["restored_prefix"]["handoff_record"])
    handoff["source_sha256"]=values[4]
    state=deepcopy(values[1].record()["state"])
    state["time_s"]+=1.
    state["propellant_kg"]-=.5
    state["v_eci_mps"][1]+=.1
    handoff["final_state"]=state
    handoff["inventory"]["observed_time_s"]=state["time_s"]
    handoff["state_history"].append({"state":state,"retained_count":13})
    encoded=raw(handoff)
    origin=contract.ActualRestoredOrigin(sha256(encoded).hexdigest(),encoded,values[3],values[4])
    schedule=contract.RestoredCandidateSchedule(origin,anchor=values[2])
    request=returning.RestoredReturnRequest(origin,schedule,"next_orbit",values[3],values[4])
    record=values[0].record()
    craft=dyn.vehicle_from_dict(origin.record()["vehicle"])
    state=dyn.state_from_dict(state)
    when=request.record()["return_time_s"]
    samples=[mission._sample(replace(state,time_s=t),craft,"orbital_coast") for t in (state.time_s,state.time_s+2.,when)]
    record.update(initial_state=origin.record()["state"],binding=request.record(),initial_binding=request.record(),
        samples=json.loads(raw(samples))+record["samples"][-1:])
    record["events"][0]["time_s"]=state.time_s
    record["return_start_state"]=json.loads(raw({key:samples[-1][key] for key in contract.STATE_KEYS}))
    record["return_controller"]["record"]["activation"]["state"]=json.loads(raw(samples[-1]))
    revised=returning.RestoredReturnResult(raw(record))
    new=candidate(values,prediction=revised,forecast_origin=origin)
    assert new.record()["proposal_sha256"]==old.record()["proposal_sha256"]
    assert new.record()["forecast_proof_sha256"]!=old.record()["forecast_proof_sha256"]
    assert new.record()["proposal"]["return_time_s"]==values[2].record()["next_orbit_s"]


@pytest.mark.parametrize("kind,reason",[("area","synthetic_area_or_contact_availability_window"),
    ("closed_site","synthetic_area_or_contact_availability_window"),("energy","synthetic_energy_budget"),
    ("temperature","synthetic_temperature_budget"),("loss","synthetic_reserve_after_modeled_loss_budget")])
def test_operational_resources_and_region_are_separate_from_numerical_capability(kind,reason):
    values=case()
    notice,resources=values[7:9]
    changes={}
    if kind=="area":
        changes["sites"]=(replace(values[6][0],latitude_deg=60.),)
    elif kind=="closed_site":
        notice=replace(notice,available_site_ids=())
    elif kind=="energy":
        resources=replace(resources,energy_kwh=10.)
    elif kind=="temperature":
        resources=replace(resources,temperature_k=310.)
    else:
        resources=replace(resources,modeled_propellant_loss_kg=3000.)
    selected=candidate(values,notice=notice,resources=resources,**changes)
    result=check(values,selected,notice=notice,resources=resources)
    assert result["numerical_contact_capability_met"] is True
    assert not result["accepted"] and reason in result["reasons"]


def test_dispatch_uses_first_due_reserved_tick_and_does_not_apply_new_booking_guard():
    values=case()
    selected=candidate(values)
    when=selected.record()["proposal"]["return_time_s"]
    observed=current(values,time_s=when)
    result=check(values,selected,observed,stage="dispatch")
    assert result["accepted"],result
    assert result["fresh_state_match"]["method"]=="exact_precontroller_return_start_state"
    assert "original_nominal_booking_guard" in check(values,selected,observed)["reasons"]
    observed=current(values,time_s=when+.5)
    assert "dispatch_not_first_due_reserved_tick" in check(values,selected,observed,stage="dispatch")["reasons"]


def test_next_orbit_dispatch_observation_does_not_weaken_old_prenominal_origin_contract():
    values=case(candidate_id="next_orbit")
    selected=candidate(values)
    when=selected.record()["proposal"]["return_time_s"]
    observed=current(values,time_s=when)
    result=check(values,selected,observed,stage="dispatch")
    assert when>values[2].record()["nominal_s"]
    assert result["fresh_state_match"]["method"]=="exact_precontroller_return_start_state"
    assert "dispatch_not_first_due_reserved_tick" not in result["reasons"]
    assert observed.record()["state"]["time_s"]==when and observed.record()["mission_authority_granted"] is False


def test_due_tick_slip_uses_only_explicit_precontroller_witness_at_exact_timestamp():
    def slipped(record):
        record["return_start_state"]["time_s"]+=.1
        record["events"][1]["time_s"]+=.1
        record["samples"][-2]["time_s"]+=.1
        record["return_controller"]["record"]["activation"]["time_s"]+=.1
        record["return_controller"]["record"]["activation"]["state"]["time_s"]+=.1
    values=case(mutate=slipped)
    selected=candidate(values)
    observed=current(values,time_s=selected.record()["proposal"]["return_time_s"]+.1)
    result=check(values,selected,observed,stage="dispatch")
    assert result["accepted"],result
    assert result["fresh_state_match"]["fraction"]==0.


def test_wrong_precontroller_witness_cannot_borrow_valid_contact():
    values=case(mutate=lambda record:record["return_start_state"]["r_eci_m"].__setitem__(0,record["return_start_state"]["r_eci_m"][0]+10.))
    with pytest.raises(ValueError,match="precontroller"):
        candidate(values)


def test_fixed_registered_limits_and_synthetic_site_bounds_are_not_caller_flags():
    with pytest.raises(TypeError):
        admission.RestoredAdmissionPolicy(qualified=True)
    with pytest.raises(ValueError):
        admission.RestoredSitePolicy("world",0.,0.,500001.,0.,16000.)
    with pytest.raises(ValueError):
        admission.RestoredOperationalNotice(True,0.,16000.,("synth",))
    with pytest.raises(ValueError):
        admission.RestoredResourceState(0,1.,energy_kwh=23.)
