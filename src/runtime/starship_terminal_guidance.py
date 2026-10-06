"""Local terminal feedback coordinated with the unchanged attitude controller.

For each horizontal axis the declared hover model is
  x_dot = v; v_dot = g * theta;
  theta_ddot + 2*zeta*w*theta_dot + w*w*theta = w*w*theta_target.
State feedback theta_target = -Kx*x - Kv*v - Ktheta*theta - Krate*theta_dot
places its four poles at -w: Kx=w*w/g, Kv=4*w/g, Ktheta=5,
Krate=(4-2*zeta)/w. This polynomial matching is ordinary linear state feedback;
it neither changes the physical plant nor retunes the existing inner PD.

The measured pin midpoint is converted to retained-CoM position using the
actual rotated material-point lever. Its small-angle limit is x=pin_x-L*theta,
so pin lever feedback is not accidentally counted as another attitude gain.
Velocity and tilt rate use the rotating tower frame, including Earth rotation.

The model omits gimbal translation, actuator lag, changing mass, airloads,
coupled axes and saturation. Repeated linear poles do NOT establish nonlinear
stability, arrival, fuel feasibility, or support. Output is a bounded request
to the ordinary finite actuator controller, never an assigned vehicle state.
"""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import math
from numbers import Real

from . import starship_physics as env
from . import starship_sixdof as dyn

POLICY_ID = "terminal_hover_polynomial_feedback_v1"
SURFACE_GEOMETRY_SCHEMA = "missionos.starship_surface_terminal_geometry.v1"


def _number(value, name, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value)
            or abs(value) >= 1e20 or positive and value <= 0):
        raise ValueError("invalid_terminal_"+name)
    return float(value)


def _vector(value, name):
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError("invalid_terminal_"+name)
    return tuple(_number(x, name) for x in value)


def hover_feedback_gains(frequency_rad_s, damping_ratio, gravity_mps2):
    """Match (s+w)^4 for the explicitly stated ideal hover/attitude cascade."""
    w = _number(frequency_rad_s, "frequency", positive=True)
    zeta = _number(damping_ratio, "damping", positive=True)
    gravity = _number(gravity_mps2, "gravity", positive=True)
    return {"position_rad_per_m": w*w/gravity,
            "velocity_rad_s_per_m": 4*w/gravity,
            "tilt_dimensionless": 5., "tilt_rate_s": (4-2*zeta)/w}


def terminal_horizontal_request(state, arrival, profile, catch_config, gravity_mps2, *, return_site=None):
    """Return (east/north net acceleration request, causal local diagnostics).

    Aerodynamic compensation and the final force cone belong to the caller.
    This request is additionally bounded by the existing terminal tilt limit;
    its nominal cone uses the supplied gravity, not an invented force capacity.
    Only the current physical state and current arrival observation are read.
    """
    if not isinstance(state, dyn.State6DOF) or not all(
            type(value) is dict for value in (arrival, profile, catch_config)):
        raise ValueError("invalid_terminal_input")
    if return_site is not None:
        from .starship_return_sites import validate_return_site
        validate_return_site(return_site, profile, catch_config, terminal_goal="surrogate_pin_support")
    gravity = _number(gravity_mps2, "gravity", positive=True)
    if abs(_number(arrival.get("time_s"), "observation_time")-state.time_s) > 1e-8:
        raise ValueError("terminal_observation_time_mismatch")
    guidance, launch = profile.get("guidance"), profile.get("launch")
    if type(guidance) is not dict or type(launch) is not dict:
        raise ValueError("invalid_terminal_profile")
    w = _number(guidance.get("attitude_frequency_rad_s"), "frequency", positive=True)
    zeta = _number(guidance.get("attitude_damping_ratio"), "damping", positive=True)
    gains = hover_feedback_gains(w, zeta, gravity)
    maximum_tilt = _number(catch_config.get("terminal_max_tilt_deg"), "tilt_limit", positive=True)
    if maximum_tilt >= 30.:
        raise ValueError("invalid_terminal_tilt_limit")
    supports, pins = catch_config.get("support_points_body_m"), arrival.get("pins")
    if not isinstance(supports, (list, tuple)) or len(supports) != 2 or type(pins) is not list or len(pins) != 2:
        raise ValueError("invalid_terminal_pins")
    supports = [_vector(point, "support_point") for point in supports]
    if any(type(pin) is not dict for pin in pins):
        raise ValueError("invalid_terminal_pins")
    pin_positions = [_vector(pin.get("position_enu_m"), "pin_position") for pin in pins]
    com = _vector(arrival.get("com_body_m"), "centroid")
    lever = tuple(sum(point[i] for point in supports)/2-com[i] for i in range(3))
    midpoint = tuple(sum(point[i] for point in pin_positions)/2 for i in range(3))

    latitude = _number(launch.get("latitude_deg"), "latitude")
    longitude = _number(launch.get("longitude_deg"), "longitude")
    origin = env.surface_state(latitude, longitude, time_s=state.time_s)
    up, east, north = env.local_frame(origin)
    axes = (east, north, up)

    def local(vector):
        return tuple(float(env.dot(vector, axis)) for axis in axes)

    rotated_lever = local(dyn.rotate(state.q_body_to_eci, lever))
    cg_position = tuple(midpoint[i]-rotated_lever[i] for i in range(3))
    earth_rate = (0., 0., env.EARTH_ROTATION_RAD_S)
    cg_velocity = local(env.add(state.v_eci_mps, env.scale(env.cross(earth_rate, state.r_eci_m), -1.)))
    body_axis = local(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)))
    relative_rate = env.add(dyn.rotate(state.q_body_to_eci, state.omega_body_rad_s), env.scale(earth_rate, -1.))
    axis_derivative = local(env.cross(relative_rate, dyn.rotate(state.q_body_to_eci, (0., 0., 1.))))
    tilt = tuple(math.atan2(body_axis[i], body_axis[2]) for i in (0, 1))
    tilt_rate = tuple((body_axis[2]*axis_derivative[i]-body_axis[i]*axis_derivative[2])/
                      max(1e-12, body_axis[2]**2+body_axis[i]**2) for i in (0, 1))
    requested = tuple(-gains["position_rad_per_m"]*cg_position[i]
                      -gains["velocity_rad_s_per_m"]*cg_velocity[i]
                      -gains["tilt_dimensionless"]*tilt[i]
                      -gains["tilt_rate_s"]*tilt_rate[i] for i in (0, 1))
    length = math.hypot(*requested)
    limit = math.radians(maximum_tilt)
    bounded_angle = min(limit, length)
    scale = gravity*math.tan(bounded_angle)/max(length, 1e-30)
    acceleration = tuple(component*scale for component in requested)
    return acceleration, {
        "policy_id": POLICY_ID, "time_s": state.time_s,
        "model": "local_hover_translation_and_second_order_attitude",
        "pole_selection": "four_repeated_poles_at_negative_existing_attitude_frequency",
        "desired_poles_rad_s": [-w]*4,
        "desired_characteristic_polynomial": [1., 4*w, 6*w*w, 4*w**3, w**4],
        "inner_frequency_rad_s": w, "inner_damping_ratio": zeta,
        "gravity_mps2": gravity, "feedback_gains": gains,
        "observed_pin_midpoint_enu_m": list(midpoint),
        "observed_support_centroid_lever_body_m": list(lever),
        "observed_cg_position_enu_m": list(cg_position),
        "observed_cg_velocity_enu_mps": list(cg_velocity),
        "observed_body_axis_enu": list(body_axis),
        "observed_tilt_enu_rad": list(tilt), "observed_tilt_rate_enu_rad_s": list(tilt_rate),
        "earth_rotation_removed": True, "pin_to_cg_conversion": "exact_rotated_material_lever",
        "unbounded_target_tilt_enu_rad": list(requested),
        "maximum_requested_tilt_deg": maximum_tilt, "tilt_request_saturated": length > limit,
        "requested_net_acceleration_enu_mps2": list(acceleration),
        "local_hover_model_applicable": body_axis[2] >= math.cos(limit),
        "inner_controller_retuned": False, "physical_state_assigned": False,
        "prediction_is_execution": False, "production_policy_admitted": False,
        "arrival_admitted": False, "support_admitted": False,
        "nonlinear_stability_established": False,
        "limitations": ["Pole placement is for an ideal, unsaturated local model.",
                        "Finite gimbals, thrust-induced torque/translation, airloads and mass change remain in execution.",
                        "Outside the local hover attitude region this is only a bounded recovery request."]}


