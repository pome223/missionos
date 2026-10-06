#!/usr/bin/env python3
"""One opt-in finite-actuator return attempt; preserve negative evidence."""
from __future__ import annotations

import argparse
import gzip
import json
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
import sys
import re
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.runtime.starship_booster_catch import simulate_catch  # noqa: E402
from src.runtime.starship_constrained_recovery import simulate_constrained_recovery  # noqa: E402
from src.runtime.starship_constrained_recovery_verifier import verify_constrained_recovery  # noqa: E402
from src.runtime.starship_actual_recovery_shooting import actual_planning_configuration  # noqa: E402
from src.runtime.starship_actual_forecast_corpus import verify_forecast_corpus  # noqa: E402
from src.runtime.starship_sixdof_catalog import SIXDOF_SOURCES  # noqa: E402
from scripts.study_starship_upstream_candidates import load_inputs  # noqa: E402

SOURCES = (*SIXDOF_SOURCES, "src/runtime/starship_capture_diagnostics.py",
    "scripts/study_starship_upstream_candidates.py", "src/runtime/starship_upstream_report.py",
    "src/runtime/starship_constrained_guidance.py", "src/runtime/starship_constrained_recovery.py",
    "src/runtime/starship_constrained_recovery_verifier.py", "src/runtime/starship_boostback_shooting.py",
    "src/runtime/starship_wind.py", "src/runtime/starship_wind_verifier.py",
    "src/runtime/starship_terminal_guidance.py",
    "src/runtime/starship_entry_pretrim.py",
    "src/runtime/starship_entry_pretrim_verifier.py",
    "src/runtime/starship_reference_tracking.py", "src/runtime/starship_reference_tracking_verifier.py",
    "src/runtime/starship_actual_recovery_shooting.py", "src/runtime/starship_actual_recovery_planning_verifier.py",
    "src/runtime/starship_actual_forecast_corpus.py",
    "scripts/study_starship_constrained_return.py")


