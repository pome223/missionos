#!/usr/bin/env python3
"""Runtime smoke for Gazebo Sim telemetry log failure handling.

The smoke exercises the task persistence boundary without starting Docker. It
feeds Gazebo Sim-specific bad stdout logs through the same attach helper used
by the real `gz sim` telemetry smoke and verifies that rejected logs do not
mutate the task, create HIL artifacts, or create approval/promotion/reuse
artifacts.
"""

from __future__ import annotations

import json
import sys
from tempfile import TemporaryDirectory

from src.runtime.gz_sim_log_collector import (
    GzSimLogCollectorError,
    attach_gz_sim_failure_diagnostics_artifact,
    attach_gz_sim_log_hil_review_gate_artifacts,
)
from src.runtime.task_store import TaskStore


FAILURE_CASES: dict[str, str] = {
    "no_output": "",
    "missing_server_marker": "\n".join(
        [
            "[Msg] Loading SDF world file[/tmp/empty.sdf].",
            "[Msg] Loaded level [default]",
        ]
    ),
    "missing_world_marker": "\n".join(
        [
            "[Msg] Gazebo Sim Server v8.11.0",
            "[Msg] Loading SDF world file[/tmp/empty.sdf].",
        ]
    ),
    "startup_only": "[Msg] Gazebo Sim Server v8.11.0",
    "world_load_failure": "\n".join(
        [
            "[Msg] Gazebo Sim Server v8.11.0",
            "[Err] Unable to find or load SDF world file[/tmp/missing.sdf].",
        ]
    ),
    "command_like_payload": "\n".join(
        [
            "[Msg] Gazebo Sim Server v8.11.0",
            "[Msg] Loading SDF world file[/tmp/empty.sdf].",
            "[Msg] Loaded level [default]",
            '{"metadata": {"nested": [{"RosTopic": "/cmd_vel"}]}}',
        ]
    ),
}


def _new_task(store: TaskStore, *, case_id: str) -> dict:
    return store.create(
        kind="control_supervisor",
        title=f"Gazebo Sim failure handling smoke: {case_id}",
        status="running",
        artifacts={"existing": {"case_id": case_id, "kept": True}},
    )


def _assert_task_clean(store: TaskStore, task_id: str, *, case_id: str) -> None:
    task = store.get(task_id)
    assert task is not None
    assert task["status"] == "running"
    assert task["artifacts"]["existing"] == {"case_id": case_id, "kept": True}
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
    diagnostics_reasons: dict[str, str] = {}
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        for case_id, log_text in FAILURE_CASES.items():
            task = _new_task(store, case_id=case_id)
            try:
                attach_gz_sim_log_hil_review_gate_artifacts(
                    task["task_id"],
                    log_text,
                    task_store_factory=lambda: store,
                )
            except GzSimLogCollectorError as exc:
                observed[case_id] = str(exc)
                diagnostics = attach_gz_sim_failure_diagnostics_artifact(
                    task["task_id"],
                    log_text,
                    error_message=str(exc),
                    provenance={"case_id": case_id},
                    task_store_factory=lambda: store,
                )
                diagnostics_reasons[case_id] = diagnostics["reason"]
            else:
                raise AssertionError(f"{case_id} unexpectedly persisted artifacts")
            _assert_task_clean(store, task["task_id"], case_id=case_id)
            stored = store.get(task["task_id"])
            assert stored is not None
            diagnostics_artifact = stored["artifacts"]["gz_sim_telemetry_diagnostics"]
            assert diagnostics_artifact["schema_version"] == (
                "gz_sim_telemetry_diagnostics.v1"
            )
            assert diagnostics_artifact["status"] == "invalid_evidence"
            assert diagnostics_artifact["metadata"]["debug_only"] is True
            assert diagnostics_artifact["metadata"]["case_id"] == case_id
            assert diagnostics_artifact["hil_artifacts_persisted"] is False
            assert diagnostics_artifact["gate_artifacts_persisted"] is False
            assert diagnostics_artifact["approval_promotion_reuse_created"] is False
            if case_id == "command_like_payload":
                serialized = json.dumps(
                    diagnostics_artifact,
                    ensure_ascii=True,
                    sort_keys=True,
                )
                assert "/cmd_vel" not in serialized
                assert '"nested"' not in serialized

    summary = {
        "cases": sorted(observed),
        "diagnostics_reasons": diagnostics_reasons,
        "diagnostics_artifacts_persisted": sorted(diagnostics_reasons),
        "no_output_rejected": "no_output" in observed,
        "missing_server_marker_rejected": "missing_server_marker" in observed,
        "missing_world_marker_rejected": "missing_world_marker" in observed,
        "startup_only_rejected": "startup_only" in observed,
        "world_load_failure_rejected": "world_load_failure" in observed,
        "command_like_log_rejected": "command_like_payload" in observed,
        "task_status_preserved": True,
        "existing_artifacts_retained": True,
        "diagnostics_artifacts_are_debug_only": True,
        "command_like_payload_raw_stdout_redacted": True,
        "hil_artifacts_persisted_on_failure": False,
        "approval_promotion_reuse_created": False,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
