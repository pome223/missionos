"""Contract replacing the fake-uploader PX4 recovery upload smoke."""

from __future__ import annotations

from pathlib import Path

from src.runtime.delivery_recovery_decision import DeliveryRecoveryAction
from src.runtime.px4_gazebo_sitl_mission_upload import MAV_MISSION_ACCEPTED
from src.runtime.px4_gazebo_sitl_recovery_upload_loop import (
    PX4_GAZEBO_SITL_RECOVERY_UPLOAD_LOOP_SCHEMA_VERSION,
    attach_px4_gazebo_sitl_recovery_upload_loop,
    build_px4_gazebo_sitl_recovery_upload_loop,
)
from src.runtime.task_store import TaskStore
from tests.contract.test_delivery_recovery_chain import (
    NOW,
    _preflight,
    _preflight_chain,
)


class ObservedUploader:
    def __init__(self, *, ack_type: int = MAV_MISSION_ACCEPTED):
        self.ack_type = ack_type
        self.calls: list[dict] = []

    def upload(self, *, items, target_endpoint, timeout_seconds):
        self.calls.append(
            {
                "items": tuple(items),
                "target_endpoint": target_endpoint,
                "timeout_seconds": timeout_seconds,
            }
        )
        return tuple(item.seq for item in items), self.ack_type


def _return_home_chain() -> dict:
    chain = _preflight_chain()
    chain.update(_preflight(chain))
    chain["decision"] = chain["decision"].model_copy(
        update={
            "primary_action": DeliveryRecoveryAction.RETURN_TO_HOME_RECOMMENDED,
            "return_to_home_recommended": True,
            "completed_no_recovery_needed": False,
            "continue_recommended": False,
            "operator_escalation_required": False,
        }
    )
    return chain


def test_recovery_upload_uses_bounded_fake_transport_and_preserves_task(
    tmp_path: Path,
) -> None:
    chain = _return_home_chain()
    uploader = ObservedUploader()
    loop, receipt = build_px4_gazebo_sitl_recovery_upload_loop(
        delivery_mission_contract=chain["contract"],
        delivery_recovery_decision=chain["decision"],
        simulator_command_execution_preflight=chain["preflight"],
        simulated_command_proposal=chain["proposal"],
        simulated_command_approval=chain["approval"],
        uploader=uploader,
        now=NOW,
    )
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="control_supervisor",
        title="SITL recovery upload contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )
    attached = attach_px4_gazebo_sitl_recovery_upload_loop(
        task_id=task["task_id"],
        delivery_mission_contract=chain["contract"],
        delivery_recovery_decision=chain["decision"],
        simulator_command_execution_preflight=chain["preflight"],
        simulated_command_proposal=chain["proposal"],
        simulated_command_approval=chain["approval"],
        uploader=ObservedUploader(),
        now=NOW,
        task_store_factory=lambda: store,
    )
    stored = store.get(task["task_id"])

    assert loop.schema_version == PX4_GAZEBO_SITL_RECOVERY_UPLOAD_LOOP_SCHEMA_VERSION
    assert loop.status.value == "uploaded"
    assert loop.selected_action.value == "return_to_home_mission"
    assert loop.external_dispatch_performed is True
    assert loop.mavlink_dispatch_performed is True
    assert loop.px4_mission_upload_performed is True
    assert loop.bounded_iteration_count <= loop.max_iterations
    assert receipt is not None
    assert receipt.upload_status.value == "uploaded"
    assert receipt.mission_item_count == 1
    assert receipt.mission_items[0].command == 20
    assert len(loop.receipt_refs) == 1
    assert len(uploader.calls) == 1
    assert loop.operator_escalation_required is False
    assert loop.physical_execution_invoked is False
    assert loop.hardware_target_allowed is False
    assert loop.gazebo_entity_mutation_performed is False
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert attached["px4_gazebo_sitl_recovery_upload_loop"]["schema_version"] == (
        PX4_GAZEBO_SITL_RECOVERY_UPLOAD_LOOP_SCHEMA_VERSION
    )
