"""Desktop runtime client abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection

from src.desktop.models import (
    DesktopAxFindRequest,
    DesktopAxFindResult,
    CapabilityDescriptor,
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
    DesktopTypeRequest,
    DesktopWaitElementRequest,
    DesktopWaitElementResult,
    DesktopWaitWindowRequest,
    DesktopWaitWindowResult,
    DesktopWindowsRequest,
    DesktopWindowsResult,
)


DESKTOP_NOT_IMPLEMENTED = "Desktop runtime capability is not implemented yet."


def desktop_capabilities(
    implemented: Collection[str] | None = None,
) -> CapabilityListResult:
    implemented_names = set(implemented or ())
    descriptors = [
        CapabilityDescriptor(
            name="desktop.runtime.status",
            risk="low",
            requires_approval=False,
            description="Inspect desktop runtime emergency stop state.",
            implemented="desktop.runtime.status" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.runtime.stop",
            risk="low",
            requires_approval=False,
            description="Trigger desktop runtime emergency stop.",
            implemented="desktop.runtime.stop" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.runtime.clear_stop",
            risk="high",
            requires_approval=True,
            description="Clear desktop runtime emergency stop and re-enable control.",
            implemented="desktop.runtime.clear_stop" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.view.screenshot",
            risk="medium",
            requires_approval=True,
            description="Capture a desktop screenshot from the host OS.",
            implemented="desktop.view.screenshot" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.view.windows",
            risk="low",
            requires_approval=True,
            description="List visible windows on the host OS.",
            implemented="desktop.view.windows" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.wait.window",
            risk="low",
            requires_approval=False,
            description="Wait for a matching window to appear on the host OS.",
            implemented="desktop.wait.window" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.view.frontmost_app",
            risk="low",
            requires_approval=True,
            description="Inspect the frontmost app on the host OS.",
            implemented="desktop.view.frontmost_app" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.ax.find",
            risk="low",
            requires_approval=False,
            description="Resolve a matching accessibility element on the host OS.",
            implemented="desktop.ax.find" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.wait.element",
            risk="low",
            requires_approval=False,
            description="Wait for a matching accessibility element on the host OS.",
            implemented="desktop.wait.element" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.ax.snapshot",
            risk="medium",
            requires_approval=True,
            description="Capture an accessibility tree snapshot from the host OS.",
            implemented="desktop.ax.snapshot" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.control.click",
            risk="high",
            requires_approval=True,
            description="Click on the host desktop or a matched accessibility element.",
            implemented="desktop.control.click" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.control.type",
            risk="high",
            requires_approval=True,
            description="Type text into the host desktop or a matched accessibility element.",
            implemented="desktop.control.type" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.control.launch_app",
            risk="high",
            requires_approval=True,
            description="Launch an app on the host desktop.",
            implemented="desktop.control.launch_app" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.control.focus_window",
            risk="high",
            requires_approval=True,
            description="Focus a window or app on the host desktop.",
            implemented="desktop.control.focus_window" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.control.hotkey",
            risk="high",
            requires_approval=True,
            description="Send a hotkey to the host desktop.",
            implemented="desktop.control.hotkey" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.control.scroll",
            risk="high",
            requires_approval=True,
            description="Scroll on the host desktop.",
            implemented="desktop.control.scroll" in implemented_names,
        ),
        CapabilityDescriptor(
            name="desktop.control.drag",
            risk="high",
            requires_approval=True,
            description="Drag the pointer on the host desktop.",
            implemented="desktop.control.drag" in implemented_names,
        ),
    ]
    return CapabilityListResult(capabilities=descriptors)


class DesktopClient(ABC):
    """Transport-agnostic desktop runtime interface."""

    @abstractmethod
    async def capabilities(self) -> CapabilityListResult:
        raise NotImplementedError

    @abstractmethod
    async def runtime_status(
        self, request: DesktopRuntimeStatusRequest
    ) -> DesktopRuntimeStatusResult:
        raise NotImplementedError

    @abstractmethod
    async def emergency_stop(
        self, request: DesktopEmergencyStopRequest
    ) -> DesktopRuntimeStatusResult:
        raise NotImplementedError

    @abstractmethod
    async def clear_stop(
        self, request: DesktopClearStopRequest
    ) -> DesktopRuntimeStatusResult:
        raise NotImplementedError

    @abstractmethod
    async def screenshot(
        self, request: DesktopScreenshotRequest
    ) -> DesktopScreenshotResult:
        raise NotImplementedError

    @abstractmethod
    async def windows(self, request: DesktopWindowsRequest) -> DesktopWindowsResult:
        raise NotImplementedError

    @abstractmethod
    async def wait_window(
        self, request: DesktopWaitWindowRequest
    ) -> DesktopWaitWindowResult:
        raise NotImplementedError

    @abstractmethod
    async def frontmost_app(
        self, request: DesktopFrontmostAppRequest
    ) -> DesktopFrontmostAppResult:
        raise NotImplementedError

    @abstractmethod
    async def ax_snapshot(
        self, request: DesktopAxSnapshotRequest
    ) -> DesktopAxSnapshotResult:
        raise NotImplementedError

    @abstractmethod
    async def ax_find(self, request: DesktopAxFindRequest) -> DesktopAxFindResult:
        raise NotImplementedError

    @abstractmethod
    async def wait_element(
        self, request: DesktopWaitElementRequest
    ) -> DesktopWaitElementResult:
        raise NotImplementedError

    @abstractmethod
    async def click(self, request: DesktopClickRequest) -> DesktopControlResult:
        raise NotImplementedError

    @abstractmethod
    async def type_text(self, request: DesktopTypeRequest) -> DesktopControlResult:
        raise NotImplementedError

    @abstractmethod
    async def launch_app(
        self, request: DesktopLaunchAppRequest
    ) -> DesktopControlResult:
        raise NotImplementedError

    @abstractmethod
    async def focus_window(
        self, request: DesktopFocusWindowRequest
    ) -> DesktopControlResult:
        raise NotImplementedError

    @abstractmethod
    async def hotkey(self, request: DesktopHotkeyRequest) -> DesktopControlResult:
        raise NotImplementedError

    @abstractmethod
    async def scroll(self, request: DesktopScrollRequest) -> DesktopControlResult:
        raise NotImplementedError

    @abstractmethod
    async def drag(self, request: DesktopDragRequest) -> DesktopControlResult:
        raise NotImplementedError


__all__ = ["DESKTOP_NOT_IMPLEMENTED", "DesktopClient", "desktop_capabilities"]
