"""Independent consistency checks for a saved retained-payload return policy.

No dynamics, policy, simulator or model-provider module is imported. This checks
record binding and elementary budget identities, not flight fidelity, landing
certification, thermal survivability, or the completeness of unrecorded steps.
An honestly recorded impact remains valid evidence and is never called success.
"""
from __future__ import annotations

import math

_SCHEMA = "missionos.starship_retained_return.v1"
_A = 6_378_137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_MU = 3.986004418e14
_ROTATION = 7.292115e-5
_STATE_FIELDS = ("time_s", "phase", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s",
                 "propellant_kg", "mass_kg", "com_z_m", "inertia_kg_m2", "engine_states", "flap_angles_rad")


class _Invalid(Exception):
    def __init__(self, code, detail):
        self.issue = {"code": code, "detail": detail}


def _require(condition, code, detail):
    if not condition:
        raise _Invalid(code, detail)


def _number(value):
    return type(value) in (int, float) and abs(value) < 1e50 and math.isfinite(value)


def _near(a, b, absolute=1e-6, relative=1e-10):
    return _number(a) and _number(b) and abs(a-b) <= absolute+relative*max(abs(a), abs(b))


def _vector(value, length=3):
    return type(value) is list and len(value) == length and all(_number(x) for x in value)


def _norm(v):
    return math.sqrt(sum(x*x for x in v))


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _rotate(q, v):
    first = _cross(q[1:], v)
    second = _cross(q[1:], first)
    return [x+2*q[0]*a+2*b for x, a, b in zip(v, first, second)]


def _json(root):
    pending, ancestors, count = [(root, 0, False)], set(), 0
    while pending:
        value, depth, leaving = pending.pop()
        if leaving:
            ancestors.remove(id(value))
            continue
        count += 1
        _require(count <= 200_000 and depth <= 32, "input_limit", "Record exceeds bounded JSON traversal")
        if value is None or type(value) is bool:
            continue
        if type(value) in (int, float):
            _require(_number(value), "invalid_number", "Expected finite bounded numbers")
        elif type(value) is str:
            _require(len(value) <= 16000, "input_limit", "Oversized string")
        elif type(value) in (dict, list):
            _require(id(value) not in ancestors and len(value) <= 10000, "invalid_json", "Cyclic or oversized record")
            if type(value) is dict:
                _require(all(type(key) is str and len(key) <= 256 for key in value), "invalid_json", "Invalid object key")
            ancestors.add(id(value))
            pending.append((value, depth, True))
            pending.extend((child, depth+1, False) for child in (value.values() if type(value) is dict else value))
        else:
            raise _Invalid("invalid_json", "Expected JSON-compatible record")


def _positive(value, key, *, zero=False):
    _require(_number(value) and (value >= 0 if zero else value > 0), "profile", f"Invalid {key}")
    return value


def _profile(profile):
    _require(type(profile) is dict, "profile", "Expected the bound vehicle profile")
    _json(profile)
    for key in ("geometry", "ship", "payload", "actuators", "guidance", "integration"):
        _require(type(profile.get(key)) is dict, "profile", f"Missing {key}")
    p, geom, load = profile["ship"], profile["geometry"], profile["payload"]
    for key in ("dry_mass_kg", "propellant_kg", "tank_length_m", "engine_thrust_n", "engine_isp_s"):
        _positive(p.get(key), key)
    for key in ("dry_com_z_m", "tank_center_z_m"):
        _require(_number(p.get(key)), "profile", f"Invalid {key}")
    for key in ("ship_length_m", "radius_m"):
        _positive(geom.get(key), key)
    _require(type(load.get("count")) is int and 0 <= load["count"] <= 10000, "profile", "Invalid payload count")
    _positive(load.get("mass_each_kg"), "payload mass")
    _require(_vector(load.get("dimensions_m")) and all(x > 0 for x in load["dimensions_m"])
             and _number(load.get("center_z_m")), "profile", "Invalid payload geometry")
    tensor = p.get("dry_inertia_kg_m2")
    if tensor is not None:
        _require(type(tensor) is list and len(tensor) == 3 and all(_vector(row) for row in tensor),
                 "profile", "Invalid dry inertia tensor")
        _require(all(tensor[i][i] > 0 for i in range(3)) and
                 all(_near(tensor[i][j], tensor[j][i]) for i in range(3) for j in range(3)),
                 "profile", "Dry inertia tensor must be symmetric with positive diagonal")
    _require(type(p.get("engine_count")) is int and p["engine_count"] >= 3,
             "profile", "At least three main-engine specifications required")
    return profile


