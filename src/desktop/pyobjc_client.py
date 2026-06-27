"""macOS-oriented desktop client implementation.

This client keeps the public interface transport-agnostic while using
platform-native APIs where possible. View-oriented primitives land first, then
the minimum viable control primitives needed for desktop automation.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

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
    DesktopElementSelector,
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
    DesktopWindowBounds,
    DesktopWindowDescriptor,
    DesktopWindowsRequest,
    DesktopWindowsResult,
)
from src.desktop.runtime import (
    DesktopRuntimeState,
    DesktopRuntimeStoppedError,
    get_default_desktop_runtime_state,
)


class PyObjCDesktopClient(DesktopClient):
    """Desktop client backed by macOS APIs and local OS tooling."""

    def __init__(
        self,
        *,
        appkit_module: Any | None = None,
        quartz_module: Any | None = None,
        screenshot_runner: Any | None = None,
        open_runner: Any | None = None,
        runtime_state: DesktopRuntimeState | None = None,
    ) -> None:
        self._appkit = appkit_module
        self._quartz = quartz_module
        self._screenshot_runner = screenshot_runner or _default_screenshot_runner
        self._open_runner = open_runner or _default_open_runner
        self._runtime_state = runtime_state or get_default_desktop_runtime_state()

    async def capabilities(self) -> CapabilityListResult:
        implemented = set()
        implemented.add("desktop.runtime.status")
        implemented.add("desktop.runtime.stop")
        implemented.add("desktop.runtime.clear_stop")
        if self._quartz is not None:
            implemented.add("desktop.view.windows")
            implemented.add("desktop.wait.window")
        if _supports_ax_snapshot(self._quartz, self._appkit):
            implemented.add("desktop.ax.find")
            implemented.add("desktop.wait.element")
            implemented.add("desktop.ax.snapshot")
        if _supports_mouse_click(self._quartz):
            implemented.add("desktop.control.click")
            implemented.add("desktop.control.drag")
        if _supports_text_input(self._quartz):
            implemented.add("desktop.control.type")
        if _supports_scroll(self._quartz):
            implemented.add("desktop.control.scroll")
        if _supports_open_command():
            implemented.add("desktop.control.launch_app")
        if _supports_focus_window(self._quartz, self._appkit):
            implemented.add("desktop.control.focus_window")
        if _supports_hotkey(self._quartz):
            implemented.add("desktop.control.hotkey")
        if self._appkit is not None:
            implemented.add("desktop.view.frontmost_app")
        # Phase 1 uses the built-in screencapture CLI for portability.
        # A future native companion can replace this with ScreenCaptureKit
        # without changing the DesktopClient surface.
        if shutil.which("screencapture"):
            implemented.add("desktop.view.screenshot")
        return desktop_capabilities(implemented)

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
        if not shutil.which("screencapture"):
            return DesktopScreenshotResult(
                ok=False,
                path=request.path,
                error=DESKTOP_NOT_IMPLEMENTED,
            )

        output_path = request.path or _make_temp_screenshot_path()
        try:
            self._screenshot_runner(output_path)
        except Exception as exc:
            return DesktopScreenshotResult(
                ok=False,
                path=output_path,
                error=f"screenshot failed: {exc}",
            )

        width, height = _display_size(self._quartz)
        return DesktopScreenshotResult(
            ok=True,
            path=output_path,
            width=width,
            height=height,
        )

    async def windows(self, request: DesktopWindowsRequest) -> DesktopWindowsResult:
        if self._quartz is None:
            return DesktopWindowsResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

        option = getattr(self._quartz, "kCGWindowListOptionAll", 0)
        if not request.include_minimized:
            option = getattr(self._quartz, "kCGWindowListOptionOnScreenOnly", option)

        raw_windows = self._quartz.CGWindowListCopyWindowInfo(
            option,
            getattr(self._quartz, "kCGNullWindowID", 0),
        )
        descriptors: list[DesktopWindowDescriptor] = []
        for item in raw_windows or []:
            layer = int(item.get("kCGWindowLayer", item.get("CGWindowLayer", 0)) or 0)
            if layer != 0:
                continue

            app_name = str(
                item.get("kCGWindowOwnerName", item.get("CGWindowOwnerName", "")) or ""
            )
            if not app_name:
                continue

            window_id = item.get("kCGWindowNumber", item.get("CGWindowNumber"))
            bounds = item.get("kCGWindowBounds", item.get("CGWindowBounds", {})) or {}
            title = str(item.get("kCGWindowName", item.get("CGWindowName", "")) or "")
            descriptors.append(
                DesktopWindowDescriptor(
                    window_id=str(window_id),
                    app_name=app_name,
                    title=title,
                    bounds=DesktopWindowBounds(
                        x=int(bounds.get("X", 0) or 0),
                        y=int(bounds.get("Y", 0) or 0),
                        width=int(bounds.get("Width", 0) or 0),
                        height=int(bounds.get("Height", 0) or 0),
                    ),
                )
            )

        return DesktopWindowsResult(ok=True, windows=descriptors)

    async def wait_window(
        self, request: DesktopWaitWindowRequest
    ) -> DesktopWaitWindowResult:
        if self._quartz is None:
            return DesktopWaitWindowResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        deadline = asyncio.get_running_loop().time() + request.timeout_seconds
        while True:
            result = await self.windows(
                DesktopWindowsRequest(
                    request_id=request.request_id,
                    session_id=request.session_id,
                    user_id=request.user_id,
                    agent_name=request.agent_name,
                    approval_token=request.approval_token,
                    include_minimized=True,
                )
            )
            if not result.ok:
                return DesktopWaitWindowResult(ok=False, error=result.error)
            for window in result.windows:
                if request.window_id and window.window_id != request.window_id:
                    continue
                if request.app_name and window.app_name != request.app_name:
                    continue
                if request.title and request.title not in window.title:
                    continue
                return DesktopWaitWindowResult(ok=True, matched=True, window=window)
            if asyncio.get_running_loop().time() >= deadline:
                return DesktopWaitWindowResult(ok=True, matched=False)
            await asyncio.sleep(request.poll_interval_seconds)

    async def frontmost_app(
        self, request: DesktopFrontmostAppRequest
    ) -> DesktopFrontmostAppResult:
        del request
        if self._appkit is None:
            return DesktopFrontmostAppResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

        workspace = self._appkit.NSWorkspace.sharedWorkspace()
        app = workspace.frontmostApplication()
        if app is None:
            return DesktopFrontmostAppResult(
                ok=False,
                error="frontmost application not available",
            )

        name_getter = getattr(app, "localizedName", None)
        pid_getter = getattr(app, "processIdentifier", None)
        app_name = name_getter() if callable(name_getter) else ""
        pid = pid_getter() if callable(pid_getter) else None
        return DesktopFrontmostAppResult(
            ok=True,
            app_name=str(app_name or ""),
            pid=int(pid) if pid is not None else None,
        )

    async def ax_snapshot(
        self, request: DesktopAxSnapshotRequest
    ) -> DesktopAxSnapshotResult:
        if not _supports_ax_snapshot(self._quartz, self._appkit):
            return DesktopAxSnapshotResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

        resolved = self._resolve_target_application(request.app_name)
        if resolved is None:
            return DesktopAxSnapshotResult(
                ok=False,
                error=f"desktop app not found: {request.app_name or 'frontmost'}",
            )
        app_name, pid = resolved
        ax_root = self._quartz.AXUIElementCreateApplication(pid)
        if request.window_id:
            window = self._resolve_window_element(ax_root, request.window_id)
            if window is not None:
                ax_root = window

        tree = {
            "app_name": app_name,
            "pid": pid,
            "root": self._serialize_ax_element(ax_root, depth=0, max_depth=3),
        }
        return DesktopAxSnapshotResult(ok=True, tree=tree)

    async def ax_find(self, request: DesktopAxFindRequest) -> DesktopAxFindResult:
        if not _supports_ax_snapshot(self._quartz, self._appkit):
            return DesktopAxFindResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        resolved = self._resolve_element_target(request.target)
        if resolved is None:
            return DesktopAxFindResult(ok=True, matched=False)
        return DesktopAxFindResult(
            ok=True,
            matched=True,
            target=self._descriptor_for_element(
                resolved["app_name"],
                resolved["window_id"],
                resolved["element"],
            ),
        )

    async def wait_element(
        self, request: DesktopWaitElementRequest
    ) -> DesktopWaitElementResult:
        if not _supports_ax_snapshot(self._quartz, self._appkit):
            return DesktopWaitElementResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        deadline = asyncio.get_running_loop().time() + request.timeout_seconds
        while True:
            result = await self.ax_find(
                DesktopAxFindRequest(
                    request_id=request.request_id,
                    session_id=request.session_id,
                    user_id=request.user_id,
                    agent_name=request.agent_name,
                    approval_token=request.approval_token,
                    target=request.target,
                )
            )
            if not result.ok:
                return DesktopWaitElementResult(ok=False, error=result.error)
            if result.matched:
                return DesktopWaitElementResult(
                    ok=True,
                    matched=True,
                    target=result.target,
                )
            if asyncio.get_running_loop().time() >= deadline:
                return DesktopWaitElementResult(ok=True, matched=False)
            await asyncio.sleep(request.poll_interval_seconds)

    async def click(self, request: DesktopClickRequest) -> DesktopControlResult:
        blocked = self._blocked_control_result()
        if blocked is not None:
            return blocked
        if request.target is not None:
            resolved = self._resolve_element_target(request.target)
            if resolved is None:
                return DesktopControlResult(ok=False, error="desktop element not found")
            descriptor = self._descriptor_for_element(
                resolved["app_name"],
                resolved["window_id"],
                resolved["element"],
            )
            if _supports_ax_press(self._quartz) and _perform_ax_action(
                self._quartz,
                resolved["element"],
                getattr(self._quartz, "kAXPressAction", "AXPress"),
            ):
                return DesktopControlResult(ok=True, target=descriptor)
            center = _descriptor_center(descriptor)
            if center is None:
                return DesktopControlResult(
                    ok=False,
                    error="desktop element is not actionable",
                    target=descriptor,
                )
            return self._post_click(
                x=center[0],
                y=center[1],
                button=request.button,
                click_count=request.click_count,
                target=descriptor,
            )
        if not _supports_mouse_click(self._quartz):
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        return self._post_click(
            x=int(request.x or 0),
            y=int(request.y or 0),
            button=request.button,
            click_count=request.click_count,
        )

    async def type_text(self, request: DesktopTypeRequest) -> DesktopControlResult:
        blocked = self._blocked_control_result()
        if blocked is not None:
            return blocked
        if request.target is not None:
            resolved = self._resolve_element_target(request.target)
            if resolved is None:
                return DesktopControlResult(ok=False, error="desktop element not found")
            descriptor = self._descriptor_for_element(
                resolved["app_name"],
                resolved["window_id"],
                resolved["element"],
            )
            if _supports_ax_set_value(self._quartz) and _set_ax_value(
                self._quartz,
                resolved["element"],
                getattr(self._quartz, "kAXValueAttribute", "AXValue"),
                request.text,
            ):
                return DesktopControlResult(ok=True, target=descriptor)
            if not _supports_text_input(self._quartz):
                return DesktopControlResult(
                    ok=False,
                    error="desktop element is not text-editable",
                    target=descriptor,
                )
            if _supports_ax_press(self._quartz):
                _perform_ax_action(
                    self._quartz,
                    resolved["element"],
                    getattr(self._quartz, "kAXPressAction", "AXPress"),
                )
            return self._post_text(request.text, target=descriptor)
        if not _supports_text_input(self._quartz):
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        return self._post_text(request.text)

    async def launch_app(
        self, request: DesktopLaunchAppRequest
    ) -> DesktopControlResult:
        blocked = self._blocked_control_result()
        if blocked is not None:
            return blocked
        if not _supports_open_command():
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        command = ["open"]
        if request.bundle_id:
            command.extend(["-b", request.bundle_id])
        elif request.app_name:
            command.extend(["-a", request.app_name])
        try:
            self._open_runner(command)
        except Exception as exc:
            return DesktopControlResult(ok=False, error=f"launch failed: {exc}")

        target = DesktopTargetDescriptor(
            app_name=request.app_name or "",
            identifier=request.bundle_id or "",
        )
        if request.wait_for_focus and request.app_name:
            resolved = self._resolve_target_application(request.app_name)
            if resolved is not None:
                self._activate_application(*resolved)
                target.app_name = resolved[0]
        return DesktopControlResult(ok=True, target=target)

    async def focus_window(
        self, request: DesktopFocusWindowRequest
    ) -> DesktopControlResult:
        blocked = self._blocked_control_result()
        if blocked is not None:
            return blocked
        if not _supports_focus_window(self._quartz, self._appkit):
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

        app_name_hint = request.app_name
        if app_name_hint is None and (request.window_id or request.title):
            inferred = self._window_owner_for_query(request.window_id, request.title)
            if inferred is not None:
                app_name_hint = inferred[0]

        resolved = self._resolve_target_application(app_name_hint)
        if resolved is None:
            return DesktopControlResult(
                ok=False,
                error=f"desktop app not found: {app_name_hint or 'frontmost'}",
            )
        app_name, pid = resolved
        ax_root = self._quartz.AXUIElementCreateApplication(pid)
        window = self._resolve_window_element(
            ax_root,
            request.window_id,
            request.title,
        )
        target = DesktopTargetDescriptor(app_name=app_name)
        if window is not None:
            target = self._descriptor_for_element(app_name, request.window_id, window)
            _perform_ax_action(
                self._quartz,
                window,
                getattr(self._quartz, "kAXRaiseAction", "AXRaise"),
            )
        elif request.window_id or request.title:
            return DesktopControlResult(ok=False, error="desktop window not found")
        self._activate_application(app_name, pid)
        return DesktopControlResult(ok=True, target=target)

    async def hotkey(self, request: DesktopHotkeyRequest) -> DesktopControlResult:
        blocked = self._blocked_control_result()
        if blocked is not None:
            return blocked
        if not _supports_hotkey(self._quartz):
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

        keycode, flags = _hotkey_spec(self._quartz, request.keys)
        if keycode is None:
            return DesktopControlResult(ok=False, error="unsupported hotkey")

        down = self._quartz.CGEventCreateKeyboardEvent(None, keycode, True)
        up = self._quartz.CGEventCreateKeyboardEvent(None, keycode, False)
        _set_event_flags(self._quartz, down, flags)
        _set_event_flags(self._quartz, up, flags)
        self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, down)
        self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, up)
        return DesktopControlResult(ok=True)

    async def scroll(self, request: DesktopScrollRequest) -> DesktopControlResult:
        blocked = self._blocked_control_result()
        if blocked is not None:
            return blocked
        if not _supports_scroll(self._quartz):
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        event = self._quartz.CGEventCreateScrollWheelEvent(
            None,
            getattr(self._quartz, "kCGScrollEventUnitLine", 1),
            2,
            int(request.delta_y),
            int(request.delta_x),
        )
        self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, event)
        return DesktopControlResult(ok=True)

    async def drag(self, request: DesktopDragRequest) -> DesktopControlResult:
        blocked = self._blocked_control_result()
        if blocked is not None:
            return blocked
        if not _supports_mouse_click(self._quartz):
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

        start = (request.start_x, request.start_y)
        end = (request.end_x, request.end_y)
        down = self._quartz.CGEventCreateMouseEvent(
            None,
            self._quartz.kCGEventLeftMouseDown,
            start,
            self._quartz.kCGMouseButtonLeft,
        )
        dragged = self._quartz.CGEventCreateMouseEvent(
            None,
            getattr(self._quartz, "kCGEventLeftMouseDragged", self._quartz.kCGEventLeftMouseDown),
            end,
            self._quartz.kCGMouseButtonLeft,
        )
        up = self._quartz.CGEventCreateMouseEvent(
            None,
            self._quartz.kCGEventLeftMouseUp,
            end,
            self._quartz.kCGMouseButtonLeft,
        )
        self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, down)
        self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, dragged)
        self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, up)
        return DesktopControlResult(ok=True)

    def _post_click(
        self,
        *,
        x: int,
        y: int,
        button: str,
        click_count: int,
        target: DesktopTargetDescriptor | None = None,
    ) -> DesktopControlResult:
        if not _supports_mouse_click(self._quartz):
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)

        event_down_type, event_up_type, mouse_button = _mouse_event_spec(
            self._quartz,
            button,
        )
        point = (x, y)
        for _ in range(click_count):
            down = self._quartz.CGEventCreateMouseEvent(
                None,
                event_down_type,
                point,
                mouse_button,
            )
            up = self._quartz.CGEventCreateMouseEvent(
                None,
                event_up_type,
                point,
                mouse_button,
            )
            self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, down)
            self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, up)
        return DesktopControlResult(ok=True, target=target)

    def _blocked_control_result(self) -> DesktopControlResult | None:
        try:
            self._runtime_state.ensure_active()
        except DesktopRuntimeStoppedError as exc:
            return DesktopControlResult(ok=False, error=str(exc))
        return None

    def _post_text(
        self,
        text: str,
        *,
        target: DesktopTargetDescriptor | None = None,
    ) -> DesktopControlResult:
        if not _supports_text_input(self._quartz):
            return DesktopControlResult(ok=False, error=DESKTOP_NOT_IMPLEMENTED)
        for character in text:
            down = self._quartz.CGEventCreateKeyboardEvent(None, 0, True)
            up = self._quartz.CGEventCreateKeyboardEvent(None, 0, False)
            self._quartz.CGEventKeyboardSetUnicodeString(down, len(character), character)
            self._quartz.CGEventKeyboardSetUnicodeString(up, len(character), character)
            self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, down)
            self._quartz.CGEventPost(self._quartz.kCGHIDEventTap, up)
        return DesktopControlResult(ok=True, target=target)

    def _activate_application(self, app_name: str, pid: int) -> None:
        if self._appkit is None:
            return
        workspace = self._appkit.NSWorkspace.sharedWorkspace()
        for app in workspace.runningApplications() or []:
            name_getter = getattr(app, "localizedName", None)
            pid_getter = getattr(app, "processIdentifier", None)
            candidate_name = name_getter() if callable(name_getter) else ""
            candidate_pid = pid_getter() if callable(pid_getter) else None
            if int(candidate_pid or -1) != pid:
                continue
            activator = getattr(app, "activateWithOptions_", None)
            if callable(activator):
                activator(0)
            return

    def _resolve_element_target(
        self,
        selector: DesktopElementSelector,
    ) -> dict[str, Any] | None:
        if not _supports_ax_snapshot(self._quartz, self._appkit):
            return None
        app_name_hint = selector.app_name
        window_id = selector.window_id
        if app_name_hint is None and (selector.window_id or selector.title):
            inferred = self._window_owner_for_query(selector.window_id, selector.title)
            if inferred is not None:
                app_name_hint = inferred[0]
                window_id = inferred[1] or window_id

        resolved = self._resolve_target_application(app_name_hint)
        if resolved is None:
            return None
        app_name, pid = resolved
        ax_root = self._quartz.AXUIElementCreateApplication(pid)
        search_root = ax_root
        if window_id or selector.title:
            window = self._resolve_window_element(ax_root, window_id, selector.title)
            if window is None:
                return None
            search_root = window
            if window_id is None:
                window_id = self._window_id_for_title(app_name, selector.title)

        matches: list[Any] = []
        self._collect_matching_elements(search_root, selector, matches, depth=0, max_depth=5)
        if not matches:
            return None
        index = min(selector.index, len(matches) - 1)
        return {
            "app_name": app_name,
            "pid": pid,
            "window_id": window_id or "",
            "element": matches[index],
        }

    def _resolve_target_application(
        self,
        requested_app_name: str | None,
    ) -> tuple[str, int] | None:
        if self._appkit is None:
            return None

        workspace = self._appkit.NSWorkspace.sharedWorkspace()
        if requested_app_name:
            matcher = requested_app_name.strip().lower()
            for app in workspace.runningApplications() or []:
                name_getter = getattr(app, "localizedName", None)
                pid_getter = getattr(app, "processIdentifier", None)
                app_name = name_getter() if callable(name_getter) else ""
                pid = pid_getter() if callable(pid_getter) else None
                if str(app_name or "").strip().lower() == matcher and pid is not None:
                    return str(app_name), int(pid)

        frontmost = workspace.frontmostApplication()
        if frontmost is None:
            return None
        name_getter = getattr(frontmost, "localizedName", None)
        pid_getter = getattr(frontmost, "processIdentifier", None)
        app_name = name_getter() if callable(name_getter) else ""
        pid = pid_getter() if callable(pid_getter) else None
        if pid is None:
            return None
        return (str(app_name or ""), int(pid))

    def _resolve_window_element(
        self,
        ax_root: Any,
        window_id: str | None,
        title: str | None = None,
    ) -> Any | None:
        if self._quartz is None:
            return None
        windows = _ax_value(self._quartz, ax_root, self._quartz.kAXWindowsAttribute) or []
        for window in windows:
            window_title = _ax_value(self._quartz, window, self._quartz.kAXTitleAttribute)
            if window_id:
                target_title = self._window_title_for_id(window_id)
                if target_title and _title_matches(window_title, target_title):
                    return window
            if title and _title_matches(window_title, title):
                return window
        return None

    def _window_title_for_id(self, window_id: str) -> str | None:
        if self._quartz is None:
            return None
        raw_windows = self._quartz.CGWindowListCopyWindowInfo(
            getattr(self._quartz, "kCGWindowListOptionAll", 0),
            getattr(self._quartz, "kCGNullWindowID", 0),
        )
        for item in raw_windows or []:
            candidate = item.get("kCGWindowNumber", item.get("CGWindowNumber"))
            if str(candidate) == str(window_id):
                title = item.get("kCGWindowName", item.get("CGWindowName", ""))
                return str(title or "")
        return None

    def _window_id_for_title(self, app_name: str, title: str | None) -> str | None:
        if self._quartz is None or not title:
            return None
        raw_windows = self._quartz.CGWindowListCopyWindowInfo(
            getattr(self._quartz, "kCGWindowListOptionAll", 0),
            getattr(self._quartz, "kCGNullWindowID", 0),
        )
        for item in raw_windows or []:
            candidate_title = item.get("kCGWindowName", item.get("CGWindowName", ""))
            candidate_app = item.get("kCGWindowOwnerName", item.get("CGWindowOwnerName", ""))
            if not _title_matches(candidate_title, title):
                continue
            if app_name and str(candidate_app or "") != str(app_name):
                continue
            candidate_id = item.get("kCGWindowNumber", item.get("CGWindowNumber"))
            return str(candidate_id)
        return None

    def _window_owner_for_query(
        self,
        window_id: str | None,
        title: str | None,
    ) -> tuple[str, str | None] | None:
        if self._quartz is None or (window_id is None and title is None):
            return None
        raw_windows = self._quartz.CGWindowListCopyWindowInfo(
            getattr(self._quartz, "kCGWindowListOptionAll", 0),
            getattr(self._quartz, "kCGNullWindowID", 0),
        )
        for item in raw_windows or []:
            candidate_id = item.get("kCGWindowNumber", item.get("CGWindowNumber"))
            candidate_title = item.get("kCGWindowName", item.get("CGWindowName", ""))
            if window_id is not None and str(candidate_id) != str(window_id):
                continue
            if title is not None and not _title_matches(candidate_title, title):
                continue
            candidate_app = item.get("kCGWindowOwnerName", item.get("CGWindowOwnerName", ""))
            return str(candidate_app or ""), str(candidate_id) if candidate_id is not None else None
        return None

    def _descriptor_for_element(
        self,
        app_name: str,
        window_id: str | None,
        element: Any,
    ) -> DesktopTargetDescriptor:
        title = _stringify_ax_value(
            _ax_value(self._quartz, element, self._quartz.kAXTitleAttribute)
        ) if self._quartz is not None else ""
        role = _stringify_ax_value(
            _ax_value(self._quartz, element, self._quartz.kAXRoleAttribute)
        ) if self._quartz is not None else ""
        identifier = _stringify_ax_value(
            _ax_value(
                self._quartz,
                element,
                getattr(self._quartz, "kAXIdentifierAttribute", "AXIdentifier"),
            )
        ) if self._quartz is not None else ""
        bounds = _ax_bounds(self._quartz, element)
        return DesktopTargetDescriptor(
            app_name=app_name,
            window_id=window_id or "",
            role=str(role or ""),
            title=str(title or ""),
            identifier=str(identifier or ""),
            bounds=bounds,
        )

    def _collect_matching_elements(
        self,
        element: Any,
        selector: DesktopElementSelector,
        matches: list[Any],
        *,
        depth: int,
        max_depth: int,
    ) -> None:
        if self._quartz is None:
            return
        if _element_matches(self._quartz, element, selector):
            matches.append(element)
        if depth >= max_depth:
            return
        children = _ax_value(self._quartz, element, self._quartz.kAXChildrenAttribute) or []
        if not isinstance(children, (list, tuple)):
            children = [children]
        for child in children[:50]:
            self._collect_matching_elements(
                child,
                selector,
                matches,
                depth=depth + 1,
                max_depth=max_depth,
            )

    def _serialize_ax_element(self, element: Any, *, depth: int, max_depth: int) -> dict[str, Any]:
        if self._quartz is None:
            return {}

        node = {
            "role": _ax_value(self._quartz, element, self._quartz.kAXRoleAttribute) or "",
            "title": _ax_value(self._quartz, element, self._quartz.kAXTitleAttribute) or "",
            "description": _ax_value(
                self._quartz,
                element,
                self._quartz.kAXDescriptionAttribute,
            ) or "",
            "identifier": _ax_value(
                self._quartz,
                element,
                getattr(self._quartz, "kAXIdentifierAttribute", "AXIdentifier"),
            ) or "",
            "value": _stringify_ax_value(
                _ax_value(self._quartz, element, self._quartz.kAXValueAttribute)
            ),
            "children": [],
        }
        if depth >= max_depth:
            return node

        children = _ax_value(self._quartz, element, self._quartz.kAXChildrenAttribute) or []
        if not isinstance(children, (list, tuple)):
            children = [children]
        node["children"] = [
            self._serialize_ax_element(child, depth=depth + 1, max_depth=max_depth)
            for child in children[:25]
        ]
        return node


def _default_screenshot_runner(path: str) -> None:
    subprocess.run(
        ["screencapture", "-x", path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _default_open_runner(args: list[str]) -> None:
    subprocess.run(
        args,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _make_temp_screenshot_path() -> str:
    fd, path = tempfile.mkstemp(prefix="boiled-claw-desktop-", suffix=".png")
    Path(path).unlink(missing_ok=True)
    return path


def _display_size(quartz_module: Any | None) -> tuple[int, int]:
    if quartz_module is None:
        return (0, 0)
    try:
        display_id = quartz_module.CGMainDisplayID()
        width = int(quartz_module.CGDisplayPixelsWide(display_id))
        height = int(quartz_module.CGDisplayPixelsHigh(display_id))
        return (width, height)
    except Exception:
        return (0, 0)


def _title_matches(candidate: Any, requested: Any) -> bool:
    candidate_text = str(candidate or "").strip()
    requested_text = str(requested or "").strip()
    if not candidate_text or not requested_text:
        return False
    candidate_folded = candidate_text.casefold()
    requested_folded = requested_text.casefold()
    return (
        candidate_folded == requested_folded
        or requested_folded in candidate_folded
        or candidate_folded in requested_folded
    )


def _supports_ax_snapshot(quartz_module: Any | None, appkit_module: Any | None) -> bool:
    return (
        quartz_module is not None
        and appkit_module is not None
        and hasattr(quartz_module, "AXUIElementCreateApplication")
        and hasattr(quartz_module, "AXUIElementCopyAttributeValue")
    )


def _supports_open_command() -> bool:
    return shutil.which("open") is not None


def _supports_focus_window(quartz_module: Any | None, appkit_module: Any | None) -> bool:
    return _supports_ax_snapshot(quartz_module, appkit_module) and hasattr(
        quartz_module,
        "AXUIElementPerformAction",
    )


def _supports_mouse_click(quartz_module: Any | None) -> bool:
    return (
        quartz_module is not None
        and hasattr(quartz_module, "CGEventCreateMouseEvent")
        and hasattr(quartz_module, "CGEventPost")
        and hasattr(quartz_module, "kCGHIDEventTap")
    )


def _supports_text_input(quartz_module: Any | None) -> bool:
    return (
        quartz_module is not None
        and hasattr(quartz_module, "CGEventCreateKeyboardEvent")
        and hasattr(quartz_module, "CGEventKeyboardSetUnicodeString")
        and hasattr(quartz_module, "CGEventPost")
        and hasattr(quartz_module, "kCGHIDEventTap")
    )


def _supports_hotkey(quartz_module: Any | None) -> bool:
    return _supports_text_input(quartz_module) and hasattr(quartz_module, "CGEventSetFlags")


def _supports_scroll(quartz_module: Any | None) -> bool:
    return (
        quartz_module is not None
        and hasattr(quartz_module, "CGEventCreateScrollWheelEvent")
        and hasattr(quartz_module, "CGEventPost")
        and hasattr(quartz_module, "kCGHIDEventTap")
    )


def _supports_ax_press(quartz_module: Any | None) -> bool:
    return quartz_module is not None and hasattr(quartz_module, "AXUIElementPerformAction")


def _supports_ax_set_value(quartz_module: Any | None) -> bool:
    return quartz_module is not None and hasattr(quartz_module, "AXUIElementSetAttributeValue")


def _mouse_event_spec(quartz_module: Any, button: str) -> tuple[int, int, int]:
    button_name = button.lower()
    if button_name == "right":
        return (
            quartz_module.kCGEventRightMouseDown,
            quartz_module.kCGEventRightMouseUp,
            quartz_module.kCGMouseButtonRight,
        )
    if button_name == "middle":
        return (
            quartz_module.kCGEventOtherMouseDown,
            quartz_module.kCGEventOtherMouseUp,
            quartz_module.kCGMouseButtonCenter,
        )
    return (
        quartz_module.kCGEventLeftMouseDown,
        quartz_module.kCGEventLeftMouseUp,
        quartz_module.kCGMouseButtonLeft,
    )


def _ax_value(quartz_module: Any, element: Any, attribute: str) -> Any:
    fn = quartz_module.AXUIElementCopyAttributeValue
    try:
        raw = fn(element, attribute, None)
    except TypeError:
        raw = fn(element, attribute)

    if isinstance(raw, tuple):
        if len(raw) == 2:
            error_code, value = raw
            if error_code not in (0, None):
                return None
            return value
        if len(raw) == 1:
            return raw[0]
    return raw


def _perform_ax_action(quartz_module: Any | None, element: Any, action: str) -> bool:
    if quartz_module is None:
        return False
    performer = getattr(quartz_module, "AXUIElementPerformAction", None)
    if not callable(performer):
        return False
    try:
        raw = performer(element, action)
    except Exception:
        return False
    if isinstance(raw, tuple):
        return not raw or raw[0] in (0, None)
    return raw in (0, None, True)


def _set_ax_value(quartz_module: Any | None, element: Any, attribute: str, value: Any) -> bool:
    if quartz_module is None:
        return False
    setter = getattr(quartz_module, "AXUIElementSetAttributeValue", None)
    if not callable(setter):
        return False
    try:
        raw = setter(element, attribute, value)
    except Exception:
        return False
    if isinstance(raw, tuple):
        return not raw or raw[0] in (0, None)
    return raw in (0, None, True)


def _stringify_ax_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_stringify_ax_value(item) for item in value[:10]]
    if isinstance(value, dict):
        return {str(key): _stringify_ax_value(val) for key, val in value.items()}
    return str(value)


def _ax_bounds(quartz_module: Any | None, element: Any) -> DesktopWindowBounds:
    if quartz_module is None:
        return DesktopWindowBounds()
    position = _ax_value(
        quartz_module,
        element,
        getattr(quartz_module, "kAXPositionAttribute", "AXPosition"),
    )
    size = _ax_value(
        quartz_module,
        element,
        getattr(quartz_module, "kAXSizeAttribute", "AXSize"),
    )
    pos = _coerce_point(position)
    dim = _coerce_point(size)
    return DesktopWindowBounds(
        x=pos[0],
        y=pos[1],
        width=dim[0],
        height=dim[1],
    )


def _descriptor_center(descriptor: DesktopTargetDescriptor) -> tuple[int, int] | None:
    if descriptor.bounds.width <= 0 or descriptor.bounds.height <= 0:
        return None
    return (
        descriptor.bounds.x + descriptor.bounds.width // 2,
        descriptor.bounds.y + descriptor.bounds.height // 2,
    )


def _coerce_point(value: Any) -> tuple[int, int]:
    if isinstance(value, dict):
        return (int(value.get("x", 0) or 0), int(value.get("y", 0) or 0))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return (int(value[0] or 0), int(value[1] or 0))
    return (0, 0)


def _element_matches(quartz_module: Any, element: Any, selector: DesktopElementSelector) -> bool:
    role = str(_ax_value(quartz_module, element, quartz_module.kAXRoleAttribute) or "")
    title = str(_ax_value(quartz_module, element, quartz_module.kAXTitleAttribute) or "")
    identifier = str(
        _ax_value(
            quartz_module,
            element,
            getattr(quartz_module, "kAXIdentifierAttribute", "AXIdentifier"),
        )
        or ""
    )
    value = str(_stringify_ax_value(_ax_value(quartz_module, element, quartz_module.kAXValueAttribute)) or "")
    if selector.role and role != selector.role:
        return False
    if selector.title and selector.title not in title:
        return False
    if selector.identifier and selector.identifier != identifier:
        return False
    if selector.value_contains and selector.value_contains not in value:
        return False
    if not any((selector.role, selector.title, selector.identifier, selector.value_contains, selector.window_id)):
        return False
    return True


def _set_event_flags(quartz_module: Any, event: Any, flags: int) -> None:
    setter = getattr(quartz_module, "CGEventSetFlags", None)
    if callable(setter):
        setter(event, flags)
    elif isinstance(event, dict):
        event["flags"] = flags


def _hotkey_spec(quartz_module: Any, keys: list[str]) -> tuple[int | None, int]:
    modifiers = {
        "command": getattr(quartz_module, "kCGEventFlagMaskCommand", 0),
        "cmd": getattr(quartz_module, "kCGEventFlagMaskCommand", 0),
        "shift": getattr(quartz_module, "kCGEventFlagMaskShift", 0),
        "option": getattr(quartz_module, "kCGEventFlagMaskAlternate", 0),
        "alt": getattr(quartz_module, "kCGEventFlagMaskAlternate", 0),
        "control": getattr(quartz_module, "kCGEventFlagMaskControl", 0),
        "ctrl": getattr(quartz_module, "kCGEventFlagMaskControl", 0),
        "fn": getattr(quartz_module, "kCGEventFlagMaskSecondaryFn", 0),
    }
    keycodes = {
        "a": 0,
        "b": 11,
        "c": 8,
        "d": 2,
        "e": 14,
        "f": 3,
        "g": 5,
        "h": 4,
        "i": 34,
        "j": 38,
        "k": 40,
        "l": 37,
        "m": 46,
        "n": 45,
        "o": 31,
        "p": 35,
        "q": 12,
        "r": 15,
        "s": 1,
        "t": 17,
        "u": 32,
        "v": 9,
        "w": 13,
        "x": 7,
        "y": 16,
        "z": 6,
        "0": 29,
        "1": 18,
        "2": 19,
        "3": 20,
        "4": 21,
        "5": 23,
        "6": 22,
        "7": 26,
        "8": 28,
        "9": 25,
        "space": 49,
        "return": 36,
        "enter": 36,
        "tab": 48,
        "escape": 53,
        "esc": 53,
        "left": 123,
        "right": 124,
        "down": 125,
        "up": 126,
    }

    flags = 0
    primary_key: str | None = None
    for key in keys:
        normalized = str(key).strip().lower()
        if normalized in modifiers:
            flags |= modifiers[normalized]
        else:
            primary_key = normalized
    if primary_key is None:
        return None, flags
    return keycodes.get(primary_key), flags


__all__ = ["PyObjCDesktopClient"]
