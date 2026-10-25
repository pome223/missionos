#!/usr/bin/env python3
"""Real local timing/checker processes; no mission, network or provider access."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_final_timing import MissionWallBudget, verify_completed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-stdin", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.check_stdin:
        value = json.loads(sys.stdin.buffer.read(65537))
        try:
            verify_completed(value, maximum_s=1.)
        except ValueError:
            print(json.dumps({"accepted": False, "new_flights": 0, "model_calls": 0}))
            return 2
        print(json.dumps({"accepted": True, "new_flights": 0, "model_calls": 0}))
        return 0
    if not args.run or args.output is None:
        parser.error("explicit --run and fresh --output are required")
    output = args.output.absolute()
    if output.resolve() != output or output.exists():
        raise ValueError("fresh_timing_smoke_output_required")
    output.mkdir(parents=True)
    child_env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR")}
    results = []
    for case in ("normal", "suspend_like_utc_jump_after_child_exit"):
        offset = [0.]
        budget = MissionWallBudget(1., wall_clock=lambda: time.time()+offset[0])
        child = subprocess.run([sys.executable, "-I", "-B", "-c", "pass"], env=child_env,
            capture_output=True, timeout=5, check=True)
        if case != "normal":
            offset[0] = 2.
        try:
            budget.finish()
            accepted = True
        except ValueError:
            accepted = False
        record = budget.record()
        check = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--check-stdin"],
            input=json.dumps(record).encode(), env=child_env, capture_output=True, timeout=5, check=False)
        independent = json.loads(check.stdout)
        if not (accepted == independent["accepted"] == (case == "normal")
                and check.returncode == (0 if accepted else 2)):
            raise ValueError("timing_checker_disagrees")
        results.append({"case": case, "child_exit_code": child.returncode,
            "accepted": accepted, "separate_checker_exit_code": check.returncode,
            "timing": record})
    receipt = {"schema": "missionos.starship_final_timing_smoke.v1", "passed": True,
        "scenarios": results, "non_mission_child_processes": 4,
        "new_flights": 0, "physics_steps": 0, "model_calls": 0,
        "wall_clock_jump_is_injected_not_actual_OS_sleep": True,
        "source_sha256": {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ("scripts/smoke_starship_final_timing.py", "src/runtime/starship_final_timing.py")}}
    raw = json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False).encode()+b"\n"
    with (output/"receipt.json").open("xb") as stream:
        stream.write(raw)
    print(json.dumps({"passed": True, "receipt_sha256": sha256(raw).hexdigest(),
        "new_flights": 0, "physics_steps": 0, "model_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
