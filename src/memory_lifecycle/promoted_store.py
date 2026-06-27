"""
Promoted memory store for boiled-claw v2.

Stores curated long-term memories separately from candidate memories so the
ADK memory service can search only promoted knowledge.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from src.memory_lifecycle.memory_schema import (
    MemoryType,
    PromotedMemory,
    ReviewStatus,
    SensitivityLevel,
)


class PromotedMemoryStore:
    """SQLite-backed store for curated, user-scoped memories."""

    def __init__(self, db_path: str = "data/promoted_memory.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS promoted_memories (
                    memory_id        TEXT PRIMARY KEY,
                    app_name         TEXT NOT NULL,
                    user_id          TEXT NOT NULL,
                    memory_type      TEXT NOT NULL,
                    content          TEXT NOT NULL,
                    subject          TEXT,
                    tags             TEXT NOT NULL DEFAULT '[]',
                    provenance       TEXT NOT NULL,
                    confidence       REAL NOT NULL,
                    trust_score      REAL NOT NULL,
                    sensitivity      TEXT NOT NULL DEFAULT 'internal',
                    valid_from       TEXT,
                    valid_until      TEXT,
                    review_status    TEXT NOT NULL DEFAULT 'promoted',
                    supersedes       TEXT NOT NULL DEFAULT '[]',
                    contradicts      TEXT NOT NULL DEFAULT '[]',
                    merged_from      TEXT NOT NULL DEFAULT '[]',
                    metadata         TEXT NOT NULL DEFAULT '{}',
                    created_at       REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_promoted_app_user "
                "ON promoted_memories(app_name, user_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_promoted_type "
                "ON promoted_memories(memory_type)"
            )

    def save(self, memory: PromotedMemory, *, app_name: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO promoted_memories (
                    memory_id, app_name, user_id, memory_type, content,
                    subject, tags, provenance, confidence, trust_score,
                    sensitivity, valid_from, valid_until, review_status,
                    supersedes, contradicts, merged_from, metadata, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    memory.memory_id,
                    app_name,
                    memory.user_id,
                    memory.memory_type.value,
                    memory.content,
                    memory.subject,
                    json.dumps(memory.tags),
                    memory.provenance.model_dump_json(),
                    memory.confidence,
                    memory.trust_score,
                    memory.sensitivity.value,
                    memory.valid_from.isoformat() if memory.valid_from else None,
                    memory.valid_until.isoformat() if memory.valid_until else None,
                    memory.review_status.value,
                    json.dumps(memory.supersedes),
                    json.dumps(memory.contradicts),
                    json.dumps(memory.merged_from),
                    json.dumps(memory.metadata),
                    time.time(),
                ),
            )

    def bulk_save(
        self, memories: list[PromotedMemory], *, app_name: str
    ) -> None:
        if not memories:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO promoted_memories (
                    memory_id, app_name, user_id, memory_type, content,
                    subject, tags, provenance, confidence, trust_score,
                    sensitivity, valid_from, valid_until, review_status,
                    supersedes, contradicts, merged_from, metadata, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    (
                        memory.memory_id,
                        app_name,
                        memory.user_id,
                        memory.memory_type.value,
                        memory.content,
                        memory.subject,
                        json.dumps(memory.tags),
                        memory.provenance.model_dump_json(),
                        memory.confidence,
                        memory.trust_score,
                        memory.sensitivity.value,
                        memory.valid_from.isoformat() if memory.valid_from else None,
                        memory.valid_until.isoformat() if memory.valid_until else None,
                        memory.review_status.value,
                        json.dumps(memory.supersedes),
                        json.dumps(memory.contradicts),
                        json.dumps(memory.merged_from),
                        json.dumps(memory.metadata),
                        time.time(),
                    )
                    for memory in memories
                ],
            )

    def search(
        self,
        *,
        app_name: str,
        user_id: str,
        query: str,
        limit: int = 10,
    ) -> list[PromotedMemory]:
        words = [word.strip() for word in query.split() if word.strip()]
        sql = (
            "SELECT * FROM promoted_memories "
            "WHERE app_name = ? AND user_id = ? AND review_status != ?"
        )
        params: list[Any] = [app_name, user_id, ReviewStatus.DEPRECATED.value]

        if words:
            clauses = []
            for word in words:
                clauses.append(
                    "(content LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\')"
                )
                pattern = _like_pattern(word)
                params.extend([pattern, pattern])
            sql += " AND (" + " OR ".join(clauses) + ")"

        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, limit))

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_memory(row) for row in rows]

    def list_memories(
        self,
        *,
        app_name: str,
        user_id: str,
    ) -> list[PromotedMemory]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM promoted_memories
                WHERE app_name = ? AND user_id = ? AND review_status != ?
                ORDER BY created_at DESC
                """,
                (app_name, user_id, ReviewStatus.DEPRECATED.value),
            ).fetchall()
        return [self._row_to_memory(row) for row in rows]

    def _row_to_memory(self, row: tuple[Any, ...]) -> PromotedMemory:
        from datetime import datetime

        from src.memory_lifecycle.memory_schema import Provenance

        (
            memory_id,
            _app_name,
            user_id,
            memory_type,
            content,
            subject,
            tags,
            provenance,
            confidence,
            trust_score,
            sensitivity,
            valid_from,
            valid_until,
            review_status,
            supersedes,
            contradicts,
            merged_from,
            metadata,
            _created_at,
        ) = row

        return PromotedMemory(
            memory_id=memory_id,
            user_id=user_id,
            memory_type=MemoryType(memory_type),
            content=content,
            subject=subject,
            tags=json.loads(tags),
            provenance=Provenance.model_validate_json(provenance),
            confidence=confidence,
            trust_score=trust_score,
            sensitivity=SensitivityLevel(sensitivity),
            valid_from=datetime.fromisoformat(valid_from) if valid_from else None,
            valid_until=datetime.fromisoformat(valid_until) if valid_until else None,
            review_status=ReviewStatus(review_status),
            supersedes=json.loads(supersedes),
            contradicts=json.loads(contradicts),
            merged_from=json.loads(merged_from),
            metadata=json.loads(metadata),
        )


_promoted_store: PromotedMemoryStore | None = None


def _like_pattern(word: str) -> str:
    escaped = (
        word.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def get_promoted_store() -> PromotedMemoryStore:
    global _promoted_store
    if _promoted_store is None:
        from src.config.settings import get_settings

        settings = get_settings()
        db_path = str(Path(settings.memory_db_path).parent / "promoted_memory.db")
        _promoted_store = PromotedMemoryStore(db_path=db_path)
    return _promoted_store
