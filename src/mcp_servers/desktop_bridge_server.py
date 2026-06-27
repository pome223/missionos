"""
Desktop Bridge MCP server adapter.

GUI / Accessibility capabilities live here, separate from Host Bridge.
This server delegates to a DesktopClient implementation.

起動方法:
  python -m src.mcp_servers.desktop_bridge_server
  python -m src.mcp_servers.desktop_bridge_server --sse --host 127.0.0.1 --port 8767
"""

from __future__ import annotations

import argparse
from typing import Optional

from src.config.settings import get_settings
from src.desktop import (
    DesktopAxFindRequest,
    BridgePingResult,
    DesktopAxSnapshotRequest,
    build_default_desktop_client,
    DesktopClient,
    DesktopClearStopRequest,
    DesktopClickRequest,
    DesktopDragRequest,
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
    DesktopWaitElementRequest,
    DesktopWaitWindowRequest,
    DesktopWindowsRequest,
)
from src.security.network import enforce_loopback_bind, is_loopback_host


def create_server(
    host: str = "127.0.0.1",
    port: int = 8767,
    *,
    desktop_client: DesktopClient | None = None,
):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import TransportSecuritySettings
    settings = get_settings()

    enforce_loopback_bind(
        host,
        service_name="Desktop Bridge",
        allow_remote_bind=settings.bridge_allow_remote_bind,
    )

    if is_loopback_host(host):
        # Dockerized gateway clients reach the host-bound bridge via
        # host.docker.internal while still remaining local to the machine.
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

    mcp = FastMCP("desktop-bridge")
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.transport_security = transport_security

    transport_hint = "sse" if host != "stdio" else "stdio"
    client = desktop_client or build_default_desktop_client()

    @mcp.tool(name="ping", description="Desktop Bridge health probe.")
    def ping() -> dict:
        return BridgePingResult(
            service="desktop-bridge",
            version="v1-client-adapter",
            transport=transport_hint,
        ).model_dump()

    @mcp.tool(name="capabilities.list", description="List Desktop Bridge capabilities.")
    async def list_capabilities() -> dict:
        return (await client.capabilities()).model_dump()

    @mcp.tool(
        name="desktop.runtime.status",
        description="Inspect desktop runtime emergency stop state.",
    )
    async def desktop_runtime_status(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopRuntimeStatusRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
        )
        return (await client.runtime_status(request)).model_dump()

    @mcp.tool(
        name="desktop.runtime.stop",
        description="Trigger desktop runtime emergency stop.",
    )
    async def desktop_runtime_stop(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        reason: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopEmergencyStopRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            reason=reason,
        )
        return (await client.emergency_stop(request)).model_dump()

    @mcp.tool(
        name="desktop.runtime.clear_stop",
        description="Clear desktop runtime emergency stop.",
    )
    async def desktop_runtime_clear_stop(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopClearStopRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
        )
        return (await client.clear_stop(request)).model_dump()

    @mcp.tool(
        name="desktop.view.screenshot",
        description="Capture a screenshot from the host desktop.",
    )
    async def desktop_view_screenshot(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        path: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopScreenshotRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            path=path,
        )
        return (await client.screenshot(request)).model_dump()

    @mcp.tool(name="desktop.view.windows", description="List windows on the host desktop.")
    async def desktop_view_windows(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        include_minimized: bool = False,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopWindowsRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            include_minimized=include_minimized,
        )
        return (await client.windows(request)).model_dump()

    @mcp.tool(
        name="desktop.wait.window",
        description="Wait for a matching window to appear on the host desktop.",
    )
    async def desktop_wait_window(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        app_name: Optional[str] = None,
        window_id: Optional[str] = None,
        title: Optional[str] = None,
        timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.2,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopWaitWindowRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            app_name=app_name,
            window_id=window_id,
            title=title,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return (await client.wait_window(request)).model_dump()

    @mcp.tool(
        name="desktop.view.frontmost_app",
        description="Inspect the frontmost app on the host desktop.",
    )
    async def desktop_view_frontmost_app(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopFrontmostAppRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
        )
        return (await client.frontmost_app(request)).model_dump()

    @mcp.tool(
        name="desktop.ax.find",
        description="Find a single accessibility element from the host desktop.",
    )
    async def desktop_ax_find(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        app_name: Optional[str] = None,
        window_id: Optional[str] = None,
        role: Optional[str] = None,
        title: Optional[str] = None,
        identifier: Optional[str] = None,
        value_contains: Optional[str] = None,
        index: int = 0,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopAxFindRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            target=_selector(
                app_name=app_name,
                window_id=window_id,
                role=role,
                title=title,
                identifier=identifier,
                value_contains=value_contains,
                index=index,
            ),
        )
        return (await client.ax_find(request)).model_dump()

    @mcp.tool(
        name="desktop.wait.element",
        description="Wait for a matching accessibility element from the host desktop.",
    )
    async def desktop_wait_element(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        app_name: Optional[str] = None,
        window_id: Optional[str] = None,
        role: Optional[str] = None,
        title: Optional[str] = None,
        identifier: Optional[str] = None,
        value_contains: Optional[str] = None,
        index: int = 0,
        timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.2,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopWaitElementRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            target=_selector(
                app_name=app_name,
                window_id=window_id,
                role=role,
                title=title,
                identifier=identifier,
                value_contains=value_contains,
                index=index,
            ),
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return (await client.wait_element(request)).model_dump()

    @mcp.tool(
        name="desktop.ax.snapshot",
        description="Capture an accessibility tree from the host desktop.",
    )
    async def desktop_ax_snapshot(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        app_name: Optional[str] = None,
        window_id: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopAxSnapshotRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            app_name=app_name,
            window_id=window_id,
        )
        return (await client.ax_snapshot(request)).model_dump()

    def _selector(
        *,
        app_name: Optional[str] = None,
        window_id: Optional[str] = None,
        role: Optional[str] = None,
        title: Optional[str] = None,
        identifier: Optional[str] = None,
        value_contains: Optional[str] = None,
        index: int = 0,
    ) -> Optional[DesktopElementSelector]:
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

    @mcp.tool(name="desktop.control.click", description="Click on the host desktop.")
    async def desktop_control_click(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
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
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopClickRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            x=x,
            y=y,
            button=button,
            click_count=click_count,
            target=_selector(
                app_name=app_name,
                window_id=window_id,
                role=role,
                title=title,
                identifier=identifier,
                value_contains=value_contains,
                index=index,
            ),
        )
        return (await client.click(request)).model_dump()

    @mcp.tool(name="desktop.control.type", description="Type text into the host desktop.")
    async def desktop_control_type(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        text: str,
        app_name: Optional[str] = None,
        window_id: Optional[str] = None,
        role: Optional[str] = None,
        title: Optional[str] = None,
        identifier: Optional[str] = None,
        value_contains: Optional[str] = None,
        index: int = 0,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopTypeRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            text=text,
            target=_selector(
                app_name=app_name,
                window_id=window_id,
                role=role,
                title=title,
                identifier=identifier,
                value_contains=value_contains,
                index=index,
            ),
        )
        return (await client.type_text(request)).model_dump()

    @mcp.tool(name="desktop.control.launch_app", description="Launch an app on the host desktop.")
    async def desktop_control_launch_app(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        app_name: Optional[str] = None,
        bundle_id: Optional[str] = None,
        wait_for_focus: bool = True,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopLaunchAppRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            app_name=app_name,
            bundle_id=bundle_id,
            wait_for_focus=wait_for_focus,
        )
        return (await client.launch_app(request)).model_dump()

    @mcp.tool(
        name="desktop.control.focus_window",
        description="Focus an app or window on the host desktop.",
    )
    async def desktop_control_focus_window(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        app_name: Optional[str] = None,
        window_id: Optional[str] = None,
        title: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopFocusWindowRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            app_name=app_name,
            window_id=window_id,
            title=title,
        )
        return (await client.focus_window(request)).model_dump()

    @mcp.tool(name="desktop.control.hotkey", description="Send a hotkey to the host desktop.")
    async def desktop_control_hotkey(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        keys: list[str],
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopHotkeyRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            keys=keys,
        )
        return (await client.hotkey(request)).model_dump()

    @mcp.tool(name="desktop.control.scroll", description="Scroll on the host desktop.")
    async def desktop_control_scroll(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        delta_x: int = 0,
        delta_y: int = 0,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopScrollRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            delta_x=delta_x,
            delta_y=delta_y,
        )
        return (await client.scroll(request)).model_dump()

    @mcp.tool(name="desktop.control.drag", description="Drag on the host desktop.")
    async def desktop_control_drag(
        request_id: str,
        session_id: str,
        user_id: str,
        agent_name: str,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        approval_token: Optional[str] = None,
    ) -> dict:
        request = DesktopDragRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            approval_token=approval_token,
            start_x=start_x,
            start_y=start_y,
            end_x=end_x,
            end_y=end_y,
        )
        return (await client.drag(request)).model_dump()

    return mcp


def main():
    parser = argparse.ArgumentParser(description="Desktop Bridge MCP Server")
    parser.add_argument("--sse", action="store_true", help="Run in SSE mode")
    parser.add_argument("--port", type=int, default=8767, help="SSE port")
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
