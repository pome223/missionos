"""Current-tab browser tools backed by the host bridge extension relay."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Optional
from urllib.parse import urlparse

from google.adk.agents.context import Context as ToolContext

from src.bridges.host_bridge_client import get_host_bridge_client
from src.bridges.host_bridge_exec import execute_host_bridge_call
from src.bridges.host_bridge_schema import (
    HostCurrentTabClickRequest,
    HostCurrentTabActivateRequest,
    HostCurrentTabListTabsRequest,
    HostCurrentTabExtractTextRequest,
    HostCurrentTabFillRequest,
    HostCurrentTabInfoRequest,
    HostCurrentTabNavigateRequest,
)
from src.config.settings import get_settings
from src.runtime.state_keys import StateKeys
from src.security.tool_policy import get_tool_policy_engine
from src.tools.context import resolve_tool_context


def _current_tab_error_payload(
    error: str,
    *,
    selector: Optional[str] = None,
    url: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": error, "success": False}
    if selector is not None:
        payload["selector"] = selector
    if url is not None:
        payload["url"] = url
    return payload


async def _check_current_tab_policy(
    tool_name: str,
    args: dict[str, Any],
    tool_context: Optional[ToolContext],
) -> tuple[Optional[str], Optional[str]]:
    if tool_context is None:
        return None, None
    ctx = resolve_tool_context(tool_context)
    state = getattr(tool_context, "state", None)
    approval_status = str(_state_get(state, StateKeys.APPROVAL_STATUS, "")).strip()
    approved_plan = _state_get(state, StateKeys.PLAN_APPROVED)
    if approved_plan and (
        approval_status in {"policy_approved", "human_approved", "auto_approved"}
        or ctx.get("agent_name") == "executor"
    ):
        return None, None

    engine = get_tool_policy_engine()
    action, reason = engine.evaluate(ctx["agent_name"], tool_name)
    if action == "allow":
        return None, None
    if action == "deny":
        return f"Tool blocked by policy: {reason}", None

    approved, response_reason, approval_token = await engine.request_approval_with_id(
        tool_name=tool_name,
        agent_name=ctx["agent_name"],
        args=args,
        session_id=ctx["session_id"],
        reason=reason,
    )
    if approved:
        return None, approval_token
    detail = response_reason or reason or "user rejected"
    return f"Tool approval denied: {detail}", approval_token


def _host_bridge_unavailable_error() -> str:
    settings = get_settings()
    if not settings.host_bridge_enabled:
        return "Current Tab browser control requires Host Bridge to reach the host browser."
    return "Host Bridge is enabled but the Current Tab extension relay is unavailable."


def _tool_context_state_value(
    tool_context: Optional[ToolContext],
    key: str,
) -> Any:
    state = getattr(tool_context, "state", None)
    return _state_get(state, key)


def _state_get(
    state: Any,
    key: str,
    default: Any = None,
) -> Any:
    if isinstance(state, Mapping):
        return state.get(key, default)
    getter = getattr(state, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            try:
                return getter(key)
            except Exception:
                return default
    return default


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_current_tab_url(url: str) -> str:
    normalized = str(url or "").strip()
    if not normalized:
        return normalized
    try:
        parsed = urlparse(normalized)
    except Exception:
        return normalized
    if parsed.scheme:
        return normalized
    lowered = normalized.lower()
    if lowered == "sheets.new":
        return "https://sheets.new"
    if lowered.startswith("docs.google.com/spreadsheets"):
        return f"https://{normalized}"
    return normalized


async def current_tab_info(
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_current_tab_policy(
        "current_tab_info",
        {},
        tool_context,
    )
    if approval_error:
        return _current_tab_error_payload(approval_error)

    settings = get_settings()
    if not settings.host_bridge_enabled:
        return _current_tab_error_payload(_host_bridge_unavailable_error())

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = HostCurrentTabInfoRequest(
        request_id=f"host-current-tab-info-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
    )
    result, payload = await execute_host_bridge_call(
        request=request,
        tool_name="host.current_tab.info",
        args={},
        get_client=get_host_bridge_client,
        invoke=lambda client, req: client.current_tab_info(req),
        ok_getter=lambda response: response.ok,
        error_payload=lambda error: _current_tab_error_payload(error),
        metadata={"executor": "host_bridge"},
    )
    if result is None:
        return payload
    return {
        "tab_id": result.tab_id,
        "window_id": result.window_id,
        "url": result.url,
        "title": result.title,
        "success": result.ok,
        **({"error": result.error} if result.error else {}),
    }


async def current_tab_navigate(
    url: str,
    timeout_ms: int = 15000,
    new_tab: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    normalized_url = _normalize_current_tab_url(url)
    approval_error, approval_token = await _check_current_tab_policy(
        "current_tab_navigate",
        {"url": normalized_url, "timeout_ms": timeout_ms},
        tool_context,
    )
    if approval_error:
        return _current_tab_error_payload(approval_error, url=normalized_url)

    settings = get_settings()
    if not settings.host_bridge_enabled:
        return _current_tab_error_payload(_host_bridge_unavailable_error(), url=normalized_url)

    from src.tools.browser import _validate_url

    valid, reason = _validate_url(normalized_url)
    if not valid:
        return _current_tab_error_payload(reason or "Invalid URL", url=normalized_url)

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = HostCurrentTabNavigateRequest(
        request_id=f"host-current-tab-nav-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
        url=normalized_url,
        timeout_ms=timeout_ms,
        new_tab=new_tab,
        target_tab_id=_optional_int(
            _tool_context_state_value(
                tool_context,
                StateKeys.TEMP_CURRENT_BROWSER_ACTIVE_TAB_ID,
            )
        ),
    )
    result, payload = await execute_host_bridge_call(
        request=request,
        tool_name="host.current_tab.navigate",
        args={
            "url": request.url,
            "timeout_ms": request.timeout_ms,
            "new_tab": request.new_tab,
        },
        get_client=get_host_bridge_client,
        invoke=lambda client, req: client.current_tab_navigate(req),
        ok_getter=lambda response: response.ok,
        error_payload=lambda error: _current_tab_error_payload(error, url=normalized_url),
        metadata={"executor": "host_bridge"},
    )
    if result is None:
        return payload
    return {
        "tab_id": result.tab_id,
        "window_id": result.window_id,
        "url": result.url,
        "title": result.title,
        "success": result.ok,
        **({"error": result.error} if result.error else {}),
    }


async def current_tab_list_tabs(
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Read-only enumeration of all open Chrome tabs via the extension relay.

    No side effects on focus or window state. Used by the verification path to
    discover candidate destination tabs (e.g. a Google Sheets tab opened
    earlier) without disturbing the currently-focused Control UI tab.
    """
    approval_error, approval_token = await _check_current_tab_policy(
        "current_tab_list_tabs",
        {},
        tool_context,
    )
    if approval_error:
        return _current_tab_error_payload(approval_error)

    settings = get_settings()
    if not settings.host_bridge_enabled:
        return _current_tab_error_payload(_host_bridge_unavailable_error())

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = HostCurrentTabListTabsRequest(
        request_id=f"host-current-tab-list-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
    )
    result, payload = await execute_host_bridge_call(
        request=request,
        tool_name="host.current_tab.list_tabs",
        args={},
        get_client=get_host_bridge_client,
        invoke=lambda client, req: client.current_tab_list_tabs(req),
        ok_getter=lambda response: response.ok,
        error_payload=lambda error: _current_tab_error_payload(error),
        metadata={"executor": "host_bridge"},
    )
    if result is None:
        return payload
    tabs_dump: list[dict[str, Any]] = []
    for entry in result.tabs:
        tabs_dump.append(
            {
                "tab_id": entry.tab_id,
                "window_id": entry.window_id,
                "url": entry.url,
                "title": entry.title,
                "active": entry.active,
                "index": entry.index,
            }
        )
    return {
        "tabs": tabs_dump,
        "success": result.ok,
        **({"error": result.error} if result.error else {}),
    }


