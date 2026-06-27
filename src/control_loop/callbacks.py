"""
Callbacks — boiled-claw v2

ADK callback_context.state 経由で session state を更新する。
Session を直接書き換えない（ADK 推奨の context 経由のみ）。

含まれる callback:
  - policy_judge_callback  : planner_agent の after_agent_callback
  - repair_callback        : verifier_agent の after_agent_callback
  - curator_callback       : verifier_agent pass 後の memory candidate 抽出
"""

from __future__ import annotations

import json
import uuid
import logging
import re
from datetime import datetime, timezone
from typing import AbstractSet, Optional

from google.adk.agents.callback_context import CallbackContext
from src.control_loop.constants import DEFAULT_MAX_REPAIR_ATTEMPTS
from src.runtime.state_keys import StateKeys
from src.runtime.task_keywords import (
    CURRENT_BROWSER_KEYWORDS,
    SPREADSHEET_KEYWORDS,
    TEXT_ENTRY_KEYWORDS,
    prefers_isolated_browser_for_goal,
)
from src.tools.context import resolve_callback_context

logger = logging.getLogger(__name__)

# ── Policy Judge callback ──────────────────────────────────────────────────

# human approval が必要な capability (= 自動承認不可)
_HUMAN_REQUIRED_CAPS: set[str] = {
    "file.write",
    "shell.exec",
    "spawn.agent",
    "memory.delete",
    "desktop.view.screenshot",
    "desktop.ax.snapshot",
    "desktop.control.click",
    "desktop.control.type",
    "desktop.control.launch_app",
    "desktop.control.focus_window",
    "desktop.control.hotkey",
    "desktop.control.scroll",
    "desktop.control.drag",
}

# 常に拒否する capability
_ALWAYS_DENIED_CAPS: set[str] = {"admin"}

_HOTKEY_HINT_KEYWORDS: set[str] = {
    "hotkey",
    "shortcut",
    "space key",
    "spacebar",
    "enter key",
    "return key",
    "keyboard shortcut",
    "スペースキー",
    "スペース",
    "ショートカット",
    "ホットキー",
    "enter",
    "return",
}

_PLAYBACK_HINT_KEYWORDS: set[str] = {
    "play music",
    "playback",
    "play song",
    "play track",
    "music",
    "song",
    "track",
    "audio",
    "media",
    "dj",
    "djay",
    "再生",
    "楽曲",
    "曲をかけて",
    "曲を再生",
    "音楽",
}

_PLAYBACK_ACTION_STEP_KEYWORDS: set[str] = {
    "play",
    "playback",
    "start",
    "resume",
    "再生",
    "開始",
    "スタート",
}

_CURRENT_BROWSER_NAVIGATION_HINT_KEYWORDS: set[str] = {
    "search",
    "search results",
    "navigate",
    "address bar",
    "url",
    "google sheets",
    "google spreadsheet",
    "sheets.google.com",
    "sheets.new",
    "検索",
    "検索結果",
    "移動",
    "遷移",
    "アドレスバー",
}
_CURRENT_BROWSER_SPREADSHEET_NAVIGATION_HINT_KEYWORDS: set[str] = (
    set(SPREADSHEET_KEYWORDS)
    | {
        "google sheets",
        "google spreadsheet",
        "sheets.google.com",
        "sheets.new",
        "docs.google.com/spreadsheets",
    }
)
_CURRENT_BROWSER_NEW_TAB_STEP_KEYWORDS: set[str] = {
    "new tab",
    "another new tab",
    "empty tab",
    "新しいタブ",
    "新規タブ",
    "空のタブ",
}
_CURRENT_BROWSER_SPREADSHEET_ENTRY_HINT_KEYWORDS: set[str] = set(
    TEXT_ENTRY_KEYWORDS
    | {
        "cell",
        "cells",
        "grid",
        "grid cell",
        "a1",
        "セル",
        "編集グリッド",
    }
)
_CURRENT_BROWSER_SPREADSHEET_OPEN_ACTION_KEYWORDS: set[str] = {
    "open",
    "launch",
    "access",
    "create",
    "navigate",
    "go to",
    "開く",
    "起動",
    "アクセス",
    "作成",
    "新規シート",
}

_VISUAL_EVIDENCE_KEYWORDS: set[str] = {
    "waveform",
    "indicator",
    "playing",
    "playback",
    "wave form",
    "波形",
    "インジケーター",
    "再生中",
    "動いている",
}

_CAPABILITY_MODE_BY_NAME: dict[str, str] = {
    "desktop.view.windows": "read",
    "desktop.view.frontmost_app": "read",
    "desktop.view.screenshot": "read",
    "desktop.wait.window": "read",
    "desktop.ax.find": "read",
    "desktop.wait.element": "read",
    "desktop.ax.snapshot": "read",
    "desktop.control.click": "execute",
    "desktop.control.type": "execute",
    "desktop.control.launch_app": "execute",
    "desktop.control.focus_window": "execute",
    "desktop.control.hotkey": "execute",
    "desktop.control.scroll": "execute",
    "desktop.control.drag": "execute",
    "current_tab.info": "read",
    "current_tab.extract_text": "read",
    "current_tab.navigate": "read",
    "current_tab.click": "execute",
    "current_tab.fill": "execute",
}
_MISSION_ALLOWED_ACTION_ALIASES: dict[str, set[str]] = {
    "current_tab.read": {"current_tab.info", "current_tab.extract_text"},
    "current_tab.inspect": {"current_tab.info"},
}

_CURRENT_BROWSER_GOOGLE_SHEETS_CREATE_URL = (
    "https://docs.google.com/spreadsheets/create"
)


def _text_contains_keyword(text: str, keyword: str) -> bool:
    normalized_keyword = keyword.strip().lower()
    if not normalized_keyword:
        return False
    if re.fullmatch(r"[a-z0-9 ]+", normalized_keyword):
        pattern = r"(?<![a-z0-9])" + re.escape(normalized_keyword) + r"(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return normalized_keyword in text


def _contains_any(text: str, keywords: AbstractSet[str]) -> bool:
    return any(_text_contains_keyword(text, keyword) for keyword in keywords)


