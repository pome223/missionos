"""First-class task object tools for long-running workflows."""

from __future__ import annotations

import json
from typing import Any, Optional

from google.adk.agents.context import Context as ToolContext

from src.runtime.task_store import get_task_store, TASK_STORE_UNSET
from src.tools.context import resolve_tool_context


TASK_UPDATE_ERROR_UNSET = TASK_STORE_UNSET


def _parse_json_object(name: str, payload: Optional[str]) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must decode to a JSON object")
    return value


def _parse_json_string_list(name: str, payload: Optional[str]) -> list[str]:
    if not payload:
        return []
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON: {exc}") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must decode to a JSON array of strings")
    return list(value)


def _resolve_owner(tool_context: Optional[ToolContext]) -> dict[str, str]:
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    return {
        "session_id": str(ctx.get("session_id") or "unknown_session"),
        "user_id": str(ctx.get("user_id") or "unknown_user"),
    }


def create_task_record(
    *,
    kind: str,
    title: str,
    status: str = "pending",
    owner_session_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    parent_task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    winner_task_id: Optional[str] = None,
    loser_task_ids: Optional[list[str]] = None,
    approval_dependencies: Optional[list[str]] = None,
    artifacts: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    owner = _resolve_owner(tool_context)
    return get_task_store().create(
        kind=kind,
        title=title,
        status=status,
        owner_session_id=owner_session_id or owner["session_id"],
        owner_user_id=owner_user_id or owner["user_id"],
        parent_task_id=parent_task_id,
        run_id=run_id,
        winner_task_id=winner_task_id,
        loser_task_ids=loser_task_ids,
        approval_dependencies=approval_dependencies,
        artifacts=artifacts,
        metadata=metadata,
        error=error,
    )


def update_task_record(
    task_id: str,
    *,
    status: Optional[str] = None,
    title: Optional[str] = None,
    artifacts: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
    error: Any = TASK_UPDATE_ERROR_UNSET,
    run_id: Optional[str] = None,
    winner_task_id: Optional[str] = None,
    loser_task_ids: Optional[list[str]] = None,
    approval_dependencies: Optional[list[str]] = None,
    ended_at: Optional[float] = None,
) -> dict[str, Any] | None:
    return get_task_store().update(
        task_id,
        status=status,
        title=title,
        artifacts=artifacts,
        metadata=metadata,
        error=error,
        run_id=run_id,
        winner_task_id=winner_task_id,
        loser_task_ids=loser_task_ids,
        approval_dependencies=approval_dependencies,
        ended_at=ended_at,
    )


def append_task_event_record(
    task_id: str,
    *,
    event_type: str,
    payload: Optional[dict[str, Any]] = None,
    status: Optional[str] = None,
    title: Optional[str] = None,
    error: Optional[str] = None,
    timestamp: Optional[float] = None,
) -> dict[str, Any] | None:
    return get_task_store().append_event(
        task_id,
        event_type=event_type,
        payload=payload,
        status=status,
        title=title,
        error=error,
        timestamp=timestamp,
    )


async def task_create(
    kind: str,
    title: str,
    status: str = "pending",
    parent_task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    winner_task_id: Optional[str] = None,
    loser_task_ids_json: Optional[str] = None,
    approval_dependencies_json: Optional[str] = None,
    artifacts_json: Optional[str] = None,
    metadata_json: Optional[str] = None,
    error: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Create a first-class task object for a long-running workflow."""

    try:
        task = create_task_record(
            kind=kind,
            title=title,
            status=status,
            parent_task_id=parent_task_id,
            run_id=run_id,
            winner_task_id=winner_task_id,
            loser_task_ids=_parse_json_string_list("loser_task_ids_json", loser_task_ids_json),
            approval_dependencies=_parse_json_string_list(
                "approval_dependencies_json",
                approval_dependencies_json,
            ),
            artifacts=_parse_json_object("artifacts_json", artifacts_json),
            metadata=_parse_json_object("metadata_json", metadata_json),
            error=error,
            tool_context=tool_context,
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "task": task, "task_id": task["task_id"]}


async def task_get(
    task_id: str,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Return a task object owned by the current session."""

    task = get_task_store().get(task_id)
    if task is None:
        return {"success": False, "error": f"Unknown task: {task_id}"}
    owner = _resolve_owner(tool_context)
    if task.get("owner_session_id") != owner["session_id"]:
        return {"success": False, "error": "task is not owned by this session"}
    return {"success": True, "task": task}


async def task_list(
    kind: Optional[str] = None,
    status: Optional[str] = None,
    parent_task_id: Optional[str] = None,
    query: Optional[str] = None,
    page: int = 1,
    page_size: Optional[int] = None,
    limit: int = 20,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """List task objects for the current session."""

    owner = _resolve_owner(tool_context)
    result = get_task_store().query(
        owner_session_id=owner["session_id"],
        kind=kind,
        status=status,
        parent_task_id=parent_task_id,
        q=query,
        page=page,
        page_size=max(1, min(int(page_size or limit or 20), 100)),
    )
    return {
        "success": True,
        "tasks": result["tasks"],
        "count": len(result["tasks"]),
        "pagination": result["pagination"],
        "filters": result["filters"],
    }


async def task_update(
    task_id: str,
    status: Optional[str] = None,
    title: Optional[str] = None,
    run_id: Optional[str] = None,
    winner_task_id: Optional[str] = None,
    loser_task_ids_json: Optional[str] = None,
    approval_dependencies_json: Optional[str] = None,
    artifacts_json: Optional[str] = None,
    metadata_json: Optional[str] = None,
    error: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Update a task object owned by the current session."""

    current = get_task_store().get(task_id)
    if current is None:
        return {"success": False, "error": f"Unknown task: {task_id}"}
    owner = _resolve_owner(tool_context)
    if current.get("owner_session_id") != owner["session_id"]:
        return {"success": False, "error": "task is not owned by this session"}

    try:
        updated = update_task_record(
            task_id,
            status=status,
            title=title,
            run_id=run_id,
            winner_task_id=winner_task_id,
            loser_task_ids=_parse_json_string_list("loser_task_ids_json", loser_task_ids_json)
            if loser_task_ids_json is not None
            else None,
            approval_dependencies=_parse_json_string_list(
                "approval_dependencies_json",
                approval_dependencies_json,
            )
            if approval_dependencies_json is not None
            else None,
            artifacts=_parse_json_object("artifacts_json", artifacts_json)
            if artifacts_json is not None
            else None,
            metadata=_parse_json_object("metadata_json", metadata_json)
            if metadata_json is not None
            else None,
            error=error if error is not None else TASK_UPDATE_ERROR_UNSET,
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    if updated is None:
        return {"success": False, "error": f"Unknown task: {task_id}"}
    return {"success": True, "task": updated}
