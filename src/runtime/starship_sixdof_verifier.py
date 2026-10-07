"""Independent admission checks for saved six-DOF simulator observations.

This verifier imports neither the simulator nor its dynamics helpers. It checks
stored states, receipt/event linkage and elementary kinematic identities, not
the integration history, aerodynamic fidelity or successful mission completion.
Source/profile hashes and the approval-to-process binding belong to the caller.
An honestly recorded impact, numerical failure or time limit can pass here.
"""
from __future__ import annotations

import math

SCENARIOS = ("launch", "engine_out", "entry_perturbation", "gimbal_step", "flap_asymmetry")
SUPERVISED_SCENARIOS = ("deployment_no_effect",)
MAX_SAMPLES = 250_000
MAX_JSON_NODES = 5_000_000
MAX_EVENTS = 20_000
_MU = 3.986004418e14
_A = 6_378_137.0
_B = _A * (1 - 1 / 298.257223563)
_EARTH_RATE = 7.292115e-5
_STATE_FIELDS = ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s", "propellant_kg")
_TERMINATIONS = {"time_limit", "numerical_failure", "angular_rate_envelope_exceeded", "surface_impact", "low_speed_surface_contact"}


class _Invalid(Exception):
    def __init__(self, code, path, detail):
        self.issue = {"code": code, "path": path, "detail": detail}


def _require(condition, path, detail, code="inconsistent_output"):
    if not condition:
        raise _Invalid(code, path, detail)


def _number(value):
    return type(value) in (int, float) and abs(value) < 1e100 and math.isfinite(value)


def _near(a, b, tolerance=1e-6):
    return _number(a) and _number(b) and abs(a-b) <= tolerance


def _vector(value, size=3):
    return type(value) is list and len(value) == size and all(_number(x) and abs(x) < 1e15 for x in value)


def _norm(value):
    return math.sqrt(sum(x*x for x in value))


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _rotate(quaternion, vector):
    w, *qv = quaternion
    first_cross = _cross(qv, vector)
    second_cross = _cross(qv, first_cross)
    return [v+2*w*c+2*d for v, c, d in zip(vector, first_cross, second_cross)]


def _same_state(first, second):
    return type(first) is dict and type(second) is dict and all(first.get(k) == second.get(k) for k in _STATE_FIELDS)


def _json_tree(root):
    """Bound traversal, reject cycles/non-JSON values and nonfinite numbers."""
    stack, active, count = [(root, "$", 0, False)], set(), 0
    while stack:
        value, path, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(value))
            continue
        count += 1
        _require(count <= MAX_JSON_NODES and depth <= 48, path, "JSON traversal budget exceeded", "input_limit")
        if value is None or type(value) is bool:
            continue
        if type(value) in (int, float):
            _require(_number(value), path, "Expected a bounded finite JSON number", "invalid_number")
        elif type(value) is str:
            _require(len(value) <= 100_000, path, "String exceeds verifier limit", "input_limit")
        elif type(value) in (dict, list):
            _require(id(value) not in active, path, "Cyclic input is not JSON", "invalid_json")
            _require(len(value) <= MAX_SAMPLES, path, "Container exceeds verifier limit", "input_limit")
            active.add(id(value))
            stack.append((value, path, depth, True))
            pairs = value.items() if type(value) is dict else enumerate(value)
            for key, child in pairs:
                _require(type(value) is list or type(key) is str, path, "Object keys must be strings", "invalid_json")
                _require(not isinstance(key, str) or len(key) <= 256, path, "Object key exceeds verifier limit", "input_limit")
                stack.append((child, f"{path}.{key}", depth+1, False))
        else:
            raise _Invalid("invalid_json", path, "Expected JSON-compatible values")


def _state(state, path):
    _require(type(state) is dict, path, "State must be an object")
    _require(_number(state.get("time_s")) and 0 <= state["time_s"] <= 1e7, path, "Invalid simulation time")
    for field, size in (("r_eci_m", 3), ("v_eci_mps", 3), ("q_body_to_eci", 4), ("omega_body_rad_s", 3)):
        _require(_vector(state.get(field), size), path+"."+field, "Invalid state vector")
    _require(_norm(state["r_eci_m"]) > 1, path, "Position is degenerate")
    _require(abs(sum(x*x for x in state["q_body_to_eci"])-1) < 1e-8, path, "Quaternion is not normalized")
    _require(_number(state.get("propellant_kg")) and state["propellant_kg"] >= 0, path, "Invalid propellant mass")