def _mission_contract_allowed_capabilities(goal: str) -> set[str]:
    allowed: set[str] = set()
    for line in str(goal or "").splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("- allowed_actions:"):
            continue
        _, raw_value = stripped.split(":", 1)
        try:
            parsed = json.loads(raw_value.strip())
            values = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            values = raw_value.split(",")
        for value in values:
            action = str(value or "").strip()
            if not action:
                continue
            if action in _CAPABILITY_MODE_BY_NAME:
                allowed.add(action)
            allowed.update(_MISSION_ALLOWED_ACTION_ALIASES.get(action, set()))
    return allowed


def _apply_mission_allowed_capabilities(plan: dict, allowed_caps: set[str]) -> None:
    if not allowed_caps:
        return
    required_caps = [
        cap if isinstance(cap, dict) else {"name": str(cap)}
        for cap in plan.get("required_capabilities", [])
    ]
    for capability_name in sorted(allowed_caps):
        _ensure_capability(required_caps, capability_name)
    plan["required_capabilities"] = [
        cap
        for cap in required_caps
        if str(cap.get("name", "")).strip() in allowed_caps
    ]

    steps = plan.get("steps")
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        capabilities = step.get("capabilities")
        fallback_capability = next(iter(allowed_caps)) if len(allowed_caps) == 1 else None
        if not isinstance(capabilities, list):
            if fallback_capability:
                step["capabilities"] = [
                    {
                        "name": fallback_capability,
                        "mode": _CAPABILITY_MODE_BY_NAME.get(
                            fallback_capability,
                            "execute",
                        ),
                    }
                ]
            continue
        filtered = [
            cap
            for cap in capabilities
            if isinstance(cap, dict)
            and str(cap.get("name", "")).strip() in allowed_caps
        ]
        if not filtered and fallback_capability:
            filtered = [
                {
                    "name": fallback_capability,
                    "mode": _CAPABILITY_MODE_BY_NAME.get(
                        fallback_capability,
                        "execute",
                    ),
                }
            ]
        step["capabilities"] = filtered


def _targets_current_browser(goal: str) -> bool:
    return _contains_any(goal, CURRENT_BROWSER_KEYWORDS | SPREADSHEET_KEYWORDS) and not prefers_isolated_browser_for_goal(goal)


def _needs_text_entry(goal: str) -> bool:
    return _contains_any(goal, SPREADSHEET_KEYWORDS | TEXT_ENTRY_KEYWORDS)


def _ensure_capability(
    required_caps: list[dict[str, object]],
    capability_name: str,
) -> None:
    if any(cap.get("name") == capability_name for cap in required_caps):
        return
    required_caps.append(
        {
            "name": capability_name,
            "mode": _CAPABILITY_MODE_BY_NAME.get(capability_name, "execute"),
        }
    )


def _step_has_capability(step: dict[str, object], capability_name: str) -> bool:
    for capability in step.get("capabilities", []):
        if isinstance(capability, dict) and str(capability.get("name", "")).strip() == capability_name:
            return True
    return False


def _step_has_any_capability(step: dict[str, object], capability_names: AbstractSet[str]) -> bool:
    return any(_step_has_capability(step, name) for name in capability_names)


def _ensure_step_capability(step: dict[str, object], capability_name: str) -> None:
    capabilities = step.get("capabilities")
    if not isinstance(capabilities, list):
        capabilities = []
        step["capabilities"] = capabilities
    if any(
        isinstance(capability, dict)
        and str(capability.get("name", "")).strip() == capability_name
        for capability in capabilities
    ):
        return
    capabilities.append(
        {
            "name": capability_name,
            "mode": _CAPABILITY_MODE_BY_NAME.get(capability_name, "execute"),
        }
    )


def _remove_step_capability(step: dict[str, object], capability_name: str) -> None:
    capabilities = step.get("capabilities")
    if not isinstance(capabilities, list):
        return
    step["capabilities"] = [
        capability
        for capability in capabilities
        if not (
            isinstance(capability, dict)
            and str(capability.get("name", "")).strip() == capability_name
        )
    ]


def _step_capability_names(plan: dict) -> set[str]:
    names: set[str] = set()
    for step in plan.get("steps", []):
        if not isinstance(step, dict):
            continue
        for capability in step.get("capabilities", []):
            if not isinstance(capability, dict):
                continue
            name = str(capability.get("name", "")).strip()
            if name:
                names.add(name)
    return names


def _next_step_id(existing_ids: set[str], base: str) -> str:
    if base not in existing_ids:
        return base
    index = 2
    while f"{base}_{index}" in existing_ids:
        index += 1
    return f"{base}_{index}"


def _normalized_step_dependencies(step: dict[str, object]) -> list[str]:
    depends_on = step.get("depends_on", [])
    if not isinstance(depends_on, list):
        return []
    normalized: list[str] = []
    for dependency in depends_on:
        dependency_id = str(dependency or "").strip()
        if dependency_id and dependency_id not in normalized:
            normalized.append(dependency_id)
    return normalized


def _replace_step_dependency(
    step: dict[str, object],
    removed_step_id: str,
    replacement_step_ids: list[str],
) -> None:
    normalized = _normalized_step_dependencies(step)
    if removed_step_id not in normalized:
        return
    updated: list[str] = []
    for dependency_id in normalized:
        if dependency_id == removed_step_id:
            for replacement_step_id in replacement_step_ids:
                if replacement_step_id and replacement_step_id not in updated:
                    updated.append(replacement_step_id)
            continue
        if dependency_id not in updated:
            updated.append(dependency_id)
    step["depends_on"] = updated


def _step_text_haystack(step: dict[str, object]) -> str:
    chunks = [str(step.get("title") or ""), str(step.get("description") or "")]
    expected = step.get("expected_outputs", [])
    if isinstance(expected, list):
        chunks.extend(str(item) for item in expected)
    return " ".join(chunks).lower()


