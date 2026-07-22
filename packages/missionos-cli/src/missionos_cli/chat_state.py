"""Persistent CLI context and input decoding for MissionOS chat flows."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json

import click
import yaml


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _mission_designer_payload(payload: dict[str, Any]) -> dict[str, Any]:
    mission_designer = payload.get("mission_designer")
    if isinstance(mission_designer, dict) and mission_designer:
        return mission_designer
    operation_result = payload.get("operation_result")
    if isinstance(operation_result, dict):
        return operation_result
    return {}


def _mission_designer_context_ref(payload: dict[str, Any]) -> dict[str, Any]:
    mission_designer = _mission_designer_payload(payload)
    summary = (
        mission_designer.get("summary") if isinstance(mission_designer.get("summary"), dict) else {}
    )
    context_ref = mission_designer.get("mission_designer_context_ref") or summary.get(
        "mission_designer_context_ref"
    )
    context_sha256 = mission_designer.get("mission_designer_context_sha256") or summary.get(
        "mission_designer_context_sha256"
    )
    context_session_id = mission_designer.get("mission_designer_context_session_id") or summary.get(
        "mission_designer_context_session_id"
    )
    if not context_ref or not context_sha256:
        return {}
    return {
        "mission_designer_context_ref": str(context_ref),
        "mission_designer_context_sha256": str(context_sha256),
        "mission_designer_context_session_id": str(context_session_id or ""),
    }


def _mission_designer_sitl_task_id(payload: dict[str, Any]) -> str:
    mission_designer = _mission_designer_payload(payload)
    summary = (
        mission_designer.get("summary") if isinstance(mission_designer.get("summary"), dict) else {}
    )
    task_id = (
        summary.get("sitl_execution_task_id")
        or summary.get("turtlebot3_home_mission_task_id")
        or summary.get("task_id")
    )
    if task_id:
        return str(task_id)
    task = mission_designer.get("sitl_execution_task")
    if isinstance(task, dict) and task.get("task_id"):
        return str(task["task_id"])
    turtlebot3_task = mission_designer.get("turtlebot3_home_mission_task")
    if isinstance(turtlebot3_task, dict) and turtlebot3_task.get("task_id"):
        return str(turtlebot3_task["task_id"])
    return ""


def _payload_task_id(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    summary = payload.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    for key in ("task_id", "sitl_execution_task_id"):
        task_id = summary.get(key)
        if task_id:
            return str(task_id)
    task = payload.get("task")
    if isinstance(task, dict) and task.get("task_id"):
        return str(task["task_id"])
    mission_task_id = _mission_designer_sitl_task_id(payload)
    if mission_task_id:
        return mission_task_id
    return ""


def _stored_mission_designer_context(ctx: click.Context, session_id: str) -> dict[str, Any]:
    state = _load_state(ctx.obj["missionos_state_path"])
    context = state.get("mission_designer_context")
    if not isinstance(context, dict):
        return {}
    context_session_id = str(context.get("mission_designer_context_session_id") or "")
    if context_session_id and context_session_id != session_id:
        return {}
    context_gateway_url = str(state.get("missionos_gateway_url") or "")
    current_gateway_url = str(ctx.obj.get("missionos_gateway_url") or "")
    if context_gateway_url and current_gateway_url and context_gateway_url != current_gateway_url:
        return {}
    return dict(context)


def _remember_mission_designer_context(
    ctx: click.Context,
    payload: dict[str, Any],
    *,
    session_id: str,
) -> None:
    context = _mission_designer_context_ref(payload)
    if not context:
        return
    if not context.get("mission_designer_context_session_id"):
        context["mission_designer_context_session_id"] = session_id
    state = _load_state(ctx.obj["missionos_state_path"])
    state["session_id"] = session_id
    state["missionos_gateway_url"] = str(ctx.obj.get("missionos_gateway_url") or "")
    state["mission_designer_context"] = context
    task_id = _mission_designer_sitl_task_id(payload) or _payload_task_id(payload)
    if task_id:
        state["sitl_execution_task_id"] = task_id
    _save_state(ctx.obj["missionos_state_path"], state)


def _remember_sitl_task_id(ctx: click.Context, task_id: str) -> None:
    if not task_id:
        return
    state = _load_state(ctx.obj["missionos_state_path"])
    state["sitl_execution_task_id"] = task_id
    _save_state(ctx.obj["missionos_state_path"], state)


def _remember_sitl_task_id_from_payload(
    ctx: click.Context,
    payload: dict[str, Any] | None,
    *,
    fallback_task_id: str = "",
) -> str:
    task_id = _payload_task_id(payload) or fallback_task_id
    _remember_sitl_task_id(ctx, task_id)
    return task_id


def _stored_sitl_task_id(ctx: click.Context) -> str:
    state = _load_state(ctx.obj["missionos_state_path"])
    return str(state.get("sitl_execution_task_id") or "")


def _load_json_object(raw: str | None, *, label: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{label} must be a JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"{label} must be a JSON object")
    return payload


def _load_coordinate_route_file(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(f"could not read {path}: {exc}") from exc
    if file_path.suffix.lower() in {".yaml", ".yml"}:
        try:
            payload = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise click.ClickException(f"{path} must be a YAML object: {exc}") from exc
        if not isinstance(payload, dict):
            raise click.ClickException(f"{path} must be a YAML object")
        return payload
    return _load_json_object(raw, label=path)
