"""Independent v2 sampled necessary-bound proof from immutable stored inputs.

No model observer, integrator or screen producer is imported. Hypothetical
reference geometry, configured plate loads, actuator envelopes and repaired
interval arithmetic are reconstructed. A disjoint force box rejects this
reference family at that sampled point, not the vehicle or every controller.
Sampled fuel and nominal force derivatives are never continuous certificates.
"""

from __future__ import annotations

import math

from .starship_booster_recovery_verifier import (
    _Invalid as _RecoveryInvalid,
    _json,
    _state,
    _tower,
    _cross,
)
from .starship_fixed_terminal_reference_verifier import (
    CONFIG,
    digest,
    _mass,
    _add,
    _scale,
    _dot,
    _norm,
    expected_reference_sample,
    verify_fixed_terminal_reference,
)
from .starship_terminal_wrench_verifier import (
    _specs,
    _rotate,
    _endpoint,
    _require,
    _near,
    _compare,
    _vector,
)

SCHEMA = "missionos.starship_fixed_terminal_reference_screen.v2"
_FLAGS = {
    "reference_is_achieved_state",
    "vehicle_global_infeasibility_established",
    "joint_tracking_feasibility_established",
    "hardware_validated",
    "arrival_admitted",
    "support_admitted",
    "tower_structure_geometry_available",
    "tower_interference_validated",
}
_COMMON = {
    "schema",
    "reference_sha256",
    "origin_context_sha256",
    "configuration",
    "reference_model_evaluations",
    "physical_plant_integrations",
    "nodes",
    "known_reference_violations",
    "unknowns",
    "status",
    "candidate_plant_call_allowed",
    "reason",
} | _FLAGS
_SUMMARIES = {
    "mass_interval_kg",
    "com_span_m",
    "com_velocity_bound_mps",
    "com_acceleration_bound_mps2",
    "sampled_lower_fuel_integral_kg",
    "sampled_lower_fuel_integral_continuously_certified",
    "nominal_unconstrained_fuel_kg",
    "nominal_fuel_assumptions",
    "support_reserve_minimum_mass_kg",
    "support_reserve_horizon_s",
    "support_reserve_includes_existing_four_tau_shutdown",
    "nominal_fuel_plus_minimum_support_reserve_kg",
    "initial_fuel_kg",
    "nominal_fuel_budget_exceeded",
    "nominal_fuel_budget_is_reference_feasibility_proof",
    "pose_peak_rate_bound_passed",
    "pose_peak_acceleration_bound_passed",
    "raw_pose_boundary_rate_matches_carried_request",
    "force_axis_rate_derivative_continuously_certified",
    "discrete_minimum_throttle_history_feasibility_established",
}
_REPAIR = {
    "original_screen_sha256",
    "original_reference_model_evaluations",
    "new_reference_model_evaluations",
    "bounds_repaired_from_stored_endpoints",
}
_NODE = {
    "index",
    "elapsed_s",
    "reference_sample",
    "mass_endpoints_kg",
    "required_force_body_endpoints_n",
    "fixed_panel_force_perturbation_bound_n",
    "movable_fin_force_norm_bound_n",
    "geometric_depletion_force_uncertainty_n",
    "rcs_force_component_bound_body_n",
    "outside13_residual_force_norm_bound_n",
    "required_main_force_box_lower_body_n",
    "required_main_force_box_upper_body_n",
    "main_startup_throttle_upper",
    "main_startup_capacity_n",
    "gimbal_cone_x_over_z",
    "gimbal_cone_y_over_z",
    "force_roundoff_n",
    "violations",
    "necessary_main_force_norm_lower_n",
    "nominal_required_force_eci_n",
    "nominal_force_magnitude_n",
    "hull_ground_clearance_endpoints_m",
    "minimum_enabled_main_throttle",
    "aerodynamic_bound_source_inputs",
    "density_bound_altitude_lower_m",
    "density_upper_bound_kg_m3",
    "air_velocity_uncertainty_norm_bound_mps",
    "discrete_main_counts_admitted",
    "exact_minimum_throttle_history_feasibility_established",
    "nominal_force_axis_rate_eci_rad_s",
    "nominal_force_axis_rate_norm_rad_s",
    "nominal_force_axis_rate_exceeds_existing_cap",
    "nominal_force_axis_ill_conditioned",
    "nominal_force_axis_acceleration_norm_rad_s2",
}
_NODE_REPAIR = {
    "density_bound_altitude_upper_m",
    "density_lower_bound_kg_m3",
    "panel_speed_upper_bounds_mps",
    "radius_lower_bound_m",
    "gravity_acceleration_norm_upper_bound_mps2",
}


