"""Bounded post-episode repair proposals for governed VLA simulator tasks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from missionos_core import canonical_sha256

from src.runtime.physical_ai_mission_catalog import (
    VLA_ONLY_MISSION_KIND,
    validate_physical_ai_approval,
    validate_physical_ai_proposal,
)


VLA_POST_EPISODE_REPAIR_SCHEMA_VERSION = (
    "missionos_vla_post_episode_repair_proposal.v1"
)
VLA_POST_EPISODE_REPAIR_APPROVAL_SCHEMA_VERSION = (
    "missionos_vla_post_episode_repair_approval.v1"
)
VLA_POST_EPISODE_REPAIR_ACTION = "retry_same_frozen_task"
VLA_POST_EPISODE_MAXIMUM_RETRY_ATTEMPTS = 1


def _proposal_material(proposal: Mapping[str, Any]) -> dict[str, Any]:
    lifecycle_fields = {
        "proposal_sha256",
        "proposal_status",
        "repair_approval_id",
        "repair_approval_sha256",
        "retry_task_id",
        "consumed_at",
    }
    return {
        str(key): value
        for key, value in proposal.items()
        if key not in lifecycle_fields
    }


def vla_post_episode_repair_proposal_sha256(
    proposal: Mapping[str, Any],
) -> str:
    return canonical_sha256(_proposal_material(proposal))


def _repair_attempt_index(task: Mapping[str, Any]) -> int:
    metadata = task.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    value = metadata.get("vla_post_episode_repair_attempt_index", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("vla_post_episode_repair_attempt_index_invalid")
    return value


def build_vla_post_episode_repair_proposal(
    *,
    source_task: Mapping[str, Any],
    source_proposal: Mapping[str, Any],
    source_approval: Mapping[str, Any],
    failure_evidence: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return one proposal-only retry, or ``None`` after the retry limit."""

    if source_task.get("kind") != "vla_mission_execution":
        raise ValueError("vla_post_episode_repair_source_kind_invalid")
    if source_task.get("status") != "failed":
        raise ValueError("vla_post_episode_repair_source_status_invalid")
    if source_proposal.get("mission_kind") != VLA_ONLY_MISSION_KIND:
        raise ValueError("vla_post_episode_repair_source_proposal_kind_invalid")
    proposal_reasons = validate_physical_ai_proposal(source_proposal)
    if proposal_reasons:
        raise ValueError(
            "vla_post_episode_repair_source_proposal_invalid:"
            + ",".join(proposal_reasons)
        )
    approval_reasons = validate_physical_ai_approval(
        proposal=source_proposal,
        approval=source_approval,
    )
    if approval_reasons:
        raise ValueError(
            "vla_post_episode_repair_source_approval_invalid:"
            + ",".join(approval_reasons)
        )
    attempt_index = _repair_attempt_index(source_task)
    if attempt_index >= VLA_POST_EPISODE_MAXIMUM_RETRY_ATTEMPTS:
        return None

    source_task_id = str(source_task.get("task_id") or "").strip()
    source_proposal_sha256 = str(source_proposal.get("proposal_sha256") or "")
    source_approval_sha256 = str(source_approval.get("approval_sha256") or "")
    if not source_task_id or not source_proposal_sha256 or not source_approval_sha256:
        raise ValueError("vla_post_episode_repair_source_binding_missing")
    failure_evidence_sha256 = canonical_sha256(dict(failure_evidence))
    proposed_at = (now or datetime.now(timezone.utc)).isoformat()
    next_attempt_index = attempt_index + 1
    base = {
        "schema_version": VLA_POST_EPISODE_REPAIR_SCHEMA_VERSION,
        "proposal_id": f"vla-post-episode-repair:{uuid4()}",
        "proposal_status": "awaiting_operator_approval",
        "repair_action": VLA_POST_EPISODE_REPAIR_ACTION,
        "proposal_source": "bounded_vla_post_episode_repair_planner",
        "planner_invoked": True,
        "model_inference_invoked": False,
        "source_task_id": source_task_id,
        "source_proposal_sha256": source_proposal_sha256,
        "source_approval_sha256": source_approval_sha256,
        "source_failure_evidence_sha256": failure_evidence_sha256,
        "source_catalog_entry_id": str(
            (source_proposal.get("vla_task_selection") or {}).get(
                "catalog_entry_id"
            )
        ),
        "source_catalog_entry_sha256": str(
            (source_proposal.get("vla_task_selection") or {}).get(
                "content_sha256"
            )
        ),
        "attempt_index": next_attempt_index,
        "maximum_retry_attempts": VLA_POST_EPISODE_MAXIMUM_RETRY_ATTEMPTS,
        "proposed_at": proposed_at,
        "requires_new_human_approval": True,
        "new_run_identity_required": True,
        "new_episode_identity_required": True,
        "new_contract_required": True,
        "automatic_retry_allowed": False,
        "approval_created": False,
        "dispatch_authority_created": False,
        "runtime_effect_requested": False,
        "physical_execution_invoked": False,
        "safe_stop_effect_claimed": False,
        "claim_boundary": (
            "This proposal may authorize at most one new simulator episode after "
            "separate human approval. It cannot intervene in the completed episode, "
            "change the frozen task, or claim safe-stop or physical execution."
        ),
    }
    proposal = dict(base)
    proposal["proposal_sha256"] = vla_post_episode_repair_proposal_sha256(
        proposal
    )
    return proposal


