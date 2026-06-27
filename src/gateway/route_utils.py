from __future__ import annotations

from typing import Any


def normalize_constraints(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]
