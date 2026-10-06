"""Diagnose fifteen existing return records. No simulation or provider is invoked."""
from __future__ import annotations

import argparse
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_booster_recovery_verifier import verify_recovery  # noqa: E402
from src.runtime.starship_capture_diagnostics import analyze_run  # noqa: E402
from src.runtime.starship_capture_diagnostic_report import report  # noqa: E402

OFFSETS = (0, 5, 10, 15, 20)
SOURCES = ("scripts/diagnose_starship_capture.py", "src/runtime/starship_capture_diagnostics.py",
           "src/runtime/starship_capture_diagnostic_report.py", "src/runtime/starship_booster_recovery_verifier.py",
           "src/runtime/starship_booster_catch_verifier.py")


def source_hashes():
    return {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}


def read_json(path, expected_hash=None):
    raw = path.read_bytes()
    if len(raw) > 64_000_000 or expected_hash is not None and sha256(raw).hexdigest() != expected_hash:
        raise ValueError("source_file_size_or_hash_mismatch")
    if path.name.endswith(".gz"):
        with gzip.open(path, "rb") as stream:
            payload = stream.read(64_000_001)
    else:
        payload = raw
    if len(payload) > 64_000_000:
        raise ValueError("source_file_size_or_hash_mismatch")
    return json.loads(payload), sha256(raw).hexdigest(), sha256(payload).hexdigest()


def build(baseline_dir, coast_fin_dir, fin_dir):
    original, original_hash, _ = read_json(baseline_dir/"comparison.json")
    coast, coast_hash, _ = read_json(coast_fin_dir/"comparison.json")
    inputs, inputs_hash, _ = read_json(coast_fin_dir/"inputs.json")
    other, other_hash, _ = read_json(baseline_dir/"inputs.json")
    fin, fin_hash, _ = read_json(fin_dir/"comparison.json")
    fin_inputs, fin_inputs_hash, _ = read_json(fin_dir/"inputs.json")
    if (original.get("schema") != "missionos.starship_boostback_comparison.v1"
            or coast.get("schema") != "missionos.starship_coast_fin_comparison.v1"
            or any(b.get("verification_passed") is not True or len(b.get("conditions", [])) != 5 for b in (original, coast))
            or any(inputs[k] != other[k] for k in ("initial_state", "profile", "catch_profile", "reference_study_sha256"))):
        raise ValueError("unmatched_comparison_inputs")
    if (fin.get("schema") != "missionos.starship_fin_comparison.v1" or fin.get("verification_passed") is not True
            or inputs.get("previous_comparison_sha256") != fin_hash
            or fin_inputs.get("prior_comparison_sha256") != original_hash
            or any(inputs[k] != fin_inputs[k] for k in ("initial_state", "profile", "catch_profile", "reference_study_sha256"))):
        raise ValueError("comparison_lineage_mismatch")
    before = source_hashes()
    conditions, bindings = [], {}
    for offset, old, pair in zip(OFFSETS, original["conditions"], coast["conditions"]):
        cutoff = old["cutoff_time_s"]
        if (cutoff != original["conditions"][0]["cutoff_time_s"]-offset
                or pair["cutoff_time_s"] != cutoff or pair["offset_s"] != offset
                or pair.get("landing_request_states_equal") is not True):
            raise ValueError("unmatched_cutoff_conditions")
        methods = {}
        boundary = None
        original_boundary = None
        for method, directory, item, filename, candidate, scope in (
            ("original", baseline_dir, old, f"cutoff-minus-{offset:02d}.json", False, "coast_and_landing"),
            ("all_phase", coast_fin_dir, pair["methods"]["baseline"], f"baseline-minus-{offset:02d}.json.gz", True, "coast_and_landing"),
            ("coast_only", coast_fin_dir, pair["methods"]["candidate"], f"candidate-minus-{offset:02d}.json.gz", True, "coast_only"),
        ):
            if item["run_file"] != filename:
                raise ValueError("unexpected_run_file")
            saved, digest, expanded_digest = read_json(directory/filename, item["run_sha256"])
            if candidate and item.get("uncompressed_run_sha256") != expanded_digest:
                raise ValueError("expanded_source_hash_mismatch")
            run = saved["run"]
            verdict = verify_recovery(run, inputs["initial_state"], inputs["profile"], inputs["catch_profile"],
                catch_run=saved["catch_run"], development_cutoff_time_s=cutoff,
                development_fin_allocation=candidate, development_fin_scope=scope)
            if verdict["passed"] is not True:
                raise ValueError("source_record_verification_failed: "+json.dumps(verdict["issues"]))
            if candidate:
                current = next(e["state"] for e in run["events"] if e["event"] == "landing_stage_requested")
                if boundary is not None and current != boundary:
                    raise ValueError("landing_start_states_differ")
                boundary = current
            else:
                original_boundary = next(e["state"] for e in run["events"] if e["event"] == "landing_stage_requested")
            result = analyze_run(run, inputs["profile"], inputs["catch_profile"])
            key = f"{method}/{filename}"
            bindings[key] = {"run_sha256": digest, "expanded_sha256": expanded_digest,
                             "original_source_sha256": (other if method == "original" else inputs)["source_sha256"]}
            result.update(source_key=key, verification=verdict, catch_invoked=saved["catch_run"] is not None)
            methods[method] = result
        conditions.append({"cutoff_time_s": cutoff, "offset_s": offset, "methods": methods,
                           "all_phase_and_coast_landing_state_equal": True,
                           "original_landing_state_equal": original_boundary == boundary})
    after = source_hashes()
    if before != after:
        raise ValueError("diagnostic_sources_changed")
    return {"schema": "missionos.starship_capture_diagnostics.v1", "conditions": conditions,
            "bindings": bindings, "diagnostic_source_sha256": before,
            "input_sha256": {"original_comparison": original_hash, "coast_comparison": coast_hash,
                             "original_inputs": other_hash, "coast_inputs": inputs_hash,
                             "prior_fin_comparison": fin_hash, "prior_fin_inputs": fin_inputs_hash},
            "source_records_checked": 15, "dynamics_reexecuted": False, "provider_invoked": False,
            "physical_execution": False, "production_policy_admitted": False,
            "scope": "hash-bound saved state diagnostics; sampled gates, not continuous feasibility or causal identification"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--coast-fin-dir", type=Path, required=True)
    parser.add_argument("--fin-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("Output must be empty; previous evidence cannot be overwritten")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # Reserve this output before inspection; do not overwrite a concurrent or
    # retained diagnostic. The marker remains as part of the attempt record.
    with (args.output_dir/"attempt.json").open("x") as stream:
        json.dump({"schema": "missionos.starship_capture_diagnostic_attempt.v1", "dynamics_reexecuted": False}, stream)
    try:
        bundle = build(args.baseline_dir, args.coast_fin_dir, args.fin_dir)
        raw = json.dumps(bundle, allow_nan=False, separators=(",", ":")).encode()
        with (args.output_dir/"diagnostics.json").open("xb") as stream:
            stream.write(raw)
        with (args.output_dir/"report.html").open("x") as stream:
            stream.write(report(bundle))
    except Exception as exc:
        with (args.output_dir/"failure.json").open("x") as stream:
            json.dump({"schema": "missionos.starship_capture_diagnostic_failure.v1",
                       "exception": type(exc).__name__, "detail": str(exc), "dynamics_reexecuted": False}, stream)
        raise
    print(json.dumps({"source_records_checked": 15, "diagnostics_sha256": sha256(raw).hexdigest(),
                      "dynamics_reexecuted": False, "provider_invoked": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
