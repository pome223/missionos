"""Desktop automation tools."""

from __future__ import annotations

from collections.abc import Callable
import uuid
from typing import Any, Optional, TypeVar

from google.adk.agents.context import Context as ToolContext

from src.bridges.desktop_bridge_client import get_desktop_client
from src.bridges.desktop_exec import execute_desktop_call
from src.config.settings import get_settings
from src.desktop import (
    DesktopAxFindRequest,
    DesktopAxSnapshotRequest,
    DesktopClearStopRequest,
    DesktopClickRequest,
    DesktopEmergencyStopRequest,
    DesktopElementSelector,
    DesktopFocusWindowRequest,
    DesktopFrontmostAppRequest,
    DesktopHotkeyRequest,
    DesktopLaunchAppRequest,
    DesktopRuntimeStatusRequest,
    DesktopScrollRequest,
    DesktopScreenshotRequest,
    DesktopTypeRequest,
    DesktopDragRequest,
    DesktopWaitElementRequest,
    DesktopWaitWindowRequest,
    DesktopWindowsRequest,
)
from src.security.audit import AuditEventType, get_audit_logger
from src.security.tool_policy import get_tool_policy_engine
from src.tools.context import resolve_tool_context

ResultT = TypeVar("ResultT")


async def _check_desktop_policy(
    tool_name: str,
    args: dict[str, Any],
    tool_context: Optional[ToolContext],
) -> tuple[Optional[str], Optional[str]]:
    if tool_context is None:
        return None, None

    ctx = resolve_tool_context(tool_context)
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


def _audit_desktop_event(
    *,
    event_type: AuditEventType,
    action: str,
    resource: str,
    result: str,
    metadata: dict[str, Any],
    tool_context: Optional[ToolContext],
) -> None:
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    get_audit_logger().log(
        event_type=event_type,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action=action,
        resource=resource,
        result=result,
        metadata=metadata,
    )


def _executor_metadata() -> dict[str, Any]:
    settings = get_settings()
    return {
        "executor": "desktop_bridge" if settings.desktop_bridge_enabled else "local_desktop",
    }


def _selector_from_fields(
    *,
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
    index: int = 0,
) -> DesktopElementSelector | None:
    if not any((window_id, role, title, identifier, value_contains)):
        return None
    return DesktopElementSelector(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )


def _tool_context_values(tool_context: Optional[ToolContext]) -> dict[str, str]:
    return resolve_tool_context(tool_context) if tool_context is not None else {}


