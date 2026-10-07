"""Finite, frozen-pose engine-wrench projection for development landing only.

The scalar throttle subproblem is restricted to each selected actuator's
unsaturated affine command interval actual +/- tau*rate. Nonlinear command
ranges are explicitly excluded; off engines retain exact clipped-lag decay.
Every selected mask solves a one-dimensional convex quadratic exactly. Existing
gimbal, jet and flap COMMANDS are preserved. Predicted actuator endpoints are
not assigned to the vehicle, and this local model does not certify stopping.
"""
from __future__ import annotations

from dataclasses import replace
import math
from numbers import Real

from . import starship_physics as env
from . import starship_sixdof as dyn
from .starship_fin_allocation import actuator_endpoint

SCHEMA = "missionos.starship_terminal_wrench_projection.v1"
POLICY_ID = "finite_affine_wrench_projection_v1"
MAXIMUM_MAIN_ENGINES = 13
MAXIMUM_MASKS = 20


def _vector(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(isinstance(x, bool) or not isinstance(x, Real) or not math.isfinite(x) or abs(x) >= 1e50 for x in value)):
        raise ValueError("invalid_terminal_wrench_vector")
    return tuple(float(x) for x in value)


def _direction(gx, gy, direction):
    # The SAME physical convention: Ry(gy) Rx(gx).
    x, y, z = direction
    cy, sy, cx, sx = math.cos(gy), math.sin(gy), math.cos(gx), math.sin(gx)
    return cy*x+sy*(sx*y+cx*z), cx*y-sx*z, -sy*x+cy*(sx*y+cx*z)


def _add_scaled(a, b, coefficient):
    return tuple(a[i]+coefficient*b[i] for i in range(3))


def _finite_geometry(state, vehicle, command, com, interval):
    data = []
    for index, (spec, actual, target) in enumerate(zip(vehicle.engines, state.engine_states, command.engines)):
        gx = actuator_endpoint(actual.gimbal_x_rad, target.gimbal_x_rad,
            spec.gimbal_time_constant_s, spec.gimbal_rate_rad_s, interval)
        gy = actuator_endpoint(actual.gimbal_y_rad, target.gimbal_y_rad,
            spec.gimbal_time_constant_s, spec.gimbal_rate_rad_s, interval)
        direction = _direction(gx, gy, spec.direction_body)
        force = env.scale(direction, spec.max_thrust_n if actual.available and state.propellant_kg > 0 else 0.)
        torque = env.cross(env.add(spec.position_body_m, env.scale(com, -1.)), force)
        throttle = actuator_endpoint(actual.throttle, target.throttle if target.enabled else 0.,
            spec.throttle_time_constant_s, spec.throttle_rate_per_s, interval)
        data.append({"index": index, "force_per_throttle_body_n": force, "torque_per_throttle_body_nm": torque,
            "base_endpoint_throttle": throttle, "endpoint_gimbal_x_rad": gx, "endpoint_gimbal_y_rad": gy})
    return data


def _masks(state, vehicle, command):
    count = min(MAXIMUM_MAIN_ENGINES, len(vehicle.engines))
    healthy = [i for i in range(count) if state.engine_states[i].available]
    candidates = [(), *[tuple(i for i in healthy if i < n) for n in range(1, count+1)]]
    for i in healthy:
        for j in healthy:
            if i >= j:
                continue
            a, b = vehicle.engines[i], vehicle.engines[j]
            if (a.max_gimbal_rad > 0 and b.max_gimbal_rad > 0 and a.max_thrust_n == b.max_thrust_n
                    and math.hypot(a.position_body_m[0]+b.position_body_m[0],
                                   a.position_body_m[1]+b.position_body_m[1]) <= 1e-8):
                candidates.append((i, j))
    candidates.append(tuple(i for i in healthy if command.engines[i].enabled))
    return list(dict.fromkeys(candidates))


def _halfspace(lower, upper, slope, intercept):
    """Intersect an interval with slope*lambda + intercept >= 0."""
    if slope > 0:
        lower = max(lower, -intercept/slope)
    elif slope < 0:
        upper = min(upper, -intercept/slope)
    elif intercept < 0:
        return lower, upper, False
    return lower, upper, lower <= upper


