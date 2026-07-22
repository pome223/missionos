"""Task classification and selection for live operator surfaces."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import click

from .gateway_client import MissionOSGatewayClient


def _is_home_robot_nav2_execution_target(value: Any) -> bool:
    return str(value or "") in {
        "ros2_nav2_turtlebot3_sim",
        "ros2_nav2_turtlebot4_sim",
        "isaac_ros_nav2_nova_carter_sim",
    }


def _is_turtlebot3_task_artifacts(artifacts: dict[str, Any]) -> bool:
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    execution = artifacts.get("turtlebot3_home_mission_execution")
    execution = execution if isinstance(execution, dict) else {}
    indoor_map = artifacts.get("turtlebot3_indoor_map_model")
    return (
        _is_home_robot_nav2_execution_target(summary.get("execution_target"))
        or _is_home_robot_nav2_execution_target(execution.get("execution_target"))
        or isinstance(indoor_map, dict)
    )


def _is_real_mission_designer_sitl_task(task: dict[str, Any]) -> bool:
    """Return true for production Mission Designer SITL tasks, not smoke residue."""
    kind = str(task.get("kind") or "")
    if kind == "px4_gazebo_mission_designer_sitl_execution_request":
        return True
    artifacts = task.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    return "px4_gazebo_mission_designer_sitl_execution_request" in artifacts


def _task_has_active_auto_runner_request_path(task: dict[str, Any]) -> bool:
    artifacts = task.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    receipt = artifacts.get("missionos_auto_mission_gui_dispatch_running_receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    return bool(receipt.get("operator_recovery_request_container_path"))


def _latest_running_sitl_task_id(
    client: MissionOSGatewayClient,
    *,
    prefer_active_runner: bool = False,
    require_active_runner: bool = False,
) -> str | None:
    """Find the most recent running production Mission Designer SITL task."""
    try:
        payload = client.get("/tasks?page=1&page_size=20")
    except click.ClickException:
        return None
    items = payload.get("items") or payload.get("tasks") or []
    if not isinstance(items, list):
        return None
    candidates: list[dict[str, Any]] = []
    for task in items:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or task.get("task_status") or "")
        has_active_runner = _task_has_active_auto_runner_request_path(task)
        if status != "running" or (
            not _is_real_mission_designer_sitl_task(task) and not has_active_runner
        ):
            continue
        candidates.append(task)
    if prefer_active_runner:
        active = [
            task
            for task in candidates
            if _task_has_active_auto_runner_request_path(task)
        ]
        if active:
            candidates = active
    if require_active_runner:
        candidates = [
            task
            for task in candidates
            if _task_has_active_auto_runner_request_path(task)
        ]
    for task in candidates:
        task_id = task.get("task_id")
        if task_id:
            return str(task_id)
    return None


def _resolve_live_task_id(
    client: MissionOSGatewayClient,
    *,
    explicit_task_id: str,
    stored_task_id: str,
) -> str:
    """Resolve the task for a live view without trusting stale local state first."""
    if explicit_task_id:
        return explicit_task_id
    running = _latest_running_sitl_task_id(
        client,
        prefer_active_runner=True,
        require_active_runner=True,
    )
    if running:
        return running
    if stored_task_id:
        return stored_task_id
    running = _latest_running_sitl_task_id(client)
    if running:
        return running
    raise click.ClickException(
        "no running SITL task found; run a flight first or pass --task-id"
    )


def _resolve_operator_recovery_task_id(
    client: MissionOSGatewayClient,
    *,
    explicit_task_id: str,
    stored_task_id: str,
) -> str:
    """Resolve a task that can accept an explicit operator recovery request."""
    if explicit_task_id:
        return explicit_task_id
    if stored_task_id:
        try:
            payload = client.get(f"/tasks/{quote(stored_task_id, safe='')}")
        except (click.ClickException, OSError):
            payload = {}
        task = payload.get("task") if isinstance(payload, dict) else None
        artifacts = (
            task.get("artifacts")
            if isinstance(task, dict) and isinstance(task.get("artifacts"), dict)
            else {}
        )
        checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
        if (
            isinstance(task, dict)
            and task.get("kind") == "turtlebot3_home_mission_execution"
            and str(task.get("status") or "").lower() == "pending"
            and isinstance(checkpoint, dict)
            and checkpoint.get("checkpoint_status")
            == "awaiting_operator_approval"
        ):
            return stored_task_id
    running = _latest_running_sitl_task_id(
        client,
        prefer_active_runner=True,
        require_active_runner=True,
    )
    if running:
        return running
    raise click.ClickException(
        "no active live SITL runner or pending TurtleBot3 recovery checkpoint found; "
        "start a fresh live mission, or pass --task-id explicitly"
    )
