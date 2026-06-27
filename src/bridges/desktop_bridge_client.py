"""Desktop Bridge client helpers."""

from __future__ import annotations

import json
from typing import Any, Optional

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client

from src.config.settings import get_settings
from src.desktop import (
    DesktopAxFindRequest,
    DesktopAxFindResult,
    BridgePingResult,
    CapabilityListResult,
    DesktopAxSnapshotRequest,
    DesktopAxSnapshotResult,
    DesktopClickRequest,
    DesktopClearStopRequest,
    DesktopClient,
    DesktopControlResult,
    DesktopDragRequest,
    DesktopEmergencyStopRequest,
    DesktopFocusWindowRequest,
    DesktopFrontmostAppRequest,
    DesktopFrontmostAppResult,
    DesktopHotkeyRequest,
    DesktopLaunchAppRequest,
    DesktopRuntimeStatusRequest,
    DesktopRuntimeStatusResult,
    DesktopScrollRequest,
    DesktopScreenshotRequest,
    DesktopScreenshotResult,
    DesktopTypeRequest,
    DesktopWaitElementRequest,
    DesktopWaitElementResult,
    DesktopWaitWindowRequest,
    DesktopWaitWindowResult,
    DesktopWindowsRequest,
    DesktopWindowsResult,
    build_default_desktop_client,
)


class DesktopBridgeError(RuntimeError):
    """Raised when the Desktop Bridge call fails or returns invalid data."""


def _text_content_to_string(contents: list[Any]) -> str:
    return "".join(getattr(content, "text", "") for content in contents)


def _decode_tool_payload(result: Any) -> dict[str, Any]:
    if getattr(result, "structuredContent", None) is not None:
        payload = result.structuredContent
    else:
        text = _text_content_to_string(getattr(result, "content", []))
        if not text:
            raise DesktopBridgeError("Desktop Bridge returned empty tool content")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DesktopBridgeError(
                f"Desktop Bridge returned non-JSON tool content: {exc}"
            ) from exc

    if not isinstance(payload, dict):
        raise DesktopBridgeError("Desktop Bridge returned a non-object payload")
    return payload


