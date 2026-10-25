"""Pure prospective pulse-impulse compensation, never achieved-state input.

This ideal-command approximation predicts a full 15-second minimum-throttle
pulse along the fixed desired reference. It omits finite spool, shutdown tail,
attitude error, early capture completion and other engines' forces.
"""

import math

from . import starship_physics as env, starship_sixdof as dyn
from .starship_booster_lowq_capture import _checked_reference, _number

PLANNED_DURATION_S = 15.0
QUADRATURE_INTERVAL_S = 0.25
QUADRATURE_INTERVALS = 60


def _vector(value, name):
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError("invalid_planned_capture_impulse_" + name)
    return tuple(_number(x, name) for x in value)


def planned_capture_impulse(reference, *, cutoff_time_s, mass_kg, propellant_kg, center_engines,
                            up_eci, legacy_desired_horizontal_mps, maximum_horizontal_speed_mps):
    """Return corrected horizontal guidance and its reproducible request receipt.

    ``cutoff_time_s`` is the current snapshot time treated as a prospective
    cutoff. The caller re-evaluates this open-loop approximation from its
    current mass and geometry; the actual future cutoff is not read or set.
    """
    reference = _checked_reference(reference)
    clock = _number(cutoff_time_s, "prospective_cutoff_time_s")
    mass = _number(mass_kg, "mass_kg", positive=True)
    fuel = _number(propellant_kg, "propellant_kg", positive=True)
    if fuel >= mass:
        raise ValueError("planned_capture_impulse_requires_positive_dry_mass")
    maximum = _number(maximum_horizontal_speed_mps, "maximum_horizontal_speed_mps", positive=True)
    up = _vector(up_eci, "up_eci")
    original = _vector(legacy_desired_horizontal_mps, "legacy_desired_horizontal_mps")
    if not math.isclose(math.hypot(*up), 1.0, rel_tol=0.0, abs_tol=1e-8):
        raise ValueError("planned_capture_impulse_requires_unit_up")
    if abs(env.dot(original, up)) > 1e-6 or env.norm(original) > maximum + 1e-6:
        raise ValueError("planned_capture_impulse_requires_legacy_bounded_horizontal_request")
    if type(center_engines) not in (tuple, list) or len(center_engines) != 3:
        raise ValueError("planned_capture_impulse_requires_original_three_centers")
    checked = []
    for i, engine in enumerate(center_engines):
        if (type(engine) is not dict or set(engine) != {
                "index", "name", "available", "maximum_thrust_n", "minimum_throttle", "isp_s", "direction_body"}
                or type(engine["index"]) is not int or engine["index"] != i
                or engine["name"] != f"booster_main_{i}" or type(engine["available"]) is not bool):
            raise ValueError("planned_capture_impulse_requires_actual_center_inventory")
        facts = {**engine,
            "maximum_thrust_n": _number(engine["maximum_thrust_n"], "maximum_thrust_n", positive=True),
            "minimum_throttle": _number(engine["minimum_throttle"], "minimum_throttle", positive=True),
            "isp_s": _number(engine["isp_s"], "isp_s", positive=True),
            "direction_body": list(_vector(engine["direction_body"], "direction_body"))}
        if facts["minimum_throttle"] > 1.0:
            raise ValueError("invalid_planned_capture_impulse_minimum_throttle")
        if not math.isclose(math.hypot(*facts["direction_body"]), 1.0, rel_tol=0.0, abs_tol=1e-8):
            raise ValueError("planned_capture_impulse_requires_unit_engine_direction")
        checked.append(facts)
    selected = next((facts for facts in checked if facts["available"]), None)
    thrust = 0.0 if selected is None else selected["maximum_thrust_n"] * selected["minimum_throttle"]
    mdot = 0.0 if selected is None else thrust / (selected["isp_s"] * env.STANDARD_GRAVITY_MPS2)
    final_mass = mass - mdot * PLANNED_DURATION_S
    final_fuel = fuel - mdot * PLANNED_DURATION_S
    if not math.isfinite(final_mass) or final_mass <= 0.0 or final_fuel <= 0.0:
        raise ValueError("planned_capture_impulse_requires_positive_planned_mass")
    delta = [0.0, 0.0, 0.0]
    for i in range(QUADRATURE_INTERVALS + 1):
        elapsed = i * QUADRATURE_INTERVAL_S
        desired = reference.at(clock + elapsed, actual_cutoff_time_s=clock)
        direction = dyn.rotate(desired.q_body_to_eci,
            (0.0, 0.0, 1.0) if selected is None else tuple(selected["direction_body"]))
        weight = 0.5 if i in (0, QUADRATURE_INTERVALS) else 1.0
        acceleration = thrust / (mass - mdot * elapsed)
        for axis in range(3):
            delta[axis] += weight * QUADRATURE_INTERVAL_S * acceleration * direction[axis]
    delta = tuple(float(x) for x in delta)
    horizontal = env.add(delta, env.scale(up, -env.dot(delta, up)))
    uncapped = env.add(original, env.scale(horizontal, -1.0))
    compensated = env.scale(uncapped, min(1.0, maximum / max(env.norm(uncapped), 1e-12)))
    if not all(math.isfinite(x) for vector in (delta, horizontal, uncapped, compensated) for x in vector):
        raise ValueError("nonfinite_planned_capture_impulse_request")
    receipt = {
        "schema": "missionos.booster_planned_capture_impulse.v1",
        "prospective_cutoff_time_s": clock,
        "initial_mass_kg": mass,
        "initial_propellant_kg": fuel,
        "center_engines": checked,
        "selected_center_index": None if selected is None else selected["index"],
        "status": "no_available_center" if selected is None else "prospective_commanded_pulse",
        "planned_duration_s": PLANNED_DURATION_S,
        "quadrature_interval_s": QUADRATURE_INTERVAL_S,
        "quadrature_intervals": QUADRATURE_INTERVALS,
        "planned_thrust_n": thrust,
        "planned_mass_flow_kg_s": mdot,
        "planned_final_mass_kg": final_mass,
        "planned_final_propellant_kg": final_fuel,
        "standard_gravity_mps2": env.STANDARD_GRAVITY_MPS2,
        "reference_source_sha256": reference.source_sha256,
        "reference_profile_sha256": reference.profile_sha256,
        "reference_data_sha256": reference.reference_data_sha256,
        "reference_cutoff_time_s": reference.cutoff_time_s,
        "up_eci": list(up),
        "planned_delta_v_eci_mps": list(delta),
        "planned_horizontal_delta_v_eci_mps": list(horizontal),
        "legacy_desired_horizontal_mps": list(original),
        "uncapped_compensated_horizontal_mps": list(uncapped),
        "compensated_desired_horizontal_mps": list(compensated),
        "maximum_horizontal_speed_mps": maximum,
        "approximation": "ideal immediate minimum throttle for full15s; no spool, shutdown tail, early completion, attitude error, gimbal deflection or other-engine forces",
        "quadrature": "endpoint trapezoid of desired-reference thrust/mass in ECI; linear ideal pulse propellant depletion",
        "prospective_cutoff_is_current_snapshot_not_future_truth": True,
        "prediction_is_execution": False,
        "actual_state_assigned": False,
        "physics_limits_changed": False,
        "qualification_refreshed": False,
        "model_calls": 0,
    }
    return tuple(float(x) for x in compensated), receipt