def allocate_terminal_wrench(state, vehicle, base_command, observed, desired_force_eci_n,
                            requested_engine_torque_body_nm, *, interval_s, up_eci,
                            minimum_vertical_thrust_n):
    """Project finite endpoint force without degrading base moment headroom.

    The vertical constraint preserves an explicitly supplied same-state force
    component. It is NOT a free-flight stopping or joint trajectory certificate.
    Failure returns the original command and a visible deferred receipt.
    """
    desired, demand, up = map(_vector, (desired_force_eci_n, requested_engine_torque_body_nm, up_eci))
    if (isinstance(interval_s, bool) or not isinstance(interval_s, Real) or not math.isfinite(interval_s)
            or not 0 < interval_s <= .25 or isinstance(minimum_vertical_thrust_n, bool)
            or not isinstance(minimum_vertical_thrust_n, Real) or not math.isfinite(minimum_vertical_thrust_n)
            or minimum_vertical_thrust_n < 0 or abs(env.norm(up)-1.) > 1e-8):
        raise ValueError("invalid_terminal_wrench_constraints")
    dyn._validate_pair(state, vehicle, base_command)
    com = _vector(observed["com_body_m"])
    count = min(MAXIMUM_MAIN_ENGINES, len(vehicle.engines))
    receipt = {"schema": SCHEMA, "policy_id": POLICY_ID, "interval_s": float(interval_s),
        "scope": "frozen_actual_pose_com_and_commanded_gimbal_jet_flap_endpoints",
        "desired_force_eci_n": list(desired), "requested_engine_torque_body_nm": list(demand),
        "up_eci": list(up), "minimum_vertical_thrust_n": float(minimum_vertical_thrust_n),
        "vertical_constraint": "preserve_supplied_instantaneous_vertical_thrust_component",
        "physical_state_assigned": False, "prediction_is_execution": False,
        "global_allocation_optimality_established": False, "joint_trajectory_feasibility_established": False,
        "arrival_admitted": False, "support_admitted": False, "physical_execution": False,
        "mask_candidates": [], "selected_mask_index": None}
    if any(not spec.name.startswith("rcs_") and i >= count and command.enabled
           for i, (spec, command) in enumerate(zip(vehicle.engines, base_command.engines))):
        return base_command, {**receipt, "status": "deferred", "reason": "outside_landing_main_mask_scope"}
    geometry = _finite_geometry(state, vehicle, base_command, com, interval_s)
    masks = _masks(state, vehicle, base_command)
    if len(masks) > MAXIMUM_MASKS:
        return base_command, {**receipt, "status": "deferred", "reason": "unsupported_main_mask_geometry"}
    base_force, base_torque = dyn.ZERO, dyn.ZERO
    for item in geometry:
        base_force = _add_scaled(base_force, item["force_per_throttle_body_n"], item["base_endpoint_throttle"])
        base_torque = _add_scaled(base_torque, item["torque_per_throttle_body_nm"], item["base_endpoint_throttle"])
    headroom = [abs(base_torque[i]-demand[i]) for i in range(3)]
    roundoff = [64*math.ulp(max(1., abs(base_torque[i]), abs(demand[i]))) for i in range(3)]
    receipt.update(base_predicted_force_eci_n=list(dyn.rotate(state.q_body_to_eci, base_force)),
        base_predicted_engine_torque_body_nm=list(base_torque), base_moment_error_abs_nm=headroom,
        moment_roundoff_bound_nm=roundoff, actual_q_body_to_eci=list(state.q_body_to_eci),
        frozen_com_body_m=list(com), finite_endpoint_geometry=geometry)
    best = None
    for mask_index, mask in enumerate(masks):
        force_a, force_b, torque_a, torque_b = dyn.ZERO, dyn.ZERO, dyn.ZERO, dyn.ZERO
        for i, (spec, actual, item) in enumerate(zip(vehicle.engines, state.engine_states, geometry)):
            if i < count:
                if i in mask:
                    decay = math.exp(-interval_s/spec.throttle_time_constant_s)
                    a, b = actual.throttle*decay, 1.-decay
                else:
                    a, b = actuator_endpoint(actual.throttle, 0., spec.throttle_time_constant_s,
                        spec.throttle_rate_per_s, interval_s), 0.
            else:
                a, b = item["base_endpoint_throttle"], 0.
            force_a = _add_scaled(force_a, item["force_per_throttle_body_n"], a)
            force_b = _add_scaled(force_b, item["force_per_throttle_body_n"], b)
            torque_a = _add_scaled(torque_a, item["torque_per_throttle_body_nm"], a)
            torque_b = _add_scaled(torque_b, item["torque_per_throttle_body_nm"], b)
        a_world, b_world = dyn.rotate(state.q_body_to_eci, force_a), dyn.rotate(state.q_body_to_eci, force_b)
        lower, upper = (max(vehicle.engines[i].min_throttle for i in mask), 1.) if mask else (0., 0.)
        affine_intervals = []
        for i in mask:
            actual, spec = state.engine_states[i], vehicle.engines[i]
            radius = spec.throttle_time_constant_s*spec.throttle_rate_per_s
            lo, hi = max(0., actual.throttle-radius), min(1., actual.throttle+radius)
            affine_intervals.append({"engine_index": i, "command_interval": [lo, hi],
                "rate_saturated_command_ranges_excluded": True})
            lower, upper = max(lower, lo), min(upper, hi)
        constraints = [{"kind": "vertical", "slope": env.dot(b_world, up),
                        "intercept": env.dot(a_world, up)-minimum_vertical_thrust_n}]
        for axis in range(3):
            error = torque_a[axis]-demand[axis]
            allowed = headroom[axis]+roundoff[axis]
            constraints.extend(({"kind": "moment_upper", "axis": axis, "slope": -torque_b[axis], "intercept": allowed-error},
                                {"kind": "moment_lower", "axis": axis, "slope": torque_b[axis], "intercept": allowed+error}))
        feasible = lower <= upper
        for constraint in constraints:
            lower, upper, available = _halfspace(lower, upper, constraint["slope"], constraint["intercept"])
            feasible = feasible and available
        entry = {"mask": list(mask), "force_a_eci_n": list(a_world), "force_b_eci_n": list(b_world),
            "moment_a_body_nm": list(torque_a), "moment_b_body_nm": list(torque_b),
            "selected_engine_affine_intervals": affine_intervals,
            "constraints": constraints, "feasible": feasible, "feasible_interval": [lower, upper]}
        if feasible:
            curvature = env.dot(b_world, b_world)
            free = env.dot(b_world, env.add(desired, env.scale(a_world, -1.)))/curvature if curvature else lower
            throttle = min(upper, max(lower, free))
            predicted = _add_scaled(a_world, b_world, throttle)
            residual = env.add(predicted, env.scale(desired, -1.))
            objective = env.dot(residual, residual)
            entry.update(throttle=throttle, unconstrained_throttle=free, objective=objective,
                predicted_force_eci_n=list(predicted), force_residual_eci_n=list(residual),
                predicted_engine_torque_body_nm=list(_add_scaled(torque_a, torque_b, throttle)),
                half_objective_gradient=env.dot(b_world, residual))
            if best is None or objective < best[0]:
                best = objective, mask_index, mask, throttle
        receipt["mask_candidates"].append(entry)
    if best is None:
        return base_command, {**receipt, "status": "deferred", "reason": "no_finite_projection_preserves_vertical_and_moment_headroom"}
    _, selected_index, selected, throttle = best
    commands = list(base_command.engines)
    for i in range(count):
        commands[i] = replace(commands[i], enabled=i in selected, throttle=throttle if i in selected else 0.)
    command = dyn.Command6DOF(tuple(commands), base_command.flap_angles_rad)
    dyn._validate_pair(state, vehicle, command)
    return command, {**receipt, "status": "accepted", "reason": None, "selected_mask_index": selected_index,
                     "main_gimbal_commands_preserved": True, "jet_commands_preserved": True, "flap_commands_preserved": True}
