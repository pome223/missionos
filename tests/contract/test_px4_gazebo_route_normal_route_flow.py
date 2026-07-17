from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.runtime.px4_gazebo_route.bootstrap import bootstrap_route_task
from src.runtime.px4_gazebo_route.normal_route_flow import (
    NormalRouteFlowInputs,
    NormalRouteRuntime,
    NormalRouteTerminalProjection,
    run_normal_route_flow,
)
from src.runtime.px4_gazebo_route_dispatcher import derive_px4_gazebo_route_target_ned
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _RecoveryDispatch:
    dispatch_status: str = "accepted"
    command_ack_observed: bool = True
    command_ack_result_name: str = "ACCEPTED"


def _bootstrap(
    tmp_path: Path,
    *,
    on_deviation_action: str = "abort_only",
) -> tuple[NormalRouteFlowInputs, tuple[float, float, float]]:
    store = TaskStore(str(tmp_path / "tasks.db"))
    initial = bootstrap_route_task(
        store=store,
        max_pose_deviation_xy_m=2.0,
        on_deviation_action=on_deviation_action,
        operator_approval_performed=True,
        now=NOW,
    )
    target = derive_px4_gazebo_route_target_ned(initial.route)
    return (
        NormalRouteFlowInputs(
            store=store,
            task_id=initial.task["task_id"],
            task_db_path=tmp_path / "tasks.db",
            artifact_dir=tmp_path / "run",
            route=initial.route,
            approval=initial.approval,
            route_allowlist=initial.route_allowlist,
            endpoint_port=14600,
            pickup_pose={"x": 0.0, "y": 0.0, "z": 0.0},
            climb_samples=[{"x": 0.0, "y": 0.0, "z": -1.0}],
            inject_target_offset_m=0.0,
            collision_observation_attempts=1,
            rth_requested=False,
            observed_at=NOW,
            preupload_summary=None,
            live_pose_trace_populated=False,
        ),
        target,
    )


def _route_stream(*, ack: bool = True, deviation: bool = False) -> dict[str, Any]:
    return {
        "pose_deviation_aborted": deviation,
        "setpoint_frames_sent": 250,
        "setpoint_stream_duration_seconds": 25.0,
        "offboard_mode_switch_allowed": True,
        "offboard_mode_switch_command_id": 176,
        "offboard_mode_switch_frame_sent": True,
        "offboard_mode_switch_ack_required": True,
        "offboard_mode_switch_ack_command_id": 176,
        "offboard_mode_switch_ack_observed": ack,
        "offboard_mode_switch_ack_result_code": 0 if ack else None,
        "offboard_mode_switch_ack_result_name": "ACCEPTED" if ack else None,
        "offboard_mode_switch_ack_timeout_seconds": 5.0,
        "deviation_samples": [],
        "route_monitor_sample_count": 5,
        "feed_forward_velocity_x_mps": 0.0,
        "feed_forward_velocity_y_mps": 0.0,
        "feed_forward_phase_schedule": "full_then_linear_ramp_down",
        "feed_forward_ramp_start_fraction": 0.65,
        "feed_forward_ramp_end_fraction": 0.9,
        "feed_forward_scale_min": 0.0,
        "feed_forward_scale_max": 1.0,
        "feed_forward_scale_sample_count": 5,
    }