def _mass_properties(profile, fuel, retained):
    """Compose dry hull, cylindrical fuel and retained finite rectangular boxes."""
    ship, geometry, payload = profile["ship"], profile["geometry"], profile["payload"]
    dry, load = ship["dry_mass_kg"], retained*payload["mass_each_kg"]
    mass = dry+fuel+load
    com = (dry*ship["dry_com_z_m"]+fuel*ship["tank_center_z_m"]+load*payload["center_z_m"])/mass
    radius, length = geometry["radius_m"], geometry["ship_length_m"]
    dry_tensor = ship.get("dry_inertia_kg_m2")
    diagonal = [dry*(3*radius*radius+length*length)/12]*2+[dry*radius*radius/2]
    inertia = [[(diagonal[i] if i == j else 0.) if dry_tensor is None else dry_tensor[i][j]
                for j in range(3)] for i in range(3)]
    x, y, z = payload["dimensions_m"]
    payload_diagonal = [load*(y*y+z*z)/12, load*(x*x+z*z)/12, load*(x*x+y*y)/12]
    fuel_diagonal = [fuel*(3*radius*radius+ship["tank_length_m"]**2)/12]*2+[fuel*radius*radius/2]
    transverse = (dry*(ship["dry_com_z_m"]-com)**2+fuel*(ship["tank_center_z_m"]-com)**2
                  +load*(payload["center_z_m"]-com)**2)
    for i in range(3):
        inertia[i][i] += payload_diagonal[i]+fuel_diagonal[i]+(transverse if i < 2 else 0.)
    return mass, com, inertia


def _state(state, profile, retained):
    _require(type(state) is dict, "state", "Expected saved state")
    for field, length in (("r_eci_m", 3), ("v_eci_mps", 3), ("q_body_to_eci", 4), ("omega_body_rad_s", 3)):
        _require(_vector(state.get(field), length), "state", f"Invalid {field}")
    _require(_number(state.get("time_s")) and state["time_s"] >= 0 and _norm(state["r_eci_m"]) > 1,
             "state", "Invalid time or position")
    _require(_near(_dot(state["q_body_to_eci"], state["q_body_to_eci"]), 1., 1e-8),
             "state", "Quaternion must be normalized")
    fuel = state.get("propellant_kg")
    _require(_number(fuel) and 0 <= fuel <= profile["ship"]["propellant_kg"], "state", "Invalid propellant")
    mass, com, inertia = _mass_properties(profile, fuel, retained)
    _require(_near(state.get("mass_kg"), mass) and _near(state.get("com_z_m"), com),
             "mass_properties", "Saved mass or centroid disagrees with retained payload composition")
    tensor = state.get("inertia_kg_m2")
    _require(type(tensor) is list and len(tensor) == 3 and all(_vector(row) for row in tensor)
             and all(_near(tensor[i][j], inertia[i][j], 1e-5) for i in range(3) for j in range(3)),
             "mass_properties", "Saved inertia disagrees with finite boxes, fuel and hull composition")
    engines = state.get("engine_states")
    _require(type(engines) is list and len(engines) == profile["ship"]["engine_count"]+12,
             "state", "Missing complete main and RCS engine states")
    for e in engines:
        _require(type(e) is dict and type(e.get("available")) is bool and
                 _number(e.get("throttle")) and 0 <= e["throttle"] <= 1 and
                 _number(e.get("gimbal_x_rad")) and _number(e.get("gimbal_y_rad")), "state", "Invalid engine state")
    flaps = state.get("flap_angles_rad")
    _require(type(flaps) is list and len(flaps) == 3+len(profile["ship"].get("flap_panels", []))
             and all(_number(x) for x in flaps), "state", "Missing complete panel actuator states")
    return mass, com, inertia


