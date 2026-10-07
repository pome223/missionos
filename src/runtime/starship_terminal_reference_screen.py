"""Pre-plant necessary bounds for a declared pin/pose reference family.

Reference states are hypothetical algebraic inputs to the configured load law,
never assigned to an actual vehicle. Force-cone exclusion overapproximates every
fuel mass, movable-fin force and available RCS force. A failed necessary bound
rejects this reference; it does not establish vehicle-wide infeasibility.
Nominal force/fuel/force-axis derivatives remain explicitly assumption-dependent.
"""
from __future__ import annotations

import math
from copy import deepcopy

from . import starship_physics as env
from . import starship_sixdof as dyn
from . import starship_actual_recovery_shooting as shooting
from .starship_fixed_terminal_reference import CONFIG, SCREEN_SCHEMA, evaluate_terminal_reference, _tower_frame
from .starship_fin_allocation import actuator_endpoint
from .starship_sixdof_mission import vehicle
from .starship_sixdof_contact import hull_clearance

SCREEN_SCHEMA_V2 = "missionos.starship_fixed_terminal_reference_screen.v2"


def _reference_state(sample, initial, booster, profile, catch, fuel):
    """A hypothetical undepleted-CoM-derivative reference state, not execution."""
    site, axes = _tower_frame(profile, sample["time_s"])
    props = dyn.mass_properties(booster, fuel)
    q = tuple(sample["pose_q_body_to_eci"])
    omega = dyn.inverse_rotate(q, tuple(sample["pose_rate_eci_rad_s"]))
    alpha = dyn.inverse_rotate(q, tuple(sample["pose_acceleration_eci_rad_s2"]))
    lever = env.add(tuple(sum(p[i] for p in catch["support_points_body_m"])/2 for i in range(3)),
                    env.scale(props.com_body_m, -1.))
    def world(local):
        return tuple(sum(local[j]*axes[j][i] for j in range(3)) for i in range(3))
    earth = (0., 0., env.EARTH_ROTATION_RAD_S)
    pin_r = env.add(site.r, world(sample["position_enu_m"]))
    local_v, local_a = world(sample["velocity_enu_mps"]), world(sample["acceleration_enu_mps2"])
    pin_v = env.add(env.cross(earth, pin_r), local_v)
    pin_a = env.add(env.add(env.cross(earth, env.cross(earth, pin_r)), env.scale(env.cross(earth, local_v), 2.)), local_a)
    cg_r = env.add(pin_r, env.scale(dyn.rotate(q, lever), -1.))
    cg_v = env.add(pin_v, env.scale(dyn.rotate(q, env.cross(omega, lever)), -1.))
    rotational_a = env.add(env.cross(alpha, lever), env.cross(omega, env.cross(omega, lever)))
    cg_a = env.add(pin_a, env.scale(dyn.rotate(q, rotational_a), -1.))
    state = dyn.State6DOF(sample["time_s"], cg_r, cg_v, q, omega, fuel,
        initial.engine_states, initial.flap_angles_rad)
    return state, cg_a, props


def _fixed_and_fin_bounds(observed, booster, rho_max, velocity_uncertainty):
    fixed = dyn.ZERO
    perturbation, fin_bound = 0., 0.
    for panel, load in zip(booster.aero_panels, observed["panel_loads"]):
        speed = env.norm(load["local_air_velocity_body_mps"])
        coefficient = max(panel.normal_coefficient, panel.tangential_coefficient)
        if panel.max_deflection_rad > 0:
            # Orthogonal normal/tangent terms give ||F|| <= rho*A*Cmax*v²/2
            # for EVERY normal angle; no sampled trim is treated as authority.
            fin_bound += .5*rho_max*panel.area_m2*coefficient*(speed+velocity_uncertainty)**2
        else:
            fixed = env.add(fixed, load["force_body_n"])
            density_delta = abs(rho_max-observed["atmosphere"]["density_kg_m3"])
            perturbation += (rho_max*panel.area_m2*coefficient*(speed+velocity_uncertainty)*velocity_uncertainty
                +.5*density_delta*panel.area_m2*coefficient*speed*speed)
    return fixed, perturbation, fin_bound


