"""Opt-in five source replays and twenty fixed finite six-DOF landing forecasts."""
from __future__ import annotations

import argparse
from copy import deepcopy
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_booster_recovery import simulate_recovery  # noqa: E402
from src.runtime.starship_booster_recovery_verifier import verify_recovery  # noqa: E402
from src.runtime.starship_landing_context import digest  # noqa: E402
from src.runtime.starship_landing_forecast import forecast  # noqa: E402
from src.runtime.starship_landing_forecast_verifier import verify_forecast  # noqa: E402
from src.runtime.starship_landing_forecast_report import report  # noqa: E402
from src.runtime.starship_capture_diagnostics import state_diagnostic  # noqa: E402
from src.runtime.starship_sixdof_catalog import SIXDOF_SOURCES, SIXDOF_PROFILE, CATCH_PROFILE  # noqa: E402

METHODS = (("legacy", None), ("burn_now", 0.), ("wait_2", 2.), ("wait_4", 4.))
OFFSETS = (0, 5, 10, 15, 20)


def write(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, separators=(",", ":"), allow_nan=False)


def save_run(path, value):
    raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    with path.open("xb") as stream:
        stream.write(gzip.compress(raw, mtime=0))
    compressed = path.read_bytes()
    return json.loads(gzip.decompress(compressed)), {"file": path.name, "sha256": sha256(compressed).hexdigest(),
        "expanded_sha256": sha256(raw).hexdigest()}


