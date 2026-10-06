"""Public current-state arithmetic fixtures, never a flight or terminal replay."""
from copy import deepcopy
from dataclasses import asdict,replace
import math
import time

import pytest

from test_starship_fixed_terminal_reference import fixture
from src.runtime import starship_coupled_pin_landing as landing
from src.runtime import starship_fixed_terminal_reference as reference
from src.runtime import starship_sixdof as dyn
from src.runtime import starship_physics as env
from src.runtime import starship_actual_recovery_shooting as shooting
from src.runtime.starship_booster_recovery import _tower_observation


def prepared():
    profile,catch,body,state,snapshot,prior,command=fixture()
    plan=reference.build_terminal_reference(snapshot,profile,catch,prior_state=prior,previous_command=command)
    return profile,catch,body,state,dyn.command_from_dict(command),plan


def test_pin_feedback_keeps_existing_horizontal_and_vertical_gains():
    _,catch,_,_,_,_=prepared()
    kin={"position_enu_m":[1.,2.,3.],"velocity_enu_mps":[4.,5.,6.]}
    sample={"position_enu_m":[2.,4.,6.],"velocity_enu_mps":[8.,10.,12.],"acceleration_enu_mps2":[.1,.2,.3]}
    requested,detail=landing.desired_pin_acceleration(kin,sample,catch)
    p,v=catch["terminal_position_tau_s"],catch["terminal_velocity_tau_s"]
    expected=[.1+1/p**2+8/p,.2+2/p**2+10/p,.3+3/(p*v)+6/v]
    assert requested==pytest.approx(expected)
    assert detail["feedback_gains"]["position_per_s2"]==[1/p**2,1/p**2,1/(p*v)]


def test_actual_material_inverse_recovers_current_cg_rhs_without_assigning_state(monkeypatch):
    profile,catch,body,state,command,plan=prepared()
    before=deepcopy(asdict(state))
    arrival=_tower_observation(state,body,profile,catch)
    monkeypatch.setattr(dyn,"step",lambda *a,**k:pytest.fail("guidance never integrates or assigns trajectory"))
    force,axis,detail=landing.pin_guidance_request(state,body,profile,catch,command,plan,arrival,
        carried_axis_eci=dyn.rotate(state.q_body_to_eci,(0.,0.,1.)))
    kin=detail["actual_pin_kinematics"]
    assert detail["requested_cg_acceleration_eci_mps2"]==pytest.approx(kin["current_cg_acceleration_eci_mps2"],abs=1e-9)
    loads=kin["source_model_load_inputs"]["engine_loads"]
    current_engine_force=tuple(sum(load["force_body_n"][i]for load in loads)for i in range(3))
    assert detail["unbounded_engine_force_eci_n"]==pytest.approx(dyn.rotate(state.q_body_to_eci,current_engine_force),abs=1e-3)
    assert math.hypot(*axis)==pytest.approx(1.)
    assert env.norm(force)<=13*profile["booster"]["engine_thrust_n"]*(1+1e-12)
    assert asdict(state)==before
    assert detail["actual_state_assigned"] is False
    assert detail["force_request_is_achieved"] is False
    assert detail["independent_pose_quintic_used"] is False


def test_after_fixed_clock_real_state_request_holds_original_target():
    profile,catch,body,state,command,plan=prepared()
    # A separately declared public state/time fixture, not an integrated future.
    later=replace(state,time_s=plan["terminal_time_s"]+.1)
    arrival=_tower_observation(later,body,profile,catch)
    _,_,detail=landing.pin_guidance_request(later,body,profile,catch,command,plan,arrival,
        carried_axis_eci=dyn.rotate(later.q_body_to_eci,(0.,0.,1.)))
    assert detail["mode"]=="measured_pin_target_hold"
    assert detail["reference_sample"]["position_enu_m"]==plan["target_position_enu_m"]
    assert detail["reference_sample"]["velocity_enu_mps"]==[0.,0.,0.]
    assert detail["arrival_admitted"] is detail["support_admitted"] is False


