#!/usr/bin/env python3
"""Reopen cargo poses, pad contacts and receipt before accepting a simulated return."""

import argparse
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.runtime.yokohama_payload import (  # noqa: E402
    assess_delivery,
    digest,
    make_receipt,
    read_jsonl,
    require_receipt,
)


def verify(root):
    def read(name):
        return json.loads((root / name).read_text())

    config, request, receipt = (
        read(n) for n in ["config.json", "payload-request.json", "payload-receipt.json"]
    )
    rows = read_jsonl(root / "flight-trajectory.jsonl")
    events = read_jsonl(root / "flight-events.jsonl")
    end = receipt["assessment"]["observed_through_wall_s"]
    proof = assess_delivery(
        config,
        request,
        [r for r in rows if r["wall_s"] <= end],
        read_jsonl(root / "sensor-events.jsonl"),
        read_jsonl(root / "payload-joint-events.jsonl"),
    )
    checks = dict(recomputed_delivery=proof["verified"], receipt_exactly_recomputed=False)
    if proof["verified"]:
        checks["receipt_exactly_recomputed"] = make_receipt(config, request, proof) == receipt
    received = [e for e in events if e["event"] == "payload_received"]
    requested = [e for e in events if e["event"] == "payload_release_requested"]
    commands = [e for e in events if e["event"] == "payload_detach_command"]
    authorized = [e for e in events if e["event"] == "payload_return_authorized"]
    uploads = [
        e for e in events if e["event"] == "upload_receipt" and e["segment"] == "PAYLOAD-CLIMB"
    ]
    checks["one_release_and_receipt"] = (
        len(requested) == len(received) == len(authorized) == len(uploads) == 1
    )
    checks["bounded_detach_commands"] = 1 <= len(commands) <= 3
    checks["return_after_receipt"] = False
    checks["receipt_fresh_at_acceptance"] = False
    if checks["one_release_and_receipt"] and commands:
        checks["return_after_receipt"] = (
            requested[0]["request_sha256"] == digest(request)
            and requested[0]["wall_s"]
            < commands[0]["wall_s"]
            <= commands[-1]["wall_s"]
            < received[0]["wall_s"]
            < authorized[0]["wall_s"]
            < uploads[0]["wall_s"]
            and received[0]["receipt"] == receipt
            and received[0]["receipt_sha256"] == digest(receipt)
            and authorized[0]["receipt_id"] == receipt["receipt_id"]
        )
        try:
            require_receipt(config, request, receipt, received[0]["observation"])
            checks["receipt_fresh_at_acceptance"] = True
        except ValueError:
            pass
    checks["models_stopped_before_release"] = not config.get("decisions") or (
        len(
            [
                e
                for e in events
                if e["event"] == "city_session_revoked"
                and requested
                and e["wall_s"] < requested[0]["wall_s"]
            ]
        )
        == 1
    )
    checks["worker_claim_matches_receipt"] = (
        read("worker-result.json").get("payload_receipt_id") == receipt["receipt_id"]
    )
    result = read("result.json")
    checks["owned_runtime_finished_and_cleaned"] = (
        result["status"] == "passed"
        and result.get("cleanup") is True
        and result.get("payload_receiver_stopped") is True
    )
    final = rows[-1]
    checks["return_landed_disarmed"] = (
        final["phase"] == "return_land" and final["landed"] is True and final["arming_state"] == 1
    )
    # Geometry, home position, fresh deck contact and model lifecycle are also
    # recomputed by the existing flight/decision verifiers; do not replace them.
    return dict(
        status="passed" if all(checks.values()) else "failed",
        run_id=config["run_id"],
        checks=checks,
        receipt_id=receipt["receipt_id"],
        physical_receipt_verified=False,
        evidence_hashes={
            name: digest(read_jsonl(root / name))
            for name in [
                "flight-trajectory.jsonl",
                "sensor-events.jsonl",
                "payload-joint-events.jsonl",
            ]
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.run_dir)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps(result))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
