from __future__ import annotations

from dataclasses import dataclass, field
from typing import AbstractSet, Any

from src.runtime.task_keywords import (
    COMPUTER_USE_KEYWORDS,
    CURRENT_BROWSER_KEYWORDS,
    SPREADSHEET_KEYWORDS,
    TEXT_ENTRY_KEYWORDS,
    prefers_isolated_browser_for_goal,
)

VALID_TARGETS = {"root_agent", "control_loop", "specialist", "dynamic_agent"}
VALID_SPECIALISTS = {
    "web_researcher",
    "file_manager",
    "browser_automator",
    "current_tab_operator",
    "control_ui_chat_operator",
    "desktop_operator",
    "computer_operator",
    "system_operator",
    "memory_keeper",
}
VALID_HANDOFF_MODES = {"direct", "preflight_then_root"}

_RESEARCH_KEYWORDS = {
    "最新",
    "最近",
    "ニュース",
    "調べて",
    "調査",
    "リサーチ",
    "噂",
    "今年",
    "来日",
    "予定",
    "公演",
    "ライブ",
    "フェス",
    "開催",
    "話題",
    "発表",
    "gtc",
    "report",
    "research",
    "検索",
    "search",
}

_LONGFORM_KEYWORDS = {
    "詳細",
    "詳しく",
    "レポート",
    "report",
    "分析",
    "analyze",
    "analysis",
    "比較",
    "まとめて",
    "提案",
    "計画",
    "plan",
    "戦略",
    "多角的",
    "多面的",
}

_SEQUENCE_KEYWORDS = {
    "その後",
    "してから",
    "次に",
    "順番に",
    "手順",
    "step by step",
    "multi-step",
    "verify",
    "検証",
    "確認しながら",
}

_BROWSER_KEYWORDS = {
    "url",
    "http://",
    "https://",
    "ブラウザ",
    "ページ",
    "サイト",
    "スクレイプ",
    "スクレイピング",
    "抽出",
    "navigate",
    "browse",
}

_BROWSER_INTERACTION_KEYWORDS = {
    "click",
    "chat",
    "fill",
    "form",
    "input",
    "press",
    "question",
    "ask",
    "message",
    "talk",
    "submit",
    "type",
    "入力",
    "打って",
    "押して",
    "送信",
    "聞いて",
    "質問",
    "会話",
    "話して",
}

_COMPUTER_SURFACE_KEYWORDS = {
    "gui",
    "computer",
    "screen-aware",
    "visible ui",
    "見えて",
    "画面",
    "スクリーン",
    "ui",
}

_DESKTOP_VIEW_KEYWORDS = {
    "画面",
    "スクリーン",
    "スクショ",
    "スクリーンショット",
    "ウィンドウ",
    "前面アプリ",
    "待って",
    "wait",
    "出るまで",
    "現れるまで",
    "frontmost",
    "window",
    "screen",
    "desktop",
}

_DESKTOP_CONTROL_KEYWORDS = {
    "クリック",
    "click",
    "drag",
    "ドラッグ",
    "type",
    "入力",
    "打って",
    "押して",
    "scroll",
    "スクロール",
    "hotkey",
    "ショートカット",
    "起動",
    "開いて",
    "アプリを開いて",
    "切り替えて",
    "focus",
    "フォーカス",
}

_DESKTOP_RUNTIME_KEYWORDS = {
    "停止",
    "止めて",
    "emergency stop",
    "panic",
    "abort desktop",
    "abort gui",
}

_DESKTOP_PLAYBACK_KEYWORDS = {
    "djay",
    "spotify",
    "apple music",
    "itunes",
    "music",
    "song",
    "track",
    "playlist",
    "曲",
    "再生",
    "音楽",
    "プレイリスト",
    "かけて",
    "流して",
    "play",
}

_FILE_KEYWORDS = {
    "ファイル",
    "readme",
    "コード",
    "差分",
    "diff",
    "編集",
    "修正",
    "書き換え",
    "refactor",
}

_SYSTEM_KEYWORDS = {
    "shell",
    "コマンド",
    "ターミナル",
    "terminal",
    "docker",
    "git",
    "bash",
    "zsh",
    "cli",
}

_MEMORY_KEYWORDS = {
    "覚えて",
    "remember",
    "記憶",
    "メモリ",
    "memory",
    "過去の会話",
    "嗜好",
}

_DYNAMIC_AGENT_KEYWORDS = {
    "mcp",
    "カスタムエージェント",
    "専用エージェント",
    "dynamic agent",
    "custom agent",
    "このサーバーを使って",
}

_SKILL_KEYWORDS = {
    "skill",
    "スキル",
}



@dataclass
class DynamicAgentRequest:
    instruction: str = ""
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "run"


