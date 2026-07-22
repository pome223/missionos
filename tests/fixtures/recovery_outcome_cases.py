"""Shared logic-only recovery outcome cases for advisory invariance tests."""

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from src.runtime.delivery_fault_event import DeliveryFaultCategory
from src.runtime.delivery_recovery_decision import DeliveryRecoveryAction
from src.runtime.delivery_recovery_outcome import build_delivery_recovery_outcome
from src.runtime.delivery_recovery_request import (
    DeliveryRecoveryRequest,
    DeliveryRecoveryRequestKind,
    DeliveryRecoveryRequestStatus,
)
from src.runtime.delivery_recovery_run import (
    DeliveryRecoveryRun,
    DeliveryRecoveryRunStatus,
)
from src.runtime.px4_gazebo_sitl_mission_upload import PX4GazeboSITLMissionItem


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
CORPUS_PATH = Path(__file__).with_name(
    "recovery_advisory_outcome_invariance_cases.json"
)


def load_recovery_outcome_corpus() -> list[dict[str, Any]]:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def _request_kind(case: Mapping[str, Any]) -> DeliveryRecoveryRequestKind:
    category = DeliveryFaultCategory(str(case["fault_category"]))
    action = DeliveryRecoveryAction(str(case["action"]))
    if category is DeliveryFaultCategory.VEHICLE_HEALTH_UNSAFE:
        return DeliveryRecoveryRequestKind.OPERATOR_ESCALATION_ONLY
    if category is DeliveryFaultCategory.PAYLOAD_RELEASE_NOT_OBSERVED:
        return DeliveryRecoveryRequestKind.RETRY_DROPOFF_SIMULATION
    if action is DeliveryRecoveryAction.HOLD_RECOMMENDED:
        return DeliveryRecoveryRequestKind.HOLD_POSITION_SIMULATION
    if action in {
        DeliveryRecoveryAction.ABORT,
        DeliveryRecoveryAction.ABORT_RECOMMENDED,
    }:
        return DeliveryRecoveryRequestKind.ABORT_AND_LAND_SIMULATION
    return DeliveryRecoveryRequestKind.RETURN_TO_HOME_SIMULATION


def _mission_item(kind: DeliveryRecoveryRequestKind) -> PX4GazeboSITLMissionItem:
    command = {
        DeliveryRecoveryRequestKind.RETURN_TO_HOME_SIMULATION: 20,
        DeliveryRecoveryRequestKind.ABORT_AND_LAND_SIMULATION: 21,
        DeliveryRecoveryRequestKind.RETRY_DROPOFF_SIMULATION: 16,
        DeliveryRecoveryRequestKind.HOLD_POSITION_SIMULATION: 19,
    }[kind]
    return PX4GazeboSITLMissionItem(
        seq=0,
        command=command,
        latitude_deg=35.0,
        longitude_deg=139.0,
        altitude_m=30.0,
    )


def run_logic_only_recovery_outcome_case(
    case: Mapping[str, Any],
    _advisory_context: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Run the production outcome verifier without reading advisory context."""

    case_id = str(case["id"])
    kind = _request_kind(case)
    blocked = kind is DeliveryRecoveryRequestKind.OPERATOR_ESCALATION_ONLY
    blocked_reasons = ("vehicle_health_unsafe_requires_operator",) if blocked else ()
    request = DeliveryRecoveryRequest(
        request_id=f"request-{case_id}",
        mission_contract_ref="delivery_mission_contract:advisory-invariance",
        recovery_decision_ref="delivery_recovery_decision:advisory-invariance",
        fault_event_ref=f"delivery_fault_event:{case_id}",
        operator_minimal_delivery_simulation_status_ref=(
            "operator_minimal_delivery_simulation_status:advisory-invariance"
        ),
        request_kind=kind,
        request_status=(
            DeliveryRecoveryRequestStatus.BLOCKED
            if blocked
            else DeliveryRecoveryRequestStatus.READY
        ),
        compiled_from_action=DeliveryRecoveryAction(str(case["action"])),
        fault_category=DeliveryFaultCategory(str(case["fault_category"])),
        blocked_reasons=blocked_reasons,
        created_at=NOW,
    )
    items = () if blocked else (_mission_item(kind),)
    run = DeliveryRecoveryRun(
        recovery_run_id=f"run-{case_id}",
        recovery_request_ref=f"delivery_recovery_request:{request.request_id}",
        mission_contract_ref=request.mission_contract_ref,
        simulator_command_execution_preflight_ref=(
            "simulator_command_execution_preflight:advisory-invariance"
        ),
        simulated_command_proposal_ref=(
            "simulated_command_proposal:advisory-invariance"
        ),
        simulated_command_approval_ref=(
            "simulated_command_approval:advisory-invariance"
        ),
        sitl_session_ref="sitl_session:logic-only-advisory-invariance",
        execution_scope=(
            "blocked_no_execution" if blocked else "logic_only_stub_recovery_plan"
        ),
        planned_mission_items=items,
        mission_item_count=len(items),
        recovery_request_kind=kind,
        status=(
            DeliveryRecoveryRunStatus.BLOCKED
            if blocked
            else DeliveryRecoveryRunStatus.LOGIC_ONLY_RECORDED
        ),
        blocked_reasons=blocked_reasons,
        started_at=NOW,
        finished_at=NOW,
    )
    outcome = build_delivery_recovery_outcome(
        delivery_recovery_request=request,
        delivery_recovery_run=run,
        observed_facts=dict(case.get("facts") or {}),
        now=NOW,
    )
    return {"delivery_recovery_outcome": outcome.model_dump(mode="json")}


__all__ = [
    "NOW",
    "load_recovery_outcome_corpus",
    "run_logic_only_recovery_outcome_case",
]
