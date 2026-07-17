"""Contract tests for the shadow ledger, promotion CLI, and envelope loading.

This is the connective tissue an implementation-substance audit found
missing: shadow comparisons were recorded but nothing read them back, and
promotion evaluate/apply had no operator entry point or effect on future
missions. These tests pin the closed loop: record -> ledger -> evaluate ->
apply -> envelope.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from src.runtime.recovery_shadow_ledger import (
    collect_recovery_shadow_comparisons,
)
from src.runtime.task_store import TaskStore
from src.runtime.turtlebot3_home_mission import (
    TURTLEBOT3_PROMOTED_ACTIONS_JSON_ENV,
    TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL_ENV,
    TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL_ENV,
    TURTLEBOT3_RECOVERY_SHADOW_COMPARISON_SCHEMA_VERSION,
    _build_autonomy_envelope,
)

_CLI = ("scripts/turtlebot3_recovery_promotion_cli.py",)


def _shadow(action: str, *, agreement: bool | None = True) -> dict:
    return {
        "schema_version": TURTLEBOT3_RECOVERY_SHADOW_COMPARISON_SCHEMA_VERSION,
        "deterministic_action": action,
        "deterministic_trigger": "runtime_obstacle_observed",
        "llm_action": action if agreement else "hold",
        "llm_proposal_available": agreement is not None,
        "planner_status": "proposal_guardrail_passed",
        "agreement": agreement,
        "measurement_only": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def _seed_mission_task(store: TaskStore, shadow: dict) -> dict:
    # The same shadow dict is stored on multiple artifact surfaces, exactly
    # as a real mission does (plan proposal + execution summary).
    return store.create(
        kind="control_supervisor",
        title="tb3 mission with shadow evidence",
        status="completed",
        artifacts={
            "scenario_proposal": {
                "recovery_planner_result": {"shadow_comparison": shadow},
            },
            "turtlebot3_home_mission_execution": {
                "summary": {
                    "recovery_planner_result": {"shadow_comparison": shadow},
                },
            },
        },
    )


def test_ledger_collects_chronologically_and_dedupes_within_task() -> None:
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        for index in range(3):
            _seed_mission_task(
                store,
                _shadow("avoid_obstacle", agreement=index != 0),
            )
        ledger = collect_recovery_shadow_comparisons(task_store=store)

    assert ledger["task_count_scanned"] == 3
    # One entry per mission despite two storage surfaces per task.
    assert ledger["shadow_comparison_count"] == 3
    agreements = [entry["agreement"] for entry in ledger["shadow_comparisons"]]
    assert agreements == [False, True, True]
    assert all(entry["task_id"] for entry in ledger["shadow_comparisons"])
    assert ledger["dispatch_authority_created"] is False


def test_envelope_applies_valid_promotion_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL_ENV, "1"
    )
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL_ENV, raising=False)
    demoted = _build_autonomy_envelope(proposal_id="promotion_test")
    assert "avoid_obstacle" in demoted["requires_human_approval_for"]

    application = {
        "schema_version": "missionos_recovery_action_promotion_application.v1",
        "application_id": "recovery_action_promotion_application_testabcd",
        "proposal_ref": "recovery_action_promotion_proposal_testabcd",
        "action": "avoid_obstacle",
        "consecutive_agreement_count": 5,
        "operator_approval_ref": "approval_promote_avoid_obstacle",
        "approval_actor": "operator_alice",
        "approved_at": "2026-07-15T12:00:00+00:00",
        "previous_envelope_ref": "mission_autonomy_envelope_before",
        "new_envelope_ref": "mission_autonomy_envelope_after",
    }
    applications_path = tmp_path / "promotions.json"
    applications_path.write_text(json.dumps([application]), encoding="utf-8")
    monkeypatch.setenv(
        TURTLEBOT3_PROMOTED_ACTIONS_JSON_ENV, str(applications_path)
    )

    promoted = _build_autonomy_envelope(proposal_id="promotion_test")
    assert "avoid_obstacle" in promoted["preapproved_recovery_actions"]
    assert "avoid_obstacle" not in promoted["requires_human_approval_for"]
    assert promoted["applied_recovery_promotions"] == [
        "recovery_action_promotion_application_testabcd"
    ]


def test_envelope_ignores_invalid_promotion_entries(
    tmp_path: Path, monkeypatch
) -> None:
    applications_path = tmp_path / "promotions.json"
    applications_path.write_text(
        json.dumps(
            [
                {"action": "reroute"},  # no application record fields
                {
                    "schema_version": (
                        "missionos_recovery_action_promotion_application.v1"
                    ),
                    "application_id": "recovery_action_promotion_application_x",
                    "proposal_ref": "p",
                    "action": "reroute",
                    "consecutive_agreement_count": 5,
                    "operator_approval_ref": "   ",  # blank approval ref
                    "approval_actor": "operator",
                    "approved_at": "2026-07-15T12:00:00+00:00",
                    "previous_envelope_ref": "a",
                    "new_envelope_ref": "b",
                },
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        TURTLEBOT3_PROMOTED_ACTIONS_JSON_ENV, str(applications_path)
    )

    envelope = _build_autonomy_envelope(proposal_id="promotion_test")
    assert "reroute" in envelope["requires_human_approval_for"]
    assert envelope["applied_recovery_promotions"] == []


def test_master_tighten_env_blocks_all_promotions(
    tmp_path: Path, monkeypatch
) -> None:
    application = {
        "schema_version": "missionos_recovery_action_promotion_application.v1",
        "application_id": "recovery_action_promotion_application_master",
        "proposal_ref": "p",
        "action": "avoid_obstacle",
        "consecutive_agreement_count": 5,
        "operator_approval_ref": "approval_x",
        "approval_actor": "operator",
        "approved_at": "2026-07-15T12:00:00+00:00",
        "previous_envelope_ref": "a",
        "new_envelope_ref": "b",
    }
    applications_path = tmp_path / "promotions.json"
    applications_path.write_text(json.dumps([application]), encoding="utf-8")
    monkeypatch.setenv(
        TURTLEBOT3_PROMOTED_ACTIONS_JSON_ENV, str(applications_path)
    )
    monkeypatch.setenv(TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL_ENV, "1")

    envelope = _build_autonomy_envelope(proposal_id="promotion_test")
    assert envelope["preapproved_recovery_actions"] == []
    assert envelope["applied_recovery_promotions"] == []


def test_promotion_cli_evaluate_and_apply_close_the_loop(
    tmp_path: Path, monkeypatch
) -> None:
    """record -> ledger -> evaluate -> apply -> envelope, end to end."""

    monkeypatch.setenv(
        TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL_ENV, "1"
    )
    monkeypatch.delenv(TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL_ENV, raising=False)
    monkeypatch.delenv(TURTLEBOT3_PROMOTED_ACTIONS_JSON_ENV, raising=False)

    db_path = tmp_path / "tasks.db"
    store = TaskStore(str(db_path))
    for _ in range(5):
        _seed_mission_task(store, _shadow("avoid_obstacle", agreement=True))

    evaluate = subprocess.run(
        (
            sys.executable,
            *_CLI,
            "evaluate",
            "--db",
            str(db_path),
            "--min-consecutive-agreements",
            "5",
        ),
        capture_output=True,
        text=True,
        check=False,
        env=_cli_env(monkeypatch),
    )
    assert evaluate.returncode == 0, evaluate.stderr
    evaluated = json.loads(evaluate.stdout)
    assert evaluated["shadow_comparison_count"] == 5
    proposals = evaluated["promotion_proposals"]
    assert [proposal["action"] for proposal in proposals] == ["avoid_obstacle"]
    assert proposals[0]["consecutive_agreement_count"] == 5

    applications_path = tmp_path / "promotions.json"
    apply = subprocess.run(
        (
            sys.executable,
            *_CLI,
            "apply",
            "--db",
            str(db_path),
            "--min-consecutive-agreements",
            "5",
            "--action",
            "avoid_obstacle",
            "--operator-approval-ref",
            "approval_promote_avoid_obstacle",
            "--approval-actor",
            "operator_alice",
            "--applications-path",
            str(applications_path),
        ),
        capture_output=True,
        text=True,
        check=False,
        env=_cli_env(monkeypatch),
    )
    assert apply.returncode == 0, apply.stderr
    applied = json.loads(apply.stdout)
    assert applied["application"]["action"] == "avoid_obstacle"
    assert applied["application"]["operator_approval_ref"] == (
        "approval_promote_avoid_obstacle"
    )
    assert "avoid_obstacle" in applied["new_envelope"][
        "preapproved_recovery_actions"
    ]

    # The audit task landed in the task store.
    audit_task = store.get(applied["audit_task_id"])
    assert audit_task is not None
    assert audit_task["kind"] == "recovery_promotion"
    assert (
        audit_task["artifacts"]["recovery_action_promotion_application"][
            "application_id"
        ]
        == applied["application"]["application_id"]
    )

    # Future missions pick the promotion up through the env-pointed file.
    monkeypatch.setenv(
        TURTLEBOT3_PROMOTED_ACTIONS_JSON_ENV, str(applications_path)
    )
    envelope = _build_autonomy_envelope(proposal_id="post_promotion_mission")
    assert "avoid_obstacle" in envelope["preapproved_recovery_actions"]
    assert envelope["applied_recovery_promotions"] == [
        applied["application"]["application_id"]
    ]


def test_promotion_cli_apply_refuses_without_streak(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(
        TURTLEBOT3_RECOVERY_AVOID_OBSTACLE_REQUIRES_APPROVAL_ENV, "1"
    )
    db_path = tmp_path / "tasks.db"
    store = TaskStore(str(db_path))
    _seed_mission_task(store, _shadow("avoid_obstacle", agreement=True))

    apply = subprocess.run(
        (
            sys.executable,
            *_CLI,
            "apply",
            "--db",
            str(db_path),
            "--min-consecutive-agreements",
            "5",
            "--action",
            "avoid_obstacle",
            "--operator-approval-ref",
            "approval_x",
            "--applications-path",
            str(tmp_path / "promotions.json"),
        ),
        capture_output=True,
        text=True,
        check=False,
        env=_cli_env(monkeypatch),
    )
    assert apply.returncode == 1
    assert "no promotion proposal" in json.loads(apply.stdout)["error"]
    assert not (tmp_path / "promotions.json").exists()


def _cli_env(monkeypatch) -> dict:
    import os

    return dict(os.environ)
