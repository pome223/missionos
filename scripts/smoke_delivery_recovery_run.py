#!/usr/bin/env python3
"""Runtime smoke for logic-only delivery recovery run artifact (#429)."""

from __future__ import annotations

import json
from tempfile import TemporaryDirectory

from src.runtime.delivery_fault_event import (
    DeliveryFaultCategory,
    DeliveryFaultSeverity,
    build_delivery_fault_event,
)
from src.runtime.delivery_recovery_decision import DeliveryRecoveryAction
from src.runtime.delivery_recovery_request import build_delivery_recovery_request
from src.runtime.delivery_recovery_run import (
    DELIVERY_RECOVERY_RUN_SCHEMA_VERSION,
    DeliveryRecoveryRunStatus,
    attach_delivery_recovery_run,
)
from src.runtime.px4_gazebo_sitl_mission_upload import MAV_CMD_NAV_RETURN_TO_LAUNCH
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
    request = build_delivery_recovery_request(
        delivery_mission_contract=chain["contract"],
        delivery_recovery_decision=decision,
        delivery_fault_event=fault,
        operator_minimal_delivery_simulation_status=chain["operator_status"],
        now=NOW,
    )
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="delivery recovery run smoke",
            status="running",
            artifacts={
                "existing": {"kept": True},
                "delivery_recovery_request": request.model_dump(mode="json"),
            },
        )
        attached = attach_delivery_recovery_run(
            task["task_id"],
            delivery_mission_contract=chain["contract"],
            delivery_recovery_request=request,
            simulator_command_execution_preflight=chain["preflight"],
            simulated_command_proposal=chain["proposal"],
            simulated_command_approval=chain["approval"],
            sitl_session_ref="sitl_session:logic-only-recovery",
            observed_facts={"bounded_recovery_plan_recorded": True},
            started_at=NOW,
            finished_at=NOW,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])

    run = attached["delivery_recovery_run"]
    summary = {
        "schema_version": run["schema_version"],
        "status": run["status"],
        "execution_scope": run["execution_scope"],
        "recovery_request_kind": run["recovery_request_kind"],
        "mission_item_count": run["mission_item_count"],
        "planned_commands": [item["command"] for item in run["planned_mission_items"]],
        "task_status": stored["status"] if stored else None,
        "artifact_persisted": bool(
            stored and "delivery_recovery_run" in stored["artifacts"]
        ),
        "existing_artifact_kept": bool(
            stored and stored["artifacts"]["existing"]["kept"]
        ),
        "epic_dod_satisfied": False,
        "executed_against_real_sitl": run["executed_against_real_sitl"],
        "recovery_chain_evidence_source": run["recovery_chain_evidence_source"],
        "request_executed_against_real_sitl": request.executed_against_real_sitl,
        "request_recovery_chain_evidence_source": (
            request.recovery_chain_evidence_source
        ),
        "invariants": {
            "logic_only_stub": run["logic_only_stub"],
            "real_sitl_execution_claimed": run["real_sitl_execution_claimed"],
            "mission_upload_performed": run["mission_upload_performed"],
            "external_dispatch_performed": run["external_dispatch_performed"],
            "mavlink_dispatch_performed": run["mavlink_dispatch_performed"],
            "px4_mission_upload_performed": run["px4_mission_upload_performed"],
            "gazebo_simulator_command_performed": (
                run["gazebo_simulator_command_performed"]
            ),
            "hardware_target_allowed": run["hardware_target_allowed"],
            "real_hardware_target": run["real_hardware_target"],
            "physical_execution_invoked": run["physical_execution_invoked"],
            "approval_free_stronger_execution_allowed": (
                run["approval_free_stronger_execution_allowed"]
            ),
        },
        "environment_limitations": [
            "logic-only recovery run smoke; no real PX4/Gazebo SITL container was started"
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    assert summary["schema_version"] == DELIVERY_RECOVERY_RUN_SCHEMA_VERSION
    assert summary["status"] == DeliveryRecoveryRunStatus.LOGIC_ONLY_RECORDED.value
    assert summary["execution_scope"] == "logic_only_stub_recovery_plan"
    assert summary["mission_item_count"] == 1
    assert summary["planned_commands"] == [MAV_CMD_NAV_RETURN_TO_LAUNCH]
    assert summary["artifact_persisted"] is True
    assert summary["existing_artifact_kept"] is True
    assert summary["epic_dod_satisfied"] is False
    assert summary["executed_against_real_sitl"] is False
    assert summary["recovery_chain_evidence_source"] == "logic_only_stub"
    assert summary["request_executed_against_real_sitl"] is False
    assert summary["request_recovery_chain_evidence_source"] == "logic_only_stub"
    assert summary["invariants"]["logic_only_stub"] is True
    assert summary["invariants"]["real_sitl_execution_claimed"] is False
    assert summary["invariants"]["mission_upload_performed"] is False
    assert summary["invariants"]["external_dispatch_performed"] is False
    assert summary["invariants"]["mavlink_dispatch_performed"] is False
    assert summary["invariants"]["px4_mission_upload_performed"] is False
    assert summary["invariants"]["gazebo_simulator_command_performed"] is False
    assert summary["invariants"]["hardware_target_allowed"] is False
    assert summary["invariants"]["real_hardware_target"] is False
    assert summary["invariants"]["physical_execution_invoked"] is False
    assert summary["invariants"]["approval_free_stronger_execution_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
