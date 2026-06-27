"""Global tool event notifier for out-of-band tool executions."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from src.gateway.protocol import ev_tool_result, ev_tool_start

_tool_event_notifier: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None


def set_tool_event_notifier(
    notifier: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]],
) -> None:
    global _tool_event_notifier
    _tool_event_notifier = notifier


def _summarize_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        summarized: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, str):
                summarized[key] = value[:400]
            elif isinstance(value, list):
                summarized[key] = value[:5]
            elif isinstance(value, dict):
                summarized[key] = _summarize_payload(value)
            else:
                summarized[key] = value
        return summarized
    return {"value": str(payload)[:400]}


async def emit_tool_start(
    *,
    session_id: str,
    tool_name: str,
    agent_name: str,
    args: Optional[dict[str, Any]] = None,
    request_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    if not session_id or _tool_event_notifier is None:
        return
    await _tool_event_notifier(
        session_id,
        ev_tool_start(
            tool_name=tool_name,
            agent_name=agent_name,
            args=_summarize_payload(args or {}),
            request_id=request_id,
            metadata=metadata,
        ),
    )


async def emit_tool_result(
    *,
    session_id: str,
    tool_name: str,
    agent_name: str,
    ok: bool,
    result: Optional[dict[str, Any]] = None,
    request_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    if not session_id or _tool_event_notifier is None:
        return
    await _tool_event_notifier(
        session_id,
        ev_tool_result(
            tool_name=tool_name,
            agent_name=agent_name,
            ok=ok,
            result=_summarize_payload(result or {}),
            request_id=request_id,
            metadata=metadata,
        ),
    )
