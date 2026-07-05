from __future__ import annotations

from datetime import datetime, timezone
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
)
from src.runtime.unitree_hardware_adapter import (
    UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV,
    UnitreeBoundedLocalMove,
    UnitreeHardwareAdapter,
    UnitreeHardwareAdapterConfig,
    build_unitree_hardware_adapter_capabilities,
)


class RecordingUnitreeSimClient:
    def __init__(
        self,
        *,
        dispatch_result: dict[str, Any] | None = None,
        state_result: dict[str, Any] | None = None,
        progress_result: dict[str, Any] | None = None,
    ) -> None:
        self.move_calls: list[UnitreeBoundedLocalMove] = []
        self.hold_calls = 0
        self.safe_stop_calls = 0
        self._dispatch_result = dispatch_result or {
            "ack_status": "accepted",
            "ack_source": "recording_unitree_mujoco_client",
        }
        self._state_result = state_result or {
            "pose_observed": True,
            "base_stable": True,
        }
        self._progress_result = progress_result or {
            "runtime_progress_observed": True,
            "completion_observed": True,
            "unitree_status": "succeeded",
        }

    def send_bounded_local_move(
        self,
        move: UnitreeBoundedLocalMove,
    ) -> dict[str, Any]:
        self.move_calls.append(move)
        return dict(self._dispatch_result)

    def hold(self) -> dict[str, Any]:
        self.hold_calls += 1
        return dict(self._dispatch_result)

    def safe_stop(self) -> dict[str, Any]:
        self.safe_stop_calls += 1
        return dict(self._dispatch_result)

    def read_state(self) -> dict[str, Any]:
        return dict(self._state_result)

    def read_progress(self) -> dict[str, Any]:
        return dict(self._progress_result)


def _approved_config(
    *,
    action_kind: HardwareActionKind = HardwareActionKind.BOUNDED_LOCAL_MOVE,
    operator_approval_ref: str | None = "approval_unitree_001",
    opt_in: bool = True,
    execution_mode: HardwareExecutionMode = HardwareExecutionMode.SIM,
    local_move: UnitreeBoundedLocalMove | None = None,
) -> UnitreeHardwareAdapterConfig:
    return UnitreeHardwareAdapterConfig(
        missionos_action_ref="missionos_plan_unitree_bounded_local_move",
        action_kind=action_kind,
        local_move=local_move
        or UnitreeBoundedLocalMove(forward_m=0.25, lateral_m=0.0),
        execution_mode=execution_mode,
        operator_approval_ref=operator_approval_ref,
        approval_actor="operator-001",
        approval_timestamp=datetime.now(timezone.utc),
        opt_in=opt_in,
    )


def test_unitree_capabilities_are_bounded_and_block_special_motion() -> None:
    capabilities = build_unitree_hardware_adapter_capabilities()

    assert capabilities.adapter_kind is HardwareAdapterKind.UNITREE_SDK2
    assert capabilities.vehicle_class is HardwareVehicleClass.GROUND_ROBOT
    assert capabilities.execution_mode is HardwareExecutionMode.SIM
    assert capabilities.allowed_actions == (
        HardwareActionKind.SAFE_STOP,
        HardwareActionKind.HOLD,
        HardwareActionKind.BOUNDED_LOCAL_MOVE,
    )
    assert HardwareActionKind.RAW_MOTOR in capabilities.blocked_actions
    assert HardwareActionKind.RAW_VELOCITY in capabilities.blocked_actions
    assert HardwareActionKind.SPECIAL_MOTION in capabilities.blocked_actions
    assert capabilities.max_speed_mps == 0.3
    assert capabilities.max_distance_m == 0.5


