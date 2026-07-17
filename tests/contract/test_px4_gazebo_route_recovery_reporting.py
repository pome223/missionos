from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.runtime.px4_gazebo_route import recovery_outcomes
from src.runtime.px4_gazebo_route import recovery_reporting


def _outcome(
    *,
    action: str | None,
    observed: bool,
    approval: str = "approval:1",
    dispatch: str = "dispatch:1",
) -> recovery_outcomes.RecoveryCycleOutcome:
    return recovery_outcomes.RecoveryCycleOutcome(
        action=action,
        approval_ref=approval if action is not None else None,
        dispatch_ref=dispatch if action is not None else None,
        dispatch_status="accepted" if action is not None else None,
        command_ack_observed=True if action is not None else None,
        command_ack_result_name="ACCEPTED" if action is not None else None,
        ack_complete=observed,
        state_observed=observed,
        state_label="return_to_launch_state_observed" if action == "rtl" else None,
        completed=observed,
        pose_z_m=0.1 if action == "land" else 1.0,
        completion_basis=(
            "ack_observed_and_state_observed" if action is not None else None
        ),
        completion_ref="completion:1" if action is not None else None,
    )


def test_payload_summary_keeps_recovery_and_delivery_completion_separate() -> None:
    summary = recovery_reporting.build_payload_recovery_summary(
        recovery_reporting.PayloadRecoverySummaryInputs(
            artifact_dir=Path("output/run"),
            task_status="completed",
            existing_artifacts_retained=True,
            final_status="payload_advisory_recovered_rtl",
            advisory_ref="payload_feasibility_advisory:1",
            payload_action_ref="payload_recovery_action:1",
            payload_outcome=_outcome(action="rtl", observed=True),
            payload_action_artifact={"schema_version": "payload_recovery_action.v1"},
            payload_route_progress_payload=None,
            payload_route_progress_away_from_pickup_observed=False,
            payload_pre_recovery_distance_to_pickup_m=None,
            payload_recovery_distance_to_pickup_m=0.5,
            post_recovery_outcome=_outcome(action=None, observed=False),
            payload_post_recovery_action_ref=None,
            payload_post_recovery_action_artifact=None,
            supervisor_loop=None,
            wind_realism_artifacts={"wind_readback": "observed"},
            vehicle_realism_artifacts={"vehicle_readback": "observed"},
        )
    )

    assert summary["payload_recovery_completed"] is True
    assert summary["delivery_completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["decision_loop_driver"] == "scripted_payload_recovery_smoke"
    assert summary["wind_readback"] == "observed"


def test_payload_summary_rejects_extra_authority_overwrite() -> None:
    inputs = recovery_reporting.PayloadRecoverySummaryInputs(
        artifact_dir=Path("output/run"),
        task_status="blocked",
        existing_artifacts_retained=True,
        final_status="payload_advisory_recovery_unconfirmed",
        advisory_ref="payload_feasibility_advisory:1",
        payload_action_ref="payload_recovery_action:1",
        payload_outcome=_outcome(action="rtl", observed=False),
        payload_action_artifact={},
        payload_route_progress_payload=None,
        payload_route_progress_away_from_pickup_observed=False,
        payload_pre_recovery_distance_to_pickup_m=None,
        payload_recovery_distance_to_pickup_m=None,
        post_recovery_outcome=_outcome(action=None, observed=False),
        payload_post_recovery_action_ref=None,
        payload_post_recovery_action_artifact=None,
        supervisor_loop=None,
        wind_realism_artifacts={"delivery_completion_claimed": True},
        vehicle_realism_artifacts={},
    )
    with pytest.raises(ValueError, match="cannot overwrite"):
        recovery_reporting.build_payload_recovery_summary(inputs)


def test_deviation_summary_does_not_treat_ack_as_observed_recovery() -> None:
    ack_only = _outcome(action="rtl", observed=False)
    summary = recovery_reporting.build_route_deviation_recovery_summary(
        recovery_reporting.RouteDeviationRecoverySummaryInputs(
            artifact_dir=Path("output/run"),
            task_status="blocked",
            existing_artifacts_retained=True,
            final_status="emergency_recovery_unconfirmed",
            deviation_abort=SimpleNamespace(
                schema_version="px4_gazebo_route_deviation_abort.v1",
                abort_id="abort-1",
            ),
            route=SimpleNamespace(
                schema_version="px4_gazebo_pickup_dropoff_route_plan.v1",
                on_deviation_action="rtl",
            ),
            route_stream={
                "deviation_samples": [{"deviation_xy_m": 2.0}],
                "route_monitor_sample_count": 2,
                "route_stream_terminated_before_recovery_dispatch": True,
                "route_stream_process_returncode": 0,
                "route_stream_stop_reason": "pose_deviation",
                "route_stream_forced_kill": False,
            },
            recovery_outcome=ack_only,
            recovery_completion=SimpleNamespace(
                schema_version="px4_gazebo_route_recovery_completion.v1"
            ),
            post_recovery_outcome=_outcome(action=None, observed=False),
            supervisor_loop=None,
            wind_realism_artifacts={},
            vehicle_realism_artifacts={},
        )
    )

    assert summary["recovery_command_ack_observed"] is True
    assert summary["recovery_state_observed"] is False
    assert summary["recovery_completed"] is False
    assert summary["delivery_completion_claimed"] is False


def test_recovery_pose_rows_keep_phase_order_and_do_not_claim_evidence() -> None:
    rows = recovery_reporting.recovery_pose_rows(
        pre_phase="before",
        pre_pose={"x": 0.0, "y": 0.0, "z": 1.0},
        primary_phase="recovery_rtl",
        primary_samples=[{"x": 1.0, "y": 0.0, "z": 1.0}],
        primary_pose={"x": 0.1, "y": 0.0, "z": 1.0},
        primary_completed_phase="recovery_completed",
        post_phase="post_recovery_land",
        post_samples=[{"x": 0.1, "y": 0.0, "z": 0.5}],
        post_pose={"x": 0.1, "y": 0.0, "z": 0.1},
        post_completed_phase="post_recovery_completed",
    )

    assert [row["phase"] for row in rows] == [
        "before",
        "recovery_rtl",
        "recovery_completed",
        "post_recovery_land",
        "post_recovery_completed",
    ]
    assert all("delivery_completion_claimed" not in row for row in rows)
