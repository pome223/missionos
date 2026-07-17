from pathlib import Path

from src.runtime.delivery_fault_event import (
    DeliveryFaultCategory,
    DeliveryFaultSeverity,
    build_delivery_fault_event,
)
from src.runtime.delivery_recovery_loop import (
    DELIVERY_RECOVERY_LOOP_SCHEMA_VERSION,
    attach_delivery_recovery_loop,
)
from src.runtime.task_store import TaskStore


def test_logic_only_recovery_loop_preserves_task_without_execution_claim(
    tmp_path: Path,
) -> None:
    fault = build_delivery_fault_event(
        fault_category=DeliveryFaultCategory.BATTERY_RESERVE_VIOLATION,
        severity=DeliveryFaultSeverity.BLOCKING,
        telemetry_refs=["px4_gazebo_sanitized_telemetry:logic-only-battery"],
        episode_ref="simulated_delivery_episode:logic-only-episode",
        bounded_run_ref="px4_gazebo_bounded_simulation_run:logic-only-run",
        evidence_refs=["delivery_episode_review:logic-only-review"],
        blocked_reasons=["battery_reserve_violation"],
    )
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="control_supervisor",
        title="Delivery recovery loop contract",
        status="running",
        artifacts={
            "existing": {"kept": True},
            "delivery_mission_contract": {
                "schema_version": "delivery_mission_contract.v1",
                "contract_id": "logic-only-contract",
            },
            "simulated_delivery_episode": {
                "schema_version": "simulated_delivery_episode.v1",
                "episode_id": "logic-only-episode",
            },
            "delivery_fault_event": fault.model_dump(mode="json"),
            "delivery_recovery_decision": {
                "schema_version": "delivery_recovery_decision.v1",
                "decision_id": "logic-only-decision",
            },
            "px4_gazebo_bounded_simulation_run": {
                "schema_version": "px4_gazebo_bounded_simulation_run.v1",
                "run_id": "logic-only-run",
            },
            "px4_gazebo_sitl_mission_upload_receipt": {
                "schema_version": "px4_gazebo_sitl_mission_upload_receipt.v1",
                "receipt_id": "previous-logic-only",
            },
        },
    )

    attached = attach_delivery_recovery_loop(
        task["task_id"],
        previous_receipt_refs=(
            "px4_gazebo_sitl_mission_upload_receipt:previous-logic-only",
        ),
        task_store_factory=lambda: store,
    )
    stored = store.get(task["task_id"])
    loop = attached["delivery_recovery_loop"]

    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert loop["schema_version"] == DELIVERY_RECOVERY_LOOP_SCHEMA_VERSION
    assert fault.fault_category.value == "battery_reserve_violation"
    assert loop["executed_against_real_sitl"] is False
    assert loop["recovery_chain_evidence_source"] == "logic_only_stub"
    assert fault.executed_against_real_sitl is False
    assert fault.recovery_chain_evidence_source == "logic_only_stub"
    for field in (
        "physical_execution_invoked",
        "hardware_target_allowed",
        "real_hardware_target",
        "approval_free_stronger_execution_allowed",
    ):
        assert loop[field] is False
