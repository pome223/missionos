"""Opt-in historical-incident-inspired faults in the actual concurrent 6DOF plant.

This standalone development runner does not invoke providers or reuse a managed
flight's approval. History supplies failure families, not failure probabilities.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
import importlib.metadata
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_trouble_engine import FAMILIES, make_manifest, validate_manifest  # noqa: E402
from src.runtime.starship_trouble_verifier import verify_run  # noqa: E402
from src.runtime.starship_sixdof_mission import simulate_concurrent  # noqa: E402
from src.runtime.starship_artifacts import write_verified_input  # noqa: E402
from src.runtime.starship_sixdof_report import build_report  # noqa: E402
from src.runtime.starship_sixdof_verifier import verify_study  # noqa: E402

CATALOG = "docs/agents/starship-incident-catalog.json"
PROFILE = "examples/spaceflight/starship-sixdof-profile.json"


def sources():
    paths = [ROOT / PROFILE, ROOT / CATALOG, Path(__file__).resolve()]
    paths += sorted((ROOT / "src/runtime").glob("starship_*.py"))
    return {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in paths}



def render_report(study, verification):
    """Presentation-only annotations; never mutate the saved flight evidence."""
    view = deepcopy(study)
    view["checks"] = [
        {"name": "Fault record consistency", "passed": verification["trouble"]["passed"],
         "scope": "故障予定・適用記録・観測の照合。帰還成功や実機の再現精度は示しません。"},
        {"name": "6DOF record consistency", "passed": verification["sixdof"]["passed"],
         "scope": "保存した物理計算の記録を検証。衝突もそのまま結果として残します。"},
    ]
    view["coverage"] = [
        {"component": "Trouble engine", "implementation": "シードで固定した能力喪失・機構不良", "status": "試験用の合成故障"},
        {"component": "Fault propagation", "implementation": "故障後も位置・姿勢・角速度・燃料を積分", "status": "6DOF"},
        {"component": "MissionOS AI", "implementation": "この実行ではモデルを呼びません", "status": "故障あり管制は未接続"},
    ]
    for run in view["runs"]:
        for receipt in run["trouble"]["receipts"]:
            body = run if receipt["body"] == "ship" else run.get("booster_run")
            if body is not None:
                body["events"].append({"time_s": receipt["applied_at_s"],
                    "event": "injected_" + receipt["family"],
                    "detail": receipt["body"] + " / synthetic capability fault / not actual flight telemetry"})
        for body in (run, run.get("booster_run")):
            if body is not None:
                body["events"].sort(key=lambda event: event["time_s"])
    return build_report(view)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--seed", type=int)
    identity.add_argument("--manifest", type=Path, help="Replay an exact previously frozen manifest")
    parser.add_argument("--family", action="append", choices=tuple(FAMILIES))
    parser.add_argument("--max-events", type=int, default=1)
    parser.add_argument("--nominal-probability", type=float, default=.2)
    parser.add_argument("--duration-s", type=float, help="Absolute simulation deadline; unreached events remain unapplied")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.approve_simulation:
        parser.error("explicit --approve-simulation is required")
    if args.manifest and (args.family is not None or args.max_events != 1 or args.nominal_probability != .2):
        parser.error("a replay manifest already fixes its distribution")
    if args.output_dir.exists():
        parser.error("use a fresh output directory; never overwrite failed trials")
    try:
        manifest = (validate_manifest(json.loads(args.manifest.read_text())) if args.manifest else
                    make_manifest(args.seed, allowed_families=args.family, max_events=args.max_events,
                                  nominal_probability=args.nominal_probability))
        profile = json.loads((ROOT / PROFILE).read_text())
        duration = args.duration_s if args.duration_s is not None else profile["integration"]["max_duration_s"]
        import math
        if not math.isfinite(duration) or not 0 < duration <= profile["integration"]["max_duration_s"]:
            raise ValueError("duration must be positive and no greater than the profile horizon")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    frozen = sources()
    backend = {name: importlib.metadata.version(name) for name in ("numpy", "scipy")}
    backend["python"] = sys.version.split()[0]
    catalog = json.loads((ROOT / CATALOG).read_text())
    used_families = {row["family"] for row in manifest["events"]}
    incident_basis = [{"flight_number": flight["flight_number"], "incident_id": incident["id"],
        "source_urls": incident["source_urls"], "family": mapping["family"],
        "mapping": mapping["status"]}
        for flight in catalog["flights"] for incident in flight["incidents"]
        for mapping in incident["simulation_mapping"] if mapping["family"] in used_families]
    args.output_dir.mkdir(parents=True)
    # Persist latent ground truth BEFORE physics. Never use this file as model input.
    write_verified_input(args.output_dir / "manifest.json", manifest)
    write_verified_input(args.output_dir / "inputs.json", {
        "schema": "missionos.starship_trouble_trial_inputs.v1", "profile": profile,
        "duration_s": duration, "source_sha256": frozen, "backend": backend,
        "incident_basis": incident_basis,
        "model_inference_invoked": False, "missionos_approval": None,
        "fault_return_qualification": False, "physical_execution": False})
    started = time.monotonic()
    try:
        run = simulate_concurrent(profile, duration_s=duration, trouble_manifest=manifest)
        study = {"schema": "missionos.starship_sixdof_study.v1", "title": "STARSHIP / TROUBLE ENGINE",
            "profile": profile, "runs": [run], "provenance": {
                "source_sha256": frozen, "backend": backend, "physical_execution_invoked": False,
                "starship_vehicle_validated": False, "model_inference_invoked": False,
                "scope": "Synthetic capability faults, not historical flight reconstruction or recovery qualification"}}
        write_verified_input(args.output_dir / "study.json", study)
        # Verify serialized evidence; producers own tuple-to-JSON conversion.
        saved = json.loads((args.output_dir / "study.json").read_text())
        faults = verify_run(saved["runs"][0])
        physics_record = verify_study(saved, expected_scenario="launch")
        verification = {"passed": faults["passed"] and physics_record["passed"],
            "trouble": faults, "sixdof": physics_record,
            "claim": "record_consistency_only_not_recovery_or_ai_success"}
        write_verified_input(args.output_dir / "verification.json", verification)
        summary = {"schema": "missionos.starship_trouble_trial_summary.v1",
            "source_unchanged": sources() == frozen, "manifest_sha256": run["trouble"]["manifest_sha256"],
            "applied_faults": len(run["trouble"]["receipts"]),
            "unapplied_event_ids": run["trouble"]["unapplied_event_ids"],
            "record_checks_passed": verification["passed"], "outcome": run["outcome"],
            "booster_outcome": run["booster_run"]["outcome"] if run["booster_run"] else None,
            "wall_s": time.monotonic()-started, "model_inference_invoked": False,
            "fault_return_qualified": False, "historical_fidelity_verified": False,
            "study_sha256": sha256((args.output_dir / "study.json").read_bytes()).hexdigest()}
        write_verified_input(args.output_dir / "summary.json", summary)
        (args.output_dir / "report.html").write_text(render_report(saved, verification))
        print(json.dumps({key: summary[key] for key in ("record_checks_passed", "source_unchanged",
            "applied_faults", "unapplied_event_ids", "wall_s")}, allow_nan=False))
        # Record consistency is not flight success. Impacts are retained as impacts.
        return 0 if summary["source_unchanged"] and verification["passed"] else 2
    except Exception as error:
        write_verified_input(args.output_dir / "failure.json", {
            "exception": type(error).__name__, "detail": str(error),
            "source_unchanged": sources() == frozen, "wall_s": time.monotonic()-started,
            "automatic_retry": False,
            "concurrent_execution": getattr(error, "concurrent_execution", None)})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
