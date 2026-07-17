#!/usr/bin/env python3
"""Operator CLI for recovery-action promotion (issue #31).

``evaluate`` reads accumulated shadow_comparison records from the task store
(via recovery_shadow_ledger) and prints which actions have earned a
promotion proposal under the current TB3 autonomy envelope.

``apply`` takes an explicit operator approval for one proposed action,
produces the promoted envelope plus a
RecoveryActionPromotionApplication audit record, appends the record to the
applications JSON file (which MISSIONOS_TURTLEBOT3_PROMOTED_ACTIONS_JSON
points future missions at), and stores an audit task in the task store.

Nothing here is automatic: evaluate never mutates anything, and apply
requires --operator-approval-ref. The applications file is
operator-controlled configuration, the same trust level as the existing
envelope-shaping environment variables.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from src.runtime.recovery_action_promotion import (
    DEFAULT_MIN_CONSECUTIVE_AGREEMENTS,
    RecoveryActionPromotionError,
    apply_recovery_action_promotion,
    evaluate_recovery_action_promotion_candidates,
)
from src.runtime.recovery_shadow_ledger import (
    DEFAULT_TASK_SCAN_LIMIT,
    collect_recovery_shadow_comparisons,
)
from src.runtime.task_store import TaskStore, get_task_store
from src.runtime.turtlebot3_home_mission import _build_autonomy_envelope


def _task_store(db_path: str | None) -> TaskStore:
    if db_path:
        return TaskStore(db_path)
    return get_task_store()


def _evaluate(args: argparse.Namespace) -> dict:
    store = _task_store(args.db)
    ledger = collect_recovery_shadow_comparisons(
        task_store=store,
        task_scan_limit=args.task_scan_limit,
    )
    envelope = _build_autonomy_envelope(
        proposal_id="turtlebot3_recovery_promotion_cli",
    )
    proposals = evaluate_recovery_action_promotion_candidates(
        ledger["shadow_comparisons"],
        envelope=envelope,
        min_consecutive_agreements=args.min_consecutive_agreements,
    )
    return {
        "shadow_comparison_count": ledger["shadow_comparison_count"],
        "task_count_scanned": ledger["task_count_scanned"],
        "min_consecutive_agreements": args.min_consecutive_agreements,
        "current_requires_human_approval_for": list(
            envelope["requires_human_approval_for"]
        ),
        "promotion_proposals": [
            proposal.model_dump(mode="json") for proposal in proposals
        ],
    }


def _apply(args: argparse.Namespace) -> dict:
    store = _task_store(args.db)
    ledger = collect_recovery_shadow_comparisons(
        task_store=store,
        task_scan_limit=args.task_scan_limit,
    )
    envelope = _build_autonomy_envelope(
        proposal_id="turtlebot3_recovery_promotion_cli",
        operator_approved=True,
        operator_approval_ref=args.operator_approval_ref,
    )
    proposals = evaluate_recovery_action_promotion_candidates(
        ledger["shadow_comparisons"],
        envelope=envelope,
        min_consecutive_agreements=args.min_consecutive_agreements,
    )
    matching = [
        proposal for proposal in proposals if proposal.action == args.action
    ]
    if not matching:
        raise RecoveryActionPromotionError(
            f"no promotion proposal for action {args.action!r}: the trailing "
            "agreement streak has not reached "
            f"{args.min_consecutive_agreements} "
            f"(evidence: {ledger['shadow_comparison_count']} shadow "
            "comparisons scanned)"
        )
    proposal = matching[0]
    new_envelope, application = apply_recovery_action_promotion(
        envelope=envelope,
        proposal=proposal,
        operator_approval_ref=args.operator_approval_ref,
        approval_actor=args.approval_actor,
    )

    applications_path = Path(args.applications_path)
    existing: list = []
    if applications_path.exists():
        loaded = json.loads(applications_path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            existing = loaded
    application_payload = application.model_dump(mode="json")
    existing.append(application_payload)
    applications_path.parent.mkdir(parents=True, exist_ok=True)
    applications_path.write_text(
        json.dumps(existing, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    audit_task = store.create(
        kind="recovery_promotion",
        title=f"Promote recovery action {proposal.action} to preapproved",
        status="completed",
        artifacts={
            "recovery_action_promotion_application": application_payload,
            "recovery_action_promotion_proposal": proposal.model_dump(
                mode="json"
            ),
        },
    )
    return {
        "application": application_payload,
        "applications_path": str(applications_path),
        "audit_task_id": audit_task["task_id"],
        "new_envelope": new_envelope.model_dump(mode="json"),
        "activation_hint": (
            "Point MISSIONOS_TURTLEBOT3_PROMOTED_ACTIONS_JSON at "
            f"{applications_path} so future missions build their envelope "
            "with this promotion applied."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        default=None,
        help="Task store SQLite path (defaults to the configured store).",
    )
    common.add_argument(
        "--task-scan-limit",
        type=int,
        default=DEFAULT_TASK_SCAN_LIMIT,
        help="Maximum number of tasks to scan for shadow comparisons.",
    )
    common.add_argument(
        "--min-consecutive-agreements",
        type=int,
        default=DEFAULT_MIN_CONSECUTIVE_AGREEMENTS,
        help="Trailing agreement streak required to propose promotion.",
    )

    subparsers.add_parser(
        "evaluate",
        parents=[common],
        help="Print promotion proposals earned by accumulated shadow evidence.",
    )

    apply_parser = subparsers.add_parser(
        "apply",
        parents=[common],
        help="Apply one proposed promotion with explicit operator approval.",
    )
    apply_parser.add_argument("--action", required=True)
    apply_parser.add_argument("--operator-approval-ref", required=True)
    apply_parser.add_argument(
        "--approval-actor", default="missionos_operator"
    )
    apply_parser.add_argument(
        "--applications-path",
        default="output/turtlebot3_recovery_promotions.json",
        help="JSON file the applied promotion is appended to.",
    )

    args = parser.parse_args()
    try:
        result = _evaluate(args) if args.command == "evaluate" else _apply(args)
    except RecoveryActionPromotionError as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