def _normalize_desktop_plan_steps(plan: dict, goal: str) -> None:
    raw_steps = plan.get("steps", [])
    if not isinstance(raw_steps, list) or not raw_steps:
        return
    steps = [step for step in raw_steps if isinstance(step, dict)]
    if not steps:
        return

    top_level_cap_names = {
        str(cap.get("name", "")).strip()
        for cap in plan.get("required_capabilities", [])
        if isinstance(cap, dict)
    }
    step_cap_names = _step_capability_names(plan)
    cap_names = top_level_cap_names | step_cap_names
    ui_capability_names = {
        "desktop.ax.find",
        "desktop.wait.element",
        "desktop.control.click",
        "desktop.control.type",
        "desktop.control.drag",
        "desktop.control.hotkey",
        "desktop.control.scroll",
    }

    existing_ids = {
        str(step.get("step_id", "")).strip()
        for step in steps
        if str(step.get("step_id", "")).strip()
    }
    launch_index = next(
        (index for index, step in enumerate(steps) if _step_has_capability(step, "desktop.control.launch_app")),
        None,
    )

    has_desktop_ui_plan = bool(cap_names & ui_capability_names)
    if launch_index is not None and has_desktop_ui_plan and not any(
        _step_has_capability(step, "desktop.control.focus_window") for step in steps
    ):
        launch_step_id = str(steps[launch_index].get("step_id") or "").strip()
        if launch_step_id:
            focus_step_id = _next_step_id(existing_ids, f"{launch_step_id}_focus")
            focus_step = {
                "step_id": focus_step_id,
                "title": "アプリを前面にする",
                "description": "起動したアプリのウィンドウを前面にして操作対象を確定する。",
                "depends_on": [launch_step_id],
                "capabilities": [
                    {"name": "desktop.control.focus_window", "mode": "execute"},
                    {"name": "desktop.wait.window", "mode": "read"},
                ],
                "expected_outputs": ["対象アプリのウィンドウが前面で操作可能になっていること"],
                "retryable": True,
            }
            steps.insert(launch_index + 1, focus_step)
            existing_ids.add(focus_step_id)
            for step in steps[launch_index + 2 :]:
                depends_on = step.get("depends_on", [])
                if not isinstance(depends_on, list):
                    continue
                normalized_deps = [str(dep) for dep in depends_on]
                if launch_step_id in normalized_deps and focus_step_id not in normalized_deps:
                    step["depends_on"] = [
                        focus_step_id if dep == launch_step_id else dep
                        for dep in normalized_deps
                    ]

    playback_index = next(
        (
            index
            for index, step in enumerate(steps)
            if _step_is_playback_action_step(step)
            and _step_has_any_capability(
                step,
                {"desktop.control.click", "desktop.control.hotkey"},
            )
        ),
        None,
    )
    if playback_index is not None:
        has_pre_playback_capture = any(
            _step_has_capability(step, "desktop.view.screenshot")
            for step in steps[:playback_index]
        )
        if not has_pre_playback_capture:
            playback_step = steps[playback_index]
            depends_on = playback_step.get("depends_on", [])
            dependency_step_id = ""
            if isinstance(depends_on, list) and depends_on:
                dependency_step_id = str(depends_on[-1] or "").strip()
            if not dependency_step_id and playback_index > 0:
                dependency_step_id = str(
                    steps[playback_index - 1].get("step_id") or ""
                ).strip()
            capture_step_id = _next_step_id(
                existing_ids,
                "capture_pre_playback_state",
            )
            capture_step = {
                "step_id": capture_step_id,
                "title": "再生前の状態を記録",
                "description": "再生操作の前にUIの状態をスクリーンショットで記録する。",
                "depends_on": [dependency_step_id] if dependency_step_id else [],
                "capabilities": [
                    {"name": "desktop.view.screenshot", "mode": "read"},
                ],
                "expected_outputs": [
                    "再生前のUI状態のスクリーンショットが取得できていること",
                ],
                "retryable": True,
            }
            steps.insert(playback_index, capture_step)
            existing_ids.add(capture_step_id)
            if isinstance(depends_on, list) and depends_on:
                playback_step["depends_on"] = [
                    capture_step_id if str(dep or "").strip() == dependency_step_id else dep
                    for dep in depends_on
                ]
            else:
                playback_step["depends_on"] = [capture_step_id]

    playback_index = next(
        (
            index
            for index, step in enumerate(steps)
            if _step_is_playback_action_step(step)
            and _step_has_any_capability(
                step,
                {"desktop.control.click", "desktop.control.hotkey"},
            )
        ),
        None,
    )
    # Only inject a visual-evidence capture step when there is an actual
    # playback action step in the plan.  For non-playback tasks (e.g.
    # spreadsheet editing) the "再生状態を確認" description would confuse
    # the executor into calling desktop.control.launch_app, so we skip the
    # injection entirely and rely on required_capabilities + screenshot
    # handling in _normalize_required_capabilities instead.
    if playback_index is not None and _plan_needs_visual_evidence_capture(plan, goal) and not any(
        _step_has_any_capability(step, {"desktop.ax.snapshot", "desktop.view.screenshot"})
        for step in steps[playback_index + 1:]
    ):
        dependency_step_id = str(steps[-1].get("step_id") or "").strip()
        verify_step_id = _next_step_id(existing_ids, "verify_visual_state")
        verify_step = {
            "step_id": verify_step_id,
            "title": "再生状態を確認",
            "description": "UIの再生インジケーターや波形を読み取り、必要ならスクリーンショットも残して再生状態を確認する。",
            "depends_on": [dependency_step_id] if dependency_step_id else [],
            "capabilities": [
                {"name": "desktop.ax.find", "mode": "read"},
                {"name": "desktop.wait.element", "mode": "read"},
                {"name": "desktop.ax.snapshot", "mode": "read"},
                {"name": "desktop.view.screenshot", "mode": "read"},
            ],
            "expected_outputs": ["再生中であることの視覚的証拠が取得できていること"],
            "retryable": True,
        }
        steps.append(verify_step)

    if _plan_text_has_playback_hint(plan, goal):
        for step in reversed(steps):
            if not _step_has_playback_hint(step):
                continue
            if not _step_has_capability(step, "desktop.control.click"):
                continue
            _ensure_step_capability(step, "desktop.control.hotkey")
            description = str(step.get("description") or "")
            lowered = description.lower()
            if "スペースキー" not in description and "space" not in lowered:
                step["description"] = (
                    f"{description} 必要ならスペースキーなどのホットキーで再生開始も試みる。".strip()
                )
            break

    normalized_goal = (goal or "").strip().lower()
    if (
        _targets_current_browser(normalized_goal)
        and not prefers_isolated_browser_for_goal(normalized_goal)
        and _needs_text_entry(normalized_goal)
    ):
        for step in list(steps):
            if not _step_is_current_browser_new_tab_step(step):
                continue
            step_id = str(step.get("step_id") or "").strip()
            if not step_id:
                continue
            replacement_dependencies = _normalized_step_dependencies(step)
            for candidate_step in steps:
                if candidate_step is step:
                    continue
                _replace_step_dependency(
                    candidate_step,
                    step_id,
                    replacement_dependencies,
                )
            steps = [candidate_step for candidate_step in steps if candidate_step is not step]

        for step in steps:
            if not _step_has_any_capability(
                step,
                {
                    "desktop.control.hotkey",
                    "desktop.control.type",
                    "current_tab.navigate",
                },
            ):
                continue
            if not _step_needs_current_browser_navigation_rewrite(step):
                continue
            _ensure_step_capability(step, "current_tab.navigate")
            _remove_step_capability(step, "desktop.control.type")
            _remove_step_capability(step, "desktop.control.hotkey")
            _rewrite_current_browser_navigation_description(step)

        for step in steps:
            if not _step_has_any_capability(
                step,
                {"desktop.control.click", "desktop.control.type"},
            ):
                continue
            if _step_needs_current_browser_navigation_rewrite(step):
                continue
            if not _step_has_current_browser_spreadsheet_entry_hint(step):
                continue
            _ensure_step_capability(step, "desktop.control.click")
            _ensure_step_capability(step, "desktop.control.type")
            _ensure_step_capability(step, "desktop.control.hotkey")
            _ensure_step_capability(step, "desktop.ax.find")
            _ensure_step_capability(step, "desktop.wait.element")
            _rewrite_current_browser_spreadsheet_entry_description(step)

        has_current_tab_capture = any(
            _step_has_capability(step, "current_tab.info") for step in steps
        )
        if not has_current_tab_capture:
            dependency_step_id = str(steps[-1].get("step_id") or "").strip()
            capture_step_id = _next_step_id(existing_ids, "capture_current_tab_state")
            capture_step = {
                "step_id": capture_step_id,
                "title": "現在のタブ状態を記録",
                "description": (
                    "入力後に現在のブラウザタブのURLとタイトルを取得し、"
                    "必要ならページテキストとスクリーンショットで入力先を確認する。"
                ),
                "depends_on": [dependency_step_id] if dependency_step_id else [],
                "capabilities": [
                    {"name": "current_tab.info", "mode": "read"},
                    {"name": "current_tab.extract_text", "mode": "read"},
                    {"name": "desktop.view.screenshot", "mode": "read"},
                ],
                "expected_outputs": [
                    "現在のタブURLとタイトルが取得でき、入力後のページ状態の証拠が残っていること"
                ],
                "retryable": True,
            }
            steps.append(capture_step)

    if not prefers_isolated_browser_for_goal(normalized_goal):
        for step in steps:
            if not _step_needs_spreadsheet_navigation_rewrite(step):
                continue
            _ensure_step_capability(step, "current_tab.navigate")
            _remove_step_capability(step, "desktop.control.type")
            _remove_step_capability(step, "desktop.control.hotkey")
            _rewrite_current_browser_navigation_description(step)

    plan["steps"] = steps


