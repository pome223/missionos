"""Independent saved-state checks for a booster return and terminal handoff.

Only the independent catch checker is imported. Guidance, dynamics, control
allocation and producer helpers are deliberately not called. This module does
not establish the preceding launch, replay the integrator or validate hardware.
"""
from __future__ import annotations

import math
from itertools import product

from .starship_booster_catch_verifier import verify_catch

_A = 6_378_137.0
_E2 = (1 / 298.257223563) * (2 - 1 / 298.257223563)
_ROTATION = 7.292115e-5
_G0 = 9.80665
_STATE = ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s",
          "propellant_kg", "engine_states", "flap_angles_rad")
_POLICY = "predictive_return_v1"
_PHASES = ("recovery_boostback_slew", "recovery_boostback_burn", "recovery_powered_rate_settle",
           "recovery_entry_coast", "recovery_landing_13", "recovery_landing_5", "recovery_landing_3")
_TERMINATIONS = {"time_limit", "surface_contact", "angular_rate_envelope_exceeded",
                 "rate_settle_failed", "catch_handoff"}
_GUIDANCE = {"prediction_dt_s": .5, "prediction_horizon_s": 600., "entry_max_angle_deg": 60.,
             "point_model_entry_max_angle_deg": 30.,
             "entry_start_altitude_m": 80_000., "boostback_alignment_deg": 15.,
             "boostback_velocity_tolerance_mps": 12., "rate_settle_limit_rad_s": .003,
             "rate_settle_max_s": 30., "terminal_capture_height_m": 3.4, "terminal_position_tau_s": 6.,
             "maximum_full_coast_predictions": 24, "braking_max_tilt_deg": 60.,
             "sample_interval_s": .5, "maximum_duration_s": 1200.}


class _Invalid(Exception):
    def __init__(self, code, detail):
        self.issue = {"code": code, "detail": detail}


def _require(condition, code, detail):
    if not condition:
        raise _Invalid(code, detail)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) < 1e50


def _vector(value, count=3):
    return type(value) is list and len(value) == count and all(_number(x) for x in value)


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _norm(a):
    return math.sqrt(_dot(a, a))


def _add(a, b):
    return [x+y for x, y in zip(a, b)]


def _sub(a, b):
    return [x-y for x, y in zip(a, b)]


def _scale(a, k):
    return [x*k for x in a]


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _rotate(q, v):
    first = _cross(q[1:], v)
    second = _cross(q[1:], first)
    return [x+2*q[0]*a+2*b for x, a, b in zip(v, first, second)]


def _near(a, b, tolerance=1e-6):
    return _number(a) and _number(b) and abs(a-b) <= tolerance


def _same_state(a, b, code):
    _require(type(a) is dict and type(b) is dict and all(key in a and key in b and _same_value(a[key], b[key])
             for key in _STATE), code, "Time, pose, velocity, fuel and every actuator must be inherited exactly")


def _same_value(a, b):
    if _number(a) or _number(b):
        return _number(a) and _number(b) and a == b
    if type(a) is not type(b):
        return False
    if type(a) is dict:
        return a.keys() == b.keys() and all(_same_value(a[k], b[k]) for k in a)
    if type(a) is list:
        return len(a) == len(b) and all(_same_value(x, y) for x, y in zip(a, b))
    return a == b


def _json(value, *, maximum_nodes=5_000_000):
    # Offline macrostep campaigns may explicitly retain larger records. The
    # ordinary production checker keeps its original five-million-node bound.
    _require(type(maximum_nodes) is int and 0 < maximum_nodes <= 32_000_000,
             "input_limit", "Invalid bounded JSON traversal budget")
    pending, active, count = [(value, 0, False)], set(), 0
    while pending:
        item, depth, leaving = pending.pop()
        if leaving:
            active.remove(id(item))
            continue
        count += 1
        _require(count <= maximum_nodes and depth <= 40, "input_limit", "Bounded JSON traversal exceeded")
        if item is None or type(item) is bool:
            continue
        if type(item) in (int, float):
            _require(_number(item), "invalid_json", "Nonfinite or oversized number")
        elif type(item) is str:
            _require(len(item) <= 16000, "input_limit", "Oversized string")
        elif type(item) in (dict, list):
            _require(id(item) not in active and len(item) <= 100000, "input_limit", "Cyclic or oversized container")
            if type(item) is dict:
                _require(all(type(k) is str and len(k) <= 256 for k in item), "invalid_json", "Invalid object key")
            active.add(id(item))
            pending.append((item, depth, True))
            pending.extend((child, depth+1, False) for child in (item.values() if type(item) is dict else item))
        else:
            raise _Invalid("invalid_json", "Only persisted JSON values are accepted")


def _state(state, profile):
    _require(type(state) is dict and _number(state.get("time_s")) and 0 <= state["time_s"] <= 1e7,
             "state", "Invalid inherited clock")
    for name, size in (("r_eci_m", 3), ("v_eci_mps", 3), ("q_body_to_eci", 4), ("omega_body_rad_s", 3)):
        _require(_vector(state.get(name), size), "state", f"Invalid {name}")
    _require(_norm(state["r_eci_m"]) > 1 and _near(_dot(state["q_body_to_eci"], state["q_body_to_eci"]), 1, 1e-8),
             "state", "Invalid position or nonunit attitude")
    fuel = state.get("propellant_kg")
    _require(_number(fuel) and 0 <= fuel <= profile["booster"]["propellant_kg"], "state", "Invalid propellant")
    engines, fins = state.get("engine_states"), state.get("flap_angles_rad")
    count = profile["booster"]["engine_count"]
    _require(type(engines) is list and len(engines) == count+12, "actuators", "Missing main engine or RCS state")
    limit = math.radians(profile["actuators"]["max_gimbal_deg"])
    for index, engine in enumerate(engines):
        gimbal = limit if index < profile["booster"]["gimbal_engine_count"] else 0.
        _require(type(engine) is dict and type(engine.get("available")) is bool and
                 _number(engine.get("throttle")) and 0 <= engine["throttle"] <= 1 and
                 all(_number(engine.get(key)) and abs(engine[key]) <= gimbal+1e-8
                     for key in ("gimbal_x_rad", "gimbal_y_rad")), "actuators", "Invalid actual engine state")
    _require(type(fins) is list and len(fins) == 3+len(profile["booster"]["grid_fin_panels"]),
             "actuators", "Missing hull/grid-fin state")
    fin_limit = math.radians(profile["actuators"]["grid_fin_limit_deg"])
    _require(all(_number(x) and abs(x) <= (0. if i < 3 else fin_limit)+1e-8 for i, x in enumerate(fins)),
             "actuators", "Actual panel angle exceeds configured bounds")


def _flow_and_centroid(state, profile):
    booster, actuators = profile["booster"], profile["actuators"]
    dry, fuel = booster["dry_mass_kg"], state["propellant_kg"]
    mass = dry+fuel
    com = (dry*booster["dry_com_z_m"]+fuel*booster["tank_center_z_m"])/mass
    flow = 0.
    if fuel > 0:
        for index, engine in enumerate(state["engine_states"]):
            if not engine["available"]:
                continue
            main = index < booster["engine_count"]
            thrust = booster["engine_thrust_n"] if main else actuators["rcs_thrust_n"]
            isp = booster["engine_isp_s"] if main else actuators["rcs_isp_s"]
            flow += engine["throttle"]*thrust/(isp*_G0)
    rate = -flow*dry*(booster["tank_center_z_m"]-booster["dry_com_z_m"])/(mass*mass)
    return mass, [0., 0., com], [0., 0., rate], flow


def _tower(profile, time_s):
    lat = math.radians(profile["launch"]["latitude_deg"])
    lon = math.radians(profile["launch"]["longitude_deg"])+_ROTATION*time_s
    sl, cl, so, co = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    radius = _A/math.sqrt(1-_E2*sl*sl)
    return [radius*cl*co, radius*cl*so, radius*(1-_E2)*sl], [
        [-so, co, 0.], [-sl*co, -sl*so, cl], [cl*co, cl*so, sl]]


