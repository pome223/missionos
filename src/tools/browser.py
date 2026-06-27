"""
ブラウザ自動化ツール - Playwright
OpenClaw のブラウザ自動化機能を参考
"""

import asyncio
import ipaddress
import uuid
from typing import Optional, Dict, Any, Tuple
from pathlib import Path
from urllib.parse import urlparse

from google.adk.agents.context import Context as ToolContext

from src.bridges.host_bridge_client import get_host_bridge_client
from src.bridges.host_bridge_exec import execute_host_bridge_call
from src.bridges.host_bridge_schema import (
    HostBrowserClickRequest,
    HostBrowserExtractTextRequest,
    HostBrowserFillRequest,
    HostBrowserNavigateRequest,
    HostBrowserPressRequest,
    HostBrowserScreenshotRequest,
)
from src.config.settings import get_settings
from src.security.audit import AuditEventType, get_audit_logger
from src.security.tool_policy import get_tool_policy_engine
from src.tools.context import resolve_tool_context

# Playwright は遅延インポート (インストールされていない場合のエラー回避)
playwright = None
async_playwright = None

try:
    from playwright.async_api import async_playwright, Browser, Page
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class BrowserSession:
    """ブラウザセッション管理"""

    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self.headless = True

    async def start(self, headless: bool = True):
        """ブラウザセッションを開始"""
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install playwright && playwright install"
            )

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=headless)
        self.page = await self.browser.new_page()
        self.headless = headless

    @property
    def is_alive(self) -> bool:
        """ブラウザセッションが利用可能かチェック"""
        try:
            return (
                self.browser is not None
                and self.browser.is_connected()
                and self.page is not None
                and not self.page.is_closed()
            )
        except Exception:
            return False

    async def close(self):
        """ブラウザセッションを閉じる"""
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        self.page = None
        self.browser = None
        self.playwright = None


_ALLOWED_SCHEMES = {"http", "https"}
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}


def _audit_browser_event(
    *,
    action: str,
    resource: str,
    result: str,
    metadata: Dict[str, Any],
    tool_context: Optional[ToolContext],
) -> None:
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    get_audit_logger().log(
        event_type=AuditEventType.BROWSER_NAVIGATE,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action=action,
        resource=resource,
        result=result,
        metadata=metadata,
    )