def _environment(state):
    """WGS84 ellipsoid normal in inertial axes; rotational symmetry removes time."""
    r = state["r_eci_m"]
    horizontal = math.hypot(r[0], r[1])
    latitude = math.atan2(r[2], horizontal*(1-_E2))
    for _ in range(15):
        n = _A/math.sqrt(1-_E2*math.sin(latitude)**2)
        next_latitude = math.atan2(r[2]+_E2*n*math.sin(latitude), horizontal)
        if abs(next_latitude-latitude) < 1e-14:
            latitude = next_latitude
            break
        latitude = next_latitude
    altitude = horizontal*math.cos(latitude)+r[2]*math.sin(latitude)-_A*math.sqrt(1-_E2*math.sin(latitude)**2)
    relative = [state["v_eci_mps"][0]+_ROTATION*r[1], state["v_eci_mps"][1]-_ROTATION*r[0], state["v_eci_mps"][2]]
    geometric = min(86000., max(0., altitude))
    h = 6356766.*geometric/(6356766.+geometric)
    boundaries = [0., 11000., 20000., 32000., 47000., 51000., 71000., 84852.04584490575]
    lapse = [-.0065, 0., .001, .0028, 0., -.0028, -.002]
    temperature = 288.15
    for low, high, rate in zip(boundaries, boundaries[1:], lapse):
        if h <= low:
            break
        temperature += rate*(min(h, high)-low)
        if h <= high:
            break
    sound = math.sqrt(1.4*(8314.32/28.9644)*temperature)
    return {"altitude_m": altitude, "relative": relative, "mach": _norm(relative)/sound}


def _largest_eigenvalue(tensor):
    # Closed-form symmetric 3x3 eigenvalue; independent of producer numpy path.
    off = tensor[0][1]**2+tensor[0][2]**2+tensor[1][2]**2
    if off < 1e-24:
        return max(tensor[i][i] for i in range(3))
    mean = sum(tensor[i][i] for i in range(3))/3
    scale = math.sqrt((sum((tensor[i][i]-mean)**2 for i in range(3))+2*off)/6)
    matrix = [[(tensor[i][j]-(mean if i == j else 0))/scale for j in range(3)] for i in range(3)]
    determinant = (matrix[0][0]*(matrix[1][1]*matrix[2][2]-matrix[1][2]*matrix[2][1])
                   -matrix[0][1]*(matrix[1][0]*matrix[2][2]-matrix[1][2]*matrix[2][0])
                   +matrix[0][2]*(matrix[1][0]*matrix[2][1]-matrix[1][1]*matrix[2][0]))
    angle = math.acos(max(-1., min(1., determinant/2)))/3
    return mean+2*scale*math.cos(angle)


