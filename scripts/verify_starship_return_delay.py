"""Audit the two saved timing probes without rerunning any dynamics or model."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.probe_starship_return_delay import POLICY, SCOPE, LIMITS, contact_metrics, contact_passed  # noqa: E402
from src.runtime.starship_sixdof_verifier import verify_study  # noqa: E402
from src.runtime.starship_retained_return_verifier import verify_retained_return  # noqa: E402
from src.runtime.starship_return_feasibility import sources, readiness, backend  # noqa: E402


def audit(nominal, delayed):
    base = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    cases, runs = [], []
    for folder, delay in ((nominal, 0), (delayed, 60)):
        raw = (folder/"study.json").read_bytes()
        study = json.loads(raw)
        original = json.loads((folder/"result.json").read_text())
        inputs = json.loads((folder/"inputs.json").read_text())
        run = study["runs"][0]
        expected = json.loads(json.dumps(base))
        expected["guidance"]["coast_before_return_s"] += delay
        record = verify_study(study, expected_scenario="launch", expected_development_return_qualification=SCOPE)
        policy = verify_retained_return(run, study["profile"], expected_policy=POLICY,
                                       expected_development_return_qualification=SCOPE)
        # File names come from this checkout's source list, not artifact paths.
        source_files = (*sources(), "scripts/probe_starship_return_delay.py")
        captured = inputs["source_sha256"]
        bound = all(captured.get(name) == value for name, value in sources().items())
        saved = all(sha256((folder/"source"/name).read_bytes()).hexdigest() == captured.get(name)
                    for name in source_files)
        checks = {"profile_only_requested_coast_change": study["profile"] == inputs["profile"] == expected,
                  "core_source_matches": bound, "source_snapshot_matches": saved,
                  "study_unchanged": sha256(raw).hexdigest() == original["study_sha256"],
                  "backend_matches": inputs["backend"] == backend(),
                  "declared_inputs_match": inputs["delay_s"] == delay and inputs["scope"] == SCOPE
                      and inputs["limits"] == LIMITS and inputs["baseline_coast_s"] == base["guidance"]["coast_before_return_s"],
                  "record_verified": record["passed"], "policy_verified": policy["passed"],
                  "orbit_and_26_releases": run["outcome"]["orbit_gate_reached"]
                      and run["outcome"]["payload_released_count"] == 26}
        metrics = contact_metrics(run)
        cases.append({"delay_s": delay, "checks": checks, "contact": metrics,
                      "contact_limits_met": bool(all(checks.values()) and contact_passed(metrics)),
                      "return_requested_s": [e["time_s"] for e in run["events"] if e["event"] == "return_requested"],
                      "study_sha256": sha256(raw).hexdigest(),
                      "executed_probe_sha256": captured["scripts/probe_starship_return_delay.py"],
                      "original_verdict": {k: original[k] for k in ("record_verified", "policy_verified", "contact_limits_met")},
                      "record_issues": record["issues"], "policy_issues": policy["issues"]})
        runs.append(run)
    returns = [c["return_requested_s"] for c in cases]
    timing_valid = all(len(r) == 1 for r in returns)
    cutoff = returns[0][0] if timing_valid else -1.
    prefixes = [[s for s in run["samples"] if s["time_s"] < cutoff-1e-8] for run in runs]
    releases = [[e for e in run["events"] if e["event"] == "payload_released"] for run in runs]
    paired = {"return_request_delayed_60_s": timing_valid and math.isclose(returns[1][0]-returns[0][0], 60., abs_tol=1e-7),
              "same_nonempty_pre_return_prefix": bool(prefixes[0]) and prefixes[0] == prefixes[1],
              "same_26_release_events": len(releases[0]) == 26 and releases[0] == releases[1],
              "same_booster_separation_state": runs[0]["booster_separation_state"] == runs[1]["booster_separation_state"]}
    shift = None
    if all(c["contact"] is not None for c in cases):
        a, b = [c["contact"] for c in cases]
        lat1, lat2 = [math.radians(c["latitude_deg"]) for c in (a, b)]
        dlon = math.radians(b["longitude_deg"]-a["longitude_deg"])
        h = math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
        h = min(1., max(0., h))
        shift = {"great_circle_distance_m_spherical_6371008_8": 6371008.8*2*math.atan2(math.sqrt(h), math.sqrt(1-h)),
                 "time_s": b["time_s"]-a["time_s"], "fuel_difference_kg": b["reserve_kg"]-a["reserve_kg"]}
    return {"schema": "missionos.starship_return_delay_screen.v1", "cases": cases,
            "paired_checks": paired, "common_prefix_sample_count": len(prefixes[0]), "contact_shift": shift,
            "gate_passed": all(paired.values()) and all(c["contact_limits_met"] for c in cases),
            "baseline_certificate_ready": readiness(base)[2] is None,
            "core_source_sha256": sources(), "backend": backend(),
            "audit_script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
            "limits": LIMITS, "dynamics_executed_by_audit": False, "model_calls": 0,
            "online_rollout": False, "recovery_area_qualified": False,
            "waiting_endurance_qualified": False, "milestone_complete": False, "physical_execution": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nominal", type=Path, required=True)
    parser.add_argument("--delayed", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("use a fresh output file; preserve previous verdicts")
    result = audit(args.nominal, args.delayed)
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2, allow_nan=False)+"\n")
    print(json.dumps({"gate_passed": result["gate_passed"], "contact_shift": result["contact_shift"]}))
    return 0 if result["gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