def _build_request(
    request_cls: Callable[..., Any],
    prefix: str,
    tool_context: Optional[ToolContext],
    *,
    approval_token: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    ctx = _tool_context_values(tool_context)
    return request_cls(
        request_id=f"{prefix}-{uuid.uuid4().hex[:12]}",
        session_id=ctx.get("session_id") or "standalone-session",
        user_id=ctx.get("user_id") or "standalone-user",
        agent_name=ctx.get("agent_name") or "unknown_agent",
        approval_token=approval_token,
        **kwargs,
    )


def _resolve_dynamic(
    value: str | dict[str, Any] | Callable[[Any], str | dict[str, Any]] | None,
    result: Any,
) -> str | dict[str, Any] | None:
    if callable(value):
        try:
            return value(result)
        except Exception:
            return None
    return value


async def _run_desktop_tool(
    *,
    request: Any,
    tool_name: str,
    args: dict[str, Any],
    invoke: Callable[[Any, Any], Any],
    event_type: AuditEventType,
    action: str,
    success_resource: str | Callable[[Any], str],
    tool_context: Optional[ToolContext],
    success_response: Callable[[ResultT], dict[str, Any]],
    default_error: str,
    extra_metadata: dict[str, Any] | Callable[[Any], dict[str, Any]] | None = None,
    error_resource: str | Callable[[Any], str] | None = None,
    error_response: dict[str, Any] | Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    execution_metadata = _executor_metadata()
    result, payload = await execute_desktop_call(
        request=request,
        tool_name=tool_name,
        args=args,
        get_client=get_desktop_client,
        invoke=invoke,
        ok_getter=lambda result: result.ok,
        error_payload=lambda error: {"error": error},
        metadata=execution_metadata,
    )

    audit_metadata = {**execution_metadata, "request_id": request.request_id}
    resolved_metadata = _resolve_dynamic(extra_metadata, result)
    if isinstance(resolved_metadata, dict):
        audit_metadata.update(resolved_metadata)

    if result is None or not result.ok:
        error = (getattr(result, "error", None) if result else payload.get("error")) or default_error
        resource = _resolve_dynamic(error_resource or success_resource, result) or "desktop"
        _audit_desktop_event(
            event_type=event_type,
            action=action,
            resource=str(resource),
            result=error,
            metadata=audit_metadata,
            tool_context=tool_context,
        )
        response = {"error": error}
        resolved_error_response = _resolve_dynamic(error_response, result)
        if isinstance(resolved_error_response, dict):
            response.update(resolved_error_response)
        return response

    resource = _resolve_dynamic(success_resource, result) or "desktop"
    _audit_desktop_event(
        event_type=event_type,
        action=action,
        resource=str(resource),
        result="success",
        metadata=audit_metadata,
        tool_context=tool_context,
    )
    return success_response(result)


async def desktop_view_windows(
    include_minimized: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    request = _build_request(
        DesktopWindowsRequest,
        "desktop-windows",
        tool_context,
        include_minimized=include_minimized,
    )
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.view.windows",
        args={"include_minimized": include_minimized},
        invoke=lambda client, req: client.windows(req),
        event_type=AuditEventType.DESKTOP_VIEW,
        action="windows",
        success_resource="desktop",
        tool_context=tool_context,
        success_response=lambda result: {
            "windows": [window.model_dump() for window in result.windows]
        },
        default_error="desktop query failed",
        extra_metadata=lambda result: {"count": len(result.windows)} if result else {},
    )


async def desktop_runtime_status(
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    request = _build_request(
        DesktopRuntimeStatusRequest,
        "desktop-runtime-status",
        tool_context,
    )
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.runtime.status",
        args={},
        invoke=lambda client, req: client.runtime_status(req),
        event_type=AuditEventType.DESKTOP_VIEW,
        action="runtime_status",
        success_resource="desktop_runtime",
        tool_context=tool_context,
        success_response=lambda result: {
            "stopped": result.stopped,
            "reason": result.reason,
            "stopped_at": result.stopped_at,
        },
        default_error="desktop runtime status failed",
    )


async def desktop_runtime_stop(
    reason: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    request = _build_request(
        DesktopEmergencyStopRequest,
        "desktop-runtime-stop",
        tool_context,
        reason=reason,
    )
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.runtime.stop",
        args={"reason": reason},
        invoke=lambda client, req: client.emergency_stop(req),
        event_type=AuditEventType.DESKTOP_CONTROL,
        action="runtime_stop",
        success_resource="desktop_runtime",
        tool_context=tool_context,
        success_response=lambda result: {
            "success": True,
            "stopped": result.stopped,
            "reason": result.reason,
            "stopped_at": result.stopped_at,
        },
        default_error="desktop emergency stop failed",
        extra_metadata=lambda result: {"reason": result.reason} if result else {},
    )


async def desktop_runtime_clear_stop(
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_runtime_clear_stop",
        {},
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    request = _build_request(
        DesktopClearStopRequest,
        "desktop-runtime-clear",
        tool_context,
        approval_token=approval_token,
    )
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.runtime.clear_stop",
        args={},
        invoke=lambda client, req: client.clear_stop(req),
        event_type=AuditEventType.DESKTOP_CONTROL,
        action="runtime_clear_stop",
        success_resource="desktop_runtime",
        tool_context=tool_context,
        success_response=lambda result: {
            "success": True,
            "stopped": result.stopped,
            "reason": result.reason,
            "stopped_at": result.stopped_at,
        },
        default_error="desktop clear stop failed",
        extra_metadata={"approval_token": approval_token},
    )


async def desktop_wait_window(
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    title: Optional[str] = None,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.2,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    request = _build_request(
        DesktopWaitWindowRequest,
        "desktop-wait-window",
        tool_context,
        app_name=app_name,
        window_id=window_id,
        title=title,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    resource = title or window_id or app_name or "desktop"
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.wait.window",
        args={
            "app_name": app_name,
            "window_id": window_id,
            "title": title,
            "timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval_seconds,
        },
        invoke=lambda client, req: client.wait_window(req),
        event_type=AuditEventType.DESKTOP_VIEW,
        action="wait_window",
        success_resource=resource,
        tool_context=tool_context,
        success_response=lambda result: {
            "matched": result.matched,
            "window": result.window.model_dump() if result.window else None,
        },
        default_error="desktop wait window failed",
        extra_metadata=lambda result: {"matched": result.matched} if result else {},
    )


async def desktop_view_frontmost_app(
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    request = _build_request(
        DesktopFrontmostAppRequest,
        "desktop-frontmost",
        tool_context,
    )
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.view.frontmost_app",
        args={},
        invoke=lambda client, req: client.frontmost_app(req),
        event_type=AuditEventType.DESKTOP_VIEW,
        action="frontmost_app",
        success_resource=lambda result: result.app_name or "desktop",
        tool_context=tool_context,
        success_response=lambda result: {"app_name": result.app_name, "pid": result.pid},
        default_error="desktop query failed",
        extra_metadata=lambda result: {"pid": result.pid} if result else {},
    )


async def desktop_view_screenshot(
    path: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_view_screenshot",
        {"path": path},
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    request = _build_request(
        DesktopScreenshotRequest,
        "desktop-shot",
        tool_context,
        approval_token=approval_token,
        path=path,
    )
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.view.screenshot",
        args={"path": path},
        invoke=lambda client, req: client.screenshot(req),
        event_type=AuditEventType.DESKTOP_VIEW,
        action="screenshot",
        success_resource=lambda result: result.path or "desktop",
        tool_context=tool_context,
        success_response=lambda result: {
            "path": result.path,
            "width": result.width,
            "height": result.height,
            "success": True,
        },
        default_error="desktop screenshot failed",
        extra_metadata=lambda result: {
            "approval_token": approval_token,
            "width": result.width,
            "height": result.height,
        }
        if result
        else {"approval_token": approval_token},
        error_resource=path or "desktop",
        error_response={"path": path},
    )


async def desktop_ax_snapshot(
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_ax_snapshot",
        {"app_name": app_name, "window_id": window_id},
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    request = _build_request(
        DesktopAxSnapshotRequest,
        "desktop-ax",
        tool_context,
        approval_token=approval_token,
        app_name=app_name,
        window_id=window_id,
    )
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.ax.snapshot",
        args={"app_name": app_name, "window_id": window_id},
        invoke=lambda client, req: client.ax_snapshot(req),
        event_type=AuditEventType.DESKTOP_VIEW,
        action="ax_snapshot",
        success_resource=app_name or "desktop",
        tool_context=tool_context,
        success_response=lambda result: {"tree": result.tree},
        default_error="desktop ax snapshot failed",
        extra_metadata={"approval_token": approval_token, "window_id": window_id},
    )


async def desktop_ax_find(
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
    index: int = 0,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    selector = _selector_from_fields(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )
    if selector is None:
        return {"error": "desktop ax find requires a selector target"}
    request = _build_request(
        DesktopAxFindRequest,
        "desktop-find",
        tool_context,
        target=selector,
    )
    resource = app_name or window_id or identifier or title or "desktop"
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.ax.find",
        args={"selector": selector.model_dump(exclude_none=True)},
        invoke=lambda client, req: client.ax_find(req),
        event_type=AuditEventType.DESKTOP_VIEW,
        action="ax_find",
        success_resource=resource,
        tool_context=tool_context,
        success_response=lambda result: {
            "matched": result.matched,
            "target": result.target.model_dump() if result.target else None,
        },
        default_error="desktop ax find failed",
        extra_metadata=lambda result: {"matched": result.matched} if result else {},
    )


async def desktop_wait_element(
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
    index: int = 0,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.2,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    selector = _selector_from_fields(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )
    if selector is None:
        return {"error": "desktop wait element requires a selector target"}

    request = _build_request(
        DesktopWaitElementRequest,
        "desktop-wait-element",
        tool_context,
        target=selector,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    resource = app_name or window_id or identifier or title or "desktop"
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.wait.element",
        args={
            "selector": selector.model_dump(exclude_none=True),
            "timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval_seconds,
        },
        invoke=lambda client, req: client.wait_element(req),
        event_type=AuditEventType.DESKTOP_VIEW,
        action="wait_element",
        success_resource=resource,
        tool_context=tool_context,
        success_response=lambda result: {
            "matched": result.matched,
            "target": result.target.model_dump() if result.target else None,
        },
        default_error="desktop wait element failed",
        extra_metadata=lambda result: {"matched": result.matched} if result else {},
    )


async def desktop_control_click(
    x: Optional[int] = None,
    y: Optional[int] = None,
    button: str = "left",
    click_count: int = 1,
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
    index: int = 0,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    selector = _selector_from_fields(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )
    if selector is None and (x is None or y is None):
        return {"error": "desktop click requires coordinates or a selector target"}
    selector_payload = selector.model_dump(exclude_none=True) if selector else None
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_control_click",
        {
            "x": x,
            "y": y,
            "button": button,
            "click_count": click_count,
            "selector": selector_payload,
        },
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    request = _build_request(
        DesktopClickRequest,
        "desktop-click",
        tool_context,
        approval_token=approval_token,
        x=x,
        y=y,
        button=button,
        click_count=click_count,
        target=selector,
    )
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.control.click",
        args={
            "x": x,
            "y": y,
            "button": button,
            "click_count": click_count,
            "selector": selector_payload,
        },
        invoke=lambda client, req: client.click(req),
        event_type=AuditEventType.DESKTOP_CONTROL,
        action="click",
        success_resource=f"{x},{y}",
        tool_context=tool_context,
        success_response=lambda result: {
            "success": True,
            "target": result.target.model_dump() if result.target else None,
        },
        default_error="desktop click failed",
        extra_metadata={
            "approval_token": approval_token,
            "button": button,
            "click_count": click_count,
            "selector": selector_payload,
        },
    )


async def desktop_control_type(
    text: str,
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    role: Optional[str] = None,
    title: Optional[str] = None,
    identifier: Optional[str] = None,
    value_contains: Optional[str] = None,
    index: int = 0,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    selector = _selector_from_fields(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )
    selector_payload = selector.model_dump(exclude_none=True) if selector else None
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_control_type",
        {
            "text": text,
            "selector": selector_payload,
        },
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    request = _build_request(
        DesktopTypeRequest,
        "desktop-type",
        tool_context,
        approval_token=approval_token,
        text=text,
        target=selector,
    )
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.control.type",
        args={
            "text": text,
            "selector": selector_payload,
        },
        invoke=lambda client, req: client.type_text(req),
        event_type=AuditEventType.DESKTOP_CONTROL,
        action="type",
        success_resource="desktop",
        tool_context=tool_context,
        success_response=lambda result: {
            "success": True,
            "target": result.target.model_dump() if result.target else None,
        },
        default_error="desktop type failed",
        extra_metadata={
            "approval_token": approval_token,
            "length": len(text),
            "selector": selector_payload,
        },
    )


async def desktop_control_launch_app(
    app_name: Optional[str] = None,
    bundle_id: Optional[str] = None,
    wait_for_focus: bool = True,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_control_launch_app",
        {
            "app_name": app_name,
            "bundle_id": bundle_id,
            "wait_for_focus": wait_for_focus,
        },
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    request = _build_request(
        DesktopLaunchAppRequest,
        "desktop-launch",
        tool_context,
        approval_token=approval_token,
        app_name=app_name,
        bundle_id=bundle_id,
        wait_for_focus=wait_for_focus,
    )
    resource = app_name or bundle_id or "desktop"
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.control.launch_app",
        args={
            "app_name": app_name,
            "bundle_id": bundle_id,
            "wait_for_focus": wait_for_focus,
        },
        invoke=lambda client, req: client.launch_app(req),
        event_type=AuditEventType.DESKTOP_CONTROL,
        action="launch_app",
        success_resource=resource,
        tool_context=tool_context,
        success_response=lambda result: {
            "success": True,
            "target": result.target.model_dump() if result.target else None,
        },
        default_error="desktop launch failed",
        extra_metadata={
            "approval_token": approval_token,
            "wait_for_focus": wait_for_focus,
        },
    )


async def desktop_control_focus_window(
    app_name: Optional[str] = None,
    window_id: Optional[str] = None,
    title: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_control_focus_window",
        {"app_name": app_name, "window_id": window_id, "title": title},
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    request = _build_request(
        DesktopFocusWindowRequest,
        "desktop-focus",
        tool_context,
        approval_token=approval_token,
        app_name=app_name,
        window_id=window_id,
        title=title,
    )
    resource = title or window_id or app_name or "desktop"
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.control.focus_window",
        args={"app_name": app_name, "window_id": window_id, "title": title},
        invoke=lambda client, req: client.focus_window(req),
        event_type=AuditEventType.DESKTOP_CONTROL,
        action="focus_window",
        success_resource=resource,
        tool_context=tool_context,
        success_response=lambda result: {
            "success": True,
            "target": result.target.model_dump() if result.target else None,
        },
        default_error="desktop focus failed",
        extra_metadata={"approval_token": approval_token},
    )


async def desktop_control_hotkey(
    keys: list[str],
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_control_hotkey",
        {"keys": keys},
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    request = _build_request(
        DesktopHotkeyRequest,
        "desktop-hotkey",
        tool_context,
        approval_token=approval_token,
        keys=keys,
    )
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.control.hotkey",
        args={"keys": keys},
        invoke=lambda client, req: client.hotkey(req),
        event_type=AuditEventType.DESKTOP_CONTROL,
        action="hotkey",
        success_resource="desktop",
        tool_context=tool_context,
        success_response=lambda result: {"success": True},
        default_error="desktop hotkey failed",
        extra_metadata={"approval_token": approval_token, "keys": keys},
    )


async def desktop_control_scroll(
    delta_x: int = 0,
    delta_y: int = 0,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_control_scroll",
        {"delta_x": delta_x, "delta_y": delta_y},
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    request = _build_request(
        DesktopScrollRequest,
        "desktop-scroll",
        tool_context,
        approval_token=approval_token,
        delta_x=delta_x,
        delta_y=delta_y,
    )
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.control.scroll",
        args={"delta_x": delta_x, "delta_y": delta_y},
        invoke=lambda client, req: client.scroll(req),
        event_type=AuditEventType.DESKTOP_CONTROL,
        action="scroll",
        success_resource="desktop",
        tool_context=tool_context,
        success_response=lambda result: {"success": True},
        default_error="desktop scroll failed",
        extra_metadata={
            "approval_token": approval_token,
            "delta_x": delta_x,
            "delta_y": delta_y,
        },
    )


async def desktop_control_drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    approval_error, approval_token = await _check_desktop_policy(
        "desktop_control_drag",
        {
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
        },
        tool_context,
    )
    if approval_error:
        return {"error": approval_error}

    request = _build_request(
        DesktopDragRequest,
        "desktop-drag",
        tool_context,
        approval_token=approval_token,
        start_x=start_x,
        start_y=start_y,
        end_x=end_x,
        end_y=end_y,
    )
    return await _run_desktop_tool(
        request=request,
        tool_name="desktop.control.drag",
        args={
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
        },
        invoke=lambda client, req: client.drag(req),
        event_type=AuditEventType.DESKTOP_CONTROL,
        action="drag",
        success_resource=f"{start_x},{start_y}->{end_x},{end_y}",
        tool_context=tool_context,
        success_response=lambda result: {"success": True},
        default_error="desktop drag failed",
        extra_metadata={"approval_token": approval_token},
    )
