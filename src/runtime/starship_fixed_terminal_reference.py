"""One fixed-time material-pin/pose reference, never an imposed trajectory.

This is a development boundary-conditioned reference, not SpaceX software or
NASA DQG. Known actuator/geometry/fuel violations must reject the reference
before any actual finite-plant continuation. Passing a screen admits no flight.
"""
from __future__ import annotations

from copy import deepcopy
import math
from numbers import Real

from . import starship_physics as env
from . import starship_sixdof as dyn
from . import starship_actual_recovery_shooting as shooting

SCHEMA = "missionos.starship_fixed_terminal_reference.v1"
POLICY_ID = "fixed_time_pin_pose_quintic_v1"
SCREEN_SCHEMA = "missionos.starship_fixed_terminal_reference_screen.v1"
CONFIG = {"policy_id": POLICY_ID, "duration_source": "initial_existing_zem_zev_tgo",
    "position_frame": "rotating_tower_enu_material_pin_midpoint", "maximum_candidate_calls": 1,
    "maximum_candidate_duration_s": 60., "maximum_candidate_steps": 600,
    "maximum_candidate_wall_s": 120., "reference_screen_intervals": 100,
    "runtime_candidate_calls": 0, "physics_coefficients_changed": False,
    "gains_changed": False, "catch_gates_changed": False, "physical_execution": False}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or abs(value) >= 1e50:
        raise ValueError("invalid_fixed_terminal_number")
    return float(value)


def _vector(value, size=3):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError("invalid_fixed_terminal_vector")
    return tuple(_number(x) for x in value)


def quintic_coefficients(p0, v0, a0, p1, v1, a1, duration_s):
    """The unique scalar quintic satisfying six endpoint conditions."""
    p0, v0, a0, p1, v1, a1, t = map(_number, (p0, v0, a0, p1, v1, a1, duration_s))
    if t <= 0:
        raise ValueError("invalid_fixed_terminal_duration")
    dp, dv, da = p1-p0-v0*t-.5*a0*t*t, v1-v0-a0*t, a1-a0
    return [p0, v0, .5*a0, 10*dp/t**3-4*dv/t**2+.5*da/t,
            -15*dp/t**4+7*dv/t**3-da/t**2, 6*dp/t**5-3*dv/t**4+.5*da/t**3]


def _polynomial(coefficients, time_s, derivative=0):
    return sum(coefficients[i]*math.factorial(i)/math.factorial(i-derivative)*time_s**(i-derivative)
               for i in range(derivative, len(coefficients)))


def _rotation_vector(q):
    q = tuple(float(x) for x in dyn.normalize_quaternion(q))
    axis = max(range(1, 4), key=lambda i: abs(q[i]))
    if (abs(q[0]) <= 1e-14 and q[axis] < 0) or (abs(q[0]) > 1e-14 and q[0] < 0):
        q = tuple(-x for x in q)
    length = math.hypot(*q[1:])
    angle = 2*math.atan2(length, max(0., q[0]))
    return tuple(x*angle/length for x in q[1:]) if length > 1e-15 else (0., 0., 0.)


def _tower_frame(profile, time_s):
    site = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], time_s=time_s)
    up, east, north = env.local_frame(site)
    return site, (east, north, up)


