"""Opt-in, predefined retained-mass comparison of two return-frame policies.

Runs complete launch/fault/return trajectories for 13, 26 and 39 retained
1,700 kg payloads. These are numerical robustness probes, not demonstrated
Starship loading configurations. There is no parameter search or provider call.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_sixdof_mission import simulate  # noqa: E402
from src.runtime.starship_sixdof_verifier import verify_study  # noqa: E402
from src.runtime.starship_retained_return_verifier import verify_retained_return  # noqa: E402

COUNTS = (13, 26, 39)
POLICIES = ("mass_state_terminal_v1", "mass_state_terminal_v2", "mass_state_terminal_v3")
SOURCES = ("scripts/study_starship_attitude_return.py",
           "src/runtime/starship_sixdof_mission.py", "src/runtime/starship_sixdof.py",
           "src/runtime/starship_physics.py", "src/runtime/starship_sixdof_separation.py",
           "src/runtime/starship_sixdof_contact.py", "src/runtime/starship_sixdof_booster.py",
           "src/runtime/starship_flight_supervision.py", "src/runtime/starship_attitude_reference.py",
           "src/runtime/starship_retained_return.py", "src/runtime/starship_retained_return_verifier.py",
           "src/runtime/starship_sixdof_verifier.py")


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))


def _case(args):
    original, count, policy, destination, source_hashes = args
    started = time.monotonic()
    profile = deepcopy(original)
    profile["payload"]["count"] = count
    case_id = f"payload-{count}-{policy}"
    destination = Path(destination)/case_id
    destination.mkdir()
    _write(destination/"profile.json", profile)
    provenance = {"created_utc": datetime.now(timezone.utc).isoformat(),
                  "source_sha256": source_hashes,
                  "profile_sha256": sha256((destination/"profile.json").read_bytes()).hexdigest(),
                  "profile_change": {"payload.count": count}, "return_policy": policy,
                  "dt_scale": 1., "duration_override_s": None,
                  "physical_execution_invoked": False, "starship_vehicle_validated": False}
    try:
        run = simulate(profile, scenario="deployment_no_effect", return_policy=policy)
    except Exception as exc:
        _write(destination/"failure.json", {"status": "execution_error", "error": f"{type(exc).__name__}: {exc}",
                                          "profile": profile, "provenance": provenance})
        raise
    provenance["wall_time_s"] = time.monotonic()-started
    study = {"schema": "missionos.starship_sixdof_study.v1", "profile": profile,
             "runs": [run], "provenance": provenance}
    _write(destination/"study.json", study)
    # The independent verifier consumes the saved JSON contract, not internal
    # dataclass tuples which are deliberately outside that contract.
    study = json.loads((destination/"study.json").read_bytes())
    run = study["runs"][0]
    trajectory = verify_study(study, expected_scenario="deployment_no_effect")
    retained = verify_retained_return(run, profile, expected_policy=policy)
    _write(destination/"verification.json", {"trajectory": trajectory, "retained_return": retained})
    attitude_samples = [s for s in run["samples"] if s["phase"] in ("ballistic_return", "landing_burn")
                        and "target_q_body_to_eci" in s.get("controller", {})]
    changes = []
    for left, right in zip(attitude_samples, attitude_samples[1:]):
        if left["phase"] == right["phase"] and right["time_s"] > left["time_s"]:
            q1, q2 = left["controller"]["target_q_body_to_eci"], right["controller"]["target_q_body_to_eci"]
            angle = 2*math.acos(min(1., abs(sum(a*b for a, b in zip(q1, q2)))))
            changes.append(angle/(right["time_s"]-left["time_s"]))
    outcome = run["outcome"]
    contact = outcome["contact_receipt"] or {}
    trigger = run["retained_return"]["trigger"]
    result = {"case_id": case_id, "payload_count": count, "payload_mass_kg": count*profile["payload"]["mass_each_kg"],
              "return_policy": policy, "termination": outcome["termination"],
              "contact_point_speed_mps": contact.get("surface_relative_speed_mps"),
              "fuel_at_end_kg": run["final_state"]["propellant_kg"],
              "max_saved_target_rate_rad_s": max(changes, default=None),
              "target_rate_scope": "same-phase saved samples only; excludes commanded entry/landing phase change",
              "payload_released_count": outcome["payload_released_count"],
              "trigger_altitude_m": trigger["state"]["altitude_m"] if trigger else None,
              "trajectory_verified": trajectory["passed"], "return_record_verified": retained["passed"],
              "wall_time_s": provenance["wall_time_s"], "physical_execution": False,
              "starship_vehicle_validated": False}
    _write(destination/"summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--policies", nargs="+", choices=POLICIES, default=["mass_state_terminal_v1", "mass_state_terminal_v3"])
    args = parser.parse_args()
    if len(set(args.policies)) != len(args.policies):
        parser.error("Duplicate policies are not a comparison.")
    if not args.approve_simulation:
        print(json.dumps({"status": "not_started", "required_flag": "--approve-simulation"}))
        return 2
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("Output is not empty; use a fresh directory to preserve earlier results.")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    profile_path = ROOT/"examples/spaceflight/starship-sixdof-profile.json"
    original = json.loads(profile_path.read_bytes())
    sources = {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}
    _write(args.output_dir/"predeclared.json", {"payload_counts": COUNTS, "return_policies": args.policies,
        "base_profile_sha256": sha256(profile_path.read_bytes()).hexdigest(), "source_sha256": sources,
        "claim": "fixed numerical robustness probes, not certified payload or return envelopes"})
    cases = [(original, count, policy, str(args.output_dir), sources) for count in COUNTS for policy in args.policies]
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(_case, case) for case in cases]):
            result = future.result()
            results.append(result)
            print(json.dumps(result), flush=True)
    changed = [name for name, digest in sources.items() if sha256((ROOT/name).read_bytes()).hexdigest() != digest]
    summary = {"schema": "missionos.starship_attitude_return_study.v1", "results": sorted(results, key=lambda r: r["case_id"]),
               "sources_changed_during_execution": changed,
               "passed": not changed and all(r["trajectory_verified"] and r["return_record_verified"] for r in results),
               "scope": "execution and saved evidence integrity; physical failures are retained, not verifier failures",
               "physical_execution": False, "starship_vehicle_validated": False}
    _write(args.output_dir/"summary.json", summary)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