def _runtime(
    events: list[str],
    *,
    target: tuple[float, float, float],
    stream: dict[str, Any],
) -> NormalRouteRuntime:
    pose_samples = iter(
        [
            {"x": target[0], "y": target[1], "z": target[2]},
        ]
    )

    def send_route(**kwargs: Any) -> dict[str, Any]:
        events.append("stream")
        assert kwargs["target_x"] == target[0]
        assert kwargs["target_y"] == target[1]
        if stream["pose_deviation_aborted"]:
            kwargs["on_deviation"]()
        return dict(stream)

    return NormalRouteRuntime(
        terrain_relative_xy_origin=lambda _pickup: (0.0, 0.0),
        wind_compensation_request=lambda: {
            "route_geometry_compensation_applied": False,
            "feed_forward_ramp_start_fraction": 0.65,
            "feed_forward_ramp_end_fraction": 0.9,
        },
        wind_compensation_xy_offset=lambda _request: (0.0, 0.0),
        wind_feed_forward_xy_mps=lambda _request: (0.0, 0.0),
        assert_stream_budget=lambda **_kwargs: events.append("budget"),
        send_route_with_monitor=send_route,
        dispatch_recovery=lambda action: (
            events.append(f"dispatch:{action}")
            or ("approval", "allowlist", _RecoveryDispatch())
        ),
        handle_route_deviation=lambda **kwargs: (
            events.append("deviation")
            or {
                "route_stream": kwargs["route_stream"],
                "dispatch": kwargs["recovery_dispatch"],
            }
        ),
        pose_sample=lambda: next(pose_samples),
        append_pose_row=lambda phase, _pose: events.append(f"pose:{phase}"),
        collect_route_blocking_observation=lambda **_kwargs: (
            events.append("observe-blocking") or {}
        ),
        record_wait_observation=lambda attempt: events.append(f"wait:{attempt}"),
        dispatch_rth=lambda: (_ for _ in ()).throw(AssertionError("unexpected RTH")),
        observe_recovery_state=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected recovery observation")
        ),
        upload_alternate_mission=lambda: (_ for _ in ()).throw(
            AssertionError("unexpected alternate mission")
        ),
        execute_alternate_route=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected alternate route")
        ),
        dispatch_alternate_landing=lambda: (_ for _ in ()).throw(
            AssertionError("unexpected alternate landing")
        ),
        send_standard_land=lambda: events.append("land"),
        wait_for_landing=lambda phase: (
            events.append(f"landing:{phase}")
            or {"x": target[0], "y": target[1], "z": 0.0},
            [{"x": target[0], "y": target[1], "z": 0.0}],
        ),
        project_terminal_realism=lambda **_kwargs: (
            events.append("project")
            or NormalRouteTerminalProjection(
                payload_release_summary=None,
                telemetry_summary={},
                vehicle_summary={},
                obstacle_supervisor_recovery_loop=None,
                wind_artifacts={},
                vehicle_artifacts={},
            )
        ),
        snapshot_task_database=lambda **_kwargs: events.append("snapshot"),
        recorded_at=lambda: NOW,
    )


def test_normal_route_completes_from_observed_progress_not_ack_alone(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    inputs, target = _bootstrap(tmp_path)
    result = run_normal_route_flow(
        inputs,
        runtime=_runtime(events, target=target, stream=_route_stream()),
    )

    assert result.branch == "normal_route"
    assert events == [
        "budget",
        "stream",
        "pose:route",
        "observe-blocking",
        "land",
        "landing:landing",
        "pose:completed",
        "project",
        "snapshot",
    ]
    assert result.updated_task is not None
    assert result.updated_task["status"] == "completed"
    assert result.summary is not None
    assert result.summary["dropoff_region_reached"] is True
    assert result.summary["physical_execution_invoked"] is False
    assert result.summary["hardware_target_allowed"] is False
    assert result.fallback_pose_rows is not None


def test_deviation_hands_captured_authority_to_separate_flow(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    inputs, target = _bootstrap(tmp_path, on_deviation_action="rtl")
    result = run_normal_route_flow(
        inputs,
        runtime=_runtime(
            events,
            target=target,
            stream=_route_stream(deviation=True),
        ),
    )

    assert result.branch == "route_deviation"
    assert events == ["budget", "stream", "dispatch:rtl", "deviation"]
    assert result.deviation_result["dispatch"] == _RecoveryDispatch()
    persisted = inputs.store.get(inputs.task_id)
    assert persisted is not None
    assert persisted["status"] == "running"


def test_missing_offboard_ack_fails_before_terminal_or_completion(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    inputs, target = _bootstrap(tmp_path)

    with pytest.raises(RuntimeError, match="required observed OFFBOARD ACK"):
        run_normal_route_flow(
            inputs,
            runtime=_runtime(
                events,
                target=target,
                stream=_route_stream(ack=False),
            ),
        )

    assert events == ["budget", "stream"]
    persisted = inputs.store.get(inputs.task_id)
    assert persisted is not None
    assert persisted["status"] == "running"
    assert "px4_gazebo_route_delivery_completion_gate" not in persisted["artifacts"]
