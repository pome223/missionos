from __future__ import annotations

from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from pydantic import ValidationError

from src.runtime.hardware_adapter_contract import (
    HardwareAckStatus,
    HardwareActionKind,
    HardwareAdapterContractError,
    HardwareAdapterEvidence,
    HardwareAdapterKind,
    HardwareDispatchStatus,
    HardwareExecutionMode,
    HardwarePreflightStatus,
    HardwareVehicleClass,
    SCHEMA_EXAMPLE_ADAPTER_ID,
    build_blocked_px4_bench_hardware_adapter_evidence,
    build_px4_bench_hardware_adapter_preflight,
    build_px4_bench_safe_stop_hardware_adapter_evidence,
    build_schema_example_capabilities,
)
from src.runtime.missionos_llm_schemas import (
    LLM_INVOCATION_EVIDENCE_SCHEMA_VERSION,
    LLM_RESPONSE_PROPOSAL_SCHEMA_VERSION,
)
from src.runtime.missionos_real_hardware_dispatch_runtime import (
    REAL_HARDWARE_DISPATCH_BACKEND_TARGET,
    REAL_HARDWARE_DISPATCH_RESPONSE_KIND,
    invoke_missionos_real_hardware_dispatch_runtime,
)
from src.runtime.px4_mavlink_ack_state import MAV_RESULT_ACCEPTED
from src.runtime.px4_real_hardware_actuator_backend import (
    MAV_CMD_COMPONENT_ARM_DISARM,
    build_px4_real_hardware_actuator_approval,
)
from src.runtime.px4_real_hardware_mavlink_reader import MAV_MODE_FLAG_SAFETY_ARMED
from src.runtime.task_store import TaskStore

pytestmark = pytest.mark.contract


class _LoopbackMessage(dict):
    def get_type(self) -> str:
        return str(self["type"])


class _LoopbackMav:
    def __init__(self, connection: "_LoopbackMavlinkConnection") -> None:
        self._connection = connection

    def command_long_send(
        self,
        _target_system: int,
        _target_component: int,
        command_id: int,
        _confirmation: int,
        param1: float,
        *_params: float,
    ) -> None:
        self._connection.commands.append({"command_id": command_id, "param1": param1})
        if command_id == MAV_CMD_COMPONENT_ARM_DISARM:
            self._connection.armed = bool(param1)


class _LoopbackMavlinkConnection:
    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []
        self.armed = False
        self.closed = False
        self.mav = _LoopbackMav(self)

    def recv_match(
        self,
        *,
        type: str | list[str],
        blocking: bool,
        timeout: float,
    ) -> _LoopbackMessage:
        assert blocking is True
        assert timeout > 0
        if type == "COMMAND_ACK":
            command_id = self.commands[-1]["command_id"]
            return _LoopbackMessage(
                type="COMMAND_ACK",
                command=command_id,
                result=MAV_RESULT_ACCEPTED,
            )
        expected = set(type)
        assert "HEARTBEAT" in expected
        return _LoopbackMessage(
            type="HEARTBEAT",
            base_mode=MAV_MODE_FLAG_SAFETY_ARMED if self.armed else 0,
            system_status=4,
        )

    def close(self) -> None:
        self.closed = True


def _proposal() -> dict[str, Any]:
    return {
        "schema_version": LLM_RESPONSE_PROPOSAL_SCHEMA_VERSION,
        "llm_judgment_in_gate": False,
        "progress_counted": False,
        "goal_640_progress_counted": False,
        "ai_agent_progress_counted": False,
        "drone_physics_affected": False,
        "dispatch_authority_created": False,
        "operator_approved": False,
        "llm_invocation_evidence": {
            "schema_version": LLM_INVOCATION_EVIDENCE_SCHEMA_VERSION,
            "model_id": "contract-test",
            "prompt_sha256": "0" * 64,
            "response_sha256": "1" * 64,
            "temperature": 0.0,
            "replay_n_runs": 1,
            "replay_agreement_ratio": 1.0,
            "llm_judgment_in_gate": False,
        },
        "response_kind": REAL_HARDWARE_DISPATCH_RESPONSE_KIND,
        "parameters": {},
        "rationale": "Contract test proposal only.",
        "expected_outcome": "Operator-gated bench arm/disarm may be dispatched.",
        "uncertainty": "Loopback connection is not physical execution.",
        "approval_request": "Operator approval required before dispatch.",
    }


