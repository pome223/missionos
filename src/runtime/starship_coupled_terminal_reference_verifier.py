"""Independent arithmetic for hypothetical coupled force/pose references.

No observer, controller producer, or trajectory integrator is imported. Stored
static panel loads and requested-frame history can be checked; these hypotheses
never certify finite actuator, fuel, clearance, or nonlinear tracking feasibility.
"""

from __future__ import annotations

import math

from .starship_booster_recovery_verifier import _cross, _json, _tower
from .starship_fixed_terminal_reference_verifier import (
    _add, _dot, _norm, _scale, _mass, digest,
    verify_fixed_terminal_reference, verify_reference_sample,
)
from .starship_reference_tracking_verifier import (
    _clip_vector, _exp, _inverse, _multiply, _rotation_vector,
)
from .starship_terminal_reference_screen_verifier import (
    _altitude, _density, _gravity, _hypothetical, _panel_inputs, _panels,
)
from .starship_terminal_wrench_verifier import _compare, _near, _number, _require, _rotate, _vector

SCHEMA = "missionos.starship_coupled_terminal_reference.v1"
SCHEMA_V2 = "missionos.starship_coupled_terminal_reference.v2"
SCHEMA_V3 = "missionos.starship_coupled_terminal_reference.v3"
CONFIG = {
    "maximum_sweeps": 3,
    "node_count": 101,
    "maximum_updates_per_node": 8,
    "maximum_line_search_queries": 4,
    "maximum_model_queries": 3 * 101 * 8 * 7 + 3 * 101,
    "maximum_wall_s": 120.0,
    "maximum_persisted_bytes": 100 * 1024 * 1024,
    "minimum_free_bytes": 400 * 1024 * 1024,
    "jacobian_angle_source": "sqrt_machine_epsilon_radians",
    "maximum_newton_update_rad": math.pi / 4,
    "physical_plant_calls": 0,
    "translation_reference_changed": False,
    "profile_or_gain_changed": False,
    "duration_or_origin_search": False,
}
CONFIG_V2 = {
    **CONFIG,
    "newton_increment_source": "analytic_continuous_quaternion_exponential",
    "negative_axial_stationary_source":
        "existing_tolerance_transverse_stationary_negative_axial_unresolved",
}
CONFIG_V3 = {
    **CONFIG_V2,
    "positive_axial_branch_source": "one_body_pitch_pi_hypothesis_then_positive_axial_constrained_newton",
    "maximum_positive_axial_branch_probes": 3 * 101,
    "maximum_model_queries": CONFIG["maximum_model_queries"] + 3 * 101,
}
_LOAD_FIELDS = {
    "hypothetical_reference_state", "integrated_observation", "reference_sample",
    "mass_kg", "propellant_kg", "r_eci_m", "v_eci_mps", "omega_body_rad_s",
    "com_body_m", "gravity_acceleration_eci_mps2", "cg_reference_acceleration_eci_mps2",
    "aero_force_body_n", "aero_torque_body_nm", "panel_loads", "required_main_force_body_n",
    "aero_loads_independently_replayed", "future_fin_angles_assumption",
    "required_engine_torque_body_nm", "rigid_body_torque_body_nm", "inertia_kg_m2",
    "gravity_gradient_torque_body_nm", "angular_variable_mass_convention",
    "mass_kinematics_convention",
}


def _unit(q):
    _require(_vector(q, 4) and _near(_norm(q), 1.0, absolute=1e-10), "hypothetical_unit_quaternion")
    return q


def _increment(q, x, y):
    increment = _exp([x, y, 0.0])
    result = _multiply(q, increment)
    return _scale(result, 1.0 / _norm(result))


def _increment_continuous(q, x, y):
    """Independent analytic exp map, without the legacy small-angle cutoff."""
    _unit(q)
    _require(_number(x) and _number(y), "finite_continuous_increment")
    angle = math.hypot(x, y)
    half_angle = angle / 2.0
    # At zero (or a subnormal half-angle rounded to zero), sin(a/2)/a
    # has the analytic limit 1/2. This is not a convergence tolerance.
    factor = 0.5 if half_angle == 0.0 else math.sin(half_angle) / angle
    delta = [math.cos(half_angle), x * factor, y * factor, 0.0]
    result = _multiply(q, delta)
    return _scale(result, 1.0 / _norm(result))


