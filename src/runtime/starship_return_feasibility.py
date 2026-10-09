"""Source-bound model-domain admission and quantitative return/coast margins.

This is not a flight certificate or an oracle. Empirical guidance qualification
is combined with fresh measured state and an analytic deorbit fuel correction.
Outside that tested profile/domain, return is unknown and cannot be dispatched.
"""
from hashlib import sha256
import importlib.metadata
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CERTIFICATE = "docs/assets/starship-state-return-qualification/qualification.json"
POLICY = "trimmed_state_terminal_v4"
# Numerical matching tolerances for interpolation of saved 2 s coast samples.
# These are not a qualified physical perturbation box or a reachability proof.
MATCH_LIMITS = {"sample_gap_s": 2.01, "position_m": 12., "velocity_mps": .05,
                "attitude_deg": .1, "body_rate_rad_s": .0002, "fuel_kg": 100.1}
CORE_SOURCES = ("src/runtime/starship_sixdof_mission.py", "src/runtime/starship_sixdof.py",
    "src/runtime/starship_entry_trim.py", "src/runtime/starship_fin_allocation.py",
    "src/runtime/starship_retained_return.py", "src/runtime/starship_retained_return_verifier.py",
    "src/runtime/starship_sixdof_verifier.py", "src/runtime/starship_sixdof_separation.py",
    "src/runtime/starship_sixdof_contact.py", "src/runtime/starship_attitude_reference.py",
    "src/runtime/starship_physics.py", "src/runtime/starship_wind.py",
    "src/runtime/starship_return_feasibility.py", "src/runtime/starship_return_feasibility_verifier.py",
    "examples/spaceflight/starship-sixdof-profile.json")


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def sources():
    return {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in CORE_SOURCES}


