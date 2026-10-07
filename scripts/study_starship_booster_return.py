#!/usr/bin/env python3
"""Opt-in diagnostic continuation of a SHA-bound recorded booster separation."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.runtime.starship_sixdof_booster import simulate_booster  # noqa: E402

SOURCES = ("src/runtime/starship_sixdof.py", "src/runtime/starship_physics.py",
           "src/runtime/starship_sixdof_booster.py", "src/runtime/starship_sixdof_mission.py",
           "src/runtime/starship_sixdof_contact.py", "src/runtime/starship_sixdof_separation.py",
           "src/runtime/starship_retained_return.py", "src/runtime/starship_attitude_reference.py",
           "scripts/study_starship_booster_return.py")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def source_hashes():
    return {name: sha((ROOT/name).read_bytes()) for name in SOURCES if (ROOT/name).exists()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--study-sha256", required=True, help="Expected exact SHA256 of the approved recorded input")
    parser.add_argument("--profile", type=Path, default=ROOT/"examples/spaceflight/starship-sixdof-profile.json")
    parser.add_argument("--scenario", required=True, help="Exact source run scenario")
    parser.add_argument("--policy", choices=("fixed_v1", "site_return_v1", "site_return_v2", "site_return_v3"), required=True)
    parser.add_argument("--duration-s", type=float)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approve-simulation", action="store_true")
    args = parser.parse_args(argv)
    if not args.approve_simulation:
        parser.error("--approve-simulation is required; this is a local diagnostic, not operational authority")
    raw, profile_raw = args.study.read_bytes(), args.profile.read_bytes()
    if sha(raw) != args.study_sha256:
        parser.error("input study SHA256 does not match expected value")
    study, profile = json.loads(raw), json.loads(profile_raw)
    if study.get("profile") != profile or study.get("provenance", {}).get("profile_sha256") != sha(profile_raw):
        parser.error("recorded profile differs from the exact supplied profile")
    matches = [r for r in study.get("runs", []) if r.get("scenario") == args.scenario]
    if len(matches) != 1 or not isinstance(matches[0].get("booster_separation_state"), dict):
        parser.error("exact source run with recorded booster separation is required")
    horizon = args.duration_s if args.duration_s is not None else profile.get("booster_return", {}).get("max_duration_s", 1200.)
    steps = [profile.get("integration", {}).get(k) for k in ("powered_dt_s", "coast_dt_s")]
    if (type(horizon) not in (int, float) or not math.isfinite(horizon) or not 0 < horizon <= 2000
            or any(type(dt) not in (int, float) or not math.isfinite(dt) or not 0 < dt <= 10 for dt in steps)
            or horizon/min(steps) > 100_000):
        parser.error("diagnostic integration requires at most 100000 finite steps within 2000 seconds")
    start = matches[0]["booster_separation_state"].get("time_s")
    if type(start) not in (int, float) or not math.isfinite(start) or start+min(steps) <= start or start+horizon <= start:
        parser.error("diagnostic time steps and horizon must advance the inherited clock")
    if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
        parser.error("output must be a fresh empty directory; failed trials are never overwritten")
    args.output.mkdir(parents=True, exist_ok=True)
    before = source_hashes()
    try:
        run = simulate_booster(profile, matches[0]["booster_separation_state"], duration_s=args.duration_s,
                               guidance_policy=args.policy)
        if source_hashes() != before:
            raise RuntimeError("diagnostic_source_changed_during_execution")
        record = {"schema": "missionos.starship_booster_return_diagnostic.v1", "profile": profile,
                  "run": run, "provenance": {"input_study_sha256": sha(raw), "profile_sha256": sha(profile_raw),
                                              "source_scenario": args.scenario, "policy": args.policy,
                                              "requested_duration_s": args.duration_s, "resolved_duration_s": horizon,
                                              "source_sha256": before},
                  "operational_policy_approved": False, "catch_handoff_attempted": False,
                  "catch_verified": False, "physical_execution": False}
        (args.output/"result.json").write_text(json.dumps(record, indent=2, allow_nan=False)+"\n")
        print(json.dumps({"policy": args.policy, "termination": run["outcome"]["termination"],
                          "contact_speed_mps": (run.get("contact") or {}).get("surface_relative_speed_mps"),
                          "return_site_distance_m": run["outcome"]["return_site_distance_m"],
                          "catch_handoff_attempted": False}))
    except Exception as exc:
        (args.output/"failure.json").write_text(json.dumps({"schema": "missionos.starship_booster_return_failure.v1",
            "error": type(exc).__name__+": "+str(exc), "input_study_sha256": sha(raw),
            "source_sha256": before, "physical_execution": False}, indent=2)+"\n")
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
