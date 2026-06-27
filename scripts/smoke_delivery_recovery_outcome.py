#!/usr/bin/env python3
"""Runtime smoke for logic-only delivery recovery outcome artifact (#430)."""

from __future__ import annotations

import json
from tempfile import TemporaryDirectory

from src.runtime.delivery_fault_event import (
    DeliveryFaultCategory,
    DeliveryFaultSeverity,
    build_delivery_fault_event,
)
from src.runtime.delivery_recovery_decision import DeliveryRecoveryAction
from src.runtime.delivery_recovery_outcome import (
    DELIVERY_RECOVERY_OUTCOME_SCHEMA_VERSION,
    DeliveryRecoveryOutcomeCategory,
    attach_delivery_recovery_outcome,
)
from src.runtime.delivery_recovery_request import build_delivery_recovery_request
from src.runtime.delivery_recovery_run import build_delivery_recovery_run
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
    run = build_delivery_recovery_run(
        delivery_mission_contract=chain["contract"],
        delivery_recovery_request=request,
        simulator_command_execution_preflight=chain["preflight"],
        simulated_command_proposal=chain["proposal"],
        simulated_command_approval=chain["approval"],
        sitl_session_ref="sitl_session:logic-only-recovery",
        observed_facts={"bounded_recovery_plan_recorded": True},
        started_at=NOW,
        finished_at=NOW,
    )
    facts = {
        "safe_landing_event_source": "logic_only_stub",
        "safe_landing_observed": True,
        "mission_terminated_safely": True,
        "vehicle_disarmed_or_landed": True,
    }
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="delivery recovery outcome smoke",
            status="running",
            artifacts={
                "existing": {"kept": True},
                "delivery_recovery_request": request.model_dump(mode="json"),
                "delivery_recovery_run": run.model_dump(mode="json"),
            },
        )
        attached = attach_delivery_recovery_outcome(
            task["task_id"],
            delivery_recovery_request=request,
            delivery_recovery_run=run,
            observed_facts=facts,
            now=NOW,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])

    outcome = attached["delivery_recovery_outcome"]
    summary = {
        "schema_version": outcome["schema_version"],
        "outcome_category": outcome["outcome_category"],
        "request_kind": outcome["request_kind"],
        "observed_facts": outcome["observed_facts"],
        "task_status": stored["status"] if stored else None,
        "artifact_persisted": bool(
            stored and "delivery_recovery_outcome" in stored["artifacts"]
        ),
        "existing_artifact_kept": bool(
            stored and stored["artifacts"]["existing"]["kept"]
        ),
        "epic_dod_satisfied": False,
        "executed_against_real_sitl": outcome["executed_against_real_sitl"],
        "recovery_chain_evidence_source": outcome["recovery_chain_evidence_source"],
        "run_executed_against_real_sitl": run.executed_against_real_sitl,
        "run_recovery_chain_evidence_source": run.recovery_chain_evidence_source,
        "invariants": {
            "logic_only_stub": outcome["logic_only_stub"],
            "real_sitl_execution_claimed": outcome["real_sitl_execution_claimed"],
            "real_sitl_chain_required_for_epic_exit": (
                outcome["real_sitl_chain_required_for_epic_exit"]
            ),
            "observed_facts_only": outcome["observed_facts_only"],
            "synthetic_success_allowed": outcome["synthetic_success_allowed"],
            "command_sent_by_verifier": outcome["command_sent_by_verifier"],
            "external_dispatch_performed_by_verifier": (
                outcome["external_dispatch_performed_by_verifier"]
            ),
            "mavlink_dispatch_performed_by_verifier": (
                outcome["mavlink_dispatch_performed_by_verifier"]
            ),
            "px4_mission_upload_performed_by_verifier": (
                outcome["px4_mission_upload_performed_by_verifier"]
            ),
            "hardware_target_allowed": outcome["hardware_target_allowed"],
            "real_hardware_target": outcome["real_hardware_target"],
            "physical_execution_invoked": outcome["physical_execution_invoked"],
            "approval_free_stronger_execution_allowed": (
                outcome["approval_free_stronger_execution_allowed"]
            ),
        },
        "environment_limitations": [
            "logic-only recovery outcome smoke; no real PX4/Gazebo SITL container was started"
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    assert summary["schema_version"] == DELIVERY_RECOVERY_OUTCOME_SCHEMA_VERSION
    assert (
        summary["outcome_category"] == DeliveryRecoveryOutcomeCategory.RECOVERED.value
    )
    assert summary["artifact_persisted"] is True
    assert summary["existing_artifact_kept"] is True
    assert summary["epic_dod_satisfied"] is False
    assert summary["executed_against_real_sitl"] is False
    assert summary["recovery_chain_evidence_source"] == "logic_only_stub"
    assert summary["run_executed_against_real_sitl"] is False
    assert summary["run_recovery_chain_evidence_source"] == "logic_only_stub"
    assert summary["invariants"]["logic_only_stub"] is True
    assert summary["invariants"]["real_sitl_execution_claimed"] is False
    assert summary["invariants"]["real_sitl_chain_required_for_epic_exit"] is True
    assert summary["invariants"]["observed_facts_only"] is True
    assert summary["invariants"]["synthetic_success_allowed"] is False
    assert summary["invariants"]["command_sent_by_verifier"] is False
    assert summary["invariants"]["external_dispatch_performed_by_verifier"] is False
    assert summary["invariants"]["mavlink_dispatch_performed_by_verifier"] is False
    assert summary["invariants"]["px4_mission_upload_performed_by_verifier"] is False
    assert summary["invariants"]["hardware_target_allowed"] is False
    assert summary["invariants"]["real_hardware_target"] is False
    assert summary["invariants"]["physical_execution_invoked"] is False
    assert summary["invariants"]["approval_free_stronger_execution_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
