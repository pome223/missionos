#!/usr/bin/env python3
"""Independent DOP853 diagnostic for stored passive 3D satellite coasts.

This file deliberately imports neither the production physics kernel nor its
integrator. It independently expresses central/J2 gravity, ellipsoid height and
co-rotating drag, using the SAME declared, unvalidated atmosphere above 86 km.
Agreement tests numerical implementation, NOT Starship or atmospheric fidelity.
It does not authenticate or verify the mission artifact; run the study verifier
separately. scipy is an optional local diagnostic dependency.

References: https://docs.scipy.org/doc/scipy/reference/generated/scipy.integrate.solve_ivp.html
WGS84: https://earth-info.nga.mil/?action=wgs84&dir=wgs84
J2: https://ntrs.nasa.gov/citations/20160006944 (slide 33)
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
from pathlib import Path
import sys


MU = 3.986004418e14
EQUATORIAL_RADIUS = 6378137.0
FLATTENING = 1 / 298.257223563
ECCENTRICITY2 = FLATTENING * (2-FLATTENING)
OMEGA = 7.292115e-5
J2 = 1.08262982e-3
RTOL = 1e-12
TIGHT_RTOL = 1e-13
ATOL = (1e-5, 1e-5, 1e-5, 1e-8, 1e-8, 1e-8)
MAX_POSITION_DIFFERENCE_M = 1.0
MAX_VELOCITY_DIFFERENCE_MPS = 0.001


def ellipsoid_height(r) -> float:
    """Iterated geodetic latitude; independent implementation, no runtime import."""
    xy = math.hypot(r[0], r[1])
    if xy < 1e-8:
        return abs(r[2])-EQUATORIAL_RADIUS*(1-FLATTENING)
    lat = math.atan2(r[2], xy*(1-ECCENTRICITY2))
    for _ in range(15):
        n = EQUATORIAL_RADIUS/math.sqrt(1-ECCENTRICITY2*math.sin(lat)**2)
        updated = math.atan2(r[2]+n*ECCENTRICITY2*math.sin(lat), xy)
        if abs(updated-lat) < 1e-15:
            lat = updated
            break
        lat = updated
    return xy*math.cos(lat)+r[2]*math.sin(lat)-EQUATORIAL_RADIUS*math.sqrt(1-ECCENTRICITY2*math.sin(lat)**2)


def _density_at_86km() -> float:
    # Independently integrate the seven COESA hydrostatic layers; temperature is
    # molecular-scale. Constants match the explicitly declared production model.
    radius = 6356766.0
    h86 = radius*86000/(radius+86000)
    boundaries = (0, 11000, 20000, 32000, 47000, 51000, 71000, h86)
    gradients = (-0.0065, 0, 0.001, 0.0028, 0, -0.0028, -0.002)
    t, p, gas = 288.15, 101325.0, 8314.32/28.9644
    for low, high, lapse in zip(boundaries, boundaries[1:], gradients):
        new_t = t+lapse*(high-low)
        p *= math.exp(-9.80665*(high-low)/(gas*t)) if lapse == 0 else math.exp(-9.80665/(gas*lapse)*math.log(new_t/t))
        t = new_t
    return p/(gas*t)


RHO_86 = _density_at_86km()


def reference_derivative(_time, state, mass_kg: float, area_m2: float, cd: float,
                         *, j2: bool = True, atmosphere: bool = True):
    x, y, z, vx, vy, vz = state
    r2 = x*x+y*y+z*z
    r = math.sqrt(r2)
    acceleration = [-MU*x/r**3, -MU*y/r**3, -MU*z/r**3]
    if j2:
        factor = 1.5*J2*MU*EQUATORIAL_RADIUS**2/r**5
        z_fraction = z*z/r2
        acceleration[0] += factor*x*(5*z_fraction-1)
        acceleration[1] += factor*y*(5*z_fraction-1)
        acceleration[2] += factor*z*(5*z_fraction-3)
    if atmosphere and area_m2 and cd:
        altitude = ellipsoid_height(state[:3])
        if altitude <= 86000:
            raise ValueError("DOP853 coast diagnostic is restricted to altitude > 86 km")
        density = RHO_86*math.exp(-(altitude-86000)/15000) if altitude < 1_000_000 else 0.0
        relative = (vx+OMEGA*y, vy-OMEGA*x, vz)
        speed = math.sqrt(sum(v*v for v in relative))
        multiplier = -0.5*density*area_m2*cd*speed/mass_kg
        acceleration = [a+multiplier*v for a, v in zip(acceleration, relative)]
    return [vx, vy, vz, *acceleration]


def propagate_reference(initial, times, *, mass_kg: float, area_m2: float = 20,
                        cd: float = 2.2, j2: bool = True, atmosphere: bool = True,
                        rtol: float = RTOL):
    """Return independent DOP853 states at exact supplied timestamps."""
    import numpy as np
    from scipy.integrate import solve_ivp

    initial = np.asarray(initial, dtype=float)
    times = np.asarray(times, dtype=float)
    if initial.shape != (6,) or not np.isfinite(initial).all():
        raise ValueError("initial state must be six finite values")
    if times.ndim != 1 or len(times) < 2 or not np.isfinite(times).all() or not np.all(np.diff(times) > 0):
        raise ValueError("at least two strictly increasing finite times are required")
    if not math.isfinite(mass_kg) or mass_kg <= 0 or min(area_m2, cd) < 0:
        raise ValueError("invalid passive vehicle inputs")
    absolute = [v*rtol/RTOL for v in ATOL]
    solution = solve_ivp(
        lambda t, y: reference_derivative(t, y, mass_kg, area_m2, cd, j2=j2, atmosphere=atmosphere),
        (times[0], times[-1]), initial, method="DOP853", t_eval=times,
        rtol=rtol, atol=absolute, max_step=120.0,
    )
    if not solution.success or len(solution.t) != len(times):
        raise ValueError(f"DOP853 did not cover the full reference interval: {solution.message}")
    return solution.y.T, solution.nfev


def check_run(run: dict, *, satellite_id: str | None = None) -> dict:
    import numpy as np
    import scipy

    tracks = run["traces"]["satellites"]
    if not tracks:
        raise ValueError("input has no satellite coasts to compare")
    identities = [satellite_id] if satellite_id is not None else sorted(tracks)
    mass = run["profile"]["satellite_mass_kg"]
    results = []
    for identity in identities:
        track = tracks[identity]
        if any(s["phase"] != "passive_satellite_coast" or s["propellant_mass_kg"] != 0
               or abs(s["mass_kg"]-mass) > 1e-8 for s in track):
            raise ValueError("diagnostic requires fixed-mass passive satellite samples")
        times = [s["time_s"] for s in track]
        stored = np.array([[s[f"{axis}_m"] for axis in "xyz"]+[s[f"v{axis}_mps"] for axis in "xyz"] for s in track])
        if not np.isfinite(stored).all() or any(ellipsoid_height(s[:3]) <= 86000 for s in stored):
            raise ValueError("stored coast must remain finite and above 86 km")
        reference, calls = propagate_reference(stored[0], times, mass_kg=mass)
        tighter, tight_calls = propagate_reference(stored[0], times, mass_kg=mass, rtol=TIGHT_RTOL)
        differences = np.linalg.norm(stored[:, :3]-tighter[:, :3], axis=1)
        vdifferences = np.linalg.norm(stored[:, 3:]-tighter[:, 3:], axis=1)
        convergence_r = float(np.max(np.linalg.norm(reference[:, :3]-tighter[:, :3], axis=1)))
        convergence_v = float(np.max(np.linalg.norm(reference[:, 3:]-tighter[:, 3:], axis=1)))
        maximum_r, maximum_v = float(np.max(differences)), float(np.max(vdifferences))
        results.append({
            "satellite_id": identity, "sample_count": len(times),
            "start_time_s": times[0], "end_time_s": times[-1],
            "duration_s": times[-1]-times[0],
            "max_position_difference_m": maximum_r,
            "max_velocity_difference_mps": maximum_v,
            "reference_tolerance_convergence_position_m": convergence_r,
            "reference_tolerance_convergence_velocity_mps": convergence_v,
            "reference_rhs_evaluations": calls, "tight_reference_rhs_evaluations": tight_calls,
            "numerical_agreement_verified": maximum_r <= MAX_POSITION_DIFFERENCE_M
                and maximum_v <= MAX_VELOCITY_DIFFERENCE_MPS
                and convergence_r <= 0.005 and convergence_v <= 1e-5,
            "reference_trace": [
                {"time_s": time, "r_m": row[:3].tolist(), "v_mps": row[3:].tolist(),
                 "position_difference_m": float(dr), "velocity_difference_mps": float(dv)}
                for time, row, dr, dv in zip(times, tighter, differences, vdifferences)
            ],
        })
    return {
        "schema": "missionos.starship_3d_numerical_crosscheck.v1",
        "scenario": run["scenario"], "algorithm": "SciPy DOP853",
        "scipy_version": scipy.__version__, "reference_rtol": RTOL, "tight_rtol": TIGHT_RTOL,
        "reference_atol": list(ATOL), "reference_max_step_s": 120.0,
        "force_model": {"mu_m3_s2": MU, "equatorial_radius_m": EQUATORIAL_RADIUS,
                        "j2": J2, "earth_rotation_rad_s": OMEGA,
                        "density_at_86km_kg_m3": RHO_86, "above86km_scale_height_m": 15000,
                        "satellite_mass_kg": mass, "area_m2": 20, "cd": 2.2},
        "satellite_count": len(results), "comparisons": results,
        "numerical_agreement_verified": all(r["numerical_agreement_verified"] for r in results),
        "claim_boundary": {
            "independent_integrator_and_rhs_implementation": True,
            "same_assumed_force_model": True, "input_mission_artifact_authenticated": False,
            "starship_vehicle_validated": False, "real_atmosphere_validated": False,
            "mission_execution_or_completion_verified": False,
            "limitation": "Passive coasts above 86 km only. Shared unvalidated drag/upper atmosphere assumptions; common initial state. No ascent, staging, powered return, control, attitude, TPS or real-flight calibration test.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", default="flight14_inspired")
    parser.add_argument("--satellite-id", default=None)
    parser.add_argument("--approve-simulation", action="store_true")
    args = parser.parse_args()
    if not args.approve_simulation:
        parser.error("Use --approve-simulation for this local numerical diagnostic")
    if args.output.exists():
        parser.error("output already exists; preserve previous diagnostics")
    try:
        raw = args.input_run.read_bytes()
        material = json.loads(raw)
        runs = material if isinstance(material, list) else material.get("runs", [material])
        matches = [r for r in runs if r["scenario"] == args.scenario]
        if len(matches) != 1:
            raise ValueError("input must contain exactly one matching scenario")
        result = check_run(matches[0], satellite_id=args.satellite_id)
        result["input_file_sha256"] = sha256(raw).hexdigest()
        result["diagnostic_script_sha256"] = sha256(Path(__file__).read_bytes()).hexdigest()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        print(json.dumps({k: result[k] for k in ("scenario", "satellite_count", "numerical_agreement_verified")}))
        return 0 if result["numerical_agreement_verified"] else 1
    except (ImportError, ValueError, KeyError, TypeError, OSError) as error:
        print(f"Numerical diagnostic failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
