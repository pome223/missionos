"""Opt-in full-flight timing probe, not online replanning or return admission.

Only the coast duration changes. The existing development qualification path
retains real six-DOF dynamics and the original contact limits. Every outcome,
including failure, is saved. A new profile does not inherit the old certificate.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_starship_return_qualification import sources, write  # noqa: E402
from src.runtime.starship_sixdof_mission import simulate  # noqa: E402
from src.runtime.starship_sixdof_verifier import verify_study  # noqa: E402
from src.runtime.starship_retained_return_verifier import verify_retained_return  # noqa: E402
from src.runtime.starship_return_feasibility import backend  # noqa: E402
from src.runtime.starship_artifacts import write_verified_input  # noqa: E402
from src.runtime import starship_physics as env  # noqa: E402

POLICY = "trimmed_state_terminal_v4"
SCOPE = {"release_limit": 26, "bounded_ship_flaps": True,
         "application": "wind_trim_state_return_v2"}
LIMITS = {"speed_mps": 5., "tilt_deg": 5., "body_rate_rad_s": .02, "reserve_kg": 28000.}


def contact_metrics(run):
    """Scalar orientation/rate checks; no controller success flag is trusted."""
    contact = run["outcome"]["contact_receipt"]
    if contact is None:
        return None
    w, x, y, z = contact["q_body_to_eci"]
    axis = (2*(x*z+w*y), 2*(y*z-w*x), 1-2*(x*x+y*y))
    cosine = sum(a*b for a, b in zip(axis, contact["surface_normal_eci"]))
    latitude, longitude, _ = env.ecef_to_geodetic(
        env.eci_to_ecef(contact["point_eci_m"], contact["event_time_s"]))
    return {"time_s": contact["event_time_s"], "speed_mps": contact["surface_relative_speed_mps"],
            "tilt_deg": math.degrees(math.acos(max(-1., min(1., cosine)))),
            "body_rate_rad_s": math.sqrt(sum(v*v for v in run["final_state"]["omega_body_rad_s"])),
            "reserve_kg": contact["propellant_kg"],
            "latitude_deg": math.degrees(latitude), "longitude_deg": math.degrees(longitude)}


def contact_passed(metrics):
    return bool(metrics and all(math.isfinite(v) for v in metrics.values())
                and all(metrics[k] <= LIMITS[k] for k in ("speed_mps", "tilt_deg", "body_rate_rad_s"))
                and metrics["reserve_kg"] >= LIMITS["reserve_kg"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--delay-s", type=int, choices=(0, 60), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.approve_simulation:
        parser.error("explicit --approve-simulation is required")
    if args.output_dir.exists():
        parser.error("use a fresh output directory; failures must be preserved")
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    baseline_coast = profile["guidance"]["coast_before_return_s"]
    profile["guidance"]["coast_before_return_s"] += args.delay_s
    before = sources()
    before["scripts/probe_starship_return_delay.py"] = sha256(Path(__file__).read_bytes()).hexdigest()
    before["src/runtime/starship_artifacts.py"] = sha256((ROOT/"src/runtime/starship_artifacts.py").read_bytes()).hexdigest()
    args.output_dir.mkdir(parents=True)
    write(args.output_dir/"inputs.json", {"delay_s": args.delay_s, "profile": profile,
        "baseline_coast_s": baseline_coast, "scope": SCOPE, "limits": LIMITS,
        "source_sha256": before, "backend": backend(), "model_inference_invoked": False,
        "online_state_rollout": False, "missionos_return_admission": False, "physical_execution": False})
    for name in before:
        dest = args.output_dir/"source"/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT/name).read_bytes())
    started = time.monotonic()
    try:
        run = simulate(profile, return_policy=POLICY, _development_return_qualification=SCOPE)
        study = {"schema": "missionos.starship_sixdof_study.v1", "profile": profile, "runs": [run],
                 "provenance": {"physical_execution_invoked": False, "starship_vehicle_validated": False,
                                "source_sha256": before, "hashes_are_execution_attestation": False}}
        study = write_verified_input(args.output_dir/"study.json", study)
        run = study["runs"][0]
        record = verify_study(study, expected_scenario="launch", expected_development_return_qualification=SCOPE)
        policy = verify_retained_return(run, profile, expected_policy=POLICY,
                                       expected_development_return_qualification=SCOPE)
        write(args.output_dir/"verification.json", {"record": record, "policy": policy})
        after = {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in before}
        metrics = contact_metrics(run)
        valid = record["passed"] and policy["passed"] and before == after
        result = {"delay_s": args.delay_s, "source_unchanged": before == after,
            "record_verified": record["passed"], "policy_verified": policy["passed"],
            "released_count": run["outcome"]["payload_released_count"],
            "return_requested_s": [e["time_s"] for e in run["events"] if e["event"] == "return_requested"],
            "termination": run["outcome"]["termination"], "contact": metrics,
            "contact_limits_met": bool(valid and run["outcome"]["orbit_gate_reached"]
                                       and run["outcome"]["payload_released_count"] == 26 and contact_passed(metrics)),
            "study_sha256": sha256((args.output_dir/"study.json").read_bytes()).hexdigest(),
            "wall_time_s": time.monotonic()-started,
            "missionos_return_admission": False, "recovery_area_qualified": False,
            "model_inference_invoked": False, "physical_execution": False}
        write(args.output_dir/"result.json", result)
        print(json.dumps(result, allow_nan=False), flush=True)
        # A well-formed failure is an informative probe, never a passing return.
        return 0 if valid else 2
    except Exception as error:
        write(args.output_dir/"failure.json", {"type": type(error).__name__, "detail": str(error),
                                             "elapsed_s": time.monotonic()-started})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