def _arrival(state, profile, config):
    """Reconstruct actual material points; no truth trajectory is reset."""
    origin, axes = _tower(profile, state["time_s"])
    mass, centroid, centroid_rate, _ = _flow_and_centroid(state, profile)
    pins = []
    for point in config["support_points_body_m"]:
        lever = _sub(point, centroid)
        position = _add(state["r_eci_m"], _rotate(state["q_body_to_eci"], lever))
        velocity = _add(state["v_eci_mps"], _rotate(state["q_body_to_eci"],
                        _sub(_cross(state["omega_body_rad_s"], lever), centroid_rate)))
        relative = _sub(velocity, _cross([0., 0., _ROTATION], position))
        enu = [_dot(_sub(position, origin), axis) for axis in axes]
        pins.append({"position_enu_m": enu, "height_above_support_m": enu[2]-config["support_height_m"],
                     "velocity_enu_mps": [_dot(relative, axis) for axis in axes]})
    midpoint = _scale(_add(pins[0]["position_enu_m"], pins[1]["position_enu_m"]), .5)
    body_up = _rotate(state["q_body_to_eci"], [0., 0., 1.])
    body_x = _rotate(state["q_body_to_eci"], [1., 0., 0.])
    def angle(dot):
        return math.degrees(math.acos(max(-1., min(1., dot))))
    lever_z = max(abs(p[2]-centroid[2]) for p in config["support_points_body_m"])
    _require(lever_z > 0, "handoff_geometry", "Surrogate supports must lie above the current centroid")
    tilt_limit = math.degrees(math.atan(config["arm_half_width_m"]*.5/lever_z))
    horizon = ((config["initial_pin_clearance_m"]+config["arm_half_width_m"])
               /(-.5*config["initial_vertical_speed_mps"])+4*profile["actuators"]["throttle_tau_s"])
    reserve = mass*9.81/(profile["booster"]["engine_isp_s"]*_G0)*horizon
    limits = {"horizontal_position_m": config["arm_half_width_m"]/4,
              "pin_clearance_min_m": config["initial_pin_clearance_m"],
              "pin_clearance_max_m": config["initial_pin_clearance_m"]+config["arm_half_width_m"],
              "pin_vertical_speed_min_mps": 3*config["initial_vertical_speed_mps"],
              "pin_vertical_speed_max_mps": .5*config["initial_vertical_speed_mps"],
              "pin_horizontal_speed_mps": 5*config["settle_pin_speed_mps"],
              "body_rate_rad_s": config["settle_body_rate_rad_s"],
              "attitude_angle_deg": tilt_limit, "propellant_reserve_kg": reserve, "reserve_horizon_s": horizon}
    tilt, clocking, rate = angle(_dot(body_up, axes[2])), angle(_dot(body_x, axes[0])), _norm(state["omega_body_rad_s"])
    eligible = (math.hypot(*midpoint[:2]) <= limits["horizontal_position_m"]
                and all(limits["pin_clearance_min_m"] <= p["height_above_support_m"] <= limits["pin_clearance_max_m"]
                        and limits["pin_vertical_speed_min_mps"] <= p["velocity_enu_mps"][2] <= limits["pin_vertical_speed_max_mps"]
                        and math.hypot(*p["velocity_enu_mps"][:2]) <= limits["pin_horizontal_speed_mps"] for p in pins)
                and rate <= limits["body_rate_rad_s"] and tilt <= tilt_limit and clocking <= tilt_limit
                and state["propellant_kg"] >= limits["propellant_reserve_kg"])
    return {"eligible": eligible, "pins": pins, "midpoint_enu_m": midpoint, "tilt_deg": tilt,
            "clocking_error_deg": clocking, "body_rate_rad_s": rate, "propellant_kg": state["propellant_kg"],
            "mass_kg": mass, "com_body_m": centroid, "com_rate_body_mps": centroid_rate, "limits": limits}


def _profile_and_config(profile, config):
    _json(profile)
    _json(config)
    from .starship_wind_verifier import validate_wind_profile
    try:
        validate_wind_profile(profile)
    except ValueError:
        _require(False, "configuration", "Invalid declared synthetic wind")
    _require(profile.get("schema") == "missionos.starship_sixdof_profile.v1" and
             config.get("schema") == "missionos.starship_catch_configuration.v1", "configuration", "Unknown model contract")
    for group, keys in (("booster", ("dry_mass_kg", "propellant_kg", "engine_thrust_n", "engine_isp_s")),
                        ("actuators", ("throttle_tau_s", "throttle_rate_s", "gimbal_rate_deg_s", "max_gimbal_deg",
                                       "rcs_thrust_n", "rcs_isp_s", "flap_rate_deg_s", "grid_fin_limit_deg")),
                        ("guidance", ("max_q_pa",))):
        _require(all(_number(profile[group].get(k)) and profile[group][k] > 0 for k in keys),
                 "configuration", "Nonpositive model parameter")
    _require(profile["booster"]["engine_count"] == 33 and profile["booster"]["gimbal_engine_count"] == 13,
             "configuration", "Recovery v1 requires the declared 33-engine/13-gimbal vehicle")
    _require(all(_number(profile["launch"].get(k)) for k in ("latitude_deg", "longitude_deg")) and
             abs(profile["launch"]["latitude_deg"]) <= 90 and abs(profile["launch"]["longitude_deg"]) <= 180,
             "configuration", "Invalid tower location")
    _require(all(_number(config.get(k)) and config[k] > 0 for k in
                 ("support_height_m", "arm_half_width_m", "initial_pin_clearance_m", "settle_pin_speed_mps", "settle_body_rate_rad_s"))
             and _number(config.get("initial_vertical_speed_mps")) and config["initial_vertical_speed_mps"] < 0,
             "configuration", "Invalid terminal bounds")
    points = config.get("support_points_body_m")
    _require(type(points) is list and len(points) == 2 and all(_vector(p) for p in points),
             "configuration", "Two configured surrogate support points are required")


def _compare_vector(actual, expected, code, tolerance=1e-6):
    _require(_vector(actual, len(expected)) and all(_near(a, b, tolerance) for a, b in zip(actual, expected)),
             code, "Recorded vector differs from independently reconstructed state")


def _command(command, phase, state, profile):
    if command is None:
        return
    _require(type(command) is dict, "command", "Missing finite actuator command")
    engines, panels = command.get("engines"), command.get("flap_angles_rad")
    _require(type(engines) is list and len(engines) == len(state["engine_states"])
             and type(panels) is list and len(panels) == len(state["flap_angles_rad"]),
             "command", "Command actuator count mismatch")
    limit = math.radians(profile["actuators"]["max_gimbal_deg"])
    for index, engine in enumerate(engines):
        _require(type(engine) is dict and type(engine.get("enabled")) is bool and
                 _number(engine.get("throttle")) and 0 <= engine["throttle"] <= 1,
                 "command", "Invalid engine command")
        _require(engine["throttle"] == 0 if not engine["enabled"] else engine["throttle"] >= (.4 if index < 33 else 0),
                 "command", "Engine enable/minimum throttle mismatch")
        gimbal = limit if index < 13 else 0.
        _require(all(_number(engine.get(k)) and abs(engine[k]) <= gimbal+1e-8 for k in ("gimbal_x_rad", "gimbal_y_rad")),
                 "command", "Commanded gimbal exceeds finite authority")
    fin_limit = math.radians(profile["actuators"]["grid_fin_limit_deg"])
    _require(all(_number(x) and abs(x) <= (0. if i < 3 else fin_limit)+1e-8 for i, x in enumerate(panels)),
             "command", "Commanded fin exceeds finite authority")
    count = sum(e["enabled"] for e in engines[:33])
    allowed = {"recovery_boostback_slew": {3}, "recovery_boostback_burn": set(range(1, 34)),
               "recovery_powered_rate_settle": {3}, "recovery_entry_coast": {0},
               "recovery_landing_13": set(range(1, 14)), "recovery_landing_5": set(range(1, 6)),
               "recovery_landing_3": {1, 2, 3}}
    # A failed engine retains its requested command; availability determines
    # actual force, which is reconstructed separately from that request.
    _require(count in allowed[phase], "command_phase", "Requested engines contradict the recorded recovery phase")


