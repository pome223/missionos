"""Truth-preserving recovery outcome projections for the PX4 route runtime.

The functions in this module receive approval, dispatch, and observation facts
that already exist.  They do not select an action, mint approval, send a
command, infer an outcome from an ACK, mutate a task, or claim delivery or
physical execution.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


PAYLOAD_RECOVERY_ACTION_REF = (
    "payload_recovery_action:mission_designer_payload_mass"
)


@dataclass(frozen=True)
class RecoveryCycleOutcome:
    """Already-observed facts for one bounded recovery action."""

    action: str | None
    approval_ref: str | None = None
    dispatch_ref: str | None = None
    dispatch_status: str | None = None
    command_ack_observed: bool | None = None
    command_ack_result_name: str | None = None
    ack_complete: bool = False
    state_observed: bool = False
    state_label: str | None = None
    completed: bool = False
    pose_z_m: float | None = None
    completion_basis: str | None = None
    completion_ref: str | None = None

    def __post_init__(self) -> None:
        if self.completed and not self.state_observed:
            raise ValueError("completed recovery requires an observed recovery state")
        if self.dispatch_ref is not None and self.approval_ref is None:
            raise ValueError("recovery dispatch reference requires an approval reference")
        if self.command_ack_observed is True and self.dispatch_ref is None:
            raise ValueError("recovery ACK evidence requires a dispatch reference")


def emergency_approval_ref(approval: Any | None) -> str | None:
    if approval is None:
        return None
    return f"px4_gazebo_emergency_command_approval:{approval.approval_id}"


def emergency_dispatch_ref(dispatch: Any | None) -> str | None:
    if dispatch is None:
        return None
    return (
        "px4_gazebo_emergency_command_dispatch_result:"
        f"{dispatch.dispatch_result_id}"
    )


def route_recovery_completion_ref(completion: Any | None) -> str | None:
    if completion is None:
        return None
    return (
        "px4_gazebo_route_recovery_completion:"
        f"{completion.recovery_completion_id}"
    )


def payload_recovery_terminal_status(
    *,
    payload_action: str,
    payload_outcome: RecoveryCycleOutcome,
    supervisor_loop_requested: bool,
    post_recovery_outcome: RecoveryCycleOutcome,
) -> tuple[str, str]:
    """Derive status from observed outcomes without upgrading weak evidence."""

    if post_recovery_outcome.completed:
        final_status = "payload_supervisor_post_recovery_land_observed"
    elif supervisor_loop_requested:
        final_status = "payload_supervisor_post_recovery_unconfirmed"
    elif payload_outcome.completed:
        final_status = f"payload_advisory_recovered_{payload_action}"
    else:
        final_status = "payload_advisory_recovery_unconfirmed"

    completed = (
        post_recovery_outcome.completed
        if supervisor_loop_requested
        else payload_outcome.completed
    )
    return final_status, "completed" if completed else "blocked"


def build_payload_recovery_action(
    *,
    advisory_ref: str,
    outcome: RecoveryCycleOutcome,
    observed_at: str,
    action_ref: str = PAYLOAD_RECOVERY_ACTION_REF,
) -> dict[str, Any]:
    if not advisory_ref.startswith("payload_feasibility_advisory:"):
        raise ValueError("payload recovery action requires a payload advisory reference")
    if outcome.action is None:
        raise ValueError("payload recovery action requires a bounded action")
    if outcome.approval_ref is None or outcome.dispatch_ref is None:
        raise ValueError("payload recovery action requires approval and dispatch references")
    return {
        "schema_version": "payload_recovery_action.v1",
        "action_id": action_ref,
        "action_ref": action_ref,
        "condition_kind": "payload_mass_feasibility_recovery",
        "causal_form": "Form 2a",
        "form2_subtype": "Form 2a",
        "trigger_level": "level_2_inferred",
        "mission_response_kind": "action",
        "payload_feasibility_advisory_ref": advisory_ref,
        "advisory_ref": advisory_ref,
        "advisory_consumed_by_ref": action_ref,
        "advisory_lifecycle_state": "reviewed_consumed_by_action_pr",
        "operator_approval_required": True,
        "operator_approval_performed": True,
        "approval_ref": outcome.approval_ref,
        "dispatch_ref": outcome.dispatch_ref,
        "bounded_action_ref": outcome.dispatch_ref,
        "bounded_action_kind": outcome.action,
        "dispatch_status": outcome.dispatch_status,
        "command_ack_observed": outcome.command_ack_observed,
        "command_ack_result_name": outcome.command_ack_result_name,
        "recovery_state_observed": outcome.state_observed,
        "recovery_state_label": outcome.state_label,
        "recovery_completed": outcome.completed,
        "recovery_pose_z_m": outcome.pose_z_m,
        "automatic_dispatch_suppressed": False,
        "approval_free_recovery_dispatch_allowed": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": observed_at,
    }


def build_payload_post_recovery_action(
    *,
    advisory_ref: str,
    source_cycle1_outcome_ref: str,
    outcome: RecoveryCycleOutcome,
    observed_at: str,
    action_ref: str = (
        "payload_supervisor_post_recovery_action:mission_designer_payload_mass"
    ),
) -> dict[str, Any]:
    if outcome.action is None:
        raise ValueError("post-recovery action requires a bounded action")
    if outcome.approval_ref is None or outcome.dispatch_ref is None:
        raise ValueError("post-recovery action requires approval and dispatch references")
    return {
        "schema_version": "payload_supervisor_post_recovery_action.v1",
        "action_id": action_ref,
        "action_ref": action_ref,
        "condition_kind": "payload_mass_supervisor_form3_recovery",
        "causal_form": "Form 2a",
        "form2_subtype": "Form 2a",
        "trigger_level": "level_2_inferred",
        "mission_response_kind": "action",
        "decision_loop_driver": "mission_os_supervisor",
        "supervisor_scope": "payload_form3_sitl_only",
        "full_gateway_runtime_loop": False,
        "source_cycle1_outcome_ref": source_cycle1_outcome_ref,
        "payload_feasibility_advisory_ref": advisory_ref,
        "advisory_ref": advisory_ref,
        "operator_approval_required": True,
        "operator_approval_performed": True,
        "approval_ref": outcome.approval_ref,
        "dispatch_ref": outcome.dispatch_ref,
        "bounded_action_ref": outcome.dispatch_ref,
        "bounded_action_kind": outcome.action,
        "dispatch_status": outcome.dispatch_status,
        "command_ack_observed": outcome.command_ack_observed,
        "command_ack_result_name": outcome.command_ack_result_name,
        "recovery_state_observed": outcome.state_observed,
        "recovery_state_label": outcome.state_label,
        "recovery_completed": outcome.completed,
        "recovery_pose_z_m": outcome.pose_z_m,
        "automatic_dispatch_suppressed": False,
        "approval_free_recovery_dispatch_allowed": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": observed_at,
    }


def _serialized(value: Any | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    return value.model_dump(mode="json")


def recovery_task_artifacts(
    *,
    deviation_abort: Any | None = None,
    approval: Any | None = None,
    allowlist: Any | None = None,
    dispatch: Any | None = None,
    completion: Any | None = None,
    post_approval: Any | None = None,
    post_allowlist: Any | None = None,
    post_dispatch: Any | None = None,
    post_completion: Any | None = None,
    payload_recovery_action: Mapping[str, Any] | None = None,
    payload_post_recovery_action: Mapping[str, Any] | None = None,
    supervisor_loop: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize existing artifacts while dropping only absent optional values."""

    values = {
        "px4_gazebo_route_deviation_abort": _serialized(deviation_abort),
        "px4_gazebo_emergency_command_approval": _serialized(approval),
        "px4_gazebo_emergency_command_allowlist": _serialized(allowlist),
        "px4_gazebo_emergency_command_dispatch_result": _serialized(dispatch),
        "px4_gazebo_route_recovery_completion": _serialized(completion),
        "px4_gazebo_post_recovery_emergency_command_approval": _serialized(
            post_approval
        ),
        "px4_gazebo_post_recovery_emergency_command_allowlist": _serialized(
            post_allowlist
        ),
        "px4_gazebo_post_recovery_emergency_command_dispatch_result": _serialized(
            post_dispatch
        ),
        "px4_gazebo_post_recovery_completion": _serialized(post_completion),
        "payload_recovery_action": (
            None if payload_recovery_action is None else dict(payload_recovery_action)
        ),
        "payload_supervisor_post_recovery_action": (
            None
            if payload_post_recovery_action is None
            else dict(payload_post_recovery_action)
        ),
        "mission_os_supervisor_recovery_loop": (
            None if supervisor_loop is None else dict(supervisor_loop)
        ),
    }
    return {key: value for key, value in values.items() if value is not None}


__all__ = [
    "PAYLOAD_RECOVERY_ACTION_REF",
    "RecoveryCycleOutcome",
    "build_payload_post_recovery_action",
    "build_payload_recovery_action",
    "emergency_approval_ref",
    "emergency_dispatch_ref",
    "payload_recovery_terminal_status",
    "recovery_task_artifacts",
    "route_recovery_completion_ref",
]
