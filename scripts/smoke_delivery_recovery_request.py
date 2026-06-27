#!/usr/bin/env python3
"""Runtime smoke for delivery recovery request artifact (#428)."""

from __future__ import annotations

import json
from tempfile import TemporaryDirectory

from src.runtime.delivery_fault_event import (
    DeliveryFaultCategory,
    DeliveryFaultSeverity,
    build_delivery_fault_event,
)
from src.runtime.delivery_recovery_decision import DeliveryRecoveryAction
from src.runtime.delivery_recovery_request import (
    DELIVERY_RECOVERY_REQUEST_SCHEMA_VERSION,
    DeliveryRecoveryRequestKind,
    DeliveryRecoveryRequestStatus,
    attach_delivery_recovery_request,
)
from src.runtime.task_store import TaskStore
from tests.test_simulated_delivery_command import NOW, _preflight, _preflight_chain


def main() -> int:
    chain = _preflight_chain()
    chain["preflight"] = _preflight(chain)
    decision = chain["decision"].model_copy(
        update={
            "primary_action": DeliveryRecoveryAction.RETURN_TO_HOME_RECOMMENDED,
            "return_to_home_recommended": True,
            "abort_recommended": False,
            "hold_recommended": False,
            "hold_proposed": False,
            "operator_escalation_required": False,
        }
    )
    fault = build_delivery_fault_event(
        fault_category=DeliveryFaultCategory.BATTERY_LOW,
        severity=DeliveryFaultSeverity.BLOCKING,
        telemetry_refs=["px4_gazebo_sanitized_telemetry:logic-only-battery"],
        episode_ref=chain["operator_status"].simulated_delivery_episode_ref,
        evidence_refs=[chain["operator_status"].delivery_episode_review_ref],
        blocked_reasons=["battery_low"],
        observed_at=NOW,
    )
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="delivery recovery request smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        attached = attach_delivery_recovery_request(
            task["task_id"],
            delivery_mission_contract=chain["contract"],
            delivery_recovery_decision=decision,
            delivery_fault_event=fault,
            operator_minimal_delivery_simulation_status=chain["operator_status"],
            now=NOW,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])

    request = attached["delivery_recovery_request"]
    summary = {
        "schema_version": request["schema_version"],
        "request_kind": request["request_kind"],
        "request_status": request["request_status"],
        "compiled_from_action": request["compiled_from_action"],
        "fault_category": request["fault_category"],
        "task_status": stored["status"] if stored else None,
        "artifact_persisted": bool(
            stored and "delivery_recovery_request" in stored["artifacts"]
        ),
        "existing_artifact_kept": bool(
            stored and stored["artifacts"]["existing"]["kept"]
        ),
        "epic_dod_satisfied": False,
        "executed_against_real_sitl": request["executed_against_real_sitl"],
        "recovery_chain_evidence_source": request["recovery_chain_evidence_source"],
        "fault_executed_against_real_sitl": fault.executed_against_real_sitl,
        "fault_recovery_chain_evidence_source": (fault.recovery_chain_evidence_source),
        "invariants": {
            "request_only": request["request_only"],
            "bounded": request["bounded"],
            "command_payload_allowed": request["command_payload_allowed"],
            "raw_mavlink_command_allowed": request["raw_mavlink_command_allowed"],
            "raw_ros_action_allowed": request["raw_ros_action_allowed"],
            "setpoint_stream_allowed": request["setpoint_stream_allowed"],
            "actuator_command_allowed": request["actuator_command_allowed"],
            "physical_execution_invoked": request["physical_execution_invoked"],
            "hardware_target_allowed": request["hardware_target_allowed"],
            "real_hardware_target": request["real_hardware_target"],
            "approval_free_stronger_execution_allowed": (
                request["approval_free_stronger_execution_allowed"]
            ),
        },
        "environment_limitations": [
            "logic-only request smoke; no real PX4/Gazebo SITL container was started"
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    assert summary["schema_version"] == DELIVERY_RECOVERY_REQUEST_SCHEMA_VERSION
    assert summary["request_kind"] == (
        DeliveryRecoveryRequestKind.RETURN_TO_HOME_SIMULATION.value
    )
    assert summary["request_status"] == DeliveryRecoveryRequestStatus.READY.value
    assert summary["artifact_persisted"] is True
    assert summary["existing_artifact_kept"] is True
    assert summary["epic_dod_satisfied"] is False
    assert summary["executed_against_real_sitl"] is False
    assert summary["recovery_chain_evidence_source"] == "logic_only_stub"
    assert summary["fault_executed_against_real_sitl"] is False
    assert summary["fault_recovery_chain_evidence_source"] == "logic_only_stub"
    assert summary["invariants"]["request_only"] is True
    assert summary["invariants"]["bounded"] is True
    assert summary["invariants"]["command_payload_allowed"] is False
    assert summary["invariants"]["raw_mavlink_command_allowed"] is False
    assert summary["invariants"]["raw_ros_action_allowed"] is False
    assert summary["invariants"]["setpoint_stream_allowed"] is False
    assert summary["invariants"]["actuator_command_allowed"] is False
    assert summary["invariants"]["physical_execution_invoked"] is False
    assert summary["invariants"]["hardware_target_allowed"] is False
    assert summary["invariants"]["real_hardware_target"] is False
    assert summary["invariants"]["approval_free_stronger_execution_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
