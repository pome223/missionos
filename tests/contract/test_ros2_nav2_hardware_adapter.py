from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.runtime.hardware_adapter_contract import (
    HardwareActionKind,
    HardwareAdapterKind,
    HardwareDispatchStatus,
    HardwareExecutionMode,
    HardwarePreflightStatus,
)
from src.runtime.ros2_nav2_hardware_adapter import (
    ROS2_NAV2_HARDWARE_ADAPTER_OPT_IN_ENV,
    Nav2GoalPose,
    Ros2Nav2HardwareAdapter,
    Ros2Nav2HardwareAdapterConfig,
    build_ros2_nav2_hardware_adapter_capabilities,
)


class RecordingNav2Client:
    def __init__(
        self,
        *,
        dispatch_result: dict[str, Any] | None = None,
        state_result: dict[str, Any] | None = None,
        progress_result: dict[str, Any] | None = None,
    ) -> None:
        self.goal_calls: list[Nav2GoalPose] = []
        self.cancel_calls = 0
        self._dispatch_result = dispatch_result or {
            "ack_status": "accepted",
            "ack_source": "recording_nav2_action_server",
        }
        self._state_result = state_result or {
            "pose_observed": True,
            "robot_motion_observed": True,
            "odom_delta_m": 0.25,
            "controller_state": "active",
        }
        self._progress_result = progress_result or {
            "runtime_progress_observed": True,
            "completion_observed": True,
            "robot_motion_observed": True,
            "nav2_status": "succeeded",
        }

    def send_goal_pose(self, goal: Nav2GoalPose) -> dict[str, Any]:
        self.goal_calls.append(goal)
        return dict(self._dispatch_result)

    def cancel_goal(self) -> dict[str, Any]:
        self.cancel_calls += 1
        return dict(self._dispatch_result)

    def read_state(self) -> dict[str, Any]:
        return dict(self._state_result)

    def read_progress(self) -> dict[str, Any]:
        return dict(self._progress_result)


def _approved_config(
    *,
    execution_mode: HardwareExecutionMode = HardwareExecutionMode.LOOPBACK,
    operator_approval_ref: str | None = "approval_nav2_001",
    opt_in: bool = False,
    physical_estop_available: bool = False,
) -> Ros2Nav2HardwareAdapterConfig:
    return Ros2Nav2HardwareAdapterConfig(
        missionos_action_ref="missionos_plan_bounded_nav2_goal_pose",
        goal_pose=Nav2GoalPose(x_m=1.0, y_m=0.5, yaw_rad=0.0),
        execution_mode=execution_mode,
        operator_approval_ref=operator_approval_ref,
        approval_actor="operator-001",
        approval_timestamp=datetime.now(timezone.utc),
        opt_in=opt_in,
        physical_estop_available=physical_estop_available,
    )


def test_ros2_nav2_capabilities_are_bounded() -> None:
    capabilities = build_ros2_nav2_hardware_adapter_capabilities()

    assert capabilities.adapter_kind is HardwareAdapterKind.ROS2_NAV2
    assert capabilities.allowed_actions == (
        HardwareActionKind.NAV2_GOAL_POSE,
        HardwareActionKind.NAV2_CANCEL_GOAL,
        HardwareActionKind.HOLD,
        HardwareActionKind.SAFE_STOP,
    )
    assert HardwareActionKind.RAW_MAVLINK in capabilities.blocked_actions
    assert capabilities.requires_operator_approval is True
    assert capabilities.requires_geofence is True


def test_ros2_nav2_physical_mode_blocks_without_client_opt_in_and_estop(
    monkeypatch,
) -> None:
    monkeypatch.delenv(ROS2_NAV2_HARDWARE_ADAPTER_OPT_IN_ENV, raising=False)
    adapter = Ros2Nav2HardwareAdapter(
        config=_approved_config(execution_mode=HardwareExecutionMode.BENCH),
        client=None,
    )

    preflight = adapter.preflight_check()
    assert preflight.preflight_status is HardwarePreflightStatus.BLOCKED
    assert "ros2_nav2_client_missing" in preflight.blocking_reasons
    assert "physical_estop_missing" in preflight.blocking_reasons
    assert "ros2_nav2_runtime_opt_in_not_enabled" in preflight.blocking_reasons

    evidence = adapter.dispatch_approved_action()
    assert evidence.dispatch_status is HardwareDispatchStatus.BLOCKED
    assert evidence.dispatch_request_sent is False
    assert evidence.physical_execution_invoked is False
    assert "dispatch_not_sent" in evidence.unproven_claims


