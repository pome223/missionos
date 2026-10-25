"""Synthetic saved-state contracts; no flight, controller or provider calls."""
from copy import deepcopy
from dataclasses import asdict, FrozenInstanceError, replace
from hashlib import sha256
import json
import math
from pathlib import Path
import socket
import subprocess

import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_restored_contract as contract
from src.runtime import starship_sixdof_mission as mission
from src.runtime.starship_ascent import (AscentControlOptions, AscentCoastOrbitObjectives,
    AscentScreenSettings, AscentShutdown, StateInsertionOptions)
from src.runtime.starship_state_mission import ExperimentalStateMissionAscentSettings
from src.runtime.starship_booster_terminal_control import BoosterTerminalRecoverySettings
from src.runtime.starship_sixdof_separation import release_payload

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_plant_controller_or_external_call(monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError("Restored contract tests cannot run physics, controllers or providers")
    for owner,name in ((dyn,"step"),(dyn,"_rhs"),(dyn,"_rk4"),(env,"step"),
            (mission,"advance_plant"),(mission,"control"),(socket,"socket"),
            (socket,"create_connection"),(subprocess,"Popen")):
        monkeypatch.setattr(owner,name,forbidden)


def encode(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()


def native(value):
    return json.loads(encode(value))


def fixture_record(*,retained=26,outage=False):
    profile_raw = (ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_bytes()
    profile = json.loads(profile_raw)
    source = {name:"1"*64 for name in contract.CRITICAL_SOURCES}
    source["examples/spaceflight/starship-sixdof-profile.json"] = contract.PROFILE_SHA256
    outages = (AscentShutdown("booster_main_32",76.),AscentShutdown("ship_main_3",437.)) if outage else ()
    settings = ExperimentalStateMissionAscentSettings(AscentScreenSettings(policy="state_insertion_v7",
        shutdowns=outages, objectives=AscentCoastOrbitObjectives(-20000.,300000.,275000.,220000.,50000.),
        control_options=AscentControlOptions(True,False),insertion_options=StateInsertionOptions()))
    control = BoosterTerminalRecoverySettings(powered_terminal_alignment=False,shutdown_tail_only=True,
        low_pressure_reference_capture=True,powered_reference_capture=True,residual_authority_capture=True,
        energy_terminal_descent=True,planned_capture_impulse_compensation=True,terminal_horizontal_braking=True)
    craft = mission.vehicle(profile,payload_count=26)
    radius = env.EARTH_EQUATORIAL_RADIUS_M+300000.
    state = dyn.State6DOF(1800.,(radius,0.,0.),(0.,math.sqrt(env.EARTH_MU_M3_S2/radius),0.),
        dyn.IDENTITY,(.0001,.0002,0.),81000.,
        tuple(dyn.EngineState(throttle=.00001 if i==0 else 0.,gimbal_x_rad=.001 if i==0 else 0.,
            available=not(outage and i==3)) for i in range(len(craft.engines))),
        tuple(0. for _ in craft.aero_panels))
    cutoff = native(asdict(state))
    state = replace(state,time_s=1805.,propellant_kg=80000.)
    settled = native(asdict(state))
    history,receipts = [{"state":settled,"retained_count":26}],[]
    for i in range(26-retained):
        # Analytic instantaneous-release fixture, not a coast integration.
        state = replace(state,time_s=1845.+i*profile["payload"]["interval_s"])
        history.append({"state":native(asdict(state)),"retained_count":26-i})
        after = mission.vehicle(profile,payload_count=25-i)
        child_mass = profile["payload"]["mass_each_kg"]
        mass = dyn.mass_properties(after,state.propellant_kg).mass_kg
        state,_,_,receipt = release_payload(state,craft,after,child_mass,
            (0.,0.,profile["payload"]["center_z_m"]),mission.payload_inertia(profile),
            (mass*child_mass/(mass+child_mass)*profile["payload"]["release_speed_mps"],0.,0.))
        receipts.append(native(receipt))
        history.append({"state":native(asdict(state)),"retained_count":25-i})
        craft = after
    state = replace(state,time_s=state.time_s+5.,propellant_kg=state.propellant_kg-1.)
    current = native(asdict(state))
    history.append({"state":current,"retained_count":retained})
    record = {"schema":contract.RECORD_SCHEMA,"source_sha256":source,"profile_sha256":contract.PROFILE_SHA256,
        "experimental_mission_ascent":{"schema":"missionos.experimental_state_mission_ascent.v1",
            "settings":settings.record(),"cutoff_measurement":{"time_s":1800.,"state":cutoff,"gate_reached":True},
            "insertion_shutdown_until_s":1805.,
            "settled_measurement":{"time_s":1805.,"state":settled,"vehicle":native(asdict(mission.vehicle(profile,payload_count=26))),
                "orbit":native(env.orbital_elements(env.State3D(1805.,tuple(settled["r_eci_m"]),tuple(settled["v_eci_mps"]),80000.))),
                "gate_reached":True},
            "schedule":{"final_gate_time_s":1800.,"first_release_time_s":1845.,"return_request_time_s":3600.}},
        "booster_control_settings":control.record(),"phase":"orbital_coast","final_state":current,
        "final_vehicle":native(asdict(craft)),"state_history":history,
        "inventory":{"schema":contract.INVENTORY_SCHEMA,"total_count":26,"released_count":26-retained,
            "retained_count":retained,"generation":26-retained,"sequencer_state":"running" if retained==0 else "skipped",
            "pending_action":None,"observed_time_s":state.time_s,"release_receipts":receipts}}
    return record,profile_raw,deepcopy(source)


def build(record,profile_raw,source):
    raw = encode(record)
    return contract.ActualRestoredOrigin(sha256(raw).hexdigest(),raw,profile_raw,source)


def later_origin(record, profile, source, *, elapsed_s=5., velocity_delta_mps=.1):
    """Invented later evidence only; no integration or trajectory claim."""
    record = deepcopy(record)
    state = deepcopy(record["final_state"])
    state["time_s"] += elapsed_s
    state["v_eci_mps"][1] += velocity_delta_mps
    state["propellant_kg"] -= .1
    record["final_state"] = state
    record["inventory"]["observed_time_s"] = state["time_s"]
    record["state_history"].append({"state":state,"retained_count":record["inventory"]["retained_count"]})
    return build(record,profile,source),record


def test_optional_anchor_freezes_original_opportunities_across_actual_period_drift():
    record, profile, source = fixture_record(retained=13,outage=True)
    origin = build(record,profile,source)
    base = contract.RestoredCandidateSchedule(origin)
    current, later = later_origin(record,profile,source)
    recomputed = contract.RestoredCandidateSchedule(current)
    anchored = contract.RestoredCandidateSchedule(current,anchor=base)
    initial, fresh, fixed = base.record(),recomputed.record(),anchored.record()
    assert "opportunity_anchor" not in initial
    assert fresh["next_orbit_s"] != initial["next_orbit_s"]
    assert fixed["nominal_s"] == initial["nominal_s"] and fixed["next_orbit_s"] == initial["next_orbit_s"]
    assert fixed["actual_current_orbital_period_s"] == fresh["actual_current_orbital_period_s"]
    assert fixed["opportunity_anchor"] == base.opportunity_anchor_record()
    assert fixed["opportunity_anchor"]["source_record_sha256"] == origin.source_record_sha256
    assert fixed["opportunity_anchor"]["schedule_sha256"] == sha256(base._schedule_json).hexdigest()
    newest,_ = later_origin(later,profile,source)
    nested = contract.RestoredCandidateSchedule(newest,anchor=anchored)
    assert nested.record()["opportunity_anchor"] == base.opportunity_anchor_record()
    assert nested.record()["next_orbit_s"] == initial["next_orbit_s"]
    nested.record()["opportunity_anchor"]["next_orbit_s"] = 999999.
    assert nested.record()["opportunity_anchor"] == base.opportunity_anchor_record()


@pytest.mark.parametrize("mutation",["raw_anchor","bool_anchor","source","inventory","availability","horizon","earlier","bool_horizon"])
def test_anchor_rejects_untyped_source_inventory_horizon_or_backwards_evidence(mutation):
    record,profile,source = fixture_record(retained=13,outage=True)
    original = build(record,profile,source)
    current,_ = later_origin(record,profile,source)
    anchor = contract.RestoredCandidateSchedule(original)
    horizon = 16000.
    if mutation == "raw_anchor":
        anchor = anchor.record()
    elif mutation == "bool_anchor":
        anchor = True
    elif mutation == "source":
        changed = deepcopy(source)
        changed["src/runtime/starship_ship_return.py"] = "2"*64
        different = deepcopy(record)
        different["source_sha256"] = changed
        current,_ = later_origin(different,profile,changed)
    elif mutation == "inventory":
        other,_,_ = fixture_record(retained=0,outage=True)
        current,_ = later_origin(other,profile,source)
    elif mutation == "availability":
        other,_,_ = fixture_record(retained=13,outage=False)
        current,_ = later_origin(other,profile,source)
    elif mutation == "horizon":
        horizon -= 1.
    elif mutation == "earlier":
        anchor = contract.RestoredCandidateSchedule(current)
        current = original
    else:
        horizon = True
    with pytest.raises(ValueError):
        contract.RestoredCandidateSchedule(current,horizon,anchor)


def test_anchor_rejects_a_separately_valid_different_actual_cutoff_epoch():
    original,profile,source = fixture_record(retained=26,outage=True)
    anchor = contract.RestoredCandidateSchedule(build(original,profile,source))
    changed = native(original)
    metadata = changed["experimental_mission_ascent"]
    for name in ("cutoff_measurement","settled_measurement"):
        metadata[name]["time_s"] += 1.
        metadata[name]["state"]["time_s"] += 1.
    metadata["insertion_shutdown_until_s"] += 1.
    for key in metadata["schedule"]:
        metadata["schedule"][key] += 1.
    changed["state_history"][0]["state"]["time_s"] += 1.
    changed["state_history"][-1]["state"]["time_s"] += 1.
    changed["final_state"]["time_s"] += 1.
    changed["inventory"]["observed_time_s"] += 1.
    current = build(changed,profile,source)
    with pytest.raises(ValueError,match="anchor_source_inventory_clock_or_horizon"):
        contract.RestoredCandidateSchedule(current,anchor=anchor)


@pytest.mark.parametrize("retained",[0,13,26])
@pytest.mark.parametrize("outage",[False,True])
def test_actual_inventory_bound_origin_is_immutable_and_schedule_uses_current_orbit(retained,outage):
    record,profile,source = fixture_record(retained=retained,outage=outage)
    before = deepcopy(record)
    origin = build(record,profile,source)
    result = origin.record()
    assert result["state"] == record["final_state"] and result["vehicle"] == record["final_vehicle"]
    assert result["retained_count"] == retained and result["released_count"] == result["inventory_generation"] == 26-retained
    assert result["release_generation"] == result["inventory_generation"]
    assert result["command_revision"] is None and "excludes AI command revisions" in result["inventory_generation_semantics"]
    assert result["state"]["engine_states"][3]["available"] is not outage
    assert result["source_provenance_is_not_authenticated_host_receipt"] is True
    assert result["old_certificate_admitted"] is result["mission_authority_granted"] is result["dispatch_authorized"] is False
    schedule = contract.RestoredCandidateSchedule(origin).record()
    assert schedule["nominal_s"] == 3600.
    assert schedule["next_orbit_s"] == 3600.+result["orbit"]["period_s"]
    assert schedule["maximum_simulation_time_s"] == 16000.
    assert record == before and json.loads(profile)["integration"]["max_duration_s"] != 16000.
    result["state"]["propellant_kg"] = -1
    source.clear()
    record["inventory"]["generation"] = 999
    assert origin.record()["state"]["propellant_kg"] > 0.
    assert origin.record()["inventory_generation"] == 26-retained
    with pytest.raises(FrozenInstanceError):
        origin.source_record_sha256 = "0"*64


def test_raw_bytes_and_host_source_bindings_are_required_not_self_asserted_flags():
    record,profile,source = fixture_record()
    raw = encode(record)
    for digest,data,pr,expected in (("0"*64,raw,profile,source),
            (sha256(raw).hexdigest(),raw,profile+b" ",source),
            (sha256(raw).hexdigest(),raw,profile,{})):
        with pytest.raises(ValueError):
            contract.ActualRestoredOrigin(digest,data,pr,expected)
    altered = deepcopy(source)
    altered["src/runtime/starship_sixdof.py"] = "2"*64
    with pytest.raises(ValueError,match="source_or_profile"):
        build(record,profile,altered)
    with pytest.raises(TypeError):
        contract.ActualRestoredOrigin(source_record_sha256=sha256(raw).hexdigest(),_origin_json=b"{}")


@pytest.mark.parametrize("mutation",[
    lambda r:r.update(schema="missionos.deployment_return_handoff.v1"),
    lambda r:r.update(phase="landing_burn"),
    lambda r:r["inventory"].update(sequencer_state="held"),
    lambda r:r["inventory"].update(pending_action={"action":"continue"}),
    lambda r:r["inventory"].update(generation=1),
    lambda r:r["inventory"].update(released_count=True),
    lambda r:r["inventory"].update(observed_time_s=0.),
    lambda r:r["final_state"]["engine_states"][3].update(available=True),
    lambda r:r["final_state"].update(time_s=1000.7),
    lambda r:r["final_state"].update(propellant_kg=82000.),
    lambda r:r["final_vehicle"].update(dry_mass_kg=120001.),
    lambda r:r["experimental_mission_ascent"]["schedule"].update(return_request_time_s=3605.),
    lambda r:r["experimental_mission_ascent"]["schedule"].update(return_request_time_s=2280.7),
    lambda r:r["experimental_mission_ascent"]["settled_measurement"]["state"].update(time_s=1804.),
    lambda r:r["experimental_mission_ascent"]["settled_measurement"]["state"].update(propellant_kg=74999.),
    lambda r:r["experimental_mission_ascent"]["settings"]["ascent"].update(policy="moving_reference_v5"),
    lambda r:r["booster_control_settings"].pop("terminal_horizontal_braking"),
])
def test_stale_reset_old_time_certificate_and_wrong_scope_reject(mutation):
    record,profile,source = fixture_record(outage=True)
    mutation(record)
    with pytest.raises(ValueError):
        build(record,profile,source)


@pytest.mark.parametrize("field",["r_eci_m","v_eci_mps","q_body_to_eci","omega_body_rad_s"])
def test_nonfinite_full_state_is_rejected(field):
    record,profile,source = fixture_record()
    record["final_state"][field][0] = float("nan")
    with pytest.raises(ValueError):
        build(record,profile,source)


@pytest.mark.parametrize("tamper",["child_velocity","parent_position","availability","missing_history","retained_mass"])
def test_release_receipts_bind_actual_conservation_geometry_and_continuation(tamper):
    record,profile,source = fixture_record(retained=13,outage=True)
    receipt = record["inventory"]["release_receipts"][0]
    if tamper == "child_velocity":
        receipt["child_state"]["v_eci_mps"][0] += 1.
    elif tamper == "parent_position":
        receipt["parent_after_state"]["r_eci_m"][0] += 1.
    elif tamper == "availability":
        receipt["parent_after_state"]["engine_states"][3]["available"] = True
    elif tamper == "missing_history":
        record["state_history"].pop(2)
    else:
        record["final_vehicle"] = native(asdict(mission.vehicle(json.loads(profile),payload_count=0)))
    with pytest.raises(ValueError):
        build(record,profile,source)


def test_lower_current_fuel_requires_provided_monotonic_actual_history():
    record,profile,source = fixture_record()
    record["final_state"]["propellant_kg"] = 74000.
    record["state_history"][-1]["state"] = deepcopy(record["final_state"])
    assert build(record,profile,source).record()["state"]["propellant_kg"] == 74000.
    record["state_history"].insert(1,{"state":{**deepcopy(record["final_state"]),"time_s":1806.,"propellant_kg":81000.},"retained_count":26})
    with pytest.raises(ValueError,match="added_fuel"):
        build(record,profile,source)


def test_balanced_extra_axial_velocity_cannot_masquerade_as_configured_impulse():
    record,profile_raw,source = fixture_record(retained=13)
    profile = json.loads(profile_raw)
    receipt = record["inventory"]["release_receipts"][0]
    before,after,child = (receipt[key] for key in ("parent_before_state","parent_after_state","child_state"))
    parent_mass = dyn.mass_properties(mission.vehicle(profile,payload_count=25),after["propellant_kg"]).mass_kg
    child_mass = profile["payload"]["mass_each_kg"]
    # Both relative COM offsets are axial. Extra equal/opposite axial momentum
    # leaves total P and orbital H unchanged, but is not the registered impulse.
    extra = dyn.rotate(tuple(before["q_body_to_eci"]),(0.,0.,.1))
    after["v_eci_mps"] = list(env.add(after["v_eci_mps"],extra))
    child["v_eci_mps"] = list(env.add(child["v_eci_mps"],env.scale(extra,-parent_mass/child_mass)))
    dp = env.add(env.scale(contract._difference(after["v_eci_mps"],before["v_eci_mps"]),parent_mass),
        env.scale(contract._difference(child["v_eci_mps"],before["v_eci_mps"]),child_mass))
    assert env.norm(dp) < .1
    for state in (after,child):
        offset = contract._difference(state["r_eci_m"],before["r_eci_m"])
        assert env.norm(env.cross(offset,extra)) < 1e-8
    with pytest.raises(ValueError,match="body_impulse_velocity_failed"):
        build(record,profile_raw,source)


def test_cutoff_boolean_cannot_admit_a_forged_suborbital_cutoff():
    record,profile,source = fixture_record()
    cutoff = record["experimental_mission_ascent"]["cutoff_measurement"]
    cutoff["state"]["v_eci_mps"][1] *= .7
    assert cutoff["gate_reached"] is True
    assert contract._orbit(cutoff["state"])["perigee_altitude_m"] < 220000.
    with pytest.raises(ValueError,match="actual_settled_insertion_gate_failed"):
        build(record,profile,source)


def test_balanced_spin_exchange_still_requires_each_body_torque_impulse():
    record,profile_raw,source = fixture_record(retained=13)
    profile = json.loads(profile_raw)
    receipt = record["inventory"]["release_receipts"][0]
    parent = receipt["parent_after_state"]
    inertia = dyn.mass_properties(mission.vehicle(profile,payload_count=25),parent["propellant_kg"]).inertia_kg_m2
    child_inertia = receipt["child_inertia_kg_m2"]
    delta_parent = (0.,0.,.000001)
    delta_h = tuple(env.dot(row,delta_parent) for row in inertia)
    delta_child = dyn._solve(child_inertia,env.scale(delta_h,-1.))
    assert env.norm(env.add(delta_h,tuple(env.dot(row,delta_child) for row in child_inertia))) < 1e-8
    parent["omega_body_rad_s"] = list(env.add(parent["omega_body_rad_s"],delta_parent))
    child = receipt["child_state"]
    child["omega_body_rad_s"] = list(env.add(child["omega_body_rad_s"],delta_child))
    with pytest.raises(ValueError,match="body_torque_impulse_failed"):
        build(record,profile_raw,source)


@pytest.mark.parametrize("field",["parent_spin_delta_body_rad_s","child_spin_delta_body_rad_s"])
def test_stored_spin_delta_must_match_individual_torque_impulse(field):
    record,profile,source = fixture_record(retained=13)
    record["inventory"]["release_receipts"][0][field][0] += .001
    with pytest.raises(ValueError,match="recorded_spin_delta_failed"):
        build(record,profile,source)


@pytest.mark.parametrize("horizon",[True,16001.,0.,float("inf"),3600.])
def test_candidate_horizon_is_explicit_bounded_scope_not_profile_mutation(horizon):
    origin = build(*fixture_record())
    with pytest.raises(ValueError):
        contract.RestoredCandidateSchedule(origin,horizon)


def test_old_origin_and_free_dictionary_cannot_be_candidate_origin():
    for value in ({"state":{}},object(),None):
        with pytest.raises(ValueError):
            contract.RestoredCandidateSchedule(value)
