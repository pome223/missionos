from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from src.runtime.px4_gazebo_route.payload_recovery_flow import (
    PayloadRecoveryFlowInputs,
    PayloadRecoveryRealismRefresh,
    PayloadRecoveryRuntime,
    run_payload_recovery_flow,
)
from src.runtime.task_store import TaskStore


@dataclass(frozen=True)
class _Artifact:
    artifact_id: str

    @property
    def approval_id(self) -> str:
        return self.artifact_id

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {"artifact_id": self.artifact_id}


@dataclass(frozen=True)
class _Dispatch:
    dispatch_result_id: str
    frame_sent: bool = True
    dispatch_status: str = "accepted"
    command_ack_observed: bool = True
    command_ack_result_name: str = "ACCEPTED"

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {
            "dispatch_result_id": self.dispatch_result_id,
            "frame_sent": self.frame_sent,
            "dispatch_status": self.dispatch_status,
            "command_ack_observed": self.command_ack_observed,
            "command_ack_result_name": self.command_ack_result_name,
        }


def _inputs(
    tmp_path: Path,
    *,
    supervisor: bool,
) -> PayloadRecoveryFlowInputs:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="px4_route",
        title="payload recovery",
        status="running",
        artifacts={"existing": {"kept": True}},
    )
    return PayloadRecoveryFlowInputs(
        store=store,
        task_id=task["task_id"],
        artifact_dir=tmp_path / "run",
        pickup_pose={"x": 0.0, "y": 0.0, "z": -1.0},
        route_target_ned=(5.0, 5.0, -3.0),
        route_altitude_max_m=50.0,
        payload_action="rtl",
        advisory_ref="payload_feasibility_advisory:test",
        supervisor_advisory_ref="payload_feasibility_advisory:",
        supervisor_loop_requested=supervisor,
        post_recovery_action="land" if supervisor else "none",
        vehicle_realism_summary={},
        battery_realism_summary={},
        telemetry_realism_summary={},
    )


def _runtime(
    events: list[str],
    *,
    route_pose: dict[str, float] | None = None,
) -> PayloadRecoveryRuntime:
    dispatch_count = 0

    def dispatch(action: str) -> tuple[_Artifact, _Artifact, _Dispatch]:
        nonlocal dispatch_count
        dispatch_count += 1
        events.append(f"dispatch:{action}")
        return (
            _Artifact(f"approval-{dispatch_count}"),
            _Artifact(f"allowlist-{dispatch_count}"),
            _Dispatch(f"dispatch-{dispatch_count}"),
        )

    def observe(**kwargs: Any) -> tuple:
        action = str(kwargs["action"])
        events.append(f"observe:{action}")
        if action == "rtl":
            return (
                True,
                "return_to_launch_state_observed",
                {"x": 0.2, "y": 0.1, "z": -1.0},
                [{"x": 0.2, "y": 0.1, "z": -1.0}],
            )
        return (
            True,
            None,
            {"x": 0.2, "y": 0.1, "z": 0.0},
            [{"x": 0.2, "y": 0.1, "z": 0.0}],
        )

    return PayloadRecoveryRuntime(
        terrain_relative_xy_origin=lambda _pickup: (0.0, 0.0),
        assert_stream_budget=lambda **_kwargs: events.append("budget"),
        send_route_with_monitor=lambda **_kwargs: (
            events.append("route") or {"setpoint_frames_sent": 12}
        ),
        pose_sample=lambda: route_pose or {"x": 0.0, "y": 0.0, "z": -1.0},
        append_pose_row=lambda phase, _pose: events.append(f"pose:{phase}"),
        dispatch_recovery=dispatch,
        observe_recovery_state=observe,
        refresh_realism=lambda: (
            events.append("refresh")
            or PayloadRecoveryRealismRefresh(
                telemetry_summary={"telemetry": "refreshed"},
                vehicle_summary={"vehicle": "refreshed"},
                wind_artifacts={"wind": "observed"},
                vehicle_artifacts={"vehicle_readback": "observed"},
            )
        ),
        observed_at_iso=lambda: "2026-07-16T00:00:00+00:00",
    )


def test_single_cycle_payload_recovery_keeps_delivery_unclaimed(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    result = run_payload_recovery_flow(
        _inputs(tmp_path, supervisor=False),
        runtime=_runtime(events),
    )

    assert events == ["dispatch:rtl", "observe:rtl", "refresh"]
    assert result.updated_task["status"] == "completed"
    assert result.summary["final_status"] == "payload_advisory_recovered_rtl"
    assert result.summary["payload_recovery_completed"] is True
    assert result.summary["delivery_completion_claimed"] is False
    assert result.summary["physical_execution_invoked"] is False
    assert result.post_cycle is None
    assert result.updated_task["artifacts"]["px4_gazebo_emergency_command_approval"] == {
        "artifact_id": "approval-1"
    }


def test_supervisor_flow_requires_separate_approvals_and_observed_cycles(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    result = run_payload_recovery_flow(
        _inputs(tmp_path, supervisor=True),
        runtime=_runtime(events, route_pose={"x": 3.0, "y": 0.0, "z": -3.0}),
    )

    assert events == [
        "budget",
        "route",
        "pose:payload_pre_recovery_route",
        "dispatch:rtl",
        "observe:rtl",
        "dispatch:land",
        "observe:land",
        "refresh",
    ]
    assert result.updated_task["status"] == "completed"
    assert result.summary["final_status"] == ("payload_supervisor_post_recovery_land_observed")
    assert result.summary["payload_route_progress_away_from_pickup_observed"] is True
    assert result.summary["decision_loop_driver"] == "mission_os_supervisor"
    assert result.summary["delivery_completion_claimed"] is False
    artifacts = result.updated_task["artifacts"]
    assert artifacts["px4_gazebo_emergency_command_approval"] == {"artifact_id": "approval-1"}
    assert artifacts["px4_gazebo_post_recovery_emergency_command_approval"] == {
        "artifact_id": "approval-2"
    }


def test_supervisor_rejects_missing_route_progress_before_dispatch(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    with pytest.raises(RuntimeError, match="requires route progress"):
        run_payload_recovery_flow(
            _inputs(tmp_path, supervisor=True),
            runtime=_runtime(events, route_pose={"x": 1.0, "y": 0.0, "z": -3.0}),
        )

    assert events == ["budget", "route", "pose:payload_pre_recovery_route"]
