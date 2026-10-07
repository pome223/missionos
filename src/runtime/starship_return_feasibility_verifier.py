"""Independent scalar recheck of return/coast margin receipts, no producer import."""
from hashlib import sha256
import json
import math
from pathlib import Path

CORE_SOURCES = ("src/runtime/starship_sixdof_mission.py", "src/runtime/starship_sixdof.py",
    "src/runtime/starship_entry_trim.py", "src/runtime/starship_fin_allocation.py",
    "src/runtime/starship_retained_return.py", "src/runtime/starship_retained_return_verifier.py",
    "src/runtime/starship_sixdof_verifier.py", "src/runtime/starship_sixdof_separation.py",
    "src/runtime/starship_sixdof_contact.py", "src/runtime/starship_attitude_reference.py",
    "src/runtime/starship_physics.py", "src/runtime/starship_wind.py",
    "src/runtime/starship_return_feasibility.py", "src/runtime/starship_return_feasibility_verifier.py",
    "examples/spaceflight/starship-sixdof-profile.json")


def registered(profile, identity):
    certificate, expected_id = load_certificate()
    if (not identity or identity != expected_id or type(certificate) is not dict
            or certificate.get("qualification_complete") is not True
            or certificate.get("retained_counts") != list(range(27))
            or certificate.get("policy_id") != "trimmed_state_terminal_v4"
            or set(certificate.get("source_sha256", {})) != set(CORE_SOURCES)):
        return False
    root = Path(__file__).resolve().parents[2]
    if any(sha256((root/name).read_bytes()).hexdigest() != certificate["source_sha256"][name] for name in CORE_SOURCES):
        return False
    normalized = json.loads(json.dumps(profile, allow_nan=False))
    if not 1599000. <= normalized["ship"]["propellant_kg"] <= 1601000.:
        return False
    normalized["ship"]["propellant_kg"] = 1600000.
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return sha256(encoded).hexdigest() == certificate["normalized_profile_sha256"]


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _norm(a):
    return math.sqrt(sum(x*x for x in a))


def _kinematics(x):
    r, v, q, w = [x[k] for k in ("r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s")]
    for vector, count in zip((r, v, q, w), (3, 3, 4, 3)):
        if type(vector) is not list or len(vector) != count or any(type(a) not in (float, int) or not math.isfinite(a) for a in vector):
            raise ValueError("invalid_navigation")
    radius = _norm(r)
    energy = .5*sum(a*a for a in v)-3.986004418e14/radius
    h = _cross(r, v)
    evec = [a/3.986004418e14-b/radius for a, b in zip(_cross(v, h), r)]
    eccentricity = _norm(evec)
    bound = energy < 0 and eccentricity < 1 and _norm(h) > 1e-6
    peri = sum(a*a for a in h)/(3.986004418e14*(1+eccentricity))-6378137.
    apo = -3.986004418e14/(2*energy)*(1+eccentricity)-6378137. if bound else 1e12
    horizontal = math.hypot(r[0], r[1])
    e2 = (1/298.257223563)*(2-1/298.257223563)
    latitude = math.atan2(r[2], horizontal*(1-e2))
    for _ in range(15):
        n = 6378137./math.sqrt(1-e2*math.sin(latitude)**2)
        latitude = math.atan2(r[2]+e2*n*math.sin(latitude), horizontal)
    longitude = math.atan2(r[1], r[0])
    up = [math.cos(latitude)*math.cos(longitude), math.cos(latitude)*math.sin(longitude), math.sin(latitude)]
    radial = sum(a*b for a, b in zip(up, v))
    tangent = [a-radial*b for a, b in zip(v, up)]
    length = _norm(tangent)
    first = _cross(q[1:], [0., 0., 1.])
    second = _cross(q[1:], first)
    axis = [a+2*q[0]*b+2*c for a, b, c in zip([0., 0., 1.], first, second)]
    angle = math.degrees(math.acos(max(-1., min(1., sum(a*b for a, b in zip(axis, tangent))/length))))
    for key, value in (("perigee_altitude_m", peri), ("apogee_altitude_m", apo), ("body_rate_rad_s", _norm(w)), ("coast_axis_error_deg", angle)):
        if abs(x[key]-value) > 1e-5:
            raise ValueError("navigation_summary_mismatch")
    if x["orbit_status"] != ("bound" if bound else "unbound_or_degenerate"):
        raise ValueError("orbit_status_mismatch")


