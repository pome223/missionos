"""Independent consistency checks for configured, simulated tower contact.

No simulator, contact producer or model-provider module is imported. A valid
negative record is evidence; sustained surrogate support is not a physical
capture, a SpaceX validation, or completion of a launch-connected mission.
"""
from __future__ import annotations

import math

_SCHEMA = "missionos.starship_booster_catch.v1"
_A = 6_378_137.0
_E2 = (1 / 298.257223563) * (2 - 1 / 298.257223563)
_ROTATION = 7.292115e-5
_MU = 3.986004418e14
_J2 = 1.08262668e-3


class _Invalid(Exception):
    def __init__(self, code, detail):
        self.issue = {"code": code, "detail": detail}


def _require(condition, code, detail):
    if not condition:
        raise _Invalid(code, detail)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) < 1e50


def _near(a, b, absolute=1e-6, relative=1e-8):
    return _number(a) and _number(b) and abs(a-b) <= absolute + relative*max(abs(a), abs(b))


def _vector(value, length=3):
    return type(value) is list and len(value) == length and all(_number(x) for x in value)


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _norm(value):
    return math.sqrt(_dot(value, value))


def _add(a, b):
    return [x+y for x, y in zip(a, b)]


def _sub(a, b):
    return [x-y for x, y in zip(a, b)]


def _scale(a, scale):
    return [x*scale for x in a]


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _rotate(q, v):
    first = _cross(q[1:], v)
    second = _cross(q[1:], first)
    return [x+2*q[0]*a+2*b for x, a, b in zip(v, first, second)]


def _same_vector(actual, expected, code, detail, absolute=1e-6):
    _require(_vector(actual) and all(_near(a, b, absolute) for a, b in zip(actual, expected)), code, detail)


def _json(root):
    pending, ancestors, count = [(root, 0, False)], set(), 0
    while pending:
        value, depth, leaving = pending.pop()
        if leaving:
            ancestors.remove(id(value))
            continue
        count += 1
        _require(count <= 3_000_000 and depth <= 32, "input_limit", "Record exceeds bounded JSON traversal")
        if value is None or type(value) is bool:
            continue
        if type(value) in (int, float):
            _require(_number(value), "invalid_number", "Expected finite bounded numbers")
        elif type(value) is str:
            _require(len(value) <= 16000, "input_limit", "Oversized string")
        elif type(value) in (dict, list):
            _require(id(value) not in ancestors and len(value) <= 100000,
                     "invalid_json", "Cyclic or oversized record")
            if type(value) is dict:
                _require(all(type(key) is str and len(key) <= 256 for key in value), "invalid_json", "Invalid object key")
            ancestors.add(id(value))
            pending.append((value, depth, True))
            pending.extend((child, depth+1, False) for child in
                           (value.values() if type(value) is dict else value))
        else:
            raise _Invalid("invalid_json", "Expected JSON-compatible data")


def _tower(profile, time_s):
    latitude = math.radians(profile["launch"]["latitude_deg"])
    longitude = math.radians(profile["launch"]["longitude_deg"]) + _ROTATION*time_s
    sl, cl = math.sin(latitude), math.cos(latitude)
    so, co = math.sin(longitude), math.cos(longitude)
    n = _A/math.sqrt(1-_E2*sl*sl)
    origin = [n*cl*co, n*cl*so, n*(1-_E2)*sl]
    east, north, up = [-so, co, 0.], [-sl*co, -sl*so, cl], [cl*co, cl*so, sl]
    return origin, [east, north, up]


def _enu(vector, basis):
    return [_dot(vector, axis) for axis in basis]


def _from_enu(vector, basis):
    return [sum(vector[i]*basis[i][j] for i in range(3)) for j in range(3)]


def _profile(profile):
    _require(type(profile) is dict, "profile", "Expected the bound vehicle profile")
    _json(profile)
    for section in ("launch", "booster", "geometry"):
        _require(type(profile.get(section)) is dict, "profile", f"Missing {section}")
    launch, booster, geometry = profile["launch"], profile["booster"], profile["geometry"]
    _require(_number(launch.get("latitude_deg")) and abs(launch["latitude_deg"]) <= 90 and
             _number(launch.get("longitude_deg")) and abs(launch["longitude_deg"]) <= 180,
             "profile", "Invalid tower origin")
    for key in ("dry_mass_kg", "propellant_kg", "tank_length_m", "engine_thrust_n"):
        _require(_number(booster.get(key)) and booster[key] > 0, "profile", f"Invalid {key}")
    for key in ("dry_com_z_m", "tank_center_z_m"):
        _require(_number(booster.get(key)), "profile", f"Invalid {key}")
    for key in ("radius_m", "booster_length_m"):
        _require(_number(geometry.get(key)) and geometry[key] > 0, "profile", f"Invalid {key}")