async def current_tab_activate(
    tab_id: int,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_current_tab_policy(
        "current_tab_activate",
        {"tab_id": tab_id},
        tool_context,
    )
    if approval_error:
        return _current_tab_error_payload(approval_error)

    settings = get_settings()
    if not settings.host_bridge_enabled:
        return _current_tab_error_payload(_host_bridge_unavailable_error())

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = HostCurrentTabActivateRequest(
        request_id=f"host-current-tab-activate-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
        tab_id=tab_id,
    )
    result, payload = await execute_host_bridge_call(
        request=request,
        tool_name="host.current_tab.activate",
        args={"tab_id": request.tab_id},
        get_client=get_host_bridge_client,
        invoke=lambda client, req: client.current_tab_activate(req),
        ok_getter=lambda response: response.ok,
        error_payload=lambda error: _current_tab_error_payload(error),
        metadata={"executor": "host_bridge"},
    )
    if result is None:
        return payload
    activate_payload: dict[str, Any] = {
        "tab_id": result.tab_id,
        "window_id": result.window_id,
        "url": result.url,
        "title": result.title,
        "success": result.ok,
    }
    if result.error:
        activate_payload["error"] = result.error
    if getattr(result, "window_focus_error", None):
        activate_payload["window_focus_error"] = result.window_focus_error
    return activate_payload


async def current_tab_click(
    selector: str,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_current_tab_policy(
        "current_tab_click",
        {"selector": selector},
        tool_context,
    )
    if approval_error:
        return _current_tab_error_payload(approval_error, selector=selector)

    settings = get_settings()
    if not settings.host_bridge_enabled:
        return _current_tab_error_payload(_host_bridge_unavailable_error(), selector=selector)

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = HostCurrentTabClickRequest(
        request_id=f"host-current-tab-click-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
        selector=selector,
    )
    result, payload = await execute_host_bridge_call(
        request=request,
        tool_name="host.current_tab.click",
        args={"selector": request.selector},
        get_client=get_host_bridge_client,
        invoke=lambda client, req: client.current_tab_click(req),
        ok_getter=lambda response: response.ok,
        error_payload=lambda error: _current_tab_error_payload(error, selector=selector),
        metadata={"executor": "host_bridge"},
    )
    if result is None:
        return payload
    return {
        "selector": result.selector,
        "success": result.ok,
        **({"error": result.error} if result.error else {}),
    }


async def current_tab_fill(
    selector: str,
    text: str,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_current_tab_policy(
        "current_tab_fill",
        {"selector": selector, "text": text},
        tool_context,
    )
    if approval_error:
        return _current_tab_error_payload(approval_error, selector=selector)

    settings = get_settings()
    if not settings.host_bridge_enabled:
        return _current_tab_error_payload(_host_bridge_unavailable_error(), selector=selector)

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = HostCurrentTabFillRequest(
        request_id=f"host-current-tab-fill-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
        selector=selector,
        text=text,
    )
    result, payload = await execute_host_bridge_call(
        request=request,
        tool_name="host.current_tab.fill",
        args={"selector": request.selector, "text": request.text},
        get_client=get_host_bridge_client,
        invoke=lambda client, req: client.current_tab_fill(req),
        ok_getter=lambda response: response.ok,
        error_payload=lambda error: _current_tab_error_payload(error, selector=selector),
        metadata={"executor": "host_bridge"},
    )
    if result is None:
        return payload
    return {
        "selector": result.selector,
        "text_length": result.text_length,
        "success": result.ok,
        **({"error": result.error} if result.error else {}),
    }


async def current_tab_extract_text(
    selector: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_current_tab_policy(
        "current_tab_extract_text",
        {"selector": selector},
        tool_context,
    )
    if approval_error:
        return _current_tab_error_payload(approval_error, selector=selector)

    settings = get_settings()
    if not settings.host_bridge_enabled:
        return _current_tab_error_payload(_host_bridge_unavailable_error(), selector=selector)

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    request = HostCurrentTabExtractTextRequest(
        request_id=f"host-current-tab-text-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
        selector=selector,
        target_tab_id=_optional_int(
            _tool_context_state_value(
                tool_context,
                StateKeys.TEMP_CURRENT_BROWSER_ACTIVE_TAB_ID,
            )
        ),
    )
    result, payload = await execute_host_bridge_call(
        request=request,
        tool_name="host.current_tab.extract_text",
        args={"selector": request.selector},
        get_client=get_host_bridge_client,
        invoke=lambda client, req: client.current_tab_extract_text(req),
        ok_getter=lambda response: response.ok,
        error_payload=lambda error: _current_tab_error_payload(error, selector=selector),
        metadata={"executor": "host_bridge"},
    )
    if result is None:
        return payload
    return {
        "text": result.text,
        "selector": result.selector,
        "length": result.length,
        "success": result.ok,
        **({"error": result.error} if result.error else {}),
    }
