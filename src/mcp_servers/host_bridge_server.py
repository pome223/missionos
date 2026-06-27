"""
Host Bridge MCP server.

Host OS capability surface for boiled-claw.

v1 tools:
  - ping
  - capabilities.list
  - host.shell.run

起動方法:
  python -m src.mcp_servers.host_bridge_server
  python -m src.mcp_servers.host_bridge_server --sse --host 127.0.0.1 --port 8766
"""

import argparse
import asyncio
import logging
from pathlib import Path
import subprocess
from typing import Optional

from src.browser.current_tab_bridge import (
    CurrentTabBridgeError,
    current_tab_bridge_enabled,
    get_current_tab_extension_bridge,
)
from src.config.settings import get_settings
from src.bridges.host_bridge_schema import (
    BridgePingResult,
    CapabilityDescriptor,
    CapabilityListResult,
    HostControlUiChatSendMessageRequest,
    HostControlUiChatSendMessageResult,
    HostBrowserClickRequest,
    HostBrowserClickResult,
    HostBrowserExtractTextRequest,
    HostBrowserExtractTextResult,
    HostBrowserFillRequest,
    HostBrowserFillResult,
    HostBrowserNavigateRequest,
    HostBrowserNavigateResult,
    HostBrowserPressRequest,
    HostBrowserPressResult,
    HostBrowserScreenshotRequest,
    HostBrowserScreenshotResult,
    HostFileListRequest,
    HostFileListResult,
    HostFileReadRequest,
    HostFileReadResult,
    HostFileWriteRequest,
    HostFileWriteResult,
    HostCurrentTabClickRequest,
    HostCurrentTabClickResult,
    HostCurrentTabActivateRequest,
    HostCurrentTabActivateResult,
    HostCurrentTabListTabsRequest,
    HostCurrentTabListTabsResult,
    HostCurrentTabExtractTextRequest,
    HostCurrentTabExtractTextResult,
    HostCurrentTabFillRequest,
    HostCurrentTabFillResult,
    HostCurrentTabInfoRequest,
    HostCurrentTabInfoResult,
    HostCurrentTabNavigateRequest,
    HostCurrentTabNavigateResult,
    HostShellRunRequest,
    HostShellRunResult,
)
from src.security.policy import get_security_policy
from src.security.shell_intent import inspect_shell_command
from src.security.network import enforce_loopback_bind, is_loopback_host
from src.tools import browser as browser_tools
from src.tools import control_ui_chat as control_ui_chat_tools

logger = logging.getLogger(__name__)


# Best-effort guard only. The actual security boundary is policy.is_command_allowed()
# above; wrappers like `bash -c ...` can bypass executable-name checks.
_BLOCKED_EXECUTABLES = {
    "rm", "shred", "mkfs", "fdisk", "dd", "wipefs",
    "truncate", "srm", "secure-delete",
}


def _normalize_browser_payload(payload: dict, *, default_selector: str | None = None) -> dict:
    normalized = dict(payload)
    if "success" in normalized and "ok" not in normalized:
        normalized["ok"] = normalized.pop("success")
    if default_selector is not None and "selector" not in normalized:
        normalized["selector"] = default_selector
    return normalized


def _current_tab_error_payload(error: str) -> dict[str, object]:
    return {"ok": False, "error": error}


async def _ensure_current_tab_bridge_ready() -> None:
    if not current_tab_bridge_enabled():
        raise CurrentTabBridgeError(
            "Current Tab extension bridge is disabled. Set CURRENT_TAB_BRIDGE_ENABLED=true."
        )
    bridge = get_current_tab_extension_bridge()
    await bridge.ensure_started()


async def _current_tab_info_payload() -> dict[str, object]:
    await _ensure_current_tab_bridge_ready()
    bridge = get_current_tab_extension_bridge()
    payload = await bridge.call("get_active_tab")
    return {
        "ok": True,
        "tab_id": payload.get("tab_id"),
        "window_id": payload.get("window_id"),
        "url": payload.get("url", ""),
        "title": payload.get("title", ""),
    }


