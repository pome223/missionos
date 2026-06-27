#!/usr/bin/env python3
"""Runtime smoke for PX4 SIH telemetry log failure handling.

The smoke exercises the task persistence boundary without starting Docker. It
feeds PX4 SIH-specific bad stdout logs through the same attach helper used by
the real PX4 SIH telemetry smoke and verifies that rejected logs do not mutate
the task, create HIL artifacts, or create approval/promotion/reuse artifacts.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
from tempfile import TemporaryDirectory

from src.runtime.px4_sitl_log_collector import (
    Px4SitlLogCollectorError,
    attach_px4_sitl_log_hil_review_gate_artifacts,
)
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 4, 30, 17, 0, tzinfo=timezone.utc)

FAILURE_CASES: dict[str, str] = {
    "no_output": "",
    "prompt_only": "pxh>\npxh>\npxh>",
    "rootfs_failure": (
        "ERROR [px4] Error creating directory: "
        "/root/.local/share/px4/rootfs (Read-only file system)"
    ),
    "partial_startup": "\n".join(
        [
            "INFO  [px4] startup script: /bin/sh etc/init.d-posix/rcS 0",
            "INFO  [init] found model autostart file as SYS_AUTOSTART=10040",
            "INFO  [init] SIH simulator",
        ]
    ),
    "unknown_model": "ERROR [init] Unknown model sihsim_missing",
}


def _new_task(store: TaskStore, *, case_id: str) -> dict:
    return store.create(
        kind="control_supervisor",
        title=f"PX4 SIH failure handling smoke: {case_id}",
        status="running",
        artifacts={"existing": {"case_id": case_id, "kept": True}},
    )


def _assert_task_clean(store: TaskStore, task_id: str, *, case_id: str) -> None:
    task = store.get(task_id)
    assert task is not None
    assert task["status"] == "running"
    assert task["artifacts"] == {"existing": {"case_id": case_id, "kept": True}}
    assert "px4_gazebo_sanitized_telemetry" not in task["artifacts"]
    assert "hil_telemetry_envelope" not in task["artifacts"]
    assert "hil_telemetry_evidence" not in task["artifacts"]
    assert "hil_telemetry_review" not in task["artifacts"]
    assert "autonomy_gate_result" not in task["artifacts"]
    assert "approval" not in task["artifacts"]
    assert "promotion_package" not in task["artifacts"]
    assert "reuse_plan" not in task["artifacts"]


def main() -> int:
    observed: dict[str, str] = {}
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        for case_id, log_text in FAILURE_CASES.items():
            task = _new_task(store, case_id=case_id)
            try:
                attach_px4_sitl_log_hil_review_gate_artifacts(
                    task["task_id"],
                    log_text,
                    captured_at=NOW,
                    task_store_factory=lambda: store,
                )
            except Px4SitlLogCollectorError as exc:
                observed[case_id] = str(exc)
            else:
                raise AssertionError(f"{case_id} unexpectedly persisted artifacts")
            _assert_task_clean(store, task["task_id"], case_id=case_id)

    summary = {
        "cases": sorted(observed),
        "no_output_rejected": "no_output" in observed,
        "prompt_only_rejected": "prompt_only" in observed,
        "rootfs_failure_rejected": "rootfs_failure" in observed,
        "partial_startup_rejected": "partial_startup" in observed,
        "unknown_model_rejected": "unknown_model" in observed,
        "task_status_preserved": True,
        "existing_artifacts_retained": True,
        "hil_artifacts_persisted_on_failure": False,
        "approval_promotion_reuse_created": False,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