def test_schema_example_capabilities_are_not_an_adapter_runtime() -> None:
    capabilities = build_schema_example_capabilities()

    assert capabilities.adapter_id == SCHEMA_EXAMPLE_ADAPTER_ID
    assert capabilities.adapter_kind is HardwareAdapterKind.SCHEMA_EXAMPLE_ONLY
    assert capabilities.execution_mode is HardwareExecutionMode.SCHEMA_EXAMPLE_ONLY
    assert capabilities.requires_operator_approval is True
    assert capabilities.requires_fresh_telemetry is True
    assert capabilities.requires_geofence is True
    assert HardwareActionKind.NAV2_GOAL_POSE in capabilities.allowed_actions
    assert HardwareActionKind.LAND in capabilities.blocked_actions


def test_hardware_adapter_evidence_rejects_dispatch_without_operator_approval() -> None:
    with pytest.raises((HardwareAdapterContractError, ValidationError)):
        HardwareAdapterEvidence(
            adapter_id=SCHEMA_EXAMPLE_ADAPTER_ID,
            adapter_kind=HardwareAdapterKind.SCHEMA_EXAMPLE_ONLY,
            vehicle_class=HardwareVehicleClass.GROUND_ROBOT,
            execution_mode=HardwareExecutionMode.SCHEMA_EXAMPLE_ONLY,
            missionos_action_ref="missionos_action:unsafe",
            adapter_action_kind=HardwareActionKind.NAV2_GOAL_POSE,
            preflight_status=HardwarePreflightStatus.PASSED,
            dispatch_status=HardwareDispatchStatus.SENT,
            dispatch_request_sent=True,
            command_ack_observed=True,
            ack_status=HardwareAckStatus.ACCEPTED,
            telemetry_fresh=True,
        )


def test_hardware_adapter_evidence_rejects_ack_as_completion() -> None:
    with pytest.raises((HardwareAdapterContractError, ValidationError)):
        HardwareAdapterEvidence(
            adapter_id="px4_real_hardware_bench_adapter.v1",
            adapter_kind=HardwareAdapterKind.PX4_MAVLINK,
            vehicle_class=HardwareVehicleClass.MULTIROTOR,
            execution_mode=HardwareExecutionMode.LOOPBACK,
            missionos_action_ref="operator_gated_real_hardware_arm_disarm",
            adapter_action_kind=HardwareActionKind.PX4_ARM_DISARM_BENCH,
            operator_approval_ref="operator_dispatch_approval:test",
            preflight_status=HardwarePreflightStatus.PASSED,
            dispatch_status=HardwareDispatchStatus.SENT,
            dispatch_request_sent=True,
            command_ack_observed=True,
            ack_status=HardwareAckStatus.ACCEPTED,
            runtime_progress_observed=False,
            completion_claimed=True,
            telemetry_fresh=True,
        )


def test_hardware_adapter_evidence_rejects_dispatch_with_stale_telemetry() -> None:
    with pytest.raises((HardwareAdapterContractError, ValidationError)):
        HardwareAdapterEvidence(
            adapter_id="px4_real_hardware_bench_adapter.v1",
            adapter_kind=HardwareAdapterKind.PX4_MAVLINK,
            vehicle_class=HardwareVehicleClass.MULTIROTOR,
            execution_mode=HardwareExecutionMode.BENCH,
            missionos_action_ref="operator_gated_real_hardware_arm_disarm",
            adapter_action_kind=HardwareActionKind.PX4_ARM_DISARM_BENCH,
            operator_approval_ref="operator_dispatch_approval:test",
            preflight_status=HardwarePreflightStatus.PASSED,
            dispatch_status=HardwareDispatchStatus.SENT,
            dispatch_request_sent=True,
            command_ack_observed=False,
            ack_status=HardwareAckStatus.TIMEOUT,
            telemetry_fresh=False,
        )


