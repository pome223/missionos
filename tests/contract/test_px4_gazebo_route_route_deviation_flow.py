from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.runtime.px4_gazebo_route.recovery_execution import ObservedRecoveryCycle
from src.runtime.px4_gazebo_route.recovery_outcomes import RecoveryCycleOutcome
from src.runtime.px4_gazebo_route.route_deviation_flow import (
    RouteDeviationFlowInputs,
    RouteDeviationRealismRefresh,
    RouteDeviationRuntime,
    run_route_deviation_flow,
)
from src.runtime.task_store import TaskStore


@dataclass(frozen=True)
class _Artifact:
    artifact_id: str

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {"artifact_id": self.artifact_id}


@dataclass(frozen=True)
class _Abort:
    abort_id: str = "abort-1"
    schema_version: str = "px4_gazebo_route_deviation_abort.v1"

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {
            "abort_id": self.abort_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class _Completion:
    final_status: str
    completion_id: str
    schema_version: str = "px4_gazebo_route_recovery_completion.v1"

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {
            "final_status": self.final_status,
            "completion_id": self.completion_id,
            "schema_version": self.schema_version,
            "delivery_completion_claimed": False,
            "physical_execution_invoked": False,
        }


def _inputs(
    tmp_path: Path,
    *,
    recovery: bool,
    post_action: str = "none",
) -> RouteDeviationFlowInputs:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="px4_route",
        title="route deviation",
        status="running",
        artifacts={"existing": {"kept": True}},
    )
    return RouteDeviationFlowInputs(
        store=store,
        task_id=task["task_id"],
        artifact_dir=tmp_path / "run",
        route=SimpleNamespace(
            schema_version="px4_gazebo_pickup_dropoff_route_plan.v1",
            on_deviation_action="rtl" if recovery else "abort_only",
        ),
        route_allowlist=_Artifact("route-allowlist"),
        route_stream={
            "deviation_samples": [{"deviation_xy_m": 4.0}],
            "route_monitor_sample_count": 3,
            "route_stream_terminated_before_recovery_dispatch": True,
            "route_stream_process_returncode": 0,
            "route_stream_stop_reason": "pose_deviation",
            "route_stream_forced_kill": False,
        },
        pickup_pose={"x": 0.0, "y": 0.0, "z": -3.0},
        post_recovery_action=post_action,
        recovery_approval=_Artifact("approval-1") if recovery else None,
        recovery_allowlist=_Artifact("allowlist-1") if recovery else None,
        recovery_dispatch=_Artifact("dispatch-1") if recovery else None,
        supervisor_loop_requested=False,
        multi_condition_supervisor_requested=False,
        wind_requested_profile={},
        observed_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )


def _runtime(
    events: list[str],
    *,
    primary_completed: bool,
) -> RouteDeviationRuntime:
    def observe(**kwargs: Any) -> ObservedRecoveryCycle:
        action = str(kwargs["action"])
        events.append(f"observe:{action}")
        completed = primary_completed if action == "rtl" else True
        index = 1 if action == "rtl" else 2
        completion = _Completion(
            final_status="recovered" if completed else "recovery_unconfirmed",
            completion_id=f"completion-{index}",
        )
        return ObservedRecoveryCycle(
            outcome=RecoveryCycleOutcome(
                action=action,
                approval_ref=f"approval:{index}",
                dispatch_ref=f"dispatch:{index}",
                dispatch_status="accepted",
                command_ack_observed=True,
                command_ack_result_name="ACCEPTED",
                ack_complete=completed,
                state_observed=completed,
                state_label=("return_to_launch_state_observed" if action == "rtl" else None),
                completed=completed,
                pose_z_m=0.0 if action == "land" else -1.0,
                completion_basis="ack_and_state" if completed else None,
                completion_ref=f"completion:{index}",
            ),
            pose={"x": 0.0, "y": 0.0, "z": 0.0 if action == "land" else -1.0},
            samples=(),
            completion=completion,
        )

    def dispatch(action: str) -> tuple[_Artifact, _Artifact, _Artifact]:
        events.append(f"dispatch:{action}")
        return (
            _Artifact("approval-2"),
            _Artifact("allowlist-2"),
            _Artifact("dispatch-2"),
        )

    return RouteDeviationRuntime(
        build_deviation_abort=lambda **_kwargs: _Abort(),
        observe_dispatched_recovery=observe,
        build_recovery_completion=lambda **_kwargs: None,
        observe_recovery_state=lambda **_kwargs: (),
        dispatch_recovery=dispatch,
        refresh_realism=lambda: (
            events.append("refresh")
            or RouteDeviationRealismRefresh(
                route_blocking_verification_summary={},
                vehicle_summary={},
                battery_summary={},
                telemetry_summary={},
                wind_artifacts={"wind": "observed"},
                vehicle_artifacts={"vehicle": "observed"},
            )
        ),
    )


def test_abort_only_persists_blocked_without_dispatch_authority(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    result = run_route_deviation_flow(
        _inputs(tmp_path, recovery=False),
        runtime=_runtime(events, primary_completed=False),
    )

    assert events == ["refresh"]
    assert result.updated_task["status"] == "blocked"
    assert result.summary["final_status"] == "aborted_pose_deviation"
    assert result.summary["recovery_action_taken"] is None
    assert result.summary["delivery_completion_claimed"] is False
    assert "px4_gazebo_emergency_command_approval" not in (result.updated_task["artifacts"])


def test_completed_primary_uses_new_post_recovery_approval(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    result = run_route_deviation_flow(
        _inputs(tmp_path, recovery=True, post_action="land"),
        runtime=_runtime(events, primary_completed=True),
    )

    assert events == ["observe:rtl", "dispatch:land", "observe:land", "refresh"]
    assert result.updated_task["status"] == "completed"
    assert result.summary["final_status"] == "post_recovery_recovered"
    assert result.summary["recovery_completed"] is True
    assert result.summary["post_recovery_completed"] is True
    assert result.summary["delivery_completion_claimed"] is False
    artifacts = result.updated_task["artifacts"]
    assert artifacts["px4_gazebo_emergency_command_approval"] == {"artifact_id": "approval-1"}
    assert artifacts["px4_gazebo_post_recovery_emergency_command_approval"] == {
        "artifact_id": "approval-2"
    }


def test_unobserved_primary_ack_does_not_dispatch_post_action(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    result = run_route_deviation_flow(
        _inputs(tmp_path, recovery=True, post_action="land"),
        runtime=_runtime(events, primary_completed=False),
    )

    assert events == ["observe:rtl", "refresh"]
    assert result.updated_task["status"] == "blocked"
    assert result.summary["recovery_command_ack_observed"] is True
    assert result.summary["recovery_state_observed"] is False
    assert result.summary["recovery_completed"] is False
    assert result.summary["post_recovery_action_taken"] is None