def _density(altitude):
    radius = 6356766.0
    gas = 8314.32 / 28.9644
    z = max(0.0, min(86000.0, altitude))
    h = radius * z / (radius + z)
    temperature, pressure = 288.15, 101325.0
    heights = (0.0, 11000.0, 20000.0, 32000.0, 47000.0, 51000.0, 71000.0, 84852.04584490575)
    for lo, hi, lapse in zip(
        heights, heights[1:], (-0.0065, 0.0, 0.001, 0.0028, 0.0, -0.0028, -0.002)
    ):
        if h <= lo:
            break
        dh = min(h, hi) - lo
        next_t = temperature + lapse * dh
        pressure *= (
            math.exp(-9.80665 * dh / (gas * temperature))
            if lapse == 0
            else (temperature / next_t) ** (9.80665 / (gas * lapse))
        )
        temperature = next_t
        if h <= hi:
            break
    if altitude > 86000:
        pressure *= math.exp(-(altitude - 86000) / 15000.0)
    if altitude >= 1000000:
        pressure = 0.0
    return pressure / (gas * temperature)


def _altitude(r):
    radial = math.hypot(*r[:2])
    e2 = (1 / 298.257223563) * (2 - 1 / 298.257223563)
    lat = math.atan2(r[2], radial * (1 - e2))
    for _ in range(12):
        s = math.sin(lat)
        n = 6378137.0 / math.sqrt(1 - e2 * s * s)
        new = math.atan2(r[2] + e2 * n * s, radial)
        if abs(new - lat) < 1e-14:
            lat = new
            break
        lat = new
    return (
        radial * math.cos(lat)
        + r[2] * math.sin(lat)
        - 6378137.0 * math.sqrt(1 - e2 * math.sin(lat) ** 2)
    )


def _gravity(r):
    radius = _norm(r)
    mu = 3.986004418e14
    z2 = (r[2] / radius) ** 2
    factor = 1.5 * 1.08262982e-3 * mu * 6378137.0**2 / radius**5
    return _add(
        _scale(r, -mu / radius**3),
        [factor * r[0] * (5 * z2 - 1), factor * r[1] * (5 * z2 - 1), factor * r[2] * (5 * z2 - 3)],
    )


def _panels(profile):
    p, a, g = profile["booster"], profile["aero"], profile["geometry"]
    result = []
    for axis in range(3):
        result.append(
            {
                "name": f"hull_{axis}",
                "position": [0.0, 0.0, p["hull_cp_z_m"]],
                "normal": [float(i == axis) for i in range(3)],
                "hinge": [1.0, 0.0, 0.0],
                "area": math.pi * g["radius_m"] ** 2
                if axis == 2
                else 2 * g["radius_m"] * g["booster_length_m"],
                "cn": a["axial_cd"] if axis == 2 else a["crossflow_cd"],
                "ct": 0.0,
                "movable": False,
            }
        )
    for spec in p["grid_fin_panels"]:
        result.append(
            {
                "name": spec["name"],
                "position": spec["position_body_m"],
                "normal": spec["normal_body"],
                "hinge": spec.get("hinge_axis_body", [1.0, 0.0, 0.0]),
                "area": spec["area_m2"],
                "cn": a["panel_normal_coefficient"],
                "ct": a["panel_tangential_coefficient"],
                "movable": True,
            }
        )
    return result