class DesktopBridgeClient(DesktopClient):
    """Thin MCP client for the Desktop Bridge SSE endpoint."""

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

    async def _call_tool(
        self,
        name: str,
        arguments: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        async with sse_client(
            self.url,
            timeout=self.timeout_seconds,
            sse_read_timeout=self.sse_read_timeout_seconds,
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments or {})

        if getattr(result, "isError", False):
            raise DesktopBridgeError(f"Desktop Bridge tool call failed: {name}")
        return _decode_tool_payload(result)

    async def ping(self) -> BridgePingResult:
        return BridgePingResult.model_validate(await self._call_tool("ping"))

    async def capabilities(self) -> CapabilityListResult:
        return CapabilityListResult.model_validate(await self._call_tool("capabilities.list"))

    async def runtime_status(
        self, request: DesktopRuntimeStatusRequest
    ) -> DesktopRuntimeStatusResult:
        payload = await self._call_tool("desktop.runtime.status", request.model_dump())
        return DesktopRuntimeStatusResult.model_validate(payload)

    async def emergency_stop(
        self, request: DesktopEmergencyStopRequest
    ) -> DesktopRuntimeStatusResult:
        payload = await self._call_tool("desktop.runtime.stop", request.model_dump())
        return DesktopRuntimeStatusResult.model_validate(payload)

    async def clear_stop(
        self, request: DesktopClearStopRequest
    ) -> DesktopRuntimeStatusResult:
        payload = await self._call_tool("desktop.runtime.clear_stop", request.model_dump())
        return DesktopRuntimeStatusResult.model_validate(payload)

    async def screenshot(
        self, request: DesktopScreenshotRequest
    ) -> DesktopScreenshotResult:
        payload = await self._call_tool("desktop.view.screenshot", request.model_dump())
        return DesktopScreenshotResult.model_validate(payload)

    async def windows(self, request: DesktopWindowsRequest) -> DesktopWindowsResult:
        payload = await self._call_tool("desktop.view.windows", request.model_dump())
        return DesktopWindowsResult.model_validate(payload)

    async def wait_window(
        self, request: DesktopWaitWindowRequest
    ) -> DesktopWaitWindowResult:
        payload = await self._call_tool("desktop.wait.window", request.model_dump())
        return DesktopWaitWindowResult.model_validate(payload)

    async def frontmost_app(
        self, request: DesktopFrontmostAppRequest
    ) -> DesktopFrontmostAppResult:
        payload = await self._call_tool("desktop.view.frontmost_app", request.model_dump())
        return DesktopFrontmostAppResult.model_validate(payload)

    async def ax_snapshot(
        self, request: DesktopAxSnapshotRequest
    ) -> DesktopAxSnapshotResult:
        payload = await self._call_tool("desktop.ax.snapshot", request.model_dump())
        return DesktopAxSnapshotResult.model_validate(payload)

    async def ax_find(self, request: DesktopAxFindRequest) -> DesktopAxFindResult:
        payload = await self._call_tool("desktop.ax.find", request.model_dump())
        return DesktopAxFindResult.model_validate(payload)

    async def wait_element(
        self, request: DesktopWaitElementRequest
    ) -> DesktopWaitElementResult:
        payload = await self._call_tool("desktop.wait.element", request.model_dump())
        return DesktopWaitElementResult.model_validate(payload)

    async def click(self, request: DesktopClickRequest) -> DesktopControlResult:
        payload = await self._call_tool("desktop.control.click", request.model_dump())
        return DesktopControlResult.model_validate(payload)

    async def type_text(self, request: DesktopTypeRequest) -> DesktopControlResult:
        payload = await self._call_tool("desktop.control.type", request.model_dump())
        return DesktopControlResult.model_validate(payload)

    async def launch_app(
        self, request: DesktopLaunchAppRequest
    ) -> DesktopControlResult:
        payload = await self._call_tool("desktop.control.launch_app", request.model_dump())
        return DesktopControlResult.model_validate(payload)

    async def focus_window(
        self, request: DesktopFocusWindowRequest
    ) -> DesktopControlResult:
        payload = await self._call_tool("desktop.control.focus_window", request.model_dump())
        return DesktopControlResult.model_validate(payload)

    async def hotkey(self, request: DesktopHotkeyRequest) -> DesktopControlResult:
        payload = await self._call_tool("desktop.control.hotkey", request.model_dump())
        return DesktopControlResult.model_validate(payload)

    async def scroll(self, request: DesktopScrollRequest) -> DesktopControlResult:
        payload = await self._call_tool("desktop.control.scroll", request.model_dump())
        return DesktopControlResult.model_validate(payload)

    async def drag(self, request: DesktopDragRequest) -> DesktopControlResult:
        payload = await self._call_tool("desktop.control.drag", request.model_dump())
        return DesktopControlResult.model_validate(payload)


def get_desktop_client() -> DesktopClient:
    """Return the configured desktop runtime client.

    If a Desktop Bridge endpoint is configured, use it. Otherwise fall back to a
    local desktop client implementation for host-side development.
    """

    settings = get_settings()
    if settings.desktop_bridge_enabled:
        if not settings.desktop_bridge_url:
            raise DesktopBridgeError(
                "DESKTOP_BRIDGE_ENABLED is true but DESKTOP_BRIDGE_URL is not set"
            )
        return DesktopBridgeClient(
            url=settings.desktop_bridge_url,
            timeout_seconds=settings.desktop_bridge_timeout_seconds,
            sse_read_timeout_seconds=settings.desktop_bridge_sse_read_timeout_seconds,
        )
    return build_default_desktop_client()


__all__ = ["DesktopBridgeClient", "DesktopBridgeError", "get_desktop_client"]
