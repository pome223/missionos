from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import bootstrap, finalization
from src.runtime.px4_gazebo_route_dispatcher import (
    PX4GazeboRouteDispatcherError,
    derive_px4_gazebo_route_target_ned,
)
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)


def _inputs(tmp_path: Path, **overrides: Any) -> finalization.RouteFinalizationInputs:
    store = TaskStore(str(tmp_path / "tasks.db"))
    initial = bootstrap.bootstrap_route_task(
        store=store,
        max_pose_deviation_xy_m=2.0,
        on_deviation_action="abort_only",
        operator_approval_performed=True,
        now=NOW,
    )
    target_x, target_y, target_z = derive_px4_gazebo_route_target_ned(initial.route)
    values: dict[str, Any] = {
        "store": store,
        "task_id": initial.task["task_id"],
        "route": initial.route,
        "route_allowlist": initial.route_allowlist,
        "approval": initial.approval,
        "endpoint_port": 14540,
        "target_x_m": target_x,
        "target_y_m": target_y,
        "target_z_m": target_z,
        "route_stream": {
            "setpoint_frames_sent": 20,
            "setpoint_stream_duration_seconds": 1.0,
            "offboard_mode_switch_frame_sent": True,
            "offboard_mode_switch_ack_observed": True,
            "offboard_mode_switch_ack_result_code": 0,
            "offboard_mode_switch_ack_result_name": "ACCEPTED",
            "offboard_mode_switch_ack_timeout_seconds": 5.0,
            "deviation_samples": [],
        },
        "pickup_pose_xy_m": (0.0, 0.0),
        "observed_pose_xy_m": (target_x, target_y),
        "horizontal_route_motion_observed": True,
        "px4_telemetry_correlated": True,
        "gazebo_pose_correlated": True,
        "actual_px4_gazebo_horizontal_smoke_observed": True,
        "now": NOW,
    }
    values.update(overrides)
    return finalization.RouteFinalizationInputs(**values)


def test_legacy_entrypoint_delegates_route_finalization_to_package() -> None:
    assert route_entrypoint._RouteFinalizationInputs is finalization.RouteFinalizationInputs
    assert route_entrypoint._RouteFinalizationResult is finalization.RouteFinalizationResult
    assert route_entrypoint._finalize_route_observation is finalization.finalize_route_observation


def test_finalization_persists_observed_chain_and_completes_with_all_evidence(
    tmp_path: Path,
) -> None:
    result = finalization.finalize_route_observation(_inputs(tmp_path))

    assert result.updated_task["status"] == "completed"
    assert result.dispatch.offboard_mode_switch_ack_observed is True
    assert result.progress.dropoff_region_reached is True
    assert result.gate.final_status.value == "completed"
    assert result.gate.physical_execution_invoked is False
    assert set(result.updated_task["artifacts"]).issuperset(
        {
            "px4_gazebo_route_command_dispatch_result",
            "px4_gazebo_route_progress_evidence",
            "px4_gazebo_route_delivery_completion_gate",
            "px4_gazebo_route_delivery_runner_result",
        }
    )


def test_ack_and_dropoff_pose_do_not_replace_motion_observation(tmp_path: Path) -> None:
    result = finalization.finalize_route_observation(
        _inputs(tmp_path, horizontal_route_motion_observed=False)
    )

    assert result.dispatch.offboard_mode_switch_ack_observed is True
    assert result.progress.dropoff_region_reached is True
    assert result.updated_task["status"] == "blocked"
    assert "horizontal_route_motion_missing" in result.gate.blocked_reasons


def test_stale_progress_remains_blocked(tmp_path: Path) -> None:
    result = finalization.finalize_route_observation(
        _inputs(tmp_path, route_progress_age_seconds=6.0)
    )

    assert result.updated_task["status"] == "blocked"
    assert "stale_route_progress" in result.gate.blocked_reasons


def test_missing_offboard_ack_cannot_create_progress_or_completion(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    stream = dict(inputs.route_stream)
    stream.update(
        {
            "offboard_mode_switch_ack_observed": False,
            "offboard_mode_switch_ack_result_code": None,
            "offboard_mode_switch_ack_result_name": None,
        }
    )

    with pytest.raises(PX4GazeboRouteDispatcherError, match="requires sent route dispatch"):
        finalization.finalize_route_observation(replace(inputs, route_stream=stream))

    persisted = inputs.store.get(inputs.task_id)
    assert persisted is not None
    assert persisted["status"] == "running"
    assert "px4_gazebo_route_progress_evidence" not in persisted["artifacts"]
    assert "px4_gazebo_route_delivery_completion_gate" not in persisted["artifacts"]


def test_unknown_task_fails_before_artifact_construction(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path, task_id="task_missing")

    with pytest.raises(finalization.RouteFinalizationError, match="not found"):
        finalization.finalize_route_observation(inputs)
