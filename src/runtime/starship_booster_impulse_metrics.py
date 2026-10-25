"""Independent saved-only checks of a prospective capture-impulse request.

The reference interpolation and coordinate transforms are pure geometry. No
producer, controller, actuator, plant, forecast or model provider is invoked.
The ideal fifteen-second thrust integral is an approximation, never evidence
that a future pulse, trajectory correction or water entry was achieved.
"""

import math

from . import starship_physics as env, starship_sixdof as dyn
from . import starship_sixdof_verifier as saved
from .starship_booster_lowq_capture import load_reference


def _require(condition, detail):
    saved._require(condition, "$", detail)


def _near(actual, expected, detail, tolerance=1e-6):
    if isinstance(expected, (tuple, list)):
        _require(isinstance(actual, (tuple, list)) and len(actual) == len(expected), detail)
        for left, right in zip(actual, expected):
            _near(left, right, detail, tolerance)
        return
    _require(saved._number(actual) and math.isclose(actual, expected, rel_tol=2e-10, abs_tol=tolerance), detail)


def _receipt(actual, expected):
    detail = "Prospective capture impulse differs from actual snapshot, approved site or fixed numerical law"
    if type(expected) is dict:
        _require(type(actual) is dict and set(actual) == set(expected), detail)
        for key, value in expected.items():
            _receipt(actual[key], value)
    elif type(expected) is list:
        _require(type(actual) is list and len(actual) == len(expected), detail)
        for left, right in zip(actual, expected):
            _receipt(left, right)
    elif type(expected) in (bool, int, str) or expected is None:
        _require(type(actual) is type(expected) and actual == expected, detail)
    else:
        _near(actual, expected, detail)


def _horizontal(vector, up):
    return tuple(value-env.dot(vector, up)*axis for value, axis in zip(vector, up))


def _cap(vector, maximum):
    factor = min(1., maximum/max(math.hypot(*vector), 1e-12))
    return tuple(value*factor for value in vector)


def _site_point(expected_site, state, profile):
    # Missing explicit sites retain only the original profile launch target.
    # An approved divert is supplied by the caller that already binds the
    # record's active site to the immutable mission declaration.
    site = expected_site.to_dict() if hasattr(expected_site, "to_dict") else expected_site
    _require(site is None or type(site) is dict and site.get("site_id") in ("capture", "divert"),
        "Capture compensation requires an approved return-site declaration")
    if site is None or site["site_id"] == "capture":
        latitude, longitude, elevation = profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 0.
    else:
        latitude, longitude, elevation = (site[key] for key in ("latitude_deg", "longitude_deg", "elevation_m"))
    return env.surface_state(latitude, longitude, elevation, time_s=state.time_s)