def _closest_trim_secondary(matrix, image, lower, upper, prior, limit):
    """Independent weighted projection over every face of a tiny box.

    Orthogonalized constraint rows compute the closest trim on each affine
    face without importing the producer or squaring matrix conditioning.
    """
    count, best, best_cost = len(prior), None, math.inf
    for face in product((-1, 0, 1), repeat=count):
        free = [i for i, flag in enumerate(face) if flag == 0]
        trial = [lower[i] if flag == -1 else upper[i] if flag == 1 else prior[i]
                 for i, flag in enumerate(face)]
        rows, values, consistent = [], [], True
        row_scale = max((_norm([row[i]*limit for i in free]) for row in matrix), default=0.)
        for row, target in zip(matrix, image):
            residual = [row[i]*limit for i in free]
            value = target-_dot(row, trial)
            for _ in range(2):
                for basis, normalized_value in zip(rows, values):
                    coefficient = _dot(residual, basis)
                    residual = [a-coefficient*b for a, b in zip(residual, basis)]
                    value -= coefficient*normalized_value
            magnitude = _norm(residual)
            if magnitude > max(1e-14, row_scale*1e-12):
                rows.append([x/magnitude for x in residual])
                values.append(value/magnitude)
            elif abs(value) > 1e-8:
                consistent = False
                break
        if not consistent:
            continue
        for index, channel in enumerate(free):
            trial[channel] = prior[channel]+limit*sum(row[index]*value for row, value in zip(rows, values))
        if (any(x < lo-1e-9 or x > hi+1e-9 for x, lo, hi in zip(trial, lower, upper))
                or any(abs(_dot(row, trial)-target) > 1e-8 for row, target in zip(matrix, image))):
            continue
        cost = sum(((x-target)/limit)**2 for x, target in zip(trial, prior))
        if cost < best_cost:
            best, best_cost = trial, cost
    _require(best is not None, "fin_allocation", "No bounded trim projection shares the declared primary moment image")
    return best, best_cost


def _fin_allocation(checkpoint, sample, profile, enabled, remaining_s, *, expected_policy="finite_regularized_fins_v1"):
    """Check scalar finite response and convex KKT arithmetic, not panel laws."""
    navigation = checkpoint.get("navigation", {})
    _require(expected_policy in ("finite_regularized_fins_v1", "finite_moment_priority_fins_v1"),
             "fin_allocation", "Unknown explicitly scoped finite fin policy")
    priority = expected_policy == "finite_moment_priority_fins_v1"
    item = navigation.get("development_fin_allocation")
    other = sample.get("controller", {}).get("development_fin_allocation")
    _require(_same_value(item, other), "fin_allocation", "Sample/controller receipt mismatch")
    if item is None:
        # Intermediate commands in the candidate's high-q coast/landing region
        # must retain the allocator receipt. A terminal state has no command.
        required = (enabled and checkpoint.get("command") is not None
                    and (checkpoint["phase"] == "recovery_entry_coast" and sample.get("dynamic_pressure_pa", 0) > 100
                         or checkpoint["phase"].startswith("recovery_landing_") and sample.get("dynamic_pressure_pa", 0) > 50))
        _require(not required, "fin_allocation", "Candidate lost its finite allocation receipt")
        return
    _require(enabled and type(item) is dict and item.get("schema") ==
             ("missionos.starship_fin_allocation.v2" if priority else "missionos.starship_fin_allocation.v1")
             and item.get("policy_id") == expected_policy and item.get("regularization") == .05
             and all(item.get(k) is False for k in ("prediction_is_execution", "actual_state_assigned", "production_policy_admitted")),
             "fin_allocation", "Experimental allocation escaped its explicit scope")
    state, command = checkpoint["state"], checkpoint["command"]
    indices = list(range(3, len(state["flap_angles_rad"])))
    count = len(indices)
    _require(1 <= count <= 4 and item.get("fin_indices") == indices and command is not None,
             "fin_allocation", "Invalid finite fin channels")
    keys = ("actual_angles_rad", "command_angles_rad", "predicted_endpoint_angles_rad", "trim_angles_rad",
            "reachable_delta_lower_rad", "reachable_delta_upper_rad", "half_objective_gradient")
    _require(all(_vector(item.get(k), count) for k in keys), "fin_allocation", "Invalid allocation vectors")
    _compare_vector(item["actual_angles_rad"], [state["flap_angles_rad"][i] for i in indices], "fin_allocation")
    _compare_vector(item["command_angles_rad"], [command["flap_angles_rad"][i] for i in indices], "fin_allocation")
    interval, actuators = item.get("interval_s"), profile["actuators"]
    tau = actuators.get("flap_tau_s")
    _require(_number(interval) and 0 < interval <= .25 and _number(tau) and tau > 0,
             "fin_allocation", "Invalid finite response horizon")
    altitude = _prediction_origin(state, profile)[2]
    powered = any(e["enabled"] for e in command["engines"][:33])
    expected_interval = min(.1 if altitude < 100000. or powered else .25, remaining_s)
    _require(_near(interval, expected_interval, 1e-9), "fin_allocation", "Response horizon differs from the integration clock")
    limit, rate = math.radians(actuators["grid_fin_limit_deg"]), math.radians(actuators["flap_rate_deg_s"])
    def endpoint(actual, target):
        distance = abs(target-actual)
        switch = max(0., (distance-rate*tau)/rate)
        movement = rate*interval if interval <= switch else distance-min(distance, rate*tau)*math.exp(-(interval-switch)/tau)
        return actual+math.copysign(movement, target-actual)
    actual, target, predicted, trim = (item[k] for k in keys[:4])
    delta = _sub(predicted, actual)
    for i in range(count):
        lower, upper = endpoint(actual[i], -limit)-actual[i], endpoint(actual[i], limit)-actual[i]
        _require(abs(target[i]) <= limit+1e-9 and abs(predicted[i]) <= limit+1e-9 and abs(trim[i]) <= limit+1e-9
                 and abs(delta[i]) <= rate*interval+1e-9 and _near(predicted[i], endpoint(actual[i], target[i]), 1e-9)
                 and _near(item["reachable_delta_lower_rad"][i], lower, 1e-9)
                 and _near(item["reachable_delta_upper_rad"][i], upper, 1e-9),
                 "fin_allocation", "Predicted motion violates unchanged actuator lag/rate/position")
    reference = item.get("trim_reference_source")
    if reference == "entry_target_static_trim_not_actual_deflection":
        all_trim = navigation.get("predicted_trim_flap_angles_rad")
        _require(_vector(all_trim, len(state["flap_angles_rad"])), "fin_allocation", "Missing entry trim reference")
        _compare_vector(trim, [all_trim[i] for i in indices], "fin_allocation")
    else:
        _require(reference == "current_angle_no_static_trim_available", "fin_allocation", "Unknown trim reference")
        _compare_vector(trim, actual, "fin_allocation")
    matrix, scale = item.get("effectiveness_nm_per_rad"), item.get("moment_scale_nm")
    _require(type(matrix) is list and len(matrix) == 3 and all(_vector(row, count) for row in matrix)
             and _vector(scale) and all(x > 0 for x in scale), "fin_allocation", "Invalid effectiveness normalization")
    floor = 2*actuators["rcs_radius_m"]*actuators["rcs_thrust_n"]
    _compare_vector(scale, [max(floor, sum(abs(x)*limit for x in row)) for row in matrix], "fin_allocation", tolerance=1e-4)
    demand, nonlinear = item.get("requested_increment_torque_body_nm"), item.get("nonlinear_predicted_increment_torque_body_nm")
    _require(_vector(demand) and _vector(nonlinear), "fin_allocation", "Missing bounded moment request")
    linear = [_dot(row, delta) for row in matrix]
    _compare_vector(item.get("linear_predicted_increment_torque_body_nm"), linear, "fin_allocation", tolerance=1e-4)
    residual = _sub(demand, nonlinear)
    _compare_vector(item.get("predicted_residual_torque_body_nm"), residual, "fin_allocation", tolerance=1e-4)
    _compare_vector(navigation.get("requested_torque_body_nm"), residual, "fin_allocation", tolerance=1e-4)
    errors = [(linear[i]-demand[i])/scale[i] for i in range(3)]
    if priority:
        _require(item.get("allocation_priority") == "moment_error_then_trim_with_fixed_primary_image"
                 and item.get("secondary_projection") == "weighted_equality_projection_over_box_faces"
                 and type(item.get("maximum_secondary_faces")) is int and item["maximum_secondary_faces"] == 3**count,
                 "fin_allocation", "Moment priority must retain its explicitly bounded nullspace trim scope")
        scaled = [[value/scale[index] for value in row] for index, row in enumerate(matrix)]
        primary = item.get("primary_optimum_delta_rad")
        _require(_vector(primary, count) and all(lo-1e-9 <= value <= hi+1e-9
                 for value, lo, hi in zip(primary, item["reachable_delta_lower_rad"], item["reachable_delta_upper_rad"])),
                 "fin_allocation", "Primary optimum lies outside the finite actuator endpoint box")
        primary_image = [_dot(row, primary) for row in scaled]
        final_image = [linear[index]/scale[index] for index in range(3)]
        image_error = _sub(final_image, primary_image)
        _compare_vector(item.get("secondary_image_residual"), image_error, "fin_allocation", tolerance=1e-9)
        _require(_norm(image_error) <= 1e-8*(1.+_norm(primary_image)),
                 "fin_allocation", "Trim projection degraded the primary moment image")
        primary_errors = [primary_image[index]-demand[index]/scale[index] for index in range(3)]
        cost, primary_cost = _dot(errors, errors), _dot(primary_errors, primary_errors)
        _require(_near(item.get("objective"), cost, 1e-7*max(1., cost))
                 and _near(item.get("primary_objective"), cost, 1e-7*max(1., cost))
                 and _near(item.get("primary_optimum_objective"), primary_cost, 1e-7*max(1., primary_cost))
                 and _near(cost, primary_cost, 1e-7*max(1., primary_cost)),
                 "fin_allocation", "Primary moment objective differs from independent residual arithmetic")
        gradient = [sum(scaled[j][i]*errors[j] for j in range(3)) for i in range(count)]
        _compare_vector(item.get("primary_half_objective_gradient"), gradient, "fin_allocation", tolerance=1e-7)
        _compare_vector(item["half_objective_gradient"], gradient, "fin_allocation", tolerance=1e-7)
        for solution, error in ((primary, primary_errors), (delta, errors)):
            for index in range(count):
                value = sum(scaled[j][index]*error[j] for j in range(3))
                at_lower = abs(solution[index]-item["reachable_delta_lower_rad"][index]) <= 1e-9
                at_upper = abs(solution[index]-item["reachable_delta_upper_rad"][index]) <= 1e-9
                _require((at_lower or value <= 1e-7) and (at_upper or value >= -1e-7),
                         "fin_allocation", "Primary moment box solution violates independent KKT conditions")
        trim_delta = _sub(trim, actual)
        trim_cost = sum(((value-reference)/limit)**2 for value, reference in zip(delta, trim_delta))
        _, minimum_trim_cost = _closest_trim_secondary(scaled, primary_image,
            item["reachable_delta_lower_rad"], item["reachable_delta_upper_rad"], trim_delta, limit)
        _require(_near(item.get("secondary_trim_objective"), trim_cost, 1e-7*max(1., trim_cost))
                 and _near(trim_cost, minimum_trim_cost, 1e-6*max(1., minimum_trim_cost)),
                 "fin_allocation", "Secondary endpoint is not the closest bounded trim preserving the primary moment image")
        return
    prior = [.05*(predicted[i]-trim[i])/limit for i in range(count)]
    cost = _dot(errors, errors)+_dot(prior, prior)
    _require(_near(item.get("objective"), cost, 1e-7*max(1., cost)), "fin_allocation", "Recorded optimization cost mismatch")
    gradient = [sum(matrix[j][i]/scale[j]*errors[j] for j in range(3))+.05*prior[i]/limit for i in range(count)]
    _compare_vector(item["half_objective_gradient"], gradient, "fin_allocation", tolerance=1e-7)
    for i, value in enumerate(gradient):
        at_lower = abs(delta[i]-item["reachable_delta_lower_rad"][i]) <= 1e-9
        at_upper = abs(delta[i]-item["reachable_delta_upper_rad"][i]) <= 1e-9
        _require((at_lower or value <= 1e-7) and (at_upper or value >= -1e-7),
                 "fin_allocation", "Convex box solution violates optimality conditions")