def sources():
    names = (*SIXDOF_SOURCES, "src/runtime/starship_landing_forecast.py",
        "src/runtime/starship_landing_forecast_verifier.py", "src/runtime/starship_landing_forecast_report.py",
        "src/runtime/starship_capture_diagnostics.py", "scripts/study_starship_landing_forecast.py")
    return {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in names}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.approve_simulation:
        parser.error("Explicit local offline --approve-simulation is required")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("Output must be empty; prior results/failures cannot be overwritten")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    before, started, completed, replays = sources(), time.monotonic(), 0, 0
    write(args.output_dir/"attempt.json", {"budget": {"source_replays": 5, "forecasts": 20,
        "forecast_horizon_s": 90., "wall_time_checked_between_calls_s": 600.}, "source_sha256": before,
        "production_policy_admitted": False, "missionos_approval": None, "physical_execution": False})
    def budget():
        if time.monotonic()-started > 600:
            raise TimeoutError("offline_experiment_wall_budget_exhausted")
    try:
        raw_inputs, raw_comparison = (args.baseline_dir/"inputs.json").read_bytes(), (args.baseline_dir/"comparison.json").read_bytes()
        inputs, previous = json.loads(raw_inputs), json.loads(raw_comparison)
        profile, config, initial = inputs["profile"], inputs["catch_profile"], inputs["initial_state"]
        if (previous.get("schema") != "missionos.starship_boostback_comparison.v1"
                or previous.get("verification_passed") is not True or len(previous.get("conditions", [])) != 5
                or profile != json.loads((ROOT/SIXDOF_PROFILE).read_text())
                or config != json.loads((ROOT/CATCH_PROFILE).read_text())):
            raise ValueError("unmatched_baseline_configuration")
        write(args.output_dir/"inputs.json", {"initial_state": initial, "profile": profile, "catch_profile": config,
            "source_inputs_sha256": sha256(raw_inputs).hexdigest(), "source_comparison_sha256": sha256(raw_comparison).hexdigest(),
            "source_reference_sha256": inputs["reference_study_sha256"], "source_sha256": before,
            "candidate_delays_s": [d for _, d in METHODS], "origin_rule": "last saved entry checkpoint at/before legacy request minus 4 seconds"})
        cases = []
        for offset, item in zip(OFFSETS, previous["conditions"]):
            budget()
            cutoff, name = item["cutoff_time_s"], f"cutoff-minus-{offset:02d}.json"
            raw = (args.baseline_dir/name).read_bytes()
            if (item["run_file"] != name or item["run_sha256"] != sha256(raw).hexdigest()
                    or cutoff != previous["conditions"][0]["cutoff_time_s"]-offset):
                raise ValueError("source_record_hash_or_cutoff_mismatch")
            old = json.loads(raw)
            verdict = verify_recovery(old["run"], initial, profile, config, catch_run=old["catch_run"], development_cutoff_time_s=cutoff)
            if not verdict["passed"]:
                raise ValueError("source_record_invalid")
            landing = next(e for e in old["run"]["events"] if e["event"] == "landing_stage_requested")
            origin = max((p for p in old["run"]["recovery_record"]["checkpoints"]
                if p["phase"] == "recovery_entry_coast" and p["time_s"] <= landing["time_s"]-4.), key=lambda p: p["time_s"])
            base = simulate_recovery(deepcopy(profile), deepcopy(initial), deepcopy(config),
                _development_cutoff_time_s=cutoff, _development_landing_probe_time_s=origin["time_s"])
            base, binding = save_run(args.output_dir/f"replay-minus-{offset:02d}.json.gz", base)
            replays += 1
            base_verdict = verify_recovery(base, initial, profile, config, development_cutoff_time_s=cutoff,
                                           development_landing_probe_time_s=origin["time_s"])
            points, old_points = base["recovery_record"]["checkpoints"], old["run"]["recovery_record"]["checkpoints"]
            equal = ([{k:p[k] for k in ("state", "command")} for p in points]
                == [{k:p[k] for k in ("state", "command")} for p in old_points] and base["final_state"] == old["run"]["final_state"])
            if not base_verdict["passed"] or not equal:
                raise ValueError("instrumented_source_replay_not_exact")
            snap = base["recovery_record"]["development_landing_probe"]["snapshot"]
            if snap["state"] != origin["state"]:
                raise ValueError("captured_origin_differs_from_frozen_checkpoint")
            write(args.output_dir/f"context-minus-{offset:02d}.json", snap)
            case = {"cutoff_time_s": cutoff, "origin_time_s": origin["time_s"], "context_sha256": digest(snap),
                    "source_run_sha256": sha256(raw).hexdigest(), "replay_binding": binding,
                    "source_physical_states_and_commands_equal": equal, "source_verification": base_verdict, "methods": {}}
            for method, delay in METHODS:
                budget()
                horizon = min(90., snap["deadline_s"]-snap["state"]["time_s"])
                value = forecast(snap, profile, config, delay_s=delay, duration_s=horizon)
                completed += 1
                value, binding = save_run(args.output_dir/f"{method}-minus-{offset:02d}.json.gz", value)
                run = value["run"]
                checked = verify_forecast(run, snap, profile, config, delay_s=delay, duration_s=horizon)
                if not checked["passed"]:
                    raise ValueError("forecast_record_invalid: "+json.dumps(checked["issues"]))
                suffix = [{k:p[k] for k in ("state", "command")} for p in points if p["time_s"] >= origin["time_s"]]
                replay_equal = ([{k:p[k] for k in ("state", "command")} for p in run["recovery_record"]["checkpoints"]] == suffix
                                and run["final_state"] == base["final_state"]) if method == "legacy" else None
                if method == "legacy" and not replay_equal:
                    raise ValueError("legacy_forecast_context_not_complete")
                states = [state_diagnostic(p["state"], profile, config) for p in run["recovery_record"]["checkpoints"]]
                case["methods"][method] = {"delay_s": delay, "binding": binding, "verification": checked,
                    "landing_request_time_s": next((e["time_s"] for e in run["events"] if e["event"] == "landing_stage_requested"), None),
                    "termination": run["outcome"]["termination"], "duration_s": run["outcome"]["duration_s"],
                    "terminal_ground_speed_mps": run["outcome"]["final_ground_speed_mps"],
                    "terminal": states[-1], "states": states, "legacy_suffix_equal": replay_equal,
                    "forecast_wall_time_s": value["forecast_wall_time_s"], "prediction_is_execution": False}
                print(json.dumps({"cutoff": cutoff, "method": method, "termination": run["outcome"]["termination"],
                    "speed_mps": run["outcome"]["final_ground_speed_mps"], "predicted_arrival": checked["predicted_handoff_eligible"]}), flush=True)
            eligible = [m for m, _ in METHODS if case["methods"][m]["verification"]["predicted_handoff_eligible"]]
            case["selection"] = eligible[0] if eligible else "no_capture_candidate"
            cases.append(case)
        after = sources()
        if before != after:
            raise ValueError("experiment_sources_changed")
        bundle = {"schema": "missionos.starship_landing_forecast_study.v1", "cases": cases,
            "source_replay_count": replays, "forecast_count": completed, "source_sha256": before,
            "source_unchanged": True, "predicted_arrival_count": sum(v["verification"]["predicted_handoff_eligible"] for c in cases for v in c["methods"].values()),
            "physical_execution": False, "production_policy_admitted": False, "missionos_dispatch": False,
            "forecast_is_execution": False, "independent_dynamics_reexecution": False}
        write(args.output_dir/"study.json", bundle)
        with (args.output_dir/"report.html").open("x") as stream:
            stream.write(report(bundle))
        print(json.dumps({"completed": True, "forecasts": completed, "replays": replays,
            "predicted_arrival_count": bundle["predicted_arrival_count"]}), flush=True)
        return 0
    except Exception as exc:
        write(args.output_dir/"failure.json", {"exception": type(exc).__name__, "detail": str(exc),
            "completed_forecasts": completed, "completed_source_replays": replays, "source_sha256_after": sources()})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
