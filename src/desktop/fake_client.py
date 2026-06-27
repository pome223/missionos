"""Fake desktop client for contract tests and early integration work."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from src.desktop.client import (
    DESKTOP_NOT_IMPLEMENTED,
    DesktopClient,
    desktop_capabilities,
)
from src.desktop.models import (
    DesktopAxFindRequest,
    DesktopAxFindResult,
    CapabilityListResult,
    DesktopAxSnapshotRequest,
    DesktopAxSnapshotResult,
    DesktopClickRequest,
    DesktopControlResult,
    DesktopDragRequest,
    DesktopEmergencyStopRequest,
    DesktopFocusWindowRequest,
    DesktopFrontmostAppRequest,
    DesktopFrontmostAppResult,
    DesktopHotkeyRequest,
    DesktopLaunchAppRequest,
    DesktopClearStopRequest,
    DesktopRuntimeStatusRequest,
    DesktopRuntimeStatusResult,
    DesktopScrollRequest,
    DesktopScreenshotRequest,
    DesktopScreenshotResult,
    DesktopTargetDescriptor,
    DesktopTypeRequest,
    DesktopWaitElementRequest,
    DesktopWaitElementResult,
    DesktopWaitWindowRequest,
    DesktopWaitWindowResult,
    DesktopWindowDescriptor,
    DesktopWindowsRequest,
    DesktopWindowsResult,
)
from src.desktop.runtime import DesktopRuntimeState


class FakeDesktopClient(DesktopClient):
    """In-memory desktop runtime used before a real companion exists."""

    def __init__(
        self,
        *,
        implemented: Collection[str] | None = None,
        windows: list[DesktopWindowDescriptor] | None = None,
        frontmost_app_name: str = "",
        frontmost_pid: int | None = None,
        screenshot_path: str | None = None,
        screenshot_width: int = 0,
        screenshot_height: int = 0,
        ax_tree: dict[str, Any] | None = None,
        runtime_state: DesktopRuntimeState | None = None,
    ) -> None:
        self._implemented = set(implemented or ())
        self._windows = list(windows or [])
        self._frontmost_app_name = frontmost_app_name
        self._frontmost_pid = frontmost_pid
        self._screenshot_path = screenshot_path
        self._screenshot_width = screenshot_width
        self._screenshot_height = screenshot_height
        self._ax_tree = dict(ax_tree or {})
        self._runtime_state = runtime_state or DesktopRuntimeState()
        self.last_click_request: DesktopClickRequest | None = None
        self.last_type_request: DesktopTypeRequest | None = None
        self.last_launch_request: DesktopLaunchAppRequest | None = None
        self.last_focus_request: DesktopFocusWindowRequest | None = None

    async def capabilities(self) -> CapabilityListResult:
        return desktop_capabilities(
            self._implemented
            | {
                "desktop.runtime.status",
                "desktop.runtime.stop",
                "desktop.runtime.clear_stop",
            }
        )

    async def runtime_status(
        self, request: DesktopRuntimeStatusRequest
    ) -> DesktopRuntimeStatusResult:
        del request
        snapshot = self._runtime_state.snapshot()
        return DesktopRuntimeStatusResult(
            ok=True,
            stopped=snapshot.stopped,
            reason=snapshot.reason,
            stopped_at=snapshot.stopped_at,
        )

    async def emergency_stop(
        self, request: DesktopEmergencyStopRequest
    ) -> DesktopRuntimeStatusResult:
        snapshot = self._runtime_state.emergency_stop(request.reason)
        return DesktopRuntimeStatusResult(
            ok=True,
            stopped=snapshot.stopped,
            reason=snapshot.reason,
            stopped_at=snapshot.stopped_at,
            changed=True,
        )

    async def clear_stop(
        self, request: DesktopClearStopRequest
    ) -> DesktopRuntimeStatusResult:
        del request
        snapshot = self._runtime_state.clear_stop()
        return DesktopRuntimeStatusResult(
            ok=True,
            stopped=snapshot.stopped,
            reason=snapshot.reason,
            stopped_at=snapshot.stopped_at,
            changed=True,
        )

    async def screenshot(
        self, request: DesktopScreenshotRequest
    ) -> DesktopScreenshotResult:
        if "desktop.view.screenshot" not in self._implemented:
            return DesktopScreenshotResult(
                ok=False,
                path=request.path,
                error=DESKTOP_NOT_IMPLEMENTED,
            )
        return DesktopScreenshotResult(
            ok=True,
            path=request.path or self._screenshot_path,
            width=self._screenshot_width,
            height=self._screenshot_height,
        )

    async def windows(self, request: DesktopWindowsRequest) -> DesktopWindowsResult:
        del request
        if "desktop.view.windows" not in self._implemented:
            return DesktopWindowsResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        return DesktopWindowsResult(ok=True, windows=self._windows)

    async def wait_window(
        self, request: DesktopWaitWindowRequest
    ) -> DesktopWaitWindowResult:
        if "desktop.wait.window" not in self._implemented:
            return DesktopWaitWindowResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        for window in self._windows:
            if request.window_id and window.window_id != request.window_id:
                continue
            if request.app_name and window.app_name != request.app_name:
                continue
            if request.title and window.title != request.title:
                continue
            return DesktopWaitWindowResult(ok=True, matched=True, window=window)
        return DesktopWaitWindowResult(ok=True, matched=False)

    async def frontmost_app(
        self, request: DesktopFrontmostAppRequest
    ) -> DesktopFrontmostAppResult:
        del request
        if "desktop.view.frontmost_app" not in self._implemented:
            return DesktopFrontmostAppResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        return DesktopFrontmostAppResult(
            ok=True,
            app_name=self._frontmost_app_name,
            pid=self._frontmost_pid,
        )

    async def ax_snapshot(
        self, request: DesktopAxSnapshotRequest
    ) -> DesktopAxSnapshotResult:
        del request
        if "desktop.ax.snapshot" not in self._implemented:
            return DesktopAxSnapshotResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        return DesktopAxSnapshotResult(ok=True, tree=self._ax_tree)

    async def ax_find(
        self, request: DesktopAxFindRequest
    ) -> DesktopAxFindResult:
        if "desktop.ax.find" not in self._implemented:
            return DesktopAxFindResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        target = self._resolve_target_from_request(request.target)
        return DesktopAxFindResult(ok=True, matched=target is not None, target=target)

    async def wait_element(
        self, request: DesktopWaitElementRequest
    ) -> DesktopWaitElementResult:
        if "desktop.wait.element" not in self._implemented:
            return DesktopWaitElementResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        target = self._resolve_target_from_request(request.target)
        return DesktopWaitElementResult(ok=True, matched=target is not None, target=target)

    async def click(self, request: DesktopClickRequest) -> DesktopControlResult:
        blocked = self._blocked_control_result()
        if blocked is not None:
            return blocked
        self.last_click_request = request
        target = self._resolve_target_from_request(request.target)
        return self._control_result("desktop.control.click", target=target)

    async def type_text(self, request: DesktopTypeRequest) -> DesktopControlResult:
        blocked = self._blocked_control_result()
        if blocked is not None:
            return blocked
        self.last_type_request = request
        target = self._resolve_target_from_request(request.target)
        return self._control_result("desktop.control.type", target=target)

    async def launch_app(
        self, request: DesktopLaunchAppRequest
    ) -> DesktopControlResult:
        blocked = self._blocked_control_result()
        if blocked is not None:
            return blocked
        self.last_launch_request = request
        return self._control_result(
            "desktop.control.launch_app",
            target=DesktopTargetDescriptor(
                app_name=request.app_name or "",
                identifier=request.bundle_id or "",
            ),
        )

    async def focus_window(
        self, request: DesktopFocusWindowRequest
    ) -> DesktopControlResult:
        blocked = self._blocked_control_result()
        if blocked is not None:
            return blocked
        self.last_focus_request = request
        target = None
        for window in self._windows:
            if request.window_id and window.window_id != request.window_id:
                continue
            if request.app_name and window.app_name != request.app_name:
                continue
            if request.title and window.title != request.title:
                continue
            target = DesktopTargetDescriptor(
                app_name=window.app_name,
                window_id=window.window_id,
                title=window.title,
                bounds=window.bounds,
            )
            break
        if target is None and (request.app_name or request.title or request.window_id):
            target = DesktopTargetDescriptor(
                app_name=request.app_name or "",
                window_id=request.window_id or "",
                title=request.title or "",
            )
        return self._control_result("desktop.control.focus_window", target=target)

    async def hotkey(self, request: DesktopHotkeyRequest) -> DesktopControlResult:
        del request
        blocked = self._blocked_control_result()
        if blocked is not None:
            return blocked
        return self._control_result("desktop.control.hotkey")

    async def scroll(self, request: DesktopScrollRequest) -> DesktopControlResult:
        del request
        blocked = self._blocked_control_result()
        if blocked is not None:
            return blocked
        return self._control_result("desktop.control.scroll")

    async def drag(self, request: DesktopDragRequest) -> DesktopControlResult:
        del request
        blocked = self._blocked_control_result()
        if blocked is not None:
            return blocked
        return self._control_result("desktop.control.drag")

    def _control_result(
        self,
        capability: str,
        *,
        target: DesktopTargetDescriptor | None = None,
    ) -> DesktopControlResult:
        if capability not in self._implemented:
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        return DesktopControlResult(ok=True, target=target)

    def _resolve_target_from_request(
        self,
        selector: Any,
    ) -> DesktopTargetDescriptor | None:
        if selector is None:
            return None
        for window in self._windows:
            if selector.app_name and window.app_name != selector.app_name:
                continue
            if selector.window_id and window.window_id != selector.window_id:
                continue
            return DesktopTargetDescriptor(
                app_name=window.app_name,
                window_id=window.window_id,
                title=window.title,
                role=selector.role or "",
                identifier=selector.identifier or "",
                bounds=window.bounds,
            )
        return DesktopTargetDescriptor(
            app_name=selector.app_name or "",
            window_id=selector.window_id or "",
            role=selector.role or "",
            title=selector.title or "",
            identifier=selector.identifier or "",
        )

    def _blocked_control_result(self) -> DesktopControlResult | None:
        snapshot = self._runtime_state.snapshot()
        if not snapshot.stopped:
            return None
        detail = snapshot.reason or "Emergency stop requested"
        return DesktopControlResult(ok=False, error=f"desktop runtime stopped: {detail}")


__all__ = ["FakeDesktopClient"]
