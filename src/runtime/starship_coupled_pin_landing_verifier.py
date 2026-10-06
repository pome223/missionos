"""Pure arithmetic for the fixed development coupled-pin landing policy.

Saved simulator state, controller requests, finite commands and terminal
observations are separate facts. No observer, policy producer or trajectory
integrator is imported here. Arithmetic alone grants no physical invocation.
"""

from __future__ import annotations

import math

from .starship_booster_recovery_verifier import (
    _Invalid as _RecoveryInvalid, _arrival, _continuity, _cross, _fin_allocation, _flow_and_centroid, _json,
    _observe_handoff, _profile_and_config, _state, _tower,
)
from .starship_fixed_terminal_reference_verifier import (
    _add, _dot, _kinematics, _mass, _norm, _scale, digest, expected_reference_sample,
)
from .starship_terminal_wrench_verifier import (
    _compare, _near, _number, _require, _rotate, _specs, _vector, verify_terminal_wrench,
)
from .starship_coupled_terminal_reference_verifier import (
    _artifact, _attitude, _conditioned, _multiply, _same, _tracking,
)
from .starship_reference_tracking_verifier import _tracking_control

POLICY_ID = "development_coupled_pin_landing_v1"
GUIDANCE_SCHEMA = "missionos.starship_coupled_pin_guidance.v1"
SCHEMA = "missionos.starship_coupled_pin_landing_trial.v1"
REQUEST_SCHEMA = "missionos.starship_coupled_pin_trial_request.v1"
BINDING_SCHEMA = "missionos.starship_coupled_pin_input_binding.v1"
CONFIG = {"maximum_simulated_duration_s": 60.0, "maximum_integration_steps": 600,
    "maximum_wall_s": 120.0, "maximum_macrostep_s": 0.1, "maximum_main_engines": 13,
    "feedback_gain_source": "existing_material_pin_horizontal_pd_and_vertical_cascade",
    "force_direction_source": "causal_current_pin_feedback_and_current_finite_model_loads",
    "post_reference_mode": "original_pin_target_hold_then_measured_corridor_descent",
    "reference_pose_is_assigned_state": False, "physics_coefficients_changed": False,
    "gains_or_actuator_or_catch_limits_changed": False, "hardware_execution": False}
PHASE = "recovery_landing_coupled_pin"
_GUIDANCE_FALSE = {"pin_acceleration_is_achieved", "force_request_is_achieved",
    "current_rhs_is_independently_measured_angular_acceleration",
    "prospective_control_angular_acceleration_is_current_rhs", "independent_pose_quintic_used",
    "static_pose_assigned_to_vehicle", "actual_state_assigned", "arrival_admitted", "support_admitted",
    "physical_execution", "terminal_horizontal_pin_lever_subtracted_twice"}
_GUIDANCE_FIELDS = {"schema", "policy_id", "time_s", "mode", "state_sha256", "previous_command_sha256",
    "translation_reference_sha256", "reference_sample", "actual_pin_kinematics", "feedback",
    "nonvertical_corridor_ready", "pin_height_in_existing_window", "current_model_angular_acceleration_body_rad_s2",
    "requested_cg_acceleration_eci_mps2", "unbounded_engine_force_eci_n", "requested_engine_force_eci_n",
    "requested_axis_eci", "carried_axis_eci", "low_force_anchor_unresolved", "machine_force_tolerance_n",
    "maximum_tilt_deg", "lateral_scale", "force_capacity_scale", "minimum_vertical_thrust_n",
    "target_frame_origin_eci_m", "target_frame_axes_eci", "terminal_horizontal_feedback",
    "horizontal_request_space", "vertical_floor_clipped", "force_bounds_are_requests_not_achieved"} | _GUIDANCE_FALSE


def actual_pin_midpoint(state, profile, catch):
    """Reconstruct actual material-point p/v with COMdot and Earth rotation."""
    _state(state, profile)
    arrival = _arrival(state, profile, catch)
    position = list(arrival["midpoint_enu_m"])
    velocity = [sum(pin["velocity_enu_mps"][i] for pin in arrival["pins"]) / 2.0 for i in range(3)]
    return position, velocity, arrival


def feedback_pin_acceleration(position, velocity, position_reference, velocity_reference,
                              acceleration_reference, catch):
    """Existing horizontal/vertical feedback gains, with no gain fitting."""
    _require(all(_vector(value) for value in (position, velocity, position_reference,
        velocity_reference, acceleration_reference)), "finite_pin_feedback_vectors")
    tau_position, tau_velocity = (catch[key] for key in ("terminal_position_tau_s", "terminal_velocity_tau_s"))
    _require(_number(tau_position) and tau_position > 0 and _number(tau_velocity) and tau_velocity > 0,
             "unchanged_positive_pin_feedback_times")
    kp = [1.0 / tau_position**2, 1.0 / tau_position**2, 1.0 / (tau_position * tau_velocity)]
    kv = [2.0 / tau_position, 2.0 / tau_position, 1.0 / tau_velocity]
    acceleration = [acceleration_reference[i] + kp[i] * (position_reference[i] - position[i])
        + kv[i] * (velocity_reference[i] - velocity[i]) for i in range(3)]
    return acceleration, kp, kv


def centroid_motion_from_command(state, command, profile):
    """Instantaneous model COM derivatives under the explicitly supplied command.

    The caller must identify a past causal command versus a prospective
    request. These derivatives are not integrated future observations.
    """
    _state(state, profile)
    _require(type(command) is dict and set(command) == {"engines", "flap_angles_rad"}
             and type(command["engines"]) is list and len(command["engines"]) == len(state["engine_states"]),
             "source_bound_centroid_command_shape")
    mass, com, cdot, flow = _flow_and_centroid(state, profile)
    _, _, dc, _ = _mass(profile, state["propellant_kg"])
    flow_acceleration = 0.0
    for i, (actual, target) in enumerate(zip(state["engine_states"], command["engines"])):
        _require(type(target) is dict and set(target) == {"enabled", "throttle", "gimbal_x_rad", "gimbal_y_rad"}
                 and type(target["enabled"]) is bool and _number(target["throttle"])
                 and 0 <= target["throttle"] <= 1, "finite_centroid_throttle_command")
        if not actual["available"] or state["propellant_kg"] <= 0:
            continue
        goal = target["throttle"] if target["enabled"] else 0.0
        delta = goal - actual["throttle"]
        rate_limit = profile["actuators"]["throttle_rate_s"] if i < profile["booster"]["engine_count"] else 2.0
        rate = 0.0 if abs(delta) <= 1e-10 else max(-rate_limit, min(rate_limit,
            delta / profile["actuators"]["throttle_tau_s"]))
        thrust = profile["booster"]["engine_thrust_n"] if i < profile["booster"]["engine_count"] else profile["actuators"]["rcs_thrust_n"]
        isp = profile["booster"]["engine_isp_s"] if i < profile["booster"]["engine_count"] else profile["actuators"]["rcs_isp_s"]
        flow_acceleration += rate * thrust / (isp * 9.80665)
    cddot = _add(_scale(dc, -flow_acceleration), dc, -2.0 * flow**2 / mass)
    return {"mass_kg": mass, "com_body_m": com, "com_rate_body_mps": cdot,
        "com_acceleration_body_mps2": cddot, "fuel_rate_kg_s": -flow,
        "fuel_acceleration_kg_s2": -flow_acceleration}