_ISOLATED_BROWSER_REWRITE_CAPS: set[str] = {
    "current_tab.info",
    "current_tab.extract_text",
    "current_tab.navigate",
    "current_tab.click",
    "current_tab.fill",
    "desktop.view.windows",
    "desktop.view.frontmost_app",
    "desktop.view.screenshot",
    "desktop.wait.window",
    "desktop.ax.find",
    "desktop.wait.element",
    "desktop.ax.snapshot",
    "desktop.control.click",
    "desktop.control.type",
    "desktop.control.launch_app",
    "desktop.control.focus_window",
    "desktop.control.hotkey",
    "desktop.control.scroll",
    "desktop.control.drag",
}


def _normalize_isolated_browser_steps(plan: dict, goal: str) -> None:
    normalized_goal = (goal or plan.get("goal") or "").strip().lower()
    if not prefers_isolated_browser_for_goal(normalized_goal):
        return
    steps = plan.get("steps", [])
    if not isinstance(steps, list):
        return

    for step in steps:
        if not isinstance(step, dict):
            continue
        capabilities = step.get("capabilities")
        if not isinstance(capabilities, list):
            continue

        rewritten_capabilities: list[dict[str, object]] = []
        saw_rewritten_capability = False
        saw_browser_capability = False
        for capability in capabilities:
            if not isinstance(capability, dict):
                continue
            name = str(capability.get("name", "")).strip()
            if not name:
                continue
            if name == "browser.navigate":
                saw_browser_capability = True
                rewritten_capabilities.append(capability)
                continue
            if name in _ISOLATED_BROWSER_REWRITE_CAPS:
                saw_rewritten_capability = True
                continue
            rewritten_capabilities.append(capability)

        if saw_rewritten_capability and not saw_browser_capability:
            rewritten_capabilities.append(
                {"name": "browser.navigate", "mode": "network"}
            )

        if rewritten_capabilities:
            step["capabilities"] = rewritten_capabilities

        title = str(step.get("title") or "")
        description = str(step.get("description") or "")
        step["title"] = (
            title.replace("現在のタブ", "隔離ブラウザのページ")
            .replace("Current Tab", "Isolated Browser Page")
            .replace("Google Spreadsheet", "Google Spreadsheet (Isolated Browser)")
        )
        step["description"] = (
            description.replace("現在のブラウザ", "隔離ブラウザ")
            .replace("今開いているブラウザ", "隔離ブラウザ")
            .replace("現在のタブ", "隔離ブラウザのページ")
            .replace("existing browser window", "isolated browser session")
        )
        expected_outputs = step.get("expected_outputs")
        if isinstance(expected_outputs, list):
            step["expected_outputs"] = [
                str(item)
                .replace("現在のブラウザ", "隔離ブラウザ")
                .replace("現在のタブ", "隔離ブラウザのページ")
                for item in expected_outputs
            ]