@dataclass
class RoutingDecision:
    target: str = "root_agent"
    specialist: str | None = None
    handoff_mode: str = "direct"
    reason: str = ""
    confidence: float = 0.0
    dynamic_agent: DynamicAgentRequest = field(default_factory=DynamicAgentRequest)

    @property
    def preflight_specialist(self) -> bool:
        return self.target == "specialist" and self.handoff_mode == "preflight_then_root"

    @property
    def route_label(self) -> str:
        if self.target == "specialist" and self.specialist:
            return self.specialist
        return self.target


def coerce_decision(payload: dict[str, Any] | None) -> RoutingDecision:
    if not isinstance(payload, dict):
        return heuristic_decision("")

    target = str(payload.get("target") or "root_agent").strip()
    if target not in VALID_TARGETS:
        target = "root_agent"

    specialist = payload.get("specialist")
    if specialist is not None:
        specialist = str(specialist).strip()
    if specialist not in VALID_SPECIALISTS:
        specialist = None

    handoff_mode = str(payload.get("handoff_mode") or "direct").strip()
    if handoff_mode not in VALID_HANDOFF_MODES:
        handoff_mode = "direct"

    confidence = _coerce_confidence(payload.get("confidence"))
    reason = str(payload.get("reason") or "").strip()

    dynamic_payload = payload.get("dynamic_agent")
    dynamic_request = DynamicAgentRequest()
    if isinstance(dynamic_payload, dict):
        instruction = str(dynamic_payload.get("instruction") or "").strip()
        mode = str(dynamic_payload.get("mode") or "run").strip().lower()
        if mode not in {"run", "session"}:
            mode = "run"
        mcp_servers = dynamic_payload.get("mcp_servers") or []
        if not isinstance(mcp_servers, list):
            mcp_servers = []
        normalized_servers = [
            item for item in mcp_servers if isinstance(item, dict)
        ]
        dynamic_request = DynamicAgentRequest(
            instruction=instruction,
            mcp_servers=normalized_servers,
            mode=mode,
        )

    if target == "specialist" and specialist is None:
        target = "root_agent"
        handoff_mode = "direct"
    if target != "specialist":
        specialist = None
        handoff_mode = "direct"
    if target != "dynamic_agent":
        dynamic_request = DynamicAgentRequest()

    return RoutingDecision(
        target=target,
        specialist=specialist,
        handoff_mode=handoff_mode,
        reason=reason,
        confidence=confidence,
        dynamic_agent=dynamic_request,
    )


def heuristic_decision(message: str) -> RoutingDecision:
    normalized = (message or "").strip().lower()
    if not normalized:
        return RoutingDecision()

    has_research = _contains_any(normalized, _RESEARCH_KEYWORDS)
    has_longform = _contains_any(normalized, _LONGFORM_KEYWORDS)
    has_sequence = _contains_any(normalized, _SEQUENCE_KEYWORDS)
    has_computer_use = _contains_any(normalized, COMPUTER_USE_KEYWORDS)
    has_browser = _contains_any(normalized, _BROWSER_KEYWORDS)
    has_desktop_view = _contains_any(normalized, _DESKTOP_VIEW_KEYWORDS)
    has_desktop_control = _contains_any(normalized, _DESKTOP_CONTROL_KEYWORDS)
    has_desktop_runtime = _contains_any(normalized, _DESKTOP_RUNTIME_KEYWORDS)
    has_spreadsheet = _contains_any(normalized, SPREADSHEET_KEYWORDS)
    has_file = _contains_any(normalized, _FILE_KEYWORDS)
    has_system = _contains_any(normalized, _SYSTEM_KEYWORDS)
    has_memory = _contains_any(normalized, _MEMORY_KEYWORDS)
    has_dynamic = _contains_any(normalized, _DYNAMIC_AGENT_KEYWORDS)
    has_skill = _contains_any(normalized, _SKILL_KEYWORDS)

    if has_dynamic:
        return RoutingDecision(
            target="dynamic_agent",
            reason="request explicitly asks for a custom or MCP-backed agent",
            confidence=0.9,
        )

    if has_computer_use and (has_sequence or has_longform or has_spreadsheet):
        return RoutingDecision(
            target="control_loop",
            reason="computer-use request is multi-step or verification-heavy",
            confidence=0.9,
        )

    if _is_computer_use_specialist_flow(normalized):
        return RoutingDecision(
            target="specialist",
            specialist="computer_operator",
            handoff_mode="direct",
            reason="browser-first or screen-aware GUI request should use the computer operator",
            confidence=0.86,
        )

    if has_browser and has_spreadsheet:
        return RoutingDecision(
            target="control_loop",
            reason="browser spreadsheet request requires multi-step automation instead of research/file fallback",
            confidence=0.9,
        )

    if has_research and has_longform:
        return RoutingDecision(
            target="control_loop",
            reason="latest or research-heavy request with long-form output",
            confidence=0.82,
        )

    if has_desktop_runtime:
        return RoutingDecision(
            target="specialist",
            specialist="desktop_operator",
            handoff_mode="direct",
            reason="desktop runtime safety request",
            confidence=0.84,
        )

    if _requires_desktop_control_loop(normalized):
        return RoutingDecision(
            target="control_loop",
            reason="multi-step or verification-heavy desktop automation request",
            confidence=0.86,
        )

    if has_desktop_control:
        return RoutingDecision(
            target="specialist",
            specialist="desktop_operator",
            handoff_mode="direct",
            reason="desktop control request",
            confidence=0.83,
        )

    if has_desktop_view:
        return RoutingDecision(
            target="specialist",
            specialist="desktop_operator",
            handoff_mode="preflight_then_root",
            reason="desktop state inspection request",
            confidence=0.8,
        )

    if has_browser:
        return RoutingDecision(
            target="specialist",
            specialist="browser_automator",
            handoff_mode="preflight_then_root",
            reason="browser or page interaction request",
            confidence=0.8,
        )

    if has_research:
        return RoutingDecision(
            target="specialist",
            specialist="web_researcher",
            handoff_mode="preflight_then_root",
            reason="latest or research-sensitive request",
            confidence=0.84,
        )

    if has_system:
        return RoutingDecision(
            target="specialist",
            specialist="system_operator",
            reason="shell or system task",
            confidence=0.78,
        )

    if has_file:
        return RoutingDecision(
            target="specialist",
            specialist="file_manager",
            reason="file or code-oriented request",
            confidence=0.76,
        )

    if has_memory:
        return RoutingDecision(
            target="specialist",
            specialist="memory_keeper",
            reason="memory-oriented request",
            confidence=0.72,
        )

    if has_skill:
        return RoutingDecision(
            target="root_agent",
            reason="skill requests should be handled by root_agent unless a dynamic agent is required",
            confidence=0.7,
        )

    return RoutingDecision(
        target="root_agent",
        reason="default conversational handling",
        confidence=0.6,
    )