def _continuity(previous, current, profile):
    dt = current["time_s"]-previous["time_s"]
    _require(0 < dt <= .75000001, "time_order", "Checkpoint gap exceeds the sampling/integration contract")
    displacement = _sub(current["r_eci_m"], previous["r_eci_m"])
    average = _scale(_add(current["v_eci_mps"], previous["v_eci_mps"]), .5*dt)
    delta_v = _norm(_sub(current["v_eci_mps"], previous["v_eci_mps"]))
    _require(_norm(_sub(displacement, average)) <= .05+dt*delta_v,
             "translation_continuity", "Position changed without compatible recorded velocity")
    qdot = abs(_dot(previous["q_body_to_eci"], current["q_body_to_eci"]))
    angle = 2*math.acos(min(1., qdot))
    _require(angle <= dt*max(_norm(previous["omega_body_rad_s"]), _norm(current["omega_body_rad_s"]))+dt*dt+.0001,
             "attitude_continuity", "Attitude jump exceeds recorded body rates and bounded inter-sample variation")
    actuators = profile["actuators"]
    for index, (before, after) in enumerate(zip(previous["engine_states"], current["engine_states"])):
        _require(before["available"] is after["available"], "actuator_continuity", "Unrecorded engine availability reset")
        throttle_rate = actuators["throttle_rate_s"] if index < 33 else 2.
        _require(abs(before["throttle"]-after["throttle"]) <= throttle_rate*dt+1e-7 and
                 all(abs(before[k]-after[k]) <= math.radians(actuators["gimbal_rate_deg_s"])*dt+1e-7
                     for k in ("gimbal_x_rad", "gimbal_y_rad")), "actuator_continuity", "Actual engine changed beyond actuator rate")
    _require(all(abs(a-b) <= math.radians(actuators["flap_rate_deg_s"])*dt+1e-7
                 for a, b in zip(previous["flap_angles_rad"], current["flap_angles_rad"])),
             "actuator_continuity", "Actual fin changed beyond actuator rate")
    booster = profile["booster"]
    maximum_flow = 33*booster["engine_thrust_n"]/(booster["engine_isp_s"]*_G0)+12*actuators["rcs_thrust_n"]/(actuators["rcs_isp_s"]*_G0)
    consumed = previous["propellant_kg"]-current["propellant_kg"]
    _require(-1e-7 <= consumed <= maximum_flow*dt+1e-5, "fuel_continuity", "Refueling or impossible instantaneous propellant loss")


def _observe_handoff(recorded, reconstructed, state, config):
    _require(type(recorded) is dict and _near(recorded.get("time_s"), state["time_s"]), "handoff_observation", "Arrival clock mismatch")
    expected_position = reconstructed["midpoint_enu_m"][:2]+[reconstructed["midpoint_enu_m"][2]-config["support_height_m"]]
    for key, expected in (("position_error_enu_m", expected_position),
                          ("pin_height_above_support_m", [p["height_above_support_m"] for p in reconstructed["pins"]]),
                          ("com_body_m", reconstructed["com_body_m"]), ("com_rate_body_mps", reconstructed["com_rate_body_mps"])):
        _compare_vector(recorded.get(key), expected, "handoff_observation")
    for key, expected in (("tilt_deg", reconstructed["tilt_deg"]), ("body_x_east_angle_deg", reconstructed["clocking_error_deg"]),
                          ("body_rate_rad_s", reconstructed["body_rate_rad_s"]), ("propellant_kg", state["propellant_kg"]),
                          ("mass_kg", reconstructed["mass_kg"])):
        _require(_near(recorded.get(key), expected, 1e-5), "handoff_observation", f"Arrival {key} is not supported by actual state")
    _require(recorded.get("eligible") is reconstructed["eligible"], "handoff_observation", "Recorded arrival gate differs from actual material-point state")
    limits = recorded.get("limits")
    _require(type(limits) is dict and limits.keys() == reconstructed["limits"].keys() and
             all(_near(limits.get(k), v) for k, v in reconstructed["limits"].items()),
             "handoff_limits", "Arrival limits were widened or differ from independently derived bounds")
    pins = recorded.get("pins")
    _require(type(pins) is list and len(pins) == 2, "handoff_observation", "Missing material-point observations")
    for index, (pin, expected) in enumerate(zip(pins, reconstructed["pins"])):
        _require(pin.get("id") == index, "handoff_observation", "Support identity mismatch")
        _compare_vector(pin.get("position_body_m"), config["support_points_body_m"][index], "handoff_observation")
        _compare_vector(pin.get("position_enu_m"), expected["position_enu_m"], "handoff_observation")
        _compare_vector(pin.get("relative_velocity_enu_mps"), expected["velocity_enu_mps"], "handoff_observation")
        _require(pin.get("footprint_active") is False and pin.get("top_contact_eligible") is False
                 and pin.get("normal_force_n") == 0, "handoff_observation", "Pre-handoff prediction cannot supply support force")
        _compare_vector(pin.get("force_enu_n"), [0., 0., 0.], "handoff_observation")


