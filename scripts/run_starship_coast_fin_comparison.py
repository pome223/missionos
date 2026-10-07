"""Ten frozen 6DOF continuations comparing all-phase and coast-only fins.

This local development experiment neither authorizes MissionOS dispatch nor
adopts the allocator. It tests the saved state at the first landing request
before attributing an outcome difference to the downstream allocation change.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import gzip
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import run_starship_fin_comparison as fins  # noqa: E402
from src.runtime.starship_fin_report import report, replay_points  # noqa: E402

base = fins.base


def sources():
    result = fins.sources()
    name = "scripts/run_starship_coast_fin_comparison.py"
    result[name] = sha256((ROOT / name).read_bytes()).hexdigest()
    return result


def landing_boundary(run):
    event = next((event for event in run["events"] if event["event"] == "landing_stage_requested"), None)
    if event is None:
        return None, []
    prefix = [{"state": point["state"], "command": point["command"]}
              for point in run["recovery_record"]["checkpoints"] if point["time_s"] < event["time_s"]]
    return event["state"], prefix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--separation-study", required=True, type=Path)
    parser.add_argument("--previous-fin-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.approve_simulation:
        parser.error("Explicit local development --approve-simulation opt-in is required")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("Output directory must be empty; retained results cannot be overwritten")
    before = sources()
    reference_bytes = args.separation_study.read_bytes()
    study = json.loads(reference_bytes)
    initial, cutoff_time, _, reference_verification = base.inputs(study)
    profile, config = study["profile"], study["catch_profile"]
    previous_bytes = (args.previous_fin_dir / "comparison.json").read_bytes()
    previous = json.loads(previous_bytes)
    previous_inputs = json.loads((args.previous_fin_dir / "inputs.json").read_bytes())
    if (previous.get("schema") != "missionos.starship_fin_comparison.v1"
            or previous.get("verification_passed") is not True
            or previous_inputs.get("initial_state") != initial
            or previous_inputs.get("profile") != profile
            or previous_inputs.get("catch_profile") != config
            or previous_inputs.get("regularization") != fins.REGULARIZATION
            or len(previous.get("conditions", [])) != len(base.OFFSETS_S)):
        raise ValueError("unmatched_previous_fin_comparison")
    previous_runs = []
    for offset, pair in zip(base.OFFSETS_S, previous["conditions"]):
        item = pair["methods"]["candidate"]
        filename = f"candidate-minus-{int(offset):02d}.json"
        raw = (args.previous_fin_dir / filename).read_bytes()
        old = json.loads(raw)
        if (pair.get("cutoff_time_s") != cutoff_time-offset or item.get("run_file") != filename
                or item.get("run_sha256") != sha256(raw).hexdigest()):
            raise ValueError("previous_fin_hash_mismatch")
        verdict = base.verify_recovery(old["run"], initial, profile, config, catch_run=old["catch_run"],
            development_cutoff_time_s=cutoff_time-offset, development_fin_allocation=True)
        if verdict["passed"] is not True:
            raise ValueError("previous_fin_verification_failed")
        previous_runs.append(old["run"])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    base.write(args.output_dir / "inputs.json", {"initial_state": initial, "profile": profile, "catch_profile": config,
        "reference_study_sha256": sha256(reference_bytes).hexdigest(), "reference_verification": reference_verification,
        "previous_comparison_sha256": sha256(previous_bytes).hexdigest(), "offsets_s": base.OFFSETS_S,
        "candidate_policy": fins.POLICY_ID, "regularization": fins.REGULARIZATION, "source_sha256": before,
        "candidate_scope": "coast_only", "minimum_dynamic_pressure_pa": 100.,
        "missionos_approval": None, "opt_in_scope": "local_development", "production_policy_admitted": False})
    for name in before:
        path = args.output_dir / "source" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    conditions, prefixes = [], []
    earliest = cutoff_time-max(base.OFFSETS_S)
    try:
        for index, offset in enumerate(base.OFFSETS_S):
            pair = {"offset_s": offset, "cutoff_time_s": cutoff_time-offset, "methods": {}}
            runs = {}
            for method, scope in (("baseline", "coast_and_landing"), ("candidate", "coast_only")):
                started = time.monotonic()
                run = base.simulate_recovery(deepcopy(profile), deepcopy(initial), deepcopy(config),
                    _development_cutoff_time_s=cutoff_time-offset, _development_fin_allocation=True,
                    _development_fin_scope=scope)
                catch = None
                if run["recovery_record"]["handoff"]["eligible"]:
                    catch = base.simulate_catch(profile, config, initial_state=run["final_state"],
                        duration_s=30., control_policy="net_thrust_trim_v1")
                raw = json.dumps({"run": run, "catch_run": catch}, allow_nan=False, separators=(",", ":")).encode()
                filename = f"{method}-minus-{int(offset):02d}.json.gz"
                with (args.output_dir / filename).open("xb") as stream:
                    stream.write(gzip.compress(raw, mtime=0))
                saved = json.loads(gzip.decompress((args.output_dir / filename).read_bytes()))
                run, catch = saved["run"], saved["catch_run"]
                verdict = base.verify_recovery(run, initial, profile, config, catch_run=catch,
                    development_cutoff_time_s=cutoff_time-offset, development_fin_allocation=True,
                    development_fin_scope=scope)
                item = base.summarize(run, catch, verdict, profile, config, cutoff_time-offset, time.monotonic()-started)
                item.update(method=method, application_scope=scope, run_file=filename, serialization="gzip_json",
                    uncompressed_run_sha256=sha256(raw).hexdigest(),
                    run_sha256=sha256((args.output_dir / filename).read_bytes()).hexdigest(),
                    metrics=fins.sampled_metrics(run, profile), replay=replay_points(run, profile, config))
                if method == "baseline":
                    old = previous_runs[index]
                    item["previous_physical_states_equal"] = ([c["state"] for c in run["recovery_record"]["checkpoints"]]
                        == [c["state"] for c in old["recovery_record"]["checkpoints"]] and run["final_state"] == old["final_state"])
                    item["previous_commands_equal"] = ([c["command"] for c in run["recovery_record"]["checkpoints"]]
                        == [c["command"] for c in old["recovery_record"]["checkpoints"]])
                pair["methods"][method], runs[method] = item, run
                prefixes.append([c["state"] for c in run["recovery_record"]["checkpoints"] if c["time_s"] < earliest-1e-9])
                base.write(args.output_dir / f"{method}-minus-{int(offset):02d}-summary.json", item)
                print(json.dumps({"method": scope, "offset_s": offset, "termination": run["outcome"]["termination"],
                    "speed_mps": run["outcome"]["final_ground_speed_mps"], "distance_m": run["outcome"]["return_site_distance_m"],
                    "verified": verdict["passed"], "issues": verdict["issues"]}), flush=True)
            left, left_prefix = landing_boundary(runs["baseline"])
            right, right_prefix = landing_boundary(runs["candidate"])
            pair["landing_request_states_equal"] = left is not None and left == right
            pair["pre_landing_saved_states_and_commands_equal"] = left is not None and left_prefix == right_prefix
            pair["pre_landing_checkpoint_count"] = len(left_prefix)
            pair["landing_state_difference"] = base.physical_difference(left, right) if left is not None and right is not None else None
            conditions.append(pair)
        after = sources()
        prefix_equal = all(prefix == prefixes[0] for prefix in prefixes[1:])
        passed = (before == after and prefix_equal and all(pair["landing_request_states_equal"]
            and pair["pre_landing_saved_states_and_commands_equal"]
            and pair["methods"]["baseline"]["previous_physical_states_equal"]
            and pair["methods"]["baseline"]["previous_commands_equal"]
            and all(item["verification"]["passed"] and item["scheduled_cutoff_executed"] for item in pair["methods"].values())
            for pair in conditions))
        bundle = {"schema": "missionos.starship_coast_fin_comparison.v1", "verification_passed": passed,
            "conditions": conditions, "source_sha256_before": before, "source_sha256_after": after,
            "source_unchanged": before == after, "common_prefix_equal": prefix_equal,
            "common_prefix_checkpoint_count": len(prefixes[0]), "candidate_policy": fins.POLICY_ID,
            "candidate_scope": "coast_only", "baseline_label": "前回候補：coast＋着陸", "candidate_label": "coast限定・着陸は従来",
            "candidate_supported_count": sum(pair["methods"]["candidate"]["candidate_supported"] for pair in conditions),
            "landing_request_states_equal_count": sum(pair["landing_request_states_equal"] for pair in conditions),
            "missionos_approval": None, "opt_in_scope": "local_development", "production_policy_admitted": False,
            "physical_execution": False, "full_launch_reexecuted": False, "integrator_independently_reexecuted": False}
        base.write(args.output_dir / "comparison.json", bundle)
        (args.output_dir / "report.html").write_text(report(bundle))
        return 0 if passed else 2
    except Exception as exc:
        base.write(args.output_dir / "failure.json", {"exception": type(exc).__name__, "detail": str(exc),
            "completed_pairs": len(conditions), "source_sha256_after": sources()})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