def _attitude(z, reference):
    z = _scale(z, 1 / _norm(z))
    x = _add(reference, z, -_dot(reference, z))
    if _norm(x) < 1e-6:
        index = min(range(3), key=lambda i: abs(z[i]))
        alternative = [float(i == index) for i in range(3)]
        x = _add(alternative, z, -_dot(alternative, z))
    x = _scale(x, 1 / _norm(x))
    y = _cross(z, x)
    m = [[x[i], y[i], z[i]] for i in range(3)]
    trace = sum(m[i][i] for i in range(3))
    if trace > 0:
        s = 2 * math.sqrt(trace + 1)
        q = [s / 4, (m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s]
    else:
        i = max(range(3), key=lambda j: m[j][j])
        j, k = (i + 1) % 3, (i + 2) % 3
        s = 2 * math.sqrt(1 + m[i][i] - m[j][j] - m[k][k])
        v = [0.0] * 3
        v[i], v[j], v[k] = s / 4, (m[i][j] + m[j][i]) / s, (m[i][k] + m[k][i]) / s
        q = [(m[k][j] - m[j][k]) / s, *v]
    return _scale(q, 1 / _norm(q))


def _conditioned(previous, raw_q, when, profile):
    """Parallel-transport then bounded East-roll reacquisition, as REQUEST math."""
    q = _unit(previous["quaternion"])
    desired = _rotate(_unit(raw_q), [0.0, 0.0, 1.0])
    old_axis = _rotate(q, [0.0, 0.0, 1.0])
    sine_axis = _cross(old_axis, desired)
    sine, cosine = _norm(sine_axis), max(-1.0, min(1.0, _dot(old_axis, desired)))
    angle = math.atan2(sine, cosine)
    antipodal = sine < 1e-12 and cosine < 0
    if antipodal:
        delta = [0.0, *_rotate(q, [1.0, 0.0, 0.0])]
    elif sine < 1e-12:
        delta = [1.0, 0.0, 0.0, 0.0]
    else:
        delta = [math.cos(angle / 2), *_scale(sine_axis, math.sin(angle / 2) / sine)]
    transported = _multiply(delta, q)
    transported = _scale(transported, 1 / _norm(transported))
    if _dot(transported, q) < 0:
        transported = _scale(transported, -1)
    z = _rotate(transported, [0.0, 0.0, 1.0])
    _, axes = _tower(profile, when)
    reference = _scale(axes[0], 1 / _norm(axes[0]))
    projection = _norm(_add(reference, z, -_dot(reference, z)))
    preferred = _attitude(desired, axes[0])
    _require(_number(when) and when >= previous["time_s"], "conditioned_reference_monotonic_clock")
    dt = when - previous["time_s"]
    maximum = profile["guidance"]["max_angular_acceleration_rad_s2"] / profile["guidance"]["attitude_frequency_rad_s"]
    _require(_near(previous["maximum_roll_rate_rad_s"], maximum), "unchanged_geographic_roll_limit")
    bridging = previous["bridging"] or projection < 0.1
    error = step = 0.0
    if not bridging:
        target, mode = preferred, "legacy_geographic"
    elif projection < 0.1:
        target, mode = transported, "transport_ill_conditioned"
    else:
        x, wanted = _rotate(transported, [1.0, 0.0, 0.0]), _rotate(preferred, [1.0, 0.0, 0.0])
        error = math.atan2(_dot(z, _cross(x, wanted)), _dot(x, wanted))
        step = max(-maximum * dt, min(maximum * dt, error))
        if abs(error) <= maximum * dt:
            target, bridging, mode = preferred, False, "geographic_reacquired"
        else:
            delta = [math.cos(step / 2), *_scale(z, math.sin(step / 2))]
            target = _multiply(delta, transported)
            target = _scale(target, 1 / _norm(target))
            mode = "bounded_roll_reacquisition"
    diagnostic = {"policy_id": "conditioned_geographic_v1", "target_q_body_to_eci": target,
        "axis_step_rad": angle, "antipodal_fallback": antipodal, "mode": mode,
        "reference_projection_norm": projection, "minimum_reference_projection": 0.1,
        "roll_error_rad": error, "roll_step_rad": step, "maximum_roll_rate_rad_s": maximum,
        "elapsed_s": dt}
    return {"quaternion": target, "time_s": when, "maximum_roll_rate_rad_s": maximum,
            "bridging": bridging}, diagnostic


def _load(item, request, reference, initial, profile, catch):
    """Reconstruct one stored calm-flow, frozen-mass reference load query."""
    _require(type(item) is dict and set(item) == _LOAD_FIELDS
             and item["hypothetical_reference_state"] is True
             and item["integrated_observation"] is False
             and item["aero_loads_independently_replayed"] is False
             and item["mass_kinematics_convention"] == "nodewise_frozen_mass_com_derivatives_zero"
             and item["future_fin_angles_assumption"] == "held_origin_actual_angles",
             "closed_hypothetical_load_scope")
    q = _unit(request["q"])
    rate, acceleration, fuel = (request[k] for k in ("rate_eci_rad_s", "acceleration_eci_rad_s2", "fuel_kg"))
    _require(_vector(rate) and _vector(acceleration) and _number(fuel)
             and 0 <= fuel <= initial["propellant_kg"], "hypothetical_mass_rate_inputs")
    when = request["reference_sample"]["time_s"]
    original = request["reference_sample"]
    _require(verify_reference_sample(original, reference, when)["passed"], "immutable_pin_polynomial_sample")
    sample = {**original, "pose_q_body_to_eci": q, "pose_rate_eci_rad_s": rate,
              "pose_acceleration_eci_rad_s2": acceleration}
    _require(item["reference_sample"] == sample and item["propellant_kg"] == fuel,
             "query_reference_pose_binding")
    mass, com, _, omega, r, v, cg_acceleration, _ = _hypothetical(sample, initial, profile, catch, fuel)
    for key, expected in (("com_body_m", com), ("omega_body_rad_s", omega),
                          ("r_eci_m", r), ("v_eci_mps", v),
                          ("cg_reference_acceleration_eci_mps2", cg_acceleration),
                          ("gravity_acceleration_eci_mps2", _gravity(r))):
        _compare(item[key], expected, absolute=1e-6)
    _require(_near(item["mass_kg"], mass), "hypothetical_mass_arithmetic")
    # The independent screen helper checks each configured plate normal,
    # velocity, force, and moment. Its result here is not an envelope proof.
    _panel_inputs({"altitude_m": _altitude(r), "density_kg_m3": _density(_altitude(r)),
                   "panel_loads": item["panel_loads"], "r_eci_m": r, "v_eci_mps": v,
                   "q_body_to_eci": q, "omega_body_rad_s": omega, "propellant_kg": fuel,
                   "cg_reference_acceleration_eci_mps2": cg_acceleration,
                   "aero_loads_independently_replayed": False},
                  sample, initial, profile, catch, fuel, _panels(profile))
    force = [sum(row["force_body_n"][axis] for row in item["panel_loads"]) for axis in range(3)]
    torque = [sum(row["torque_body_nm"][axis] for row in item["panel_loads"]) for axis in range(3)]
    required = _add(_rotate(_inverse(q), _scale(_add(cg_acceleration, _gravity(r), -1.0), mass)), force, -1.0)
    _compare(item["aero_force_body_n"], force, absolute=1e-4)
    _compare(item["aero_torque_body_nm"], torque, absolute=1e-3)
    _compare(item["required_main_force_body_n"], required, absolute=1e-4)
    _, _, _, inertia = _mass(profile, fuel)
    _same(item["inertia_kg_m2"], inertia, "hypothetical_inertia")
    def matvec(v):
        return [_dot(row, v) for row in inertia]
    alpha = _rotate(_inverse(q), acceleration)
    rigid = _add(matvec(alpha), _cross(omega, matvec(omega)))
    radial = _rotate(_inverse(q), _scale(r, 1 / _norm(r)))
    gg = _scale(_cross(radial, matvec(radial)), 3 * 3.986004418e14 / _norm(r) ** 3)
    _compare(item["rigid_body_torque_body_nm"], rigid, absolute=1e-3)
    _compare(item["gravity_gradient_torque_body_nm"], gg, absolute=1e-3)
    _compare(item["required_engine_torque_body_nm"], _add(_add(rigid, torque, -1), gg, -1), absolute=1e-3)
    _require(item["angular_variable_mass_convention"] == "instantaneous_properties_without_depletion_rate_loads",
             "unchanged_angular_variable_mass_convention")
    return required


def _recorded_clipping(item, ceiling, maximum):
    """Check exact floating-point flags after the preclip vectors are verified.

    The tracker uses math.hypot on these recorded inputs. Reconstructing a
    quaternion history can differ by an ulp at the clipping boundary; its
    vector arithmetic is checked separately with the unchanged tolerance.
    """
    for flag, field, limit in (("goal_rate_clipped", "unbounded_goal_rate_eci_rad_s", ceiling),
            ("rate_clipped", "unbounded_reference_rate_eci_rad_s", ceiling),
            ("acceleration_clipped", "unbounded_reference_acceleration_eci_rad_s2", maximum)):
        _require(_vector(item[field]) and item[flag] is (math.hypot(*item[field]) > limit),
                 "governor_clipping_arithmetic")


def _tracking(item, previous, goal, actual, body_rate, when, profile):
    """Check a cloned tracker; actual inputs here remain hypothetical."""
    _unit(goal)
    _unit(actual)
    q, old_rate, old_goal, old_time = (previous[k] for k in
        ("quaternion", "rate_eci_rad_s", "raw_goal_q", "time_s"))
    _unit(q)
    _unit(old_goal)
    _require(type(item) is dict and item.get("schema") == "missionos.starship_reference_tracking.v1"
             and item.get("policy_id") == "bounded_reference_tracking_v1"
             and item.get("request_is_execution") is False and item.get("actual_state_assigned") is False,
             "cloned_governor_scope")
    dt = when - old_time
    _require(0 <= dt <= 1.0 and (dt > 0 or previous["first_update"] is True)
             and previous["raw_goal_time_s"] == old_time
             and item.get("time_s") == when and item.get("previous_time_s") == old_time
             and item.get("previous_raw_goal_time_s") == old_time
             and item.get("interval_s") == dt and item.get("first_update") is previous["first_update"]
             and item.get("initialized_at_current_time") is (dt == 0), "causal_cloned_governor_clock")
    maximum = profile["guidance"]["max_angular_acceleration_rad_s2"]
    ceiling = maximum / profile["guidance"]["attitude_frequency_rad_s"]
    _require(_near(item.get("maximum_reference_rate_rad_s"), ceiling)
             and _near(item.get("maximum_reference_acceleration_rad_s2"), maximum)
             and item.get("maximum_interval_s") == 1.0 and _norm(old_rate) <= ceiling + 1e-10,
             "unchanged_cloned_governor_limits")
    error = _rotation_vector(_multiply(goal, _inverse(q)))
    rotation = _rotation_vector(_multiply(goal, _inverse(old_goal)))
    if dt == 0:
        goal_raw = goal_rate = closing_rate = [0.0] * 3
        unbounded_rate = limited_rate = rate = old_rate
        unbounded_acceleration = acceleration = [0.0] * 3
        bound, propagated, closing_error, requested = 0.0, q, error, q
    else:
        goal_raw = _scale(rotation, 1 / dt)
        goal_rate = _clip_vector(goal_raw, ceiling)
        propagated = _multiply(_exp(_scale(goal_rate, dt)), q)
        propagated = _scale(propagated, 1 / _norm(propagated))
        closing_error = _rotation_vector(_multiply(goal, _inverse(propagated)))
        angle = _norm(closing_error)
        bound = math.sqrt((maximum * dt) ** 2 + 2 * maximum * angle) - maximum * dt
        closing_rate = _scale(closing_error, min(1 / dt, bound / max(angle, 1e-300)))
        unbounded_rate = _add(goal_rate, closing_rate)
        limited_rate = _clip_vector(unbounded_rate, ceiling)
        unbounded_acceleration = _scale(_add(limited_rate, old_rate, -1), 1 / dt)
        acceleration = _clip_vector(unbounded_acceleration, maximum)
        rate = _add(old_rate, acceleration, dt)
        requested = _multiply(_exp(_scale(rate, dt)), q)
        requested = _scale(requested, 1 / _norm(requested))
        if _dot(requested, q) < 0:
            requested = _scale(requested, -1)
    vectors = {"previous_requested_q_body_to_eci": q,
        "previous_reference_rate_eci_rad_s": old_rate, "previous_raw_goal_q_body_to_eci": old_goal,
        "actual_q_body_to_eci": actual, "actual_body_rate_rad_s": body_rate,
        "raw_goal_q_body_to_eci": goal, "goal_error_rotation_eci_rad": error,
        "raw_goal_rotation_eci_rad": rotation, "unbounded_goal_rate_eci_rad_s": goal_raw,
        "goal_rate_eci_rad_s": goal_rate, "goal_rate_propagated_request_q_body_to_eci": propagated,
        "closing_error_rotation_eci_rad": closing_error, "closing_reference_rate_eci_rad_s": closing_rate,
        "unbounded_reference_rate_eci_rad_s": unbounded_rate,
        "rate_limited_reference_rate_eci_rad_s": limited_rate,
        "unbounded_reference_acceleration_eci_rad_s2": unbounded_acceleration,
        "reference_acceleration_eci_rad_s2": acceleration, "reference_rate_eci_rad_s": rate,
        "requested_q_body_to_eci": requested,
        "reference_rate_body_rad_s": _rotate(_inverse(actual), rate),
        "reference_acceleration_body_rad_s2": _rotate(_inverse(actual), acceleration)}
    fields = set(vectors) | {"schema", "policy_id", "time_s", "interval_s", "maximum_interval_s",
        "previous_time_s", "previous_raw_goal_time_s", "goal_rate_clipped", "closing_rate_bound_rad_s",
        "maximum_reference_rate_rad_s", "maximum_reference_acceleration_rad_s2", "rate_clipped",
        "acceleration_clipped", "first_update", "initialized_at_current_time", "reference_kinematics",
        "closing_rate_law", "request_is_execution", "actual_state_assigned"}
    _require(set(item) == fields
             and item["reference_kinematics"] == "constant-rate quaternion increments; inertial backward-difference acceleration"
             and item["closing_rate_law"] == "v*dt + v^2/(2*amax) <= shortest goal error; causal raw-goal-rate feedforward",
             "closed_existing_governor_law")
    for key, value in vectors.items():
        _compare(item.get(key), value, absolute=1e-10)
    _recorded_clipping(item, ceiling, maximum)
    _require(_near(item.get("closing_rate_bound_rad_s"), bound, absolute=1e-10)
             and _norm(rate) <= ceiling + 1e-10 and _norm(acceleration) <= maximum + 1e-10,
             "governor_clipping_arithmetic")
    return {"quaternion": requested, "rate_eci_rad_s": rate, "raw_goal_q": goal,
            "time_s": when, "raw_goal_time_s": when, "first_update": False}


_MANIFEST = {"artifact_id", "relative_path", "format", "bytes", "sha256", "raw_json_sha256", "persisted_before_analysis"}
_REQUEST = {"kind", "index", "sweep", "node", "stage", "q", "rate_eci_rad_s",
            "acceleration_eci_rad_s2", "fuel_kg", "reference_sample", "hypothetical_not_integrated"}
_PROTOCOL = {"kind", "schema", "configuration", "translation_reference", "origin_context", "profile",
             "catch_profile", "wall_deadline_monotonic_s", "source_map", "source_map_sha256",
             "reference_sha256", "origin_context_sha256", "profile_sha256", "catch_profile_sha256",
             "hypothetical_reference_states_only", "physical_plant_calls", "source_binding_is_caller_assertion",
             "physical_execution", "started_monotonic_s", "initial_resource_status", "source_binding_independently_verified",
             "mass_kinematics_convention"}
_FALSE = {"actual_state_assigned", "physics_coefficients_changed", "gain_or_gate_changes",
          "global_optimality_established", "joint_reference_feasibility_established", "candidate_plant_admitted",
          "arrival_admitted", "support_admitted", "physical_execution"}
_RECORD = {"kind", "schema", "origin_artifact", "sweeps", "current_partial_nodes", "query_artifacts", "counters",
           "failure", "partial_sweep_index", "partial_node_index", "incomplete_io", "reference_sha256",
           "origin_context_sha256", "source_map_sha256", "wall_seconds", "hypothetical_reference_states_only",
           "physical_plant_calls", "final_resource_status_before_summary", "mass_kinematics_convention"} | _FALSE
_RECORD_V2 = _RECORD | {"failure_category", "failure_stage", "computation_completed"}
_FAILURE_STAGES = {"resource_check", "resource_status_contract", "attempt_record_persistence",
    "static_budget_guard", "static_model_evaluation", "failed_model_record_persistence",
    "raw_model_record_persistence", "reference_geometry_or_iteration"}
_IO_ERROR_NAMES = {"StaticArtifactLimit", "OSError", "BlockingIOError", "ChildProcessError",
    "ConnectionError", "BrokenPipeError", "ConnectionAbortedError", "ConnectionRefusedError",
    "ConnectionResetError", "FileExistsError", "FileNotFoundError", "InterruptedError",
    "IsADirectoryError", "NotADirectoryError", "PermissionError", "ProcessLookupError", "TimeoutError"}
_NODE = {"index", "time_s", "raw_goal_q_body_to_eci", "governed_request_q_body_to_eci",
         "inverse_converged", "low_force_anchor_unresolved", "updates", "conditioned_goal_q_body_to_eci",
         "conditioned_frame_diagnostics", "tracking_receipt", "tracking_actual_inputs_are_hypothetical",
         "governed_load_query", "nominal_force_cone_passed", "inverse_last_query",
         "nominal_force_cone_is_model_feasibility_certificate"}
_NODE_V3 = _NODE | {"positive_axial_branch"}
_COUNTERS = {"model_queries_attempted", "model_queries_completed", "model_queries_failed",
             "model_queries_skipped_after_durable_attempt", "sweeps_attempted", "sweeps_completed",
             "updates_attempted", "line_search_queries"}
_COUNTERS_V3 = _COUNTERS | {"positive_axial_branch_probes"}
_RESOURCE_MINIMUM = {"free_bytes", "maximum_stored_bytes", "minimum_free_bytes", "persisted_bytes"}
_RESOURCE_FULL = _RESOURCE_MINIMUM | {"summary_reserve_bytes", "artifact_bytes", "ledger_bytes",
    "artifact_count", "free_space_guard_passed", "query_storage_available", "model_query_allowed",
    "final_summary_written", "handles_closed", "stop_reason"}


def _resource(status, *, initial=False):
    _require(type(status) is dict and set(status) in (_RESOURCE_MINIMUM, _RESOURCE_FULL)
             and all(type(status[k]) is int and status[k] >= 0 for k in _RESOURCE_MINIMUM)
             and 0 < status["maximum_stored_bytes"] <= 100 * 1024 * 1024
             and status["minimum_free_bytes"] >= 400 * 1024 * 1024
             and status["persisted_bytes"] <= status["maximum_stored_bytes"], "bounded_caller_resource_status")
    if initial:
        _require(status["free_bytes"] >= status["minimum_free_bytes"], "initial_free_space_guard")
    if set(status) == _RESOURCE_FULL:
        _require(all(type(status[k]) is int and status[k] >= 0 for k in
                     ("summary_reserve_bytes", "artifact_bytes", "ledger_bytes", "artifact_count"))
                 and 0 < status["summary_reserve_bytes"] < status["maximum_stored_bytes"]
                 and status["artifact_bytes"] + status["ledger_bytes"] == status["persisted_bytes"]
                 and status["free_space_guard_passed"] is (status["free_bytes"] >= status["minimum_free_bytes"])
                 and status["query_storage_available"] is
                    (status["persisted_bytes"] < status["maximum_stored_bytes"] - status["summary_reserve_bytes"])
                 and all(type(status[k]) is bool for k in ("model_query_allowed", "final_summary_written", "handles_closed"))
                 and (status["stop_reason"] is None or type(status["stop_reason"]) is str),
                 "resource_byte_accounting_and_limits")
        allowed = (not status["final_summary_written"] and not status["handles_closed"]
                   and status["stop_reason"] is None and status["free_space_guard_passed"]
                   and status["query_storage_available"])
        _require(status["model_query_allowed"] is allowed, "resource_model_gate_arithmetic")
        return True
    return False


def _hash(value, size=64):
    return type(value) is str and len(value) == size and all(c in "0123456789abcdef" for c in value)


def _artifact(manifest, corpus):
    _require(type(manifest) is dict and set(manifest) == _MANIFEST and _hash(manifest["artifact_id"], 32)
             and manifest["format"] in ("json", "json.gz") and type(manifest["bytes"]) is int
             and manifest["bytes"] > 0 and _hash(manifest["sha256"]) and _hash(manifest["raw_json_sha256"])
             and manifest["persisted_before_analysis"] is True
             and manifest["relative_path"] == manifest["artifact_id"] + "." + manifest["format"],
             "closed_opaque_persisted_artifact")
    payload = corpus.get(manifest["artifact_id"])
    _require(type(payload) is dict and digest(payload) == manifest["raw_json_sha256"], "persisted_raw_json_binding")
    return payload


def _same(actual, expected, reason):
    if type(expected) is dict:
        _require(type(actual) is dict and set(actual) == set(expected), reason)
        for key in expected:
            _same(actual[key], expected[key], reason)
    elif type(expected) is list:
        if all(_number(x) for x in expected):
            _compare(actual, expected, absolute=1e-10)
        else:
            _require(type(actual) is list and len(actual) == len(expected), reason)
            for a, b in zip(actual, expected):
                _same(a, b, reason)
    elif type(expected) in (float, int) and type(expected) is not bool:
        _require(_near(actual, expected, absolute=1e-10), reason)
    else:
        _require(actual == expected and type(actual) is type(expected), reason)


def _failure_classification(record, persisted_failed):
    """V2 labels are constrained by retained error evidence, not success credit."""
    failure, category, stage = (record[key] for key in ("failure", "failure_category", "failure_stage"))
    completed = record["computation_completed"]
    _require(type(completed) is bool, "boolean_static_computation_completion")
    if failure is None:
        _require(category is None and stage is None and completed is True
                 and record["counters"]["sweeps_completed"] == 3,
                 "completed_static_computation_has_no_failure")
        return
    _require(completed is False and category in ("budget", "resource_or_io", "recorded_model_failure", "implementation")
             and stage in _FAILURE_STAGES, "closed_static_failure_classification")
    incomplete = record["incomplete_io"]
    if category == "budget":
        _require(failure == "static_wall_or_query_budget_exhausted" and stage == "static_budget_guard"
                 and record["counters"]["model_queries_failed"] == 0,
                 "budget_failure_requires_budget_guard")
    elif category == "recorded_model_failure":
        _require(stage == "static_model_evaluation" and persisted_failed == 1 and incomplete is None,
                 "model_failure_requires_persisted_model_error")
    else:
        _require(failure != "static_wall_or_query_budget_exhausted" and stage != "static_model_evaluation"
                 and persisted_failed == 0, "nonmodel_failure_cannot_relabel_model_or_budget")
        _require((failure in _IO_ERROR_NAMES) is (category == "resource_or_io"),
                 "resource_and_implementation_error_classes_distinct")
    if incomplete is not None:
        expected_stages = {
            "model_failed_before_failure_persist": {"failed_model_record_persistence"},
            "model_completed_before_raw_persist": {"raw_model_record_persistence"},
            "after_attempt_before_model": {"resource_check", "resource_status_contract", "static_budget_guard"},
        }
        _require(stage in expected_stages[incomplete["stage"]], "incomplete_io_matches_failure_stage")
        if incomplete["stage"] == "model_failed_before_failure_persist":
            _require(category in ("resource_or_io", "implementation") and persisted_failed == 0,
                     "unpersisted_model_failure_is_not_recorded_model_failure")


def _node(node, queries, previous_raw, assumption, frame, tracker, reference, snapshot, profile, catch, fuel, corpus,
          *, schema=SCHEMA):
    _require(schema in (SCHEMA, SCHEMA_V2, SCHEMA_V3), "known_static_node_revision")
    increment = _increment if schema == SCHEMA else _increment_continuous
    node_fields = _NODE_V3 if schema == SCHEMA_V3 else _NODE
    _require(type(node) is dict and set(node) == node_fields and type(node["index"]) is int
             and node["tracking_actual_inputs_are_hypothetical"] is True
             and node["nominal_force_cone_is_model_feasibility_certificate"] is False,
             "closed_hypothetical_node")
    position = 0

    def take(stage, q, rate, acceleration):
        nonlocal position
        _require(position < len(queries), "missing_stored_model_branch_query")
        manifest, envelope, request = queries[position]
        position += 1
        _require(request["stage"] == stage and request["reference_sample"]["time_s"] == node["time_s"]
                 and request["fuel_kg"] == fuel, "ordered_model_branch_clock_and_fuel")
        for field, vector in (("q", q), ("rate_eci_rad_s", rate), ("acceleration_eci_rad_s2", acceleration)):
            _compare(request[field], vector, absolute=1e-10)
        return manifest, envelope["result"]

    raw_q, history, converged, low_force = previous_raw, [], False, False
    branch_receipt = {"attempted": False, "from_query": None, "probe_query": None,
        "positive_axial_passed": None, "applied": False,
        "proposal_source": "body_pitch_pi_hypothesis", "physical_state_assigned": False}
    rate, acceleration = assumption["rate"], assumption["acceleration"]
    last = None
    for update in range(8):
        last, raw = take("base", raw_q, rate, acceleration)
        force = raw["required_main_force_body_n"]
        tolerance = math.sqrt(math.ulp(1.0)) * max(1.0, raw["mass_kg"] * 9.81)
        residual = math.hypot(*force[:2])
        if _norm(force) <= tolerance:
            low_force = True
            break
        if schema == SCHEMA_V3 and force[2] < 0 and not branch_receipt["attempted"]:
            original = last
            branch_q = _increment_continuous(raw_q, 0.0, math.pi)
            probe_manifest, probe = take("positive_axial_branch_probe", branch_q, rate, acceleration)
            probe_force = probe["required_main_force_body_n"]
            branch_receipt.update(attempted=True, from_query=original, probe_query=probe_manifest,
                positive_axial_passed=probe_force[2] >= 0)
            if _norm(probe_force) <= tolerance or probe_force[2] < 0:
                low_force = _norm(probe_force) <= tolerance
                history.append({"update": update, "status": "positive_axial_branch_unresolved",
                    "base_query": original})
                break
            branch_receipt["applied"] = True
            raw_q, last, raw, force = branch_q, probe_manifest, probe, probe_force
            residual = math.hypot(*force[:2])
        if residual <= tolerance and force[2] >= 0:
            converged = True
            break
        if schema != SCHEMA and residual <= tolerance and force[2] < 0:
            history.append({"update": update, "status": "transverse_stationary_negative_axial",
                "base_query": last, "transverse_residual_n": residual,
                "positive_axial_passed": False, "inverse_convergence_established": False})
            break
        angle = math.sqrt(math.ulp(1.0))
        left = take("jacobian_0", increment(raw_q, angle, 0), rate, acceleration)
        right = take("jacobian_1", increment(raw_q, 0, angle), rate, acceleration)
        a, b = [(trial["required_main_force_body_n"][0] - force[0]) / angle for _, trial in (left, right)]
        c, d = [(trial["required_main_force_body_n"][1] - force[1]) / angle for _, trial in (left, right)]
        determinant = a * d - b * c
        basis = {"update": update, "base_query": last, "jacobian_queries": [left[0], right[0]]}
        if abs(determinant) <= math.sqrt(math.ulp(1.0)) * max(1.0, abs(a * d), abs(b * c)):
            history.append({**basis, "status": "singular_transverse_jacobian"})
            break
        dx, dy = (-d * force[0] + b * force[1]) / determinant, (c * force[0] - a * force[1]) / determinant
        size = math.hypot(dx, dy)
        if size > math.pi / 4:
            dx, dy = dx * (math.pi / 4) / size, dy * (math.pi / 4) / size
        lines, selected = [], None
        for line in range(4):
            trial_q = increment(raw_q, dx / 2**line, dy / 2**line)
            manifest, trial = take("line_search_" + str(line), trial_q, rate, acceleration)
            lines.append(manifest)
            if (math.hypot(*trial["required_main_force_body_n"][:2]) < residual
                    and (schema != SCHEMA_V3 or trial["required_main_force_body_n"][2] >= 0)):
                selected = trial_q
                break
        history.append({**basis, "status": "accepted" if selected else "line_search_no_decrease",
            "transverse_residual_n": residual, "bounded_update_body_rad": [dx, dy], "line_search_queries": lines})
        if selected is None:
            break
        raw_q = selected
    _same(node["updates"], history, "newton_history_and_line_selection")
    if schema == SCHEMA_V3:
        _same(node["positive_axial_branch"], branch_receipt, "one_body_pitch_pi_probe_before_branch_selection")
    _require(node["inverse_last_query"] == last and node["inverse_converged"] is converged
             and node["low_force_anchor_unresolved"] is low_force, "inverse_status_is_source_bound")
    _compare(node["raw_goal_q_body_to_eci"], raw_q, absolute=1e-10)
    frame, diagnostic = _conditioned(frame, raw_q, node["time_s"], profile)
    _same(node["conditioned_frame_diagnostics"], diagnostic, "causal_geographic_roll_reacquisition")
    _compare(node["conditioned_goal_q_body_to_eci"], frame["quaternion"], absolute=1e-10)
    tracker = _tracking(node["tracking_receipt"], tracker, frame["quaternion"], assumption["q"],
        _rotate(_inverse(assumption["q"]), rate), node["time_s"], profile)
    _compare(node["governed_request_q_body_to_eci"], tracker["quaternion"], absolute=1e-10)
    manifest, governed = take("governed_recheck", tracker["quaternion"], tracker["rate_eci_rad_s"],
        node["tracking_receipt"]["reference_acceleration_eci_rad_s2"])
    stored = node["governed_load_query"]
    _require(type(stored) is dict and set(stored) == _LOAD_FIELDS | {"raw_artifact", "attempted_artifact"}
             and stored["raw_artifact"] == manifest,
             "governed_load_is_the_persisted_post_clip_query")
    envelope = _artifact(manifest, corpus)
    _require(stored["attempted_artifact"] == envelope["attempted"]
             and {k: v for k, v in stored.items() if k in _LOAD_FIELDS} == governed,
             "stored_governed_load_duplicate_binding")
    force = governed["required_main_force_body_n"]
    gx = math.radians(profile["actuators"]["max_gimbal_deg"])
    passed = force[2] >= 0 and abs(force[0]) <= math.tan(gx) * force[2] and abs(force[1]) <= math.tan(gx) / math.cos(gx) * force[2]
    _require(position == len(queries) and node["nominal_force_cone_passed"] is passed,
             "nominal_post_governor_cone_is_only_partial")
    return raw_q, frame, tracker, _norm(force) / (profile["booster"]["engine_isp_s"] * 9.80665)


def _check(record, protocol, reference, snapshot, profile, catch, corpus, prior, source_run, source_map):
    for value in (record, protocol, reference, snapshot, profile, catch, corpus):
        _json(value, maximum_nodes=32_000_000)
    checked = verify_fixed_terminal_reference(reference, snapshot, profile, catch,
        prior_checkpoint=prior, source_run=source_run)
    _require(checked["arithmetic_passed"], "fixed_translation_arithmetic_contract")
    _require(type(protocol) is dict and protocol.get("schema") in (SCHEMA, SCHEMA_V2, SCHEMA_V3),
             "known_static_protocol_revision")
    schema = protocol["schema"]
    configuration = {SCHEMA: CONFIG, SCHEMA_V2: CONFIG_V2, SCHEMA_V3: CONFIG_V3}[schema]
    record_fields = _RECORD if schema == SCHEMA else _RECORD_V2
    counter_fields = _COUNTERS_V3 if schema == SCHEMA_V3 else _COUNTERS
    maximum_queries = configuration["maximum_model_queries"]
    _require(type(protocol) is dict and set(protocol) == _PROTOCOL
             and protocol["kind"] == "coupled_static_protocol"
             and protocol["configuration"] == configuration and protocol["translation_reference"] == reference
             and protocol["origin_context"] == snapshot and protocol["profile"] == profile
             and protocol["catch_profile"] == catch and protocol["hypothetical_reference_states_only"] is True
             and protocol["physical_plant_calls"] == 0 and type(protocol["physical_plant_calls"]) is int
             and protocol["source_binding_is_caller_assertion"] is True and protocol["physical_execution"] is False
             and protocol["source_binding_independently_verified"] is False
             and protocol["mass_kinematics_convention"] == "nodewise_frozen_mass_com_derivatives_zero"
             and _number(protocol["started_monotonic_s"]) and _number(protocol["wall_deadline_monotonic_s"])
             and 0 < protocol["wall_deadline_monotonic_s"] - protocol["started_monotonic_s"] <= 120,
             "closed_static_protocol")
    _same(protocol["configuration"], configuration, "closed_configuration_types")
    full_resources = _resource(protocol["initial_resource_status"], initial=True)
    sources = protocol["source_map"]
    _require(type(sources) is dict and sources and all(type(k) is str and k and _hash(v) for k, v in sources.items())
             and (source_map is None or source_map == sources), "frozen_source_map_binding")
    for key, value in (("reference_sha256", reference), ("origin_context_sha256", snapshot),
                       ("profile_sha256", profile), ("catch_profile_sha256", catch), ("source_map_sha256", sources)):
        _require(protocol[key] == digest(value), "static_protocol_source_digest")
    environment = profile.get("environment")
    if environment is not None:
        _require(type(environment) is dict and set(environment) == {"wind"}, "closed_static_environment")
        wind = environment["wind"]
        _require(wind is None or (type(wind) is dict and _vector(wind.get("mean_enu_mps"))
            and _vector(wind.get("gust_amplitude_enu_mps")) and all(x == 0 for x in
                wind["mean_enu_mps"] + wind["gust_amplitude_enu_mps"])), "nonzero_static_wind_unsupported")
    _require(type(record) is dict and set(record) == record_fields | {"raw_artifact", "raw_persisted_before_admission_analysis"}
             and record["schema"] == schema and record["kind"] == "coupled_static_complete_or_partial"
             and record["hypothetical_reference_states_only"] is True and record["physical_plant_calls"] == 0
             and type(record["physical_plant_calls"]) is int
             and all(record[k] is False for k in _FALSE)
             and record["mass_kinematics_convention"] == "nodewise_frozen_mass_com_derivatives_zero"
             and record["raw_persisted_before_admission_analysis"] is True
             and _number(record["wall_seconds"]) and record["wall_seconds"] >= 0,
             "closed_static_record_and_no_admission")
    for key in ("reference_sha256", "origin_context_sha256", "source_map_sha256"):
        _require(record[key] == protocol[key], "static_record_binding")
    _require(_artifact(record["origin_artifact"], corpus) == protocol
             and _artifact(record["raw_artifact"], corpus) ==
                {k: v for k, v in record.items() if k in record_fields}, "raw_persisted_before_branch_use")
    counters = record["counters"]
    full_resources = _resource(record["final_resource_status_before_summary"]) and full_resources
    _require(type(counters) is dict and set(counters) == counter_fields
             and all(type(x) is int and x >= 0 for x in counters.values())
             and counters["model_queries_attempted"] <= maximum_queries
             and counters["model_queries_completed"] + counters["model_queries_failed"]
                 + counters["model_queries_skipped_after_durable_attempt"] <= counters["model_queries_attempted"]
             and 0 <= counters["sweeps_completed"] <= counters["sweeps_attempted"] <= 3
             and counters["updates_attempted"] <= 3 * 101 * 8
             and counters["line_search_queries"] <= 3 * 101 * 8 * 4,
             "bounded_attempts_include_failures")
    if schema == SCHEMA_V3:
        _require(counters["positive_axial_branch_probes"] <= 303, "bounded_body_pitch_pi_probes")
    _require(type(corpus) is dict and len(corpus) <= 2 * maximum_queries + 4
             and all(_hash(key, 32) for key in corpus) and type(record["query_artifacts"]) is list
             and len(record["query_artifacts"]) <= maximum_queries, "bounded_static_corpus")
    queries, completed, failed, requests = {}, 0, 0, {}
    seen = set()
    referenced = {record["origin_artifact"]["artifact_id"], record["raw_artifact"]["artifact_id"]}
    for manifest in record["query_artifacts"]:
        envelope = _artifact(manifest, corpus)
        _require(manifest["artifact_id"] not in seen, "unique_query_artifact")
        seen.add(manifest["artifact_id"])
        referenced.update((manifest["artifact_id"], envelope["attempted"]["artifact_id"]))
        request = _artifact(envelope.get("attempted"), corpus)
        _require(type(request) is dict and set(request) == _REQUEST and request["kind"] == "static_model_attempted"
                 and request["hypothetical_not_integrated"] is True and type(request["index"]) is int
                 and request["index"] == len(requests) + 1
                 and type(request["sweep"]) is int and 0 <= request["sweep"] < 3
                 and type(request["node"]) is int and 0 <= request["node"] < 101,
                 "durable_attempt_before_static_model")
        requests[request["index"]] = request
        if envelope["kind"] == "static_model_raw":
            _require(set(envelope) == {"kind", "attempted", "result"}, "closed_static_model_raw")
            _load(envelope["result"], request, reference, snapshot["state"], profile, catch)
            completed += 1
            queries.setdefault((request["sweep"], request["node"]), []).append((manifest, envelope, request))
        else:
            _require(envelope["kind"] == "static_model_failed"
                     and set(envelope) == {"kind", "attempted", "error_class", "counters"}
                     and type(envelope["error_class"]) is str and envelope["error_class"].isidentifier()
                     and record["failure"] is not None, "retained_failed_static_model")
            failed += 1
            declared = envelope["counters"]
            _require(type(declared) is dict and set(declared) == counter_fields
                     and declared["model_queries_attempted"] == request["index"]
                     and declared["model_queries_completed"] == completed
                     and declared["model_queries_failed"] == failed
                     and record["failure"] == envelope["error_class"], "failed_model_counter_prefix")
    persisted_failed = failed
    incomplete = record["incomplete_io"]
    if incomplete is not None:
        incomplete_fields = {"after_attempt_before_model": {"stage", "attempted_artifact", "request"},
            "model_completed_before_raw_persist": {"stage", "attempted_artifact", "request", "result"}}
        if schema != SCHEMA:
            incomplete_fields["model_failed_before_failure_persist"] = {
                "stage", "attempted_artifact", "request", "error_class"}
        _require(type(incomplete) is dict and incomplete.get("stage") in incomplete_fields
            and set(incomplete) == incomplete_fields[incomplete["stage"]]
            and record["failure"] is not None, "retained_incomplete_io")
        request = _artifact(incomplete["attempted_artifact"], corpus)
        referenced.add(incomplete["attempted_artifact"]["artifact_id"])
        _require(set(request) == _REQUEST and request["kind"] == "static_model_attempted"
                 and request["hypothetical_not_integrated"] is True
                 and type(request["index"]) is int and request["index"] == len(requests) + 1
                 and type(request["sweep"]) is int and 0 <= request["sweep"] < 3
                 and type(request["node"]) is int and 0 <= request["node"] < 101
                 and request == incomplete["request"],
                 "incomplete_durable_attempt_binding")
        requests[request["index"]] = request
        if incomplete["stage"] == "model_completed_before_raw_persist":
            _require("result" in incomplete, "missing_completed_model_before_io_failure")
            _load(incomplete["result"], request, reference, snapshot["state"], profile, catch)
            completed += 1
        elif incomplete["stage"] == "model_failed_before_failure_persist":
            _require(type(incomplete["error_class"]) is str and incomplete["error_class"].isidentifier()
                     and record["failure_stage"] == "failed_model_record_persistence",
                     "original_model_failure_retained_before_failed_record_io")
            failed += 1
    _require(completed == counters["model_queries_completed"] and failed == counters["model_queries_failed"]
             and len(requests) <= counters["model_queries_attempted"] <= len(requests) + (record["failure"] is not None),
             "all_static_calls_accounted")
    extras = set(corpus) - referenced
    _require(not extras or (record["failure"] is not None and len(extras) == 1
        and corpus[next(iter(extras))].get("kind") == "static_model_attempted"
        and set(corpus[next(iter(extras))]) == _REQUEST
        and corpus[next(iter(extras))].get("index") == counters["model_queries_attempted"]),
        "no_hidden_unreferenced_static_calls")
    partial_allowance = int(record["failure"] is not None)
    bases = sum(request["stage"] == "base" for request in requests.values())
    lines = sum(type(request["stage"]) is str and request["stage"].startswith("line_search_") for request in requests.values())
    _require(bases <= counters["updates_attempted"] <= bases + partial_allowance
             and lines <= counters["line_search_queries"] <= lines + partial_allowance
             and failed <= 1 and counters["model_queries_skipped_after_durable_attempt"] <= 1,
             "newton_and_line_attempt_counters_recomputed")
    if schema == SCHEMA_V3:
        probes = [request for request in requests.values() if request["stage"] == "positive_axial_branch_probe"]
        keys = [(request["sweep"], request["node"]) for request in probes]
        _require(len(keys) == len(set(keys))
                 and len(probes) <= counters["positive_axial_branch_probes"] <= len(probes) + partial_allowance,
                 "one_durable_body_pitch_pi_probe_per_node_and_attempt_counter")
    else:
        _require(not any(request["stage"] == "positive_axial_branch_probe" for request in requests.values()),
                 "legacy_corpus_cannot_import_new_branch_family")
    sweeps = record["sweeps"]
    partial = record["current_partial_nodes"]
    _require(type(sweeps) is list and len(sweeps) == counters["sweeps_completed"]
             and type(partial) is list and len(partial) <= 101
             and (not partial or counters["sweeps_attempted"] == counters["sweeps_completed"] + 1),
             "bounded_unique_completed_and_partial_sweeps")
    if counters["sweeps_attempted"] > counters["sweeps_completed"]:
        _require(record["partial_sweep_index"] == counters["sweeps_completed"]
                 and record["partial_node_index"] == len(partial), "exact_unfinished_sweep_and_node")
    else:
        _require(record["partial_sweep_index"] is None and record["partial_node_index"] is None,
                 "no_repeated_completed_sweep_as_partial")
    carried = snapshot["context"]["reference_tracker"]
    _require(type(carried) is dict, "exact_carried_tracker_required")
    assumptions = [{"q": _multiply(_exp([0.0, 0.0, 7.292115e-5 * reference["duration_s"] * i / 100]), carried["quaternion"]),
                    "rate": [0.0, 0.0, 7.292115e-5], "acceleration": [0.0, 0.0, 0.0]} for i in range(101)]
    node_count = 0
    for index in range(len(sweeps) + bool(partial)):
        full = index < len(sweeps)
        sweep = sweeps[index] if full else {"sweep": index, "nodes": partial}
        _require(type(sweep) is dict and set(sweep) == ({"sweep", "nodes", "nominal_remaining_fuel_kg"} if full else {"sweep", "nodes"})
                 and type(sweep["sweep"]) is int and sweep["sweep"] == index
                 and type(sweep["nodes"]) is list and (len(sweep["nodes"]) == 101 if full else len(sweep["nodes"]) <= 101),
                 "ordered_complete_or_partial_sweep")
        frame, tracker = dict(snapshot["context"]["conditioned_reference"]), dict(carried)
        previous_raw, fuel = carried["quaternion"], snapshot["state"]["propellant_kg"]
        for i, node in enumerate(sweep["nodes"]):
            _require(node["index"] == i and node["time_s"] == snapshot["state"]["time_s"] + reference["duration_s"] * i / 100,
                     "fixed_translation_node_clock")
            previous_raw, frame, tracker, flow = _node(node, queries.pop((index, i), []), previous_raw,
                assumptions[i], frame, tracker, reference, snapshot, profile, catch, fuel, corpus,
                schema=schema)
            node_count += 1
            if i + 1 < 101:
                fuel = max(0.0, fuel - flow * reference["duration_s"] / 100)
        if full:
            _require(_near(sweep["nominal_remaining_fuel_kg"], fuel), "nominal_fuel_is_only_discrete_force_integral")
            assumptions = [{"q": node["governed_request_q_body_to_eci"],
                "rate": node["tracking_receipt"]["reference_rate_eci_rad_s"],
                "acceleration": node["tracking_receipt"]["reference_acceleration_eci_rad_s2"]} for node in sweep["nodes"]]
    if record["failure"] is None:
        _require(counters["sweeps_completed"] == counters["sweeps_attempted"] == 3
                 and not partial and incomplete is None and not queries and failed == 0
                 and counters["model_queries_attempted"] == completed
                 and counters["model_queries_skipped_after_durable_attempt"] == 0
                 and record["wall_seconds"] <= 120, "complete_static_corpus_without_hidden_failures")
    else:
        _require(type(record["failure"]) is str and len(record["failure"]) <= 100
                 and (record["failure"] == "static_wall_or_query_budget_exhausted" or record["failure"].isidentifier())
                 and all(k == (counters["sweeps_completed"], len(partial)) for k in queries),
                 "only_last_partial_node_may_be_unfinished")
    if schema != SCHEMA:
        _failure_classification(record, persisted_failed)
    return node_count, checked["past_source_anchors_bound"], full_resources


def verify_coupled_terminal_reference(record, protocol, reference, snapshot, profile, catch_config, *,
                                      query_corpus, prior_checkpoint=None, source_run=None, expected_source_map=None):
    """Check arithmetic record integrity; a partial or bug is not completed work."""
    result = {"schema": "missionos.starship_coupled_terminal_reference_verification.v1", "passed": False,
        "arithmetic_passed": False, "arithmetic_record_integrity_passed": False,
        "source_bound_arithmetic_passed": False, "physical_invocation_admitted": False,
        "verification_scope": "coupled_static_arithmetic_not_execution_admission",
        "computation_completed": False, "failure_category": None, "failure_stage": None,
        "issues": [], "hypothetical_nodes_checked": 0, "past_source_anchors_bound": False,
        "configured_panel_load_arithmetic_checked": False, "causal_governor_arithmetic_checked": False,
        "caller_resource_budget_arithmetic_checked": False,
        "compressed_file_bytes_independently_verified": False, "os_persistence_authenticated": False,
        "source_authenticated": False, "model_observers_invoked": 0, "physical_plant_integrations_performed": 0,
        "finite_actuator_feasibility_established": False, "continuous_fuel_feasibility_established": False,
        "joint_reference_feasibility_established": False, "candidate_plant_admitted": False,
        "mass_kinematics_convention": "nodewise_frozen_mass_com_derivatives_zero",
        "arrival_admitted": False, "support_admitted": False, "physical_execution": False}
    try:
        count, anchors, resources = _check(record, protocol, reference, snapshot, profile, catch_config, query_corpus,
            prior_checkpoint, source_run, expected_source_map)
        computation_completed = record["failure"] is None and record["counters"]["sweeps_completed"] == 3
        result.update(passed=True, arithmetic_passed=True, arithmetic_record_integrity_passed=True,
            source_bound_arithmetic_passed=anchors and expected_source_map is not None,
            computation_completed=computation_completed,
            failure_category=record.get("failure_category", "legacy_unclassified" if record["failure"] else None),
            failure_stage=record.get("failure_stage"),
            hypothetical_nodes_checked=count, past_source_anchors_bound=anchors,
            configured_panel_load_arithmetic_checked=record["counters"]["model_queries_completed"] > 0,
            causal_governor_arithmetic_checked=count > 0,
            caller_resource_budget_arithmetic_checked=resources)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError, ZeroDivisionError, AttributeError):
        result["issues"].append("Invalid coupled static-reference source/corpus/arithmetic contract")
    return result


def verify_source_bound_coupled_terminal_reference(record, protocol, reference, snapshot, profile, catch_config, *,
        query_corpus, prior_checkpoint=None, source_run=None, expected_source_map=None):
    """Require retained anchors and the caller's explicit matching source map.

    Source-bound arithmetic can validate retained partial/error records. It
    grants no computation-completion, finite-plant, arrival or support credit.
    """
    result = verify_coupled_terminal_reference(record, protocol, reference, snapshot, profile, catch_config,
        query_corpus=query_corpus, prior_checkpoint=prior_checkpoint, source_run=source_run,
        expected_source_map=expected_source_map)
    result["verification_scope"] = "source_bound_coupled_static_arithmetic_not_execution_admission"
    result["passed"] = result["source_bound_arithmetic_passed"]
    if result["arithmetic_passed"] and not result["source_bound_arithmetic_passed"]:
        result["issues"].append("Retained source run, prior checkpoint and matching expected source map are required")
    return result
