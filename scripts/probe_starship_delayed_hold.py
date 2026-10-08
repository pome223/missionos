"""Opt-in fixture probe of a hold that actually delays payload releases.

Uses the existing worker/mailbox contract. The host withholds a fixture response
for 20 wall seconds while the worker continues its paced simulation clock.
No inference, new authority, guidance change or tolerance change is involved.
"""
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
from src.intelligence.starship_mission_director import fixture_decision  # noqa: E402
from src.runtime.starship_mission_director import publish, source_hashes  # noqa: E402
from src.runtime.starship_return_feasibility import readiness  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.approve_simulation or args.output_dir.exists():
        parser.error("Explicit simulation opt-in and a fresh output directory are required")
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    _, _, reason = readiness(profile)
    if reason:
        parser.error(reason)
    out = args.output_dir.resolve()
    out.mkdir(parents=True, mode=0o700)
    mailbox = out/"mailbox"
    mailbox.mkdir(mode=0o700)
    before = source_hashes(ROOT)
    probe_hash = sha256(Path(__file__).read_bytes()).hexdigest()
    command = [sys.executable, str(ROOT/"scripts/run_starship_managed_mission.py"),
        "--approve-simulation", "--case", "normal", "--splashdown", "--director-mode", "fixture",
        "--response-fault", "hold_deployment_monitor", "--run-id", "delayed-hold-probe",
        "--mailbox", str(mailbox), "--output-dir", str(out/"flight")]
    environment = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT") if k in os.environ}
    events, seen, pending = [], set(), None
    timed_out = False
    with (out/"worker.log").open("w") as log:
        child = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic()+900.
        try:
            while child.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    child.terminate()
                    break
                path = mailbox/"request.json"
                if path.is_file():
                    request = json.loads(path.read_text())
                    if request["request_id"] not in seen:
                        seen.add(request["request_id"])
                        delay = 20. if request["point"] == "deployment_reassessment" else 0.
                        pending = (request, time.monotonic(), delay)
                if pending is not None and time.monotonic()-pending[1] >= pending[2]:
                    request, started, delay = pending
                    publish(mailbox/"response.json", fixture_decision(request))
                    events.append({"point": request["point"], "observation_time_s": request["observation"]["time_s"],
                        "requested_wall_delay_s": delay, "actual_wall_delay_s": time.monotonic()-started})
                    pending = None
                time.sleep(.02)
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
            child.wait()
            publish(out/"broker-receipt.json", {"exit_code": child.returncode, "timed_out": timed_out,
                "responses": events, "model_inference_invoked": False, "automatic_retry": False,
                "probe_source_sha256": probe_hash, "source_unchanged": before == source_hashes(ROOT)})
    print(json.dumps({"exit_code": child.returncode, "timed_out": timed_out, "responses": len(events),
                      "source_unchanged": before == source_hashes(ROOT)}))
    return 0 if child.returncode == 0 and not timed_out and before == source_hashes(ROOT) else 2


if __name__ == "__main__":
    raise SystemExit(main())
