"""Causal bounded attitude requests and reference-rate tracking, opt-in only.

This is a request generator, not a flight-state integrator.  Sample-to-sample
quaternion increments have a bounded inertial rotation rate; changes of that
rate have a bounded finite-difference acceleration.  A moving goal may remain
unreached or be overshot while the requested rate brakes.  No physical state is
assigned and neither bounds nor feedforward imply available actuator authority.

The frame/transport convention follows the reference-rate inputs documented by
Basilisk mrpFeedback, rather than importing or executing that controller:
https://avslab.github.io/basilisk/Documentation/fswAlgorithms/attControl/mrpFeedback/mrpFeedback.html
"""
from __future__ import annotations

import math
from numbers import Real

from . import starship_sixdof as dyn

POLICY_ID = "bounded_reference_tracking_v1"
SCHEMA = "missionos.starship_reference_tracking.v1"
CONTROL_SCHEMA = "missionos.starship_reference_tracking_control.v1"
MAXIMUM_INTERVAL_S = 1.


def _vector(value, count, label):
    try:
        result = tuple(value)
    except TypeError as exc:
        raise ValueError(f"invalid_{label}") from exc
    if len(result) != count or any(not isinstance(x, Real) or isinstance(x, bool) or not math.isfinite(x)
                                   for x in result):
        raise ValueError(f"invalid_{label}")
    return tuple(float(x) for x in result)


def _quaternion(value, label):
    result = _vector(value, 4, label)
    if abs(math.hypot(*result)-1.) > 1e-6:
        raise ValueError(f"invalid_{label}")
    return dyn.normalize_quaternion(result)


def _limits(profile):
    try:
        guide = profile["guidance"]
        acceleration = guide["max_angular_acceleration_rad_s2"]
        frequency = guide["attitude_frequency_rad_s"]
        damping = guide["attitude_damping_ratio"]
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid_reference_profile") from exc
    if any(not isinstance(x, Real) or isinstance(x, bool) or not math.isfinite(x) or x <= 0
           for x in (acceleration, frequency, damping)):
        raise ValueError("invalid_reference_profile")
    return float(acceleration/frequency), float(acceleration), float(frequency), float(damping)


def _time(value):
    if not isinstance(value, Real) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("invalid_reference_time")
    return float(value)


def _inverse(quaternion):
    return (quaternion[0], *(-value for value in quaternion[1:]))


def _clip_norm(vector, maximum):
    length = math.hypot(*vector)
    scale = min(1., maximum/max(length, 1e-300))
    return tuple(value*scale for value in vector), length > maximum


def _rotation_vector(quaternion):
    # Inputs use the shortest quaternion hemisphere.  At exactly pi choose the
    # sign of the largest axis component deterministically, including q/-q.
    q = quaternion
    if ((abs(q[0]) > 1e-14 and q[0] < 0)
            or (abs(q[0]) <= 1e-14 and q[1+max(range(3), key=lambda i: abs(q[i+1]))] < 0)):
        q = tuple(-value for value in q)
    length = math.hypot(*q[1:])
    if length < 1e-14:
        return (0., 0., 0.)
    angle = 2*math.atan2(length, max(0., q[0]))
    return tuple(value*angle/length for value in q[1:])


def _increment(rotation):
    angle = math.hypot(*rotation)
    if angle < 1e-14:
        return (1., 0., 0., 0.)
    scale = math.sin(angle/2)/angle
    return (math.cos(angle/2), *(value*scale for value in rotation))