def test_stale_arrival_and_uncarried_axis_fail_before_guidance():
    profile,catch,body,state,command,plan=prepared()
    arrival=_tower_observation(state,body,profile,catch)
    with pytest.raises(ValueError,match="current_arrival"):
        landing.pin_guidance_request(state,body,profile,catch,command,plan,{**arrival,"time_s":state.time_s-1.},
            carried_axis_eci=(0.,0.,1.))
    with pytest.raises(ValueError,match="carried_reference_axis"):
        landing.pin_guidance_request(state,body,profile,catch,command,plan,arrival,carried_axis_eci=(0.,0.,2.))


def trial_inputs():
    from src.runtime.starship_reference_tracking import ReferenceTracker
    profile,catch,body,state,snapshot,prior,command=fixture()
    tracker=ReferenceTracker(profile,snapshot["context"]["prior_command_reference"]["quaternion"],prior["time_s"])
    snapshot["context"]["reference_tracker"]=shooting.saved(vars(tracker))
    plan=reference.build_terminal_reference(snapshot,profile,catch,prior_state=prior,previous_command=command)
    command=dyn.command_from_dict(command)
    hashes={"source_run_sha256","source_prior_checkpoint_sha256","source_origin_checkpoint_sha256",
        "origin_context_sha256","prior_command_sha256","translation_reference_sha256","profile_sha256",
        "catch_profile_sha256","static_17_result_sha256","static_17_reverification_sha256",
        "static_17_raw_json_sha256","static_17_source_map_sha256"}
    binding={key:"a"*64 for key in hashes}
    binding.update(schema=landing.BINDING_SCHEMA,source_origin_checkpoint_index=1,
        source_binding_is_caller_assertion=True,source_authentication_independently_verified=False,
        reference_is_execution=False,full_launch_reexecuted=False)
    for key,value in (("origin_context_sha256",snapshot),("prior_command_sha256",asdict(command)),
            ("translation_reference_sha256",plan),("profile_sha256",profile),("catch_profile_sha256",catch)):
        binding[key]=shooting.digest(value)
    request={"schema":landing.REQUEST_SCHEMA,"origin_context_sha256":shooting.digest(snapshot),"duration_s":.1,
        "maximum_integration_steps":1,"wall_deadline_monotonic_s":time.monotonic()+120.,
        "automatic_retry":False,"hardware_execution":False,"catch_execution":False}
    return snapshot,plan,profile,catch,command,binding,request


def test_trial_rejects_arbitrary_evidence_or_control_callback_before_plant(monkeypatch):
    snapshot,plan,profile,catch,command,binding,request=trial_inputs()
    monkeypatch.setattr(dyn,"observe",lambda *a,**k:pytest.fail("invalid callback before current model use"))
    with pytest.raises(ValueError,match="fixed_request_and_evidence_sink"):
        landing.simulate_coupled_pin_trial(snapshot,plan,profile,catch,command,request=request,
            artifact_sink=lambda *a:None,source_bindings=binding)


@pytest.mark.parametrize("change",["digest","extra","hardware_claim"])
def test_closed_input_binding_refuses_drift_or_authority_expansion(change):
    snapshot,plan,profile,catch,command,binding,_=trial_inputs()
    if change=="digest":
        binding["prior_command_sha256"]="b"*64
    elif change=="extra":
        binding["untrusted_callback"]="not an allowed field"
    else:
        binding["source_authentication_independently_verified"]=True
    with pytest.raises(ValueError):
        landing.validate_input_binding(binding,snapshot,plan,profile,catch,command)


def test_failed_public_step_preserves_carried_context_raw_request_and_last_actual_command(tmp_path,monkeypatch):
    from src.runtime.starship_static_artifacts import BoundedStaticArtifactSink
    snapshot,plan,profile,catch,command,binding,request=trial_inputs()
    monkeypatch.setattr(BoundedStaticArtifactSink,"_free",lambda self:1024*1024*1024)
    def refuse_step(*args,**kwargs):
        raise ArithmeticError("public fixture forbids plant integration")
    monkeypatch.setattr(dyn,"step",refuse_step)
    sink=BoundedStaticArtifactSink(tmp_path/"corpus")
    try:
        run=landing.simulate_coupled_pin_trial(snapshot,plan,profile,catch,command,request=request,
            artifact_sink=sink,source_bindings=binding)
    finally:
        sink.close()
    record=run["recovery_record"]
    assert run["outcome"]["integration_steps"]==0
    assert record["failure"]["error_class"]=="ArithmeticError"
    assert run["initial_state"]==run["final_state"]==snapshot["state"]
    assert record["initial_controller_memory"]["reference_tracker"]==snapshot["context"]["reference_tracker"]
    assert record["final_controller_memory"]["previous_actual_command"]==shooting.saved(asdict(command))
    assert record["final_controller_memory"]["previous_actual_command_time_s"]==plan["prior_state"]["time_s"]
    assert record["checkpoints"][0]["step_receipt_artifact"]["persisted_before_analysis"] is True
    assert record["checkpoints"][0]["command"] is None
    assert run["outcome"]["actual_isolated_simulator_continuation"] is False
    assert run["outcome"]["mission_completed"] is False