def _samples(samples, path, budget, body_id, *, wind_profile=None):
    _require(type(samples) is list and 1 <= len(samples) <= MAX_SAMPLES, path, "A bounded nonempty trajectory is required")
    budget[0] += len(samples)
    _require(budget[0] <= MAX_SAMPLES, path, "Total trajectory budget exceeded", "input_limit")
    previous = -1.
    for index, sample in enumerate(samples):
        p = f"{path}[{index}]"
        _state(sample, p)
        _require(sample["time_s"] >= previous, p, "Trajectory time regressed")
        previous = sample["time_s"]
        _require(sample.get("body_id") == body_id, p, "Trajectory body identity differs")
        _require(type(sample.get("phase")) is str and bool(sample["phase"]), p, "Missing phase")
        for key in ("altitude_m", "ground_speed_mps", "mass_kg", "dynamic_pressure_pa", "com_z_m"):
            _require(_number(sample.get(key)), p+"."+key, "Missing finite observation")
        _require(sample["mass_kg"] > 0 and sample["ground_speed_mps"] >= 0 and sample["dynamic_pressure_pa"] >= 0, p, "Invalid observation range")
        x, y, _ = sample["r_eci_m"]
        vx, vy, vz = sample["v_eci_mps"]
        ground_speed = _norm([vx+_EARTH_RATE*y, vy-_EARTH_RATE*x, vz])
        _require(_near(ground_speed, sample["ground_speed_mps"], 1e-5), p, "Ground speed disagrees with saved inertial state")
        if wind_profile is not None:
            from .starship_wind_verifier import verify_wind_observation
            _require(verify_wind_observation(sample, wind_profile)["passed"], p,
                     "Wind-relative airspeed disagrees with the separately declared environment")
    return samples[0], samples[-1]


def _orbit(state):
    r, v = state["r_eci_m"], state["v_eci_mps"]
    radius = _norm(r)
    angular = _cross(r, v)
    eccentric = [a/_MU-b/radius for a, b in zip(_cross(v, angular), r)]
    eccentricity, h = _norm(eccentric), _norm(angular)
    energy = sum(x*x for x in v)/2-_MU/radius
    return energy < 0 and eccentricity < 1 and h > 1e-6, h*h/(_MU*(1+eccentricity))-_A


def _events(events, path, start, end):
    _require(type(events) is list and 1 <= len(events) <= MAX_EVENTS, path, "Missing bounded event record")
    previous, grouped = start, {}
    for index, event in enumerate(events):
        p = f"{path}[{index}]"
        _require(type(event) is dict and type(event.get("event")) is str, p, "Invalid event")
        time = event.get("time_s")
        _require(_number(time) and previous-1e-7 <= time <= end+1e-7, p, "Event time outside ordered trajectory")
        previous = time
        grouped.setdefault(event["event"], []).append(event)
    return grouped


def _contact(receipt, sample, path, length_m, radius_m):
    _require(type(receipt) is dict and receipt.get("contact") is True, path, "Missing contact receipt")
    for field in ("landing_verified", "starship_vehicle_validated", "contact_response_modeled"):
        _require(receipt.get(field) is False, path+"."+field, "Contact must not claim validated landing or physical response")
    _require(_near(receipt.get("event_time_s"), sample["time_s"]), path, "Contact time differs from final state")
    for field in ("q_body_to_eci", "omega_body_rad_s", "propellant_kg"):
        _require(receipt.get(field) == sample[field], path+"."+field, "Contact state differs from trajectory")
    for field in ("point_eci_m", "point_body_m", "point_velocity_eci_mps", "surface_relative_velocity_mps", "surface_normal_eci"):
        _require(_vector(receipt.get(field)), path+"."+field, "Invalid contact vector")
    point, velocity = receipt["point_eci_m"], receipt["point_velocity_eci_mps"]
    body_point = receipt["point_body_m"]
    _require(-1e-6 <= body_point[2] <= length_m+1e-6 and math.hypot(*body_point[:2]) <= radius_m+1e-6,
             path, "Contact point lies outside configured cylinder")
    # Current configured cylinders have centerline mass centroids. Reconstruct
    # the recorded material point from the actual saved body pose, not merely
    # any arbitrary point on Earth's surface.
    lever = [*receipt["point_body_m"][:2], receipt["point_body_m"][2]-sample["com_z_m"]]
    expected_point = [position+offset for position, offset in zip(sample["r_eci_m"], _rotate(sample["q_body_to_eci"], lever))]
    _require(all(_near(a, b, 1e-5) for a, b in zip(point, expected_point)), path, "Contact point differs from saved body pose")
    _require(_vector(sample.get("com_rate_body_mps")), path, "Contact requires the saved moving-centroid rate observation")
    local_velocity = [rotational-centroid for rotational, centroid in
                      zip(_cross(sample["omega_body_rad_s"], lever), sample["com_rate_body_mps"])]
    expected_velocity = [v+local for v, local in
                         zip(sample["v_eci_mps"], _rotate(sample["q_body_to_eci"], local_velocity))]
    _require(all(_near(a, b) for a, b in zip(velocity, expected_velocity)), path, "Contact point velocity differs from saved rigid-body state and moving centroid")
    relative = [velocity[0]+_EARTH_RATE*point[1], velocity[1]-_EARTH_RATE*point[0], velocity[2]]
    _require(all(_near(a, b) for a, b in zip(relative, receipt["surface_relative_velocity_mps"])), path, "Contact velocity differs from rotating-surface kinematics")
    speed = _norm(relative)
    _require(_near(speed, receipt.get("surface_relative_speed_mps")), path, "Contact speed is inconsistent")
    _require(_near(_norm(receipt["surface_normal_eci"]), 1), path, "Invalid surface normal")
    gradient = [point[0], point[1], (_A/_B)**2*point[2]]
    gradient_norm = _norm(gradient)
    _require(gradient_norm > 0 and all(_near(component/gradient_norm, normal, 1e-7)
             for component, normal in zip(gradient, receipt["surface_normal_eci"])), path, "Contact normal differs from ellipsoid gradient")
    normal_speed = sum(a*b for a, b in zip(relative, receipt["surface_normal_eci"]))
    _require(_near(normal_speed, receipt.get("surface_normal_speed_mps")), path, "Contact normal speed is inconsistent")
    clearance = math.sqrt(point[0]**2+point[1]**2+(_A/_B*point[2])**2)-_A
    _require(_near(clearance, receipt.get("signed_clearance_m"), 1e-5) and clearance <= 1e-4, path, "Contact point does not reach the ellipsoid")
    _require(type(receipt.get("initial_overlap")) is bool, path, "Missing overlap flag")
    _require(receipt["initial_overlap"] or abs(clearance) < .01, path, "First contact is unbracketed deep penetration")
    bracket = receipt.get("time_bracket_s")
    _require(_vector(bracket, 2) and bracket[0] <= receipt["event_time_s"]+1e-7 and bracket[0] <= bracket[1]
             and _near(bracket[1], receipt["event_time_s"]), path, "Invalid contact time bracket")
    return speed


