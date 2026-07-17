from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import alternate_route


@dataclass(frozen=True)
class _Approval:
    approval_id: str = "approval-1"
    operator_approval_performed: bool = True


@dataclass(frozen=True)
class _Allowlist:
    allowlist_id: str = "allowlist-1"
    operator_approval_ref: str = "px4_gazebo_coupled_command_approval:approval-1"


def _candidate_summary(*, requested: bool = True) -> dict[str, Any]:
    return {
        "alternate_landing_candidate_evidence": {
            "observed": {
                "alternate_landing_candidate": requested,
                "candidate_id": "candidate-1",
                "candidate_xy_m": [4.0, 3.0],
            }
        }
    }


def _upload_ack() -> dict[str, Any]:
    return {
        "mission_ack_observed": True,
        "mission_ack_type": 0,
        "mission_request_sequences": [0, 1, 2],
    }


def _assert_no_completion_or_physical_claims(
    *artifacts: dict[str, Any],
) -> None:
    for artifact in artifacts:
        assert artifact["physical_execution_invoked"] is False
        assert artifact["delivery_completion_claimed"] is False


def test_alternate_route_blocks_before_transport_without_upload_ack() -> None:
    callback_calls: list[str] = []

    result = alternate_route.execute_alternate_route_rewrite(
        candidate_summary=_candidate_summary(),
        target_z=-5.0,
        altitude_max_m=20.0,
        upload_result=None,
        approval=_Approval(),
        route_allowlist=_Allowlist(),
        pose_sample=lambda: callback_calls.append("pose") or {},
        send_route_with_monitor=lambda **_kwargs: callback_calls.append("dispatch"),
        append_live_pose_row=lambda _phase, _pose: callback_calls.append("trace"),
    )

    assert callback_calls == []
    assert result["sent"] is False
    assert result["blocked_reasons"] == ["alternate_mission_upload_ack_not_observed"]
    dispatch = result["dispatch_evidence"]
    assert dispatch["mavlink_dispatch_performed"] is False
    assert dispatch["approval_ref"].endswith("approval-1")
    assert dispatch["allowlist_ref"].endswith("allowlist-1")
    _assert_no_completion_or_physical_claims(dispatch)


def test_alternate_route_requires_pose_progress_after_supplied_dispatch() -> None:
    poses = iter(
        [
            {"x": 0.0, "y": 0.0, "z": -5.0},
            {"x": 3.5, "y": 2.5, "z": -5.0},
        ]
    )
    trace_rows: list[tuple[str, dict[str, float]]] = []
    dispatch_kwargs: dict[str, Any] = {}

    def send_route(**kwargs: Any) -> dict[str, Any]:
        dispatch_kwargs.update(kwargs)
        return {"mode": "route", "sent": True, "blocked_reasons": []}

    result = alternate_route.execute_alternate_route_rewrite(
        candidate_summary=_candidate_summary(),
        target_z=-5.0,
        altitude_max_m=20.0,
        upload_result=_upload_ack(),
        approval=_Approval(),
        route_allowlist=_Allowlist(),
        pose_sample=lambda: next(poses),
        send_route_with_monitor=send_route,
        append_live_pose_row=lambda phase, pose: trace_rows.append((phase, dict(pose))),
    )

    assert dispatch_kwargs["target_x"] == 3.0
    assert dispatch_kwargs["target_y"] == 4.0
    assert result["sent"] is True
    assert result["alternate_route_execution_observed"] is True
    assert result["alternate_waypoint_reached_observed"] is True
    assert result["completion_basis"] == ("alternate_waypoint_reached_from_pose_progress")
    assert trace_rows == [
        (
            "alternate_route_rewrite",
            {"x": 3.5, "y": 2.5, "z": -5.0},
        )
    ]
    _assert_no_completion_or_physical_claims(
        result,
        result["dispatch_evidence"],
    )


def test_alternate_route_rejects_unapproved_or_mismatched_authority() -> None:
    callback_calls: list[str] = []

    result = alternate_route.execute_alternate_route_rewrite(
        candidate_summary=_candidate_summary(),
        target_z=-5.0,
        altitude_max_m=20.0,
        upload_result=_upload_ack(),
        approval=_Approval(operator_approval_performed=False),
        route_allowlist=_Allowlist(operator_approval_ref="wrong-approval"),
        pose_sample=lambda: callback_calls.append("pose") or {},
        send_route_with_monitor=lambda **_kwargs: callback_calls.append("dispatch"),
        append_live_pose_row=lambda _phase, _pose: callback_calls.append("trace"),
    )

    assert callback_calls == []
    assert result["blocked_reasons"] == [
        "alternate_route_operator_approval_missing",
        "alternate_route_allowlist_approval_mismatch",
    ]
    assert result["dispatch_evidence"]["mavlink_dispatch_performed"] is False


def test_upload_ack_alone_is_not_route_execution_evidence() -> None:
    result = alternate_route.project_alternate_mission_upload(
        candidate_summary=_candidate_summary(),
        upload_result=_upload_ack(),
        alternate_behavior_observation={},
        alternate_route_execution=None,
        route_endpoint_port=14580,
        operator_approval_performed=True,
    )

    request = result["alternate_mission_upload_request"]
    receipt = result["alternate_mission_upload_receipt"]
    dispatch = result["alternate_route_command_dispatch"]
    evidence = result["alternate_route_execution_evidence"]
    assert request["operator_approval_performed"] is True
    assert receipt["alternate_mission_uploaded"] is True
    assert receipt["mission_item_count"] == 3
    assert dispatch["mavlink_dispatch_performed"] is False
    assert evidence["alternate_route_execution_observed"] is False
    assert evidence["alternate_waypoint_reached_observed"] is False
    assert evidence["observation_status"] == ("alternate_route_execution_not_observed")
    _assert_no_completion_or_physical_claims(
        request,
        receipt,
        dispatch,
        evidence,
    )


def test_projection_does_not_invent_operator_approval() -> None:
    result = alternate_route.project_alternate_mission_upload(
        candidate_summary=_candidate_summary(),
        upload_result=None,
        alternate_behavior_observation={},
        alternate_route_execution=None,
        route_endpoint_port=14580,
        operator_approval_performed=False,
    )

    request = result["alternate_mission_upload_request"]
    assert request["requested_present"] is True
    assert request["request_status"] == "approval_missing"
    assert request["operator_approval_performed"] is False


def test_entrypoint_delegates_projection_with_supplied_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def project(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"projected": True}

    monkeypatch.setattr(
        route_entrypoint,
        "ALTERNATE_LANDING_CANDIDATE_SUMMARY",
        _candidate_summary(),
    )
    monkeypatch.setattr(
        route_entrypoint,
        "_project_alternate_mission_upload",
        project,
    )

    result = route_entrypoint._alternate_mission_upload_realism(
        upload_result=_upload_ack(),
        alternate_behavior_observation={},
        alternate_route_execution=None,
        operator_approval_performed=False,
    )

    assert result == {"projected": True}
    assert captured["candidate_summary"] == _candidate_summary()
    assert captured["operator_approval_performed"] is False