def verify(proof, certificate, identity, *, time_s, fuel_kg, released_count):
    if proof is None:
        return {"passed": False, "reason": "missing_return_feasibility"}
    try:
        if (proof["schema"] != "missionos.starship_return_feasibility.v1" or proof["time_s"] != time_s
                or proof["inputs"]["time_s"] != time_s or proof["inputs"]["fuel_observed_kg"] != fuel_kg
                or proof["inputs"]["retained_count"] != 26-released_count
                or proof["physical_recovery_certified"] is not False or proof["is_trajectory_rollout"] is not False):
            raise ValueError("state_or_claim_binding")
        x = proof["inputs"]
        _kinematics(x)
        r = x["r_eci_m"]
        v = x["v_eci_mps"]
        radius = math.sqrt(sum(a*a for a in r))
        radial = sum(a*b for a, b in zip(r, v))/radius
        transverse = math.sqrt(max(0., sum(a*a for a in v)-radial**2))
        goal = math.sqrt(max(0., 3.986004418e14*(2/radius-2/(radius+6378137.+x["deorbit_perigee_m"]))))
        dv = max(0., transverse-goal)+abs(radial)
        deorbit = (x["dry_and_payload_mass_kg"]+fuel_kg)*(1-math.exp(-dv/(9.80665*x["engine_isp_s"])))
        supported = (type(certificate) is dict and certificate.get("qualification_complete") is True
                     and identity == proof["certificate_sha256"] and x["backend"] == certificate["backend"]
                     and x["normalized_profile_sha256"] == certificate["normalized_profile_sha256"])
        required = (certificate["maximum_return_consumption_kg"]+29000.
                    +max(0., deorbit-certificate["maximum_reference_deorbit_fuel_kg"])) if supported else None
        margin = fuel_kg-100.-required if required is not None else None
        domain = (supported and x["orbit_status"] == "bound" and 150000. <= x["perigee_altitude_m"] <= 350000.
            and 150000. <= x["apogee_altitude_m"] <= 600000. and x["body_rate_rad_s"] <= .01
            and x["coast_axis_error_deg"] <= 5. and x["available_landing_engines"] == 3)
        coast_required = 120.*x["rcs_thrust_n"]/(x["rcs_isp_s"]*9.80665)*3.+1000.
        coast_margin = fuel_kg-100.-coast_required
        coast = x["orbit_status"] == "bound" and x["perigee_altitude_m"] >= 150000. and x["body_rate_rad_s"] <= .01 and coast_margin >= 0.
        expected = {"profile_and_source_qualified": supported, "within_tested_state_domain": bool(domain),
            "return_required_fuel_kg": required, "return_fuel_margin_kg": margin,
            "return_admitted": bool(domain and margin is not None and margin >= 0.),
            "coast_required_fuel_kg": coast_required, "coast_fuel_margin_kg": coast_margin,
            "bounded_coast_admitted": bool(coast), "maximum_coast_s": 30.}
        for key, value in expected.items():
            actual = proof[key]
            if type(value) is bool or value is None:
                if actual is not value:
                    raise ValueError("margin_or_admission_mismatch")
            elif type(actual) not in (float, int) or not math.isfinite(actual) or abs(actual-value) > 1e-6:
                raise ValueError("numeric_margin_mismatch")
        return {"passed": True, "reason": None}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return {"passed": False, "reason": "invalid_return_feasibility"}


def load_certificate():
    path = Path(__file__).resolve().parents[2]/"docs/assets/starship-state-return-qualification/qualification.json"
    try:
        raw = path.read_bytes()
        return json.loads(raw), sha256(raw).hexdigest()
    except (OSError, ValueError):
        return None, None