def _plan_text_chunks(plan: dict, goal: str) -> list[str]:
    chunks: list[str] = [str(goal or plan.get("goal") or "")]
    for step in plan.get("steps", []):
        if not isinstance(step, dict):
            continue
        chunks.append(str(step.get("title") or ""))
        chunks.append(str(step.get("description") or ""))
        expected = step.get("expected_outputs", [])
        if isinstance(expected, list):
            chunks.extend(str(item) for item in expected)
    for criterion in plan.get("success_criteria", []):
        if not isinstance(criterion, dict):
            continue
        chunks.append(str(criterion.get("description") or ""))
    return chunks


def _plan_text_has_hotkey_hint(plan: dict, goal: str) -> bool:
    chunks = _plan_text_chunks(plan, goal)
    haystack = " ".join(chunks).lower()
    return _contains_any(haystack, _HOTKEY_HINT_KEYWORDS)


def _plan_text_has_playback_hint(plan: dict, goal: str) -> bool:
    chunks = _plan_text_chunks(plan, goal)
    haystack = " ".join(chunks).lower()
    return _contains_any(haystack, _PLAYBACK_HINT_KEYWORDS)


def _plan_needs_visual_evidence_capture(plan: dict, goal: str) -> bool:
    chunks = _plan_text_chunks(plan, goal)
    haystack = " ".join(chunks).lower()
    return _contains_any(haystack, _VISUAL_EVIDENCE_KEYWORDS | _PLAYBACK_HINT_KEYWORDS)


def _step_has_playback_hint(step: dict[str, object]) -> bool:
    haystack = _step_text_haystack(step)
    return _contains_any(haystack, _PLAYBACK_HINT_KEYWORDS)


def _step_is_playback_action_step(step: dict[str, object]) -> bool:
    haystack = _step_text_haystack(step)
    return _contains_any(haystack, _PLAYBACK_ACTION_STEP_KEYWORDS)


def _step_has_current_browser_navigation_hint(step: dict[str, object]) -> bool:
    haystack = _step_text_haystack(step)
    return _contains_any(haystack, _CURRENT_BROWSER_NAVIGATION_HINT_KEYWORDS)


def _step_has_current_browser_spreadsheet_navigation_hint(
    step: dict[str, object],
) -> bool:
    haystack = _step_text_haystack(step)
    return _contains_any(haystack, _CURRENT_BROWSER_SPREADSHEET_NAVIGATION_HINT_KEYWORDS)


def _step_has_current_browser_spreadsheet_entry_hint(
    step: dict[str, object],
) -> bool:
    haystack = _step_text_haystack(step)
    return _contains_any(
        haystack, _CURRENT_BROWSER_SPREADSHEET_NAVIGATION_HINT_KEYWORDS
    ) and _contains_any(haystack, _CURRENT_BROWSER_SPREADSHEET_ENTRY_HINT_KEYWORDS)


def _step_is_current_browser_new_tab_step(step: dict[str, object]) -> bool:
    capability_names = {
        str(capability.get("name", "")).strip()
        for capability in step.get("capabilities", [])
        if isinstance(capability, dict)
    }
    if capability_names != {"desktop.control.hotkey"}:
        return False
    haystack = _step_text_haystack(step)
    return _contains_any(haystack, _CURRENT_BROWSER_NEW_TAB_STEP_KEYWORDS)


def _step_needs_current_browser_navigation_rewrite(
    step: dict[str, object],
) -> bool:
    if _step_has_current_browser_spreadsheet_entry_hint(step):
        return False
    if _step_has_current_browser_navigation_hint(step):
        return True
    return _step_has_current_browser_spreadsheet_navigation_hint(step) and _step_has_any_capability(
        step,
        {"desktop.control.hotkey", "current_tab.navigate"},
    )


def _step_needs_spreadsheet_navigation_rewrite(
    step: dict[str, object],
) -> bool:
    if _step_has_current_browser_spreadsheet_entry_hint(step):
        return False
    return _step_has_current_browser_spreadsheet_navigation_hint(step) and _contains_any(
        _step_text_haystack(step),
        _CURRENT_BROWSER_SPREADSHEET_OPEN_ACTION_KEYWORDS,
    ) and _step_has_any_capability(
        step,
        {
            "desktop.control.hotkey",
            "desktop.control.type",
            "desktop.view.frontmost_app",
            "current_tab.navigate",
        },
    )


def _rewrite_current_browser_navigation_description(
    step: dict[str, object],
) -> None:
    description = str(step.get("description") or "").strip()
    for pattern, replacement in (
        (
            r"(?i)\busing\s+(?:cmd|ctrl)\s*/\s*(?:cmd|ctrl)\s*\+\s*t\s*(?:and\s+)?",
            "",
        ),
        (
            r"(?i)\busing\s+(?:cmd|ctrl|cmd/control|ctrl/cmd)\s*\+\s*t\s*(?:and\s+)?",
            "",
        ),
        (
            r"(?i)^\(\s*(?:cmd|ctrl)\s*/\s*(?:cmd|ctrl)\s*\+\s*t\s*\)\s*(?:,\s*|and\s+)?",
            "",
        ),
        (
            r"(?i)^\(\s*(?:cmd|ctrl)\s*/\s*(?:cmd|ctrl)\s*\+\s*t\s*\),?\s*",
            "",
        ),
        (
            r"(?i)\b(?:cmd|ctrl)\s*\+\s*t\s*\(\s*(?:cmd|ctrl)\s*\+\s*t\s*\)\s*を?使用して\s*",
            "",
        ),
        (
            r"(?i)\b(?:cmd|ctrl)\s*/\s*(?:cmd|ctrl)\s*\+\s*t\s*で\s*",
            "",
        ),
        (r"(?i)\b(?:cmd|ctrl|cmd/control|ctrl/cmd)\+t\b[^。.]*[、,]\s*", ""),
        (r"(?i)\b(?:cmd|ctrl|cmd/control|ctrl/cmd)\+tで\s*", ""),
        (r"(?i)\bopen another new tab and\s*", ""),
        (r"(?i)\bopen a new tab and\s*", ""),
        (r"(?i)\bopen a new tab,?\s*", ""),
        (r"(?i)\bin another new tab\b,?\s*", ""),
        (r"(?i)\bin a new tab\b,?\s*", ""),
        (r"新しいタブを開き、?", ""),
        (r"新規タブを開き、?", ""),
    ):
        description = re.sub(pattern, replacement, description).strip()
    description = re.sub(r"^[,、)\]\s]+", "", description).strip()
    if _step_has_current_browser_spreadsheet_navigation_hint(step):
        description = re.sub(
            r"(?i)\bsheets\.new\b",
            _CURRENT_BROWSER_GOOGLE_SHEETS_CREATE_URL,
            description,
        )
        description = re.sub(
            r"(?i)\bsheets\.google\.com\b",
            _CURRENT_BROWSER_GOOGLE_SHEETS_CREATE_URL,
            description,
        )
        guidance = (
            "Google Sheets はテンプレート一覧ではなく、"
            f"current_tab.navigate で {_CURRENT_BROWSER_GOOGLE_SHEETS_CREATE_URL} "
            "に直接遷移して新規スプレッドシートを開く。"
        )
    else:
        guidance = (
            "検索やページ遷移は、未送信の文字入力に頼らず、"
            "current_tab.navigate で完全なURLまたはGoogle検索URLを使って確実に遷移する。"
        )
    if guidance not in description:
        description = f"{description} {guidance}".strip()
    step["description"] = description


