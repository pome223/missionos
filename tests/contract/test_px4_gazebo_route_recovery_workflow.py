from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.runtime.px4_gazebo_route.recovery_execution import ObservedRecoveryCycle
from src.runtime.px4_gazebo_route.recovery_outcomes import RecoveryCycleOutcome
from src.runtime.px4_gazebo_route.recovery_workflow import (
    assemble_route_deviation_recovery,
)


def _cycle(
    *,
    action: str,
    completed: bool,
    final_status: str,
) -> ObservedRecoveryCycle:
    return ObservedRecoveryCycle(
        outcome=RecoveryCycleOutcome(
            action=action,
            approval_ref=f"approval:{action}",
            dispatch_ref=f"dispatch:{action}",
            state_observed=completed,
            completed=completed,
        ),
        pose={"x": 1.0, "y": 2.0, "z": 0.2},
        samples=({"x": 0.0, "y": 0.0, "z": 1.0},),
        completion=SimpleNamespace(final_status=final_status),
    )


def test_empty_route_deviation_workflow_remains_blocked() -> None:
    workflow = assemble_route_deviation_recovery()

    assert workflow.final_status == "aborted_pose_deviation"
    assert workflow.task_status == "blocked"
    assert workflow.primary.outcome.action is None
    assert workflow.primary.outcome.completed is False
    assert workflow.post.outcome.action is None


def test_primary_recovery_status_comes_from_completion_evidence() -> None:
    primary = _cycle(
        action="rtl",
        completed=True,
        final_status="recovered_return_to_launch",
    )

    workflow = assemble_route_deviation_recovery(primary=primary)

    assert workflow.final_status == "recovered_return_to_launch"
    assert workflow.task_status == "completed"
    assert workflow.primary is primary
    assert workflow.post.outcome.action is None


def test_post_recovery_controls_terminal_state_without_delivery_claim() -> None:
    primary = _cycle(
        action="rtl",
        completed=True,
        final_status="recovered_return_to_launch",
    )
    post = _cycle(
        action="land",
        completed=False,
        final_status="landing_unconfirmed",
    )

    workflow = assemble_route_deviation_recovery(primary=primary, post=post)

    assert workflow.final_status == "post_recovery_landing_unconfirmed"
    assert workflow.task_status == "blocked"
    assert workflow.post is post
    assert workflow.post.outcome.completed is False


def test_post_recovery_requires_primary_completion_and_completion_evidence() -> None:
    primary_unconfirmed = _cycle(
        action="rtl",
        completed=False,
        final_status="recovery_unconfirmed",
    )
    post = _cycle(
        action="land",
        completed=True,
        final_status="landing_observed",
    )

    with pytest.raises(ValueError, match="requires a primary cycle"):
        assemble_route_deviation_recovery(post=post)
    with pytest.raises(ValueError, match="requires completed primary"):
        assemble_route_deviation_recovery(primary=primary_unconfirmed, post=post)
    with pytest.raises(ValueError, match="requires completion evidence"):
        assemble_route_deviation_recovery(
            primary=ObservedRecoveryCycle(
                outcome=RecoveryCycleOutcome(action="rtl"),
                pose=None,
                samples=(),
            )
        )
