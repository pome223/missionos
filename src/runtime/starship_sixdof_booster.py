"""Separated-booster six-DOF return with exposed development guidance.

The inherited separation state is never projected onto a return trajectory.
Body forces come only from the common finite engines, gimbals, jets and panels.
This is not SpaceX flight software, a catch controller, or a validated airload
model. A missed return, fuel exhaustion or impact is retained as an outcome.
"""
from __future__ import annotations

from dataclasses import asdict
import math

from . import starship_physics as env
from . import starship_sixdof as dyn

DEFAULTS = {
    "max_duration_s": 1200., "boostback_max_slew_s": 90., "boostback_max_burn_s": 70.,
    "boostback_engine_count": 33, "boostback_slew_throttle": .4,
    "boostback_alignment_deg": 20., "boostback_velocity_tolerance_mps": 30.,
    "boostback_velocity_tau_s": 25., "boostback_max_horizontal_speed_mps": 700.,
    "boostback_upward_acceleration_mps2": 2., "landing_reserve_kg": 60_000.,
    "landing_ignition_altitude_m": 2000., "landing_velocity_tau_s": 1.5,
    "landing_horizontal_tau_s": 8., "landing_target_speed_mps": 2., "landing_max_tilt_deg": 15.,
    "landing_ignition_margin_m": 300., "contact_speed_limit_mps": 5., "contact_tilt_limit_deg": 10.,
    "contact_rate_limit_rad_s": .05, "return_site_tolerance_m": 1000.,
}


def _configuration(profile):
    supplied = profile.get("booster_return", {})
    if type(supplied) is not dict or set(supplied)-set(DEFAULTS):
        raise ValueError("unknown_booster_return_configuration")
    resolved = {**DEFAULTS, **supplied}
    if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
           for value in resolved.values()):
        raise ValueError("invalid_booster_return_configuration")
    if resolved["max_duration_s"] > 2000 or any(resolved[key] >= 90 for key in
            ("boostback_alignment_deg", "landing_max_tilt_deg", "contact_tilt_limit_deg")):
        raise ValueError("invalid_booster_return_configuration")
    if (type(resolved["boostback_engine_count"]) is not int
            or not 3 <= resolved["boostback_engine_count"] <= profile["booster"]["engine_count"]
            or resolved["boostback_slew_throttle"] > 1.):
        raise ValueError("invalid_booster_return_configuration")
    return resolved


def _minus(a, b):
    return env.add(a, env.scale(b, -1))


def _horizontal(vector, up):
    return _minus(vector, env.scale(up, env.dot(vector, up)))


def _cap(vector, maximum):
    length = env.norm(vector)
    return env.scale(vector, min(1., maximum/max(length, 1e-12)))


def _landing_engine_demand(state, booster, required_force_n, maximum_count):
    """Use the smallest available prefix that can supply the requested force.

    The finite actuator still enforces each engine's minimum throttle and spool.
    Counting only available engines preserves inherited failures, including a
    failed central engine. This is development guidance, not SpaceX sequencing.
    """
    available = 0.
    for count in range(1, maximum_count+1):
        engine = booster.engines[count-1]
        if state.engine_states[count-1].available:
            available += engine.max_thrust_n
        if available >= required_force_n:
            break
    floor = max((e.min_throttle for i, e in enumerate(booster.engines[:count])
                 if state.engine_states[i].available), default=0.)
    return count, max(floor, min(1., required_force_n/max(available, 1.)))


