"""
監査ログシステム
OpenClaw のセキュリティ監査機能を参考
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import Enum
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from src.config.settings import get_settings


class AuditEventType(Enum):
    """監査イベントタイプ"""

    SHELL_COMMAND = "shell_command"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    WEB_SEARCH = "web_search"
    BROWSER_NAVIGATE = "browser_navigate"
    DESKTOP_VIEW = "desktop_view"
    DESKTOP_CONTROL = "desktop_control"
    PHYSICAL_AI = "physical_ai"
    MEMORY_STORE = "memory_store"
    MEMORY_SEARCH = "memory_search"
    AGENT_MESSAGE = "agent_message"
    CHANNEL_MESSAGE = "channel_message"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    TOOL_APPROVAL = "tool_approval"
    ERROR = "error"


class AuditLogger:
    """JSONL + SQLite-backed audit logger."""

    def __init__(self, log_path: str = "data/audit.log"):
        self.log_path = Path(log_path)
        suffix = f"{self.log_path.suffix}.sqlite3" if self.log_path.suffix else ".sqlite3"
        self.db_path = self.log_path.with_suffix(suffix)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fts_enabled = False
        self._notifier: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_entries (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    line_no INTEGER UNIQUE,
                    timestamp REAL NOT NULL,
                    datetime TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    user_id TEXT,
                    session_id TEXT,
                    action TEXT,
                    resource TEXT,
                    result TEXT,
                    tool_name TEXT,
                    tool_pattern TEXT,
                    source TEXT,
                    actor_user_id TEXT,
                    target_session_id TEXT,
                    task_id TEXT,
                    run_id TEXT,
                    request_id TEXT,
                    source_request_id TEXT,
                    metadata_json TEXT NOT NULL,
                    tool_text TEXT NOT NULL DEFAULT '',
                    search_text TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_entries_ts
                ON audit_entries(timestamp DESC, entry_id DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_entries_session
                ON audit_entries(session_id, timestamp DESC, entry_id DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_entries_target_session
                ON audit_entries(target_session_id, timestamp DESC, entry_id DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_entries_actor
                ON audit_entries(actor_user_id, timestamp DESC, entry_id DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_entries_event_type
                ON audit_entries(event_type, timestamp DESC, entry_id DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_entries_tool_name
                ON audit_entries(tool_name, timestamp DESC, entry_id DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_entries_source
                ON audit_entries(source, timestamp DESC, entry_id DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_entries_task_id
                ON audit_entries(task_id, timestamp DESC, entry_id DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_entries_run_id
                ON audit_entries(run_id, timestamp DESC, entry_id DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_entries_request_id
                ON audit_entries(request_id, timestamp DESC, entry_id DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_entries_source_request_id
                ON audit_entries(source_request_id, timestamp DESC, entry_id DESC)
                """
            )
            try:
                cursor.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS audit_search
                    USING fts5(entry_id UNINDEXED, search_text)
                    """
                )
                self._fts_enabled = True
            except sqlite3.OperationalError:
                self._fts_enabled = False
            self._sync_log_to_db(cursor)
            conn.commit()

    def set_notifier(
        self,
        notifier: Optional[Callable[[Dict[str, Any]], Awaitable[None]]],
    ) -> None:
        self._notifier = notifier

    def _notify(self, payload: Dict[str, Any]) -> None:
        if self._notifier is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._notifier(payload))

    @staticmethod
    def _normalize_metadata(metadata: Any) -> dict[str, Any]:
        return metadata if isinstance(metadata, dict) else {}

    @staticmethod
    def _search_text(entry: Dict[str, Any]) -> str:
        metadata = AuditLogger._normalize_metadata(entry.get("metadata"))
        parts = [
            entry.get("event_type"),
            entry.get("user_id"),
            entry.get("session_id"),
            entry.get("action"),
            entry.get("resource"),
            entry.get("result"),
            metadata.get("tool_name"),
            metadata.get("tool_pattern"),
            metadata.get("source"),
            metadata.get("actor_user_id"),
            metadata.get("target_session_id"),
            metadata.get("task_id"),
            metadata.get("run_id"),
            metadata.get("request_id"),
            metadata.get("source_request_id"),
        ]
        if metadata:
            parts.append(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
        return " ".join(str(part).strip() for part in parts if part).lower()

    @staticmethod
    def _tool_text(entry: Dict[str, Any]) -> str:
        metadata = AuditLogger._normalize_metadata(entry.get("metadata"))
        parts = [
            metadata.get("tool_name"),
            metadata.get("tool_pattern"),
            entry.get("action"),
            entry.get("resource"),
        ]
        return " ".join(str(part).strip() for part in parts if part).lower()

    @staticmethod
    def _fts_query(query: str) -> str | None:
        tokens = [token.strip() for token in query.split() if token.strip()]
        if not tokens:
            return None
        return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)

    @staticmethod
    def _row_to_entry(row: tuple[Any, ...]) -> Dict[str, Any]:
        return {
            "entry_id": f"audit-{row[0]}",
            "timestamp": row[2],
            "datetime": row[3],
            "event_type": row[4],
            "user_id": row[5],
            "session_id": row[6],
            "action": row[7],
            "resource": row[8],
            "result": row[9],
            "metadata": json.loads(row[18]) if row[18] else {},
        }

    @staticmethod
    def _entry_filters_payload(
        *,
        actor_user_id: str,
        session_id: str,
        tool: str,
        source: str,
        result: str,
        q: str,
    ) -> dict[str, Any]:
        return {
            "actor_user_id": actor_user_id,
            "session_id": session_id,
            "tool": tool,
            "source": source,
            "result": result,
            "q": q,
        }

    @staticmethod
    def _meta_get(cursor: sqlite3.Cursor, key: str, default: str = "0") -> str:
        cursor.execute("SELECT value FROM audit_meta WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row is None:
            return default
        return str(row[0])

    @staticmethod
    def _meta_set(cursor: sqlite3.Cursor, key: str, value: Any) -> None:
        cursor.execute(
            """
            INSERT INTO audit_meta(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )

    def _insert_entry(
        self,
        cursor: sqlite3.Cursor,
        entry: Dict[str, Any],
        *,
        line_no: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        metadata = self._normalize_metadata(entry.get("metadata"))
        tool_text = self._tool_text(entry)
        search_text = self._search_text(entry)
        cursor.execute(
            """
            INSERT OR IGNORE INTO audit_entries (
                line_no,
                timestamp,
                datetime,
                event_type,
                user_id,
                session_id,
                action,
                resource,
                result,
                tool_name,
                tool_pattern,
                source,
                actor_user_id,
                target_session_id,
                task_id,
                run_id,
                request_id,
                source_request_id,
                metadata_json,
                tool_text,
                search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                line_no,
                float(entry.get("timestamp") or time.time()),
                str(entry.get("datetime") or datetime.now().isoformat()),
                str(entry.get("event_type") or ""),
                entry.get("user_id"),
                entry.get("session_id"),
                entry.get("action"),
                entry.get("resource"),
                entry.get("result"),
                metadata.get("tool_name"),
                metadata.get("tool_pattern"),
                metadata.get("source"),
                metadata.get("actor_user_id"),
                metadata.get("target_session_id"),
                metadata.get("task_id"),
                metadata.get("run_id"),
                metadata.get("request_id"),
                metadata.get("source_request_id"),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                tool_text,
                search_text,
            ),
        )
        if cursor.rowcount <= 0:
            return None
        row_id = int(cursor.lastrowid)
        if self._fts_enabled:
            cursor.execute(
                "INSERT INTO audit_search(entry_id, search_text) VALUES (?, ?)",
                (row_id, search_text),
            )
        return self._row_to_entry(
            (
                row_id,
                line_no,
                float(entry.get("timestamp") or time.time()),
                str(entry.get("datetime") or datetime.now().isoformat()),
                str(entry.get("event_type") or ""),
                entry.get("user_id"),
                entry.get("session_id"),
                entry.get("action"),
                entry.get("resource"),
                entry.get("result"),
                metadata.get("tool_name"),
                metadata.get("tool_pattern"),
                metadata.get("source"),
                metadata.get("actor_user_id"),
                metadata.get("target_session_id"),
                metadata.get("task_id"),
                metadata.get("run_id"),
                metadata.get("request_id"),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            )
        )

    def _sync_log_to_db(self, cursor: sqlite3.Cursor) -> None:
        if not self.log_path.exists():
            return
        last_line_no = int(self._meta_get(cursor, "last_line_no", "0") or 0)
        last_offset = int(self._meta_get(cursor, "last_offset", "0") or 0)
        new_last_line_no = last_line_no
        new_last_offset = last_offset
        with open(self.log_path, "r", encoding="utf-8") as handle:
            if last_offset > 0:
                handle.seek(last_offset)
            for raw_line in handle:
                line_no = new_last_line_no + 1
                new_last_line_no = line_no
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                self._insert_entry(cursor, entry, line_no=line_no)
            new_last_offset = handle.tell()
        if new_last_line_no > last_line_no or new_last_offset != last_offset:
            self._meta_set(cursor, "last_line_no", new_last_line_no)
            self._meta_set(cursor, "last_offset", new_last_offset)

    def _query_rows(
        self,
        *,
        actor_user_id: str = "",
        session_id: str = "",
        tool: str = "",
        source: str = "",
        result: str = "",
        q: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        resolved_page = max(1, int(page or 1))
        resolved_page_size = max(1, min(int(page_size or 20), 100))
        offset = (resolved_page - 1) * resolved_page_size
        conditions: list[str] = []
        params: list[Any] = []

        if actor_user_id:
            conditions.append(
                "(LOWER(COALESCE(user_id, '')) = ? OR LOWER(COALESCE(actor_user_id, '')) = ?)"
            )
            params.extend([actor_user_id, actor_user_id])
        if session_id:
            conditions.append(
                "(LOWER(COALESCE(session_id, '')) = ? OR LOWER(COALESCE(target_session_id, '')) = ?)"
            )
            params.extend([session_id, session_id])
        if tool:
            conditions.append("LOWER(tool_text) LIKE ?")
            params.append(f"%{tool}%")
        if source:
            conditions.append("LOWER(COALESCE(source, '')) = ?")
            params.append(source)
        if result:
            conditions.append("LOWER(COALESCE(result, '')) LIKE ?")
            params.append(f"%{result}%")
        if q:
            fts_query = self._fts_query(q)
            if self._fts_enabled and fts_query:
                conditions.append(
                    "entry_id IN (SELECT entry_id FROM audit_search WHERE audit_search MATCH ?)"
                )
                params.append(fts_query)
            else:
                conditions.append("LOWER(search_text) LIKE ?")
                params.append(f"%{q}%")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        count_query = f"SELECT COUNT(*) FROM audit_entries {where}"
        select_query = f"""
            SELECT entry_id, line_no, timestamp, datetime, event_type, user_id, session_id,
                   action, resource, result, tool_name, tool_pattern, source,
                   actor_user_id, target_session_id, task_id, run_id, request_id,
                   metadata_json
            FROM audit_entries
            {where}
            ORDER BY timestamp DESC, entry_id DESC
            LIMIT ? OFFSET ?
        """

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            self._sync_log_to_db(cursor)
            cursor.execute(count_query, tuple(params))
            total = int(cursor.fetchone()[0] or 0)
            cursor.execute(select_query, tuple([*params, resolved_page_size, offset]))
            rows = cursor.fetchall()
            conn.commit()

        entries = [self._row_to_entry(row) for row in rows]
        return {
            "entries": entries,
            "pagination": {
                "page": resolved_page,
                "page_size": resolved_page_size,
                "total": total,
                "has_more": offset + len(entries) < total,
            },
        }

    def log(
        self,
        event_type: AuditEventType,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        action: Optional[str] = None,
        resource: Optional[str] = None,
        result: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """監査ログを記録"""
        timestamp = time.time()
        log_entry = {
            "timestamp": timestamp,
            "datetime": datetime.fromtimestamp(timestamp).isoformat(),
            "event_type": event_type.value,
            "user_id": user_id,
            "session_id": session_id,
            "action": action,
            "resource": resource,
            "result": result,
            "metadata": metadata or {},
        }

        persisted: Dict[str, Any] | None = None
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            self._sync_log_to_db(cursor)
            next_line_no = int(self._meta_get(cursor, "last_line_no", "0") or 0) + 1
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                next_offset = handle.tell()
            persisted = self._insert_entry(cursor, log_entry, line_no=next_line_no)
            self._meta_set(cursor, "last_line_no", next_line_no)
            self._meta_set(cursor, "last_offset", next_offset)
            conn.commit()

        payload = persisted or {
            **log_entry,
            "entry_id": f"audit-{int(timestamp * 1000)}",
        }
        self._notify(payload)
        return payload

    def log_shell_command(
        self,
        command: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        result: Optional[str] = None,
        return_code: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """シェルコマンド実行ログ"""
        self.log(
            event_type=AuditEventType.SHELL_COMMAND,
            user_id=user_id,
            session_id=session_id,
            action="execute",
            resource=command,
            result=result or "success" if return_code == 0 else "failed",
            metadata={"command": command, "return_code": return_code, **(metadata or {})},
        )

    def log_file_operation(
        self,
        operation: str,
        file_path: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        result: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """ファイル操作ログ"""
        event_type = (
            AuditEventType.FILE_READ if operation == "read" else AuditEventType.FILE_WRITE
        )
        self.log(
            event_type=event_type,
            user_id=user_id,
            session_id=session_id,
            action=operation,
            resource=file_path,
            result=result or "success",
            metadata={"file_path": file_path, "operation": operation, **(metadata or {})},
        )

    def log_agent_message(
        self,
        agent_name: str,
        message: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        role: str = "assistant",
    ):
        """エージェントメッセージログ"""
        self.log(
            event_type=AuditEventType.AGENT_MESSAGE,
            user_id=user_id,
            session_id=session_id,
            action="message",
            resource=agent_name,
            result="sent",
            metadata={"agent": agent_name, "role": role, "message_preview": message[:100]},
        )

    def log_error(
        self,
        error: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        """エラーログ"""
        self.log(
            event_type=AuditEventType.ERROR,
            user_id=user_id,
            session_id=session_id,
            action="error",
            resource=None,
            result="error",
            metadata={"error": error, "context": context or {}},
        )

    def get_recent_logs(self, limit: int = 100) -> list:
        """最近のログを取得"""
        result = self._query_rows(page=1, page_size=max(1, min(int(limit or 100), 500)))
        return result["entries"]

    def query_logs(
        self,
        *,
        actor_user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        tool: Optional[str] = None,
        source: Optional[str] = None,
        result: Optional[str] = None,
        q: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """Query audit logs using the indexed SQLite store."""
        normalized_actor = str(actor_user_id or "").strip().lower()
        normalized_session = str(session_id or "").strip().lower()
        normalized_tool = str(tool or "").strip().lower()
        normalized_source = str(source or "").strip().lower()
        normalized_result = str(result or "").strip().lower()
        normalized_query = str(q or "").strip().lower()
        result_payload = self._query_rows(
            actor_user_id=normalized_actor,
            session_id=normalized_session,
            tool=normalized_tool,
            source=normalized_source,
            result=normalized_result,
            q=normalized_query,
            page=page,
            page_size=page_size,
        )
        result_payload["filters"] = self._entry_filters_payload(
            actor_user_id=normalized_actor,
            session_id=normalized_session,
            tool=normalized_tool,
            source=normalized_source,
            result=normalized_result,
            q=normalized_query,
        )
        return result_payload

    def query_related(
        self,
        *,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
        request_ids: Optional[list[str]] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        resolved_limit = max(1, min(int(limit or 100), 500))
        related_ids = [str(item).strip() for item in (request_ids or []) if str(item).strip()]
        conditions: list[str] = []
        params: list[Any] = []

        normalized_session = str(session_id or "").strip()
        if normalized_session:
            conditions.append("(session_id = ? OR target_session_id = ?)")
            params.extend([normalized_session, normalized_session])

        relation_conditions: list[str] = []
        if task_id:
            relation_conditions.append("task_id = ?")
            params.append(task_id)
        if run_id:
            relation_conditions.append("run_id = ?")
            params.append(run_id)
        if related_ids:
            placeholders = ", ".join("?" for _ in related_ids)
            relation_conditions.append(
                f"(request_id IN ({placeholders}) OR source_request_id IN ({placeholders}) OR resource IN ({placeholders}))"
            )
            params.extend(related_ids)
            params.extend(related_ids)
            params.extend(related_ids)

        if relation_conditions:
            conditions.append("(" + " OR ".join(relation_conditions) + ")")

        if not conditions:
            return []

        where = f"WHERE {' AND '.join(conditions)}"
        query = f"""
            SELECT entry_id, line_no, timestamp, datetime, event_type, user_id, session_id,
                   action, resource, result, tool_name, tool_pattern, source,
                   actor_user_id, target_session_id, task_id, run_id, request_id,
                   metadata_json
            FROM audit_entries
            {where}
            ORDER BY timestamp DESC, entry_id DESC
            LIMIT ?
        """

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            self._sync_log_to_db(cursor)
            cursor.execute(query, tuple([*params, resolved_limit]))
            rows = cursor.fetchall()
            conn.commit()
        return [self._row_to_entry(row) for row in rows]


# グローバルインスタンス
_audit_logger: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    """監査ロガーインスタンスを取得"""
    global _audit_logger
    if _audit_logger is None:
        settings = get_settings()
        _audit_logger = AuditLogger(str(settings.audit_log_path))
    return _audit_logger
