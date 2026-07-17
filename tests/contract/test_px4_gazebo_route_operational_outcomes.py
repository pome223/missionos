from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import operational_outcomes


@dataclass(frozen=True)
class _Approval:
    approval_id: str = "approval-1"
    operator_approval_performed: bool = True


@dataclass(frozen=True)
class _Allowlist:
    allowlist_id: str = "allowlist-1"


@dataclass(frozen=True)
class _Dispatch:
    dispatch_result_id: str = "dispatch-1"
    dispatch_status: str = "accepted"
    command_ack_observed: bool = True
    command_ack_result_code: int | None = 0
    recovery_command_sent: bool = True

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {
            "dispatch_status": self.dispatch_status,
            "command_ack_observed": self.command_ack_observed,
            "command_ack_result_code": self.command_ack_result_code,
            "command_ack_result_name": (
                "MAV_RESULT_ACCEPTED"
                if self.command_ack_result_code == 0
                else "MAV_RESULT_UNKNOWN"
            ),
            "recovery_command_sent": self.recovery_command_sent,
            "command_name": "MAV_CMD_NAV_RETURN_TO_LAUNCH",
            "command_id": 20,
        }


def _landing_candidate_summary() -> dict[str, object]:
    return {
        "alternate_landing_candidate_evidence": {
            "observed": {"alternate_landing_candidate": True}
        }
    }


def _route_blocking_summary() -> dict[str, object]:
    return {
        "route_blocking_verification": {
            "verification_status": "route_blocking_verified",
            "observed": {"route_blocking_verified": True},
        }
    }


def _landing_projection(
    *,
    dispatch: _Dispatch | None = None,
    pose: dict[str, float] | None = None,
) -> dict[str, Any]:
    return operational_outcomes.project_alternate_landing_outcome(
        alternate_landing_candidate_summary=_landing_candidate_summary(),
        emergency_approval=_Approval(),
        emergency_allowlist=_Allowlist(),
        emergency_dispatch=dispatch,
        completed_pose=pose,
        landing_samples=[] if pose is None else [pose],
    )


def _rth_projection(
    *,
    dispatch: _Dispatch | None = None,
    state_observed: bool = False,
) -> dict[str, Any]:
    return operational_outcomes.project_rth_outcome(
        route_blocking_verification_summary=_route_blocking_summary(),
        rth_requested=True,
        emergency_approval=_Approval(),
        emergency_allowlist=_Allowlist(),
        emergency_dispatch=dispatch,
        rth_state_observed=state_observed,
        rth_state_label=("return_home_position_observed" if state_observed else None),
        rth_pose=(
            {"x": 0.0, "y": 0.0, "z": -3.0}
            if state_observed
            else None
        ),
        rth_samples=(
            [{"x": 0.0, "y": 0.0, "z": -3.0}]
            if state_observed
            else []
        ),
    )


def _assert_false_claims(*artifacts: dict[str, Any]) -> None:
    for artifact in artifacts:
        assert artifact["physical_execution_invoked"] is False
        assert artifact["delivery_completion_claimed"] is False


def test_alternate_landing_not_requested_creates_no_dispatch_claim() -> None:
    result = operational_outcomes.project_alternate_landing_outcome(
        alternate_landing_candidate_summary={},
        emergency_approval=None,
        emergency_allowlist=None,
        emergency_dispatch=None,
        completed_pose=None,
        landing_samples=[],
    )

    request = result["alternate_landing_execution_request"]
    dispatch = result["alternate_landing_command_dispatch"]
    behavior = result["alternate_landing_behavior_observation"]
    assert request["request_status"] == "not_requested"
    assert request["operator_approval_performed"] is False
    assert dispatch["mavlink_dispatch_performed"] is False
    assert dispatch["approval_ref"] == ""
    assert behavior["alternate_landing_behavior_observed"] is False
    assert behavior["observation_status"] == "not_requested"
    _assert_false_claims(request, dispatch, behavior, result["alternate_landing_outcome"])


def test_alternate_landing_ack_without_pose_is_not_behavior_observation() -> None:
    result = _landing_projection(dispatch=_Dispatch(), pose=None)

    dispatch = result["alternate_landing_command_dispatch"]
    behavior = result["alternate_landing_behavior_observation"]
    assert dispatch["command_ack_observed"] is True
    assert dispatch["completion_basis"] == (
        "state_not_observed_or_command_unconfirmed"
    )
    assert behavior["landing_observed"] is False
    assert behavior["alternate_landing_behavior_observed"] is False
    assert result["alternate_landing_outcome"]["outcome_status"] == (
        "alternate_landing_behavior_pending_or_unconfirmed"
    )


