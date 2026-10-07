"""Independent arithmetic for causal bounded attitude requests.

The requested quaternion is a command reference, never an assigned vehicle
state. These checks neither import nor replay the reference/control producer or
the nonlinear dynamics. Finite references do not certify actuator feasibility.
"""
from __future__ import annotations

import math

from .starship_booster_recovery_verifier import (
    _compare_vector, _cross, _near, _norm, _number, _require, _rotate,
    _same_value, _vector,
)

POLICY_ID = "bounded_reference_tracking_v1"
_FORBIDDEN_CLAIMS = ("production_policy_admitted", "physical_execution", "mission_completed",
                     "arrival_admitted", "support_admitted", "nonlinear_plant_certified")


def _multiply(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return [aw*bw-ax*bx-ay*by-az*bz, aw*bx+ax*bw+ay*bz-az*by,
            aw*by-ax*bz+ay*bw+az*bx, aw*bz+ax*by-ay*bx+az*bw]


def _inverse(q):
    return [q[0], *(-x for x in q[1:])]


def _quaternion(value):
    return _vector(value, 4) and _near(_norm(value), 1., 1e-8)


def _rotation_vector(q):
    if ((abs(q[0]) > 1e-14 and q[0] < 0)
            or (abs(q[0]) <= 1e-14 and q[1+max(range(3), key=lambda i: abs(q[i+1]))] < 0)):
        q = [-x for x in q]
    length = math.hypot(*q[1:])
    if length < 1e-14:
        return [0., 0., 0.]
    scale = 2*math.atan2(length, max(0., q[0]))/length
    return [x*scale for x in q[1:]]


def _exp(rotation):
    angle = math.hypot(*rotation)
    if angle < 1e-14:
        return [1., 0., 0., 0.]
    return [math.cos(angle/2), *(x*math.sin(angle/2)/angle for x in rotation)]


def _clip_vector(value, limit):
    length = math.hypot(*value)
    return [x*min(1., limit/max(length, 1e-30)) for x in value]


def _inertia(state, profile):
    booster, geometry = profile["booster"], profile["geometry"]
    dry, fuel = booster["dry_mass_kg"], state["propellant_kg"]
    radius, length = geometry["radius_m"], geometry["booster_length_m"]
    base = [dry*(3*radius*radius+length*length)/12]*2+[dry*radius*radius/2]
    tensor = booster.get("dry_inertia_kg_m2")
    if tensor is not None:
        _require(type(tensor) is list and len(tensor) == 3 and all(_vector(row) for row in tensor),
                 "reference_tracking", "Invalid configured dry inertia")
    result = [[(base[i] if i == j else 0.) if tensor is None else tensor[i][j]
               for j in range(3)] for i in range(3)]
    fuel_diagonal = [fuel*(3*radius*radius+booster["tank_length_m"]**2)/12]*2+[fuel*radius*radius/2]
    transverse = dry*fuel/(dry+fuel)*(booster["tank_center_z_m"]-booster["dry_com_z_m"])**2
    for i in range(3):
        result[i][i] += fuel_diagonal[i]+(transverse if i < 2 else 0.)
    return result


def _matvec(matrix, vector):
    return [sum(a*b for a, b in zip(row, vector)) for row in matrix]


def verify_reference_checkpoint(checkpoint, sample, profile, *, previous_checkpoint=None, checkpoint_index=None,
                                initial_previous_phase="recovery_powered_entry_prepare"):
    """Check one scoped request and, when selected, its tracking-law algebra.

    The first entry request must descend from the immediately preceding saved
    powered-preparation command. Its zero reference rate is a declared initial
    assumption, not a measured body rate. Later requests bind adjacent records.
    Only an explicitly declared first historyless fixture may instead retain
    its actual current pose as a zero-rate REQUEST at that same clock.
    """
    navigation, state = checkpoint["navigation"], checkpoint["state"]
    item, control = navigation.get("reference_tracking"), navigation.get("reference_tracking_control")
    scoped = checkpoint["phase"] == "recovery_entry_coast" or checkpoint["phase"].startswith("recovery_landing_")
    if checkpoint.get("command") is None or not scoped:
        _require(item is None and control is None, "reference_tracking", "Tracking escaped its commanded entry/landing scope")
        return None
    _require(type(item) is dict and item.get("schema") == "missionos.starship_reference_tracking.v1"
             and item.get("policy_id") == POLICY_ID and item.get("time_s") == state["time_s"]
             and _same_value(item, sample.get("controller", {}).get("reference_tracking")),
             "reference_tracking", "Missing clock-bound causal request receipt")
    _require(item.get("request_is_execution") is False and item.get("actual_state_assigned") is False
             and all(item.get(key, False) is False for key in _FORBIDDEN_CLAIMS)
             and item.get("reference_kinematics") == "constant-rate quaternion increments; inertial backward-difference acceleration"
             and item.get("closing_rate_law") == "v*dt + v^2/(2*amax) <= shortest goal error; causal raw-goal-rate feedforward"
             and item.get("maximum_interval_s") == 1.,
             "reference_tracking", "A bounded request cannot reset state or replace its preceding command anchor")
    historyless = previous_checkpoint is None
    if historyless:
        _require(type(checkpoint_index) is int and checkpoint_index == 0,
                 "reference_tracking", "A historyless actual-pose request anchor is confined to the first fixture checkpoint")
        first, previous_q, previous_goal = True, state["q_body_to_eci"], state["q_body_to_eci"]
        previous_rate, previous_time = [0., 0., 0.], state["time_s"]
    else:
        _require(type(previous_checkpoint) is dict and previous_checkpoint.get("command") is not None,
                 "reference_tracking", "A reference requires its immediately preceding executed checkpoint")
        prior = previous_checkpoint.get("navigation", {}).get("reference_tracking")
        first, previous_time = prior is None, previous_checkpoint["time_s"]
    if not historyless and first:
        _require(checkpoint["phase"] == "recovery_entry_coast"
                 and initial_previous_phase in ("recovery_powered_entry_prepare", "recovery_entry_shutdown_tail")
                 and previous_checkpoint.get("phase") == initial_previous_phase,
                 "reference_tracking", "The first request must inherit the preceding powered preparation goal")
        previous_q = previous_checkpoint["navigation"].get("target_q_body_to_eci")
        previous_rate = [0., 0., 0.]
        previous_goal = previous_q
    elif not historyless:
        _require(previous_checkpoint.get("phase") == "recovery_entry_coast"
                 or previous_checkpoint.get("phase", "").startswith("recovery_landing_"),
                 "reference_tracking", "Requested-frame history cannot restart after entry begins")
        previous_q, previous_rate = prior.get("requested_q_body_to_eci"), prior.get("reference_rate_eci_rad_s")
        previous_goal = prior.get("raw_goal_q_body_to_eci")
    _require(_quaternion(previous_q) and _quaternion(previous_goal) and _vector(previous_rate) and item.get("first_update") is first
             and item.get("previous_time_s") == previous_time
             and item.get("previous_raw_goal_time_s") == previous_time
             and item.get("initialized_at_current_time") is historyless,
             "reference_tracking", "Previous goal, rate or clock is not the adjacent recorded request")
    _compare_vector(item.get("previous_requested_q_body_to_eci"), previous_q, "reference_tracking", tolerance=1e-10)
    _compare_vector(item.get("previous_reference_rate_eci_rad_s"), previous_rate, "reference_tracking", tolerance=1e-10)
    _compare_vector(item.get("previous_raw_goal_q_body_to_eci"), previous_goal, "reference_tracking", tolerance=1e-10)
    interval = state["time_s"]-previous_time
    _require(_number(interval) and (interval == 0 if historyless else 0 < interval <= 1.)
             and _near(item.get("interval_s"), interval, 1e-12),
             "reference_tracking", "Reference interval differs from adjacent actual checkpoint clocks")
    guide = profile["guidance"]
    maximum, frequency, damping = (guide.get(key) for key in
        ("max_angular_acceleration_rad_s2", "attitude_frequency_rad_s", "attitude_damping_ratio"))
    _require(all(_number(x) and x > 0 for x in (maximum, frequency, damping)),
             "reference_tracking", "Reference bounds require positive finite existing profile gains")
    rate_limit = maximum/frequency
    _require(_near(item.get("maximum_reference_rate_rad_s"), rate_limit, 1e-12)
             and _near(item.get("maximum_reference_acceleration_rad_s2"), maximum, 1e-12)
             and _norm(previous_rate) <= rate_limit+1e-10,
             "reference_tracking", "Requested-rate limits differ from the declared unchanged profile")
    goal, actual = item.get("raw_goal_q_body_to_eci"), state["q_body_to_eci"]
    _require(_quaternion(goal) and _quaternion(actual), "reference_tracking", "Invalid raw or measured quaternion")
    _compare_vector(item.get("actual_q_body_to_eci"), actual, "reference_tracking", tolerance=1e-10)
    _compare_vector(item.get("actual_body_rate_rad_s"), state["omega_body_rad_s"], "reference_tracking", tolerance=1e-10)
    error = _rotation_vector(_multiply(goal, _inverse(previous_q)))
    goal_rotation = _rotation_vector(_multiply(goal, _inverse(previous_goal)))
    if historyless:
        unbounded_goal_rate = goal_rate = closing_rate = [0., 0., 0.]
        unbounded_rate = rate_limited = unbounded_acceleration = acceleration = rate = [0., 0., 0.]
        closing_bound, propagated, closing_error, requested = 0., previous_q, error, previous_q
    else:
        unbounded_goal_rate = [x/interval for x in goal_rotation]
        goal_rate = _clip_vector(unbounded_goal_rate, rate_limit)
        propagated = _multiply(_exp([x*interval for x in goal_rate]), previous_q)
        length = _norm(propagated)
        propagated = [x/length for x in propagated]
        closing_error = _rotation_vector(_multiply(goal, _inverse(propagated)))
        angle = math.hypot(*closing_error)
        acceleration_interval = maximum*interval
        closing_bound = math.sqrt(acceleration_interval**2+2*maximum*angle)-acceleration_interval
        closing_scale = min(1/interval, closing_bound/max(angle, 1e-300))
        closing_rate = [x*closing_scale for x in closing_error]
        unbounded_rate = [a+b for a, b in zip(goal_rate, closing_rate)]
        rate_limited = _clip_vector(unbounded_rate, rate_limit)
        unbounded_acceleration = [(a-b)/interval for a, b in zip(rate_limited, previous_rate)]
        acceleration = _clip_vector(unbounded_acceleration, maximum)
        rate = [a+interval*b for a, b in zip(previous_rate, acceleration)]
        requested = _multiply(_exp([x*interval for x in rate]), previous_q)
        length = _norm(requested)
        requested = [x/length for x in requested]
        if sum(a*b for a, b in zip(requested, previous_q)) < 0:
            requested = [-x for x in requested]
    body_rate = _rotate(_inverse(actual), rate)
    body_acceleration = _rotate(_inverse(actual), acceleration)
    for key, value in (("goal_error_rotation_eci_rad", error),
                       ("raw_goal_rotation_eci_rad", goal_rotation),
                       ("unbounded_goal_rate_eci_rad_s", unbounded_goal_rate),
                       ("goal_rate_eci_rad_s", goal_rate),
                       ("goal_rate_propagated_request_q_body_to_eci", propagated),
                       ("closing_error_rotation_eci_rad", closing_error),
                       ("closing_reference_rate_eci_rad_s", closing_rate),
                       ("unbounded_reference_rate_eci_rad_s", unbounded_rate),
                       ("rate_limited_reference_rate_eci_rad_s", rate_limited),
                       ("unbounded_reference_acceleration_eci_rad_s2", unbounded_acceleration),
                       ("reference_acceleration_eci_rad_s2", acceleration),
                       ("reference_rate_eci_rad_s", rate), ("requested_q_body_to_eci", requested),
                       ("reference_rate_body_rad_s", body_rate), ("reference_acceleration_body_rad_s2", body_acceleration)):
        _compare_vector(item.get(key), value, "reference_tracking", tolerance=1e-10)
    _require(_near(item.get("closing_rate_bound_rad_s"), closing_bound, 1e-10)
             and item.get("goal_rate_clipped") is (math.hypot(*unbounded_goal_rate) > rate_limit)
             and item.get("rate_clipped") is (math.hypot(*unbounded_rate) > rate_limit)
             and item.get("acceleration_clipped") is (math.hypot(*unbounded_acceleration) > maximum)
             and _norm(rate) <= rate_limit+1e-10 and _norm(acceleration) <= maximum+1e-10,
             "reference_tracking", "Finite request-rate clipping or acceleration bounds were changed")
    _compare_vector(navigation.get("target_q_body_to_eci"), requested, "reference_tracking", tolerance=1e-10)
    pressure = sample.get("dynamic_pressure_pa")
    _require(_number(pressure) and pressure >= 0., "reference_tracking", "Missing current sampled dynamic pressure")
    feedback = checkpoint["phase"].startswith("recovery_landing_") or pressure > 100.
    if not feedback:
        _require(control is None and navigation.get("control_allocation") == "coast_stopping_distance_v1",
                 "reference_tracking", "Low-q coast keeps its existing stopping-distance law without feedforward")
        return item
    _tracking_control(control, navigation, state, sample, profile, requested, body_rate, body_acceleration,
                      maximum, rate_limit, frequency, damping)
    return item


def _tracking_control(item, navigation, state, sample, profile, requested, rate, acceleration,
                      maximum, rate_limit, frequency, damping):
    _require(type(item) is dict and item.get("schema") == "missionos.starship_reference_tracking_control.v1"
             and item.get("policy_id") == POLICY_ID and item.get("reference_tracking_is_execution") is False
             and all(item.get(key, False) is False for key in _FORBIDDEN_CLAIMS)
             and _same_value(item, sample.get("controller", {}).get("reference_tracking_control"))
             and navigation.get("control_allocation") == "bounded_linearized_flap_gimbal_plus_physical_jet_pairs"
             and _near(item.get("component_acceleration_limit_rad_s2"), maximum, 1e-12)
             and _near(item.get("rate_ceiling_rad_s"), rate_limit, 1e-12),
             "reference_tracking", "Missing scoped finite tracking control law")
    actual = state["omega_body_rad_s"]
    error = _multiply(_inverse(state["q_body_to_eci"]), requested)
    if error[0] < 0:
        error = [-x for x in error]
    transport = _cross(actual, rate)
    raw = [2*frequency**2*e-2*damping*frequency*(w-r)+a-c
           for e, w, r, a, c in zip(error[1:], actual, rate, acceleration, transport)]
    limited = [max(-maximum, min(maximum, x)) for x in raw]
    for key, value in (("reference_rate_body_rad_s", rate), ("reference_acceleration_body_rad_s", acceleration),
                       ("actual_body_rate_rad_s", actual), ("transport_term_body_rad_s2", transport),
                       ("raw_requested_acceleration_body_rad_s2", raw),
                       ("limited_requested_acceleration_body_rad_s2", limited)):
        _compare_vector(item.get(key), value, "reference_tracking", tolerance=1e-10)
    _require(item.get("acceleration_clipped") is any(a != b for a, b in zip(raw, limited)),
             "reference_tracking", "Tracking feedback acceleration clipping is not the unchanged component bound")
    inertia = _inertia(state, profile)
    measured_inertia = sample.get("inertia_kg_m2")
    _require(type(measured_inertia) is list and len(measured_inertia) == 3, "reference_tracking", "Missing measured current inertia")
    for recorded, expected in zip(measured_inertia, inertia):
        _compare_vector(recorded, expected, "reference_tracking", tolerance=.01)
    gyro = _cross(actual, _matvec(inertia, actual))
    wanted = [a+b for a, b in zip(_matvec(inertia, limited), gyro)]
    aero = sample.get("aero_torque_body_nm")
    _require(_vector(aero), "reference_tracking", "Missing measured aerodynamic moment")
    before_fins = [a-b for a, b in zip(wanted, aero)]
    _compare_vector(item.get("requested_rigid_body_torque_body_nm"), wanted, "reference_tracking", tolerance=.01)
    _compare_vector(item.get("actual_aero_torque_body_nm"), aero, "reference_tracking", tolerance=1e-4)
    _compare_vector(item.get("pre_fin_torque_body_nm"), before_fins, "reference_tracking", tolerance=.01)
    fin = navigation.get("development_fin_allocation")
    destination = fin.get("requested_increment_torque_body_nm") if type(fin) is dict else navigation.get("requested_torque_body_nm")
    _compare_vector(destination, before_fins, "reference_tracking", tolerance=.01)
