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