def _hypothetical(sample, state, profile, catch, fuel):
    mass, com, _, _ = _mass(profile, fuel)
    q = sample["pose_q_body_to_eci"]
    inverse = [q[0], -q[1], -q[2], -q[3]]
    omega = _rotate(inverse, sample["pose_rate_eci_rad_s"])
    alpha = _rotate(inverse, sample["pose_acceleration_eci_rad_s2"])
    lever = _add(
        [sum(p[i] for p in catch["support_points_body_m"]) / 2 for i in range(3)], com, -1.0
    )
    origin, axes = _tower(profile, sample["time_s"])

    def world(v):
        return [sum(v[j] * axes[j][i] for j in range(3)) for i in range(3)]

    pinr = _add(origin, world(sample["position_enu_m"]))
    localv = world(sample["velocity_enu_mps"])
    locala = world(sample["acceleration_enu_mps2"])
    earth = [0.0, 0.0, 7.292115e-5]
    pinv = _add(_cross(earth, pinr), localv)
    pina = _add(_add(_cross(earth, _cross(earth, pinr)), _cross(earth, localv), 2.0), locala)
    r = _add(pinr, _rotate(q, lever), -1.0)
    v = _add(pinv, _rotate(q, _cross(omega, lever)), -1.0)
    acceleration = _add(
        pina, _rotate(q, _add(_cross(alpha, lever), _cross(omega, _cross(omega, lever)))), -1.0
    )
    return mass, com, q, omega, r, v, acceleration, pinr


def _panel_inputs(item, sample, state, profile, catch, fuel, panels):
    keys = {
        "altitude_m",
        "density_kg_m3",
        "panel_loads",
        "r_eci_m",
        "v_eci_mps",
        "q_body_to_eci",
        "omega_body_rad_s",
        "propellant_kg",
        "cg_reference_acceleration_eci_mps2",
        "aero_loads_independently_replayed",
    }
    _require(
        type(item) is dict
        and set(item) == keys
        and item["aero_loads_independently_replayed"] is False
        and item["propellant_kg"] == fuel,
        "stored_hypothetical_load_scope",
    )
    mass, com, q, omega, r, v, cga, pinr = _hypothetical(sample, state, profile, catch, fuel)
    for key, value in (
        ("r_eci_m", r),
        ("v_eci_mps", v),
        ("q_body_to_eci", q),
        ("omega_body_rad_s", omega),
        ("cg_reference_acceleration_eci_mps2", cga),
    ):
        _compare(item[key], value, absolute=1e-6)
    altitude = _altitude(r)
    rho = _density(altitude)
    _require(
        _near(item["altitude_m"], altitude, absolute=1e-6)
        and _near(item["density_kg_m3"], rho, absolute=1e-12),
        "configured_atmosphere_at_stored_reference",
    )
    inverse = [q[0], -q[1], -q[2], -q[3]]
    air = _rotate(inverse, _add(v, _cross([0.0, 0.0, 7.292115e-5], r), -1.0))
    rows = item["panel_loads"]
    _require(type(rows) is list and len(rows) == len(panels), "panel_count")
    fixed = [0.0, 0.0, 0.0]
    for index, (row, panel) in enumerate(zip(rows, panels)):
        _require(
            type(row) is dict
            and set(row)
            == {
                "name",
                "force_body_n",
                "torque_body_nm",
                "normal_body",
                "local_air_velocity_body_mps",
            }
            and row["name"] == panel["name"],
            "configured_panel_identity",
        )
        velocity = _add(air, _cross(omega, _add(panel["position"], com, -1.0)))
        angle = state["flap_angles_rad"][index]
        h = panel["hinge"]
        length = _norm(h)
        h = _scale(h, 1 / length)
        normal = _rotate([math.cos(angle / 2), *_scale(h, math.sin(angle / 2))], panel["normal"])
        vn = _dot(velocity, normal)
        tangent = _add(velocity, normal, -vn)
        scale = 0.5 * rho * panel["area"]
        force = _add(
            _scale(normal, -scale * panel["cn"] * vn * abs(vn)),
            _scale(tangent, -scale * panel["ct"] * _norm(tangent)),
        )
        _compare(row["local_air_velocity_body_mps"], velocity, absolute=1e-6)
        _compare(row["normal_body"], normal)
        _compare(row["force_body_n"], force, absolute=1e-4)
        _compare(
            row["torque_body_nm"], _cross(_add(panel["position"], com, -1.0), force), absolute=1e-3
        )
        if not panel["movable"]:
            fixed = _add(fixed, row["force_body_n"])
    required = _add(_rotate(inverse, _scale(_add(cga, _gravity(r), -1.0), mass)), fixed, -1.0)
    # A mass-independent enclosing sphere about the fixed support midpoint
    # certifies positive ground clearance when its lower level-set bound>0.
    support = [sum(p[i] for p in catch["support_points_body_m"]) / 2 for i in range(3)]
    farthest = math.sqrt(
        profile["geometry"]["radius_m"] ** 2
        + max(abs(support[2]), abs(profile["geometry"]["booster_length_m"] - support[2])) ** 2
    )
    polar = 6378137.0 * (1 - 1 / 298.257223563)

    def weighted(x):
        return math.sqrt(x[0] ** 2 + x[1] ** 2 + (6378137.0 / polar * x[2]) ** 2)

    hull_lower = weighted(pinr) - 6378137.0 - farthest * (6378137.0 / polar)
    return required, hull_lower


