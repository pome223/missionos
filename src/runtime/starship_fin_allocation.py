"""Development-only regularized fin allocation over finite actuator response.

The optimizer requests bounded commands, never assigns an actual fin state or
injects a moment. Effectiveness is local to the unchanged surrogate panel law.
The one-step response is a prediction with frozen attitude/flow, not execution.
"""
from __future__ import annotations

from dataclasses import replace
from itertools import product
import math

import numpy as np

from . import starship_sixdof as dyn

POLICY_ID = "finite_regularized_fins_v1"
MOMENT_PRIORITY_POLICY_ID = "finite_moment_priority_fins_v1"
POLICIES = (POLICY_ID, MOMENT_PRIORITY_POLICY_ID)
REGULARIZATION = .05  # Fixed design choice, not identified from flight data.


def actuator_endpoint(actual, target, tau, rate, interval):
    """Exact scalar endpoint for clipped first-order lag, constant command."""
    delta = target-actual
    magnitude = abs(delta)
    saturation_time = max(0., (magnitude-tau*rate)/rate)
    if interval <= saturation_time:
        move = rate*interval
    else:
        move = magnitude-min(magnitude, tau*rate)*math.exp(-(interval-saturation_time)/tau)
    return actual+math.copysign(move, delta)


def _command_for_endpoint(actual, endpoint, panel, interval):
    # Monotonic inverse, including the flat rate-saturated portions. These are
    # command angles; only predicted actual motion is constrained by rate*dt.
    lower, upper = -panel.max_deflection_rad, panel.max_deflection_rad
    for _ in range(48):
        middle = (lower+upper)/2
        prediction = actuator_endpoint(actual, middle, panel.deflection_time_constant_s,
                                       panel.deflection_rate_rad_s, interval)
        if prediction < endpoint:
            lower = middle
        else:
            upper = middle
    return (lower+upper)/2


def bounded_least_squares(matrix, demand, lower, upper):
    """Enumerate faces of a small box; solve each free-face least squares.

    With positive regularization this gives the convex quadratic minimum, not
    component-wise clipping of an unconstrained solution. At most four fins.
    """
    matrix, demand = np.asarray(matrix, dtype=float), np.asarray(demand, dtype=float)
    lower, upper = np.asarray(lower, dtype=float), np.asarray(upper, dtype=float)
    count = len(lower)
    if (matrix.ndim != 2 or matrix.shape != (len(demand), count) or not 1 <= count <= 4
            or upper.shape != lower.shape or np.any(lower > upper)
            or not all(np.all(np.isfinite(x)) for x in (matrix, demand, lower, upper))):
        raise ValueError("invalid_small_box_problem")
    candidate = np.linalg.lstsq(matrix, demand, rcond=1e-12)[0]
    if np.all(candidate >= lower) and np.all(candidate <= upper):
        return candidate
    best, best_cost = None, math.inf
    for face in product((-1, 0, 1), repeat=count):
        free = [i for i, side in enumerate(face) if side == 0]
        trial = np.array([lower[i] if side == -1 else upper[i] if side == 1 else 0.
                          for i, side in enumerate(face)])
        if free:
            trial[free] = np.linalg.lstsq(matrix[:, free], demand-matrix@trial, rcond=1e-12)[0]
        if np.any(trial < lower-1e-12) or np.any(trial > upper+1e-12):
            continue
        trial = np.clip(trial, lower, upper)
        residual = matrix@trial-demand
        cost = float(residual@residual)
        if cost < best_cost:
            best, best_cost = trial, cost
    if best is None:
        raise ValueError("box_solver_has_no_finite_solution")
    return best