def validate_vla_post_episode_repair_proposal(
    proposal: Mapping[str, Any],
    *,
    source_task: Mapping[str, Any],
    source_proposal: Mapping[str, Any],
    source_approval: Mapping[str, Any],
    failure_evidence: Mapping[str, Any],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if proposal.get("schema_version") != VLA_POST_EPISODE_REPAIR_SCHEMA_VERSION:
        reasons.append("vla_post_episode_repair_schema_not_supported")
    if proposal.get("proposal_sha256") != vla_post_episode_repair_proposal_sha256(
        proposal
    ):
        reasons.append("vla_post_episode_repair_digest_mismatch")
    if proposal.get("proposal_status") != "awaiting_operator_approval":
        reasons.append("vla_post_episode_repair_not_awaiting_approval")
    if proposal.get("repair_action") != VLA_POST_EPISODE_REPAIR_ACTION:
        reasons.append("vla_post_episode_repair_action_not_approved")
    if proposal.get("source_task_id") != source_task.get("task_id"):
        reasons.append("vla_post_episode_repair_source_task_mismatch")
    if proposal.get("source_proposal_sha256") != source_proposal.get(
        "proposal_sha256"
    ):
        reasons.append("vla_post_episode_repair_source_proposal_mismatch")
    if proposal.get("source_approval_sha256") != source_approval.get(
        "approval_sha256"
    ):
        reasons.append("vla_post_episode_repair_source_approval_mismatch")
    if proposal.get("source_failure_evidence_sha256") != canonical_sha256(
        dict(failure_evidence)
    ):
        reasons.append("vla_post_episode_repair_failure_evidence_mismatch")
    task_selection = source_proposal.get("vla_task_selection")
    task_selection = task_selection if isinstance(task_selection, Mapping) else {}
    if proposal.get("source_catalog_entry_id") != task_selection.get(
        "catalog_entry_id"
    ):
        reasons.append("vla_post_episode_repair_catalog_entry_mismatch")
    if proposal.get("source_catalog_entry_sha256") != task_selection.get(
        "content_sha256"
    ):
        reasons.append("vla_post_episode_repair_catalog_digest_mismatch")
    try:
        expected_attempt = _repair_attempt_index(source_task) + 1
    except ValueError:
        reasons.append("vla_post_episode_repair_attempt_index_invalid")
    else:
        if proposal.get("attempt_index") != expected_attempt:
            reasons.append("vla_post_episode_repair_attempt_index_mismatch")
    if proposal.get("maximum_retry_attempts") != (
        VLA_POST_EPISODE_MAXIMUM_RETRY_ATTEMPTS
    ):
        reasons.append("vla_post_episode_repair_limit_mismatch")
    for field in (
        "automatic_retry_allowed",
        "approval_created",
        "dispatch_authority_created",
        "runtime_effect_requested",
        "physical_execution_invoked",
        "safe_stop_effect_claimed",
    ):
        if proposal.get(field) is not False:
            reasons.append(f"vla_post_episode_repair_{field}_forbidden")
    for field in (
        "requires_new_human_approval",
        "new_run_identity_required",
        "new_episode_identity_required",
        "new_contract_required",
    ):
        if proposal.get(field) is not True:
            reasons.append(f"vla_post_episode_repair_{field}_required")
    return tuple(dict.fromkeys(reasons))


def build_vla_post_episode_repair_approval(
    *,
    repair_proposal: Mapping[str, Any],
    operator_approval_ref: str,
    approved_at: datetime | None = None,
) -> dict[str, Any]:
    operator_ref = str(operator_approval_ref or "").strip()
    if not operator_ref:
        raise ValueError("vla_post_episode_repair_operator_approval_ref_missing")
    base = {
        "schema_version": VLA_POST_EPISODE_REPAIR_APPROVAL_SCHEMA_VERSION,
        "approval_id": f"vla-post-episode-repair-approval:{uuid4()}",
        "approved_at": (approved_at or datetime.now(timezone.utc)).isoformat(),
        "operator_approval_ref": operator_ref,
        "repair_proposal_id": repair_proposal.get("proposal_id"),
        "repair_proposal_sha256": repair_proposal.get("proposal_sha256"),
        "source_task_id": repair_proposal.get("source_task_id"),
        "repair_action": repair_proposal.get("repair_action"),
        "human_operator_approval_recorded": True,
        "automatic_retry_allowed": False,
        "physical_execution_invoked": False,
    }
    return {**base, "approval_sha256": canonical_sha256(base)}


def validate_vla_post_episode_repair_approval(
    approval: Mapping[str, Any],
    *,
    repair_proposal: Mapping[str, Any],
) -> tuple[str, ...]:
    reasons: list[str] = []
    material = {
        str(key): value
        for key, value in approval.items()
        if key != "approval_sha256"
    }
    if approval.get("schema_version") != (
        VLA_POST_EPISODE_REPAIR_APPROVAL_SCHEMA_VERSION
    ):
        reasons.append("vla_post_episode_repair_approval_schema_not_supported")
    if approval.get("approval_sha256") != canonical_sha256(material):
        reasons.append("vla_post_episode_repair_approval_digest_mismatch")
    for field in ("repair_proposal_id", "repair_proposal_sha256", "source_task_id"):
        expected_field = {
            "repair_proposal_id": "proposal_id",
            "repair_proposal_sha256": "proposal_sha256",
            "source_task_id": "source_task_id",
        }[field]
        if approval.get(field) != repair_proposal.get(expected_field):
            reasons.append(f"vla_post_episode_repair_approval_{field}_mismatch")
    if approval.get("repair_action") != VLA_POST_EPISODE_REPAIR_ACTION:
        reasons.append("vla_post_episode_repair_approval_action_mismatch")
    if approval.get("human_operator_approval_recorded") is not True:
        reasons.append("vla_post_episode_repair_human_approval_missing")
    if approval.get("automatic_retry_allowed") is not False:
        reasons.append("vla_post_episode_repair_automatic_retry_forbidden")
    if approval.get("physical_execution_invoked") is not False:
        reasons.append("vla_post_episode_repair_physical_execution_forbidden")
    return tuple(dict.fromkeys(reasons))


__all__ = [
    "VLA_POST_EPISODE_MAXIMUM_RETRY_ATTEMPTS",
    "VLA_POST_EPISODE_REPAIR_ACTION",
    "build_vla_post_episode_repair_approval",
    "build_vla_post_episode_repair_proposal",
    "validate_vla_post_episode_repair_proposal",
    "validate_vla_post_episode_repair_approval",
    "vla_post_episode_repair_proposal_sha256",
]