def test_oblique_base_selector_does_not_amplify_actual_axis_projection():
    from src.runtime.starship_constrained_recovery import landing_actuation_demand
    _,_,body,state,_,_=prepared()
    axis=dyn.rotate(state.q_body_to_eci,(.8,0.,.6))
    force=env.scale(axis,3e6)
    count,throttle,pair,detail=landing.base_actuation_request(state,body,force,axis)
    assert detail["least_squares_actual_axis_projection_n"]==pytest.approx(1.8e6)
    assert detail["legacy_selector_input_n"]==pytest.approx(1.08e6)
    expected_count,expected_throttle,expected_pair,_=landing_actuation_demand(state,body,axis,1.08e6)
    assert (count,pair)==(expected_count,expected_pair)
    assert throttle==pytest.approx(expected_throttle,abs=4*math.ulp(1.))
    assert detail["base_scalar_force_projection_is_achieved"] is False


def test_nonpositive_base_alignment_keeps_existing_three_engine_slew():
    _,_,body,state,_,_=prepared()
    axis=dyn.rotate(state.q_body_to_eci,(0.,0.,-1.))
    count,throttle,pair,detail=landing.base_actuation_request(state,body,env.scale(axis,3e6),axis)
    assert (count,throttle,pair)==(3,.4,None)
    assert detail["least_squares_actual_axis_projection_n"]==0.
    assert detail["landing_alignment_slew"]["translational_thrust_admitted"] is False


def test_final_memory_error_keeps_original_integration_failure_chain(tmp_path,monkeypatch):
    from src.runtime.starship_static_artifacts import BoundedStaticArtifactSink
    snapshot,plan,profile,catch,command,binding,request=trial_inputs()
    monkeypatch.setattr(BoundedStaticArtifactSink,"_free",lambda self:1024*1024*1024)
    real_memory=landing._controller_memory
    count={"value":0}
    def memory(*args):
        count["value"]+=1
        if count["value"]==3:
            raise ValueError("public final-memory fixture failure")
        return real_memory(*args)
    def fail_step(*args,**kwargs):
        raise ArithmeticError("public fixture forbids integration")
    monkeypatch.setattr(landing,"_controller_memory",memory)
    monkeypatch.setattr(dyn,"step",fail_step)
    sink=BoundedStaticArtifactSink(tmp_path/"corpus")
    try:
        run=landing.simulate_coupled_pin_trial(snapshot,plan,profile,catch,command,request=request,
            artifact_sink=sink,source_bindings=binding)
    finally:
        sink.close()
    record=run["recovery_record"]
    assert record["failure"]["error_class"]=="ArithmeticError"
    assert [item["error_class"]for item in record["failure_chain"]]==["ArithmeticError","ValueError"]
    assert record["final_controller_memory"] is None
    assert run["outcome"]["integration_steps"]==0