class ReferenceTracker:
    """Store requested-frame history; never store or overwrite vehicle state.

    Seed on the first controlled sample.  Calling ``update`` at that same clock
    emits the declared anchor and zero acceleration.  Subsequent samples must
    advance by at most one second; a larger gap requires a declared new tracker.
    """

    def __init__(self, profile, initial_requested_q, initial_time_s, *,
                 initial_reference_rate_eci_rad_s=(0., 0., 0.), initial_raw_goal_q=None):
        self.maximum_rate_rad_s, self.maximum_acceleration_rad_s2, _, _ = _limits(profile)
        self.quaternion = _quaternion(initial_requested_q, "initial_reference_quaternion")
        self.time_s = _time(initial_time_s)
        self.rate_eci_rad_s = _vector(initial_reference_rate_eci_rad_s, 3, "initial_reference_rate")
        if math.hypot(*self.rate_eci_rad_s) > self.maximum_rate_rad_s+1e-12:
            raise ValueError("initial_reference_rate_exceeds_bound")
        self.raw_goal_q = (self.quaternion if initial_raw_goal_q is None else
                           _quaternion(initial_raw_goal_q, "initial_raw_goal_quaternion"))
        self.raw_goal_time_s = self.time_s
        self.first_update = True

    def update(self, raw_goal_q, actual_q, actual_body_rate_rad_s, time_s):
        goal = _quaternion(raw_goal_q, "raw_goal_quaternion")
        actual = _quaternion(actual_q, "actual_quaternion")
        body_rate = _vector(actual_body_rate_rad_s, 3, "actual_body_rate")
        current_time = _time(time_s)
        previous_q, previous_time, previous_rate = self.quaternion, self.time_s, self.rate_eci_rad_s
        previous_goal, previous_goal_time = self.raw_goal_q, self.raw_goal_time_s
        interval = current_time-previous_time
        if interval < 0 or interval > MAXIMUM_INTERVAL_S or (interval == 0 and not self.first_update):
            raise ValueError("invalid_reference_interval")
        error = _rotation_vector(dyn.quaternion_multiply(goal, _inverse(previous_q)))
        goal_rotation = _rotation_vector(dyn.quaternion_multiply(goal, _inverse(previous_goal)))
        initialized = interval == 0
        if initialized:
            unbounded_rate = previous_rate
            rate_limited, bounded_rate = previous_rate, previous_rate
            unbounded_goal_rate = goal_rate = closing_rate = (0., 0., 0.)
            closing_bound = 0.
            propagated_request, closing_error = previous_q, error
            goal_rate_clipped = False
            unbounded_acceleration = acceleration = (0., 0., 0.)
            rate_clipped = acceleration_clipped = False
            requested = previous_q
        else:
            # Closing speed can stop within remaining angle: v*dt + v^2/(2a)
            # <= angle.  The raw-goal velocity is measured from past/current
            # goals only; a discontinuity is declared and bounded, not replayed.
            unbounded_goal_rate = tuple(value/interval for value in goal_rotation)
            goal_rate, goal_rate_clipped = _clip_norm(unbounded_goal_rate, self.maximum_rate_rad_s)
            # Remove the measured goal increment before computing closing
            # error; otherwise adding goal-rate feedforward double counts the
            # same sample's goal motion and leaves the request one sample ahead.
            propagated_request = dyn.normalize_quaternion(dyn.quaternion_multiply(
                _increment(tuple(value*interval for value in goal_rate)), previous_q))
            closing_error = _rotation_vector(dyn.quaternion_multiply(goal, _inverse(propagated_request)))
            angle = math.hypot(*closing_error)
            adt = self.maximum_acceleration_rad_s2*interval
            closing_bound = math.sqrt(adt**2+2*self.maximum_acceleration_rad_s2*angle)-adt
            closing_scale = min(1/interval, closing_bound/max(angle, 1e-300))
            closing_rate = tuple(value*closing_scale for value in closing_error)
            unbounded_rate = tuple(a+b for a, b in zip(goal_rate, closing_rate))
            rate_limited, rate_clipped = _clip_norm(unbounded_rate, self.maximum_rate_rad_s)
            unbounded_acceleration = tuple((a-b)/interval for a, b in zip(rate_limited, previous_rate))
            acceleration, acceleration_clipped = _clip_norm(unbounded_acceleration, self.maximum_acceleration_rad_s2)
            bounded_rate = tuple(a+interval*b for a, b in zip(previous_rate, acceleration))
            requested = dyn.normalize_quaternion(dyn.quaternion_multiply(
                _increment(tuple(value*interval for value in bounded_rate)), previous_q))
            if sum(a*b for a, b in zip(requested, previous_q)) < 0:
                requested = tuple(-value for value in requested)
        reference_rate_body = dyn.rotate(_inverse(actual), bounded_rate)
        reference_acceleration_body = dyn.rotate(_inverse(actual), acceleration)
        receipt = {"schema": SCHEMA, "policy_id": POLICY_ID, "time_s": current_time,
            "interval_s": interval, "maximum_interval_s": MAXIMUM_INTERVAL_S,
            "raw_goal_q_body_to_eci": list(goal), "requested_q_body_to_eci": list(requested),
            "previous_requested_q_body_to_eci": list(previous_q), "previous_time_s": previous_time,
            "previous_raw_goal_q_body_to_eci": list(previous_goal), "previous_raw_goal_time_s": previous_goal_time,
            "previous_reference_rate_eci_rad_s": list(previous_rate), "actual_q_body_to_eci": list(actual),
            "actual_body_rate_rad_s": list(body_rate), "goal_error_rotation_eci_rad": list(error),
            "raw_goal_rotation_eci_rad": list(goal_rotation),
            "unbounded_goal_rate_eci_rad_s": list(unbounded_goal_rate),
            "goal_rate_eci_rad_s": list(goal_rate), "goal_rate_clipped": goal_rate_clipped,
            "goal_rate_propagated_request_q_body_to_eci": list(propagated_request),
            "closing_error_rotation_eci_rad": list(closing_error),
            "closing_rate_bound_rad_s": closing_bound, "closing_reference_rate_eci_rad_s": list(closing_rate),
            "unbounded_reference_rate_eci_rad_s": list(unbounded_rate),
            "rate_limited_reference_rate_eci_rad_s": list(rate_limited),
            "reference_rate_eci_rad_s": list(bounded_rate),
            "unbounded_reference_acceleration_eci_rad_s2": list(unbounded_acceleration),
            "reference_acceleration_eci_rad_s2": list(acceleration),
            "reference_rate_body_rad_s": list(reference_rate_body),
            "reference_acceleration_body_rad_s2": list(reference_acceleration_body),
            "maximum_reference_rate_rad_s": self.maximum_rate_rad_s,
            "maximum_reference_acceleration_rad_s2": self.maximum_acceleration_rad_s2,
            "rate_clipped": rate_clipped, "acceleration_clipped": acceleration_clipped,
            "first_update": self.first_update, "initialized_at_current_time": initialized,
            "reference_kinematics": "constant-rate quaternion increments; inertial backward-difference acceleration",
            "closing_rate_law": "v*dt + v^2/(2*amax) <= shortest goal error; causal raw-goal-rate feedforward",
            "request_is_execution": False, "actual_state_assigned": False}
        # Validation/projection above completes before committing requested history.
        self.quaternion, self.time_s, self.rate_eci_rad_s = requested, current_time, bounded_rate
        self.raw_goal_q, self.raw_goal_time_s = goal, current_time
        self.first_update = False
        return requested, {"reference_rate_body_rad_s": reference_rate_body,
            "reference_acceleration_body_rad_s": reference_acceleration_body, "receipt": receipt}


