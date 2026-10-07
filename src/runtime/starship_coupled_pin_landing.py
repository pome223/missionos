"""Opt-in fixed-pin feedback requests, never an assigned vehicle trajectory.

The current simulator state supplies material-pin kinematics, including real
propellant-flow centroid derivatives. A fixed position polynomial is followed
until its declared end; afterwards the same pin target is held while attitude
and horizontal motion settle. Ordinary finite actuators must execute requests.
"""
from __future__ import annotations

from dataclasses import asdict,replace
from copy import deepcopy
import math
from numbers import Real
import time

from . import starship_actual_recovery_shooting as shooting
from . import starship_physics as env
from . import starship_sixdof as dyn
from .starship_fixed_terminal_reference import initial_pin_kinematics, evaluate_terminal_reference, _tower_frame

POLICY_ID = "development_coupled_pin_landing_v1"
SCHEMA = "missionos.starship_coupled_pin_landing_trial.v1"
GUIDANCE_SCHEMA = "missionos.starship_coupled_pin_guidance.v1"
REQUEST_SCHEMA = "missionos.starship_coupled_pin_trial_request.v1"
BINDING_SCHEMA = "missionos.starship_coupled_pin_input_binding.v1"
CONFIG = {"maximum_simulated_duration_s": 60., "maximum_integration_steps": 600,
    "maximum_wall_s": 120., "maximum_macrostep_s": .1, "maximum_main_engines": 13,
    "feedback_gain_source": "existing_material_pin_horizontal_pd_and_vertical_cascade",
    "force_direction_source": "causal_current_pin_feedback_and_current_finite_model_loads",
    "post_reference_mode": "original_pin_target_hold_then_measured_corridor_descent",
    "reference_pose_is_assigned_state": False, "physics_coefficients_changed": False,
    "gains_or_actuator_or_catch_limits_changed": False, "hardware_execution": False}


def _number(value):
    if isinstance(value,bool) or not isinstance(value,Real) or not math.isfinite(value):
        raise ValueError("invalid_coupled_pin_number")
    return float(value)


def _vector(value):
    if not isinstance(value,(tuple,list)) or len(value)!=3:
        raise ValueError("invalid_coupled_pin_vector")
    return tuple(_number(x) for x in value)


def feedback_gains(catch):
    position = _number(catch["terminal_position_tau_s"])
    velocity = _number(catch["terminal_velocity_tau_s"])
    if min(position,velocity)<=0:
        raise ValueError("invalid_coupled_pin_time_constants")
    return {"position_per_s2":[1/position**2,1/position**2,1/(position*velocity)],
        "velocity_per_s":[2/position,2/position,1/velocity]}


def desired_pin_acceleration(kinematics,reference_sample,catch):
    position,velocity = (_vector(kinematics[key]) for key in ("position_enu_m","velocity_enu_mps"))
    goal_position,goal_velocity,feedforward = (_vector(reference_sample[key]) for key in
        ("position_enu_m","velocity_enu_mps","acceleration_enu_mps2"))
    gains = feedback_gains(catch)
    error_p = [goal_position[i]-position[i] for i in range(3)]
    error_v = [goal_velocity[i]-velocity[i] for i in range(3)]
    requested = [feedforward[i]+gains["position_per_s2"][i]*error_p[i]
        +gains["velocity_per_s"][i]*error_v[i] for i in range(3)]
    return requested,{"feedback_gains":gains,"position_error_enu_m":error_p,
        "velocity_error_enu_mps":error_v,"pin_acceleration_request_enu_mps2":requested}


