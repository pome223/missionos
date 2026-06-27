"""Host Bridge v1 schema definitions."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field
from src.bridges.common_schema import (
    BridgePingResult,
    CapabilityDescriptor,
    CapabilityListResult,
)


class HostShellRunRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    command: str = Field(min_length=1)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    cwd: Optional[str] = None


class HostShellRunResult(BaseModel):
    ok: bool
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0
    timed_out: bool = False
    intent: Optional[str] = None
    risk: Optional[Literal["low", "medium", "high"]] = None
    summary: Optional[str] = None
    error: Optional[str] = None


class HostFileReadRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    path: str = Field(min_length=1)


class HostFileReadResult(BaseModel):
    ok: bool
    path: Optional[str] = None
    content: str = ""
    size: int = 0
    error: Optional[str] = None


class HostFileWriteRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    path: str = Field(min_length=1)
    content: str = ""


class HostFileWriteResult(BaseModel):
    ok: bool
    path: Optional[str] = None
    size: int = 0
    error: Optional[str] = None


class HostFileListRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    path: str = Field(min_length=1)


class HostFileEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int


class HostFileListResult(BaseModel):
    ok: bool
    path: Optional[str] = None
    entries: list[HostFileEntry] = Field(default_factory=list)
    error: Optional[str] = None


class HostBrowserNavigateRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    url: str = Field(min_length=1)
    wait_for: str = Field(default="load", min_length=1)
    timeout: int = Field(default=30000, ge=1, le=300000)
    visible: Optional[bool] = None


class HostBrowserNavigateResult(BaseModel):
    ok: bool
    url: Optional[str] = None
    title: str = ""
    status: Optional[int] = None
    error: Optional[str] = None


class HostBrowserClickRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    selector: str = Field(min_length=1)
    timeout: int = Field(default=30000, ge=1, le=300000)


class HostBrowserClickResult(BaseModel):
    ok: bool
    selector: Optional[str] = None
    error: Optional[str] = None


class HostBrowserFillRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    selector: str = Field(min_length=1)
    text: str = ""
    timeout: int = Field(default=30000, ge=1, le=300000)


class HostBrowserFillResult(BaseModel):
    ok: bool
    selector: Optional[str] = None
    text_length: int = 0
    error: Optional[str] = None


class HostBrowserPressRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    key: str = Field(min_length=1)
    selector: Optional[str] = None
    timeout: int = Field(default=30000, ge=1, le=300000)


class HostBrowserPressResult(BaseModel):
    ok: bool
    key: str = ""
    selector: Optional[str] = None
    error: Optional[str] = None


class HostBrowserScreenshotRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    path: Optional[str] = None
    full_page: bool = False


class HostBrowserScreenshotResult(BaseModel):
    ok: bool
    path: Optional[str] = None
    full_page: bool = False
    error: Optional[str] = None


class HostBrowserExtractTextRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    selector: Optional[str] = None


class HostBrowserExtractTextResult(BaseModel):
    ok: bool
    text: str = ""
    selector: str = "body"
    length: int = 0
    error: Optional[str] = None


class HostControlUiChatSendMessageRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    url: str = Field(min_length=1)
    message: str = Field(min_length=1)
    timeout_ms: int = Field(default=90000, ge=1000, le=300000)
    connect_timeout_ms: int = Field(default=15000, ge=1000, le=120000)
    stable_wait_ms: int = Field(default=800, ge=100, le=10000)
    visible: bool = True


class HostControlUiChatSendMessageResult(BaseModel):
    ok: bool
    url: Optional[str] = None
    title: str = ""
    message: str = ""
    assistant_reply: str = ""
    connected: bool = False
    agent_bubble_count: int = 0
    error: Optional[str] = None


class HostCurrentTabInfoRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None


class HostCurrentTabInfoResult(BaseModel):
    ok: bool
    tab_id: Optional[int] = None
    window_id: Optional[int] = None
    url: str = ""
    title: str = ""
    error: Optional[str] = None


class HostCurrentTabNavigateRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    url: str = Field(min_length=1)
    timeout_ms: int = Field(default=15000, ge=1000, le=120000)
    new_tab: bool = False
    target_tab_id: Optional[int] = None


class HostCurrentTabNavigateResult(BaseModel):
    ok: bool
    tab_id: Optional[int] = None
    window_id: Optional[int] = None
    url: str = ""
    title: str = ""
    error: Optional[str] = None


class HostCurrentTabListTabsRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None


class HostCurrentTabListEntry(BaseModel):
    tab_id: Optional[int] = None
    window_id: Optional[int] = None
    url: str = ""
    title: str = ""
    active: bool = False
    index: Optional[int] = None


class HostCurrentTabListTabsResult(BaseModel):
    ok: bool
    tabs: list[HostCurrentTabListEntry] = Field(default_factory=list)
    error: Optional[str] = None


class HostCurrentTabActivateRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    tab_id: int


class HostCurrentTabActivateResult(BaseModel):
    ok: bool
    tab_id: Optional[int] = None
    window_id: Optional[int] = None
    url: str = ""
    title: str = ""
    error: Optional[str] = None
    # Surfaced when the tab-focus step succeeded but the subsequent
    # window-focus step (chrome.windows.update focused=true) failed.
    # Lets verifier/caller distinguish "tab active in background window"
    # from "tab active and window foreground".
    window_focus_error: Optional[str] = None


class HostCurrentTabClickRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    selector: str = Field(min_length=1)


class HostCurrentTabClickResult(BaseModel):
    ok: bool
    selector: Optional[str] = None
    error: Optional[str] = None


class HostCurrentTabFillRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    selector: str = Field(min_length=1)
    text: str = ""


class HostCurrentTabFillResult(BaseModel):
    ok: bool
    selector: Optional[str] = None
    text_length: int = 0
    error: Optional[str] = None


class HostCurrentTabExtractTextRequest(BaseModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    approval_token: Optional[str] = None
    selector: Optional[str] = None
    target_tab_id: Optional[int] = None


class HostCurrentTabExtractTextResult(BaseModel):
    ok: bool
    selector: str = "body"
    text: str = ""
    length: int = 0
    error: Optional[str] = None