def final_endpoint_stub_run(tmp_path,monkeypatch,*,eligible=True,initial_eligible=False,
        failure=False,contact=False,fuel_empty=False,rate_exceeded=False,pending=False,footer_failure=None):
    """Emitter lifecycle only: every model/observer/integrator is a stub."""
    from src.runtime import starship_booster_control as control
    from src.runtime import starship_booster_recovery as recovery
    from src.runtime import starship_sixdof_booster as booster_module
    from src.runtime import starship_sixdof_contact as contact_module
    from src.runtime import starship_terminal_wrench as wrench_module
    from src.runtime.starship_reference_tracking import ReferenceTracker
    from src.runtime.starship_static_artifacts import BoundedStaticArtifactSink
    profile,catch,body,state,snapshot,prior,command_dict=fixture()
    if fuel_empty or rate_exceeded:
        state=replace(state,propellant_kg=0. if fuel_empty else state.propellant_kg,
            omega_body_rad_s=(6.,0.,0.) if rate_exceeded else state.omega_body_rad_s)
        snapshot["state"]=shooting.saved(asdict(state))
    tracker=ReferenceTracker(profile,snapshot["context"]["prior_command_reference"]["quaternion"],prior["time_s"])
    snapshot["context"]["reference_tracker"]=shooting.saved(vars(tracker))
    plan={"origin_context_sha256":shooting.digest(snapshot),"previous_command":command_dict,"prior_state":prior}
    command=dyn.command_from_dict(command_dict)
    hash_fields={"source_run_sha256","source_prior_checkpoint_sha256","source_origin_checkpoint_sha256",
        "origin_context_sha256","prior_command_sha256","translation_reference_sha256","profile_sha256",
        "catch_profile_sha256","static_17_result_sha256","static_17_reverification_sha256",
        "static_17_raw_json_sha256","static_17_source_map_sha256"}
    binding={key:"a"*64 for key in hash_fields}
    binding.update(schema=landing.BINDING_SCHEMA,source_origin_checkpoint_index=1,
        source_binding_is_caller_assertion=True,source_authentication_independently_verified=False,
        reference_is_execution=False,full_launch_reexecuted=False)
    for key,value in (("origin_context_sha256",snapshot),("prior_command_sha256",asdict(command)),
            ("translation_reference_sha256",plan),("profile_sha256",profile),("catch_profile_sha256",catch)):
        binding[key]=shooting.digest(value)
    request={"schema":landing.REQUEST_SCHEMA,"origin_context_sha256":shooting.digest(snapshot),
        "duration_s":.1,"maximum_integration_steps":1,"wall_deadline_monotonic_s":120.,
        "automatic_retry":False,"hardware_execution":False,"catch_execution":False}
    counts={"observation":0,"advance_stub":0,"clock":0,"memory":0}
    def clock():
        counts["clock"]+=1
        return 121. if pending and counts["clock"]>=3 else 10.
    monkeypatch.setattr(landing.time,"monotonic",clock)
    monkeypatch.setattr(BoundedStaticArtifactSink,"_free",lambda self:1024*1024*1024)
    def observation(s,*args):
        counts["observation"]+=1
        if footer_failure=="observation" and counts["observation"]==2:
            raise ValueError("public stub final observation refusal")
        return {"time_s":s.time_s,"eligible":initial_eligible if counts["observation"]==1 else eligible,
            "limits":{"public_stub":True},"com_rate_body_mps":[0.,0.,0.]}
    monkeypatch.setattr(recovery,"_tower_observation",observation)
    real_memory=landing._controller_memory
    def memory(*args):
        counts["memory"]+=1
        if footer_failure=="memory" and counts["memory"]==2:
            raise ValueError("public stub final memory refusal")
        return real_memory(*args)
    monkeypatch.setattr(landing,"_controller_memory",memory)
    monkeypatch.setattr(dyn,"observe",lambda *a,**k:{"altitude_m":0. if contact else 2000.,
        "com_rate_body_mps":[0.,0.,0.],"dynamic_pressure_pa":0.})
    monkeypatch.setattr(dyn,"_rhs",lambda *a,**k:pytest.fail("no model RHS in emitter fixture"))
    def advance(s,*args):
        counts["advance_stub"]+=1
        if failure:
            raise ArithmeticError("public stub integration refusal")
        # This is a declared fixture endpoint, never a physical propagation.
        return replace(s,time_s=s.time_s+.1)
    monkeypatch.setattr(dyn,"step",advance)
    monkeypatch.setattr(contact_module,"find_contact",lambda s,*args:(advance(s),{"contact":True,"public_stub":True}))
    monkeypatch.setattr(contact_module,"hull_clearance",lambda *a:{"signed_clearance_m":100.})
    monkeypatch.setattr(booster_module,"_sample_booster",lambda s,*a:{"time_s":s.time_s})
    axis=dyn.rotate(state.q_body_to_eci,(0.,0.,1.))
    monkeypatch.setattr(landing,"pin_guidance_request",lambda *a,**k:((1.,0.,0.),axis,{"minimum_vertical_thrust_n":0.}))
    monkeypatch.setattr(landing,"base_actuation_request",lambda *a:(3,.4,None,{}))
    monkeypatch.setattr(control,"control_with_measured_tvc",lambda s,v,q,*a,**k:
        (command,{"requested_torque_body_nm":[0.,0.,0.],"target_q_body_to_eci":list(q)}))
    monkeypatch.setattr(wrench_module,"allocate_terminal_wrench",lambda *a,**k:
        (command,{"status":"deferred","reason":"public_stub","mask_candidates":[],"selected_mask_index":None}))
    sink=BoundedStaticArtifactSink(tmp_path/"emitter-corpus")
    try:
        run=landing.simulate_coupled_pin_trial(snapshot,plan,profile,catch,command,request=request,
            artifact_sink=sink,source_bindings=binding)
    finally:
        sink.close()
    return run,counts