def backend():
    result = {}
    for name in ("numpy", "scipy"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def normalized_profile(profile):
    result = json.loads(json.dumps(profile, allow_nan=False))
    result["ship"]["propellant_kg"] = 1600000.
    return result


def load_certificate():
    try:
        raw = (ROOT/CERTIFICATE).read_bytes()
        value = json.loads(raw)
        return value, sha256(raw).hexdigest()
    except (OSError, ValueError):
        return None, None


def readiness(profile, *, check_backend=True):
    value, identity = load_certificate()
    if (type(value) is not dict or value.get("schema") != "missionos.starship_state_return_qualification.v2"
            or value.get("qualification_complete") is not True or value.get("policy_id") != POLICY
            or value.get("retained_counts") != list(range(27))
            or value.get("matching_tolerances") != MATCH_LIMITS or len(value.get("coast_corridors", [])) != 39):
        return None, identity, "return_qualification_missing_or_incomplete"
    if value.get("source_sha256") != sources():
        return None, identity, "return_qualification_source_mismatch"
    if (value.get("normalized_profile_sha256") != digest(normalized_profile(profile))
            or not 1599000. <= profile["ship"]["propellant_kg"] <= 1601000.):
        return None, identity, "return_qualification_profile_mismatch"
    if check_backend and value.get("backend") != backend():
        return None, identity, "return_qualification_backend_mismatch"
    return value, identity, None


def certificate_ready(profile, *, check_backend=True):
    value, identity, _ = readiness(profile, check_backend=check_backend)
    return value, identity


def match_coast(inputs, certificate):
    """Match one inventory-specific saved trajectory; never extrapolate time."""
    if certificate is None or certificate.get("matching_tolerances") != MATCH_LIMITS:
        return None
    now = inputs["time_s"]
    for corridor in certificate.get("coast_corridors", []):
        if corridor["retained_count"] != inputs["retained_count"]:
            continue
        for a, b in zip(corridor["samples"], corridor["samples"][1:]):
            gap = b[0]-a[0]
            if not (0 < gap <= MATCH_LIMITS["sample_gap_s"] and a[0] <= now <= b[0]):
                continue
            fraction = (now-a[0])/gap
            predicted = [x+fraction*(y-x) for x, y in zip(a[1:], b[1:])]
            # Quaternions describe the same orientation with either sign.
            qa, qb = a[7:11], b[7:11]
            sign = -1. if sum(x*y for x, y in zip(qa, qb)) < 0 else 1.
            q = [x+fraction*(sign*y-x) for x, y in zip(qa, qb)]
            length = math.sqrt(sum(x*x for x in q))
            dot = abs(sum(x*y/length for x, y in zip(q, inputs["q_body_to_eci"])))
            errors = {"position_m": math.dist(predicted[:3], inputs["r_eci_m"]),
                "velocity_mps": math.dist(predicted[3:6], inputs["v_eci_mps"]),
                "attitude_deg": math.degrees(2*math.acos(min(1., dot))),
                "body_rate_rad_s": math.dist(predicted[10:13], inputs["omega_body_rad_s"]),
                "fuel_kg": abs(predicted[13]-inputs["fuel_observed_kg"])}
            if all(error <= MATCH_LIMITS[key] for key, error in errors.items()):
                return {"case_id": corridor["case_id"], "sample_times_s": [a[0], b[0]],
                        "errors": errors}
    return None


def deorbit_fuel(inputs):
    """Retrograde vis-viva estimate with radial-speed correction; not a rollout."""
    r, v = inputs["r_eci_m"], inputs["v_eci_mps"]
    radius = math.sqrt(sum(x*x for x in r))
    radial = sum(x*y for x, y in zip(r, v))/radius
    tangent = math.sqrt(max(0., sum(x*x for x in v)-radial*radial))
    rp = 6378137.+inputs["deorbit_perigee_m"]
    target_speed = math.sqrt(max(0., 3.986004418e14*(2/radius-2/(radius+rp))))
    delta_v = max(0., tangent-target_speed)+abs(radial)
    mass = inputs["dry_and_payload_mass_kg"]+inputs["fuel_observed_kg"]
    return mass*(1-math.exp(-delta_v/(inputs["engine_isp_s"]*9.80665)))


def calculate(inputs, certificate):
    required = None
    supported = certificate is not None
    if supported:
        required = (certificate["maximum_return_consumption_kg"]+28000.+1000.
            +max(0., deorbit_fuel(inputs)-certificate["maximum_reference_deorbit_fuel_kg"]))
    matched = match_coast(inputs, certificate)
    domain = (supported and matched is not None and inputs["orbit_status"] == "bound"
        and inputs["body_rate_rad_s"] <= .01 and inputs["coast_axis_error_deg"] <= 5.
        and inputs["available_landing_engines"] == 3 and 0 <= inputs["retained_count"] <= 26)
    margin = inputs["fuel_observed_kg"]-100.-required if required is not None else None
    # Even all twelve RCS jets at full thrust cannot consume more than this
    # over the delegated 30 s coast. This bound does not certify later recovery.
    coast_required = 12*inputs["rcs_thrust_n"]/(inputs["rcs_isp_s"]*9.80665)*30.+1000.
    coast_margin = inputs["fuel_observed_kg"]-100.-coast_required
    coast = (inputs["orbit_status"] == "bound" and inputs["perigee_altitude_m"] >= 150000.
             and inputs["body_rate_rad_s"] <= .01 and coast_margin >= 0.)
    return {"schema": "missionos.starship_return_feasibility.v1", "time_s": inputs["time_s"],
        "inputs": inputs, "profile_and_source_qualified": supported, "within_tested_state_domain": bool(domain),
        "coast_evidence_match": matched,
        "return_required_fuel_kg": required, "return_fuel_margin_kg": margin,
        "return_admitted": bool(domain and margin is not None and margin >= 0.),
        "coast_required_fuel_kg": coast_required, "coast_fuel_margin_kg": coast_margin,
        "bounded_coast_admitted": bool(coast), "maximum_coast_s": 30.,
        "is_trajectory_rollout": False, "physical_recovery_certified": False,
        "claim_scope": "inventory-specific saved coast interpolation with explicit numerical tolerances and fresh margins; not a physical perturbation envelope or per-trajectory proof"}


def terminal_receipt(observed, budget, certificate):
    """Record a late limit violation; entry cannot be undone by an exception."""
    required = certificate["maximum_terminal_consumption_kg"]+29000. if certificate else None
    lower = observed["propellant_kg"]-100.
    passed = (required is not None and lower >= required and budget["available_landing_engines"] == 3
              and budget["aligned_net_acceleration_mps2"] > 0)
    return {"time_s": observed["time_s"], "required_fuel_kg": required,
            "fuel_lower_bound_kg": lower, "available_landing_engines": budget["available_landing_engines"],
            "aligned_net_acceleration_mps2": budget["aligned_net_acceleration_mps2"], "passed": passed,
            "continuation": "qualified_terminal_guidance" if passed else "unqualified_best_effort_terminal_guidance",
            "physical_recovery_certified": False}


def evaluate(profile, state, released_count, fuel_observed_kg):
    from . import starship_physics as env, starship_sixdof as dyn
    point = env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg)
    orbit = env.orbital_elements(point)
    up, east, _ = env.local_frame(point)
    radial = env.dot(state.v_eci_mps, up)
    horizontal = env.add(state.v_eci_mps, env.scale(up, -radial))
    tangent = env.unit(horizontal) if env.norm(horizontal) > 1. else east
    axis = dyn.rotate(state.q_body_to_eci, (0., 0., 1.))
    inputs = {"time_s": state.time_s, "r_eci_m": list(state.r_eci_m), "v_eci_mps": list(state.v_eci_mps),
        "q_body_to_eci": list(state.q_body_to_eci), "omega_body_rad_s": list(state.omega_body_rad_s),
        "fuel_observed_kg": fuel_observed_kg, "retained_count": profile["payload"]["count"]-released_count,
        "orbit_status": orbit["status"], "perigee_altitude_m": orbit.get("perigee_altitude_m", -1.),
        "apogee_altitude_m": orbit.get("apogee_altitude_m") if orbit.get("apogee_altitude_m") is not None else 1e12,
        "body_rate_rad_s": env.norm(state.omega_body_rad_s),
        "coast_axis_error_deg": math.degrees(math.acos(max(-1., min(1., env.dot(axis, tangent))))),
        "available_landing_engines": sum(e.available for e in state.engine_states[:3]),
        "dry_and_payload_mass_kg": profile["ship"]["dry_mass_kg"]+(profile["payload"]["count"]-released_count)*profile["payload"]["mass_each_kg"],
        "engine_isp_s": profile["ship"]["engine_isp_s"], "deorbit_perigee_m": profile["guidance"]["deorbit_perigee_m"],
        "rcs_thrust_n": profile["actuators"]["rcs_thrust_n"], "rcs_isp_s": profile["actuators"]["rcs_isp_s"],
        "normalized_profile_sha256": digest(normalized_profile(profile)), "backend": backend()}
    certificate, identity = certificate_ready(profile)
    return {**calculate(inputs, certificate), "certificate_sha256": identity}
