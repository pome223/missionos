from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from src.runtime.px4_gazebo_route.recovery_execution import ObservedRecoveryCycle
from src.runtime.px4_gazebo_route.recovery_outcomes import RecoveryCycleOutcome
from src.runtime.px4_gazebo_route.recovery_persistence import (
    RecoveryPersistenceError,
    RouteDeviationRecoveryPersistenceInputs,
    persist_route_deviation_recovery,
)
from src.runtime.px4_gazebo_route.recovery_workflow import (
    assemble_route_deviation_recovery,
)
from src.runtime.task_store import TaskStore


@dataclass(frozen=True)
class _Completion:
    final_status: str
    completion_id: str

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {
            "final_status": self.final_status,
            "completion_id": self.completion_id,
            "delivery_completion_claimed": False,
            "physical_execution_invoked": False,
        }


def _cycle(*, completed: bool, completion_id: str) -> ObservedRecoveryCycle:
    return ObservedRecoveryCycle(
        outcome=RecoveryCycleOutcome(
            action="rtl",
            approval_ref="approval:1",
            dispatch_ref="dispatch:1",
            state_observed=completed,
            completed=completed,
        ),
        pose=None,
        samples=(),
        completion=_Completion(
            final_status="recovered" if completed else "recovery_unconfirmed",
            completion_id=completion_id,
        ),
    )


def test_recovery_persistence_retains_existing_artifacts(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="px4_route",
        title="route",
        status="running",
        artifacts={"existing": {"kept": True}},
    )
    workflow = assemble_route_deviation_recovery(
        primary=_cycle(completed=False, completion_id="completion-1")
    )

    updated = persist_route_deviation_recovery(
        RouteDeviationRecoveryPersistenceInputs(
            store=store,
            task_id=task["task_id"],
            workflow=workflow,
            deviation_abort={"abort_id": "abort-1"},
            approval={"approval_id": "approval-1"},
            allowlist={"allowlist_id": "allowlist-1"},
            dispatch={"dispatch_result_id": "dispatch-1"},
        )
    )

    assert updated["status"] == "blocked"
    assert updated["artifacts"]["existing"]["kept"] is True
    assert updated["artifacts"]["px4_gazebo_route_deviation_abort"] == {
        "abort_id": "abort-1"
    }
    completion = updated["artifacts"]["px4_gazebo_route_recovery_completion"]
    assert completion["completion_id"] == "completion-1"
    assert completion["delivery_completion_claimed"] is False
    assert completion["physical_execution_invoked"] is False


def test_recovery_persistence_rejects_unknown_task(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))

    with pytest.raises(RecoveryPersistenceError, match="task not found"):
        persist_route_deviation_recovery(
            RouteDeviationRecoveryPersistenceInputs(
                store=store,
                task_id="task_missing",
                workflow=assemble_route_deviation_recovery(),
                deviation_abort={"abort_id": "abort-1"},
            )
        )