def _state(frame, profile):
    _require(type(frame) is dict and _number(frame.get("time_s")) and frame["time_s"] >= 0,
             "state", "Expected a timestamped state")
    for key, length in (("r_eci_m", 3), ("v_eci_mps", 3), ("q_body_to_eci", 4),
                        ("omega_body_rad_s", 3), ("com_body_m", 3), ("com_rate_body_mps", 3)):
        _require(_vector(frame.get(key), length), "state", f"Invalid {key}")
    _require(_near(_dot(frame["q_body_to_eci"], frame["q_body_to_eci"]), 1., 1e-8) and
             _norm(frame["r_eci_m"]) > 1., "state", "Invalid quaternion or position")
    fuel, booster = frame.get("propellant_kg"), profile["booster"]
    _require(_number(fuel) and 0 <= fuel <= booster["propellant_kg"], "state", "Invalid propellant")
    mass = booster["dry_mass_kg"] + fuel
    com_z = (booster["dry_mass_kg"]*booster["dry_com_z_m"] + fuel*booster["tank_center_z_m"])/mass
    _require(_near(frame.get("mass_kg"), mass), "mass_properties", "Booster mass is not dry mass plus propellant")
    _same_vector(frame["com_body_m"], [0., 0., com_z], "mass_properties", "Booster centroid is inconsistent")
    _require(_number(frame.get("engine_thrust_n")) and frame["engine_thrust_n"] >= 0,
             "state", "Invalid engine thrust")
    for key in ("force_body_n", "torque_body_nm", "total_force_eci_n", "total_torque_body_nm"):
        _require(_vector(frame.get(key)), "state", f"Invalid {key}")
    tensor = frame.get("inertia_kg_m2")
    _require(type(tensor) is list and len(tensor) == 3 and all(_vector(row) for row in tensor),
             "state", "Invalid inertia tensor")
    radius, length = profile["geometry"]["radius_m"], profile["geometry"]["booster_length_m"]
    dry = booster["dry_mass_kg"]
    base = [dry*(3*radius*radius+length*length)/12]*2+[dry*radius*radius/2]
    fuel_diagonal = [fuel*(3*radius*radius+booster["tank_length_m"]**2)/12]*2+[fuel*radius*radius/2]
    transverse = dry*(booster["dry_com_z_m"]-com_z)**2+fuel*(booster["tank_center_z_m"]-com_z)**2
    dry_tensor = booster.get("dry_inertia_kg_m2")
    if dry_tensor is not None:
        _require(type(dry_tensor) is list and len(dry_tensor) == 3 and all(_vector(row) for row in dry_tensor),
                 "profile", "Invalid dry inertia tensor")
    expected = [[(base[i] if i == j else 0.) if dry_tensor is None else dry_tensor[i][j]
                 for j in range(3)] for i in range(3)]
    for i in range(3):
        expected[i][i] += fuel_diagonal[i]+(transverse if i < 2 else 0.)
    _require(all(_near(tensor[i][j], expected[i][j], .01) for i in range(3) for j in range(3)),
             "mass_properties", "Inertia differs from configured dry hull and fuel")
    return mass


