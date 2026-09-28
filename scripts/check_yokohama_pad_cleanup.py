#!/usr/bin/env python3
"""Opt-in runtime check: a supervisor-close error must not skip container cleanup."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--approve-sitl", action="store_true")
    p.add_argument("--output-dir", type=Path, required=True)
    a = p.parse_args()
    if not a.approve_sitl:
        p.error("Explicit --approve-sitl required")
    from src.runtime.yokohama_pad_queue import PadSupervisor
    from scripts import yokohama_sitl

    old_close = PadSupervisor.close

    def fault(self):
        old_close(self)
        raise RuntimeError("injected supervisor close failure after thread termination")

    old_argv = sys.argv
    try:
        PadSupervisor.close = fault
        sys.argv = [
            "yokohama_sitl.py",
            "--phase",
            "flight",
            "--approve-sitl",
            "--sea-round-trip",
            "--deliver-payload",
            "--occupied-pad",
            "--timeout-seconds",
            "1",
            "--output-dir",
            str(a.output_dir),
        ]
        code = yokohama_sitl.main()
    finally:
        PadSupervisor.close = old_close
        sys.argv = old_argv
    result = json.loads((a.output_dir / "result.json").read_text())
    checks = dict(
        rejected=code == 1 and result["status"] == "failed",
        injected_error_observed=result.get("pad_supervisor_error")
        == "injected supervisor close failure after thread termination",
        payload_receiver_stopped=result.get("payload_receiver_stopped") is True,
        owned_container_removed=result.get("cleanup") is True,
        no_gpu_or_models=all(
            result.get(k) is False for k in ("gpu_requested", "vla_invoked", "wam_invoked")
        ),
    )
    receipt = dict(
        status="passed" if all(checks.values()) else "failed",
        checks=checks,
        run_id=result["run_id"],
        scope="Expected timeout and injected cleanup error; not a delivery flight",
        fault_injection_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    )
    (a.output_dir / "cleanup-verification.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
