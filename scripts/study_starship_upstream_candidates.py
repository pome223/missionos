"""Opt-in baseline replay and five upstream command policies, with fixed physics."""
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
from src.runtime.starship_booster_catch import simulate_catch  # noqa: E402
from src.runtime.starship_capture_diagnostics import state_diagnostic  # noqa: E402
from src.runtime.starship_landing_context import digest  # noqa: E402
from src.runtime.starship_sixdof_catalog import SIXDOF_SOURCES, SIXDOF_PROFILE, CATCH_PROFILE  # noqa: E402
from src.runtime.starship_upstream_report import report  # noqa: E402


def write(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, separators=(",", ":"), allow_nan=False)


def sources():
    names = (*SIXDOF_SOURCES, "src/runtime/starship_capture_diagnostics.py",
             "src/runtime/starship_upstream_report.py", "scripts/study_starship_upstream_candidates.py")
    return {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in names}


def load_inputs(directory):
    raw_inputs, raw_comparison = (directory/"inputs.json").read_bytes(), (directory/"comparison.json").read_bytes()
    inputs, comparison = json.loads(raw_inputs), json.loads(raw_comparison)
    if (comparison.get("schema") != "missionos.starship_boostback_comparison.v1"
            or comparison.get("verification_passed") is not True or len(comparison.get("conditions", [])) != 5
            or comparison.get("initial_state_sha256") != digest(inputs["initial_state"])
            or inputs["profile"] != json.loads((ROOT/SIXDOF_PROFILE).read_text())
            or inputs["catch_profile"] != json.loads((ROOT/CATCH_PROFILE).read_text())):
        raise ValueError("unmatched_upstream_inputs")
    item = comparison["conditions"][0]
    raw = (directory/"cutoff-minus-00.json").read_bytes()
    if (item["run_file"] != "cutoff-minus-00.json" or item["run_sha256"] != sha256(raw).hexdigest()
            or item["cutoff_time_s"] != inputs["reference_cutoff_time_s"]):
        raise ValueError("baseline_lineage_mismatch")
    old = json.loads(raw)
    verdict = verify_recovery(old["run"], inputs["initial_state"], inputs["profile"], inputs["catch_profile"],
                             catch_run=old["catch_run"], development_cutoff_time_s=item["cutoff_time_s"])
    if not verdict["passed"]:
        raise ValueError("invalid_baseline_record")
    binding = {"source_inputs_sha256": sha256(raw_inputs).hexdigest(),
               "source_comparison_sha256": sha256(raw_comparison).hexdigest(), "source_run_sha256": sha256(raw).hexdigest(),
               "source_reference_sha256": inputs["reference_study_sha256"]}
    return inputs, item["cutoff_time_s"], old["run"], binding


def same_physical_replay(run, old):
    return (run["final_state"] == old["final_state"] and
        [{k: p[k] for k in ("state", "command")} for p in run["recovery_record"]["checkpoints"]] ==
        [{k: p[k] for k in ("state", "command")} for p in old["recovery_record"]["checkpoints"]])


