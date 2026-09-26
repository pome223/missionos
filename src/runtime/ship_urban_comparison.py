"""Reverify saved local SITL runs before comparing their urban action outcomes."""

import hashlib
import json
import math
from pathlib import Path

from missionos_core import canonical_sha256

from src.runtime.runtime_claim_evidence import validate_runtime_invocation_evidence
from src.runtime.ship_delivery_sitl import _read_jsonl, verify_ship_sitl_run
from src.runtime.ship_urban_decision import CASES


def compare_urban_runs(directories):
    runs = []
    for directory in directories:
        root = Path(directory)
        report = json.loads((root / "result.json").read_text())
        config = json.loads((root / "config.json").read_text())
        if report.get("config") != config or not config.get("urban"):
            raise ValueError("Run configuration mismatch or missing urban experiment")
        if report.get("run_id") != config["run_id"]:
            raise ValueError("Run identity mismatch")
        if canonical_sha256(report["world_files_sha256"]) != config["world_sha256"]:
            raise ValueError("World hash manifest is not bound to the runtime configuration")
        plan = {
            # The scenario schema fixes this legacy fixture field; SITL reports
            # omit it because execution_backend identifies the real runtime.
            "scenario": {"backend": "kinematic_fixture", **report["scenario_parameters"]},
            "missions": report["missions"],
            "world": report["world_files_sha256"],
            "urban": config["urban"],
        }
        if canonical_sha256(plan) != config["plan_sha256"]:
            raise ValueError("Scenario or mission plan is not bound to the runtime configuration")
        verified = verify_ship_sitl_run(
            _read_jsonl(root / "telemetry.jsonl"), _read_jsonl(root / "events.jsonl"), config
        )
        invocation = validate_runtime_invocation_evidence(report.get("runtime_invocation_evidence"))
        if (
            invocation.get("run_id") != config["run_id"]
            or invocation.get("execution_scope") != "sim"
        ):
            raise ValueError("Invocation is not bound to this simulation run")
        if (
            invocation["worker_sha256"]
            != hashlib.sha256((root / "worker.py").read_bytes()).hexdigest()
        ):
            raise ValueError("Worker artifact hash mismatch")
        if any(
            hashlib.sha256((root / "models" / name).read_bytes()).hexdigest() != digest
            for name, digest in report["world_files_sha256"].items()
        ):
            raise ValueError("World artifact hash mismatch")
        if (
            not verified["verified"]
            or invocation["invocation_exit_code"] != 0
            or report.get("status") != "completed"
        ):
            raise ValueError(f"Cannot compare an unsuccessful or unverified run: {root.name}")
        runs.append(
            {
                "run_id": config["run_id"],
                "case": config["urban"]["case"],
                "scenario": report["scenario_parameters"],
                "world_sha256": config["world_sha256"],
                "image_id": report["image_id"],
                "worker_sha256": invocation["worker_sha256"],
                "support_code_sha256": {
                    name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for name in ("urban_policy.py", "ship_urban_sitl_stage.py")
                },
                **verified["urban_verification"],
            }
        )
    return compare_verified_outcomes(runs)


def compare_verified_outcomes(runs):
    """Pure aggregation; callers must first verify raw runtime evidence."""
    if (
        len(runs) != 4
        or len({r["run_id"] for r in runs}) != len(runs)
        or any(r["case"] not in CASES for r in runs)
    ):
        raise ValueError("Missing runs or duplicate run identity")
    if any(
        r.get("verified") is not True
        or r.get("collision_observed")
        or not r.get("geometric_clearance_verified")
        or not math.isfinite(r["urban_elapsed_s"])
        or r["urban_elapsed_s"] <= 0
        for r in runs
    ):
        raise ValueError("All outcomes must have valid timing and verified clearance")
    for key in ("scenario", "world_sha256", "image_id", "worker_sha256", "support_code_sha256"):
        if any(r[key] != runs[0][key] for r in runs):
            raise ValueError(f"Unmatched experiment configuration: {key}")
    pairs = []
    for case in CASES:
        case_runs = [r for r in runs if r["case"] == case]
        candidates = {r["action"]: r for r in case_runs}
        rules = [r for r in case_runs if r["policy"] == "constant_velocity"]
        if len(case_runs) != 2 or set(candidates) != {"wait", "detour"} or len(rules) != 1:
            raise ValueError(
                "Require one velocity-rule run and the opposite fixed-action run for each case"
            )
        rule = rules[0]
        best = min(case_runs, key=lambda r: r["urban_elapsed_s"])
        other = next(r for r in case_runs if r is not rule)
        pairs.append(
            {
                "case": case,
                "observed_actions": {
                    a: {
                        k: r[k]
                        for k in (
                            "run_id",
                            "policy",
                            "urban_elapsed_s",
                            "max_outbound_lateral_m",
                            "collision_observed",
                            "geometric_clearance_verified",
                        )
                    }
                    for a, r in candidates.items()
                },
                "rule_action": rule["action"],
                "best_observed_action": best["action"],
                "rule_gain_over_opposite_action_s": other["urban_elapsed_s"]
                - rule["urban_elapsed_s"],
                "headroom_over_rule_s": rule["urban_elapsed_s"] - best["urban_elapsed_s"],
            }
        )
    return {
        "schema_version": "ship_urban_runtime_comparison.v1",
        "status": "compared",
        "verified_runs": len(runs),
        "comparison_scope": "simulator_state_baseline",
        "cases": pairs,
        "mean_observed_headroom_over_rule_s": sum(p["headroom_over_rule_s"] for p in pairs)
        / len(pairs),
        "learned_model_comparison_admitted": False,
        "vla_invoked": False,
        "wam_invoked": False,
        "limitations": [
            "Two cases, one run per candidate action; no statistical or held-out efficacy claim.",
            "The best observed wait/detour action is an offline comparator, not a dispatch policy.",
            "Privileged simulator position history; camera-matched observations are not implemented.",
            "Comparison interval is urban entry to delivery hover; common return detour is excluded.",
            "Elapsed seconds are worker wall-clock time, including simulator load and control overhead.",
        ],
    }