def initial_pin_kinematics(state, vehicle, profile, catch, previous_command):
    """Current finite-plant RHS and geometric material-point derivatives only.

    No numerical integration occurs. The command is the immediately previous
    actual causal command. CoM depletion derivatives affect pin kinematics;
    the unchanged plant's angular variable-mass convention remains unchanged.
    """
    dyn._validate_pair(state, vehicle, previous_command)
    observed = dyn.observe(state, vehicle)
    derivative = dyn._rhs(dyn._pack(state), vehicle, previous_command,
        tuple(x.available for x in state.engine_states), True, True, True, time_s=state.time_s)
    props = dyn.mass_properties(vehicle, state.propellant_kg)
    fuel_rate = derivative[13]
    fuel_acceleration = -sum(spec.max_thrust_n/(spec.isp_s*env.STANDARD_GRAVITY_MPS2)*derivative[14+3*i]
        for i, (spec, actual) in enumerate(zip(vehicle.engines, state.engine_states))
        if actual.available and state.propellant_kg > 0)
    com_ddot = env.add(env.scale(props.dcom_dpropellant_m_per_kg, fuel_acceleration),
        env.scale(props.dcom_dpropellant_m_per_kg, -2*fuel_rate*fuel_rate/props.mass_kg))
    support_mid = tuple(sum(p[i] for p in catch["support_points_body_m"])/2 for i in range(3))
    lever = env.add(support_mid, env.scale(props.com_body_m, -1.))
    lever_dot = env.scale(observed["com_rate_body_mps"], -1.)
    lever_ddot = env.scale(com_ddot, -1.)
    pin_position = env.add(state.r_eci_m, dyn.rotate(state.q_body_to_eci, lever))
    pin_velocity = env.add(state.v_eci_mps, dyn.rotate(state.q_body_to_eci,
        env.add(env.cross(state.omega_body_rad_s, lever), lever_dot)))
    angular_acceleration = tuple(derivative[10:13])
    rotational_acceleration = env.add(env.add(env.cross(angular_acceleration, lever),
        env.cross(state.omega_body_rad_s, env.cross(state.omega_body_rad_s, lever))),
        env.add(env.scale(env.cross(state.omega_body_rad_s, lever_dot), 2.), lever_ddot))
    pin_acceleration = env.add(tuple(derivative[3:6]), dyn.rotate(state.q_body_to_eci, rotational_acceleration))
    site, axes = _tower_frame(profile, state.time_s)
    earth = (0., 0., env.EARTH_ROTATION_RAD_S)
    relative_velocity = env.add(pin_velocity, env.scale(env.cross(earth, pin_position), -1.))
    relative_acceleration = env.add(env.add(pin_acceleration, env.scale(env.cross(earth, pin_velocity), -2.)),
                                    env.cross(earth, env.cross(earth, pin_position)))
    def local(vector):
        return [float(env.dot(vector, axis)) for axis in axes]
    return {"position_enu_m": local(env.add(pin_position, env.scale(site.r, -1.))),
        "velocity_enu_mps": local(relative_velocity), "acceleration_enu_mps2": local(relative_acceleration),
        "com_body_m": list(props.com_body_m), "com_rate_body_mps": list(observed["com_rate_body_mps"]),
        "com_acceleration_body_mps2": list(com_ddot), "angular_acceleration_body_rad_s2": list(angular_acceleration),
        "current_cg_acceleration_eci_mps2": list(derivative[3:6]),
        "source_model_load_inputs": {"mass_kg": observed["mass_kg"], "inertia_kg_m2": observed["inertia_kg_m2"],
            "engine_loads": deepcopy(observed["engine_loads"]),
            "aero_force_body_n": observed["aero_force_body_n"], "aero_torque_body_nm": observed["aero_torque_body_nm"],
            "gravity_acceleration_eci_mps2": env.gravity_acceleration(state.r_eci_m, j2=True),
            "gravity_gradient_torque_body_nm": observed["gravity_gradient_torque_body_nm"],
            "fuel_rate_kg_s": fuel_rate, "fuel_acceleration_kg_s2": fuel_acceleration,
            "engine_throttle_derivatives_per_s": [derivative[14+3*i] for i in range(len(vehicle.engines))],
            "rhs_angular_variable_mass_convention": dyn.VARIABLE_MASS_CONVENTION,
            "aero_and_plant_rhs_independently_replayed": False},
        "convention": "current_finite_plant_rhs_under_last_causal_command",
        "physical_plant_integrations": 0, "physical_state_assigned": False}