def closest_trim_with_primary_image(matrix, image, trim_delta, limits, lower, upper):
    """Minimize normalized trim distance while preserving the primary image.

    Every face of the finite endpoint box is considered (at most 81 faces).
    On a face, the equality-constrained weighted projection has a closed-form
    minimum-norm solution. This changes no attainable primary moment vector.
    """
    matrix, image = np.asarray(matrix, dtype=float), np.asarray(image, dtype=float)
    trim, limits = np.asarray(trim_delta, dtype=float), np.asarray(limits, dtype=float)
    lower, upper = np.asarray(lower, dtype=float), np.asarray(upper, dtype=float)
    count = len(lower)
    if (matrix.ndim != 2 or matrix.shape != (len(image), count) or not 1 <= count <= 4
            or any(value.shape != lower.shape for value in (upper, limits, trim))
            or np.any(lower > upper) or np.any(limits <= 0)
            or not all(np.all(np.isfinite(x)) for x in (matrix, image, trim, limits, lower, upper))):
        raise ValueError("invalid_primary_image_projection")
    def project(free, trial):
        if free:
            source = matrix[:, free]
            residual = image-matrix@trial-source@trim[free]
            correction = np.linalg.lstsq(source*limits[free], residual, rcond=1e-12)[0]
            trial[free] = trim[free]+limits[free]*correction
        if (np.any(trial < lower-1e-10) or np.any(trial > upper+1e-10)
                or np.linalg.norm(matrix@trial-image) > 1e-10*(1+np.linalg.norm(image))):
            return None
        return np.clip(trial, lower, upper)
    unconstrained = project(list(range(count)), np.zeros(count))
    if unconstrained is not None:
        return unconstrained
    best, best_cost = None, math.inf
    for face in product((-1, 0, 1), repeat=count):
        free = [i for i, side in enumerate(face) if side == 0]
        trial = np.asarray([lower[i] if side == -1 else upper[i] if side == 1 else 0.
                            for i, side in enumerate(face)])
        trial = project(free, trial)
        if trial is None:
            continue
        cost = float(np.sum(((trial-trim)/limits)**2))
        if cost < best_cost:
            best, best_cost = trial, cost
    if best is None:
        raise ValueError("primary_image_has_no_box_projection")
    return best


