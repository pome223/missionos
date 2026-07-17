from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import SimpleNamespace
from typing import Any

import pytest

from src.runtime.px4_gazebo_route import recovery_execution


NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Approval:
    approval_id: str = "approval-1"


@dataclass(frozen=True)
class _Dispatch:
    dispatch_result_id: str = "dispatch-1"
    frame_sent: bool = True
    dispatch_status: str = "accepted"
    command_ack_observed: bool = True
    command_ack_result_name: str = "ACCEPTED"


class _Basis(str, Enum):
    OBSERVED = "ack_observed_and_state_observed"


def _observer(**_kwargs: Any) -> tuple[bool, str, dict[str, float], list[dict[str, float]]]:
    return (
        True,
        "return_to_launch_state_observed",
        {"x": 0.0, "y": 0.0, "z": 1.0},
        [{"x": 1.0, "y": 0.0, "z": 1.0}],
    )


def test_observation_boundary_does_not_dispatch_or_mint_approval() -> None:
    calls: list[dict[str, Any]] = []

    def observer(**kwargs: Any) -> tuple[bool, None, None, list[dict[str, float]]]:
        calls.append(kwargs)
        return False, None, None, []

    result = recovery_execution.observe_dispatched_recovery(
        action="rtl",
        approval=_Approval(),
        dispatch=_Dispatch(),
        pickup_pose={"x": 0.0, "y": 0.0, "z": 1.0},
        observe_state=observer,
    )

    assert len(calls) == 1
    assert calls[0]["dispatch_frame_sent"] is True
    assert result.outcome.approval_ref.endswith("approval-1")
    assert result.outcome.dispatch_ref.endswith("dispatch-1")
    assert result.outcome.command_ack_observed is True
    assert result.outcome.state_observed is False
    assert result.outcome.completed is False


def test_route_completion_controls_truth_after_observation() -> None:
    def completion_builder(**kwargs: Any) -> SimpleNamespace:
        assert kwargs["recovery_state_observed"] is True
        assert kwargs["recovery_pose_z_m"] == 1.0
        return SimpleNamespace(
            recovery_completion_id="completion-1",
            recovery_completed=True,
            recovery_ack_complete=True,
            recovery_state_observed=True,
            recovery_state_label="return_to_launch_state_observed",
            recovery_completion_basis=_Basis.OBSERVED,
        )

    result = recovery_execution.observe_dispatched_recovery(
        action="rtl",
        approval=_Approval(),
        dispatch=_Dispatch(),
        pickup_pose={"x": 2.0, "y": 3.0, "z": 1.0},
        observe_state=_observer,
        deviation_abort=object(),
        build_completion=completion_builder,
        observed_at=NOW,
    )

    assert result.outcome.completed is True
    assert result.outcome.completion_basis == _Basis.OBSERVED.value
    assert result.outcome.completion_ref.endswith("completion-1")
    assert result.pose == {"x": 0.0, "y": 0.0, "z": 1.0}
    assert len(result.samples) == 1


def test_route_completion_requires_abort_and_timestamp() -> None:
    with pytest.raises(ValueError, match="deviation abort and observed_at"):
        recovery_execution.observe_dispatched_recovery(
            action="rtl",
            approval=_Approval(),
            dispatch=_Dispatch(),
            pickup_pose={"x": 0.0, "y": 0.0, "z": 1.0},
            observe_state=_observer,
            build_completion=lambda **_kwargs: None,
        )
