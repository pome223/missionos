"""Independent frozen-pose finite engine-wrench certificate checks.

The enumerated affine throttle domains exclude nonlinear response regions.
Consistency/optimality in these domains never proves global allocation,
stopping, trajectory feasibility, arrival, hardware validity or execution.
No allocation producer, vehicle factory or dynamics integrator is imported.
"""

from __future__ import annotations

import math

from .starship_booster_recovery_verifier import (
    _Invalid as _RecoveryInvalid,
    _command,
    _cross,
    _json,
    _state,
)

SCHEMA = "missionos.starship_terminal_wrench_projection.v1"
POLICY_ID = "finite_affine_wrench_projection_v1"
_COMMON = {
    "schema",
    "policy_id",
    "interval_s",
    "scope",
    "desired_force_eci_n",
    "requested_engine_torque_body_nm",
    "up_eci",
    "minimum_vertical_thrust_n",
    "vertical_constraint",
    "physical_state_assigned",
    "prediction_is_execution",
    "global_allocation_optimality_established",
    "joint_trajectory_feasibility_established",
    "arrival_admitted",
    "support_admitted",
    "physical_execution",
    "mask_candidates",
    "selected_mask_index",
    "status",
    "reason",
}
_GEOMETRY = {
    "base_predicted_force_eci_n",
    "base_predicted_engine_torque_body_nm",
    "base_moment_error_abs_nm",
    "moment_roundoff_bound_nm",
    "actual_q_body_to_eci",
    "frozen_com_body_m",
    "finite_endpoint_geometry",
}