def _check(screen, original, plan, snapshot, profile, catch, prior_checkpoint, source_run):
    for v in (screen, original, plan, snapshot, profile, catch):
        _json(v, maximum_nodes=32_000_000)
    reference = verify_fixed_terminal_reference(
        plan, snapshot, profile, catch, prior_checkpoint=prior_checkpoint, source_run=source_run
    )
    _require(reference["passed"], "independent_reference_contract_failed")
    _require(
        type(original) is dict
        and original.get("schema") == "missionos.starship_fixed_terminal_reference_screen.v1"
        and original.get("reference_sha256") == digest(plan)
        and original.get("origin_context_sha256") == digest(snapshot),
        "immutable_original_screen_binding",
    )
    _require(
        type(screen) is dict
        and screen.get("schema") == SCHEMA
        and screen.get("configuration") == CONFIG
        and screen.get("reference_sha256") == digest(plan)
        and screen.get("origin_context_sha256") == digest(snapshot)
        and screen.get("original_screen_sha256") == digest(original)
        and type(screen.get("reference_model_evaluations")) is int
        and screen["reference_model_evaluations"] == 0
        and type(screen.get("new_reference_model_evaluations")) is int
        and screen["new_reference_model_evaluations"] == 0
        and type(screen.get("physical_plant_integrations")) is int
        and screen["physical_plant_integrations"] == 0
        and screen.get("bounds_repaired_from_stored_endpoints") is True
        and all(screen.get(k) is False for k in _FLAGS),
        "derived_screen_budget_or_claim_boundary",
    )
    _require(
        screen.get("unknowns") == original.get("unknowns")
        and type(screen.get("unknowns")) is list
        and len(screen["unknowns"]) == 2,
        "missing_unmodeled_structure_and_future_load_limits",
    )
    if original.get("status") == "deferred":
        _require(
            set(screen) == _COMMON | _REPAIR
            and screen["nodes"] == []
            and screen["known_reference_violations"] == []
            and screen["original_reference_model_evaluations"] == 0
            and screen["status"] == "deferred"
            and screen["candidate_plant_call_allowed"] is False
            and screen["reason"] == "nonzero_declared_wind_interval_bound_not_implemented",
            "invalid_unknown_wind_defer",
        )
        return [], False, False
    _require(
        set(screen) == _COMMON | _SUMMARIES | _REPAIR
        and type(screen["original_reference_model_evaluations"]) is int
        and screen["original_reference_model_evaluations"]
        == original["reference_model_evaluations"]
        == 303
        and len(screen["nodes"]) == len(original["nodes"]) == 101,
        "bounded_complete_reused_screen_nodes",
    )
    initial = snapshot["state"]
    _state(initial, profile)
    specs = _specs(profile)
    panels = _panels(profile)
    fuel = initial["propellant_kg"]
    m0, c0, dc0, _ = _mass(profile, 0.0)
    m1, c1, _, _ = _mass(profile, fuel)
    span = _norm(_add(c1, c0, -1.0))
    flow = 0.0
    flowdot = 0.0
    rcs = [0.0, 0.0, 0.0]
    outside = 0.0
    for i, (spec, actual) in enumerate(zip(specs, initial["engine_states"])):
        if not actual["available"]:
            continue
        isp = profile["booster"]["engine_isp_s"] if i < 33 else profile["actuators"]["rcs_isp_s"]
        per = spec["thrust"] / (isp * 9.80665)
        if i < 13 or i >= 33:
            flow += per
            flowdot += per * min(spec["rate"], 1 / spec["tau"])
        else:
            flow += per * actual["throttle"]
            flowdot += per * min(spec["rate"], actual["throttle"] / spec["tau"])
            outside += spec["thrust"] * actual["throttle"]
        if i >= 33:
            rcs = _add(rcs, [spec["thrust"] * abs(x) for x in spec["direction"]])
    cdot = _norm(dc0) * flow
    cddot = _norm(dc0) * (flowdot + 2 * flow * flow / m0)
    dv = cdot + 7.292115e-5 * span
    for key, value in (
        ("com_span_m", span),
        ("com_velocity_bound_mps", cdot),
        ("com_acceleration_bound_mps2", cddot),
    ):
        _require(_near(screen[key], value), "all_mass_depletion_envelope")
    _compare(screen["mass_interval_kg"], [m0, m1])
    gimbal = math.radians(profile["actuators"]["max_gimbal_deg"])
    ratios = [math.tan(gimbal), math.tan(gimbal) / math.cos(gimbal)]
    violations = []
    lower_norms = []
    hull_spheres = []
    for index, (node, oldnode) in enumerate(zip(screen["nodes"], original["nodes"])):
        _require(
            type(node) is dict
            and set(node) == _NODE | _NODE_REPAIR
            and type(node["index"]) is int
            and node["index"] == index
            and node["elapsed_s"] == plan["duration_s"] * index / 100,
            "derived_node_closed_clock",
        )
        sample = expected_reference_sample(plan, plan["reference_start_time_s"] + node["elapsed_s"])
        from .starship_fixed_terminal_reference_verifier import verify_reference_sample

        _require(
            verify_reference_sample(node["reference_sample"], plan, sample["time_s"])["passed"],
            "derived_node_quintic_sample",
        )
        inputs = node["aerodynamic_bound_source_inputs"]
        _require(
            type(inputs) is list
            and len(inputs) == 2
            and inputs == oldnode["aerodynamic_bound_source_inputs"],
            "no_replaced_or_reevaluated_endpoint_loads",
        )
        required = []
        hulls = []
        for item, propellant in zip(inputs, (0.0, fuel)):
            force, hull = _panel_inputs(item, sample, initial, profile, catch, propellant, panels)
            required.append(force)
            hulls.append(hull)
        _require(
            node["required_force_body_endpoints_n"] == oldnode["required_force_body_endpoints_n"],
            "no_replaced_main_force_endpoints",
        )
        for a, b in zip(node["required_force_body_endpoints_n"], required):
            _compare(a, b, absolute=1e-3)
        altlo = min(x["altitude_m"] for x in inputs) - span
        althi = max(x["altitude_m"] for x in inputs) + span
        rhomax = max(_density(altlo), *(x["density_kg_m3"] for x in inputs))
        rhomin = min(_density(althi), *(x["density_kg_m3"] for x in inputs))
        speeds = [
            max(_norm(x["panel_loads"][j]["local_air_velocity_body_mps"]) for x in inputs)
            for j in range(len(panels))
        ]
        pert = fin = 0.0
        for panel, speed in zip(panels, speeds):
            coefficient = max(panel["cn"], panel["ct"])
            if panel["movable"]:
                fin += 0.5 * rhomax * panel["area"] * coefficient * (speed + dv) ** 2
            else:
                pert += (
                    rhomax * panel["area"] * coefficient * (speed + dv) * dv
                    + 0.5 * (rhomax - rhomin) * panel["area"] * coefficient * speed * speed
                )
        rlo = min(_norm(x["r_eci_m"]) for x in inputs) - span
        _require(rlo > 6378137.0 * 0.99, "outside_declared_near_earth_gravity_bound")
        gmax = 3.986004418e14 / rlo**2 * (1 + 6 * 1.08262982e-3 * (6378137.0 / rlo) ** 2)
        geometric = m1 * (
            2 * _norm(sample["pose_rate_eci_rad_s"]) * cdot + cddot + 6 * gmax * span / rlo
        )
        for key, value in (
            ("density_bound_altitude_lower_m", altlo),
            ("density_bound_altitude_upper_m", althi),
            ("density_upper_bound_kg_m3", rhomax),
            ("density_lower_bound_kg_m3", rhomin),
            ("radius_lower_bound_m", rlo),
            ("gravity_acceleration_norm_upper_bound_mps2", gmax),
            ("air_velocity_uncertainty_norm_bound_mps", dv),
            ("fixed_panel_force_perturbation_bound_n", pert),
            ("movable_fin_force_norm_bound_n", fin),
            ("geometric_depletion_force_uncertainty_n", geometric),
            ("outside13_residual_force_norm_bound_n", outside),
        ):
            _require(
                _near(node[key], value, absolute=1e-5),
                "global_density_speed_radius_or_force_allowance",
            )
        _compare(node["panel_speed_upper_bounds_mps"], speeds)
        _compare(node["rcs_force_component_bound_body_n"], rcs)
        uncertainty = pert + fin + geometric + outside
        forces = node["required_force_body_endpoints_n"]
        lo = [min(x[i] for x in forces) - uncertainty - rcs[i] for i in range(3)]
        hi = [max(x[i] for x in forces) + uncertainty + rcs[i] for i in range(3)]
        _compare(node["required_main_force_box_lower_body_n"], lo)
        _compare(node["required_main_force_box_upper_body_n"], hi)
        startup = [
            _endpoint(x["throttle"], 1.0, spec["tau"], spec["rate"], node["elapsed_s"])
            for x, spec in zip(initial["engine_states"][:13], specs[:13])
        ]
        cap = sum(
            spec["thrust"] * value
            for value, spec, x in zip(startup, specs[:13], initial["engine_states"][:13])
            if x["available"]
        )
        _compare(node["main_startup_throttle_upper"], startup)
        _require(
            _near(node["main_startup_capacity_n"], cap), "available_engine_spool_startup_envelope"
        )
        _require(
            _near(node["gimbal_cone_x_over_z"], ratios[0], absolute=1e-12)
            and _near(node["gimbal_cone_y_over_z"], ratios[1], absolute=1e-12),
            "unchanged_physical_gimbal_cone",
        )
        tolerance = 128 * math.ulp(max(1.0, cap, *(abs(x) for x in lo + hi)))
        _require(node["force_roundoff_n"] == tolerance, "machine_roundoff_not_new_force_tolerance")
        zmax = min(hi[2], cap)
        codes = []
        if zmax < max(0.0, lo[2]) - tolerance:
            codes.append("main_positive_axial_force_or_startup_capacity")
        for axis, ratio in enumerate(ratios):
            if (
                lo[axis] > ratio * max(0.0, zmax) + tolerance
                or hi[axis] < -ratio * max(0.0, zmax) - tolerance
            ):
                codes.append("main_gimbal_force_cone_axis_" + str(axis))
        # This proof admits no ground-collision exclusion based on an unchecked
        # scalar minimum. Positive enclosing-sphere clearance is sufficient.
        _require(
            _vector(node["hull_ground_clearance_endpoints_m"], 2)
            and all(
                a >= b - 1e-6 for a, b in zip(node["hull_ground_clearance_endpoints_m"], hulls)
            ),
            "hypothetical_hull_lower_bound",
        )
        _require(min(hulls) > 0.0, "hull_exact_minimum_needed_before_negative_geometry_exclusion")
        lowernorm = math.sqrt(sum(max(0.0, a, -b) ** 2 for a, b in zip(lo, hi)))
        lower_norms.append(lowernorm)
        hull_spheres.append(min(hulls))
        _require(
            _near(node["necessary_main_force_norm_lower_n"], lowernorm)
            and node["violations"] == codes
            and node["discrete_main_counts_admitted"] == list(range(14))
            and node["minimum_enabled_main_throttle"] == 0.4
            and node["exact_minimum_throttle_history_feasibility_established"] is False,
            "necessary_point_exclusion_not_throttle_history_certificate",
        )
        if codes:
            violations.append({"node_index": index, "elapsed_s": node["elapsed_s"], "codes": codes})
    rates = plan["pose_peak_rate_rad_s"] <= plan["maximum_reference_rate_rad_s"]
    accel = plan["pose_peak_acceleration_rad_s2"] <= plan["maximum_reference_acceleration_rad_s2"]
    if not rates or not accel:
        violations.append({"node_index": None, "codes": ["raw_pose_rate_or_acceleration_bound"]})
    _require(
        screen["known_reference_violations"] == violations
        and screen["pose_peak_rate_bound_passed"] is rates
        and screen["pose_peak_acceleration_bound_passed"] is accel,
        "complete_sampled_exclusion_list",
    )
    integral = sum(
        (lower_norms[i] + lower_norms[i + 1])
        * 0.5
        * plan["duration_s"]
        / 100
        / (profile["booster"]["engine_isp_s"] * 9.80665)
        for i in range(100)
    )
    horizon = (catch["initial_pin_clearance_m"] + catch["arm_half_width_m"]) / (
        -0.5 * catch["initial_vertical_speed_mps"]
    ) + 4 * profile["actuators"]["throttle_tau_s"]
    reserve = m0 * 9.81 / (profile["booster"]["engine_isp_s"] * 9.80665) * horizon
    _require(
        _near(screen["sampled_lower_fuel_integral_kg"], integral)
        and screen["sampled_lower_fuel_integral_continuously_certified"] is False
        and _near(screen["support_reserve_minimum_mass_kg"], reserve)
        and screen["support_reserve_horizon_s"] == horizon
        and screen["support_reserve_includes_existing_four_tau_shutdown"] is True
        and screen["nominal_unconstrained_fuel_kg"] == original["nominal_unconstrained_fuel_kg"]
        and screen["nominal_fuel_assumptions"] == "held_origin_fins_and_zero_future_com_derivatives"
        and screen["nominal_fuel_budget_is_reference_feasibility_proof"] is False
        and screen["force_axis_rate_derivative_continuously_certified"] is False
        and screen["discrete_minimum_throttle_history_feasibility_established"] is False,
        "sampled_or_nominal_quantities_cannot_certify_continuous_feasibility",
    )
    _require(
        screen["nominal_fuel_plus_minimum_support_reserve_kg"]
        == screen["nominal_unconstrained_fuel_kg"] + reserve
        and screen["initial_fuel_kg"] == fuel
        and screen["nominal_fuel_budget_exceeded"]
        is (screen["nominal_unconstrained_fuel_kg"] + reserve > fuel)
        and screen["raw_pose_boundary_rate_matches_carried_request"]
        is plan["raw_pose_boundary_is_carried_rate"],
        "nominal_fuel_and_raw_rate_labels",
    )
    allowed = not violations
    _require(
        screen["candidate_plant_call_allowed"] is False
        and screen["status"]
        == ("necessary_bounds_not_excluded_unproved" if allowed else "known_reference_bound_failed")
        and screen["reason"]
        == (None if allowed else "fixed_reference_inconsistent_with_declared_model_bounds"),
        "screen_stop_condition",
    )
    return violations, allowed, min(hull_spheres) > 0