def _rewrite_current_browser_spreadsheet_entry_description(
    step: dict[str, object],
) -> None:
    description = str(step.get("description") or "").strip()
    guidance = (
        "Google Sheets はcanvasベースのため、まず空の新規シートなら A1 が"
        "選択済みとみなして desktop.control.type で直接値を入力し、Tab"
        "（次の列）または Enter（次の行）で次のセルへ移動する。ファイル名"
        "タイトル欄やツールバーのテキスト入力欄はクリックしない。アクティブ"
        "セルに入力できない場合だけ、Name Box（名前ボックス / セル参照入力欄）"
        "と明示的に分かる要素に限って使う。ツールバーやダイアログには入力しない。"
    )
    if guidance not in description:
        description = f"{description} {guidance}".strip()
    step["description"] = description


def _normalize_required_capabilities(plan: dict, goal: str) -> dict:
    _normalize_desktop_plan_steps(plan, goal)
    _normalize_isolated_browser_steps(plan, goal)
    steps = plan.get("steps", [])
    step_capability_names = _step_capability_names(plan)
    has_current_browser_navigation_steps = isinstance(steps, list) and any(
        isinstance(step, dict)
        and _step_has_current_browser_navigation_hint(step)
        and _step_has_any_capability(step, {"desktop.control.hotkey", "current_tab.navigate"})
        for step in steps
    )
    required_caps = [
        cap if isinstance(cap, dict) else {"name": str(cap)}
        for cap in plan.get("required_capabilities", [])
    ]
    for step_capability_name in step_capability_names:
        _ensure_capability(required_caps, step_capability_name)
    normalized_goal = (goal or plan.get("goal") or "").strip().lower()
    is_current_browser_goal = _targets_current_browser(normalized_goal)
    prefers_isolated_browser = prefers_isolated_browser_for_goal(normalized_goal)

    if is_current_browser_goal and not prefers_isolated_browser:
        required_caps = [
            cap
            for cap in required_caps
            if str(cap.get("name", "")) != "desktop.control.launch_app"
        ]
        if "desktop.control.type" not in step_capability_names:
            required_caps = [
                cap
                for cap in required_caps
                if str(cap.get("name", "")) != "desktop.control.type"
            ]
        if (
            has_current_browser_navigation_steps
            and "desktop.control.hotkey" not in step_capability_names
        ):
            required_caps = [
                cap
                for cap in required_caps
                if str(cap.get("name", "")) != "desktop.control.hotkey"
            ]

    # Plans that include current_tab.* capabilities use the host-bridge relay
    # and therefore do NOT need to launch or re-focus the browser.  Remove
    # desktop.control.launch_app unconditionally when the plan is current-tab
    # based, regardless of whether the goal text uses current-browser keywords.
    has_current_tab_caps = any(
        str(cap.get("name", "")).startswith("current_tab.")
        for cap in required_caps
    )
    if has_current_tab_caps:
        required_caps = [
            cap
            for cap in required_caps
            if str(cap.get("name", "")) != "desktop.control.launch_app"
        ]

    cap_names = {str(cap.get("name", "")) for cap in required_caps}

    if prefers_isolated_browser:
        required_caps = [
            cap
            for cap in required_caps
            if str(cap.get("name", "")) not in _ISOLATED_BROWSER_REWRITE_CAPS
        ]
        _ensure_capability(required_caps, "browser.navigate")
    elif is_current_browser_goal:
        # Current-browser tasks should reuse the browser the user already has
        # open, so we remove launch-app and expand the read/focus capabilities
        # needed to verify and steer that existing window safely.
        has_desktop_browser_plan = bool(
            cap_names
            & {
                "desktop.view.windows",
                "desktop.view.frontmost_app",
                "desktop.ax.snapshot",
                "desktop.control.focus_window",
                "desktop.ax.find",
                "desktop.wait.element",
                "desktop.control.click",
                "desktop.control.type",
                "desktop.control.hotkey",
                "current_tab.navigate",
            }
        )
        if has_desktop_browser_plan or "browser.navigate" in cap_names:
            _ensure_capability(required_caps, "current_tab.navigate")
            _ensure_capability(required_caps, "current_tab.info")
            _ensure_capability(required_caps, "desktop.view.windows")
            _ensure_capability(required_caps, "desktop.view.frontmost_app")
            _ensure_capability(required_caps, "desktop.control.focus_window")
            _ensure_capability(required_caps, "desktop.control.click")
            if (
                "desktop.control.hotkey" in step_capability_names
                or not has_current_browser_navigation_steps
            ):
                _ensure_capability(required_caps, "desktop.control.hotkey")
            _ensure_capability(required_caps, "desktop.control.scroll")
            _ensure_capability(required_caps, "desktop.ax.find")
            _ensure_capability(required_caps, "desktop.wait.element")
            _ensure_capability(required_caps, "desktop.view.screenshot")
            _ensure_capability(required_caps, "desktop.ax.snapshot")

            if _needs_text_entry(normalized_goal):
                _ensure_capability(required_caps, "current_tab.extract_text")
                if "desktop.control.type" in step_capability_names:
                    _ensure_capability(required_caps, "desktop.control.type")
    else:
        has_desktop_ui_plan = bool(
            cap_names
            & {
                "desktop.ax.find",
                "desktop.wait.element",
                "desktop.control.click",
                "desktop.control.type",
                "desktop.control.drag",
                "desktop.control.hotkey",
                "desktop.control.scroll",
            }
        )
        if cap_names & {"desktop.control.launch_app", "desktop.control.focus_window"}:
            _ensure_capability(required_caps, "desktop.view.windows")
            _ensure_capability(required_caps, "desktop.wait.window")
        if "desktop.control.launch_app" in cap_names and has_desktop_ui_plan:
            _ensure_capability(required_caps, "desktop.control.focus_window")
        if has_desktop_ui_plan:
            _ensure_capability(required_caps, "desktop.ax.find")
            _ensure_capability(required_caps, "desktop.wait.element")
            _ensure_capability(required_caps, "desktop.ax.snapshot")
        if _plan_needs_visual_evidence_capture(plan, normalized_goal):
            _ensure_capability(required_caps, "desktop.view.screenshot")
        if _plan_text_has_hotkey_hint(plan, normalized_goal) or _plan_text_has_playback_hint(
            plan, normalized_goal
        ):
            _ensure_capability(required_caps, "desktop.control.hotkey")
        if _plan_text_has_playback_hint(plan, normalized_goal) and (
            "desktop.control.focus_window" in cap_names
            or "desktop.wait.window" in cap_names
            or "desktop.view.windows" in cap_names
        ):
            # Media-app transport tasks often begin with a focus step, but the
            # executor may still need to reopen the app when the window is gone
            # or hidden. Surface launch_app in approval so that fallback is
            # explicit instead of failing mid-run on an unapproved capability.
            _ensure_capability(required_caps, "desktop.control.launch_app")

    # Spreadsheet text-entry tasks need desktop.control.type and
    # desktop.control.hotkey regardless of whether the goal contains
    # current-browser keywords.  The planner often omits type/hotkey from
    # required_capabilities for Sheets tasks, causing the executor to fail
    # with "Capability 'desktop.control.type' is not in the approved plan."
    if (
        _contains_any(normalized_goal, SPREADSHEET_KEYWORDS)
        and _needs_text_entry(normalized_goal)
        and not prefers_isolated_browser
    ):
        _ensure_capability(required_caps, "desktop.control.type")
        _ensure_capability(required_caps, "desktop.control.hotkey")

    plan["required_capabilities"] = required_caps
    _apply_mission_allowed_capabilities(
        plan,
        _mission_contract_allowed_capabilities(goal or plan.get("goal", "")),
    )
    return plan