def _command(command, state, profile):
    """Legal actuator requests for this known policy, including the off mask."""
    _require(type(command) is dict and set(command) == {"engines", "flap_angles_rad"}
             and type(command["engines"]) is list and len(command["engines"]) == len(state["engine_states"])
             and type(command["flap_angles_rad"]) is list
             and len(command["flap_angles_rad"]) == len(state["flap_angles_rad"]), "closed_pin_policy_command")
    count, gimbals = profile["booster"]["engine_count"], profile["booster"]["gimbal_engine_count"]
    limit = math.radians(profile["actuators"]["max_gimbal_deg"])
    for i, command_row in enumerate(command["engines"]):
        _require(type(command_row) is dict and set(command_row) == {"enabled", "throttle", "gimbal_x_rad", "gimbal_y_rad"}
                 and type(command_row["enabled"]) is bool and _number(command_row["throttle"])
                 and 0 <= command_row["throttle"] <= 1
                 and (command_row["throttle"] >= (0.4 if i < count else 0.0)
                      if command_row["enabled"] else command_row["throttle"] == 0)
                 and all(_number(command_row[key]) and abs(command_row[key]) <= (limit if i < gimbals else 0.0) + 1e-8
                     for key in ("gimbal_x_rad", "gimbal_y_rad")), "legal_pin_policy_engine_command")
        if 13 <= i < count:
            _require(command_row["enabled"] is False, "pin_policy_main_mask_within_13")
    fin_limit = math.radians(profile["actuators"]["grid_fin_limit_deg"])
    _require(all(_number(value) and abs(value) <= (0.0 if i < 3 else fin_limit) + 1e-8
        for i, value in enumerate(command["flap_angles_rad"])), "legal_pin_policy_fin_command")


def _terminal_horizontal(state, profile, catch, arrival, gravity):
    """Independent arithmetic for the existing CG-space hover polynomial law."""
    _require(_number(gravity) and gravity > 0, "positive_current_model_gravity")
    w, damping = (profile["guidance"][key] for key in ("attitude_frequency_rad_s", "attitude_damping_ratio"))
    gains = {"position_rad_per_m": w * w / gravity, "velocity_rad_s_per_m": 4 * w / gravity,
        "tilt_dimensionless": 5.0, "tilt_rate_s": (4 - 2 * damping) / w}
    midpoint = arrival["midpoint_enu_m"]
    lever = _add([sum(point[i] for point in catch["support_points_body_m"]) / 2 for i in range(3)],
        arrival["com_body_m"], -1.0)
    _, axes = _tower(profile, state["time_s"])
    def local(vector):
        return [_dot(vector, axis) for axis in axes]
    q, omega = state["q_body_to_eci"], state["omega_body_rad_s"]
    rotated_lever = local(_rotate(q, lever))
    cg_position = _add(midpoint, rotated_lever, -1.0)
    earth = [0.0, 0.0, 7.292115e-5]
    cg_velocity = local(_add(state["v_eci_mps"], _cross(earth, state["r_eci_m"]), -1.0))
    body_axis = local(_rotate(q, [0.0, 0.0, 1.0]))
    relative_rate = _add(_rotate(q, omega), earth, -1.0)
    axis_derivative = local(_cross(relative_rate, _rotate(q, [0.0, 0.0, 1.0])))
    tilt = [math.atan2(body_axis[i], body_axis[2]) for i in (0, 1)]
    tilt_rate = [(body_axis[2] * axis_derivative[i] - body_axis[i] * axis_derivative[2])
        / max(1e-12, body_axis[2]**2 + body_axis[i]**2) for i in (0, 1)]
    requested = [-gains["position_rad_per_m"] * cg_position[i] - gains["velocity_rad_s_per_m"] * cg_velocity[i]
        - gains["tilt_dimensionless"] * tilt[i] - gains["tilt_rate_s"] * tilt_rate[i] for i in (0, 1)]
    length = math.hypot(*requested)
    maximum_tilt = catch["terminal_max_tilt_deg"]
    limit = math.radians(maximum_tilt)
    scale = gravity * math.tan(min(limit, length)) / max(length, 1e-30)
    acceleration = [value * scale for value in requested]
    return acceleration, {"policy_id": "terminal_hover_polynomial_feedback_v1", "time_s": state["time_s"],
        "model": "local_hover_translation_and_second_order_attitude",
        "pole_selection": "four_repeated_poles_at_negative_existing_attitude_frequency",
        "desired_poles_rad_s": [-w] * 4,
        "desired_characteristic_polynomial": [1.0, 4 * w, 6 * w * w, 4 * w**3, w**4],
        "inner_frequency_rad_s": w, "inner_damping_ratio": damping, "gravity_mps2": gravity,
        "feedback_gains": gains, "observed_pin_midpoint_enu_m": midpoint,
        "observed_support_centroid_lever_body_m": lever, "observed_cg_position_enu_m": cg_position,
        "observed_cg_velocity_enu_mps": cg_velocity, "observed_body_axis_enu": body_axis,
        "observed_tilt_enu_rad": tilt, "observed_tilt_rate_enu_rad_s": tilt_rate,
        "earth_rotation_removed": True, "pin_to_cg_conversion": "exact_rotated_material_lever",
        "unbounded_target_tilt_enu_rad": requested, "maximum_requested_tilt_deg": maximum_tilt,
        "tilt_request_saturated": length > limit, "requested_net_acceleration_enu_mps2": acceleration,
        "local_hover_model_applicable": body_axis[2] >= math.cos(limit), "inner_controller_retuned": False,
        "physical_state_assigned": False, "prediction_is_execution": False, "production_policy_admitted": False,
        "arrival_admitted": False, "support_admitted": False, "nonlinear_stability_established": False,
        "limitations": ["Pole placement is for an ideal, unsaturated local model.",
            "Finite gimbals, thrust-induced torque/translation, airloads and mass change remain in execution.",
            "Outside the local hover attitude region this is only a bounded recovery request."]}


