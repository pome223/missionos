from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

from src.runtime.task_store import TaskStore


def test_concurrent_artifact_updates_do_not_erase_each_other(tmp_path) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_atomic_artifact_updates",
        kind="contract_test",
        title="Atomic artifact updates",
        artifacts={"initial": True},
    )
    writer_count = 16
    start = threading.Barrier(writer_count)

    def _write(index: int) -> None:
        start.wait()
        updated = store.update(
            task["task_id"],
            artifacts={f"writer_{index}": {"observed": True}},
        )
        assert updated is not None

    with ThreadPoolExecutor(max_workers=writer_count) as executor:
        list(executor.map(_write, range(writer_count)))

    final = store.get(task["task_id"])
    assert final is not None
    assert final["artifacts"]["initial"] is True
    for index in range(writer_count):
        assert final["artifacts"][f"writer_{index}"] == {"observed": True}


def test_update_can_replace_one_current_state_artifact_without_stale_merge(
    tmp_path,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_replace_current_state_artifact",
        kind="contract_test",
        title="Replace current-state artifact",
        artifacts={
            "sibling_evidence": {"preserved": True},
            "live_bridge": {
                "bridge_status": "proposal_attached",
                "runtime_result": {
                    "runtime_status": "proposal_guardrail_passed",
                    "assessment": {"selected_bounded_action": "adjust_altitude"},
                },
            },
        },
    )

    store.update(
        task["task_id"],
        replace_artifacts={
            "live_bridge": {
                "bridge_status": "proposal_skipped",
                "runtime_result": {
                    "runtime_status": "proposal_skipped",
                    "assessment": {},
                },
            }
        },
    )

    final = store.get(task["task_id"])
    assert final is not None
    assert final["artifacts"]["sibling_evidence"] == {"preserved": True}
    assert final["artifacts"]["live_bridge"] == {
        "bridge_status": "proposal_skipped",
        "runtime_result": {
            "runtime_status": "proposal_skipped",
            "assessment": {},
        },
    }


def test_one_shot_artifact_claim_has_exactly_one_winner(tmp_path) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_atomic_artifact_claim",
        kind="contract_test",
        title="Atomic artifact claim",
        artifacts={
            "approvals": {
                "approval_1": {
                    "approval_status": "issued_unconsumed",
                    "consumed_in_runtime": False,
                }
            }
        },
    )
    claimant_count = 12
    start = threading.Barrier(claimant_count)

    def _claim(index: int) -> bool:
        start.wait()
        claimed = store.claim_nested_artifact(
            task["task_id"],
            collection_key="approvals",
            artifact_id="approval_1",
            expected={
                "approval_status": "issued_unconsumed",
                "consumed_in_runtime": False,
            },
            updates={
                "approval_status": "consumed_before_runtime",
                "consumed_in_runtime": True,
                "claimant": index,
            },
            expected_task_status="pending",
        )
        return claimed is not None

    with ThreadPoolExecutor(max_workers=claimant_count) as executor:
        winners = list(executor.map(_claim, range(claimant_count)))

    assert winners.count(True) == 1
    final = store.get(task["task_id"])
    assert final is not None
    approval = final["artifacts"]["approvals"]["approval_1"]
    assert approval["approval_status"] == "consumed_before_runtime"
    assert approval["consumed_in_runtime"] is True