def test_final_completed_stub_endpoint_promotes_exact_state_without_extra_step(tmp_path,monkeypatch):
    run,counts=final_endpoint_stub_run(tmp_path,monkeypatch)
    record=run["recovery_record"]
    assert counts["advance_stub"]==1
    assert counts["observation"]==2
    assert run["outcome"]["integration_steps"]==1
    assert run["outcome"]["termination"]=="catch_handoff"
    assert record["handoff"]["eligible"] is True
    assert record["handoff"]["state"]==run["final_state"]==record["checkpoints"][-1]["state"]
    assert record["checkpoints"][-1]["command"] is record["checkpoints"][-1]["step_receipt_artifact"] is None
    assert record["final_controller_memory"]["previous_actual_command_time_s"]==run["initial_state"]["time_s"]
    assert run["outcome"]["catch_executed"] is run["outcome"]["mission_completed"] is False


@pytest.mark.parametrize("reason",["failure","contact","fuel_empty","rate_exceeded","pending","not_eligible"])
def test_final_stub_eligibility_never_promotes_excluded_or_pending_result(reason,tmp_path,monkeypatch):
    args={reason:True} if reason!="not_eligible" else {"eligible":False}
    run,_=final_endpoint_stub_run(tmp_path,monkeypatch,**args)
    assert run["recovery_record"]["handoff"]["eligible"] is False
    assert run["outcome"]["handoff_reached"] is False
    assert run["recovery_record"]["handoff"]["state"] is None


def test_initial_eligible_stub_remains_observation_only_without_forced_step(tmp_path,monkeypatch):
    run,counts=final_endpoint_stub_run(tmp_path,monkeypatch,initial_eligible=True)
    assert counts["advance_stub"]==0
    assert run["outcome"]["integration_steps"]==0
    assert run["recovery_record"]["handoff"]["eligible"] is True
    assert run["outcome"]["actual_isolated_simulator_continuation"] is False
    assert run["outcome"]["full_launch_reexecuted"] is False


@pytest.mark.parametrize("footer_failure",["memory","observation"])
def test_initial_eligible_stub_footer_failure_revokes_success_reason(footer_failure,tmp_path,monkeypatch):
    run,counts=final_endpoint_stub_run(tmp_path,monkeypatch,initial_eligible=True,footer_failure=footer_failure)
    record=run["recovery_record"]
    assert counts["advance_stub"]==run["outcome"]["integration_steps"]==0
    assert run["initial_state"]==run["final_state"]==record["checkpoints"][-1]["state"]
    assert run["outcome"]["termination"]=="coupled_pin_controller_or_record_exception"
    assert record["handoff"]["eligible"] is run["outcome"]["handoff_reached"] is False
    assert record["handoff"]["state"] is None
    assert record["failure"]["stage"]==("final_controller_memory" if footer_failure=="memory" else "final_observation")
    assert record["failure_chain"]==[record["failure"]]
    assert record["initial_controller_memory"]["previous_actual_command"]==record["translation_reference"]["previous_command"]
    if footer_failure=="memory":
        assert record["final_controller_memory"] is None
    else:
        assert record["final_controller_memory"]==record["initial_controller_memory"]
