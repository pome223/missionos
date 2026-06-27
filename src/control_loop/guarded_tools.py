"""
Guarded Tools — boiled-claw v2

ToolContext.state で approval:status を確認してから実行する
policy-aware tool ラッパー。

Executor agent にアタッチし、approved plan の範囲外の実行を防ぐ。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote_plus, urlsplit

from google.adk.tools import ToolContext

from src.gateway.routing import targets_user_browser
from src.runtime.state_keys import StateKeys
from src.runtime.task_keywords import (
    SPREADSHEET_KEYWORDS,
    prefers_isolated_browser_for_goal,
)

_APPROVED_STATUSES = {"policy_approved", "human_approved", "auto_approved"}
_IMPLICIT_PLAN_CAPABILITIES = {
    "current_tab.info": {
        "current_tab.navigate",
    },
    # Removed desktop.control.launch_app from view.windows / frontmost_app /
    # wait.window implicit grants.  launch_app requires an explicit capability
    # entry in the plan.  Plans that genuinely need launch_app (e.g. media
    # playback tasks) receive it explicitly via _normalize_required_capabilities.
    # Keeping the implicit grant was causing current-tab spreadsheet tasks to
    # have launch_app implicitly allowed via desktop.view.frontmost_app.
    "desktop.view.windows": {
        "desktop.control.focus_window",
        "desktop.wait.window",
    },
    "desktop.view.frontmost_app": {
        "desktop.control.focus_window",
        "desktop.wait.window",
    },
    "desktop.wait.window": {
        "desktop.control.focus_window",
    },
    "desktop.ax.find": {
        "desktop.control.click",
        "desktop.control.type",
        "desktop.wait.element",
    },
    "desktop.wait.element": {
        "desktop.control.click",
        "desktop.control.type",
        "desktop.ax.find",
    },
}
_CURRENT_BROWSER_ALLOWED_HOTKEYS = {
    ("control", "l"),
    ("enter",),
    ("l", "meta"),
}
_CURRENT_BROWSER_NEW_TAB_HOTKEYS = {
    ("control", "t"),
    ("meta", "t"),
}
# Hotkeys safe for spreadsheet cell editing when new-tab mode is active.
_CURRENT_BROWSER_NEWTAB_EDITING_HOTKEYS = {
    ("escape",),
    ("tab",),
    ("enter",),
    ("shift", "tab"),
    ("down",), ("up",), ("left",), ("right",),
    ("control", "f"), ("f", "meta"),
    ("enter", "shift"),        # sorted form of Shift+Enter
    ("control", "enter"), ("enter", "meta"),
    ("control", "z"), ("meta", "z"),
    ("control", "s"), ("meta", "s"),
}
_CURRENT_BROWSER_SPREADSHEET_CELL_READY_HOTKEYS = {
    ("tab",),
    ("enter",),
    ("shift", "tab"),
    ("down",),
    ("up",),
    ("left",),
    ("right",),
    ("enter", "shift"),
    ("control", "enter"),
    ("enter", "meta"),
}
_GOOGLE_SHEETS_OVERLAY_MARKERS = (
    "introducing conversation history",
    "generate a custom spreadsheet",
    "ask gemini",
    "gemini in workspace",
    "build",
)
_GOOGLE_SHEETS_OVERLAY_DISMISS_SELECTORS = (
    'button[aria-label="Got it"]',
    '[role="button"][aria-label="Got it"]',
    'button[aria-label="Close"]',
    '[role="button"][aria-label="Close"]',
    '[aria-label="Close"]',
)
_CURRENT_BROWSER_GOOGLE_SHEETS_URL_MARKERS = (
    "docs.google.com/spreadsheets",
    "sheets.new",
)
_CURRENT_BROWSER_GOOGLE_SHEETS_TITLE_MARKERS = (
    "google sheets",
    "spreadsheet",
)
_CURRENT_BROWSER_SPREADSHEET_TEXT_FIELD_ROLES = {
    "axcombobox",
    "axsearchfield",
    "axtextarea",
    "axtextfield",
}
_CURRENT_BROWSER_SPREADSHEET_SAFE_TEXT_TARGET_MARKERS = (
    "cell-",
    "formula",
    "formula bar",
    "fx",
    "name box",
    "name-box",
    "name_box",
    "セル参照",
    "名前ボックス",
)
_CURRENT_BROWSER_SPREADSHEET_UNSAFE_TEXT_TARGET_MARKERS = (
    "document title",
    "file name",
    "spreadsheet title",
    "untitled spreadsheet",
)
_HOTKEY_ALIASES = {
    "arrowdown": "down",
    "arrow_down": "down",
    "down_arrow": "down",
    "arrow-down": "down",
    "down-arrow": "down",
    "arrowleft": "left",
    "arrow_left": "left",
    "left_arrow": "left",
    "arrow-left": "left",
    "left-arrow": "left",
    "arrowright": "right",
    "arrow_right": "right",
    "right_arrow": "right",
    "arrow-right": "right",
    "right-arrow": "right",
    "arrowup": "up",
    "arrow_up": "up",
    "up_arrow": "up",
    "arrow-up": "up",
    "up-arrow": "up",
    "cmd": "meta",
    "command": "meta",
    "control": "control",
    "ctrl": "control",
    "return": "enter",
}
_CURRENT_BROWSER_HOTKEY_REWRITES = {
    ("control", "e"): ["control", "l"],
    ("control", "k"): ["control", "l"],
    ("e", "meta"): ["meta", "l"],
    ("k", "meta"): ["meta", "l"],
}
# Keep current-tab navigation/editing guarded by the existing umbrella
# capability, while allowing read-only current-tab probes to use the narrower
# current_tab.info capability.
_CURRENT_TAB_CAPABILITY = "current_tab.navigate"
_CURRENT_TAB_INFO_CAPABILITY = "current_tab.info"
_KNOWN_BROWSER_APPS = {
    "Google Chrome",
    "Chromium",
    "Safari",
    "Arc",
    "Firefox",
    "Brave Browser",
    "Microsoft Edge",
}
_PRESERVE_CONTROL_UI_MARKER = "preserve that tab and open a new tab in the same browser window"
_CURRENT_BROWSER_ADDRESS_BAR_STATE_KEY = "temp:current_browser_address_bar_focused"
_CURRENT_BROWSER_NEW_TAB_COUNT_LIMIT = 1
_CURRENT_BROWSER_NEW_TAB_COUNT_LIMIT_MAX = 2
_CURRENT_BROWSER_NEW_TAB_VERIFY_ATTEMPTS = 5
_CURRENT_BROWSER_NEW_TAB_VERIFY_DELAY_SECONDS = 0.4
_CURRENT_BROWSER_NEW_TAB_STEP_MARKERS = (
    "new tab",
    "another new tab",
    "新しいタブ",
    "新規タブ",
)
_CURRENT_BROWSER_CONTROL_UI_TITLE_HINTS = (
    "boiled-claw Control UI",
    "boiled-claw",
)
_CURRENT_BROWSER_CONTROL_UI_URL_MARKERS = (
    "localhost:18789/chat",
    "127.0.0.1:18789/chat",
)
_ADDRESS_BAR_FALLBACK_QUERY_MAX_CHARS = 120
_CURRENT_BROWSER_SEARCH_KEYWORDS = {
    "search",
    "weather",
    "pollen",
    "latest",
    "latest news",
    "forecast",
    "research",
    "調べ",
    "検索",
    "花粉",
    "天気",
    "最新",
}
_CURRENT_BROWSER_SAFE_SEARCH_HOST_MARKERS = (
    "google.com/search",
    "www.google.com/search",
    "google.co.jp/search",
    "www.google.co.jp/search",
)
_CURRENT_TAB_EXTENSION_DISCONNECTED_KIND = "current_tab_extension_disconnected"
_CURRENT_TAB_EXTENSION_DISCONNECTED_MARKERS = (
    "current tab extension disconnected",
    "extension disconnected",
)
_PLAYBACK_TASK_KEYWORDS = {
    "djay",
    "spotify",
    "apple music",
    "itunes",
    "music",
    "song",
    "track",
    "playlist",
    "playback",
    "audio",
    "media",
    "再生",
    "停止",
    "止めて",
    "一時停止",
    "曲",
    "楽曲",
    "音楽",
    "プレイリスト",
    "かけて",
    "流して",
}
_PLAYBACK_APP_NAME_HINTS = (
    ("djay", "djay Pro"),
    ("spotify", "Spotify"),
    ("apple music", "Music"),
    ("itunes", "Music"),
    ("music", "Music"),
)


def _check_approval(tool_context: ToolContext, capability: str) -> None:
    """
    approval:status を確認し、未承認なら PermissionError を上げる。
    """
    status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
    if status not in _APPROVED_STATUSES:
        raise PermissionError(
            f"Tool '{capability}' blocked: plan approval status is '{status}'. "
            "Requires policy_approved, human_approved, or auto_approved."
        )


def _check_capability_in_plan(
    tool_context: ToolContext, capability_name: str
) -> None:
    """
    plan:approved に capability が含まれているか確認する。
    """
    plan = _approved_plan(tool_context)
    if plan is None:
        raise PermissionError("No approved plan in session state.")
    required = {
        cap.get("name", "") for cap in plan.get("required_capabilities", [])
    }
    implied_by = _IMPLICIT_PLAN_CAPABILITIES.get(capability_name, set())
    if capability_name not in required and not (required & implied_by):
        raise PermissionError(
            f"Capability '{capability_name}' is not in the approved plan."
        )


def _memory_entry_to_result(entry: Any) -> dict[str, Any]:
    content = getattr(entry, "content", None)
    parts = getattr(content, "parts", None) or []
    text = "\n".join(
        part.text for part in parts if getattr(part, "text", None)
    ).strip()
    return {
        "content": text,
        "author": getattr(entry, "author", None),
        "timestamp": getattr(entry, "timestamp", None),
    }


def _is_current_browser_task(tool_context: ToolContext | None) -> bool:
    if tool_context is None:
        return False
    goal = tool_context.state.get(StateKeys.TASK_GOAL, "")
    if (
        isinstance(goal, str)
        and targets_user_browser(goal)
        and not prefers_isolated_browser_for_goal(goal)
    ):
        return True
    plan = _approved_plan(tool_context)
    if not isinstance(plan, dict):
        return False
    required = {
        str(cap.get("name", "")).strip()
        for cap in plan.get("required_capabilities", [])
        if isinstance(cap, dict)
    }
    return "current_tab.navigate" in required


def _approved_plan(tool_context: ToolContext | None) -> dict[str, Any] | None:
    if tool_context is None:
        return None
    raw_plan = tool_context.state.get(StateKeys.PLAN_APPROVED)
    if raw_plan is None:
        return None
    try:
        return raw_plan if isinstance(raw_plan, dict) else json.loads(raw_plan)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PermissionError("Approved plan is not valid JSON.") from exc


def _plan_allows_capability(
    tool_context: ToolContext | None,
    capability_name: str,
) -> bool:
    plan = _approved_plan(tool_context)
    if not isinstance(plan, dict):
        return False
    required = {
        str(cap.get("name", "")).strip()
        for cap in plan.get("required_capabilities", [])
        if isinstance(cap, dict)
    }
    implied_by = _IMPLICIT_PLAN_CAPABILITIES.get(capability_name, set())
    return capability_name in required or bool(required & implied_by)


def _result_succeeded(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    return bool(result.get("success") or result.get("ok"))


def _error_text(result: object) -> str:
    if isinstance(result, dict):
        parts: list[str] = []
        for key in ("error", "message", "detail", "reason"):
            value = result.get(key)
            if value:
                parts.append(str(value))
        return " ".join(parts).strip()
    return str(result or "").strip()


def _current_browser_tab_snapshot(
    result: object,
    *,
    source: str,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    snapshot: dict[str, Any] = {"source": source}
    for key in ("tab_id", "window_id"):
        value = result.get(key)
        if value is not None:
            snapshot[key] = value
    for key in ("url", "title"):
        value = str(result.get(key) or "").strip()
        if value:
            snapshot[key] = value
    url = str(snapshot.get("url") or "").strip()
    title = str(snapshot.get("title") or "").strip()
    if _is_current_browser_control_ui_tab(url, title):
        snapshot["surface"] = "control_ui"
    elif _is_current_browser_google_sheets_tab(url, title):
        snapshot["surface"] = "google_sheets"
    return snapshot


def _is_current_browser_google_sheets_tab(url: str, title: str) -> bool:
    lowered_url = url.lower()
    lowered_title = title.lower()
    return any(marker in lowered_url for marker in _CURRENT_BROWSER_GOOGLE_SHEETS_URL_MARKERS) or any(
        marker in lowered_title for marker in _CURRENT_BROWSER_GOOGLE_SHEETS_TITLE_MARKERS
    )


def _record_current_browser_tab_state(
    tool_context: ToolContext | None,
    result: object,
    *,
    source: str,
) -> None:
    if tool_context is None or not _is_current_browser_task(tool_context):
        return
    if not _result_succeeded(result):
        return

    snapshot = _current_browser_tab_snapshot(result, source=source)
    if not snapshot:
        return

    tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_LAST_OBSERVED_TAB] = snapshot

    surface = str(snapshot.get("surface") or "").strip()
    if surface == "control_ui":
        tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_CONTROL_UI_TAB] = snapshot
        return
    if surface == "google_sheets":
        if source == "current_tab.navigate":
            _clear_current_browser_spreadsheet_target(tool_context)
            # Fresh Google Sheets tabs land with A1 selected by default. Prime
            # one selector-less type so the first header/value can go into the
            # grid even if the model skips an explicit Name Box click.
            _set_current_browser_spreadsheet_cell_edit_ready(tool_context, True)
        elif source == "current_tab.activate":
            _clear_current_browser_spreadsheet_target(tool_context)
        tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_DESTINATION_TAB] = snapshot


def _is_current_tab_extension_disconnected_result(result: object) -> bool:
    text = _error_text(result).lower()
    return any(marker in text for marker in _CURRENT_TAB_EXTENSION_DISCONNECTED_MARKERS)


def _mark_current_tab_extension_disconnected(
    tool_context: ToolContext | None,
    raw_result: object | None = None,
) -> None:
    if tool_context is None:
        return
    tool_context.state[StateKeys.TEMP_CURRENT_TAB_EXTENSION_DISCONNECTED] = True
    raw_error = _error_text(raw_result)
    if raw_error:
        tool_context.state[
            StateKeys.TEMP_CURRENT_TAB_EXTENSION_DISCONNECTED_RAW_ERROR
        ] = raw_error


def _is_current_tab_extension_disconnected(
    tool_context: ToolContext | None,
) -> bool:
    if tool_context is None:
        return False
    return bool(
        tool_context.state.get(StateKeys.TEMP_CURRENT_TAB_EXTENSION_DISCONNECTED)
    )


def _current_tab_extension_disconnected_message() -> str:
    return (
        "Current Tab extension is not connected to the relay. Reload the "
        "current_tab_adapter extension in chrome://extensions, then open its "
        "Service Worker or popup so it reconnects to ws://127.0.0.1:8768. "
        "No current-browser action was retried because this is a "
        "transport-level failure."
    )


def _current_tab_extension_disconnected_error(tool_name: str) -> dict[str, Any]:
    return {
        "ok": False,
        "success": False,
        "error": _current_tab_extension_disconnected_message(),
        "tool": tool_name,
        "transport_available": False,
        "failure_kind": _CURRENT_TAB_EXTENSION_DISCONNECTED_KIND,
        "non_retriable": True,
        "retryable": False,
        "abort_current_browser_action": True,
    }


def _is_non_retriable_transport_error(result: object) -> bool:
    if isinstance(result, dict):
        if result.get("non_retriable") is True:
            return True
        if result.get("failure_kind") == _CURRENT_TAB_EXTENSION_DISCONNECTED_KIND:
            return True
    return _is_current_tab_extension_disconnected_result(result)


def _abort_current_browser_action_if_current_tab_unavailable(
    tool_context: ToolContext | None,
) -> None:
    if tool_context is None or not _is_current_browser_task(tool_context):
        return
    if _is_current_tab_extension_disconnected(tool_context):
        raise PermissionError(_current_tab_extension_disconnected_message())


async def _call_current_tab_info(
    tool_context: ToolContext | None,
) -> dict[str, Any]:
    if _is_current_tab_extension_disconnected(tool_context):
        return _current_tab_extension_disconnected_error("host.current_tab.info")

    from src.tools.current_tab import current_tab_info

    result = await current_tab_info(tool_context=tool_context)
    if _is_current_tab_extension_disconnected_result(result):
        _mark_current_tab_extension_disconnected(tool_context, result)
        return _current_tab_extension_disconnected_error("host.current_tab.info")
    _record_current_browser_tab_state(
        tool_context,
        result,
        source="current_tab.info",
    )
    return result if isinstance(result, dict) else {"success": False, "error": str(result)}


async def _call_current_tab_activate(
    tab_id: int,
    tool_context: ToolContext | None,
) -> dict[str, Any]:
    if _is_current_tab_extension_disconnected(tool_context):
        return _current_tab_extension_disconnected_error("host.current_tab.activate")

    from src.tools.current_tab import current_tab_activate

    result = await current_tab_activate(tab_id, tool_context=tool_context)
    if _is_current_tab_extension_disconnected_result(result):
        _mark_current_tab_extension_disconnected(tool_context, result)
        return _current_tab_extension_disconnected_error("host.current_tab.activate")
    _record_current_browser_tab_state(
        tool_context,
        result,
        source="current_tab.activate",
    )
    return result if isinstance(result, dict) else {"success": False, "error": str(result)}


async def _call_current_tab_navigate(
    url: str,
    *,
    timeout_ms: int,
    new_tab: bool,
    tool_context: ToolContext | None,
) -> dict[str, Any]:
    if _is_current_tab_extension_disconnected(tool_context):
        return _current_tab_extension_disconnected_error("host.current_tab.navigate")

    from src.tools.current_tab import current_tab_navigate

    result = await current_tab_navigate(
        url,
        timeout_ms=timeout_ms,
        new_tab=new_tab,
        tool_context=tool_context,
    )
    if _is_current_tab_extension_disconnected_result(result):
        _mark_current_tab_extension_disconnected(tool_context, result)
        return _current_tab_extension_disconnected_error("host.current_tab.navigate")
    _record_current_browser_tab_state(
        tool_context,
        result,
        source="current_tab.navigate",
    )
    return result if isinstance(result, dict) else {"success": False, "error": str(result)}


async def _call_current_tab_extract_text(
    selector: str | None,
    tool_context: ToolContext | None,
) -> dict[str, Any]:
    if _is_current_tab_extension_disconnected(tool_context):
        return _current_tab_extension_disconnected_error("host.current_tab.extract_text")

    from src.tools.current_tab import current_tab_extract_text

    result = await current_tab_extract_text(selector=selector, tool_context=tool_context)
    if _is_current_tab_extension_disconnected_result(result):
        _mark_current_tab_extension_disconnected(tool_context, result)
        return _current_tab_extension_disconnected_error("host.current_tab.extract_text")
    return result if isinstance(result, dict) else {"success": False, "error": str(result)}


async def _call_current_tab_click(
    selector: str,
    tool_context: ToolContext | None,
) -> dict[str, Any]:
    if _is_current_tab_extension_disconnected(tool_context):
        return _current_tab_extension_disconnected_error("host.current_tab.click")

    from src.tools.current_tab import current_tab_click

    result = await current_tab_click(selector, tool_context=tool_context)
    if _is_current_tab_extension_disconnected_result(result):
        _mark_current_tab_extension_disconnected(tool_context, result)
        return _current_tab_extension_disconnected_error("host.current_tab.click")
    return result if isinstance(result, dict) else {"success": False, "error": str(result)}


async def _call_current_tab_fill(
    selector: str,
    text: str,
    tool_context: ToolContext | None,
) -> dict[str, Any]:
    if _is_current_tab_extension_disconnected(tool_context):
        return _current_tab_extension_disconnected_error("host.current_tab.fill")

    from src.tools.current_tab import current_tab_fill

    result = await current_tab_fill(selector, text, tool_context=tool_context)
    if _is_current_tab_extension_disconnected_result(result):
        _mark_current_tab_extension_disconnected(tool_context, result)
        return _current_tab_extension_disconnected_error("host.current_tab.fill")
    return result if isinstance(result, dict) else {"success": False, "error": str(result)}


def _current_browser_new_tab_count(tool_context: ToolContext | None) -> int:
    if tool_context is None:
        return 0
    raw = tool_context.state.get(StateKeys.TEMP_CURRENT_BROWSER_NEW_TAB_COUNT, 0)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _current_browser_new_tab_limit(tool_context: ToolContext | None) -> int:
    plan = _approved_plan(tool_context)
    if not isinstance(plan, dict):
        return _CURRENT_BROWSER_NEW_TAB_COUNT_LIMIT
    steps = plan.get("steps", [])
    if not isinstance(steps, list):
        return _CURRENT_BROWSER_NEW_TAB_COUNT_LIMIT

    explicit_new_tab_steps = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        chunks: list[str] = []
        for key in ("title", "description"):
            value = step.get(key)
            if isinstance(value, str):
                chunks.append(value.lower())
        haystack = " ".join(chunks)
        if any(marker in haystack for marker in _CURRENT_BROWSER_NEW_TAB_STEP_MARKERS):
            explicit_new_tab_steps += 1

    if explicit_new_tab_steps <= 0:
        return _CURRENT_BROWSER_NEW_TAB_COUNT_LIMIT
    return max(
        _CURRENT_BROWSER_NEW_TAB_COUNT_LIMIT,
        min(explicit_new_tab_steps, _CURRENT_BROWSER_NEW_TAB_COUNT_LIMIT_MAX),
    )


def _current_browser_opened_tab_ids(tool_context: ToolContext | None) -> set[int]:
    if tool_context is None:
        return set()
    raw = tool_context.state.get(StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS, [])
    if not isinstance(raw, list):
        return set()
    tab_ids: set[int] = set()
    for item in raw:
        try:
            tab_ids.add(int(item))
        except (TypeError, ValueError):
            continue
    return tab_ids


def _current_browser_opened_tab_order(tool_context: ToolContext | None) -> list[int]:
    if tool_context is None:
        return []
    raw = tool_context.state.get(StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS, [])
    if not isinstance(raw, list):
        return []
    ordered: list[int] = []
    seen: set[int] = set()
    for item in raw:
        try:
            normalized = int(item)
        except (TypeError, ValueError):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _current_browser_preferred_tab_id(tool_context: ToolContext | None) -> int | None:
    ordered = _current_browser_opened_tab_order(tool_context)
    if not ordered:
        return None
    return ordered[-1]


def _remember_current_browser_opened_tab(
    tool_context: ToolContext | None,
    tab_id: Any,
) -> None:
    if tool_context is None:
        return
    try:
        normalized = int(tab_id)
    except (TypeError, ValueError):
        return
    current = [item for item in _current_browser_opened_tab_order(tool_context) if item != normalized]
    current.append(normalized)
    tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS] = current
    tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_ACTIVE_TAB_ID] = normalized


async def _wait_for_current_browser_tab_verification(
    tool_context: ToolContext | None,
    previous_tab_id: Any | None = None,
) -> dict[str, Any]:
    try:
        expected_previous_tab_id = int(previous_tab_id)
    except (TypeError, ValueError):
        expected_previous_tab_id = None

    last_info: dict[str, Any] = {}
    for attempt in range(_CURRENT_BROWSER_NEW_TAB_VERIFY_ATTEMPTS):
        info = await _call_current_tab_info(tool_context)
        if isinstance(info, dict):
            last_info = info
            if _result_succeeded(info):
                try:
                    current_tab_id = int(info.get("tab_id"))
                except (TypeError, ValueError):
                    current_tab_id = None
                if (
                    expected_previous_tab_id is None
                    or current_tab_id is None
                    or current_tab_id != expected_previous_tab_id
                ):
                    return info
            elif _is_non_retriable_transport_error(info):
                return info
        if attempt + 1 < _CURRENT_BROWSER_NEW_TAB_VERIFY_ATTEMPTS:
            await asyncio.sleep(_CURRENT_BROWSER_NEW_TAB_VERIFY_DELAY_SECONDS)
    return last_info


def _is_desktop_playback_task(tool_context: ToolContext | None) -> bool:
    if tool_context is None:
        return False
    plan = _approved_plan(tool_context) or {}
    chunks: list[str] = []
    goal = tool_context.state.get(StateKeys.TASK_GOAL, "")
    if isinstance(goal, str):
        chunks.append(goal)
    for value in (
        plan.get("goal"),
        plan.get("plan_id"),
    ):
        if isinstance(value, str):
            chunks.append(value)
    for step in plan.get("steps", []):
        if not isinstance(step, dict):
            continue
        for key in ("title", "description"):
            value = step.get(key)
            if isinstance(value, str):
                chunks.append(value)
        expected = step.get("expected_outputs", [])
        if isinstance(expected, list):
            chunks.extend(str(item) for item in expected)
    haystack = " ".join(chunks).lower()
    return any(keyword in haystack for keyword in _PLAYBACK_TASK_KEYWORDS)


def _playback_app_name_hint(
    tool_context: ToolContext | None,
    app_name: str | None,
) -> str | None:
    if isinstance(app_name, str) and app_name.strip():
        return app_name.strip()
    plan = _approved_plan(tool_context) or {}
    chunks: list[str] = []
    goal = tool_context.state.get(StateKeys.TASK_GOAL, "") if tool_context is not None else ""
    if isinstance(goal, str):
        chunks.append(goal)
    for value in (plan.get("goal"), plan.get("plan_id")):
        if isinstance(value, str):
            chunks.append(value)
    haystack = " ".join(chunks).lower()
    for marker, resolved_name in _PLAYBACK_APP_NAME_HINTS:
        if marker in haystack:
            return resolved_name
    return None


def _normalize_hotkeys(keys: list[str]) -> tuple[str, ...]:
    normalized = []
    for key in keys:
        # Split compound keys like "cmd+t" into ["cmd", "t"]
        parts = key.split("+") if "+" in key else [key]
        for part in parts:
            value = _HOTKEY_ALIASES.get(part.strip().lower(), part.strip().lower())
            normalized.append(value)
    # De-duplicate: executors occasionally emit repeated keys like
    # ["left", "left"] which have no meaningful hotkey semantics. Collapsing
    # them to ("left",) lets the allow-list match cleanly.
    return tuple(sorted({item for item in normalized if item}))


def _rewrite_current_browser_hotkeys(keys: list[str]) -> list[str]:
    normalized = _normalize_hotkeys(keys)
    return list(_CURRENT_BROWSER_HOTKEY_REWRITES.get(normalized, keys))


def _allows_current_browser_new_tab(tool_context: ToolContext | None) -> bool:
    if tool_context is None:
        return False
    constraints = tool_context.state.get(StateKeys.TASK_CONSTRAINTS, [])
    if not isinstance(constraints, list):
        return False
    return any(
        _PRESERVE_CONTROL_UI_MARKER in str(item).lower()
        for item in constraints
    )


def _current_browser_goal_text(tool_context: ToolContext | None) -> str:
    if tool_context is None:
        return ""
    goal = tool_context.state.get(StateKeys.TASK_GOAL, "")
    return goal if isinstance(goal, str) else ""


def _is_current_browser_search_task(tool_context: ToolContext | None) -> bool:
    goal = _current_browser_goal_text(tool_context).lower()
    if _is_current_browser_spreadsheet_task(tool_context):
        return False
    return any(keyword in goal for keyword in _CURRENT_BROWSER_SEARCH_KEYWORDS)


def _is_current_browser_spreadsheet_task(tool_context: ToolContext | None) -> bool:
    goal = _current_browser_goal_text(tool_context).lower()
    return any(keyword in goal for keyword in SPREADSHEET_KEYWORDS)


def _spreadsheet_target_from_fields(
    *,
    app_name: str | None = None,
    window_id: str | None = None,
    role: str | None = None,
    title: str | None = None,
    identifier: str | None = None,
    value_contains: str | None = None,
) -> dict[str, Any]:
    target: dict[str, Any] = {}
    for key, value in (
        ("app_name", app_name),
        ("window_id", window_id),
        ("role", role),
        ("title", title),
        ("identifier", identifier),
        ("value_contains", value_contains),
    ):
        normalized = str(value or "").strip()
        if normalized:
            target[key] = normalized
    return target


def _normalize_current_browser_spreadsheet_target(
    target: Any,
) -> dict[str, Any]:
    if not isinstance(target, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key in (
        "app_name",
        "window_id",
        "role",
        "title",
        "identifier",
        "value_contains",
    ):
        value = str(target.get(key) or "").strip()
        if value:
            normalized[key] = value
    return normalized


def _looks_like_safe_current_browser_spreadsheet_text_target(target: Any) -> bool:
    normalized = _normalize_current_browser_spreadsheet_target(target)
    if not normalized:
        return False
    marker_text = " ".join(
        str(normalized.get(key) or "").strip().lower()
        for key in ("title", "identifier", "value_contains")
        if str(normalized.get(key) or "").strip()
    )
    if not marker_text:
        return False
    if any(marker in marker_text for marker in _CURRENT_BROWSER_SPREADSHEET_UNSAFE_TEXT_TARGET_MARKERS):
        return False
    if any(marker in marker_text for marker in _CURRENT_BROWSER_SPREADSHEET_SAFE_TEXT_TARGET_MARKERS):
        return True
    for token in (
        str(normalized.get("title") or "").strip().lower(),
        str(normalized.get("identifier") or "").strip().lower(),
    ):
        compact = token.replace("$", "").replace("!", "")
        if 2 <= len(compact) <= 8 and any(ch.isalpha() for ch in compact) and any(
            ch.isdigit() for ch in compact
        ):
            return True
    return False


def _is_current_browser_spreadsheet_text_field_target(target: Any) -> bool:
    normalized = _normalize_current_browser_spreadsheet_target(target)
    role = str(normalized.get("role") or "").strip().lower()
    return role in _CURRENT_BROWSER_SPREADSHEET_TEXT_FIELD_ROLES


def _guard_unsafe_current_browser_spreadsheet_text_target(
    tool_context: ToolContext | None,
    target: Any,
    *,
    action: str,
) -> None:
    if tool_context is None or not _is_current_browser_spreadsheet_task(tool_context):
        return
    if not _has_current_browser_google_sheets_destination(tool_context):
        return
    if not _is_current_browser_spreadsheet_text_field_target(target):
        return
    if _looks_like_safe_current_browser_spreadsheet_text_target(target):
        return
    raise PermissionError(
        f"Blocked spreadsheet {action} because the selector points at a generic "
        "text field, not a safe spreadsheet target. Do not click/type into the "
        "document title or toolbar text inputs; type directly into the active "
        "cell and use Tab/Enter to move."
    )


def _remember_current_browser_spreadsheet_target(
    tool_context: ToolContext | None,
    target: Any,
    fallback: Any = None,
) -> None:
    if tool_context is None or not _is_current_browser_spreadsheet_task(tool_context):
        return
    normalized = _normalize_current_browser_spreadsheet_target(target)
    if not _looks_like_safe_current_browser_spreadsheet_text_target(normalized):
        normalized = _normalize_current_browser_spreadsheet_target(fallback)
    if not _looks_like_safe_current_browser_spreadsheet_text_target(normalized):
        return
    if normalized:
        tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_SPREADSHEET_TARGET] = normalized


def _clear_current_browser_spreadsheet_target(
    tool_context: ToolContext | None,
) -> None:
    if tool_context is None:
        return
    _drop_tool_context_state_key(
        tool_context,
        StateKeys.TEMP_CURRENT_BROWSER_SPREADSHEET_TARGET,
    )


def _drop_tool_context_state_key(
    tool_context: ToolContext | None,
    key: str,
) -> None:
    if tool_context is None:
        return
    state = tool_context.state
    if hasattr(state, "pop"):
        state.pop(key, None)
        return

    # google.adk.sessions.state.State exposes get/set but no delete/pop API.
    # Remove the remembered target from both the committed value and pending
    # delta so later type calls do not reuse a stale Name Box target.
    raw_value = getattr(state, "_value", None)
    if isinstance(raw_value, dict):
        raw_value.pop(key, None)
    raw_delta = getattr(state, "_delta", None)
    if isinstance(raw_delta, dict):
        raw_delta.pop(key, None)


def _set_current_browser_spreadsheet_cell_edit_ready(
    tool_context: ToolContext | None,
    ready: bool,
) -> None:
    if tool_context is None:
        return
    key = StateKeys.TEMP_CURRENT_BROWSER_SPREADSHEET_CELL_EDIT_READY
    if ready:
        tool_context.state[key] = True
        return
    _drop_tool_context_state_key(tool_context, key)


def _current_browser_spreadsheet_cell_edit_ready(
    tool_context: ToolContext | None,
) -> bool:
    if tool_context is None:
        return False
    return bool(
        tool_context.state.get(
            StateKeys.TEMP_CURRENT_BROWSER_SPREADSHEET_CELL_EDIT_READY,
            False,
        )
    )


def _current_browser_spreadsheet_target(
    tool_context: ToolContext | None,
) -> dict[str, Any] | None:
    if tool_context is None:
        return None
    raw = tool_context.state.get(StateKeys.TEMP_CURRENT_BROWSER_SPREADSHEET_TARGET)
    if not isinstance(raw, dict):
        return None
    normalized = _normalize_current_browser_spreadsheet_target(raw)
    return normalized or None


def _has_current_browser_google_sheets_destination(
    tool_context: ToolContext | None,
) -> bool:
    if tool_context is None:
        return False
    raw = tool_context.state.get(StateKeys.TEMP_CURRENT_BROWSER_DESTINATION_TAB)
    if not isinstance(raw, dict):
        return False
    surface = str(raw.get("surface") or "").strip().lower()
    if surface == "google_sheets":
        return True
    url = str(raw.get("url") or "").strip()
    title = str(raw.get("title") or "").strip()
    return _is_current_browser_google_sheets_tab(url, title)


async def _current_browser_spreadsheet_target_is_available(
    target: dict[str, Any] | None,
) -> bool:
    if not isinstance(target, dict) or not target:
        return False
    try:
        from src.tools.desktop import desktop_wait_element

        result = await desktop_wait_element(
            app_name=str(target.get("app_name") or "").strip() or None,
            window_id=str(target.get("window_id") or "").strip() or None,
            role=str(target.get("role") or "").strip() or None,
            title=str(target.get("title") or "").strip() or None,
            identifier=str(target.get("identifier") or "").strip() or None,
            value_contains=str(target.get("value_contains") or "").strip() or None,
            timeout_seconds=0.35,
            poll_interval_seconds=0.1,
            tool_context=None,
        )
    except Exception:
        return False
    return bool(isinstance(result, dict) and result.get("matched"))


def _is_safe_current_browser_destination(
    *,
    tool_context: ToolContext | None,
    url: str,
    title: str,
) -> bool:
    lowered_url = url.lower()
    lowered_title = title.lower()
    if _is_current_browser_spreadsheet_task(tool_context):
        parsed = urlsplit(url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if host == "sheets.new":
            return True
        if host != "docs.google.com":
            return False
        if path.startswith("/spreadsheets/create"):
            return True
        return path.startswith("/spreadsheets/d/") or (
            path.startswith("/spreadsheets/u/") and "/d/" in path
        )
    if _is_current_browser_search_task(tool_context):
        return (
            any(marker in lowered_url for marker in _CURRENT_BROWSER_SAFE_SEARCH_HOST_MARKERS)
            or lowered_url.rstrip("/") in {"https://www.google.com", "https://google.com", "https://www.google.co.jp", "https://google.co.jp"}
            or "google" in lowered_title
        )
    return True


async def _assert_safe_current_browser_target(
    tool_context: ToolContext | None,
) -> None:
    if tool_context is None or not _is_current_browser_task(tool_context):
        return
    if not (
        _is_current_browser_search_task(tool_context)
        or _is_current_browser_spreadsheet_task(tool_context)
    ):
        return

    activation = await _activate_current_browser_task_tab(tool_context)
    if _is_non_retriable_transport_error(activation):
        raise PermissionError(_current_tab_extension_disconnected_message())

    info = await _call_current_tab_info(tool_context)
    if _is_non_retriable_transport_error(info):
        raise PermissionError(_current_tab_extension_disconnected_message())
    if not isinstance(info, dict) or not _result_succeeded(info):
        raise PermissionError(
            "Current-browser text/click actions require current_tab.info to verify the "
            "destination tab before interacting with page forms."
        )
    tab_id = info.get("tab_id")
    opened_tab_ids = _current_browser_opened_tab_ids(tool_context)
    try:
        normalized_tab_id = int(tab_id)
    except (TypeError, ValueError):
        normalized_tab_id = None
    if normalized_tab_id is None or normalized_tab_id not in opened_tab_ids:
        raise PermissionError(
            "Blocked current-browser interaction because the active tab was not "
            "opened by this task."
        )
    url = str(info.get("url") or "").strip()
    title = str(info.get("title") or "").strip()
    if not _is_safe_current_browser_destination(
        tool_context=tool_context,
        url=url,
        title=title,
    ):
        raise PermissionError(
            "Blocked current-browser interaction because the active tab does not match "
            f"the expected search/spreadsheet destination. url={url or '-'} title={title or '-'}"
        )


def _looks_like_url(text: str) -> bool:
    lowered = text.lower()
    return lowered.startswith(("http://", "https://")) or "://" in lowered


def _looks_like_search_url(text: str) -> bool:
    lowered = text.lower()
    return "google.com/search?q=" in lowered or "google.co.jp/search?q=" in lowered


def _looks_like_blocked_spreadsheet_payload(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return _looks_like_search_url(stripped) or (_looks_like_url(stripped) and len(stripped) >= 24)


def _rewrite_current_browser_address_bar_text(
    text: str,
    tool_context: ToolContext | None,
) -> str:
    if tool_context is None:
        return text
    if _is_current_browser_spreadsheet_task(tool_context):
        return text
    focused = bool(tool_context.state.get(_CURRENT_BROWSER_ADDRESS_BAR_STATE_KEY))
    tool_context.state[_CURRENT_BROWSER_ADDRESS_BAR_STATE_KEY] = False
    if not _is_current_browser_search_task(tool_context):
        return text
    stripped = text.strip()
    if not stripped or _looks_like_url(stripped):
        return text
    # ToolContext.state mutations are not guaranteed to survive every model/tool
    # boundary, so treat selector-less text entry in current-browser search tasks
    # as an address-bar query even if the transient "focused" flag was dropped.
    # Cap the fallback so long arbitrary text does not get rewritten into a
    # search URL when the address-bar focus signal was likely lost.
    if not focused and len(stripped) > _ADDRESS_BAR_FALLBACK_QUERY_MAX_CHARS:
        return text
    return f"https://www.google.com/search?q={quote_plus(stripped)}"


async def _focus_control_ui_browser_window(
    *,
    app_name: str,
) -> dict[str, Any] | None:
    from src.tools.desktop import desktop_control_focus_window

    for title_hint in _CURRENT_BROWSER_CONTROL_UI_TITLE_HINTS:
        result = await desktop_control_focus_window(
            app_name=app_name,
            title=title_hint,
        )
        if result.get("success") or result.get("ok"):
            return result
    return None


def _is_current_browser_control_ui_tab(url: str, title: str) -> bool:
    lowered_url = url.lower()
    lowered_title = title.lower()
    return any(marker in lowered_url for marker in _CURRENT_BROWSER_CONTROL_UI_URL_MARKERS) or any(
        hint.lower() in lowered_title for hint in _CURRENT_BROWSER_CONTROL_UI_TITLE_HINTS
    )


async def _activate_current_browser_task_tab(
    tool_context: ToolContext | None,
) -> dict[str, Any] | None:
    if tool_context is None or not _is_current_browser_task(tool_context):
        return None
    target_tab_id = _current_browser_preferred_tab_id(tool_context)
    if target_tab_id is None:
        return None

    current_info = await _call_current_tab_info(tool_context)
    if _is_non_retriable_transport_error(current_info):
        return current_info
    if isinstance(current_info, dict) and _result_succeeded(current_info):
        try:
            current_tab_id = int(current_info.get("tab_id"))
        except (TypeError, ValueError):
            current_tab_id = None
        if current_tab_id == target_tab_id:
            tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_ACTIVE_TAB_ID] = target_tab_id
            return current_info
        current_url = str(current_info.get("url") or "").strip()
        current_title = str(current_info.get("title") or "").strip()
        if current_tab_id in _current_browser_opened_tab_ids(tool_context):
            tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_ACTIVE_TAB_ID] = current_tab_id
            return current_info
        if not _is_current_browser_control_ui_tab(current_url, current_title):
            return current_info

    activated = await _call_current_tab_activate(target_tab_id, tool_context)
    if isinstance(activated, dict) and _result_succeeded(activated):
        tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_ACTIVE_TAB_ID] = target_tab_id
        return activated
    return activated if isinstance(activated, dict) else None


async def _should_open_current_browser_task_tab(
    tool_context: ToolContext | None,
) -> bool:
    if tool_context is None:
        return False
    if not _plan_allows_capability(tool_context, _CURRENT_TAB_CAPABILITY):
        return False

    current_info = await _call_current_tab_info(tool_context)
    if not isinstance(current_info, dict) or not _result_succeeded(current_info):
        return False

    try:
        current_tab_id = int(current_info.get("tab_id"))
    except (TypeError, ValueError):
        current_tab_id = None

    if current_tab_id is not None and current_tab_id in _current_browser_opened_tab_ids(
        tool_context
    ):
        return False

    current_url = str(current_info.get("url") or "").strip()
    current_title = str(current_info.get("title") or "").strip()
    if not _is_current_browser_control_ui_tab(current_url, current_title):
        return False

    new_tab_count = _current_browser_new_tab_count(tool_context)
    new_tab_limit = _current_browser_new_tab_limit(tool_context)
    if new_tab_count >= new_tab_limit:
        quantity = "one" if new_tab_limit == 1 else str(new_tab_limit)
        noun = "tab is" if new_tab_limit == 1 else "tabs are"
        raise PermissionError(
            f"Only {quantity} preserved-browser {noun} allowed for this task."
        )
    return True


# ── Guarded tool implementations ──────────────────────────────────────────


async def guarded_web_search(
    query: str,
    tool_context: ToolContext,
) -> dict:
    """web.search capability が承認済みの場合のみ Web 検索を実行する。"""
    _check_approval(tool_context, "web.search")
    _check_capability_in_plan(tool_context, "web.search")

    from src.tools.web_search import web_search
    return await web_search(query)


async def guarded_read_file(
    path: str,
    tool_context: ToolContext,
) -> dict:
    """file.read capability が承認済みの場合のみファイルを読む。"""
    _check_approval(tool_context, "file.read")
    _check_capability_in_plan(tool_context, "file.read")

    from src.tools.file_manager import read_file
    return await read_file(path)


async def guarded_write_file(
    path: str,
    content: str,
    tool_context: ToolContext,
) -> dict:
    """
    file.write capability が承認済みの場合のみファイルを書く。
    HIGH リスク: human_approved が必要。
    """
    status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
    if status != "human_approved":
        raise PermissionError(
            "file.write requires human_approved status. "
            f"Current status: '{status}'"
        )
    _check_capability_in_plan(tool_context, "file.write")

    from src.tools.file_manager import write_file
    return await write_file(path, content)


async def guarded_memory_read(
    query: str | None = None,
    tags: str | None = None,
    limit: int = 10,
    tool_context: ToolContext | None = None,
) -> dict:
    """memory.read capability が承認済みの場合のみメモリを検索する。"""
    if tool_context is not None:
        _check_approval(tool_context, "memory.read")
        _check_capability_in_plan(tool_context, "memory.read")

        if query and not tags:
            try:
                response = await tool_context.search_memory(query)
            except ValueError:
                response = None
            else:
                memories = response.memories[: max(1, limit)]
                return {
                    "results": [_memory_entry_to_result(entry) for entry in memories],
                    "count": len(memories),
                    "query": query,
                    "tags": None,
                    "source": "adk_memory",
                    "success": True,
                }

    from src.tools.memory import memory_search
    return await memory_search(query=query, tags=tags, limit=limit)


async def guarded_browser_navigate(
    url: str,
    tool_context: ToolContext,
) -> dict:
    """browser.navigate capability が承認済みの場合のみブラウザを操作する。"""
    if _is_current_browser_task(tool_context) and _plan_allows_capability(
        tool_context, _CURRENT_TAB_CAPABILITY
    ):
        if _is_current_tab_extension_disconnected(tool_context):
            return _current_tab_extension_disconnected_error(
                "browser.navigate/current_tab.navigate"
            )
        result = await guarded_current_tab_navigate(url, tool_context=tool_context)
        if _is_non_retriable_transport_error(result):
            return _current_tab_extension_disconnected_error(
                "browser.navigate/current_tab.navigate"
            )
        return result

    _check_approval(tool_context, "browser.navigate")
    _check_capability_in_plan(tool_context, "browser.navigate")

    from src.tools.browser import browser_navigate
    return await browser_navigate(url)


async def guarded_current_tab_info(
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        _check_approval(tool_context, _CURRENT_TAB_INFO_CAPABILITY)
        _check_capability_in_plan(tool_context, _CURRENT_TAB_INFO_CAPABILITY)
    return await _call_current_tab_info(tool_context)


async def guarded_current_tab_navigate(
    url: str,
    timeout_ms: int = 15000,
    tool_context: ToolContext | None = None,
) -> dict:
    # Always open in a new tab so the Control UI tab is never overwritten.
    open_new_tab = True
    if tool_context is not None:
        _check_approval(tool_context, _CURRENT_TAB_CAPABILITY)
        _check_capability_in_plan(tool_context, _CURRENT_TAB_CAPABILITY)
        if _is_current_tab_extension_disconnected(tool_context):
            return _current_tab_extension_disconnected_error("host.current_tab.navigate")
        activation = await _activate_current_browser_task_tab(tool_context)
        if _is_non_retriable_transport_error(activation):
            return activation
        open_new_tab = await _should_open_current_browser_task_tab(tool_context)
        if _is_current_tab_extension_disconnected(tool_context):
            return _current_tab_extension_disconnected_error("host.current_tab.navigate")

    result = await _call_current_tab_navigate(
        url,
        timeout_ms=timeout_ms,
        new_tab=open_new_tab,
        tool_context=tool_context,
    )
    if tool_context is not None and isinstance(result, dict) and _result_succeeded(result):
        if open_new_tab:
            tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_NEW_TAB_COUNT] = (
                _current_browser_new_tab_count(tool_context) + 1
            )
        _remember_current_browser_opened_tab(tool_context, result.get("tab_id"))
        await _dismiss_google_sheets_overlays(tool_context, url, result)
    return result


async def _dismiss_google_sheets_overlays(
    tool_context: ToolContext | None,
    url: str,
    nav_result: dict,
) -> None:
    """Close the Gemini/"Build" side panel that Google Sheets auto-opens on
    new spreadsheets.

    On a fresh /spreadsheets/create or /spreadsheets/d/.../edit load, Google
    Sheets now opens a Gemini-in-Workspace overlay whose text input captures
    keyboard focus. Subsequent desktop.control.hotkey / type calls silently
    go to that overlay instead of the grid, leaving cells empty.

    Dismiss it with a single Escape keypress right after navigation, but only
    for current-browser spreadsheet tasks and only when the resulting tab
    actually landed on a Google Sheets URL.
    """
    if tool_context is None or not _is_current_browser_spreadsheet_task(tool_context):
        return

    landed_url = ""
    if isinstance(nav_result, dict):
        landed_url = str(nav_result.get("url") or "").strip()
    if not landed_url:
        landed_url = url or ""

    lowered = landed_url.lower()
    if not ("docs.google.com/spreadsheets" in lowered or "sheets.new" in lowered):
        return

    # Give the Gemini panel a moment to appear before we try to dismiss it.
    await asyncio.sleep(1.5)

    extracted = await _call_current_tab_extract_text(None, tool_context)
    if _is_non_retriable_transport_error(extracted):
        return
    overlay_text = str(extracted.get("text") or "") if isinstance(extracted, dict) else ""

    if overlay_text:
        lowered_text = overlay_text.lower()
        if not any(marker in lowered_text for marker in _GOOGLE_SHEETS_OVERLAY_MARKERS):
            return

    await _ensure_current_browser_frontmost(tool_context)

    for selector in _GOOGLE_SHEETS_OVERLAY_DISMISS_SELECTORS:
        clicked = await _call_current_tab_click(selector, tool_context)
        if _is_non_retriable_transport_error(clicked):
            return
        if _result_succeeded(clicked):
            await asyncio.sleep(0.15)

    try:
        from src.tools.desktop import desktop_control_hotkey

        await desktop_control_hotkey(keys=["escape"], tool_context=None)
    except Exception:
        return


async def guarded_current_tab_extract_text(
    selector: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        _check_approval(tool_context, _CURRENT_TAB_CAPABILITY)
        _check_capability_in_plan(tool_context, _CURRENT_TAB_CAPABILITY)
        if _is_current_tab_extension_disconnected(tool_context):
            return _current_tab_extension_disconnected_error(
                "host.current_tab.extract_text"
            )
        activation = await _activate_current_browser_task_tab(tool_context)
        if _is_non_retriable_transport_error(activation):
            return activation
    return await _call_current_tab_extract_text(selector, tool_context)


async def guarded_current_tab_click(
    selector: str,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        if _is_current_browser_task(tool_context):
            await _assert_safe_current_browser_target(tool_context)
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "current_tab.click requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, _CURRENT_TAB_CAPABILITY)
        if _is_current_tab_extension_disconnected(tool_context):
            return _current_tab_extension_disconnected_error("host.current_tab.click")
    return await _call_current_tab_click(selector, tool_context)


async def guarded_current_tab_fill(
    selector: str,
    text: str,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        if _is_current_browser_task(tool_context):
            await _assert_safe_current_browser_target(tool_context)
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "current_tab.fill requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, _CURRENT_TAB_CAPABILITY)
        if _is_current_tab_extension_disconnected(tool_context):
            return _current_tab_extension_disconnected_error("host.current_tab.fill")
    return await _call_current_tab_fill(selector, text, tool_context)


async def guarded_browser_extract_text(
    selector: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict:
    """browser.navigate と同じ承認のもとでテキスト抽出を許可する。"""
    if tool_context is not None:
        _check_approval(tool_context, "browser.navigate")
        _check_capability_in_plan(tool_context, "browser.navigate")

    from src.tools.browser import browser_extract_text
    return await browser_extract_text(selector)


async def guarded_browser_click(
    selector: str,
    timeout: int = 30000,
    tool_context: ToolContext | None = None,
) -> dict:
    """browser.navigate と同じ承認のもとでクリックを許可する。"""
    if tool_context is not None:
        _check_approval(tool_context, "browser.navigate")
        _check_capability_in_plan(tool_context, "browser.navigate")

    from src.tools.browser import browser_click
    return await browser_click(selector, timeout=timeout)


async def guarded_browser_fill(
    selector: str,
    text: str,
    timeout: int = 30000,
    tool_context: ToolContext | None = None,
) -> dict:
    """browser.navigate と同じ承認のもとで入力を許可する。"""
    if tool_context is not None:
        _check_approval(tool_context, "browser.navigate")
        _check_capability_in_plan(tool_context, "browser.navigate")

    from src.tools.browser import browser_fill
    return await browser_fill(selector, text, timeout=timeout)


async def guarded_browser_press(
    key: str,
    selector: str | None = None,
    timeout: int = 30000,
    tool_context: ToolContext | None = None,
) -> dict:
    """browser.navigate と同じ承認のもとでキー送信を許可する。"""
    if tool_context is not None:
        _check_approval(tool_context, "browser.navigate")
        _check_capability_in_plan(tool_context, "browser.navigate")

    from src.tools.browser import browser_press
    return await browser_press(key, selector=selector, timeout=timeout)


async def guarded_desktop_view_windows(
    include_minimized: bool = False,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        _check_approval(tool_context, "desktop.view.windows")
        _check_capability_in_plan(tool_context, "desktop.view.windows")

    from src.tools.desktop import desktop_view_windows
    return await desktop_view_windows(include_minimized=include_minimized)


async def guarded_desktop_wait_window(
    app_name: str | None = None,
    window_id: str | None = None,
    title: str | None = None,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.2,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        _check_approval(tool_context, "desktop.wait.window")
        _check_capability_in_plan(tool_context, "desktop.wait.window")

    from src.tools.desktop import desktop_wait_window
    return await desktop_wait_window(
        app_name=app_name,
        window_id=window_id,
        title=title,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


async def guarded_desktop_view_frontmost_app(
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        _check_approval(tool_context, "desktop.view.frontmost_app")
        _check_capability_in_plan(tool_context, "desktop.view.frontmost_app")

    from src.tools.desktop import desktop_view_frontmost_app
    return await desktop_view_frontmost_app()


async def guarded_desktop_view_screenshot(
    path: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.view.screenshot requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.view.screenshot")

    from src.tools.desktop import desktop_view_screenshot
    return await desktop_view_screenshot(path=path)


async def guarded_desktop_ax_find(
    app_name: str | None = None,
    window_id: str | None = None,
    role: str | None = None,
    title: str | None = None,
    identifier: str | None = None,
    value_contains: str | None = None,
    index: int = 0,
    tool_context: ToolContext | None = None,
) -> dict:
    selector_target = _spreadsheet_target_from_fields(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
    )
    if tool_context is not None:
        _check_approval(tool_context, "desktop.ax.find")
        _check_capability_in_plan(tool_context, "desktop.ax.find")

    from src.tools.desktop import desktop_ax_find
    result = await desktop_ax_find(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )
    if isinstance(result, dict) and result.get("matched"):
        _remember_current_browser_spreadsheet_target(
            tool_context,
            result.get("target"),
            fallback=selector_target,
        )
    return result


async def guarded_desktop_wait_element(
    app_name: str | None = None,
    window_id: str | None = None,
    role: str | None = None,
    title: str | None = None,
    identifier: str | None = None,
    value_contains: str | None = None,
    index: int = 0,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.2,
    tool_context: ToolContext | None = None,
) -> dict:
    selector_target = _spreadsheet_target_from_fields(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
    )
    if tool_context is not None:
        _check_approval(tool_context, "desktop.wait.element")
        _check_capability_in_plan(tool_context, "desktop.wait.element")

    from src.tools.desktop import desktop_wait_element
    result = await desktop_wait_element(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    if isinstance(result, dict) and result.get("matched"):
        _remember_current_browser_spreadsheet_target(
            tool_context,
            result.get("target"),
            fallback=selector_target,
        )
    return result


async def guarded_desktop_ax_snapshot(
    app_name: str | None = None,
    window_id: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.ax.snapshot requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.ax.snapshot")

    from src.tools.desktop import desktop_ax_snapshot
    return await desktop_ax_snapshot(app_name=app_name, window_id=window_id)


async def guarded_desktop_control_click(
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    click_count: int = 1,
    app_name: str | None = None,
    window_id: str | None = None,
    role: str | None = None,
    title: str | None = None,
    identifier: str | None = None,
    value_contains: str | None = None,
    index: int = 0,
    tool_context: ToolContext | None = None,
) -> dict:
    selector_target = _spreadsheet_target_from_fields(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
    )
    if tool_context is not None:
        _abort_current_browser_action_if_current_tab_unavailable(tool_context)
        if _is_current_browser_task(tool_context):
            await _assert_safe_current_browser_target(tool_context)
        _guard_unsafe_current_browser_spreadsheet_text_target(
            tool_context,
            selector_target,
            action="click",
        )
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.click requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.click")

    from src.tools.desktop import desktop_control_click
    result = await desktop_control_click(
        x=x,
        y=y,
        button=button,
        click_count=click_count,
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )
    if isinstance(result, dict) and result.get("success"):
        _remember_current_browser_spreadsheet_target(
            tool_context,
            result.get("target"),
            fallback=selector_target,
        )
    return result


async def _ensure_current_browser_frontmost(
    tool_context: ToolContext | None,
) -> str | None:
    """Bring the current-browser app to the OS-frontmost window before a
    desktop.control.type / hotkey fires.

    desktop.control.hotkey has no selector — it injects global key events,
    which land on whichever app is frontmost. For current-browser tasks
    that app MUST be the browser, otherwise cell-navigation keys (Tab,
    arrows, Enter) and typed text silently get absorbed by whichever OS
    window happens to be in front (VS Code, Terminal, Control UI tab, ...).

    Called with tool_context=None on the inner desktop.control.focus_window
    call so we don't re-trigger approval / capability checks; the outer
    guarded_desktop_control_{type,hotkey} has already verified the plan
    grants the enclosing capability.
    """
    if tool_context is None or not _is_current_browser_task(tool_context):
        return None

    # Prefer the remembered spreadsheet target's app_name if we have one.
    remembered = _current_browser_spreadsheet_target(tool_context) or {}
    candidate = (remembered.get("app_name") or "").strip()

    if not candidate:
        try:
            from src.tools.desktop import desktop_view_windows
            windows = await desktop_view_windows(include_minimized=False)
        except Exception:
            windows = {}
        if isinstance(windows, dict):
            for window in windows.get("windows", []):
                app = str(window.get("app_name") or "").strip()
                if app in _KNOWN_BROWSER_APPS:
                    candidate = app
                    break

    if not candidate:
        candidate = "Google Chrome"

    try:
        from src.tools.desktop import desktop_control_focus_window
        await desktop_control_focus_window(app_name=candidate, tool_context=None)
    except Exception:
        return None
    return candidate


async def guarded_desktop_control_type(
    text: str,
    app_name: str | None = None,
    window_id: str | None = None,
    role: str | None = None,
    title: str | None = None,
    identifier: str | None = None,
    value_contains: str | None = None,
    index: int = 0,
    tool_context: ToolContext | None = None,
) -> dict:
    explicit_target = any((app_name, window_id, role, title, identifier, value_contains))
    selector_target = _spreadsheet_target_from_fields(
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
    )
    if tool_context is not None:
        _abort_current_browser_action_if_current_tab_unavailable(tool_context)
        if _is_current_browser_spreadsheet_task(tool_context):
            if not _has_current_browser_google_sheets_destination(tool_context):
                try:
                    await _call_current_tab_info(tool_context)
                except Exception:
                    pass
            remembered_target = _current_browser_spreadsheet_target(tool_context)
            if remembered_target and not explicit_target:
                target_available = await _current_browser_spreadsheet_target_is_available(
                    remembered_target
                )
                if not target_available:
                    _clear_current_browser_spreadsheet_target(tool_context)
                    remembered_target = None
            cell_edit_ready = _current_browser_spreadsheet_cell_edit_ready(tool_context)
            if remembered_target and not explicit_target:
                app_name = remembered_target.get("app_name") or app_name
                window_id = remembered_target.get("window_id") or window_id
                role = remembered_target.get("role") or role
                title = remembered_target.get("title") or title
                identifier = remembered_target.get("identifier") or identifier
                value_contains = remembered_target.get("value_contains") or value_contains
            if not explicit_target and not remembered_target and not cell_edit_ready:
                raise PermissionError(
                    "Blocked spreadsheet typing because no remembered safe text "
                    "target or active-cell-ready state is established. Type into "
                    "the already selected cell when possible; otherwise use "
                    "desktop.ax.find/click only on an explicitly labeled Name Box, "
                    "then press Enter/Tab into the target cell before typing."
                )
            if (
                _has_current_browser_google_sheets_destination(tool_context)
                and _looks_like_blocked_spreadsheet_payload(text)
            ):
                raise PermissionError(
                    "Blocked spreadsheet typing because the payload looks like a "
                    "browser/search URL instead of spreadsheet cell data."
                )
            _guard_unsafe_current_browser_spreadsheet_text_target(
                tool_context,
                selector_target,
                action="typing",
            )
        if (
            _is_current_browser_task(tool_context)
            and not any((app_name, window_id, role, title, identifier, value_contains))
        ):
            text = _rewrite_current_browser_address_bar_text(text, tool_context)
        elif _is_current_browser_task(tool_context):
            await _assert_safe_current_browser_target(tool_context)
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.type requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.type")
        # Ensure the browser app is OS-frontmost so AX-injected typing lands
        # on the right window rather than whatever was previously in front.
        await _ensure_current_browser_frontmost(tool_context)

    from src.tools.desktop import desktop_control_type
    result = await desktop_control_type(
        text=text,
        app_name=app_name,
        window_id=window_id,
        role=role,
        title=title,
        identifier=identifier,
        value_contains=value_contains,
        index=index,
    )
    if isinstance(result, dict) and result.get("success"):
        _remember_current_browser_spreadsheet_target(
            tool_context,
            result.get("target"),
            fallback=selector_target,
        )
        if _is_current_browser_spreadsheet_task(tool_context) and not explicit_target:
            _set_current_browser_spreadsheet_cell_edit_ready(tool_context, False)
    return result


async def guarded_desktop_control_launch_app(
    app_name: str | None = None,
    bundle_id: str | None = None,
    wait_for_focus: bool = True,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        if _is_current_browser_task(tool_context):
            from src.tools.desktop import (
                desktop_control_focus_window,
                desktop_view_windows,
            )

            candidate_app = app_name.strip() if isinstance(app_name, str) else ""
            if candidate_app and candidate_app not in _KNOWN_BROWSER_APPS:
                raise PermissionError(
                    "desktop.control.launch_app is not allowed for current-browser tasks. "
                    "Use the existing frontmost browser window instead."
                )

            if not candidate_app:
                windows = await desktop_view_windows(include_minimized=False)
                for window in windows.get("windows", []):
                    window_app = str(window.get("app_name", "")).strip()
                    if window_app in _KNOWN_BROWSER_APPS:
                        candidate_app = window_app
                        break

            if candidate_app:
                if _allows_current_browser_new_tab(tool_context):
                    focused_control_ui = await _focus_control_ui_browser_window(
                        app_name=candidate_app
                    )
                    if focused_control_ui is not None:
                        return focused_control_ui
                return await desktop_control_focus_window(app_name=candidate_app)

            raise PermissionError(
                "desktop.control.launch_app is not allowed for current-browser tasks. "
                "No existing browser window could be identified."
            )
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.launch_app requires human_approved status. "
                f"Current status: '{status}'"
            )
        try:
            _check_capability_in_plan(tool_context, "desktop.control.launch_app")
        except PermissionError:
            if (
                _is_desktop_playback_task(tool_context)
                and _plan_allows_capability(tool_context, "desktop.control.focus_window")
            ):
                from src.tools.desktop import desktop_control_focus_window

                candidate_app = _playback_app_name_hint(tool_context, app_name)
                if candidate_app:
                    return await desktop_control_focus_window(app_name=candidate_app)
            raise

    from src.tools.desktop import desktop_control_launch_app
    return await desktop_control_launch_app(
        app_name=app_name,
        bundle_id=bundle_id,
        wait_for_focus=wait_for_focus,
    )


async def guarded_desktop_control_focus_window(
    app_name: str | None = None,
    window_id: str | None = None,
    title: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.focus_window requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.focus_window")
        if (
            _is_current_browser_task(tool_context)
            and _allows_current_browser_new_tab(tool_context)
            and not window_id
            and not title
            and isinstance(app_name, str)
            and app_name.strip() in _KNOWN_BROWSER_APPS
        ):
            focused_control_ui = await _focus_control_ui_browser_window(
                app_name=app_name.strip()
            )
            if focused_control_ui is not None:
                return focused_control_ui

    from src.tools.desktop import desktop_control_focus_window
    return await desktop_control_focus_window(
        app_name=app_name,
        window_id=window_id,
        title=title,
    )


async def guarded_desktop_control_hotkey(
    keys: list[str],
    tool_context: ToolContext | None = None,
) -> dict:
    effective_keys = keys
    verify_new_tab_after_hotkey = False
    previous_tab_id: int | None = None
    new_tab_count = 0
    normalized_keys: tuple[str, ...] = ()
    spreadsheet_input_primed = False
    if tool_context is not None:
        _abort_current_browser_action_if_current_tab_unavailable(tool_context)
        if _is_current_browser_task(tool_context):
            effective_keys = _rewrite_current_browser_hotkeys(keys)
            normalized_keys = _normalize_hotkeys(effective_keys)
            allow_new_tab = _allows_current_browser_new_tab(tool_context)
            # Allow spreadsheet cell-editing hotkeys (Tab / arrows / Enter /
            # Shift+Tab / Cmd+S / Cmd+Z / Escape) for current-browser
            # spreadsheet tasks. Tab etc. are cell-navigation inside Google
            # Sheets, not tab-switching. The approved plan must still grant
            # desktop.control.hotkey at the step contract level (enforced by
            # _check_capability_in_plan below).
            allow_spreadsheet_editing = _is_current_browser_spreadsheet_task(
                tool_context
            )
            if (
                normalized_keys not in _CURRENT_BROWSER_ALLOWED_HOTKEYS
                and not (
                    allow_new_tab
                    and normalized_keys in _CURRENT_BROWSER_NEW_TAB_HOTKEYS
                )
                and not (
                    allow_new_tab
                    and normalized_keys in _CURRENT_BROWSER_NEWTAB_EDITING_HOTKEYS
                )
                and not (
                    allow_spreadsheet_editing
                    and normalized_keys in _CURRENT_BROWSER_NEWTAB_EDITING_HOTKEYS
                )
            ):
                raise PermissionError(
                    "Only focus-address-bar or submit hotkeys are allowed for "
                    f"current-browser tasks. attempted={normalized_keys}"
                )
            if normalized_keys in _CURRENT_BROWSER_NEW_TAB_HOTKEYS:
                new_tab_count = _current_browser_new_tab_count(tool_context)
                new_tab_limit = _current_browser_new_tab_limit(tool_context)
                if new_tab_count >= new_tab_limit:
                    quantity = "one" if new_tab_limit == 1 else str(new_tab_limit)
                    noun = "hotkey is" if new_tab_limit == 1 else "hotkeys are"
                    raise PermissionError(
                        f"Only {quantity} new-tab {noun} allowed for a current-browser "
                        "task. Reuse the existing browser tab/window state for "
                        "retries instead of opening more tabs than the approved "
                        "plan requires."
                    )
                verify_new_tab_after_hotkey = True
            if (
                normalized_keys in _CURRENT_BROWSER_ALLOWED_HOTKEYS
                or normalized_keys in _CURRENT_BROWSER_NEW_TAB_HOTKEYS
            ):
                tool_context.state[_CURRENT_BROWSER_ADDRESS_BAR_STATE_KEY] = (
                    normalized_keys != ("enter",)
                )
        if _is_current_browser_spreadsheet_task(tool_context):
            spreadsheet_input_primed = bool(
                _current_browser_spreadsheet_target(tool_context)
                or _current_browser_spreadsheet_cell_edit_ready(tool_context)
            )
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.hotkey requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.hotkey")
        # Ensure the browser app is OS-frontmost. Hotkeys have no selector —
        # without this, Tab / arrows / Enter land on whichever app is
        # frontmost, silently missing Google Sheets.
        await _ensure_current_browser_frontmost(tool_context)

        if verify_new_tab_after_hotkey:
            previous_info = await _call_current_tab_info(tool_context)
            if _is_non_retriable_transport_error(previous_info):
                raise PermissionError(_current_tab_extension_disconnected_message())
            if isinstance(previous_info, dict) and _result_succeeded(previous_info):
                try:
                    previous_tab_id = int(previous_info.get("tab_id"))
                except (TypeError, ValueError):
                    previous_tab_id = None

    from src.tools.desktop import desktop_control_hotkey
    result = await desktop_control_hotkey(keys=effective_keys)

    if (
        tool_context is not None
        and _is_current_browser_spreadsheet_task(tool_context)
        and isinstance(result, dict)
        and (result.get("success") or result.get("ok"))
        and normalized_keys in _CURRENT_BROWSER_NEWTAB_EDITING_HOTKEYS
    ):
        # A remembered spreadsheet target usually points at the Name Box used
        # to jump to A1/B2/etc. Once a spreadsheet editing hotkey commits or
        # moves focus, later text should go to the active grid cell instead of
        # being forced back into that stale text field.
        _clear_current_browser_spreadsheet_target(tool_context)
        _set_current_browser_spreadsheet_cell_edit_ready(
            tool_context,
            (
                spreadsheet_input_primed
                or _has_current_browser_google_sheets_destination(tool_context)
            )
            and normalized_keys in _CURRENT_BROWSER_SPREADSHEET_CELL_READY_HOTKEYS,
        )

    if verify_new_tab_after_hotkey and isinstance(result, dict) and (
        result.get("success") or result.get("ok")
    ):
        info = await _wait_for_current_browser_tab_verification(
            tool_context,
            previous_tab_id=previous_tab_id,
        )
        if _is_non_retriable_transport_error(info):
            raise PermissionError(_current_tab_extension_disconnected_message())
        if not isinstance(info, dict) or not info.get("success"):
            raise PermissionError(
                "Failed to verify the newly opened browser tab. Refusing to "
                "continue interacting with an unverified tab."
            )
        try:
            verified_tab_id = int(info.get("tab_id"))
        except (TypeError, ValueError):
            verified_tab_id = None
        if previous_tab_id is not None and verified_tab_id == previous_tab_id:
            raise PermissionError(
                "Failed to confirm that the newly opened browser tab became active. "
                "Refusing to continue interacting with an unverified tab."
            )
        if tool_context is not None:
            tool_context.state[StateKeys.TEMP_CURRENT_BROWSER_NEW_TAB_COUNT] = (
                new_tab_count + 1
            )
        _remember_current_browser_opened_tab(tool_context, info.get("tab_id"))

    return result


async def guarded_desktop_control_scroll(
    delta_x: int = 0,
    delta_y: int = 0,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        _abort_current_browser_action_if_current_tab_unavailable(tool_context)
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.scroll requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.scroll")

    from src.tools.desktop import desktop_control_scroll
    return await desktop_control_scroll(delta_x=delta_x, delta_y=delta_y)


async def guarded_desktop_control_drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    tool_context: ToolContext | None = None,
) -> dict:
    if tool_context is not None:
        status = tool_context.state.get(StateKeys.APPROVAL_STATUS, "")
        if status != "human_approved":
            raise PermissionError(
                "desktop.control.drag requires human_approved status. "
                f"Current status: '{status}'"
            )
        _check_capability_in_plan(tool_context, "desktop.control.drag")

    from src.tools.desktop import desktop_control_drag
    return await desktop_control_drag(
        start_x=start_x,
        start_y=start_y,
        end_x=end_x,
        end_y=end_y,
    )
