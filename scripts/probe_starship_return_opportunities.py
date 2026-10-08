"""Four fixed offline opportunities through one following orbital period.

Each opt-in invocation executes one candidate, never searches or retries. The
origin is a qualified saved nominal coast state, not an onboard estimate. No
recovery region, availability, endurance, or runtime authority is granted.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.probe_starship_return_delay import contact_metrics, contact_passed, LIMITS  # noqa: E402
from scripts.run_starship_return_qualification import sources  # noqa: E402
from src.runtime.starship_artifacts import write_verified_input  # noqa: E402
from src.runtime.starship_return_prediction import forecast, snapshot, opportunity_window, validate_origin  # noqa: E402
from src.runtime.starship_return_prediction_verifier import compare_saved_suffix  # noqa: E402
from src.runtime.starship_return_feasibility import readiness, backend, CORE_SOURCES, digest  # noqa: E402
from src.runtime.starship_sixdof_mission import point_state  # noqa: E402
from src.runtime.starship_physics import orbital_elements  # noqa: E402

CANDIDATES = ("nominal", "delay60", "next_orbit", "next_orbit60")
EXTRA_SOURCES = ("scripts/probe_starship_return_opportunities.py", "scripts/probe_starship_return_delay.py",
                 "src/runtime/starship_artifacts.py", "src/runtime/starship_return_prediction.py",
                 "src/runtime/starship_return_prediction_verifier.py")


def source_hashes():
    return {**sources(), **{n: sha256((ROOT/n).read_bytes()).hexdigest() for n in EXTRA_SOURCES}}


def candidate_set(origin, profile, scheduled):
    state, _ = validate_origin(origin, profile, scheduled, 3600.)
    period = orbital_elements(point_state(state))["period_s"]
    window = opportunity_window(scheduled)
    times = dict(zip(CANDIDATES, (scheduled, scheduled+60., scheduled+period, scheduled+period+60.)))
    for when in times.values():
        validate_origin(origin, profile, when, 10000., window=window)
    return {"schema": "missionos.ship_return_opportunities.v1", "origin_sha256": digest(origin),
            "window": window, "osculating_period_s": period, "return_times_s": times,
            "maximum_forecasts": 4, "maximum_workers": 2, "per_run_wall_limit_s": 900,
            "batch_wall_limit_s": 1800, "automatic_retry": False, "runtime_admission": False}


def load_reference(directory):
    raw = (directory/"study.json").read_bytes()
    result = json.loads((directory/"result.json").read_text())
    study = json.loads(raw)
    run, profile = study["runs"][0], study["profile"]
    certificate, identity, reason = readiness(profile)
    if reason:
        raise ValueError(reason)
    row = next(c for c in certificate["cases"] if c["retained_count"] == 0 and c["initial_fuel_offset_kg"] == 0)
    checksum = sha256(raw).hexdigest()
    current = source_hashes()
    if (checksum != row["study_sha256"] or checksum != result["study_sha256"]
            or result["provisional_speed_and_reserve_gate_met"] is not True
            or run["outcome"]["payload_released_count"] != 26
            or any(run["development_source_sha256"].get(k) != current[k] for k in CORE_SOURCES)):
        raise ValueError("reference_not_bound_to_current_nominal_qualification")
    scheduled = run["retained_return"]["activation"]["time_s"]
    initial = max((s for s in run["samples"] if s["phase"] == "orbital_coast"
                   and s["time_s"] <= scheduled-90.), key=lambda s: s["time_s"])
    origin = snapshot(initial, retained_count=0)
    return run, profile, origin, candidate_set(origin, profile, scheduled), identity, checksum


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--candidate", choices=CANDIDATES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.approve_simulation or args.output_dir.exists():
        parser.error("explicit opt-in and a fresh output directory are required")
    reference, profile, origin, candidates, identity, checksum = load_reference(args.reference_dir)
    before = source_hashes()
    request = {"origin": origin, "profile": profile, "window": candidates["window"],
        "return_time_s": candidates["return_times_s"][args.candidate], "duration_s": 10000.,
        "candidate": args.candidate, "source_sha256": before, "backend": backend(),
        "reference_certificate_sha256": identity, "reference_study_sha256": checksum, "limits": LIMITS}
    args.output_dir.mkdir(parents=True)
    write_verified_input(args.output_dir/"candidate-set.json", candidates)
    request = write_verified_input(args.output_dir/"inputs.json", request)
    for name in before:
        destination = args.output_dir/"source"/name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT/name).read_bytes())
    try:
        prediction = forecast(request["origin"], request["profile"], return_time_s=request["return_time_s"],
                              duration_s=request["duration_s"], window=request["window"])
        prediction = write_verified_input(args.output_dir/"prediction.json", prediction)
        verification = None
        if args.candidate == "nominal":
            verification = compare_saved_suffix(prediction, reference, request)
            write_verified_input(args.output_dir/"verification.json", verification)
        metrics = contact_metrics(prediction)
        unchanged = before == source_hashes()
        result = {"schema": "missionos.ship_return_opportunity_result.v1", "candidate": args.candidate,
            "candidate_set_sha256": digest(candidates), "source_sha256": before, "backend": backend(),
            "origin_time_s": origin["state"]["time_s"], "requested_return_time_s": request["return_time_s"],
            "actual_return_time_s": [e["time_s"] for e in prediction["events"] if e["event"] == "return_requested"],
            "termination": prediction["outcome"]["termination"], "contact": metrics,
            "contact_limits_met": bool(unchanged and contact_passed(metrics)), "source_unchanged": unchanged,
            "nominal_reproduction": verification, "wall_time_s": prediction["wall_time_s"],
            "prediction_sha256": sha256((args.output_dir/"prediction.json").read_bytes()).hexdigest(),
            "prediction_is_execution": False, "runtime_admission": False, "model_calls": 0,
            "independent_physics_validation": False, "recovery_area_qualified": False,
            "observation_uncertainty_qualified": False, "waiting_endurance_qualified": False,
            "milestone_complete": False}
        write_verified_input(args.output_dir/"result.json", result)
        print(json.dumps({k: result[k] for k in ("candidate", "contact", "termination", "contact_limits_met", "wall_time_s")}), flush=True)
        return 0 if unchanged and (verification is None or verification["passed"]) else 2
    except Exception as error:
        write_verified_input(args.output_dir/"failure.json", {"type": type(error).__name__, "detail": str(error)})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