def needs_contact_continuation(verdict):
    # The recovery checker has checked the complete prefix, actual arrival and
    # exact handoff state before this specific final dependency. Never bypass
    # another issue, and never promote this intermediate verdict to passed.
    return verdict.get("passed") is False and verdict.get("issues") == [{
        "code": "catch_binding", "detail": "Eligible handoff requires the actual terminal continuation"}]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.approve_simulation:
        parser.error("Explicit offline --approve-simulation is required")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("Output must be empty; retained records cannot be overwritten")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    before, started, completed, catches, cases = sources(), time.monotonic(), 0, 0, []
    write(args.output_dir/"attempt.json", {"budget": {"recoveries": 6, "catch_calls": 6,
        "recovery_horizon_s": 1200., "catch_horizon_s": 30., "wall_guard_between_calls_s": 600.},
        "source_sha256": before, "candidate_indices": [None, 0, 1, 2, 3, 4],
        "production_policy_admitted": False, "missionos_dispatch": False, "physical_execution": False})
    def budget():
        if time.monotonic()-started > 600:
            raise TimeoutError("upstream_wall_budget_exhausted")
    try:
        inputs, cutoff, old, binding = load_inputs(args.baseline_dir)
        initial, profile, config = inputs["initial_state"], inputs["profile"], inputs["catch_profile"]
        write(args.output_dir/"inputs.json", {"initial_state": initial, "profile": profile, "catch_profile": config,
            "cutoff_time_s": cutoff, "source_sha256": before, **binding})
        for name in before:
            path = args.output_dir/"source"/name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write((ROOT/name).read_bytes())
        for index in (None, 0, 1, 2, 3, 4):
            budget()
            tick = time.monotonic()
            run = simulate_recovery(deepcopy(profile), deepcopy(initial), deepcopy(config),
                _development_cutoff_time_s=cutoff, _development_boostback_candidate_index=index)
            completed += 1
            # Persist and reopen before independent checks; failed records survive.
            label = "baseline" if index is None else f"candidate-{index}"
            filename = f"{label}.json.gz"
            raw = json.dumps(run, separators=(",", ":"), allow_nan=False).encode()
            with (args.output_dir/filename).open("xb") as stream:
                stream.write(gzip.compress(raw, mtime=0))
            compressed = (args.output_dir/filename).read_bytes()
            run = json.loads(gzip.decompress(compressed))
            preliminary = verify_recovery(run, initial, profile, config, development_cutoff_time_s=cutoff,
                                          development_boostback_candidate_index=index)
            write(args.output_dir/f"{label}-verification.json", preliminary)
            needs_contact = needs_contact_continuation(preliminary)
            if not preliminary["passed"] and not needs_contact:
                raise ValueError(f"{label}_record_verification_failed:{preliminary['issues']}")
            if index is None and not same_physical_replay(run, old):
                raise ValueError("baseline_physical_replay_not_exact")
            catch_run = None
            if needs_contact:
                budget()
                catch_run = simulate_catch(deepcopy(profile), deepcopy(config), initial_state=deepcopy(run["final_state"]),
                                           duration_s=30., control_policy="net_thrust_trim_v1")
                catches += 1
                write(args.output_dir/f"{label}-catch.json", catch_run)
                catch_run = json.loads((args.output_dir/f"{label}-catch.json").read_bytes())
            verdict = verify_recovery(run, initial, profile, config, catch_run=catch_run,
                development_cutoff_time_s=cutoff, development_boostback_candidate_index=index)
            if not verdict["passed"]:
                raise ValueError(f"{label}_catch_verification_failed:{verdict['issues']}")
            landing = next((e for e in run["events"] if e["event"] == "landing_stage_requested"), None)
            cutoff_event = next((e for e in run["events"] if e["event"] == "boostback_complete_rate_settle"), None)
            states = [state_diagnostic(p["state"], profile, config) for p in run["recovery_record"]["checkpoints"]]
            cases.append({"label": label, "candidate_index": index, "run_file": filename,
                "run_sha256": sha256(compressed).hexdigest(), "expanded_sha256": sha256(raw).hexdigest(),
                "baseline_physical_replay_exact": True if index is None else None,
                "cutoff_time_s": cutoff_event["time_s"] if cutoff_event else None,
                "cutoff_basis": cutoff_event.get("cutoff_basis") if cutoff_event else None,
                "landing_request": state_diagnostic(landing["state"], profile, config) if landing else None,
                "plans": [{"time_s": e["time_s"], "plan": e["plan"]} for e in run["events"] if "plan" in e],
                "outcome": run["outcome"], "terminal": states[-1], "states": states,
                "verification": verdict, "catch_invoked": catch_run is not None,
                "catch_outcome": catch_run["outcome"] if catch_run else None, "wall_time_s": time.monotonic()-tick})
            write(args.output_dir/f"{label}-summary.json", {k: v for k, v in cases[-1].items() if k != "states"})
            print(json.dumps({"case": label, "handoff": verdict["handoff_reached"],
                "support": verdict["catch_supported_after_handoff"], "speed_mps": run["outcome"]["final_ground_speed_mps"]}), flush=True)
        if before != sources():
            raise ValueError("sources_changed_during_upstream_study")
        bundle = {"schema": "missionos.starship_upstream_study.v1", "cases": cases, "cutoff_time_s": cutoff,
            "recovery_count": completed, "catch_call_count": catches, "source_sha256": before, "source_unchanged": True,
            "handoff_count": sum(c["verification"]["handoff_reached"] for c in cases),
            "support_count": sum(c["verification"]["catch_supported_after_handoff"] for c in cases),
            "production_policy_admitted": False, "missionos_dispatch": False, "physical_execution": False,
            "full_launch_reexecuted": False, "independent_dynamics_reexecution": False, "model_value_established": False}
        write(args.output_dir/"study.json", bundle)
        with (args.output_dir/"report.html").open("x") as stream:
            stream.write(report(bundle))
        print(json.dumps({"completed": True, "recoveries": completed, "catch_calls": catches,
                          "handoff": bundle["handoff_count"], "support": bundle["support_count"]}), flush=True)
        return 0
    except Exception as exc:
        write(args.output_dir/"failure.json", {"exception": type(exc).__name__, "detail": str(exc),
            "returned_recovery_count": completed, "returned_catch_count": catches, "verified_cases": len(cases),
            "source_sha256_after": sources()})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