def test_ros2_nav2_refuses_dispatch_without_operator_approval() -> None:
    client = RecordingNav2Client()
    adapter = Ros2Nav2HardwareAdapter(
        config=_approved_config(operator_approval_ref=None),
        client=client,
    )

    evidence = adapter.dispatch_approved_action()

    assert evidence.dispatch_status is HardwareDispatchStatus.BLOCKED
    assert evidence.dispatch_request_sent is False
    assert "operator_approval_missing" in evidence.blocking_reasons
    assert client.goal_calls == []


def test_ros2_nav2_loopback_dispatch_calls_client_without_physical_claim() -> None:
    client = RecordingNav2Client()
    adapter = Ros2Nav2HardwareAdapter(config=_approved_config(), client=client)

    candidate = adapter.propose_dispatch()
    assert candidate.dispatch_request_sent is False
    assert candidate.adapter_action_kind is HardwareActionKind.NAV2_GOAL_POSE

    evidence = adapter.dispatch_approved_action()

    assert len(client.goal_calls) == 1
    assert client.goal_calls[0].x_m == 1.0
    assert evidence.dispatch_status is HardwareDispatchStatus.SENT
    assert evidence.command_ack_observed is True
    assert evidence.runtime_progress_observed is True
    assert evidence.completion_claimed is True
    assert evidence.completion_scope == "loopback_action"
    assert evidence.physical_execution_invoked is False
    assert "loopback_action_completion_not_physical" in evidence.unproven_claims
    assert adapter.collect_evidence() == (evidence,)


def test_ros2_nav2_sim_dispatch_claims_only_sim_action() -> None:
    client = RecordingNav2Client()
    adapter = Ros2Nav2HardwareAdapter(
        config=_approved_config(execution_mode=HardwareExecutionMode.SIM),
        client=client,
    )

    evidence = adapter.dispatch_approved_action()

    assert evidence.dispatch_status is HardwareDispatchStatus.SENT
    assert evidence.command_ack_observed is True
    assert evidence.runtime_progress_observed is True
    assert evidence.completion_claimed is True
    assert evidence.completion_scope == "sim_action"
    assert evidence.physical_execution_invoked is False
    assert "sim_action_completion_not_physical" in evidence.unproven_claims


def test_ros2_nav2_ack_without_progress_is_not_completion() -> None:
    client = RecordingNav2Client(
        progress_result={
            "runtime_progress_observed": False,
            "completion_observed": False,
            "nav2_status": "accepted",
        },
    )
    adapter = Ros2Nav2HardwareAdapter(config=_approved_config(), client=client)

    evidence = adapter.dispatch_approved_action()

    assert evidence.dispatch_status is HardwareDispatchStatus.SENT
    assert evidence.command_ack_observed is True
    assert evidence.completion_claimed is False
    assert evidence.completion_scope == "none"
    assert "ack_is_not_success" in evidence.unproven_claims


def test_ros2_nav2_goal_outside_operating_volume_blocks_dispatch() -> None:
    config = Ros2Nav2HardwareAdapterConfig(
        missionos_action_ref="missionos_plan_bounded_nav2_goal_pose",
        goal_pose=Nav2GoalPose(x_m=10.0, y_m=0.0),
        operator_approval_ref="approval_nav2_002",
        approval_actor="operator-001",
        approval_timestamp=datetime.now(timezone.utc),
        max_distance_m=3.0,
    )
    client = RecordingNav2Client()
    adapter = Ros2Nav2HardwareAdapter(config=config, client=client)

    evidence = adapter.dispatch_approved_action()

    assert evidence.dispatch_status is HardwareDispatchStatus.BLOCKED
    assert "operating_volume_violation" in evidence.blocking_reasons
    assert client.goal_calls == []


def test_ros2_nav2_operator_abort_requests_safe_stop_without_dispatch() -> None:
    adapter = Ros2Nav2HardwareAdapter(
        config=_approved_config(),
        client=RecordingNav2Client(),
    )

    evidence = adapter.abort_or_safe_stop()

    assert evidence.dispatch_status is HardwareDispatchStatus.SAFE_STOP_REQUESTED
    assert evidence.safe_stop_requested is True
    assert evidence.abort_requested is True
    assert evidence.dispatch_request_sent is False
    assert evidence.physical_execution_invoked is False