def pin_guidance_request(state,booster,profile,catch,previous_command,translation_reference,arrival,*,carried_axis_eci):
    """Current-load force/axis request, with no prediction of body realization."""
    from .starship_constrained_recovery import nonvertical_corridor_ready
    dyn._validate_pair(state,booster,previous_command)
    if type(arrival) is not dict or arrival.get("time_s")!=state.time_s:
        raise ValueError("coupled_pin_requires_current_arrival_observation")
    carried_axis = _vector(carried_axis_eci)
    if abs(env.norm(carried_axis)-1.)>1e-8:
        raise ValueError("coupled_pin_requires_carried_reference_axis")
    sample = evaluate_terminal_reference(translation_reference,state.time_s)
    following = state.time_s<=translation_reference["terminal_time_s"]
    corridor = nonvertical_corridor_ready(arrival)
    height_ready = all(catch["initial_pin_clearance_m"]<=h<=catch["initial_pin_clearance_m"]+catch["arm_half_width_m"]
        for h in arrival["pin_height_above_support_m"])
    mode = "fixed_pin_polynomial_tracking"
    if not following:
        mode = "measured_pin_target_hold"
        sample = {"time_s":state.time_s,"position_enu_m":list(translation_reference["target_position_enu_m"]),
            "velocity_enu_mps":[0.,0.,0.],"acceleration_enu_mps2":[0.,0.,0.]}
        if corridor and height_ready:
            mode = "measured_settled_terminal_descent"
            sample["velocity_enu_mps"][2]=1.5*catch["initial_vertical_speed_mps"]
    kinematics = initial_pin_kinematics(state,booster,profile,catch,previous_command)
    pin_acceleration,feedback = desired_pin_acceleration(kinematics,sample,catch)
    site,axes = _tower_frame(profile,state.time_s)
    earth=(0.,0.,env.EARTH_ROTATION_RAD_S)
    support_mid=tuple(sum(p[i] for p in catch["support_points_body_m"])/2 for i in range(3))
    lever=env.add(support_mid,env.scale(kinematics["com_body_m"],-1.))
    lever_dot=env.scale(kinematics["com_rate_body_mps"],-1.)
    lever_ddot=env.scale(kinematics["com_acceleration_body_mps2"],-1.)
    pin_position=env.add(state.r_eci_m,dyn.rotate(state.q_body_to_eci,lever))
    pin_velocity=env.add(state.v_eci_mps,dyn.rotate(state.q_body_to_eci,
        env.add(env.cross(state.omega_body_rad_s,lever),lever_dot)))
    requested_pin_eci=tuple(sum(pin_acceleration[j]*axes[j][i] for j in range(3)) for i in range(3))
    requested_pin_eci=env.add(env.add(requested_pin_eci,env.scale(env.cross(earth,pin_velocity),2.)),
        env.scale(env.cross(earth,env.cross(earth,pin_position)),-1.))
    angular=kinematics["angular_acceleration_body_rad_s2"]
    rotational=env.add(env.add(env.cross(angular,lever),env.cross(state.omega_body_rad_s,
        env.cross(state.omega_body_rad_s,lever))),env.add(env.scale(env.cross(state.omega_body_rad_s,lever_dot),2.),lever_ddot))
    requested_cg=env.add(requested_pin_eci,env.scale(dyn.rotate(state.q_body_to_eci,rotational),-1.))
    inputs=kinematics["source_model_load_inputs"]
    terminal_detail=None
    if not following:
        # This existing coordinated policy already acts on retained-CoM
        # position/velocity and measured tilt/rate. Replace CG coordinates,
        # rather than subtracting the pin lever from its output a second time.
        from .starship_terminal_guidance import terminal_horizontal_request
        horizontal,terminal_detail=terminal_horizontal_request(state,arrival,profile,catch,
            env.norm(inputs["gravity_acceleration_eci_mps2"]))
        cg_relative_acceleration=env.add(env.add(requested_cg,env.scale(env.cross(earth,state.v_eci_mps),-2.)),
            env.cross(earth,env.cross(earth,state.r_eci_m)))
        cg_local=[env.dot(cg_relative_acceleration,axis) for axis in axes]
        cg_local[:2]=horizontal
        cg_relative_acceleration=tuple(sum(cg_local[j]*axes[j][i]for j in range(3))for i in range(3))
        requested_cg=env.add(env.add(cg_relative_acceleration,env.scale(env.cross(earth,state.v_eci_mps),2.)),
            env.scale(env.cross(earth,env.cross(earth,state.r_eci_m)),-1.))
    mass=inputs["mass_kg"]
    force=env.add(env.scale(env.add(requested_cg,env.scale(inputs["gravity_acceleration_eci_mps2"],-1.)),mass),
        env.scale(dyn.rotate(state.q_body_to_eci,inputs["aero_force_body_n"]),-1.))
    unbounded=list(force)
    local=[env.dot(force,axis) for axis in axes]
    local[2]=max(.5*mass,local[2])
    height=min(arrival["pin_height_above_support_m"])-translation_reference["target_position_enu_m"][2]+catch["support_height_m"]
    maximum_tilt=60. if following and height>1000. else 30. if following and height>100. else catch["terminal_max_tilt_deg"]
    lateral=math.hypot(*local[:2])
    lateral_scale=min(1.,local[2]*math.tan(math.radians(maximum_tilt))/max(lateral,1e-30))
    local[:2]=[x*lateral_scale for x in local[:2]]
    force=tuple(sum(local[j]*axes[j][i] for j in range(3)) for i in range(3))
    maximum_force=CONFIG["maximum_main_engines"]*profile["booster"]["engine_thrust_n"]
    capacity_scale=min(1.,maximum_force/max(env.norm(force),1e-30))
    force=env.scale(force,capacity_scale)
    machine_tolerance=math.sqrt(math.ulp(1.))*max(1.,mass*9.81)
    low_force=env.norm(force)<=machine_tolerance
    axis=carried_axis if low_force else env.unit(force)
    receipt={"schema":GUIDANCE_SCHEMA,"policy_id":POLICY_ID,"time_s":state.time_s,"mode":mode,
        "state_sha256":shooting.digest(asdict(state)),"previous_command_sha256":shooting.digest(asdict(previous_command)),
        "translation_reference_sha256":shooting.digest(translation_reference),"reference_sample":sample,
        "actual_pin_kinematics":kinematics,"feedback":feedback,"nonvertical_corridor_ready":corridor,
        "terminal_horizontal_feedback":terminal_detail,
        "horizontal_request_space":"material_pin_tower_enu" if following else "cg_tower_enu",
        "terminal_horizontal_pin_lever_subtracted_twice":False,
        "pin_height_in_existing_window":height_ready,"current_model_angular_acceleration_body_rad_s2":list(angular),
        "requested_cg_acceleration_eci_mps2":list(requested_cg),"unbounded_engine_force_eci_n":unbounded,
        "requested_engine_force_eci_n":list(force),"requested_axis_eci":list(axis),
        "carried_axis_eci":list(carried_axis),"low_force_anchor_unresolved":low_force,
        "machine_force_tolerance_n":machine_tolerance,"maximum_tilt_deg":maximum_tilt,
        "lateral_scale":lateral_scale,"force_capacity_scale":capacity_scale,
        "vertical_floor_clipped":env.dot(unbounded,axes[2])<.5*mass,
        "force_bounds_are_requests_not_achieved":True,
        "minimum_vertical_thrust_n":max(0.,env.dot(force,axes[2])),
        "target_frame_origin_eci_m":list(site.r),"target_frame_axes_eci":[list(x) for x in axes],
        "pin_acceleration_is_achieved":False,"force_request_is_achieved":False,
        "current_rhs_is_independently_measured_angular_acceleration":False,
        "prospective_control_angular_acceleration_is_current_rhs":False,
        "independent_pose_quintic_used":False,"static_pose_assigned_to_vehicle":False,
        "actual_state_assigned":False,"arrival_admitted":False,"support_admitted":False,
        "physical_execution":False}
    return force,axis,shooting.saved(receipt)


