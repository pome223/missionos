"""
Memory Curator — boiled-claw v2

候補メモリを審査し、promote / merge / reject を判断する。
session 終了後または重要イベント後に呼び出される。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field

from src.memory_lifecycle.candidate_store import CandidateStore
from src.memory_lifecycle.conflict_detector import ConflictDetector
from src.memory_lifecycle.memory_schema import (
    ConflictRecord,
    MemoryCandidate,
    PromotedMemory,
    ReviewStatus,
)

# 昇格に必要な最小スコア閾値
_MIN_CONFIDENCE = 0.60
_MIN_TRUST = 0.55


@dataclass
class CurationResult:
    """Curator の審査結果。"""

    promoted: list[PromotedMemory] = field(default_factory=list)
    merged: list[PromotedMemory] = field(default_factory=list)
    updated: list[PromotedMemory] = field(default_factory=list)
    rejected_ids: list[str] = field(default_factory=list)
    conflict_records: list[ConflictRecord] = field(default_factory=list)

    @property
    def all_promoted(self) -> list[PromotedMemory]:
        return self.promoted + self.merged

    @property
    def persisted_memories(self) -> list[PromotedMemory]:
        return self.promoted + self.merged + self.updated

    @property
    def promoted_ids(self) -> list[str]:
        return [m.memory_id for m in self.all_promoted]


class Curator:
    """候補メモリのライフサイクルを管理する。"""

    def __init__(
        self,
        store: CandidateStore,
        existing_promoted: list[PromotedMemory] | None = None,
    ) -> None:
        self._store = store
        self._existing = existing_promoted or []
        self._detector = ConflictDetector()

    async def curate_session(
        self, session_id: str, user_id: str
    ) -> CurationResult:
        """セッションの全 CANDIDATE を審査する。"""
        candidates = self._store.list_by_session(
            session_id,
            user_id=user_id,
            status=ReviewStatus.CANDIDATE,
        )
        return await self._curate(candidates, user_id)

    async def curate_candidates(
        self, candidates: list[MemoryCandidate], user_id: str
    ) -> CurationResult:
        """指定候補リストを審査する。"""
        return await self._curate(candidates, user_id)

    async def _curate(
        self, candidates: list[MemoryCandidate], user_id: str
    ) -> CurationResult:
        result = CurationResult()

        for candidate in candidates:
            # 基本スコアフィルタ
            if (
                candidate.confidence < _MIN_CONFIDENCE
                or candidate.trust_score < _MIN_TRUST
            ):
                self._store.update_status(
                    candidate.candidate_id, ReviewStatus.REJECTED
                )
                result.rejected_ids.append(candidate.candidate_id)
                continue

            # 衝突検出
            conflicts = self._detector.detect(candidate, self._existing)
            result.conflict_records.extend(conflicts)

            has_duplicate = any(
                c.conflict_type.value == "duplicate" for c in conflicts
            )
            has_contradiction = any(
                c.conflict_type.value == "contradiction" for c in conflicts
            )

            if has_duplicate:
                # 重複: 既存と merge（既存の方が信頼度が高ければ reject）
                promoted = self._merge_with_existing(candidate)
                if promoted:
                    result.merged.append(promoted)
                    self._store.update_status(
                        candidate.candidate_id, ReviewStatus.MERGED
                    )
                else:
                    self._store.update_status(
                        candidate.candidate_id, ReviewStatus.REJECTED
                    )
                    result.rejected_ids.append(candidate.candidate_id)
            elif has_contradiction:
                # 矛盾: 信頼度が高い方を採用
                promoted, deprecated = self._resolve_contradiction(candidate)
                if promoted:
                    result.promoted.append(promoted)
                    if deprecated is not None:
                        result.updated.append(deprecated)
                    self._store.update_status(
                        candidate.candidate_id, ReviewStatus.PROMOTED
                    )
                else:
                    self._store.update_status(
                        candidate.candidate_id, ReviewStatus.REJECTED
                    )
                    result.rejected_ids.append(candidate.candidate_id)
            else:
                # 衝突なし: そのまま昇格
                promoted = _candidate_to_promoted(candidate)
                result.promoted.append(promoted)
                self._existing.append(promoted)
                self._store.update_status(
                    candidate.candidate_id, ReviewStatus.PROMOTED
                )

        return result

    def _merge_with_existing(
        self, candidate: MemoryCandidate
    ) -> PromotedMemory | None:
        """候補と既存メモリをマージする。信頼度が高い方のコンテンツを採用。"""
        from src.memory_lifecycle.conflict_detector import _jaccard

        for index, mem in enumerate(self._existing):
            if mem.memory_type != candidate.memory_type:
                continue
            sim = _jaccard(candidate.content, mem.content)
            if sim >= 0.75:
                if candidate.trust_score > mem.trust_score:
                    # 候補の方が信頼度が高い → 既存を更新
                    merged = mem.model_copy(update={
                        "content": candidate.content,
                        "confidence": max(mem.confidence, candidate.confidence),
                        "trust_score": max(mem.trust_score, candidate.trust_score),
                        "merged_from": mem.merged_from + [candidate.candidate_id],
                    })
                    self._existing[index] = merged
                    return merged
                return None  # 既存の方が信頼度が高い
        return None

    def _resolve_contradiction(
        self, candidate: MemoryCandidate
    ) -> tuple[PromotedMemory | None, PromotedMemory | None]:
        """矛盾する候補と既存メモリを比較し、新旧の更新結果を返す。"""
        from src.memory_lifecycle.conflict_detector import (
            _CONTRADICTION_THRESHOLD,
            _DUPLICATE_THRESHOLD,
            _jaccard,
        )

        for index, mem in enumerate(self._existing):
            if mem.memory_type != candidate.memory_type:
                continue
            same_subject = (
                candidate.subject is not None
                and mem.subject is not None
                and candidate.subject.lower() == mem.subject.lower()
            )
            if not same_subject:
                continue

            sim = _jaccard(candidate.content, mem.content)
            if not (_CONTRADICTION_THRESHOLD <= sim < _DUPLICATE_THRESHOLD):
                continue
            if candidate.trust_score <= max(_MIN_TRUST, mem.trust_score):
                return None, None

            promoted = _candidate_to_promoted(candidate).model_copy(
                update={
                    "supersedes": _append_unique([], mem.memory_id),
                    "contradicts": _append_unique([], mem.memory_id),
                }
            )
            deprecated = mem.model_copy(
                update={
                    "review_status": ReviewStatus.DEPRECATED,
                    "contradicts": _append_unique(mem.contradicts, promoted.memory_id),
                    "metadata": {
                        **mem.metadata,
                        "superseded_by": promoted.memory_id,
                    },
                }
            )
            self._existing[index] = promoted
            return promoted, deprecated

        if candidate.trust_score >= _MIN_TRUST:
            promoted = _candidate_to_promoted(candidate)
            self._existing.append(promoted)
            return promoted, None
        return None, None


def _append_unique(values: list[str], item: str) -> list[str]:
    if item in values:
        return values
    return values + [item]


def _candidate_to_promoted(candidate: MemoryCandidate) -> PromotedMemory:
    return PromotedMemory(
        memory_id=f"mem_{uuid.uuid4().hex[:12]}",
        user_id=candidate.user_id,
        memory_type=candidate.memory_type,
        content=candidate.content,
        subject=candidate.subject,
        tags=candidate.tags,
        provenance=candidate.provenance,
        confidence=candidate.confidence,
        trust_score=candidate.trust_score,
        sensitivity=candidate.sensitivity,
        valid_from=candidate.valid_from,
        valid_until=candidate.valid_until,
        review_status=ReviewStatus.PROMOTED,
        metadata=candidate.metadata,
    )