def test_stale_telemetry_preflight_and_blocked_evidence_fail_closed() -> None:
    preflight = build_px4_bench_hardware_adapter_preflight(
        blocking_reasons=("telemetry_stale",),
        telemetry_fresh=False,
    )
    evidence = build_blocked_px4_bench_hardware_adapter_evidence(
        missionos_action_ref="operator_gated_real_hardware_arm_disarm",
        operator_approval_ref="operator_dispatch_approval:stale",
        blocking_reasons=preflight.blocking_reasons,
        telemetry_fresh=preflight.telemetry_fresh,
    )

    assert preflight.preflight_status is HardwarePreflightStatus.BLOCKED
    assert evidence.dispatch_status is HardwareDispatchStatus.BLOCKED
    assert evidence.dispatch_request_sent is False
    assert evidence.telemetry_fresh is False
    assert "telemetry_stale" in evidence.blocking_reasons
    assert "dispatch_not_sent" in evidence.unproven_claims


def test_heartbeat_loss_requests_safe_stop_without_command_dispatch() -> None:
    evidence = build_px4_bench_safe_stop_hardware_adapter_evidence(
        missionos_action_ref="operator_gated_real_hardware_arm_disarm",
        operator_approval_ref="operator_dispatch_approval:heartbeat",
        blocking_reasons=("heartbeat_lost",),
        telemetry_fresh=False,
    )

    assert evidence.dispatch_status is HardwareDispatchStatus.SAFE_STOP_REQUESTED
    assert evidence.safe_stop_requested is True
    assert evidence.dispatch_request_sent is False
    assert evidence.command_ack_observed is False
    assert "heartbeat_lost" in evidence.blocking_reasons
    assert "dispatch_preempted_by_safe_stop" in evidence.unproven_claims


def test_operator_abort_preempts_dispatch_and_requests_safe_stop() -> None:
    evidence = build_px4_bench_safe_stop_hardware_adapter_evidence(
        missionos_action_ref="operator_gated_real_hardware_arm_disarm",
        operator_approval_ref="operator_dispatch_approval:abort",
        blocking_reasons=("operator_abort_requested",),
        abort_requested=True,
    )

    assert evidence.abort_requested is True
    assert evidence.safe_stop_requested is True
    assert evidence.dispatch_status is HardwareDispatchStatus.SAFE_STOP_REQUESTED
    assert evidence.dispatch_request_sent is False
    assert "operator_abort_requested" in evidence.blocking_reasons


def test_abort_claim_without_safe_stop_is_rejected() -> None:
    with pytest.raises((HardwareAdapterContractError, ValidationError)):
        HardwareAdapterEvidence(
            adapter_id="px4_real_hardware_bench_adapter.v1",
            adapter_kind=HardwareAdapterKind.PX4_MAVLINK,
            vehicle_class=HardwareVehicleClass.MULTIROTOR,
            execution_mode=HardwareExecutionMode.BENCH,
            missionos_action_ref="operator_gated_real_hardware_arm_disarm",
            adapter_action_kind=HardwareActionKind.PX4_ARM_DISARM_BENCH,
            operator_approval_ref="operator_dispatch_approval:test",
            preflight_status=HardwarePreflightStatus.BLOCKED,
            dispatch_status=HardwareDispatchStatus.BLOCKED,
            dispatch_request_sent=False,
            command_ack_observed=False,
            ack_status=HardwareAckStatus.NOT_REQUESTED,
            telemetry_fresh=True,
            abort_requested=True,
            safe_stop_requested=False,
            blocking_reasons=("operator_abort_requested",),
        )


def test_operating_volume_violation_blocks_dispatch() -> None:
    preflight = build_px4_bench_hardware_adapter_preflight(
        blocking_reasons=("operating_volume_violation",),
        operating_volume_satisfied=False,
    )
    evidence = build_blocked_px4_bench_hardware_adapter_evidence(
        missionos_action_ref="operator_gated_real_hardware_arm_disarm",
        operator_approval_ref="operator_dispatch_approval:volume",
        blocking_reasons=preflight.blocking_reasons,
    )

    assert preflight.operating_volume_satisfied is False
    assert evidence.dispatch_status is HardwareDispatchStatus.BLOCKED
    assert evidence.dispatch_request_sent is False
    assert "operating_volume_violation" in evidence.blocking_reasons