def test_nested_artifact_claim_atomically_updates_related_evidence_and_metadata(
    tmp_path,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_atomic_checkpoint_revision",
        kind="turtlebot3_home_mission",
        title="Atomic checkpoint revision",
        artifacts={
            "turtlebot3_recovery_checkpoints": {
                "checkpoint_old": {
                    "checkpoint_id": "checkpoint_old",
                    "checkpoint_hash": "hash_old",
                    "checkpoint_status": "awaiting_operator_approval",
                }
            },
            "turtlebot3_recovery_checkpoint": {
                "checkpoint_id": "checkpoint_old",
            },
            "turtlebot3_home_mission_execution": {
                "turtlebot3_recovery_checkpoint": {
                    "checkpoint_id": "checkpoint_old",
                }
            },
        },
        metadata={"recovery_revision_number": 0},
    )
    new_checkpoint = {
        "checkpoint_id": "checkpoint_new",
        "checkpoint_hash": "hash_new",
        "checkpoint_status": "awaiting_operator_approval",
    }

    updated = store.claim_nested_artifact(
        task["task_id"],
        collection_key="turtlebot3_recovery_checkpoints",
        artifact_id="checkpoint_old",
        expected={
            "checkpoint_hash": "hash_old",
            "checkpoint_status": "awaiting_operator_approval",
        },
        updates={
            "checkpoint_status": "superseded",
            "superseded_by_checkpoint_id": "checkpoint_new",
        },
        expected_task_status="pending",
        artifacts={
            "turtlebot3_recovery_checkpoints": {
                "checkpoint_new": new_checkpoint,
            },
        },
        replace_artifacts={
            "turtlebot3_recovery_checkpoint": new_checkpoint,
            "turtlebot3_home_mission_execution": {
                "turtlebot3_recovery_checkpoint": new_checkpoint,
            },
        },
        metadata={"recovery_revision_number": 1},
        next_task_status="running",
    )

    assert updated is not None
    checkpoints = updated["artifacts"]["turtlebot3_recovery_checkpoints"]
    assert checkpoints["checkpoint_old"]["checkpoint_status"] == "superseded"
    assert (
        checkpoints["checkpoint_old"]["superseded_by_checkpoint_id"]
        == "checkpoint_new"
    )
    assert checkpoints["checkpoint_new"] == new_checkpoint
    assert updated["artifacts"]["turtlebot3_recovery_checkpoint"] == new_checkpoint
    assert (
        updated["artifacts"]["turtlebot3_home_mission_execution"][
            "turtlebot3_recovery_checkpoint"
        ]
        == new_checkpoint
    )
    assert updated["metadata"]["recovery_revision_number"] == 1
    assert updated["status"] == "running"
    assert updated["started_at"] is not None


def test_nested_artifact_claim_replaces_stale_checkpoint_copies(tmp_path) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    old_checkpoint = {
        "checkpoint_id": "checkpoint_old",
        "checkpoint_hash": "hash_old",
        "checkpoint_status": "awaiting_operator_approval",
        "selected_action": "avoid_obstacle",
        "approved_parameters": {
            "target_x_m": -0.2,
            "target_y_m": -1.4,
            "obstacle_avoidance_required": True,
        },
    }
    task = store.create(
        task_id="task_replace_stale_checkpoint_copies",
        kind="turtlebot3_home_mission",
        title="Replace stale checkpoint copies",
        artifacts={
            "checkpoints": {"checkpoint_old": old_checkpoint},
            "current_checkpoint": old_checkpoint,
            "execution": {
                "turtlebot3_recovery_checkpoint": old_checkpoint,
                "unrelated_evidence": {"preserved": True},
            },
        },
    )
    new_checkpoint = {
        "checkpoint_id": "checkpoint_new",
        "checkpoint_hash": "hash_new",
        "checkpoint_status": "awaiting_operator_approval",
        "selected_action": "avoid_obstacle",
        "approved_parameters": {
            "recovery_waypoints": [
                {"target_x_m": 0.1, "target_y_m": 0.2},
                {"target_x_m": 0.3, "target_y_m": 0.4},
            ],
            "obstacle_avoidance_required": True,
        },
    }

    updated = store.claim_nested_artifact(
        task["task_id"],
        collection_key="checkpoints",
        artifact_id="checkpoint_old",
        expected={
            "checkpoint_hash": "hash_old",
            "checkpoint_status": "awaiting_operator_approval",
        },
        updates={"checkpoint_status": "superseded"},
        artifacts={"checkpoints": {"checkpoint_new": new_checkpoint}},
        replace_artifacts={
            "current_checkpoint": new_checkpoint,
            "execution": {
                "turtlebot3_recovery_checkpoint": new_checkpoint,
                "unrelated_evidence": {"preserved": True},
            },
        },
    )

    assert updated is not None
    current = updated["artifacts"]["current_checkpoint"]
    assert current == new_checkpoint
    assert "target_x_m" not in current["approved_parameters"]
    embedded = updated["artifacts"]["execution"]
    assert embedded["turtlebot3_recovery_checkpoint"] == new_checkpoint
    assert "target_y_m" not in embedded["turtlebot3_recovery_checkpoint"][
        "approved_parameters"
    ]
    assert embedded["unrelated_evidence"] == {"preserved": True}