def _budget(state, profile, retained):
    mass, com, inertia = _state(state, profile, retained)
    environment = _environment(state)
    _require(_near(state.get("altitude_m"), environment["altitude_m"], 1e-5), "state", "Altitude differs from ellipsoid position")
    ship, actuators, guidance = profile["ship"], profile["actuators"], profile["guidance"]
    r, velocity = state["r_eci_m"], environment["relative"]
    radius = _norm(r)
    up = [x/radius for x in r]
    vertical = _dot(velocity, up)
    angle = math.acos(max(-1., min(1., _dot(_rotate(state["q_body_to_eci"], [0, 0, 1]), up))))
    rate = math.hypot(*state["omega_body_rad_s"][:2])
    _require(type(ship.get("gimbal_engine_count")) is int and 0 <= ship["gimbal_engine_count"] <= ship["engine_count"],
             "profile", "Invalid gimbal engine count")
    available = [i for i in range(min(3, ship["gimbal_engine_count"])) if state["engine_states"][i]["available"]]
    anchors = ship.get("engine_positions_body_m")
    _require(type(anchors) is list and len(anchors) >= 3 and all(_vector(row) for row in anchors),
             "profile", "Missing engine anchors")
    for key in ("max_gimbal_deg", "throttle_tau_s", "gimbal_tau_s", "gimbal_rate_deg_s"):
        _positive(actuators.get(key), key, zero=key == "max_gimbal_deg")
    for key in ("flip_min_throttle", "max_angular_acceleration_rad_s2", "flip_altitude_m"):
        _positive(guidance.get(key), key)
    thrust = len(available)*ship["engine_thrust_n"]
    moment = sum(ship["engine_thrust_n"]*guidance["flip_min_throttle"]
                 *math.sin(math.radians(actuators["max_gimbal_deg"]))*abs(com-anchors[i][2]) for i in available)
    largest = _largest_eigenvalue(inertia)
    _require(largest > 0, "mass_properties", "Inertia must have positive largest eigenvalue")
    alpha = min(guidance["max_angular_acceleration_rad_s2"], moment/largest)
    gravity = _MU/radius**2
    net = thrust/mass-gravity
    lag = 4*max(actuators["throttle_tau_s"], actuators["gimbal_tau_s"])+actuators["max_gimbal_deg"]/actuators["gimbal_rate_deg_s"]
    expected = {"time_s": state["time_s"], "altitude_m": state["altitude_m"], "vertical_speed_mps": vertical,
                "mach": environment["mach"], "mass_kg": mass, "propellant_kg": state["propellant_kg"],
                "gravity_mps2": gravity, "thrust_axis_angle_rad": angle, "transverse_rate_rad_s": rate,
                "available_landing_engines": len(available), "available_thrust_n": thrust,
                "tvc_moment_estimate_nm": moment, "inertia_upper_kg_m2": largest,
                "slew_accel_estimate_rad_s2": alpha, "aligned_net_acceleration_mps2": net,
                "actuator_lag_s": lag, "applicable": False, "trigger": False,
                "preparation_time_s": None, "required_altitude_m": None,
                "fuel_budget_estimate_kg": None, "fuel_margin_estimate_kg": None}
    if alpha <= 0 or net <= 0 or state["propellant_kg"] <= 0:
        expected["reason"] = "insufficient_modeled_propulsion"
        return expected
    preparation = rate/alpha+2*math.sqrt((angle+rate*rate/(2*alpha))/alpha)+lag
    descent = max(0., -vertical)
    after = descent+gravity*preparation
    required = max(guidance["flip_altitude_m"], descent*preparation+gravity*preparation*preparation/2
                   +after*after/(2*net)+profile["geometry"]["ship_length_m"])
    fuel = thrust/(ship["engine_isp_s"]*9.80665)*(preparation+after/net)
    applicable = vertical < 0 and environment["mach"] <= 1
    expected.update(preparation_time_s=preparation, required_altitude_m=required, fuel_budget_estimate_kg=fuel,
                    fuel_margin_estimate_kg=state["propellant_kg"]-fuel, applicable=applicable,
                    trigger=applicable and state["altitude_m"] <= required,
                    reason="subsonic_descent" if applicable else "outside_subsonic_descent")
    return expected


def _same_state(a, b):
    return all(a.get(key) == b.get(key) for key in _STATE_FIELDS)


def _linked_state(record, samples, marker, profile, retained):
    state = record.get("state")
    _state(state, profile, retained)
    _require(_near(record.get("time_s", state["time_s"]), state["time_s"]), "state_binding", "Record time differs from saved state")
    candidates = [sample for sample in samples if sample.get("retained_return") == marker]
    _require(len(candidates) == 1 and state.get("retained_return") == marker and _same_state(state, candidates[0]),
             "state_binding", f"Expected one matching saved {marker} state")
    _require(state.get("body_id") == candidates[0].get("body_id") == "ship", "state_binding", "Wrong body identity")
    return state