def _claims(run, record, outcome):
    forbidden = ("physical_execution", "physical_execution_invoked", "catch_verified", "mission_completed",
                 "starship_vehicle_validated", "landing_hardware_validated", "launch_connected", "launch_connected_catch_supported",
                 "state_reset", "attitude_prescribed", "simulated_catch_supported")
    for section in (run, record, outcome):
        _require(all(section.get(k, False) is False for k in forbidden), "claim_boundary", "Return evidence carries an unsupported execution or success claim")
    _require(record.get("physical_execution") is False and record.get("catch_verified") is False and
             record.get("forecast_only") is False and record.get("landing_start_forecast") is None and
             outcome.get("six_dof_integrated") is True and outcome.get("booster_return_6dof_implemented") is True,
             "claim_boundary", "Missing explicit simulated execution boundary")
    _require(outcome.get("orbit_gate_reached", False) is False and type(outcome.get("payload_released_count", 0)) is int
             and outcome.get("payload_released_count", 0) == 0,
             "claim_boundary", "A booster return cannot establish payload release or orbit insertion")


def _events(events, record, outcome, start, end):
    _require(type(events) is list and 2 <= len(events) <= 10000, "events", "Missing bounded event sequence")
    allowed = _TERMINATIONS | {"booster_return_start", "predictive_boostback_plan", "predictive_boostback_plan_refreshed", "boostback_ignition",
                              "boostback_complete_rate_settle", "powered_rate_settled_cutoff", "landing_stage_requested"}
    previous, stages, names, recorded_plans = start, [], [], []
    for event in events:
        _require(type(event) is dict and _number(event.get("time_s")) and previous <= event["time_s"] <= end+1e-8
                 and event.get("event") in allowed, "events", "Unknown, unordered or out-of-range event")
        previous = event["time_s"]
        name = event["event"]
        names.append(name)
        if name == "boostback_ignition":
            _require(event.get("requested_engine_count") == 33, "events", "V3-inspired boostback request must name 33 engines")
        if name == "landing_stage_requested":
            stages.append(event.get("requested_engine_count"))
        if name in ("predictive_boostback_plan", "predictive_boostback_plan_refreshed"):
            recorded_plans.append(event.get("plan"))
    _require(stages in ([], [13], [13, 5], [13, 5, 3]), "events", "Landing requests must follow 13, 5, 3 without duplicates")
    _require(names[0] == "booster_return_start" and events[0]["time_s"] == start and
             names[-1] == outcome["termination"] and events[-1]["time_s"] == end and
             not any(n in _TERMINATIONS for n in names[:-1]), "events", "Start or termination is inconsistent with trajectory")
    _require(outcome.get("boostback_started") is ("boostback_ignition" in names) and
             outcome.get("coast_started") is ("powered_rate_settled_cutoff" in names),
             "events", "Phase summary is not backed by events")
    plan = record.get("boostback_plan")
    _require(type(plan) is dict and plan.get("prediction_is_execution") is False and
             plan.get("method") == "finite_command_candidates_and_point_model_intercept" and
             type(plan.get("candidates")) is list and len(plan["candidates"]) == 5 and
             plan.get("selected") in plan["candidates"] and names.count("predictive_boostback_plan") == 1 and
             names.count("predictive_boostback_plan_refreshed") <= 1 and recorded_plans[-1] == plan and
             all(type(p) is dict and p.get("prediction_is_execution") is False for p in recorded_plans),
             "prediction_binding", "Planning alternatives cannot be treated as executed truth")
    return stages


def _prediction_records(record, start, end, profile):
    receipts = record.get("cutoff_predictions")
    _require(type(receipts) is list and len(receipts) == record["full_coast_prediction_count"],
             "prediction_binding", "Bounded full-forecast receipts differ from the recorded count")
    previous = start-1.
    for receipt in receipts:
        _require(type(receipt) is dict and _number(receipt.get("time_s")) and
                 start <= receipt["time_s"] <= end and receipt["time_s"] > previous,
                 "prediction_binding", "Forecast origin is not an ordered executed clock")
        previous = receipt["time_s"]
        prediction = receipt.get("prediction")
        _require(type(prediction) is dict and prediction.get("prediction_is_execution") is False and
                 prediction.get("model") == "same_finite_6dof_policy_continuation" and
                 prediction.get("termination") in _TERMINATIONS and
                 _vector(prediction.get("position_enu_m")) and _vector(prediction.get("velocity_enu_mps")) and
                 _number(prediction.get("elapsed_s")) and 0 <= prediction["elapsed_s"] <= 1200.+1e-7 and
                 _number(prediction.get("terminal_propellant_kg")) and
                 0 <= prediction["terminal_propellant_kg"] <= profile["booster"]["propellant_kg"],
                 "prediction_binding", "A forecast must remain bounded counterfactual evidence")
        remaining = start+record["resolved_duration_s"]-receipt["time_s"]
        _require(_near(prediction.get("remaining_duration_s"), remaining) and
                 prediction["elapsed_s"] <= remaining+1e-7,
                 "prediction_binding", "Counterfactual continuation reset the remaining execution horizon")
    for checkpoint in record["checkpoints"]:
        diagnostic = checkpoint.get("navigation")
        _require(type(diagnostic) is dict, "record", "Missing checkpoint navigation record")
        prediction = diagnostic.get("cutoff_prediction")
        if prediction is not None:
            _require(type(prediction) is dict and prediction.get("prediction_is_execution") is False,
                     "prediction_binding", "Navigation forecast was promoted to executed truth")
            if prediction.get("model") == "same_finite_6dof_policy_continuation":
                _require(any(r["time_s"] == checkpoint["time_s"] and r["prediction"] == prediction for r in receipts),
                         "prediction_binding", "Navigation full forecast is not bound to its separate receipt")
            else:
                _require(prediction.get("model") == "point_model_coarse_without_finite_shutdown",
                         "prediction_binding", "Unknown predictive model identity")


def _prediction_reserve(fuel, profile, config):
    horizon = ((config["initial_pin_clearance_m"]+config["arm_half_width_m"])
               /(-.5*config["initial_vertical_speed_mps"])+4*profile["actuators"]["throttle_tau_s"])
    return (profile["booster"]["dry_mass_kg"]+max(0., fuel))*9.81/(profile["booster"]["engine_isp_s"]*_G0)*horizon


def _prediction_origin(state, profile):
    """Ellipsoid-normal ENU at the executed forecast origin, independently."""
    x, y, z = state["r_eci_m"]
    radial = math.hypot(x, y)
    lat = math.atan2(z, radial*(1-_E2))
    for _ in range(12):
        radius = _A/math.sqrt(1-_E2*math.sin(lat)**2)
        lat = math.atan2(z+_E2*radius*math.sin(lat), radial)
    lon = math.atan2(y, x)
    east = [-math.sin(lon), math.cos(lon), 0.]
    north = [-math.sin(lat)*math.cos(lon), -math.sin(lat)*math.sin(lon), math.cos(lat)]
    altitude = radial*math.cos(lat)+z*math.sin(lat)-_A*math.sqrt(1-_E2*math.sin(lat)**2)
    displacement = _sub(state["r_eci_m"], _tower(profile, state["time_s"])[0])
    return [_dot(displacement, east), _dot(displacement, north), altitude]


