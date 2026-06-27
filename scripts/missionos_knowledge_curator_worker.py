#!/usr/bin/env python3
"""Bounded Knowledge Curator worker for MissionOS production publish."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid


ACTIVE_LESSON_INDEX_SCHEMA_VERSION = "missionos_active_lesson_index.v1"
PRODUCTION_CURATOR_SCHEMA_VERSION = "missionos_knowledge_curator_run.v1"


def _read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lesson-path", required=True)
    parser.add_argument("--lesson-artifact-path", required=True)
    parser.add_argument("--active-index-path", required=True)
    parser.add_argument("--curator-run-path", required=True)
    parser.add_argument("--operator-approval-ref", required=True)
    parser.add_argument("--operator-approval-artifact-path", required=True)
    parser.add_argument("--generated-at", default="")
    args = parser.parse_args()

    generated_at = args.generated_at or datetime.now(timezone.utc).isoformat()
    lesson_path = Path(args.lesson_path)
    lesson = _read_json(lesson_path)
    lesson_id = str(lesson.get("lesson_id") or "")
    if not lesson_id:
        raise ValueError("lesson_id_required")

    index_id = f"active_lesson_index_{uuid.uuid4().hex[:12]}"
    index = {
        "schema_version": ACTIVE_LESSON_INDEX_SCHEMA_VERSION,
        "index_id": index_id,
        "index_status": "updated",
        "causal_form": "Form 0b",
        "progress_counted": False,
        "generated_at": generated_at,
        "lesson_ref": f"cross_session_lesson:{lesson_id}",
        "lesson_artifact_path": args.lesson_artifact_path,
        "source_failure_mode_id": lesson.get("source_failure_mode_id"),
        "source_artifact_path": lesson.get("source_artifact_path"),
        "active_for_future_diagnostics": True,
        "knowledge_index_updated": False,
        "knowledge_index_recorded_by_worker": True,
        "operator_approval_ref": args.operator_approval_ref,
        "operator_approval_artifact_path": args.operator_approval_artifact_path,
        "policy_update_applied": False,
        "automatic_recovery_rule_created": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "public_sync_performed": False,
    }
    _write_json(Path(args.active_index_path), index)

    curator_run_id = f"knowledge_curator_run_{uuid.uuid4().hex[:12]}"
    curator = {
        "schema_version": PRODUCTION_CURATOR_SCHEMA_VERSION,
        "curator_run_id": curator_run_id,
        "curator_status": "completed",
        "causal_form": "Form 0b",
        "progress_counted": False,
        "generated_at": generated_at,
        "agent_role": "knowledge_curator",
        "operator_approved": False,
        "operator_approval_ref": args.operator_approval_ref,
        "operator_approval_artifact_path": args.operator_approval_artifact_path,
        "operator_approval_ref_consumed": False,
        "agent_execution_started": False,
        "agent_execution_recorded_by_worker": True,
        "dry_run_only": False,
        "dry_run_agent_execution_started": False,
        "no_background_automation": True,
        "background_work_scheduled": False,
        "source_failure_mode_id": lesson.get("source_failure_mode_id"),
        "source_artifact_path": lesson.get("source_artifact_path"),
        "cross_session_lesson_ref": f"cross_session_lesson:{lesson_id}",
        "cross_session_lesson_artifact_path": args.lesson_artifact_path,
        "active_lesson_index_ref": f"active_lesson_index:{index_id}",
        "active_lesson_index_artifact_path": str(args.active_index_path),
        "knowledge_index_updated": False,
        "knowledge_index_recorded_by_worker": True,
        "policy_update_applied": False,
        "automatic_recovery_rule_created": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "public_sync_performed": False,
    }
    _write_json(Path(args.curator_run_path), curator)

    print(
        json.dumps(
            {
                "worker_status": "completed",
                "active_lesson_index_id": index_id,
                "curator_run_id": curator_run_id,
                "active_index_path": str(args.active_index_path),
                "curator_run_path": str(args.curator_run_path),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