def _continuity(previous, current, dt, profile):
    """Reject discrete state resets without duplicating the dynamics integrator.

    Dense contact frames bound translational/rotational motion. These checks
    are necessary consistency conditions, not a complete force-balance proof.
    """
    _require(0 < dt <= .02 + 1e-9, "time_order", "Contact frames must be dense and strictly time ordered")
    mass = min(previous["mass_kg"], current["mass_kg"])
    contact = max(_norm(previous["force_body_n"]), _norm(current["force_body_n"]))
    thrust = max(previous["engine_thrust_n"], current["engine_thrust_n"])
    acceleration_bound = 20. + (contact+thrust)/mass
    mean_velocity = _scale(_add(previous["v_eci_mps"], current["v_eci_mps"]), .5)
    position_residual = _norm(_sub(_sub(current["r_eci_m"], previous["r_eci_m"]), _scale(mean_velocity, dt)))
    _require(position_residual <= 1e-4 + acceleration_bound*dt*dt,
             "state_discontinuity", "Position changed without a compatible velocity history")
    _require(_norm(_sub(current["v_eci_mps"], previous["v_eci_mps"])) <= acceleration_bound*dt + 1e-3,
             "state_discontinuity", "Velocity reset exceeds finite load bounds")
    predicted_delta = _scale(previous["total_force_eci_n"], dt/previous["mass_kg"])
    _require(_norm(_sub(_sub(current["v_eci_mps"], previous["v_eci_mps"]), predicted_delta)) <=
             .002 + 5*acceleration_bound*dt*dt,
             "state_discontinuity", "Velocity changed without a compatible finite-force history")
    quaternion_dot = abs(_dot(previous["q_body_to_eci"], current["q_body_to_eci"]))
    angle = 2*math.acos(min(1., quaternion_dot))
    rate_bound = max(_norm(previous["omega_body_rad_s"]), _norm(current["omega_body_rad_s"]))
    _require(angle <= (rate_bound+.02)*dt+1e-5,
             "state_discontinuity", "Attitude changed without a compatible angular-rate history")
    _require(current["propellant_kg"] <= previous["propellant_kg"]+1e-6,
             "state_discontinuity", "Propellant increased during terminal capture")
    minimum_inertia = min(min(previous["inertia_kg_m2"][i][i], current["inertia_kg_m2"][i][i]) for i in range(3))
    maximum_inertia = max(max(previous["inertia_kg_m2"][i][i], current["inertia_kg_m2"][i][i]) for i in range(3))
    torque = max(_norm(previous["total_torque_body_nm"]), _norm(current["total_torque_body_nm"]))
    angular_bound = (torque + 2*maximum_inertia*rate_bound*rate_bound)/max(1., minimum_inertia)
    _require(_norm(_sub(current["omega_body_rad_s"], previous["omega_body_rad_s"])) <= angular_bound*dt+.0001,
             "state_discontinuity", "Angular velocity reset exceeds finite moment bounds")


def _force_envelope(frame, profile):
    r, mass = frame["r_eci_m"], frame["mass_kg"]
    radius = _norm(r)
    gravity = _scale(r, -_MU/radius**3)
    z_fraction = r[2]*r[2]/(radius*radius)
    factor = 1.5*_J2*_MU*_A*_A/radius**5
    gravity = _add(gravity, [factor*r[0]*(5*z_fraction-1), factor*r[1]*(5*z_fraction-1), factor*r[2]*(5*z_fraction-3)])
    residual = _sub(_sub(frame["total_force_eci_n"], _scale(gravity, mass)),
                    _rotate(frame["q_body_to_eci"], frame["force_body_n"]))
    from .starship_wind_verifier import validate_wind_profile, _expected_wind
    wind = validate_wind_profile(profile)
    wind_eci = _expected_wind(wind, frame, True)[1] if wind is not None else [0., 0., 0.]
    air_speed = _norm(_sub(_sub(frame["v_eci_mps"], _cross([0., 0., _ROTATION], r)), wind_eci))
    # Conservative terminal airload bound, not an aerodynamic reconstruction.
    # This prevents using an invented force to explain a hard state reset.
    area = 4*profile["geometry"]["radius_m"]*profile["geometry"]["booster_length_m"]
    aero_bound = 2.*area*air_speed*air_speed + 10000.
    _require(_norm(residual) <= frame["engine_thrust_n"]+aero_bound,
             "force_envelope", "Unexplained force exceeds engine and broad terminal airload bound")