def build_terminal_reference(snapshot, profile, catch, *, prior_state, previous_command):
    from .starship_sixdof_mission import vehicle, _attitude
    from .starship_sixdof_booster import _navigation
    from .starship_booster_recovery import _tower_observation
    from .starship_constrained_guidance import landing_force
    shooting.validate_context(snapshot, profile, catch, shooting.physical_configuration_from_context(snapshot))
    state, prior = dyn.state_from_dict(snapshot["state"]), dyn.state_from_dict(prior_state)
    command = dyn.command_from_dict(previous_command)
    if (snapshot["context"]["phase"] != "recovery_entry_coast" or not 0 < state.time_s-prior.time_s <= .25000001
            or snapshot["context"]["prior_command_reference"]["time_s"] != prior.time_s):
        raise ValueError("fixed_terminal_requires_exact_causal_coast_anchor")
    booster = vehicle(profile, "booster")
    observed = dyn.observe(state, booster)
    kinematics = initial_pin_kinematics(state, booster, profile, catch, command)
    arrival = _tower_observation(state, booster, profile, catch)
    up, east, north, velocity, _, _ = _navigation(state, profile)
    local_v = [env.dot(velocity, axis) for axis in (east, north, up)]
    aero = dyn.rotate(state.q_body_to_eci, observed["aero_force_body_n"])
    _, original = landing_force(arrival["position_error_enu_m"], min(arrival["pin_height_above_support_m"]),
        local_v, [env.dot(aero, axis)/observed["mass_kg"] for axis in (east, north, up)],
        observed["mass_kg"], state.propellant_kg, profile, catch)
    duration = original["remaining_descent_time_s"]
    if not 0 < duration <= CONFIG["maximum_candidate_duration_s"]:
        raise ValueError("fixed_terminal_duration_outside_declared_candidate_budget")
    target_position = [0., 0., catch["support_height_m"]+catch["initial_pin_clearance_m"]+.5*catch["arm_half_width_m"]]
    target_velocity = [0., 0., 1.5*catch["initial_vertical_speed_mps"]]
    coefficients = [quintic_coefficients(kinematics["position_enu_m"][i], kinematics["velocity_enu_mps"][i],
        kinematics["acceleration_enu_mps2"][i], target_position[i], target_velocity[i], 0., duration) for i in range(3)]
    tracker = snapshot["context"]["reference_tracker"]
    start_q = tracker["quaternion"] if tracker is not None else snapshot["context"]["prior_command_reference"]["quaternion"]
    _, axes = _tower_frame(profile, state.time_s+duration)
    target_q = tuple(float(x) for x in _attitude(axes[2], axes[0]))
    rotation = _rotation_vector(dyn.quaternion_multiply(target_q, dyn.quaternion_conjugate(start_q)))
    angle = env.norm(rotation)
    maximum_rate = profile["guidance"]["max_angular_acceleration_rad_s2"]/profile["guidance"]["attitude_frequency_rad_s"]
    maximum_acceleration = profile["guidance"]["max_angular_acceleration_rad_s2"]
    return shooting.saved({"schema": SCHEMA, "policy_id": POLICY_ID, "configuration": deepcopy(CONFIG),
        "origin_context_sha256": shooting.digest(snapshot), "origin_state_sha256": shooting.digest(snapshot["state"]),
        "prior_state": deepcopy(prior_state), "prior_state_sha256": shooting.digest(prior_state),
        "previous_command": deepcopy(previous_command), "previous_command_sha256": shooting.digest(previous_command),
        "profile_sha256": shooting.digest(profile), "catch_profile_sha256": shooting.digest(catch),
        "reference_start_time_s": state.time_s, "terminal_time_s": state.time_s+duration, "duration_s": duration,
        "duration_source": "initial_existing_zem_zev_tgo", "initial_tgo_inputs": {
            "minimum_pin_clearance_m": min(arrival["pin_height_above_support_m"]),
            "local_cg_vertical_velocity_mps": local_v[2], "target_pin_clearance_m": original["target_pin_clearance_m"],
            "target_vertical_velocity_mps": original["target_pin_vertical_speed_mps"]},
        "initial_pin_kinematics": kinematics, "position_coefficients": coefficients,
        "target_position_enu_m": target_position, "target_velocity_enu_mps": target_velocity,
        "target_acceleration_enu_mps2": [0., 0., 0.], "pose_start_q_body_to_eci": list(start_q),
        "pose_target_q_body_to_eci": list(target_q), "pose_relative_rotation_vector_eci_rad": list(rotation),
        "pose_scalar_coefficients": quintic_coefficients(0., 0., 0., 1., 0., 0., duration),
        "pose_peak_rate_rad_s": 1.875*angle/duration,
        "pose_peak_acceleration_rad_s2": 10*angle/(math.sqrt(3)*duration*duration),
        "initial_raw_pose_rate_eci_rad_s": [0., 0., 0.],
        "carried_requested_rate_eci_rad_s": tracker["rate_eci_rad_s"] if tracker is not None else [0., 0., 0.],
        "raw_pose_boundary_is_carried_rate": tracker is None or math.hypot(*tracker["rate_eci_rad_s"]) == 0.,
        "raw_pose_global_rate_continuity_established": False,
        "maximum_reference_rate_rad_s": maximum_rate, "maximum_reference_acceleration_rad_s2": maximum_acceleration,
        "reference_tracker_history_reset": False, "reference_is_achieved_state": False,
        "prediction_is_execution": False, "physical_state_assigned": False, "physical_execution": False,
        "arrival_admitted": False, "support_admitted": False, "native_solver_invoked": False,
        "joint_reference_feasibility_established": False})