def surface_terminal_geometry(state, profile, *, return_site):
    """Current CG/body-axis kinematics in the declared surface target frame.

    This is a distinct surface geometry observation, without tower pins,
    control gains, actuation or a contact/success gate. Accepted quaternion
    roundoff is normalized for geometry only; the supplied state is unchanged.
    """
    from .starship_return_sites import _canonical, return_site_frame, state_errors, validate_return_site
    if not isinstance(state, dyn.State6DOF):
        raise ValueError("invalid_surface_terminal_state")
    validate_return_site(return_site, profile, terminal_goal="model_surface_contact")
    body = asdict(state)
    measured = state_errors(return_site, body)
    frame = return_site_frame(return_site, state.time_s)
    axes = tuple(tuple(frame[key]) for key in ("east_eci", "north_eci", "up_eci"))
    qnorm = math.sqrt(sum(x*x for x in state.q_body_to_eci))
    quaternion = tuple(x/qnorm for x in state.q_body_to_eci)
    def local(vector):
        return tuple(float(env.dot(vector, axis)) for axis in axes)
    world_axis = dyn.rotate(quaternion, (0., 0., 1.))
    body_axis = local(world_axis)
    earth_rate = (0., 0., env.EARTH_ROTATION_RAD_S)
    relative_rate = env.add(dyn.rotate(quaternion, state.omega_body_rad_s), env.scale(earth_rate, -1.))
    axis_derivative = local(env.cross(relative_rate, world_axis))
    tilt = tuple(math.atan2(body_axis[i], body_axis[2]) for i in (0, 1))
    tilt_rate = tuple((body_axis[2]*axis_derivative[i]-body_axis[i]*axis_derivative[2])/
        max(1e-12, body_axis[2]**2+body_axis[i]**2) for i in (0, 1))
    return {"schema": SURFACE_GEOMETRY_SCHEMA, "time_s": state.time_s, "site_id": return_site.site_id,
        "terminal_goal": return_site.terminal_goal, "return_site_sha256": return_site.sha256,
        "profile_sha256": sha256(_canonical(profile)).hexdigest(), "state_sha256": sha256(_canonical(body)).hexdigest(),
        "target_frame": frame, "position_error_enu_m": measured["position_error_enu_m"],
        "position_error_frame": "target_site_tangent_enu_not_geodetic_height",
        "ground_velocity_enu_mps": measured["ground_velocity_enu_mps"],
        "geometry_q_body_to_eci": list(quaternion), "observed_body_axis_enu": list(body_axis),
        "observed_tilt_enu_rad": list(tilt), "observed_tilt_rate_enu_rad_s": list(tilt_rate),
        "tilt_deg": measured["tilt_deg"], "body_rate_rad_s": measured["body_rate_rad_s"],
        "earth_rotation_removed": True, "quaternion_normalized_for_geometry_only": True,
        "state_assigned": False, "contact_or_support_verified": False, "safe_landing_verified": False}