def _navigation(state, profile, *, return_site=None):
    from .starship_sixdof_mission import point_state
    point = point_state(state)
    up, east, north = env.local_frame(point)
    if return_site is not None:
        from .starship_return_sites import validate_return_site
        validate_return_site(return_site, profile)
    if return_site is None or return_site.site_id == "capture":
        # Explicit original capture retains the exact legacy arithmetic.
        target = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
                                   time_s=state.time_s)
    else:
        target = env.surface_state(return_site.latitude_deg, return_site.longitude_deg,
                                   return_site.elevation_m, time_s=state.time_s)
    relative_velocity = env.air_relative_velocity(point)
    displacement = _horizontal(_minus(target.r, state.r_eci_m), up)
    # Surface great-circle distance is a diagnostic; guidance uses the local
    # projected displacement, an explicitly approximate return targeting law.
    a, b = env.unit(state.r_eci_m), env.unit(target.r)
    distance = env.EARTH_EQUATORIAL_RADIUS_M*math.atan2(env.norm(env.cross(a, b)), env.dot(a, b))
    return up, east, north, relative_velocity, displacement, distance


def _sample_booster(state, vehicle, phase, command=None, diagnostics=None):
    from .starship_sixdof_mission import _sample
    record = _sample(state, vehicle, phase, command, diagnostics)
    record["body_id"] = "booster"
    return record


def _drag_aware_stop_estimate(state, booster, observed, up, vertical, profile, maximum_count):
    """Local 1D burn preview, with measured drag area, density, spool and fuel.

    This is a guidance estimate, not a second truth trajectory. It freezes the
    currently observed axial drag area and assumes aligned thrust. Actual six-
    DOF motion, attitude, allocation and force integration remain unchanged.
    A fuel-infeasible high-altitude burn is not mistaken for a landing window.
    """
    height = observed["altitude_m"]
    speed = max(0., -vertical)
    rho = observed["atmosphere"]["density_kg_m3"]
    drag_up = max(0., env.dot(dyn.rotate(state.q_body_to_eci, observed["aero_force_body_n"]), up))
    area = 2*drag_up/(rho*speed*speed) if rho > 1e-12 and speed > 10 else math.pi*profile["geometry"]["radius_m"]**2*profile["aero"]["axial_cd"]
    selected = [e for i, e in enumerate(booster.engines[:maximum_count]) if state.engine_states[i].available]
    available = sum(e.max_thrust_n for e in selected)
    mass, fuel = observed["mass_kg"], state.propellant_kg
    dry = mass-fuel
    throttle = max((state.engine_states[i].throttle for i in range(maximum_count) if state.engine_states[i].available), default=0.)
    elapsed, feasible, initial_fuel, initial_height = 0., False, fuel, height
    tau = max((e.throttle_time_constant_s for e in selected), default=1.)
    rate = min((e.throttle_rate_per_s for e in selected), default=1.)
    flow_full = sum(e.max_thrust_n/(e.isp_s*env.STANDARD_GRAVITY_MPS2) for e in selected)
    while elapsed < 100. and available > 0 and fuel > 1e-6:
        if speed <= 2.:
            feasible = True
            break
        dt = min(.1, fuel/max(flow_full, 1.))
        throttle = min(1., throttle+min(rate, (1.-throttle)/tau)*dt)
        density = env.standard_atmosphere(max(0., height))["density_kg_m3"]
        gravity = env.EARTH_MU_M3_S2/(env.EARTH_EQUATORIAL_RADIUS_M+max(0., height))**2
        acceleration = available*throttle/(dry+fuel)+.5*density*area*speed*speed/(dry+fuel)-gravity
        after_speed = max(0., speed-acceleration*dt)
        height -= .5*(speed+after_speed)*dt
        fuel = max(0., fuel-flow_full*throttle*dt)
        speed, elapsed = after_speed, elapsed+dt
    return {"estimated_stopping_height_m": initial_height-height,
            "estimated_burn_time_s": elapsed, "estimated_burn_propellant_kg": initial_fuel-fuel,
            "estimated_drag_area_m2": area, "burn_fuel_feasible": feasible,
            "available_landing_engine_count": len(selected), "available_landing_thrust_n": available,
            "preview_assumption": "aligned_thrust_current_drag_area_spool_density_and_fuel_1d"}