def _validate_url(url: str) -> Tuple[bool, Optional[str]]:
    """URL の安全性を検証する (SSRF 対策)"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL"

    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False, f"Scheme '{parsed.scheme}' is not allowed (only http/https)"

    hostname = parsed.hostname
    if not hostname:
        return False, "Missing hostname"

    settings = get_settings()
    allow_loopback = getattr(settings, "browser_allow_loopback", False)

    if hostname.lower() in _BLOCKED_HOSTS and not allow_loopback:
        return False, f"Access to '{hostname}' is blocked"

    try:
        ip = ipaddress.ip_address(hostname)
        for network in _PRIVATE_NETWORKS:
            if ip in network:
                if allow_loopback and ip.is_loopback:
                    return True, None
                return False, f"Access to private/loopback address {ip} is blocked"
    except ValueError:
        pass  # ホスト名（IP でない）は IP チェックをスキップ

    return True, None


# グローバルセッション (再利用のため)
_browser_session: Optional[BrowserSession] = None


async def get_browser_session(*, visible: Optional[bool] = None) -> BrowserSession:
    """ブラウザセッションを取得"""
    global _browser_session
    settings = get_settings()
    desired_headless = settings.browser_headless if visible is None else (not visible)

    if _browser_session is not None and not _browser_session.is_alive:
        await _browser_session.close()
        _browser_session = None

    if (
        _browser_session is not None
        and visible is not None
        and _browser_session.headless != desired_headless
    ):
        await _browser_session.close()
        _browser_session = None

    if _browser_session is None:
        _browser_session = BrowserSession()
        await _browser_session.start(headless=desired_headless)
    return _browser_session


async def _maybe_activate_visible_browser(
    session: BrowserSession,
    *,
    title: Optional[str] = None,
) -> None:
    if getattr(session, "headless", True) or session.page is None:
        return

    try:
        await session.page.bring_to_front()
    except Exception:
        pass

    try:
        from src.bridges.desktop_bridge_client import get_desktop_client
        from src.desktop import DesktopFocusWindowRequest
    except Exception:
        return

    try:
        desktop_client = get_desktop_client()
        request_kwargs = {
            "request_id": f"browser-focus-{uuid.uuid4().hex[:12]}",
            "session_id": "browser-visible-session",
            "user_id": "browser-visible-user",
            "agent_name": "browser_runtime",
        }
        if title:
            result = await desktop_client.focus_window(
                DesktopFocusWindowRequest(title=title, **request_kwargs)
            )
            if result.ok:
                return
        await desktop_client.focus_window(
            DesktopFocusWindowRequest(app_name="Chromium", **request_kwargs)
        )
    except Exception:
        return


async def _check_browser_policy(
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


def _playwright_missing_payload() -> dict[str, Any]:
    return {
        "error": "Playwright is not installed. Run: pip install playwright && playwright install",
        "success": False,
    }


def _default_screenshot_path() -> str:
    screenshots_dir = Path("data/screenshots")
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    import time

    return str(screenshots_dir / f"screenshot_{int(time.time())}.png")


def _bridge_browser_error_payload(
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


async def _browser_navigate_local(
    url: str,
    wait_for: str = "load",
    timeout: int = 30000,
    visible: Optional[bool] = None,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    if not PLAYWRIGHT_AVAILABLE:
        payload = _playwright_missing_payload()
        _audit_browser_event(
            action="navigate",
            resource=url,
            result="error",
            metadata={"reason": "playwright_missing"},
            tool_context=tool_context,
        )
        return payload

    valid, reason = _validate_url(url)
    if not valid:
        payload = {"error": f"URL blocked: {reason}", "url": url, "success": False}
        _audit_browser_event(
            action="navigate",
            resource=url,
            result=f"blocked:{reason}",
            metadata={"wait_for": wait_for, "timeout": timeout},
            tool_context=tool_context,
        )
        return payload

    try:
        session = await get_browser_session(visible=visible)
        page = session.page
        response = await page.goto(url, wait_until=wait_for, timeout=timeout)
        payload = {
            "url": page.url,
            "title": await page.title(),
            "status": response.status if response else None,
            "success": True,
        }
        await _maybe_activate_visible_browser(session, title=payload["title"])
        _audit_browser_event(
            action="navigate",
            resource=url,
            result="success",
            metadata={
                "status": payload["status"],
                "title": payload["title"],
                "visible": not session.headless,
            },
            tool_context=tool_context,
        )
        return payload
    except Exception as e:
        payload = {"error": str(e), "url": url, "success": False}
        _audit_browser_event(
            action="navigate",
            resource=url,
            result=f"error:{e}",
            metadata={"wait_for": wait_for, "timeout": timeout},
            tool_context=tool_context,
        )
        return payload


async def _browser_screenshot_local(
    path: Optional[str] = None,
    full_page: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    if not PLAYWRIGHT_AVAILABLE:
        payload = _playwright_missing_payload()
        _audit_browser_event(
            action="screenshot",
            resource=path or "",
            result="error",
            metadata={"reason": "playwright_missing"},
            tool_context=tool_context,
        )
        return payload

    try:
        session = await get_browser_session()
        page = session.page
        screenshot_path = path or _default_screenshot_path()
        await page.screenshot(path=screenshot_path, full_page=full_page)
        payload = {
            "path": screenshot_path,
            "full_page": full_page,
            "success": True,
        }
        _audit_browser_event(
            action="screenshot",
            resource=screenshot_path,
            result="success",
            metadata={"full_page": full_page},
            tool_context=tool_context,
        )
        return payload
    except Exception as e:
        payload = {"error": str(e), "success": False}
        _audit_browser_event(
            action="screenshot",
            resource=path or "",
            result=f"error:{e}",
            metadata={"full_page": full_page},
            tool_context=tool_context,
        )
        return payload


async def _browser_extract_text_local(
    selector: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    if not PLAYWRIGHT_AVAILABLE:
        payload = _playwright_missing_payload()
        _audit_browser_event(
            action="extract_text",
            resource=selector or "body",
            result="error",
            metadata={"reason": "playwright_missing"},
            tool_context=tool_context,
        )
        return payload

    try:
        session = await get_browser_session()
        page = session.page
        if selector:
            element = await page.query_selector(selector)
            if element:
                text = await element.inner_text()
            else:
                payload = {"error": f"Element not found: {selector}", "success": False}
                _audit_browser_event(
                    action="extract_text",
                    resource=selector,
                    result="not_found",
                    metadata={},
                    tool_context=tool_context,
                )
                return payload
        else:
            text = await page.inner_text("body")

        payload = {
            "text": text,
            "selector": selector or "body",
            "length": len(text),
            "success": True,
        }
        _audit_browser_event(
            action="extract_text",
            resource=payload["selector"],
            result="success",
            metadata={"length": payload["length"]},
            tool_context=tool_context,
        )
        return payload
    except Exception as e:
        payload = {"error": str(e), "success": False}
        _audit_browser_event(
            action="extract_text",
            resource=selector or "body",
            result=f"error:{e}",
            metadata={},
            tool_context=tool_context,
        )
        return payload


async def _browser_click_local(
    selector: str,
    timeout: int = 30000,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    if not PLAYWRIGHT_AVAILABLE:
        payload = _playwright_missing_payload()
        _audit_browser_event(
            action="click",
            resource=selector,
            result="error",
            metadata={"reason": "playwright_missing"},
            tool_context=tool_context,
        )
        return payload

    try:
        session = await get_browser_session()
        page = session.page
        await page.click(selector, timeout=timeout)
        payload = {"selector": selector, "success": True}
        _audit_browser_event(
            action="click",
            resource=selector,
            result="success",
            metadata={"timeout": timeout},
            tool_context=tool_context,
        )
        return payload
    except Exception as e:
        payload = {"error": str(e), "selector": selector, "success": False}
        _audit_browser_event(
            action="click",
            resource=selector,
            result=f"error:{e}",
            metadata={"timeout": timeout},
            tool_context=tool_context,
        )
        return payload


async def _browser_fill_local(
    selector: str,
    text: str,
    timeout: int = 30000,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    if not PLAYWRIGHT_AVAILABLE:
        payload = _playwright_missing_payload()
        _audit_browser_event(
            action="fill",
            resource=selector,
            result="error",
            metadata={"reason": "playwright_missing"},
            tool_context=tool_context,
        )
        return payload

    try:
        session = await get_browser_session()
        page = session.page
        await page.fill(selector, text, timeout=timeout)
        payload = {
            "selector": selector,
            "text_length": len(text),
            "success": True,
        }
        _audit_browser_event(
            action="fill",
            resource=selector,
            result="success",
            metadata={"timeout": timeout, "text_length": len(text)},
            tool_context=tool_context,
        )
        return payload
    except Exception as e:
        payload = {
            "error": str(e),
            "selector": selector,
            "text_length": len(text),
            "success": False,
        }
        _audit_browser_event(
            action="fill",
            resource=selector,
            result=f"error:{e}",
            metadata={"timeout": timeout, "text_length": len(text)},
            tool_context=tool_context,
        )
        return payload


async def _browser_press_local(
    key: str,
    selector: Optional[str] = None,
    timeout: int = 30000,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    if not PLAYWRIGHT_AVAILABLE:
        payload = _playwright_missing_payload()
        _audit_browser_event(
            action="press",
            resource=selector or key,
            result="error",
            metadata={"reason": "playwright_missing"},
            tool_context=tool_context,
        )
        return payload

    try:
        session = await get_browser_session()
        page = session.page
        if selector:
            await page.press(selector, key, timeout=timeout)
        else:
            await page.keyboard.press(key)
        payload = {
            "key": key,
            "selector": selector,
            "success": True,
        }
        _audit_browser_event(
            action="press",
            resource=selector or key,
            result="success",
            metadata={"timeout": timeout, "key": key},
            tool_context=tool_context,
        )
        return payload
    except Exception as e:
        payload = {
            "error": str(e),
            "key": key,
            "selector": selector,
            "success": False,
        }
        _audit_browser_event(
            action="press",
            resource=selector or key,
            result=f"error:{e}",
            metadata={"timeout": timeout, "key": key},
            tool_context=tool_context,
        )
        return payload


async def browser_navigate(
    url: str,
    wait_for: str = "load",
    timeout: int = 30000,
    visible: Optional[bool] = None,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    URLに移動してページを読み込む

    Args:
        url: 移動先URL
        wait_for: 待機イベント ('load', 'domcontentloaded', 'networkidle')
        timeout: タイムアウト (ミリ秒)
        visible: true の場合は visible browser window を優先する

    Returns:
        ページ情報 (title, url, status)
    """
    approval_error, approval_token = await _check_browser_policy(
        "browser_navigate",
        {"url": url, "wait_for": wait_for, "timeout": timeout, "visible": visible},
        tool_context,
    )
    if approval_error:
        _audit_browser_event(
            action="navigate",
            resource=url,
            result=approval_error,
            metadata={"wait_for": wait_for, "timeout": timeout},
            tool_context=tool_context,
        )
        return {"error": approval_error, "url": url, "success": False}

    settings = get_settings()
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    if settings.host_bridge_enabled:
        request = HostBrowserNavigateRequest(
            request_id=f"host-browser-nav-{uuid.uuid4().hex[:12]}",
            session_id=ctx.get("session_id") or "standalone-session",
            user_id=ctx.get("user_id") or "standalone-user",
            agent_name=ctx.get("agent_name") or "unknown_agent",
            approval_token=approval_token,
            url=url,
            wait_for=wait_for,
            timeout=timeout,
            visible=visible,
        )
        result, payload = await execute_host_bridge_call(
            request=request,
            tool_name="host.browser.navigate",
            args={
                "url": request.url,
                "wait_for": request.wait_for,
                "timeout": request.timeout,
                "visible": request.visible,
            },
            get_client=get_host_bridge_client,
            invoke=lambda client, req: client.navigate_browser(req),
            ok_getter=lambda result: result.ok,
            error_payload=lambda error: _bridge_browser_error_payload(error, url=url),
            metadata={"executor": "host_bridge"},
        )
        if result is not None:
            _audit_browser_event(
                action="navigate",
                resource=url,
                result="bridge_success" if result.ok else f"bridge_error:{result.error}",
                metadata={
                    "executor": "host_bridge",
                    "request_id": request.request_id,
                    "approval_token": approval_token,
                    "visible": request.visible,
                },
                tool_context=tool_context,
            )
            return {
                "url": result.url,
                "title": result.title,
                "status": result.status,
                "success": result.ok,
                **({"error": result.error} if result.error else {}),
            }
        _audit_browser_event(
            action="navigate",
            resource=url,
            result=f"bridge_error:{payload['error']}",
            metadata={
                "executor": "host_bridge",
                "request_id": request.request_id,
                "approval_token": approval_token,
                "visible": request.visible,
            },
            tool_context=tool_context,
        )
        return payload

    return await _browser_navigate_local(
        url,
        wait_for,
        timeout,
        visible=visible,
        tool_context=tool_context,
    )