def _capture_planning(record, events, profile, config, development_cutoff_time_s=None, development_boostback_candidate_index=None):
    """Verify admission arithmetic and binding, never the forecast dynamics.

    Historical records without this contract remain readable and unassessed.
    Admission does not certify capture: measured arrival and contact still gate
    execution. Intercept fallback must explicitly retain its negative status.
    """
    summary = record.get("capture_planning")
    if summary is None:
        _require(not any("admission" in r for r in record["cutoff_predictions"]) and
                 all("selection_rule" not in e.get("plan", {}) for e in events),
                 "capture_planning", "A new admission record lost its planning summary")
        return None
    _require(type(summary) is dict and summary.get("schema") == "missionos.starship_capture_planning.v1"
             and summary.get("prediction_is_execution") is False and summary.get("dispatch_authority_created") is False,
             "capture_planning", "Missing explicit planning and authority boundary")
    plans = [event["plan"] for event in events if event["event"] in
             ("predictive_boostback_plan", "predictive_boostback_plan_refreshed")]
    for plan in plans:
        _require(plan.get("selection_rule") == ("explicit_development_candidate" if development_boostback_candidate_index is not None
                                               else "contact_budget_first_else_best_effort_surplus") and
                 plan.get("development_candidate_index") == development_boostback_candidate_index,
                 "capture_planning", "Unknown coarse-budget selection rule")
        feasible = []
        for candidate in plan["candidates"]:
            fuel = candidate.get("predicted_point_model_after_arrest_fuel_kg")
            _require(_number(fuel), "capture_planning", "Missing finite point-model propellant estimate")
            reserve = _prediction_reserve(fuel, profile, config)
            _require(_near(candidate.get("point_model_contact_propellant_reserve_kg"), reserve) and
                     candidate.get("point_model_contact_budget_met") is (fuel >= reserve),
                     "capture_planning", "Coarse prediction spent the reserved contact budget")
            if fuel >= reserve:
                feasible.append(candidate)
        selected = max(feasible or plan["candidates"], key=lambda c:
                       c["predicted_point_model_after_arrest_fuel_kg"]-c["point_model_contact_propellant_reserve_kg"])
        if development_boostback_candidate_index is not None:
            selected = plan["candidates"][development_boostback_candidate_index]
        _require(type(plan.get("contact_budget_candidate_count")) is int and
                 plan["contact_budget_candidate_count"] == len(feasible) and plan["selected"] == selected and
                 plan.get("selected_contact_budget_met") is (selected in feasible),
                 "capture_planning", "Coarse selection is not backed by the recorded budget candidates")
    admitted, cutoffs = 0, {}
    if development_cutoff_time_s is not None:
        endings = [e for e in events if e["event"] == "boostback_complete_rate_settle"]
        phase = record["checkpoints"][-1]["phase"]
        _require(len(endings) <= 1 and (len(endings) == 1 or phase.startswith("recovery_boostback"))
                 and all(e.get("cutoff_basis") in ("development_scheduled_cutoff", "fuel_or_time_guard") for e in endings),
                 "development_scope", "Scheduled return requires one declared cutoff after the boostback phase")
    for receipt in record["cutoff_predictions"]:
        prediction, decision = receipt["prediction"], receipt.get("admission")
        origin = receipt.get("origin_state")
        _state(origin, profile)
        _require(origin["time_s"] == receipt["time_s"], "capture_planning", "Forecast origin clock mismatch")
        checkpoints = record["checkpoints"]
        left = max((c["state"] for c in checkpoints if c["time_s"] <= origin["time_s"]), key=lambda s: s["time_s"])
        right = min((c["state"] for c in checkpoints if c["time_s"] >= origin["time_s"]), key=lambda s: s["time_s"])
        if left["time_s"] == origin["time_s"]:
            _same_state(left, origin, "capture_planning")
        else:
            _continuity(left, origin, profile)
        if right["time_s"] != origin["time_s"]:
            _continuity(origin, right, profile)
        position = _prediction_origin(origin, profile)
        _compare_vector(receipt.get("origin_position_enu_m"), position, "capture_planning", tolerance=1e-4)
        fuel = prediction["terminal_propellant_kg"]
        reserve = _prediction_reserve(fuel, profile, config)
        met = fuel >= reserve
        _require(_near(prediction.get("terminal_propellant_reserve_kg"), reserve) and
                 prediction.get("terminal_fuel_reserve_met") is met and
                 _near(prediction.get("terminal_ground_speed_mps"), _norm(prediction["velocity_enu_mps"])) and
                 type(prediction.get("terminal_handoff_eligible")) is bool and
                 prediction["terminal_handoff_eligible"] is (prediction["termination"] == "catch_handoff"),
                 "capture_planning", "Forecast fuel, speed or arrival status is inconsistent")
        reasons = []
        if not prediction["terminal_handoff_eligible"]:
            reasons.append("predicted_arrival_gate_not_reached")
        if not met:
            reasons.append("predicted_contact_propellant_reserve_not_met")
        along = sum(prediction["position_enu_m"][i]*position[i] for i in (0, 1))/max(1., math.hypot(*position[:2]))
        geographic = (prediction["termination"] != "rate_settle_failed" and
                      (along <= 0 or math.hypot(*prediction["position_enu_m"][:2]) <= 100.))
        admission = not reasons
        expected = {"schema": "missionos.starship_capture_plan_admission.v1", "capture_plan_admissible": admission,
                    "reasons": reasons, "geographic_intercept_predicted": geographic,
                    "best_effort_cutoff_requested": geographic and not admission, "cutoff_requested": admission or geographic,
                    "prediction_is_execution": False, "dispatch_authority_created": False}
        _require(_same_value(decision, expected), "capture_planning", "Geographic intercept was promoted to an admitted capture plan")
        admitted += admission
        cutoffs[receipt["time_s"]] = expected
    fallback = False
    for event in events:
        if event["event"] != "boostback_complete_rate_settle":
            continue
        basis = event.get("cutoff_basis")
        if basis == "development_scheduled_cutoff":
            ignition = [e for e in events if e["event"] == "boostback_ignition"]
            _require(development_cutoff_time_s is not None and len(ignition) == 1
                     and ignition[0]["time_s"] < event["time_s"]
                     and -1e-9 <= event["time_s"]-development_cutoff_time_s <= .10000001,
                     "development_scope", "Scheduled cutoff differs from the explicit experiment input")
            continue
        if basis == "fuel_or_time_guard":
            if development_cutoff_time_s is not None:
                state = event.get("state")
                _state(state, profile)
                _require(_near(event.get("remaining_propellant_kg"), state["propellant_kg"]),
                         "development_scope", "Guard fuel must match the actual cutoff state")
                configuration = profile.get("booster_return", {})
                reserve, maximum = configuration.get("landing_reserve_kg", 60000.), configuration.get("boostback_max_burn_s", 70.)
                _require(_number(reserve) and reserve > 0 and _number(maximum) and maximum > 0,
                         "development_scope", "Invalid independently configured cutoff guards")
                ignition = [e for e in events if e["event"] == "boostback_ignition"]
                fuel_guard = state["propellant_kg"] <= reserve
                time_guard = len(ignition) == 1 and event["time_s"]-ignition[0]["time_s"] >= maximum
                _require(fuel_guard or time_guard, "development_scope", "Neither measured fuel nor burn time justifies guard cutoff")
            continue
        decision = cutoffs.get(event["time_s"])
        _require(decision is not None and decision["cutoff_requested"], "capture_planning", "Cutoff has no bound forecast decision")
        _require(basis == ("admitted_capture_prediction" if decision["capture_plan_admissible"]
                           else "unadmitted_geographic_best_effort"), "capture_planning", "Cutoff lost its admission boundary")
        fallback = fallback or decision["best_effort_cutoff_requested"]
    _require(type(summary.get("admissible_prediction_count")) is int and summary["admissible_prediction_count"] == admitted
             and summary.get("best_effort_cutoff_used") is fallback,
             "capture_planning", "Capture-planning summary differs from the decisions")
    return {"assessed": True, "forecast_count": len(cutoffs), "admissible_prediction_count": admitted,
            "best_effort_cutoff_used": fallback, "forecast_dynamics_reexecuted": False,
            "dispatch_authority_created": False}


def _trim_diagnostics(diagnostic, profile):
    """Check the prediction's finite envelope, not replay its trim solver.

    The actual flap states and their rates are verified separately. These
    predicted angles must never be substituted for actual deflections.
    """
    _require(type(diagnostic) is dict, "record", "Missing saved navigation diagnostics")
    if "candidate_count" not in diagnostic:
        return
    count, angle = diagnostic["candidate_count"], diagnostic.get("entry_angle_deg")
    _require(type(count) is int and 1 <= count <= 15 and _number(angle) and 0 <= angle <= _GUIDANCE["entry_max_angle_deg"],
             "trim_prediction", "Entry command candidates exceed the configured envelope")
    if count == 1 and "steady_trim_feasible" not in diagnostic:
        _require(angle == 0., "trim_prediction", "Single rising-flight candidate must retain zero relative entry angle")
        return
    actuator = profile["actuators"]
    jet_capacity = 2*actuator["rcs_radius_m"]*actuator["rcs_thrust_n"]
    limit = math.radians(actuator["grid_fin_limit_deg"])
    angles, residual = diagnostic.get("predicted_trim_flap_angles_rad"), diagnostic.get("predicted_residual_moment_body_nm")
    _require(diagnostic.get("steady_trim_feasible") is True and diagnostic.get("trim_is_executed_deflection") is False,
             "trim_prediction", "An infeasible or imposed trim cannot be an accepted predicted command")
    _require(_vector(angles, 3+len(profile["booster"]["grid_fin_panels"])) and
             all(abs(x) <= (0. if i < 3 else limit)+1e-8 for i, x in enumerate(angles)) and
             _vector(residual) and max(abs(x) for x in residual) <= jet_capacity+1e-6 and
             _near(diagnostic.get("available_residual_jet_torque_nm"), jet_capacity),
             "trim_prediction", "Predicted fin angle or residual moment exceeds configured authority")
    loads = diagnostic.get("predicted_panel_force_magnitudes_n")
    _require(_vector(loads, len(angles)) and all(x >= 0 for x in loads) and
             _number(diagnostic.get("prediction_dynamic_pressure_pa")) and diagnostic["prediction_dynamic_pressure_pa"] >= 0 and
             _vector(diagnostic.get("predicted_force_enu_n")) and
             type(diagnostic.get("trim_rejected_candidate_count")) is int and
             0 <= diagnostic["trim_rejected_candidate_count"] < count,
             "trim_prediction", "Missing finite-load evidence or no admitted trim candidate")
    pressure = diagnostic["prediction_dynamic_pressure_pa"]
    validation_pressure = diagnostic.get("trim_validation_dynamic_pressure_pa")
    validation_residual = diagnostic.get("trim_validation_residual_moment_body_nm")
    expected_pressure = max(pressure, profile["guidance"]["max_q_pa"])
    _require(_near(validation_pressure, expected_pressure, max(1e-6, expected_pressure*1e-10)) and
             validation_pressure > 0 and _vector(validation_residual) and
             max(abs(x) for x in validation_residual) <= jet_capacity+1e-6,
             "trim_prediction", "Trim must retain finite authority at the declared future-pressure bound")
    # Compare toward lower pressure to avoid amplifying near-vacuum roundoff.
    # At zero current pressure only the independently bounded future moment is
    # observable here; this consistency check does not reconstruct the solver.
    _require(all(_near(current, future*(pressure/validation_pressure), 1e-6)
                 for current, future in zip(residual, validation_residual)),
             "trim_prediction", "Current and future trim moments do not share their declared pressure scaling")


