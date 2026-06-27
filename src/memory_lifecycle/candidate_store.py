"""
Candidate Memory Store — boiled-claw v2

MemoryCandidate を SQLite に保存・検索するストア。
Curator が review/promote/reject するまでの中間層。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from src.memory_lifecycle.memory_schema import (
    MemoryCandidate,
    MemoryType,
    ReviewStatus,
    SensitivityLevel,
)


class CandidateStore:
    """SQLite バックエンドの候補メモリストア。"""

    def __init__(self, db_path: str = "data/candidates.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id    TEXT PRIMARY KEY,
                    session_id      TEXT NOT NULL,
                    user_id         TEXT NOT NULL,
                    memory_type     TEXT NOT NULL,
                    content         TEXT NOT NULL,
                    subject         TEXT,
                    tags            TEXT NOT NULL DEFAULT '[]',
                    provenance      TEXT NOT NULL,
                    confidence      REAL NOT NULL,
                    trust_score     REAL NOT NULL,
                    sensitivity     TEXT NOT NULL DEFAULT 'internal',
                    ttl_seconds     INTEGER,
                    valid_from      TEXT,
                    valid_until     TEXT,
                    review_status   TEXT NOT NULL DEFAULT 'candidate',
                    dedup_key       TEXT,
                    contradiction_refs TEXT NOT NULL DEFAULT '[]',
                    source_event_ids   TEXT NOT NULL DEFAULT '[]',
                    metadata        TEXT NOT NULL DEFAULT '{}',
                    created_at      REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidates_session "
                "ON candidates(session_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidates_status "
                "ON candidates(review_status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidates_dedup "
                "ON candidates(dedup_key)"
            )

    def save(self, candidate: MemoryCandidate) -> None:
        """候補を保存または更新する。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO candidates (
                    candidate_id, session_id, user_id, memory_type, content,
                    subject, tags, provenance, confidence, trust_score,
                    sensitivity, ttl_seconds, valid_from, valid_until,
                    review_status, dedup_key, contradiction_refs,
                    source_event_ids, metadata, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                candidate.candidate_id,
                candidate.session_id,
                candidate.user_id,
                candidate.memory_type.value,
                candidate.content,
                candidate.subject,
                json.dumps(candidate.tags),
                candidate.provenance.model_dump_json(),
                candidate.confidence,
                candidate.trust_score,
                candidate.sensitivity.value,
                candidate.ttl_seconds,
                candidate.valid_from.isoformat() if candidate.valid_from else None,
                candidate.valid_until.isoformat() if candidate.valid_until else None,
                candidate.review_status.value,
                candidate.dedup_key,
                json.dumps(candidate.contradiction_refs),
                json.dumps(candidate.source_event_ids),
                json.dumps(candidate.metadata),
                time.time(),
            ))

    def get(self, candidate_id: str) -> MemoryCandidate | None:
        """ID で候補を取得する。"""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_candidate(row)

    def list_by_session(
        self,
        session_id: str,
        user_id: str | None = None,
        status: ReviewStatus | None = None,
    ) -> list[MemoryCandidate]:
        """セッションIDで候補一覧を取得する。"""
        sql = "SELECT * FROM candidates WHERE session_id = ?"
        params: list[Any] = [session_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if status is not None:
            sql += " AND review_status = ?"
            params.append(status.value)
        sql += " ORDER BY created_at DESC"
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_candidate(r) for r in rows]

    def list_pending(self, user_id: str) -> list[MemoryCandidate]:
        """レビュー待ち候補をユーザーIDで取得する。"""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE user_id = ? AND review_status = ? "
                "ORDER BY created_at DESC",
                (user_id, ReviewStatus.CANDIDATE.value),
            ).fetchall()
        return [self._row_to_candidate(r) for r in rows]

    def find_by_dedup_key(
        self, dedup_key: str, user_id: str
    ) -> list[MemoryCandidate]:
        """dedup_key が一致する候補を返す。"""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE dedup_key = ? AND user_id = ?",
                (dedup_key, user_id),
            ).fetchall()
        return [self._row_to_candidate(r) for r in rows]

    def update_status(
        self, candidate_id: str, status: ReviewStatus
    ) -> None:
        """候補のレビューステータスを更新する。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE candidates SET review_status = ? WHERE candidate_id = ?",
                (status.value, candidate_id),
            )

    def _row_to_candidate(self, row: tuple) -> MemoryCandidate:
        from datetime import datetime
        from src.memory_lifecycle.memory_schema import Provenance

        (
            candidate_id, session_id, user_id, memory_type, content,
            subject, tags, provenance, confidence, trust_score,
            sensitivity, ttl_seconds, valid_from, valid_until,
            review_status, dedup_key, contradiction_refs,
            source_event_ids, metadata, _created_at,
        ) = row

        return MemoryCandidate(
            candidate_id=candidate_id,
            session_id=session_id,
            user_id=user_id,
            memory_type=MemoryType(memory_type),
            content=content,
            subject=subject,
            tags=json.loads(tags),
            provenance=Provenance.model_validate_json(provenance),
            confidence=confidence,
            trust_score=trust_score,
            sensitivity=SensitivityLevel(sensitivity),
            ttl_seconds=ttl_seconds,
            valid_from=datetime.fromisoformat(valid_from) if valid_from else None,
            valid_until=datetime.fromisoformat(valid_until) if valid_until else None,
            review_status=ReviewStatus(review_status),
            dedup_key=dedup_key,
            contradiction_refs=json.loads(contradiction_refs),
            source_event_ids=json.loads(source_event_ids),
            metadata=json.loads(metadata),
        )


_candidate_store: CandidateStore | None = None


def get_candidate_store() -> CandidateStore:
    global _candidate_store
    if _candidate_store is None:
        from src.config.settings import get_settings
        settings = get_settings()
        db_path = str(Path(settings.memory_db_path).parent / "candidates.db")
        _candidate_store = CandidateStore(db_path=db_path)
    return _candidate_store
