from __future__ import annotations

from src.runtime.px4_gazebo_route.route_decision import (
    observe_route_blocking_decision,
)


def _summaries(*, alternate: bool = False, blocked: bool = False) -> dict:
    return {
        "moving_actor_pose": {"cycle": "observed"},
        "route_blocking_verification": {
            "route_blocking_verification": {"observed": {"route_blocking_verified": blocked}}
        },
        "alternate_landing_candidate": {
            "alternate_landing_candidate_evidence": {
                "observed": {"alternate_landing_candidate": alternate}
            }
        },
    }


def test_route_blocking_observation_retries_without_creating_authority() -> None:
    waits: list[int] = []
    sleeps: list[float] = []

    def observe(attempt: int) -> dict:
        return _summaries(alternate=attempt == 3, blocked=attempt == 3)

    result = observe_route_blocking_decision(
        observation_attempts=8,
        rth_requested=False,
        observe_once=observe,
        record_wait_observation=waits.append,
        sleep=sleeps.append,
    )

    assert result.alternate_landing_requested is True
    assert result.rth_behavior_requested is False
    assert result.observation_attempts_performed == 3
    assert waits == [1, 2]
    assert sleeps == [1.0, 1.0]
    assert "approval" not in result.__dict__
    assert "dispatch" not in result.__dict__


def test_rth_requires_both_request_and_verified_blocking() -> None:
    result = observe_route_blocking_decision(
        observation_attempts=1,
        rth_requested=True,
        observe_once=lambda _attempt: _summaries(blocked=True),
        record_wait_observation=lambda _attempt: None,
    )

    assert result.rth_behavior_requested is True
    assert result.alternate_landing_requested is False


def test_no_decision_preserves_empty_decision_snapshot() -> None:
    result = observe_route_blocking_decision(
        observation_attempts=2,
        rth_requested=True,
        observe_once=lambda _attempt: _summaries(),
        record_wait_observation=lambda _attempt: None,
        sleep=lambda _seconds: None,
    )

    assert result.decision_summaries == {}
    assert result.observation_attempts_performed == 2