def test_sim_action_completion_scope_is_only_valid_for_sim_execution() -> None:
    HardwareAdapterEvidence(
        adapter_id="unitree_sdk2_mujoco_adapter.v1",
        adapter_kind=HardwareAdapterKind.UNITREE_SDK2,
        vehicle_class=HardwareVehicleClass.GROUND_ROBOT,
        execution_mode=HardwareExecutionMode.SIM,
        missionos_action_ref="missionos_plan_unitree_bounded_local_move",
        adapter_action_kind=HardwareActionKind.BOUNDED_LOCAL_MOVE,
        operator_approval_ref="approval_unitree_001",
        preflight_status=HardwarePreflightStatus.PASSED,
        dispatch_status=HardwareDispatchStatus.SENT,
        dispatch_request_sent=True,
        command_ack_observed=True,
        ack_status=HardwareAckStatus.ACCEPTED,
        runtime_progress_observed=True,
        completion_claimed=True,
        completion_scope="sim_action",
        physical_execution_invoked=False,
        telemetry_fresh=True,
    )

    with pytest.raises((HardwareAdapterContractError, ValidationError)):
        HardwareAdapterEvidence(
            adapter_id="unitree_sdk2_mujoco_adapter.v1",
            adapter_kind=HardwareAdapterKind.UNITREE_SDK2,
            vehicle_class=HardwareVehicleClass.GROUND_ROBOT,
            execution_mode=HardwareExecutionMode.SIM,
            missionos_action_ref="missionos_plan_unitree_bounded_local_move",
            adapter_action_kind=HardwareActionKind.BOUNDED_LOCAL_MOVE,
            operator_approval_ref="approval_unitree_001",
            preflight_status=HardwarePreflightStatus.PASSED,
            dispatch_status=HardwareDispatchStatus.SENT,
            dispatch_request_sent=True,
            command_ack_observed=True,
            ack_status=HardwareAckStatus.ACCEPTED,
            runtime_progress_observed=True,
            completion_claimed=True,
            completion_scope="adapter_action",
            physical_execution_invoked=False,
            telemetry_fresh=True,
        )

    with pytest.raises((HardwareAdapterContractError, ValidationError)):
        HardwareAdapterEvidence(
            adapter_id="unitree_sdk2_mujoco_adapter.v1",
            adapter_kind=HardwareAdapterKind.UNITREE_SDK2,
            vehicle_class=HardwareVehicleClass.GROUND_ROBOT,
            execution_mode=HardwareExecutionMode.LOOPBACK,
            missionos_action_ref="missionos_plan_unitree_bounded_local_move",
            adapter_action_kind=HardwareActionKind.BOUNDED_LOCAL_MOVE,
            operator_approval_ref="approval_unitree_001",
            preflight_status=HardwarePreflightStatus.PASSED,
            dispatch_status=HardwareDispatchStatus.SENT,
            dispatch_request_sent=True,
            command_ack_observed=True,
            ack_status=HardwareAckStatus.ACCEPTED,
            runtime_progress_observed=True,
            completion_claimed=True,
            completion_scope="sim_action",
            physical_execution_invoked=False,
            telemetry_fresh=True,
        )


def test_unitree_gate_off_blocks_without_client_or_dispatch(monkeypatch) -> None:
    monkeypatch.delenv(UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV, raising=False)
    adapter = UnitreeHardwareAdapter(
        config=_approved_config(opt_in=False),
        client=None,
    )

    preflight = adapter.preflight_check()
    assert preflight.preflight_status is HardwarePreflightStatus.BLOCKED
    assert "unitree_sim_client_missing" in preflight.blocking_reasons
    assert "unitree_mujoco_runtime_opt_in_not_enabled" in preflight.blocking_reasons

    evidence = adapter.dispatch_approved_action()
    assert evidence.dispatch_status is HardwareDispatchStatus.BLOCKED
    assert evidence.dispatch_request_sent is False
    assert evidence.physical_execution_invoked is False
    assert evidence.completion_claimed is False
    assert "dispatch_not_sent" in evidence.unproven_claims


def test_unitree_refuses_dispatch_without_operator_approval(monkeypatch) -> None:
    monkeypatch.setenv(UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV, "1")
    client = RecordingUnitreeSimClient()
    adapter = UnitreeHardwareAdapter(
        config=_approved_config(operator_approval_ref=None),
        client=client,
    )

    evidence = adapter.dispatch_approved_action()

    assert evidence.dispatch_status is HardwareDispatchStatus.BLOCKED
    assert evidence.dispatch_request_sent is False
    assert "operator_approval_missing" in evidence.blocking_reasons
    assert client.move_calls == []