def verify_recovery(booster_run, separation_state, profile, catch_config, *, catch_run=None,
                    development_cutoff_time_s=None, development_fin_allocation=False,
                    development_fin_scope="coast_and_landing", development_landing_probe_time_s=None,
                    development_boostback_candidate_index=None):
    """Verify isolated return evidence and an optional exact terminal handoff.

    The caller must independently verify the preceding launch and bind source,
    profile and approval. Passing here never awards launch or physical success.
    """
    result = {"schema": "missionos.starship_booster_recovery_verification.v1", "passed": False,
              "handoff_reached": False, "catch_supported_after_handoff": False, "launch_connected": False,
              "catch_verified": False, "physical_execution": False, "physical_execution_invoked": False,
              "mission_completed": False, "checkpoint_count": 0, "issues": [],
              "limitations": ["Saved-state consistency and independently reconstructed arrival, not an integrator replay.",
                              "Preceding launch, exact-source execution and approvals require the enclosing verifier.",
                              "Surrogate support geometry and shared fuel reservoir are not validated SpaceX hardware."]}
    try:
        _json(booster_run)
        _json(separation_state)
        _profile_and_config(profile, catch_config)
        _state(separation_state, profile)
        run = booster_run
        _require(type(run) is dict and run.get("scenario") == "booster_return" and run.get("body_id") == "booster",
                 "record", "Expected isolated booster return evidence")
        record, outcome = run.get("recovery_record"), run.get("outcome")
        _require(type(record) is dict and type(outcome) is dict and record.get("schema") == "missionos.starship_booster_recovery.v1"
                 and record.get("policy_id") == _POLICY and run.get("guidance_policy") == _POLICY,
                 "record", "Missing predictive recovery contract")
        _claims(run, record, outcome)
        candidate_marker = record.get("development_boostback_candidate")
        if development_boostback_candidate_index is None:
            _require(candidate_marker is None, "development_scope", "Development velocity alternatives are not production policy")
        else:
            _require(all(section.get(key, False) is False for section in (run, record, outcome)
                         for key in ("production_policy_admitted", "missionos_dispatch", "model_advantage_established")),
                     "claim_boundary", "Development alternatives cannot claim production admission or model value")
            _require(type(development_boostback_candidate_index) is int and 0 <= development_boostback_candidate_index <= 4
                     and development_cutoff_time_s is not None and development_fin_allocation is False
                     and development_landing_probe_time_s is None and candidate_marker == {
                         "schema": "missionos.starship_boostback_candidate.v1",
                         "candidate_index": development_boostback_candidate_index,
                         "selection_scope": "initial_and_alignment_refresh", "recording": "every_macrostep",
                         "production_policy_admitted": False},
                     "development_scope", "Missing explicitly bound development velocity alternative")
        _require(type(development_fin_allocation) is bool, "development_scope", "Finite fin scope must be explicit")
        _require(development_fin_scope in ("coast_and_landing", "coast_only") and
                 (development_fin_allocation or development_fin_scope == "coast_and_landing"),
                 "development_scope", "Invalid finite fin application scope")
        fin_marker = record.get("development_fin_allocation")
        if development_fin_allocation:
            _require(development_cutoff_time_s is not None and fin_marker == {
                "schema": "missionos.starship_fin_experiment.v1" if development_fin_scope == "coast_and_landing" else "missionos.starship_fin_experiment.v2",
                "policy_id": "finite_regularized_fins_v1",
                **({"application_scope": "coast_only", "minimum_dynamic_pressure_pa": 100.}
                   if development_fin_scope == "coast_only" else {}),
                "regularization": .05, "production_policy_admitted": False},
                "development_scope", "Missing explicitly bound finite fin experiment")
        else:
            _require(fin_marker is None, "development_scope", "Experimental fins are not approved production policy")
        development = record.get("development_cutoff")
        if development_cutoff_time_s is None:
            _require(development is None, "development_scope", "An experimental cutoff is not an approved production policy")
        else:
            _require(_number(development_cutoff_time_s) and
                     separation_state["time_s"] < development_cutoff_time_s < separation_state["time_s"]+1200
                     and development == {"schema": "missionos.starship_scheduled_cutoff.v1",
                         "time_s": development_cutoff_time_s, "forecast_suppressed": True,
                         "production_policy_admitted": False}
                     and record.get("full_coast_prediction_count") == 0 and record.get("cutoff_predictions") == [],
                     "development_scope", "Missing explicitly bound scheduled-cutoff experiment")
        _same_state(separation_state, run.get("booster_separation_state"), "separation_binding")
        _same_state(separation_state, record.get("input_separation_state"), "separation_binding")
        _same_state(separation_state, run.get("initial_state"), "separation_binding")
        configuration = record.get("guidance_configuration")
        _require(configuration == _GUIDANCE, "configuration", "Guidance record differs from the independently specified policy contract")
        prediction_count = record.get("full_coast_prediction_count")
        _require(type(prediction_count) is int and 0 <= prediction_count <= _GUIDANCE["maximum_full_coast_predictions"],
                 "prediction_binding", "Recorded forecast count exceeds the configured budget")
        checkpoints, samples = record.get("checkpoints"), run.get("samples")
        _require(type(checkpoints) is list and 2 <= len(checkpoints) <= 10000 and
                 type(samples) is list and len(samples) == len(checkpoints), "record", "Missing saved return states")
        previous, previous_phase = None, 0
        for index, (checkpoint, sample) in enumerate(zip(checkpoints, samples)):
            _require(type(checkpoint) is dict and type(sample) is dict and sample.get("body_id") == "booster", "record", "Invalid booster checkpoint")
            state, phase = checkpoint.get("state"), checkpoint.get("phase")
            _state(state, profile)
            _require(phase in _PHASES and _PHASES.index(phase) >= previous_phase and
                     checkpoint.get("time_s") == state["time_s"] and sample.get("phase") == phase,
                     "phase", "Clock/phase mismatch or recovery phase moved backwards")
            previous_phase = _PHASES.index(phase)
            _same_state(state, sample, "sample_binding")
            from .starship_wind_verifier import verify_wind_observation
            _require(verify_wind_observation(sample, profile)["passed"],
                     "wind_observation", "Recorded air/ground velocity differs from declared wind")
            mass, centroid, centroid_rate, _ = _flow_and_centroid(state, profile)
            _require(_near(sample.get("mass_kg"), mass) and _near(sample.get("com_z_m"), centroid[2]),
                     "mass_properties", "Recorded mass or centroid differs from actual fuel")
            _compare_vector(checkpoint.get("com_rate_body_mps"), centroid_rate, "mass_properties")
            _compare_vector(sample.get("com_rate_body_mps"), centroid_rate, "mass_properties")
            _command(checkpoint.get("command"), phase, state, profile)
            _trim_diagnostics(checkpoint.get("navigation"), profile)
            resolved = record.get("resolved_duration_s")
            _require(_number(resolved), "time_order", "Missing resolved simulation duration")
            _fin_allocation(checkpoint, sample, profile, development_fin_allocation and
                            (development_fin_scope == "coast_and_landing" or phase == "recovery_entry_coast"),
                            separation_state["time_s"]+resolved-state["time_s"])
            if index < len(checkpoints)-1:
                _require(checkpoint.get("command") is not None and sample.get("command") == checkpoint["command"],
                         "command_binding", "Missing executed request at an intermediate checkpoint")
            if previous is None:
                _same_state(separation_state, state, "separation_binding")
            else:
                _continuity(previous, state, profile)
                if development_boostback_candidate_index is not None:
                    prior_phase = checkpoints[index-1]["phase"]
                    expected_step = (.1 if _prediction_origin(previous, profile)[2] < 100000.
                                     or prior_phase != "recovery_entry_coast" else .25)
                    _require(state["time_s"]-previous["time_s"] <= expected_step+1e-8,
                             "development_scope", "Development alternatives must retain each macrostep")
            previous = state
        final = checkpoints[-1]["state"]
        _same_state(final, run.get("final_state"), "final_binding")
        start, end = separation_state["time_s"], final["time_s"]
        duration = end-start
        requested, resolved = record.get("requested_duration_s"), record.get("resolved_duration_s")
        _require(_number(resolved) and 0 < resolved <= 1200 and
                 (resolved == 1200 if requested is None else _number(requested) and requested == resolved),
                 "time_order", "Invalid requested/resolved duration binding")
        _require(0 < duration <= 1200.+1e-7 and _near(outcome.get("start_time_s"), start) and
                 _near(outcome.get("end_time_s"), end) and _near(outcome.get("duration_s"), duration) and duration <= resolved+1e-7,
                 "time_order", "Invalid bounded recovery interval")
        steps = outcome.get("integration_steps")
        _require(type(steps) is int and len(checkpoints)-1 <= steps <= 12001 and steps*.25+1e-7 >= duration,
                 "outcome", "Integration step count is not compatible with recorded duration")
        if development_boostback_candidate_index is not None:
            _require(steps == len(checkpoints)-1, "development_scope", "Missing declared macrostep checkpoints")
        termination = outcome.get("termination")
        _require(termination in _TERMINATIONS and outcome.get("phase") == checkpoints[-1]["phase"], "outcome", "Invalid terminal phase")
        stages = _events(run.get("events"), record, outcome, start, end)
        _prediction_records(record, start, end, profile)
        planning = _capture_planning(record, run["events"], profile, catch_config, development_cutoff_time_s,
                                     development_boostback_candidate_index)
        if development_cutoff_time_s is not None:
            for event in run["events"]:
                state = event.get("state")
                _state(state, profile)
                _require(state["time_s"] == event["time_s"], "development_scope", "Event state clock mismatch")
                left = max((c["state"] for c in checkpoints if c["time_s"] <= state["time_s"]), key=lambda s: s["time_s"])
                right = min((c["state"] for c in checkpoints if c["time_s"] >= state["time_s"]), key=lambda s: s["time_s"])
                if left["time_s"] == state["time_s"]:
                    _same_state(left, state, "development_scope")
                else:
                    _continuity(left, state, profile)
                if right["time_s"] != state["time_s"]:
                    _continuity(state, right, profile)
        if termination == "time_limit":
            _require(_near(duration, resolved), "outcome", "Time-limit record ended before the bounded request")
        probe = record.get("development_landing_probe")
        if development_landing_probe_time_s is None:
            _require(probe is None, "development_scope", "Landing contexts require explicit development scope")
        else:
            from .starship_landing_context import validate as validate_context
            _require(development_cutoff_time_s is not None and not development_fin_allocation
                     and _number(development_landing_probe_time_s) and type(probe) is dict
                     and set(probe) == {"schema", "requested_time_s", "snapshot", "production_policy_admitted"}
                     and probe["schema"] == "missionos.starship_landing_probe.v1"
                     and probe["requested_time_s"] == development_landing_probe_time_s
                     and probe["production_policy_admitted"] is False, "development_scope", "Unbound landing context")
            snapshot = probe["snapshot"]
            validate_context(snapshot, profile, catch_config)
            t = snapshot["state"]["time_s"]
            points = [c for c in checkpoints if c["time_s"] == t]
            _require(len(points) == 1 and points[0]["phase"] == "recovery_entry_coast"
                     and development_landing_probe_time_s <= t <= development_landing_probe_time_s+.75000001
                     and snapshot["deadline_s"] == start+resolved, "development_scope", "Context must bind a live checkpoint and its original deadline")
            _same_state(snapshot["state"], points[0]["state"], "development_scope")
        if termination == "angular_rate_envelope_exceeded":
            _require(_norm(final["omega_body_rad_s"]) > 5, "outcome", "Angular-rate termination lacks an exceeded bound")
        if termination == "rate_settle_failed":
            starts = [e for e in run["events"] if e["event"] == "boostback_complete_rate_settle"]
            _require(len(starts) == 1 and outcome["phase"] == "recovery_powered_rate_settle" and
                     _norm(final["omega_body_rad_s"]) >= _GUIDANCE["rate_settle_limit_rad_s"] and
                     _GUIDANCE["rate_settle_max_s"] < end-starts[0]["time_s"] <= _GUIDANCE["rate_settle_max_s"]+.25000001,
                     "outcome", "Rate-settle failure lacks the actual rate and elapsed timeout")
        for checkpoint in checkpoints:
            phase = checkpoint["phase"]
            if phase.startswith("recovery_landing_"):
                stage = int(phase.rsplit("_", 1)[1])
                _require(stage in stages and any(e["event"] == "landing_stage_requested" and
                         e.get("requested_engine_count") == stage and e["time_s"] <= checkpoint["time_s"] for e in run["events"]),
                         "events", "Landing command precedes its recorded stage request")
        if termination == "surface_contact":
            _require(type(run.get("contact")) is dict and run["contact"].get("contact") is True,
                     "outcome", "Surface-contact termination has no contact receipt")
        else:
            _require(run.get("contact") is None, "outcome", "Unexpected surface contact before terminal handoff")
        handoff = record.get("handoff")
        _require(type(handoff) is dict and type(handoff.get("eligible")) is bool and handoff.get("time_s") == end,
                 "handoff", "Missing measured terminal gate")
        actual = _arrival(final, profile, catch_config)
        _observe_handoff(handoff.get("observation"), actual, final, catch_config)
        _require(handoff.get("limits") == handoff["observation"]["limits"], "handoff_limits", "Gate limits differ across records")
        reached = handoff["eligible"]
        _require(reached is (termination == "catch_handoff"), "handoff", "Termination and handoff disagree")
        catch_verification = None
        if reached:
            _require(actual["eligible"] and outcome["phase"].startswith("recovery_landing_"),
                     "handoff_geometry", "Actual material points did not reach the terminal gate")
            _same_state(final, handoff.get("state"), "handoff_binding")
            _require(type(catch_run) is dict, "catch_binding", "Eligible handoff requires the actual terminal continuation")
            _json(catch_run)
            initialization = catch_run.get("catch_record", {}).get("initialization", {})
            _require(initialization.get("kind") == "supplied_state" and initialization.get("launch_connected") is False,
                     "catch_binding", "An initialized terminal fixture cannot replace the arrived physical state")
            _require(catch_run["catch_record"].get("control_policy") in ("fixed_v1", "net_thrust_trim_v1"),
                     "catch_binding", "Unknown terminal control policy")
            _same_state(final, catch_run.get("initial_state"), "catch_binding")
            samples_catch = catch_run.get("samples")
            _require(type(samples_catch) is list and samples_catch, "catch_binding", "Missing catch continuation")
            _same_state(final, samples_catch[0], "catch_binding")
            catch_verification = verify_catch(catch_run, profile, catch_config)
            _require(catch_verification["passed"], "catch_verification", "Independent terminal contact verification failed")
        else:
            _require(handoff.get("state") is None and catch_run is None, "catch_binding", "No terminal execution is allowed without arrival")
        result.update(passed=True, handoff_reached=reached, checkpoint_count=len(checkpoints),
                      catch_supported_after_handoff=bool(catch_verification and catch_verification["simulated_catch_supported"]))
        if planning is not None:
            result["capture_planning"] = planning
        if catch_verification is not None:
            result["catch_verification"] = catch_verification
    except _Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError, OverflowError, AttributeError):
        result["issues"].append({"code": "record", "detail": "Malformed recovery evidence or model configuration"})
    return result