def _guidance(receipt, state, previous_command, reference, profile, catch, arrival, carried_axis):
    for value in (receipt, state, previous_command, reference, profile, catch, arrival, carried_axis):
        _json(value, maximum_nodes=32_000_000)
    _state(state, profile)
    _command(previous_command, state, profile)
    actual = _arrival(state, profile, catch)
    _observe_handoff(arrival, actual, state, catch)
    _require(type(receipt) is dict and set(receipt) == _GUIDANCE_FIELDS
             and receipt["schema"] == GUIDANCE_SCHEMA and receipt["policy_id"] == POLICY_ID
             and receipt["time_s"] == state["time_s"] and all(receipt[key] is False for key in _GUIDANCE_FALSE)
             and receipt["force_bounds_are_requests_not_achieved"] is True,
             "closed_pin_guidance_scope")
    for key, value in (("state_sha256", state), ("previous_command_sha256", previous_command),
            ("translation_reference_sha256", reference)):
        _require(receipt[key] == digest(value), "pin_guidance_causal_source_binding")
    _require(_vector(carried_axis) and _near(_norm(carried_axis), 1.0, absolute=1e-8), "carried_pin_axis_not_reset")
    _compare(receipt["carried_axis_eci"], carried_axis, absolute=1e-10)
    kin = _kinematics(receipt["actual_pin_kinematics"], state, previous_command, profile, catch)
    limits = actual["limits"]
    corridor = (math.hypot(*actual["midpoint_enu_m"][:2]) <= limits["horizontal_position_m"]
        and all(math.hypot(*pin["velocity_enu_mps"][:2]) <= limits["pin_horizontal_speed_mps"] for pin in actual["pins"])
        and actual["tilt_deg"] <= limits["attitude_angle_deg"]
        and actual["clocking_error_deg"] <= limits["attitude_angle_deg"]
        and actual["body_rate_rad_s"] <= limits["body_rate_rad_s"]
        and state["propellant_kg"] >= limits["propellant_reserve_kg"])
    height_ready = all(catch["initial_pin_clearance_m"] <= pin["height_above_support_m"]
        <= catch["initial_pin_clearance_m"] + catch["arm_half_width_m"] for pin in actual["pins"])
    following = state["time_s"] <= reference["terminal_time_s"]
    sample = expected_reference_sample(reference, state["time_s"]) if following else {
        "time_s": state["time_s"], "position_enu_m": reference["target_position_enu_m"],
        "velocity_enu_mps": [0.0, 0.0, 0.0], "acceleration_enu_mps2": [0.0, 0.0, 0.0]}
    mode = "fixed_pin_polynomial_tracking" if following else "measured_pin_target_hold"
    if not following and corridor and height_ready:
        mode = "measured_settled_terminal_descent"
        sample["velocity_enu_mps"][2] = 1.5 * catch["initial_vertical_speed_mps"]
    _require(receipt["mode"] == mode and receipt["nonvertical_corridor_ready"] is corridor
             and receipt["pin_height_in_existing_window"] is height_ready, "measured_hold_and_descent_gate")
    _require(type(receipt["reference_sample"]) is dict and set(receipt["reference_sample"]) == set(sample),
             "closed_pin_reference_sample")
    for key in sample:
        if type(sample[key]) is list:
            _compare(receipt["reference_sample"][key], sample[key], absolute=1e-8)
        elif type(sample[key]) is bool:
            _require(receipt["reference_sample"][key] is sample[key], "reference_sample_claim")
        else:
            _require(_near(receipt["reference_sample"][key], sample[key], absolute=1e-8), "reference_sample_clock")
    acceleration, kp, kv = feedback_pin_acceleration(kin["position_enu_m"], kin["velocity_enu_mps"],
        sample["position_enu_m"], sample["velocity_enu_mps"], sample["acceleration_enu_mps2"], catch)
    feedback = receipt["feedback"]
    _require(type(feedback) is dict and set(feedback) == {"feedback_gains", "position_error_enu_m",
        "velocity_error_enu_mps", "pin_acceleration_request_enu_mps2"}
        and type(feedback["feedback_gains"]) is dict
        and set(feedback["feedback_gains"]) == {"position_per_s2", "velocity_per_s"}, "closed_existing_pin_feedback")
    for key, expected in (("position_error_enu_m", _add(sample["position_enu_m"], kin["position_enu_m"], -1.0)),
            ("velocity_error_enu_mps", _add(sample["velocity_enu_mps"], kin["velocity_enu_mps"], -1.0)),
            ("pin_acceleration_request_enu_mps2", acceleration)):
        _compare(feedback[key], expected, absolute=1e-8)
    _compare(feedback["feedback_gains"]["position_per_s2"], kp, absolute=1e-12)
    _compare(feedback["feedback_gains"]["velocity_per_s"], kv, absolute=1e-12)
    site, axes = _tower(profile, state["time_s"])
    _compare(receipt["target_frame_origin_eci_m"], site, absolute=1e-6)
    _require(type(receipt["target_frame_axes_eci"]) is list and len(receipt["target_frame_axes_eci"]) == 3,
             "closed_rotating_tower_axes")
    for given, expected in zip(receipt["target_frame_axes_eci"], axes):
        _compare(given, expected, absolute=1e-10)
    earth = [0.0, 0.0, 7.292115e-5]
    lever = _add([sum(point[i] for point in catch["support_points_body_m"]) / 2 for i in range(3)], kin["com_body_m"], -1.0)
    ldot, lddot = _scale(kin["com_rate_body_mps"], -1.0), _scale(kin["com_acceleration_body_mps2"], -1.0)
    q, omega = state["q_body_to_eci"], state["omega_body_rad_s"]
    pin_r = _add(state["r_eci_m"], _rotate(q, lever))
    pin_v = _add(state["v_eci_mps"], _rotate(q, _add(_cross(omega, lever), ldot)))
    pin_acc = [sum(acceleration[j] * axes[j][i] for j in range(3)) for i in range(3)]
    pin_acc = _add(_add(pin_acc, _scale(_cross(earth, pin_v), 2.0)), _cross(earth, _cross(earth, pin_r)), -1.0)
    alpha = kin["angular_acceleration_body_rad_s2"]
    rotational = _add(_add(_cross(alpha, lever), _cross(omega, _cross(omega, lever))),
        _add(_scale(_cross(omega, ldot), 2.0), lddot))
    cg_acc = _add(pin_acc, _rotate(q, rotational), -1.0)
    loads = kin["source_model_load_inputs"]
    if following:
        _require(receipt["terminal_horizontal_feedback"] is None
                 and receipt["horizontal_request_space"] == "material_pin_tower_enu", "polynomial_pin_request_space")
    else:
        # The current arrival vectors/centroid were independently reconstructed
        # and checked above with their existing bounds. The existing hover law
        # reads the producer's recorded pin mean and centroid, so retain those
        # exact validated operands instead of re-feeding cancellation roundoff
        # from a second ECI-to-tower reconstruction into strict receipt checks.
        hover_arrival = {**actual,
            "midpoint_enu_m": [sum(pin["position_enu_m"][i] for pin in arrival["pins"]) / 2.0 for i in range(3)],
            "com_body_m": arrival["com_body_m"]}
        horizontal, detail = _terminal_horizontal(state, profile, catch, hover_arrival,
            _norm(loads["gravity_acceleration_eci_mps2"]))
        _same(receipt["terminal_horizontal_feedback"], detail, "existing_hover_feedback_not_retuned")
        _require(receipt["horizontal_request_space"] == "cg_tower_enu", "hold_horizontal_is_already_cg_request")
        relative = _add(_add(cg_acc, _scale(_cross(earth, state["v_eci_mps"]), -2.0)),
            _cross(earth, _cross(earth, state["r_eci_m"])))
        cg_local = [_dot(relative, axis) for axis in axes]
        cg_local[:2] = horizontal
        relative = [sum(cg_local[j] * axes[j][i] for j in range(3)) for i in range(3)]
        cg_acc = _add(_add(relative, _scale(_cross(earth, state["v_eci_mps"]), 2.0)),
            _cross(earth, _cross(earth, state["r_eci_m"])), -1.0)
    _compare(receipt["current_model_angular_acceleration_body_rad_s2"], alpha, absolute=1e-8)
    _compare(receipt["requested_cg_acceleration_eci_mps2"], cg_acc, absolute=1e-8)
    mass = loads["mass_kg"]
    force = _add(_scale(_add(cg_acc, loads["gravity_acceleration_eci_mps2"], -1.0), mass),
        _rotate(q, loads["aero_force_body_n"]), -1.0)
    _compare(receipt["unbounded_engine_force_eci_n"], force, absolute=1e-3)
    _require(receipt["vertical_floor_clipped"] is
        (_dot(receipt["unbounded_engine_force_eci_n"], receipt["target_frame_axes_eci"][2]) < 0.5 * mass),
        "vertical_floor_is_request_clipping_only")
    local = [_dot(force, axis) for axis in axes]
    local[2] = max(0.5 * mass, local[2])
    height = min(pin["height_above_support_m"] for pin in actual["pins"]) - reference["target_position_enu_m"][2] + catch["support_height_m"]
    tilt = 60.0 if following and height > 1000.0 else 30.0 if following and height > 100.0 else catch["terminal_max_tilt_deg"]
    lateral_scale = min(1.0, local[2] * math.tan(math.radians(tilt)) / max(math.hypot(*local[:2]), 1e-30))
    local[:2] = [value * lateral_scale for value in local[:2]]
    force = [sum(local[j] * axes[j][i] for j in range(3)) for i in range(3)]
    capacity = min(1.0, 13 * profile["booster"]["engine_thrust_n"] / max(_norm(force), 1e-30))
    force = _scale(force, capacity)
    tolerance = math.sqrt(math.ulp(1.0)) * max(1.0, mass * 9.81)
    low_force = _norm(force) <= tolerance
    axis = carried_axis if low_force else _scale(force, 1.0 / _norm(force))
    for key, expected in (("requested_engine_force_eci_n", force), ("requested_axis_eci", axis)):
        _compare(receipt[key], expected, absolute=1e-3 if key.endswith("_n") else 1e-10)
    for key, expected in (("machine_force_tolerance_n", tolerance), ("maximum_tilt_deg", tilt),
            ("lateral_scale", lateral_scale), ("force_capacity_scale", capacity),
            ("minimum_vertical_thrust_n", max(0.0, _dot(force, axes[2])))):
        _require(_near(receipt[key], expected, absolute=1e-8), "existing_force_request_limits")
    _require(receipt["low_force_anchor_unresolved"] is low_force, "small_force_not_achieved_axis")
    # Every recorded operand has just been independently checked against the
    # unchanged force/axis tolerances and request limits. Downstream selectors
    # and allocation arithmetic must use the exact floats the producer used,
    # rather than amplify tiny reconstruction differences into strict branch
    # or dictionary comparisons.
    return receipt["requested_engine_force_eci_n"], receipt["requested_axis_eci"]