async def _current_tab_navigate_payload(request: HostCurrentTabNavigateRequest) -> dict[str, object]:
    await _ensure_current_tab_bridge_ready()
    bridge = get_current_tab_extension_bridge()
    payload = await bridge.call(
        "navigate",
        {
            "url": request.url,
            "timeout_ms": request.timeout_ms,
            "new_tab": request.new_tab,
            "target_tab_id": request.target_tab_id,
        },
        timeout_seconds=max(5.0, request.timeout_ms / 1000 + 2.0),
    )
    return {
        "ok": True,
        "tab_id": payload.get("tab_id"),
        "window_id": payload.get("window_id"),
        "url": payload.get("url", ""),
        "title": payload.get("title", ""),
    }


async def _current_tab_list_tabs_payload(
    request: HostCurrentTabListTabsRequest,
) -> dict[str, object]:
    await _ensure_current_tab_bridge_ready()
    bridge = get_current_tab_extension_bridge()
    payload = await bridge.call("list_tabs", {})
    raw_tabs = payload.get("tabs") or []
    tabs: list[dict[str, object]] = []
    for entry in raw_tabs:
        if not isinstance(entry, dict):
            continue
        tabs.append(
            {
                "tab_id": entry.get("tab_id"),
                "window_id": entry.get("window_id"),
                "url": entry.get("url", ""),
                "title": entry.get("title", ""),
                "active": bool(entry.get("active", False)),
                "index": entry.get("index"),
            }
        )
    return {"ok": True, "tabs": tabs}


async def _current_tab_activate_payload(
    request: HostCurrentTabActivateRequest,
) -> dict[str, object]:
    await _ensure_current_tab_bridge_ready()
    bridge = get_current_tab_extension_bridge()
    payload = await bridge.call("activate_tab", {"tab_id": request.tab_id})
    result: dict[str, object] = {
        "ok": True,
        "tab_id": payload.get("tab_id"),
        "window_id": payload.get("window_id"),
        "url": payload.get("url", ""),
        "title": payload.get("title", ""),
    }
    window_focus_error = payload.get("window_focus_error")
    if window_focus_error:
        result["window_focus_error"] = str(window_focus_error)
    return result


async def _current_tab_click_payload(request: HostCurrentTabClickRequest) -> dict[str, object]:
    await _ensure_current_tab_bridge_ready()
    bridge = get_current_tab_extension_bridge()
    payload = await bridge.call("click", {"selector": request.selector})
    return {"ok": True, "selector": payload.get("selector", request.selector)}


async def _current_tab_fill_payload(request: HostCurrentTabFillRequest) -> dict[str, object]:
    await _ensure_current_tab_bridge_ready()
    bridge = get_current_tab_extension_bridge()
    payload = await bridge.call("fill", {"selector": request.selector, "text": request.text})
    return {
        "ok": True,
        "selector": payload.get("selector", request.selector),
        "text_length": int(payload.get("text_length", len(request.text))),
    }


async def _current_tab_extract_text_payload(
    request: HostCurrentTabExtractTextRequest,
) -> dict[str, object]:
    await _ensure_current_tab_bridge_ready()
    bridge = get_current_tab_extension_bridge()
    payload = await bridge.call(
        "extract_text",
        {
            "selector": request.selector,
            "target_tab_id": request.target_tab_id,
        },
    )
    return {
        "ok": True,
        "selector": payload.get("selector", request.selector or "body"),
        "text": payload.get("text", ""),
        "length": int(payload.get("length", len(str(payload.get("text", ""))))),
    }


