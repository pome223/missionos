from __future__ import annotations

from rich.console import Console

from missionos_cli import cli as missionos_cli
from missionos_cli.flight_map_html import _mission_map_html
from missionos_cli.job_status import _vla_mission_job_operator_summary
from missionos_cli.map_model import _mission_map_model
from missionos_cli.operate_commands import OperateConsoleCommand
from missionos_cli.operate_view import (
    _build_operate_status_group,
    _operate_robot_from_task_payload,
)
from missionos_cli.vla_operator import _vla_operator_snapshot


def _vla_task(*, status: str = "completed", recovery_status: str = "not_required") -> dict:
    task = {
        "task_id": "task_vla_companion",
        "kind": "vla_mission_execution",
        "status": status,
        "artifacts": {
            "physical_ai_mission_proposal": {
                "parent_run_identity": "chat-run:test",
                "episode_identity": "chat-run:test:episode-1",
                "proposal_sha256": "1" * 64,
                "vla_task_selection": {
                    "catalog_entry_id": "catalog:libero-stove-moka",
                    "display_name": "Turn on stove and place moka pot",
                    "environment": "libero_sim/task",
                },
            },
            "physical_ai_mission_approval": {"approval_sha256": "2" * 64},
            "missionos_vla_mission_run_record": {
                "run_identity": "chat-run:test",
                "episode_identity": "chat-run:test:episode-1",
                "contract_sha256": "3" * 64,
                "execution_mode": "live",
                "bounded_outcome_claimed": True,
                "controller_ack_observed": False,
                "policy_instruction_delivery_observed": False,
                "mission_completion_claimed": False,
                "physical_execution_invoked": False,
                "predicate_evaluation": {
                    "status": "satisfied",
                    "evidence_readiness": "ready",
                    "actual_verification_basis": "deterministic",
                    "outcome_claim_scope": "exact_libero_episode",
                    "observation_content_sha256": "4" * 64,
                },
            },
            "missionos_vla_recovery_state": {
                "recovery_status": recovery_status,
                "repair_action": (
                    "retry_same_frozen_task"
                    if recovery_status == "operator_review_required"
                    else "none"
                ),
                "proposal_status": (
                    "proposal_only"
                    if recovery_status == "operator_review_required"
                    else "none"
                ),
                "automatic_retry_allowed": False,
                "retry_requires_new_human_approval": True,
                "in_episode_intervention_available": False,
                "post_episode_repair_implemented": True,
                "dispatch_authority_created": False,
            },
        },
    }
    if recovery_status == "operator_review_required":
        task["artifacts"][
            "missionos_vla_post_episode_repair_last_proposal"
        ] = {
            "schema_version": (
                "missionos_vla_post_episode_repair_proposal.v1"
            ),
            "proposal_id": "vla-repair:one",
            "proposal_sha256": "5" * 64,
            "proposal_status": "awaiting_operator_approval",
            "repair_action": "retry_same_frozen_task",
            "proposal_source": "bounded_vla_post_episode_repair_planner",
            "source_failure_evidence_sha256": "6" * 64,
            "attempt_index": 1,
            "maximum_retry_attempts": 1,
        }
    return task


def _render(renderable) -> str:
    console = Console(record=True, width=180)
    console.print(renderable)
    return console.export_text()


def test_operate_uses_vla_profile_and_never_px4_controls() -> None:
    task = _vla_task()
    group, _ = _build_operate_status_group(
        task,
        proposal=None,
        pending=None,
        status="completed",
        task_id="task_vla_companion",
    )
    rendered = _render(group)

    assert _operate_robot_from_task_payload(task) == "vla"
    assert "governed VLA" in rendered
    assert "controller_ack=False" in rendered
    assert "physical_execution=False" in rendered
    help_text = _render(
        missionos_cli._operate_console_help_panel("task_vla_companion", robot="vla")
    )
    assert "no external mid-episode stop" in help_text
    assert "request return-to-launch" not in help_text
    assert "request land" not in help_text


def test_operate_blocks_vla_dispatch_before_gateway_recovery_route(monkeypatch) -> None:
    class Client:
        def __init__(self) -> None:
            self.dispatch_calls = 0

        def get(self, _path: str) -> dict:
            return _vla_task(status="failed", recovery_status="operator_review_required")

        def recovery_dispatch(self, **_kwargs):
            self.dispatch_calls += 1
            raise AssertionError("VLA dispatch route must not be called")

    client = Client()
    monkeypatch.setattr(missionos_cli, "console", Console(record=True, width=180))
    result = missionos_cli._handle_operate_console_command(
        client,
        "task_vla_companion",
        OperateConsoleCommand(kind="dispatch", action="land"),
    )

    assert result is True
    assert client.dispatch_calls == 0
    rendered = missionos_cli.console.export_text()
    assert "no verified in-episode" in rendered
    assert "dispatched" in rendered


