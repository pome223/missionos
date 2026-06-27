"""
Conflict Detector — boiled-claw v2

候補メモリと既存メモリ間の重複・矛盾・stale を検出する。
Curator から呼び出される。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from src.memory_lifecycle.memory_schema import (
    ConflictRecord,
    ConflictType,
    MemoryCandidate,
    PromotedMemory,
    SensitivityLevel,
)

# 内容の類似判定に使う単純な Jaccard 閾値
_DUPLICATE_THRESHOLD = 0.75
_CONTRADICTION_THRESHOLD = 0.45


def _token_set(text: str) -> set[str]:
    return set(text.lower().split())


def _jaccard(a: str, b: str) -> float:
    sa, sb = _token_set(a), _token_set(b)
    if not sa and not sb:
        return 1.0
    intersection = len(sa & sb)
    union = len(sa | sb)
    return intersection / union if union else 0.0


class ConflictDetector:
    """候補と昇格済みメモリの衝突を検出する。"""

    def detect(
        self,
        candidate: MemoryCandidate,
        existing: list[PromotedMemory],
    ) -> list[ConflictRecord]:
        """候補に対する全衝突レコードを返す。"""
        records: list[ConflictRecord] = []

        for mem in existing:
            # memory_type が一致するものだけ比較
            if mem.memory_type != candidate.memory_type:
                continue

            sim = _jaccard(candidate.content, mem.content)

            if sim >= _DUPLICATE_THRESHOLD:
                records.append(ConflictRecord(
                    conflict_id=f"conflict_{uuid.uuid4().hex[:10]}",
                    conflict_type=ConflictType.DUPLICATE,
                    left_ref=candidate.candidate_id,
                    right_ref=mem.memory_id,
                    summary=(
                        f"High similarity ({sim:.2f}) detected between "
                        f"candidate '{candidate.candidate_id}' "
                        f"and promoted memory '{mem.memory_id}'"
                    ),
                    detected_at=datetime.now(tz=timezone.utc),
                ))
            elif _CONTRADICTION_THRESHOLD <= sim < _DUPLICATE_THRESHOLD:
                # subject が同じで内容が似ているが完全一致でない → contradiction 候補
                same_subject = (
                    candidate.subject is not None
                    and mem.subject is not None
                    and candidate.subject.lower() == mem.subject.lower()
                )
                if same_subject:
                    records.append(ConflictRecord(
                        conflict_id=f"conflict_{uuid.uuid4().hex[:10]}",
                        conflict_type=ConflictType.CONTRADICTION,
                        left_ref=candidate.candidate_id,
                        right_ref=mem.memory_id,
                        summary=(
                            f"Possible contradiction on subject '{candidate.subject}': "
                            f"similarity={sim:.2f}"
                        ),
                        detected_at=datetime.now(tz=timezone.utc),
                    ))

            # sensitivity mismatch チェック
            if _sensitivity_order(candidate.sensitivity) < _sensitivity_order(
                mem.sensitivity
            ):
                records.append(ConflictRecord(
                    conflict_id=f"conflict_{uuid.uuid4().hex[:10]}",
                    conflict_type=ConflictType.SENSITIVITY_MISMATCH,
                    left_ref=candidate.candidate_id,
                    right_ref=mem.memory_id,
                    summary=(
                        f"Candidate sensitivity '{candidate.sensitivity}' is lower "
                        f"than existing memory sensitivity '{mem.sensitivity}'"
                    ),
                    detected_at=datetime.now(tz=timezone.utc),
                ))

        return records

    def is_stale(
        self, promoted: PromotedMemory, now: datetime | None = None
    ) -> bool:
        """有効期限切れかどうかを返す。"""
        if promoted.valid_until is None:
            return False
        _now = now or datetime.now(tz=timezone.utc)
        until = promoted.valid_until
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return _now > until


def _sensitivity_order(level: SensitivityLevel) -> int:
    order = {
        SensitivityLevel.PUBLIC: 0,
        SensitivityLevel.INTERNAL: 1,
        SensitivityLevel.CONFIDENTIAL: 2,
        SensitivityLevel.RESTRICTED: 3,
    }
    return order.get(level, 0)