def _check_budget(actual, state, profile, retained):
    expected = _budget(state, profile, retained)
    _require(type(actual) is dict and set(actual) == set(expected), "budget_schema", "Budget fields differ from fixed policy")
    for key, value in expected.items():
        valid = _near(actual[key], value, 1e-5) if type(value) in (int, float) else type(actual[key]) is type(value) and actual[key] == value
        if key == "available_landing_engines":
            valid = type(actual[key]) is int and actual[key] == value
        _require(valid, "budget_identity", f"Budget {key} differs from saved state and vehicle profile")
    return expected


def _contact_outcome(run):
    outcome = run.get("outcome")
    _require(type(outcome) is dict, "outcome", "Expected saved terminal outcome")
    termination = outcome.get("termination")
    receipt = outcome.get("contact_receipt")
    if termination not in ("surface_impact", "low_speed_surface_contact"):
        _require(receipt is None, "outcome", "Non-contact outcome cannot carry contact receipt")
        return False, None
    _require(type(receipt) is dict and receipt.get("contact") is True and receipt.get("landing_verified") is False
             and receipt.get("starship_vehicle_validated") is False, "outcome", "Missing bounded contact observation")
    relative = receipt.get("surface_relative_velocity_mps")
    _require(_vector(relative), "outcome", "Missing contact-point relative velocity")
    speed = _norm(relative)
    _require(_near(receipt.get("surface_relative_speed_mps"), speed), "outcome", "Contact speed differs from velocity vector")
    _require((speed > 5) == (termination == "surface_impact"), "outcome", "Termination differs from contact speed")
    return speed <= 5, speed