def test_operate_approves_only_exact_vla_post_episode_retry(monkeypatch) -> None:
    class Client:
        def __init__(self) -> None:
            self.repair_calls: list[dict] = []
            self.generic_dispatch_calls = 0

        def get(self, _path: str) -> dict:
            return _vla_task(
                status="failed",
                recovery_status="operator_review_required",
            )

        def vla_post_episode_repair_approve_and_run(self, **kwargs):
            self.repair_calls.append(kwargs)
            return {
                "summary": {
                    "retry_task_id": "task_vla_retry",
                    "attempt_index": 1,
                }
            }

        def recovery_dispatch(self, **_kwargs):
            self.generic_dispatch_calls += 1
            raise AssertionError("generic dispatch must not own VLA retry")

    client = Client()
    monkeypatch.setattr(missionos_cli.click, "confirm", lambda *_a, **_k: True)
    monkeypatch.setattr(missionos_cli, "console", Console(record=True, width=180))

    result = missionos_cli._handle_operate_console_command(
        client,
        "task_vla_companion",
        OperateConsoleCommand(kind="approve_pending"),
    )

    assert result is True
    assert client.generic_dispatch_calls == 0
    assert client.repair_calls == [
        {
            "task_id": "task_vla_companion",
            "repair_proposal_id": "vla-repair:one",
            "expected_repair_proposal_sha256": "5" * 64,
        }
    ]
    rendered = missionos_cli.console.export_text()
    assert "task_vla_retry" in rendered
    assert "physical execution" in rendered


def test_chat_review_exposes_vla_retry_as_proposal_only() -> None:
    pending = missionos_cli._pending_recovery_approval_from_task(
        _vla_task(
            status="failed",
            recovery_status="operator_review_required",
        )
    )

    assert pending is not None
    assert pending["vla_post_episode_repair_supported"] is True
    assert pending["recovery_action"] == "retry_same_frozen_task"
    assert pending["automatic_retry_allowed"] is False
    assert pending["dispatch_authority_created"] is False
    rendered = _render(missionos_cli._render_chat_recovery_review(pending))
    assert "retry_same_frozen_task" in rendered
    assert "dispatch_authority=False" in rendered


def test_operate_keeps_failed_vla_task_open_for_pending_repair_review() -> None:
    class Client:
        def get(self, _path: str) -> dict:
            return _vla_task(
                status="failed",
                recovery_status="operator_review_required",
            )

    _group, console_status, _fingerprint = missionos_cli._operate_status_group(
        Client(),
        "task_vla_companion",
    )

    assert console_status == "awaiting_recovery_approval"


def test_job_status_shows_post_episode_repair_separately_from_in_episode() -> None:
    lines = _vla_mission_job_operator_summary(
        _vla_task(
            status="failed",
            recovery_status="operator_review_required",
        )
    )
    rendered = "\n".join(lines)

    assert "Recovery: status=operator_review_required" in rendered
    assert "action=retry_same_frozen_task" in rendered
    assert "in_episode=False" in rendered
    assert "post_episode=True" in rendered
    assert "automatic_retry=False" in rendered


def test_vla_map_is_non_spatial_evidence_timeline() -> None:
    model = _mission_map_model(
        task_payload=_vla_task(),
        provider="osm",
    )
    page = _mission_map_html(model)

    assert model["map_kind"] == "vla_evidence_timeline"
    assert model["spatial_map_claimed"] is False
    assert model["point_count"] == 0
    assert model["bounded_outcome_claimed"] is True
    assert "MissionOS VLA Evidence Timeline" in page
    assert "Non-spatial view" in page


def test_missing_vla_evidence_is_unknown_not_success() -> None:
    task = {
        "task_id": "task_vla_missing",
        "kind": "vla_mission_execution",
        "status": "running",
        "artifacts": {},
    }
    snapshot = _vla_operator_snapshot(task)

    assert snapshot["predicate_status"] == "unknown"
    assert snapshot["bounded_outcome_claimed"] == "unknown"
    assert snapshot["controller_ack_observed"] == "unknown"
    assert snapshot["recovery_status"] == "unknown"
