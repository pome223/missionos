#!/usr/bin/env python3
"""Check whether changed files hit the advisory lesson invariance watch-list."""

from __future__ import annotations

import argparse
import fnmatch
import json
from pathlib import Path


def _read_lines(path: Path) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _matches(pattern: str, changed_path: str) -> bool:
    if fnmatch.fnmatch(changed_path, pattern):
        return True
    if pattern.endswith("/") and changed_path.startswith(pattern):
        return True
    return changed_path == pattern


def changed_paths_hit_watchlist(
    *,
    changed_paths: tuple[str, ...],
    watch_paths: tuple[str, ...],
) -> tuple[str, ...]:
    hits: list[str] = []
    for changed_path in changed_paths:
        for watch_path in watch_paths:
            if _matches(watch_path, changed_path):
                hits.append(changed_path)
                break
    return tuple(sorted(set(hits)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist", type=Path, required=True)
    parser.add_argument("--changed", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    watch_paths = _read_lines(args.watchlist)
    changed_paths = _read_lines(args.changed)
    hits = changed_paths_hit_watchlist(
        changed_paths=changed_paths,
        watch_paths=watch_paths,
    )
    required = bool(hits)
    summary = {
        "lesson_invariance_required": required,
        "watchlist_path_count": len(watch_paths),
        "changed_path_count": len(changed_paths),
        "matched_changed_paths": list(hits),
    }
    if args.github_output is not None:
        with args.github_output.open("a") as handle:
            handle.write(f"required={str(required).lower()}\n")
            handle.write(f"matched_paths={','.join(hits)}\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
