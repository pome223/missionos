#!/usr/bin/env python3
"""Run the opt-in synthetic dispenser comparison or verify a saved result."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.runtime.starship_dispenser_experiment import run_dispenser_experiment  # noqa: E402
from src.runtime.starship_dispenser_report import build_report  # noqa: E402
from src.runtime.starship_dispenser_verifier import verify_dispenser_experiment  # noqa: E402


def _write(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as target:
        json.dump(value, target, ensure_ascii=False, indent=2, allow_nan=False)
        target.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output-dir", type=Path, help="New directory; existing results are preserved")
    mode.add_argument("--verify", type=Path, help="Verify saved study.json without rerunning policies")
    parser.add_argument("--approve-synthetic", action="store_true",
                        help="Opt in to the local synthetic fixture, not flight or hardware approval")
    args = parser.parse_args()
    try:
        if args.verify:
            result = verify_dispenser_experiment(json.loads(args.verify.read_text()))
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0 if result["verified"] else 1
        if not args.approve_synthetic:
            parser.error("--output-dir requires --approve-synthetic; no experiment was started")
        args.output_dir.mkdir(parents=True, exist_ok=False)
        started = perf_counter()
        study = run_dispenser_experiment(approved=True)
        elapsed = perf_counter() - started
        paths = [
            "src/runtime/starship_dispenser_experiment.py",
            "src/runtime/starship_dispenser_verifier.py",
            "src/runtime/starship_dispenser_report.py",
            "scripts/run_starship_dispenser_experiment.py",
            "docs/agents/starship-dispenser-experiment.md",
        ]
        try:
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                text=True, capture_output=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            revision = None
        study["provenance"] = {
            "schema": "missionos.synthetic_dispenser_provenance.v1",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "experiment_wall_time_s": elapsed,
            "code_revision": revision,
            "source_sha256": {
                name: sha256((ROOT / name).read_bytes()).hexdigest()
                for name in paths if (ROOT / name).is_file()
            },
            "backend": "local_in_process_synthetic_fixture",
            "source_hashes_bind_working_files": True,
            "llm_invoked": False,
            "physical_execution": False,
        }
        _write(args.output_dir / "study.json", study)
        # Check the serialization boundary, not just the in-memory result.
        verification = verify_dispenser_experiment(
            json.loads((args.output_dir / "study.json").read_text())
        )
        _write(args.output_dir / "verification.json", verification)
        with (args.output_dir / "report.html").open("x", encoding="utf-8") as target:
            target.write(build_report(study, verification))
        _write(args.output_dir / "manifest.json", {
            "schema": "missionos.synthetic_dispenser_manifest.v1",
            "files": {
                name: sha256((args.output_dir / name).read_bytes()).hexdigest()
                for name in ("study.json", "verification.json", "report.html")
            },
        })
        print(json.dumps({
            "status": "verified" if verification["verified"] else "verification_failed",
            "world_count": len(study["worlds"]),
            "policy_run_count": len(study["policy_runs"]),
            "summary": study["summary"],
            "model_bounds": study["model_bounds"],
            "wall_time_s": elapsed,
        }, ensure_ascii=False, allow_nan=False))
        return 0 if verification["verified"] else 1
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