def tracking_acceleration(error_vector, actual_rate, reference_rate, reference_acceleration, profile):
    """Return the existing-gain PD tracking acceleration and its clipped receipt.

    Reference acceleration is the INERTIAL derivative expressed in actual body
    axes.  Its body-coordinate derivative is alpha_ref - omega_body x omega_ref.
    Physical torque allocation, actuator limits and aerodynamic loads remain the
    caller's responsibility.
    """
    maximum_rate, maximum_acceleration, frequency, damping = _limits(profile)
    error = _vector(error_vector, 3, "tracking_error_vector")
    actual = _vector(actual_rate, 3, "actual_body_rate")
    reference = _vector(reference_rate, 3, "reference_body_rate")
    acceleration = _vector(reference_acceleration, 3, "reference_body_acceleration")
    if math.hypot(*reference) > maximum_rate+1e-10 or math.hypot(*acceleration) > maximum_acceleration+1e-10:
        raise ValueError("reference_tracking_input_exceeds_bound")
    transport = (actual[1]*reference[2]-actual[2]*reference[1],
                 actual[2]*reference[0]-actual[0]*reference[2],
                 actual[0]*reference[1]-actual[1]*reference[0])
    raw = tuple(2*frequency**2*e-2*damping*frequency*(w-r)+a-c
                for e, w, r, a, c in zip(error, actual, reference, acceleration, transport))
    limited = tuple(max(-maximum_acceleration, min(maximum_acceleration, value)) for value in raw)
    return limited, {"schema": CONTROL_SCHEMA, "policy_id": POLICY_ID,
        "reference_rate_body_rad_s": list(reference),
        "reference_acceleration_body_rad_s": list(acceleration), "actual_body_rate_rad_s": list(actual),
        "transport_term_body_rad_s2": list(transport),
        "raw_requested_acceleration_body_rad_s2": list(raw),
        "limited_requested_acceleration_body_rad_s2": list(limited),
        "component_acceleration_limit_rad_s2": maximum_acceleration, "rate_ceiling_rad_s": maximum_rate,
        "acceleration_clipped": any(a != b for a, b in zip(raw, limited)),
        "reference_tracking_is_execution": False}
