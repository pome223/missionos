#!/usr/bin/env python3
"""Opt-in smoke for PX4/Gazebo route recovery and golden corpus artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import socket
import threading
from tempfile import TemporaryDirectory

from src.runtime.px4_gazebo_coupled_delivery import (
    build_px4_gazebo_coupled_command_approval,
)
from src.runtime.px4_gazebo_route_delivery import (
    build_px4_gazebo_route_delivery_completion_gate,
)
from src.runtime.px4_gazebo_route_dispatcher import (
    build_px4_gazebo_route_command_allowlist,
    build_px4_gazebo_route_progress_evidence,
    run_px4_gazebo_route_command_dispatch,
)
from src.runtime.px4_gazebo_route_plan import (
    build_px4_gazebo_pickup_dropoff_route_plan,
)
from src.runtime.px4_gazebo_route_recovery import (
    PX4GazeboRouteGoldenCorpusCase,
    build_px4_gazebo_route_golden_corpus,
    build_px4_gazebo_route_recovery_allowlist,
    build_px4_gazebo_route_recovery_approval,
    build_px4_gazebo_route_recovery_diagnostics,
    build_px4_gazebo_route_recovery_proposal,
    run_px4_gazebo_route_recovery_task,
)
from src.runtime.task_store import TaskStore

OPT_IN_ENV = "RUN_PX4_GAZEBO_ROUTE_RECOVERY_CORPUS_SMOKE"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class _FakeRoutePX4Endpoint:
    def __init__(self) -> None:
        self.received: list[bytes] = []
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.port: int | None = None

    def __enter__(self) -> "_FakeRoutePX4Endpoint":
        self._thread.start()
        if not self._ready.wait(2):
            raise RuntimeError("fake route endpoint did not start")
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"x", ("127.0.0.1", self.port or 9))
        self._thread.join(timeout=2)

    def _run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.settimeout(0.2)
            self.port = int(sock.getsockname()[1])
            self._ready.set()
            while not self._stop.is_set():
                try:
                    data, _addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                if data == b"x":
                    continue
                self.received.append(data)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the PX4/Gazebo route recovery smoke."
        )


def _route_bundle():
    route = build_px4_gazebo_pickup_dropoff_route_plan(
        pickup_pad_ref="gazebo_pad:pickup",
        dropoff_pad_ref="gazebo_pad:dropoff",
        route_waypoint_refs=["gazebo_waypoint:mid"],
        geofence_polygon=[(-2.0, -2.0), (8.0, -2.0), (8.0, 8.0), (-2.0, 8.0)],
        altitude_min_m=1.0,
        altitude_max_m=4.0,
        min_battery_margin_pct=25.0,
        now=NOW,
    )
    approval = build_px4_gazebo_coupled_command_approval(
        operator_approval_performed=True,
        now=NOW,
    )
    allowlist = build_px4_gazebo_route_command_allowlist(
        route_plan=route,
        approval=approval,
        now=NOW,
    )
    with _FakeRoutePX4Endpoint() as endpoint:
        if endpoint.port is None:
            raise RuntimeError("fake route endpoint did not publish a port")
        dispatch = run_px4_gazebo_route_command_dispatch(
            route_plan=route,
            route_allowlist=allowlist,
            approval=approval,
            endpoint_port=endpoint.port,
            live_mavlink_opt_in=True,
            now=NOW,
        )
    progress = build_px4_gazebo_route_progress_evidence(
        route_plan=route,
        route_dispatch_result=dispatch,
        pickup_pose_xy_m=(0.0, 0.0),
        observed_pose_xy_m=(7.25, 4.0),
        now=NOW,
    )
    return route, dispatch, progress


def _corpus_extra_cases() -> list[PX4GazeboRouteGoldenCorpusCase]:
    return [
        PX4GazeboRouteGoldenCorpusCase(
            case_id="blocked:mavlink_timeout",
            expected_terminal_status="blocked",
            required_artifact_schema_versions=(
                "px4_gazebo_route_recovery_diagnostics.v1",
            ),
            expected_blocked_reasons=("mavlink_timeout",),
        ),
        PX4GazeboRouteGoldenCorpusCase(
            case_id="blocked:command_rejected",
            expected_terminal_status="blocked",
            required_artifact_schema_versions=(
                "px4_gazebo_route_recovery_diagnostics.v1",
            ),
            expected_blocked_reasons=("command_rejected",),
        ),
        PX4GazeboRouteGoldenCorpusCase(
            case_id="blocked:wrong_target",
            expected_terminal_status="blocked",
            required_artifact_schema_versions=(
                "px4_gazebo_route_recovery_diagnostics.v1",
            ),
            expected_blocked_reasons=("wrong_target",),
        ),
        PX4GazeboRouteGoldenCorpusCase(
            case_id="blocked:route_geofence_violation",
            expected_terminal_status="blocked",
            required_artifact_schema_versions=(
                "px4_gazebo_route_delivery_completion_gate.v1",
            ),
            expected_blocked_reasons=("route_geofence_violation",),
        ),
        PX4GazeboRouteGoldenCorpusCase(
            case_id="blocked:route_pose_missing",
            expected_terminal_status="blocked",
            required_artifact_schema_versions=(
                "px4_gazebo_route_delivery_completion_gate.v1",
            ),
            expected_blocked_reasons=("route_pose_missing",),
        ),
        PX4GazeboRouteGoldenCorpusCase(
            case_id="blocked:missing_px4_telemetry",
            expected_terminal_status="blocked",
            required_artifact_schema_versions=(
                "px4_gazebo_route_delivery_completion_gate.v1",
            ),
            expected_blocked_reasons=("missing_px4_telemetry_correlated",),
        ),
        PX4GazeboRouteGoldenCorpusCase(
            case_id="rejection:command_like_metadata",
            expected_terminal_status="blocked",
            required_artifact_schema_versions=(
                "px4_gazebo_route_recovery_diagnostics.v1",
            ),
            expected_blocked_reasons=("command_like_metadata_rejected",),
        ),
        PX4GazeboRouteGoldenCorpusCase(
            case_id="rejection:hardware_target_override",
            expected_terminal_status="blocked",
            required_artifact_schema_versions=(
                "px4_gazebo_route_recovery_diagnostics.v1",
            ),
            expected_blocked_reasons=("hardware_target_override_rejected",),
        ),
        PX4GazeboRouteGoldenCorpusCase(
            case_id="recovery:state_observed_after_dispatch_timeout",
            expected_terminal_status="completed",
            required_artifact_schema_versions=(
                "px4_gazebo_route_recovery_completion.v1",
            ),
            expected_recovery_completion_basis="state_observed_after_dispatch_timeout",
        ),
        PX4GazeboRouteGoldenCorpusCase(
            case_id="recovery:hold_state_observed_after_dispatch_timeout",
            expected_terminal_status="completed",
            required_artifact_schema_versions=(
                "px4_gazebo_route_recovery_completion.v1",
            ),
            expected_recovery_completion_basis="state_observed_after_dispatch_timeout",
            expected_recovery_action="hold",
        ),
        PX4GazeboRouteGoldenCorpusCase(
            case_id="recovery:rtl_ack_observed_and_state_observed",
            expected_terminal_status="completed",
            required_artifact_schema_versions=(
                "px4_gazebo_route_recovery_completion.v1",
            ),
            expected_recovery_completion_basis="ack_observed_and_state_observed",
            expected_recovery_action="return_to_launch",
        ),
        PX4GazeboRouteGoldenCorpusCase(
            case_id="recovery:state_not_observed_after_dispatch_timeout",
            expected_terminal_status="blocked",
            required_artifact_schema_versions=(
                "px4_gazebo_route_recovery_completion.v1",
            ),
            expected_blocked_reasons=("emergency_recovery_unconfirmed",),
            expected_recovery_completion_basis=(
                "state_not_observed_after_dispatch_timeout"
            ),
        ),
        PX4GazeboRouteGoldenCorpusCase(
            case_id="recovery:dispatch_blocked_before_send",
            expected_terminal_status="blocked",
            required_artifact_schema_versions=(
                "px4_gazebo_route_recovery_completion.v1",
            ),
            expected_blocked_reasons=("emergency_recovery_dispatch_blocked",),
            expected_recovery_completion_basis="dispatch_blocked_before_send",
        ),
    ]


def main() -> None:
    _require_opt_in()
    route, dispatch, progress = _route_bundle()
    completed_gate = build_px4_gazebo_route_delivery_completion_gate(
        route_plan=route,
        route_dispatch_result=dispatch,
        route_progress_evidence=progress,
        horizontal_route_motion_observed=True,
        px4_telemetry_correlated=True,
        gazebo_pose_correlated=True,
        actual_px4_gazebo_horizontal_smoke_observed=True,
        now=NOW,
    )
    stale_gate = build_px4_gazebo_route_delivery_completion_gate(
        route_plan=route,
        route_dispatch_result=dispatch,
        route_progress_evidence=progress,
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
    recovery_approval = build_px4_gazebo_route_recovery_approval(
        proposal=proposal,
        operator_approval_performed=True,
        now=NOW,
    )
    recovery_allowlist = build_px4_gazebo_route_recovery_allowlist(
        proposal=proposal,
        approval=recovery_approval,
        now=NOW,
    )
    corpus = build_px4_gazebo_route_golden_corpus(
        completion_gates=[completed_gate, stale_gate],
        recovery_proposals=[proposal],
        extra_cases=_corpus_extra_cases(),
        command_leakage_rejection_case_ids=[
            "rejection:command_like_metadata",
            "rejection:hardware_target_override",
        ],
        now=NOW,
    )
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_gazebo_route_recovery",
            title="PX4/Gazebo route recovery smoke",
            status="running",
            artifacts={"existing": {"case_id": "route-recovery", "kept": True}},
        )
        updated = run_px4_gazebo_route_recovery_task(
            task["task_id"],
            completion_gate=stale_gate,
            recovery_proposal=proposal,
            recovery_approval=recovery_approval,
            recovery_allowlist=recovery_allowlist,
            recovery_diagnostics=missing_approval_diagnostics,
            golden_corpus=corpus,
            task_store_factory=lambda: store,
        )

    summary = {
        "task_status": updated["status"],
        "existing_artifacts_retained": updated["artifacts"]["existing"]["kept"],
        "completed_gate_status": completed_gate.final_status.value,
        "blocked_gate_status": stale_gate.final_status.value,
        "proposal_schema_version": proposal.schema_version,
        "proposal_action": proposal.recommended_action.value,
        "approval_schema_version": recovery_approval.schema_version,
        "allowlist_schema_version": recovery_allowlist.schema_version,
        "diagnostics_schema_version": missing_approval_diagnostics.schema_version,
        "diagnostics_blocked_reasons": list(
            missing_approval_diagnostics.blocked_reasons
        ),
        "golden_corpus_schema_version": corpus.schema_version,
        "golden_corpus_coverage_labels": list(corpus.coverage_labels),
        "golden_corpus_case_count": len(corpus.corpus_cases),
        "golden_corpus_blocked_case_count": len(corpus.blocked_case_ids),
        "command_leakage_rejection_case_count": len(
            corpus.command_leakage_rejection_cases
        ),
        "recovery_command_sent": proposal.recovery_command_sent,
        "approval_free_recovery_dispatch_allowed": (
            proposal.approval_free_recovery_dispatch_allowed
        ),
        "recovery_allowlist_dispatch_allowed": (
            recovery_allowlist.recovery_command_dispatch_allowed
        ),
        "hardware_target_allowed": proposal.hardware_target_allowed,
        "physical_execution_invoked": proposal.physical_execution_invoked,
        "unbounded_setpoint_stream_allowed": proposal.unbounded_setpoint_stream_allowed,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    assert summary["task_status"] == "blocked"
    assert summary["existing_artifacts_retained"] is True
    assert summary["completed_gate_status"] == "completed"
    assert summary["blocked_gate_status"] == "blocked"
    assert summary["proposal_action"] == "hold"
    assert "missing_recovery_approval" in summary["diagnostics_blocked_reasons"]
    assert summary["golden_corpus_case_count"] == 16
    assert summary["golden_corpus_blocked_case_count"] == 12
    assert summary["command_leakage_rejection_case_count"] == 2
    assert "rejected_command" in summary["golden_corpus_coverage_labels"]
    assert "wrong_target" in summary["golden_corpus_coverage_labels"]
    assert "geofence_violation" in summary["golden_corpus_coverage_labels"]
    assert "missing_telemetry_or_pose" in summary["golden_corpus_coverage_labels"]
    assert "state_observed_recovery" in summary["golden_corpus_coverage_labels"]
    assert "hold_state_observed_recovery" in summary["golden_corpus_coverage_labels"]
    assert "rtl_state_observed_recovery" in summary["golden_corpus_coverage_labels"]
    assert "recovery_unconfirmed" in summary["golden_corpus_coverage_labels"]
    assert "recovery_dispatch_blocked" in summary["golden_corpus_coverage_labels"]
    assert "command_leakage_rejection" in summary["golden_corpus_coverage_labels"]
    assert summary["recovery_command_sent"] is False
    assert summary["approval_free_recovery_dispatch_allowed"] is False
    assert summary["recovery_allowlist_dispatch_allowed"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["unbounded_setpoint_stream_allowed"] is False


if __name__ == "__main__":
    main()
