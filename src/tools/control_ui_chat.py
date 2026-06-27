"""Deterministic browser operator for the boiled-claw Control UI chat page."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

from google.adk.agents.context import Context as ToolContext

from src.bridges.common_errors import flatten_exception_text
from src.bridges.host_bridge_client import get_host_bridge_client
from src.bridges.host_bridge_exec import execute_host_bridge_call
from src.bridges.host_bridge_schema import (
    HostControlUiChatSendMessageRequest,
)
from src.config.settings import get_settings
from src.tools import browser as browser_tools
from src.tools.context import resolve_tool_context

_CONTROL_UI_INPUT_SELECTOR = "#messageInput"
_CONTROL_UI_SEND_SELECTOR = "#chatForm .send"
_CONTROL_UI_MESSAGES_SELECTOR = "#messages"
_CONTROL_UI_USER_BUBBLES_SELECTOR = "#messages .bubble.user"
_CONTROL_UI_AGENT_BUBBLES_SELECTOR = "#messages .bubble.agent"
_CONTROL_UI_CONNECT_SELECTOR = "#connectBtn"
_CONTROL_UI_STATUS_SELECTOR = "#statusText"
_CONTROL_UI_APPROVE_SELECTOR = "#approvalList .approve-btn"


def _control_ui_error_payload(
    error: str,
    *,
    url: str,
    message: str,
) -> dict[str, Any]:
    return {
        "error": error,
        "url": url,
        "message": message,
        "success": False,
    }


def _validate_control_ui_chat_url(url: str) -> tuple[bool, Optional[str]]:
    valid, reason = browser_tools._validate_url(url)
    if not valid:
        return valid, reason

    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/chat":
        return False, "Control UI chat operator only supports URLs ending with /chat"
    return True, None


def _same_page_url(current: str, target: str) -> bool:
    if not current:
        return False

    current_parsed = urlparse(current)
    target_parsed = urlparse(target)
    return (
        current_parsed.scheme == target_parsed.scheme
        and current_parsed.netloc == target_parsed.netloc
        and (current_parsed.path or "/") == (target_parsed.path or "/")
    )


async def _locator_count(page: Any, selector: str) -> int:
    return await page.locator(selector).count()


async def _locator_text(page: Any, selector: str, *, index: int = 0) -> Optional[str]:
    locator = page.locator(selector)
    count = await locator.count()
    if count <= index:
        return None
    text = await locator.nth(index).inner_text()
    return text.strip()


async def _last_locator_text(page: Any, selector: str) -> Optional[str]:
    locator = page.locator(selector)
    count = await locator.count()
    if count <= 0:
        return None
    text = await locator.nth(count - 1).inner_text()
    return text.strip()


async def _locator_is_enabled(page: Any, selector: str) -> bool:
    locator = page.locator(selector)
    count = await locator.count()
    if count <= 0:
        return False
    return await locator.nth(0).is_enabled()


async def _wait_until(
    predicate,
    *,
    timeout_ms: int,
    interval_ms: int = 200,
    failure_message: str,
):
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(interval_ms / 1000)
    raise RuntimeError(failure_message)


async def _ensure_control_ui_connected(page: Any, connect_timeout_ms: int) -> bool:
    async def status_text() -> Optional[str]:
        return await _locator_text(page, _CONTROL_UI_STATUS_SELECTOR)

    current = await status_text()
    if current == "online":
        return True

    connect_locator = page.locator(_CONTROL_UI_CONNECT_SELECTOR)
    if await connect_locator.count() > 0:
        await connect_locator.nth(0).click(timeout=connect_timeout_ms)

    await _wait_until(
        lambda: _status_is_online(page),
        timeout_ms=connect_timeout_ms,
        failure_message="Control UI did not reach online state within timeout",
    )
    return True


async def _status_is_online(page: Any) -> bool:
    return (await _locator_text(page, _CONTROL_UI_STATUS_SELECTOR)) == "online"


async def _wait_for_input_enabled(page: Any, timeout_ms: int) -> None:
    await _wait_until(
        lambda: _locator_is_enabled(page, _CONTROL_UI_INPUT_SELECTOR),
        timeout_ms=timeout_ms,
        failure_message="Control UI input did not become enabled in time",
    )


async def _wait_for_user_message(page: Any, message: str, timeout_ms: int) -> None:
    async def user_echo() -> bool:
        return (await _last_locator_text(page, _CONTROL_UI_USER_BUBBLES_SELECTOR)) == message

    await _wait_until(
        user_echo,
        timeout_ms=timeout_ms,
        failure_message="Control UI did not echo the submitted user message",
    )


async def _wait_for_assistant_reply(
    page: Any,
    *,
    initial_count: int,
    initial_text: Optional[str],
    timeout_ms: int,
    stable_wait_ms: int,
) -> str:
    stable_text: Optional[str] = None
    stable_since: Optional[float] = None
    deadline = time.monotonic() + (timeout_ms / 1000)

    while time.monotonic() < deadline:
        await _approve_pending_inner_requests(page)

        input_enabled = await _locator_is_enabled(page, _CONTROL_UI_INPUT_SELECTOR)
        current_count = await _locator_count(page, _CONTROL_UI_AGENT_BUBBLES_SELECTOR)
        current_text = await _last_locator_text(page, _CONTROL_UI_AGENT_BUBBLES_SELECTOR)
        changed = bool(current_text) and (
            current_count > initial_count
            or (initial_text is not None and current_text != initial_text)
        )

        if input_enabled and changed:
            if current_text == stable_text:
                if stable_since is not None and (
                    time.monotonic() - stable_since
                ) >= (stable_wait_ms / 1000):
                    return current_text
            else:
                stable_text = current_text
                stable_since = time.monotonic()
        else:
            stable_text = None
            stable_since = None

        await asyncio.sleep(0.2)

    transcript_text = await _locator_text(page, _CONTROL_UI_MESSAGES_SELECTOR)
    raise RuntimeError(
        "Timed out waiting for assistant reply in Control UI chat"
        + (f"; transcript={transcript_text[:240]}" if transcript_text else "")
    )


async def _approve_pending_inner_requests(page: Any) -> None:
    approve_locator = page.locator(_CONTROL_UI_APPROVE_SELECTOR)
    while await approve_locator.count() > 0:
        await approve_locator.nth(0).click(timeout=5000)
        await asyncio.sleep(0.1)


async def _control_ui_chat_send_message_local(
    url: str,
    message: str,
    *,
    timeout_ms: int = 90000,
    connect_timeout_ms: int = 15000,
    stable_wait_ms: int = 800,
    visible: bool = True,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    if not browser_tools.PLAYWRIGHT_AVAILABLE:
        payload = browser_tools._playwright_missing_payload()
        payload.update({"url": url, "message": message})
        browser_tools._audit_browser_event(
            action="control_ui_chat.send_message",
            resource=url,
            result="error",
            metadata={"reason": "playwright_missing"},
            tool_context=tool_context,
        )
        return payload

    valid, reason = _validate_control_ui_chat_url(url)
    if not valid:
        payload = _control_ui_error_payload(f"URL blocked: {reason}", url=url, message=message)
        browser_tools._audit_browser_event(
            action="control_ui_chat.send_message",
            resource=url,
            result=f"blocked:{reason}",
            metadata={"message_length": len(message)},
            tool_context=tool_context,
        )
        return payload

    try:
        session = await browser_tools.get_browser_session(visible=visible)
        page = session.page
        response = None
        if not _same_page_url(getattr(page, "url", ""), url):
            response = await page.goto(url, wait_until="load", timeout=timeout_ms)
        await browser_tools._maybe_activate_visible_browser(session, title=await page.title())

        await _wait_until(
            lambda: _locator_count(page, _CONTROL_UI_INPUT_SELECTOR),
            timeout_ms=timeout_ms,
            failure_message="Control UI chat page did not render the message input",
        )
        connected = await _ensure_control_ui_connected(page, connect_timeout_ms)
        await _wait_for_input_enabled(page, timeout_ms)

        initial_agent_count = await _locator_count(page, _CONTROL_UI_AGENT_BUBBLES_SELECTOR)
        initial_agent_text = await _last_locator_text(page, _CONTROL_UI_AGENT_BUBBLES_SELECTOR)

        input_locator = page.locator(_CONTROL_UI_INPUT_SELECTOR)
        await input_locator.nth(0).fill(message, timeout=timeout_ms)
        send_locator = page.locator(_CONTROL_UI_SEND_SELECTOR)
        await send_locator.nth(0).click(timeout=timeout_ms)

        await _wait_for_user_message(page, message, timeout_ms)
        assistant_reply = await _wait_for_assistant_reply(
            page,
            initial_count=initial_agent_count,
            initial_text=initial_agent_text,
            timeout_ms=timeout_ms,
            stable_wait_ms=stable_wait_ms,
        )

        payload = {
            "url": page.url,
            "title": await page.title(),
            "message": message,
            "assistant_reply": assistant_reply,
            "connected": connected,
            "agent_bubble_count": await _locator_count(page, _CONTROL_UI_AGENT_BUBBLES_SELECTOR),
            "status": response.status if response else None,
            "success": True,
        }
        browser_tools._audit_browser_event(
            action="control_ui_chat.send_message",
            resource=url,
            result="success",
            metadata={
                "message_length": len(message),
                "connected": connected,
                "agent_bubble_count": payload["agent_bubble_count"],
                "visible": visible,
            },
            tool_context=tool_context,
        )
        return payload
    except Exception as exc:
        payload = _control_ui_error_payload(
            flatten_exception_text(exc),
            url=url,
            message=message,
        )
        browser_tools._audit_browser_event(
            action="control_ui_chat.send_message",
            resource=url,
            result=f"error:{exc}",
            metadata={"message_length": len(message)},
            tool_context=tool_context,
        )
        return payload


async def control_ui_chat_send_message(
    url: str,
    message: str,
    timeout_ms: int = 90000,
    connect_timeout_ms: int = 15000,
    stable_wait_ms: int = 800,
    visible: bool = True,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """
    boiled-claw Control UI の /chat ページでメッセージ送信と返信待ちを行う。

    Args:
        url: Control UI chat URL (例: http://localhost:18789/chat)
        message: 送信するメッセージ
        timeout_ms: 返信待ちを含む全体タイムアウト
        connect_timeout_ms: Connect 待ちタイムアウト
        stable_wait_ms: assistant bubble が安定したとみなす待機時間
        visible: true の場合は visible browser window を優先する

    Returns:
        送信結果と assistant reply
    """
    approval_error, approval_token = await browser_tools._check_browser_policy(
        "control_ui_chat_send_message",
        {
            "url": url,
            "message": message,
            "timeout_ms": timeout_ms,
            "connect_timeout_ms": connect_timeout_ms,
            "visible": visible,
        },
        tool_context,
    )
    if approval_error:
        browser_tools._audit_browser_event(
            action="control_ui_chat.send_message",
            resource=url,
            result=approval_error,
            metadata={"message_length": len(message)},
            tool_context=tool_context,
        )
        return _control_ui_error_payload(approval_error, url=url, message=message)

    settings = get_settings()
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    if settings.host_bridge_enabled:
        request = HostControlUiChatSendMessageRequest(
            request_id=f"host-control-ui-chat-{uuid.uuid4().hex[:12]}",
            session_id=ctx.get("session_id") or "standalone-session",
            user_id=ctx.get("user_id") or "standalone-user",
            agent_name=ctx.get("agent_name") or "unknown_agent",
            approval_token=approval_token,
            url=url,
            message=message,
            timeout_ms=timeout_ms,
            connect_timeout_ms=connect_timeout_ms,
            stable_wait_ms=stable_wait_ms,
            visible=visible,
        )
        result, payload = await execute_host_bridge_call(
            request=request,
            tool_name="host.control_ui_chat.send_message",
            args={
                "url": request.url,
                "message": request.message,
                "timeout_ms": request.timeout_ms,
                "connect_timeout_ms": request.connect_timeout_ms,
                "stable_wait_ms": request.stable_wait_ms,
                "visible": request.visible,
            },
            get_client=get_host_bridge_client,
            invoke=lambda client, req: client.send_control_ui_chat_message(req),
            ok_getter=lambda result: result.ok,
            error_payload=lambda error: _control_ui_error_payload(error, url=url, message=message),
            metadata={"executor": "host_bridge"},
        )
        if result is not None:
            browser_tools._audit_browser_event(
                action="control_ui_chat.send_message",
                resource=url,
                result="bridge_success" if result.ok else f"bridge_error:{result.error}",
                metadata={
                    "executor": "host_bridge",
                    "request_id": request.request_id,
                    "message_length": len(message),
                    "visible": request.visible,
                },
                tool_context=tool_context,
            )
            return {
                "url": result.url,
                "title": result.title,
                "message": result.message,
                "assistant_reply": result.assistant_reply,
                "connected": result.connected,
                "agent_bubble_count": result.agent_bubble_count,
                "success": result.ok,
                **({"error": result.error} if result.error else {}),
            }
        browser_tools._audit_browser_event(
            action="control_ui_chat.send_message",
            resource=url,
            result=f"bridge_error:{payload['error']}",
            metadata={
                "executor": "host_bridge",
                "request_id": request.request_id,
                "message_length": len(message),
                "visible": request.visible,
            },
            tool_context=tool_context,
        )
        return payload

    return await _control_ui_chat_send_message_local(
        url,
        message,
        timeout_ms=timeout_ms,
        connect_timeout_ms=connect_timeout_ms,
        stable_wait_ms=stable_wait_ms,
        visible=visible,
        tool_context=tool_context,
    )
