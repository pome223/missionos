"""Shared task keyword sets used across routing and control-loop normalization."""

from __future__ import annotations

CURRENT_BROWSER_KEYWORDS: frozenset[str] = frozenset(
    {
        "このブラウザ",
        "このタブ",
        "このページ",
        "このウィンドウ",
        "私が開いているブラウザ",
        "今開いているブラウザ",
        "開いているブラウザ",
        "今のブラウザ",
        "いまのブラウザ",
        "現在のブラウザ",
        "既存のブラウザ",
        "私のブラウザ",
        "current browser",
        "existing browser",
        "今開いているタブ",
        "開いているタブ",
        "今のタブ",
        "現在のタブ",
        "既存のタブ",
        "今開いているスプレッドシート",
        "開いているスプレッドシート",
        "今のスプレッドシート",
        "現在のスプレッドシート",
        "既存のスプレッドシート",
    }
)

SPREADSHEET_KEYWORDS: frozenset[str] = frozenset(
    {
        "spreadsheet",
        "spread sheet",
        "sheet",
        "google sheet",
        "google sheets",
        "googleスプレッドシート",
        "スプレッド",
        "すぷれっど",
        "スプシ",
        "スプレッドシート",
        "シート",
        "表計算",
        # Keep a few observed typo/OCR variants so current-browser heuristics remain
        # tolerant of slightly garbled user text.
        "スプレッドsーと",
        "スプレッドシーート",
    }
)

COMPUTER_USE_KEYWORDS: frozenset[str] = frozenset(
    {
        "computer use",
        "computer using",
        "computer operator",
        "gui automation",
        "gui operator",
        "visible browser",
        "visible ui",
        "画面を見て",
        "画面を見ながら",
        "画面を確認しながら",
        "見えているブラウザ",
        "見えてるブラウザ",
        "見えている画面",
        "guiを見て",
        "uiを見て",
        "目で見て操作",
    }
)

TEXT_ENTRY_KEYWORDS: frozenset[str] = frozenset(
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
        "form",
        "フォーム",
    }
)


def _contains_any(text: str, keywords: frozenset[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def prefers_isolated_browser_for_goal(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    has_current_browser = _contains_any(normalized, CURRENT_BROWSER_KEYWORDS)
    has_spreadsheet = _contains_any(normalized, SPREADSHEET_KEYWORDS)
    needs_visible_text_entry = _contains_any(normalized, TEXT_ENTRY_KEYWORDS)
    # Current-browser spreadsheet requests often depend on the user's existing
    # authenticated web session (for example Google Sheets). Keep those tasks in
    # the user's browser and reserve isolated browsers for generic form/text
    # entry where session carry-over is not required.
    return has_current_browser and needs_visible_text_entry and not has_spreadsheet