class _Invalid(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise _Invalid(message)


def _number(x):
    return type(x) in (int, float) and math.isfinite(x) and abs(x) < 1e40


def _vector(value, size=3):
    return type(value) is list and len(value) == size and all(_number(x) for x in value)


def _near(a, b, *, absolute=1e-7, relative=2e-13):
    return _number(a) and _number(b) and abs(a - b) <= absolute + relative * max(abs(a), abs(b))


def _compare(a, b, *, absolute=1e-7):
    _require(
        _vector(a, len(b)) and all(_near(x, y, absolute=absolute) for x, y in zip(a, b)),
        "independent_wrench_arithmetic",
    )


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _add(a, b, c=1.0):
    return [x + c * y for x, y in zip(a, b)]


def _rotate(q, v):
    length = math.sqrt(_dot(q, q))
    q = [x / length for x in q]
    # The active quaternion formula is evaluated as nested cross products.
    return _add(v, _cross(q[1:], _add(_cross(q[1:], v), v, q[0])), 2.0)


def _endpoint(actual, target, tau, rate, interval):
    distance = abs(target - actual)
    switch = max(0.0, (distance - tau * rate) / rate)
    movement = (
        rate * interval
        if interval <= switch
        else distance - min(distance, tau * rate) * math.exp(-(interval - switch) / tau)
    )
    return actual + math.copysign(movement, target - actual)


def _specs(profile):
    p, a = profile["booster"], profile["actuators"]
    _require(
        p["engine_count"] == 33
        and p["gimbal_engine_count"] == 13
        and len(p["engine_positions_body_m"]) == 33,
        "unsupported_engine_factory",
    )
    result = []
    for i, pos in enumerate(p["engine_positions_body_m"]):
        _require(_vector(pos), "invalid_original_engine_geometry")
        result.append(
            {
                "position": pos,
                "direction": [0.0, 0.0, 1.0],
                "thrust": p["engine_thrust_n"],
                "tau": a["throttle_tau_s"],
                "rate": a["throttle_rate_s"],
                "gtau": a["gimbal_tau_s"],
                "grate": math.radians(a["gimbal_rate_deg_s"]),
                "gimbal": i < 13,
            }
        )
    for axis in range(3):
        lever_axis, force_axis = (axis + 1) % 3, (axis + 2) % 3
        for sign in (-1, 1):
            for side in (-1, 1):
                pos = [0.0, 0.0, p["dry_com_z_m"]]
                direction = [0.0, 0.0, 0.0]
                pos[lever_axis] += side * a["rcs_radius_m"]
                direction[force_axis] = side * sign
                result.append(
                    {
                        "position": pos,
                        "direction": direction,
                        "thrust": a["rcs_thrust_n"],
                        "tau": a["throttle_tau_s"],
                        "rate": 2.0,
                        "gtau": a["gimbal_tau_s"],
                        "grate": math.radians(15.0),
                        "gimbal": False,
                    }
                )
    _require(
        all(
            _number(spec[key]) and spec[key] > 0
            for spec in result
            for key in ("thrust", "tau", "rate", "gtau", "grate")
        ),
        "invalid_physical_actuator_parameters",
    )
    return result


def _finite_geometry(state, base, profile, interval, specs):
    p = profile["booster"]
    fuel = state["propellant_kg"]
    mass = p["dry_mass_kg"] + fuel
    com = [0.0, 0.0, p["dry_com_z_m"] + (p["tank_center_z_m"] - p["dry_com_z_m"]) * (fuel / mass)]
    geometry = []
    for index, (spec, actual, target) in enumerate(
        zip(specs, state["engine_states"], base["engines"])
    ):
        gx = _endpoint(
            actual["gimbal_x_rad"], target["gimbal_x_rad"], spec["gtau"], spec["grate"], interval
        )
        gy = _endpoint(
            actual["gimbal_y_rad"], target["gimbal_y_rad"], spec["gtau"], spec["grate"], interval
        )
        x, y, z = spec["direction"]
        cx, sx, cy, sy = math.cos(gx), math.sin(gx), math.cos(gy), math.sin(gy)
        direction = [
            cy * x + sy * (sx * y + cx * z),
            cx * y - sx * z,
            -sy * x + cy * (sx * y + cx * z),
        ]
        force = [
            x * (spec["thrust"] if actual["available"] and fuel > 0 else 0.0) for x in direction
        ]
        geometry.append(
            {
                "index": index,
                "force_per_throttle_body_n": force,
                "torque_per_throttle_body_nm": _cross(
                    [x - y for x, y in zip(spec["position"], com)], force
                ),
                "base_endpoint_throttle": _endpoint(
                    actual["throttle"],
                    target["throttle"] if target["enabled"] else 0.0,
                    spec["tau"],
                    spec["rate"],
                    interval,
                ),
                "endpoint_gimbal_x_rad": gx,
                "endpoint_gimbal_y_rad": gy,
            }
        )
    return com, geometry


def _masks(state, base, specs):
    healthy = [i for i in range(13) if state["engine_states"][i]["available"]]
    masks = [(), *[tuple(i for i in healthy if i < n) for n in range(1, 14)]]
    for i in healthy:
        for j in healthy:
            a, b = specs[i], specs[j]
            if (
                i < j
                and a["gimbal"]
                and b["gimbal"]
                and a["thrust"] == b["thrust"]
                and math.hypot(
                    a["position"][0] + b["position"][0], a["position"][1] + b["position"][1]
                )
                <= 1e-8
            ):
                masks.append((i, j))
    masks.append(tuple(i for i in healthy if base["engines"][i]["enabled"]))
    return list(dict.fromkeys(masks))


def _check(receipt, state, base, command, profile, expected):
    for value in (receipt, state, base, command, profile):
        _json(value)
    _state(state, profile)
    # The explicit new mask family includes OFF. This is a command-domain
    # check only; it does not widen any old recovery/capture phase contract.
    for value in (base, command):
        count = sum(engine.get("enabled", False) for engine in value["engines"][:33])
        _command(
            value, "recovery_entry_coast" if count == 0 else "recovery_landing_13", state, profile
        )
    _require(
        type(receipt) is dict
        and receipt.get("schema") == SCHEMA
        and receipt.get("policy_id") == POLICY_ID
        and receipt.get("scope") == "frozen_actual_pose_com_and_commanded_gimbal_jet_flap_endpoints"
        and receipt.get("vertical_constraint")
        == "preserve_supplied_instantaneous_vertical_thrust_component"
        and all(
            receipt.get(k) is False
            for k in (
                "physical_state_assigned",
                "prediction_is_execution",
                "global_allocation_optimality_established",
                "joint_trajectory_feasibility_established",
                "arrival_admitted",
                "support_admitted",
                "physical_execution",
            )
        ),
        "wrench_scope_or_claim_boundary",
    )
    dt = receipt.get("interval_s")
    desired = receipt.get("desired_force_eci_n")
    demand = receipt.get("requested_engine_torque_body_nm")
    up = receipt.get("up_eci")
    floor = receipt.get("minimum_vertical_thrust_n")
    _require(
        _number(dt)
        and 0 < dt <= 0.25
        and _vector(desired)
        and _vector(demand)
        and _vector(up)
        and abs(_dot(up, up) - 1.0) <= 1e-8
        and _number(floor)
        and floor >= 0,
        "invalid_wrench_input",
    )
    for key, value in expected.items():
        if value is not None:
            _compare(receipt[key], value) if type(value) is list else _require(
                _near(receipt[key], value), "wrench_expected_input_binding"
            )
    specs = _specs(profile)
    masks = _masks(state, base, specs)
    early_reason = (
        "outside_landing_main_mask_scope"
        if any(e["enabled"] for e in base["engines"][13:33])
        else "unsupported_main_mask_geometry"
        if len(masks) > 20
        else None
    )
    if early_reason:
        _require(
            set(receipt) == _COMMON
            and receipt["status"] == "deferred"
            and receipt["reason"] == early_reason
            and receipt["mask_candidates"] == []
            and receipt["selected_mask_index"] is None
            and command == base,
            "invalid_scope_defer",
        )
        return False
    com, geometry = _finite_geometry(state, base, profile, dt, specs)
    _require(
        set(receipt)
        == _COMMON
        | _GEOMETRY
        | (
            {"main_gimbal_commands_preserved", "jet_commands_preserved", "flap_commands_preserved"}
            if receipt["status"] == "accepted"
            else set()
        ),
        "wrench_closed_shape",
    )
    _compare(receipt["actual_q_body_to_eci"], state["q_body_to_eci"])
    _compare(receipt["frozen_com_body_m"], com)
    rows = receipt["finite_endpoint_geometry"]
    _require(type(rows) is list and len(rows) == 45, "finite_engine_geometry_count")
    for actual, reference in zip(rows, geometry):
        _require(
            type(actual) is dict
            and set(actual) == set(reference)
            and type(actual["index"]) is int
            and actual["index"] == reference["index"],
            "finite_engine_geometry_identity",
        )
        for key in ("force_per_throttle_body_n", "torque_per_throttle_body_nm"):
            _compare(actual[key], reference[key])
        for key in ("base_endpoint_throttle", "endpoint_gimbal_x_rad", "endpoint_gimbal_y_rad"):
            _require(_near(actual[key], reference[key], absolute=1e-12), "finite_actuator_endpoint")
    baseforce, basemoment = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
    for row in rows:
        baseforce = _add(baseforce, row["force_per_throttle_body_n"], row["base_endpoint_throttle"])
        basemoment = _add(
            basemoment, row["torque_per_throttle_body_nm"], row["base_endpoint_throttle"]
        )
    _compare(receipt["base_predicted_force_eci_n"], _rotate(state["q_body_to_eci"], baseforce))
    _compare(receipt["base_predicted_engine_torque_body_nm"], basemoment)
    headroom = [abs(x - y) for x, y in zip(basemoment, demand)]
    roundoff = [64 * math.ulp(max(1.0, abs(x), abs(y))) for x, y in zip(basemoment, demand)]
    _compare(receipt["base_moment_error_abs_nm"], headroom)
    _compare(receipt["moment_roundoff_bound_nm"], roundoff, absolute=1e-15)
    candidates = receipt["mask_candidates"]
    _require(
        type(candidates) is list and len(candidates) == len(masks) <= 20,
        "bounded_complete_mask_set",
    )
    best = None
    for index, (entry, mask) in enumerate(zip(candidates, masks)):
        keys = {
            "mask",
            "force_a_eci_n",
            "force_b_eci_n",
            "moment_a_body_nm",
            "moment_b_body_nm",
            "selected_engine_affine_intervals",
            "constraints",
            "feasible",
            "feasible_interval",
        }
        if entry.get("feasible") is True:
            keys |= {
                "throttle",
                "unconstrained_throttle",
                "objective",
                "predicted_force_eci_n",
                "force_residual_eci_n",
                "predicted_engine_torque_body_nm",
                "half_objective_gradient",
            }
        _require(
            type(entry) is dict
            and set(entry) == keys
            and entry["mask"] == list(mask)
            and type(entry["feasible"]) is bool,
            "mask_shape_or_order",
        )
        fa, fb, ma, mb = ([0.0, 0.0, 0.0] for _ in range(4))
        for i, (row, spec, actual) in enumerate(zip(rows, specs, state["engine_states"])):
            if i < 13:
                decay = math.exp(-dt / spec["tau"])
                a, b = (
                    (actual["throttle"] * decay, 1.0 - decay)
                    if i in mask
                    else (_endpoint(actual["throttle"], 0.0, spec["tau"], spec["rate"], dt), 0.0)
                )
            else:
                a, b = row["base_endpoint_throttle"], 0.0
            fa = _add(fa, row["force_per_throttle_body_n"], a)
            fb = _add(fb, row["force_per_throttle_body_n"], b)
            ma = _add(ma, row["torque_per_throttle_body_nm"], a)
            mb = _add(mb, row["torque_per_throttle_body_nm"], b)
        _compare(entry["force_a_eci_n"], _rotate(state["q_body_to_eci"], fa))
        _compare(entry["force_b_eci_n"], _rotate(state["q_body_to_eci"], fb))
        _compare(entry["moment_a_body_nm"], ma)
        _compare(entry["moment_b_body_nm"], mb)
        intervals = []
        lower, upper = (0.4, 1.0) if mask else (0.0, 0.0)
        for i in mask:
            radius = specs[i]["tau"] * specs[i]["rate"]
            value = state["engine_states"][i]["throttle"]
            lo, hi = max(0.0, value - radius), min(1.0, value + radius)
            intervals.append(
                {
                    "engine_index": i,
                    "command_interval": [lo, hi],
                    "rate_saturated_command_ranges_excluded": True,
                }
            )
            lower, upper = max(lower, lo), min(upper, hi)
        _require(
            entry["selected_engine_affine_intervals"] == intervals,
            "nonaffine_command_range_or_limits",
        )
        # The certified world vectors have already been independently checked.
        # Intersect their recorded scalar halfspaces without re-rounding them.
        aw, bw = entry["force_a_eci_n"], entry["force_b_eci_n"]
        constraints = [
            {"kind": "vertical", "slope": _dot(bw, up), "intercept": _dot(aw, up) - floor}
        ]
        for axis in range(3):
            error = ma[axis] - demand[axis]
            allowed = headroom[axis] + roundoff[axis]
            constraints.extend(
                (
                    {
                        "kind": "moment_upper",
                        "axis": axis,
                        "slope": -mb[axis],
                        "intercept": allowed - error,
                    },
                    {
                        "kind": "moment_lower",
                        "axis": axis,
                        "slope": mb[axis],
                        "intercept": allowed + error,
                    },
                )
            )
        _require(
            type(entry["constraints"]) is list and len(entry["constraints"]) == 7,
            "bounded_halfspaces",
        )
        feasible = lower <= upper
        for actual, reference in zip(entry["constraints"], constraints):
            _require(
                type(actual) is dict
                and set(actual) == set(reference)
                and actual["kind"] == reference["kind"]
                and actual.get("axis") == reference.get("axis"),
                "halfspace_identity",
            )
            _require(
                _near(actual["slope"], reference["slope"])
                and _near(actual["intercept"], reference["intercept"]),
                "halfspace_arithmetic",
            )
            slope, intercept = actual["slope"], actual["intercept"]
            if slope > 0:
                lower = max(lower, -intercept / slope)
            elif slope < 0:
                upper = min(upper, -intercept / slope)
            elif intercept < 0:
                feasible = False
            feasible = feasible and lower <= upper
        _compare(entry["feasible_interval"], [lower, upper], absolute=1e-12)
        _require(entry["feasible"] is feasible, "false_mask_feasibility")
        if feasible:
            curvature = _dot(bw, bw)
            free = _dot(bw, _add(desired, aw, -1.0)) / curvature if curvature else lower
            throttle = min(upper, max(lower, free))
            force = _add(aw, bw, throttle)
            residual = _add(force, desired, -1.0)
            objective = _dot(residual, residual)
            _require(
                _near(entry["throttle"], throttle, absolute=1e-12)
                and _near(entry["unconstrained_throttle"], free, absolute=1e-12)
                and _near(entry["objective"], objective, absolute=1e-5)
                and _near(entry["half_objective_gradient"], _dot(bw, residual), absolute=1e-5),
                "scalar_projection_optimality",
            )
            _compare(entry["predicted_force_eci_n"], force)
            _compare(entry["force_residual_eci_n"], residual)
            _compare(entry["predicted_engine_torque_body_nm"], _add(ma, mb, throttle))
            if best is None or objective < best[0]:
                best = (objective, index, mask, throttle)
    if best is None:
        _require(
            receipt["status"] == "deferred"
            and receipt["reason"] == "no_finite_projection_preserves_vertical_and_moment_headroom"
            and receipt["selected_mask_index"] is None
            and command == base,
            "invalid_empty_projection_defer",
        )
        return False
    _, index, mask, throttle = best
    _require(
        receipt["status"] == "accepted"
        and receipt["reason"] is None
        and type(receipt["selected_mask_index"]) is int
        and receipt["selected_mask_index"] == index
        and all(
            receipt[k] is True
            for k in (
                "main_gimbal_commands_preserved",
                "jet_commands_preserved",
                "flap_commands_preserved",
            )
        ),
        "projection_selection_or_preservation",
    )
    expected_command = {
        "engines": [dict(e) for e in base["engines"]],
        "flap_angles_rad": list(base["flap_angles_rad"]),
    }
    for i in range(13):
        expected_command["engines"][i].update(
            enabled=i in mask, throttle=throttle if i in mask else 0.0
        )
    _require(command == expected_command, "selected_finite_command_changed")
    return True


def verify_terminal_wrench(
    receipt,
    state,
    base_command,
    command,
    profile,
    *,
    desired_force_eci_n=None,
    requested_engine_torque_body_nm=None,
    up_eci=None,
    minimum_vertical_thrust_n=None,
):
    result = {
        "schema": "missionos.starship_terminal_wrench_verification.v1",
        "passed": False,
        "issues": [],
        "finite_affine_projection_selected": False,
        "global_allocation_optimality_established": False,
        "joint_trajectory_feasibility_established": False,
        "actual_force_independently_observed": False,
        "dynamics_replayed": False,
        "arrival_admitted": False,
        "support_admitted": False,
        "physical_execution": False,
    }
    try:
        selected = _check(
            receipt,
            state,
            base_command,
            command,
            profile,
            {
                "desired_force_eci_n": desired_force_eci_n,
                "requested_engine_torque_body_nm": requested_engine_torque_body_nm,
                "up_eci": up_eci,
                "minimum_vertical_thrust_n": minimum_vertical_thrust_n,
            },
        )
        result.update(passed=True, finite_affine_projection_selected=selected)
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
        result["issues"].append("Invalid source-bound finite engine-wrench projection certificate")
    return result
