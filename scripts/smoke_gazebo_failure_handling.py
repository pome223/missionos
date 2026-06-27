#!/usr/bin/env python3
"""Runtime smoke for Gazebo telemetry log failure handling.

The smoke exercises the task persistence boundary without starting Docker. It
feeds Gazebo-specific bad stdout logs through the same attach helper used by
the Gazebo telemetry smoke and verifies that rejected logs do not mutate the
task, create HIL artifacts, or create approval/promotion/reuse artifacts.
"""

from __future__ import annotations

import json
import sys
from tempfile import TemporaryDirectory

from src.runtime.gazebo_log_collector import (
    GAZEBO_TELEMETRY_STARTUP_MARKER,
    GazeboLogCollectorError,
    attach_gazebo_log_smoke_artifacts,
)
from src.runtime.px4_gazebo_log_collector import PX4_GAZEBO_TELEMETRY_LOG_PREFIX
from src.runtime.task_store import TaskStore
from src.simulators.gazebo_compatible_log_source import _telemetry_sample


def _command_like_logs() -> str:
    sample = _telemetry_sample(tick=1)
    sample["metadata"]["nested"] = [{"RosTopic": "/cmd_vel"}]
    return "\n".join(
        [
            GAZEBO_TELEMETRY_STARTUP_MARKER,
            PX4_GAZEBO_TELEMETRY_LOG_PREFIX + json.dumps(sample, sort_keys=True),
        ]
    )


FAILURE_CASES: dict[str, str] = {
    "no_output": "",
    "missing_startup_marker": "PX4_GAZEBO_TELEMETRY {}",
    "startup_only": GAZEBO_TELEMETRY_STARTUP_MARKER,
    "malformed_log": (
        GAZEBO_TELEMETRY_STARTUP_MARKER
        + "\n"
        + PX4_GAZEBO_TELEMETRY_LOG_PREFIX
        + "{not-json"
    ),
    "command_like_payload": _command_like_logs(),
}


def _new_task(store: TaskStore, *, case_id: str) -> dict:
    return store.create(
        kind="control_supervisor",
        title=f"Gazebo failure handling smoke: {case_id}",
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
                attach_gazebo_log_smoke_artifacts(
                    task["task_id"],
                    log_text,
                    task_store_factory=lambda: store,
                )
            except GazeboLogCollectorError as exc:
                observed[case_id] = str(exc)
            else:
                raise AssertionError(f"{case_id} unexpectedly persisted artifacts")
            _assert_task_clean(store, task["task_id"], case_id=case_id)

    summary = {
        "cases": sorted(observed),
        "no_output_rejected": "no_output" in observed,
        "missing_startup_marker_rejected": "missing_startup_marker" in observed,
        "startup_only_rejected": "startup_only" in observed,
        "malformed_log_rejected": "malformed_log" in observed,
        "command_like_log_rejected": "command_like_payload" in observed,
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
