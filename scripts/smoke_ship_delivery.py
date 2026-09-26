"""Exercise the real packaged CLI for the stationary-ship fixture boundary."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output = args.output_dir or Path(tempfile.mkdtemp(prefix="missionos-ship-smoke-"))
    output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        str(p)
        for p in (
            root / "packages/missionos-core/src",
            root / "packages/missionos-cli/src",
            root,
        )
    )
    cases = (
        ("plan", "plan", {}, False, 0),
        ("clear", "run", {}, True, 0),
        ("wait", "run", {"urban_blockage_s": 10}, True, 0),
        ("detour", "run", {"urban_blockage_s": 120}, True, 0),
        ("unapproved", "run", {}, False, 1),
        ("reserve", "run", {"battery_wh": 45, "reserve_wh": 40}, True, 1),
        ("perception", "run", {"urban_perception_ready": False}, True, 1),
        ("px4-plan", "px4-plan", {}, False, 0),
    )
    summary = []
    for name, command, scenario, approved, expected_exit in cases:
        scenario_path = output / f"{name}-scenario.json"
        scenario_path.write_text(json.dumps(scenario) + "\n", encoding="utf-8")
        report_path = output / f"{name}-report.json"
        argv = [
            sys.executable,
            "-m",
            "missionos_cli",
            "ship-delivery",
            command,
            "--scenario",
            str(scenario_path),
            "--output",
            str(report_path),
        ]
        if approved:
            argv.append("--approve-fixture")
        result = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, timeout=30)
        if result.returncode != expected_exit:
            raise RuntimeError(f"{name}: exit={result.returncode}; {result.stderr}")
        report = json.loads(result.stdout)
        if report != json.loads(report_path.read_text(encoding="utf-8")):
            raise RuntimeError(f"{name}: saved/stdout report mismatch")
        if command == "run":
            completed = expected_exit == 0
            if report["fixture_mission_completed"] is not completed:
                raise RuntimeError(f"{name}: unexpected fixture outcome")
            if completed and not (report["delivery_verified"] and report["recovery_verified"]):
                raise RuntimeError(f"{name}: missing independent delivery/recovery verification")
            if not completed and report["trajectory"]:
                raise RuntimeError(f"{name}: preflight/approval denial still executed")
        if any(report[key] for key in ("physical_execution_invoked", "vla_invoked", "wam_invoked")):
            raise RuntimeError(f"{name}: execution scope promoted")
        summary.append(
            {
                "case": name,
                "exit_code": result.returncode,
                "report": str(report_path),
                "status": report.get("status", "planned"),
                "elapsed_fixture_s": report.get("elapsed_s"),
                "decision": report.get("decisions"),
            }
        )
    material = {"status": "passed", "cases": summary, "scope": "CLI and kinematic fixture only"}
    (output / "summary.json").write_text(json.dumps(material, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(material, indent=2))


if __name__ == "__main__":
    main()
