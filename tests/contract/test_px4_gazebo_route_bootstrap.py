from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import bootstrap
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


def _store(tmp_path: Path) -> TaskStore:
    return TaskStore(str(tmp_path / "tasks.db"))


def test_legacy_entrypoint_delegates_route_bootstrap_to_package() -> None:
    assert route_entrypoint._RouteBootstrapResult is bootstrap.RouteBootstrapResult
    assert route_entrypoint._bootstrap_route_task is bootstrap.bootstrap_route_task


def test_bootstrap_requires_fresh_operator_approval_before_task_creation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    with pytest.raises(PermissionError, match="fresh operator approval"):
        bootstrap.bootstrap_route_task(
            store=store,
            max_pose_deviation_xy_m=2.0,
            on_deviation_action="abort_only",
            operator_approval_performed=False,
            now=NOW,
        )

    assert store.list() == []


def test_bootstrap_persists_plan_approval_and_bounded_allowlists(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    result = bootstrap.bootstrap_route_task(
        store=store,
        max_pose_deviation_xy_m=2.0,
        on_deviation_action="abort_only",
        operator_approval_performed=True,
        now=NOW,
    )

    persisted = store.get(result.task["task_id"])
    assert persisted is not None
    assert persisted["status"] == "running"
    assert persisted["artifacts"]["existing"]["kept"] is True
    assert result.approval.operator_approval_performed is True
    assert result.coupled_allowlist.approval_ref.endswith(result.approval.approval_id)
    assert result.route_allowlist.operator_approval_ref.endswith(result.approval.approval_id)
    assert result.route_allowlist.route_plan_ref.endswith(result.route.route_plan_id)
    assert set(persisted["artifacts"]) == {
        "existing",
        "px4_gazebo_pickup_dropoff_route_plan",
        "px4_gazebo_coupled_command_approval",
        "px4_gazebo_coupled_command_allowlist",
        "px4_gazebo_route_command_allowlist",
    }


def test_bootstrap_does_not_create_dispatch_or_completion_artifacts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    result = bootstrap.bootstrap_route_task(
        store=store,
        max_pose_deviation_xy_m=3.5,
        on_deviation_action="rtl",
        operator_approval_performed=True,
        now=NOW,
    )

    persisted = store.get(result.task["task_id"])
    assert persisted is not None
    artifact_names = set(persisted["artifacts"])
    assert not any("dispatch_result" in name for name in artifact_names)
    assert not any("progress_evidence" in name for name in artifact_names)
    assert not any("completion" in name for name in artifact_names)
    assert result.route.max_pose_deviation_xy_m == 3.5
    assert result.route.on_deviation_action == "rtl"


def test_invalid_route_configuration_does_not_leave_partial_task(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValidationError):
        bootstrap.bootstrap_route_task(
            store=store,
            max_pose_deviation_xy_m=2.0,
            on_deviation_action="unbounded",
            operator_approval_performed=True,
            now=NOW,
        )

    assert store.list() == []