def _run_host_shell(request: HostShellRunRequest) -> HostShellRunResult:
    try:
        inspection = inspect_shell_command(request.command)
    except ValueError as exc:
        return HostShellRunResult(
            ok=False,
            error=f"Invalid command syntax: {exc}",
            return_code=-1,
        )

    normalized = inspection.normalized

    policy = get_security_policy()
    allowed, reason = policy.is_command_allowed(normalized, inspection=inspection)
    if not allowed:
        return HostShellRunResult(
            ok=False,
            error=f"Command blocked by security policy: {reason}",
            return_code=-1,
        )

    tokens = inspection.ast.exec_tokens

    executable = inspection.ast.executable_basename or tokens[0].lstrip("./").split("/")[-1]
    if executable in _BLOCKED_EXECUTABLES:
        return HostShellRunResult(
            ok=False,
            error=f"Executable '{executable}' is blocked for safety.",
            return_code=-1,
        )

    try:
        completed = subprocess.run(
            tokens,
            cwd=request.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=request.timeout_seconds,
            check=False,
        )
        return HostShellRunResult(
            ok=completed.returncode == 0,
            stdout=completed.stdout.decode("utf-8", errors="replace"),
            stderr=completed.stderr.decode("utf-8", errors="replace"),
            return_code=completed.returncode,
            intent=inspection.intent.category,
            risk=inspection.intent.risk,
            summary=inspection.intent.summary,
        )
    except subprocess.TimeoutExpired:
        return HostShellRunResult(
            ok=False,
            error=f"Command timed out after {request.timeout_seconds} seconds",
            return_code=-1,
            timed_out=True,
        )
    except FileNotFoundError:
        return HostShellRunResult(
            ok=False,
            error=f"Command not found: {tokens[0]}",
            return_code=-1,
        )
    except Exception as exc:
        return HostShellRunResult(
            ok=False,
            error=str(exc),
            return_code=-1,
        )


def _capabilities() -> CapabilityListResult:
    return CapabilityListResult(
        capabilities=[
            CapabilityDescriptor(
                name="host.shell.run",
                risk="medium",
                requires_approval=True,
                description="Run a guarded shell command on the host OS.",
            ),
            CapabilityDescriptor(
                name="host.file.read",
                risk="low",
                requires_approval=False,
                description="Read a guarded file from the host OS.",
            ),
            CapabilityDescriptor(
                name="host.file.write",
                risk="medium",
                requires_approval=True,
                description="Write a guarded file on the host OS.",
            ),
            CapabilityDescriptor(
                name="host.file.list",
                risk="low",
                requires_approval=False,
                description="List a guarded directory on the host OS.",
            ),
            CapabilityDescriptor(
                name="host.browser.navigate",
                risk="medium",
                requires_approval=True,
                description="Navigate the host browser to a guarded URL, optionally in a visible window.",
                implemented=browser_tools.PLAYWRIGHT_AVAILABLE,
            ),
            CapabilityDescriptor(
                name="host.browser.extract_text",
                risk="medium",
                requires_approval=True,
                description="Extract text from the current host browser page.",
                implemented=browser_tools.PLAYWRIGHT_AVAILABLE,
            ),
            CapabilityDescriptor(
                name="host.browser.click",
                risk="medium",
                requires_approval=True,
                description="Click an element in the current host browser page.",
                implemented=browser_tools.PLAYWRIGHT_AVAILABLE,
            ),
            CapabilityDescriptor(
                name="host.browser.fill",
                risk="medium",
                requires_approval=True,
                description="Fill a form field in the current host browser page.",
                implemented=browser_tools.PLAYWRIGHT_AVAILABLE,
            ),
            CapabilityDescriptor(
                name="host.browser.press",
                risk="medium",
                requires_approval=True,
                description="Send a key press to the current host browser page.",
                implemented=browser_tools.PLAYWRIGHT_AVAILABLE,
            ),
            CapabilityDescriptor(
                name="host.browser.screenshot",
                risk="medium",
                requires_approval=True,
                description="Capture a screenshot from the host browser page.",
                implemented=browser_tools.PLAYWRIGHT_AVAILABLE,
            ),
            CapabilityDescriptor(
                name="host.control_ui_chat.send_message",
                risk="medium",
                requires_approval=True,
                description="Use the boiled-claw Control UI chat with deterministic selectors, optionally in a visible window.",
                implemented=browser_tools.PLAYWRIGHT_AVAILABLE,
            ),
            CapabilityDescriptor(
                name="host.current_tab.info",
                risk="low",
                requires_approval=False,
                description="Inspect the active Chrome tab through the current-tab extension relay.",
                implemented=current_tab_bridge_enabled(),
            ),
            CapabilityDescriptor(
                name="host.current_tab.navigate",
                risk="medium",
                requires_approval=True,
                description="Navigate the active Chrome tab through the current-tab extension relay.",
                implemented=current_tab_bridge_enabled(),
            ),
            CapabilityDescriptor(
                name="host.current_tab.list_tabs",
                risk="low",
                requires_approval=False,
                description="Enumerate open Chrome tabs via the current-tab extension relay (read-only).",
                implemented=current_tab_bridge_enabled(),
            ),
            CapabilityDescriptor(
                name="host.current_tab.activate",
                risk="medium",
                requires_approval=True,
                description="Activate a specific Chrome tab through the current-tab extension relay.",
                implemented=current_tab_bridge_enabled(),
            ),
            CapabilityDescriptor(
                name="host.current_tab.click",
                risk="medium",
                requires_approval=True,
                description="Click a selector inside the active Chrome tab through the current-tab extension relay.",
                implemented=current_tab_bridge_enabled(),
            ),
            CapabilityDescriptor(
                name="host.current_tab.fill",
                risk="medium",
                requires_approval=True,
                description="Fill a selector inside the active Chrome tab through the current-tab extension relay.",
                implemented=current_tab_bridge_enabled(),
            ),
            CapabilityDescriptor(
                name="host.current_tab.extract_text",
                risk="medium",
                requires_approval=True,
                description="Extract text from the active Chrome tab through the current-tab extension relay.",
                implemented=current_tab_bridge_enabled(),
            ),
        ]
    )


