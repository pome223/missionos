#!/usr/bin/env python3
"""Runtime smoke for recovery decision to SITL mission upload loop (#412)."""

from __future__ import annotations

import json
from tempfile import TemporaryDirectory

from src.runtime.delivery_recovery_decision import DeliveryRecoveryAction
from src.runtime.px4_gazebo_sitl_mission_upload import MAV_MISSION_ACCEPTED
from src.runtime.px4_gazebo_sitl_recovery_upload_loop import (
    PX4_GAZEBO_SITL_RECOVERY_UPLOAD_LOOP_SCHEMA_VERSION,
    attach_px4_gazebo_sitl_recovery_upload_loop,
    build_px4_gazebo_sitl_recovery_upload_loop,
)
from src.runtime.task_store import TaskStore
from tests.test_simulated_delivery_command import NOW, _preflight, _preflight_chain


class ObservedUploader:
    def __init__(self, *, ack_type: int = MAV_MISSION_ACCEPTED):
        self.ack_type = ack_type
        self.calls = []

    def upload(self, *, items, target_endpoint, timeout_seconds):
        self.calls.append(
            {
                "items": tuple(items),
                "target_endpoint": target_endpoint,
                "timeout_seconds": timeout_seconds,
            }
        )
        return tuple(item.seq for item in items), self.ack_type


def _chain():
    chain = _preflight_chain()
    chain["preflight"] = _preflight(chain)
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


def main() -> int:
    chain = _chain()
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
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="SITL recovery upload loop smoke",
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
    summary = {
        "schema_version": loop.schema_version,
        "loop_status": loop.status.value,
        "selected_action": loop.selected_action.value,
        "external_dispatch_performed": loop.external_dispatch_performed,
        "mavlink_dispatch_performed": loop.mavlink_dispatch_performed,
        "px4_mission_upload_performed": loop.px4_mission_upload_performed,
        "bounded_iteration_count": loop.bounded_iteration_count,
        "max_iterations": loop.max_iterations,
        "receipt_created": receipt is not None,
        "receipt_upload_status": receipt.upload_status.value if receipt else None,
        "receipt_ref_count": len(loop.receipt_refs),
        "mission_item_count": receipt.mission_item_count if receipt else 0,
        "first_mission_item_command": (
            receipt.mission_items[0].command
            if receipt and receipt.mission_items
            else None
        ),
        "operator_escalation_required": loop.operator_escalation_required,
        "physical_execution_invoked": loop.physical_execution_invoked,
        "hardware_target_allowed": loop.hardware_target_allowed,
        "gazebo_entity_mutation_performed": loop.gazebo_entity_mutation_performed,
        "task_status": stored["status"] if stored else None,
        "existing_artifact_kept": bool(
            stored and stored["artifacts"]["existing"]["kept"]
        ),
        "attached_schema_version": attached["px4_gazebo_sitl_recovery_upload_loop"][
            "schema_version"
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    assert (
        summary["schema_version"] == PX4_GAZEBO_SITL_RECOVERY_UPLOAD_LOOP_SCHEMA_VERSION
    )
    assert summary["loop_status"] == "uploaded"
    assert summary["selected_action"] == "return_to_home_mission"
    assert summary["external_dispatch_performed"] is True
    assert summary["receipt_created"] is True
    assert summary["first_mission_item_command"] == 20
    assert summary["operator_escalation_required"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["gazebo_entity_mutation_performed"] is False
    assert summary["task_status"] == "running"
    assert summary["existing_artifact_kept"] is True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
