"""TaskStore persistence for already-decided PX4 route recovery results.

This module serializes supplied approval, allowlist, dispatch, completion, and
workflow artifacts into an existing task.  It does not create any of those
artifacts, choose an action, invoke a backend, or upgrade completion evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.runtime.px4_gazebo_route.recovery_outcomes import recovery_task_artifacts
from src.runtime.px4_gazebo_route.recovery_workflow import (
    RouteDeviationRecoveryWorkflow,
)
from src.runtime.task_store import TaskStore


class RecoveryPersistenceError(RuntimeError):
    """Raised when a recovery result cannot be written to its existing task."""


@dataclass(frozen=True)
class RouteDeviationRecoveryPersistenceInputs:
    store: TaskStore
    task_id: str
    workflow: RouteDeviationRecoveryWorkflow
    deviation_abort: Any
    approval: Any | None = None
    allowlist: Any | None = None
    dispatch: Any | None = None
    post_approval: Any | None = None
    post_allowlist: Any | None = None
    post_dispatch: Any | None = None
    mission_assurance_live_guard: Mapping[str, Any] | None = None


def persist_route_deviation_recovery(
    inputs: RouteDeviationRecoveryPersistenceInputs,
) -> dict[str, Any]:
    """Persist supplied recovery facts without creating new authority."""

    artifacts = recovery_task_artifacts(
        deviation_abort=inputs.deviation_abort,
        approval=inputs.approval,
        allowlist=inputs.allowlist,
        dispatch=inputs.dispatch,
        completion=inputs.workflow.primary.completion,
        post_approval=inputs.post_approval,
        post_allowlist=inputs.post_allowlist,
        post_dispatch=inputs.post_dispatch,
        post_completion=inputs.workflow.post.completion,
    )
    if inputs.mission_assurance_live_guard is not None:
        artifacts["mission_assurance_live_guard"] = dict(inputs.mission_assurance_live_guard)
    updated = inputs.store.update(
        inputs.task_id,
        status=inputs.workflow.task_status,
        artifacts=artifacts,
    )
    if updated is None:
        raise RecoveryPersistenceError(f"route recovery task not found: {inputs.task_id}")
    return updated


__all__ = [
    "RecoveryPersistenceError",
    "RouteDeviationRecoveryPersistenceInputs",
    "persist_route_deviation_recovery",
]