def _configuration(config):
    _require(type(config) is dict, "configuration", "Expected separately bound catch configuration")
    _json(config)
    required = {"support_height_m", "arm_open_half_span_m", "arm_closed_half_span_m", "arm_speed_mps",
                "arm_half_width_m", "arm_half_length_m", "normal_stiffness_npm", "normal_damping_ns_pm",
                "tangential_damping_ns_pm", "friction_coefficient", "maximum_support_force_n",
                "maximum_compression_m", "settle_time_s", "settle_pin_speed_mps", "settle_body_rate_rad_s",
                "settle_engine_thrust_n", "initial_pin_clearance_m", "initial_propellant_kg",
                "terminal_position_tau_s", "terminal_velocity_tau_s", "terminal_max_tilt_deg",
                "divert_north_offset_m", "integration_dt_s", "duration_s"}
    _require(set(config) == required | {"schema", "profile_id", "claim", "support_points_body_m", "initial_vertical_speed_mps"}
             and config["schema"] == "missionos.starship_catch_configuration.v1",
             "configuration", "Unknown catch configuration fields or schema")
    _require(all(_number(config[key]) and config[key] > 0 for key in required),
             "configuration", "Catch parameters must be positive finite numbers")
    points = config["support_points_body_m"]
    _require(type(points) is list and len(points) == 2 and all(_vector(p) for p in points)
             and points[0][0] < 0 < points[1][0] and
             _number(config["initial_vertical_speed_mps"]) and config["initial_vertical_speed_mps"] < 0 and
             config["arm_open_half_span_m"] > config["arm_closed_half_span_m"] and
             config["integration_dt_s"] <= .01 and config["duration_s"] <= 30 and
             config["settle_time_s"] >= 2 and config["terminal_max_tilt_deg"] < 30,
             "configuration", "Catch configuration exceeds declared bounds")


def _contact(frame, profile, config, elapsed, authorized, missing_support, prior_eligible):
    origin, axes = _tower(profile, frame["time_s"])
    q = frame["q_body_to_eci"]
    inverse = [q[0], -q[1], -q[2], -q[3]]
    travel = min(config["arm_open_half_span_m"]-config["arm_closed_half_span_m"],
                 config["arm_speed_mps"]*max(0., elapsed)) if authorized else 0.
    span = config["arm_open_half_span_m"]-travel
    rate = -config["arm_speed_mps"] if authorized and span > config["arm_closed_half_span_m"]+1e-12 else 0.
    _require(_near(frame.get("arm_half_span_m"), span) and _near(frame.get("arm_rate_mps"), rate),
             "arm_motion", "Arm motion disagrees with finite configured travel")
    pins = frame.get("pins")
    _require(type(pins) is list and len(pins) == 2 and all(type(p) is dict for p in pins),
             "contact", "Expected both configured support points")
    forces, torques, loads, compression, speeds, next_eligible = [], [], [], [], [], []
    for index, (point, pin) in enumerate(zip(config["support_points_body_m"], pins)):
        _require(type(pin.get("id")) is int and pin["id"] == index, "contact", "Support-point identity changed")
        _same_vector(pin.get("position_body_m"), point, "contact", "Support geometry differs from bound configuration")
        lever = _sub(point, frame["com_body_m"])
        world = _add(frame["r_eci_m"], _rotate(q, lever))
        material_velocity = _add(frame["v_eci_mps"], _rotate(q,
            _sub(_cross(frame["omega_body_rad_s"], lever), frame["com_rate_body_mps"])))
        relative = _sub(material_velocity, _cross([0., 0., _ROTATION], world))
        position, velocity = _enu(_sub(world, origin), axes), _enu(relative, axes)
        sign = -1. if index == 0 else 1.
        arm_velocity = [sign*rate, 0., 0.]
        velocity = _sub(velocity, arm_velocity)
        _same_vector(pin.get("position_enu_m"), position, "point_kinematics", "Saved support-point position is inconsistent")
        _same_vector(pin.get("relative_velocity_enu_mps"), velocity, "point_kinematics", "Point velocity must include rotation and centroid motion")
        _same_vector(pin.get("arm_velocity_enu_mps"), arm_velocity, "point_kinematics", "Arm velocity is inconsistent")
        footprint = (authorized and not (missing_support and index == 1)
                     and abs(position[0]-sign*span) <= config["arm_half_width_m"]
                     and abs(position[1]) <= config["arm_half_length_m"])
        _require(type(pin.get("footprint_active")) is bool and pin["footprint_active"] == footprint,
                 "footprint", "Support footprint or authority is inconsistent")
        top_eligible = bool(footprint and (position[2] >= config["support_height_m"] or prior_eligible[index]))
        _require(type(pin.get("top_contact_eligible")) is bool and pin["top_contact_eligible"] == top_eligible,
                 "contact_history", "An arm cannot capture a point that already passed beneath it")
        next_eligible.append(top_eligible)
        penetration = max(0., config["support_height_m"]-position[2])
        normal = max(0., config["normal_stiffness_npm"]*penetration-config["normal_damping_ns_pm"]*velocity[2]) if top_eligible and penetration > 0 else 0.
        tangent = [-velocity[0]*config["tangential_damping_ns_pm"], -velocity[1]*config["tangential_damping_ns_pm"], 0.]
        tangent = _scale(tangent, min(1., config["friction_coefficient"]*normal/max(_norm(tangent), 1e-30)))
        force = _add(tangent, [0., 0., normal])
        body_force = _rotate(inverse, _from_enu(force, axes))
        forces.append(body_force)
        torques.append(_cross(lever, body_force))
        loads.append(normal)
        compression.append(penetration if normal > 0 else 0.)
        speeds.append(_norm(velocity))
        _require(_near(pin.get("penetration_m"), penetration) and _near(pin.get("normal_force_n"), normal, .02),
                 "contact_load", "Normal contact load does not follow configured compliant contact law")
        _same_vector(pin.get("force_enu_n"), force, "contact_load", "Tangential force violates configured friction law", .02)
    _same_vector(frame.get("force_body_n"), _add(*forces), "force_sum", "Contact forces do not sum about the saved body", .02)
    _same_vector(frame.get("torque_body_nm"), _add(*torques), "torque_sum", "Contact moment must use the instantaneous centroid", .5)
    return loads, compression, speeds, next_eligible


