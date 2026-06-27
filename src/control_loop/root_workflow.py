"""
Root Workflow — boiled-claw v2

ADK Runner を中心に置き、
Planner → PolicyJudge (callback) → Executor → Verifier → Repair (callback)
のループを Runner.run_async() で回す。

Runner が session.state / event history / output_key の保存を管理する。
Session への直接書き込みは行わない。
"""

from __future__ import annotations

import json
import logging
import math
import re
import struct
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import Runner
from google.adk.sessions import Session
from google.genai.types import Content, Part

from src.agents.model_config import DEFAULT_MODEL
from src.control_loop.callbacks import (
    curator_callback,
    _plan_text_has_playback_hint,
    policy_judge_callback,
    repair_callback,
)
from src.control_loop.constants import DEFAULT_MAX_REPAIR_ATTEMPTS
from src.control_loop.executor_agent import executor_agent
from src.control_loop.replay_trace import (
    _build_executor_message,
    _build_step_trace,
    _extract_plan_id,
    _infer_tail_replay_from_step,
    _parse_replay_context,
)
from src.control_loop.guarded_tools import (
    guarded_browser_click,
    guarded_browser_extract_text,
    guarded_browser_fill,
    guarded_browser_navigate,
    guarded_browser_press,
    guarded_current_tab_click,
    guarded_current_tab_extract_text,
    guarded_current_tab_fill,
    guarded_current_tab_info,
    guarded_current_tab_navigate,
    guarded_desktop_ax_find,
    guarded_desktop_ax_snapshot,
    guarded_desktop_control_click,
    guarded_desktop_control_drag,
    guarded_desktop_control_focus_window,
    guarded_desktop_control_hotkey,
    guarded_desktop_control_launch_app,
    guarded_desktop_control_scroll,
    guarded_desktop_control_type,
    guarded_desktop_view_frontmost_app,
    guarded_desktop_view_screenshot,
    guarded_desktop_view_windows,
    guarded_desktop_wait_element,
    guarded_desktop_wait_window,
    guarded_memory_read,
    guarded_read_file,
    guarded_web_search,
    guarded_write_file,
)
from src.control_loop.planner_agent import planner_agent
from src.runtime.session_service import create_session_service
from src.control_loop.verifier_agent import verifier_agent
from src.runtime.replay_schema import ReplayContext
from src.runtime.state_keys import StateKeys
from src.runtime.task_keywords import (
    CURRENT_BROWSER_KEYWORDS,
    SPREADSHEET_KEYWORDS,
    prefers_isolated_browser_for_goal,
)

logger = logging.getLogger(__name__)

_APP_NAME = "boiled_claw_v2"
_MAX_REPAIR_ATTEMPTS = DEFAULT_MAX_REPAIR_ATTEMPTS
_APPROVED_STATUSES = {"policy_approved", "human_approved", "auto_approved"}
_TERMINAL_VERIFY_STATUSES = {"pass", "fail", "partial_pass", "error"}
_CONTROL_LOOP_AUTHOR = "control_loop"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PLAYBACK_SCREENSHOT_DIFF_RATIO_THRESHOLD = 0.002
_PLAYBACK_SCREENSHOT_DELTA_THRESHOLD = 0.0005
_GOOGLE_SHEETS_UI_ONLY_PHRASES = frozenset(
    {
        "untitled spreadsheet",
        "google sheets",
        "ask gemini",
        "gemini in workspace can make mistakes",
        "generate a custom spreadsheet",
        "turn on screen reader support",
        "learn more",
    }
)
_GOOGLE_SHEETS_SURFACE_MARKERS = frozenset(
    {
        "untitled spreadsheet",
        "google sheets",
        "ask gemini",
        "sheet1",
        "turn on screen reader support",
        "fileeditview",
        "share",
    }
)
_GOOGLE_SHEETS_UI_ONLY_TOKENS = frozenset(
    {
        "ask",
        "gemini",
        "share",
        "file",
        "edit",
        "view",
        "insert",
        "format",
        "data",
        "tools",
        "extensions",
        "help",
        "sheet1",
        "build",
        "submit",
        "close",
        "turn",
        "screen",
        "reader",
        "support",
        "generate",
        "custom",
        "spreadsheet",
        "learn",
        "more",
        "default",
        "arial",
        "fileeditview",
        "123",
    }
)
_TEXT_ENTRY_KEYWORDS = SPREADSHEET_KEYWORDS | frozenset(
    {
        "入力",
        "記入",
        "書いて",
        "書き込",
        "貼り付",
        "ペースト",
        "まとめて",
        "まとめる",
        "追加",
        "更新",
        "fill",
        "enter",
        "paste",
        "type",
        "write",
    }
)
_CURRENT_TAB_INFO_RESPONSE_NAMES = frozenset(
    {
        "guarded_current_tab_info",
        "current_tab.info",
        "current_tab_info",
        "host.current_tab.info",
        "guarded_current_tab_navigate",
        "current_tab.navigate",
        "current_tab_navigate",
        "host.current_tab.navigate",
    }
)
_CURRENT_TAB_EXTRACT_TEXT_RESPONSE_NAMES = frozenset(
    {
        "guarded_current_tab_extract_text",
        "current_tab.extract_text",
        "current_tab_extract_text",
        "host.current_tab.extract_text",
    }
)


# ── Callback helpers ───────────────────────────────────────────────────────

def _chain_after_callbacks(
    *cbs: Callable[[CallbackContext], None],
) -> Callable[[CallbackContext], None]:
    """複数の after_agent_callback を順番に呼ぶ合成関数を返す。"""
    def chained(
        ctx: CallbackContext | None = None,
        *,
        callback_context: CallbackContext | None = None,
    ) -> None:
        resolved_ctx = callback_context or ctx
        if resolved_ctx is None:
            raise TypeError("callback_context is required")
        for cb in cbs:
            cb(resolved_ctx)
        return
    return chained


