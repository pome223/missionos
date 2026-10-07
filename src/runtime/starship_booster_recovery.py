"""Opt-in predictive booster recovery using unchanged finite six-DOF dynamics.

Coarse planning uses a local point-mass approximation; bounded cutoff forecasts
clone the same finite six-DOF policy continuation. Neither replaces executed
state. Commands act through finite engines, gimbals, jets and fins. Entry trim
is checked against an upcoming pressure envelope from the development profile.
A terminal state is handed to catch mechanics only after measured gates.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from copy import deepcopy
import math

import numpy as np

from . import starship_physics as env
from . import starship_sixdof as dyn
from .starship_sixdof_booster import _configuration, _navigation, _sample_booster, _coast_control_profile, _landing_engine_demand
from .starship_booster_control import control_with_measured_tvc, control_coast_stopping_distance

POLICY_ID = "predictive_return_v1"
CONFIG = {
    "prediction_dt_s": .5, "prediction_horizon_s": 600.,
    "entry_max_angle_deg": 60., "point_model_entry_max_angle_deg": 30., "entry_start_altitude_m": 80_000.,
    "boostback_alignment_deg": 15., "boostback_velocity_tolerance_mps": 12.,
    "rate_settle_limit_rad_s": .003, "rate_settle_max_s": 30.,
    "terminal_capture_height_m": 3.4, "terminal_position_tau_s": 6.,
    "maximum_full_coast_predictions": 24, "braking_max_tilt_deg": 60.,
    "sample_interval_s": .5, "maximum_duration_s": 1200.,
}


def _sub(a, b):
    return env.add(a, env.scale(b, -1.))


def _length(v):
    return math.sqrt(sum(x*x for x in v))


def _norm(v):
    length = _length(v)
    return tuple(x/max(length, 1e-30) for x in v)


def _local_force(velocity, axis, density, panels):
    """Same panel law in a predicted local frame; no actuator/state mutation."""
    z = np.asarray(axis)
    x = np.asarray([1., 0., 0.])-z[0]*z
    if float(x@x) < 1e-8:
        x = np.asarray([0., 1., 0.])-z[1]*z
    x /= np.linalg.norm(x)
    rotation = np.column_stack([x, np.cross(z, x), z])
    air = rotation.T@np.asarray(velocity)
    force = np.zeros(3)
    for panel in panels:
        normal = np.asarray(panel.normal_body)
        normal_speed = float(air@normal)
        tangent = air-normal*normal_speed
        force -= .5*density*panel.area_m2*(panel.normal_coefficient*normal_speed*abs(normal_speed)*normal
                   +panel.tangential_coefficient*float(np.linalg.norm(tangent))*tangent)
    return tuple(rotation@force)


def _trim_entry_candidate(velocity, axis, density, panels, com_body_m, jet_torque_nm, minimum_trim_dynamic_pressure_pa=0.):
    """Static bounded-fin prediction using the unchanged finite panel law.

    This solves only a proposed steady trim. Executed deflections retain their
    lag/rate limits; the accepted prediction is not imposed on physical state.
    """
    z = np.asarray(axis)
    x = np.asarray([1., 0., 0.])-z[0]*z
    if float(x@x) < 1e-8:
        x = np.asarray([0., 1., 0.])-z[1]*z
    x /= np.linalg.norm(x)
    rotation = np.column_stack([x, np.cross(z, x), z])
    speed = max(_length(velocity), 1e-30)
    air = rotation.T@np.asarray(velocity)/speed
    pressure = .5*density*speed*speed
    validation_pressure = max(pressure, minimum_trim_dynamic_pressure_pa)
    base = np.asarray([p.normal_body for p in panels])
    hinges = np.asarray([p.hinge_axis_body for p in panels])
    cross = np.cross(hinges, base)
    parallel = hinges*np.sum(hinges*base, axis=1)[:, None]
    levers = np.asarray([p.position_body_m for p in panels])-com_body_m
    areas = np.asarray([p.area_m2 for p in panels])[:, None]
    normal_coefficient = np.asarray([p.normal_coefficient for p in panels])[:, None]
    tangent_coefficient = np.asarray([p.tangential_coefficient for p in panels])[:, None]
    indices = [i for i, p in enumerate(panels) if p.name.startswith("grid_fin_") and p.max_deflection_rad > 0]
    angles = np.zeros(len(panels))
    def loads(values):
        cosine, sine = np.cos(values)[:, None], np.sin(values)[:, None]
        normals = base*cosine+cross*sine+parallel*(1-cosine)
        vn = (normals@air)[:, None]
        tangent = air[None, :]-normals*vn
        forces = -areas*(normal_coefficient*vn*np.abs(vn)*normals
                          +tangent_coefficient*np.linalg.norm(tangent, axis=1)[:, None]*tangent)
        return forces.sum(axis=0), np.cross(levers, forces).sum(axis=0), forces
    for _ in range(4):
        force, torque, panel_forces = loads(angles)
        if np.max(np.abs(torque))*validation_pressure <= jet_torque_nm*.25:
            break
        columns = []
        for index in indices:
            probe = angles.copy()
            probe[index] += .001
            columns.append((loads(probe)[1]-torque)/.001)
        if not columns:
            break
        changes = np.linalg.lstsq(np.asarray(columns).T, -torque, rcond=1e-6)[0]
        for index, change in zip(indices, changes):
            angles[index] = np.clip(angles[index]+change, -panels[index].max_deflection_rad, panels[index].max_deflection_rad)
    force, torque, panel_forces = loads(angles)
    residual = torque*pressure
    validation_residual = torque*validation_pressure
    feasible = bool(np.max(np.abs(validation_residual)) <= jet_torque_nm)
    return tuple(rotation@(force*pressure)), {
        "steady_trim_feasible": feasible, "predicted_trim_flap_angles_rad": angles.tolist(),
        "predicted_residual_moment_body_nm": residual.tolist(),
        "available_residual_jet_torque_nm": jet_torque_nm,
        "predicted_panel_force_magnitudes_n": (np.linalg.norm(panel_forces, axis=1)*pressure).tolist(),
        "prediction_dynamic_pressure_pa": pressure,
        "trim_validation_dynamic_pressure_pa": validation_pressure,
        "trim_validation_residual_moment_body_nm": validation_residual.tolist(),
        "trim_is_executed_deflection": False}


def _entry_axis(position, velocity, mass, fuel, panels, *, isp_s, previous=None, trim_context=None):
    """Bounded model force allocation, including future site and energy errors.

    Candidate attitudes are predictions only. Their aerodynamic forces are
    scored against a finite-horizon translational demand; actual attitude must
    still be reached and trimmed by the six-DOF controller.
    """
    height = position[2]
    if velocity[2] >= 0:
        return (0., 0., 1.), {"entry_angle_deg": 0., "candidate_count": 1}
    lookahead = 0.
    if height > CONFIG["entry_start_altitude_m"]:
        lookahead = (velocity[2]+math.sqrt(velocity[2]**2+19.6*(height-CONFIG["entry_start_altitude_m"])))/9.8
        position = [position[i]+velocity[i]*lookahead for i in (0, 1)]+[CONFIG["entry_start_altitude_m"]]
        velocity = [velocity[0], velocity[1], velocity[2]-9.8*lookahead]
        height = position[2]
    rho = env.standard_atmosphere(max(0., height))["density_kg_m3"]
    speed = _length(velocity)
    tail = _norm(tuple(-v for v in velocity))
    # Aim at the site over the measured remaining descent timescale. Needed
    # aerodynamic braking reflects the unchanged available rocket delta-v.
    tgo = max(8., min(120., height/max(50., -velocity[2])))
    desired_h = tuple(-2*(position[i]+velocity[i]*tgo)/(tgo*tgo) for i in (0, 1))
    remaining_dv = isp_s*env.STANDARD_GRAVITY_MPS2*math.log(mass/max(1., mass-fuel))
    terminal_speed = max(100., .6*remaining_dv)
    desired_up = 9.8+max(0., (velocity[2]**2-terminal_speed**2)/(2*max(1000., height-1500.)))
    desired = (desired_h[0], desired_h[1], desired_up)
    candidates = [(tail, 0.)]
    heading = np.asarray([desired_h[0], desired_h[1], 0.])
    if np.linalg.norm(heading) < 1e-8:
        heading = np.asarray([velocity[0], velocity[1], 0.])
    if np.linalg.norm(heading) < 1e-8:
        heading = np.asarray([1., 0., 0.])
    heading /= np.linalg.norm(heading)
    angle_candidates = ((10., 20., CONFIG["point_model_entry_max_angle_deg"]) if trim_context is None
                        else (10., 20., 30., 35., 40., 45., CONFIG["entry_max_angle_deg"]))
    for angle in angle_candidates:
        for sign in (-1., 1.):
            direction = sign*heading.copy()
            direction -= float(direction@np.asarray(tail))*np.asarray(tail)
            if np.linalg.norm(direction) < 1e-8:
                continue
            direction /= np.linalg.norm(direction)
            axis = tuple(math.cos(math.radians(angle))*np.asarray(tail)+math.sin(math.radians(angle))*direction)
            candidates.append((axis, angle))
    candidate_loads = []
    for item in candidates:
        if trim_context is None:
            candidate_loads.append((_local_force(velocity, item[0], rho, panels), {}))
        else:
            candidate_loads.append(_trim_entry_candidate(velocity, item[0], rho, panels, **trim_context))
    def score(index):
        item = candidates[index]
        force, trim = candidate_loads[index]
        if trim and not trim["steady_trim_feasible"]:
            return math.inf
        acceleration = tuple(v/mass for v in force)
        # Vertical deficit is an energy/fuel constraint; lateral error is a
        # target constraint. Penalize excess lift rather than assuming no lift.
        error = sum((acceleration[i]-desired[i])**2 for i in (0, 1))
        error += 2*max(0., desired[2]-acceleration[2])**2
        if previous is not None:
            error += .1*sum((item[0][i]-previous[i])**2 for i in range(3))
        return error
    selected = min(range(len(candidates)), key=score)
    axis, angle = candidates[selected]
    selected_force, selected_trim = candidate_loads[selected]
    return axis, {"entry_angle_deg": angle, "candidate_count": len(candidates),
                  "trim_rejected_candidate_count": sum(bool(t) and not t["steady_trim_feasible"] for _, t in candidate_loads),
                  **selected_trim,
                  "attitude_preparation_lookahead_s": lookahead,
                  "desired_aerodynamic_acceleration_enu_mps2": list(desired),
                  "predicted_force_enu_n": list(selected_force),
                  "measured_speed_mps": speed}


def _slew_axis(before, requested, maximum_angle):
    before, requested = _norm(before), _norm(requested)
    cross = env.cross(before, requested)
    angle = math.atan2(_length(cross), max(-1., min(1., env.dot(before, requested))))
    if angle <= maximum_angle:
        return requested
    axis = _norm(cross) if _length(cross) > 1e-12 else (1., 0., 0.)
    return dyn.rotate(dyn.axis_angle(axis, maximum_angle), before)


def _coast_prediction(position, velocity, mass, fuel, panels, *, isp_s, slew_rate_rad_s, entry=True):
    """Small local forward model for commanded alternatives, not verification."""
    p, v = list(position), list(velocity)
    elapsed, last_axis, maximum_q = 0., (0., 0., 1.), 0.
    while p[2] > 1500. and elapsed < CONFIG["prediction_horizon_s"]:
        rho = env.standard_atmosphere(max(0., p[2]))["density_kg_m3"]
        if entry:
            axis, _ = _entry_axis(p, v, mass, fuel, panels, isp_s=isp_s, previous=last_axis)
            axis = _slew_axis(last_axis, axis, slew_rate_rad_s*CONFIG["prediction_dt_s"])
        else:
            axis = (0., 0., 1.)
        force = _local_force(v, axis, rho, panels)
        acceleration = [force[i]/mass-(9.8 if i == 2 else 0.) for i in range(3)]
        dt = CONFIG["prediction_dt_s"]
        for i in range(3):
            p[i] += v[i]*dt+.5*acceleration[i]*dt*dt
            v[i] += acceleration[i]*dt
        maximum_q = max(maximum_q, .5*rho*sum(x*x for x in v))
        elapsed, last_axis = elapsed+dt, axis
    return {"position_enu_m": p, "velocity_enu_mps": v, "elapsed_s": elapsed, "max_dynamic_pressure_pa": maximum_q}


def _capture_reserve(mass, profile, catch_config):
    """Contact approach budget, using the same exposed assumptions as arrival."""
    horizon = ((catch_config["initial_pin_clearance_m"]+catch_config["arm_half_width_m"])
               /(-.5*catch_config["initial_vertical_speed_mps"])
               +4*profile["actuators"]["throttle_tau_s"])
    return mass*9.81/(profile["booster"]["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2)*horizon


def _cutoff_admission(prediction, position):
    """Distinguish predicted capture from a geographic best-effort cutoff.

    This is a planning decision. It grants no authority and never substitutes
    for the independent measured-arrival and contact gates. The historical
    intercept fallback remains an explicitly unadmitted development trial.
    """
    reasons = []
    if prediction["termination"] != "catch_handoff" or not prediction["terminal_handoff_eligible"]:
        reasons.append("predicted_arrival_gate_not_reached")
    if not prediction["terminal_fuel_reserve_met"]:
        reasons.append("predicted_contact_propellant_reserve_not_met")
    along = sum(prediction["position_enu_m"][i]*position[i] for i in (0, 1))/max(1., _length(position[:2]))
    geographic = bool(prediction["termination"] != "rate_settle_failed"
                      and (along <= 0 or _length(prediction["position_enu_m"][:2]) <= 100.))
    admitted = not reasons
    return {"schema": "missionos.starship_capture_plan_admission.v1",
            "capture_plan_admissible": admitted, "reasons": reasons,
            "geographic_intercept_predicted": geographic,
            "best_effort_cutoff_requested": bool(geographic and not admitted),
            "cutoff_requested": bool(admitted or geographic),
            "prediction_is_execution": False, "dispatch_authority_created": False}


def _plan_boostback(position, velocity, mass, fuel, booster, profile, catch_config, *, development_candidate_index=None):
    """Optimize command-space intercept alternatives under unchanged physics.

    A finite set of post-burn vertical velocity commands is evaluated, with
    horizontal velocity solved by a forward-model impact correction. This is
    onboard planning, not a sweep over vehicle coefficients or observed data.
    """
    exhaust = profile["booster"]["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2
    dry = mass-fuel
    candidates = []
    for target_vertical in (200., 400., 600., 800., 1000.):
        fall_time = (target_vertical+math.sqrt(target_vertical**2+19.6*position[2]))/9.8
        horizontal = [-position[i]/max(30., fall_time) for i in (0, 1)]
        remaining_mass, prediction = mass, None
        for _ in range(3):
            difference = [horizontal[i]-velocity[i] for i in (0, 1)]+[target_vertical-velocity[2]]
            estimated_burn = _length(difference)/max(1., .4*33*profile["booster"]["engine_thrust_n"]/mass)
            difference[2] += 9.8*estimated_burn
            remaining_mass = mass*math.exp(-_length(difference)/exhaust)
            remaining_fuel = max(0., remaining_mass-dry)
            post_position = [position[i]+.5*(velocity[i]+horizontal[i])*estimated_burn for i in (0, 1)]
            post_position.append(position[2]+.5*(velocity[2]+target_vertical)*estimated_burn)
            prediction = _coast_prediction(post_position, [*horizontal, target_vertical], remaining_mass, remaining_fuel, booster.aero_panels, isp_s=profile["booster"]["engine_isp_s"], slew_rate_rad_s=profile["guidance"]["max_angular_acceleration_rad_s2"]/profile["guidance"]["attitude_frequency_rad_s"], entry=True)
            for i in (0, 1):
                horizontal[i] -= prediction["position_enu_m"][i]/max(30., prediction["elapsed_s"])
        speed = _length(prediction["velocity_enu_mps"])
        after_stop = remaining_mass*math.exp(-(speed+60.)/exhaust)-dry
        contact_reserve = _capture_reserve(dry+max(0., after_stop), profile, catch_config)
        candidates.append({"target_velocity_enu_mps": [*horizontal, target_vertical],
                           "predicted_post_burn_fuel_kg": remaining_mass-dry,
                           "predicted_point_model_after_arrest_fuel_kg": after_stop,
                           "point_model_contact_propellant_reserve_kg": contact_reserve,
                           "point_model_contact_budget_met": bool(after_stop >= contact_reserve),
                           "prediction": prediction})
    feasible = [c for c in candidates if c["point_model_contact_budget_met"]]
    chosen = max(feasible or candidates, key=lambda c: c["predicted_point_model_after_arrest_fuel_kg"]
                 -c["point_model_contact_propellant_reserve_kg"])
    if development_candidate_index is not None:
        chosen = candidates[development_candidate_index]
    return {"method": "finite_command_candidates_and_point_model_intercept", "candidates": candidates,
            "selected": chosen, "prediction_is_execution": False,
            "selection_rule": ("explicit_development_candidate" if development_candidate_index is not None
                               else "contact_budget_first_else_best_effort_surplus"),
            **({"development_candidate_index": development_candidate_index} if development_candidate_index is not None else {}),
            "contact_budget_candidate_count": len(feasible),
            "selected_contact_budget_met": chosen["point_model_contact_budget_met"],
            "limitations": "Local frame, slew-limited axis without attitude dynamics, no trim or finite burn integration in coarse planning; actual six-DOF outcome is independent of this estimate."}


def _predict_cutoff(state, booster, profile, catch_config, *, full=False, reference=None, remaining_duration_s=None):
    """Propagate a cloned policy and physical state; never overwrite execution.

    The full forecast uses the exact powered-settle/coast/landing phase machine,
    including rate-settle failure, finite shutdown and carried roll reference.
    The inexpensive point forecast is explicitly only a coarse screening model.
    """
    if full:
        forecast = simulate_recovery(profile, asdict(state), catch_config,
                                     _initial_phase="recovery_powered_rate_settle", _forecast=True,
                                     _reference=deepcopy(reference), duration_s=remaining_duration_s)
        endpoint = dyn.state_from_dict(forecast["final_state"])
        up, east, north, velocity, displacement, _ = _navigation(endpoint, profile)
        settle = next((e for e in forecast["events"] if e["event"] == "powered_rate_settled_cutoff"), None)
        return {"position_enu_m": [-env.dot(displacement, east), -env.dot(displacement, north), forecast["outcome"]["final_altitude_m"]],
                "velocity_enu_mps": [env.dot(velocity, a) for a in (east, north, up)],
                "elapsed_s": endpoint.time_s-state.time_s, "terminal_propellant_kg": endpoint.propellant_kg,
                "termination": forecast["outcome"]["termination"], "model": "same_finite_6dof_policy_continuation",
                "terminal_ground_speed_mps": forecast["outcome"]["final_ground_speed_mps"],
                "terminal_handoff_eligible": forecast["recovery_record"]["handoff"]["eligible"],
                "terminal_propellant_reserve_kg": forecast["recovery_record"]["handoff"]["limits"]["propellant_reserve_kg"],
                "terminal_fuel_reserve_met": bool(endpoint.propellant_kg >= forecast["recovery_record"]["handoff"]["limits"]["propellant_reserve_kg"]),
                "predicted_settle_propellant_kg": settle["remaining_propellant_kg"] if settle else None,
                "finite_settle_elapsed_s": settle["time_s"]-state.time_s if settle else None,
                "prediction_is_execution": False, "remaining_duration_s": remaining_duration_s}
    observed = dyn.observe(state, booster)
    up, east, north, velocity, displacement, _ = _navigation(state, profile)
    position = [-env.dot(displacement, east), -env.dot(displacement, north), observed["altitude_m"]]
    vlocal = [env.dot(velocity, a) for a in (east, north, up)]
    prediction = _coast_prediction(position, vlocal, observed["mass_kg"], state.propellant_kg,
                                   booster.aero_panels, isp_s=profile["booster"]["engine_isp_s"],
                                   slew_rate_rad_s=profile["guidance"]["max_angular_acceleration_rad_s2"]/profile["guidance"]["attitude_frequency_rad_s"])
    prediction.update(model="point_model_coarse_without_finite_shutdown", prediction_is_execution=False)
    return prediction


def _vector_burn_preview(state, booster, observed, profile, maximum_count=13):
    """Local vector burn estimate, not the executed six-DOF trajectory.

    The measured equivalent drag area is frozen and thrust is ideally aligned
    against velocity. Height/density and gravity use the vertical coordinate,
    never total path length. Finite aggregate spool and fuel budget remain in
    this approximation; actual attitude and lift are handled by execution.
    """
    up, east, north, velocity, _, _ = _navigation(state, profile)
    v = [env.dot(velocity, a) for a in (east, north, up)]
    initial_velocity = list(v)
    height = observed["altitude_m"]
    initial_height = height
    speed = env.norm(v)
    rho = observed["atmosphere"]["density_kg_m3"]
    aero = dyn.rotate(state.q_body_to_eci, observed["aero_force_body_n"])
    drag = max(0.0, -env.dot(aero, velocity) / max(speed, 1e-30))
    area = (
        2 * drag / (rho * speed**2)
        if rho > 1e-12 and speed > 10
        else math.pi * profile["geometry"]["radius_m"] ** 2 * profile["aero"]["axial_cd"]
    )
    selected = [
        (i, e)
        for i, e in enumerate(booster.engines[:maximum_count])
        if state.engine_states[i].available
    ]
    available = sum(e.max_thrust_n for _, e in selected)
    fuel = initial_fuel = state.propellant_kg
    dry = observed["mass_kg"] - fuel
    throttle = sum(e.max_thrust_n * state.engine_states[i].throttle for i, e in selected) / max(
        available, 1.0
    )
    tau = max((e.throttle_time_constant_s for _, e in selected), default=1.0)
    rate = min((e.throttle_rate_per_s for _, e in selected), default=1.0)
    flow = sum(e.max_thrust_n / (e.isp_s * env.STANDARD_GRAVITY_MPS2) for _, e in selected)
    elapsed = 0.0
    displacement = [0.0, 0.0]
    steps = 0
    while elapsed < 100.0 and available > 0 and fuel > 1e-6 and env.norm(v) > 2.0 and steps < 10000:
        speed = env.norm(v)
        mass = dry + fuel
        density = env.standard_atmosphere(max(0.0, height))["density_kg_m3"]
        gravity = env.EARTH_MU_M3_S2 / (env.EARTH_EQUATORIAL_RADIUS_M + max(0.0, height)) ** 2
        maximum_acceleration = (
            available / mass + 0.5 * density * area * speed * speed / mass + gravity
        )
        dt = min(0.1, fuel / max(flow, 1.0), 0.2 * speed / max(maximum_acceleration, 1.0))
        throttle = min(1.0, throttle + min(rate, (1.0 - throttle) / tau) * dt)
        deceleration = available * throttle / mass + 0.5 * density * area * speed * speed / mass
        acceleration = [-deceleration * a / speed for a in v]
        acceleration[2] -= gravity
        after = [v[i] + acceleration[i] * dt for i in range(3)]
        for i in (0, 1):
            displacement[i] += 0.5 * (v[i] + after[i]) * dt
        height += 0.5 * (v[2] + after[2]) * dt
        fuel = max(0.0, fuel - flow * throttle * dt)
        elapsed += dt
        v = after
        steps += 1
    return {
        "estimated_stopping_height_m": initial_height - height,
        "estimated_burn_time_s": elapsed,
        "estimated_burn_propellant_kg": initial_fuel - fuel,
        "estimated_drag_area_m2": area,
        "burn_fuel_feasible": env.norm(v) <= 2.0,
        "available_landing_engine_count": len(selected),
        "available_landing_thrust_n": available,
        "initial_velocity_enu_mps": initial_velocity,
        "estimated_final_velocity_enu_mps": v,
        "estimated_horizontal_displacement_m": displacement,
        "steps": steps,
        "estimated_final_speed_mps": env.norm(v),
        "estimate_exhausted_available_fuel": bool(fuel <= 1e-6),
        "estimated_propellant_is_required_amount": env.norm(v) <= 2.0,
        "preview_assumption": "local_3d_velocity_vertical_gravity_frozen_measured_drag_area_ideal_thrust_alignment_spool_and_fuel",
        "prediction_is_execution": False,
    }

def _tower_observation(state, booster, profile, catch_config, *, return_site=None):
    from .starship_booster_catch import contact_frame
    if return_site is not None:
        from .starship_return_sites import validate_return_site
        validate_return_site(return_site, profile, catch_config, terminal_goal="surrogate_pin_support")
    frame = contact_frame(state, booster, profile, catch_config, state.time_s, authorized=False)
    origin = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], time_s=state.time_s)
    up, east, _ = env.local_frame(origin)
    tilt = math.degrees(math.acos(max(-1., min(1., env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), up)))))
    roll = math.degrees(math.acos(max(-1., min(1., env.dot(dyn.rotate(state.q_body_to_eci, (1., 0., 0.)), east)))))
    points = frame["pins"]
    midpoint = [sum(p["position_enu_m"][i] for p in points)/2 for i in range(3)]
    clearance = [p["position_enu_m"][2]-catch_config["support_height_m"] for p in points]
    # Fuel to remain powered through the slowest allowed contact approach and
    # four throttle time constants. It is not the initialized fixture's fuel.
    horizon = (catch_config["initial_pin_clearance_m"]+catch_config["arm_half_width_m"])/(-.5*catch_config["initial_vertical_speed_mps"])+4*profile["actuators"]["throttle_tau_s"]
    reserve = frame["mass_kg"]*9.81/(profile["booster"]["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2)*horizon
    lever = max(abs(p[2]-frame["com_body_m"][2]) for p in catch_config["support_points_body_m"])
    angle_limit = math.degrees(math.atan((catch_config["arm_half_width_m"]/2)/lever))
    limits = {"horizontal_position_m": catch_config["arm_half_width_m"]/4,
              "pin_clearance_min_m": catch_config["initial_pin_clearance_m"],
              "pin_clearance_max_m": catch_config["initial_pin_clearance_m"]+catch_config["arm_half_width_m"],
              "pin_vertical_speed_min_mps": 3*catch_config["initial_vertical_speed_mps"],
              "pin_vertical_speed_max_mps": .5*catch_config["initial_vertical_speed_mps"],
              "pin_horizontal_speed_mps": 5*catch_config["settle_pin_speed_mps"],
              "body_rate_rad_s": catch_config["settle_body_rate_rad_s"], "attitude_angle_deg": angle_limit,
              "propellant_reserve_kg": reserve, "reserve_horizon_s": horizon}
    eligible = (_length(midpoint[:2]) <= limits["horizontal_position_m"]
                and all(limits["pin_clearance_min_m"] <= h <= limits["pin_clearance_max_m"] for h in clearance)
                and all(limits["pin_vertical_speed_min_mps"] <= p["relative_velocity_enu_mps"][2] <= limits["pin_vertical_speed_max_mps"] for p in points)
                and all(_length(p["relative_velocity_enu_mps"][:2]) <= limits["pin_horizontal_speed_mps"] for p in points)
                and tilt <= angle_limit and roll <= angle_limit
                and _length(state.omega_body_rad_s) <= limits["body_rate_rad_s"] and state.propellant_kg >= reserve)
    return {"time_s": state.time_s, "position_error_enu_m": midpoint[:2]+[midpoint[2]-catch_config["support_height_m"]],
            "pin_height_above_support_m": clearance, "pins": points, "com_rate_body_mps": frame["com_rate_body_mps"],
            "tilt_deg": tilt, "body_x_east_angle_deg": roll, "body_rate_rad_s": _length(state.omega_body_rad_s),
            "propellant_kg": state.propellant_kg, "mass_kg": frame["mass_kg"], "com_body_m": frame["com_body_m"],
            "limits": limits, "eligible": bool(eligible)}


def surface_return_observation(state, booster, profile, *, return_site):
    """Measured surface-target CG and cylinder-envelope material kinematics.

    The target tangent height and the hull's ellipsoid level-set clearance are
    deliberately separate. No offshore tower, contact response or landing
    success is introduced. This function does not advance a physical state.
    """
    from .starship_sixdof_contact import hull_clearance
    from .starship_terminal_guidance import surface_terminal_geometry
    terminal = surface_terminal_geometry(state, profile, return_site=return_site)
    observed = dyn.observe(state, booster, atmosphere=False)
    # Normalize accepted quaternion roundoff only for this geometry query;
    # retain the actual input state and its signed digest without alteration.
    geometry_state = replace(state, q_body_to_eci=tuple(terminal["geometry_q_body_to_eci"]))
    geometry = hull_clearance(geometry_state, booster,
        profile["geometry"]["booster_length_m"], profile["geometry"]["radius_m"])
    axes = tuple(tuple(terminal["target_frame"][key]) for key in ("east_eci", "north_eci", "up_eci"))
    def local(vector):
        return [float(env.dot(vector, axis)) for axis in axes]
    lever = tuple(geometry["point_body_m"][i]-observed["com_body_m"][i] for i in range(3))
    material_body_velocity = env.add(env.cross(state.omega_body_rad_s, lever), env.scale(observed["com_rate_body_mps"], -1.))
    material_velocity = env.add(state.v_eci_mps, dyn.rotate(geometry_state.q_body_to_eci, material_body_velocity))
    ground_velocity = env.add(material_velocity,
        env.scale(env.cross((0., 0., env.EARTH_ROTATION_RAD_S), geometry["point_eci_m"]), -1.))
    position = local(tuple(geometry["point_eci_m"][i]-terminal["target_frame"]["origin_eci_m"][i] for i in range(3)))
    return {"schema": "missionos.starship_surface_return_observation.v1", "time_s": state.time_s,
        "site_id": return_site.site_id, "terminal_goal": "model_surface_contact",
        "return_site_sha256": return_site.sha256, "profile_sha256": terminal["profile_sha256"],
        "state_sha256": terminal["state_sha256"], "terminal_geometry": terminal,
        "propellant_kg": state.propellant_kg, "mass_kg": observed["mass_kg"],
        "com_body_m": list(observed["com_body_m"]), "com_rate_body_mps": list(observed["com_rate_body_mps"]),
        "hull_geometry": geometry, "material_point_lever_body_m": list(lever),
        "material_point_position_enu_m": position, "material_point_ground_velocity_enu_mps": local(ground_velocity),
        "material_point_ground_speed_mps": env.norm(ground_velocity), "state_assigned": False,
        "capture_allowed": False, "contact_or_support_verified": False, "safe_landing_verified": False}


def simulate_recovery(profile, initial_state, catch_config, *, duration_s=None, _initial_phase=None, _forecast=False, _reference=None,
                      _development_cutoff_time_s=None, _development_fin_allocation=False,
                      _development_fin_scope="coast_and_landing", _development_landing_probe_time_s=None,
                      _landing_context=None, _landing_delay_s=None, _development_boostback_candidate_index=None):
    from .starship_sixdof_mission import _attitude, vehicle
    from .starship_sixdof_contact import find_contact, hull_clearance
    from .starship_attitude_reference import ConditionedGeographicFrame
    from . import starship_landing_context as landing_context
    state = dyn.state_from_dict(initial_state)
    booster = vehicle(profile, "booster")
    configuration = _configuration(profile)
    dyn.observe(state, booster)
    duration = CONFIG["maximum_duration_s"] if duration_s is None else duration_s
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 1200:
        raise ValueError("invalid_recovery_duration")
    if _initial_phase not in (None, "recovery_entry_coast", "recovery_powered_rate_settle") or type(_forecast) is not bool:
        raise ValueError("invalid_internal_recovery_forecast")
    if (_initial_phase is not None or _reference is not None) and not _forecast:
        raise ValueError("initial_phase_override_requires_forecast")
    if _development_cutoff_time_s is not None and (
            type(_development_cutoff_time_s) not in (int, float) or not math.isfinite(_development_cutoff_time_s)
            or not state.time_s < _development_cutoff_time_s < state.time_s+duration
            or _forecast or _initial_phase is not None or _reference is not None):
        raise ValueError("invalid_development_cutoff")
    if type(_development_fin_allocation) is not bool or (_development_fin_allocation and _development_cutoff_time_s is None):
        raise ValueError("development_fin_allocation_requires_scheduled_experiment")
    if (_development_fin_scope not in ("coast_and_landing", "coast_only")
            or not _development_fin_allocation and _development_fin_scope != "coast_and_landing"):
        raise ValueError("invalid_development_fin_scope")
    if _development_boostback_candidate_index is not None and (
            type(_development_boostback_candidate_index) is not int or not 0 <= _development_boostback_candidate_index <= 4
            or _development_cutoff_time_s is None or _forecast or _development_fin_allocation
            or _development_landing_probe_time_s is not None or _landing_context is not None):
        raise ValueError("invalid_development_boostback_candidate")
    if _development_landing_probe_time_s is not None and (
            type(_development_landing_probe_time_s) not in (int, float) or not math.isfinite(_development_landing_probe_time_s)
            or _development_cutoff_time_s is None or _forecast or _development_fin_allocation
            or not _development_cutoff_time_s < _development_landing_probe_time_s < state.time_s+duration):
        raise ValueError("invalid_development_landing_probe")
    forecast_request = None
    if _landing_context is not None:
        if (not _forecast or _reference is not None or _initial_phase is not None
                or _development_cutoff_time_s is not None or _development_landing_probe_time_s is not None):
            raise ValueError("landing_context_requires_forecast")
        forecast_request = landing_context.request(_landing_context, profile, catch_config, _landing_delay_s, duration)
        if landing_context.digest(asdict(state)) != landing_context.digest(_landing_context["state"]):
            raise ValueError("landing_context_state_mismatch")
    elif _landing_delay_s is not None:
        raise ValueError("landing_delay_requires_context")
    if state.time_s+duration <= state.time_s or state.time_s+.1 <= state.time_s:
        raise ValueError("recovery_clock_must_advance")
    start, phase = state.time_s, "recovery_entry_coast" if _landing_context is not None else _initial_phase or "recovery_boostback_slew"
    samples, events, checkpoints = [], [], []
    plan, burn_start, settle_start, settle_target = None, None, None, None
    planned_after_slew, cutoff_prediction, cutoff_admission = False, None, None
    full_prediction_count, prediction_receipts = 0, []
    next_full_prediction = start
    termination, contact, handoff = "time_limit", None, None
    steps, next_sample, landing_stage = 0, start, 13
    reference = _reference or ConditionedGeographicFrame(state.q_body_to_eci, state.time_s,
                                           maximum_roll_rate_rad_s=profile["guidance"]["attitude_frequency_rad_s"])
    if phase == "recovery_powered_rate_settle":
        settle_start, settle_target = start, state.q_body_to_eci
    previous_axis, next_entry_update, entry_axis, entry_diagnostic = None, start, (0., 0., 1.), {}
    if _landing_context is not None:
        ctx, ref = deepcopy(_landing_context["context"]), _landing_context["context"]["reference"]
        previous_axis, entry_axis = ctx["previous_axis"], ctx["entry_axis"]
        next_entry_update, entry_diagnostic = ctx["next_entry_update_s"], ctx["entry_diagnostic"]
        reference = ConditionedGeographicFrame(ref["quaternion"], ref["time_s"], maximum_roll_rate_rad_s=ref["maximum_roll_rate_rad_s"])
        # Restore controller reference memory exactly. This is not the vehicle
        # attitude; physical q/omega/actuators remain the inherited state.
        reference.frame.quaternion, reference.bridging = tuple(ref["quaternion"]), ref["bridging"]
    probe_snapshot = None
    def event(name, **fields):
        if _development_cutoff_time_s is not None:
            fields["state"] = asdict(state)
        events.append({"time_s": state.time_s, "event": name, **fields})
    event("booster_return_start", detail=("Forecast-only continuation from captured physical/controller context; no execution."
          if _landing_context is not None else "Exact launch-derived separation; predictive development guidance, no state reset."))
    while state.time_s < start+duration-1e-9:
        if (_development_landing_probe_time_s is not None and probe_snapshot is None
                and state.time_s >= _development_landing_probe_time_s-1e-9
                and state.time_s >= next_sample-1e-9 and phase == "recovery_entry_coast"):
            probe_snapshot = landing_context.capture(asdict(state), reference, profile, catch_config,
                deadline_s=start+duration, previous_axis=previous_axis, entry_axis=entry_axis,
                next_entry_update_s=next_entry_update, entry_diagnostic=entry_diagnostic)
        observed = dyn.observe(state, booster)
        up, east, north, velocity, displacement, distance = _navigation(state, profile)
        position = [-env.dot(displacement, east), -env.dot(displacement, north), observed["altitude_m"]]
        vlocal = [env.dot(velocity, a) for a in (east, north, up)]
        mass, fuel = observed["mass_kg"], state.propellant_kg
        gravity = env.EARTH_MU_M3_S2/_length(state.r_eci_m)**2
        target_axis, throttle, count = up, 0., 0
        diagnostic = {"return_site_distance_m": distance, "vertical_speed_mps": vlocal[2], "guidance_origin": POLICY_ID}
        control_profile = profile
        if phase.startswith("recovery_boostback"):
            if plan is None:
                plan = _plan_boostback(position, vlocal, mass, fuel, booster, profile, catch_config,
                                      development_candidate_index=_development_boostback_candidate_index)
                event("predictive_boostback_plan", plan=plan)
            desired = plan["selected"]["target_velocity_enu_mps"]
            error = [desired[i]-vlocal[i] for i in range(3)]
            # Gravity is included in the requested acceleration, not hidden as
            # an upward energy bias. Predictive target includes vertical speed.
            requested = [error[i]/10. for i in range(3)]
            requested[2] += gravity
            target_axis = tuple(sum(requested[j]*a[j] for j in range(3)) for a in zip(east, north, up))
            target_axis = _norm(target_axis)
            aligned = env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), target_axis) >= math.cos(math.radians(CONFIG["boostback_alignment_deg"]))
            predictive_cut = False
            if aligned and not planned_after_slew:
                plan = _plan_boostback(position, vlocal, mass, fuel, booster, profile, catch_config,
                                      development_candidate_index=_development_boostback_candidate_index)
                planned_after_slew = True
                event("predictive_boostback_plan_refreshed", plan=plan)
            if (_development_cutoff_time_s is None and burn_start is not None and state.time_s-burn_start > 5.
                    and vlocal[0]*position[0]+vlocal[1]*position[1] < 0
                    and state.time_s >= next_full_prediction
                    and full_prediction_count < CONFIG["maximum_full_coast_predictions"]):
                # A coarse intercept must not exclude a feasible finite-dynamics
                # continuation. Screen every two seconds once heading home.
                cutoff_prediction = _predict_cutoff(state, booster, profile, catch_config, full=True,
                                                    reference=reference, remaining_duration_s=start+duration-state.time_s)
                full_prediction_count += 1
                next_full_prediction = state.time_s+2.
                cutoff_admission = _cutoff_admission(cutoff_prediction, position)
                prediction_receipts.append({"time_s": state.time_s, "prediction": cutoff_prediction,
                                            "origin_state": asdict(state),
                                            "origin_position_enu_m": list(position), "admission": cutoff_admission})
                predictive_cut = cutoff_admission["cutoff_requested"]
                diagnostic["cutoff_prediction"] = cutoff_prediction
                diagnostic["capture_plan_admission"] = cutoff_admission
            scheduled_cut = (_development_cutoff_time_s is not None and burn_start is not None
                             and state.time_s >= _development_cutoff_time_s-1e-9)
            if fuel <= configuration["landing_reserve_kg"] or (burn_start is not None and (scheduled_cut or predictive_cut or state.time_s-burn_start >= configuration["boostback_max_burn_s"])):
                phase, settle_start, settle_target = "recovery_powered_rate_settle", state.time_s, state.q_body_to_eci
                event("boostback_complete_rate_settle", remaining_propellant_kg=fuel, velocity_error_mps=_length(error),
                      cutoff_basis=("development_scheduled_cutoff" if scheduled_cut else
                                    ("admitted_capture_prediction" if cutoff_admission["capture_plan_admissible"]
                                     else "unadmitted_geographic_best_effort") if predictive_cut else "fuel_or_time_guard"),
                      predicted_handoff_eligible=bool(cutoff_prediction and cutoff_prediction.get("terminal_handoff_eligible")))
            elif aligned:
                if burn_start is None:
                    burn_start = state.time_s
                    event("boostback_ignition", requested_engine_count=33)
                phase, count = "recovery_boostback_burn", 33
                available = sum(e.max_thrust_n for i, e in enumerate(booster.engines[:count]) if state.engine_states[i].available)
                throttle = max(.4, min(1., mass*_length(requested)/available))
                count, throttle = _landing_engine_demand(state, booster, mass*_length(requested), 33)
            else:
                count, throttle = 3, .4
        if phase == "recovery_powered_rate_settle":
            count, throttle = 3, .4
            target_axis = dyn.rotate(settle_target, (0., 0., 1.))
            if _length(state.omega_body_rad_s) < CONFIG["rate_settle_limit_rad_s"]:
                phase, count, throttle = "recovery_entry_coast", 0, 0.
                event("powered_rate_settled_cutoff", body_rate_rad_s=_length(state.omega_body_rad_s), remaining_propellant_kg=fuel)
            elif state.time_s-settle_start > CONFIG["rate_settle_max_s"]:
                termination = "rate_settle_failed"
                break
        if phase == "recovery_entry_coast":
            if state.time_s >= next_entry_update:
                requested_axis, entry_diagnostic = _entry_axis(position, vlocal, mass, fuel, booster.aero_panels, isp_s=profile["booster"]["engine_isp_s"], previous=previous_axis,
                                                               trim_context={"com_body_m": observed["com_body_m"],
                                                                             "jet_torque_nm": 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"],
                                                                             "minimum_trim_dynamic_pressure_pa": profile["guidance"]["max_q_pa"]})
                rate = profile["guidance"]["max_angular_acceleration_rad_s2"]/profile["guidance"]["attitude_frequency_rad_s"]
                entry_axis = _slew_axis(previous_axis or (0., 0., 1.), requested_axis, rate)
                previous_axis, next_entry_update = entry_axis, state.time_s+1.
            target_axis = tuple(sum(entry_axis[j]*a[j] for j in range(3)) for a in zip(east, north, up))
            diagnostic.update(entry_diagnostic)
            control_profile, frequency, alpha = _coast_control_profile(profile, observed, include_spooled_tvc=True)
            if observed["dynamic_pressure_pa"] > 100:
                control_profile = profile
            diagnostic.update(coast_control_frequency_rad_s=frequency, coast_angular_acceleration_limit_rad_s2=alpha)
            if vlocal[2] < 0:
                preview = _vector_burn_preview(state, booster, observed, profile, 13)
                diagnostic["braking_prediction"] = preview
                trigger = observed["altitude_m"] <= preview["estimated_stopping_height_m"]+500.+catch_config["support_height_m"]
                if _landing_context is not None and _landing_delay_s is not None:
                    trigger = state.time_s >= start+_landing_delay_s-1e-9
                if trigger:
                    phase = "recovery_landing_13"
                    event("landing_stage_requested", requested_engine_count=13, prediction=preview)
        if phase.startswith("recovery_landing"):
            observation = _tower_observation(state, booster, profile, catch_config)
            clear = min(observation["pin_height_above_support_m"])
            horizontal_error = observation["position_error_enu_m"][:2]
            braking_speed = _length(vlocal)
            if landing_stage == 13 and braking_speed < 100.:
                landing_stage = 5
                event("landing_stage_requested", requested_engine_count=5)
            if landing_stage == 5 and braking_speed < 20.:
                landing_stage = 3
                event("landing_stage_requested", requested_engine_count=3)
            phase = "recovery_landing_"+str(landing_stage)
            desired_vertical = -max(1.5, min(100., max(0., clear)/6.))
            vertical_accel = max(.5, gravity+(desired_vertical-vlocal[2])/1.5)
            tau = CONFIG["terminal_position_tau_s"]
            horizontal_accel = [-horizontal_error[i]/(tau*tau)-2*vlocal[i]/tau for i in (0, 1)]
            max_tilt = CONFIG["braking_max_tilt_deg"] if landing_stage == 13 else 30. if landing_stage == 5 else 15.
            limit = vertical_accel*math.tan(math.radians(max_tilt))
            ratio = min(1., limit/max(_length(horizontal_accel), 1e-30))
            local_accel = [a*ratio for a in horizontal_accel]+[vertical_accel]
            target_axis = _norm(tuple(sum(local_accel[j]*a[j] for j in range(3)) for a in zip(east, north, up)))
            alignment = max(.2, env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), target_axis))
            count = landing_stage
            available = sum(e.max_thrust_n for i, e in enumerate(booster.engines[:count]) if state.engine_states[i].available)
            throttle = max(.4, min(1., mass*_length(local_accel)/max(1., available*alignment)))
            count, throttle = _landing_engine_demand(state, booster, mass*_length(local_accel)/alignment, landing_stage)
            diagnostic["arrival_observation"] = observation
            if observation["eligible"]:
                termination = "catch_handoff"
                handoff = {"eligible": True, "time_s": state.time_s, "state": asdict(state), "observation": observation, "limits": observation["limits"]}
                break
        preferred = _attitude(target_axis, east)
        target = state.q_body_to_eci if phase == "recovery_powered_rate_settle" else reference.target(target_axis, east, preferred, time_s=state.time_s)
        if phase == "recovery_entry_coast" and observed["dynamic_pressure_pa"] <= 100.:
            command, control_diagnostic = control_coast_stopping_distance(state, booster, target, profile, control_interval_s=.25)
        else:
            command, control_diagnostic = control_with_measured_tvc(state, booster, target, throttle, count, control_profile,
                use_flaps=phase in ("recovery_entry_coast", "recovery_landing_13", "recovery_landing_5", "recovery_landing_3"),
                development_fin_allocation=(_development_fin_allocation and
                    (_development_fin_scope == "coast_and_landing" or
                     phase == "recovery_entry_coast" and observed["dynamic_pressure_pa"] > 100.)),
                control_interval_s=min(.1 if observed["altitude_m"] < 100_000 or count else .25, start+duration-state.time_s),
                trim_angles_rad=(entry_diagnostic.get("predicted_trim_flap_angles_rad") if phase == "recovery_entry_coast" else None))
        diagnostic.update(control_diagnostic)
        if state.time_s >= next_sample-1e-9:
            sample = _sample_booster(state, booster, phase, command, diagnostic)
            samples.append(sample)
            checkpoints.append({"time_s": state.time_s, "phase": phase, "state": asdict(state), "command": asdict(command),
                                "com_rate_body_mps": list(observed["com_rate_body_mps"]), "navigation": diagnostic})
            # Changed start-time forecasts retain every macroscopic integration
            # step. Fuel-cutoff acceleration can reverse inside a sparse .5 s
            # interval even when its net delta-v is small. Keep the legacy
            # forecast cadence for exact historical-suffix comparison.
            sample_interval = (.1 if _development_boostback_candidate_index is not None
                               or _landing_context is not None and _landing_delay_s is not None else
                               5. if _forecast and _landing_context is None else CONFIG["sample_interval_s"])
            next_sample = state.time_s+sample_interval
        if _length(state.omega_body_rad_s) > 5:
            termination = "angular_rate_envelope_exceeded"
            break
        step = min(.1 if observed["altitude_m"] < 100_000 or count else .25, start+duration-state.time_s)
        if observed["altitude_m"] < 300.+_length(velocity)*step:
            state, contact = find_contact(state, booster, command, step, profile["geometry"]["booster_length_m"], profile["geometry"]["radius_m"])
            if contact["contact"]:
                termination = "surface_contact"
                steps += 1
                break
            contact = None
        else:
            state = dyn.step(state, booster, command, step)
        steps += 1
    final = _sample_booster(state, booster, phase)
    final["contact"] = contact is not None
    if not samples or samples[-1]["time_s"] != state.time_s:
        samples.append(final)
        checkpoints.append({"time_s": state.time_s, "phase": phase, "state": asdict(state), "command": None,
                            "com_rate_body_mps": final["com_rate_body_mps"], "navigation": {}})
    else:
        samples[-1] = final
    arrival = _tower_observation(state, booster, profile, catch_config)
    if handoff is None:
        handoff = {"eligible": False, "time_s": state.time_s, "state": None, "observation": arrival, "limits": arrival["limits"]}
    event(termination, detail="Measured development recovery result; no capture or physical-execution claim.")
    distance = _navigation(state, profile)[-1]
    vehicle_up = _navigation(state, profile)[0]
    vehicle_tilt = math.degrees(math.acos(max(-1., min(1., env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), vehicle_up)))))
    configuration = _configuration(profile)
    envelope = (contact is not None and not contact["initial_overlap"] and contact["surface_normal_speed_mps"] <= 0
                and contact["surface_relative_speed_mps"] <= configuration["contact_speed_limit_mps"]
                and vehicle_tilt <= configuration["contact_tilt_limit_deg"]
                and _length(state.omega_body_rad_s) <= configuration["contact_rate_limit_rad_s"]
                and distance <= configuration["return_site_tolerance_m"])
    clearance = hull_clearance(state, booster, profile["geometry"]["booster_length_m"], profile["geometry"]["radius_m"])["signed_clearance_m"]
    return {"scenario": "booster_return", "body_id": "booster", "samples": samples, "events": events,
            "booster_separation_state": initial_state, "initial_state": samples[0], "final_state": asdict(state),
            "final_vehicle": asdict(booster), "contact": contact, "guidance_configuration": configuration, "guidance_policy": POLICY_ID,
            "recovery_record": {"schema": "missionos.starship_booster_recovery.v1", "policy_id": POLICY_ID,
                                "input_separation_state": initial_state, "guidance_configuration": dict(CONFIG),
                                "checkpoints": checkpoints, "boostback_plan": plan, "handoff": handoff,
                                "requested_duration_s": duration_s, "resolved_duration_s": duration,
                                "full_coast_prediction_count": full_prediction_count, "cutoff_predictions": prediction_receipts, "forecast_only": _forecast,
                                **({"development_landing_probe": {"schema": "missionos.starship_landing_probe.v1",
                                    "requested_time_s": _development_landing_probe_time_s, "snapshot": probe_snapshot,
                                    "production_policy_admitted": False}} if _development_landing_probe_time_s is not None else {}),
                                **({"landing_start_forecast": forecast_request} if _landing_context is not None else {}),
                                **({"development_boostback_candidate": {
                                    "schema": "missionos.starship_boostback_candidate.v1",
                                    "candidate_index": _development_boostback_candidate_index,
                                    "selection_scope": "initial_and_alignment_refresh", "recording": "every_macrostep",
                                    "production_policy_admitted": False}}
                                   if _development_boostback_candidate_index is not None else {}),
                                **({"development_cutoff": {"schema": "missionos.starship_scheduled_cutoff.v1",
                                    "time_s": _development_cutoff_time_s, "forecast_suppressed": True,
                                    "production_policy_admitted": False}} if _development_cutoff_time_s is not None else {}),
                                **({"development_fin_allocation": {"schema": "missionos.starship_fin_experiment.v1" if _development_fin_scope == "coast_and_landing" else "missionos.starship_fin_experiment.v2",
                                    "policy_id": "finite_regularized_fins_v1", "regularization": .05,
                                    **({"application_scope": "coast_only", "minimum_dynamic_pressure_pa": 100.}
                                       if _development_fin_scope == "coast_only" else {}),
                                    "production_policy_admitted": False}} if _development_fin_allocation else {}),
                                "capture_planning": {"schema": "missionos.starship_capture_planning.v1",
                                    "admissible_prediction_count": sum(r["admission"]["capture_plan_admissible"] for r in prediction_receipts),
                                    "best_effort_cutoff_used": any(e.get("cutoff_basis") == "unadmitted_geographic_best_effort" for e in events),
                                    "prediction_is_execution": False, "dispatch_authority_created": False},
                                "physical_execution": False, "catch_verified": False},
            "outcome": {"termination": termination, "phase": phase, "duration_s": state.time_s-start,
                        "start_time_s": start, "end_time_s": state.time_s, "integration_steps": steps,
                        "max_altitude_m": max(s["altitude_m"] for s in samples),
                        "max_body_rate_rad_s": max(_length(s["omega_body_rad_s"]) for s in samples),
                        "max_attitude_error_deg": max(s["controller"].get("attitude_error_deg", 0.) for s in samples),
                        "final_ground_speed_mps": final["ground_speed_mps"], "final_altitude_m": final["altitude_m"],
                        "final_hull_clearance_m": clearance, "return_site_distance_m": distance,
                        "final_tilt_deg": vehicle_tilt, "final_body_rate_rad_s": arrival["body_rate_rad_s"],
                        "simulated_contact_envelope_met": bool(envelope), "catch_verified": False, "landing_hardware_validated": False,
                        "starship_vehicle_validated": False, "six_dof_integrated": True, "attitude_prescribed": False,
                        "booster_return_6dof_implemented": True, "boostback_started": burn_start is not None,
                        "coast_started": any(e["event"] == "powered_rate_settled_cutoff" for e in events), "physical_execution": False},
            "limitations": ["Generic predictive development controller, not SpaceX guidance or identified aerodynamic data.",
                            "Shared main/header propellant reservoir; no thermal or structural survival certification.",
                            "Offline predictive simulation: forecast CPU time does not advance the simulated flight clock; real-time flight scheduling is not validated.",
                            "Predictive point-model attitude is not executed attitude; finite six-DOF actuation determines the outcome."]}