def policy_judge_callback(
    callback_context: CallbackContext,
) -> None:
    """
    planner_agent の after_agent_callback。
    temp:planner_draft を読み、capability と risk_level を評価して:
      - plan:approved
      - approval:status
      - plan:risk_level
    を session.state に書き込む。
    """
    raw_draft = callback_context.state.get(StateKeys.TEMP_PLANNER_DRAFT)
    if not raw_draft:
        callback_context.state[StateKeys.APPROVAL_STATUS] = "denied"
        logger.warning("policy_judge_callback: temp:planner_draft is empty")
        return

    try:
        plan = (
            raw_draft
            if isinstance(raw_draft, dict)
            else json.loads(raw_draft)
        )
    except (json.JSONDecodeError, TypeError) as e:
        callback_context.state[StateKeys.APPROVAL_STATUS] = "denied"
        logger.error("policy_judge_callback: JSON parse error: %s", e)
        return

    original_goal = callback_context.state.get(StateKeys.TASK_GOAL) or plan.get("goal", "")
    plan = _normalize_required_capabilities(plan, original_goal)
    required_caps: list[dict] = plan.get("required_capabilities", [])
    cap_names = {c.get("name", "") for c in required_caps}
    risk_level: str = plan.get("risk_level", "low")

    # 常に拒否
    denied = cap_names & _ALWAYS_DENIED_CAPS
    if denied:
        callback_context.state[StateKeys.APPROVAL_STATUS] = "denied"
        callback_context.state[StateKeys.APPROVAL_REQUEST] = None
        callback_context.state[StateKeys.PLAN_RISK_LEVEL] = risk_level
        logger.warning("policy_judge_callback: denied caps=%s", denied)
        return

    # Human approval が必要な capability
    needs_human = bool(cap_names & _HUMAN_REQUIRED_CAPS) or risk_level == "critical"
    if needs_human:
        approval_request = {
            "request_id": f"plan_{uuid.uuid4().hex[:12]}",
            "plan_id": plan.get("plan_id", ""),
            "goal": original_goal,
            "risk_level": risk_level,
            "required_capabilities": sorted(cap_names),
            "reason": (
                "Human approval required due to capability or risk level."
            ),
            "plan": plan,
        }
        callback_context.state[StateKeys.APPROVAL_STATUS] = "needs_human"
        callback_context.state[StateKeys.APPROVAL_REQUEST] = approval_request
        callback_context.state[StateKeys.PLAN_APPROVED] = plan
        callback_context.state[StateKeys.PLAN_RISK_LEVEL] = risk_level
        logger.info(
            "policy_judge_callback: needs_human (caps=%s, risk=%s)",
            cap_names & _HUMAN_REQUIRED_CAPS,
            risk_level,
        )
        return

    # 自動承認
    callback_context.state[StateKeys.PLAN_APPROVED] = plan
    callback_context.state[StateKeys.PLAN_RISK_LEVEL] = risk_level
    callback_context.state[StateKeys.APPROVAL_STATUS] = "policy_approved"
    callback_context.state[StateKeys.APPROVAL_REQUEST] = None
    logger.info(
        "policy_judge_callback: policy_approved (risk=%s)", risk_level
    )
    return


# ── Repair callback ────────────────────────────────────────────────────────

_MAX_REPAIR_ATTEMPTS = DEFAULT_MAX_REPAIR_ATTEMPTS
_REPAIR_THRESHOLD_SCORE = 0.85


