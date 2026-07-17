"""Chronological ledger of recovery shadow comparisons from the task store.

``shadow_comparison`` records (LLM proposal vs. deterministic candidate) are
written on every TB3 recovery event, but they live embedded inside
``recovery_planner_result`` dicts scattered across task artifacts — plan
proposals, execution summaries, recovery decision summaries. Until this
module existed nothing read them back, so the promotion criteria in
``recovery_action_promotion.py`` had no data source. This walks the task
store, extracts every shadow comparison in task-creation order, and
de-duplicates the copies a single mission stores in multiple artifact
surfaces.

Read-only: this module never writes to the task store and never grants
authority — it turns already-recorded measurement artifacts into the
chronological sequence ``evaluate_recovery_action_promotion_candidates``
expects.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping

from src.runtime.task_store import TaskStore, get_task_store

RECOVERY_SHADOW_LEDGER_SCHEMA_VERSION = "missionos_recovery_shadow_ledger.v1"

_SHADOW_COMPARISON_SCHEMA_VERSION = (
    "missionos_turtlebot3_recovery_shadow_comparison.v1"
)
_MAX_WALK_DEPTH = 16
DEFAULT_TASK_SCAN_LIMIT = 500


def _walk_for_shadow_comparisons(
    value: Any,
    *,
    depth: int = 0,
) -> list[dict[str, Any]]:
    if depth > _MAX_WALK_DEPTH:
        return []
    found: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        if value.get("schema_version") == _SHADOW_COMPARISON_SCHEMA_VERSION:
            return [dict(value)]
        for sub in value.values():
            found.extend(_walk_for_shadow_comparisons(sub, depth=depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_walk_for_shadow_comparisons(item, depth=depth + 1))
    return found


def _shadow_fingerprint(shadow: Mapping[str, Any]) -> str:
    return sha256(
        json.dumps(dict(shadow), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def collect_recovery_shadow_comparisons(
    *,
    task_store: TaskStore | None = None,
    task_scan_limit: int = DEFAULT_TASK_SCAN_LIMIT,
) -> dict[str, Any]:
    """Collect shadow comparisons chronologically (oldest task first).

    A single mission records the same shadow comparison on several artifact
    surfaces (plan proposal, execution summary, decision summary); identical
    copies within one task are counted once, so a mission contributes one
    ledger entry per actual recovery event, not one per storage location.
    """

    store = task_store or get_task_store()
    tasks = store.list(limit=task_scan_limit)
    tasks_oldest_first = sorted(
        tasks, key=lambda task: str(task.get("created_at") or "")
    )
    entries: list[dict[str, Any]] = []
    for task in tasks_oldest_first:
        artifacts = task.get("artifacts")
        if not isinstance(artifacts, Mapping):
            continue
        seen_in_task: set[str] = set()
        for shadow in _walk_for_shadow_comparisons(artifacts):
            fingerprint = _shadow_fingerprint(shadow)
            if fingerprint in seen_in_task:
                continue
            seen_in_task.add(fingerprint)
            entries.append(
                {
                    **shadow,
                    "task_id": str(task.get("task_id") or ""),
                    "task_created_at": str(task.get("created_at") or ""),
                }
            )
    return {
        "schema_version": RECOVERY_SHADOW_LEDGER_SCHEMA_VERSION,
        "shadow_comparisons": entries,
        "shadow_comparison_count": len(entries),
        "task_count_scanned": len(tasks),
        "read_only": True,
        "approval_created": False,
        "dispatch_authority_created": False,
    }


__all__ = [
    "DEFAULT_TASK_SCAN_LIMIT",
    "RECOVERY_SHADOW_LEDGER_SCHEMA_VERSION",
    "collect_recovery_shadow_comparisons",
]
