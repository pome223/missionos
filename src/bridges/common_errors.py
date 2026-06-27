"""Shared bridge/runtime error helpers."""

from __future__ import annotations


def flatten_exception_text(exc: BaseException) -> str:
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, tuple) and nested:
        parts: list[str] = []
        for item in nested:
            text = flatten_exception_text(item)
            if text and text not in parts:
                parts.append(text)
        if parts:
            return "; ".join(parts)
    return str(exc)


__all__ = ["flatten_exception_text"]