def _coast_control_profile(profile, observed, *, include_spooled_tvc=False):
    """Match coast PD bandwidth to the configured physical jet-pair authority."""
    jets = profile["actuators"]
    # Each signed pitch/yaw channel is two opposite jets, each at radius r.
    torque = 2*jets["rcs_radius_m"]*jets["rcs_thrust_n"]
    if include_spooled_tvc:
        # A cutoff command does not erase thrust. Preserve gimbal authority
        # during actual spool-down rather than prematurely using jets alone.
        for i, load in enumerate(observed["engine_loads"][:profile["booster"]["gimbal_engine_count"]]):
            lever = abs(observed["com_body_m"][2]-profile["booster"]["engine_positions_body_m"][i][2])
            torque += load["thrust_n"]*lever*math.sin(math.radians(jets["max_gimbal_deg"]))
    inertia_bound = max(sum(abs(x) for x in row) for row in observed["inertia_kg_m2"])
    alpha = min(profile["guidance"]["max_angular_acceleration_rad_s2"], torque/inertia_bound)
    frequency = min(profile["guidance"]["attitude_frequency_rad_s"], math.sqrt(alpha/math.pi))
    return {**profile, "guidance": {**profile["guidance"], "attitude_frequency_rad_s": frequency,
                                   "max_angular_acceleration_rad_s2": alpha}}, frequency, alpha


