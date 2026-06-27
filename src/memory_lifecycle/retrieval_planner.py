"""
Retrieval Planner — boiled-claw v2

タスクに応じて必要な memory class を選択し、
relevance / freshness / trust でリランクする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.memory_lifecycle.memory_schema import (
    MemoryType,
    PromotedMemory,
    SensitivityLevel,
)


@dataclass
class RetrievalQuery:
    """単一クラスへの検索クエリ。"""

    memory_type: MemoryType
    query_text: str
    max_results: int = 5
    min_confidence: float = 0.5
    min_trust: float = 0.5


@dataclass
class RetrievalBundle:
    """task-conditioned retrieval の結果束。"""

    goal: str
    results: dict[str, list[PromotedMemory]] = field(default_factory=dict)
    policy_rules: list[PromotedMemory] = field(default_factory=list)

    def all_memories(self) -> list[PromotedMemory]:
        out: list[PromotedMemory] = []
        for mems in self.results.values():
            out.extend(mems)
        return out

    def to_context_text(self, max_chars: int = 4000) -> str:
        """Planner や Executor に渡せるテキスト形式に変換する。"""
        lines: list[str] = [f"[Memory Context for: {self.goal}]"]

        if self.policy_rules:
            lines.append("\n## Policy Rules")
            for mem in self.policy_rules:
                lines.append(f"- {mem.content}")

        for mem_type, memories in self.results.items():
            if not memories:
                continue
            lines.append(f"\n## {mem_type.title()} Memory")
            for mem in memories:
                lines.append(
                    f"- [{mem.memory_type.value}] {mem.content}"
                    + (f" (confidence={mem.confidence:.2f})" if mem.confidence < 0.9 else "")
                )

        text = "\n".join(lines)
        return text[:max_chars] if len(text) > max_chars else text


class RetrievalPlanner:
    """
    タスクに応じた task-conditioned retrieval を実行する。

    単純な類似検索ではなく、タスクに必要な memory class を判定し、
    relevance × freshness × trust でリランクする。
    """

    def __init__(self, memory_store: list[PromotedMemory] | None = None) -> None:
        self._memory: list[PromotedMemory] = memory_store or []

    def plan_queries(self, goal: str, task_type: str | None = None) -> list[RetrievalQuery]:
        """goal から必要な RetrievalQuery リストを生成する。"""
        queries: list[RetrievalQuery] = []

        # 全タスクで policy は常に引く
        queries.append(RetrievalQuery(
            memory_type=MemoryType.POLICY,
            query_text=goal,
            max_results=5,
            min_confidence=0.7,
        ))

        # semantic は常に引く（事実ベース）
        queries.append(RetrievalQuery(
            memory_type=MemoryType.SEMANTIC,
            query_text=goal,
            max_results=5,
        ))

        # procedural は「どうやるか」が必要なタスク
        if task_type in ("write", "generate", "refactor", "plan", None):
            queries.append(RetrievalQuery(
                memory_type=MemoryType.PROCEDURAL,
                query_text=goal,
                max_results=3,
            ))

        # episodic は「最近何があったか」が関係するタスク
        queries.append(RetrievalQuery(
            memory_type=MemoryType.EPISODIC,
            query_text=goal,
            max_results=3,
        ))

        return queries

    def execute(
        self,
        goal: str,
        task_type: str | None = None,
        allowed_sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL,
    ) -> RetrievalBundle:
        """task-conditioned retrieval を実行して RetrievalBundle を返す。"""
        queries = self.plan_queries(goal, task_type)
        bundle = RetrievalBundle(goal=goal)

        for query in queries:
            candidates = self._filter(
                query.memory_type,
                query.min_confidence,
                query.min_trust,
                allowed_sensitivity,
            )
            ranked = self._rank(candidates, query.query_text)
            top = ranked[: query.max_results]

            if query.memory_type == MemoryType.POLICY:
                bundle.policy_rules = top
            else:
                bundle.results[query.memory_type.value] = top

        return bundle

    def _filter(
        self,
        memory_type: MemoryType,
        min_confidence: float,
        min_trust: float,
        allowed_sensitivity: SensitivityLevel,
    ) -> list[PromotedMemory]:
        now = datetime.now(tz=timezone.utc)
        result: list[PromotedMemory] = []
        for mem in self._memory:
            if mem.memory_type != memory_type:
                continue
            if mem.confidence < min_confidence:
                continue
            if mem.trust_score < min_trust:
                continue
            if _sensitivity_order(mem.sensitivity) > _sensitivity_order(allowed_sensitivity):
                continue
            if mem.valid_until is not None:
                until = mem.valid_until
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if now > until:
                    continue
            result.append(mem)
        return result

    def _rank(
        self, memories: list[PromotedMemory], query: str
    ) -> list[PromotedMemory]:
        """relevance × freshness × trust でスコアリングしてソートする。"""
        now = datetime.now(tz=timezone.utc)

        def score(mem: PromotedMemory) -> float:
            # relevance: 簡易 Jaccard
            from src.memory_lifecycle.conflict_detector import _jaccard
            relevance = _jaccard(query, mem.content)

            # freshness: captured_at からの経過日数 (最大 30 日で減衰)
            captured = mem.provenance.captured_at
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=timezone.utc)
            days_old = max(0, (now - captured).days)
            freshness = max(0.0, 1.0 - days_old / 30)

            return relevance * 0.5 + freshness * 0.2 + mem.trust_score * 0.3

        return sorted(memories, key=score, reverse=True)

    def add_memory(self, memory: PromotedMemory) -> None:
        self._memory.append(memory)

    def bulk_add(self, memories: list[PromotedMemory]) -> None:
        self._memory.extend(memories)


def _sensitivity_order(level: SensitivityLevel) -> int:
    order = {
        SensitivityLevel.PUBLIC: 0,
        SensitivityLevel.INTERNAL: 1,
        SensitivityLevel.CONFIDENTIAL: 2,
        SensitivityLevel.RESTRICTED: 3,
    }
    return order.get(level, 0)