def verify_retained_return(run, profile, expected_policy="fixed_v1"):
    """Check stored policy evidence; a pass does not imply a successful return.

    The preceding saved step and every earlier stored ballistic sample are
    checked for a premature crossing. Unrecorded integration steps are outside
    this verifier's scope. Full trajectory/contact physics and approval/source
    binding remain the caller's separate verification responsibilities.
    """
    result = {"schema": "missionos.starship_retained_return_verification.v1", "passed": False,
              "issues": [], "policy_id": None, "policy_active": False, "trigger_verified": False,
              "observed_low_speed_contact": False, "contact_point_speed_mps": None,
              "landing_verified": False, "starship_vehicle_validated": False,
              "mission_completed": False, "physical_execution": False,
              "scope": "saved composition, policy budget, adjacent crossing and event/state binding; not flight certification"}
    try:
        _require(type(expected_policy) is str and expected_policy in ("fixed_v1", "mass_state_terminal_v1", "mass_state_terminal_v2", "mass_state_terminal_v3"),
                 "approval_policy", "Unknown expected policy")
        _require(type(run) is dict, "run", "Expected saved run")
        record = run.get("retained_return")
        _require(type(record) is dict, "record", "Missing retained-return record")
        _json(record)
        _require(record.get("schema") == _SCHEMA and record.get("policy_id") == expected_policy,
                 "approval_policy", "Recorded policy differs from expected approved policy")
        _require(record.get("landing_verified") is False and record.get("starship_vehicle_validated") is False,
                 "claim_boundary", "Policy evidence cannot certify the vehicle or landing")
        result["policy_id"] = expected_policy
        count = record.get("evaluation_count")
        _require(type(count) is int and 0 <= count <= 1_000_000, "evaluation_count", "Invalid evaluation count")
        samples, events = run.get("samples"), run.get("events")
        _require(type(samples) is list and 1 <= len(samples) <= 250_000 and all(type(s) is dict for s in samples),
                 "samples", "Expected bounded saved trajectory")
        _require(type(events) is list and len(events) <= 20000 and all(type(e) is dict for e in events),
                 "events", "Expected bounded event list")
        prior_time = -1.
        for sample in samples:
            time = sample.get("time_s")
            _require(_number(time) and time >= prior_time, "samples", "Sample time regressed")
            prior_time = time
        grouped = {}
        for event in events:
            _require(type(event.get("event")) is str and _number(event.get("time_s")), "events", "Malformed event")
            grouped.setdefault(event["event"], []).append(event)
        policy_events = grouped.get("retained_return_activated", [])+grouped.get("retained_return_terminal_trigger", [])
        if expected_policy == "fixed_v1":
            _require(record.get("status") == "fixed" and record.get("activation") is None and record.get("trigger") is None
                     and count == 0 and not policy_events and not any("retained_return" in s for s in samples),
                     "fixed_policy", "Fixed policy cannot activate or contain adaptive commands")
        else:
            _profile(profile)
            _require(run.get("scenario") == "deployment_no_effect", "scenario", "Adaptive policy requires the approved retained-payload scenario")
            released = run.get("outcome", {}).get("payload_released_count") if type(run.get("outcome")) is dict else None
            release_events = grouped.get("payload_released", [])
            _require(type(released) is int and released == len(release_events) and 0 <= released <= profile["payload"]["count"],
                     "payload", "Released count disagrees with physical release events")
            retained = profile["payload"]["count"]-released
            activation, trigger = record.get("activation"), record.get("trigger")
            returns = grouped.get("return_requested", [])
            orbit_events = grouped.get("orbit_cutoff_command", [])
            _require(len(returns) <= 1 and len(orbit_events) <= 1, "activation", "Ambiguous return request or orbit cutoff")
            # Event deletion must not turn an executed return into a valid
            # not-activated policy. Physical phases and elapsed coast provide
            # positive evidence independent of the policy's own markers.
            activation_due = any(s.get("phase") in ("deorbit_slew", "deorbit_burn") for s in samples)
            if orbit_events:
                coast = _positive(profile["guidance"].get("coast_before_return_s"), "coast before return")
                maximum_coast_step = 2*_positive(profile["integration"].get("coast_dt_s"), "coast dt")
                deadline = orbit_events[0]["time_s"]+coast
                activation_due = activation_due or any(
                    s["time_s"] >= deadline-1e-7 and s.get("phase") in ("ballistic_return", "landing_burn") for s in samples)
                activation_due = activation_due or any(
                    s["time_s"] >= deadline-1e-7 and s.get("phase") == "orbital_coast" for s in samples[:-1])
                # A horizon may stop at the first deadline-crossing state,
                # before the next loop iteration processes the return.
                activation_due = activation_due or samples[-1]["time_s"] > deadline+maximum_coast_step+1e-7
            if activation is None:
                _require(record.get("status") == "not_activated" and trigger is None and count == 0
                         and not policy_events and not any("retained_return" in s for s in samples)
                         and (retained == 0 or (not returns and not activation_due)), "activation", "Missing or contradictory policy activation")
            else:
                _require(type(activation) is dict and retained > 0 and len(returns) == 1,
                         "activation", "Activation requires a return request with retained payload")
                _require(type(activation.get("payload_retained_count")) is int and activation["payload_retained_count"] == retained
                         and _near(activation.get("payload_retained_mass_kg"), retained*profile["payload"]["mass_each_kg"]),
                         "payload", "Activation payload accounting differs from release record")
                active_state = _linked_state(activation, samples, "activation", profile, retained)
                _require(active_state["phase"] == "orbital_coast" and _near(active_state["time_s"], returns[0]["time_s"]),
                         "activation", "Activation must use pre-deorbit return-request state")
                orbit_events = grouped.get("orbit_cutoff_command", [])
                coast = _positive(profile["guidance"].get("coast_before_return_s"), "coast before return")
                coast_step = 2*_positive(profile["integration"].get("coast_dt_s"), "coast dt")
                _require(len(orbit_events) == 1 and
                         -1e-7 <= active_state["time_s"]-orbit_events[0]["time_s"]-coast <= coast_step+1e-7,
                         "return_timing", "Activation changed the configured orbit-coast return time")
                acts = grouped.get("retained_return_activated", [])
                _require(len(acts) == 1 and acts[0].get("policy_id") == expected_policy
                         and _near(acts[0]["time_s"], active_state["time_s"]), "activation", "Activation event differs from saved state")
                for release in release_events:
                    _require(release["time_s"] <= active_state["time_s"], "payload", "Payload release after retained-return activation")
                observed_evaluations = {s["time_s"] for s in samples[:-1]
                                        if s["time_s"] > active_state["time_s"] and s.get("phase") == "ballistic_return"}
                _require(count >= len(observed_evaluations), "evaluation_count",
                         "Evaluation count is below distinct observed preterminal ballistic states")
                result["policy_active"] = True
                if trigger is None:
                    _require(record.get("status") == "active" and not grouped.get("retained_return_terminal_trigger")
                             and not any(s.get("retained_return") in ("trigger", "previous") for s in samples),
                             "trigger", "Active policy cannot claim a terminal trigger without evidence")
                    _require(not grouped.get("flip_and_landing_command")
                             and not any(s.get("phase") == "landing_burn" for s in samples),
                             "trigger", "Active record concealed executed terminal guidance")
                    # The final horizon sample may never have been evaluated;
                    # earlier saved ballistic states must have remained outside.
                    for sample in samples[:-1]:
                        if sample["time_s"] > active_state["time_s"] and sample.get("phase") == "ballistic_return":
                            _require(_budget(sample, profile, retained)["trigger"] is False,
                                     "earlier_crossing", "An earlier saved state already required terminal preparation")
                else:
                    _require(type(trigger) is dict and record.get("status") == "triggered" and count >= 1,
                             "trigger", "Invalid triggered policy record")
                    state = _linked_state(trigger, samples, "trigger", profile, retained)
                    _require(state["phase"] == "ballistic_return" and state["time_s"] > active_state["time_s"],
                             "trigger", "Trigger must follow activation during ballistic return")
                    calculated = _check_budget(trigger.get("budget"), state, profile, retained)
                    _require(calculated["trigger"] is True, "trigger", "Saved state does not cross applicable preparation budget")
                    terminal = grouped.get("retained_return_terminal_trigger", [])
                    flips = grouped.get("flip_and_landing_command", [])
                    _require(len(terminal) == len(flips) == 1 and terminal[0].get("policy_id") == expected_policy
                             and _near(terminal[0]["time_s"], state["time_s"])
                             and _near(flips[0]["time_s"], state["time_s"])
                             and _near(terminal[0].get("required_altitude_m"), calculated["required_altitude_m"], 1e-5),
                             "trigger_event", "Policy trigger and actual guidance command differ")
                    previous = trigger.get("previous")
                    if count > 1:
                        _require(type(previous) is dict, "previous", "Missing preceding evaluation")
                        before = _linked_state(previous, samples, "previous", profile, retained)
                        dt = state["time_s"]-before["time_s"]
                        maximum_step = 2*max(_positive(profile["integration"].get("powered_dt_s"), "powered dt"),
                                             _positive(profile["integration"].get("coast_dt_s"), "coast dt"))
                        _require(before["phase"] == "ballistic_return" and before["time_s"] > active_state["time_s"]
                                 and 0 < dt <= maximum_step+1e-7, "previous", "Preceding evaluation is not an adjacent forward step")
                        _require(_check_budget(previous.get("budget"), before, profile, retained)["trigger"] is False,
                                 "previous", "Policy delayed after a previously crossed budget")
                    else:
                        _require(previous is None, "previous", "First evaluation cannot have a previous budget")
                    for sample in samples:
                        if active_state["time_s"] < sample["time_s"] < state["time_s"] and sample.get("phase") == "ballistic_return":
                            _require(_budget(sample, profile, retained)["trigger"] is False, "earlier_crossing", "An earlier saved state already required terminal preparation")
                    result["trigger_verified"] = True
        low_speed, speed = _contact_outcome(run)
        result.update(passed=True, observed_low_speed_contact=low_speed, contact_point_speed_mps=speed)
    except _Invalid as error:
        result["issues"].append(error.issue)
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError) as error:
        result["issues"].append({"code": "malformed_record", "detail": f"Invalid saved structure ({type(error).__name__})"})
    return result