def _contains_any(text: str, keywords: set[str] | frozenset[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _targets_current_browser_goal(goal: str) -> bool:
    normalized = (goal or "").strip().lower()
    return (
        _contains_any(normalized, CURRENT_BROWSER_KEYWORDS)
        or _contains_any(normalized, SPREADSHEET_KEYWORDS)
    ) and not prefers_isolated_browser_for_goal(normalized)


def _goal_needs_text_entry(goal: str) -> bool:
    return _contains_any((goal or "").strip().lower(), _TEXT_ENTRY_KEYWORDS)


def _response_succeeded(response: dict[str, Any]) -> bool:
    return bool(response.get("success") or response.get("ok"))


def _is_google_sheets_destination(current_tab: dict[str, Any]) -> bool:
    destination = " ".join(
        str(current_tab.get(key) or "").strip().lower()
        for key in ("url", "title")
    )
    return (
        "docs.google.com/spreadsheets" in destination
        or "sheets.new" in destination
        or "google sheets" in destination
    )


def _is_spreadsheet_task(goal: Any) -> bool:
    normalized = str(goal or "").strip().lower()
    if not normalized:
        return False
    return _contains_any(normalized, SPREADSHEET_KEYWORDS)


def _has_sheets_candidate_window(window_titles: Any) -> bool:
    if not isinstance(window_titles, list):
        return False
    text = " ".join(str(title or "") for title in window_titles).lower()
    return (
        "google sheets" in text
        or "spreadsheet" in text
        or "docs.google.com/spreadsheets" in text
    )


def _is_destination_bound_for_spreadsheet(current_tab: dict[str, Any]) -> bool:
    """Whether current_tab points to a known spreadsheet-authoring destination.

    Today we only recognize Google Sheets; extend as other destinations are supported.
    """
    return _is_google_sheets_destination(current_tab)


def _should_fail_for_target_context_mismatch(
    goal: Any,
    current_tab: dict[str, Any] | None,
    desktop: dict[str, Any] | None,
    destination_tab: dict[str, Any] | None = None,
) -> bool:
    """Generalized destination-bound evidence rule.

    If the task is a spreadsheet-authoring task but evidence is NOT destination-bound
    (no Sheets tab reachable via current_tab OR destination_tab), the evidence cannot
    certify success regardless of what text any tab contains.

    When `destination_tab` is destination-bound we've located and can read the
    spreadsheet surface, so target context is confirmed even when current_tab is
    Control UI — this is the expected B2/B3 flow where the user keeps the Control UI
    focused and the verifier side-channels the destination tab read-only.

    Non-destination-bound evidence (chat, docs listing, about:blank, unrelated site)
    still counts as a mismatch — URL/title alone never clears this rule.
    """
    if not _is_spreadsheet_task(goal):
        return False
    current_tab = current_tab if isinstance(current_tab, dict) else {}
    desktop = desktop if isinstance(desktop, dict) else {}
    # Destination-bound destination_tab clears the mismatch regardless of current_tab.
    if isinstance(destination_tab, dict) and _is_destination_bound_for_spreadsheet(
        destination_tab
    ):
        return False
    has_tab_evidence = bool(
        str(current_tab.get("url") or "").strip()
        or str(current_tab.get("title") or "").strip()
        or str(current_tab.get("text_excerpt") or "").strip()
    )
    if not has_tab_evidence:
        return False
    if _is_destination_bound_for_spreadsheet(current_tab):
        return False
    # Tab evidence exists but points somewhere other than the expected destination.
    # This is a target-context mismatch: non-destination-bound evidence.
    return True


def _destination_tab_has_meaningful_text_entry_evidence(
    destination_tab: dict[str, Any],
    goal: Any = None,
) -> bool:
    """Mirror of _current_tab_has_meaningful_text_entry_evidence for destination_tab.

    destination_tab text evidence is read from the actual spreadsheet surface
    (not the focused tab), so it satisfies the destination-bound requirement
    for spreadsheet tasks. URL presence alone is never enough — text_excerpt must
    pass the same non-boilerplate check the current_tab path already applies.
    """
    if not isinstance(destination_tab, dict):
        return False
    if not (
        destination_tab.get("extract_text_succeeded")
        and int(destination_tab.get("text_length") or 0) > 0
    ):
        return False
    text_excerpt = str(destination_tab.get("text_excerpt") or "").strip()
    if not text_excerpt:
        return False
    if _is_google_sheets_destination(destination_tab):
        return _google_sheets_excerpt_has_entered_content(text_excerpt)
    return True


def _has_destination_bound_text_evidence(
    verification_inputs: dict[str, Any],
) -> bool:
    """Whether any destination-bound tab captured meaningful text evidence.

    Checks destination_tab first (preferred, read-only capture), then falls back
    to current_tab when current_tab IS the destination (the shortcut path in
    _capture_destination_tab). URL-only never counts.
    """
    goal = verification_inputs.get("goal")
    destination_tab = verification_inputs.get("destination_tab")
    if isinstance(
        destination_tab, dict
    ) and _destination_tab_has_meaningful_text_entry_evidence(destination_tab, goal):
        return True
    current_tab = verification_inputs.get("current_tab")
    if (
        isinstance(current_tab, dict)
        and _is_destination_bound_for_spreadsheet(current_tab)
        and _current_tab_has_meaningful_text_entry_evidence(current_tab, goal)
    ):
        return True
    return False


def _google_sheets_excerpt_has_entered_content(text_excerpt: str) -> bool:
    normalized = " ".join(str(text_excerpt or "").split()).lower()
    if not normalized:
        return False
    if not any(marker in normalized for marker in _GOOGLE_SHEETS_SURFACE_MARKERS):
        return False
    cleaned = normalized
    for phrase in _GOOGLE_SHEETS_UI_ONLY_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    tokens = re.findall(
        r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]{2,}|[a-z0-9][a-z0-9_./:+#&()'-]{2,}",
        cleaned,
    )
    meaningful_tokens = [
        token
        for token in tokens
        if token not in _GOOGLE_SHEETS_UI_ONLY_TOKENS
    ]
    return bool(meaningful_tokens)


def _google_sheets_excerpt_meaningful_token_count(text_excerpt: str) -> int:
    normalized = " ".join(str(text_excerpt or "").split()).lower()
    if not normalized:
        return 0
    if not any(marker in normalized for marker in _GOOGLE_SHEETS_SURFACE_MARKERS):
        return 0
    cleaned = normalized
    for phrase in _GOOGLE_SHEETS_UI_ONLY_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    tokens = re.findall(
        r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]{2,}|[a-z0-9][a-z0-9_./:+#&()'-]{2,}",
        cleaned,
    )
    meaningful_tokens = [
        token
        for token in tokens
        if token not in _GOOGLE_SHEETS_UI_ONLY_TOKENS
    ]
    return len(dict.fromkeys(meaningful_tokens))


def _has_sparse_google_sheets_destination_text_evidence(
    verification_inputs: dict[str, Any],
) -> bool:
    goal = verification_inputs.get("goal")
    if not _is_spreadsheet_task(goal):
        return False
    for key in ("destination_tab", "current_tab"):
        tab = verification_inputs.get(key)
        if not isinstance(tab, dict) or not _is_destination_bound_for_spreadsheet(tab):
            continue
        if not _is_google_sheets_destination(tab):
            continue
        text_excerpt = str(tab.get("text_excerpt") or "").strip()
        if not text_excerpt:
            continue
        token_count = _google_sheets_excerpt_meaningful_token_count(text_excerpt)
        if 0 < token_count < 4:
            return True
    return False


def _current_tab_has_meaningful_text_entry_evidence(
    current_tab: dict[str, Any],
    goal: Any = None,
) -> bool:
    if not (
        current_tab.get("extract_text_succeeded")
        and int(current_tab.get("text_length") or 0) > 0
    ):
        return False
    text_excerpt = str(current_tab.get("text_excerpt") or "").strip()
    if not text_excerpt:
        return False
    # Generalized destination-bound rule: for spreadsheet tasks, evidence from a
    # non-destination tab (Control UI, unrelated site, new-tab page, etc.) is not
    # meaningful no matter how much text it contains.
    if _is_spreadsheet_task(goal) and not _is_destination_bound_for_spreadsheet(current_tab):
        return False
    if _is_google_sheets_destination(current_tab):
        return _google_sheets_excerpt_has_entered_content(text_excerpt)
    return True


def _current_tab_text_conflicts_with_destination(
    current_tab: dict[str, Any],
    goal: Any = None,
) -> bool:
    if not (
        current_tab.get("extract_text_succeeded")
        and int(current_tab.get("text_length") or 0) > 0
    ):
        return False
    text_excerpt = str(current_tab.get("text_excerpt") or "").strip()
    if not text_excerpt:
        return False
    # Generalized: a spreadsheet task with a non-destination current_tab is always
    # a destination mismatch, regardless of what the tab's text contains.
    if _is_spreadsheet_task(goal) and not _is_destination_bound_for_spreadsheet(current_tab):
        return True
    if _is_google_sheets_destination(current_tab):
        return not any(
            marker in " ".join(text_excerpt.split()).lower()
            for marker in _GOOGLE_SHEETS_SURFACE_MARKERS
        )
    return False


def _artifact_refs_have_visual_evidence(artifact_refs: Any) -> bool:
    refs = artifact_refs if isinstance(artifact_refs, list) else []
    for ref in refs:
        normalized = str(ref or "").strip().lower()
        if not normalized:
            continue
        if normalized.startswith(("http://", "https://")):
            continue
        if normalized.endswith(
            (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".svg")
        ):
            return True
    return False


# ── Agents with callbacks ──────────────────────────────────────────────────

# Planner + PolicyJudge (after callback)
planner_with_policy = LlmAgent(
    name="planner",
    model=DEFAULT_MODEL.name,
    instruction=planner_agent.instruction,
    output_key=StateKeys.TEMP_PLANNER_DRAFT,
    after_agent_callback=policy_judge_callback,
    description="Produces a structured plan, then auto-evaluates via policy_judge_callback.",
)

# Verifier + Repair + Curator (chained after callbacks)
verifier_with_hooks = LlmAgent(
    name="verifier",
    model=DEFAULT_MODEL.name,
    instruction=verifier_agent.instruction,
    output_key=StateKeys.VERIFY_LAST_REPORT,
    after_agent_callback=_chain_after_callbacks(repair_callback, curator_callback),
    description=(
        "Evaluates execution results, then triggers repair or memory curation "
        "via chained after_agent_callbacks."
    ),
)

# Executor (with guarded tools, no callbacks)
executor_with_tools = LlmAgent(
    name="executor",
    model=DEFAULT_MODEL.name,
    instruction=executor_agent.instruction,
    tools=[
        guarded_web_search,
        guarded_read_file,
        guarded_write_file,
        guarded_memory_read,
        guarded_current_tab_info,
        guarded_current_tab_navigate,
        guarded_current_tab_extract_text,
        guarded_current_tab_click,
        guarded_current_tab_fill,
        guarded_browser_navigate,
        guarded_browser_extract_text,
        guarded_browser_click,
        guarded_browser_fill,
        guarded_browser_press,
        guarded_desktop_view_windows,
        guarded_desktop_wait_window,
        guarded_desktop_view_frontmost_app,
        guarded_desktop_view_screenshot,
        guarded_desktop_ax_find,
        guarded_desktop_wait_element,
        guarded_desktop_ax_snapshot,
        guarded_desktop_control_click,
        guarded_desktop_control_type,
        guarded_desktop_control_launch_app,
        guarded_desktop_control_focus_window,
        guarded_desktop_control_hotkey,
        guarded_desktop_control_scroll,
        guarded_desktop_control_drag,
    ],
    output_key=StateKeys.TEMP_EXECUTOR_OUTPUTS,
    description="Executes the approved plan using policy-gated tools.",
)


# ── Execution result ───────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    """制御ループの最終実行結果。"""

    request_id: str
    session_id: str
    user_id: str
    final_text: str
    plan_id: str | None = None
    verification_report_id: str | None = None
    promoted_memory_ids: list[str] = field(default_factory=list)
    success: bool = False
    repair_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Control Loop ───────────────────────────────────────────────────────────

class ControlLoop:
    """
    ADK Runner を使って Planner → Executor → Verifier のループを実行する。

    session_service: 外部から注入可能（デフォルトは configured session service）。
    """

    def __init__(
        self,
        session_service=None,
        memory_service=None,
        max_repair_attempts: int = _MAX_REPAIR_ATTEMPTS,
    ) -> None:
        self._session_service = session_service or create_session_service()
        if memory_service is None:
            from src.memory_lifecycle.adk_memory_service import (
                get_promoted_memory_service,
            )

            memory_service = get_promoted_memory_service()
        self._memory_service = memory_service
        self._max_repair = max_repair_attempts

    async def run(
        self,
        goal: str,
        user_id: str,
        *,
        constraints: list[str] | None = None,
        session_id: str | None = None,
        initial_state: dict[str, Any] | None = None,
        reset_if_terminal: bool = False,
    ) -> ExecutionResult:
        """
        制御ループを実行して ExecutionResult を返す。

        1. session 作成（または再利用）
        2. task:goal 等を initial state に設定
        3. repair 上限まで Planner → Executor → Verifier を反復
        4. ExecutionResult を返す
        """
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        session_id = session_id or f"sess_{uuid.uuid4().hex[:12]}"

        init_state: dict[str, Any] = {
            StateKeys.TASK_GOAL: goal,
            StateKeys.TASK_CONSTRAINTS: constraints or [],
            StateKeys.REPAIR_COUNT: 0,
            **(initial_state or {}),
        }
        session, created = await self._get_or_create_session(
            user_id=user_id,
            session_id=session_id,
            goal=goal,
            init_state=init_state,
            reset_if_terminal=reset_if_terminal,
        )
        session_id = session.id

        result = ExecutionResult(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            final_text="",
        )
        result.metadata["session_created"] = created

        for attempt in range(self._max_repair + 1):
            logger.info(
                "ControlLoop: attempt=%d, session=%s", attempt, session_id
            )

            state = await self._get_state(user_id, session_id)
            approval = state.get(StateKeys.APPROVAL_STATUS, "")
            has_approved_plan = bool(state.get(StateKeys.PLAN_APPROVED))
            replay_context = _parse_replay_context(state.get(StateKeys.REPLAY_CONTEXT))
            approved_plan = _parse_json(state.get(StateKeys.PLAN_APPROVED)) or {}
            repair_patch = _parse_json(state.get(StateKeys.TEMP_REPAIR_PATCH)) or {}
            resume_existing_plan = _should_resume_existing_plan(
                attempt=attempt,
                has_approved_plan=has_approved_plan,
                approval=approval,
                replay_context=replay_context,
                repair_patch=repair_patch,
            )

            if not resume_existing_plan:
                # ── Step 1: Planner + PolicyJudge callback ─────────────────
                plan_message = (
                    goal
                    if attempt == 0
                    else f"[repair attempt {attempt}] {goal}"
                )
                await self._run_agent(
                    planner_with_policy,
                    session_id=session_id,
                    user_id=user_id,
                    message=plan_message,
                )

                # approval:status を確認
                state = await self._get_state(user_id, session_id)
                approval = state.get(StateKeys.APPROVAL_STATUS, "")
                approved_plan = _parse_json(state.get(StateKeys.PLAN_APPROVED)) or {}
            else:
                logger.info(
                    "ControlLoop: resuming approved plan for session=%s", session_id
                )

            if approval == "denied":
                result.final_text = "Plan was denied by policy judge."
                result.success = False
                result.plan_id = _extract_plan_id(state, approved_plan)
                if approved_plan:
                    result.metadata["approved_plan"] = approved_plan
                break

            if approval == "needs_human":
                result.final_text = (
                    "Plan requires human approval. "
                    "Please review plan:approved in session state."
                )
                result.success = False
                result.plan_id = _extract_plan_id(state, approved_plan)
                result.metadata["needs_human"] = True
                result.metadata["approval_request"] = state.get(
                    StateKeys.APPROVAL_REQUEST
                )
                if approved_plan:
                    result.metadata["approved_plan"] = approved_plan
                break

            # ── Step 2: Executor ───────────────────────────────────────────
            executor_message = _build_executor_message(
                approved_plan=approved_plan,
                replay_context=replay_context,
            )
            await self._run_agent(
                executor_with_tools,
                session_id=session_id,
                user_id=user_id,
                message=executor_message,
            )
            verification_inputs = await self._prepare_verification_state(
                user_id=user_id,
                session_id=session_id,
            )

            # ── Step 3: Verifier + Repair/Curator callbacks ────────────────
            verification_message = "Verify execution results."
            if verification_inputs:
                verification_message = (
                    "Verify execution results.\n\n"
                    "Structured verification inputs:\n"
                    f"{json.dumps(verification_inputs, ensure_ascii=False, indent=2)}"
                )
            # Attach screenshots so the verifier can visually inspect results.
            screenshot_paths: list[str] = []
            if verification_inputs:
                desktop_vi = verification_inputs.get("desktop") or {}
                raw_paths = desktop_vi.get("screenshot_paths") or []
                screenshot_paths = [
                    path
                    for path in raw_paths
                    if isinstance(path, str) and Path(path).is_file()
                ]
            await self._run_agent(
                verifier_with_hooks,
                session_id=session_id,
                user_id=user_id,
                message=verification_message,
                image_paths=screenshot_paths or None,
            )

            # verify:last_report を確認
            state = await self._get_state(user_id, session_id)
            raw_report = state.get(StateKeys.VERIFY_LAST_REPORT)
            report = _parse_json(raw_report) or {}
            promoted_report = await self._maybe_promote_visual_playback_report(
                user_id=user_id,
                session_id=session_id,
                state=state,
                report=report,
            )
            if promoted_report is not None:
                report = promoted_report
                state = await self._get_state(user_id, session_id)
            if _should_demote_browser_text_entry_report(
                report=report,
                verification_inputs=verification_inputs or {},
            ):
                report = _demote_browser_text_entry_report(
                    report=report,
                    verification_inputs=verification_inputs or {},
                )
            elif _should_retarget_browser_text_entry_repair(
                report=report,
                verification_inputs=verification_inputs or {},
            ):
                report = _retarget_browser_text_entry_repair(
                    report=report,
                    verification_inputs=verification_inputs or {},
                )
            verify_status = report.get("status", "error")

            result.repair_count = state.get(StateKeys.REPAIR_COUNT, 0)
            approved_plan = _parse_json(state.get(StateKeys.PLAN_APPROVED)) or {}
            result.plan_id = _extract_plan_id(state, approved_plan)
            result.verification_report_id = report.get("report_id")
            if approved_plan:
                result.metadata["approved_plan"] = approved_plan
            result.metadata["verification_status"] = verify_status
            result.metadata["verification_report"] = report
            executor_outputs = _parse_json(state.get(StateKeys.TEMP_EXECUTOR_OUTPUTS)) or {}
            if executor_outputs:
                result.metadata["executor_outputs"] = executor_outputs
            if verification_inputs:
                result.metadata["verification_inputs"] = verification_inputs
                artifact_refs = verification_inputs.get("artifact_refs")
                if isinstance(artifact_refs, list):
                    result.metadata["artifact_refs"] = [
                        str(ref) for ref in artifact_refs if str(ref).strip()
                    ]
                output_location = _output_location_from_verification_inputs(
                    verification_inputs
                )
                if output_location:
                    result.metadata["output_location"] = output_location
                current_tab_inputs = verification_inputs.get("current_tab")
                if isinstance(current_tab_inputs, dict):
                    result.metadata["current_tab"] = current_tab_inputs
            candidate_ids = state.get(StateKeys.MEMORY_LAST_CANDIDATE_IDS, [])
            if candidate_ids:
                result.metadata["memory_candidate_ids"] = candidate_ids
            step_trace = _build_step_trace(
                plan=approved_plan,
                executor_outputs=executor_outputs,
                report=report,
                replay_context=replay_context,
            )
            if step_trace:
                result.metadata["step_trace"] = step_trace
                tail_replay_from_step = _infer_tail_replay_from_step(
                    step_trace=step_trace,
                    report=report,
                )
                if tail_replay_from_step:
                    result.metadata["tail_replay_from_step_id"] = tail_replay_from_step

            normalized_state_delta: dict[str, Any] = {}
            if report != (_parse_json(raw_report) or {}):
                normalized_state_delta[StateKeys.VERIFY_LAST_REPORT] = report
                normalized_state_delta[StateKeys.TEMP_REPAIR_PATCH] = _build_repair_patch_from_report(
                    report=report,
                    state=state,
                )
            tail_replay_from_step = str(
                result.metadata.get("tail_replay_from_step_id") or ""
            ).strip()
            if verify_status != "pass" and tail_replay_from_step and step_trace:
                normalized_state_delta[StateKeys.REPLAY_SOURCE_TASK_ID] = request_id
                normalized_state_delta[StateKeys.REPLAY_FROM_STEP] = tail_replay_from_step
                normalized_state_delta[StateKeys.REPLAY_CONTEXT] = _build_replay_context_payload(
                    source_task_id=request_id,
                    from_step=tail_replay_from_step,
                    report=report,
                    step_trace=step_trace,
                )
            elif verify_status == "pass":
                normalized_state_delta[StateKeys.REPLAY_SOURCE_TASK_ID] = None
                normalized_state_delta[StateKeys.REPLAY_FROM_STEP] = None
                normalized_state_delta[StateKeys.REPLAY_CONTEXT] = None

            if normalized_state_delta:
                session = await self._get_session(user_id, session_id)
                if session is not None:
                    await self._append_state_delta(
                        session=session,
                        author=_CONTROL_LOOP_AUTHOR,
                        invocation_prefix="repair_normalization",
                        state_delta=normalized_state_delta,
                    )
                    state = await self._get_state(user_id, session_id)

            if verify_status == "pass":
                result.promoted_memory_ids = await self._promote_memories(
                    user_id=user_id,
                    session_id=session_id,
                )
                result.success = True
                result.final_text = _build_final_text(state, report)
                break

            # fail / partial_pass → repair_callback が repair:count を更新済み
            repair_patch = state.get(StateKeys.TEMP_REPAIR_PATCH)
            if not repair_patch or result.repair_count >= self._max_repair:
                result.success = False
                result.final_text = (
                    f"Verification failed after {result.repair_count} repair attempt(s). "
                    f"Status: {verify_status}."
                )
                break

            logger.info(
                "ControlLoop: repair triggered (attempt=%d)", result.repair_count
            )

        return result

    async def _run_agent(
        self,
        agent: LlmAgent,
        *,
        session_id: str,
        user_id: str,
        message: str,
        image_paths: list[str] | None = None,
    ) -> None:
        """指定 agent を Runner 経由で一度実行する。
        image_paths が指定された場合、PNG 画像を inline_data Part として添付する。
        """
        runner = Runner(
            agent=agent,
            app_name=_APP_NAME,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )
        parts: list[Part] = [Part(text=message)]
        for image_path in image_paths or []:
            try:
                image_bytes = Path(image_path).read_bytes()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not attach screenshot %s: %s", image_path, exc)
                continue
            parts.append(
                Part(
                    inline_data={
                        "mime_type": "image/png",
                        "data": image_bytes,
                    }
                )
            )
        user_content = Content(role="user", parts=parts)
        async for _event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=user_content,
        ):
            pass  # 結果は session.state の output_key 経由で取得

    async def resolve_human_approval(
        self,
        *,
        user_id: str,
        session_id: str,
        approved: bool,
        request_id: str | None = None,
    ) -> bool:
        """Record a human approval decision via ADK state_delta."""
        session = await self._get_session(user_id, session_id)
        if session is None or not session.state.get(StateKeys.PLAN_APPROVED):
            return False
        pending_request = session.state.get(StateKeys.APPROVAL_REQUEST) or {}
        if request_id and pending_request.get("request_id") != request_id:
            return False

        await self._append_state_delta(
            session=session,
            author=_CONTROL_LOOP_AUTHOR,
            invocation_prefix="approval",
            state_delta={
                StateKeys.APPROVAL_STATUS: (
                    "human_approved" if approved else "denied"
                ),
                StateKeys.APPROVAL_REQUEST: None,
            },
        )
        return True

    async def get_pending_approval(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        session = await self._get_session(user_id, session_id)
        if session is None:
            return None
        approval = session.state.get(StateKeys.APPROVAL_STATUS)
        request = session.state.get(StateKeys.APPROVAL_REQUEST)
        if approval != "needs_human" or not isinstance(request, dict):
            return None
        return request

    async def get_task_goal(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> str | None:
        session = await self._get_session(user_id, session_id)
        if session is None:
            return None
        goal = session.state.get(StateKeys.TASK_GOAL)
        return str(goal).strip() if goal else None

    async def _get_or_create_session(
        self,
        *,
        user_id: str,
        session_id: str,
        goal: str,
        init_state: dict[str, Any],
        reset_if_terminal: bool,
    ) -> tuple[Session, bool]:
        session = await self._get_session(user_id, session_id)
        if session is None:
            session = await self._session_service.create_session(
                app_name=_APP_NAME,
                user_id=user_id,
                session_id=session_id,
                state=init_state,
            )
            return session, True

        current_goal = session.state.get(StateKeys.TASK_GOAL)
        if current_goal and current_goal != goal and not _workflow_is_terminal(session.state):
            raise ValueError(
                "Session already has a different task goal. "
                "Use a new session_id for a new workflow."
            )
        if _workflow_is_terminal(session.state) and (reset_if_terminal or (current_goal and current_goal != goal)):
            await self._append_state_delta(
                session=session,
                author=_CONTROL_LOOP_AUTHOR,
                invocation_prefix="reset",
                state_delta=_build_next_goal_state(init_state),
            )
            session = await self._get_session(user_id, session_id)
            assert session is not None
            return session, False

        missing_state = {
            key: value
            for key, value in init_state.items()
            if key not in session.state
        }
        if missing_state:
            await self._append_state_delta(
                session=session,
                author=_CONTROL_LOOP_AUTHOR,
                invocation_prefix="bootstrap",
                state_delta=missing_state,
            )
            session = await self._get_session(user_id, session_id)
            assert session is not None

        return session, False

    async def _append_state_delta(
        self,
        *,
        session: Session,
        author: str,
        invocation_prefix: str,
        state_delta: dict[str, Any],
    ) -> None:
        """Persist state updates through ADK session events."""
        event = Event(
            invocation_id=f"{invocation_prefix}:{uuid.uuid4().hex[:12]}",
            author=author,
            actions=EventActions(state_delta=state_delta),
        )
        await self._session_service.append_event(session, event)

    async def _prepare_verification_state(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        session = await self._get_session(user_id, session_id)
        if session is None:
            return None

        state = session.state if isinstance(session.state, dict) else {}
        executor_outputs = _parse_json(state.get(StateKeys.TEMP_EXECUTOR_OUTPUTS))
        executor_invocation_id: str | None = None
        if executor_outputs is None:
            executor_outputs, executor_invocation_id = _extract_latest_agent_json_output(
                session.events,
                "executor",
            )
        else:
            executor_invocation_id = _latest_agent_invocation_id(
                session.events,
                "executor",
            )

        tool_responses = _collect_agent_function_responses(
            session.events,
            agent_name="executor",
            invocation_id=executor_invocation_id,
        )
        plan = _parse_json(state.get(StateKeys.PLAN_APPROVED)) or {}
        goal = str(state.get(StateKeys.TASK_GOAL) or "")
        verification_inputs = _build_verification_inputs(
            plan=plan,
            goal=goal,
            executor_outputs=executor_outputs,
            tool_responses=tool_responses,
        )
        opened_tab_ids = _parse_opened_tab_ids(
            state.get(StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS)
        )
        if _verification_needs_current_tab_backfill(verification_inputs):
            verification_inputs = await _backfill_current_tab_verification_inputs(
                verification_inputs,
                opened_tab_ids=opened_tab_ids,
            )

        state_delta: dict[str, Any] = {}
        if executor_outputs is not None:
            state_delta[StateKeys.TEMP_EXECUTOR_OUTPUTS] = executor_outputs
        if verification_inputs:
            state_delta[StateKeys.TEMP_VERIFICATION_INPUTS] = verification_inputs
            artifact_refs = verification_inputs.get("artifact_refs")
            if artifact_refs:
                state_delta[StateKeys.TEMP_ARTIFACT_REFS] = artifact_refs
        if not state_delta:
            return verification_inputs or None

        await self._append_state_delta(
            session=session,
            author=_CONTROL_LOOP_AUTHOR,
            invocation_prefix="verification_prep",
            state_delta=state_delta,
        )
        return verification_inputs or None

    async def _maybe_promote_visual_playback_report(
        self,
        *,
        user_id: str,
        session_id: str,
        state: dict[str, Any],
        report: dict[str, Any],
    ) -> dict[str, Any] | None:
        verification_inputs = _parse_json(
            state.get(StateKeys.TEMP_VERIFICATION_INPUTS)
        ) or {}
        plan = _parse_json(state.get(StateKeys.PLAN_APPROVED)) or {}
        goal = str(state.get(StateKeys.TASK_GOAL) or "")
        if not _should_promote_visual_playback_report(
            plan=plan,
            goal=goal,
            report=report,
            verification_inputs=verification_inputs,
        ):
            return None

        promoted_report = _promote_visual_playback_report(
            report=report,
            verification_inputs=verification_inputs,
        )
        session = await self._get_session(user_id, session_id)
        if session is None:
            return promoted_report
        await self._append_state_delta(
            session=session,
            author=_CONTROL_LOOP_AUTHOR,
            invocation_prefix="verification_override",
            state_delta={
                StateKeys.VERIFY_LAST_REPORT: promoted_report,
                StateKeys.REPAIR_COUNT: 0,
                StateKeys.TEMP_REPAIR_PATCH: None,
            },
        )
        return promoted_report

    async def _promote_memories(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> list[str]:
        """Curate session candidates and sync promoted memories to ADK memory."""
        from src.memory_lifecycle.candidate_store import get_candidate_store
        from src.memory_lifecycle.curator import Curator
        from src.memory_lifecycle.promoted_store import get_promoted_store

        store = get_candidate_store()
        existing_promoted = get_promoted_store().list_memories(
            app_name=_APP_NAME,
            user_id=user_id,
        )
        curation = await Curator(
            store,
            existing_promoted=existing_promoted,
        ).curate_session(
            session_id=session_id,
            user_id=user_id,
        )
        promoted_ids = curation.promoted_ids
        if not curation.persisted_memories:
            return []

        if hasattr(self._memory_service, "store_promoted_memories"):
            await self._memory_service.store_promoted_memories(
                app_name=_APP_NAME,
                memories=curation.persisted_memories,
            )

        session = await self._get_session(user_id, session_id)
        if session is not None:
            await self._append_state_delta(
                session=session,
                author=_CONTROL_LOOP_AUTHOR,
                invocation_prefix="memory_promotion",
                state_delta={StateKeys.MEMORY_LAST_PROMOTED_IDS: promoted_ids},
            )

        return promoted_ids

    async def _get_session(
        self, user_id: str, session_id: str
    ) -> Session | None:
        return await self._session_service.get_session(
            app_name=_APP_NAME,
            user_id=user_id,
            session_id=session_id,
        )

    async def _get_state(self, user_id: str, session_id: str) -> dict[str, Any]:
        """session state を dict として返す。"""
        session = await self._get_session(user_id, session_id)
        return session.state if session and session.state else {}


# ── Helpers ────────────────────────────────────────────────────────────────

def _parse_json(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _latest_agent_invocation_id(events: list[Event], agent_name: str) -> str | None:
    for event in reversed(events or []):
        if getattr(event, "author", None) != agent_name:
            continue
        invocation_id = getattr(event, "invocation_id", None)
        if invocation_id:
            return str(invocation_id)
    return None


def _extract_latest_agent_json_output(
    events: list[Event],
    agent_name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    for event in reversed(events or []):
        if getattr(event, "author", None) != agent_name:
            continue
        content = getattr(event, "content", None)
        if content is None:
            continue
        parts = list(getattr(content, "parts", None) or [])
        for part in reversed(parts):
            text = getattr(part, "text", None)
            if not isinstance(text, str) or "{" not in text:
                continue
            parsed = _parse_json(text)
            if parsed is not None:
                return parsed, str(getattr(event, "invocation_id", "") or "")
    return None, None


def _collect_agent_function_responses(
    events: list[Event],
    *,
    agent_name: str,
    invocation_id: str | None,
) -> list[dict[str, Any]]:
    if not invocation_id:
        return []
    responses: list[dict[str, Any]] = []
    for event in events or []:
        if (
            getattr(event, "author", None) != agent_name
            or str(getattr(event, "invocation_id", "") or "") != invocation_id
        ):
            continue
        content = getattr(event, "content", None)
        if content is None:
            continue
        for part in getattr(content, "parts", None) or []:
            function_response = getattr(part, "function_response", None)
            if function_response is None:
                continue
            responses.append(
                {
                    "name": str(getattr(function_response, "name", "") or ""),
                    "response": getattr(function_response, "response", None),
                }
            )
    return responses


def _count_ax_nodes(node: Any) -> int:
    if not isinstance(node, dict):
        return 0
    children = node.get("children", [])
    count = 1
    if isinstance(children, list):
        for child in children:
            count += _count_ax_nodes(child)
    return count


def _png_chunk_iter(data: bytes):
    if data[:8] != _PNG_SIGNATURE:
        raise ValueError("unsupported PNG signature")
    pos = 8
    while pos < len(data):
        if pos + 8 > len(data):
            raise ValueError("truncated PNG chunk header")
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        pos += 4
        chunk_type = data[pos : pos + 4]
        pos += 4
        if pos + length + 4 > len(data):
            raise ValueError("truncated PNG chunk body")
        chunk = data[pos : pos + length]
        pos += length + 4  # skip crc
        yield chunk_type, chunk
        if chunk_type == b"IEND":
            break


def _decode_png_image(path: str) -> tuple[int, int, int, bytes] | None:
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = Path.cwd() / file_path
    if not file_path.exists():
        return None
    data = file_path.read_bytes()
    width = height = bit_depth = color_type = interlace = None
    idat = bytearray()
    for chunk_type, chunk in _png_chunk_iter(data):
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _comp, _flt, interlace = struct.unpack(
                ">IIBBBBB",
                chunk,
            )
        elif chunk_type == b"IDAT":
            idat.extend(chunk)
        elif chunk_type == b"IEND":
            break
    if (
        width is None
        or height is None
        or bit_depth != 8
        or color_type not in {2, 6}
        or interlace != 0
    ):
        return None
    channels = 4 if color_type == 6 else 3
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    decoded = bytearray(height * stride)
    read_offset = 0
    previous_row = bytearray(stride)

    def paeth(a: int, b: int, c: int) -> int:
        candidate = a + b - c
        dist_a = abs(candidate - a)
        dist_b = abs(candidate - b)
        dist_c = abs(candidate - c)
        if dist_a <= dist_b and dist_a <= dist_c:
            return a
        if dist_b <= dist_c:
            return b
        return c

    for row_index in range(height):
        filter_type = raw[read_offset]
        read_offset += 1
        row = bytearray(raw[read_offset : read_offset + stride])
        read_offset += stride
        if filter_type == 1:
            for idx in range(stride):
                left = row[idx - channels] if idx >= channels else 0
                row[idx] = (row[idx] + left) & 0xFF
        elif filter_type == 2:
            for idx in range(stride):
                row[idx] = (row[idx] + previous_row[idx]) & 0xFF
        elif filter_type == 3:
            for idx in range(stride):
                left = row[idx - channels] if idx >= channels else 0
                up = previous_row[idx]
                row[idx] = (row[idx] + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:
            for idx in range(stride):
                left = row[idx - channels] if idx >= channels else 0
                up = previous_row[idx]
                up_left = previous_row[idx - channels] if idx >= channels else 0
                row[idx] = (row[idx] + paeth(left, up, up_left)) & 0xFF
        elif filter_type != 0:
            return None
        start = row_index * stride
        decoded[start : start + stride] = row
        previous_row = row
    return width, height, channels, bytes(decoded)


def _compute_png_visual_change(
    before_path: str,
    after_path: str,
) -> dict[str, Any] | None:
    before = _decode_png_image(before_path)
    after = _decode_png_image(after_path)
    if before is None or after is None:
        return None
    before_width, before_height, before_channels, before_pixels = before
    after_width, after_height, after_channels, after_pixels = after
    if (
        before_width != after_width
        or before_height != after_height
        or before_channels != after_channels
    ):
        return None
    channels = min(before_channels, 3)
    total_pixels = before_width * before_height
    changed_pixels = 0
    rgb_delta_total = 0
    for idx in range(0, len(before_pixels), before_channels):
        delta = 0
        for channel in range(channels):
            delta += abs(before_pixels[idx + channel] - after_pixels[idx + channel])
        rgb_delta_total += delta
        if delta:
            changed_pixels += 1
    changed_ratio = changed_pixels / total_pixels if total_pixels else 0.0
    normalized_rgb_delta = (
        rgb_delta_total / (total_pixels * 255 * channels)
        if total_pixels
        else 0.0
    )
    return {
        "before_path": before_path,
        "after_path": after_path,
        "pixels": total_pixels,
        "changed_pixels": changed_pixels,
        "changed_ratio": changed_ratio,
        "normalized_rgb_delta": normalized_rgb_delta,
        "playback_ui_changed": (
            changed_ratio >= _PLAYBACK_SCREENSHOT_DIFF_RATIO_THRESHOLD
            and normalized_rgb_delta >= _PLAYBACK_SCREENSHOT_DELTA_THRESHOLD
        ),
    }


def _verification_needs_current_tab_backfill(
    verification_inputs: dict[str, Any],
) -> bool:
    if not verification_inputs.get("current_browser_goal"):
        return False
    current_tab = verification_inputs.get("current_tab")
    if not isinstance(current_tab, dict):
        return True
    has_location = bool(
        str(current_tab.get("url") or "").strip()
        and str(current_tab.get("title") or "").strip()
    )
    has_text = bool(
        current_tab.get("extract_text_succeeded")
        and int(current_tab.get("text_length") or 0) > 0
    )
    if not current_tab.get("info_succeeded") or not has_location:
        return True
    if verification_inputs.get("text_entry_goal") and not has_text:
        return True
    if _verification_needs_destination_tab_backfill(verification_inputs):
        return True
    return False


def _verification_needs_destination_tab_backfill(
    verification_inputs: dict[str, Any],
) -> bool:
    """True when the task is destination-bound (spreadsheet today) and
    verification_inputs do not yet carry a destination_tab entry.

    The destination_tab is a read-only side-channel capture: we discover a
    candidate tab (e.g. the Google Sheets tab the task opened earlier), then
    call extract_text with target_tab_id to read it without stealing focus
    from whatever tab the user is currently viewing (typically Control UI).
    """
    goal = verification_inputs.get("goal")
    if not _is_spreadsheet_task(goal):
        return False
    destination_tab = verification_inputs.get("destination_tab")
    if not isinstance(destination_tab, dict):
        return True
    # Already captured — allow backfill only if location is missing.
    return not bool(str(destination_tab.get("url") or "").strip())


def _select_destination_tab_candidate(
    tabs: list[dict[str, Any]],
    *,
    goal: Any,
    opened_tab_ids: set[int],
) -> dict[str, Any] | None:
    """Pick the best destination_tab candidate from a tab listing.

    Preference order (spreadsheet task):
      1. A tab in opened_tab_ids whose URL/title indicates Google Sheets.
      2. Any tab whose URL/title indicates Google Sheets.
      3. A tab in opened_tab_ids (last resort — caller should treat as non-
         destination-bound and fail the target_context_mismatch rule).

    Returns the selected raw tab dict or None when nothing matches.
    """
    spreadsheet = _is_spreadsheet_task(goal)

    def _looks_like_sheets(tab: dict[str, Any]) -> bool:
        return _is_google_sheets_destination(
            {"url": tab.get("url"), "title": tab.get("title")}
        )

    if spreadsheet:
        in_opened_and_sheets = [
            t
            for t in tabs
            if isinstance(t, dict)
            and _optional_int_from_tab(t) in opened_tab_ids
            and _looks_like_sheets(t)
        ]
        if in_opened_and_sheets:
            return in_opened_and_sheets[0]
        any_sheets = [
            t for t in tabs if isinstance(t, dict) and _looks_like_sheets(t)
        ]
        if any_sheets:
            return any_sheets[0]

    in_opened = [
        t
        for t in tabs
        if isinstance(t, dict) and _optional_int_from_tab(t) in opened_tab_ids
    ]
    if in_opened:
        return in_opened[0]
    return None


def _optional_int_from_tab(tab: dict[str, Any]) -> int | None:
    try:
        return int(tab.get("tab_id"))
    except (TypeError, ValueError):
        return None


def _parse_opened_tab_ids(raw: Any) -> set[int]:
    if not isinstance(raw, (list, tuple, set)):
        return set()
    out: set[int] = set()
    for entry in raw:
        try:
            out.add(int(entry))
        except (TypeError, ValueError):
            continue
    return out


async def _backfill_current_tab_verification_inputs(
    verification_inputs: dict[str, Any],
    *,
    opened_tab_ids: set[int] | None = None,
) -> dict[str, Any]:
    from src.tools.current_tab import current_tab_extract_text, current_tab_info

    current_tab = verification_inputs.setdefault("current_tab", {})
    try:
        info_result = await current_tab_info()
    except Exception:
        logger.exception("verification backfill: current_tab.info failed")
    else:
        if isinstance(info_result, dict):
            if _response_succeeded(info_result):
                current_tab["info_succeeded"] = True
            url = str(info_result.get("url") or "").strip()
            title = str(info_result.get("title") or "").strip()
            if url:
                current_tab["url"] = url
            if title:
                current_tab["title"] = title
            if info_result.get("tab_id") is not None:
                current_tab["tab_id"] = info_result.get("tab_id")
            if info_result.get("window_id") is not None:
                current_tab["window_id"] = info_result.get("window_id")
            current_tab["destination_bound"] = _is_destination_bound_for_spreadsheet(
                current_tab
            )

    if verification_inputs.get("text_entry_goal"):
        try:
            text_result = await current_tab_extract_text()
        except Exception:
            logger.exception("verification backfill: current_tab.extract_text failed")
        else:
            if isinstance(text_result, dict):
                text = str(text_result.get("text") or "")
                if _response_succeeded(text_result):
                    current_tab["extract_text_succeeded"] = True
                if text:
                    current_tab["text_excerpt"] = text[:500]
                current_tab["text_length"] = max(
                    int(current_tab.get("text_length") or 0),
                    int(text_result.get("length") or len(text) or 0),
                )

    # ── destination_tab capture (read-only, no activate) ────────────────────
    goal = verification_inputs.get("goal")
    if _is_spreadsheet_task(goal):
        await _capture_destination_tab(
            verification_inputs,
            goal=goal,
            opened_tab_ids=opened_tab_ids or set(),
        )

    return verification_inputs


async def _capture_destination_tab(
    verification_inputs: dict[str, Any],
    *,
    goal: Any,
    opened_tab_ids: set[int],
) -> None:
    """Read-only destination_tab capture via list_tabs + targeted extract_text.

    Does NOT call current_tab.activate — focus stays on whatever tab the user
    is currently viewing (typically the Control UI). We enumerate tabs,
    pick the Sheets candidate, and read its text via extract_text with
    target_tab_id. If current_tab already points to the destination, we reuse
    it rather than issuing a redundant capture.
    """
    from src.tools.current_tab import current_tab_list_tabs

    current_tab = verification_inputs.get("current_tab") or {}
    if _is_destination_bound_for_spreadsheet(current_tab):
        # The focused tab already IS the destination — mirror it into
        # destination_tab so downstream verifier rules have a uniform handle.
        destination_tab = dict(current_tab)
        destination_tab["destination_bound"] = True
        destination_tab["discovery"] = "current_tab_is_destination"
        verification_inputs["destination_tab"] = destination_tab
        return

    tabs: list[dict[str, Any]] = []
    try:
        list_result = await current_tab_list_tabs()
    except Exception:
        logger.exception("verification backfill: current_tab.list_tabs failed")
        list_result = None
    if isinstance(list_result, dict) and _response_succeeded(list_result):
        raw_tabs = list_result.get("tabs") or []
        tabs = [t for t in raw_tabs if isinstance(t, dict)]

    if not tabs:
        return

    candidate = _select_destination_tab_candidate(
        tabs, goal=goal, opened_tab_ids=opened_tab_ids
    )
    if candidate is None:
        return

    destination_tab: dict[str, Any] = {
        "url": str(candidate.get("url") or "").strip(),
        "title": str(candidate.get("title") or "").strip(),
        "tab_id": _optional_int_from_tab(candidate),
        "window_id": candidate.get("window_id"),
    }
    destination_tab["destination_bound"] = _is_destination_bound_for_spreadsheet(
        destination_tab
    )
    destination_tab["discovery"] = (
        "opened_tab_ids"
        if destination_tab["tab_id"] in opened_tab_ids
        else "window_titles"
    )

    target_tab_id = destination_tab["tab_id"]
    if target_tab_id is not None:
        # Read the destination tab's text WITHOUT activating it. We bypass
        # current_tab_extract_text() (which derives target_tab_id from
        # tool_context / TEMP_CURRENT_BROWSER_ACTIVE_TAB_ID and would read the
        # focused tab instead) and call the host-bridge client directly with
        # an explicit target_tab_id.
        text_result = await _extract_text_for_tab(target_tab_id)
        if isinstance(text_result, dict):
            text = str(text_result.get("text") or "")
            if _response_succeeded(text_result):
                destination_tab["extract_text_succeeded"] = True
            if text:
                destination_tab["text_excerpt"] = text[:500]
            destination_tab["text_length"] = int(
                text_result.get("length") or len(text) or 0
            )

    verification_inputs["destination_tab"] = destination_tab


async def _extract_text_for_tab(tab_id: int) -> dict[str, Any] | None:
    """Call host.current_tab.extract_text against a specific tab_id without
    touching tool_context / session state. Read-only, no side effects on
    focus."""
    import uuid as _uuid

    from src.bridges.host_bridge_client import get_host_bridge_client
    from src.bridges.host_bridge_schema import HostCurrentTabExtractTextRequest

    client = get_host_bridge_client()
    if client is None:
        return None
    request = HostCurrentTabExtractTextRequest(
        request_id=f"host-current-tab-dest-text-{_uuid.uuid4().hex[:12]}",
        session_id="verification-destination-capture",
        user_id="verification-destination-capture",
        agent_name="control_loop_verifier",
        approval_token=None,
        selector=None,
        target_tab_id=tab_id,
    )
    try:
        result = await client.current_tab_extract_text(request)
    except Exception:
        logger.exception("verification backfill: extract_text for tab_id=%s failed", tab_id)
        return None
    return {
        "text": result.text,
        "length": result.length,
        "success": result.ok,
        **({"error": result.error} if result.error else {}),
    }


def _build_verification_inputs(
    *,
    plan: dict[str, Any],
    goal: str,
    executor_outputs: dict[str, Any] | None,
    tool_responses: list[dict[str, Any]],
) -> dict[str, Any]:
    artifact_refs: list[str] = []
    if isinstance(executor_outputs, dict):
        refs = executor_outputs.get("artifact_refs", [])
        if isinstance(refs, list):
            artifact_refs.extend(str(ref) for ref in refs if str(ref).strip())
        for step in executor_outputs.get("steps_executed", []) or []:
            if not isinstance(step, dict):
                continue
            artifact_ref = str(step.get("artifact_ref") or "").strip()
            if artifact_ref:
                artifact_refs.append(artifact_ref)

    screenshot_paths: list[str] = []
    launch_succeeded = False
    focus_succeeded = False
    hotkey_succeeded = False
    click_succeeded = False
    ax_node_count = 0
    window_titles: list[str] = []
    current_tab_url = ""
    current_tab_title = ""
    current_tab_tab_id: int | None = None
    current_tab_window_id: int | None = None
    current_tab_info_succeeded = False
    current_tab_text = ""
    current_tab_text_length = 0
    current_tab_extract_succeeded = False

    for item in tool_responses:
        name = str(item.get("name") or "")
        response = item.get("response")
        if not isinstance(response, dict):
            continue
        if name == "guarded_desktop_view_screenshot":
            path = str(response.get("path") or "").strip()
            if path:
                screenshot_paths.append(path)
                artifact_refs.append(path)
        elif name == "guarded_desktop_ax_snapshot":
            tree = response.get("tree", {})
            root = tree.get("root") if isinstance(tree, dict) else {}
            ax_node_count = max(ax_node_count, _count_ax_nodes(root))
        elif name == "guarded_desktop_control_launch_app":
            launch_succeeded = launch_succeeded or _response_succeeded(response) or not response.get("error")
        elif name == "guarded_desktop_control_focus_window":
            focus_succeeded = focus_succeeded or _response_succeeded(response)
        elif name == "guarded_desktop_control_hotkey":
            hotkey_succeeded = hotkey_succeeded or _response_succeeded(response)
        elif name == "guarded_desktop_control_click":
            click_succeeded = click_succeeded or _response_succeeded(response)
        elif name == "guarded_desktop_view_windows":
            windows = response.get("windows", [])
            if isinstance(windows, list):
                for window in windows:
                    if not isinstance(window, dict):
                        continue
                    title = str(window.get("title") or "").strip()
                    app_name = str(window.get("app_name") or "").strip()
                    if title or app_name:
                        window_titles.append(f"{app_name}::{title}".strip(":"))
        elif name in _CURRENT_TAB_INFO_RESPONSE_NAMES:
            url = str(response.get("url") or "").strip()
            title = str(response.get("title") or "").strip()
            if url:
                current_tab_url = url
            if title:
                current_tab_title = title
            if response.get("tab_id") is not None:
                current_tab_tab_id = response.get("tab_id")
            if response.get("window_id") is not None:
                current_tab_window_id = response.get("window_id")
            current_tab_info_succeeded = current_tab_info_succeeded or _response_succeeded(
                response
            )
        elif name in _CURRENT_TAB_EXTRACT_TEXT_RESPONSE_NAMES:
            current_tab_text = str(response.get("text") or "")
            current_tab_text_length = max(
                current_tab_text_length,
                int(response.get("length") or len(current_tab_text) or 0),
            )
            current_tab_extract_succeeded = current_tab_extract_succeeded or _response_succeeded(
                response
            )

    visual_change = None
    if len(screenshot_paths) >= 2:
        visual_change = _compute_png_visual_change(
            screenshot_paths[0],
            screenshot_paths[-1],
        )

    unique_artifacts = list(dict.fromkeys(ref for ref in artifact_refs if ref))
    return {
        "goal": goal,
        "playback_goal": _plan_text_has_playback_hint(plan, goal),
        "current_browser_goal": _targets_current_browser_goal(goal),
        "text_entry_goal": _goal_needs_text_entry(goal),
        "executor_outputs_present": executor_outputs is not None,
        "artifact_refs": unique_artifacts,
        "current_tab": {
            "info_succeeded": current_tab_info_succeeded,
            "url": current_tab_url,
            "title": current_tab_title,
            "tab_id": current_tab_tab_id,
            "window_id": current_tab_window_id,
            "extract_text_succeeded": current_tab_extract_succeeded,
            "text_excerpt": current_tab_text[:500],
            "text_length": current_tab_text_length,
        },
        "desktop": {
            "launch_succeeded": launch_succeeded,
            "focus_succeeded": focus_succeeded,
            "hotkey_succeeded": hotkey_succeeded,
            "click_succeeded": click_succeeded,
            "playback_interaction_attempted": hotkey_succeeded or click_succeeded,
            "ax_node_count": ax_node_count,
            "window_titles": window_titles,
            "screenshot_paths": screenshot_paths,
            "visual_change": visual_change,
        },
    }


def _output_location_from_verification_inputs(
    verification_inputs: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(verification_inputs, dict):
        return None
    current_tab = verification_inputs.get("current_tab")
    if not isinstance(current_tab, dict):
        return None
    url = str(current_tab.get("url") or "").strip()
    title = str(current_tab.get("title") or "").strip()
    if not url and not title:
        return None
    payload: dict[str, Any] = {}
    if url:
        payload["url"] = url
    if title:
        payload["title"] = title
    tab_id = current_tab.get("tab_id")
    window_id = current_tab.get("window_id")
    if tab_id is not None:
        payload["tab_id"] = tab_id
    if window_id is not None:
        payload["window_id"] = window_id
    return payload


def _should_promote_visual_playback_report(
    *,
    plan: dict[str, Any],
    goal: str,
    report: dict[str, Any],
    verification_inputs: dict[str, Any],
) -> bool:
    if not report or report.get("status") not in {"fail", "partial_pass"}:
        return False
    if str(report.get("failure_type") or "") != "insufficient_evidence":
        return False
    if not _plan_text_has_playback_hint(plan, goal):
        return False
    desktop_inputs = verification_inputs.get("desktop")
    if not isinstance(desktop_inputs, dict):
        return False
    visual_change = desktop_inputs.get("visual_change")
    if not isinstance(visual_change, dict) or not visual_change.get("playback_ui_changed"):
        return False
    if not desktop_inputs.get("playback_interaction_attempted"):
        return False
    if not (desktop_inputs.get("launch_succeeded") or desktop_inputs.get("focus_succeeded")):
        return False
    return True


def _should_demote_browser_text_entry_report(
    *,
    report: dict[str, Any],
    verification_inputs: dict[str, Any],
) -> bool:
    if not report or report.get("status") != "pass":
        return False
    if not verification_inputs.get("current_browser_goal"):
        return False
    if not verification_inputs.get("text_entry_goal"):
        return False

    current_tab = verification_inputs.get("current_tab")
    desktop = verification_inputs.get("desktop")
    if not isinstance(current_tab, dict) or not isinstance(desktop, dict):
        return True

    goal = verification_inputs.get("goal")
    destination_tab = verification_inputs.get("destination_tab")
    destination_tab = destination_tab if isinstance(destination_tab, dict) else None

    # Generalized target-context-mismatch: any spreadsheet task whose captured evidence
    # isn't destination-bound (no Sheets tab reachable via destination_tab OR current_tab)
    # cannot carry a pass, regardless of what text the focused tab holds.
    if _should_fail_for_target_context_mismatch(
        goal, current_tab, desktop, destination_tab=destination_tab
    ):
        return True

    # When destination_tab IS destination-bound (B2 path), the text-conflict rule
    # for current_tab no longer applies — current_tab is allowed to be Control UI.
    destination_bound = bool(
        destination_tab and _is_destination_bound_for_spreadsheet(destination_tab)
    )
    if not destination_bound and _current_tab_text_conflicts_with_destination(
        current_tab, goal
    ):
        return True

    has_destination = destination_bound or bool(
        str(current_tab.get("url") or "").strip()
        or str(current_tab.get("title") or "").strip()
    )
    artifact_refs = verification_inputs.get("artifact_refs")
    # Pass-evidence sources (OR'd):
    #   - destination_tab meaningful text (B2-read, destination-bound)
    #   - current_tab meaningful text (only counts when current_tab is destination)
    #   - screenshot / visual artifacts (weaker: NOT guaranteed destination-bound, but
    #     retained as a fallback for non-spreadsheet text-entry tasks)
    has_destination_text = _has_destination_bound_text_evidence(verification_inputs)
    has_legacy_text = _current_tab_has_meaningful_text_entry_evidence(current_tab, goal)
    has_visual_evidence = bool(desktop.get("screenshot_paths")) or _artifact_refs_have_visual_evidence(
        artifact_refs
    )
    has_post_action_evidence = (
        has_destination_text or has_legacy_text or has_visual_evidence
    )

    # `hotkey_succeeded` without `focus_succeeded` is not evidence that keystrokes
    # reached the intended target — only that the hotkey call returned ok.
    # For spreadsheet tasks, require real focus OR destination-bound text evidence.
    # (Plain screenshot is NOT accepted here because it isn't guaranteed to have
    # captured the destination tab.)
    focus_succeeded = bool(desktop.get("focus_succeeded"))
    if (
        _is_spreadsheet_task(goal)
        and not focus_succeeded
        and not has_destination_text
    ):
        return True

    # URL-only must never pass: spreadsheet task needs destination-bound text
    # evidence specifically (plain screenshot is an insufficient substitute).
    if _is_spreadsheet_task(goal) and not has_destination_text:
        return True

    return not (has_destination and has_post_action_evidence)


def _demote_browser_text_entry_report(
    *,
    report: dict[str, Any],
    verification_inputs: dict[str, Any],
) -> dict[str, Any]:
    demoted = json.loads(json.dumps(report))
    current_tab = verification_inputs.get("current_tab")
    current_tab = current_tab if isinstance(current_tab, dict) else {}
    desktop = verification_inputs.get("desktop")
    desktop = desktop if isinstance(desktop, dict) else {}
    destination_tab = verification_inputs.get("destination_tab")
    destination_tab = destination_tab if isinstance(destination_tab, dict) else None
    artifact_refs = verification_inputs.get("artifact_refs")
    goal = verification_inputs.get("goal")
    missing_evidence: list[str] = []
    if not str(current_tab.get("url") or "").strip():
        missing_evidence.append("current_tab.url")
    if not str(current_tab.get("title") or "").strip():
        missing_evidence.append("current_tab.title")
    if _should_fail_for_target_context_mismatch(
        goal, current_tab, desktop, destination_tab=destination_tab
    ):
        missing_evidence.append(
            "no destination-bound tab for the spreadsheet task "
            "(destination_tab missing or not Sheets; target_context_mismatch)"
        )
    destination_bound = bool(
        destination_tab and _is_destination_bound_for_spreadsheet(destination_tab)
    )
    if not destination_bound and _current_tab_text_conflicts_with_destination(
        current_tab, goal
    ):
        missing_evidence.append("current_tab.extract_text matches the actual spreadsheet tab")
    has_destination_text = _has_destination_bound_text_evidence(verification_inputs)
    has_meaningful_text = _current_tab_has_meaningful_text_entry_evidence(current_tab, goal)
    has_visual_evidence = bool(desktop.get("screenshot_paths")) or _artifact_refs_have_visual_evidence(
        artifact_refs
    )
    if _is_spreadsheet_task(goal) and not has_destination_text:
        missing_evidence.append(
            "destination_tab (Sheets) extract_text with non-boilerplate cell content"
        )
    elif not has_meaningful_text and _is_google_sheets_destination(current_tab):
        missing_evidence.append("non-boilerplate spreadsheet cell evidence")
    if not (has_destination_text or has_meaningful_text or has_visual_evidence):
        missing_evidence.append("meaningful post-action text/screenshot evidence")
    if (
        _is_spreadsheet_task(goal)
        and not bool(desktop.get("focus_succeeded"))
        and not has_destination_text
    ):
        missing_evidence.append(
            "focus_succeeded=false and no destination-bound text evidence; "
            "hotkey/screenshot without destination proof cannot certify keystrokes reached Sheets"
        )

    explanation = (
        "現在のブラウザでの入力タスクですが、入力先のタブURL/titleや入力後の証拠が不足しているため、"
        "成功判定を維持できません。欠けている evidence: "
        + ", ".join(missing_evidence)
    )

    for criterion in demoted.get("criterion_results", []) or []:
        if not isinstance(criterion, dict):
            continue
        criterion["passed"] = False
        criterion["score"] = min(float(criterion.get("score") or 0.0), 0.35)
        criterion["explanation"] = explanation

    demoted["status"] = "fail"
    demoted["overall_score"] = min(float(demoted.get("overall_score") or 1.0), 0.35)
    demoted["confidence"] = min(float(demoted.get("confidence") or 1.0), 0.45)
    demoted["failure_type"] = "insufficient_evidence"
    demoted["summary"] = explanation
    demoted["repair_actions"] = [
        {
            "action_id": "gather_browser_destination_evidence",
            "action_type": "gather_more_evidence",
            "description": "Capture the current tab URL/title and post-entry evidence before marking the spreadsheet/text-entry task as complete.",
            "target_step_ids": ["capture_current_tab_state"],
            "priority": 1,
        }
    ]
    return demoted


def _should_retarget_browser_text_entry_repair(
    *,
    report: dict[str, Any],
    verification_inputs: dict[str, Any],
) -> bool:
    if not report or report.get("status") not in {"fail", "partial_pass"}:
        return False
    if str(report.get("failure_type") or "") != "insufficient_evidence":
        return False
    if not verification_inputs.get("current_browser_goal"):
        return False
    if not verification_inputs.get("text_entry_goal"):
        return False

    current_tab = verification_inputs.get("current_tab")
    current_tab = current_tab if isinstance(current_tab, dict) else {}
    desktop = verification_inputs.get("desktop")
    desktop = desktop if isinstance(desktop, dict) else {}
    destination_tab = verification_inputs.get("destination_tab")
    destination_tab = destination_tab if isinstance(destination_tab, dict) else None
    artifact_refs = verification_inputs.get("artifact_refs")
    goal = verification_inputs.get("goal")
    if _should_fail_for_target_context_mismatch(
        goal, current_tab, desktop, destination_tab=destination_tab
    ):
        return True
    destination_bound = bool(
        destination_tab and _is_destination_bound_for_spreadsheet(destination_tab)
    )
    if not destination_bound and _current_tab_text_conflicts_with_destination(
        current_tab, goal
    ):
        return True
    has_destination = destination_bound or bool(
        str(current_tab.get("url") or "").strip()
        or str(current_tab.get("title") or "").strip()
    )
    has_destination_text = _has_destination_bound_text_evidence(verification_inputs)
    has_text_evidence = _current_tab_has_meaningful_text_entry_evidence(current_tab, goal)
    has_visual_evidence = bool(desktop.get("screenshot_paths")) or _artifact_refs_have_visual_evidence(
        artifact_refs
    )
    if (
        _is_spreadsheet_task(goal)
        and _has_sparse_google_sheets_destination_text_evidence(verification_inputs)
        and not has_visual_evidence
    ):
        return True
    # URL-only must never allow a pass for spreadsheet tasks: require destination-bound text.
    if _is_spreadsheet_task(goal) and not has_destination_text:
        return True
    return not (
        has_destination
        and (has_destination_text or has_text_evidence or has_visual_evidence)
    )


def _retarget_browser_text_entry_repair(
    *,
    report: dict[str, Any],
    verification_inputs: dict[str, Any],
) -> dict[str, Any]:
    retargeted = json.loads(json.dumps(report))
    current_tab = verification_inputs.get("current_tab")
    current_tab = current_tab if isinstance(current_tab, dict) else {}
    desktop = verification_inputs.get("desktop")
    desktop = desktop if isinstance(desktop, dict) else {}
    destination_tab = verification_inputs.get("destination_tab")
    destination_tab = destination_tab if isinstance(destination_tab, dict) else None
    artifact_refs = verification_inputs.get("artifact_refs")
    goal = verification_inputs.get("goal")

    missing_evidence: list[str] = []
    if not str(current_tab.get("url") or "").strip():
        missing_evidence.append("current_tab.url")
    if not str(current_tab.get("title") or "").strip():
        missing_evidence.append("current_tab.title")
    if _should_fail_for_target_context_mismatch(
        goal, current_tab, desktop, destination_tab=destination_tab
    ):
        missing_evidence.append(
            "no destination-bound tab for the spreadsheet task "
            "(destination_tab missing or not Sheets; target_context_mismatch)"
        )
    destination_bound = bool(
        destination_tab and _is_destination_bound_for_spreadsheet(destination_tab)
    )
    if not destination_bound and _current_tab_text_conflicts_with_destination(
        current_tab, goal
    ):
        missing_evidence.append("current_tab.extract_text matches the actual spreadsheet tab")
    has_destination_text = _has_destination_bound_text_evidence(verification_inputs)
    has_meaningful_text = _current_tab_has_meaningful_text_entry_evidence(current_tab, goal)
    has_visual_evidence = bool(desktop.get("screenshot_paths")) or _artifact_refs_have_visual_evidence(
        artifact_refs
    )
    if (
        _is_spreadsheet_task(goal)
        and _has_sparse_google_sheets_destination_text_evidence(verification_inputs)
        and not has_visual_evidence
    ):
        missing_evidence.append(
            "spreadsheet screenshot with multiple populated cells; current_tab.extract_text only captured sparse active-cell text"
        )
    if _is_spreadsheet_task(goal) and not has_destination_text:
        missing_evidence.append(
            "destination_tab (Sheets) extract_text with non-boilerplate cell content"
        )
    elif not has_meaningful_text and _is_google_sheets_destination(current_tab):
        missing_evidence.append("non-boilerplate spreadsheet cell evidence")
    if not (has_destination_text or has_meaningful_text or has_visual_evidence):
        missing_evidence.append("current_tab.extract_text or screenshot evidence")
    if (
        _is_spreadsheet_task(goal)
        and not bool(desktop.get("focus_succeeded"))
        and not has_destination_text
    ):
        missing_evidence.append(
            "focus_succeeded=false and no destination-bound text evidence; "
            "hotkey/screenshot without destination proof cannot certify keystrokes reached Sheets"
        )

    summary = (
        "現在のブラウザでの入力タスクは証拠不足です。入力先タブの URL/title と入力後のページ証拠を"
        " `capture_current_tab_state` で再取得する必要があります。欠けている evidence: "
        + ", ".join(missing_evidence)
    )

    repair_actions = retargeted.get("repair_actions")
    repair_actions = repair_actions if isinstance(repair_actions, list) else []
    normalized_actions: list[dict[str, Any]] = []
    replaced = False
    for action in repair_actions:
        if not isinstance(action, dict):
            continue
        copied = json.loads(json.dumps(action))
        target_step_ids = copied.get("target_step_ids")
        target_step_ids = target_step_ids if isinstance(target_step_ids, list) else []
        normalized_target_ids = [str(step_id or "").strip() for step_id in target_step_ids]
        normalized_target_ids = [step_id for step_id in normalized_target_ids if step_id]
        if "capture_current_tab_state" not in normalized_target_ids:
            copied["target_step_ids"] = ["capture_current_tab_state"]
            copied["description"] = (
                "Gather current-tab URL/title and post-entry page evidence before retrying spreadsheet verification."
            )
            copied["action_type"] = "gather_more_evidence"
            copied["priority"] = 1
            replaced = True
        normalized_actions.append(copied)

    if not normalized_actions:
        normalized_actions = [
            {
                "action_id": "gather_browser_destination_evidence",
                "action_type": "gather_more_evidence",
                "description": "Gather current-tab URL/title and post-entry page evidence before retrying spreadsheet verification.",
                "target_step_ids": ["capture_current_tab_state"],
                "priority": 1,
            }
        ]
        replaced = True

    if replaced:
        retargeted["repair_actions"] = normalized_actions
    retargeted["summary"] = summary
    return retargeted


def _promote_visual_playback_report(
    *,
    report: dict[str, Any],
    verification_inputs: dict[str, Any],
) -> dict[str, Any]:
    promoted = json.loads(json.dumps(report))
    desktop_inputs = verification_inputs.get("desktop", {})
    visual_change = desktop_inputs.get("visual_change", {})
    ratio = float(visual_change.get("changed_ratio") or 0.0)
    delta = float(visual_change.get("normalized_rgb_delta") or 0.0)
    evidence_refs = [
        ref
        for ref in (
            visual_change.get("before_path"),
            visual_change.get("after_path"),
        )
        if isinstance(ref, str) and ref
    ]
    explanation = (
        "再生前後のスクリーンショット差分が閾値を超えており、"
        f"changed_ratio={ratio:.4f}, normalized_rgb_delta={delta:.4f} でした。"
        "Djay の AX 情報が疎でも、再生操作の後に UI が明確に変化しているため、"
        "再生状態へ遷移した証拠として扱います。"
    )
    for criterion in promoted.get("criterion_results", []) or []:
        if not isinstance(criterion, dict):
            continue
        criterion["passed"] = True
        criterion["score"] = max(float(criterion.get("score") or 0.0), 0.9)
        criterion["explanation"] = explanation
        refs = criterion.get("evidence_refs", [])
        normalized_refs = list(refs) if isinstance(refs, list) else []
        for ref in evidence_refs:
            if ref not in normalized_refs:
                normalized_refs.append(ref)
        criterion["evidence_refs"] = normalized_refs
    promoted["status"] = "pass"
    promoted["overall_score"] = max(float(promoted.get("overall_score") or 0.0), 0.9)
    promoted["confidence"] = max(float(promoted.get("confidence") or 0.0), 0.8)
    promoted["failure_type"] = None
    promoted["summary"] = (
        "スクリーンショット比較で再生前後の UI 変化が確認できたため、"
        "desktop playback task を成功として扱いました。"
    )
    promoted["repair_actions"] = []
    return promoted


def _build_repair_patch_from_report(
    *,
    report: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any] | None:
    status = str(report.get("status") or "").strip()
    if status == "pass":
        return None

    failed_criteria = [
        str(item.get("name") or "").strip()
        for item in report.get("criterion_results", []) or []
        if isinstance(item, dict) and not item.get("passed")
    ]
    repair_actions = report.get("repair_actions")
    repair_actions = repair_actions if isinstance(repair_actions, list) else []
    repair_count = int(state.get(StateKeys.REPAIR_COUNT) or 0)
    approved_plan = _parse_json(state.get(StateKeys.PLAN_APPROVED)) or {}
    previous_plan_id = str(report.get("plan_id") or approved_plan.get("plan_id") or "").strip()
    return {
        "note": (
            f"Re-plan required. Failed criteria: {failed_criteria}. "
            f"Repair attempt {repair_count}/{_MAX_REPAIR_ATTEMPTS}."
        ),
        "failed_criteria": failed_criteria,
        "repair_actions": repair_actions,
        "previous_plan_id": previous_plan_id,
    }


def _build_replay_context_payload(
    *,
    source_task_id: str,
    from_step: str,
    report: dict[str, Any],
    step_trace: list[dict[str, Any]],
) -> dict[str, Any]:
    replay_context = ReplayContext(
        source_task_id=source_task_id,
        from_step=from_step,
        mode="tail",
        previous_verification_status=str(report.get("status") or "") or None,
        previous_failed_criteria=[
            str(item.get("name") or "").strip()
            for item in report.get("criterion_results", []) or []
            if isinstance(item, dict) and not item.get("passed")
        ],
        step_trace=step_trace,
    )
    return replay_context.model_dump(mode="json")


def _build_final_text(state: dict, report: dict) -> str:
    goal = state.get(StateKeys.TASK_GOAL, "")
    score = report.get("overall_score", 0.0)
    summary = report.get("summary", "")
    lines = [
        f"Task completed: {goal}",
        f"Score: {score:.2f}",
        f"{summary}",
    ]
    verification_inputs = _parse_json(state.get(StateKeys.TEMP_VERIFICATION_INPUTS)) or {}
    output_location = _output_location_from_verification_inputs(verification_inputs)
    if output_location:
        url = str(output_location.get("url") or "").strip()
        title = str(output_location.get("title") or "").strip()
        if url:
            lines.append(f"Location: {url}")
        if title:
            lines.append(f"Page: {title}")
    return "\n".join(line for line in lines if line).strip()


def _workflow_is_terminal(state: dict[str, Any]) -> bool:
    approval = state.get(StateKeys.APPROVAL_STATUS, "")
    if approval == "needs_human" and state.get(StateKeys.APPROVAL_REQUEST):
        return False
    if approval == "denied":
        return True
    report = _parse_json(state.get(StateKeys.VERIFY_LAST_REPORT)) or {}
    return report.get("status") in _TERMINAL_VERIFY_STATUSES


def _should_resume_existing_plan(
    *,
    attempt: int,
    has_approved_plan: bool,
    approval: str,
    replay_context: dict[str, Any] | None,
    repair_patch: dict[str, Any] | None,
) -> bool:
    if not has_approved_plan or approval not in _APPROVED_STATUSES:
        return False
    if attempt == 0:
        return True
    if replay_context:
        return True
    if repair_patch:
        return True
    return False


def _build_next_goal_state(init_state: dict[str, Any]) -> dict[str, Any]:
    state_delta: dict[str, Any] = {
        StateKeys.TASK_GOAL: None,
        StateKeys.TASK_CONSTRAINTS: None,
        StateKeys.TASK_SUCCESS_CRITERIA: None,
        StateKeys.PLAN_CURRENT: None,
        StateKeys.PLAN_APPROVED: None,
        StateKeys.PLAN_RISK_LEVEL: None,
        StateKeys.REPLAY_SOURCE_TASK_ID: None,
        StateKeys.REPLAY_FROM_STEP: None,
        StateKeys.REPLAY_CONTEXT: None,
        StateKeys.APPROVAL_STATUS: None,
        StateKeys.APPROVAL_REQUEST: None,
        StateKeys.VERIFY_LAST_REPORT: None,
        StateKeys.REPAIR_COUNT: 0,
        StateKeys.MEMORY_LAST_CANDIDATE_IDS: None,
        StateKeys.MEMORY_LAST_PROMOTED_IDS: None,
        StateKeys.TEMP_RETRIEVAL_BUNDLE: None,
        StateKeys.TEMP_PLANNER_DRAFT: None,
        StateKeys.TEMP_EXECUTOR_OUTPUTS: None,
        StateKeys.TEMP_ARTIFACT_REFS: None,
        StateKeys.TEMP_VERIFICATION_INPUTS: None,
        StateKeys.TEMP_REPAIR_PATCH: None,
        StateKeys.TEMP_CURRENT_BROWSER_NEW_TAB_COUNT: None,
        StateKeys.TEMP_CURRENT_BROWSER_OPENED_TAB_IDS: None,
    }
    state_delta.update(init_state)
    return state_delta


# ── Default singleton ──────────────────────────────────────────────────────

_default_loop: ControlLoop | None = None


def get_control_loop() -> ControlLoop:
    global _default_loop
    if _default_loop is None:
        _default_loop = ControlLoop()
    return _default_loop
