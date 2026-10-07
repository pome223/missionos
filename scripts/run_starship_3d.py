"""Run an opt-in Starship-inspired 3DOF study or recheck its saved evidence."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "packages/missionos-core/src")]

from src.runtime.starship_flight import FlightProfile, SCENARIOS  # noqa: E402
from src.runtime.starship_study import run_study, verify_study, worker  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="flight14_inspired")
    parser.add_argument("--output-dir", type=Path, default=Path("output/starship-3d"))
    parser.add_argument("--dt-s", type=float, default=1.0)
    parser.add_argument("--sample-interval-s", type=float, default=5.0)
    parser.add_argument(
        "--profile", type=Path, help="JSON object of explicit FlightProfile overrides"
    )
    parser.add_argument(
        "--verify", type=Path, help="Reproduce and numerically verify a saved study locally"
    )
    parser.add_argument("--worker-request", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.verify:
            verdict = verify_study(json.loads(args.verify.read_text()))
            print(json.dumps(verdict, ensure_ascii=False, allow_nan=False))
            return 0 if verdict["study_verified"] else 2
        if not args.approve_simulation:
            print(
                json.dumps(
                    {"status": "simulation_not_started", "required_flag": "--approve-simulation"}
                )
            )
            return 2
        if args.worker_request or args.worker_output:
            if not (args.worker_request and args.worker_output):
                parser.error("Both worker paths are required.")
            print(json.dumps(worker(args.worker_request, args.worker_output), allow_nan=False))
            return 0
        profile = (
            FlightProfile(**json.loads(args.profile.read_text()))
            if args.profile
            else FlightProfile()
        )
        study = run_study(
            args.output_dir,
            approved=True,
            scenarios=list(SCENARIOS) if args.scenario == "all" else [args.scenario],
            profile=profile,
            dt_s=args.dt_s,
            sample_interval_s=args.sample_interval_s,
        )
        study_path = args.output_dir / "study.json"
        study_path.write_text(
            json.dumps(study, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        )
        from src.runtime.starship_3d_report import build_report

        report_path = args.output_dir / "report.html"
        report_path.write_text(build_report(study))
        manifest = {
            "schema": "missionos.starship_3d_manifest.v1",
            "run_id": study["run_id"],
            "files": {
                p.name: sha256(p.read_bytes()).hexdigest()
                for p in (
                    study_path,
                    report_path,
                    args.output_dir / "request.json",
                    args.output_dir / "worker-runs.json",
                    args.output_dir / "worker.stdout.txt",
                    args.output_dir / "worker.stderr.txt",
                )
            },
        }
        (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        summary = {
            "study": str(study_path),
            "report": str(report_path),
            "study_verified": study["verification"]["study_verified"],
            "verification_failures": study["verification"]["reasons"],
            "starship_vehicle_validated": False,
            "scenarios": [
                {
                    "scenario": r["scenario"],
                    "evidence_verified": r["verification"]["evidence_verified"],
                    "failures": r["verification"]["reasons"],
                    "payload_orbit_verified": r["verification"]["payload_orbit_verified"],
                    "ship_soft_contact_verified": r["verification"]["ship_soft_contact_verified"],
                    "contact_speed_mps": r["verification"]["metrics"].get("ship_contact_speed_mps"),
                }
                for r in study["runs"]
            ],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if study["verification"]["study_verified"] else 2
    except (OSError, ValueError, TypeError, PermissionError, RuntimeError) as exc:
        print(
            json.dumps({"status": "not_verified", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