def test_unitree_sim_dispatch_claims_only_sim_action(monkeypatch) -> None:
    monkeypatch.setenv(UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV, "1")
    client = RecordingUnitreeSimClient()
    adapter = UnitreeHardwareAdapter(
        config=_approved_config(),
        client=client,
    )

    candidate = adapter.propose_dispatch()
    assert candidate.dispatch_request_sent is False
    assert candidate.adapter_action_kind is HardwareActionKind.BOUNDED_LOCAL_MOVE

    evidence = adapter.dispatch_approved_action()

    assert len(client.move_calls) == 1
    assert client.move_calls[0].forward_m == 0.25
    assert evidence.dispatch_status is HardwareDispatchStatus.SENT
    assert evidence.command_ack_observed is True
    assert evidence.runtime_progress_observed is True
    assert evidence.completion_claimed is True
    assert evidence.completion_scope == "sim_action"
    assert evidence.physical_execution_invoked is False
    assert "sim_action_completion_not_physical" in evidence.unproven_claims
    assert "special_motion_not_invoked" in evidence.unproven_claims
    assert adapter.collect_evidence() == (evidence,)


def test_unitree_ack_without_progress_is_not_completion(monkeypatch) -> None:
    monkeypatch.setenv(UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV, "1")
    client = RecordingUnitreeSimClient(
        progress_result={
            "runtime_progress_observed": False,
            "completion_observed": False,
            "unitree_status": "accepted",
        },
    )
    adapter = UnitreeHardwareAdapter(config=_approved_config(), client=client)

    evidence = adapter.dispatch_approved_action()

    assert evidence.dispatch_status is HardwareDispatchStatus.SENT
    assert evidence.command_ack_observed is True
    assert evidence.completion_claimed is False
    assert evidence.completion_scope == "none"
    assert "ack_is_not_success" in evidence.unproven_claims


def test_unitree_special_motion_is_blocked_before_client_call(monkeypatch) -> None:
    monkeypatch.setenv(UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV, "1")
    client = RecordingUnitreeSimClient()
    adapter = UnitreeHardwareAdapter(
        config=_approved_config(action_kind=HardwareActionKind.SPECIAL_MOTION),
        client=client,
    )

    evidence = adapter.dispatch_approved_action()

    assert evidence.dispatch_status is HardwareDispatchStatus.BLOCKED
    assert "action_not_allowed_by_adapter_capabilities" in evidence.blocking_reasons
    assert "action_blocked_by_adapter_capabilities" in evidence.blocking_reasons
    assert client.move_calls == []
    assert client.hold_calls == 0
    assert client.safe_stop_calls == 0


def test_unitree_move_outside_operating_volume_blocks_dispatch(monkeypatch) -> None:
    monkeypatch.setenv(UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV, "1")
    client = RecordingUnitreeSimClient()
    adapter = UnitreeHardwareAdapter(
        config=_approved_config(
            local_move=UnitreeBoundedLocalMove(forward_m=1.0, lateral_m=0.0),
        ),
        client=client,
    )

    evidence = adapter.dispatch_approved_action()

    assert evidence.dispatch_status is HardwareDispatchStatus.BLOCKED
    assert "operating_volume_violation" in evidence.blocking_reasons
    assert client.move_calls == []


def test_unitree_operator_abort_requests_safe_stop_without_dispatch() -> None:
    adapter = UnitreeHardwareAdapter(
        config=_approved_config(),
        client=RecordingUnitreeSimClient(),
    )

    evidence = adapter.abort_or_safe_stop()

    assert evidence.dispatch_status is HardwareDispatchStatus.SAFE_STOP_REQUESTED
    assert evidence.safe_stop_requested is True
    assert evidence.abort_requested is True
    assert evidence.dispatch_request_sent is False
    assert evidence.physical_execution_invoked is False