def _read_host_file(request: HostFileReadRequest) -> HostFileReadResult:
    policy = get_security_policy()
    allowed, reason = policy.is_path_allowed(request.path, "read")
    if not allowed:
        return HostFileReadResult(ok=False, error=f"Access denied: {reason}")

    try:
        file_path = Path(request.path).expanduser().resolve()
        content = file_path.read_text(encoding="utf-8")
        return HostFileReadResult(
            ok=True,
            path=str(file_path),
            content=content,
            size=len(content),
        )
    except FileNotFoundError:
        return HostFileReadResult(ok=False, error=f"File not found: {request.path}")
    except PermissionError:
        return HostFileReadResult(ok=False, error=f"Permission denied: {request.path}")
    except Exception as exc:
        return HostFileReadResult(ok=False, error=str(exc))


def _write_host_file(request: HostFileWriteRequest) -> HostFileWriteResult:
    policy = get_security_policy()
    allowed, reason = policy.is_path_allowed(request.path, "write")
    if not allowed:
        return HostFileWriteResult(ok=False, error=f"Access denied: {reason}")

    content_allowed, content_reason = policy.validate_file_content(request.content, request.path)
    if not content_allowed:
        return HostFileWriteResult(
            ok=False,
            error=f"Content blocked by security policy: {content_reason}",
        )

    try:
        file_path = Path(request.path).expanduser().resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(request.content, encoding="utf-8")
        return HostFileWriteResult(
            ok=True,
            path=str(file_path),
            size=len(request.content),
        )
    except PermissionError:
        return HostFileWriteResult(ok=False, error=f"Permission denied: {request.path}")
    except Exception as exc:
        return HostFileWriteResult(ok=False, error=str(exc))


def _list_host_files(request: HostFileListRequest) -> HostFileListResult:
    policy = get_security_policy()
    allowed, reason = policy.is_path_allowed(request.path, "read")
    if not allowed:
        return HostFileListResult(ok=False, error=f"Access denied: {reason}")

    try:
        dir_path = Path(request.path).expanduser().resolve()
        entries = []
        for entry in sorted(dir_path.iterdir(), key=lambda item: item.name.lower()):
            try:
                stat = entry.stat()
                size = stat.st_size if entry.is_file() else 0
            except OSError:
                size = 0
            entries.append({
                "name": entry.name,
                "path": str(entry),
                "is_dir": entry.is_dir(),
                "size": size,
            })

        return HostFileListResult(ok=True, path=str(dir_path), entries=entries)
    except FileNotFoundError:
        return HostFileListResult(ok=False, error=f"File not found: {request.path}")
    except NotADirectoryError:
        return HostFileListResult(ok=False, error=f"Not a directory: {request.path}")
    except PermissionError:
        return HostFileListResult(ok=False, error=f"Permission denied: {request.path}")
    except Exception as exc:
        return HostFileListResult(ok=False, error=str(exc))