def verify_pin_guidance_request(receipt, state, previous_command, reference, profile, catch, *,
                                arrival_observation, carried_axis_eci):
    result = {"schema": "missionos.starship_coupled_pin_guidance_verification.v1", "passed": False,
        "arithmetic_passed": False, "source_authenticated": False, "physical_invocation_admitted": False,
        "configured_aerodynamic_loads_independently_replayed": False, "dynamics_replayed": False,
        "actual_pin_kinematics_arithmetic_checked": False, "issues": [], "actual_state_assigned": False,
        "force_request_is_achieved": False, "arrival_admitted": False, "support_admitted": False,
        "physical_execution": False}
    try:
        _guidance(receipt, state, previous_command, reference, profile, catch, arrival_observation, carried_axis_eci)
        result.update(passed=True, arithmetic_passed=True, actual_pin_kinematics_arithmetic_checked=True)
    except (_RecoveryInvalid, ValueError, TypeError, KeyError, IndexError, OverflowError,
            RecursionError, ZeroDivisionError, AttributeError):
        result["issues"].append("Invalid source-bound actual-pin feedback request arithmetic")
    return result


def _base_selection(state, force, axis, profile):
    body_axis = _rotate(state["q_body_to_eci"], [0.0, 0.0, 1.0])
    signed = _dot(body_axis, axis)
    projection = max(0.0, _dot(body_axis, force))
    selector_input = projection * max(0.2, signed)
    pair = None
    detail = {"requested_axis_thrust_alignment": signed}
    if signed <= 0:
        count, throttle = 3, 0.4
        detail["landing_alignment_slew"] = {"active": True, "translational_thrust_admitted": False,
            "signed_projection": signed}
    else:
        required = selector_input / max(0.2, signed)
        available = 0.0
        for count in range(1, 14):
            if state["engine_states"][count - 1]["available"]:
                available += profile["booster"]["engine_thrust_n"]
            if available >= required:
                break
        floor = 0.4 if any(e["available"] for e in state["engine_states"][:count]) else 0.0
        throttle = max(floor, min(1.0, required / max(available, 1.0)))
        positions = profile["booster"]["engine_positions_body_m"]
        for i in range(13):
            for j in range(i + 1, 13):
                if (state["engine_states"][i]["available"] and state["engine_states"][j]["available"]
                        and math.hypot(positions[i][0] + positions[j][0], positions[i][1] + positions[j][1]) <= 1e-8):
                    pair_throttle = required / (2 * profile["booster"]["engine_thrust_n"])
                    if 0.4 <= pair_throttle <= 1.0:
                        pair = [i, j]
                        count, throttle = 2, pair_throttle
                        break
            if pair is not None:
                break
    detail.update(least_squares_actual_axis_projection_n=projection, legacy_selector_input_n=selector_input,
        base_pair_indices=pair, base_scalar_force_projection_is_achieved=False)
    return count, throttle, pair, detail


