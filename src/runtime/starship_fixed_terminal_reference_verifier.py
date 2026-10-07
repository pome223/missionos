"""Pure source/kinematic/quintic checks for a fixed terminal REQUEST.

Engine, gravity, mass, gyro and material-point arithmetic is reconstructed.
Declared aerodynamic loads remain source-model inputs: neither their validity
nor the nonlinear plant is replayed. A valid reference is not an attainable
joint trajectory, an actual commanded pose, arrival or physical execution.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math

from .starship_booster_recovery_verifier import (
    _Invalid as _RecoveryInvalid,
    _arrival,
    _command,
    _cross,
    _json,
    _profile_and_config,
    _state,
    _tower,
)
from .starship_terminal_wrench_verifier import (
    _specs,
    _rotate,
    _compare,
    _near,
    _number,
    _vector,
    _require,
)
from .starship_actual_recovery_planning_verifier import _validate_context

SCHEMA = "missionos.starship_fixed_terminal_reference.v1"
POLICY_ID = "fixed_time_pin_pose_quintic_v1"
CONFIG = {
    "policy_id": POLICY_ID,
    "duration_source": "initial_existing_zem_zev_tgo",
    "position_frame": "rotating_tower_enu_material_pin_midpoint",
    "maximum_candidate_calls": 1,
    "maximum_candidate_duration_s": 60.0,
    "maximum_candidate_steps": 600,
    "maximum_candidate_wall_s": 120.0,
    "reference_screen_intervals": 100,
    "runtime_candidate_calls": 0,
    "physics_coefficients_changed": False,
    "gains_changed": False,
    "catch_gates_changed": False,
    "physical_execution": False,
}
_FLAGS = {
    "reference_tracker_history_reset",
    "reference_is_achieved_state",
    "prediction_is_execution",
    "physical_state_assigned",
    "physical_execution",
    "arrival_admitted",
    "support_admitted",
    "native_solver_invoked",
    "joint_reference_feasibility_established",
}
_PLAN = {
    "schema",
    "policy_id",
    "configuration",
    "origin_context_sha256",
    "origin_state_sha256",
    "prior_state",
    "prior_state_sha256",
    "previous_command",
    "previous_command_sha256",
    "profile_sha256",
    "catch_profile_sha256",
    "reference_start_time_s",
    "terminal_time_s",
    "duration_s",
    "duration_source",
    "initial_tgo_inputs",
    "initial_pin_kinematics",
    "position_coefficients",
    "target_position_enu_m",
    "target_velocity_enu_mps",
    "target_acceleration_enu_mps2",
    "pose_start_q_body_to_eci",
    "pose_target_q_body_to_eci",
    "pose_relative_rotation_vector_eci_rad",
    "pose_scalar_coefficients",
    "pose_peak_rate_rad_s",
    "pose_peak_acceleration_rad_s2",
    "maximum_reference_rate_rad_s",
    "maximum_reference_acceleration_rad_s2",
    "carried_requested_rate_eci_rad_s",
    "initial_raw_pose_rate_eci_rad_s",
    "raw_pose_boundary_is_carried_rate",
    "raw_pose_global_rate_continuity_established",
} | _FLAGS


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _add(a, b, scale=1.0):
    return [x + scale * y for x, y in zip(a, b)]


def _scale(a, b):
    return [x * b for x in a]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _norm(a):
    return math.sqrt(_dot(a, a))


def _mv(a, b):
    return [_dot(row, b) for row in a]


def _normalize(q):
    length = _norm(q)
    return [x / length for x in q]


def _multiply(a, b):
    w, x, y, z = a
    s, u, v, t = b
    return [
        w * s - x * u - y * v - z * t,
        w * u + x * s + y * t - z * v,
        w * v - x * t + y * s + z * u,
        w * t + x * v - y * u + z * s,
    ]


def _rotation_vector(q):
    q = _normalize(q)
    axis = max(range(1, 4), key=lambda i: abs(q[i]))
    if abs(q[0]) <= 1e-14 and q[axis] < 0 or abs(q[0]) > 1e-14 and q[0] < 0:
        q = [-x for x in q]
    length = math.hypot(*q[1:])
    angle = 2 * math.atan2(length, max(0.0, q[0]))
    return [x * angle / length for x in q[1:]] if length > 1e-15 else [0.0, 0.0, 0.0]


def _target_quaternion(profile, time):
    _, axes = _tower(profile, time)
    east, north, up = axes
    m = [[east[i], north[i], up[i]] for i in range(3)]
    trace = sum(m[i][i] for i in range(3))
    if trace > 0:
        k = math.sqrt(1 + trace) * 2
        q = [k / 4, (m[2][1] - m[1][2]) / k, (m[0][2] - m[2][0]) / k, (m[1][0] - m[0][1]) / k]
    else:
        i = max(range(3), key=lambda j: m[j][j])
        j, k = (i + 1) % 3, (i + 2) % 3
        f = math.sqrt(1 + m[i][i] - m[j][j] - m[k][k]) * 2
        v = [0.0, 0.0, 0.0]
        v[i] = f / 4
        v[j] = (m[j][i] + m[i][j]) / f
        v[k] = (m[k][i] + m[i][k]) / f
        q = [(m[k][j] - m[j][k]) / f, *v]
    return _normalize(q)


def _coefficients(p0, v0, a0, p1, v1, a1, t):
    # Solve the normalized Hermite endpoint residuals algebraically.
    c0, c1, c2 = p0, v0 * t, 0.5 * a0 * t * t
    y = p1 - c0 - c1 - c2
    d = v1 * t - c1 - 2 * c2
    a = a1 * t * t - 2 * c2
    return [
        c0,
        c1 / t,
        c2 / t**2,
        (10 * y - 4 * d + 0.5 * a) / t**3,
        (-15 * y + 7 * d - a) / t**4,
        (6 * y - 3 * d + 0.5 * a) / t**5,
    ]


def _poly(c, t, order=0):
    result = 0.0
    for i in range(len(c) - 1, order - 1, -1):
        result = result * t + c[i] * math.factorial(i) / math.factorial(i - order)
    return result


def _mass(profile, fuel):
    p, g = profile["booster"], profile["geometry"]
    dry = p["dry_mass_kg"]
    mass = dry + fuel
    offset = p["tank_center_z_m"] - p["dry_com_z_m"]
    com = [0.0, 0.0, p["dry_com_z_m"] + offset * (fuel / mass)]
    dc = [0.0, 0.0, offset * (dry / mass**2)]
    if p.get("dry_inertia_kg_m2") is not None:
        inertia = [list(row) for row in p["dry_inertia_kg_m2"]]
    else:
        inertia = [
            [dry * (3 * g["radius_m"] ** 2 + g["booster_length_m"] ** 2) / 12, 0.0, 0.0],
            [0.0, dry * (3 * g["radius_m"] ** 2 + g["booster_length_m"] ** 2) / 12, 0.0],
            [0.0, 0.0, dry * g["radius_m"] ** 2 / 2],
        ]
    per = (3 * g["radius_m"] ** 2 + p["tank_length_m"] ** 2) / 12
    inertia[0][0] += fuel * per + dry * fuel / mass * offset * offset
    inertia[1][1] += fuel * per + dry * fuel / mass * offset * offset
    inertia[2][2] += fuel * g["radius_m"] ** 2 / 2
    return mass, com, dc, inertia


def _solve(a, b):
    rows = [list(row) + [value] for row, value in zip(a, b)]
    for i in range(3):
        k = max(range(i, 3), key=lambda j: abs(rows[j][i]))
        rows[i], rows[k] = rows[k], rows[i]
        _require(abs(rows[i][i]) > 1e-12, "singular_declared_inertia")
        v = rows[i][i]
        rows[i] = [x / v for x in rows[i]]
        for j in range(3):
            if i != j:
                v = rows[j][i]
                rows[j] = [x - v * y for x, y in zip(rows[j], rows[i])]
    return [row[3] for row in rows]


def _kinematics(kin, state, command, profile, catch):
    keys = {
        "position_enu_m",
        "velocity_enu_mps",
        "acceleration_enu_mps2",
        "com_body_m",
        "com_rate_body_mps",
        "com_acceleration_body_mps2",
        "angular_acceleration_body_rad_s2",
        "current_cg_acceleration_eci_mps2",
        "source_model_load_inputs",
        "convention",
        "physical_plant_integrations",
        "physical_state_assigned",
    }
    _require(
        type(kin) is dict
        and set(kin) == keys
        and kin["convention"] == "current_finite_plant_rhs_under_last_causal_command"
        and type(kin["physical_plant_integrations"]) is int
        and kin["physical_plant_integrations"] == 0
        and kin["physical_state_assigned"] is False,
        "initial_rhs_scope",
    )
    loads = kin["source_model_load_inputs"]
    loadkeys = {
        "mass_kg",
        "inertia_kg_m2",
        "engine_loads",
        "aero_force_body_n",
        "aero_torque_body_nm",
        "gravity_acceleration_eci_mps2",
        "gravity_gradient_torque_body_nm",
        "fuel_rate_kg_s",
        "fuel_acceleration_kg_s2",
        "engine_throttle_derivatives_per_s",
        "rhs_angular_variable_mass_convention",
        "aero_and_plant_rhs_independently_replayed",
    }
    _require(
        type(loads) is dict
        and set(loads) == loadkeys
        and loads["rhs_angular_variable_mass_convention"]
        == "instantaneous_properties_without_depletion_rate_loads"
        and loads["aero_and_plant_rhs_independently_replayed"] is False,
        "source_model_rhs_boundary",
    )
    mass, com, dc, inertia = _mass(profile, state["propellant_kg"])
    _require(_near(loads["mass_kg"], mass), "initial_mass")
    _compare(kin["com_body_m"], com)
    _require(
        type(loads["inertia_kg_m2"]) is list and len(loads["inertia_kg_m2"]) == 3, "initial_inertia"
    )
    for a, b in zip(loads["inertia_kg_m2"], inertia):
        _compare(a, b)
    specs = _specs(profile)
    rows = loads["engine_loads"]
    rates = []
    force = [0.0, 0.0, 0.0]
    moment = [0.0, 0.0, 0.0]
    flow = 0.0
    flowdot = 0.0
    _require(type(rows) is list and len(rows) == 45, "current_engine_load_count")
    for index, (spec, actual, target, row) in enumerate(
        zip(specs, state["engine_states"], command["engines"], rows)
    ):
        name = (
            f"booster_main_{index}"
            if index < 33
            else f"rcs_{(index - 33) // 4}_{(-1, 1)[((index - 33) % 4) // 2]}_{(-1, 1)[(index - 33) % 2]}"
        )
        isp = (
            profile["booster"]["engine_isp_s"] if index < 33 else profile["actuators"]["rcs_isp_s"]
        )
        thrust = (
            spec["thrust"] * actual["throttle"]
            if actual["available"] and state["propellant_kg"] > 0
            else 0.0
        )
        gx, gy = actual["gimbal_x_rad"], actual["gimbal_y_rad"]
        x, y, z = spec["direction"]
        cx, sx, cy, sy = math.cos(gx), math.sin(gx), math.cos(gy), math.sin(gy)
        f = _scale(
            [cy * x + sy * (sx * y + cx * z), cx * y - sx * z, -sy * x + cy * (sx * y + cx * z)],
            thrust,
        )
        m = _cross(_add(spec["position"], com, -1.0), f)
        rate = thrust / (isp * 9.80665)
        _require(
            type(row) is dict
            and set(row) == {"name", "force_body_n", "torque_body_nm", "thrust_n", "mass_flow_kg_s"}
            and row["name"] == name
            and _near(row["thrust_n"], thrust)
            and _near(row["mass_flow_kg_s"], rate),
            "current_engine_load_identity",
        )
        _compare(row["force_body_n"], f)
        _compare(row["torque_body_nm"], m)
        force = _add(force, f)
        moment = _add(moment, m)
        flow += rate
        goal = (
            target["throttle"]
            if target["enabled"] and actual["available"] and state["propellant_kg"] > 0
            else 0.0
        )
        td = (
            0.0
            if abs(goal - actual["throttle"]) <= 1e-10
            else max(-spec["rate"], min(spec["rate"], (goal - actual["throttle"]) / spec["tau"]))
        )
        rates.append(td)
        if actual["available"] and state["propellant_kg"] > 0:
            flowdot += spec["thrust"] / (isp * 9.80665) * td
    _compare(loads["engine_throttle_derivatives_per_s"], rates)
    _require(
        _near(loads["fuel_rate_kg_s"], -flow) and _near(loads["fuel_acceleration_kg_s2"], -flowdot),
        "current_causal_fuel_derivative",
    )
    cdot = _scale(dc, -flow)
    cddot = _add(_scale(dc, -flowdot), dc, -2 * flow * flow / mass)
    _compare(kin["com_rate_body_mps"], cdot)
    _compare(kin["com_acceleration_body_mps2"], cddot)
    r = state["r_eci_m"]
    radius = _norm(r)
    mu = 3.986004418e14
    z2 = (r[2] / radius) ** 2
    factor = 1.5 * 1.08262982e-3 * mu * 6378137.0**2 / radius**5
    gravity = _add(
        _scale(r, -mu / radius**3),
        [factor * r[0] * (5 * z2 - 1), factor * r[1] * (5 * z2 - 1), factor * r[2] * (5 * z2 - 3)],
    )
    _compare(loads["gravity_acceleration_eci_mps2"], gravity)
    q = state["q_body_to_eci"]
    n = _rotate([q[0], -q[1], -q[2], -q[3]], _scale(r, 1 / radius))
    gg = _scale(_cross(n, _mv(inertia, n)), 3 * mu / radius**3)
    _compare(loads["gravity_gradient_torque_body_nm"], gg)
    _require(
        _vector(loads["aero_force_body_n"]) and _vector(loads["aero_torque_body_nm"]),
        "invalid_declared_aero_model_inputs",
    )
    cgacc = _add(_scale(_rotate(q, _add(force, loads["aero_force_body_n"])), 1 / mass), gravity)
    omega = state["omega_body_rad_s"]
    alpha = _solve(
        inertia,
        _add(
            _add(_add(moment, loads["aero_torque_body_nm"]), gg),
            _cross(omega, _mv(inertia, omega)),
            -1.0,
        ),
    )
    _compare(kin["current_cg_acceleration_eci_mps2"], cgacc)
    _compare(kin["angular_acceleration_body_rad_s2"], alpha)
    lever = _add(
        [sum(point[i] for point in catch["support_points_body_m"]) / 2 for i in range(3)], com, -1.0
    )
    ldot = _scale(cdot, -1.0)
    lddot = _scale(cddot, -1.0)
    position = _add(r, _rotate(q, lever))
    velocity = _add(state["v_eci_mps"], _rotate(q, _add(_cross(omega, lever), ldot)))
    rotation_acc = _add(
        _add(_cross(alpha, lever), _cross(omega, _cross(omega, lever))),
        _add(_scale(_cross(omega, ldot), 2.0), lddot),
    )
    acceleration = _add(cgacc, _rotate(q, rotation_acc))
    earth = [0.0, 0.0, 7.292115e-5]
    relative_v = _add(velocity, _cross(earth, position), -1.0)
    relative_a = _add(
        _add(acceleration, _cross(earth, velocity), -2.0), _cross(earth, _cross(earth, position))
    )
    origin, axes = _tower(profile, state["time_s"])
    for key, value in (
        ("position_enu_m", _add(position, origin, -1.0)),
        ("velocity_enu_mps", relative_v),
        ("acceleration_enu_mps2", relative_a),
    ):
        _compare(kin[key], [_dot(value, axis) for axis in axes], absolute=1e-6)
    return kin


def _plan(plan, snapshot, profile, catch, prior_checkpoint=None, source_run=None):
    for v in (plan, snapshot, profile, catch, prior_checkpoint, source_run):
        _json(v, maximum_nodes=32_000_000)
    _profile_and_config(profile, catch)
    _state(snapshot["state"], profile)
    from .starship_constrained_recovery_verifier import _CONFIGURATIONS

    _validate_context(
        snapshot,
        snapshot["state"],
        profile,
        catch,
        _CONFIGURATIONS["constrained_return_development_v10"],
    )
    _require(
        type(plan) is dict
        and set(plan) == _PLAN
        and plan["schema"] == SCHEMA
        and plan["policy_id"] == POLICY_ID
        and plan["configuration"] == CONFIG
        and all(plan[k] is False for k in _FLAGS),
        "fixed_reference_closed_scope",
    )
    state = snapshot["state"]
    now = state["time_s"]
    prior = plan["prior_state"]
    cmd = plan["previous_command"]
    _state(prior, profile)
    _command(cmd, "recovery_entry_coast", prior, profile)
    _require(
        snapshot["context"]["phase"] == "recovery_entry_coast"
        and 0 < now - prior["time_s"] <= 0.25000001
        and snapshot["context"]["prior_command_reference"]["time_s"] == prior["time_s"]
        and plan["origin_context_sha256"] == digest(snapshot)
        and plan["origin_state_sha256"] == digest(state)
        and plan["prior_state_sha256"] == digest(prior)
        and plan["previous_command_sha256"] == digest(cmd)
        and plan["profile_sha256"] == digest(profile)
        and plan["catch_profile_sha256"] == digest(catch),
        "causal_origin_and_profile_binding",
    )
    if prior_checkpoint is not None:
        _require(
            prior_checkpoint["state"] == prior
            and prior_checkpoint["command"] == cmd
            and prior_checkpoint["time_s"] == prior["time_s"]
            and prior_checkpoint["phase"] == "recovery_entry_coast",
            "prior_actual_checkpoint_binding",
        )
        navigation = prior_checkpoint["navigation"]
        _require(
            snapshot["context"]["prior_command_reference"]
            == {
                "quaternion": navigation["target_q_body_to_eci"],
                "time_s": prior_checkpoint["time_s"],
            },
            "causal_prior_requested_quaternion",
        )
        tracker = snapshot["context"]["reference_tracker"]
        if tracker is not None:
            tracked = navigation["reference_tracking"]
            _require(
                tracker["quaternion"] == tracked["requested_q_body_to_eci"]
                and tracker["rate_eci_rad_s"] == tracked["reference_rate_eci_rad_s"]
                and tracker["raw_goal_q"] == tracked["raw_goal_q_body_to_eci"]
                and tracker["time_s"] == tracker["raw_goal_time_s"] == prior_checkpoint["time_s"]
                and tracker["first_update"] is False,
                "carried_tracker_from_actual_prior_checkpoint",
            )
    if source_run is not None:
        points = source_run["recovery_record"]["checkpoints"]
        matches = [i for i, p in enumerate(points) if p["time_s"] == now]
        _require(
            len(matches) == 1
            and matches[0] > 0
            and points[matches[0]]["state"] == state
            and prior_checkpoint == points[matches[0] - 1],
            "no_future_or_substituted_source_anchors",
        )
    kin = _kinematics(plan["initial_pin_kinematics"], state, cmd, profile, catch)
    inputs = plan["initial_tgo_inputs"]
    a = _arrival(state, profile, catch)
    from .starship_constrained_recovery_verifier import _local_velocity

    velocity = _local_velocity(state)
    clear = catch["initial_pin_clearance_m"] + 0.5 * catch["arm_half_width_m"]
    target_v = 1.5 * catch["initial_vertical_speed_mps"]
    _require(
        type(inputs) is dict
        and set(inputs)
        == {
            "minimum_pin_clearance_m",
            "local_cg_vertical_velocity_mps",
            "target_pin_clearance_m",
            "target_vertical_velocity_mps",
        }
        and _near(
            inputs["minimum_pin_clearance_m"],
            min(p["height_above_support_m"] for p in a["pins"]),
            absolute=1e-6,
        )
        and _near(inputs["local_cg_vertical_velocity_mps"], velocity[2], absolute=1e-6)
        and inputs["target_pin_clearance_m"] == clear
        and inputs["target_vertical_velocity_mps"] == target_v,
        "initial_existing_tgo_inputs",
    )
    t = max(
        2.0,
        2
        * max(0.0, inputs["minimum_pin_clearance_m"] - clear)
        / max(1.0, -inputs["local_cg_vertical_velocity_mps"] - target_v),
    )
    _require(
        plan["duration_s"] == t
        and 0 < t <= 60.0
        and plan["reference_start_time_s"] == now
        and plan["terminal_time_s"] == now + t
        and plan["duration_source"] == "initial_existing_zem_zev_tgo",
        "fixed_nonsearched_reference_clock",
    )
    p1 = [0.0, 0.0, catch["support_height_m"] + clear]
    v1 = [0.0, 0.0, target_v]
    a1 = [0.0, 0.0, 0.0]
    _require(
        plan["target_position_enu_m"] == p1
        and plan["target_velocity_enu_mps"] == v1
        and plan["target_acceleration_enu_mps2"] == a1,
        "material_pin_endpoint_not_cg_reset",
    )
    _require(
        type(plan["position_coefficients"]) is list and len(plan["position_coefficients"]) == 3,
        "quintic_dimensions",
    )
    for i, row in enumerate(plan["position_coefficients"]):
        expected = _coefficients(
            kin["position_enu_m"][i],
            kin["velocity_enu_mps"][i],
            kin["acceleration_enu_mps2"][i],
            p1[i],
            v1[i],
            0.0,
            t,
        )
        _compare(row, expected, absolute=1e-9)
        for clock, value in (
            (
                0.0,
                [
                    kin["position_enu_m"][i],
                    kin["velocity_enu_mps"][i],
                    kin["acceleration_enu_mps2"][i],
                ],
            ),
            (t, [p1[i], v1[i], 0.0]),
        ):
            _compare([_poly(row, clock, j) for j in range(3)], value, absolute=1e-6)
    tracker = snapshot["context"]["reference_tracker"]
    q0 = (
        tracker["quaternion"]
        if tracker
        else snapshot["context"]["prior_command_reference"]["quaternion"]
    )
    _require(plan["pose_start_q_body_to_eci"] == q0, "request_history_anchor_not_actual_reset")
    carried = tracker["rate_eci_rad_s"] if tracker else [0.0, 0.0, 0.0]
    _require(
        plan["carried_requested_rate_eci_rad_s"] == carried
        and plan["initial_raw_pose_rate_eci_rad_s"] == [0.0, 0.0, 0.0]
        and plan["raw_pose_boundary_is_carried_rate"]
        is (tracker is None or math.hypot(*carried) == 0.0)
        and plan["raw_pose_global_rate_continuity_established"] is False,
        "raw_pose_rate_boundary_not_tracker_reset",
    )
    qt = _target_quaternion(profile, now + t)
    _compare(plan["pose_target_q_body_to_eci"], qt, absolute=1e-9)
    phi = _rotation_vector(_multiply(qt, [q0[0], -q0[1], -q0[2], -q0[3]]))
    _compare(plan["pose_relative_rotation_vector_eci_rad"], phi, absolute=1e-9)
    _compare(
        plan["pose_scalar_coefficients"],
        _coefficients(0.0, 0.0, 0.0, 1.0, 0.0, 0.0, t),
        absolute=1e-12,
    )
    theta = _norm(phi)
    g = profile["guidance"]
    _require(
        _near(plan["pose_peak_rate_rad_s"], 1.875 * theta / t, absolute=1e-12)
        and _near(
            plan["pose_peak_acceleration_rad_s2"],
            10 * theta / (math.sqrt(3) * t * t),
            absolute=1e-12,
        )
        and plan["maximum_reference_rate_rad_s"]
        == g["max_angular_acceleration_rad_s2"] / g["attitude_frequency_rad_s"]
        and plan["maximum_reference_acceleration_rad_s2"] == g["max_angular_acceleration_rad_s2"],
        "analytical_pose_rate_acceleration_caps",
    )
    return theta


def verify_fixed_terminal_reference(
    plan, snapshot, profile, catch_config, *, prior_checkpoint=None, source_run=None
):
    result = {
        "schema": "missionos.starship_fixed_terminal_reference_verification.v1",
        "passed": False,
        "arithmetic_passed": False,
        "source_bound_arithmetic_passed": False,
        "verification_scope": "reference_arithmetic_not_execution_admission",
        "physical_invocation_admitted": False,
        "issues": [],
        "past_source_anchors_bound": False,
        "pose_request_caps_satisfied": False,
        "aerodynamic_rhs_independently_replayed": False,
        "joint_reference_feasibility_established": False,
        "dynamics_replayed": False,
        "source_authenticated": False,
        "arrival_admitted": False,
        "support_admitted": False,
        "physical_execution": False,
    }
    try:
        _plan(plan, snapshot, profile, catch_config, prior_checkpoint, source_run)
        result.update(
            passed=True,
            arithmetic_passed=True,
            source_bound_arithmetic_passed=source_run is not None and prior_checkpoint is not None,
            past_source_anchors_bound=source_run is not None and prior_checkpoint is not None,
            pose_request_caps_satisfied=plan["pose_peak_rate_rad_s"]
            <= plan["maximum_reference_rate_rad_s"]
            and plan["pose_peak_acceleration_rad_s2"]
            <= plan["maximum_reference_acceleration_rad_s2"],
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
            "Invalid fixed-time source/kinematic/quintic/pose reference contract"
        )
    return result


def verify_source_bound_terminal_reference(
    plan, snapshot, profile, catch_config, *, prior_checkpoint=None, source_run=None
):
    """Require retained past anchors in addition to arithmetic consistency.

    Even this strict result is not a force, actuator, integration, arrival or
    execution-admission certificate. Source authentication remains unproved.
    """
    result = verify_fixed_terminal_reference(
        plan, snapshot, profile, catch_config,
        prior_checkpoint=prior_checkpoint, source_run=source_run,
    )
    result["verification_scope"] = "source_bound_reference_arithmetic_not_execution_admission"
    result["passed"] = result["source_bound_arithmetic_passed"]
    if result["arithmetic_passed"] and not result["past_source_anchors_bound"]:
        result["issues"].append("Retained source run and prior checkpoint are required")
    return result


def expected_reference_sample(plan, time_s):
    _require(
        _number(time_s) and time_s >= plan["reference_start_time_s"] - 1e-9,
        "reference_sample_before_origin",
    )
    t = max(0.0, time_s - plan["reference_start_time_s"])
    duration = plan["duration_s"]
    if t <= duration:
        values = [[_poly(c, t, j) for c in plan["position_coefficients"]] for j in range(4)]
        scalar = [_poly(plan["pose_scalar_coefficients"], t, j) for j in range(3)]
        phi = plan["pose_relative_rotation_vector_eci_rad"]
        theta = _norm(phi)
        angle = scalar[0] * theta
        increment = [
            math.cos(angle / 2),
            *(_scale(phi, math.sin(angle / 2) / theta) if theta > 1e-15 else [0.0, 0.0, 0.0]),
        ]
        q = _normalize(_multiply(increment, plan["pose_start_q_body_to_eci"]))
        rate, acceleration = _scale(phi, scalar[1]), _scale(phi, scalar[2])
    else:
        delta = t - duration
        values = [
            _add(plan["target_position_enu_m"], plan["target_velocity_enu_mps"], delta),
            plan["target_velocity_enu_mps"],
            [0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0],
        ]
        angle = 7.292115e-5 * delta
        q = _normalize(
            _multiply(
                [math.cos(angle / 2), 0.0, 0.0, math.sin(angle / 2)],
                plan["pose_target_q_body_to_eci"],
            )
        )
        rate, acceleration = [0.0, 0.0, 7.292115e-5], [0.0, 0.0, 0.0]
    return {
        "time_s": time_s,
        "elapsed_s": t,
        "position_enu_m": values[0],
        "velocity_enu_mps": values[1],
        "acceleration_enu_mps2": values[2],
        "jerk_enu_mps3": values[3],
        "pose_q_body_to_eci": q,
        "pose_rate_eci_rad_s": rate,
        "pose_acceleration_eci_rad_s2": acceleration,
        "reference_is_execution": False,
    }


def verify_reference_sample(sample, plan, time_s):
    result = {
        "passed": False,
        "issues": [],
        "reference_is_execution": False,
        "arrival_admitted": False,
        "physical_execution": False,
    }
    try:
        _json(sample)
        expected = expected_reference_sample(plan, time_s)
        _require(
            type(sample) is dict
            and set(sample) == set(expected)
            and sample["reference_is_execution"] is False
            and sample["time_s"] == time_s
            and sample["elapsed_s"] == expected["elapsed_s"],
            "sample_clock_or_authority",
        )
        for key in expected:
            if type(expected[key]) is list:
                _compare(sample[key], expected[key], absolute=1e-6)
        result["passed"] = True
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
        result["issues"].append("Invalid same-time fixed reference sample")
    return result
