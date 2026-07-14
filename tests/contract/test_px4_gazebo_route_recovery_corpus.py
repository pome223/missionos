from pathlib import Path

from src.runtime.px4_gazebo_route_delivery import (
    build_px4_gazebo_route_delivery_completion_gate,
)
from src.runtime.px4_gazebo_route_recovery import (
    build_px4_gazebo_route_golden_corpus,
    build_px4_gazebo_route_recovery_allowlist,
    build_px4_gazebo_route_recovery_approval,
    build_px4_gazebo_route_recovery_diagnostics,
    build_px4_gazebo_route_recovery_proposal,
    run_px4_gazebo_route_recovery_task,
)
from src.runtime.task_store import TaskStore
from tests.fixtures.delivery_artifact_chain import NOW
from tests.fixtures.px4_gazebo_route_recovery import (
    build_route_bundle,
    route_recovery_extra_cases,
)


def test_route_recovery_corpus_preserves_authority_and_evidence_boundaries(
    tmp_path: Path,
) -> None:
    bundle = build_route_bundle()
    assert bundle.datagrams_received > 0
    completed_gate = build_px4_gazebo_route_delivery_completion_gate(
        route_plan=bundle.route,
        route_dispatch_result=bundle.dispatch,
        route_progress_evidence=bundle.progress,
        horizontal_route_motion_observed=True,
        px4_telemetry_correlated=True,
        gazebo_pose_correlated=True,
        actual_px4_gazebo_horizontal_smoke_observed=True,
        now=NOW,
    )
    stale_gate = build_px4_gazebo_route_delivery_completion_gate(
        route_plan=bundle.route,
        route_dispatch_result=bundle.dispatch,
        route_progress_evidence=bundle.progress,
        horizontal_route_motion_observed=True,
        px4_telemetry_correlated=True,
        gazebo_pose_correlated=True,
        route_progress_age_seconds=30.0,
        max_route_progress_age_seconds=5.0,
        now=NOW,
    )
    proposal = build_px4_gazebo_route_recovery_proposal(
        completion_gate=stale_gate,
        now=NOW,
    )
    missing_approval_diagnostics = build_px4_gazebo_route_recovery_diagnostics(
        proposal=proposal,
        recovery_unavailable_reason="missing_recovery_approval",
        now=NOW,
    )
    approval = build_px4_gazebo_route_recovery_approval(
        proposal=proposal,
        operator_approval_performed=True,
        now=NOW,
    )
    allowlist = build_px4_gazebo_route_recovery_allowlist(
        proposal=proposal,
        approval=approval,
        now=NOW,
    )
    corpus = build_px4_gazebo_route_golden_corpus(
        completion_gates=[completed_gate, stale_gate],
        recovery_proposals=[proposal],
        extra_cases=route_recovery_extra_cases(),
        command_leakage_rejection_case_ids=(
            "rejection:command_like_metadata",
            "rejection:hardware_target_override",
        ),
        now=NOW,
    )
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="px4_gazebo_route_recovery",
        title="PX4/Gazebo route recovery contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )

    updated = run_px4_gazebo_route_recovery_task(
        task["task_id"],
        completion_gate=stale_gate,
        recovery_proposal=proposal,
        recovery_approval=approval,
        recovery_allowlist=allowlist,
        recovery_diagnostics=missing_approval_diagnostics,
        golden_corpus=corpus,
        task_store_factory=lambda: store,
    )

    assert updated["status"] == "blocked"
    assert updated["artifacts"]["existing"] == {"kept": True}
    assert completed_gate.final_status.value == "completed"
    assert stale_gate.final_status.value == "blocked"
    assert proposal.recommended_action.value == "hold"
    assert "missing_recovery_approval" in (
        missing_approval_diagnostics.blocked_reasons
    )
    assert len(corpus.corpus_cases) == 16
    assert len(corpus.blocked_case_ids) == 12
    assert len(corpus.command_leakage_rejection_cases) == 2
    for label in (
        "rejected_command",
        "wrong_target",
        "geofence_violation",
        "missing_telemetry_or_pose",
        "state_observed_recovery",
        "hold_state_observed_recovery",
        "rtl_state_observed_recovery",
        "recovery_unconfirmed",
        "recovery_dispatch_blocked",
        "command_leakage_rejection",
    ):
        assert label in corpus.coverage_labels
    assert proposal.recovery_command_sent is False
    assert proposal.approval_free_recovery_dispatch_allowed is False
    assert allowlist.recovery_command_dispatch_allowed is False
    assert proposal.hardware_target_allowed is False
    assert proposal.physical_execution_invoked is False
    assert proposal.unbounded_setpoint_stream_allowed is False