def repair_callback(
    callback_context: CallbackContext,
) -> None:
    """
    verifier_agent の after_agent_callback。
    verify:last_report を読み、repair が必要かどうかを判断して:
      - repair:count をインクリメント
      - temp:repair_patch を設定
    を session.state に書き込む。

    pass の場合は何もしない（curator_callback が後続処理する）。
    repair 上限に達した場合も何もしない。
    """
    raw_report = callback_context.state.get(StateKeys.VERIFY_LAST_REPORT)
    if not raw_report:
        return

    try:
        report = (
            raw_report
            if isinstance(raw_report, dict)
            else json.loads(raw_report)
        )
    except (json.JSONDecodeError, TypeError):
        return

    status = report.get("status", "error")
    if status == "pass":
        # 検証通過: repair 不要、repair:count をリセット
        callback_context.state[StateKeys.REPAIR_COUNT] = 0
        callback_context.state[StateKeys.TEMP_REPAIR_PATCH] = None
        return

    # fail / partial_pass → repair 判断
    repair_count = callback_context.state.get(StateKeys.REPAIR_COUNT, 0)

    if repair_count >= _MAX_REPAIR_ATTEMPTS:
        logger.warning(
            "repair_callback: max repair attempts (%d) reached", _MAX_REPAIR_ATTEMPTS
        )
        return

    repair_actions = report.get("repair_actions", [])
    failed_criteria = [
        r["name"] for r in report.get("criterion_results", []) if not r.get("passed")
    ]

    patch = {
        "note": f"Re-plan required. Failed criteria: {failed_criteria}. "
                f"Repair attempt {repair_count + 1}/{_MAX_REPAIR_ATTEMPTS}.",
        "failed_criteria": failed_criteria,
        "repair_actions": repair_actions,
        "previous_plan_id": (
            report.get("plan_id") or
            (callback_context.state.get(StateKeys.PLAN_APPROVED) or {}).get("plan_id")
        ),
    }

    callback_context.state[StateKeys.REPAIR_COUNT] = repair_count + 1
    callback_context.state[StateKeys.TEMP_REPAIR_PATCH] = patch
    logger.info(
        "repair_callback: repair triggered (attempt=%d, status=%s)",
        repair_count + 1,
        status,
    )
    return


# ── Curator callback ───────────────────────────────────────────────────────


def curator_callback(
    callback_context: CallbackContext,
) -> None:
    """
    verifier_agent pass 後の memory candidate 抽出 callback。
    verify:last_report が pass のとき、session の情報から memory candidate を
    非同期で抽出して candidate store に登録する。

    Note: ここでは候補の ID を memory:last_candidate_ids に書くにとどめる。
    実際の promote は Curator クラスが別途実行する。
    """
    raw_report = callback_context.state.get(StateKeys.VERIFY_LAST_REPORT)
    if not raw_report:
        return

    try:
        report = (
            raw_report
            if isinstance(raw_report, dict)
            else json.loads(raw_report)
        )
    except (json.JSONDecodeError, TypeError):
        return

    if report.get("status") != "pass":
        return

    # approved plan から memory candidates を生成
    raw_plan = callback_context.state.get(StateKeys.PLAN_APPROVED)
    if not raw_plan:
        return

    try:
        plan = raw_plan if isinstance(raw_plan, dict) else json.loads(raw_plan)
    except (json.JSONDecodeError, TypeError):
        return

    candidate_ids = _extract_and_register_candidates(
        plan=plan,
        report=report,
        callback_context=callback_context,
    )

    if candidate_ids:
        callback_context.state[StateKeys.MEMORY_LAST_CANDIDATE_IDS] = candidate_ids
        logger.info(
            "curator_callback: %d candidate(s) registered", len(candidate_ids)
        )

    return


def _extract_and_register_candidates(
    plan: dict,
    report: dict,
    callback_context: CallbackContext,
) -> list[str]:
    """
    plan / report の情報から MemoryCandidate を生成して CandidateStore に登録する。
    登録した candidate_id のリストを返す。
    """
    try:
        from src.memory_lifecycle.candidate_store import get_candidate_store
        from src.memory_lifecycle.memory_schema import (
            MemoryCandidate,
            MemoryType,
            OriginatorType,
            Provenance,
            SensitivityLevel,
        )
    except ImportError:
        logger.warning("curator_callback: memory_lifecycle not available")
        return []

    store = get_candidate_store()
    now = datetime.now(tz=timezone.utc)

    runtime_context = resolve_callback_context(callback_context)
    session_id = runtime_context["session_id"] or "unknown"
    user_id = runtime_context["user_id"] or "unknown"

    candidate_ids: list[str] = []

    # 1. goal → procedural memory candidate
    goal = plan.get("goal", "")
    if goal:
        cid = f"cand_{uuid.uuid4().hex[:10]}"
        candidate = MemoryCandidate(
            candidate_id=cid,
            session_id=session_id,
            user_id=user_id,
            memory_type=MemoryType.PROCEDURAL,
            content=f"Task completed successfully: {goal}",
            subject=goal[:80],
            provenance=Provenance(
                originator_type=OriginatorType.SYSTEM,
                capture_method="control_loop_completion",
                captured_at=now,
            ),
            confidence=report.get("overall_score", 0.8),
            trust_score=report.get("confidence", 0.8),
            sensitivity=SensitivityLevel.INTERNAL,
        )
        store.save(candidate)
        candidate_ids.append(cid)

    # 2. success criteria → episodic memory candidate
    criteria = report.get("criterion_results", [])
    passed = [c["name"] for c in criteria if c.get("passed")]
    if passed:
        cid = f"cand_{uuid.uuid4().hex[:10]}"
        candidate = MemoryCandidate(
            candidate_id=cid,
            session_id=session_id,
            user_id=user_id,
            memory_type=MemoryType.EPISODIC,
            content=(
                f"Successfully completed '{goal}'. "
                f"Passed criteria: {', '.join(passed)}."
            ),
            subject=goal[:80],
            provenance=Provenance(
                originator_type=OriginatorType.SYSTEM,
                capture_method="control_loop_completion",
                captured_at=now,
            ),
            confidence=0.85,
            trust_score=0.85,
            sensitivity=SensitivityLevel.INTERNAL,
        )
        store.save(candidate)
        candidate_ids.append(cid)

    return candidate_ids