def _claim_boundary(run, record):
    for object_ in (run, run.get("outcome", {}), record):
        _require(type(object_) is dict, "record", "Expected an object")
        for key in ("catch_verified", "physical_execution", "physical_execution_invoked", "mission_completed", "starship_vehicle_validated",
                    "landing_hardware_validated", "structural_deformation_validated", "complete_swept_volume_test"):
            if key in object_:
                _require(object_[key] is False, "claim_boundary", f"Unsupported claim: {key}")
    _require(record.get("physical_execution") is False and record.get("catch_verified") is False and
             record.get("structural_deformation_validated") is False and
             record.get("complete_swept_volume_test") is False and record.get("contact_response_modeled") is True,
             "claim_boundary", "Contact evidence must preserve its surrogate boundary")


def _binding(sample, frame, code):
    _require(type(sample) is dict, code, "Missing saved state")
    for key in ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s", "propellant_kg"):
        _require(sample.get(key) == frame.get(key), code, f"Unbound state field {key}")


def _surface_contact(run, frame, profile):
    receipt = run.get("contact")
    if run["outcome"]["termination"] != "surface_contact":
        _require(receipt is None, "ground_contact", "Unexpected ground-contact claim")
        return
    _require(type(receipt) is dict and receipt.get("contact") is True and
             _near(receipt.get("event_time_s"), frame["time_s"]) and receipt.get("landing_verified") is False and
             receipt.get("starship_vehicle_validated") is False, "ground_contact", "Missing or invalid ground-contact receipt")
    point = receipt.get("point_body_m")
    radius, length = profile["geometry"]["radius_m"], profile["geometry"]["booster_length_m"]
    _require(_vector(point) and -1e-6 <= point[2] <= length+1e-6 and math.hypot(*point[:2]) <= radius+1e-6,
             "ground_contact", "Ground contact point lies outside the configured cylinder")
    _require(min(abs(point[2]), abs(point[2]-length), abs(math.hypot(*point[:2])-radius)) <= 1e-5,
             "ground_contact", "Ground receipt point is not on the hull envelope")
    q = frame["q_body_to_eci"]
    lever = _sub(point, frame["com_body_m"])
    world = _add(frame["r_eci_m"], _rotate(q, lever))
    velocity = _add(frame["v_eci_mps"], _rotate(q,
        _sub(_cross(frame["omega_body_rad_s"], lever), frame["com_rate_body_mps"])))
    relative = _sub(velocity, _cross([0., 0., _ROTATION], world))
    level = (world[0]*world[0]+world[1]*world[1])/(_A*_A)+world[2]*world[2]/(_A*_A*(1-_E2))
    _require(abs(math.sqrt(level)-1)*_A < .01, "ground_contact", "Receipt does not locate the ellipsoid surface")
    _same_vector(receipt.get("point_eci_m"), world, "ground_contact", "Ground point position is inconsistent")
    _same_vector(receipt.get("point_velocity_eci_mps"), velocity, "ground_contact", "Ground point velocity is inconsistent")
    _same_vector(receipt.get("surface_relative_velocity_mps"), relative, "ground_contact", "Ground relative velocity is inconsistent")
    _require(_near(receipt.get("surface_relative_speed_mps"), _norm(relative)),
             "ground_contact", "Ground-contact speed is inconsistent")


