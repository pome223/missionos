#!/usr/bin/env python3
"""Verify that every Python smoke program has one explicit disposition."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
import argparse
import json
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
DEFAULT_INVENTORY = SCRIPTS_DIR / "smoke_inventory.toml"


def load_inventory(path: Path = DEFAULT_INVENTORY) -> dict[str, Any]:
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    categories = payload.get("categories")
    if not isinstance(categories, dict) or not categories:
        raise ValueError("smoke inventory must define non-empty categories")
    return payload


def verify_inventory(path: Path = DEFAULT_INVENTORY) -> dict[str, Any]:
    payload = load_inventory(path)
    categories = payload["categories"]
    classified: list[str] = []
    counts: dict[str, int] = {}
    actions: dict[str, str] = {}
    for name, raw in categories.items():
        if not isinstance(raw, dict):
            raise ValueError(f"category {name!r} must be a table")
        action = str(raw.get("action") or "").strip()
        files = raw.get("files")
        if not action or not isinstance(files, list):
            raise ValueError(f"category {name!r} needs action and files")
        normalized = [str(item).strip() for item in files]
        if any(not item.startswith("smoke_") or not item.endswith(".py") for item in normalized):
            raise ValueError(f"category {name!r} contains a non-smoke filename")
        classified.extend(normalized)
        counts[name] = len(normalized)
        actions[name] = action

    duplicates = sorted(name for name, count in Counter(classified).items() if count > 1)
    actual = sorted(path.name for path in SCRIPTS_DIR.glob("smoke_*.py"))
    missing = sorted(set(actual) - set(classified))
    absent = sorted(set(classified) - set(actual))
    ok = not duplicates and not missing and not absent and len(classified) == len(actual)
    return {
        "schema_version": payload.get("schema_version"),
        "ok": ok,
        "actual_count": len(actual),
        "classified_count": len(classified),
        "category_counts": counts,
        "category_actions": actions,
        "duplicates": duplicates,
        "missing": missing,
        "absent": absent,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    result = verify_inventory(args.inventory)
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