def write(path, value):
    with path.open("x") as file:
        json.dump(value, file, allow_nan=False, separators=(",", ":"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--duration-s", type=float, default=1200.)
    parser.add_argument("--actual-sixdof-planning", action="store_true")
    parser.add_argument("--transport-prepare-roll", action="store_true")
    parser.add_argument("--baseline-flight-dir", type=Path)
    args = parser.parse_args(argv)
    if not args.approve_simulation:
        parser.error("local simulation requires --approve-simulation")
    if not 0 < args.duration_s <= 1200:
        parser.error("duration must be in (0,1200]")
    if args.actual_sixdof_planning != (args.baseline_flight_dir is not None):
        parser.error("Actual planning requires its matching saved nominal baseline, used only for equivalence validation")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("refusing to replace an existing attempt")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    attempted, completed, catch_attempted, catch_completed = 0, 0, 0, 0
    try:
        inputs, _, _, lineage = load_inputs(args.baseline_dir)
        baseline_reference = None
        baseline_binding = None
        if args.actual_sixdof_planning:
            baseline_result = json.loads((args.baseline_flight_dir/"result.json").read_text())
            baseline_inputs = json.loads((args.baseline_flight_dir/"inputs.json").read_text())
            baseline_raw = gzip.decompress((args.baseline_flight_dir/"return.json.gz").read_bytes())
            baseline_reference = json.loads(baseline_raw)
            if (sha256(baseline_raw).hexdigest() != baseline_result["return_json_sha256"]
                    or sha256((args.baseline_flight_dir/"return.json.gz").read_bytes()).hexdigest() != baseline_result["return_gzip_sha256"]
                    or baseline_reference.get("guidance_policy") !=
                        ("constrained_return_development_v10" if args.transport_prepare_roll else "constrained_return_development_v8")
                    or any(baseline_inputs[key] != inputs[key] for key in ("profile", "catch_profile", "initial_state"))
                    or any(sha256((args.baseline_flight_dir/"sources"/name).read_bytes()).hexdigest() != value
                           for name, value in baseline_result["source_sha256"].items())):
                raise ValueError("unbound_actual_planning_baseline_reference")
            baseline_verdict = verify_constrained_recovery(baseline_reference, inputs["initial_state"],
                inputs["profile"], inputs["catch_profile"])
            if not baseline_verdict["passed"] or baseline_verdict != baseline_result["verification"]:
                raise ValueError("invalid_actual_planning_baseline_reference")
            if args.transport_prepare_roll:
                def canonical_hash(value):
                    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                baseline_binding = {"run_sha256": canonical_hash(baseline_reference),
                    "profile_sha256": canonical_hash(baseline_inputs["profile"]),
                    "catch_profile_sha256": canonical_hash(baseline_inputs["catch_profile"])}
        source = {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}
        runtime_versions = {"python": sys.version, "numpy": version("numpy"), "scipy": version("scipy")}
        write(args.output_dir/"inputs.json", {**inputs, **lineage, "source_sha256": source})
        snapshots = args.output_dir/"sources"
        for name in source:
            target = snapshots/name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as file:
                file.write((ROOT/name).read_bytes())
        write(args.output_dir/"attempt.json", {"recovery_call_budget": 1, "catch_call_budget": 1,
            "maximum_recovery_duration_s": args.duration_s, "maximum_catch_duration_s": 30.,
            "initial_state_sha256": sha256(json.dumps(inputs["initial_state"], sort_keys=True,
                separators=(",", ":")).encode()).hexdigest(), "source_sha256": source,
            "candidate_generation": True, "production_policy_admitted": False,
            "actual_sixdof_planning": args.actual_sixdof_planning,
            "transport_prepare_roll": args.transport_prepare_roll,
            "actual_planning_budget": actual_planning_configuration(args.transport_prepare_roll)
                if args.actual_sixdof_planning else None,
            "runtime_versions": runtime_versions,
            "missionos_dispatch": False, "physical_execution": False})

        def persist_forecast(artifact_id, payload):
            if (not re.fullmatch(r"[a-f0-9]{32}", artifact_id) or type(payload) is not dict
                    or payload.get("kind") not in ("origin_context", "attempted", "raw_forecast")):
                raise ValueError("invalid_forecast_artifact")
            raw_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            compressed_payload = payload["kind"] == "raw_forecast"
            stored = gzip.compress(raw_payload, mtime=0) if compressed_payload else raw_payload
            relative = artifact_id+(".json.gz" if compressed_payload else ".json")
            path = args.output_dir/"forecasts"/relative
            path.parent.mkdir(exist_ok=True)
            with path.open("xb") as file:
                file.write(stored)
            return {"artifact_id": artifact_id, "format": "json.gz" if compressed_payload else "json",
                "relative_path": relative, "sha256": sha256(stored).hexdigest(),
                "raw_json_sha256": sha256(raw_payload).hexdigest(), "bytes": len(stored),
                "persisted_before_analysis": True}

        attempted += 1
        run = simulate_constrained_recovery(inputs["profile"], inputs["initial_state"], inputs["catch_profile"],
            duration_s=args.duration_s, development_actual_planning=args.actual_sixdof_planning,
            development_transport_prepare_roll=args.transport_prepare_roll,
            actual_planning_artifact_sink=persist_forecast if args.actual_sixdof_planning else None,
            actual_planning_baseline_reference=baseline_reference,
            actual_planning_baseline_binding=baseline_binding)
        completed += 1
        raw = json.dumps(run, allow_nan=False, separators=(",", ":")).encode()
        compressed = gzip.compress(raw, mtime=0)
        with (args.output_dir/"return.json.gz").open("xb") as file:
            file.write(compressed)
        run = json.loads(gzip.decompress((args.output_dir/"return.json.gz").read_bytes()))
        if any(sha256((ROOT/name).read_bytes()).hexdigest() != value for name, value in source.items()):
            raise ValueError("source_changed_during_execution")
        corpus_verdict = None
        if args.actual_sixdof_planning:
            corpus_verdict = verify_forecast_corpus(args.output_dir/"forecasts",
                run["recovery_record"].get("actual_planning_receipt"),
                baseline_reference=baseline_reference, source_sha256=source, executed_run=run)
            write(args.output_dir/"forecast-corpus-verification.json", corpus_verdict)
            if not corpus_verdict["passed"]:
                raise ValueError("invalid_external_forecast_corpus")
        preliminary = verify_constrained_recovery(run, inputs["initial_state"], inputs["profile"], inputs["catch_profile"])
        write(args.output_dir/"preliminary-verification.json", preliminary)
        catch = None
        if preliminary.get("requires_catch_continuation") is True:
            catch_attempted += 1
            catch = simulate_catch(inputs["profile"], inputs["catch_profile"], initial_state=run["final_state"],
                duration_s=30., control_policy="net_thrust_trim_v1")
            catch_completed += 1
            write(args.output_dir/"catch.json", catch)
        elif not preliminary["passed"]:
            raise ValueError("invalid_constrained_return_record")
        final = verify_constrained_recovery(run, inputs["initial_state"], inputs["profile"], inputs["catch_profile"], catch_run=catch)
        write(args.output_dir/"verification.json", final)
        if not final["passed"]:
            raise ValueError("invalid_constrained_return_or_catch")
        if any(sha256((ROOT/name).read_bytes()).hexdigest() != value for name, value in source.items()):
            raise ValueError("source_changed_during_execution")
        write(args.output_dir/"result.json", {"schema": "missionos.starship_constrained_return_study.v1",
            "outcome": run["outcome"], "arrival": run["recovery_record"]["handoff"]["observation"],
            "verification": final, "recovery_calls_attempted": attempted, "recovery_calls_completed": completed,
            "catch_calls_attempted": catch_attempted, "catch_calls_completed": catch_completed,
            "return_gzip_sha256": sha256(compressed).hexdigest(), "return_json_sha256": sha256(raw).hexdigest(),
            "source_sha256": source, "source_unchanged": True, "wall_s": monotonic()-started,
            "actual_planning_corpus_verification": corpus_verdict,
            "runtime_versions": runtime_versions,
            "full_launch_reexecuted": False, "physical_execution": False, "missionos_dispatch": False,
            "production_policy_admitted": False, "model_value_established": False})
        print(json.dumps({"termination": run["outcome"]["termination"], "verification_passed": final["passed"],
            "handoff_reached": final["handoff_reached"], "catch_supported_after_handoff": final["catch_supported_after_handoff"]}))
        return 0
    except Exception as error:
        write(args.output_dir/"failure.json", {"error_type": type(error).__name__, "error": str(error),
            "recovery_calls_attempted": attempted, "recovery_calls_completed": completed,
            "catch_calls_attempted": catch_attempted, "catch_calls_completed": catch_completed,
            "wall_s": monotonic()-started,
            "raw_records_retained": any(args.output_dir.glob("*.json.gz")),
            "forecast_files_retained": len(list((args.output_dir/"forecasts").glob("*.json*"))),
            "forecast_receipt": getattr(error, "forecast_receipt", None)})
        print(json.dumps({"status": "failed", "error_type": type(error).__name__, "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