def test_losing_nested_artifact_claim_does_not_leak_related_updates(tmp_path) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_atomic_checkpoint_revision_race",
        kind="turtlebot3_home_mission",
        title="Atomic checkpoint revision race",
        artifacts={
            "checkpoints": {
                "checkpoint_old": {
                    "checkpoint_hash": "hash_old",
                    "checkpoint_status": "awaiting_operator_approval",
                }
            }
        },
    )
    claimant_count = 8
    start = threading.Barrier(claimant_count)

    def _revise(index: int) -> bool:
        start.wait()
        claimed = store.claim_nested_artifact(
            task["task_id"],
            collection_key="checkpoints",
            artifact_id="checkpoint_old",
            expected={
                "checkpoint_hash": "hash_old",
                "checkpoint_status": "awaiting_operator_approval",
            },
            updates={
                "checkpoint_status": "superseded",
                "superseded_by_checkpoint_id": f"checkpoint_{index}",
            },
            artifacts={
                "checkpoints": {
                    f"checkpoint_{index}": {
                        "checkpoint_status": "awaiting_operator_approval"
                    }
                },
                "winner": index,
            },
            metadata={"winner": index},
        )
        return claimed is not None

    with ThreadPoolExecutor(max_workers=claimant_count) as executor:
        winners = list(executor.map(_revise, range(claimant_count)))

    assert winners.count(True) == 1
    final = store.get(task["task_id"])
    assert final is not None
    winner = final["artifacts"]["winner"]
    assert final["metadata"]["winner"] == winner
    assert (
        final["artifacts"]["checkpoints"]["checkpoint_old"][
            "superseded_by_checkpoint_id"
        ]
        == f"checkpoint_{winner}"
    )
    assert set(final["artifacts"]["checkpoints"]) == {
        "checkpoint_old",
        f"checkpoint_{winner}",
    }


def test_nested_artifact_claim_rejects_stale_task_snapshot_without_lost_update(
    tmp_path,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    snapshot = store.create(
        task_id="task_atomic_checkpoint_stale_snapshot",
        kind="turtlebot3_home_mission_execution",
        title="Reject stale checkpoint replacement",
        status="pending",
        artifacts={
            "checkpoints": {
                "checkpoint_old": {
                    "checkpoint_hash": "hash_old",
                    "checkpoint_status": "awaiting_operator_approval",
                }
            },
            "execution": {"source_generation": 1},
        },
    )
    concurrent = store.update(
        snapshot["task_id"],
        artifacts={
            "execution": {
                "source_generation": 2,
                "new_source_evidence": True,
            }
        },
    )
    assert concurrent is not None
    assert concurrent["updated_at"] != snapshot["updated_at"]

    claimed = store.claim_nested_artifact(
        snapshot["task_id"],
        collection_key="checkpoints",
        artifact_id="checkpoint_old",
        expected={
            "checkpoint_hash": "hash_old",
            "checkpoint_status": "awaiting_operator_approval",
        },
        updates={"checkpoint_status": "superseded"},
        expected_task_status="pending",
        expected_updated_at=snapshot["updated_at"],
        replace_artifacts={
            "execution": {"source_generation": 1},
        },
    )

    assert claimed is None
    final = store.get(snapshot["task_id"])
    assert final is not None
    assert final["artifacts"]["execution"] == {
        "source_generation": 2,
        "new_source_evidence": True,
    }
    assert final["artifacts"]["checkpoints"]["checkpoint_old"][
        "checkpoint_status"
    ] == "awaiting_operator_approval"