def _controller_certificate(step, state, profile, pretrim, requested, tracking, force, axis, remaining_s):
    """Reuse pure PD/fin/wrench checks; close the measured TVC command algebra."""
    import numpy as np

    nav, base, command = (step[key] for key in ("navigation", "base_command", "command"))
    _command(base, state, profile)
    _command(command, state, profile)
    kin = nav["coupled_pin_guidance"]["actual_pin_kinematics"]
    loads = kin["source_model_load_inputs"]
    pressure = nav["current_dynamic_pressure_pa"]
    _require(_number(pressure) and pressure >= 0, "current_model_pressure_not_future_prediction")
    expected_fin = "finite_regularized_fins_v1" if pretrim is None else "finite_moment_priority_fins_v1"
    _require(nav["finite_fin_policy"] == expected_fin, "carried_fin_latch_policy")
    count, throttle, pair, selection = _base_selection(state, force, axis, profile)
    _same(nav["base_actuation"], selection, "existing_scalar_projection_selector")
    _require(nav["base_main_engine_count"] == count and _near(nav["base_main_throttle"], throttle, absolute=1e-10),
             "known_base_count_and_throttle")
    sample = {"controller": nav, "inertia_kg_m2": loads["inertia_kg_m2"],
        "aero_torque_body_nm": loads["aero_torque_body_nm"], "dynamic_pressure_pa": pressure}
    guide = profile["guidance"]
    maximum, frequency, damping = (guide[k] for k in
        ("max_angular_acceleration_rad_s2", "attitude_frequency_rad_s", "attitude_damping_ratio"))
    _tracking_control(nav["reference_tracking_control"], nav, state, sample, profile, requested,
        tracking["reference_rate_body_rad_s"], tracking["reference_acceleration_body_rad_s2"],
        maximum, maximum / frequency, frequency, damping)
    _fin_allocation({"state": state, "command": base, "phase": PHASE, "navigation": nav}, sample,
        profile, True, remaining_s, expected_policy=expected_fin)
    demand = nav["requested_torque_body_nm"]
    _require(_vector(demand), "finite_remaining_engine_torque_request")
    specs = _specs(profile)
    com = kin["com_body_m"]
    expected = [{"enabled": i < count and throttle > 0, "throttle": throttle if i < count else 0.0,
        "gimbal_x_rad": 0.0, "gimbal_y_rad": 0.0} for i in range(45)]
    columns, channels, fixed = [], [], [0.0] * 3
    for i in range(count):
        if not state["engine_states"][i]["available"]:
            continue
        thrust = specs[i]["thrust"] * throttle
        lever = _add(specs[i]["position"], com, -1.0)
        fixed = _add(fixed, _cross(lever, [0.0, 0.0, thrust]))
        for key, derivative in (("gimbal_x_rad", [0.0, -thrust, 0.0]), ("gimbal_y_rad", [thrust, 0.0, 0.0])):
            columns.append(_cross(lever, derivative))
            channels.append((i, key))
    residual = _add(demand, fixed, -1.0)
    limit = math.radians(profile["actuators"]["max_gimbal_deg"])
    if columns:
        matrix = np.asarray(columns, dtype=float).T
        solution = np.linalg.lstsq(matrix, np.asarray(residual), rcond=1e-9)[0]
        clipped = [max(-limit, min(limit, float(value))) for value in solution]
        for value, (i, key) in zip(clipped, channels):
            expected[i][key] = value
        residual = _add(residual, (matrix @ np.asarray(clipped)).tolist(), -1.0)

    def jets(target, remaining):
        capacity = 2 * profile["actuators"]["rcs_radius_m"] * profile["actuators"]["rcs_thrust_n"]
        for i in range(33, 45):
            coordinate, sign = (i - 33) // 4, (-1, 1)[((i - 33) % 4) // 2]
            value = max(0.0, min(1.0, remaining[coordinate] * sign / capacity))
            target[i].update(enabled=value > 1e-8, throttle=value if value > 1e-8 else 0.0)

    jets(expected, residual)
    measured_columns, measured_channels, main_moment = [], [], [0.0] * 3
    actual_thrust = 0.0
    for i in range(33):
        current = loads["engine_loads"][i]
        main_moment = _add(main_moment, current["torque_body_nm"])
        thrust = current["thrust_n"]
        actual_thrust += thrust
        actual = state["engine_states"][i]
        if i >= 13 or not actual["available"] or thrust <= 0:
            continue
        gx, gy = actual["gimbal_x_rad"], actual["gimbal_y_rad"]
        lever = _add(specs[i]["position"], com, -1.0)
        for key, direction in (("gimbal_x_rad", [-math.sin(gy) * math.sin(gx), -math.cos(gx), -math.cos(gy) * math.sin(gx)]),
                ("gimbal_y_rad", [math.cos(gy) * math.cos(gx), 0.0, -math.sin(gy) * math.cos(gx)])):
            measured_columns.append(_cross(lever, _scale(direction, thrust)))
            measured_channels.append((i, key))
    info = {"policy_id": "measured_tvc_v1", "main_thrust_n": actual_thrust,
        "gimballed_engine_count": len(measured_channels) // 2, "main_throttle_commands_preserved": True,
        "current_main_torque_body_nm": main_moment, "applied": bool(measured_columns)}
    if measured_columns:
        matrix = np.asarray(measured_columns, dtype=float).T
        solution = np.linalg.lstsq(matrix, np.asarray(_add(demand, main_moment, -1.0)), rcond=1e-9)[0]
        clipped = []
        for value, (i, key) in zip(solution, measured_channels):
            actual = state["engine_states"][i][key]
            target = max(-limit, min(limit, actual + float(value)))
            expected[i][key] = target
            clipped.append(target - actual)
        residual = _add(_add(demand, main_moment, -1.0), (matrix @ np.asarray(clipped)).tolist(), -1.0)
        jets(expected, residual)
        info.update(predicted_residual_torque_body_nm=residual,
            allocation_scope="current main thrust and local gimbal Jacobian; finite actuator tracking still required")
    if pair is not None:
        for i in range(33):
            expected[i].update(enabled=i in pair, throttle=throttle if i in pair else 0.0)
    _same(nav["measured_tvc_allocation"], info, "current_measured_tvc_inputs_and_local_residual")
    for given, calculated in zip(base["engines"], expected):
        _require(given["enabled"] is calculated["enabled"], "known_controller_engine_enable")
        for key in ("throttle", "gimbal_x_rad", "gimbal_y_rad"):
            _require(_near(given[key], calculated[key], absolute=1e-8), "known_controller_finite_engine_command")
    if nav.get("development_fin_allocation") is None:
        _compare(base["flap_angles_rad"], state["flap_angles_rad"], absolute=1e-10)
    _, axes = _tower(profile, state["time_s"])
    wrench = verify_terminal_wrench(nav["terminal_wrench"], state, base, command, profile,
        desired_force_eci_n=force, requested_engine_torque_body_nm=demand, up_eci=axes[2],
        minimum_vertical_thrust_n=nav["coupled_pin_guidance"]["minimum_vertical_thrust_n"])
    _require(wrench["passed"], "finite_wrench_closed_request_not_achieved_force")
    stored = nav["terminal_wrench"]
    selected = stored["mask_candidates"][stored["selected_mask_index"]] if stored["status"] == "accepted" else None
    predicted_force = selected["predicted_force_eci_n"] if selected else stored.get("base_predicted_force_eci_n")
    predicted_torque = selected["predicted_engine_torque_body_nm"] if selected else stored.get("base_predicted_engine_torque_body_nm")
    summary = {"status": stored["status"], "fallback_reason": stored["reason"],
        "commanded_main_indices": [i for i, e in enumerate(command["engines"][:33]) if e["enabled"]],
        "predicted_finite_force_eci_n": predicted_force, "predicted_finite_engine_torque_body_nm": predicted_torque,
        "predicted_force_residual_eci_n": _add(predicted_force, force, -1.0) if predicted_force is not None else None,
        "prediction_is_achieved_force_or_moment": False, "command_is_hardware_execution": False}
    _same(nav["finite_wrench_summary"], summary, "finite_endpoint_summary_is_only_a_prediction")


