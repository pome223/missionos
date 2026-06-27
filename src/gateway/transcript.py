"""
Gateway-owned transcript store.

SQLite-backed persistent transcript that serves as the single source of truth
for conversation history. Replaces client-side localStorage dependency.

Each entry records: role, content, aborted flag, metadata, timestamps.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


_DB_PATH = Path("data/transcript.db")


@dataclass
class TranscriptEntry:
    id: str
    session_id: str
    role: str  # "user" | "assistant" | "system" | "tool" | "inject"
    content: str
    request_id: Optional[str]
    aborted: bool
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "request_id": self.request_id,
            "aborted": self.aborted,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


@dataclass
class TranscriptSession:
    session_id: str
    user_id: str
    created_at: float
    last_activity: float
    entry_count: int = 0
    preview: str = ""
    last_role: str = ""
    last_preview: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "entry_count": self.entry_count,
            "preview": self.preview,
            "last_role": self.last_role,
            "last_preview": self.last_preview,
        }


class TranscriptStore:
    """SQLite-backed transcript storage."""

    def __init__(self, db_path: Path = _DB_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS transcript_sessions (
                    session_id     TEXT PRIMARY KEY,
                    user_id        TEXT NOT NULL,
                    created_at     REAL NOT NULL,
                    last_activity  REAL NOT NULL,
                    entry_count    INTEGER NOT NULL DEFAULT 0,
                    preview        TEXT NOT NULL DEFAULT '',
                    last_role      TEXT NOT NULL DEFAULT '',
                    last_preview   TEXT NOT NULL DEFAULT ''
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS transcript (
                    id          TEXT PRIMARY KEY,
                    session_id  TEXT NOT NULL,
                    role        TEXT NOT NULL,
                    content     TEXT NOT NULL DEFAULT '',
                    request_id  TEXT,
                    aborted     INTEGER NOT NULL DEFAULT 0,
                    metadata    TEXT NOT NULL DEFAULT '{}',
                    created_at  REAL NOT NULL
                )
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_transcript_session
                ON transcript (session_id, created_at)
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_transcript_sessions_user
                ON transcript_sessions (user_id, last_activity)
            """)
            self._conn.commit()

    @staticmethod
    def _summarize(content: str, limit: int = 96) -> str:
        text = " ".join((content or "").strip().split())
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    def ensure_session(self, session_id: str, user_id: str) -> TranscriptSession:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO transcript_sessions
                   (session_id, user_id, created_at, last_activity)
                   VALUES (?, ?, ?, ?)""",
                (session_id, user_id, now, now),
            )
            self._conn.execute(
                """UPDATE transcript_sessions
                   SET user_id = CASE WHEN user_id = '' OR user_id = 'unknown_user'
                                      THEN ? ELSE user_id END,
                       last_activity = MAX(last_activity, ?)
                   WHERE session_id = ?""",
                (user_id, now, session_id),
            )
            self._conn.commit()
        session = self.get_session(session_id)
        if session is None:
            raise RuntimeError(f"failed to ensure transcript session: {session_id}")
        return session

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        user_id: Optional[str] = None,
        request_id: Optional[str] = None,
        aborted: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TranscriptEntry:
        entry_id = uuid.uuid4().hex[:16]
        now = time.time()
        metadata = metadata or {}
        session = self.get_session(session_id)
        resolved_user_id = user_id or (session.user_id if session else "") or "unknown_user"
        self.ensure_session(session_id, resolved_user_id)
        meta_json = json.dumps(metadata, ensure_ascii=False)

        preview = self._summarize(content)
        with self._lock:
            self._conn.execute(
                """INSERT INTO transcript
                   (id, session_id, role, content, request_id, aborted, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (entry_id, session_id, role, content, request_id, 1 if aborted else 0,
                 meta_json, now),
            )
            existing_preview_row = self._conn.execute(
                "SELECT preview FROM transcript_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            existing_preview = existing_preview_row[0] if existing_preview_row else ""
            session_preview = existing_preview or (preview if role == "user" else "")
            self._conn.execute(
                """UPDATE transcript_sessions
                   SET last_activity = ?,
                       entry_count = entry_count + 1,
                       preview = ?,
                       last_role = ?,
                       last_preview = ?
                   WHERE session_id = ?""",
                (now, session_preview, role, preview, session_id),
            )
            self._conn.commit()

        return TranscriptEntry(
            id=entry_id,
            session_id=session_id,
            role=role,
            content=content,
            request_id=request_id,
            aborted=aborted,
            metadata=metadata,
            created_at=now,
        )

    def get_history(
        self,
        session_id: str,
        limit: int = 100,
        before: Optional[float] = None,
    ) -> List[TranscriptEntry]:
        with self._lock:
            if before is not None:
                rows = self._conn.execute(
                    """SELECT * FROM transcript
                       WHERE session_id = ? AND created_at < ?
                       ORDER BY created_at DESC LIMIT ?""",
                    (session_id, before, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT * FROM transcript
                       WHERE session_id = ?
                       ORDER BY created_at DESC LIMIT ?""",
                    (session_id, limit),
                ).fetchall()

        entries = [self._row(r) for r in rows]
        entries.reverse()  # chronological order
        return entries

    def get_entry(self, entry_id: str) -> Optional[TranscriptEntry]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM transcript WHERE id = ?", (entry_id,)
            ).fetchone()
        return self._row(row) if row else None

    def session_count(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM transcript WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row[0] if row else 0

    def get_session(self, session_id: str) -> Optional[TranscriptSession]:
        with self._lock:
            row = self._conn.execute(
                """SELECT session_id, user_id, created_at, last_activity,
                          entry_count, preview, last_role, last_preview
                   FROM transcript_sessions
                   WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return TranscriptSession(
            session_id=row[0],
            user_id=row[1],
            created_at=row[2],
            last_activity=row[3],
            entry_count=row[4],
            preview=row[5],
            last_role=row[6],
            last_preview=row[7],
        )

    def has_session(self, session_id: str, user_id: Optional[str] = None) -> bool:
        session = self.get_session(session_id)
        if session is None:
            return False
        if user_id is None:
            return True
        return session.user_id == user_id

    def list_sessions(
        self,
        limit: int = 50,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return transcript-backed sessions ordered by last activity."""
        with self._lock:
            if user_id:
                rows = self._conn.execute(
                    """SELECT session_id, user_id, created_at, last_activity,
                              entry_count, preview, last_role, last_preview
                       FROM transcript_sessions
                       WHERE user_id = ?
                       ORDER BY last_activity DESC
                       LIMIT ?""",
                    (user_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT session_id, user_id, created_at, last_activity,
                              entry_count, preview, last_role, last_preview
                       FROM transcript_sessions
                       ORDER BY last_activity DESC
                       LIMIT ?""",
                    (limit,),
                ).fetchall()
        return [
            TranscriptSession(
                session_id=r[0],
                user_id=r[1],
                created_at=r[2],
                last_activity=r[3],
                entry_count=r[4],
                preview=r[5],
                last_role=r[6],
                last_preview=r[7],
            ).to_dict()
            for r in rows
        ]

    @staticmethod
    def _row(r: tuple) -> TranscriptEntry:
        try:
            meta = json.loads(r[6])
        except (json.JSONDecodeError, TypeError):
            meta = {}
        return TranscriptEntry(
            id=r[0],
            session_id=r[1],
            role=r[2],
            content=r[3],
            request_id=r[4],
            aborted=bool(r[5]),
            metadata=meta,
            created_at=r[7],
        )


# Global singleton
_store: Optional[TranscriptStore] = None


def get_transcript_store() -> TranscriptStore:
    global _store
    if _store is None:
        _store = TranscriptStore()
    return _store