def simulate_booster(profile, separation_state_dict, duration_s=None, *, guidance_policy="fixed_v1", mission_director=None, return_sites=None, tower_ready=True, splashdown_goal=None):
    """Continue from the exact supplied state for at most 2000 elapsed seconds.

    The return site is the rotating launch location. This is a geometric target,
    not catch hardware. A passing contact envelope means only its explicitly
    configured speed/tilt/rate/location bounds were met in this simulation.
    """
    from .starship_sixdof_mission import _attitude, control, vehicle
    from .starship_sixdof_contact import find_contact, hull_clearance
    configuration = _configuration(profile)
    if splashdown_goal is not None:
        from .starship_splashdown import SplashdownGoal
        if type(splashdown_goal) is not SplashdownGoal or return_sites is None or guidance_policy != "fixed_v1":
            raise ValueError("splashdown_requires_approved_goal_and_fixed_base_guidance")
        splashdown_goal.validate_site(return_sites.divert)
    if guidance_policy not in ("fixed_v1", "site_return_v1", "site_return_v2", "site_return_v3"):
        raise ValueError("unknown_booster_guidance_policy")
    site_feedback = guidance_policy in ("site_return_v1", "site_return_v2", "site_return_v3")
    drag_aware = guidance_policy in ("site_return_v2", "site_return_v3")
    mitigate_infeasible_burn = guidance_policy == "site_return_v3"
    duration = configuration["max_duration_s"] if duration_s is None else duration_s
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 2000:
        raise ValueError("booster_duration_must_be_in_zero_to_2000_seconds")
    state = dyn.state_from_dict(separation_state_dict)
    time_steps = [profile.get("integration", {}).get(key) for key in ("powered_dt_s", "coast_dt_s")]
    if any(type(dt) not in (int, float) or not math.isfinite(dt) or not 0 < dt <= 10 for dt in time_steps):
        raise ValueError("invalid_booster_integration_step")
    if duration/min(time_steps) > 100_000:
        raise ValueError("booster_integration_budget_exceeded")
    if state.time_s+min(time_steps) <= state.time_s or state.time_s+duration <= state.time_s:
        raise ValueError("booster_integration_clock_must_advance")
    booster = vehicle(profile, "booster")
    # Validation checks the supplied actuator cardinality and limits, preserving
    # throttle, gimbals and unavailable-engine flags inherited from separation.
    dyn.observe(state, booster)
    start_time = state.time_s
    phase, termination = "booster_boostback_slew", "time_limit"
    burn_start, coast_started, slew_commanded = None, None, False
    steps, samples, events = 0, [], []
    max_altitude, max_rate, max_error = -math.inf, 0., 0.
    next_sample, contact_receipt = state.time_s, None
    length, radius = profile["geometry"]["booster_length_m"], profile["geometry"]["radius_m"]

    def event(name, detail, **fields):
        events.append({"time_s": state.time_s, "event": name, "detail": detail, **fields})

    active_site = return_sites.divert if return_sites is not None else None
    # Capture corridor has not been qualified from launch for this controller.
    # The same independent restriction applies to the fixed-timeline comparator.
    def director_observation():
        from .starship_mission_director import fuel_sensor
        return {"time_s": state.time_s, "phase": "booster_return", "released_count": 0,
            "release_acknowledged": False, "sequencer_state": "running",
            "fuel_kg": fuel_sensor(state.propellant_kg, state.time_s, "booster"), "return_deadline_s": start_time+duration,
            "hold_expires_at_s": None,
            "tower_ready": tower_ready, "operations_notice": "",
            "numerical_tools": {"orbit_release_feasible": False, "retained_payload_present": False,
                "capture_corridor_certified": False, "mechanism_status": "not_collected"}}
    event("booster_return_start", "Exact separated state inherited; no position, velocity, attitude, rate or fault-state reset.")
    while state.time_s < start_time+duration-1e-9:
        if mission_director is not None:
            mission_director.pace("booster", state.time_s)
            row = director_observation()
            action = mission_director.update("booster_selection", row)
            if action is not None:
                active_site = return_sites.divert if action == "splashdown" else return_sites.site(action)
                event("managed_booster_command", "preapproved early return-site decision; finite control integrates the trajectory",
                    action=action, return_site_sha256=active_site.sha256)
            mission_director.confirm("booster", row)
        observed = dyn.observe(state, booster)
        up, east, north, velocity, displacement, site_distance = _navigation(state, profile, return_site=active_site)
        altitude, vertical = observed["altitude_m"], env.dot(velocity, up)
        horizontal_velocity = _horizontal(velocity, up)
        mass = observed["mass_kg"]
        g = env.EARTH_MU_M3_S2/env.norm(state.r_eci_m)**2
        target, throttle, count = _attitude(up, north), 0., 0
        guidance = {"return_site_distance_m": site_distance, "vertical_speed_mps": vertical,
                    "guidance_origin": "explicit_generic_booster_return_assumption"}
        if phase in ("booster_boostback_slew", "booster_boostback"):
            fall_time = max(30., min(400., (vertical+math.sqrt(vertical*vertical+2*g*max(0., altitude)))/g))
            desired_horizontal = _cap(env.scale(displacement, 1/fall_time), configuration["boostback_max_horizontal_speed_mps"])
            velocity_error = _minus(desired_horizontal, horizontal_velocity)
            requested_acceleration = env.add(env.scale(velocity_error, 1/configuration["boostback_velocity_tau_s"]),
                                             env.scale(up, configuration["boostback_upward_acceleration_mps2"]))
            desired_axis = env.unit(requested_acceleration)
            target = _attitude(desired_axis, north)
            alignment = env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), desired_axis)
            guidance.update(horizontal_velocity_error_mps=env.norm(velocity_error), estimated_fall_time_s=fall_time)
            ready = alignment >= math.cos(math.radians(configuration["boostback_alignment_deg"]))
            tolerance = configuration["boostback_velocity_tolerance_mps"]
            if site_feedback and not drag_aware:
                # The old fixed velocity tolerance alone can represent many
                # kilometres of coast error. Bind it to the stated site radius.
                tolerance = min(tolerance, configuration["return_site_tolerance_m"]/fall_time)
            cutoff = (state.propellant_kg <= configuration["landing_reserve_kg"]
                      or env.norm(velocity_error) <= tolerance
                      or (burn_start is not None and state.time_s-burn_start >= configuration["boostback_max_burn_s"]))
            slew_timeout = burn_start is None and state.time_s-start_time >= configuration["boostback_max_slew_s"]
            if cutoff or slew_timeout:
                phase, coast_started = "booster_entry_coast", state.time_s
                event("booster_boostback_cutoff_command", "Return targeting/reserve/time guard; finite shutdown spool remains integrated.",
                      alignment_timeout=slew_timeout, remaining_propellant_kg=state.propellant_kg)
            elif ready:
                if phase == "booster_boostback_slew":
                    phase, burn_start = "booster_boostback", state.time_s
                    event("booster_boostback_ignition_command", "Configured main engines requested after measured alignment and reserve gates.",
                          requested_engine_count=configuration["boostback_engine_count"])
                count = configuration["boostback_engine_count"]
                available = sum(e.max_thrust_n for i, e in enumerate(booster.engines[:count]) if state.engine_states[i].available)
                throttle = max(.4, min(1., mass*env.norm(requested_acceleration)/max(available, 1.)))
                if site_feedback and not drag_aware:
                    count, throttle = _landing_engine_demand(state, booster, mass*env.norm(requested_acceleration), count)
            else:
                # Main-engine gimbals cannot create a moment with zero thrust.
                # Flight 8 documents three center engines retained at staging;
                # retaining them throughout this slew is an exposed engineering
                # assumption, not a recovered Flight 14 throttle programme.
                count = min(3, profile["booster"]["engine_count"])
                throttle = max(configuration["boostback_slew_throttle"],
                               max(e.min_throttle for e in booster.engines[:count]))
                if not slew_commanded:
                    event("booster_powered_slew_command", "Three center engines requested for finite gimbal authority; generic throttle assumption, not measured Flight 14 guidance.",
                          requested_engine_count=count, requested_throttle=throttle)
                    slew_commanded = True
        if phase == "booster_entry_coast":
            target = _attitude(env.scale(velocity, -1) if env.norm(velocity) > 20 and vertical < 0 else up, north)
            if drag_aware:
                # Engine-end down throughout coast. Avoid the old 90-degree
                # target change around apogee with nearly zero vertical speed.
                target = _attitude(up, north)
            available = sum(e.max_thrust_n for i, e in enumerate(booster.engines[:3]) if state.engine_states[i].available)
            stopping_height = max(0., -vertical)**2/(2*max(.1, available/mass-g))
            ignition_height = max(configuration["landing_ignition_altitude_m"],
                                  stopping_height+configuration["landing_ignition_margin_m"])
            guidance.update(estimated_stopping_height_m=stopping_height)
            feasible = True
            if drag_aware:
                preview = _drag_aware_stop_estimate(state, booster, observed, up, vertical, profile,
                                                   min(profile["booster"]["gimbal_engine_count"], profile["booster"]["engine_count"]))
                guidance.update(preview)
                feasible = preview["burn_fuel_feasible"]
                ignition_height = preview["estimated_stopping_height_m"]+configuration["landing_ignition_margin_m"]+observed["com_body_m"][2]
            if vertical < 0 and (feasible or mitigate_infeasible_burn) and altitude <= ignition_height:
                phase = "booster_landing_burn"
                event("booster_landing_burn_requested",
                      "Terminal braking thrust is alignment-gated; optional powered slew may still request three finite engines." if site_feedback
                      else "Landing attitude and up to three main engines requested; actual ignition remains alignment-gated.",
                      fuel_feasible=feasible,
                      burn_intent="terminal_braking" if feasible else "impact_mitigation_insufficient_predicted_fuel",
                      maximum_engine_count=profile["booster"]["gimbal_engine_count"] if drag_aware else 3)
        if phase == "booster_landing_burn":
            height = altitude
            if site_feedback or splashdown_goal is not None:
                height = hull_clearance(state, booster, length, radius)["signed_clearance_m"]
            # Aim below the admitted downward-entry limit while avoiding an
            # unnecessarily prolonged low-altitude powered descent.
            contact_target = .75*splashdown_goal.maximum_downward_speed_mps if splashdown_goal is not None else configuration["landing_target_speed_mps"]
            desired_vertical = -max(contact_target, min(100., max(0., height)/6))
            vertical_acceleration = max(.5, g+(desired_vertical-vertical)/configuration["landing_velocity_tau_s"])
            horizontal_acceleration = env.scale(horizontal_velocity, -1/configuration["landing_horizontal_tau_s"])
            if site_feedback:
                tau = configuration["landing_horizontal_tau_s"]
                horizontal_acceleration = env.add(env.scale(displacement, 1/(tau*tau)), env.scale(horizontal_velocity, -2/tau))
            horizontal_acceleration = _cap(horizontal_acceleration,
                                           vertical_acceleration*math.tan(math.radians(configuration["landing_max_tilt_deg"])))
            requested = env.add(env.scale(up, vertical_acceleration), horizontal_acceleration)
            target = _attitude(env.unit(requested), north)
            alignment = env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), env.unit(requested))
            guidance.update(terminal_thrust_alignment_cosine=float(alignment),
                            terminal_braking_alignment_gate_met=bool(alignment > .5))
            if alignment > .5:
                count, throttle = _landing_engine_demand(state, booster, mass*env.norm(requested)/alignment,
                                                          min(profile["booster"]["gimbal_engine_count"] if drag_aware else 3, profile["booster"]["engine_count"]))
            elif site_feedback:
                # A gimbal cannot rotate the booster with its engine off.
                count, throttle = min(3, profile["booster"]["engine_count"]), configuration["boostback_slew_throttle"]
        control_profile = profile
        if drag_aware and phase == "booster_entry_coast":
            control_profile, bandwidth, alpha = _coast_control_profile(profile, observed, include_spooled_tvc=mitigate_infeasible_burn)
            guidance.update(coast_control_frequency_rad_s=bandwidth, coast_angular_acceleration_limit_rad_s2=alpha)
        command, diagnostic = control(state, booster, target, throttle, count, control_profile,
                                      use_flaps=phase in ("booster_entry_coast", "booster_landing_burn"))
        diagnostic.update(guidance)
        max_altitude = max(max_altitude, altitude)
        max_rate = max(max_rate, env.norm(state.omega_body_rad_s))
        max_error = max(max_error, diagnostic["attitude_error_deg"])
        if state.time_s >= next_sample-1e-9:
            samples.append(_sample_booster(state, booster, phase, command, diagnostic))
            next_sample = state.time_s+2.
        if env.norm(state.omega_body_rad_s) > 5:
            termination = "angular_rate_envelope_exceeded"
            event(termination, "Development 5 rad/s bound; no structural breakup solver.")
            break
        dt = min(start_time+duration-state.time_s,
                 profile["integration"]["powered_dt_s"] if throttle > 0 or altitude < 100_000
                 else profile["integration"]["coast_dt_s"])
        if type(dt) not in (int, float) or not math.isfinite(dt) or not 0 < dt <= 10:
            raise ValueError("invalid_booster_integration_step")
        # A conservative clearance bound avoids repeatedly solving near-Earth
        # hull geometry while the entire body is obviously far from the surface.
        maximum_acceleration = 50.+sum(e.max_thrust_n for e in booster.engines)/booster.dry_mass_kg
        near_surface = altitude <= length+radius+env.norm(observed["com_body_m"])+100.+env.norm(velocity)*dt+.5*maximum_acceleration*dt*dt
        if near_surface:
            state, receipt = find_contact(state, booster, command, dt, length, radius)
            if receipt["contact"]:
                contact_receipt = receipt
                termination = "surface_contact"
                event(termination, "Cylinder-envelope contact event; no mechanical catch or structural solver.",
                      surface_relative_speed_mps=receipt["surface_relative_speed_mps"])
                steps += 1
                break
        else:
            state = dyn.step(state, booster, command, dt)
        steps += 1
    final = _sample_booster(state, booster, phase)
    final["contact"] = contact_receipt is not None
    if not samples or samples[-1]["time_s"] != state.time_s:
        samples.append(final)
    else:
        samples[-1] = final
    up, _, _, _, _, distance = _navigation(state, profile, return_site=active_site)
    tilt = math.degrees(math.acos(max(-1., min(1., env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), up)))))
    rate = env.norm(state.omega_body_rad_s)
    envelope = (contact_receipt is not None
                and not contact_receipt["initial_overlap"]
                and contact_receipt["surface_normal_speed_mps"] <= 0
                and contact_receipt["surface_relative_speed_mps"] <= configuration["contact_speed_limit_mps"]
                and tilt <= configuration["contact_tilt_limit_deg"] and rate <= configuration["contact_rate_limit_rad_s"]
                and distance <= configuration["return_site_tolerance_m"])
    if termination == "time_limit":
        event("time_limit", "Return integration horizon reached; no terminal success inferred.")
    clearance = hull_clearance(state, booster, length, radius)
    result = {"scenario": "booster_return", "body_id": "booster", "samples": samples, "events": events,
            "booster_separation_state": separation_state_dict, "initial_state": samples[0],
            "final_state": asdict(state), "final_vehicle": asdict(booster), "contact": contact_receipt,
            "guidance_configuration": configuration, "guidance_policy": guidance_policy,
            **({"active_return_site": active_site.to_dict(), "return_site_sha256": active_site.sha256,
                "capture_inhibited": True, "divert_destination_reached": bool(envelope)} if active_site else {}),
            "outcome": {"termination": termination, "phase": phase, "duration_s": state.time_s-start_time,
                        "start_time_s": start_time, "end_time_s": state.time_s, "integration_steps": steps,
                        "max_altitude_m": max(max_altitude, final["altitude_m"]), "max_body_rate_rad_s": max(max_rate, rate),
                        "max_attitude_error_deg": max_error, "final_ground_speed_mps": final["ground_speed_mps"],
                        "final_altitude_m": final["altitude_m"], "final_hull_clearance_m": clearance["signed_clearance_m"],
                        "return_site_distance_m": distance, "final_tilt_deg": tilt, "final_body_rate_rad_s": rate,
                        "simulated_contact_envelope_met": bool(envelope), "catch_verified": False,
                        "landing_hardware_validated": False, "starship_vehicle_validated": False,
                        "six_dof_integrated": True, "attitude_prescribed": False,
                        "booster_return_6dof_implemented": True, "boostback_started": burn_start is not None,
                        "coast_started": coast_started is not None, "physical_execution": False},
            "limitations": ["Generic closed-loop return guidance and assumed actuator/aerodynamic parameters, not SpaceX flight software.",
                            "site_return_v2/v3 are optional bounded development trials: measured-drag one-dimensional burn preview and RCS-authority coast control; no terminal capture guarantee. v3 preserves spool-down TVC and attempts impact mitigation even when the burn cannot fully arrest descent.",
                            "Three-center-engine powered slew references the historical staging arrangement; slew throttle and control law are engineering assumptions, not a measured V3 flip sequence.",
                            "Return target is the declared model-test site when supplied, otherwise the launch-site ground location; catch tower, mechanisms, contact loads and structural survival are unmodeled.",
                            "No separate entry burn is introduced. Configured boostback count defaults to the V3 planned 33; fixed_v1/site_return_v1 final burn selects one to three, while v2/v3 can select up to the configured gimballed count. Neither reproduces Flight 14's 13-to-five-to-three landing sequence.",
                            "Shared propellant reservoir, no feed-path momentum, slosh, combustion or TPS model."]}
    if splashdown_goal is not None:
        from .starship_splashdown import entry_result
        result["splashdown"] = entry_result(result, splashdown_goal)
        result["limitations"].append("Controlled water-entry envelope at a synthetic offshore area; no waves, buoyancy, structural survival or real clearance validation.")
    return result