_MEMORY_FIELDS = {"conditioned_reference", "reference_tracker", "entry_pretrim_prepared_at_s",
    "previous_actual_command", "previous_actual_command_time_s"}
_CP_FIELDS = {"time_s", "phase", "state", "command", "com_rate_body_mps", "step_receipt_artifact"}
_STEP_FIELDS = {"kind", "step_index", "time_s", "state", "state_sha256", "base_command", "command", "navigation",
    "controller_memory_before"}
_BINDING_HASHES = {"source_run_sha256", "source_prior_checkpoint_sha256", "source_origin_checkpoint_sha256",
    "origin_context_sha256", "prior_command_sha256", "translation_reference_sha256", "profile_sha256",
    "catch_profile_sha256", "static_17_result_sha256", "static_17_reverification_sha256",
    "static_17_raw_json_sha256", "static_17_source_map_sha256"}
_BINDING_FIELDS = _BINDING_HASHES | {"schema", "source_origin_checkpoint_index", "source_binding_is_caller_assertion",
    "source_authentication_independently_verified", "reference_is_execution", "full_launch_reexecuted"}
_REQUEST_FIELDS = {"schema", "origin_context_sha256", "duration_s", "maximum_integration_steps",
    "wall_deadline_monotonic_s", "automatic_retry", "hardware_execution", "catch_execution"}
_NAV_FIELDS = {"target_q_body_to_eci", "requested_torque_body_nm", "attitude_error_deg", "control_allocation",
    "reference_tracking_control", "measured_tvc_allocation", "coupled_pin_guidance", "reference_tracking",
    "conditioned_reference_diagnostics", "terminal_wrench", "finite_wrench_summary", "current_dynamic_pressure_pa",
    "arrival_observation", "macrostep_s", "base_main_throttle", "base_main_engine_count", "finite_fin_policy",
    "requested_engine_force_eci_n", "base_actuation"}