def verify_terminal_reference_screen(
    screen,
    original_screen,
    plan,
    snapshot,
    profile,
    catch_config,
    *,
    prior_checkpoint=None,
    source_run=None,
):
    result = {
        "schema": "missionos.starship_terminal_reference_screen_verification.v1",
        "passed": False,
        "arithmetic_passed": False,
        "past_source_anchors_bound": False,
        "source_bound_arithmetic_passed": False,
        "verification_scope": "sampled_bound_arithmetic_not_execution_admission",
        "physical_invocation_admitted": False,
        "nominal_force_and_fuel_independently_replayed": False,
        "nominal_rate_derivatives_independently_replayed": False,
        "issues": [],
        "this_reference_excluded_by_sampled_necessary_bound": False,
        "candidate_plant_call_allowed": False,
        "sampled_violation_count": 0,
        "configured_panel_load_arithmetic_checked": False,
        "continuous_fuel_bound_certified": False,
        "joint_reference_feasibility_established": False,
        "vehicle_global_infeasibility_established": False,
        "tower_interference_validated": False,
        "source_authenticated": False,
        "dynamics_replayed": False,
        "physical_plant_integrations_performed": 0,
        "arrival_admitted": False,
        "support_admitted": False,
        "physical_execution": False,
    }
    try:
        violations, allowed, hull = _check(
            screen,
            original_screen,
            plan,
            snapshot,
            profile,
            catch_config,
            prior_checkpoint,
            source_run,
        )
        result.update(
            passed=True,
            arithmetic_passed=True,
            past_source_anchors_bound=source_run is not None and prior_checkpoint is not None,
            source_bound_arithmetic_passed=source_run is not None and prior_checkpoint is not None,
            this_reference_excluded_by_sampled_necessary_bound=bool(violations),
            sampled_necessary_bounds_not_excluded=allowed,
            sampled_violation_count=len(violations),
            configured_panel_load_arithmetic_checked=screen["status"] != "deferred",
            hypothetical_hull_ground_sphere_bounds_positive=hull,
        )
    except (
        _RecoveryInvalid,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        RecursionError,
        ZeroDivisionError,
        AttributeError,
    ):
        result["issues"].append(
            "Invalid immutable-input repaired necessary-bound screen or claim boundary"
        )
    return result
