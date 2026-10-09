"""Opt-in offline return forecasts, checked against two saved full flights.

One invocation consumes one forecast; there is no search or retry. The previous
two-flight audit must pass first. Expected future samples are used only after
forecasting, for reproduction checks, never passed to the prediction function.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_starship_return_delay import audit  # noqa: E402
from scripts.probe_starship_return_delay import contact_metrics, contact_passed, LIMITS  # noqa: E402
from scripts.run_starship_return_qualification import sources  # noqa: E402
from src.runtime.starship_artifacts import write_verified_input  # noqa: E402
from src.runtime.starship_return_prediction import forecast, snapshot, STATE_KEYS  # noqa: E402
from src.runtime.starship_return_feasibility import backend  # noqa: E402
from src.runtime.starship_return_prediction_verifier import compare_saved_suffix  # noqa: E402

EXTRA_SOURCES = ("src/runtime/starship_artifacts.py", "src/runtime/starship_return_prediction.py",
                 "src/runtime/starship_return_prediction_verifier.py",
                 "scripts/study_starship_return_candidates.py", "scripts/verify_starship_return_delay.py",
                 "scripts/probe_starship_return_delay.py")


def source_hashes():
    return {**sources(), **{name: sha256((ROOT/name).read_bytes()).hexdigest() for name in EXTRA_SOURCES}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--nominal", type=Path, required=True)
    parser.add_argument("--delayed", type=Path, required=True)
    parser.add_argument("--delay-s", type=int, choices=(0, 60), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.approve_simulation:
        parser.error("explicit --approve-simulation is required")
    if args.output_dir.exists():
        parser.error("use a fresh output directory; failed outcomes must be preserved")
    prior = audit(args.nominal, args.delayed)
    if not prior["gate_passed"]:
        parser.error("saved timing screen failed current source/record checks")
    nominal = json.loads((args.nominal/"study.json").read_text())
    expected = nominal if args.delay_s == 0 else json.loads((args.delayed/"study.json").read_text())
    nominal_time = prior["cases"][0]["return_requested_s"][0]
    initial = max((s for s in nominal["runs"][0]["samples"]
                   if s["phase"] == "orbital_coast" and s["time_s"] <= nominal_time-90.), key=lambda s: s["time_s"])
    origin = snapshot(initial, retained_count=0)
    before = source_hashes()
    profile = nominal["profile"]
    request = {"origin": origin, "profile": profile, "return_time_s": nominal_time+args.delay_s,
               "duration_s": 3600., "delay_s": args.delay_s, "limits": LIMITS,
               "source_sha256": before, "backend": backend(),
               "saved_study_sha256": [c["study_sha256"] for c in prior["cases"]],
               "model_calls": 0, "full_flights": 0, "runtime_admission": False}
    args.output_dir.mkdir(parents=True)
    request = write_verified_input(args.output_dir/"inputs.json", request)
    write_verified_input(args.output_dir/"prior-audit.json", prior)
    for name in before:
        dest = args.output_dir/"source"/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT/name).read_bytes())
    try:
        prediction = forecast(request["origin"], request["profile"],
                              return_time_s=request["return_time_s"], duration_s=request["duration_s"])
        prediction = write_verified_input(args.output_dir/"prediction.json", prediction)
        # A compact reference includes every recorded physical suffix sample.
        # It is never an argument to forecast().
        run = expected["runs"][0]
        reference = {"final_state": run["final_state"], "outcome": run["outcome"],
            "events": run["events"], "samples": [
                {**{k: s[k] for k in STATE_KEYS}, "phase": s["phase"]}
                for s in run["samples"] if s["time_s"] >= origin["state"]["time_s"]]}
        reference = write_verified_input(args.output_dir/"reference-suffix.json", reference)
        verification = compare_saved_suffix(prediction, reference, request)
        write_verified_input(args.output_dir/"verification.json", verification)
        metrics = contact_metrics(prediction)
        result = {"schema": "missionos.ship_return_candidate_screen.v1", "delay_s": args.delay_s,
            "origin_time_s": origin["state"]["time_s"], "origin_sha256": sha256(json.dumps(origin, sort_keys=True).encode()).hexdigest(),
            "requested_return_time_s": request["return_time_s"],
            "actual_return_time_s": [e["time_s"] for e in prediction["events"] if e["event"] == "return_requested"],
            "termination": prediction["outcome"]["termination"], "contact": metrics,
            "reproduction_checks_passed": verification["passed"],
            "contact_limits_met": contact_passed(metrics), "source_unchanged": before == source_hashes(),
            "wall_time_s": prediction["wall_time_s"], "model_calls": 0,
            "source_sha256": before, "backend": backend(),
            "prediction_sha256": sha256((args.output_dir/"prediction.json").read_bytes()).hexdigest(),
            "prediction_is_execution": False, "runtime_admission": False,
            "independent_physics_validation": False, "recovery_area_qualified": False,
            "observation_uncertainty_qualified": False, "waiting_endurance_qualified": False,
            "milestone_complete": False}
        write_verified_input(args.output_dir/"result.json", result)
        print(json.dumps({k: result[k] for k in ("delay_s", "termination", "contact", "reproduction_checks_passed", "source_unchanged")}))
        return 0 if result["source_unchanged"] and verification["passed"] else 2
    except Exception as error:
        write_verified_input(args.output_dir/"failure.json", {"type": type(error).__name__, "detail": str(error)})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