def create_server(host: str = "127.0.0.1", port: int = 8766):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import TransportSecuritySettings
    settings = get_settings()

    enforce_loopback_bind(
        host,
        service_name="Host Bridge",
        allow_remote_bind=settings.bridge_allow_remote_bind,
    )

    if is_loopback_host(host):
        # Dockerized gateway clients reach the host-bound bridge via
        # host.docker.internal while staying local to the developer machine.
        allowed_loopback_hosts = [
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            "host.docker.internal:*",
        ]
        allowed_loopback_origins = [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
            "http://host.docker.internal:*",
        ]
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_loopback_hosts,
            allowed_origins=allowed_loopback_origins,
        )
    else:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )

    mcp = FastMCP("host-bridge")
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.transport_security = transport_security

    if host != "stdio" and current_tab_bridge_enabled():
        original_sse_app = mcp.sse_app

        def sse_app_with_runtime_startup(mount_path: str | None = None):
            app = original_sse_app(mount_path)

            async def _startup_current_tab_relay() -> None:
                bridge = get_current_tab_extension_bridge()
                await bridge.ensure_started()
                logger.info("Current Tab relay listening on %s", bridge.ws_url)

            app.router.on_startup.append(_startup_current_tab_relay)
            return app

        mcp.sse_app = sse_app_with_runtime_startup

    transport_hint = "sse" if host != "stdio" else "stdio"

    @mcp.tool(name="ping", description="Host Bridge health probe.")
    def ping() -> dict:
        return BridgePingResult(
            service="host-bridge",
            version="v1",
            transport=transport_hint,
        ).model_dump()

    @mcp.tool(name="capabilities.list", description="List implemented host capabilities.")
    def list_capabilities() -> dict:
        return _capabilities().model_dump()

    @mcp.tool(name="host.shell.run", description="Run a guarded shell command on the host.")
    def host_shell_run(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        command: str,
        timeout_seconds: int = 30,
        cwd: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostShellRunRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            command=command,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
        )
        return _run_host_shell(request).model_dump()

    @mcp.tool(name="host.file.read", description="Read a guarded file on the host.")
    def host_file_read(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        path: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostFileReadRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            path=path,
        )
        return _read_host_file(request).model_dump()

    @mcp.tool(name="host.file.write", description="Write a guarded file on the host.")
    def host_file_write(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        path: str,
        content: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostFileWriteRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            path=path,
            content=content,
        )
        return _write_host_file(request).model_dump()

    @mcp.tool(name="host.file.list", description="List a guarded directory on the host.")
    def host_file_list(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        path: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostFileListRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            path=path,
        )
        return _list_host_files(request).model_dump()

    @mcp.tool(name="host.browser.navigate", description="Navigate the host browser to a guarded URL.")
    async def host_browser_navigate(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        url: str,
        wait_for: str = "load",
        timeout: int = 30000,
        visible: Optional[bool] = None,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostBrowserNavigateRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            url=url,
            wait_for=wait_for,
            timeout=timeout,
            visible=visible,
        )
        payload = await browser_tools._browser_navigate_local(
            request.url,
            wait_for=request.wait_for,
            timeout=request.timeout,
            visible=request.visible,
            tool_context=None,
        )
        return HostBrowserNavigateResult.model_validate(
            _normalize_browser_payload(payload)
        ).model_dump()

    @mcp.tool(
        name="host.browser.extract_text",
        description="Extract text from the current host browser page.",
    )
    async def host_browser_extract_text(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        selector: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostBrowserExtractTextRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            selector=selector,
        )
        payload = await browser_tools._browser_extract_text_local(
            request.selector,
            tool_context=None,
        )
        return HostBrowserExtractTextResult.model_validate(
            _normalize_browser_payload(payload, default_selector=request.selector or "body")
        ).model_dump()

    @mcp.tool(
        name="host.browser.click",
        description="Click an element in the current host browser page.",
    )
    async def host_browser_click(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        selector: str,
        timeout: int = 30000,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostBrowserClickRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            selector=selector,
            timeout=timeout,
        )
        payload = await browser_tools._browser_click_local(
            request.selector,
            timeout=request.timeout,
            tool_context=None,
        )
        return HostBrowserClickResult.model_validate(
            _normalize_browser_payload(payload, default_selector=request.selector)
        ).model_dump()

    @mcp.tool(
        name="host.browser.fill",
        description="Fill a form field in the current host browser page.",
    )
    async def host_browser_fill(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        selector: str,
        text: str,
        timeout: int = 30000,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostBrowserFillRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            selector=selector,
            text=text,
            timeout=timeout,
        )
        payload = await browser_tools._browser_fill_local(
            request.selector,
            request.text,
            timeout=request.timeout,
            tool_context=None,
        )
        return HostBrowserFillResult.model_validate(
            _normalize_browser_payload(payload, default_selector=request.selector)
        ).model_dump()

    @mcp.tool(
        name="host.browser.press",
        description="Send a key press to the current host browser page.",
    )
    async def host_browser_press(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        key: str,
        selector: Optional[str] = None,
        timeout: int = 30000,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostBrowserPressRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            key=key,
            selector=selector,
            timeout=timeout,
        )
        payload = await browser_tools._browser_press_local(
            request.key,
            selector=request.selector,
            timeout=request.timeout,
            tool_context=None,
        )
        return HostBrowserPressResult.model_validate(
            _normalize_browser_payload(payload, default_selector=request.selector)
        ).model_dump()

    @mcp.tool(
        name="host.browser.screenshot",
        description="Capture a screenshot from the current host browser page.",
    )
    async def host_browser_screenshot(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        path: Optional[str] = None,
        full_page: bool = False,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostBrowserScreenshotRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            path=path,
            full_page=full_page,
        )
        payload = await browser_tools._browser_screenshot_local(
            request.path,
            full_page=request.full_page,
            tool_context=None,
        )
        return HostBrowserScreenshotResult.model_validate(
            _normalize_browser_payload(payload)
        ).model_dump()

    @mcp.tool(
        name="host.control_ui_chat.send_message",
        description="Send a message through the boiled-claw Control UI chat and wait for the assistant reply.",
    )
    async def host_control_ui_chat_send_message(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        url: str,
        message: str,
        timeout_ms: int = 90000,
        connect_timeout_ms: int = 15000,
        stable_wait_ms: int = 800,
        visible: bool = True,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostControlUiChatSendMessageRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            url=url,
            message=message,
            timeout_ms=timeout_ms,
            connect_timeout_ms=connect_timeout_ms,
            stable_wait_ms=stable_wait_ms,
            visible=visible,
        )
        payload = await control_ui_chat_tools._control_ui_chat_send_message_local(
            request.url,
            request.message,
            timeout_ms=request.timeout_ms,
            connect_timeout_ms=request.connect_timeout_ms,
            stable_wait_ms=request.stable_wait_ms,
            visible=request.visible,
            tool_context=None,
        )
        return HostControlUiChatSendMessageResult.model_validate(
            {
                "ok": payload.get("success", False),
                "url": payload.get("url"),
                "title": payload.get("title", ""),
                "message": payload.get("message", ""),
                "assistant_reply": payload.get("assistant_reply", ""),
                "connected": payload.get("connected", False),
                "agent_bubble_count": payload.get("agent_bubble_count", 0),
                "error": payload.get("error"),
            }
        ).model_dump()

    @mcp.tool(
        name="host.current_tab.info",
        description="Inspect the active Chrome tab through the current-tab extension relay.",
    )
    async def host_current_tab_info(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostCurrentTabInfoRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
        )
        try:
            payload = await _current_tab_info_payload()
        except CurrentTabBridgeError as exc:
            payload = _current_tab_error_payload(str(exc))
        return HostCurrentTabInfoResult.model_validate(payload).model_dump()

    @mcp.tool(
        name="host.current_tab.navigate",
        description="Navigate the active Chrome tab through the current-tab extension relay.",
    )
    async def host_current_tab_navigate(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        url: str,
        timeout_ms: int = 15000,
        new_tab: bool = False,
        target_tab_id: Optional[int] = None,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostCurrentTabNavigateRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            url=url,
            timeout_ms=timeout_ms,
            new_tab=new_tab,
            target_tab_id=target_tab_id,
        )
        try:
            payload = await _current_tab_navigate_payload(request)
        except CurrentTabBridgeError as exc:
            payload = _current_tab_error_payload(str(exc))
        return HostCurrentTabNavigateResult.model_validate(payload).model_dump()

    @mcp.tool(
        name="host.current_tab.list_tabs",
        description="Enumerate open Chrome tabs via the current-tab extension relay (read-only).",
    )
    async def host_current_tab_list_tabs(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostCurrentTabListTabsRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
        )
        try:
            payload = await _current_tab_list_tabs_payload(request)
        except CurrentTabBridgeError as exc:
            payload = _current_tab_error_payload(str(exc))
        return HostCurrentTabListTabsResult.model_validate(payload).model_dump()

    @mcp.tool(
        name="host.current_tab.activate",
        description="Activate a specific Chrome tab through the current-tab extension relay.",
    )
    async def host_current_tab_activate(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        tab_id: int,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostCurrentTabActivateRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            tab_id=tab_id,
        )
        try:
            payload = await _current_tab_activate_payload(request)
        except CurrentTabBridgeError as exc:
            payload = _current_tab_error_payload(str(exc))
        return HostCurrentTabActivateResult.model_validate(payload).model_dump()

    @mcp.tool(
        name="host.current_tab.click",
        description="Click a selector inside the active Chrome tab through the current-tab extension relay.",
    )
    async def host_current_tab_click(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        selector: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostCurrentTabClickRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            selector=selector,
        )
        try:
            payload = await _current_tab_click_payload(request)
        except CurrentTabBridgeError as exc:
            payload = _current_tab_error_payload(str(exc))
        return HostCurrentTabClickResult.model_validate(payload).model_dump()

    @mcp.tool(
        name="host.current_tab.fill",
        description="Fill a selector inside the active Chrome tab through the current-tab extension relay.",
    )
    async def host_current_tab_fill(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        selector: str,
        text: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostCurrentTabFillRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            selector=selector,
            text=text,
        )
        try:
            payload = await _current_tab_fill_payload(request)
        except CurrentTabBridgeError as exc:
            payload = _current_tab_error_payload(str(exc))
        return HostCurrentTabFillResult.model_validate(payload).model_dump()

    @mcp.tool(
        name="host.current_tab.extract_text",
        description="Extract text from the active Chrome tab through the current-tab extension relay.",
    )
    async def host_current_tab_extract_text(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        selector: Optional[str] = None,
        target_tab_id: Optional[int] = None,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = HostCurrentTabExtractTextRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            selector=selector,
            target_tab_id=target_tab_id,
        )
        try:
            payload = await _current_tab_extract_text_payload(request)
        except CurrentTabBridgeError as exc:
            payload = _current_tab_error_payload(str(exc))
        return HostCurrentTabExtractTextResult.model_validate(payload).model_dump()

    return mcp


def main():
    parser = argparse.ArgumentParser(description="Host Bridge MCP Server")
    parser.add_argument("--sse", action="store_true", help="Run in SSE mode")
    parser.add_argument("--port", type=int, default=8766, help="SSE port")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="SSE host")
    args = parser.parse_args()

    if args.sse:
        mcp = create_server(host=args.host, port=args.port)
        print(f"SSE mode: http://{args.host}:{args.port}/sse")
        mcp.run(transport="sse")
    else:
        mcp = create_server(host="stdio")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
