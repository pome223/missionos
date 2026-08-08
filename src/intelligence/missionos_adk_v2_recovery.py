"""Create a bounded approval-pending recovery proposal after verifier failure."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import uuid

from src.gateway.missionos_knowledge_sharing import ARTIFACT_ROOT


MISSIONOS_ADK_V2_RECOVERY_ENV = "MISSIONOS_ADK_V2_RECOVERY_ENABLED"
MISSIONOS_ADK_V2_RECOVERY_PROPOSAL_SCHEMA_VERSION = (
    "missionos_adk_v2_recovery_proposal.v1"
)


def _sha256_json(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def create_bounded_recovery_proposal(
    verifier_result: Mapping[str, Any],
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Persist a new proposal and approval request without dispatching it."""

    guarded_value = verifier_result.get("guarded_execution")
    guarded = guarded_value if isinstance(guarded_value, Mapping) else {}
    if guarded.get("verifier_status") != "failed":
        return {
            "schema_version": MISSIONOS_ADK_V2_RECOVERY_PROPOSAL_SCHEMA_VERSION,
            "recovery_status": "not_required",
            "blocking_reasons": ["recovery_requires_explicit_verifier_failure"],
            "approval_request_created": False,
            "approval_created": False,
            "dispatch_authority_created": False,
            "executor_invoked": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        }
    task_id = str(verifier_result.get("task_id") or "")
    prior_approval_ref = str(verifier_result.get("approval_ref") or "")
    prior_bounded_action_ref = str(
        verifier_result.get("bounded_action_ref") or ""
    )
    prior_dispatch_ref = str(verifier_result.get("dispatch_ref") or "")
    failure_evidence = {
        "task_id": task_id,
        "prior_approval_ref": prior_approval_ref,
        "prior_bounded_action_ref": prior_bounded_action_ref,
        "prior_dispatch_ref": prior_dispatch_ref,
        "verifier_status": guarded.get("verifier_status"),
        "verifier_reasons": list(guarded.get("verifier_reasons") or []),
        "execution_receipt": guarded.get("execution_receipt") or {},
    }
    failure_sha256 = _sha256_json(failure_evidence)
    proposal_id = f"recovery_{uuid.uuid4().hex[:16]}"
    recovery_task_id = f"{task_id}:recovery:{proposal_id}"
    proposal_ref = f"missionos_recovery_proposal:{proposal_id}"
    approval_request_ref = f"missionos_recovery_approval_request:{proposal_id}"
    bounded_action_ref = f"missionos_recovery_bounded_action:{proposal_id}"
    dispatch_ref = f"missionos_recovery_pending_dispatch:{proposal_id}"
    root = Path(artifact_root)
    artifact_path = (
        root
        / f"missionos_adk_v2_recovery_{proposal_id}"
        / "missionos_adk_v2_recovery_proposal.json"
    )
    proposal = {
        "schema_version": MISSIONOS_ADK_V2_RECOVERY_PROPOSAL_SCHEMA_VERSION,
        "recovery_status": "approval_pending",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "recovery_task_id": recovery_task_id,
        "recovery_proposal_ref": proposal_ref,
        "recovery_proposal_artifact_path": artifact_path.as_posix(),
        "recovery_attempt_ordinal": 1,
        "maximum_recovery_attempts": 1,
        "planner_source": "deterministic_fail_closed_verifier_router",
        "proposed_action_kind": "hold_and_reconcile_failed_verifier",
        "proposal_only": True,
        "failure_evidence_sha256": failure_sha256,
        "prior_approval_ref": prior_approval_ref,
        "prior_approval_reusable": False,
        "prior_bounded_action_ref": prior_bounded_action_ref,
        "prior_dispatch_ref": prior_dispatch_ref,
        "approval_request_ref": approval_request_ref,
        "approval_request_created": True,
        "new_human_approval_required": True,
        "approval_created": False,
        "bounded_action_ref": bounded_action_ref,
        "dispatch_ref": dispatch_ref,
        "requires_fresh_telemetry": True,
        "requires_fresh_dispatch_preflight": True,
        "dispatch_authority_created": False,
        "executor_invoked": False,
        "external_sender_invoked": False,
        "automatic_recovery_executed": False,
        "automatic_redispatch_performed": False,
        "ack_observed": False,
        "outcome_observed": False,
        "verifier_passed": False,
        "completion_claimed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
        "blocking_reasons": [],
    }
    _write_json(artifact_path, proposal)
    return proposal


__all__ = [
    "MISSIONOS_ADK_V2_RECOVERY_ENV",
    "MISSIONOS_ADK_V2_RECOVERY_PROPOSAL_SCHEMA_VERSION",
    "create_bounded_recovery_proposal",
]
