"""Opt-in composition supplies the simulator; this boundary never loads hardware.

Reuses MissionOS's existing validated evidence contract, explicitly identifying
the MuJoCo goal executor. It does not impersonate Nav2 or Unitree's Sport API.
"""

from dataclasses import dataclass
import math
from typing import Any, Protocol

from src.runtime.hardware_adapter_contract import HardwareAdapterEvidence


@dataclass(frozen=True)
class DeliveryGoal:
    x_m: float
    y_m: float
    yaw_rad: float
    tolerance_m: float
    max_speed_mps: float
    label: str


class DeliveryNavigationClient(Protocol):
    metadata: dict

    def send_goal_pose(self, goal: DeliveryGoal) -> dict: ...
    def read_state(self) -> dict: ...
    def wait(self, seconds: float) -> None: ...


def dispatch_navigation(
    *,
    client: DeliveryNavigationClient,
    goal: DeliveryGoal,
    state: dict,
    action_ref: str,
    operator_approval_ref: str,
) -> HardwareAdapterEvidence:
    reasons = [
        key
        for key in ("telemetry_fresh", "heartbeat_alive", "geofence_satisfied", "base_stable")
        if state.get(key) is not True
    ]
    if not operator_approval_ref.strip():
        reasons.append("missing_operator_approval")
    if state.get("safety_violation_observed"):
        reasons.append("prior_safety_violation")
    if (
        not all(
            math.isfinite(v)
            for v in (goal.x_m, goal.y_m, goal.yaw_rad, goal.tolerance_m, goal.max_speed_mps)
        )
        or math.hypot(goal.x_m, goal.y_m) > 3
        or not 0 < goal.max_speed_mps <= 0.25
        or not 0 < goal.tolerance_m <= 0.4
    ):
        reasons.append("goal_outside_bounds")
    response: dict[str, Any] = {}
    if not reasons:
        response = client.send_goal_pose(goal)
    final = client.read_state()
    accepted = response.get("ack_status") == "accepted"
    moved = final.get("robot_motion_observed") is True
    completed = (
        accepted
        and moved
        and final.get("navigation_status") == "succeeded"
        and final.get("telemetry_fresh") is True
        and final.get("base_stable") is True
    )
    return HardwareAdapterEvidence(
        adapter_id="go2_mujoco_goal_adapter.v1",
        adapter_kind="vendor_specific",
        vehicle_class="ground_robot",
        execution_mode="sim",
        missionos_action_ref=action_ref,
        adapter_action_kind="bounded_local_move",
        operator_approval_ref=operator_approval_ref,
        preflight_status="blocked" if reasons else "passed",
        dispatch_status="blocked" if reasons else "sent",
        dispatch_request_sent=not reasons,
        command_ack_observed=bool(response),
        ack_status="accepted" if accepted else ("rejected" if response else "not_requested"),
        ack_source=response.get("ack_source"),
        runtime_state_observed=final.get("state_observed") is True,
        runtime_progress_observed=moved,
        completion_claimed=completed,
        completion_scope="sim_action" if completed else "none",
        telemetry_fresh=state.get("telemetry_fresh") is True,
        blocking_reasons=tuple(reasons),
        physical_execution_invoked=False,
        unproven_claims=("physical_delivery", "payload_manipulation", "sensor_based_localization"),
    )