def _controller_memory(frame,tracker,pretrim,previous_command,previous_command_time):
    return shooting.saved({"conditioned_reference":{
        "quaternion":frame.frame.quaternion,"time_s":frame.time_s,
        "maximum_roll_rate_rad_s":frame.maximum_roll_rate_rad_s,"bridging":frame.bridging,
        "diagnostics":deepcopy(frame.diagnostics),"frame_diagnostics":deepcopy(frame.frame.diagnostics)},
        "reference_tracker":deepcopy(vars(tracker)),"entry_pretrim_prepared_at_s":pretrim,
        "previous_actual_command":asdict(previous_command),"previous_actual_command_time_s":previous_command_time})


def validate_input_binding(binding,snapshot,reference,profile,catch,previous_command):
    hashes={"source_run_sha256","source_prior_checkpoint_sha256","source_origin_checkpoint_sha256",
        "origin_context_sha256","prior_command_sha256","translation_reference_sha256","profile_sha256",
        "catch_profile_sha256","static_17_result_sha256","static_17_reverification_sha256",
        "static_17_raw_json_sha256","static_17_source_map_sha256"}
    expected=hashes|{"schema","source_origin_checkpoint_index","source_binding_is_caller_assertion",
        "source_authentication_independently_verified","reference_is_execution","full_launch_reexecuted"}
    if (type(binding) is not dict or set(binding)!=expected or binding["schema"]!=BINDING_SCHEMA
            or any(type(binding[k]) is not str or len(binding[k])!=64 or any(c not in "0123456789abcdef" for c in binding[k]) for k in hashes)
            or type(binding["source_origin_checkpoint_index"]) is not int or binding["source_origin_checkpoint_index"]<1
            or binding["source_binding_is_caller_assertion"] is not True
            or any(binding[k] is not False for k in ("source_authentication_independently_verified","reference_is_execution","full_launch_reexecuted"))):
        raise ValueError("invalid_coupled_pin_input_binding")
    for key,value in (("origin_context_sha256",snapshot),("prior_command_sha256",asdict(previous_command)),
            ("translation_reference_sha256",reference),("profile_sha256",profile),("catch_profile_sha256",catch)):
        if binding[key]!=shooting.digest(value):
            raise ValueError("coupled_pin_input_digest_mismatch")