def test_alternate_landing_requires_ack_and_observed_pose() -> None:
    result = _landing_projection(
        dispatch=_Dispatch(),
        pose={"x": 1.0, "y": 2.0, "z": 0.1},
    )

    dispatch = result["alternate_landing_command_dispatch"]
    behavior = result["alternate_landing_behavior_observation"]
    assert dispatch["approval_ref"].endswith("approval-1")
    assert dispatch["allowlist_ref"].endswith("allowlist-1")
    assert dispatch["emergency_dispatch_ref"].endswith("dispatch-1")
    assert behavior["alternate_landing_behavior_observed"] is True
    assert behavior["completion_basis"] == "ack_observed_and_state_observed"
    assert behavior["landing_sample_count"] == 1
    _assert_false_claims(dispatch, behavior, result["alternate_landing_outcome"])


def test_alternate_landing_accepts_observed_state_after_dispatch_timeout() -> None:
    result = _landing_projection(
        dispatch=_Dispatch(
            dispatch_status="timeout",
            command_ack_observed=False,
            command_ack_result_code=None,
        ),
        pose={"x": 1.0, "y": 2.0, "z": 0.1},
    )

    behavior = result["alternate_landing_behavior_observation"]
    assert behavior["alternate_landing_behavior_observed"] is True
    assert behavior["completion_basis"] == (
        "state_observed_after_dispatch_timeout"
    )


def test_rth_not_verified_is_not_requested() -> None:
    result = operational_outcomes.project_rth_outcome(
        route_blocking_verification_summary={},
        rth_requested=True,
        emergency_approval=None,
        emergency_allowlist=None,
        emergency_dispatch=None,
        rth_state_observed=False,
        rth_state_label=None,
        rth_pose=None,
        rth_samples=[],
    )

    request = result["rth_execution_request"]
    dispatch = result["rth_command_dispatch"]
    behavior = result["rth_behavior_observation"]
    assert request["request_status"] == "not_requested"
    assert dispatch["mavlink_dispatch_performed"] is False
    assert behavior["return_to_home_behavior_observed"] is False
    _assert_false_claims(request, dispatch, behavior, result["rth_outcome"])


def test_rth_ack_without_state_is_not_behavior_observation() -> None:
    result = _rth_projection(dispatch=_Dispatch(), state_observed=False)

    dispatch = result["rth_command_dispatch"]
    behavior = result["rth_behavior_observation"]
    assert dispatch["command_ack_observed"] is True
    assert dispatch["completion_basis"] == (
        "state_not_observed_or_command_unconfirmed"
    )
    assert behavior["rth_state_observed"] is False
    assert behavior["return_to_home_behavior_observed"] is False


def test_rth_requires_ack_and_observed_state() -> None:
    result = _rth_projection(dispatch=_Dispatch(), state_observed=True)

    behavior = result["rth_behavior_observation"]
    assert behavior["return_to_home_behavior_observed"] is True
    assert behavior["completion_basis"] == "ack_observed_and_state_observed"
    assert behavior["rth_state_label"] == "return_home_position_observed"
    assert behavior["rth_sample_count"] == 1
    _assert_false_claims(
        result["rth_command_dispatch"],
        behavior,
        result["rth_outcome"],
    )


def test_rth_accepts_observed_state_after_dispatch_timeout() -> None:
    result = _rth_projection(
        dispatch=_Dispatch(
            dispatch_status="timeout",
            command_ack_observed=False,
            command_ack_result_code=None,
        ),
        state_observed=True,
    )

    behavior = result["rth_behavior_observation"]
    assert behavior["return_to_home_behavior_observed"] is True
    assert behavior["completion_basis"] == (
        "state_observed_after_dispatch_timeout"
    )


def test_entrypoint_delegates_land_and_rth_outcome_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        route_entrypoint,
        "ALTERNATE_LANDING_CANDIDATE_SUMMARY",
        _landing_candidate_summary(),
    )
    monkeypatch.setattr(
        route_entrypoint,
        "ROUTE_BLOCKING_VERIFICATION_SUMMARY",
        _route_blocking_summary(),
    )
    monkeypatch.setattr(route_entrypoint, "_rth_behavior_requested", lambda: True)

    landing = route_entrypoint._alternate_landing_execution_realism(
        emergency_approval=_Approval(),
        emergency_allowlist=_Allowlist(),
        emergency_dispatch=_Dispatch(),
        completed_pose={"x": 1.0, "y": 2.0, "z": 0.1},
        landing_samples=[{"x": 1.0, "y": 2.0, "z": 0.1}],
    )
    rth = route_entrypoint._rth_behavior_execution_realism(
        emergency_approval=_Approval(),
        emergency_allowlist=_Allowlist(),
        emergency_dispatch=_Dispatch(),
        rth_state_observed=True,
        rth_state_label="return_home_position_observed",
        rth_pose={"x": 0.0, "y": 0.0, "z": -3.0},
        rth_samples=[{"x": 0.0, "y": 0.0, "z": -3.0}],
    )

    assert landing["alternate_landing_behavior_observation"][
        "alternate_landing_behavior_observed"
    ] is True
    assert rth["rth_behavior_observation"][
        "return_to_home_behavior_observed"
    ] is True
