"""Persistent task objects for background and long-running agent workflows."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from src.config.settings import get_settings


# Public sentinel for "leave the current field unchanged" update calls.
TASK_STORE_UNSET = object()
_UNSET = TASK_STORE_UNSET
_SEARCH_FRAGMENT_CHAR_LIMIT = 4096
_SEARCH_FRAGMENT_TOKEN_LIMIT = 256
_SEARCH_FRAGMENT_DEPTH_LIMIT = 6


def _merge_json(base: Any, updates: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(updates, dict):
        return updates
    merged = dict(base)
    for key, value in updates.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_json(merged[key], value)
        else:
            merged[key] = value
    return merged


class TaskStore:
    """SQLite-backed task store for persistent workflow objects."""

    def __init__(self, db_path: str = "data/tasks.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fts_enabled = False
        self._notifier: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner_session_id TEXT,
                    owner_user_id TEXT,
                    parent_task_id TEXT,
                    run_id TEXT,
                    winner_task_id TEXT,
                    loser_task_ids_json TEXT,
                    approval_dependencies_json TEXT,
                    artifacts_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    artifacts_search_text TEXT NOT NULL DEFAULT '',
                    metadata_search_text TEXT NOT NULL DEFAULT '',
                    search_text TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    ended_at REAL
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_owner_created_at
                ON tasks(owner_session_id, created_at DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_kind_status
                ON tasks(kind, status, updated_at DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_parent
                ON tasks(parent_task_id, created_at DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_run_id
                ON tasks(run_id)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    owner_session_id TEXT,
                    owner_user_id TEXT,
                    run_id TEXT,
                    timestamp REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT,
                    title TEXT,
                    error TEXT,
                    payload_json TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_task_events_task_ts
                ON task_events(task_id, timestamp DESC, event_id DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_task_events_session_ts
                ON task_events(owner_session_id, timestamp DESC, event_id DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_task_events_run_id
                ON task_events(run_id)
                """
            )
            try:
                cursor.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS tasks_search
                    USING fts5(task_id UNINDEXED, search_text)
                    """
                )
                self._fts_enabled = True
            except sqlite3.OperationalError:
                self._fts_enabled = False

            columns = {
                row[1]
                for row in cursor.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "search_text" not in columns:
                cursor.execute(
                    "ALTER TABLE tasks ADD COLUMN search_text TEXT NOT NULL DEFAULT ''"
                )
            if "artifacts_search_text" not in columns:
                cursor.execute(
                    """
                    ALTER TABLE tasks
                    ADD COLUMN artifacts_search_text TEXT NOT NULL DEFAULT ''
                    """
                )
            if "metadata_search_text" not in columns:
                cursor.execute(
                    """
                    ALTER TABLE tasks
                    ADD COLUMN metadata_search_text TEXT NOT NULL DEFAULT ''
                    """
                )

            if self._should_rebuild_search_index(cursor):
                self._rebuild_search_index(cursor)
            conn.commit()

    def _should_rebuild_search_index(self, cursor: sqlite3.Cursor) -> bool:
        cursor.execute("SELECT COUNT(*) FROM tasks")
        total_tasks = int(cursor.fetchone()[0] or 0)
        if total_tasks <= 0:
            return False
        cursor.execute("SELECT COUNT(*) FROM tasks WHERE search_text = ''")
        missing_search_text = int(cursor.fetchone()[0] or 0)
        if missing_search_text > 0:
            return True
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM tasks
            WHERE artifacts_search_text = ''
              AND COALESCE(artifacts_json, '{}') NOT IN ('', '{}', 'null')
            """
        )
        missing_artifacts_search_text = int(cursor.fetchone()[0] or 0)
        if missing_artifacts_search_text > 0:
            return True
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM tasks
            WHERE metadata_search_text = ''
              AND COALESCE(metadata_json, '{}') NOT IN ('', '{}', 'null')
            """
        )
        missing_metadata_search_text = int(cursor.fetchone()[0] or 0)
        if missing_metadata_search_text > 0:
            return True
        if not self._fts_enabled:
            return False
        try:
            cursor.execute("SELECT COUNT(*) FROM tasks_search")
            indexed_rows = int(cursor.fetchone()[0] or 0)
        except sqlite3.OperationalError:
            return True
        return indexed_rows != total_tasks

    def _rebuild_search_index(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute(
            """
            SELECT task_id, kind, title, status, owner_session_id, owner_user_id,
                   parent_task_id, run_id, winner_task_id, loser_task_ids_json,
                   approval_dependencies_json, artifacts_json, metadata_json,
                   artifacts_search_text, metadata_search_text, error, created_at,
                   updated_at, started_at, ended_at
            FROM tasks
            """
        )
        rows = cursor.fetchall()
        if self._fts_enabled:
            cursor.execute("DELETE FROM tasks_search")
        for row in rows:
            task = self._row_to_task(row)
            (
                artifacts_search_text,
                metadata_search_text,
                search_text,
            ) = self._task_search_document(task)
            cursor.execute(
                """
                UPDATE tasks
                SET artifacts_search_text = ?,
                    metadata_search_text = ?,
                    search_text = ?
                WHERE task_id = ?
                """,
                (
                    artifacts_search_text,
                    metadata_search_text,
                    search_text,
                    task["task_id"],
                ),
            )
            if self._fts_enabled:
                cursor.execute(
                    "INSERT INTO tasks_search(task_id, search_text) VALUES (?, ?)",
                    (task["task_id"], search_text),
                )

    def set_notifier(
        self,
        notifier: Optional[Callable[[dict[str, Any]], Awaitable[None]]],
    ) -> None:
        self._notifier = notifier

    @staticmethod
    def _truncate_search_text(text: str, limit: int = _SEARCH_FRAGMENT_CHAR_LIMIT) -> str:
        compact = re.sub(r"\s+", " ", text.strip())
        return compact[:limit]

    @classmethod
    def _search_fragment(
        cls,
        value: Any,
        *,
        char_limit: int = _SEARCH_FRAGMENT_CHAR_LIMIT,
        token_limit: int = _SEARCH_FRAGMENT_TOKEN_LIMIT,
    ) -> str:
        tokens: list[str] = []
        char_budget = max(0, int(char_limit))

        def _append(token: Any) -> bool:
            nonlocal char_budget
            if len(tokens) >= token_limit or char_budget <= 0:
                return False
            text = cls._truncate_search_text(str(token), limit=char_budget)
            if not text:
                return True
            lowered = text.lower()
            tokens.append(lowered)
            char_budget -= len(lowered) + 1
            return char_budget > 0 and len(tokens) < token_limit

        def _walk(node: Any, depth: int) -> None:
            if len(tokens) >= token_limit or char_budget <= 0 or depth > _SEARCH_FRAGMENT_DEPTH_LIMIT:
                return
            if node is None or isinstance(node, bool):
                return
            if isinstance(node, dict):
                for key, child in node.items():
                    if not _append(key):
                        return
                    _walk(child, depth + 1)
                    if len(tokens) >= token_limit or char_budget <= 0:
                        return
                return
            if isinstance(node, (list, tuple, set)):
                for child in node:
                    _walk(child, depth + 1)
                    if len(tokens) >= token_limit or char_budget <= 0:
                        return
                return
            _append(node)

        _walk(value, 0)
        return " ".join(tokens)

    @classmethod
    def _task_search_document(cls, task: dict[str, Any]) -> tuple[str, str, str]:
        artifacts_search_text = cls._search_fragment(task.get("artifacts") or {})
        metadata_search_text = cls._search_fragment(task.get("metadata") or {})
        parts = [
            task.get("task_id"),
            task.get("kind"),
            task.get("title"),
            task.get("status"),
            task.get("owner_session_id"),
            task.get("owner_user_id"),
            task.get("parent_task_id"),
            task.get("run_id"),
            task.get("winner_task_id"),
            " ".join(task.get("loser_task_ids") or []),
            " ".join(task.get("approval_dependencies") or []),
            task.get("error"),
            artifacts_search_text,
            metadata_search_text,
        ]
        search_text = " ".join(str(part).strip() for part in parts if part).lower()
        return (
            artifacts_search_text,
            metadata_search_text,
            cls._truncate_search_text(search_text),
        )

    @classmethod
    def _task_search_text(cls, task: dict[str, Any]) -> str:
        return cls._task_search_document(task)[2]

    def _sync_task_search(
        self,
        cursor: sqlite3.Cursor,
        *,
        task_id: str,
        search_text: str,
    ) -> None:
        if not self._fts_enabled:
            return
        cursor.execute("DELETE FROM tasks_search WHERE task_id = ?", (task_id,))
        cursor.execute(
            "INSERT INTO tasks_search(task_id, search_text) VALUES (?, ?)",
            (task_id, search_text),
        )

    @staticmethod
    def _task_event_payload(
        *,
        previous: Optional[dict[str, Any]],
        current: dict[str, Any],
        artifacts_patch: Optional[dict[str, Any]] = None,
        metadata_patch: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": current["task_id"],
            "status": current.get("status"),
            "title": current.get("title"),
            "error": current.get("error"),
        }
        changes: dict[str, Any] = {}
        if previous is None:
            changes["created"] = True
        else:
            for field in (
                "status",
                "title",
                "error",
                "run_id",
                "winner_task_id",
                "loser_task_ids",
                "approval_dependencies",
            ):
                if previous.get(field) != current.get(field):
                    changes[field] = {
                        "before": previous.get(field),
                        "after": current.get(field),
                    }
        if artifacts_patch:
            changes["artifacts"] = artifacts_patch
        if metadata_patch:
            changes["metadata"] = metadata_patch
        payload["changes"] = changes
        return payload

    @staticmethod
    def _task_event_type(
        *,
        previous: Optional[dict[str, Any]],
        current: dict[str, Any],
    ) -> str:
        if previous is None:
            return "created"
        if previous.get("status") != current.get("status"):
            return "status_changed"
        return "updated"

    @staticmethod
    def _row_to_task_event(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "entry_id": f"taskevt-{row[0]}",
            "event_id": row[0],
            "task_id": row[1],
            "owner_session_id": row[2],
            "owner_user_id": row[3],
            "run_id": row[4],
            "timestamp": row[5],
            "event_type": row[6],
            "status": row[7],
            "title": row[8],
            "error": row[9],
            "payload": json.loads(row[10]) if row[10] else {},
        }

    def _append_task_event(
        self,
        cursor: sqlite3.Cursor,
        *,
        current: dict[str, Any],
        previous: Optional[dict[str, Any]] = None,
        artifacts_patch: Optional[dict[str, Any]] = None,
        metadata_patch: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload = self._task_event_payload(
            previous=previous,
            current=current,
            artifacts_patch=artifacts_patch,
            metadata_patch=metadata_patch,
        )
        event_type = self._task_event_type(previous=previous, current=current)
        ts = time.time()
        cursor.execute(
            """
            INSERT INTO task_events (
                task_id,
                owner_session_id,
                owner_user_id,
                run_id,
                timestamp,
                event_type,
                status,
                title,
                error,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                current["task_id"],
                current.get("owner_session_id"),
                current.get("owner_user_id"),
                current.get("run_id"),
                ts,
                event_type,
                current.get("status"),
                current.get("title"),
                current.get("error"),
                json.dumps(payload, ensure_ascii=True),
            ),
        )
        return {
            "event": self._row_to_task_event(
                (
                    cursor.lastrowid,
                    current["task_id"],
                    current.get("owner_session_id"),
                    current.get("owner_user_id"),
                    current.get("run_id"),
                    ts,
                    event_type,
                    current.get("status"),
                    current.get("title"),
                    current.get("error"),
                    json.dumps(payload, ensure_ascii=True),
                )
            ),
            "task": current,
        }

    def append_event(
        self,
        task_id: str,
        *,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        status: Optional[str] = None,
        title: Optional[str] = None,
        error: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> dict[str, Any] | None:
        current = self.get(task_id)
        if current is None:
            return None
        resolved_status = status if status is not None else current.get("status")
        resolved_title = title if title is not None else current.get("title")
        resolved_error = error if error is not None else current.get("error")
        payload_json = dict(payload or {})
        ts = float(timestamp or time.time())
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO task_events (
                    task_id,
                    owner_session_id,
                    owner_user_id,
                    run_id,
                    timestamp,
                    event_type,
                    status,
                    title,
                    error,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    current.get("owner_session_id"),
                    current.get("owner_user_id"),
                    current.get("run_id"),
                    ts,
                    event_type,
                    resolved_status,
                    resolved_title,
                    resolved_error,
                    json.dumps(payload_json, ensure_ascii=True),
                ),
            )
            event = self._row_to_task_event(
                (
                    cursor.lastrowid,
                    task_id,
                    current.get("owner_session_id"),
                    current.get("owner_user_id"),
                    current.get("run_id"),
                    ts,
                    event_type,
                    resolved_status,
                    resolved_title,
                    resolved_error,
                    json.dumps(payload_json, ensure_ascii=True),
                )
            )
            conn.commit()
        event_payload = {"event": event, "task": current}
        self._notify(event_payload)
        return event

    def _notify(self, payload: dict[str, Any]) -> None:
        if self._notifier is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._notifier(payload))

    @staticmethod
    def _task_payload(
        *,
        task_id: str,
        kind: str,
        title: str,
        status: str,
        owner_session_id: Optional[str],
        owner_user_id: Optional[str],
        parent_task_id: Optional[str],
        run_id: Optional[str],
        winner_task_id: Optional[str],
        loser_task_ids: list[str],
        approval_dependencies: list[str],
        artifacts: dict[str, Any],
        metadata: dict[str, Any],
        error: Optional[str],
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "kind": kind,
            "title": title,
            "status": status,
            "owner_session_id": owner_session_id,
            "owner_user_id": owner_user_id,
            "parent_task_id": parent_task_id,
            "run_id": run_id,
            "winner_task_id": winner_task_id,
            "loser_task_ids": loser_task_ids,
            "approval_dependencies": approval_dependencies,
            "artifacts": artifacts,
            "metadata": metadata,
            "error": error,
        }

    @staticmethod
    def _open_status_filter(status: str) -> bool:
        return status.strip().lower() == "open"

    @staticmethod
    def _fts_query(query: str) -> str | None:
        tokens = [token.strip() for token in re.split(r"\s+", query.strip()) if token.strip()]
        if not tokens:
            return None
        quoted_tokens = []
        for token in tokens:
            escaped = token.replace('"', '""')
            quoted_tokens.append(f'"{escaped}"')
        return " AND ".join(quoted_tokens)

    @staticmethod
    def _row_to_task(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "task_id": row[0],
            "kind": row[1],
            "title": row[2],
            "status": row[3],
            "owner_session_id": row[4],
            "owner_user_id": row[5],
            "parent_task_id": row[6],
            "run_id": row[7],
            "winner_task_id": row[8],
            "loser_task_ids": json.loads(row[9]) if row[9] else [],
            "approval_dependencies": json.loads(row[10]) if row[10] else [],
            "artifacts": json.loads(row[11]) if row[11] else {},
            "metadata": json.loads(row[12]) if row[12] else {},
            "error": row[15],
            "created_at": row[16],
            "updated_at": row[17],
            "started_at": row[18],
            "ended_at": row[19],
        }

    def create(
        self,
        *,
        kind: str,
        title: str,
        status: str = "pending",
        owner_session_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        run_id: Optional[str] = None,
        winner_task_id: Optional[str] = None,
        loser_task_ids: Optional[list[str]] = None,
        approval_dependencies: Optional[list[str]] = None,
        artifacts: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        now = time.time()
        resolved_task_id = (task_id or f"task_{uuid.uuid4().hex[:12]}").strip()
        started_at = now if status in {"accepted", "running", "idle"} else None
        ended_at = now if status in {"completed", "failed", "cancelled", "expired"} else None
        loser_items = loser_task_ids or []
        approval_items = approval_dependencies or []
        artifacts_payload = artifacts or {}
        metadata_payload = metadata or {}
        task_payload = self._task_payload(
            task_id=resolved_task_id,
            kind=kind,
            title=title,
            status=status,
            owner_session_id=owner_session_id,
            owner_user_id=owner_user_id,
            parent_task_id=parent_task_id,
            run_id=run_id,
            winner_task_id=winner_task_id,
            loser_task_ids=loser_items,
            approval_dependencies=approval_items,
            artifacts=artifacts_payload,
            metadata=metadata_payload,
            error=error,
        )
        (
            artifacts_search_text,
            metadata_search_text,
            search_text,
        ) = self._task_search_document(task_payload)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO tasks (
                    task_id,
                    kind,
                    title,
                    status,
                    owner_session_id,
                    owner_user_id,
                    parent_task_id,
                    run_id,
                    winner_task_id,
                    loser_task_ids_json,
                    approval_dependencies_json,
                    artifacts_json,
                    metadata_json,
                    artifacts_search_text,
                    metadata_search_text,
                    search_text,
                    error,
                    created_at,
                    updated_at,
                    started_at,
                    ended_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_task_id,
                    kind,
                    title,
                    status,
                    owner_session_id,
                    owner_user_id,
                    parent_task_id,
                    run_id,
                    winner_task_id,
                    json.dumps(loser_items, ensure_ascii=True),
                    json.dumps(approval_items, ensure_ascii=True),
                    json.dumps(artifacts_payload, ensure_ascii=True),
                    json.dumps(metadata_payload, ensure_ascii=True),
                    artifacts_search_text,
                    metadata_search_text,
                    search_text,
                    error,
                    now,
                    now,
                    started_at,
                    ended_at,
                ),
            )
            self._sync_task_search(
                cursor,
                task_id=resolved_task_id,
                search_text=search_text,
            )
            current_task = {
                **task_payload,
                "created_at": now,
                "updated_at": now,
                "started_at": started_at,
                "ended_at": ended_at,
            }
            event_payload = self._append_task_event(
                cursor,
                current=current_task,
                previous=None,
                artifacts_patch=artifacts_payload,
                metadata_patch=metadata_payload,
            )
            conn.commit()
        self._notify(event_payload)
        return current_task

    def get(self, task_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT task_id, kind, title, status, owner_session_id, owner_user_id,
                   parent_task_id, run_id, winner_task_id, loser_task_ids_json,
                   approval_dependencies_json, artifacts_json, metadata_json,
                   artifacts_search_text, metadata_search_text, error, created_at,
                   updated_at, started_at, ended_at
                FROM tasks
                WHERE task_id = ?
                """,
                (task_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    def get_by_run_id(self, run_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT task_id, kind, title, status, owner_session_id, owner_user_id,
                   parent_task_id, run_id, winner_task_id, loser_task_ids_json,
                   approval_dependencies_json, artifacts_json, metadata_json,
                   artifacts_search_text, metadata_search_text, error, created_at,
                   updated_at, started_at, ended_at
                FROM tasks
                WHERE run_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    def update(
        self,
        task_id: str,
        *,
        status: Optional[str] = None,
        title: Optional[str] = None,
        artifacts: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        error: Any = _UNSET,
        run_id: Optional[str] = None,
        winner_task_id: Optional[str] = None,
        loser_task_ids: Optional[list[str]] = None,
        approval_dependencies: Optional[list[str]] = None,
        ended_at: Optional[float] = None,
    ) -> dict[str, Any] | None:
        current = self.get(task_id)
        if current is None:
            return None

        next_status = status or current["status"]
        next_artifacts = current["artifacts"]
        if artifacts:
            next_artifacts = _merge_json(next_artifacts, artifacts)
        next_metadata = current["metadata"]
        if metadata:
            next_metadata = _merge_json(next_metadata, metadata)

        started_at = current["started_at"]
        if started_at is None and next_status in {"accepted", "running", "idle"}:
            started_at = time.time()

        resolved_ended_at = current["ended_at"]
        if ended_at is not None:
            resolved_ended_at = ended_at
        elif next_status in {"completed", "failed", "cancelled", "expired"}:
            resolved_ended_at = time.time()

        next_task = {
            "task_id": task_id,
            "kind": current["kind"],
            "title": title or current["title"],
            "status": next_status,
            "owner_session_id": current["owner_session_id"],
            "owner_user_id": current["owner_user_id"],
            "parent_task_id": current["parent_task_id"],
            "run_id": run_id if run_id is not None else current["run_id"],
            "winner_task_id": winner_task_id if winner_task_id is not None else current["winner_task_id"],
            "loser_task_ids": loser_task_ids if loser_task_ids is not None else current["loser_task_ids"],
            "approval_dependencies": approval_dependencies if approval_dependencies is not None else current["approval_dependencies"],
            "artifacts": next_artifacts,
            "metadata": next_metadata,
            "error": current["error"] if error is _UNSET else error,
        }
        (
            artifacts_search_text,
            metadata_search_text,
            search_text,
        ) = self._task_search_document(next_task)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE tasks
                SET title = ?,
                    status = ?,
                    run_id = ?,
                    winner_task_id = ?,
                    loser_task_ids_json = ?,
                    approval_dependencies_json = ?,
                    artifacts_json = ?,
                    metadata_json = ?,
                    artifacts_search_text = ?,
                    metadata_search_text = ?,
                    search_text = ?,
                    error = ?,
                    updated_at = ?,
                    started_at = ?,
                    ended_at = ?
                WHERE task_id = ?
                """,
                (
                    title or current["title"],
                    next_status,
                    run_id if run_id is not None else current["run_id"],
                    winner_task_id if winner_task_id is not None else current["winner_task_id"],
                    json.dumps(
                        loser_task_ids if loser_task_ids is not None else current["loser_task_ids"],
                        ensure_ascii=True,
                    ),
                    json.dumps(
                        approval_dependencies
                        if approval_dependencies is not None
                        else current["approval_dependencies"],
                        ensure_ascii=True,
                    ),
                    json.dumps(next_artifacts, ensure_ascii=True),
                    json.dumps(next_metadata, ensure_ascii=True),
                    artifacts_search_text,
                    metadata_search_text,
                    search_text,
                    current["error"] if error is _UNSET else error,
                    time.time(),
                    started_at,
                    resolved_ended_at,
                    task_id,
                ),
            )
            self._sync_task_search(
                cursor,
                task_id=task_id,
                search_text=search_text,
            )
            updated_task = {
                **next_task,
                "created_at": current["created_at"],
                "updated_at": time.time(),
                "started_at": started_at,
                "ended_at": resolved_ended_at,
            }
            event_payload = self._append_task_event(
                cursor,
                current=updated_task,
                previous=current,
                artifacts_patch=artifacts,
                metadata_patch=metadata,
            )
            conn.commit()
        self._notify(event_payload)
        return updated_task

    def aggregate_status_counts(
        self,
        *,
        kind: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ) -> dict[str, int]:
        conditions: list[str] = []
        params: list[Any] = []
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        if owner_user_id:
            conditions.append("owner_user_id = ?")
            params.append(owner_user_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT status, COUNT(*) FROM tasks {where} GROUP BY status",
                tuple(params),
            )
            return {row[0]: row[1] for row in cursor.fetchall()}

    def aggregate_replay_counts(
        self,
        *,
        kind: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ) -> dict[str, int]:
        conditions = [
            "json_extract(metadata_json, '$.replay_of_task_id') IS NOT NULL",
            "json_extract(metadata_json, '$.replay_of_task_id') != ''",
        ]
        params: list[Any] = []
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        if owner_user_id:
            conditions.append("owner_user_id = ?")
            params.append(owner_user_id)
        where = f"WHERE {' AND '.join(conditions)}"
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT status, COUNT(*) FROM tasks {where} GROUP BY status",
                tuple(params),
            )
            return {row[0]: row[1] for row in cursor.fetchall()}

    def query_replay_tasks(
        self,
        *,
        kind: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        conditions = [
            "json_extract(metadata_json, '$.replay_of_task_id') IS NOT NULL",
            "json_extract(metadata_json, '$.replay_of_task_id') != ''",
        ]
        params: list[Any] = []
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        if owner_user_id:
            conditions.append("owner_user_id = ?")
            params.append(owner_user_id)
        where = f"WHERE {' AND '.join(conditions)}"
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT task_id, kind, title, status, owner_session_id, owner_user_id,
                       parent_task_id, run_id, winner_task_id, loser_task_ids_json,
                       approval_dependencies_json, artifacts_json, metadata_json,
                       artifacts_search_text, metadata_search_text, error, created_at,
                       updated_at, started_at, ended_at
                FROM tasks
                {where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (*params, limit),
            )
            rows = cursor.fetchall()
        return [self._row_to_task(row) for row in rows]

    def count_step_events(
        self,
        *,
        owner_user_id: Optional[str] = None,
        kind: Optional[str] = None,
        event_type_prefix: str = "step_",
    ) -> int:
        conditions = ["event_type LIKE ?"]
        params: list[Any] = [f"{event_type_prefix}%"]
        if owner_user_id:
            conditions.append("owner_user_id = ?")
            params.append(owner_user_id)
        if kind:
            conditions.append(
                "task_id IN (SELECT task_id FROM tasks WHERE kind = ?)"
            )
            params.append(kind)
        where = f"WHERE {' AND '.join(conditions)}"
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT COUNT(*) FROM task_events {where}",
                tuple(params),
            )
            return int(cursor.fetchone()[0] or 0)

    def query_step_events(
        self,
        *,
        owner_user_id: Optional[str] = None,
        kind: Optional[str] = None,
        event_type_prefix: str = "step_",
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        conditions = ["event_type LIKE ?"]
        params: list[Any] = [f"{event_type_prefix}%"]
        if owner_user_id:
            conditions.append("owner_user_id = ?")
            params.append(owner_user_id)
        if kind:
            conditions.append(
                "task_id IN (SELECT task_id FROM tasks WHERE kind = ?)"
            )
            params.append(kind)
        where = f"WHERE {' AND '.join(conditions)}"
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT event_id, task_id, owner_session_id, owner_user_id, run_id,
                       timestamp, event_type, status, title, error, payload_json
                FROM task_events
                {where}
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (*params, limit),
            )
            rows = cursor.fetchall()
        return [self._row_to_task_event(row) for row in rows]

    def query_timeline(
        self,
        task_id: str,
        *,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        resolved_page = max(1, int(page or 1))
        resolved_page_size = max(1, min(int(page_size or 100), 500))
        offset = (resolved_page - 1) * resolved_page_size
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
                (task_id,),
            )
            total = int(cursor.fetchone()[0] or 0)
            cursor.execute(
                """
                SELECT event_id, task_id, owner_session_id, owner_user_id, run_id,
                       timestamp, event_type, status, title, error, payload_json
                FROM task_events
                WHERE task_id = ?
                ORDER BY timestamp DESC, event_id DESC
                LIMIT ? OFFSET ?
                """,
                (task_id, resolved_page_size, offset),
            )
            rows = cursor.fetchall()
        events = [self._row_to_task_event(row) for row in rows]
        return {
            "events": events,
            "pagination": {
                "page": resolved_page,
                "page_size": resolved_page_size,
                "total": total,
                "has_more": offset + len(events) < total,
            },
        }

    def query(
        self,
        *,
        owner_session_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        q: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        resolved_page = max(1, int(page or 1))
        resolved_page_size = max(1, min(int(page_size or 20), 100))
        conditions: list[str] = []
        params: list[Any] = []
        if owner_session_id:
            conditions.append("owner_session_id = ?")
            params.append(owner_session_id)
        if owner_user_id:
            conditions.append("owner_user_id = ?")
            params.append(owner_user_id)
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        if status:
            if self._open_status_filter(status):
                conditions.append(
                    "status NOT IN ('completed', 'failed', 'cancelled', 'expired')"
                )
            else:
                conditions.append("status = ?")
                params.append(status)
        if parent_task_id:
            conditions.append("parent_task_id = ?")
            params.append(parent_task_id)

        query_text = (q or "").strip()
        fts_query = self._fts_query(query_text) if query_text else None
        if query_text:
            if self._fts_enabled and fts_query:
                conditions.append(
                    "task_id IN (SELECT task_id FROM tasks_search WHERE tasks_search MATCH ?)"
                )
                params.append(fts_query)
            else:
                conditions.append("search_text LIKE ?")
                params.append(f"%{query_text.lower()}%")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        offset = (resolved_page - 1) * resolved_page_size
        select_query = f"""
            SELECT task_id, kind, title, status, owner_session_id, owner_user_id,
                   parent_task_id, run_id, winner_task_id, loser_task_ids_json,
                   approval_dependencies_json, artifacts_json, metadata_json,
                   artifacts_search_text, metadata_search_text, error, created_at,
                   updated_at, started_at, ended_at
            FROM tasks
            {where}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ? OFFSET ?
        """
        count_query = f"SELECT COUNT(*) FROM tasks {where}"

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(count_query, tuple(params))
            total = int(cursor.fetchone()[0] or 0)
            cursor.execute(select_query, tuple([*params, resolved_page_size, offset]))
            rows = cursor.fetchall()

        tasks = [self._row_to_task(row) for row in rows]
        return {
            "tasks": tasks,
            "pagination": {
                "page": resolved_page,
                "page_size": resolved_page_size,
                "total": total,
                "has_more": offset + len(tasks) < total,
            },
            "filters": {
                "owner_session_id": owner_session_id,
                "owner_user_id": owner_user_id,
                "kind": kind,
                "status": status,
                "parent_task_id": parent_task_id,
                "q": query_text,
            },
        }

    def list(
        self,
        *,
        owner_session_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        result = self.query(
            owner_session_id=owner_session_id,
            owner_user_id=owner_user_id,
            kind=kind,
            status=status,
            parent_task_id=parent_task_id,
            page=1,
            page_size=limit,
        )
        return result["tasks"]

    def clear(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM task_events")
            if self._fts_enabled:
                conn.execute("DELETE FROM tasks_search")
            conn.commit()


_task_store: TaskStore | None = None


def get_task_store() -> TaskStore:
    global _task_store
    if _task_store is None:
        settings = get_settings()
        _task_store = TaskStore(db_path=str(settings.task_store_db_path))
    return _task_store


def reset_task_store() -> None:
    global _task_store
    _task_store = None
