"""Core desktop runtime models.

These models describe desktop capabilities independently from any transport.
Bridge adapters may re-export or serialize them, but the runtime should not
depend on MCP-specific modules.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from src.bridges.common_schema import (
    BridgePingResult,
    CapabilityDescriptor,
    CapabilityListResult,
)


class DesktopRequestBase(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None


def _normalize_window_id_value(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    return value


class DesktopRuntimeStatusRequest(DesktopRequestBase):
    pass


class DesktopEmergencyStopRequest(DesktopRequestBase):
    reason: Optional[str] = None


class DesktopClearStopRequest(DesktopRequestBase):
    pass


class DesktopRuntimeStatusResult(BaseModel):
    ok: bool = True
    stopped: bool = False
    reason: Optional[str] = None
    stopped_at: Optional[float] = None
    changed: bool = False


class DesktopWindowBounds(BaseModel):
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0


class DesktopElementSelector(BaseModel):
    app_name: Optional[str] = None
    window_id: Optional[str] = None
    role: Optional[str] = None
    title: Optional[str] = None
    identifier: Optional[str] = None
    value_contains: Optional[str] = None
    index: int = Field(default=0, ge=0)

    @field_validator("window_id", mode="before")
    @classmethod
    def normalize_window_id(cls, value: Any) -> Any:
        return _normalize_window_id_value(value)

    @model_validator(mode="after")
    def validate_selector(self) -> "DesktopElementSelector":
        if any((self.role, self.title, self.identifier, self.value_contains, self.window_id)):
            return self
        raise ValueError(
            "desktop selector requires role, title, identifier, value_contains, or window_id"
        )


class DesktopTargetDescriptor(BaseModel):
    app_name: str = ""
    window_id: str = ""
    role: str = ""
    title: str = ""
    identifier: str = ""
    bounds: DesktopWindowBounds = Field(default_factory=DesktopWindowBounds)


class DesktopControlResult(BaseModel):
    ok: bool
    error: Optional[str] = None
    target: Optional[DesktopTargetDescriptor] = None


class DesktopScreenshotRequest(DesktopRequestBase):
    path: Optional[str] = None


class DesktopScreenshotResult(BaseModel):
    ok: bool
    path: Optional[str] = None
    width: int = 0
    height: int = 0
    error: Optional[str] = None


class DesktopWindowDescriptor(BaseModel):
    window_id: str
    app_name: str
    title: str = ""
    bounds: DesktopWindowBounds = Field(default_factory=DesktopWindowBounds)


class DesktopWindowsRequest(DesktopRequestBase):
    include_minimized: bool = False


class DesktopWindowsResult(BaseModel):
    ok: bool
    windows: list[DesktopWindowDescriptor] = Field(default_factory=list)
    error: Optional[str] = None


class DesktopWaitWindowRequest(DesktopRequestBase):
    app_name: Optional[str] = None
    window_id: Optional[str] = None
    title: Optional[str] = None
    timeout_seconds: float = Field(default=5.0, gt=0.0, le=120.0)
    poll_interval_seconds: float = Field(default=0.2, gt=0.0, le=5.0)

    @field_validator("window_id", mode="before")
    @classmethod
    def normalize_window_id(cls, value: Any) -> Any:
        return _normalize_window_id_value(value)

    @model_validator(mode="after")
    def validate_wait_target(self) -> "DesktopWaitWindowRequest":
        if self.app_name or self.window_id or self.title:
            return self
        raise ValueError("desktop wait.window requires app_name, window_id, or title")


class DesktopWaitWindowResult(BaseModel):
    ok: bool
    matched: bool = False
    window: Optional[DesktopWindowDescriptor] = None
    error: Optional[str] = None


class DesktopFrontmostAppRequest(DesktopRequestBase):
    pass


class DesktopFrontmostAppResult(BaseModel):
    ok: bool
    app_name: str = ""
    pid: Optional[int] = None
    error: Optional[str] = None


class DesktopAxSnapshotRequest(DesktopRequestBase):
    app_name: Optional[str] = None
    window_id: Optional[str] = None

    @field_validator("window_id", mode="before")
    @classmethod
    def normalize_window_id(cls, value: Any) -> Any:
        return _normalize_window_id_value(value)


class DesktopAxSnapshotResult(BaseModel):
    ok: bool
    tree: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class DesktopAxFindRequest(DesktopRequestBase):
    target: DesktopElementSelector


class DesktopAxFindResult(BaseModel):
    ok: bool
    matched: bool = False
    target: Optional[DesktopTargetDescriptor] = None
    error: Optional[str] = None


class DesktopWaitElementRequest(DesktopRequestBase):
    target: DesktopElementSelector
    timeout_seconds: float = Field(default=5.0, gt=0.0, le=120.0)
    poll_interval_seconds: float = Field(default=0.2, gt=0.0, le=5.0)


class DesktopWaitElementResult(BaseModel):
    ok: bool
    matched: bool = False
    target: Optional[DesktopTargetDescriptor] = None
    error: Optional[str] = None


class DesktopLaunchAppRequest(DesktopRequestBase):
    app_name: Optional[str] = None
    bundle_id: Optional[str] = None
    wait_for_focus: bool = True

    @model_validator(mode="after")
    def validate_launch_target(self) -> "DesktopLaunchAppRequest":
        if self.app_name or self.bundle_id:
            return self
        raise ValueError("desktop launch requires app_name or bundle_id")


class DesktopFocusWindowRequest(DesktopRequestBase):
    app_name: Optional[str] = None
    window_id: Optional[str] = None
    title: Optional[str] = None

    @field_validator("window_id", mode="before")
    @classmethod
    def normalize_window_id(cls, value: Any) -> Any:
        return _normalize_window_id_value(value)

    @model_validator(mode="after")
    def validate_focus_target(self) -> "DesktopFocusWindowRequest":
        if self.app_name or self.window_id or self.title:
            return self
        raise ValueError("desktop focus requires app_name, window_id, or title")


class DesktopClickRequest(DesktopRequestBase):
    x: Optional[int] = None
    y: Optional[int] = None
    button: Literal["left", "right", "middle"] = "left"
    click_count: int = Field(default=1, ge=1, le=4)
    target: Optional[DesktopElementSelector] = None

    @model_validator(mode="after")
    def validate_click_target(self) -> "DesktopClickRequest":
        if self.target is not None:
            return self
        if self.x is not None and self.y is not None:
            return self
        raise ValueError("desktop click requires coordinates or a selector target")


class DesktopTypeRequest(DesktopRequestBase):
    text: str = Field(min_length=1)
    target: Optional[DesktopElementSelector] = None


class DesktopHotkeyRequest(DesktopRequestBase):
    keys: list[str] = Field(min_length=1)


class DesktopScrollRequest(DesktopRequestBase):
    delta_x: int = 0
    delta_y: int = 0

    @model_validator(mode="after")
    def validate_scroll_delta(self) -> "DesktopScrollRequest":
        if self.delta_x == 0 and self.delta_y == 0:
            raise ValueError("desktop scroll requires non-zero delta_x or delta_y")
        return self


class DesktopDragRequest(DesktopRequestBase):
    start_x: int
    start_y: int
    end_x: int
    end_y: int


__all__ = [
    "BridgePingResult",
    "CapabilityDescriptor",
    "CapabilityListResult",
    "DesktopRequestBase",
    "DesktopRuntimeStatusRequest",
    "DesktopEmergencyStopRequest",
    "DesktopClearStopRequest",
    "DesktopRuntimeStatusResult",
    "DesktopElementSelector",
    "DesktopTargetDescriptor",
    "DesktopControlResult",
    "DesktopScreenshotRequest",
    "DesktopScreenshotResult",
    "DesktopWindowBounds",
    "DesktopWindowDescriptor",
    "DesktopWindowsRequest",
    "DesktopWindowsResult",
    "DesktopWaitWindowRequest",
    "DesktopWaitWindowResult",
    "DesktopFrontmostAppRequest",
    "DesktopFrontmostAppResult",
    "DesktopAxSnapshotRequest",
    "DesktopAxSnapshotResult",
    "DesktopAxFindRequest",
    "DesktopAxFindResult",
    "DesktopWaitElementRequest",
    "DesktopWaitElementResult",
    "DesktopLaunchAppRequest",
    "DesktopFocusWindowRequest",
    "DesktopClickRequest",
    "DesktopTypeRequest",
    "DesktopHotkeyRequest",
    "DesktopScrollRequest",
    "DesktopDragRequest",
]
