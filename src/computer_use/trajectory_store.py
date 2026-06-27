"""Trajectory capture for browser-first computer use."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from src.config.settings import get_settings


class ComputerTrajectoryStore:
    """Persist computer-use attempts for later review and repair analysis."""

    def __init__(self, db_path: str = "data/computer_trajectories.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS computer_trajectories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    final_surface TEXT,
                    attempts_json TEXT NOT NULL,
                    verification_json TEXT,
                    request_json TEXT,
                    observation_json TEXT,
                    preliminary_failure_type TEXT,
                    normalized_failure_type TEXT,
                    classified_by_json TEXT,
                    operator_override TEXT,
                    reuse_trace_json TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_computer_trajectories_created_at
                ON computer_trajectories(created_at DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_computer_trajectories_status
                ON computer_trajectories(status)
                """
            )
            columns = {
                row[1]
                for row in cursor.execute("PRAGMA table_info(computer_trajectories)").fetchall()
            }
            if "preliminary_failure_type" not in columns:
                cursor.execute(
                    """
                    ALTER TABLE computer_trajectories
                    ADD COLUMN preliminary_failure_type TEXT
                    """
                )
            if "normalized_failure_type" not in columns:
                cursor.execute(
                    """
                    ALTER TABLE computer_trajectories
                    ADD COLUMN normalized_failure_type TEXT
                    """
                )
            if "classified_by_json" not in columns:
                cursor.execute(
                    """
                    ALTER TABLE computer_trajectories
                    ADD COLUMN classified_by_json TEXT
                    """
                )
            if "operator_override" not in columns:
                cursor.execute(
                    """
                    ALTER TABLE computer_trajectories
                    ADD COLUMN operator_override TEXT
                    """
                )
            if "reuse_trace_json" not in columns:
                cursor.execute(
                    """
                    ALTER TABLE computer_trajectories
                    ADD COLUMN reuse_trace_json TEXT
                    """
                )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_computer_trajectories_failure_type
                ON computer_trajectories(normalized_failure_type)
                """
            )
            conn.commit()

    def record(
        self,
        *,
        action: str,
        status: str,
        final_surface: Optional[str],
        attempts: list[dict[str, Any]],
        verification: dict[str, Any] | None,
        request: dict[str, Any],
        observation: dict[str, Any],
        preliminary_failure_type: str | None = None,
        normalized_failure_type: str | None = None,
        classified_by: list[str] | None = None,
        operator_override: str | None = None,
        reuse_trace: dict[str, Any] | None = None,
    ) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO computer_trajectories (
                    action,
                    status,
                    final_surface,
                    attempts_json,
                    verification_json,
                    request_json,
                    observation_json,
                    preliminary_failure_type,
                    normalized_failure_type,
                    classified_by_json,
                    operator_override,
                    reuse_trace_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action,
                    status,
                    final_surface,
                    json.dumps(attempts, ensure_ascii=True),
                    json.dumps(verification, ensure_ascii=True) if verification is not None else None,
                    json.dumps(request, ensure_ascii=True),
                    json.dumps(observation, ensure_ascii=True),
                    preliminary_failure_type,
                    normalized_failure_type,
                    json.dumps(classified_by or [], ensure_ascii=True),
                    operator_override,
                    json.dumps(reuse_trace or {}, ensure_ascii=True),
                    time.time(),
                ),
            )
            trajectory_id = cursor.lastrowid
            conn.commit()
        return int(trajectory_id)

    @staticmethod
    def _row_to_trajectory(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": row[0],
            "action": row[1],
            "status": row[2],
            "final_surface": row[3],
            "attempts": json.loads(row[4]),
            "verification": json.loads(row[5]) if row[5] else None,
            "request": json.loads(row[6]) if row[6] else {},
            "observation": json.loads(row[7]) if row[7] else {},
            "preliminary_failure_type": row[8],
            "normalized_failure_type": row[9],
            "failure_type": row[9] or row[8],
            "classified_by": json.loads(row[10]) if row[10] else [],
            "operator_override": row[11],
            "reuse_trace": json.loads(row[12]) if row[12] else {},
            "created_at": row[13],
        }

    def get(self, trajectory_id: int) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, action, status, final_surface, attempts_json, verification_json,
                       request_json, observation_json, preliminary_failure_type,
                       normalized_failure_type, classified_by_json, operator_override, reuse_trace_json, created_at
                FROM computer_trajectories
                WHERE id = ?
                """,
                (trajectory_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_trajectory(row)

    def recent(self, *, status: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    """
                    SELECT id, action, status, final_surface, attempts_json, verification_json,
                           request_json, observation_json, preliminary_failure_type,
                           normalized_failure_type, classified_by_json, operator_override, reuse_trace_json, created_at
                    FROM computer_trajectories
                    WHERE status = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (status, limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, action, status, final_surface, attempts_json, verification_json,
                           request_json, observation_json, preliminary_failure_type,
                           normalized_failure_type, classified_by_json, operator_override, reuse_trace_json, created_at
                    FROM computer_trajectories
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = cursor.fetchall()
        return [self._row_to_trajectory(row) for row in rows]

    def update_failure_classification(
        self,
        trajectory_id: int,
        *,
        preliminary_failure_type: str | None,
        normalized_failure_type: str | None,
        classified_by: list[str] | None = None,
        operator_override: str | None = None,
    ) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE computer_trajectories
                SET preliminary_failure_type = ?,
                    normalized_failure_type = ?,
                    classified_by_json = ?,
                    operator_override = ?
                WHERE id = ?
                """,
                (
                    preliminary_failure_type,
                    normalized_failure_type,
                    json.dumps(classified_by or [], ensure_ascii=True),
                    operator_override,
                    trajectory_id,
                ),
            )
            conn.commit()
        return cursor.rowcount > 0

    def update_reuse_trace(
        self,
        trajectory_id: int,
        *,
        reuse_trace: dict[str, Any] | None,
    ) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE computer_trajectories
                SET reuse_trace_json = ?
                WHERE id = ?
                """,
                (
                    json.dumps(reuse_trace or {}, ensure_ascii=True),
                    trajectory_id,
                ),
            )
            conn.commit()
        return cursor.rowcount > 0


_trajectory_store: ComputerTrajectoryStore | None = None


def get_computer_trajectory_store() -> ComputerTrajectoryStore:
    global _trajectory_store
    if _trajectory_store is None:
        settings = get_settings()
        _trajectory_store = ComputerTrajectoryStore(
            db_path=str(settings.computer_trajectory_db_path),
        )
    return _trajectory_store


def reset_computer_trajectory_store() -> None:
    global _trajectory_store
    _trajectory_store = None