def test_hardware_adapter_evidence_rejects_loopback_as_adapter_action_completion() -> None:
    with pytest.raises((HardwareAdapterContractError, ValidationError)):
        HardwareAdapterEvidence(
            adapter_id="px4_real_hardware_bench_adapter.v1",
            adapter_kind=HardwareAdapterKind.PX4_MAVLINK,
            vehicle_class=HardwareVehicleClass.MULTIROTOR,
            execution_mode=HardwareExecutionMode.LOOPBACK,
            missionos_action_ref="operator_gated_real_hardware_arm_disarm",
            adapter_action_kind=HardwareActionKind.PX4_ARM_DISARM_BENCH,
            operator_approval_ref="operator_dispatch_approval:test",
            preflight_status=HardwarePreflightStatus.PASSED,
            dispatch_status=HardwareDispatchStatus.SENT,
            dispatch_request_sent=True,
            command_ack_observed=True,
            ack_status=HardwareAckStatus.ACCEPTED,
            runtime_progress_observed=True,
            completion_claimed=True,
            completion_scope="adapter_action",
            telemetry_fresh=True,
        )


def test_hardware_adapter_evidence_rejects_mission_completion_for_bench_action() -> None:
    with pytest.raises((HardwareAdapterContractError, ValidationError)):
        HardwareAdapterEvidence(
            adapter_id="px4_real_hardware_bench_adapter.v1",
            adapter_kind=HardwareAdapterKind.PX4_MAVLINK,
            vehicle_class=HardwareVehicleClass.MULTIROTOR,
            execution_mode=HardwareExecutionMode.LOOPBACK,
            missionos_action_ref="operator_gated_real_hardware_arm_disarm",
            adapter_action_kind=HardwareActionKind.PX4_ARM_DISARM_BENCH,
            operator_approval_ref="operator_dispatch_approval:test",
            preflight_status=HardwarePreflightStatus.PASSED,
            dispatch_status=HardwareDispatchStatus.SENT,
            dispatch_request_sent=True,
            command_ack_observed=True,
            ack_status=HardwareAckStatus.ACCEPTED,
            runtime_progress_observed=True,
            completion_claimed=True,
            completion_scope="mission",
            telemetry_fresh=True,
        )


def test_real_hardware_dispatch_runtime_writes_adapter_evidence_from_executor_result() -> None:
    now = datetime(2026, 6, 28, tzinfo=timezone.utc)
    connection = _LoopbackMavlinkConnection()

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_real_hardware_actuator_dispatch",
            title="PX4 bench adapter evidence contract test",
            status="running",
            artifacts={},
        )
        approval = build_px4_real_hardware_actuator_approval(
            approved_operations=("arm", "disarm"),
            physical_attestation={
                "propellers_removed": True,
                "operator_physically_present": True,
                "attesting_operator_id": "operator-contract-test",
                "attested_at": now.isoformat(),
            },
            now=now,
        )
        result = invoke_missionos_real_hardware_dispatch_runtime(
            store=store,
            task_id=task["task_id"],
            subject_id="pixhawk-loopback-contract-test",
            approval=approval,
            approval_actor="operator-contract-test",
            dispatch_validation={
                "validation_status": "valid",
                "operator_approval_consumed": True,
                "backend_target": REAL_HARDWARE_DISPATCH_BACKEND_TARGET,
                "authority_id": "authority-contract-test",
                "operator_approval_id": "operator_dispatch_approval:contract-test",
                "gate_result_id": "gate-contract-test",
            },
            llm_response_proposal=_proposal(),
            connection_factory=lambda: connection,
            clock=lambda: now,
        )
        updated = store.get(task["task_id"])

    assert updated is not None
    assert result["runtime_invoked"] is True
    assert len(connection.commands) == 2
    assert connection.closed is True

    artifacts = updated["artifacts"]
    assert artifacts["missionos_hardware_adapter_capabilities"][0]["adapter_kind"] == (
        "px4_mavlink"
    )
    assert artifacts["missionos_hardware_adapter_preflight"][0]["preflight_status"] == (
        "passed"
    )
    assert artifacts["missionos_hardware_dispatch_candidates"][0][
        "operator_approval_required"
    ] is True
    assert artifacts["missionos_hardware_operator_approvals"][0][
        "operator_approval_ref"
    ] == "operator_dispatch_approval:contract-test"
    assert artifacts["missionos_hardware_operator_approvals"][0][
        "approval_actor"
    ] == "operator-contract-test"

    adapter_evidence = artifacts["missionos_hardware_adapter_evidence"][0]
    assert adapter_evidence == result["hardware_adapter_evidence"]
    assert adapter_evidence["adapter_kind"] == "px4_mavlink"
    assert adapter_evidence["execution_mode"] == "loopback"
    assert adapter_evidence["adapter_action_kind"] == "px4_arm_disarm_bench"
    assert adapter_evidence["dispatch_request_sent"] is True
    assert adapter_evidence["command_ack_observed"] is True
    assert adapter_evidence["runtime_progress_observed"] is True
    assert adapter_evidence["completion_claimed"] is True
    assert adapter_evidence["completion_scope"] == "loopback_action"
    assert adapter_evidence["physical_execution_invoked"] is False
    assert "physical_execution_not_invoked" in adapter_evidence["unproven_claims"]
    assert (
        "loopback_action_completion_not_physical"
        in adapter_evidence["unproven_claims"]
    )
    assert (
        "mission_delivery_completion_not_claimed"
        in adapter_evidence["unproven_claims"]
    )