def _run_basics(run, path, budget, profile, body_id="ship", *, recovery=False):
    _require(type(run) is dict, path, "Run must be an object")
    first, last = _samples(run.get("samples"), path+".samples", budget, body_id, wind_profile=profile)
    _require(_same_state(run.get("initial_state"), first), path, "Initial state differs from first sample")
    _state(run.get("final_state"), path+".final_state")
    _require(_same_state(run["final_state"], last), path, "Final state differs from last sample")
    outcome = run.get("outcome")
    _require(type(outcome) is dict, path, "Missing outcome")
    for field, value in (("six_dof_integrated", True), ("attitude_prescribed", False), ("starship_vehicle_validated", False)):
        _require(outcome.get(field) is value, path+".outcome."+field, "Invalid simulator claim boundary")
    _require(_near(outcome.get("duration_s"), last["time_s"]-first["time_s"]), path, "Duration differs from trajectory")
    _require(outcome.get("phase") == last["phase"], path, "Final phase differs from trajectory")
    for field, sample_field in (("final_ground_speed_mps", "ground_speed_mps"), ("final_altitude_m", "altitude_m")):
        _require(_near(outcome.get(field), last[sample_field]), path, "Final observation differs from trajectory")
    _require(_number(outcome.get("max_altitude_m")) and outcome["max_altitude_m"] >= max(x["altitude_m"] for x in run["samples"])-1e-5, path, "Reported maximum is below a saved altitude")
    _require(type(outcome.get("integration_steps")) is int and outcome["integration_steps"] >= 0, path, "Invalid step count")
    events = _events(run.get("events"), path+".events", first["time_s"], last["time_s"])
    termination = outcome.get("termination")
    allowed = _TERMINATIONS if body_id == "ship" else {"time_limit", "surface_contact", "angular_rate_envelope_exceeded"}
    if recovery and body_id == "booster":
        allowed = allowed | {"catch_handoff", "rate_settle_failed"}
    _require(type(termination) is str and termination in allowed, path, "Unknown termination")
    terminal = events.get(termination, [])
    _require(len(terminal) == 1 and _near(terminal[0]["time_s"], last["time_s"]), path, "Termination lacks a matching final event")
    contact = termination in {"surface_contact", "surface_impact", "low_speed_surface_contact"}
    _require(last.get("contact") is contact, path, "Final contact flag differs from termination")
    receipt = outcome.get("contact_receipt") if body_id == "ship" else run.get("contact")
    geometry = profile["geometry"]
    length = geometry["booster_length_m"] if body_id == "booster" else geometry["ship_length_m"]
    if body_id == "ship" and last["phase"] == "stack_ascent":
        length += geometry["booster_length_m"]
    speed = _contact(receipt, last, path+".contact", length, geometry["radius_m"]) if contact else None
    _require(contact or receipt is None, path, "Noncontact termination has a contact receipt")
    if body_id == "ship" and contact:
        _require(terminal[0].get("contact") == receipt, path, "Contact event/receipt mismatch")
        _require((speed <= 5) == (termination == "low_speed_surface_contact"), path, "Contact classification differs from observed speed")
    if termination == "angular_rate_envelope_exceeded":
        _require(_norm(last["omega_body_rad_s"]) > 5, path, "Angular envelope stop lacks an excessive observed rate")
    return first, last, outcome, events, speed


