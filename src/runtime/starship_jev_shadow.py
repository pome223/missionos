"""Host-side Jev observer, separated from a credential-free simulation process.

The simulator emits public observations over a one-way pipe. No model response
can reach its input. Provider latency is wall time, never synthetic flight time.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import signal
import subprocess
import sys
from threading import Thread
import time

ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATHS = (
    "src/runtime/starship_jev_shadow.py", "src/intelligence/starship_jev_router.py",
    "src/runtime/starship_jev_shadow_verifier.py", "src/runtime/starship_jev_shadow_report.py",
    "src/runtime/starship_dispenser_experiment.py", "src/runtime/starship_dispenser_verifier.py",
    "src/runtime/starship_dispenser_report.py", "src/runtime/starship_mission_control.py",
    "scripts/run_starship_jev_shadow_worker.py", "scripts/run_starship_jev_shadow.py",
    "scripts/run_starship_mission_worker.py",
)
MAX_CALLS = 22
TRIGGER = "first_decision_after_observed_retry_failure"


def source_hashes() -> dict:
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_PATHS}


def digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


def write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def run_shadow(output_dir: Path, *, mode: str, router=None) -> dict:
    """Run 60 paired local conditions; observe at most one eligible fault per case."""
    from src.intelligence.starship_jev_router import StarshipJevRouter
    from .starship_mission_control import worker_environment
    from .starship_jev_shadow_verifier import verify_shadow_bundle
    from .starship_jev_shadow_report import build_report

    if mode not in {"fixture", "live"}:
        raise ValueError("jev_shadow_mode_required")
    if mode == "live" and (os.environ.get("MISSIONOS_STARSHIP_JEV_SHADOW_MODE") != "live"
                            or not os.environ.get("TYPESAFE_API_KEY", "").strip()):
        raise ValueError("jev_shadow_live_configuration_required")
    if router is not None and (mode != "fixture" or getattr(router, "mode", None) != "fixture"
                              or type(getattr(router, "max_calls", None)) is not int
                              or not 0 < router.max_calls <= MAX_CALLS):
        raise ValueError("jev_shadow_injected_router_rejected")
    router = router or StarshipJevRouter(mode=mode, max_calls=MAX_CALLS)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    sources = source_hashes()
    messages: Queue = Queue()
    started = time.monotonic()
    worker_dir = output_dir / "simulator"
    with (output_dir / "simulator.log").open("x") as log:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts/run_starship_jev_shadow_worker.py"),
             "--approve-synthetic", "--output-dir", str(worker_dir)],
            cwd=ROOT, env=worker_environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=log, start_new_session=True,
        )

    def receive():
        try:
            for _ in range(MAX_CALLS + 2):
                line = process.stdout.readline(16385)
                if not line:
                    messages.put(("eof", None))
                    return
                if len(line) > 16384 or not line.endswith(b"\n"):
                    raise ValueError("invalid_worker_frame")
                messages.put(("frame", json.loads(line)))
            raise ValueError("too_many_worker_frames")
        except Exception:
            messages.put(("error", None))

    reader = Thread(target=receive, daemon=True)
    reader.start()
    records = []
    seen_cases = set()
    complete = None
    try:
        while True:
            remaining = 420 - (time.monotonic() - started)
            if remaining <= 0:
                raise ValueError("jev_shadow_wall_timeout")
            try:
                kind, frame = messages.get(timeout=min(60, remaining))
            except Empty:
                raise ValueError("jev_shadow_worker_timeout") from None
            if kind == "eof":
                break
            if kind != "frame" or not isinstance(frame, dict):
                raise ValueError("jev_shadow_worker_protocol_failed")
            if frame.get("kind") == "complete":
                if complete is not None:
                    raise ValueError("jev_shadow_duplicate_completion")
                complete = frame
                continue
            if (complete is not None or frame.get("kind") != "fault_observation"
                    or set(frame) != {"kind", "case_ref", "step_index", "public_history", "public_budget"}
                    or len(records) >= MAX_CALLS
                    or type(frame["case_ref"]) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", frame["case_ref"]) is None
                    or frame["case_ref"] in seen_cases
                    or type(frame["step_index"]) is not int or not 0 <= frame["step_index"] < 12):
                raise ValueError("jev_shadow_invalid_observation")
            seen_cases.add(frame["case_ref"])
            with (output_dir / "routing-attempt-intents.jsonl").open("a") as stream:
                stream.write(json.dumps({"record_index": len(records), "mode": mode,
                                         "frame_sha256": digest(frame), "network_attempt_confirmed": False}) + "\n")
            # Correlation identifiers, incumbent action and future truth never enter the router.
            routing = router.route(frame["public_history"], frame["public_budget"])
            record = {"frame": frame, "frame_sha256": digest(frame), "routing": routing}
            records.append(record)
            # Keep partial, sanitized receipts if interrupted. No retry or resume is automatic.
            with (output_dir / "routing-receipts.jsonl").open("a") as stream:
                stream.write(json.dumps(record, allow_nan=False) + "\n")
        returncode = process.wait(timeout=5)
        if returncode != 0 or complete is None or complete.get("worker_pid") != process.pid:
            raise ValueError("jev_shadow_worker_incomplete")
        if sources != source_hashes():
            raise ValueError("jev_shadow_sources_changed")
        bundle = {
            "schema": "missionos.starship_jev_shadow_bundle.v1",
            "baseline": json.loads((worker_dir / "study.json").read_text()),
            "execution_runs": json.loads((worker_dir / "execution-runs.json").read_text()),
            "shadow": {
                "mode": mode, "trigger": TRIGGER, "maximum_calls": MAX_CALLS,
                "records": records, "worker": complete, "worker_exit_code": returncode,
                "source_sha256": sources, "physical_execution": False,
                "used_for_decision": False, "real_time_guarantee": False,
            },
        }
        write_json(output_dir / "study.json", bundle)
        verdict = verify_shadow_bundle(json.loads((output_dir / "study.json").read_text()))
        write_json(output_dir / "verification.json", verdict)
        (output_dir / "report.html").write_text(build_report(bundle, verdict), encoding="utf-8")
        write_json(output_dir / "manifest.json", {
            "schema": "missionos.starship_jev_shadow_manifest.v1",
            "files": {name: sha256((output_dir / name).read_bytes()).hexdigest()
                      for name in ("study.json", "verification.json", "report.html")},
        })
        return verdict
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        reader.join(timeout=2)
        if process.stdout:
            process.stdout.close()