def decision_from_payload(
    payload: dict[str, Any] | None,
    *,
    fallback_message: str,
) -> RoutingDecision:
    if _is_control_ui_chat_flow(fallback_message):
        return RoutingDecision(
            target="specialist",
            specialist="control_ui_chat_operator",
            handoff_mode="direct",
            reason="boiled-claw Control UI chat flow should stay on the dedicated chat operator",
            confidence=0.9,
        )

    if _is_current_tab_web_flow(fallback_message):
        return RoutingDecision(
            target="specialist",
            specialist="current_tab_operator",
            handoff_mode="direct",
            reason="current browser web request should stay on the current-tab operator",
            confidence=0.93,
        )

    if _is_computer_use_specialist_flow(fallback_message):
        return RoutingDecision(
            target="specialist",
            specialist="computer_operator",
            handoff_mode="direct",
            reason="screen-aware or browser-first GUI request should stay on the computer operator",
            confidence=0.92,
        )

    if _requires_current_browser_control_loop(fallback_message):
        return RoutingDecision(
            target="control_loop",
            reason="current browser or existing spreadsheet request requires desktop-backed control loop",
            confidence=0.94,
        )

    if _requires_isolated_browser_control_loop(fallback_message):
        return RoutingDecision(
            target="control_loop",
            reason="visible text-entry request should use an isolated browser control loop",
            confidence=0.94,
        )

    if _requires_desktop_control_loop(fallback_message):
        return RoutingDecision(
            target="control_loop",
            reason="desktop playback or multi-action app workflow requires control loop verification",
            confidence=0.92,
        )

    decision = coerce_decision(payload)
    if decision.target == "control_loop" and _is_browser_only_flow(fallback_message):
        return RoutingDecision(
            target="specialist",
            specialist="browser_automator",
            handoff_mode="direct",
            reason="browser-only multi-step request should stay on browser_automator",
            confidence=max(decision.confidence, 0.82),
        )
    if decision.confidence > 0.0 or decision.reason:
        return decision
    return heuristic_decision(fallback_message)


