"""Water-entry objective authority and independent evidence checks."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from src.runtime.starship_splashdown import SplashdownGoal, load_goal
from src.runtime.starship_mission_director import MissionDirector, contract, check_action
from src.intelligence.starship_mission_director import fixture_decision
from src.runtime import starship_mission_control as control
from src.intelligence.starship_mission_planner import plan_starship_request
from tests.contract.test_starship_mission_director import row


@pytest.mark.parametrize("key,value", [("maximum_downward_speed_mps",3.), ("minimum_propellant_kg",9999.),
                                      ("water_response_modeled",True), ("unknown_field",1)])
def test_goal_cannot_relax_admitted_limits_or_claim_water_physics(key,value):
    with pytest.raises(ValueError):
        SplashdownGoal.from_dict({**load_goal().to_dict(),key:value})


def test_splashdown_scope_is_separate_immutable_approval():
    goal=load_goal()
    with pytest.raises(FrozenInstanceError):
        goal.area_radius_m=20000.
    legacy=contract("fixture")
    water=contract("fixture",splashdown=True)
    assert "splashdown" not in legacy["decision_points"]["booster_selection"]
    assert water["schema"]=="missionos.starship_mission_envelope.v6"
    assert water["splashdown_goal"]==goal.to_dict()
    actor=MissionDirector(water,fixture_decider=fixture_decision)
    assert actor.update("booster_selection",row(phase="booster_return"))=="splashdown"


def test_splashdown_trial_cannot_dispatch_unconnected_capture():
    observed=row(phase="booster_return")
    observed["numerical_tools"]["capture_corridor_certified"]=True
    assert check_action(contract("fixture",splashdown=True),"booster_selection","capture",observed,
                        elapsed_s=0,observation_requests=0,hold_used_s=0)=="capture_controller_not_connected"


def test_false_water_contact_claim_is_rejected():
    from src.runtime.starship_splashdown_verifier import verify_splashdown
    root=Path(__file__).resolve().parents[2]
    site=json.loads((root/"examples/spaceflight/starship-return-sites-model-test.json").read_text())["sites"][1]
    goal=load_goal().to_dict()
    booster={"active_return_site":site,"contact":None,"splashdown":{"goal":deepcopy(goal),
        "contact_observed":False,"controlled_water_entry_envelope_met":True,
        "water_response_verified":False,"physical_execution":False}}
    result=verify_splashdown(booster,goal)
    assert result["passed"] is False
    assert result["issues"]==[{"code":"splashdown_outcome_mismatch"}]


def test_integrated_numpy_scalars_produce_json_booleans():
    import numpy as np
    from src.runtime.starship_splashdown import entry_result
    booster={"contact":{"surface_normal_speed_mps":np.float64(-1.),"surface_tangential_speed_mps":np.float64(.5),
        "surface_relative_speed_mps":np.float64(1.2),"initial_overlap":False},
        "outcome":{"return_site_distance_m":np.float64(100.),"final_tilt_deg":np.float64(1.),"final_body_rate_rad_s":np.float64(.01)},
        "final_state":{"propellant_kg":np.float64(11000.)}}
    result=entry_result(booster,load_goal())
    assert all(type(value) is bool for value in result["checks"].values())
    assert json.loads(json.dumps(result))["controlled_water_entry_envelope_met"] is True


@pytest.mark.parametrize("scenario,water", [("sixdof_managed_normal",True),("sixdof_managed_splashdown",False)])
def test_scenario_cannot_self_select_another_envelope(tmp_path,monkeypatch,scenario,water):
    from src.runtime import starship_return_feasibility as qualification
    monkeypatch.setattr(qualification, "readiness", lambda *a, **k: ({}, "fixture", None))
    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE","fixture")
    service=control.StarshipMissionService(tmp_path,planner=lambda text:plan_starship_request(text,"fixture"))
    service.plan("swap","Starship "+scenario)
    state=service._load("swap")
    p=state["plan"]
    p["mission_envelope"]=contract("fixture",splashdown=water)
    p["sha256"]=control._digest({k:v for k,v in p.items() if k!="sha256"})
    service._save("swap",state)
    ref=("swap",p["id"],p["sha256"])
    service.approve(*ref)
    with pytest.raises(control.StarshipMissionError,match="mission_envelope_not_current"):
        service.execute(*ref)
    assert service._load("swap")["execution"]=={}


def contact_fixture():
    import math
    from src.runtime.starship_sixdof_mission import _attitude
    root=Path(__file__).resolve().parents[2]
    site=json.loads((root/"examples/spaceflight/starship-return-sites-model-test.json").read_text())["sites"][1]
    lat,lon=math.radians(site["latitude_deg"]),math.radians(site["longitude_deg"])
    a=6378137.
    e2=1-(1-1/298.257223563)**2
    n=a/math.sqrt(1-e2*math.sin(lat)**2)
    p=[n*math.cos(lat)*math.cos(lon),n*math.cos(lat)*math.sin(lon),n*(1-e2)*math.sin(lat)]
    up=[math.cos(lat)*math.cos(lon),math.cos(lat)*math.sin(lon),math.sin(lat)]
    velocity=[-7.292115e-5*p[1]-up[0],7.292115e-5*p[0]-up[1],-up[2]]
    checks=dict.fromkeys(("area","speed","vertical","horizontal","tilt","rate","reserve","no_overlap"),True)
    return {"active_return_site":site,"contact":{"point_eci_m":p,"point_velocity_eci_mps":velocity,"initial_overlap":False},
        "final_state":{"r_eci_m":p,"q_body_to_eci":_attitude(up,[-math.sin(lon),math.cos(lon),0]),
            "omega_body_rad_s":[0,0,0],"time_s":0.,"propellant_kg":11000.},
        "splashdown":{"goal":load_goal().to_dict(),"contact_observed":True,"checks":checks,
            "controlled_water_entry_envelope_met":True,"water_response_verified":False,"physical_execution":False}}


def test_actual_contact_reserve_claim_is_recomputed():
    from src.runtime.starship_splashdown_verifier import verify_splashdown
    b=contact_fixture()
    assert verify_splashdown(b,load_goal().to_dict())["passed"]
    b["final_state"]["propellant_kg"]=9900.
    result=verify_splashdown(b,load_goal().to_dict())
    assert result["issues"]==[{"code":"splashdown_check_mismatch"}]


def test_reference_hash_mismatch_cannot_claim_water_entry():
    from src.runtime.starship_splashdown_verifier import verify_splashdown
    b=contact_fixture()
    b["active_return_site"]["latitude_deg"]+=.1
    assert verify_splashdown(b,load_goal().to_dict())["issues"]==[{"code":"splashdown_reference_binding"}]
