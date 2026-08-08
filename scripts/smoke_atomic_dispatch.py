#!/usr/bin/env python3
"""Prove one bounded sender invocation across concurrent Python processes."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping

from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable


DISPATCH_REF = "dispatch:atomic-process-smoke"
REQUEST_PAYLOAD = {
    "schema_version": "missionos_guarded_dispatch_request.v1",
    "task_id": "task:atomic-process-smoke",
    "approval_ref": "approval:atomic-process-smoke",
    "bounded_action_ref": "bounded:atomic-process-smoke",
    "dispatch_ref": DISPATCH_REF,
    "backend_ref": "fixture:atomic-process-smoke",
}
WORKER_COUNT = 8


def _wait_for(path: Path, *, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {path.name}")
        time.sleep(0.01)


def _worker(root: Path, worker_index: int) -> int:
    ready_dir = root / "ready"
    result_dir = root / "results"
    event_dir = root / "sender-events"
    for directory in (ready_dir, result_dir, event_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (ready_dir / f"{worker_index}.ready").write_text("ready\n", encoding="utf-8")
    _wait_for(root / "start")

    table = DispatchAuthorityTable(root / "dispatch-state.json")
    claim = table.claim_dispatch_ref(
        dispatch_ref=DISPATCH_REF,
        request_payload=REQUEST_PAYLOAD,
        correlation={
            "task_id": REQUEST_PAYLOAD["task_id"],
            "approval_ref": REQUEST_PAYLOAD["approval_ref"],
            "bounded_action_ref": REQUEST_PAYLOAD["bounded_action_ref"],
        },
    )
    sender_invoked = False
    status = str(claim["idempotency_status"])
    if claim["send_permitted"] is True:
        send_start = table.mark_dispatch_send_started(
            dispatch_ref=DISPATCH_REF,
            claim_id=str(claim["claim_id"]),
        )
        status = str(send_start["idempotency_status"])
        if send_start["send_permitted"] is True:
            sender_invoked = True
            (event_dir / f"worker-{worker_index}.json").write_text(
                json.dumps(
                    {"dispatch_ref": DISPATCH_REF, "worker_index": worker_index},
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            recorded = table.record_dispatch_receipt(
                dispatch_ref=DISPATCH_REF,
                claim_id=str(claim["claim_id"]),
                receipt={
                    "dispatch_ref": DISPATCH_REF,
                    "external_sender_invoked": True,
                    "ack_observed": False,
                    "effect_observed": False,
                    "verifier_passed": False,
                    "completion_claimed": False,
                    "physical_execution_invoked": False,
                },
            )
            status = str(recorded["idempotency_status"])

    result = {
        "worker_index": worker_index,
        "idempotency_status": status,
        "sender_invoked": sender_invoked,
        "automatic_redispatch_performed": False,
    }
    (result_dir / f"{worker_index}.json").write_text(
        json.dumps(result, sort_keys=True),
        encoding="utf-8",
    )
    return 0


def _run_parent() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="missionos-atomic-dispatch-") as raw:
        root = Path(raw)
        ready_dir = root / "ready"
        ready_dir.mkdir(parents=True)
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker-root",
                    str(root),
                    "--worker-index",
                    str(index),
                ],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(WORKER_COUNT)
        ]
        deadline = time.monotonic() + 30.0
        while len(list(ready_dir.glob("*.ready"))) != WORKER_COUNT:
            if time.monotonic() >= deadline:
                raise TimeoutError("workers did not reach the concurrency barrier")
            time.sleep(0.01)
        (root / "start").write_text("start\n", encoding="utf-8")
        for process in processes:
            _stdout, stderr = process.communicate(timeout=30)
            if process.returncode != 0:
                raise RuntimeError(f"atomic dispatch worker failed: {stderr.strip()}")

        results = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((root / "results").glob("*.json"))
        ]
        sender_events = sorted((root / "sender-events").glob("*.json"))
        record = DispatchAuthorityTable(root / "dispatch-state.json").lookup_dispatch_ref(
            DISPATCH_REF
        )
        attempts = record.get("attempts")
        attempts = attempts if isinstance(attempts, Mapping) else {}
        statuses = Counter(str(result["idempotency_status"]) for result in results)
        sender_invocations = sum(result["sender_invoked"] is True for result in results)

        if len(results) != WORKER_COUNT:
            raise RuntimeError("atomic dispatch worker result count mismatch")
        if len(sender_events) != 1 or sender_invocations != 1:
            raise RuntimeError("atomic dispatch permitted more than one sender invocation")
        if len(attempts) != 1 or not record.get("receipt"):
            raise RuntimeError("atomic dispatch ledger did not persist one completed attempt")

        return {
            "schema_version": "missionos_atomic_dispatch_smoke.v1",
            "verification_status": "passed",
            "process_count": WORKER_COUNT,
            "lock_scope": "same_host_shared_state_and_lock_files",
            "idempotency_status_counts": dict(sorted(statuses.items())),
            "dispatch_attempt_count": len(attempts),
            "fixture_sender_invocation_count": len(sender_events),
            "external_transport_invoked": False,
            "physical_execution_invoked": False,
            "automatic_redispatch_performed": False,
            "receipt_recorded": bool(record.get("receipt")),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-root", type=Path)
    parser.add_argument("--worker-index", type=int)
    args = parser.parse_args()
    if args.worker_root is not None:
        if args.worker_index is None:
            parser.error("--worker-index is required with --worker-root")
        return _worker(args.worker_root, args.worker_index)
    print(json.dumps(_run_parent(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
