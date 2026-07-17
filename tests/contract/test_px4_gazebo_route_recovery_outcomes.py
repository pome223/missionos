from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.runtime.px4_gazebo_route import recovery_outcomes


@dataclass(frozen=True)
class _Artifact:
    artifact_id: str

    def model_dump(self, *, mode: str) -> dict[str, str]:
        assert mode == "json"
        return {"artifact_id": self.artifact_id}


def _outcome(*, action: str = "rtl", completed: bool = True) -> recovery_outcomes.RecoveryCycleOutcome:
    return recovery_outcomes.RecoveryCycleOutcome(
        action=action,
        approval_ref="px4_gazebo_emergency_command_approval:approval-1",
        dispatch_ref="px4_gazebo_emergency_command_dispatch_result:dispatch-1",
        dispatch_status="accepted",
        command_ack_observed=True,
        command_ack_result_name="ACCEPTED",
        state_observed=completed,
        state_label=("return_to_launch_state_observed" if action == "rtl" else None),
        completed=completed,
        pose_z_m=0.1 if action == "land" else 1.0,
    )


def test_outcome_rejects_completion_without_state_observation() -> None:
    with pytest.raises(ValueError, match="observed recovery state"):
        recovery_outcomes.RecoveryCycleOutcome(
            action="land",
            completed=True,
            state_observed=False,
        )


def test_outcome_rejects_dispatch_without_approval_reference() -> None:
    with pytest.raises(ValueError, match="requires an approval reference"):
        recovery_outcomes.RecoveryCycleOutcome(
            action="rtl",
            dispatch_ref="dispatch:1",
        )


def test_payload_terminal_status_uses_observation_not_ack() -> None:
    ack_only = recovery_outcomes.RecoveryCycleOutcome(
        action="rtl",
        approval_ref="approval:1",
        dispatch_ref="dispatch:1",
        dispatch_status="accepted",
        command_ack_observed=True,
        completed=False,
        state_observed=False,
    )
    empty_post = recovery_outcomes.RecoveryCycleOutcome(action=None)

    assert recovery_outcomes.payload_recovery_terminal_status(
        payload_action="rtl",
        payload_outcome=ack_only,
        supervisor_loop_requested=False,
        post_recovery_outcome=empty_post,
    ) == ("payload_advisory_recovery_unconfirmed", "blocked")
    assert recovery_outcomes.payload_recovery_terminal_status(
        payload_action="rtl",
        payload_outcome=_outcome(),
        supervisor_loop_requested=False,
        post_recovery_outcome=empty_post,
    ) == ("payload_advisory_recovered_rtl", "completed")


def test_supervisor_status_requires_second_observed_outcome() -> None:
    assert recovery_outcomes.payload_recovery_terminal_status(
        payload_action="rtl",
        payload_outcome=_outcome(),
        supervisor_loop_requested=True,
        post_recovery_outcome=recovery_outcomes.RecoveryCycleOutcome(action="land"),
    ) == ("payload_supervisor_post_recovery_unconfirmed", "blocked")
    assert recovery_outcomes.payload_recovery_terminal_status(
        payload_action="rtl",
        payload_outcome=_outcome(),
        supervisor_loop_requested=True,
        post_recovery_outcome=_outcome(action="land"),
    ) == ("payload_supervisor_post_recovery_land_observed", "completed")


def test_payload_action_preserves_authority_boundary() -> None:
    action = recovery_outcomes.build_payload_recovery_action(
        advisory_ref="payload_feasibility_advisory:1",
        outcome=_outcome(),
        observed_at="2026-07-15T00:00:00+00:00",
    )

    assert action["operator_approval_performed"] is True
    assert action["approval_ref"].endswith("approval-1")
    assert action["dispatch_ref"].endswith("dispatch-1")
    assert action["approval_free_recovery_dispatch_allowed"] is False
    assert action["delivery_completion_claimed"] is False
    assert action["physical_execution_invoked"] is False


def test_payload_action_cannot_invent_missing_approval() -> None:
    with pytest.raises(ValueError, match="approval and dispatch references"):
        recovery_outcomes.build_payload_recovery_action(
            advisory_ref="payload_feasibility_advisory:1",
            outcome=recovery_outcomes.RecoveryCycleOutcome(action="rtl"),
            observed_at="2026-07-15T00:00:00+00:00",
        )


def test_task_artifacts_drop_absent_values_without_rewriting_evidence() -> None:
    values = recovery_outcomes.recovery_task_artifacts(
        deviation_abort={"abort_id": "abort-1"},
        approval=_Artifact("approval-1"),
        dispatch=_Artifact("dispatch-1"),
        payload_recovery_action={"action_ref": "action-1"},
        post_dispatch=None,
    )

    assert values == {
        "px4_gazebo_route_deviation_abort": {"abort_id": "abort-1"},
        "px4_gazebo_emergency_command_approval": {
            "artifact_id": "approval-1"
        },
        "px4_gazebo_emergency_command_dispatch_result": {
            "artifact_id": "dispatch-1"
        },
        "payload_recovery_action": {"action_ref": "action-1"},
    }
