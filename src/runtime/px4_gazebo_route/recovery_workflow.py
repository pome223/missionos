"""Truth-preserving state assembly for PX4 route-deviation recovery.

The caller owns action selection, fresh approval, allowlisting, dispatch, and
runtime observation.  This module receives only already-observed recovery
cycles and derives the terminal task/reporting state.  It cannot create
authority, invoke the backend, retry an action, or infer completion from an
ACK.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.runtime.px4_gazebo_route.recovery_execution import ObservedRecoveryCycle
from src.runtime.px4_gazebo_route.recovery_outcomes import RecoveryCycleOutcome


def _empty_cycle() -> ObservedRecoveryCycle:
    return ObservedRecoveryCycle(
        outcome=RecoveryCycleOutcome(action=None),
        pose=None,
        samples=(),
        completion=None,
    )


@dataclass(frozen=True)
class RouteDeviationRecoveryWorkflow:
    """Normalized primary/post observations and their terminal task state."""

    primary: ObservedRecoveryCycle
    post: ObservedRecoveryCycle
    final_status: str
    task_status: str


def assemble_route_deviation_recovery(
    *,
    primary: ObservedRecoveryCycle | None = None,
    post: ObservedRecoveryCycle | None = None,
) -> RouteDeviationRecoveryWorkflow:
    """Assemble route recovery state without performing recovery work."""

    if post is not None and primary is None:
        raise ValueError("post-recovery observation requires a primary cycle")
    if primary is not None and primary.completion is None:
        raise ValueError("route recovery primary cycle requires completion evidence")
    if post is not None:
        if not primary.outcome.completed:
            raise ValueError(
                "post-recovery observation requires completed primary recovery"
            )
        if post.completion is None:
            raise ValueError("route recovery post cycle requires completion evidence")

    normalized_primary = primary or _empty_cycle()
    normalized_post = post or _empty_cycle()
    if post is not None:
        final_status = f"post_recovery_{post.completion.final_status}"
        completed = post.outcome.completed
    elif primary is not None:
        final_status = str(primary.completion.final_status)
        completed = primary.outcome.completed
    else:
        final_status = "aborted_pose_deviation"
        completed = False

    return RouteDeviationRecoveryWorkflow(
        primary=normalized_primary,
        post=normalized_post,
        final_status=final_status,
        task_status="completed" if completed else "blocked",
    )


__all__ = [
    "RouteDeviationRecoveryWorkflow",
    "assemble_route_deviation_recovery",
]
