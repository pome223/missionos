"""Backend-specific registrations for the backend-neutral adapter runtime.

Backend names and config translation belong here. Authority, approval matching,
runtime evidence validation, and final verification remain in
``hardware_adapter_runtime``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.runtime.hardware_adapter_contract import (
    HardwareActionKind,
    HardwareExecutionMode,
    HardwareOperatorApproval,
)
from src.runtime.hardware_adapter_runtime import (
    HardwareAdapterRegistry,
    HardwareAdapterRuntimeRequest,
)
from src.runtime.ros2_nav2_hardware_adapter import (
    ROS2_NAV2_HARDWARE_ADAPTER_ID,
    Nav2DispatchClient,
    Nav2GoalPose,
    Ros2Nav2HardwareAdapter,
    Ros2Nav2HardwareAdapterConfig,
)
from src.runtime.unitree_hardware_adapter import (
    UNITREE_MUJOCO_HARDWARE_ADAPTER_ID,
    UnitreeBoundedLocalMove,
    UnitreeHardwareAdapter,
    UnitreeHardwareAdapterConfig,
    UnitreeSimClient,
)
from src.runtime.unitree_mujoco_dispatch_bridge import (
    UnitreeMujocoBridgeCommandClient,
)


def _approval_fields(
    approval: HardwareOperatorApproval | None,
) -> dict[str, Any]:
    if approval is None:
        return {
            "operator_approval_ref": None,
            "preparation_ref": None,
            "preparation_sha256": None,
            "approval_actor": None,
            "approval_timestamp": None,
        }
    return {
        "operator_approval_ref": approval.operator_approval_ref,
        "preparation_ref": approval.approved_preparation_ref,
        "preparation_sha256": approval.approved_preparation_sha256,
        "approval_actor": approval.approval_actor,
        "approval_timestamp": approval.approval_timestamp,
    }


def unitree_adapter_factory(
    *,
    client_factory: Callable[[], UnitreeSimClient] = UnitreeMujocoBridgeCommandClient,
) -> Callable[
    [HardwareAdapterRuntimeRequest, HardwareOperatorApproval | None],
    UnitreeHardwareAdapter,
]:
    """Return the Unitree registration factory around a supplied client boundary."""

    def _factory(
        request: HardwareAdapterRuntimeRequest,
        approval: HardwareOperatorApproval | None,
    ) -> UnitreeHardwareAdapter:
        move = None
        if request.action_kind is HardwareActionKind.BOUNDED_LOCAL_MOVE:
            move = UnitreeBoundedLocalMove.model_validate(request.adapter_parameters)
        config = UnitreeHardwareAdapterConfig(
            missionos_action_ref=request.missionos_action_ref,
            action_kind=request.action_kind,
            local_move=move,
            execution_mode=HardwareExecutionMode(request.execution_mode),
            opt_in=request.opt_in,
            telemetry_fresh=request.telemetry_fresh,
            heartbeat_alive=request.heartbeat_alive,
            geofence_satisfied=request.geofence_satisfied,
            operating_volume_satisfied=request.operating_volume_satisfied,
            **_approval_fields(approval),
        )
        return UnitreeHardwareAdapter(config=config, client=client_factory())

    return _factory


def ros2_nav2_adapter_factory(
    *,
    client_factory: Callable[[], Nav2DispatchClient],
) -> Callable[
    [HardwareAdapterRuntimeRequest, HardwareOperatorApproval | None],
    Ros2Nav2HardwareAdapter,
]:
    """Return the Nav2 registration factory around a supplied client boundary."""

    def _factory(
        request: HardwareAdapterRuntimeRequest,
        approval: HardwareOperatorApproval | None,
    ) -> Ros2Nav2HardwareAdapter:
        goal_pose = None
        if request.action_kind is HardwareActionKind.NAV2_GOAL_POSE:
            goal_pose = Nav2GoalPose.model_validate(request.adapter_parameters)
        config = Ros2Nav2HardwareAdapterConfig(
            missionos_action_ref=request.missionos_action_ref,
            action_kind=request.action_kind,
            goal_pose=goal_pose,
            execution_mode=HardwareExecutionMode(request.execution_mode),
            opt_in=request.opt_in,
            telemetry_fresh=request.telemetry_fresh,
            heartbeat_alive=request.heartbeat_alive,
            geofence_satisfied=request.geofence_satisfied,
            operating_volume_satisfied=request.operating_volume_satisfied,
            **_approval_fields(approval),
        )
        return Ros2Nav2HardwareAdapter(config=config, client=client_factory())

    return _factory


def build_default_hardware_adapter_registry() -> HardwareAdapterRegistry:
    """Build production registrations without embedding backend branches in Core."""

    registry = HardwareAdapterRegistry()
    registry.register(
        UNITREE_MUJOCO_HARDWARE_ADAPTER_ID,
        unitree_adapter_factory(),
    )
    return registry


def register_ros2_nav2_adapter(
    registry: HardwareAdapterRegistry,
    *,
    client_factory: Callable[[], Nav2DispatchClient],
) -> None:
    """Register Nav2 when its runtime supplies an explicit client boundary."""

    registry.register(
        ROS2_NAV2_HARDWARE_ADAPTER_ID,
        ros2_nav2_adapter_factory(client_factory=client_factory),
    )


__all__ = [
    "build_default_hardware_adapter_registry",
    "register_ros2_nav2_adapter",
    "ros2_nav2_adapter_factory",
    "unitree_adapter_factory",
]
