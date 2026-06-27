"""Host Bridge MCP client helpers."""

from __future__ import annotations

import json
from typing import Any, Optional

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client

from src.bridges.host_bridge_schema import (
    BridgePingResult,
    HostControlUiChatSendMessageRequest,
    HostControlUiChatSendMessageResult,
    HostBrowserClickRequest,
    HostBrowserClickResult,
    CapabilityListResult,
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
from src.config.settings import get_settings


class HostBridgeError(RuntimeError):
    """Raised when the Host Bridge call fails or returns invalid data."""


def _text_content_to_string(contents: list[Any]) -> str:
    return "".join(getattr(content, "text", "") for content in contents)


def _decode_tool_payload(result: Any) -> dict[str, Any]:
    if getattr(result, "structuredContent", None) is not None:
        payload = result.structuredContent
    else:
        text = _text_content_to_string(getattr(result, "content", []))
        if not text:
            raise HostBridgeError("Host Bridge returned empty tool content")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HostBridgeError(f"Host Bridge returned non-JSON tool content: {exc}") from exc

    if not isinstance(payload, dict):
        raise HostBridgeError("Host Bridge returned a non-object payload")
    return payload


class HostBridgeClient:
    """Thin MCP client for the Host Bridge SSE endpoint."""

    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float = 5,
        sse_read_timeout_seconds: float = 300,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.sse_read_timeout_seconds = sse_read_timeout_seconds

    async def _call_tool(self, name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        async with sse_client(
            self.url,
            timeout=self.timeout_seconds,
            sse_read_timeout=self.sse_read_timeout_seconds,
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments or {})

        if getattr(result, "isError", False):
            # Surface the MCP tool's error text so diagnostics don't get swallowed.
            # Without this the caller only ever sees "Host Bridge tool call failed: <name>",
            # which hides the underlying reason (e.g. "No tab with id: N", validation errors).
            detail = ""
            try:
                detail = _text_content_to_string(getattr(result, "content", []) or []).strip()
            except Exception:  # noqa: BLE001 — diagnostics, never fail extraction
                detail = ""
            if detail:
                raise HostBridgeError(f"Host Bridge tool call failed: {name}: {detail}")
            raise HostBridgeError(f"Host Bridge tool call failed: {name}")
        return _decode_tool_payload(result)

    async def ping(self) -> BridgePingResult:
        return BridgePingResult.model_validate(await self._call_tool("ping"))

    async def list_capabilities(self) -> CapabilityListResult:
        return CapabilityListResult.model_validate(await self._call_tool("capabilities.list"))

    async def run_shell(self, request: HostShellRunRequest) -> HostShellRunResult:
        payload = await self._call_tool("host.shell.run", request.model_dump())
        return HostShellRunResult.model_validate(payload)

    async def read_file(self, request: HostFileReadRequest) -> HostFileReadResult:
        payload = await self._call_tool("host.file.read", request.model_dump())
        return HostFileReadResult.model_validate(payload)

    async def write_file(self, request: HostFileWriteRequest) -> HostFileWriteResult:
        payload = await self._call_tool("host.file.write", request.model_dump())
        return HostFileWriteResult.model_validate(payload)

    async def list_files(self, request: HostFileListRequest) -> HostFileListResult:
        payload = await self._call_tool("host.file.list", request.model_dump())
        return HostFileListResult.model_validate(payload)

    async def navigate_browser(self, request: HostBrowserNavigateRequest) -> HostBrowserNavigateResult:
        payload = await self._call_tool("host.browser.navigate", request.model_dump())
        return HostBrowserNavigateResult.model_validate(payload)

    async def click_browser(self, request: HostBrowserClickRequest) -> HostBrowserClickResult:
        payload = await self._call_tool("host.browser.click", request.model_dump())
        return HostBrowserClickResult.model_validate(payload)

    async def fill_browser(self, request: HostBrowserFillRequest) -> HostBrowserFillResult:
        payload = await self._call_tool("host.browser.fill", request.model_dump())
        return HostBrowserFillResult.model_validate(payload)

    async def press_browser(self, request: HostBrowserPressRequest) -> HostBrowserPressResult:
        payload = await self._call_tool("host.browser.press", request.model_dump())
        return HostBrowserPressResult.model_validate(payload)

    async def screenshot_browser(
        self,
        request: HostBrowserScreenshotRequest,
    ) -> HostBrowserScreenshotResult:
        payload = await self._call_tool("host.browser.screenshot", request.model_dump())
        return HostBrowserScreenshotResult.model_validate(payload)

    async def extract_browser_text(
        self,
        request: HostBrowserExtractTextRequest,
    ) -> HostBrowserExtractTextResult:
        payload = await self._call_tool("host.browser.extract_text", request.model_dump())
        return HostBrowserExtractTextResult.model_validate(payload)

    async def send_control_ui_chat_message(
        self,
        request: HostControlUiChatSendMessageRequest,
    ) -> HostControlUiChatSendMessageResult:
        payload = await self._call_tool("host.control_ui_chat.send_message", request.model_dump())
        return HostControlUiChatSendMessageResult.model_validate(payload)

    async def current_tab_info(self, request: HostCurrentTabInfoRequest) -> HostCurrentTabInfoResult:
        payload = await self._call_tool("host.current_tab.info", request.model_dump())
        return HostCurrentTabInfoResult.model_validate(payload)

    async def current_tab_navigate(
        self,
        request: HostCurrentTabNavigateRequest,
    ) -> HostCurrentTabNavigateResult:
        payload = await self._call_tool("host.current_tab.navigate", request.model_dump())
        return HostCurrentTabNavigateResult.model_validate(payload)

    async def current_tab_list_tabs(
        self,
        request: HostCurrentTabListTabsRequest,
    ) -> HostCurrentTabListTabsResult:
        payload = await self._call_tool("host.current_tab.list_tabs", request.model_dump())
        return HostCurrentTabListTabsResult.model_validate(payload)

    async def current_tab_activate(
        self,
        request: HostCurrentTabActivateRequest,
    ) -> HostCurrentTabActivateResult:
        payload = await self._call_tool("host.current_tab.activate", request.model_dump())
        return HostCurrentTabActivateResult.model_validate(payload)

    async def current_tab_click(self, request: HostCurrentTabClickRequest) -> HostCurrentTabClickResult:
        payload = await self._call_tool("host.current_tab.click", request.model_dump())
        return HostCurrentTabClickResult.model_validate(payload)

    async def current_tab_fill(self, request: HostCurrentTabFillRequest) -> HostCurrentTabFillResult:
        payload = await self._call_tool("host.current_tab.fill", request.model_dump())
        return HostCurrentTabFillResult.model_validate(payload)

    async def current_tab_extract_text(
        self,
        request: HostCurrentTabExtractTextRequest,
    ) -> HostCurrentTabExtractTextResult:
        payload = await self._call_tool("host.current_tab.extract_text", request.model_dump())
        return HostCurrentTabExtractTextResult.model_validate(payload)


def get_host_bridge_client() -> Optional[HostBridgeClient]:
    """Build a Host Bridge client from settings when enabled."""
    settings = get_settings()
    if not settings.host_bridge_enabled:
        return None
    if not settings.host_bridge_url:
        raise HostBridgeError("HOST_BRIDGE_ENABLED is true but HOST_BRIDGE_URL is not set")

    return HostBridgeClient(
        url=settings.host_bridge_url,
        timeout_seconds=settings.host_bridge_timeout_seconds,
        sse_read_timeout_seconds=settings.host_bridge_sse_read_timeout_seconds,
    )