def _payloads(run, profile, path, budget, events, orbit_reached):
    satellites = run.get("satellites")
    releases = events.get("payload_released", [])
    count = run["outcome"].get("payload_released_count")
    _require(type(satellites) is list and type(count) is int and 0 <= count <= profile["payload"]["count"], path, "Invalid payload count")
    _require(count == len(satellites) == len(releases), path, "Payload count, release events and child trajectories differ")
    _require(not count or orbit_reached, path, "Release without preceding orbit gate")
    _require(not releases or releases[0]["time_s"] >= events["orbit_cutoff_command"][0]["time_s"], path, "Release precedes orbit gate")
    by_time = {}
    for sample in run["samples"]:
        by_time.setdefault(sample["time_s"], []).append(sample)
    for index, (satellite, event) in enumerate(zip(satellites, releases)):
        p = f"{path}.satellites[{index}]"
        _require(type(satellite) is dict and satellite.get("id") == f"satellite_{index+1:02d}", p, "Unexpected or repeated child identity")
        _require(satellite.get("orbital_propagation_only") is True, p, "Child must not imply operational satellite service")
        first, last = _samples(satellite.get("samples"), p+".samples", budget, satellite["id"])
        _require(_same_state(satellite.get("release_state"), first) and _near(first["time_s"], event["time_s"]), p, "Child did not inherit recorded release state/time")
        _require(_near(last["time_s"], run["samples"][-1]["time_s"]), p, "Child propagation did not reach mission horizon")
        receipt = satellite.get("receipt")
        _require(type(receipt) is dict and receipt == event.get("receipt"), p, "Release receipt differs from event")
        _require(receipt.get("schema") == "missionos.sixdof_payload_release.v1", p, "Wrong release receipt schema")
        _require(_same_state(receipt.get("child_state"), first), p, "Receipt child state differs from child trajectory")
        before, after = receipt.get("parent_before_state"), receipt.get("parent_after_state")
        _state(before, p+".parent_before_state")
        _state(after, p+".parent_after_state")
        candidates = by_time.get(event["time_s"], [])
        parents_before = [s for s in candidates if _same_state(s, before)]
        parents_after = [s for s in candidates if _same_state(s, after)]
        _require(bool(parents_before) and bool(parents_after), p, "Release parent states are absent from ship trajectory")
        parent_before, parent_after = parents_before[0], parents_after[0]
        bound, perigee = _orbit(before)
        _require(bound and perigee >= profile["guidance"]["target_perigee_m"]-1000-1e-4
                 and parent_before["dynamic_pressure_pa"] < 1
                 and _norm(before["omega_body_rad_s"]) < profile["guidance"]["release_max_rate_rad_s"], p, "Release gate disagrees with observed parent state")
        mass = receipt.get("child_mass_kg")
        _require(_near(mass, profile["payload"]["mass_each_kg"]) and _near(mass, first["mass_kg"]), p, "Child mass differs from configured/stored mass")
        _require(_near(parent_before["mass_kg"], parent_after["mass_kg"]+mass, 1e-5), p, "Released mass not removed from ship")
        residual = [parent_after["mass_kg"]*a+mass*c-parent_before["mass_kg"]*b
                    for a, b, c in zip(after["v_eci_mps"], before["v_eci_mps"], first["v_eci_mps"])]
        _require(_norm(residual) < .1, p, "Stored release states violate linear momentum conservation")
        _require(receipt.get("velocity_target_inserted") is False and receipt.get("real_dispenser_validated") is False, p, "Invalid release claim boundary")
        bound, perigee = _orbit(last)
        orbit = satellite.get("final_orbit")
        _require(type(orbit) is dict and (orbit.get("status") == "bound") == bound
                 and _near(orbit.get("perigee_altitude_m"), perigee, 1e-3), p, "Child final orbit disagrees with saved state")