def evaluate_terminal_reference(plan, time_s):
    now = _number(time_s)
    elapsed = now-plan["reference_start_time_s"]
    if elapsed < -1e-9:
        raise ValueError("fixed_terminal_reference_before_causal_origin")
    t, duration = max(0., elapsed), plan["duration_s"]
    if t <= duration:
        position, velocity, acceleration, jerk = ([float(_polynomial(c, t, order))
            for c in plan["position_coefficients"]] for order in range(4))
        scalar = [_polynomial(plan["pose_scalar_coefficients"], t, order) for order in range(3)]
        rotation = plan["pose_relative_rotation_vector_eci_rad"]
        angle = env.norm(rotation)
        q = (dyn.quaternion_multiply(dyn.axis_angle(rotation, scalar[0]*angle), plan["pose_start_q_body_to_eci"])
             if angle > 1e-15 else tuple(plan["pose_start_q_body_to_eci"]))
        rate, angular_acceleration = env.scale(rotation, scalar[1]), env.scale(rotation, scalar[2])
    else:
        delta = t-duration
        position = [plan["target_position_enu_m"][i]+delta*plan["target_velocity_enu_mps"][i] for i in range(3)]
        velocity, acceleration, jerk = plan["target_velocity_enu_mps"], [0., 0., 0.], [0., 0., 0.]
        q = dyn.quaternion_multiply(dyn.axis_angle((0., 0., 1.), env.EARTH_ROTATION_RAD_S*delta),
                                    plan["pose_target_q_body_to_eci"])
        rate, angular_acceleration = (0., 0., env.EARTH_ROTATION_RAD_S), (0., 0., 0.)
    return {"time_s": now, "elapsed_s": t, "position_enu_m": list(position), "velocity_enu_mps": list(velocity),
        "acceleration_enu_mps2": list(acceleration), "jerk_enu_mps3": list(jerk),
        "pose_q_body_to_eci": list(dyn.normalize_quaternion(q)), "pose_rate_eci_rad_s": list(rate),
        "pose_acceleration_eci_rad_s2": list(angular_acceleration), "reference_is_execution": False}
