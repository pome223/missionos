"""Persistence adapter that attaches toy-grid autonomy artifacts to a task.

This module is a thin bridge between ``toy_grid_world.py`` (artifact builders /
evaluators) and ``task_store.py`` (storage). It deliberately does not expose
any execution / approval / promotion / runtime-reuse path: the helper only
writes the read-only artifact bundle that the Control UI in #169 already
knows how to render.

Why a separate module: ``toy_grid_world.py`` is currently a pure simulator /
artifact module with no knowledge of the task store. Coupling the two would
muddle responsibility, so persistence lives here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from src.runtime.task_store import TaskStore, get_task_store
from src.runtime.toy_grid_world import (
    ToyGridWorldAutonomousEpisode,
    ToyGridWorldAutonomyGateResult,
    _DEFAULT_SAFETY_REGRESSION_SUITES,
    build_toy_grid_world_autonomy_safety_regression_gate,
    compare_toy_grid_world_autonomy_gate_results,
)


__all__ = [
    "AutonomyArtifactAttachError",
    "attach_toy_grid_world_autonomy_artifacts",
]


class AutonomyArtifactAttachError(RuntimeError):
    """Raised when autonomy artifacts cannot be attached to a task."""


def attach_toy_grid_world_autonomy_artifacts(
    task_id: str,
    episode: ToyGridWorldAutonomousEpisode | dict[str, Any],
    *,
    baseline_gate: ToyGridWorldAutonomyGateResult | dict[str, Any] | None = None,
    safety_eval_results: list[dict[str, Any]] | None = None,
    required_safety_suite_ids: Sequence[str] = _DEFAULT_SAFETY_REGRESSION_SUITES,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach toy-grid autonomy artifacts to ``task_id`` in the task store.

    Builds the autonomy_scorecard / episode_review / gate via the safety
    regression entry-point. When ``baseline_gate`` is provided, also builds an
    autonomy_gate_comparison_result against it. Merges the resulting bundle
    into ``task.artifacts`` using ``TaskStore.update``'s deep-merge semantics
    so any pre-existing artifacts on the task are preserved.

    Read-only contract: this helper only writes artifact rows. It does not
    change task status, request approval, promote candidates, enable runtime
    reuse, or trigger live / physical / stronger execution.

    Returns the artifact bundle that was attached. Raises
    ``AutonomyArtifactAttachError`` if the task does not exist or disappears
    mid-update.
    """

    store_factory = task_store_factory or get_task_store
    store = store_factory()
    current = store.get(task_id)
    if current is None:
        raise AutonomyArtifactAttachError(
            f"task {task_id} not found in task store; cannot attach autonomy artifacts"
        )

    current_time = now or datetime.now(timezone.utc)

    if isinstance(episode, ToyGridWorldAutonomousEpisode):
        episode_payload = episode.model_dump(mode="json")
    elif isinstance(episode, dict):
        episode_payload = dict(episode)
    else:
        raise AutonomyArtifactAttachError(
            "episode must be ToyGridWorldAutonomousEpisode or a dict payload"
        )

    gate = build_toy_grid_world_autonomy_safety_regression_gate(
        episode,
        required_safety_suite_ids=required_safety_suite_ids,
        safety_eval_results=safety_eval_results,
        now=current_time,
    )
    gate_payload = gate.model_dump(mode="json")

    artifacts: dict[str, Any] = {
        "autonomous_episode": episode_payload,
        "autonomy_scorecard": gate_payload["scorecard_snapshot"],
        "autonomy_episode_review": gate_payload["review_snapshot"],
        "autonomy_gate_result": gate_payload,
    }

    if baseline_gate is not None:
        comparison = compare_toy_grid_world_autonomy_gate_results(
            baseline_gate,
            gate,
            now=current_time,
        )
        artifacts["autonomy_gate_comparison_result"] = comparison.model_dump(mode="json")

    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise AutonomyArtifactAttachError(
            f"task {task_id} disappeared while attaching autonomy artifacts"
        )
    return artifacts
