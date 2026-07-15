"""Artifact persistence for the opt-in PX4/Gazebo route runtime.

This module creates run directories and serializes already-produced evidence.
It does not decide authority, execute commands, or promote observed data into a
completion claim.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


ARTIFACT_ROOT_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT"
DEFAULT_ARTIFACT_ROOT = "output/px4_gazebo_route_runs"


def artifact_root(*, environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    return Path(values.get(ARTIFACT_ROOT_ENV, DEFAULT_ARTIFACT_ROOT))


def create_run_directory(
    *,
    root: Path | None = None,
    observed_at: datetime | None = None,
) -> Path:
    resolved_root = artifact_root() if root is None else root
    stamp = (observed_at or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    run_dir = resolved_root / f"horizontal_route_{stamp}"
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = resolved_root / f"horizontal_route_{stamp}_{suffix}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def mark_cleanup_observed(
    run_dir: Path,
    *,
    observed_at: datetime | None = None,
) -> bool:
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        return False
    summary = json.loads(summary_path.read_text())
    cleanup = dict(summary.get("scenario_cleanup_receipt") or {})
    if not cleanup:
        return False
    cleanup["cleanup_status"] = "isolated_container_teardown_observed"
    cleanup["observed_at"] = (observed_at or datetime.now(timezone.utc)).isoformat()
    summary["scenario_cleanup_receipt"] = cleanup
    write_json(summary_path, summary)
    return True


__all__ = [
    "ARTIFACT_ROOT_ENV",
    "DEFAULT_ARTIFACT_ROOT",
    "artifact_root",
    "create_run_directory",
    "mark_cleanup_observed",
    "write_json",
    "write_jsonl",
]