def repair_terminal_reference_screen(original, plan, snapshot, profile, catch):
    """Widen v1 bounds using SAME stored loads; no model/plant reevaluation."""
    if (original.get("schema") != SCREEN_SCHEMA or original.get("reference_sha256") != shooting.digest(plan)
            or original.get("origin_context_sha256") != shooting.digest(snapshot)):
        raise ValueError("reference_screen_repair_binding")
    result = deepcopy(original)
    result.update(schema=SCREEN_SCHEMA_V2, original_screen_sha256=shooting.digest(original),
        original_reference_model_evaluations=original["reference_model_evaluations"],
        reference_model_evaluations=0, new_reference_model_evaluations=0,
        bounds_repaired_from_stored_endpoints=True, physical_plant_integrations=0)
    if original["status"] == "deferred":
        return shooting.saved(result)
    booster = vehicle(profile, "booster")
    mass_min, mass_max = original["mass_interval_kg"]
    span, dot_bound, ddot_bound = original["com_span_m"], original["com_velocity_bound_mps"], original["com_acceleration_bound_mps2"]
    failures, lower_norms = [], []
    for node in result["nodes"]:
        inputs = node["aerodynamic_bound_source_inputs"]
        altitude_upper = max(item["altitude_m"] for item in inputs)+span
        altitude_lower = min(item["altitude_m"] for item in inputs)-span
        rho_upper = max(env.standard_atmosphere(altitude_lower)["density_kg_m3"], *(item["density_kg_m3"] for item in inputs))
        rho_lower = min(env.standard_atmosphere(altitude_upper)["density_kg_m3"], *(item["density_kg_m3"] for item in inputs))
        density_width = rho_upper-rho_lower
        speeds = [max(env.norm(item["panel_loads"][i]["local_air_velocity_body_mps"]) for item in inputs)
                  for i in range(len(booster.aero_panels))]
        velocity_uncertainty = dot_bound+env.EARTH_ROTATION_RAD_S*span
        perturbation, fin_bound = 0., 0.
        for panel, speed in zip(booster.aero_panels, speeds):
            coefficient = max(panel.normal_coefficient, panel.tangential_coefficient)
            if panel.max_deflection_rad > 0:
                fin_bound += .5*rho_upper*panel.area_m2*coefficient*(speed+velocity_uncertainty)**2
            else:
                perturbation += (rho_upper*panel.area_m2*coefficient*(speed+velocity_uncertainty)*velocity_uncertainty
                    +.5*density_width*panel.area_m2*coefficient*speed*speed)
        radius_lower = min(env.norm(item["r_eci_m"]) for item in inputs)-span
        if radius_lower <= env.EARTH_EQUATORIAL_RADIUS_M*.99:
            raise ValueError("reference_gravity_bound_outside_declared_near_earth_domain")
        gravity_bound = (env.EARTH_MU_M3_S2/radius_lower**2
            *(1+6*env.EARTH_J2*(env.EARTH_EQUATORIAL_RADIUS_M/radius_lower)**2))
        omega = env.norm(node["reference_sample"]["pose_rate_eci_rad_s"])
        geometric = mass_max*(2*omega*dot_bound+ddot_bound+6*gravity_bound*span/radius_lower)
        uncertainty = perturbation+fin_bound+geometric+node["outside13_residual_force_norm_bound_n"]
        required, rcs = node["required_force_body_endpoints_n"], node["rcs_force_component_bound_body_n"]
        lower = [min(force[i] for force in required)-uncertainty-rcs[i] for i in range(3)]
        upper = [max(force[i] for force in required)+uncertainty+rcs[i] for i in range(3)]
        zmax = min(upper[2], node["main_startup_capacity_n"])
        tolerance = 128*math.ulp(max(1., node["main_startup_capacity_n"], *(abs(v) for v in lower+upper)))
        codes = []
        if zmax < max(0., lower[2])-tolerance:
            codes.append("main_positive_axial_force_or_startup_capacity")
        for axis, ratio in ((0, node["gimbal_cone_x_over_z"]), (1, node["gimbal_cone_y_over_z"])):
            if lower[axis] > ratio*max(0., zmax)+tolerance or upper[axis] < -ratio*max(0., zmax)-tolerance:
                codes.append("main_gimbal_force_cone_axis_"+str(axis))
        if min(node["hull_ground_clearance_endpoints_m"]) < 0:
            codes.append("reference_hull_crosses_actual_ellipsoid")
        if codes:
            failures.append({"node_index": node["index"], "elapsed_s": node["elapsed_s"], "codes": codes})
        lower_norm = math.sqrt(sum(max(0., lo, -hi)**2 for lo, hi in zip(lower, upper)))
        lower_norms.append(lower_norm)
        node.update(density_bound_altitude_lower_m=altitude_lower, density_bound_altitude_upper_m=altitude_upper,
            density_upper_bound_kg_m3=rho_upper, density_lower_bound_kg_m3=rho_lower,
            panel_speed_upper_bounds_mps=speeds, radius_lower_bound_m=radius_lower,
            gravity_acceleration_norm_upper_bound_mps2=gravity_bound,
            fixed_panel_force_perturbation_bound_n=perturbation, movable_fin_force_norm_bound_n=fin_bound,
            geometric_depletion_force_uncertainty_n=geometric,
            required_main_force_box_lower_body_n=lower, required_main_force_box_upper_body_n=upper,
            force_roundoff_n=tolerance, violations=codes, necessary_main_force_norm_lower_n=lower_norm)
    if not result["pose_peak_rate_bound_passed"] or not result["pose_peak_acceleration_bound_passed"]:
        failures.append({"node_index": None, "codes": ["raw_pose_rate_or_acceleration_bound"]})
    intervals, duration = CONFIG["reference_screen_intervals"], plan["duration_s"]
    result.update(known_reference_violations=failures,
        sampled_lower_fuel_integral_kg=sum((lower_norms[i]+lower_norms[i+1])*.5*duration/intervals
            /(profile["booster"]["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2) for i in range(intervals)),
        status="known_reference_bound_failed" if failures else "necessary_bounds_not_excluded_unproved",
        candidate_plant_call_allowed=False,
        reason="fixed_reference_inconsistent_with_declared_model_bounds" if failures else None)
    return shooting.saved(result)


def screen_terminal_reference(plan, snapshot, profile, catch):
    """Reject known inconsistent references before a finite-plant call.

    A non-rejection is unproved arithmetic information. This screen never
    authorizes a plant call, reference tracking, capture or execution. A separate
    recorded development protocol must authorize any bounded numerical trial.
    """
    initial = dyn.state_from_dict(snapshot["state"])
    booster = vehicle(profile, "booster")
    if (plan["origin_context_sha256"] != shooting.digest(snapshot)
            or plan["profile_sha256"] != shooting.digest(profile) or plan["catch_profile_sha256"] != shooting.digest(catch)):
        raise ValueError("terminal_screen_origin_profile_binding")
    result = {"schema": SCREEN_SCHEMA, "reference_sha256": shooting.digest(plan),
        "origin_context_sha256": shooting.digest(snapshot), "configuration": CONFIG,
        "reference_model_evaluations": 0, "physical_plant_integrations": 0,
        "nodes": [], "known_reference_violations": [], "reference_is_achieved_state": False,
        "vehicle_global_infeasibility_established": False, "joint_tracking_feasibility_established": False,
        "hardware_validated": False, "arrival_admitted": False, "support_admitted": False,
        "tower_structure_geometry_available": False, "tower_interference_validated": False,
        "unknowns": ["Unmodeled tower structure; only configured pins and actual ellipsoid hull geometry are checked.",
                     "Nominal inverse force/fuel and force-axis derivatives are not actual future controller loads."]}
    if booster.wind is not None and booster.wind.active:
        result.update(status="deferred", candidate_plant_call_allowed=False,
                      reason="nonzero_declared_wind_interval_bound_not_implemented")
        return shooting.saved(result)
    dry_props = dyn.mass_properties(booster, 0.)
    initial_props = dyn.mass_properties(booster, initial.propellant_kg)
    mass_min, mass_max = dry_props.mass_kg, initial_props.mass_kg
    com_span = env.norm(env.add(initial_props.com_body_m, env.scale(dry_props.com_body_m, -1.)))
    dcom_max = env.norm(dry_props.dcom_dpropellant_m_per_kg)
    flow_max = sum(spec.max_thrust_n/(spec.isp_s*env.STANDARD_GRAVITY_MPS2)
        for i, spec in enumerate(booster.engines) if initial.engine_states[i].available and (i < 13 or spec.name.startswith("rcs_")))
    flow_derivative_max = sum(spec.max_thrust_n/(spec.isp_s*env.STANDARD_GRAVITY_MPS2)
        *min(spec.throttle_rate_per_s, 1/spec.throttle_time_constant_s)
        for i, spec in enumerate(booster.engines) if initial.engine_states[i].available and (i < 13 or spec.name.startswith("rcs_")))
    flow_max += sum(spec.max_thrust_n*actual.throttle/(spec.isp_s*env.STANDARD_GRAVITY_MPS2)
        for i, (spec, actual) in enumerate(zip(booster.engines, initial.engine_states))
        if 13 <= i < profile["booster"]["engine_count"] and actual.available)
    flow_derivative_max += sum(spec.max_thrust_n/(spec.isp_s*env.STANDARD_GRAVITY_MPS2)
        *min(spec.throttle_rate_per_s, actual.throttle/spec.throttle_time_constant_s)
        for i, (spec, actual) in enumerate(zip(booster.engines, initial.engine_states))
        if 13 <= i < profile["booster"]["engine_count"] and actual.available)
    com_dot_max = dcom_max*flow_max
    com_ddot_max = dcom_max*(flow_derivative_max+2*flow_max*flow_max/mass_min)
    rcs_component = [sum(spec.max_thrust_n*abs(spec.direction_body[i]) for j, spec in enumerate(booster.engines)
        if spec.name.startswith("rcs_") and initial.engine_states[j].available) for i in range(3)]
    outside_residual = sum(spec.max_thrust_n*actual.throttle for i, (spec, actual) in
        enumerate(zip(booster.engines, initial.engine_states)) if 13 <= i < profile["booster"]["engine_count"] and actual.available)
    gx = max(spec.max_gimbal_rad for i, spec in enumerate(booster.engines[:13]) if initial.engine_states[i].available)
    x_ratio, y_ratio = math.tan(gx), math.tan(gx)/math.cos(gx)
    steps, duration = CONFIG["reference_screen_intervals"], plan["duration_s"]
    lower_force_norms, nominal_forces, nominal_fuel = [], [], 0.
    nominal_remaining = initial.propellant_kg
    for index in range(steps+1):
        t = duration*index/steps
        sample = evaluate_terminal_reference(plan, plan["reference_start_time_s"]+t)
        endpoints = []
        for fuel in (0., initial.propellant_kg):
            state, cg_a, props = _reference_state(sample, initial, booster, profile, catch, fuel)
            observed = dyn.observe(state, booster)
            result["reference_model_evaluations"] += 1
            endpoints.append((state, cg_a, props, observed))
        altitudes = [item[3]["altitude_m"] for item in endpoints]
        density_altitude_lower = min(altitudes)-com_span
        rho_max = max(env.standard_atmosphere(density_altitude_lower)["density_kg_m3"],
                      *(item[3]["atmosphere"]["density_kg_m3"] for item in endpoints))
        velocity_uncertainty = com_dot_max+env.EARTH_ROTATION_RAD_S*com_span
        fixed_bounds = [_fixed_and_fin_bounds(item[3], booster, rho_max, velocity_uncertainty) for item in endpoints]
        required, perturbations, fin_bounds = [], [], []
        gravity_norm_max, radius_min = 0., math.inf
        for (state, cg_a, props, observed), (fixed, perturbation, fins) in zip(endpoints, fixed_bounds):
            gravity = env.gravity_acceleration(state.r_eci_m, j2=True)
            required.append(env.add(dyn.inverse_rotate(state.q_body_to_eci,
                env.scale(env.add(cg_a, env.scale(gravity, -1.)), props.mass_kg)), env.scale(fixed, -1.)))
            perturbations.append(perturbation)
            fin_bounds.append(fins)
            gravity_norm_max, radius_min = max(gravity_norm_max, env.norm(gravity)), min(radius_min, env.norm(state.r_eci_m))
        omega = env.norm(sample["pose_rate_eci_rad_s"])
        # m*COM is affine in propellant, so pure lever terms are bracketed by
        # the endpoint masses. Unknown depletion kinematics and small gravity
        # variation get a separate conservative force allowance.
        geometric_uncertainty = mass_max*(2*omega*com_dot_max+com_ddot_max
            +6*gravity_norm_max*com_span/radius_min)
        uncertainty = max(perturbations)+max(fin_bounds)+geometric_uncertainty+outside_residual
        lower = [min(force[i] for force in required)-uncertainty-rcs_component[i] for i in range(3)]
        upper = [max(force[i] for force in required)+uncertainty+rcs_component[i] for i in range(3)]
        startup = [actuator_endpoint(actual.throttle, 1., spec.throttle_time_constant_s, spec.throttle_rate_per_s, t)
            for spec, actual in zip(booster.engines[:13], initial.engine_states[:13])]
        capacity = sum(spec.max_thrust_n*throttle for i, (spec, throttle) in enumerate(zip(booster.engines[:13], startup))
                       if initial.engine_states[i].available)
        zmax = min(upper[2], capacity)
        roundoff = 128*math.ulp(max(1., capacity, *(abs(v) for v in lower+upper)))
        failures = []
        if zmax < max(0., lower[2])-roundoff:
            failures.append("main_positive_axial_force_or_startup_capacity")
        for axis, ratio in ((0, x_ratio), (1, y_ratio)):
            if lower[axis] > ratio*max(0., zmax)+roundoff or upper[axis] < -ratio*max(0., zmax)-roundoff:
                failures.append("main_gimbal_force_cone_axis_"+str(axis))
        lower_norm = math.sqrt(sum(max(0., lo, -hi)**2 for lo, hi in zip(lower, upper)))
        lower_force_norms.append(lower_norm)
        # A separate, openly approximate fuel estimate: exact configured air
        # loads, held origin fin angles, zero future COM derivatives. It is not
        # substituted for the conservative all-fin/all-mass exclusion above.
        nominal_state, nominal_a, nominal_props = _reference_state(sample, initial, booster, profile, catch, nominal_remaining)
        nominal_loads = dyn.observe(nominal_state, booster)
        result["reference_model_evaluations"] += 1
        nominal_force = env.add(env.scale(env.add(nominal_a,
            env.scale(env.gravity_acceleration(nominal_state.r_eci_m, j2=True), -1.)), nominal_props.mass_kg),
            env.scale(dyn.rotate(nominal_state.q_body_to_eci, nominal_loads["aero_force_body_n"]), -1.))
        nominal_forces.append(nominal_force)
        magnitude = env.norm(nominal_force)
        flow = magnitude/(profile["booster"]["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2)
        if index < steps:
            spend = flow*duration/steps
            nominal_fuel += spend
            nominal_remaining = max(0., nominal_remaining-spend)
        hull = [hull_clearance(item[0], booster, profile["geometry"]["booster_length_m"],
            profile["geometry"]["radius_m"])["signed_clearance_m"] for item in endpoints]
        if min(hull) < 0:
            failures.append("reference_hull_crosses_actual_ellipsoid")
        if failures:
            result["known_reference_violations"].append({"node_index": index, "elapsed_s": t, "codes": failures})
        result["nodes"].append({"index": index, "elapsed_s": t, "reference_sample": sample,
            "mass_endpoints_kg": [mass_min, mass_max], "required_force_body_endpoints_n": required,
            "fixed_panel_force_perturbation_bound_n": max(perturbations), "movable_fin_force_norm_bound_n": max(fin_bounds),
            "geometric_depletion_force_uncertainty_n": geometric_uncertainty,
            "rcs_force_component_bound_body_n": rcs_component, "outside13_residual_force_norm_bound_n": outside_residual,
            "required_main_force_box_lower_body_n": lower, "required_main_force_box_upper_body_n": upper,
            "main_startup_throttle_upper": startup, "main_startup_capacity_n": capacity,
            "gimbal_cone_x_over_z": x_ratio, "gimbal_cone_y_over_z": y_ratio,
            "force_roundoff_n": roundoff, "violations": failures, "necessary_main_force_norm_lower_n": lower_norm,
            "nominal_required_force_eci_n": nominal_force, "nominal_force_magnitude_n": magnitude,
            "hull_ground_clearance_endpoints_m": hull,
            "minimum_enabled_main_throttle": min(spec.min_throttle for spec in booster.engines[:13]),
            "aerodynamic_bound_source_inputs": [{"altitude_m": item[3]["altitude_m"],
                "density_kg_m3": item[3]["atmosphere"]["density_kg_m3"],
                "panel_loads": item[3]["panel_loads"], "r_eci_m": item[0].r_eci_m,
                "v_eci_mps": item[0].v_eci_mps, "q_body_to_eci": item[0].q_body_to_eci,
                "omega_body_rad_s": item[0].omega_body_rad_s, "propellant_kg": item[0].propellant_kg,
                "cg_reference_acceleration_eci_mps2": item[1], "aero_loads_independently_replayed": False}
                for item in endpoints],
            "density_bound_altitude_lower_m": density_altitude_lower, "density_upper_bound_kg_m3": rho_max,
            "air_velocity_uncertainty_norm_bound_mps": velocity_uncertainty,
            "discrete_main_counts_admitted": list(range(14)),
            "exact_minimum_throttle_history_feasibility_established": False})
    dt = duration/steps
    axis_rates = []
    for i, force in enumerate(nominal_forces):
        magnitude = env.norm(force)
        left, right = max(0, i-1), min(steps, i+1)
        difference = env.scale(env.add(nominal_forces[right], env.scale(nominal_forces[left], -1.)),
                               1/((right-left)*dt))
        threshold = math.sqrt(math.ulp(1.))*max(1., result["nodes"][i]["main_startup_capacity_n"])
        if magnitude > threshold:
            axis = env.scale(force, 1/magnitude)
            perpendicular = env.add(difference, env.scale(axis, -env.dot(axis, difference)))
            axis_rate = env.scale(perpendicular, 1/magnitude)
            rate = env.cross(axis, axis_rate)
            axis_rates.append(rate)
            result["nodes"][i].update(nominal_force_axis_rate_eci_rad_s=rate,
                nominal_force_axis_rate_norm_rad_s=env.norm(rate),
                nominal_force_axis_rate_exceeds_existing_cap=env.norm(rate) > plan["maximum_reference_rate_rad_s"],
                nominal_force_axis_ill_conditioned=False)
        else:
            axis_rates.append(None)
            result["nodes"][i].update(nominal_force_axis_rate_eci_rad_s=None,
                nominal_force_axis_rate_norm_rad_s=None, nominal_force_axis_rate_exceeds_existing_cap=None,
                nominal_force_axis_ill_conditioned=True)
    for i, rate in enumerate(axis_rates):
        left, right = max(0, i-1), min(steps, i+1)
        if rate is not None and axis_rates[left] is not None and axis_rates[right] is not None:
            derivative = env.scale(env.add(axis_rates[right], env.scale(axis_rates[left], -1.)), 1/((right-left)*dt))
            result["nodes"][i]["nominal_force_axis_acceleration_norm_rad_s2"] = env.norm(derivative)
        else:
            result["nodes"][i]["nominal_force_axis_acceleration_norm_rad_s2"] = None
    lower_fuel = sum((lower_force_norms[i]+lower_force_norms[i+1])*.5*duration/steps
        /(profile["booster"]["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2) for i in range(steps))
    # Trapezoidal sampling is a diagnostic integral, not a rigorous continuous
    # lower integral. Therefore fuel screening alone is not a proved exclusion.
    horizon = ((catch["initial_pin_clearance_m"]+catch["arm_half_width_m"])
        /(-.5*catch["initial_vertical_speed_mps"])+4*profile["actuators"]["throttle_tau_s"])
    reserve_min = mass_min*9.81/(profile["booster"]["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2)*horizon
    result.update(mass_interval_kg=[mass_min, mass_max], com_span_m=com_span,
        com_velocity_bound_mps=com_dot_max, com_acceleration_bound_mps2=com_ddot_max,
        sampled_lower_fuel_integral_kg=lower_fuel, sampled_lower_fuel_integral_continuously_certified=False,
        nominal_unconstrained_fuel_kg=nominal_fuel, nominal_fuel_assumptions="held_origin_fins_and_zero_future_com_derivatives",
        support_reserve_minimum_mass_kg=reserve_min, support_reserve_horizon_s=horizon,
        support_reserve_includes_existing_four_tau_shutdown=True,
        nominal_fuel_plus_minimum_support_reserve_kg=nominal_fuel+reserve_min,
        initial_fuel_kg=initial.propellant_kg, nominal_fuel_budget_exceeded=nominal_fuel+reserve_min > initial.propellant_kg,
        nominal_fuel_budget_is_reference_feasibility_proof=False,
        pose_peak_rate_bound_passed=plan["pose_peak_rate_rad_s"] <= plan["maximum_reference_rate_rad_s"],
        pose_peak_acceleration_bound_passed=plan["pose_peak_acceleration_rad_s2"] <= plan["maximum_reference_acceleration_rad_s2"],
        raw_pose_boundary_rate_matches_carried_request=plan["raw_pose_boundary_is_carried_rate"],
        force_axis_rate_derivative_continuously_certified=False,
        discrete_minimum_throttle_history_feasibility_established=False)
    if not result["pose_peak_rate_bound_passed"] or not result["pose_peak_acceleration_bound_passed"]:
        result["known_reference_violations"].append({"node_index": None, "codes": ["raw_pose_rate_or_acceleration_bound"]})
    allowed = not result["known_reference_violations"]
    result.update(status="known_reference_bound_failed" if not allowed else "necessary_bounds_not_excluded_unproved",
        candidate_plant_call_allowed=False, reason="fixed_reference_inconsistent_with_declared_model_bounds" if not allowed else None)
    return shooting.saved(result)