def _initialized_state(frame, config, profile, scenario):
    _require(_near(frame["propellant_kg"], config["initial_propellant_kg"]) and _near(frame["time_s"], 600.),
             "initial_binding", "Initialized terminal mass or clock differs from declared fixture")
    _, axes = _tower(profile, frame["time_s"])
    for axis_body, expected in (([1., 0., 0.], axes[0]), ([0., 0., 1.], axes[2])):
        _same_vector(_rotate(frame["q_body_to_eci"], axis_body), expected,
                     "initial_binding", "Initialized terminal orientation is inconsistent")
    expected_height = config["support_height_m"]+config["initial_pin_clearance_m"]
    for index, pin in enumerate(frame["pins"]):
        point = config["support_points_body_m"][index]
        _same_vector(pin["position_enu_m"], [point[0], point[1]+(12. if scenario == "lateral_offset" else 0.),
                     expected_height+point[2]-config["support_points_body_m"][0][2]],
                     "initial_binding", "Initialized terminal support points differ from configured approach", .001)
    relative = _sub(frame["v_eci_mps"], _cross([0., 0., _ROTATION], frame["r_eci_m"]))
    _same_vector(_enu(relative, axes), [0., 0., -15. if scenario == "fast_descent" else config["initial_vertical_speed_mps"]],
                 "initial_binding", "Initialized terminal descent rate differs from declared fixture", .002)


