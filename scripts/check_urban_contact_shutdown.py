#!/usr/bin/env python3
"""Opt-in contact-probe process checks; never starts the aircraft controller."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.px4_urban_wam_trial import (  # noqa: E402
    NAME,
    call,
    cleanup,
    require_opt_in,
    setup,
)


def check(root, assets, image, instruction_ref, attempts):
    require_opt_in()
    if not 1 <= attempts <= 30:
        raise ValueError("choose 1..30 positive checks; no automatic retry")
    if root.exists():
        raise ValueError("refusing to reuse a previous diagnostic")
    rows = []
    summary = {
        "schema_version": "urban_contact_shutdown_check.v1",
        "aircraft_controller_started": False,
        "model_calls": 0,
        "rented_gpu_instances": 0,
        "probe_sha256": hashlib.sha256(
            (ROOT / "scripts/urban_headroom_contact_probe.py").read_bytes()
        ).hexdigest(),
        "checks": rows,
        "passed": False,
    }
    session = root / "session"

    def aircraft_status(label):
        result = call(
            [
                "docker",
                "exec",
                NAME,
                "/opt/px4-gazebo/bin/px4-commander",
                "status",
            ]
        )
        (root / f"commander-{label}.txt").write_text(result.stdout + result.stderr)
        return result.stdout

    def run_probe(index, negative=False):
        began = time.monotonic()
        process = subprocess.run(
            [
                "docker",
                "exec",
                "-e",
                "PYTHONFAULTHANDLER=1",
                NAME,
                "python3",
                "/session/scripts/urban_headroom_contact_probe.py",
            ],
            capture_output=True,
            text=True,
            timeout=65,
        )
        (root / f"probe-{index}.stdout").write_text(process.stdout)
        (root / f"probe-{index}.stderr").write_text(process.stderr)
        path = session / "contact-positive-control.json"
        receipt = json.loads(path.read_text()) if path.exists() else None
        row = {
            "index": index,
            "kind": "missing_contact_topic" if negative else "positive_contact",
            "exit_code": process.returncode,
            "wall_seconds": time.monotonic() - began,
            "receipt": receipt,
        }
        rows.append(row)
        (root / f"probe-{index}.json").write_text(json.dumps(row, indent=2) + "\n")
        print(json.dumps(row), flush=True)
        if negative:
            if (
                process.returncode != 1
                or receipt is not None
                or not process.stderr.rstrip().endswith(
                    "RuntimeError: no positive building contact observed"
                )
            ):
                raise RuntimeError("negative contact control did not fail as expected")
        elif (
            process.returncode != 0
            or receipt is None
            or not (
                receipt["probe_contacts"] > 0
                and receipt["probe_pose_seen"]
                and receipt["probe_removed_observed"]
                and receipt["subscriptions_released"]
                and receipt["building_sensor_positive_control"]
                and receipt["aircraft_commands_sent"] is False
                and receipt["model_invoked"] is False
            )
        ):
            raise RuntimeError("contact probe did not complete normally")

    try:
        setup(root, assets, "gap", None, instruction_ref, image)
        summary["image_id"] = json.loads((root / "container.json").read_text())[
            "image_id"
        ]
        aircraft_status("before")
        for index in range(attempts):
            run_probe(index)
        # Preserve a previous success receipt deliberately: main must invalidate it.
        path = session / "scene.json"
        original = path.read_text()
        scene = json.loads(original)
        scene["buildings"][0]["name"] = "diagnostic_missing_contact_topic"
        path.write_text(json.dumps(scene))
        try:
            run_probe(attempts, negative=True)
        finally:
            path.write_text(original)
        aircraft_status("after")
        if any(
            (session / name).exists()
            for name in (
                "events.jsonl",
                "status.json",
                "flight-result.json",
            )
        ):
            raise RuntimeError("unexpected aircraft controller artifact")
        summary["passed"] = True
    finally:
        cleanup(root)
        if root.exists():
            summary["cleanup"] = (
                json.loads((root / "cleanup.json").read_text())
                if (root / "cleanup.json").exists()
                else None
            )
            (root / "shutdown-check.json").write_text(
                json.dumps(summary, indent=2) + "\n"
            )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument(
        "--image", required=True, help="already installed, pinned image ID"
    )
    parser.add_argument("--approved-instruction-ref", required=True)
    parser.add_argument("--attempts", type=int, default=20)
    args = parser.parse_args()
    check(
        args.output_dir.resolve(),
        args.assets_dir.resolve(),
        args.image,
        args.approved_instruction_ref,
        args.attempts,
    )


if __name__ == "__main__":
    main()