def _rebuild(state, craft, profile, observed, expected_site):
    point = env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg)
    up, _, north = env.local_frame(point)
    relative = env.air_relative_velocity(point)
    target = _site_point(expected_site, state, profile)
    displacement = _horizontal(tuple(a-b for a, b in zip(target.r, state.r_eci_m)), up)
    altitude = env.ecef_to_geodetic(env.eci_to_ecef(state.r_eci_m, state.time_s))[2]
    mass = float(craft.dry_mass_kg+state.propellant_kg)
    _near(observed["mass_kg"], mass, "Capture impulse mass is not the actual retained mass", 1e-8)
    _near(observed["altitude_m"], altitude, "Capture impulse height is not the actual current geometry", 1e-6)
    gravity = env.EARTH_MU_M3_S2/math.hypot(*state.r_eci_m)**2
    vertical = env.dot(relative, up)
    fall = max(30., min(400., (vertical+math.sqrt(vertical*vertical+2*gravity*max(0., altitude)))/gravity))
    configuration = profile.get("booster_return", {})
    maximum = configuration.get("boostback_max_horizontal_speed_mps", 700.)
    original = _cap(tuple(value/fall for value in displacement), maximum)
    centers = [{"index": i, "name": engine.name, "available": actual.available,
        "maximum_thrust_n": float(engine.max_thrust_n), "minimum_throttle": float(engine.min_throttle),
        "isp_s": float(engine.isp_s), "direction_body": list(engine.direction_body)}
        for i, (engine, actual) in enumerate(zip(craft.engines[:3], state.engine_states[:3]))]
    _require(len(centers) == 3 and all(row["name"] == f"booster_main_{i}" and type(row["available"]) is bool
        for i, row in enumerate(centers)), "Capture impulse requires the actual original three center engines")
    selected = next((row for row in centers if row["available"]), None)
    force = 0. if selected is None else selected["maximum_thrust_n"]*selected["minimum_throttle"]
    flow = 0. if selected is None else force/(selected["isp_s"]*env.STANDARD_GRAVITY_MPS2)
    final_mass, final_fuel = mass-15.*flow, state.propellant_kg-15.*flow
    _require(math.isfinite(final_mass) and final_mass > 0 and final_fuel > 0,
        "Prospective full capture pulse consumes the actual available mass or propellant")
    reference = load_reference()
    delta = [0., 0., 0.]
    # Fixed endpoint trapezoid, independently spelled out. This is a thrust
    # quadrature on desired poses, not propagation of any state or actuator.
    for index in range(61):
        elapsed = index/4.
        desired = reference.at(state.time_s+elapsed, actual_cutoff_time_s=state.time_s)
        direction = dyn.rotate(desired.q_body_to_eci, (0., 0., 1.) if selected is None else tuple(selected["direction_body"]))
        weight = .125 if index in (0, 60) else .25
        for axis in range(3):
            delta[axis] += weight*force/(mass-flow*elapsed)*direction[axis]
    horizontal = _horizontal(delta, up)
    uncapped = tuple(a-b for a, b in zip(original, horizontal))
    corrected = _cap(uncapped, maximum)
    receipt = {"schema": "missionos.booster_planned_capture_impulse.v1",
        "prospective_cutoff_time_s": float(state.time_s), "initial_mass_kg": mass,
        "initial_propellant_kg": float(state.propellant_kg), "center_engines": centers,
        "selected_center_index": None if selected is None else selected["index"],
        "status": "no_available_center" if selected is None else "prospective_commanded_pulse",
        "planned_duration_s": 15., "quadrature_interval_s": .25, "quadrature_intervals": 60,
        "planned_thrust_n": force, "planned_mass_flow_kg_s": flow, "planned_final_mass_kg": final_mass,
        "planned_final_propellant_kg": final_fuel, "standard_gravity_mps2": env.STANDARD_GRAVITY_MPS2,
        "reference_source_sha256": reference.source_sha256, "reference_profile_sha256": reference.profile_sha256,
        "reference_data_sha256": reference.reference_data_sha256, "reference_cutoff_time_s": reference.cutoff_time_s,
        "up_eci": list(up), "planned_delta_v_eci_mps": delta,
        "planned_horizontal_delta_v_eci_mps": list(horizontal), "legacy_desired_horizontal_mps": list(original),
        "uncapped_compensated_horizontal_mps": list(uncapped), "compensated_desired_horizontal_mps": list(corrected),
        "maximum_horizontal_speed_mps": maximum,
        "approximation": "ideal immediate minimum throttle for full15s; no spool, shutdown tail, early completion, attitude error, gimbal deflection or other-engine forces",
        "quadrature": "endpoint trapezoid of desired-reference thrust/mass in ECI; linear ideal pulse propellant depletion",
        "prospective_cutoff_is_current_snapshot_not_future_truth": True, "prediction_is_execution": False,
        "actual_state_assigned": False, "physics_limits_changed": False, "qualification_refreshed": False, "model_calls": 0}
    velocity_error = tuple(a-b for a, b in zip(corrected, _horizontal(relative, up)))
    tau = configuration.get("boostback_velocity_tau_s", 25.)
    upward = configuration.get("boostback_upward_acceleration_mps2", 2.)
    acceleration = tuple(value/tau+upward*axis for value, axis in zip(velocity_error, up))
    return receipt, fall, velocity_error, acceleration, north


def verify_compensation(row, state, craft, profile, observed, expected_site=None, coast_context=None):
    """Verify one saved BB request, including its real cutoff transition row.

    A cutoff changes the row phase and may replace its attitude target later
    in the same tick. Its BB request receipt remains mandatory and verifiable;
    only rows still in BB additionally bind the final target's full frame.
    Return the independently rebuilt receipt, or ``None`` outside this scope.
    """
    diagnostic = row["controller"]
    actual = diagnostic.get("capture_impulse_compensation")
    degraded = any(not item.available for item in state.engine_states[:profile["booster"]["engine_count"]])
    boostback = row["phase"] in ("booster_boostback_slew", "booster_boostback")
    cutoff = (row["phase"] in ("booster_entry_coast", "booster_landing_burn") and type(coast_context) is dict
        and coast_context.get("coast_started_s") == state.time_s)
    required = degraded and (boostback or cutoff)
    _require((actual is not None) is required,
        "Capture compensation receipt is absent from actual boostback or leaks beyond its cutoff")
    if not required:
        return None
    _require(state.propellant_kg > 0., "Capture compensation cannot predict a pulse without actual propellant")
    expected, fall, error, acceleration, north = _rebuild(state, craft, profile, observed, expected_site)
    _receipt(actual, expected)
    _near(diagnostic.get("estimated_fall_time_s"), fall, "Capture compensation changed original ballistic fall clock")
    _near(diagnostic.get("horizontal_velocity_error_mps"), math.hypot(*error),
        "Boostback did not use the corrected actual horizontal error")
    if boostback:
        q = diagnostic.get("target_q_body_to_eci")
        _require(isinstance(q, (tuple, list)) and len(q) == 4, "Missing compensated boostback target frame")
        axis = env.unit(acceleration)
        _near(dyn.rotate(q, (0., 0., 1.)), axis, "Compensated boostback target direction differs", 1e-9)
        x = tuple(a-env.dot(north, axis)*b for a, b in zip(north, axis))
        # The original attitude helper's fallback is relevant only to an
        # exactly north-pointing request; reproduce its deterministic basis.
        if math.hypot(*x) < 1e-6:
            index = min(range(3), key=lambda i: abs(axis[i]))
            basis = tuple(float(i == index) for i in range(3))
            x = tuple(a-env.dot(basis, axis)*b for a, b in zip(basis, axis))
        _near(dyn.rotate(q, (1., 0., 0.)), env.unit(x), "Compensated boostback changed local-north roll", 1e-9)
    return expected