def _contains_any(text: str, keywords: AbstractSet[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def targets_user_browser(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return _contains_any(normalized, CURRENT_BROWSER_KEYWORDS)


def _requires_current_browser_control_loop(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if (
        not normalized
        or not targets_user_browser(normalized)
        or prefers_isolated_browser_for_goal(normalized)
    ):
        return False

    has_spreadsheet = _contains_any(normalized, SPREADSHEET_KEYWORDS)
    has_research = _contains_any(normalized, _RESEARCH_KEYWORDS)
    has_longform = _contains_any(normalized, _LONGFORM_KEYWORDS)
    has_sequence = _contains_any(normalized, _SEQUENCE_KEYWORDS)
    has_browser = _contains_any(normalized, _BROWSER_KEYWORDS)
    has_desktop = _contains_any(normalized, _DESKTOP_CONTROL_KEYWORDS | _DESKTOP_VIEW_KEYWORDS)

    return has_spreadsheet or has_research or has_longform or ((has_browser or has_desktop) and has_sequence)


def _requires_isolated_browser_control_loop(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized or not prefers_isolated_browser_for_goal(normalized):
        return False

    has_spreadsheet = _contains_any(normalized, SPREADSHEET_KEYWORDS)
    has_text_entry = _contains_any(normalized, TEXT_ENTRY_KEYWORDS)
    has_research = _contains_any(normalized, _RESEARCH_KEYWORDS)
    has_longform = _contains_any(normalized, _LONGFORM_KEYWORDS)
    has_sequence = _contains_any(normalized, _SEQUENCE_KEYWORDS)

    return has_spreadsheet or has_text_entry or has_research or has_longform or has_sequence


def _requires_desktop_control_loop(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False

    has_desktop_control = _contains_any(normalized, _DESKTOP_CONTROL_KEYWORDS)
    has_sequence = _contains_any(normalized, _SEQUENCE_KEYWORDS)
    has_longform = _contains_any(normalized, _LONGFORM_KEYWORDS)
    has_playback = _contains_any(normalized, _DESKTOP_PLAYBACK_KEYWORDS)

    return has_desktop_control and (has_sequence or has_longform or has_playback)


def _is_current_tab_web_flow(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if (
        not normalized
        or not targets_user_browser(normalized)
        or prefers_isolated_browser_for_goal(normalized)
    ):
        return False

    if _is_control_ui_chat_flow(normalized):
        return False

    if _contains_any(normalized, COMPUTER_USE_KEYWORDS):
        return False

    has_spreadsheet = _contains_any(normalized, SPREADSHEET_KEYWORDS)
    has_desktop = _contains_any(normalized, _DESKTOP_CONTROL_KEYWORDS | _DESKTOP_VIEW_KEYWORDS)
    has_research = _contains_any(normalized, _RESEARCH_KEYWORDS)
    has_longform = _contains_any(normalized, _LONGFORM_KEYWORDS)
    has_browser = _contains_any(normalized, _BROWSER_KEYWORDS | _BROWSER_INTERACTION_KEYWORDS)
    return not has_spreadsheet and not has_desktop and not has_longform and (has_research or has_browser)


def _is_computer_use_specialist_flow(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    if prefers_isolated_browser_for_goal(normalized):
        return False

    has_current_browser = targets_user_browser(normalized)
    has_explicit_computer_use = _contains_any(normalized, COMPUTER_USE_KEYWORDS)
    has_visible_surface = _contains_any(normalized, _COMPUTER_SURFACE_KEYWORDS)
    has_desktop_surface = _contains_any(normalized, _DESKTOP_CONTROL_KEYWORDS | _DESKTOP_VIEW_KEYWORDS)
    has_spreadsheet = _contains_any(normalized, SPREADSHEET_KEYWORDS)
    has_research = _contains_any(normalized, _RESEARCH_KEYWORDS)
    has_sequence = _contains_any(normalized, _SEQUENCE_KEYWORDS | _LONGFORM_KEYWORDS)

    return (
        (
            has_explicit_computer_use
            or (
                has_current_browser
                and (has_visible_surface or has_desktop_surface)
                and not has_research
            )
        )
        and not has_spreadsheet
        and not has_sequence
    )


def _is_browser_only_flow(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False

    has_browser = _contains_any(normalized, _BROWSER_KEYWORDS)
    has_browser_interaction = _contains_any(normalized, _BROWSER_INTERACTION_KEYWORDS)
    has_research = _contains_any(normalized, _RESEARCH_KEYWORDS)
    has_longform = _contains_any(normalized, _LONGFORM_KEYWORDS)
    has_file = _contains_any(normalized, _FILE_KEYWORDS)
    has_system = _contains_any(normalized, _SYSTEM_KEYWORDS)
    has_memory = _contains_any(normalized, _MEMORY_KEYWORDS)
    has_dynamic = _contains_any(normalized, _DYNAMIC_AGENT_KEYWORDS)
    has_skill = _contains_any(normalized, _SKILL_KEYWORDS)

    return (
        has_browser
        and has_browser_interaction
        and not has_research
        and not has_longform
        and not has_file
        and not has_system
        and not has_memory
        and not has_dynamic
        and not has_skill
    )


def _is_control_ui_chat_flow(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False

    return (
        ("localhost:18789/chat" in normalized or "127.0.0.1:18789/chat" in normalized)
        and _contains_any(normalized, _BROWSER_INTERACTION_KEYWORDS)
    )


def _coerce_confidence(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))