def verify_catch(run, profile, catch_config):
    """Check one terminal-campaign case without importing its mechanics code."""
    result = {"schema": "missionos.starship_booster_catch_verification.v1", "passed": False,
              "simulated_catch_supported": False, "catch_verified": False, "physical_execution": False,
              "physical_execution_invoked": False, "mission_completed": False, "issues": [], "frame_count": 0,
              "limitations": ["Independent consistency checks, not an integrator replay or identified SpaceX model.",
                              "Terminal support evidence is not a launch-connected catch or hardware certification."]}
    try:
        _json(run)
        _require(type(run) is dict and run.get("body_id") == "booster", "record", "Expected a booster catch run")
        _profile(profile)
        _configuration(catch_config)
        record = run.get("catch_record")
        _require(type(record) is dict and record.get("schema") == _SCHEMA, "record", "Missing catch record")
        _require(record.get("configuration") == catch_config, "configuration_binding", "Run differs from separately bound catch configuration")
        _claim_boundary(run, record)
        initialization = record.get("initialization")
        _require(type(initialization) is dict and initialization.get("kind") in ("terminal_initialized", "supplied_state")
                 and initialization.get("launch_connected") is False,
                 "initialization", "Terminal campaign must not claim launch-connected recovery")
        scenario = record.get("scenario")
        aliases = {"booster_catch": "booster_catch", "booster_catch_tower_unavailable": "tower_unavailable",
                   "booster_catch_lateral_offset": "lateral_offset", "booster_catch_fast_descent": "fast_descent",
                   "booster_catch_one_support": "one_support"}
        _require(scenario in aliases.values() and aliases.get(run.get("scenario"), run.get("scenario")) == scenario,
                 "scenario", "Scenario identity mismatch")
        _require(type(record.get("missing_support")) is bool and record["missing_support"] == (scenario == "one_support"),
                 "scenario", "Missing-support fault binding is inconsistent")
        eligibility = record.get("eligibility")
        _require(type(eligibility) is dict and all(type(eligibility.get(key)) is bool for key in
                 ("tower_ready", "vehicle_ready", "catch_authorized")), "eligibility", "Invalid readiness or approval record")
        authorized = all(eligibility[key] for key in ("tower_ready", "vehicle_ready", "catch_authorized"))
        _require(authorized == (scenario != "tower_unavailable"), "eligibility", "Readiness contradicts the scenario")
        frames, samples, events, outcome = record.get("frames"), run.get("samples"), run.get("events"), run.get("outcome")
        _require(type(frames) is list and 1 <= len(frames) <= 100000 and type(samples) is list and samples
                 and type(events) is list and all(type(e) is dict for e in events) and type(outcome) is dict,
                 "record", "Missing bounded trajectory, events or outcome")
        start, dt_limit = record.get("start_time_s"), record.get("integration_dt_s")
        duration = record.get("requested_duration_s")
        _require(_number(start) and start >= 0 and _number(dt_limit) and 0 < dt_limit <= catch_config["integration_dt_s"],
                 "time_order", "Invalid integration contract")
        _require(_number(duration) and 0 < duration <= 30, "time_order", "Invalid terminal duration")
        previous, settled, peak_load, peak_compression = None, 0., 0., 0.
        overloaded, stroke, first_contact, supported = False, False, None, False
        contact_eligible = [False, False]
        frame_times = {}
        for frame in frames:
            _state(frame, profile)
            time_s = frame["time_s"]
            _require(time_s >= start and time_s <= start+duration+1e-7, "time_order", "Frame outside bounded terminal interval")
            health = frame.get("eligibility")
            _require(type(health) is dict and _number(health.get("sampled_at_s")) and
                     _number(health.get("valid_until_s")) and
                     0 <= time_s-health["sampled_at_s"] <= .05+1e-9 and
                     time_s <= health["valid_until_s"] <= health["sampled_at_s"]+.05+1e-9,
                     "readiness_freshness", "Readiness is stale, future-dated, expired or unbounded")
            _require(health.get("tower_ready") is eligibility["tower_ready"] and
                     health.get("vehicle_ready") is eligibility["vehicle_ready"] and
                     health.get("rules_catch_allowed") is authorized,
                     "eligibility", "Rules, current health and approval disagree")
            _force_envelope(frame, profile)
            if previous is not None:
                dt = time_s-previous["time_s"]
                _require(dt <= dt_limit+1e-7, "time_order", "Frame gap exceeds integrated step")
                _continuity(previous, frame, dt, profile)
            else:
                _require(_near(time_s, start), "initial_binding", "First frame is not the declared initial time")
            loads, compression, speeds, contact_eligible = _contact(
                frame, profile, catch_config, time_s-start, authorized, record["missing_support"], contact_eligible)
            peak_load, peak_compression = max(peak_load, *loads), max(peak_compression, *compression)
            overloaded = overloaded or any(x > catch_config["maximum_support_force_n"] for x in loads)
            stroke = stroke or any(x > catch_config["maximum_compression_m"] for x in compression)
            if any(x > 0 for x in loads) and first_contact is None:
                first_contact = time_s
            stable = (all(x > 0 for x in loads) and not overloaded and not stroke
                      and max(speeds) <= catch_config["settle_pin_speed_mps"]
                      and _norm(frame["omega_body_rad_s"]) <= catch_config["settle_body_rate_rad_s"]
                      and frame["engine_thrust_n"] <= catch_config["settle_engine_thrust_n"])
            settled = settled+(time_s-previous["time_s"]) if stable and previous is not None and previous["settle_eligible"] else 0.
            _require(type(frame.get("settle_eligible")) is bool and frame["settle_eligible"] == stable
                     and _near(frame.get("settle_elapsed_s"), settled), "settling", "Settling requires consecutive bilateral support")
            supported = settled >= catch_config["settle_time_s"]-1e-9
            frame_times[time_s] = frame
            previous = frame
        result["frame_count"] = len(frames)
        if initialization["kind"] == "terminal_initialized":
            _initialized_state(frames[0], catch_config, profile, scenario)
        _binding(run.get("initial_state"), frames[0], "initial_binding")
        _binding(run.get("final_state"), frames[-1], "final_binding")
        last_time = -math.inf
        for sample in samples:
            _require(type(sample) is dict and sample.get("body_id") == "booster" and
                     _number(sample.get("time_s")) and sample["time_s"] > last_time,
                     "samples", "Samples must be timestamped booster states in strict order")
            _require(sample["time_s"] in frame_times, "samples", "Sample is not bound to a recorded mechanics frame")
            _binding(sample, frame_times[sample["time_s"]], "samples")
            from .starship_wind_verifier import verify_wind_observation
            _require(verify_wind_observation(sample, profile)["passed"],
                     "wind_observation", "Catch air/ground observations differ from declared wind")
            last_time = sample["time_s"]
        _binding(samples[0], frames[0], "initial_binding")
        _binding(samples[-1], frames[-1], "final_binding")
        _require(_near(outcome.get("end_time_s"), frames[-1]["time_s"]) and
                 _near(outcome.get("duration_s"), frames[-1]["time_s"]-start), "outcome", "Outcome time differs from trajectory")
        _require(type(outcome.get("integration_steps")) is int and outcome["integration_steps"] == len(frames)-1,
                 "outcome", "Step count differs from dense mechanics frames")
        _require(outcome.get("six_dof_integrated") is True and outcome.get("attitude_prescribed") is False and
                 type(outcome.get("simulated_catch_supported")) is bool and outcome["simulated_catch_supported"] == supported,
                 "outcome", "Outcome does not match independently measured support")
        _require(outcome.get("orbit_gate_reached", False) is False and
                 type(outcome.get("payload_released_count", 0)) is int and outcome.get("payload_released_count", 0) == 0,
                 "claim_boundary", "Terminal catch record cannot establish orbit or payload release")
        _require(record.get("support_overload") is overloaded and record.get("support_stroke_exceeded") is stroke and
                 _near(record.get("peak_support_force_n"), peak_load, .02) and
                 _near(record.get("peak_compression_m"), peak_compression), "outcome", "Peak loads or latched failure flags disagree")
        settling = record.get("settling")
        _require(type(settling) is dict and _near(settling.get("required_s"), catch_config["settle_time_s"]) and
                 _near(settling.get("observed_s"), settled), "settling", "Summary is inconsistent with consecutive settling")
        termination = outcome.get("termination")
        expected = "supported_settled" if supported else "support_overload" if overloaded else "support_stroke_exceeded" if stroke else None
        _require(termination == expected if expected else termination in ("time_limit", "surface_contact", "ground_overlap_with_support"),
                 "outcome", "Termination contradicts observed mechanical conditions")
        if termination == "time_limit":
            _require(_near(frames[-1]["time_s"]-start, duration), "outcome", "Time-limit record ended before the requested duration")
        _surface_contact(run, frames[-1], profile)
        allowed_events = {"catch_terminal_start", "catch_not_authorized_divert", "support_plane_crossing",
                          "first_support_contact_engine_cutoff", "supported_settled", "support_overload",
                          "support_stroke_exceeded", "time_limit", "surface_contact", "ground_overlap_with_support"}
        for event in events:
            _require(_number(event.get("time_s")) and start <= event["time_s"] <= frames[-1]["time_s"]+1e-8,
                     "events", "Event is outside recorded time")
            _require(event.get("event") in allowed_events, "events", "Unknown event or out-of-scope execution claim")
            if event["event"] == "support_plane_crossing":
                bracket, index = event.get("time_bracket_s"), event.get("pin_id")
                _require(_vector(bracket, 2) and type(index) is int and 0 <= index <= 1 and
                         bracket[0] in frame_times and bracket[1] in frame_times and
                         0 < bracket[1]-bracket[0] <= dt_limit+1e-7 and _near(event["time_s"], bracket[1]),
                         "events", "Crossing is not bound to adjacent mechanics frames")
                before, after = frame_times[bracket[0]]["pins"][index], frame_times[bracket[1]]["pins"][index]
                _require(before["position_enu_m"][2] > catch_config["support_height_m"] >= after["position_enu_m"][2] and
                         event.get("footprint_active") is after["footprint_active"], "events", "False support-plane crossing")
        starts = [e for e in events if e.get("event") == "catch_terminal_start"]
        ends = [e for e in events if e.get("event") == termination]
        contacts = [e for e in events if e.get("event") == "first_support_contact_engine_cutoff"]
        _require(len(starts) == 1 and _near(starts[0]["time_s"], start) and len(ends) == 1 and
                 _near(ends[0]["time_s"], frames[-1]["time_s"]), "events", "Missing or conflicting start/terminal event")
        _require((not contacts and first_contact is None) or
                 (len(contacts) == 1 and _near(contacts[0]["time_s"], first_contact)),
                 "events", "First support observation is not bound to shutdown event")
        diverts = [e for e in events if e.get("event") == "catch_not_authorized_divert"]
        _require((authorized and not diverts) or (not authorized and len(diverts) == 1 and _near(diverts[0]["time_s"], start)),
                 "eligibility", "Divert record contradicts tower readiness")
        result.update(passed=True, simulated_catch_supported=supported,
                      observed_settle_s=settled, peak_support_force_n=peak_load,
                      peak_compression_m=peak_compression, termination=termination,
                      launch_connected=False)
    except _Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, OverflowError, IndexError, ZeroDivisionError) as exc:
        result["issues"].append({"code": "invalid_record", "detail": type(exc).__name__})
    return result
