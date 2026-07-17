"""Observation boundary for an already-dispatched PX4 recovery action.

The caller must select the action, obtain approval, constrain it with an
allowlist, and dispatch it before entering this module.  This module invokes an
observation callback and normalizes the result.  It does not approve, dispatch,
retry, choose a stronger action, or infer completion from an ACK.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.runtime.px4_gazebo_route.recovery_outcomes import (
    RecoveryCycleOutcome,
    emergency_approval_ref,
    emergency_dispatch_ref,
    route_recovery_completion_ref,
)


RecoveryObserver = Callable[
    ...,
    tuple[
        bool,
        str | None,
        Mapping[str, float] | None,
        Sequence[Mapping[str, float]],
    ],
]
RecoveryCompletionBuilder = Callable[..., Any]


@dataclass(frozen=True)
class ObservedRecoveryCycle:
    outcome: RecoveryCycleOutcome
    pose: dict[str, float] | None
    samples: tuple[dict[str, float], ...]
    completion: Any | None = None


def observe_dispatched_recovery(
    *,
    action: str,
    approval: Any,
    dispatch: Any,
    pickup_pose: Mapping[str, float],
    observe_state: RecoveryObserver,
    deviation_abort: Any | None = None,
    build_completion: RecoveryCompletionBuilder | None = None,
    observed_at: datetime | None = None,
) -> ObservedRecoveryCycle:
    """Observe one dispatched action without creating new authority."""

    approval_ref = emergency_approval_ref(approval)
    dispatch_ref = emergency_dispatch_ref(dispatch)
    if approval_ref is None or dispatch_ref is None:
        raise ValueError("recovery observation requires approval and dispatch artifacts")

    state_observed, state_label, raw_pose, raw_samples = observe_state(
        action=action,
        pickup_pose=pickup_pose,
        dispatch_frame_sent=dispatch.frame_sent is True,
    )
    pose = None if raw_pose is None else dict(raw_pose)
    samples = tuple(dict(sample) for sample in raw_samples)
    completion = None
    completion_basis = None
    completion_ref = None
    ack_complete = (
        dispatch.dispatch_status == "accepted"
        and dispatch.command_ack_observed is True
    )
    completed = bool(state_observed)

    if build_completion is not None:
        if deviation_abort is None or observed_at is None:
            raise ValueError(
                "route recovery completion requires deviation abort and observed_at"
            )
        completion = build_completion(
            deviation_abort=deviation_abort,
            emergency_dispatch=dispatch,
            recovery_state_observed=state_observed,
            recovery_pose_z_m=None if pose is None else pose.get("z"),
            recovery_state_label=state_label,
            now=observed_at,
        )
        completed = completion.recovery_completed
        ack_complete = completion.recovery_ack_complete
        state_observed = completion.recovery_state_observed
        state_label = completion.recovery_state_label
        basis = completion.recovery_completion_basis
        completion_basis = getattr(basis, "value", str(basis))
        completion_ref = route_recovery_completion_ref(completion)

    outcome = RecoveryCycleOutcome(
        action=action,
        approval_ref=approval_ref,
        dispatch_ref=dispatch_ref,
        dispatch_status=dispatch.dispatch_status,
        command_ack_observed=dispatch.command_ack_observed,
        command_ack_result_name=dispatch.command_ack_result_name,
        ack_complete=ack_complete,
        state_observed=state_observed,
        state_label=state_label,
        completed=completed,
        pose_z_m=None if pose is None else pose.get("z"),
        completion_basis=completion_basis,
        completion_ref=completion_ref,
    )
    return ObservedRecoveryCycle(
        outcome=outcome,
        pose=pose,
        samples=samples,
        completion=completion,
    )


__all__ = [
    "ObservedRecoveryCycle",
    "RecoveryCompletionBuilder",
    "RecoveryObserver",
    "observe_dispatched_recovery",
]
