#!/usr/bin/env python3
"""Runtime smoke for PX4/Gazebo-compatible log collector failure handling."""

from __future__ import annotations

import json
from tempfile import TemporaryDirectory

from src.runtime.px4_gazebo_log_collector import (
    PX4_GAZEBO_TELEMETRY_LOG_PREFIX,
    Px4GazeboLogCollectorError,
    attach_px4_gazebo_log_smoke_artifacts,
)
from src.runtime.task_store import TaskStore


def _command_like_logs() -> str:
    return PX4_GAZEBO_TELEMETRY_LOG_PREFIX + json.dumps(
        {
            "sample_id": "command-like-log-smoke",
            "source": {
                "source_kind": "px4_gazebo_compatible_log_source",
                "source_id": "bad-log-source",
                "vehicle_id": "iris-bad",
            },
            "captured_at": "2026-04-30T16:00:00+00:00",
            "telemetry": {"altitude_m": 1.0},
            "metadata": {"nested": [{"RosTopic": "/cmd_vel"}]},
        }
    )


def _assert_reject_preserves_task(store: TaskStore, task_id: str, logs: str) -> str:
    try:
        attach_px4_gazebo_log_smoke_artifacts(
            task_id,
            logs,
            task_store_factory=lambda: store,
        )
    except Px4GazeboLogCollectorError as exc:
        error_message = str(exc)
    else:  # pragma: no cover - fail path
        raise AssertionError("invalid PX4/Gazebo logs should fail closed")

    stored = store.get(task_id)
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"] == {"existing": {"kept": True}}
    return error_message


def main() -> int:
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        no_output_task = store.create(
            kind="control_supervisor",
            title="PX4/Gazebo no telemetry log smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        malformed_task = store.create(
            kind="control_supervisor",
            title="PX4/Gazebo malformed telemetry log smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        command_like_task = store.create(
            kind="control_supervisor",
            title="PX4/Gazebo command-like telemetry log smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )

        no_output_error = _assert_reject_preserves_task(
            store,
            no_output_task["task_id"],
            "container | started without telemetry",
        )
        malformed_error = _assert_reject_preserves_task(
            store,
            malformed_task["task_id"],
            PX4_GAZEBO_TELEMETRY_LOG_PREFIX + "{not-json",
        )
        command_like_error = _assert_reject_preserves_task(
            store,
            command_like_task["task_id"],
            _command_like_logs(),
        )

    print(
        json.dumps(
            {
                "no_output_rejected": "not found" in no_output_error,
                "malformed_log_rejected": "invalid JSON" in malformed_error,
                "command_like_log_rejected": "RosTopic" in command_like_error,
                "task_status_preserved": True,
                "existing_artifacts_retained": True,
                "hil_artifacts_persisted_on_failure": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
