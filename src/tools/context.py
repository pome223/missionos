"""Shared ADK runtime context helpers."""

from __future__ import annotations

from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.context import Context as LegacyToolContext
from google.adk.tools import ToolContext


def _resolve_runtime_context(context: Any) -> dict[str, str]:
    """Extract runtime identifiers from ADK contexts.

    ADK exposes `user_id` and `session` on the invocation context. That object is
    currently reachable through the private `_invocation_context` attribute on
    callback/tool contexts, so the adapter is centralized here with a fallback
    for lightweight test doubles that only define `session.id`.
    """
    invocation_context = getattr(context, "_invocation_context", None)
    session = getattr(invocation_context, "session", None)
    if session is None:
        session = getattr(context, "session", None)

    return {
        "agent_name": getattr(context, "agent_name", None) or "unknown_agent",
        "session_id": getattr(session, "id", None) or "",
        "user_id": getattr(invocation_context, "user_id", None) or "",
        "app_name": getattr(invocation_context, "app_name", None) or "",
        "invocation_id": getattr(context, "invocation_id", None) or "",
    }


def resolve_tool_context(
    tool_context: Optional[ToolContext | LegacyToolContext],
) -> dict[str, str]:
    """Extract runtime identifiers from an ADK tool context."""
    return _resolve_runtime_context(tool_context)


def resolve_callback_context(
    callback_context: Optional[CallbackContext],
) -> dict[str, str]:
    """Extract runtime identifiers from an ADK callback context."""
    return _resolve_runtime_context(callback_context)
