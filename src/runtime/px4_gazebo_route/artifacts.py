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
import shutil
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


def snapshot_task_database_evidence(*, task_db_path: Path, run_dir: Path) -> Path:
    """Copy an active TaskStore database into the durable run directory.

    Callers that use a temporary TaskStore must invoke this function before the
    temporary directory is released.  The copied database is evidence only;
    this function never opens or mutates it.
    """

    snapshot_path = run_dir / "tasks.db"
    shutil.copy2(task_db_path, snapshot_path)
    return snapshot_path


def write_run_artifacts(
    *,
    run_dir: Path,
    summary: Mapping[str, Any],
    task_artifacts: Mapping[str, Any],
    log_text: str,
    pose_rows: Sequence[Mapping[str, Any]] | None = None,
    task_db_path: Path | None = None,
    recorded_at: datetime | None = None,
) -> None:
    """Serialize supplied route evidence without changing claim semantics.

    ``pose_rows=None`` preserves a live trace that has already been written by
    the runtime.  A supplied task database is copied only as evidence; this
    function never opens or mutates the task store.
    """

    write_json(run_dir / "summary.json", dict(summary))
    write_json(
        run_dir / "mission_artifacts.json",
        {
            "recorded_at": (recorded_at or datetime.now(timezone.utc)).isoformat(),
            "frozen_for_test": False,
            "artifacts": dict(task_artifacts),
        },
    )
    if pose_rows is not None:
        write_jsonl(run_dir / "pose_samples.jsonl", pose_rows)
    (run_dir / "px4_docker.log").write_text(log_text)
    if task_db_path is not None:
        snapshot_task_database_evidence(
            task_db_path=task_db_path,
            run_dir=run_dir,
        )


def write_recovery_run_artifacts(
    *,
    run_dir: Path,
    summary: Mapping[str, Any],
    task_artifacts: Mapping[str, Any],
    pose_rows: Sequence[Mapping[str, Any]],
    log_text: str,
    task_db_path: Path,
    recorded_at: datetime | None = None,
) -> None:
    """Serialize already-produced recovery evidence without changing its meaning."""

    write_run_artifacts(
        run_dir=run_dir,
        summary=summary,
        task_artifacts=task_artifacts,
        pose_rows=pose_rows,
        log_text=log_text,
        task_db_path=task_db_path,
        recorded_at=recorded_at,
    )


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
    "snapshot_task_database_evidence",
    "write_json",
    "write_jsonl",
    "write_recovery_run_artifacts",
    "write_run_artifacts",
]
