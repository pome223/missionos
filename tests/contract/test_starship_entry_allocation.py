"""Ship-surface bounds, finite physical response and opt-in comparison scope."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import math
from pathlib import Path

import numpy as np
import pytest

from scripts import run_starship_entry_allocation_comparison as experiment
from src.runtime import starship_fin_allocation as allocation
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_sixdof_mission import _attitude, _sample, control, vehicle
from src.runtime.starship_sixdof_mission import simulate
from src.runtime.starship_sixdof_verifier import verify_study, _return_allocation_scope, _Invalid
from scripts import run_starship_return_qualification as full_experiment

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def configured():
    profile = json.loads((ROOT / "examples/spaceflight/starship-sixdof-profile.json").read_text())
    body = vehicle(profile, payload_count=25)
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
                              80000., 100000., time_s=4000.)
    up, east, north = env.local_frame(point)
    axis = env.add(env.scale(east,math.cos(math.radians(70.))), env.scale(up,math.sin(math.radians(70.))))
    state = dyn.State6DOF(4000.,point.r,env.add(point.v,env.scale(east,7000.)),
        _attitude(axis,env.scale(north,-1)),(0.,0.,0.),100000.,
        tuple(dyn.EngineState() for _ in body.engines),tuple(0. for _ in body.aero_panels))
    return profile,body,state


def test_ship_allocator_moves_only_real_surfaces_with_finite_bounds(configured):
    profile,body,state=configured
    before=asdict(state)
    ordinary,_=control(state,body,state.q_body_to_eci,0.,0,profile,use_flaps=True)
    candidate,diagnostic=control(state,body,state.q_body_to_eci,0.,0,profile,use_flaps=True,
        development_fin_allocation=True,development_fin_policy=allocation.MOMENT_PRIORITY_POLICY_ID)
    receipt=diagnostic["development_fin_allocation"]
    assert receipt["surface_family"]=="ship_flap"
    assert asdict(state)==before
    assert all(not e.enabled for e in candidate.engines[:profile["ship"]["engine_count"]])
    assert [(e.max_thrust_n,e.max_gimbal_rad) for e in body.engines]==[
        (e.max_thrust_n,e.max_gimbal_rad) for e in vehicle(profile,payload_count=25).engines]
    assert candidate.flap_angles_rad[:3]==ordinary.flap_angles_rad[:3]
    for i,target,predicted in zip(receipt["fin_indices"],receipt["command_angles_rad"],receipt["predicted_endpoint_angles_rad"]):
        panel=body.aero_panels[i]
        assert abs(target)<=panel.max_deflection_rad
        assert abs(predicted-state.flap_angles_rad[i])<=.1*panel.deflection_rate_rad_s+1e-9
    actual=dyn.step(state,body,candidate,.1)
    assert actual.flap_angles_rad[3:]==pytest.approx(receipt["predicted_endpoint_angles_rad"],abs=1e-5)
    assert actual.r_eci_m!=state.r_eci_m
    assert actual.propellant_kg<=state.propellant_kg


def test_ship_box_solution_has_independent_kkt_optimality(configured):
    profile,body,state=configured
    target=dyn.quaternion_multiply(state.q_body_to_eci,dyn.axis_angle((1.,0.,0.),.2))
    _,diagnostic=control(state,body,target,0.,0,profile,use_flaps=True,
        development_fin_allocation=True,development_fin_policy=allocation.MOMENT_PRIORITY_POLICY_ID)
    receipt=diagnostic["development_fin_allocation"]
    matrix=np.array(receipt["effectiveness_nm_per_rad"])/np.array(receipt["moment_scale_nm"])[:,None]
    demand=np.array(receipt["requested_increment_torque_body_nm"])/np.array(receipt["moment_scale_nm"])
    delta=np.array(receipt["predicted_endpoint_angles_rad"])-np.array(receipt["actual_angles_rad"])
    gradient=matrix.T@(matrix@delta-demand)
    for i,value in enumerate(gradient):
        if abs(delta[i]-receipt["reachable_delta_lower_rad"][i])<1e-8:
            assert value>=-1e-8
        elif abs(delta[i]-receipt["reachable_delta_upper_rad"][i])<1e-8:
            assert value<=1e-8
        else:
            assert abs(value)<1e-8


def test_ambiguous_mixed_surface_vehicle_cannot_silently_use_allocator(configured):
    profile,body,state=configured
    panels=list(body.aero_panels)
    panels[3]=replace(panels[3],name="grid_fin_0")
    mixed=replace(body,aero_panels=tuple(panels))
    with pytest.raises(ValueError,match="one_surface_family"):
        allocation.allocate_fins(state,mixed,[0.,0.,0.],dyn.observe(state,mixed),profile,interval_s=.1)


def test_replay_inputs_reject_profile_and_reference_substitution(configured):
    profile,body,state=configured
    seed=_sample(state,body,"ballistic_return")
    seed["dynamic_pressure_pa"]=30.  # Input-validation fixture, not a flight result.
    seed["controller"]={"attitude_reference":{"mode":"legacy_geographic"},
                        "target_q_body_to_eci":list(state.q_body_to_eci)}
    later=deepcopy(seed)
    later["dynamic_pressure_pa"]=60.
    run={"retained_return":{"policy_id":"mass_state_terminal_v3"},"satellites":[{}],"samples":[seed,later]}
    restored,_,_=experiment.inputs(run,profile)
    assert asdict(restored)==asdict(state)
    changed=deepcopy(profile)
    changed["ship"]["dry_mass_kg"]+=1000.
    with pytest.raises(ValueError,match="properties_mismatch"):
        experiment.inputs(run,changed)
    run["samples"][0]["controller"]["attitude_reference"]["mode"]="transport_ill_conditioned"
    with pytest.raises(ValueError,match="not_restorable"):
        experiment.inputs(run,profile)


def test_comparison_requires_opt_in_and_does_not_overwrite_failures(tmp_path):
    arguments=["--run-record",str(tmp_path/"missing.gz"),"--output-dir",str(tmp_path)]
    with pytest.raises(SystemExit):
        experiment.main(arguments)
    (tmp_path/"failure.json").write_text("preserve this failed run")
    with pytest.raises(SystemExit):
        experiment.main(["--approve-simulation",*arguments])
    assert (tmp_path/"failure.json").read_text()=="preserve this failed run"


def test_full_flight_experiment_cannot_pass_as_production_approval(configured):
    profile,_,_=configured
    scope={"release_limit":1,"bounded_ship_flaps":True,"application":"retained_policy_active_only_v1"}
    run=simulate(profile,duration_s=.2,return_policy="mass_state_terminal_v3",
                 _development_return_qualification=scope)
    study=json.loads(json.dumps({"schema":"missionos.starship_sixdof_study.v1","profile":profile,
        "runs":[run],"provenance":{"physical_execution_invoked":False,"starship_vehicle_validated":False}}))
    default=verify_study(study,expected_scenario="launch")
    assert default["passed"] is False and default["issues"][0]["code"]=="development_scope"
    explicit=verify_study(study,expected_scenario="launch",expected_development_return_qualification=scope)
    assert explicit["passed"] is True,explicit
    changed=deepcopy(scope)
    changed["release_limit"]=0
    assert verify_study(study,expected_development_return_qualification=changed)["passed"] is False
    run.pop("development_return_qualification")
    study["runs"]=[run]
    assert verify_study(study,expected_development_return_qualification=scope)["passed"] is False


@pytest.mark.parametrize("scope",[{"release_limit":True,"bounded_ship_flaps":True},
    {"release_limit":27,"bounded_ship_flaps":True},{"release_limit":1,"bounded_ship_flaps":"yes"}])
def test_bad_return_qualification_scope_is_rejected(configured,scope):
    profile,_,_=configured
    with pytest.raises(ValueError,match="invalid_development_return_qualification"):
        simulate(profile,duration_s=.2,return_policy="mass_state_terminal_v3",_development_return_qualification=scope)


def test_full_comparison_requires_opt_in_and_preserves_prior_failure(tmp_path):
    arguments=["--retained","25","--output-dir",str(tmp_path)]
    with pytest.raises(SystemExit):
        full_experiment.main(arguments)
    (tmp_path/"failure.json").write_text("preserve previous full-flight failure")
    with pytest.raises(SystemExit):
        full_experiment.main(["--approve-simulation",*arguments])
    assert (tmp_path/"failure.json").read_text()=="preserve previous full-flight failure"


def test_declared_candidate_requires_recorded_use_in_each_eligible_cycle(configured):
    profile,body,state=configured
    command,diagnostic=control(state,body,state.q_body_to_eci,0.,0,profile,use_flaps=True,
        development_fin_allocation=True,development_fin_policy=allocation.MOMENT_PRIORITY_POLICY_ID)
    sample=json.loads(json.dumps(_sample(state,body,"ballistic_return",command,diagnostic)))
    scope={"release_limit":1,"bounded_ship_flaps":True,"application":"retained_policy_active_only_v1"}
    run={"retained_return":{"activation":{"time_s":state.time_s}},"samples":[sample]}
    _return_allocation_scope(run,scope,"test")
    erased=deepcopy(run)
    erased["samples"][0]["controller"].pop("development_fin_allocation")
    with pytest.raises(_Invalid,match="allocation"):
        _return_allocation_scope(erased,scope,"test")
    mismatched=deepcopy(run)
    mismatched["samples"][0]["command"]["flap_angles_rad"][3]+=.1
    with pytest.raises(_Invalid):
        _return_allocation_scope(mismatched,scope,"test")
    erased_commands=deepcopy(run)
    erased_commands["samples"][0]["command"]=None
    erased_commands["samples"][0]["controller"].pop("development_fin_allocation")
    with pytest.raises(_Invalid):
        _return_allocation_scope(erased_commands,scope,"test")
