"""Shared Desktop execution helper."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional, TypeVar

from src.bridges.common_errors import flatten_exception_text
from src.runtime.tool_events import emit_tool_result, emit_tool_start

ClientT = TypeVar("ClientT")
ResultT = TypeVar("ResultT")


async def execute_desktop_call(
    *,
    request: Any,
    tool_name: str,
    args: dict[str, Any],
    get_client: Callable[[], ClientT],
    invoke: Callable[[ClientT, Any], Awaitable[ResultT]],
    ok_getter: Callable[[ResultT], bool],
    error_payload: Callable[[str], dict[str, Any]],
    metadata: Optional[dict[str, Any]] = None,
) -> tuple[Optional[ResultT], dict[str, Any]]:
    """Execute a Desktop call with consistent tool event emission."""
    metadata = metadata or {"executor": "desktop"}

    try:
        client = get_client()
        await emit_tool_start(
            session_id=request.session_id,
            tool_name=tool_name,
            agent_name=request.agent_name,
            args=args,
            request_id=request.request_id,
            metadata=metadata,
        )
        result = await invoke(client, request)
        payload = result.model_dump(exclude_none=True)
        await emit_tool_result(
            session_id=request.session_id,
            tool_name=tool_name,
            agent_name=request.agent_name,
            ok=ok_getter(result),
            result=payload,
            request_id=request.request_id,
            metadata=metadata,
        )
        return result, payload
    except Exception as exc:
        payload = error_payload(flatten_exception_text(exc))
        await emit_tool_result(
            session_id=request.session_id,
            tool_name=tool_name,
            agent_name=request.agent_name,
            ok=False,
            result=payload,
            request_id=request.request_id,
            metadata=metadata,
        )
        return None, payload