async def browser_screenshot(
    path: Optional[str] = None,
    full_page: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    現在のページのスクリーンショットを撮る

    Args:
        path: 保存先パス (指定しない場合は自動生成)
        full_page: ページ全体をキャプチャ

    Returns:
        スクリーンショット情報
    """
    approval_error, approval_token = await _check_browser_policy(
        "browser_screenshot",
        {"path": path, "full_page": full_page},
        tool_context,
    )
    if approval_error:
        _audit_browser_event(
            action="screenshot",
            resource=path or "",
            result=approval_error,
            metadata={"full_page": full_page},
            tool_context=tool_context,
        )
        return {"error": approval_error, "success": False}

    settings = get_settings()
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    if settings.host_bridge_enabled:
        request = HostBrowserScreenshotRequest(
            request_id=f"host-browser-shot-{uuid.uuid4().hex[:12]}",
            session_id=ctx.get("session_id") or "standalone-session",
            user_id=ctx.get("user_id") or "standalone-user",
            agent_name=ctx.get("agent_name") or "unknown_agent",
            approval_token=approval_token,
            path=path,
            full_page=full_page,
        )
        result, payload = await execute_host_bridge_call(
            request=request,
            tool_name="host.browser.screenshot",
            args={"path": request.path, "full_page": request.full_page},
            get_client=get_host_bridge_client,
            invoke=lambda client, req: client.screenshot_browser(req),
            ok_getter=lambda result: result.ok,
            error_payload=lambda error: _bridge_browser_error_payload(error),
            metadata={"executor": "host_bridge"},
        )
        if result is not None:
            _audit_browser_event(
                action="screenshot",
                resource=result.path or path or "",
                result="bridge_success" if result.ok else f"bridge_error:{result.error}",
                metadata={
                    "executor": "host_bridge",
                    "request_id": request.request_id,
                    "approval_token": approval_token,
                    "full_page": full_page,
                },
                tool_context=tool_context,
            )
            return {
                "path": result.path,
                "full_page": result.full_page,
                "success": result.ok,
                **({"error": result.error} if result.error else {}),
            }
        _audit_browser_event(
            action="screenshot",
            resource=path or "",
            result=f"bridge_error:{payload['error']}",
            metadata={
                "executor": "host_bridge",
                "request_id": request.request_id,
                "approval_token": approval_token,
                "full_page": full_page,
            },
            tool_context=tool_context,
        )
        return payload

    return await _browser_screenshot_local(path, full_page, tool_context)


async def browser_extract_text(
    selector: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    ページからテキストを抽出する

    Args:
        selector: CSSセレクタ (指定しない場合はbody全体)

    Returns:
        抽出されたテキスト
    """
    approval_error, approval_token = await _check_browser_policy(
        "browser_extract_text",
        {"selector": selector},
        tool_context,
    )
    if approval_error:
        _audit_browser_event(
            action="extract_text",
            resource=selector or "body",
            result=approval_error,
            metadata={},
            tool_context=tool_context,
        )
        return {"error": approval_error, "success": False}

    settings = get_settings()
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    if settings.host_bridge_enabled:
        request = HostBrowserExtractTextRequest(
            request_id=f"host-browser-text-{uuid.uuid4().hex[:12]}",
            session_id=ctx.get("session_id") or "standalone-session",
            user_id=ctx.get("user_id") or "standalone-user",
            agent_name=ctx.get("agent_name") or "unknown_agent",
            approval_token=approval_token,
            selector=selector,
        )
        result, payload = await execute_host_bridge_call(
            request=request,
            tool_name="host.browser.extract_text",
            args={"selector": request.selector},
            get_client=get_host_bridge_client,
            invoke=lambda client, req: client.extract_browser_text(req),
            ok_getter=lambda result: result.ok,
            error_payload=lambda error: _bridge_browser_error_payload(
                error,
                selector=selector or "body",
            ),
            metadata={"executor": "host_bridge"},
        )
        if result is not None:
            _audit_browser_event(
                action="extract_text",
                resource=result.selector,
                result="bridge_success" if result.ok else f"bridge_error:{result.error}",
                metadata={
                    "executor": "host_bridge",
                    "request_id": request.request_id,
                    "approval_token": approval_token,
                    "length": result.length,
                },
                tool_context=tool_context,
            )
            return {
                "text": result.text,
                "selector": result.selector,
                "length": result.length,
                "success": result.ok,
                **({"error": result.error} if result.error else {}),
            }
        _audit_browser_event(
            action="extract_text",
            resource=selector or "body",
            result=f"bridge_error:{payload['error']}",
            metadata={
                "executor": "host_bridge",
                "request_id": request.request_id,
                "approval_token": approval_token,
            },
            tool_context=tool_context,
        )
        return payload

    return await _browser_extract_text_local(selector, tool_context)


async def browser_click(
    selector: str,
    timeout: int = 30000,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    要素をクリックする

    Args:
        selector: CSSセレクタ
        timeout: タイムアウト (ミリ秒)

    Returns:
        クリック結果
    """
    approval_error, approval_token = await _check_browser_policy(
        "browser_click",
        {"selector": selector, "timeout": timeout},
        tool_context,
    )
    if approval_error:
        _audit_browser_event(
            action="click",
            resource=selector,
            result=approval_error,
            metadata={"timeout": timeout},
            tool_context=tool_context,
        )
        return {"error": approval_error, "selector": selector, "success": False}

    settings = get_settings()
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    if settings.host_bridge_enabled:
        request = HostBrowserClickRequest(
            request_id=f"host-browser-click-{uuid.uuid4().hex[:12]}",
            session_id=ctx.get("session_id") or "standalone-session",
            user_id=ctx.get("user_id") or "standalone-user",
            agent_name=ctx.get("agent_name") or "unknown_agent",
            approval_token=approval_token,
            selector=selector,
            timeout=timeout,
        )
        result, payload = await execute_host_bridge_call(
            request=request,
            tool_name="host.browser.click",
            args={"selector": request.selector, "timeout": request.timeout},
            get_client=get_host_bridge_client,
            invoke=lambda client, req: client.click_browser(req),
            ok_getter=lambda result: result.ok,
            error_payload=lambda error: _bridge_browser_error_payload(error, selector=selector),
            metadata={"executor": "host_bridge"},
        )
        if result is not None:
            return {
                "selector": result.selector or selector,
                "success": result.ok,
                **({"error": result.error} if result.error else {}),
            }
        return payload

    return await _browser_click_local(selector, timeout, tool_context)


async def browser_fill(
    selector: str,
    text: str,
    timeout: int = 30000,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    フォーム入力フィールドに入力する

    Args:
        selector: CSSセレクタ
        text: 入力テキスト
        timeout: タイムアウト (ミリ秒)

    Returns:
        入力結果
    """
    approval_error, approval_token = await _check_browser_policy(
        "browser_fill",
        {"selector": selector, "text": text, "timeout": timeout},
        tool_context,
    )
    if approval_error:
        _audit_browser_event(
            action="fill",
            resource=selector,
            result=approval_error,
            metadata={"timeout": timeout, "text_length": len(text)},
            tool_context=tool_context,
        )
        return {"error": approval_error, "selector": selector, "success": False}

    settings = get_settings()
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    if settings.host_bridge_enabled:
        request = HostBrowserFillRequest(
            request_id=f"host-browser-fill-{uuid.uuid4().hex[:12]}",
            session_id=ctx.get("session_id") or "standalone-session",
            user_id=ctx.get("user_id") or "standalone-user",
            agent_name=ctx.get("agent_name") or "unknown_agent",
            approval_token=approval_token,
            selector=selector,
            text=text,
            timeout=timeout,
        )
        result, payload = await execute_host_bridge_call(
            request=request,
            tool_name="host.browser.fill",
            args={
                "selector": request.selector,
                "text": request.text,
                "timeout": request.timeout,
            },
            get_client=get_host_bridge_client,
            invoke=lambda client, req: client.fill_browser(req),
            ok_getter=lambda result: result.ok,
            error_payload=lambda error: _bridge_browser_error_payload(error, selector=selector),
            metadata={"executor": "host_bridge"},
        )
        if result is not None:
            return {
                "selector": result.selector or selector,
                "text_length": result.text_length,
                "success": result.ok,
                **({"error": result.error} if result.error else {}),
            }
        return payload

    return await _browser_fill_local(selector, text, timeout, tool_context)


async def browser_press(
    key: str,
    selector: Optional[str] = None,
    timeout: int = 30000,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    キー入力を送る

    Args:
        key: 送信するキー名 (例: Enter, Escape)
        selector: 指定時は対象要素に対して送る
        timeout: タイムアウト (ミリ秒)

    Returns:
        入力結果
    """
    approval_error, approval_token = await _check_browser_policy(
        "browser_press",
        {"key": key, "selector": selector, "timeout": timeout},
        tool_context,
    )
    if approval_error:
        _audit_browser_event(
            action="press",
            resource=selector or key,
            result=approval_error,
            metadata={"timeout": timeout, "key": key},
            tool_context=tool_context,
        )
        return {"error": approval_error, "key": key, "selector": selector, "success": False}

    settings = get_settings()
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    if settings.host_bridge_enabled:
        request = HostBrowserPressRequest(
            request_id=f"host-browser-press-{uuid.uuid4().hex[:12]}",
            session_id=ctx.get("session_id") or "standalone-session",
            user_id=ctx.get("user_id") or "standalone-user",
            agent_name=ctx.get("agent_name") or "unknown_agent",
            approval_token=approval_token,
            key=key,
            selector=selector,
            timeout=timeout,
        )
        result, payload = await execute_host_bridge_call(
            request=request,
            tool_name="host.browser.press",
            args={
                "key": request.key,
                "selector": request.selector,
                "timeout": request.timeout,
            },
            get_client=get_host_bridge_client,
            invoke=lambda client, req: client.press_browser(req),
            ok_getter=lambda result: result.ok,
            error_payload=lambda error: _bridge_browser_error_payload(error, selector=selector),
            metadata={"executor": "host_bridge"},
        )
        if result is not None:
            return {
                "key": result.key or key,
                "selector": result.selector if result.selector is not None else selector,
                "success": result.ok,
                **({"error": result.error} if result.error else {}),
            }
        return payload

    return await _browser_press_local(
        key,
        selector=selector,
        timeout=timeout,
        tool_context=tool_context,
    )
