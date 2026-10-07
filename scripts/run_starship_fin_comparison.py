"""Opt-in paired fin-allocation experiment from one saved separation.

Ten actual six-DOF continuations: unchanged allocation and one frozen candidate,
at the same five cutoff times. This creates no MissionOS approval or dispatch.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import run_starship_boostback_comparison as base  # noqa: E402
from src.runtime.starship_fin_allocation import POLICY_ID, REGULARIZATION  # noqa: E402
from src.runtime.starship_fin_report import report, replay_points  # noqa: E402


def sources():
    result = base.sources()
    for name in ("scripts/run_starship_fin_comparison.py", "src/runtime/starship_fin_report.py"):
        result[name] = sha256((ROOT / name).read_bytes()).hexdigest()
    return result


def sampled_metrics(run, profile):
    """Saved-sample metrics with left-held weighting; no continuous maxima."""
    samples = run["samples"]
    limit = math.radians(profile["actuators"]["grid_fin_limit_deg"])
    count = len(profile["booster"]["grid_fin_panels"])
    seconds, angle_seconds, reach_seconds = 0., 0., 0.
    errors, residuals, differences = [], [], []
    applied = 0
    pressure_records = []
    for index, sample in enumerate(samples):
        diagnostic = sample.get("controller", {})
        item = diagnostic.get("development_fin_allocation")
        if item:
            applied += 1
        q = sample["dynamic_pressure_pa"]
        if not 100. <= q <= 70000. or sample["phase"] != "recovery_entry_coast":
            continue
        dt = samples[index+1]["time_s"]-sample["time_s"] if index+1 < len(samples) else 0.
        seconds += dt
        fins = sample["flap_angles_rad"][3:]
        angle_seconds += dt*sum(abs(a) >= .995*limit for a in fins)/count
        error = diagnostic.get("attitude_error_deg")
        if error is not None:
            errors.append(error)
        trim = diagnostic.get("predicted_trim_flap_angles_rad")
        if trim is not None:
            differences.append(math.sqrt(sum((a-b)**2 for a, b in zip(fins, trim[3:]))/count))
        if item:
            residuals.append(math.sqrt(sum(x*x for x in item["predicted_residual_torque_body_nm"])))
            delta = [b-a for a, b in zip(item["actual_angles_rad"], item["predicted_endpoint_angles_rad"])]
            reach_seconds += dt*sum(abs(d-lo) <= 1e-9 or abs(d-hi) <= 1e-9 for d, lo, hi in
                zip(delta, item["reachable_delta_lower_rad"], item["reachable_delta_upper_rad"]))/count
        pressure_records.append({"time_s": sample["time_s"], "altitude_m": sample["altitude_m"],
            "dynamic_pressure_pa": q, "attitude_error_deg": error,
            "actual_fin_angles_deg": [math.degrees(x) for x in fins],
            "target_static_trim_angles_deg": [math.degrees(x) for x in trim[3:]] if trim is not None else None})
    return {"interval": "entry coast saved samples with 100 <= dynamic pressure Pa <= 70000",
        "sample_count": len(pressure_records), "left_held_sample_duration_s": seconds,
        "max_sampled_attitude_error_deg": max(errors, default=None),
        "sample_weighted_actual_angle_saturation_fraction": angle_seconds/seconds if seconds else None,
        "sample_weighted_reachable_endpoint_saturation_fraction": reach_seconds/seconds if seconds and applied else None,
        "max_sampled_static_trim_rms_difference_deg": math.degrees(max(differences)) if differences else None,
        "max_sampled_predicted_residual_torque_nm": max(residuals, default=None),
        "allocation_receipt_count": applied, "continuous_extrema_verified": False, "samples": pressure_records}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--separation-study", required=True, type=Path)
    parser.add_argument("--baseline-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.approve_simulation:
        parser.error("Explicit local development --approve-simulation opt-in is required")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("Output directory must be empty; retained failures cannot be overwritten")
    before = sources()
    reference_bytes = args.separation_study.read_bytes()
    study = json.loads(reference_bytes)
    initial, cutoff_time, _, reference_verification = base.inputs(study)
    profile, config = study["profile"], study["catch_profile"]
    baseline_bundle_bytes = (args.baseline_dir / "comparison.json").read_bytes()
    prior = json.loads(baseline_bundle_bytes)
    if (prior.get("schema") != "missionos.starship_boostback_comparison.v1"
            or prior.get("initial_state_sha256") != base.digest(initial)
            or prior.get("profile_sha256") != before[base.SIXDOF_PROFILE]
            or prior.get("catch_profile_sha256") != before[base.CATCH_PROFILE]
            or len(prior.get("conditions", [])) != 5):
        raise ValueError("unmatched_prior_baseline")
    # Validate every previous saved condition before spending integration time.
    previous_runs = []
    for offset, condition in zip(base.OFFSETS_S, prior["conditions"]):
        expected_name = f"cutoff-minus-{int(offset):02d}.json"
        raw = (args.baseline_dir / expected_name).read_bytes()
        old = json.loads(raw)
        if condition.get("run_file") != expected_name or condition.get("run_sha256") != sha256(raw).hexdigest():
            raise ValueError("prior_baseline_hash_mismatch")
        verdict = base.verify_recovery(old["run"], initial, profile, config,
            catch_run=old["catch_run"], development_cutoff_time_s=cutoff_time-offset)
        if verdict["passed"] is not True:
            raise ValueError("prior_baseline_verification_failed")
        previous_runs.append(old["run"])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    base.write(args.output_dir / "inputs.json", {"initial_state": initial, "profile": profile, "catch_profile": config,
        "reference_study_sha256": sha256(reference_bytes).hexdigest(), "reference_verification": reference_verification,
        "prior_comparison_sha256": sha256(baseline_bundle_bytes).hexdigest(), "offsets_s": base.OFFSETS_S,
        "candidate_policy": POLICY_ID, "regularization": REGULARIZATION, "source_sha256": before,
        "missionos_approval": None, "opt_in_scope": "local_development", "production_policy_admitted": False})
    for name in before:
        path = args.output_dir / "source" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    conditions, physical_prefixes = [], []
    earliest = cutoff_time-max(base.OFFSETS_S)
    try:
        for index, offset in enumerate(base.OFFSETS_S):
            pair = {"offset_s": offset, "cutoff_time_s": cutoff_time-offset, "methods": {}}
            paired_runs = {}
            for method in ("baseline", "candidate"):
                candidate = method == "candidate"
                started = time.monotonic()
                run = base.simulate_recovery(deepcopy(profile), deepcopy(initial), deepcopy(config),
                    _development_cutoff_time_s=cutoff_time-offset, _development_fin_allocation=candidate)
                catch = None
                if run["recovery_record"]["handoff"]["eligible"]:
                    catch = base.simulate_catch(profile, config, initial_state=run["final_state"], duration_s=30., control_policy="net_thrust_trim_v1")
                persisted = json.loads(json.dumps({"run": run, "catch_run": catch}, allow_nan=False))
                run, catch = persisted["run"], persisted["catch_run"]
                filename = f"{method}-minus-{int(offset):02d}.json"
                # Save before checking, so verifier failures are retained too.
                base.write(args.output_dir / filename, persisted)
                verification = base.verify_recovery(run, initial, profile, config, catch_run=catch,
                    development_cutoff_time_s=cutoff_time-offset, development_fin_allocation=candidate)
                item = base.summarize(run, catch, verification, profile, config, cutoff_time-offset, time.monotonic()-started)
                item.update(method=method, run_file=filename, run_sha256=sha256((args.output_dir / filename).read_bytes()).hexdigest(),
                    metrics=sampled_metrics(run, profile), replay=replay_points(run, profile, config))
                if not candidate:
                    prior_run = previous_runs[index]
                    item["prior_physical_checkpoints_equal"] = ([c["state"] for c in run["recovery_record"]["checkpoints"]]
                        == [c["state"] for c in prior_run["recovery_record"]["checkpoints"]])
                    item["prior_commands_equal"] = ([c["command"] for c in run["recovery_record"]["checkpoints"]]
                        == [c["command"] for c in prior_run["recovery_record"]["checkpoints"]])
                pair["methods"][method], paired_runs[method] = item, run
                physical_prefixes.append([c["state"] for c in run["recovery_record"]["checkpoints"] if c["time_s"] < earliest-1e-9])
                base.write(args.output_dir / f"{method}-minus-{int(offset):02d}-summary.json", item)
                print(json.dumps({"method": method, "offset_s": offset, "termination": run["outcome"]["termination"],
                    "speed_mps": run["outcome"]["final_ground_speed_mps"], "distance_m": run["outcome"]["return_site_distance_m"],
                    "fuel_kg": run["final_state"]["propellant_kg"], "verified": verification["passed"],
                    "issues": verification["issues"]}, allow_nan=False), flush=True)
            baseline = paired_runs["baseline"]["recovery_record"]["checkpoints"]
            changed = paired_runs["candidate"]["recovery_record"]["checkpoints"]
            first = next((i for i, c in enumerate(changed) if "development_fin_allocation" in c["navigation"]), len(changed))
            pair["first_saved_allocation_time_s"] = changed[first]["time_s"] if first < len(changed) else None
            pair["pre_allocation_saved_physical_prefix_equal"] = (len(baseline) >= first and
                [c["state"] for c in baseline[:first]] == [c["state"] for c in changed[:first]])
            conditions.append(pair)
        after = sources()
        prefix_equal = all(p == physical_prefixes[0] for p in physical_prefixes[1:])
        passed = (before == after and prefix_equal and all(p["pre_allocation_saved_physical_prefix_equal"]
            and p["methods"]["baseline"]["prior_physical_checkpoints_equal"] and p["methods"]["baseline"]["prior_commands_equal"]
            and all(c["verification"]["passed"] and c["scheduled_cutoff_executed"] for c in p["methods"].values()) for p in conditions))
        bundle = {"schema": "missionos.starship_fin_comparison.v1", "verification_passed": passed,
            "conditions": conditions, "source_sha256_before": before, "source_sha256_after": after,
            "source_unchanged": before == after, "common_prefix_equal": prefix_equal,
            "common_prefix_checkpoint_count": len(physical_prefixes[0]), "candidate_policy": POLICY_ID,
            "candidate_supported_count": sum(p["methods"]["candidate"]["candidate_supported"] for p in conditions),
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
