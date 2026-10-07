"""Development command generation; estimates never assign a physical state.

The point shooting model is deliberately cheaper than execution. Its optional
full-vector mode includes the unchanged panel law and ideal steady trim; its
historical drag-only mode omits lateral lift. Angular motion is absent in both.
Its fuel margin is a screen, not six-DOF arrival admission.
All returned forces and axes require the ordinary finite actuator integrator.
"""
from __future__ import annotations

import math

from . import starship_physics as env


POLICY_ID = "constrained_return_development_v1"


def _vector(value, name):
    if not isinstance(value, (list, tuple)) or len(value) != 3 or any(
            type(x) not in (int, float) or not math.isfinite(x) for x in value):
        raise ValueError(f"invalid_{name}")
    return [float(x) for x in value]


def _positive(value, name, *, zero=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise ValueError(f"invalid_{name}")
    return float(value)


def _unit(value):
    length = env.norm(value)
    if length <= 1e-12:
        return (0., 0., 1.)
    return tuple(x / length for x in value)


def _support_reserve(mass, profile, catch):
    horizon = ((catch["initial_pin_clearance_m"] + catch["arm_half_width_m"])
               / (-.5 * catch["initial_vertical_speed_mps"])
               + 4 * profile["actuators"]["throttle_tau_s"])
    return mass * 9.81 / (profile["booster"]["engine_isp_s"] * env.STANDARD_GRAVITY_MPS2) * horizon


def _drag_area(vehicle, angle_rad):
    """Drag-only projection of existing panels in an ideal body-flow frame."""
    direction = (math.sin(angle_rad), 0., math.cos(angle_rad))
    result = 0.
    for panel in vehicle.aero_panels:
        vn = env.dot(direction, panel.normal_body)
        result += panel.area_m2 * (panel.normal_coefficient * abs(vn)**3
                                  + panel.tangential_coefficient * max(0., 1. - vn*vn)**1.5)
    return result


def fixed_bank_heading(position_enu_m, velocity_enu_mps):
    """One horizontal bearing from the ballistic future miss, held by context."""
    p, v = _vector(position_enu_m, "position"), _vector(velocity_enu_mps, "velocity")
    tgo = (v[2]+math.sqrt(v[2]*v[2]+19.6*max(0., p[2])))/9.8
    horizontal = [-(p[i]+v[i]*tgo) for i in (0, 1)]
    if math.hypot(*horizontal) < 1e-10:
        horizontal = [-p[0], -p[1]]
    if math.hypot(*horizontal) < 1e-10:
        horizontal = [-v[0], -v[1]]
    if math.hypot(*horizontal) < 1e-10:
        horizontal = [1., 0.]
    magnitude = math.hypot(*horizontal)
    return (horizontal[0]/magnitude, horizontal[1]/magnitude, 0.)


def _bank_geometry(velocity, bearing, angle_deg, sign):
    """A bank plane is held; the thrust axis still follows changing airflow."""
    import numpy as np
    tail = np.asarray(_unit([-x for x in velocity]))
    heading = np.asarray(bearing)-float(np.asarray(bearing)@tail)*tail
    if np.linalg.norm(heading) < 1e-10:
        heading = -sign*(np.asarray([0., 0., 1.])-tail[2]*tail)
    if np.linalg.norm(heading) < 1e-10:
        heading = np.asarray([1., 0., 0.])-tail[0]*tail
    heading /= np.linalg.norm(heading)
    angle = math.radians(angle_deg)
    # Do not request a downward longitudinal axis near the apogee: retain the
    # same bank plane while reducing angle through that geometric boundary.
    if sign*heading[2] < 0:
        angle = min(angle, math.atan2(max(0., tail[2]), -sign*heading[2]))
    axis = math.cos(angle)*tail+sign*math.sin(angle)*heading
    return tuple(float(x) for x in axis), math.degrees(angle)


def _panel_vector_force(velocity, axis, density, panels, angles):
    """Existing normal/tangential panel law in a proposed local body frame."""
    import numpy as np
    z = np.asarray(axis)
    x = np.asarray([1., 0., 0.])-z[0]*z
    if float(x@x) < 1e-8:
        x = np.asarray([0., 1., 0.])-z[1]*z
    x /= np.linalg.norm(x)
    rotation = np.column_stack([x, np.cross(z, x), z])
    air = rotation.T@np.asarray(velocity)
    force = np.zeros(3)
    for panel, angle in zip(panels, angles):
        base, hinge = np.asarray(panel.normal_body), np.asarray(panel.hinge_axis_body)
        normal = (base*math.cos(angle)+np.cross(hinge, base)*math.sin(angle)
                  +hinge*float(hinge@base)*(1.-math.cos(angle)))
        normal_speed = float(air@normal)
        tangent = air-normal*normal_speed
        force -= .5*density*panel.area_m2*(panel.normal_coefficient*normal_speed*abs(normal_speed)*normal
                  +panel.tangential_coefficient*float(np.linalg.norm(tangent))*tangent)
    return tuple(float(x) for x in rotation@force)


def point_coast(position_enu_m, velocity_enu_mps, mass_kg, vehicle, profile, *, entry_angle_deg=35.,
                terminal_altitude_m=1500., full_panel_vector=False, bank_heading_enu=None, bank_sign=-1):
    """Bounded guidance prediction with ideal drag attitude, no physical reset."""
    p, v = _vector(position_enu_m, "position"), _vector(velocity_enu_mps, "velocity")
    mass = _positive(mass_kg, "mass")
    angle = _positive(entry_angle_deg, "entry_angle", zero=True)
    if angle > 60 or type(terminal_altitude_m) not in (float, int) or not 0 < terminal_altitude_m < 20000:
        raise ValueError("invalid_point_coast_configuration")
    if type(full_panel_vector) is not bool or bank_sign not in (-1, 1) or type(bank_sign) is not int:
        raise ValueError("invalid_point_coast_bank_configuration")
    bearing = fixed_bank_heading(p, v) if bank_heading_enu is None else _vector(bank_heading_enu, "bank_heading")
    if abs(bearing[2]) > 1e-10 or abs(env.norm(bearing)-1.) > 1e-8:
        raise ValueError("bank_heading_must_be_horizontal_unit")
    area = _drag_area(vehicle, math.radians(angle))
    elapsed, steps, next_trim = 0., 0, 0.
    trim_angles, trim_updates = [0.]*len(vehicle.aero_panels), 0
    if full_panel_vector:
        from . import starship_sixdof as dyn
        from .starship_booster_recovery import _trim_entry_candidate
        props = dyn.mass_properties(vehicle, max(0., mass-vehicle.dry_mass_kg))
        context = {"com_body_m": props.com_body_m,
                   "jet_torque_nm": 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"],
                   "minimum_trim_dynamic_pressure_pa": profile["guidance"]["max_q_pa"]}
    while p[2] > terminal_altitude_m and elapsed < 1000. and steps < 1000:
        dt = .5 if p[2] < 50000 else 1.
        rho = env.standard_atmosphere(max(0., p[2]))["density_kg_m3"]
        gravity = env.EARTH_MU_M3_S2 / (env.EARTH_EQUATORIAL_RADIUS_M + max(0., p[2]))**2
        speed = env.norm(v)
        drag = .5 * rho * area * speed / mass
        # Bound the dissipative explicit step as the lower atmosphere thickens.
        dt = min(dt, .2 / max(drag, 1e-12), 1000. - elapsed)
        if full_panel_vector:
            axis, _ = _bank_geometry(v, bearing, angle, bank_sign) if v[2] < 0 else ((0., 0., 1.), 0.)
            if elapsed >= next_trim and v[2] < 0:
                _, trim = _trim_entry_candidate(v, axis, rho, vehicle.aero_panels, **context)
                trim_angles = trim["predicted_trim_flap_angles_rad"]
                next_trim, trim_updates = elapsed+5., trim_updates+1
            force = _panel_vector_force(v, axis, rho, vehicle.aero_panels, trim_angles)
            acceleration = [x/mass for x in force]
        else:
            acceleration = [-drag * x for x in v]
        acceleration[2] -= gravity
        for i in range(3):
            p[i] += v[i]*dt + .5*acceleration[i]*dt*dt
            v[i] += acceleration[i]*dt
        elapsed += dt
        steps += 1
    return {"position_enu_m": p, "velocity_enu_mps": v, "elapsed_s": elapsed,
            "drag_area_m2": area, "entry_angle_deg": angle,
            "terminal_altitude_m": terminal_altitude_m,
            "reached_terminal_altitude": p[2] <= terminal_altitude_m,
            "model": "ideal_trim_full_panel_vector_local_point_prediction" if full_panel_vector else "ideal_trim_drag_only_local_point_prediction",
            "bank_heading_enu": list(bearing), "bank_sign": bank_sign, "integration_steps": steps,
            "trim_update_count": trim_updates, "point_step_budget": 1000,
            "lateral_lift_integrated": full_panel_vector, "attitude_dynamics_integrated": False,
            "prediction_is_execution": False}


def boostback_force(velocity_enu_mps, target_velocity_enu_mps, mass_kg, gravity_mps2, profile, *, burn_axis_enu=None):
    """Fuel-efficient saturated velocity correction through finite engines."""
    velocity, target = _vector(velocity_enu_mps, "velocity"), _vector(target_velocity_enu_mps, "target_velocity")
    mass, gravity = _positive(mass_kg, "mass"), _positive(gravity_mps2, "gravity")
    maximum = profile["booster"]["engine_count"] * profile["booster"]["engine_thrust_n"]
    if burn_axis_enu is not None:
        axis = _vector(burn_axis_enu, "burn_axis")
        if abs(env.norm(axis)-1.) > 1e-8:
            raise ValueError("burn_axis_must_be_unit")
        error = [target[i]-velocity[i] for i in range(3)]
        along = env.dot(error, axis)
        perpendicular = [error[i]-along*axis[i] for i in range(3)]
        force = [maximum*x for x in axis]
        return force, {"velocity_error_mps": env.norm(error),
                       "along_axis_velocity_remaining_mps": along,
                       "perpendicular_velocity_error_enu_mps": perpendicular,
                       "perpendicular_velocity_error_mps": env.norm(perpendicular),
                       "burn_axis_enu": axis, "force_reference": "fixed_axis_impulse",
                       "requested_force_enu_n": force, "force_request_is_achieved_thrust": False}
    response = max(.5, 4 * profile["actuators"]["throttle_tau_s"])
    acceleration = [(target[i] - velocity[i]) / response for i in range(3)]
    acceleration[2] += gravity
    force = [mass * a for a in acceleration]
    ratio = min(1., maximum / max(env.norm(force), 1e-12))
    force = [x * ratio for x in force]
    return force, {"velocity_error_mps": env.norm([target[i] - velocity[i] for i in range(3)]),
                   "force_capacity_saturated": ratio < 1., "response_s": response,
                   "requested_force_enu_n": force, "force_request_is_achieved_thrust": False}


def _fixed_burn_axis(position, velocity, mass, target, profile):
    """Gravity-compensated impulse axis; two bounded rocket-time updates."""
    delta = [target[i]-velocity[i] for i in range(3)]
    gravity = env.EARTH_MU_M3_S2/(env.EARTH_EQUATORIAL_RADIUS_M+max(0., position[2]))**2
    exhaust = profile["booster"]["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2
    maximum = profile["booster"]["engine_count"]*profile["booster"]["engine_thrust_n"]
    duration = mass*(1.-math.exp(-env.norm(delta)/exhaust))/(maximum/exhaust)
    for _ in range(2):
        impulse = list(delta)
        impulse[2] += gravity*duration
        duration = (mass*(1.-math.exp(-env.norm(impulse)/exhaust))/(maximum/exhaust)
                    +profile["actuators"]["throttle_tau_s"])
    return _unit(impulse)


def _point_burn(position, velocity, mass, fuel, target, profile):
    p, v, m, f = list(position), list(velocity), mass, fuel
    dry, elapsed = mass - fuel, 0.
    reserve = profile.get("booster_return", {}).get("landing_reserve_kg", 60000.)
    exhaust = profile["booster"]["engine_isp_s"] * env.STANDARD_GRAVITY_MPS2
    axis = _fixed_burn_axis(p, v, mass, target, profile)
    maximum = profile["booster"]["engine_count"]*profile["booster"]["engine_thrust_n"]
    # Refreshed planning occurs after a three-center-engine finite slew. The
    # initial aggregate is an exposed prediction assumption, not inherited truth.
    throttle = 3*.4/profile["booster"]["engine_count"]
    tau, rate = profile["actuators"]["throttle_tau_s"], profile["actuators"]["throttle_rate_s"]
    while env.dot([target[i]-v[i] for i in range(3)], axis) > 12. and elapsed < 70. and f > reserve:
        gravity = env.EARTH_MU_M3_S2 / (env.EARTH_EQUATORIAL_RADIUS_M + p[2])**2
        dt = min(.1, (f-reserve)/(maximum/exhaust), 70.-elapsed)
        if dt <= 1e-8:
            break
        next_throttle = min(1., throttle+min(rate, (1.-throttle)/tau)*dt)
        mean_throttle = .5*(throttle+next_throttle)
        thrust = maximum*mean_throttle
        a = [thrust*x/m for x in axis]
        a[2] -= gravity
        for i in range(3):
            p[i] += v[i]*dt + .5*a[i]*dt*dt
            v[i] += a[i]*dt
        f = max(reserve, f-thrust/exhaust*dt)
        m, elapsed = dry + f, elapsed + dt
        throttle = next_throttle
    return p, v, m, f, elapsed, axis, throttle


def _point_shutdown(position, velocity, mass, fuel, axis, throttle, profile):
    """Aggregate lag and a declared damping-time settle estimate, not attitude."""
    p, v, m, f, elapsed = list(position), list(velocity), mass, fuel, 0.
    dry = mass-fuel
    maximum = profile["booster"]["engine_count"]*profile["booster"]["engine_thrust_n"]
    exhaust = profile["booster"]["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2
    tau, rate = profile["actuators"]["throttle_tau_s"], profile["actuators"]["throttle_rate_s"]
    frequency = profile["guidance"]["attitude_frequency_rad_s"]
    damping = profile["guidance"]["attitude_damping_ratio"]
    settle_horizon = min(30., 2./(damping*frequency))
    horizon = settle_horizon+4*tau
    initial_v = list(v)
    while elapsed < horizon-1e-9:
        target = 3*.4/profile["booster"]["engine_count"] if elapsed < settle_horizon else 0.
        dt = min(.1, horizon-elapsed, settle_horizon-elapsed if elapsed < settle_horizon else horizon-elapsed)
        delta = max(-rate, min(rate, (target-throttle)/tau))*dt
        after_throttle = max(0., min(1., throttle+delta))
        mean_throttle = .5*(throttle+after_throttle)
        thrust = min(maximum*mean_throttle, f*exhaust/dt)
        gravity = env.EARTH_MU_M3_S2/(env.EARTH_EQUATORIAL_RADIUS_M+max(0., p[2]))**2
        a = [thrust*x/m for x in axis]
        a[2] -= gravity
        for i in range(3):
            p[i] += v[i]*dt+.5*a[i]*dt*dt
            v[i] += a[i]*dt
        f = max(0., f-thrust/exhaust*dt)
        m, throttle, elapsed = dry+f, after_throttle, elapsed+dt
    return p, v, m, f, {"point_controlled_settle_horizon_s": settle_horizon,
                       "point_shutdown_duration_s": horizon,
                       "point_shutdown_propellant_kg": fuel-f,
                       "point_shutdown_velocity_change_enu_mps": [v[i]-initial_v[i] for i in range(3)],
                       "point_final_aggregate_throttle": throttle,
                       "settle_model": "profile_damping_time_estimate_no_angular_state"}


def boostback_plan(position_enu_m, velocity_enu_mps, mass_kg, propellant_kg, vehicle, profile, catch_config, *, full_panel_vector=False):
    """Bounded command-space shooting; no success selected from failed records.

    Five vertical command seeds retain the earlier planner's declared span.
    Each horizontal command is corrected at most four times against its own
    predicted burn/coast. The chosen objective is remaining fuel after ideal
    velocity arrest and the independently defined support reserve. This bounds
    computation at twenty-five point continuations and does not fit coefficients.
    """
    p, v = _vector(position_enu_m, "position"), _vector(velocity_enu_mps, "velocity")
    mass, fuel = _positive(mass_kg, "mass"), _positive(propellant_kg, "propellant", zero=True)
    if fuel >= mass:
        raise ValueError("propellant_must_be_less_than_mass")
    exhaust, dry = profile["booster"]["engine_isp_s"] * env.STANDARD_GRAVITY_MPS2, mass-fuel
    site_norm = math.hypot(p[0], p[1])
    held_bearing = (-p[0]/site_norm, -p[1]/site_norm, 0.) if site_norm > 1e-10 else fixed_bank_heading(p, v)
    candidates = []
    for vertical in (200., 400., 600., 800., 1000.):
        flight_time = (vertical + math.sqrt(vertical*vertical + 19.6*max(0., p[2]))) / 9.8
        horizontal = [-p[i] / max(30., flight_time) for i in (0, 1)]
        prediction = None
        prior_h, prior_miss, jacobian = None, None, None
        if full_panel_vector:
            import numpy as np
        for _ in range(4):
            target = [*horizontal, vertical]
            bp, bv, bm, bf, bt, burn_axis, throttle = _point_burn(p, v, mass, fuel, target, profile)
            sp, sv, sm, sf, shutdown = _point_shutdown(bp, bv, bm, bf, burn_axis, throttle, profile)
            prediction = point_coast(sp, sv, sm, vehicle, profile, full_panel_vector=full_panel_vector,
                                     bank_heading_enu=held_bearing if full_panel_vector else None)
            if full_panel_vector:
                current_h, miss = np.asarray(horizontal), np.asarray(prediction["position_enu_m"][:2])
                if jacobian is None:
                    jacobian = np.eye(2)*max(30., prediction["elapsed_s"])
                elif float((current_h-prior_h)@(current_h-prior_h)) > 1e-12:
                    step = current_h-prior_h
                    jacobian += np.outer(miss-prior_miss-jacobian@step, step)/float(step@step)
                correction = np.linalg.lstsq(jacobian, miss, rcond=1e-6)[0]
                cap = profile.get("booster_return", {}).get("boostback_max_horizontal_speed_mps", 700.)
                correction *= min(1., cap/max(float(np.linalg.norm(correction)), 1e-12))
                prior_h, prior_miss = current_h, miss
                horizontal = [float(x) for x in current_h-correction]
            else:
                for i in (0, 1):
                    horizontal[i] -= prediction["position_enu_m"][i] / max(30., prediction["elapsed_s"])
        # Reevaluate the final corrected target so its payload is not stale.
        target = [*horizontal, vertical]
        bp, bv, bm, bf, bt, burn_axis, throttle = _point_burn(p, v, mass, fuel, target, profile)
        sp, sv, sm, sf, shutdown = _point_shutdown(bp, bv, bm, bf, burn_axis, throttle, profile)
        prediction = point_coast(sp, sv, sm, vehicle, profile, full_panel_vector=full_panel_vector,
                                 bank_heading_enu=held_bearing if full_panel_vector else None)
        speed = env.norm(prediction["velocity_enu_mps"])
        after_arrest = sm * math.exp(-speed/exhaust) - dry
        support_reserve = _support_reserve(dry + max(0., after_arrest), profile, catch_config)
        margin = after_arrest - support_reserve
        error = [target[i]-bv[i] for i in range(3)]
        along = env.dot(error, burn_axis)
        perpendicular = [error[i]-along*burn_axis[i] for i in range(3)]
        candidates.append({"target_velocity_enu_mps": target, "burn_axis_enu": list(burn_axis),
                           "point_burn_duration_s": bt,
                           "point_post_burn_fuel_kg": bf, "point_post_burn_position_enu_m": bp,
                           "point_post_burn_velocity_enu_mps": bv, "predicted_coast": prediction,
                           "point_post_shutdown_fuel_kg": sf,
                           "point_post_shutdown_position_enu_m": sp,
                           "point_post_shutdown_velocity_enu_mps": sv,
                           **shutdown,
                           "point_after_ideal_arrest_fuel_kg": after_arrest,
                           "point_support_reserve_kg": support_reserve, "fuel_margin_kg": margin,
                           "point_burn_target_reached": env.norm(error) <= 12.,
                           "point_along_impulse_complete": along <= 12.,
                           "point_along_axis_velocity_remaining_mps": along,
                           "point_perpendicular_velocity_error_enu_mps": perpendicular,
                           "point_perpendicular_velocity_error_mps": env.norm(perpendicular),
                           "admissible": False})
    feasible = [c for c in candidates if c["point_along_impulse_complete"] and c["fuel_margin_kg"] >= 0
                and c["predicted_coast"]["reached_terminal_altitude"]]
    selected = max(feasible or candidates, key=lambda c: c["fuel_margin_kg"])
    return {**selected, "candidates": candidates, "point_budget_candidate_count": len(feasible),
            "bank_heading_enu": list(held_bearing) if full_panel_vector else None,
            "bank_sign": -1 if full_panel_vector else None,
            "method": "fixed_axis_point_impulse",
            "point_continuation_count": 25, "production_policy_admitted": False,
            "prediction_is_execution": False,
            "limitations": ["Ideal attitude in the point coast; trim tracking and angular dynamics are omitted.",
                            "Lateral lift is included only when full_panel_vector is explicitly true.",
                            "Powered point forecast uses aggregate spool but omits finite slew and torque.",
                            "Initial aggregate throttle is the declared three-engine slew assumption.",
                            "Controlled settle duration is a profile damping-time estimate without angular dynamics.",
                            "Four time constants leave a nonzero predicted shutdown tail; the point coast omits that remainder.",
                            "A positive ideal fuel margin is not a reachable six-DOF certificate."]}


def entry_axis(position_enu_m, velocity_enu_mps, profile, *, previous_axis=None,
               mass_kg=None, propellant_kg=None, vehicle=None, com_body_m=None,
               trim_context=None, bank_heading_enu=None, bank_sign=None,
               fixed_bank_angle_deg=None, preparation_altitude_m=80000.):
    """Prioritize a fuel/energy constraint over geographic steering benefit."""
    from .starship_booster_recovery import _trim_entry_candidate
    import numpy as np

    p, v = _vector(position_enu_m, "position"), _vector(velocity_enu_mps, "velocity")
    if mass_kg is None or propellant_kg is None or vehicle is None:
        raise ValueError("entry_guidance_requires_observed_mass_fuel_vehicle")
    mass, fuel = _positive(mass_kg, "mass"), _positive(propellant_kg, "propellant", zero=True)
    if fuel >= mass:
        raise ValueError("propellant_must_be_less_than_mass")
    if previous_axis is not None:
        previous_axis = _vector(previous_axis, "previous_axis")
    held = bank_heading_enu is not None
    if held:
        bearing = _vector(bank_heading_enu, "bank_heading")
        if abs(bearing[2]) > 1e-10 or abs(env.norm(bearing)-1.) > 1e-8:
            raise ValueError("bank_heading_must_be_horizontal_unit")
        if bank_sign is None:
            bank_sign = -1
        if type(bank_sign) is not int or bank_sign not in (-1, 1):
            raise ValueError("invalid_bank_sign")
    if fixed_bank_angle_deg is not None:
        angle = _positive(fixed_bank_angle_deg, "fixed_bank_angle", zero=True)
        preparation_altitude = _positive(preparation_altitude_m, "entry_preparation_altitude")
        if angle > 60. or not held:
            raise ValueError("fixed_bank_requires_held_bearing_and_bounded_angle")
        context = trim_context or {"com_body_m": com_body_m or (0., 0., profile["booster"]["dry_com_z_m"]),
                                   "jet_torque_nm": 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"],
                                   "minimum_trim_dynamic_pressure_pa": profile["guidance"]["max_q_pa"]}
        context = {**context, "minimum_trim_dynamic_pressure_pa": max(
            context.get("minimum_trim_dynamic_pressure_pa", 0.), profile["guidance"]["max_q_pa"])}
        predicted_p, predicted_v, lookahead = list(p), list(v), 0.
        gravity = env.EARTH_MU_M3_S2/(env.EARTH_EQUATORIAL_RADIUS_M+max(0., p[2]))**2
        discriminant = v[2]*v[2]+2*gravity*(p[2]-preparation_altitude)
        future = p[2] > preparation_altitude or v[2] > 0 and discriminant >= 0.
        if future:
            lookahead = (v[2]+math.sqrt(max(0., discriminant)))/gravity
            predicted_p = [p[i]+v[i]*lookahead for i in (0, 1)]+[preparation_altitude]
            predicted_v[2] = v[2]-gravity*lookahead
        axis, effective_angle = _bank_geometry(predicted_v, bearing, angle, bank_sign)
        rho = env.standard_atmosphere(max(0., predicted_p[2]))["density_kg_m3"]
        force, trim = _trim_entry_candidate(predicted_v, axis, rho, vehicle.aero_panels, **context)
        accepted = trim["steady_trim_feasible"]
        requested_trim = trim
        if not accepted:
            # Keep an explicit failed trim prediction. The conservative tail
            # reference is a command fallback, never arrival or rescue evidence.
            axis, effective_angle = _bank_geometry(predicted_v, bearing, 0., bank_sign)
            force, trim = _trim_entry_candidate(predicted_v, axis, rho, vehicle.aero_panels, **context)
        metadata = {"entry_angle_deg": effective_angle, "fixed_bank_angle_deg": angle,
                    "fixed_bank_trim_feasible": accepted, "requested_bank_accepted": accepted,
                    "entry_mode": ("fixed_bank_descending_entry_preparation" if future else "fixed_trimmed_entry_bank")
                                  if accepted else "fixed_bank_trim_infeasible_fallback",
                    "bank_heading_enu": list(bearing), "bank_sign": bank_sign, "bank_bearing_held": True,
                    "entry_preparation_altitude_m": preparation_altitude,
                    "entry_preparation_lookahead_s": lookahead,
                    "predicted_entry_position_enu_m": predicted_p,
                    "predicted_entry_velocity_enu_mps": predicted_v,
                    "entry_axis_uses_future_velocity": future,
                    "lookahead_model": "local_constant_gravity_no_aero_or_attitude",
                    "lookahead_is_executed_velocity": False, "actual_body_axis_assigned": False,
                    "prediction_is_execution": False, "force_prediction_is_achieved_force": False,
                    "predicted_force_enu_n": list(force),
                    "requested_fixed_bank_trim": requested_trim,
                    "trim_prediction_scope": "future_descending_entry" if future else "current_flow_static_only",
                    "production_policy_admitted": False}
        if future:
            # The caller must recompute feed-forward trim at the current flow
            # and governed axis. Do not pass future angles as current deflection.
            metadata["future_entry_trim"] = trim
        else:
            metadata.update(trim)
        return axis, metadata
    if v[2] >= 0:
        return (0., 0., 1.), {"entry_angle_deg": 0., "entry_mode": "upright_preparation_during_ascent",
                            "prediction_is_execution": False}
    rho = env.standard_atmosphere(max(0., p[2]))["density_kg_m3"]
    tail = np.asarray(_unit([-x for x in v]))
    remaining_dv = profile["booster"]["engine_isp_s"] * env.STANDARD_GRAVITY_MPS2 * math.log(mass/(mass-fuel))
    # Existing energy target is retained; hard lexicographic priority replaces
    # the weighted tradeoff that could prefer steering while energy is unsafe.
    terminal_speed = max(100., .6 * remaining_dv)
    stopping_accel = max(0., (v[2]*v[2]-terminal_speed*terminal_speed)
                         / (2 * max(1000., p[2]-1500.)))
    desired_up = 9.8 + stopping_accel
    tgo = max(8., min(120., max(0., p[2])/max(50., -v[2])))
    desired_h = [-2 * (p[i]+v[i]*tgo) / (tgo*tgo) for i in (0, 1)]
    heading = np.asarray(bearing) if held else np.asarray([*desired_h, 0.])
    if np.linalg.norm(heading) < 1e-10:
        heading = np.asarray([1., 0., 0.])
    heading -= float(heading @ tail) * tail
    if np.linalg.norm(heading) < 1e-10:
        heading = np.asarray([0., 1., 0.]) - tail[1]*tail
    heading /= np.linalg.norm(heading)
    candidates = []
    for angle in (0., 10., 20., 30., 35., 40., 45., 60.):
        for sign in ((bank_sign,) if held else (1.,) if angle == 0 else (-1., 1.)):
            if held:
                axis, actual_angle = _bank_geometry(v, bearing, angle, sign)
            else:
                axis = math.cos(math.radians(angle))*tail + sign*math.sin(math.radians(angle))*heading
                actual_angle = angle
            context = trim_context or {"com_body_m": com_body_m or (0., 0., profile["booster"]["dry_com_z_m"]),
                                       "jet_torque_nm": 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"],
                                       "minimum_trim_dynamic_pressure_pa": profile["guidance"]["max_q_pa"]}
            force, trim = _trim_entry_candidate(v, tuple(axis), rho, vehicle.aero_panels, **context)
            accel = [x/mass for x in force]
            deficit = max(0., desired_up-accel[2])
            lateral_error = sum((accel[i]-desired_h[i])**2 for i in (0, 1))
            slew = 0. if previous_axis is None else sum((axis[i]-previous_axis[i])**2 for i in range(3))
            candidates.append((axis, actual_angle, force, trim, deficit, lateral_error, slew))
    feasible = [c for c in candidates if c[3]["steady_trim_feasible"]]
    pool = feasible or candidates
    if held:
        # Among choices satisfying the energy inequality, steering is useful.
        # If none can satisfy it, normalize the energy/site errors on the same
        # finite descent horizon instead of maximizing a local vertical load.
        energetic = [c for c in pool if c[4] <= 1e-6]
        if energetic:
            selected = min(energetic, key=lambda c: c[5]+.1*c[6])
        else:
            energy_scale = max(1., desired_up)
            site_scale = max(1., math.hypot(*desired_h))
            selected = min(pool, key=lambda c: (c[4]/energy_scale)**2+c[5]/(site_scale*site_scale)+.1*c[6])
    else:
        selected = min(pool, key=lambda c: (round(c[4], 6), c[5] + .1*c[6]))
    axis, angle, force, trim, deficit, _, _ = selected
    return tuple(float(x) for x in axis), {"entry_angle_deg": angle,
            "entry_mode": "trimmed_energy_constraint_then_site_guidance",
            "bank_heading_enu": list(bearing) if held else None, "bank_sign": bank_sign if held else None,
            "bank_bearing_held": held,
            "candidate_count": len(candidates), "trim_feasible_count": len(feasible),
            "selected_energy_deficit_mps2": deficit, "desired_upward_aero_acceleration_mps2": desired_up,
            "predicted_force_enu_n": list(force), "terminal_speed_budget_mps": terminal_speed,
            **trim, "prediction_is_execution": False, "force_prediction_is_achieved_force": False}


def landing_force(position_error_enu_m, pin_clearance_m, velocity_enu_mps, aero_accel_enu_mps2,
                  mass_kg, propellant_kg, profile, catch_config):
    """ZEM/ZEV braking, then the actual pin-relative terminal feedback law."""
    error, velocity = _vector(position_error_enu_m, "position_error"), _vector(velocity_enu_mps, "velocity")
    aero = _vector(aero_accel_enu_mps2, "aero_acceleration")
    mass, fuel = _positive(mass_kg, "mass"), _positive(propellant_kg, "propellant", zero=True)
    clearance = float(pin_clearance_m)
    if type(pin_clearance_m) not in (int, float) or not math.isfinite(pin_clearance_m) or fuel >= mass:
        raise ValueError("invalid_landing_state")
    desired_clearance = catch_config["initial_pin_clearance_m"] + .5*catch_config["arm_half_width_m"]
    target_vertical = 1.5 * catch_config["initial_vertical_speed_mps"]
    height = max(0., clearance-desired_clearance)
    gravity = 9.81
    # tgo follows the remaining distance and velocity, not elapsed mission time.
    tgo = max(2., 2*height/max(1., -velocity[2]-target_vertical))
    if height > 100.:
        net = [-6*error[i]/(tgo*tgo)-4*velocity[i]/tgo for i in (0, 1)]
        net.append((target_vertical-velocity[2])/tgo)
        mode = "zem_zev_braking"
    else:
        tau = catch_config["terminal_position_tau_s"]
        desired_vertical = -max(-target_vertical, min(20., height/catch_config["terminal_position_tau_s"]))
        net = [-error[i]/(tau*tau)-2*velocity[i]/tau for i in (0, 1)]
        net.append((desired_vertical-velocity[2])/catch_config["terminal_velocity_tau_s"])
        mode = "material_pin_terminal_feedback"
    acceleration = [net[i]-aero[i] for i in range(3)]
    acceleration[2] += gravity
    acceleration[2] = max(.5, acceleration[2])
    maximum_tilt = (60. if height > 1000. else 30. if height > 100. else catch_config["terminal_max_tilt_deg"])
    lateral = math.hypot(*acceleration[:2])
    tilt_ratio = min(1., acceleration[2]*math.tan(math.radians(maximum_tilt))/max(lateral, 1e-12))
    acceleration[:2] = [x*tilt_ratio for x in acceleration[:2]]
    force = [mass*x for x in acceleration]
    maximum_force = profile["booster"]["gimbal_engine_count"] * profile["booster"]["engine_thrust_n"]
    capacity_ratio = min(1., maximum_force/max(env.norm(force), 1e-12))
    force = [x*capacity_ratio for x in force]
    exhaust = profile["booster"]["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2
    after_ideal_arrest = mass*math.exp(-env.norm(velocity)/exhaust)-(mass-fuel)
    reserve = _support_reserve(mass-fuel+max(0., after_ideal_arrest), profile, catch_config)
    return force, {"mode": mode, "remaining_descent_time_s": tgo,
                   "target_pin_clearance_m": desired_clearance, "target_pin_vertical_speed_mps": target_vertical,
                   "maximum_tilt_deg": maximum_tilt, "lateral_force_limited": tilt_ratio < 1.,
                   "force_capacity_saturated": capacity_ratio < 1., "requested_force_enu_n": force,
                   "point_after_ideal_arrest_fuel_kg": after_ideal_arrest,
                   "point_support_reserve_kg": reserve, "ideal_fuel_margin_kg": after_ideal_arrest-reserve,
                   "force_request_is_achieved_thrust": False, "production_policy_admitted": False}
