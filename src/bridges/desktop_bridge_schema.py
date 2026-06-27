"""Compatibility re-exports for desktop bridge schemas.

Desktop runtime models now live under ``src.desktop.models`` so the core
runtime does not depend on bridge-specific modules.
"""

from src.desktop.models import (
    BridgePingResult,
    CapabilityDescriptor,
    CapabilityListResult,
    DesktopAxSnapshotRequest,
    DesktopAxSnapshotResult,
    DesktopClickRequest,
    DesktopControlResult,
    DesktopDragRequest,
    DesktopFrontmostAppRequest,
    DesktopFrontmostAppResult,
    DesktopHotkeyRequest,
    DesktopRequestBase,
    DesktopScreenshotRequest,
    DesktopScreenshotResult,
    DesktopTypeRequest,
    DesktopWindowBounds,
    DesktopWindowDescriptor,
    DesktopWindowsRequest,
    DesktopWindowsResult,
)

__all__ = [
    "BridgePingResult",
    "CapabilityDescriptor",
    "CapabilityListResult",
    "DesktopRequestBase",
    "DesktopControlResult",
    "DesktopScreenshotRequest",
    "DesktopScreenshotResult",
    "DesktopWindowBounds",
    "DesktopWindowDescriptor",
    "DesktopWindowsRequest",
    "DesktopWindowsResult",
    "DesktopFrontmostAppRequest",
    "DesktopFrontmostAppResult",
    "DesktopAxSnapshotRequest",
    "DesktopAxSnapshotResult",
    "DesktopClickRequest",
    "DesktopTypeRequest",
    "DesktopHotkeyRequest",
    "DesktopDragRequest",
]
