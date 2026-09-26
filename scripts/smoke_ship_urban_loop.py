"""Exercise CLI/process/HTTP urban control and preserve each expected failure."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

EXPECTED = {
    "none": None,
    "offshore_entry": "invalid_or_outside_urban_observation",
    "wrong_phase": "invalid_or_outside_urban_observation",
    "stale_observation": "invalid_or_outside_urban_observation",
    "reused_image": "urban_fresh_image_required",
    "cross_cycle": "urban_stale_or_unbound_response",
    "unsafe_candidate": "urban_candidate_outside_approved_segment",
    "rules_rejection": "urban_independent_rules_rejected",
    "http_failure": "urban_model_http_response_rejected",
    "timeout": "urban_model_timeout",
    "hold_loss": "urban_hold_not_observed",
    "shutdown_failure": "urban_model_shutdown_not_verified",
    "arrival_failure": "urban_target_or_stable_hold_not_observed",
    "tracking_deviation": "urban_segment_tracking_violated",
    "offshore_drift": "invalid_or_outside_urban_observation",
    "reserve_exhausted": "invalid_or_outside_urban_observation",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    project = Path(__file__).resolve().parents[1]
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(
            str(project / p)
            for p in (
                "packages/missionos-core/src",
                "packages/missionos-cli/src",
                ".",
            )
        ),
    )
    command = [
        sys.executable,
        "-c",
        "from missionos_cli.cli import missionos; missionos()",
        "ship-delivery",
        "urban-loop-smoke",
    ]
    paths = [
        *sorted((project / "src/runtime").glob("ship_urban_loop*.py")),
        project / "src/runtime/ship_urban_model_processes.py",
        project / "scripts/ship_urban_loop_fixture_worker.py",
        Path(__file__),
        project / "packages/missionos-cli/src/missionos_cli/ship_delivery_command.py",
    ]
    protocol = {
        "schema_version": "ship_urban_loop_smoke_protocol.v1",
        "expected": EXPECTED,
        "retry_allowed": False,
        "gpu_spend_usd": 0,
        "source_sha256": {
            str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths
        },
    }
    (root / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    results = []
    denied = subprocess.run(
        command + ["--output-dir", str(root / "unapproved")],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    results.append(
        {
            "case": "unapproved",
            "expected": denied.returncode == 1 and not (root / "unapproved").exists(),
        }
    )
    for case, failure in EXPECTED.items():
        out = root / case
        cmd = command + ["--approve-fixture", "--fault", case, "--output-dir", str(out)]
        run = subprocess.run(cmd, cwd=project, env=env, capture_output=True, text=True, timeout=30)
        (root / f"{case}.stdout").write_text(run.stdout)
        (root / f"{case}.stderr").write_text(run.stderr)
        receipt = json.loads((out / "result.json").read_text())
        verification = json.loads((out / "verification.json").read_text())
        okay = (
            run.returncode == (0 if failure is None else 1)
            and receipt["owned_processes_reaped"] is True
            and receipt["fixture_ap_return_handoff"] == (failure is None)
            and verification["control_sequence_verified"] == (failure is None)
            and (receipt["failure"] is None if failure is None else failure in receipt["failure"])
        )
        results.append(
            {
                "case": case,
                "expected": okay,
                "returncode": run.returncode,
                "completed_updates": receipt["completed_updates"],
                "failure": receipt["failure"],
                "processes_reaped": receipt["owned_processes_reaped"],
                "late_response_rejected": receipt["late_response_rejected"],
                "receipt_sha256": hashlib.sha256((out / "result.json").read_bytes()).hexdigest(),
            }
        )
        print(json.dumps(results[-1]), flush=True)
    report = {
        "schema_version": "ship_urban_loop_smoke.v1",
        "cases": results,
        "all_expected": all(r["expected"] for r in results),
        "native_model_calls": 0,
        "new_simulator_flights": 0,
        "gpu_spend_usd": 0,
    }
    (root / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["all_expected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