def _booster_envelope(booster, profile, last, path):
    outcome, configuration = booster["outcome"], booster.get("guidance_configuration")
    _require(type(configuration) is dict, path, "Missing return envelope configuration")
    defaults = {"contact_speed_limit_mps": 5., "contact_tilt_limit_deg": 10.,
                "contact_rate_limit_rad_s": .05, "return_site_tolerance_m": 1000.}
    configured = profile.get("booster_return", {})
    _require(type(configured) is dict, path, "Invalid booster profile")
    for key, default in defaults.items():
        value = configuration.get(key)
        _require(_number(value) and value > 0 and value == configured.get(key, default), path, "Return envelope differs from profile")
    w, x, y, z = last["q_body_to_eci"]
    axis = [2*(x*z+w*y), 2*(y*z-w*x), 1-2*(x*x+y*y)]
    position = last["r_eci_m"]
    # WGS84 geodetic normal from an independently iterated latitude.
    rho = math.hypot(position[0], position[1])
    e2 = 1-(_B/_A)**2
    latitude = math.atan2(position[2], rho*(1-e2))
    for _ in range(10):
        n = _A/math.sqrt(1-e2*math.sin(latitude)**2)
        latitude = math.atan2(position[2]+e2*n*math.sin(latitude), rho)
    longitude = math.atan2(position[1], position[0])
    up = [math.cos(latitude)*math.cos(longitude), math.cos(latitude)*math.sin(longitude), math.sin(latitude)]
    tilt = math.degrees(math.acos(max(-1., min(1., sum(a*b for a, b in zip(axis, up))))))
    rate = _norm(last["omega_body_rad_s"])
    _require(_near(tilt, outcome.get("final_tilt_deg"), 1e-4) and _near(rate, outcome.get("final_body_rate_rad_s")), path, "Booster terminal attitude/rate differs from trajectory")
    launch = profile.get("launch")
    _require(type(launch) is dict and _number(launch.get("latitude_deg")) and _number(launch.get("longitude_deg")), path, "Missing return target")
    target_coordinates = launch
    if "active_return_site" in booster:
        from hashlib import sha256
        import json
        site = booster["active_return_site"]
        _require(type(site) is dict and site.get("schema") == "missionos.starship_return_site.v1"
            and site.get("site_id") in ("capture", "divert") and site.get("elevation_m") == 0., path, "Invalid explicit return site")
        latitude = math.radians(launch["latitude_deg"])
        prime = _A/math.sqrt(1-e2*math.sin(latitude)**2)
        expected_lon = launch["longitude_deg"]+(math.degrees(30000./(prime*math.cos(latitude))) if site["site_id"] == "divert" else 0.)
        expected_lon = (expected_lon+180.) % 360.-180.
        _require(_near(site.get("latitude_deg"), launch["latitude_deg"], 1e-10)
            and _near(site.get("longitude_deg"), expected_lon, 1e-10), path, "Return site not original capture or declared 30 km east target")
        recorded_hash = sha256(json.dumps(booster["active_return_site"], sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        _require(recorded_hash == booster.get("return_site_sha256"), path, "Return site hash differs")
        target_coordinates = booster["active_return_site"]
    lat = math.radians(target_coordinates["latitude_deg"])
    lon = math.radians(target_coordinates["longitude_deg"])+_EARTH_RATE*last["time_s"]
    n = _A/math.sqrt(1-e2*math.sin(lat)**2)
    target = [n*math.cos(lat)*math.cos(lon), n*math.cos(lat)*math.sin(lon), n*(1-e2)*math.sin(lat)]
    distance = _A*math.atan2(_norm(_cross(position, target)), sum(a*b for a, b in zip(position, target)))
    _require(_near(distance, outcome.get("return_site_distance_m"), 1e-3), path, "Booster range differs from saved position and launch site")
    receipt = booster.get("contact")
    envelope = (receipt is not None and not receipt["initial_overlap"]
                and receipt["surface_normal_speed_mps"] <= 0
                and receipt["surface_relative_speed_mps"] <= configuration["contact_speed_limit_mps"]
                and tilt <= configuration["contact_tilt_limit_deg"]
                and rate <= configuration["contact_rate_limit_rad_s"]
                and distance <= configuration["return_site_tolerance_m"])
    _require(outcome.get("simulated_contact_envelope_met") is envelope, path, "Booster envelope claim differs from observed terminal conditions")
    return envelope


def _verify_run(run, profile, path, budget, *, booster_policy="fixed_v1", catch_config=None):
    first, last, outcome, events, speed = _run_basics(run, path, budget, profile)
    _require(_near(first["time_s"], 0), path, "Main scenario must begin at time zero")
    cutoff = events.get("orbit_cutoff_command", [])
    _require(len(cutoff) <= 1 and type(outcome.get("orbit_gate_reached")) is bool, path, "Invalid orbit gate record")
    _require(outcome["orbit_gate_reached"] == bool(cutoff), path, "Orbit gate claim differs from event record")
    if cutoff:
        candidates = [s for s in run["samples"] if _near(s["time_s"], cutoff[0]["time_s"])]
        _require(any(bound and perigee >= profile["guidance"]["target_perigee_m"]-1e-4 for bound, perigee in map(_orbit, candidates)), path, "Orbit gate lacks a bound observed trajectory above target perigee")
    _payloads(run, profile, path, budget, events, bool(cutoff))
    stage = events.get("stage_separation", [])
    _require(len(stage) <= 1, path, "Repeated stage separation")
    _require(not cutoff or bool(stage) and stage[0]["time_s"] <= cutoff[0]["time_s"], path, "Orbit gate lacks preceding stage separation")
    booster = run.get("booster_run")
    _require(type(outcome.get("booster_return_invoked")) is bool and outcome["booster_return_invoked"] == (booster is not None) == bool(stage), path, "Booster invocation and separation evidence differ")
    booster_summary = None
    recovery_verdict = None
    launch_catch = booster_policy == "predictive_return_v1"
    _require(launch_catch or ("booster_recovery" not in run and "booster_catch_run" not in run), path,
             "Recovery data requires the declared recovery policy")
    _require(launch_catch or not isinstance(booster, dict) or "recovery_record" not in booster, path,
             "Recovery trajectory requires the declared recovery policy")
    if launch_catch:
        _require(run["scenario"] == "launch", path, "Recovery policy requires an actual launch scenario")
        record = run.get("booster_recovery")
        _require(type(record) is dict and record.get("policy_id") == booster_policy, path, "Missing launch recovery record")
        _require(record.get("catch_control_policy") == "net_thrust_trim_v1" and
                 record.get("catch_maximum_duration_s") == 30., path, "Recovery contact policy differs from declared scope")
        if run.get("booster_catch_run") is not None:
            catch = run["booster_catch_run"]
            capture = catch.get("catch_record") if isinstance(catch, dict) else None
            _require(type(capture) is dict and capture.get("control_policy") == "net_thrust_trim_v1" and
                     capture.get("requested_duration_s") == 30. and
                     capture.get("integration_dt_s") == catch_config.get("integration_dt_s"), path,
                     "Contact execution differs from approved policy, time or integration step")
        for key in ("state_reset", "physical_execution", "real_hardware_validated"):
            _require(record.get(key) is False, path, "Invalid launch recovery claim boundary")
    if stage:
        separation = run.get("booster_separation_state")
        _state(separation, path+".booster_separation_state")
        _require(_near(separation["time_s"], stage[0]["time_s"]), path, "Separated booster time differs from event")
        for key, limit in (("mass_residual_kg", 1e-5), ("linear_momentum_residual_kg_mps", .1), ("angular_momentum_residual_kg_m2_s", 1.)):
            _require(_number(stage[0].get(key)) and abs(stage[0][key]) < limit, path, "Stage separation receipt exceeds conservation bound")
        bfirst, blast, boutcome, _, bspeed = _run_basics(booster, path+".booster_run", budget, profile, "booster", recovery=launch_catch)
        _require(booster.get("scenario") == "booster_return" and _same_state(separation, bfirst), path, "Booster did not inherit the separated state")
        for key in ("catch_verified", "landing_hardware_validated", "physical_execution"):
            _require(boutcome.get(key) is False, path+".booster_run.outcome."+key, "Invalid booster claim boundary")
        envelope = _booster_envelope(booster, profile, blast, path+".booster_run")
        booster_summary = {"termination": boutcome["termination"], "contact_speed_mps": bspeed,
                           "simulated_contact_envelope_met": envelope, "catch_verified": False}
        if launch_catch:
            from .starship_booster_recovery_verifier import verify_recovery
            recovery_verdict = verify_recovery(booster, separation, profile, catch_config,
                                              catch_run=run.get("booster_catch_run"))
            _require(recovery_verdict.get("passed") is True, path+".booster_recovery",
                     "Recovery verification rejected: "+str(recovery_verdict.get("issues", []))[:2000])
            for key, value in (("separation_state_preserved", True),
                               ("handoff_reached", recovery_verdict["handoff_reached"]),
                               ("catch_invoked", run.get("booster_catch_run") is not None),
                               ("launch_connected_catch_supported", recovery_verdict["catch_supported_after_handoff"])):
                _require(record.get(key) is value, path+".booster_recovery."+key, "Recovery claim differs from observed chain")
            booster_summary.update(return_site_distance_m=boutcome["return_site_distance_m"],
                                   final_ground_speed_mps=boutcome["final_ground_speed_mps"])
    else:
        _require(run.get("booster_separation_state") is None, path, "Separated state without a separation event")
        if launch_catch:
            _require(run.get("booster_catch_run") is None, path, "Catch cannot precede actual stage separation")
            for key in ("separation_state_preserved", "handoff_reached", "catch_invoked", "launch_connected_catch_supported"):
                _require(record.get(key) is False, path+".booster_recovery."+key, "Recovery cannot precede stage separation")
            recovery_verdict = {"passed": True, "issues": [], "status": "separation_not_reached",
                                "handoff_reached": False, "catch_supported_after_handoff": False}
    if run["scenario"] in SCENARIOS[2:]:
        _require(not stage and not cutoff and not run["satellites"], path, "Initialized response test cannot claim a launch/deployment sequence")
    observed = {"scenario": run["scenario"], "termination": outcome["termination"], "duration_s": outcome["duration_s"],
            "sample_count": len(run["samples"]), "orbit_gate_reached": bool(cutoff), "payload_released_count": len(run["satellites"]),
            "final_contact_point_speed_mps": speed, "booster": booster_summary,
            "booster_recovery_envelope_satisfied": booster_summary["simulated_contact_envelope_met"] if booster_summary else None,
            "booster_catch_verified": False if booster_summary else None,
            "simulation_success": None, "mission_completed": False, "physical_execution": False}
    if launch_catch:
        observed.update(booster_recovery=recovery_verdict,
                        handoff_reached=recovery_verdict["handoff_reached"],
                        launch_connected_catch_supported=recovery_verdict["catch_supported_after_handoff"])
    return observed


def _return_allocation_scope(run, scope, path):
    """Recorded command-cycle use, not processor or dynamics attestation."""
    samples = run.get("samples")
    _require(type(samples) is list, path, "Expected sample list")
    receipts = [s["controller"].get("development_fin_allocation")
                for s in samples if type(s) is dict and type(s.get("controller")) is dict]
    _require(not any(receipts) or scope is not None and scope["bounded_ship_flaps"],
             path, "Undeclared Ship surface allocation", "development_scope")
    if scope is None or not scope["bounded_ship_flaps"]:
        return
    record = run.get("retained_return")
    _require(type(record) is dict, path, "Missing retained-return policy")
    activation = record.get("activation")
    active_time = activation.get("time_s") if type(activation) is dict else None
    _require(active_time is None or _number(active_time), path, "Invalid activation time")
    eligible_count, receipt_count = 0, 0
    potential = False
    for sample in samples:
        _require(type(sample) is dict, path, "Expected sample objects")
        q, time = sample.get("dynamic_pressure_pa"), sample.get("time_s")
        eligible = (active_time is not None and _number(q) and _number(time) and time >= active_time
                    and sample.get("phase") in ("ballistic_return", "landing_burn") and q > 50.)
        potential = potential or eligible
        controller = sample.get("controller")
        receipt = controller.get("development_fin_allocation") if type(controller) is dict else None
        if sample.get("command") is not None and eligible:
            eligible_count += 1
            _require(type(receipt) is dict and receipt.get("surface_family") == "ship_flap"
                     and receipt.get("policy_id") == "finite_moment_priority_fins_v1"
                     and receipt.get("actual_state_assigned") is False
                     and receipt.get("prediction_is_execution") is False,
                     path, "Eligible Ship control cycle lacks its allocation receipt", "allocation_use")
            indices, targets = receipt.get("fin_indices"), receipt.get("command_angles_rad")
            command = sample["command"]
            _require(indices == [3, 4, 5, 6] and type(targets) is list and len(targets) == 4
                     and type(command) is dict and type(command.get("flap_angles_rad")) is list
                     and len(command["flap_angles_rad"]) == 7
                     and all(_near(targets[j], command["flap_angles_rad"][i], 1e-9)
                             for j, i in enumerate(indices)),
                     path, "Allocation receipt differs from recorded flap commands", "allocation_use")
            receipt_count += 1
        elif receipt is not None:
            _require(False, path, "Allocation receipt outside the declared application", "allocation_use")
    _require(not potential or eligible_count > 0 and receipt_count == eligible_count,
             path, "Active high-pressure interval lacks recorded allocation use", "allocation_use")


def verify_study(study: dict, expected_scenario: str | None = None, *, expected_development_return_qualification=None) -> dict:
    """Verify stored-output integrity; ``passed`` never means mission success.

    ``expected_scenario='all'`` requires exactly the five CLI scenarios. Other
    specified scenarios require exactly one matching run. Unknown/malformed
    inputs return bounded issues rather than raising or partially succeeding.
    """
    result = {"schema": "missionos.starship_sixdof_verification.v1", "passed": False, "issues": [],
              "scope": "stored six-DOF output consistency; no dynamics reexecution, source/profile binding, numerical certification, vehicle fidelity or mission-success certification",
              "mission_completed": False, "physical_execution": False, "observed_outcomes": [],
              "checks": ["finite_bounded_json", "state_and_quaternion", "ordered_time", "event_outcome_linkage", "observed_orbit_and_release_gates", "contact_kinematics", "claim_boundaries"]}
    try:
        _require(expected_scenario is None or type(expected_scenario) is str and expected_scenario in (*SCENARIOS, *SUPERVISED_SCENARIOS, "all"), "$", "Unknown expected scenario", "invalid_expected_scenario")
        _json_tree(study)
        _require(type(study) is dict and study.get("schema") == "missionos.starship_sixdof_study.v1", "$", "Unsupported study schema", "invalid_schema")
        provenance = study.get("provenance")
        _require(type(provenance) is dict and provenance.get("physical_execution_invoked") is False
                 and provenance.get("starship_vehicle_validated") is False, "$.provenance", "Study must explicitly remain a nonvalidated simulation")
        booster_policy = provenance.get("booster_policy", "fixed_v1")
        _require(booster_policy in ("fixed_v1", "predictive_return_v1"), "$.provenance", "Unknown booster policy")
        profile = study.get("profile")
        _require(type(profile) is dict and type(profile.get("payload")) is dict and type(profile.get("guidance")) is dict, "$.profile", "Missing profile verification fields")
        payload, guidance = profile["payload"], profile["guidance"]
        _require(type(payload.get("count")) is int and 0 <= payload["count"] <= 1000
                 and _number(payload.get("mass_each_kg")) and payload["mass_each_kg"] > 0, "$.profile.payload", "Invalid configured payload")
        _require(_number(guidance.get("target_perigee_m")) and _number(guidance.get("release_max_rate_rad_s"))
                 and guidance["release_max_rate_rad_s"] > 0, "$.profile.guidance", "Missing orbit/release gate configuration")
        geometry = profile.get("geometry")
        _require(type(geometry) is dict and all(_number(geometry.get(key)) and geometry[key] > 0
                 for key in ("radius_m", "ship_length_m", "booster_length_m")), "$.profile.geometry", "Missing configured envelope geometry")
        runs = study.get("runs")
        _require(type(runs) is list and 1 <= len(runs) <= len(SCENARIOS), "$.runs", "Expected one to five scenarios")
        scope = expected_development_return_qualification
        if scope is not None:
            _require(type(scope) is dict and set(scope) == {"release_limit", "bounded_ship_flaps", "application"}
                     and scope["application"] == "retained_policy_active_only_v1"
                     and type(scope["release_limit"]) is int and 0 <= scope["release_limit"] <= payload["count"]
                     and type(scope["bounded_ship_flaps"]) is bool and len(runs) == 1,
                     "$.runs", "Invalid expected local development scope", "development_scope")
        for run in runs:
            _require(type(run) is dict, "$.runs", "Expected run objects")
            _require(run.get("development_return_qualification") == scope,
                     "$.runs", "Development return scope differs from caller expectation", "development_scope")
            _return_allocation_scope(run, scope, "$.runs")
            if scope is not None:
                _require(run.get("scenario") == "launch" and run.get("outcome", {}).get("payload_released_count", -1) <= scope["release_limit"],
                         "$.runs", "Experiment release limit or scenario differs", "development_scope")
        names = [run.get("scenario") if type(run) is dict else None for run in runs]
        _require(all(type(name) is str and name in (*SCENARIOS, *SUPERVISED_SCENARIOS) for name in names), "$.runs", "Unknown scenario")
        _require(len(set(names)) == len(names), "$.runs", "Duplicate scenarios")
        if booster_policy == "predictive_return_v1":
            _require(names == ["launch"] and type(study.get("catch_profile")) is dict,
                     "$.runs", "Recovery requires one launch and its bound catch configuration")
        if expected_scenario is not None:
            expected = set(SCENARIOS) if expected_scenario == "all" else {expected_scenario}
            _require(set(names) == expected, "$.runs", "Run set differs from approved scenario", "scenario_mismatch")
        budget = [0]
        for index, run in enumerate(runs):
            try:
                observed = _verify_run(run, profile, f"$.runs[{index}]", budget,
                                       booster_policy=booster_policy, catch_config=study.get("catch_profile"))
                result["observed_outcomes"].append(observed)
                if "booster_recovery" in observed:
                    result["booster_recovery"] = observed["booster_recovery"]
                    result["launch_connected_catch_supported"] = observed["launch_connected_catch_supported"]
            except _Invalid as exc:
                result["issues"].append(exc.issue)
        result["passed"] = not result["issues"]
        if "launch_connected_catch_supported" in result:
            result["launch_connected_catch_supported"] = result["passed"] and result["launch_connected_catch_supported"]
    except _Invalid as exc:
        result["issues"].append(exc.issue)
    return result