def base_actuation_request(state,booster,force,axis):
    """Existing discrete/slew boundary with a least-squares scalar input."""
    from .starship_constrained_recovery import landing_actuation_demand
    body_axis=dyn.rotate(state.q_body_to_eci,(0.,0.,1.))
    signed=float(env.dot(body_axis,axis))
    projected_force=max(0.,env.dot(body_axis,force))
    selector_input=projected_force*max(.2,signed)
    count,throttle,pair,detail=landing_actuation_demand(state,booster,axis,selector_input)
    return count,throttle,pair,{**detail,"least_squares_actual_axis_projection_n":projected_force,
        "legacy_selector_input_n":selector_input,"base_pair_indices":list(pair[0]) if pair else None,
        "base_scalar_force_projection_is_achieved":False}


def simulate_coupled_pin_trial(snapshot,translation_reference,profile,catch,previous_command,*,request,artifact_sink,source_bindings):
    """One isolated finite-plant continuation. No catalog, callback or hardware.

    The sink is the fixed resource-bounded evidence implementation, not a plant
    or control callback. Full per-step receipts are durably saved before use by
    any independent checker; the compact physical record preserves all steps.
    """
    from .starship_static_artifacts import BoundedStaticArtifactSink
    from .starship_sixdof_mission import vehicle,_attitude
    from .starship_sixdof_booster import _sample_booster
    from .starship_booster_control import control_with_measured_tvc,reallocate_measured_tvc
    from .starship_booster_recovery import _tower_observation
    from .starship_sixdof_contact import find_contact,hull_clearance
    from .starship_terminal_wrench import allocate_terminal_wrench
    started=time.monotonic()
    expected_request={"schema","origin_context_sha256","duration_s","maximum_integration_steps",
        "wall_deadline_monotonic_s","automatic_retry","hardware_execution","catch_execution"}
    if (type(request) is not dict or set(request)!=expected_request or request["schema"]!=REQUEST_SCHEMA
            or request["origin_context_sha256"]!=shooting.digest(snapshot)
            or not 0<_number(request["duration_s"])<=60.
            or type(request["maximum_integration_steps"]) is not int or not 1<=request["maximum_integration_steps"]<=600
            or not started<_number(request["wall_deadline_monotonic_s"])<=started+120.
            or any(request[key] is not False for key in ("automatic_retry","hardware_execution","catch_execution"))
            or type(artifact_sink) is not BoundedStaticArtifactSink):
        raise ValueError("coupled_pin_trial_requires_fixed_request_and_evidence_sink")
    snapshot,translation_reference,profile,catch,source_bindings=map(deepcopy,
        (snapshot,translation_reference,profile,catch,source_bindings))
    validate_input_binding(source_bindings,snapshot,translation_reference,profile,catch,previous_command)
    shooting.validate_context(snapshot,profile,catch,shooting.physical_configuration_from_context(snapshot))
    if (snapshot["context"]["phase"]!="recovery_entry_coast"
            or translation_reference["origin_context_sha256"]!=shooting.digest(snapshot)
            or translation_reference["previous_command"]!=shooting.saved(asdict(previous_command))
            or snapshot["state"]["time_s"]+request["duration_s"]>snapshot["context"]["deadline_s"]+1e-9):
        raise ValueError("coupled_pin_trial_origin_or_causal_command_binding")
    state=dyn.state_from_dict(snapshot["state"])
    booster=vehicle(profile,"booster")
    dyn._validate_pair(state,booster,previous_command)
    frame,tracker=shooting.restore_references(snapshot["context"],profile)
    if tracker is None:
        raise ValueError("coupled_pin_trial_requires_actual_carried_tracker")
    pretrim=snapshot["context"]["entry_pretrim_prepared_at_s"]
    previous_command_time=translation_reference["prior_state"]["time_s"]
    initial_memory=_controller_memory(frame,tracker,pretrim,previous_command,previous_command_time)
    inputs_artifact=shooting._persist(artifact_sink,{"kind":"coupled_pin_trial_inputs","schema":SCHEMA,
        "origin_context":snapshot,"translation_reference":translation_reference,"profile":profile,"catch_profile":catch,
        "configuration":CONFIG,"request":request,"source_bindings":source_bindings,"controller_memory":initial_memory,
        "started_monotonic_s":started,
        "isolated_saved_state_continuation":True,"full_launch_reexecuted":False,"hardware_execution":False})
    points,samples,events=[],[],[]
    steps=0
    termination="simulated_duration_exhausted"
    failure=None
    failure_chain=[]
    contact=None
    handoff=None
    end=state.time_s+request["duration_s"]
    phase="recovery_landing_coupled_pin"
    try:
        while state.time_s<end-1e-9:
            if steps>=request["maximum_integration_steps"]:
                termination="integration_step_budget_exhausted"
                break
            if time.monotonic()>=request["wall_deadline_monotonic_s"]:
                termination="wall_budget_exhausted"
                break
            artifact_sink.check_before_query()
            observed=dyn.observe(state,booster)
            arrival=_tower_observation(state,booster,profile,catch)
            if arrival["eligible"]:
                termination="catch_handoff"
                handoff={"eligible":True,"time_s":state.time_s,"state":asdict(state),"observation":arrival,"limits":arrival["limits"]}
                break
            if state.propellant_kg<=0:
                termination="propellant_exhausted"
                break
            if env.norm(state.omega_body_rad_s)>5.:
                termination="angular_rate_envelope_exceeded"
                break
            before_memory=_controller_memory(frame,tracker,pretrim,previous_command,previous_command_time)
            force,axis,guidance=pin_guidance_request(state,booster,profile,catch,previous_command,translation_reference,arrival,
                carried_axis_eci=dyn.rotate(tracker.quaternion,(0.,0.,1.)))
            _,axes=_tower_frame(profile,state.time_s)
            preferred=_attitude(axis,axes[0])
            target=frame.target(axis,axes[0],preferred,time_s=state.time_s)
            target,tracking=tracker.update(target,state.q_body_to_eci,state.omega_body_rad_s,state.time_s)
            step=min(.1,end-state.time_s)
            if abs(step-.1)<=1e-9:
                step=.1
            # Reuse the existing discrete/slew/pair selector without its old
            # norm/signed compensation: its internal quotient then equals the
            # least-squares scalar force projection on the ACTUAL body axis.
            count,throttle,pair,base_selection=base_actuation_request(state,booster,force,axis)
            fin_policy="finite_moment_priority_fins_v1" if pretrim is not None else "finite_regularized_fins_v1"
            base,control=control_with_measured_tvc(state,booster,target,throttle,count,profile,
                use_flaps=True,development_fin_allocation=True,control_interval_s=step,development_fin_policy=fin_policy,
                reference_rate_body_rad_s=tracking["reference_rate_body_rad_s"],
                reference_acceleration_body_rad_s=tracking["reference_acceleration_body_rad_s"])
            if pair is not None:
                selected,throttle=pair
                engines=list(base.engines)
                for index in range(profile["booster"]["engine_count"]):
                    engines[index]=replace(engines[index],enabled=index in selected,throttle=throttle if index in selected else 0.)
                base=dyn.Command6DOF(tuple(engines),base.flap_angles_rad)
                base,control=reallocate_measured_tvc(state,booster,base,control,profile,observed=observed)
            command,wrench=allocate_terminal_wrench(state,booster,base,observed,force,control["requested_torque_body_nm"],
                interval_s=step,up_eci=axes[2],minimum_vertical_thrust_n=guidance["minimum_vertical_thrust_n"])
            selected=wrench["mask_candidates"][wrench["selected_mask_index"]] if wrench["status"]=="accepted" else None
            predicted_force=(selected["predicted_force_eci_n"] if selected else wrench.get("base_predicted_force_eci_n"))
            predicted_torque=(selected["predicted_engine_torque_body_nm"] if selected else wrench.get("base_predicted_engine_torque_body_nm"))
            allocation_summary={"status":wrench["status"],"fallback_reason":wrench["reason"],
                "commanded_main_indices":[i for i in range(profile["booster"]["engine_count"]) if command.engines[i].enabled],
                "predicted_finite_force_eci_n":predicted_force,"predicted_finite_engine_torque_body_nm":predicted_torque,
                "predicted_force_residual_eci_n":list(env.add(predicted_force,env.scale(force,-1.))) if predicted_force is not None else None,
                "prediction_is_achieved_force_or_moment":False,"command_is_hardware_execution":False}
            navigation={**control,"coupled_pin_guidance":guidance,"reference_tracking":tracking["receipt"],
                "conditioned_reference_diagnostics":deepcopy(frame.diagnostics),"terminal_wrench":wrench,
                "finite_wrench_summary":allocation_summary,"current_dynamic_pressure_pa":float(observed["dynamic_pressure_pa"]),
                "arrival_observation":arrival,"macrostep_s":step,"base_main_throttle":throttle,
                "base_main_engine_count":count,"finite_fin_policy":fin_policy,"requested_engine_force_eci_n":list(force),
                "base_actuation":base_selection}
            step_artifact=shooting._persist(artifact_sink,{"kind":"coupled_pin_step_receipt","step_index":steps,
                "time_s":state.time_s,"state":asdict(state),"state_sha256":shooting.digest(asdict(state)),"base_command":asdict(base),
                "command":asdict(command),"navigation":navigation,"controller_memory_before":before_memory})
            points.append({"time_s":state.time_s,"phase":phase,"state":asdict(state),"command":asdict(command),
                "com_rate_body_mps":list(observed["com_rate_body_mps"]),"step_receipt_artifact":step_artifact})
            samples.append(_sample_booster(state,booster,phase))
            if time.monotonic()>=request["wall_deadline_monotonic_s"]:
                termination="wall_budget_exhausted_before_integration"
                break
            before_time=state.time_s
            if observed["altitude_m"]<300.+env.norm(state.v_eci_mps)*step:
                state,receipt=find_contact(state,booster,command,step,profile["geometry"]["booster_length_m"],profile["geometry"]["radius_m"])
                if receipt["contact"]:
                    contact,termination=receipt,"surface_contact"
            else:
                state=dyn.step(state,booster,command,step)
            steps+=1
            previous_command,previous_command_time=command,before_time
            if contact is not None:
                break
    except Exception as exc:
        failure={"error_class":type(exc).__name__,"stage":"controller_or_record_or_integration",
            "computation_completed":False,"arrival_admitted":False}
        failure_chain.append(deepcopy(failure))
        termination="coupled_pin_controller_or_record_exception"
    final_memory=None
    try:
        final_memory=_controller_memory(frame,tracker,pretrim,previous_command,previous_command_time)
    except Exception as exc:
        final_error={"error_class":type(exc).__name__,"stage":"final_controller_memory","computation_completed":False,"arrival_admitted":False}
        failure_chain.append(final_error)
        failure=failure or final_error
    final_observation=None
    try:
        final_observation=_tower_observation(state,booster,profile,catch)
        final_sample=_sample_booster(state,booster,phase)
        final_clearance=hull_clearance(state,booster,profile["geometry"]["booster_length_m"],profile["geometry"]["radius_m"])["signed_clearance_m"]
    except Exception as exc:
        final_error={"error_class":type(exc).__name__,"stage":"final_observation","computation_completed":False,"arrival_admitted":False}
        failure_chain.append(final_error)
        failure=failure or final_error
        final_sample=None
        final_clearance=None
    if points and points[-1]["time_s"]==state.time_s:
        points[-1]["command"]=None
    else:
        points.append({"time_s":state.time_s,"phase":phase,"state":asdict(state),"command":None,
            "com_rate_body_mps":final_observation["com_rate_body_mps"] if final_observation else None,"step_receipt_artifact":None})
    if final_sample is not None:
        if samples and samples[-1]["time_s"]==state.time_s:
            samples[-1]=final_sample
        else:
            samples.append(final_sample)
    # A final macrostep may reach the window exactly at the declared horizon,
    # before the next loop-head observation. Reuse this measured endpoint;
    # never execute another step or promote a pending, unexecuted request.
    pending_final_attempt=bool(points and points[-1]["time_s"]==state.time_s
        and points[-1]["step_receipt_artifact"] is not None)
    final_handoff_safe=(failure is None and contact is None and final_memory is not None
        and final_observation is not None and final_observation["eligible"] is True
        and state.propellant_kg>0 and env.norm(state.omega_body_rad_s)<=5.
        and not pending_final_attempt and termination not in ("surface_contact","propellant_exhausted",
            "angular_rate_envelope_exceeded","wall_budget_exhausted_before_integration",
            "coupled_pin_controller_or_record_exception"))
    if final_handoff_safe and (handoff is not None or steps>=1):
        handoff={"eligible":True,"time_s":state.time_s,"state":asdict(state),
            "observation":final_observation,"limits":final_observation["limits"]}
        termination="catch_handoff"
    else:
        handoff={"eligible":False,"time_s":state.time_s,"state":None,"observation":final_observation,
            "limits":final_observation["limits"] if final_observation else None}
        if termination=="catch_handoff":
            termination="coupled_pin_controller_or_record_exception"
    events.append({"event":termination,"time_s":state.time_s,"state":asdict(state)})
    result={"scenario":"booster_coupled_pin_trial","body_id":"booster","guidance_policy":POLICY_ID,
        "initial_state":snapshot["state"],"final_state":asdict(state),"samples":samples,"events":events,"contact":contact,
        "recovery_record":{"schema":SCHEMA,"policy_id":POLICY_ID,"guidance_configuration":CONFIG,
            "origin_context":snapshot,"translation_reference":translation_reference,"source_bindings":source_bindings,
            "inputs_artifact":inputs_artifact,"initial_controller_memory":initial_memory,"final_controller_memory":final_memory,
            "checkpoints":points,"handoff":handoff,"request":request,"failure":failure,
            "started_monotonic_s":started,"failure_chain":failure_chain,
            "production_policy_admitted":False,"physical_execution":False,"missionos_dispatch":False},
        "outcome":{"termination":termination,"start_time_s":snapshot["state"]["time_s"],"end_time_s":state.time_s,
            "duration_s":state.time_s-snapshot["state"]["time_s"],"integration_steps":steps,"handoff_reached":handoff["eligible"],
            "wall_seconds":time.monotonic()-started,
            "final_hull_clearance_m":final_clearance,"actual_isolated_simulator_continuation":steps>0,
            "full_launch_reexecuted":False,"catch_executed":False,"physical_execution":False,"mission_completed":False}}
    return shooting.saved(result)
