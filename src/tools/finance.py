"""
Stock price lookup tool.

Fetches daily stock prices from the free Stooq endpoint.
"""

from __future__ import annotations

import csv
from io import StringIO
from typing import Optional

import httpx
from google.adk.tools import FunctionTool


_ALIASES = {
    "nvidia": "NVDA.US",
    "nvda": "NVDA.US",
    "tesla": "TSLA.US",
    "tsla": "TSLA.US",
    "apple": "AAPL.US",
    "aapl": "AAPL.US",
    "microsoft": "MSFT.US",
    "msft": "MSFT.US",
    "google": "GOOGL.US",
    "alphabet": "GOOGL.US",
    "amazon": "AMZN.US",
    "meta": "META.US",
}

_NON_DIRECT_STOCK_QUERY_KEYWORDS = {
    "news",
    "api",
    "ニュース",
    "記事",
    "理由",
    "なぜ",
    "解説",
    "分析",
    "予想",
    "予測",
    "推測",
    "見通し",
    "リサーチ",
    "調査",
    "チャート",
    "推移",
    "コード",
    "実装",
    "作りたい",
    "作って",
    "開発",
    "ライブラリ",
    "スクレイピング",
    "方法",
    "とは",
    "意味",
    "仕組み",
    "gtc",
    "イベント",
    "前後",
    "後",
    "前",
}


def _normalize_symbol(raw: str) -> Optional[str]:
    token = (raw or "").strip().lower()
    if not token:
        return None

    if token in _ALIASES:
        return _ALIASES[token]

    if token.endswith(".us"):
        return token.upper()

    # Treat short ASCII tokens as direct ticker symbols.
    if token.isalnum() and 1 <= len(token) <= 6:
        return f"{token.upper()}.US"

    # Handle natural-language company mentions such as "Nvidia stock price".
    for k, v in _ALIASES.items():
        if k in token:
            return v

    return None


def is_direct_stock_price_query(message: str) -> bool:
    """Enable the stock shortcut only for direct daily OHLC/current-price requests."""
    normalized = (message or "").strip().lower()
    if "株価" not in normalized:
        return False
    if any(keyword in normalized for keyword in _NON_DIRECT_STOCK_QUERY_KEYWORDS):
        return False
    return _normalize_symbol(normalized) is not None


async def stock_price(symbol_or_name: str) -> dict:
    """
    Fetch daily OHLC stock price data.

    Args:
        symbol_or_name: ticker or company name, for example NVDA or NVIDIA.

    Returns:
        Price information.
    """
    symbol = _normalize_symbol(symbol_or_name)
    if not symbol:
        return {
            "ok": False,
            "message": "Could not identify a stock symbol. Examples: NVDA, AAPL, TSLA",
        }

    url = "https://stooq.com/q/l/"
    params = {"s": symbol.lower(), "i": "d"}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params=params)
    except httpx.TimeoutException:
        return {"ok": False, "symbol": symbol, "message": "The stock price API timed out."}
    except httpx.HTTPError as exc:
        return {"ok": False, "symbol": symbol, "message": f"Stock price API call failed: {exc}"}

    if resp.status_code >= 400:
        return {
            "ok": False,
            "symbol": symbol,
            "message": f"Stock price API error: HTTP {resp.status_code}",
        }

    text = resp.text.strip()
    row = None

    # Case 1: CSV with a header row.
    reader = csv.DictReader(StringIO(text))
    parsed = next(reader, None)
    if parsed and any(k in parsed for k in ("Symbol", "Date", "Close")):
        row = parsed
    else:
        # Case 2: single-row CSV without a header.
        # Example: NVDA.US,20260227,220020,181.25,182.59,176.38,177.19,310416947,
        first_line = text.splitlines()[0] if text else ""
        parts = [p.strip() for p in first_line.split(",")]
        if len(parts) >= 8:
            row = {
                "Symbol": parts[0],
                "Date": parts[1],
                "Time": parts[2],
                "Open": parts[3],
                "High": parts[4],
                "Low": parts[5],
                "Close": parts[6],
                "Volume": parts[7],
            }

    if not row:
        return {"ok": False, "symbol": symbol, "message": "No stock price data was found."}

    close = row.get("Close")
    if not close or close.upper() == "N/D":
        return {"ok": False, "symbol": symbol, "message": "No valid close price data is available."}

    return {
        "ok": True,
        "source": "stooq",
        "symbol": row.get("Symbol", symbol),
        "date": row.get("Date"),
        "time": row.get("Time"),
        "open": row.get("Open"),
        "high": row.get("High"),
        "low": row.get("Low"),
        "close": close,
        "volume": row.get("Volume"),
    }


stock_price_tool = FunctionTool(stock_price)
