#!/usr/bin/env python3
"""Runtime smoke for delivery fault events and recovery loop refs (#426/#427)."""

from __future__ import annotations

import json
from tempfile import TemporaryDirectory

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


def main() -> int:
    fault = build_delivery_fault_event(
        fault_category=DeliveryFaultCategory.BATTERY_RESERVE_VIOLATION,
        severity=DeliveryFaultSeverity.BLOCKING,
        telemetry_refs=["px4_gazebo_sanitized_telemetry:logic-only-battery"],
        episode_ref="simulated_delivery_episode:logic-only-episode",
        bounded_run_ref="px4_gazebo_bounded_simulation_run:logic-only-run",
        evidence_refs=["delivery_episode_review:logic-only-review"],
        blocked_reasons=["battery_reserve_violation"],
    )
    previous_ref = "px4_gazebo_sitl_mission_upload_receipt:previous-logic-only"
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="delivery recovery artifacts smoke",
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
            previous_receipt_refs=[previous_ref],
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])
    loop = attached["delivery_recovery_loop"]
    summary = {
        "schema_version": loop["schema_version"],
        "fault_schema_version": fault.schema_version,
        "fault_category": fault.fault_category.value,
        "loop_status": loop["loop_status"],
        "previous_receipt_refs": loop["previous_receipt_refs"],
        "task_status": stored["status"] if stored else None,
        "existing_artifact_kept": bool(
            stored and stored["artifacts"]["existing"]["kept"]
        ),
        "epic_dod_satisfied": False,
        "executed_against_real_sitl": loop["executed_against_real_sitl"],
        "recovery_chain_evidence_source": loop["recovery_chain_evidence_source"],
        "fault_executed_against_real_sitl": fault.executed_against_real_sitl,
        "fault_recovery_chain_evidence_source": (fault.recovery_chain_evidence_source),
        "invariants": {
            "physical_execution_invoked": loop["physical_execution_invoked"],
            "hardware_target_allowed": loop["hardware_target_allowed"],
            "real_hardware_target": loop["real_hardware_target"],
            "approval_free_stronger_execution_allowed": (
                loop["approval_free_stronger_execution_allowed"]
            ),
        },
        "environment_limitations": [
            "logic-only artifact smoke; no real PX4/Gazebo SITL container was started"
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    assert summary["schema_version"] == DELIVERY_RECOVERY_LOOP_SCHEMA_VERSION
    assert summary["fault_category"] == "battery_reserve_violation"
    assert summary["task_status"] == "running"
    assert summary["existing_artifact_kept"] is True
    assert summary["epic_dod_satisfied"] is False
    assert summary["executed_against_real_sitl"] is False
    assert summary["recovery_chain_evidence_source"] == "logic_only_stub"
    assert summary["fault_executed_against_real_sitl"] is False
    assert summary["fault_recovery_chain_evidence_source"] == "logic_only_stub"
    assert summary["invariants"]["physical_execution_invoked"] is False
    assert summary["invariants"]["hardware_target_allowed"] is False
    assert summary["invariants"]["real_hardware_target"] is False
    assert summary["invariants"]["approval_free_stronger_execution_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