def allocate_fins(state, vehicle, demand, observed, profile, *, interval_s, trim_angles_rad=None,
                  policy_id=POLICY_ID):
    if type(interval_s) not in (int, float) or not math.isfinite(interval_s) or not 0 < interval_s <= .25:
        raise ValueError("invalid_fin_control_interval")
    if type(policy_id) is not str or policy_id not in POLICIES:
        raise ValueError("invalid_fin_allocation_policy")
    indices = [i for i, p in enumerate(vehicle.aero_panels)
               if p.name.startswith("grid_fin_") and p.max_deflection_rad > 0]
    if not 1 <= len(indices) <= 4:
        raise ValueError("development_allocator_requires_booster_fins")
    actual = np.asarray([state.flap_angles_rad[i] for i in indices])
    limits = np.asarray([vehicle.aero_panels[i].max_deflection_rad for i in indices])
    trim = actual.copy()
    trim_source = "current_angle_no_static_trim_available"
    if trim_angles_rad is not None:
        if (len(trim_angles_rad) != len(vehicle.aero_panels)
                or any(type(x) not in (int, float) or not math.isfinite(x) for x in trim_angles_rad)
                or any(abs(trim_angles_rad[i]) > limits[j]+1e-10 for j, i in enumerate(indices))):
            raise ValueError("invalid_fin_trim_reference")
        trim = np.asarray([trim_angles_rad[i] for i in indices])
        trim_source = "entry_target_static_trim_not_actual_deflection"
    base_moment = np.asarray(observed["aero_torque_body_nm"])
    columns, lower, upper = [], [], []
    for i in indices:
        panel = vehicle.aero_panels[i]
        angle = state.flap_angles_rad[i]
        # Central or one-sided difference stays within physical angle limits.
        lo, hi = max(-panel.max_deflection_rad, angle-.001), min(panel.max_deflection_rad, angle+.001)
        moments = []
        for value in (lo, hi):
            angles = list(state.flap_angles_rad)
            angles[i] = value
            moments.append(np.asarray(dyn.observe(replace(state, flap_angles_rad=tuple(angles)), vehicle)["aero_torque_body_nm"]))
        columns.append((moments[1]-moments[0])/(hi-lo))
        lower.append(actuator_endpoint(angle, -panel.max_deflection_rad, panel.deflection_time_constant_s,
                                       panel.deflection_rate_rad_s, interval_s)-angle)
        upper.append(actuator_endpoint(angle, panel.max_deflection_rad, panel.deflection_time_constant_s,
                                       panel.deflection_rate_rad_s, interval_s)-angle)
    effectiveness = np.asarray(columns).T
    # The aerodynamic columns carry current dynamic pressure. Normalize each
    # moment axis by full-angle fin authority with a physical jet-authority
    # floor; tiny low-q columns never cause an unconstrained inverse command.
    jet_capacity = 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"]
    scale = np.maximum(jet_capacity, np.sum(np.abs(effectiveness)*limits, axis=1))
    priority_receipt = None
    if policy_id == POLICY_ID:
        matrix = np.vstack((effectiveness/scale[:, None], REGULARIZATION*np.diag(1/limits)))
        rhs = np.concatenate((np.asarray(demand)/scale, REGULARIZATION*(trim-actual)/limits))
        delta = bounded_least_squares(matrix, rhs, lower, upper)
    else:
        matrix, rhs = effectiveness/scale[:, None], np.asarray(demand)/scale
        primary = bounded_least_squares(matrix, rhs, lower, upper)
        image = matrix@primary
        delta = closest_trim_with_primary_image(matrix, image, trim-actual, limits, lower, upper)
        priority_receipt = {"allocation_priority": "moment_error_then_trim_with_fixed_primary_image",
            "primary_optimum_delta_rad": primary.tolist(),
            "primary_optimum_objective": float(np.linalg.norm(matrix@primary-rhs)**2),
            "primary_objective": float(np.linalg.norm(matrix@delta-rhs)**2),
            "primary_half_objective_gradient": (matrix.T@(matrix@delta-rhs)).tolist(),
            "secondary_trim_objective": float(np.sum(((delta-(trim-actual))/limits)**2)),
            "secondary_image_residual": (matrix@delta-image).tolist(),
            "secondary_projection": "weighted_equality_projection_over_box_faces",
            "maximum_secondary_faces": 3**len(indices)}
    endpoints = actual+delta
    targets, predicted_angles = list(state.flap_angles_rad), list(state.flap_angles_rad)
    for j, i in enumerate(indices):
        targets[i] = _command_for_endpoint(actual[j], endpoints[j], vehicle.aero_panels[i], interval_s)
        predicted_angles[i] = float(endpoints[j])
    # Compute a finite endpoint residual in the SAME panel law with frozen
    # attitude/flow. Dynamics still recomputes the actual loads at each RK stage.
    endpoint_moment = np.asarray(dyn.observe(replace(state, flap_angles_rad=tuple(predicted_angles)), vehicle)["aero_torque_body_nm"])
    predicted_increment = endpoint_moment-base_moment
    remaining = np.asarray(demand)-predicted_increment
    gradient = matrix.T@(matrix@delta-rhs)
    diagnostic = {"schema": "missionos.starship_fin_allocation.v1" if policy_id == POLICY_ID else "missionos.starship_fin_allocation.v2",
        "policy_id": policy_id,
        "interval_s": interval_s, "regularization": REGULARIZATION, "fin_indices": indices,
        "actual_angles_rad": actual.tolist(), "command_angles_rad": [targets[i] for i in indices],
        "predicted_endpoint_angles_rad": endpoints.tolist(), "trim_angles_rad": trim.tolist(),
        "trim_reference_source": trim_source, "reachable_delta_lower_rad": lower, "reachable_delta_upper_rad": upper,
        "effectiveness_nm_per_rad": effectiveness.tolist(), "moment_scale_nm": scale.tolist(),
        "requested_increment_torque_body_nm": list(demand), "linear_predicted_increment_torque_body_nm": (effectiveness@delta).tolist(),
        "nonlinear_predicted_increment_torque_body_nm": predicted_increment.tolist(),
        "predicted_residual_torque_body_nm": remaining.tolist(), "objective": float(np.linalg.norm(matrix@delta-rhs)**2),
        "half_objective_gradient": gradient.tolist(), "prediction_is_execution": False,
        "actual_state_assigned": False, "production_policy_admitted": False,
        **(priority_receipt or {})}
    return tuple(targets), remaining, diagnostic