def _hash(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _parallel_diagnostics(previous_q, axis):
    old = _rotate(previous_q, [0.0, 0.0, 1.0])
    cross = _cross(old, axis)
    sine, cosine = _norm(cross), max(-1.0, min(1.0, _dot(old, axis)))
    angle = math.atan2(sine, cosine)
    antipodal = sine < 1e-12 and cosine < 0
    delta = [0.0, *_rotate(previous_q, [1.0, 0.0, 0.0])] if antipodal else (
        [1.0, 0.0, 0.0, 0.0] if sine < 1e-12 else [math.cos(angle / 2), *_scale(cross, math.sin(angle / 2) / sine)])
    transported = _multiply(delta, previous_q)
    transported = _scale(transported, 1.0 / _norm(transported))
    if _dot(transported, previous_q) < 0:
        transported = _scale(transported, -1.0)
    return {"policy_id": "parallel_transport_v1", "target_q_body_to_eci": transported,
        "axis_step_rad": angle, "antipodal_fallback": antipodal}


def _trace(run, profile, catch, corpus, source_run, expected_bindings):
    from .starship_fixed_terminal_reference_verifier import verify_fixed_terminal_reference

    for value in (run, profile, catch, corpus, source_run, expected_bindings):
        _json(value, maximum_nodes=32_000_000)
    _profile_and_config(profile, catch)
    _require(type(run) is dict and set(run) == {"scenario", "body_id", "guidance_policy", "initial_state",
        "final_state", "samples", "events", "contact", "recovery_record", "outcome"}
        and run["scenario"] == "booster_coupled_pin_trial" and run["body_id"] == "booster"
        and run["guidance_policy"] == POLICY_ID, "known_coupled_pin_trial_without_predictive_header")
    record, outcome = run["recovery_record"], run["outcome"]
    fields = {"schema", "policy_id", "guidance_configuration", "origin_context", "translation_reference", "source_bindings",
        "inputs_artifact", "initial_controller_memory", "final_controller_memory", "checkpoints", "handoff", "request",
        "failure", "started_monotonic_s", "failure_chain", "production_policy_admitted", "physical_execution", "missionos_dispatch"}
    _require(type(record) is dict and set(record) == fields and record["schema"] == SCHEMA
             and record["policy_id"] == POLICY_ID and record["guidance_configuration"] == CONFIG
             and all(record[k] is False for k in ("production_policy_admitted", "physical_execution", "missionos_dispatch")),
             "closed_isolated_pin_trial_record")
    _same(record["guidance_configuration"], CONFIG, "typed_pin_trial_configuration")
    origin, reference, binding, request = (record[k] for k in
        ("origin_context", "translation_reference", "source_bindings", "request"))
    _require(type(binding) is dict and set(binding) == _BINDING_FIELDS and binding["schema"] == BINDING_SCHEMA
             and all(_hash(binding[k]) for k in _BINDING_HASHES)
             and type(binding["source_origin_checkpoint_index"]) is int and binding["source_origin_checkpoint_index"] >= 1
             and binding["source_binding_is_caller_assertion"] is True
             and all(binding[k] is False for k in ("source_authentication_independently_verified", "reference_is_execution", "full_launch_reexecuted"))
             and (expected_bindings is None or binding == expected_bindings), "closed_source_input_binding")
    for key, value in (("origin_context_sha256", origin), ("prior_command_sha256", reference["previous_command"]),
            ("translation_reference_sha256", reference), ("profile_sha256", profile), ("catch_profile_sha256", catch)):
        _require(binding[key] == digest(value), "causal_pin_trial_input_digest")
    anchors = False
    if source_run is not None:
        points = source_run["recovery_record"]["checkpoints"]
        index = binding["source_origin_checkpoint_index"]
        _require(index < len(points) and digest(source_run) == binding["source_run_sha256"]
                 and digest(points[index - 1]) == binding["source_prior_checkpoint_sha256"]
                 and digest(points[index]) == binding["source_origin_checkpoint_sha256"]
                 and points[index]["state"] == origin["state"]
                 and points[index - 1]["state"] == reference["prior_state"]
                 and points[index - 1]["command"] == reference["previous_command"], "exact_prior_actuator_command_and_origin_cp")
        checked = verify_fixed_terminal_reference(reference, origin, profile, catch,
            prior_checkpoint=points[index - 1], source_run=source_run)
        _require(checked["arithmetic_passed"] and checked["past_source_anchors_bound"], "source_bound_fixed_translation")
        anchors = True
    _require(type(request) is dict and set(request) == _REQUEST_FIELDS and request["schema"] == REQUEST_SCHEMA
             and request["origin_context_sha256"] == digest(origin)
             and _number(request["duration_s"]) and 0 < request["duration_s"] <= 60.0
             and type(request["maximum_integration_steps"]) is int and 1 <= request["maximum_integration_steps"] <= 600
             and all(request[k] is False for k in ("automatic_retry", "hardware_execution", "catch_execution"))
             and _number(record["started_monotonic_s"])
             and _number(request["wall_deadline_monotonic_s"])
             and 0 < request["wall_deadline_monotonic_s"] - record["started_monotonic_s"] <= 120.0,
             "closed_bounded_learning_trial_request")
    _state(origin["state"], profile)
    _require(run["initial_state"] == origin["state"] and origin["context"]["phase"] == "recovery_entry_coast"
             and reference["origin_context_sha256"] == digest(origin), "exact_initial_state_not_reference_projection")
    initial = {"conditioned_reference": origin["context"]["conditioned_reference"],
        "reference_tracker": origin["context"]["reference_tracker"],
        "entry_pretrim_prepared_at_s": origin["context"]["entry_pretrim_prepared_at_s"],
        "previous_actual_command": reference["previous_command"],
        "previous_actual_command_time_s": reference["prior_state"]["time_s"]}
    _require(initial["reference_tracker"] is not None, "actual_carried_tracker_no_historyless_reset")
    _same(record["initial_controller_memory"], initial, "entire_initial_controller_memory_inherited")
    inputs = _artifact(record["inputs_artifact"], corpus)
    _same(inputs, {"kind": "coupled_pin_trial_inputs", "schema": SCHEMA, "origin_context": origin,
        "translation_reference": reference, "profile": profile, "catch_profile": catch, "configuration": CONFIG,
        "request": request, "source_bindings": binding, "controller_memory": initial,
        "started_monotonic_s": record["started_monotonic_s"], "isolated_saved_state_continuation": True,
        "full_launch_reexecuted": False, "hardware_execution": False}, "persisted_trial_inputs_before_commands")
    points = record["checkpoints"]
    _require(type(points) is list and 1 <= len(points) <= 601 and points[0]["state"] == origin["state"],
             "bounded_actual_cp_sequence")
    expected_memory = initial
    seen = {record["inputs_artifact"]["artifact_id"]}
    attempts = transitions = 0
    pending_attempt = False
    end = origin["state"]["time_s"] + request["duration_s"]
    for i, point in enumerate(points):
        _require(type(point) is dict and set(point) == _CP_FIELDS and point["phase"] == PHASE,
                 "typed_current_actual_cp")
        state = point["state"]
        _state(state, profile)
        _require(point["time_s"] == state["time_s"] and state["time_s"] <= end + 1e-7, "actual_cp_clock")
        _, _, cdot, _ = _flow_and_centroid(state, profile)
        _compare(point["com_rate_body_mps"], cdot, absolute=1e-6)
        if i:
            _continuity(points[i - 1]["state"], state, profile)
            _require(state["time_s"] - points[i - 1]["time_s"] <= 0.1 + 1e-7, "actual_partial_step_not_nominal_projection")
            transitions += 1
        manifest = point["step_receipt_artifact"]
        if manifest is None:
            _require(i == len(points) - 1 and point["command"] is None, "only_terminal_cp_has_no_command_attempt")
            continue
        _require(manifest["artifact_id"] not in seen, "unique_durable_step_attempt")
        seen.add(manifest["artifact_id"])
        step = _artifact(manifest, corpus)
        _require(set(step) == _STEP_FIELDS and step["kind"] == "coupled_pin_step_receipt"
                 and step["step_index"] == attempts and step["time_s"] == state["time_s"]
                 and step["state"] == state and step["state_sha256"] == digest(state), "request_before_same_state_step")
        _same(step["controller_memory_before"], expected_memory, "carried_controller_history_not_reset")
        _require(set(step["controller_memory_before"]) == _MEMORY_FIELDS, "closed_controller_memory")
        nav = step["navigation"]
        _require(set(nav) in (_NAV_FIELDS, _NAV_FIELDS | {"development_fin_allocation"}), "known_navigation_receipt_fields")
        before_tracker = expected_memory["reference_tracker"]
        carried_axis = _rotate(before_tracker["quaternion"], [0.0, 0.0, 1.0])
        force, axis = _guidance(nav["coupled_pin_guidance"], state, expected_memory["previous_actual_command"],
            reference, profile, catch, nav["arrival_observation"], carried_axis)
        _compare(nav["requested_engine_force_eci_n"], force, absolute=1e-3)
        _, axes = _tower(profile, state["time_s"])
        raw_goal = _attitude(axis, axes[0])
        frame, diagnostic = _conditioned(expected_memory["conditioned_reference"], raw_goal, state["time_s"], profile)
        _same(nav["conditioned_reference_diagnostics"], diagnostic, "current_force_axis_and_east_clocking")
        tracker = _tracking(nav["reference_tracking"], before_tracker, frame["quaternion"], state["q_body_to_eci"],
            state["omega_body_rad_s"], state["time_s"], profile)
        step_size = min(0.1, end - state["time_s"])
        if abs(step_size - 0.1) <= 1e-9:
            step_size = 0.1
        _require(_near(nav["macrostep_s"], step_size, absolute=1e-10), "requested_macrostep_bound")
        _controller_certificate(step, state, profile, expected_memory["entry_pretrim_prepared_at_s"],
            tracker["quaternion"], nav["reference_tracking"], force, axis, end - state["time_s"])
        expected_memory = {**expected_memory,
            "conditioned_reference": {**frame, "diagnostics": diagnostic,
                "frame_diagnostics": _parallel_diagnostics(expected_memory["conditioned_reference"]["quaternion"], axis)},
            "reference_tracker": {**before_tracker, **tracker}}
        applied = i + 1 < len(points)
        if applied:
            _require(point["command"] == step["command"], "applied_command_matches_durable_attempt")
            expected_memory = {**expected_memory, "previous_actual_command": step["command"],
                "previous_actual_command_time_s": state["time_s"]}
        else:
            pending_attempt = True
            _require(point["command"] is None and (record["failure"] is not None
                     or outcome.get("termination") == "wall_budget_exhausted_before_integration"),
                     "pending_command_attempt_has_no_applied_step_credit")
        attempts += 1
    _require(set(corpus) == seen, "no_hidden_control_attempts_in_supplied_step_corpus")
    _same(record["final_controller_memory"], expected_memory, "final_causal_controller_memory")
    final = points[-1]["state"]
    _require(run["final_state"] == final, "final_state_is_last_actual_cp")
    handoff = record["handoff"]
    _require(type(handoff) is dict and set(handoff) == {"eligible", "time_s", "state", "observation", "limits"}
             and type(handoff["eligible"]) is bool and handoff["time_s"] == final["time_s"], "same_time_handoff_record")
    actual = _arrival(final, profile, catch)
    _observe_handoff(handoff["observation"], actual, final, catch)
    _require(handoff["limits"] == actual["limits"], "unchanged_eight_gates_and_support_reserve")
    _require(type(outcome) is dict and set(outcome) == {"termination", "start_time_s", "end_time_s", "duration_s",
        "integration_steps", "handoff_reached", "final_hull_clearance_m", "actual_isolated_simulator_continuation",
        "full_launch_reexecuted", "catch_executed", "physical_execution", "mission_completed", "wall_seconds"}
        and outcome["start_time_s"] == origin["state"]["time_s"] and outcome["end_time_s"] == final["time_s"]
        and _near(outcome["duration_s"], final["time_s"] - origin["state"]["time_s"], absolute=1e-9)
        and outcome["integration_steps"] == transitions
        and outcome["handoff_reached"] is handoff["eligible"]
        and outcome["actual_isolated_simulator_continuation"] is (transitions > 0)
        and all(outcome[k] is False for k in ("full_launch_reexecuted", "catch_executed", "physical_execution", "mission_completed"))
        and _number(outcome["wall_seconds"]) and outcome["wall_seconds"] >= 0, "isolated_trial_outcome_not_mission_completion")
    termination = outcome["termination"]
    allowed = {"simulated_duration_exhausted", "integration_step_budget_exhausted", "wall_budget_exhausted",
        "wall_budget_exhausted_before_integration",
        "catch_handoff", "propellant_exhausted", "angular_rate_envelope_exceeded", "surface_contact",
        "coupled_pin_controller_or_record_exception"}
    _require(termination in allowed and transitions <= request["maximum_integration_steps"], "bounded_known_termination")
    failure, chain = record["failure"], record["failure_chain"]
    handoff_excluded = {"surface_contact", "propellant_exhausted", "angular_rate_envelope_exceeded",
        "wall_budget_exhausted_before_integration", "coupled_pin_controller_or_record_exception"}
    qualifying_endpoint = (transitions > 0 and failure is None and run["contact"] is None
        and final["propellant_kg"] > 0 and _norm(final["omega_body_rad_s"]) <= 5
        and not pending_attempt and termination not in handoff_excluded)
    if qualifying_endpoint:
        _require(handoff["eligible"] is actual["eligible"], "final_eligible_endpoint_has_matching_handoff")
    if handoff["eligible"]:
        _require(actual["eligible"] and termination == "catch_handoff" and handoff["state"] == final
                 and failure is None and run["contact"] is None and not pending_attempt,
                 "actual_all_eight_gates_before_handoff")
    else:
        _require(handoff["state"] is None and termination != "catch_handoff", "no_fake_handoff_state")
    if termination == "simulated_duration_exhausted":
        _require(_near(final["time_s"], end, absolute=1e-7), "actual_requested_horizon_reached")
    if termination == "integration_step_budget_exhausted":
        _require(transitions == request["maximum_integration_steps"], "step_budget_not_hidden_failure")
    if termination == "wall_budget_exhausted_before_integration":
        _require(pending_attempt, "wall_before_integration_retains_unapplied_command_attempt")
    if termination == "propellant_exhausted":
        _require(final["propellant_kg"] <= 0, "actual_fuel_exhaustion")
    if termination == "angular_rate_envelope_exceeded":
        _require(_norm(final["omega_body_rad_s"]) > 5, "actual_rate_envelope_negative")
    if termination == "surface_contact":
        _require(type(run["contact"]) is dict and run["contact"].get("contact") is True, "contact_is_not_support")
    else:
        _require(run["contact"] is None, "no_unreported_contact")
    _require(type(chain) is list and len(chain) <= 8 and (failure is None) is (not chain), "retained_primary_failure_chain")
    if failure is not None:
        _require(chain[0] == failure and all(type(x) is dict and type(x.get("error_class")) is str
            and x["error_class"].isidentifier() and x.get("computation_completed") is False
            and x.get("arrival_admitted") is False for x in chain), "errors_are_not_successful_computation")
    _require(type(run["events"]) is list and len(run["events"]) == 1
             and run["events"][0] == {"event": termination, "time_s": final["time_s"], "state": final}, "terminal_event_actual_state_binding")
    interval_completed = (failure is None and not pending_attempt
        and termination == "simulated_duration_exhausted" and _near(final["time_s"], end, absolute=1e-7))
    return anchors, transitions, attempts, actual["eligible"], failure is None, interval_completed


def verify_coupled_pin_landing_trial(run, profile, catch, *, step_corpus, source_run=None, expected_source_bindings=None):
    """Verify saved known-policy requests and causal states, never replay dynamics."""
    result = {"schema": "missionos.starship_coupled_pin_landing_verification.v1", "passed": False,
        "arithmetic_record_integrity_passed": False, "source_bound_arithmetic_passed": False,
        "verification_scope": "known_policy_causal_simulator_record_not_physical_admission",
        "source_authenticated": False, "dynamics_replayed": False, "physical_invocation_admitted": False,
        "positive_time_transitions_checked": 0, "durable_command_attempts_checked": 0,
        "computation_completed_without_recorded_error": False, "final_same_time_arrival_gates_satisfied": False,
        "requested_interval_completed": False,
        "fin_aerodynamic_effectiveness_independently_replayed": False, "runtime_invocation_independently_verified": False,
        "arrival_admitted": False, "support_admitted": False, "physical_execution": False,
        "mission_completed": False, "issues": []}
    try:
        anchors, transitions, attempts, final_gate, completed, interval_completed = _trace(run, profile, catch, step_corpus,
            source_run, expected_source_bindings)
        result.update(passed=True, arithmetic_record_integrity_passed=True,
            source_bound_arithmetic_passed=anchors and expected_source_bindings is not None,
            positive_time_transitions_checked=transitions, durable_command_attempts_checked=attempts,
            computation_completed_without_recorded_error=completed, final_same_time_arrival_gates_satisfied=final_gate,
            requested_interval_completed=interval_completed)
    except (_RecoveryInvalid, ValueError, TypeError, KeyError, IndexError, OverflowError,
            RecursionError, ZeroDivisionError, AttributeError):
        result["issues"].append("Invalid isolated coupled-pin known-policy source/command/history/state record")
    return result