def test_real_hardware_dispatch_runtime_persists_blocked_preflight_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 6, 28, tzinfo=timezone.utc)
    monkeypatch.delenv("RUN_MISSIONOS_REAL_HARDWARE_DISPATCH_RUNTIME", raising=False)

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_real_hardware_actuator_dispatch",
            title="PX4 bench blocked adapter preflight contract test",
            status="running",
            artifacts={},
        )
        approval = build_px4_real_hardware_actuator_approval(
            approved_operations=("arm", "disarm"),
            physical_attestation={
                "propellers_removed": True,
                "operator_physically_present": True,
                "attesting_operator_id": "operator-contract-test",
                "attested_at": now.isoformat(),
            },
            now=now,
        )
        result = invoke_missionos_real_hardware_dispatch_runtime(
            store=store,
            task_id=task["task_id"],
            subject_id="pixhawk-blocked-contract-test",
            approval=approval,
            approval_actor="operator-contract-test",
            dispatch_validation={
                "validation_status": "valid",
                "operator_approval_consumed": True,
                "backend_target": REAL_HARDWARE_DISPATCH_BACKEND_TARGET,
                "authority_id": "authority-contract-test",
                "operator_approval_id": "operator_dispatch_approval:blocked-test",
                "gate_result_id": "gate-contract-test",
            },
            llm_response_proposal=_proposal(),
            serial_device="/dev/tty.usbmodem-contract-test",
            opt_in=True,
            clock=lambda: now,
        )
        updated = store.get(task["task_id"])

    assert updated is not None
    assert result["runtime_invoked"] is False
    assert result["blocked_reason"] == (
        "RUN_MISSIONOS_REAL_HARDWARE_DISPATCH_RUNTIME_not_enabled"
    )
    artifacts = updated["artifacts"]
    assert artifacts["missionos_hardware_adapter_capabilities"][0]["execution_mode"] == (
        "bench"
    )
    preflight = artifacts["missionos_hardware_adapter_preflight"][0]
    assert preflight["preflight_status"] == "blocked"
    assert result["preflight"] == preflight
    assert (
        "RUN_MISSIONOS_REAL_HARDWARE_DISPATCH_RUNTIME_not_enabled"
        in preflight["blocking_reasons"]
    )
    candidate = artifacts["missionos_hardware_dispatch_candidates"][0]
    assert candidate["dispatch_request_sent"] is False
    evidence = artifacts["missionos_hardware_adapter_evidence"][0]
    assert evidence == result["hardware_adapter_evidence"]
    assert evidence["dispatch_status"] == "blocked"
    assert evidence["dispatch_request_sent"] is False
    assert evidence["command_ack_observed"] is False
    assert evidence["completion_claimed"] is False
    assert evidence["physical_execution_invoked"] is False
    assert "dispatch_not_sent" in evidence["unproven_claims"]
