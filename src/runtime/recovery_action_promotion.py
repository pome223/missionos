"""Promotion evidence for widening recovery-action autonomy (issue #31).

Accumulated ``shadow_comparison`` records (the LLM proposal vs. the
deterministic fallback candidate, recorded on every TB3 recovery event by
``_recovery_shadow_comparison`` in turtlebot3_home_mission.py) are the only
per-event agreement signal this codebase currently captures — there is no
separate per-event human-approval log. Agreement with the deterministic
floor is therefore the evidence basis here, not literal recorded human
approval; the floor itself encodes vetted, human-designed policy, so
consistent LLM agreement with it is treated as a legitimate (if narrower)
proxy for "the LLM isn't proposing anything a human reviewer would object
to for this trigger."

Evaluating evidence (``evaluate_recovery_action_promotion_candidates``) never
mutates anything or grants execution authority — it only emits a proposal
artifact. Applying a proposal to a live ``MissionAutonomyEnvelope``
(``apply_recovery_action_promotion``) is a separate, explicit function that
requires the envelope to already be operator-approved and a non-empty
``operator_approval_ref`` for the promotion itself; nothing in this module
applies a proposal without that explicit approval.

Not to be confused with ``approved_promotions.py``'s ``policy_patch.v1``,
which is a benchmark/eval-gated artifact for a different promotion pipeline
(verifier/skill improvements evaluated against eval suites). This module is
purpose-built for autonomy-envelope action promotion from shadow-comparison
evidence and does not require or produce eval-suite results.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.runtime.mission_autonomy_envelope import (
    MissionAutonomyEnvelope,
    build_mission_autonomy_envelope,
)

RECOVERY_ACTION_PROMOTION_PROPOSAL_SCHEMA_VERSION = (
    "missionos_recovery_action_promotion_proposal.v1"
)
RECOVERY_ACTION_PROMOTION_APPLICATION_SCHEMA_VERSION = (
    "missionos_recovery_action_promotion_application.v1"
)

DEFAULT_MIN_CONSECUTIVE_AGREEMENTS = 5


class RecoveryActionPromotionError(ValueError):
    """Raised when a promotion proposal cannot be applied to an envelope."""


class RecoveryActionPromotionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[RECOVERY_ACTION_PROMOTION_PROPOSAL_SCHEMA_VERSION] = (
        RECOVERY_ACTION_PROMOTION_PROPOSAL_SCHEMA_VERSION
    )
    proposal_id: str
    action: str
    current_execution_class: Literal["requires_human_approval"] = (
        "requires_human_approval"
    )
    proposed_execution_class: Literal["preapproved"] = "preapproved"
    evidence_basis: Literal["deterministic_floor_agreement"] = (
        "deterministic_floor_agreement"
    )
    consecutive_agreement_count: int
    min_consecutive_agreements_required: int
    evaluated_at: datetime
    claim_boundary: str = (
        "This proposal is evidence only. It does not modify the live "
        "autonomy envelope, grant execution authority, or replace human "
        "review. Agreement is measured against the deterministic fallback "
        "floor, not a per-event recorded human approval."
    )
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    envelope_mutation_applied: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecoveryActionPromotionApplication(BaseModel):
    """Record of an operator applying a promotion proposal to an envelope.

    This is the only place in this module that ever mutates a live
    ``MissionAutonomyEnvelope`` — and it only does so given an explicit,
    non-empty ``operator_approval_ref``. The evidence (proposal_id,
    consecutive_agreement_count) is carried into the record so the audit
    trail shows exactly what agreement history justified the operator's
    approval, without re-deriving it from raw shadow_comparisons later.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[RECOVERY_ACTION_PROMOTION_APPLICATION_SCHEMA_VERSION] = (
        RECOVERY_ACTION_PROMOTION_APPLICATION_SCHEMA_VERSION
    )
    application_id: str
    proposal_ref: str
    action: str
    consecutive_agreement_count: int
    operator_approval_ref: str = Field(min_length=1)
    approval_actor: str
    approved_at: datetime
    previous_envelope_ref: str
    new_envelope_ref: str
    claim_boundary: str = (
        "This record documents an explicit operator approval that promoted "
        "one action from requires_human_approval to preapproved. It does "
        "not itself grant dispatch authority or physical execution — those "
        "remain governed by the resulting envelope's normal classification."
    )
    dispatch_authority_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_operator_approval_ref(
        self,
    ) -> "RecoveryActionPromotionApplication":
        if not self.operator_approval_ref.strip():
            raise RecoveryActionPromotionError(
                "promotion application requires a non-empty operator_approval_ref"
            )
        return self


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_id(payload: Mapping[str, Any]) -> str:
    digest = sha256(
        json.dumps(dict(payload), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"recovery_action_promotion_proposal_{digest[:16]}"


def _trailing_agreement_streak(comparisons: Sequence[Mapping[str, Any]]) -> int:
    """Count consecutive agreements from the most recent entry backward.

    A single disagreement or an entry with no LLM proposal available
    (``agreement`` is ``None`` on fallback) breaks the streak — "N回連続"
    means the *most recent* N proposals, not N somewhere in the history.
    """

    streak = 0
    for entry in reversed(comparisons):
        if entry.get("agreement") is not True:
            break
        streak += 1
    return streak


def evaluate_recovery_action_promotion_candidates(
    shadow_comparisons: Sequence[Mapping[str, Any]],
    *,
    envelope: MissionAutonomyEnvelope | Mapping[str, Any],
    min_consecutive_agreements: int = DEFAULT_MIN_CONSECUTIVE_AGREEMENTS,
    now: datetime | None = None,
) -> tuple[RecoveryActionPromotionProposal, ...]:
    """Propose promoting actions with a long trailing agreement streak.

    ``shadow_comparisons`` must be in chronological order (oldest first).
    Only actions currently in the envelope's ``requires_human_approval_for``
    are considered — this evaluates evidence for narrowing that set, not for
    granting anything beyond what the envelope already permits.
    """

    if min_consecutive_agreements < 1:
        raise ValueError("min_consecutive_agreements must be at least 1")

    envelope_model = (
        envelope
        if isinstance(envelope, MissionAutonomyEnvelope)
        else MissionAutonomyEnvelope.model_validate(dict(envelope))
    )
    evaluated_at = _utc(now)
    proposals: list[RecoveryActionPromotionProposal] = []
    for action in envelope_model.requires_human_approval_for:
        action_comparisons = [
            dict(entry)
            for entry in shadow_comparisons
            if entry.get("deterministic_action") == action
        ]
        streak = _trailing_agreement_streak(action_comparisons)
        if streak < min_consecutive_agreements:
            continue
        proposal_id = _stable_id(
            {
                "envelope_id": envelope_model.envelope_id,
                "action": action,
                "consecutive_agreement_count": streak,
                "evaluated_at": evaluated_at.isoformat(),
            }
        )
        proposals.append(
            RecoveryActionPromotionProposal(
                proposal_id=proposal_id,
                action=action,
                consecutive_agreement_count=streak,
                min_consecutive_agreements_required=min_consecutive_agreements,
                evaluated_at=evaluated_at,
            )
        )
    return tuple(proposals)


def _application_id(payload: Mapping[str, Any]) -> str:
    digest = sha256(
        json.dumps(dict(payload), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"recovery_action_promotion_application_{digest[:16]}"


def apply_recovery_action_promotion(
    *,
    envelope: MissionAutonomyEnvelope | Mapping[str, Any],
    proposal: RecoveryActionPromotionProposal | Mapping[str, Any],
    operator_approval_ref: str,
    approval_actor: str = "missionos_operator",
    now: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[MissionAutonomyEnvelope, RecoveryActionPromotionApplication]:
    """Apply an operator-approved promotion proposal to an envelope.

    This is the only function in this module that mutates a live
    ``MissionAutonomyEnvelope`` — every other artifact here (the proposal,
    the shadow comparisons it's built from) is evidence only. Requires the
    envelope to already be operator-approved and the proposed action to
    still be in its ``requires_human_approval_for`` — a proposal evaluated
    against a since-changed envelope is refused rather than silently
    reinterpreted.
    """

    envelope_model = (
        envelope
        if isinstance(envelope, MissionAutonomyEnvelope)
        else MissionAutonomyEnvelope.model_validate(dict(envelope))
    )
    proposal_model = (
        proposal
        if isinstance(proposal, RecoveryActionPromotionProposal)
        else RecoveryActionPromotionProposal.model_validate(dict(proposal))
    )
    if not envelope_model.operator_approved:
        raise RecoveryActionPromotionError(
            "cannot apply a promotion to an envelope that is not itself "
            "operator-approved"
        )
    if proposal_model.action not in envelope_model.requires_human_approval_for:
        raise RecoveryActionPromotionError(
            f"proposal action {proposal_model.action!r} is not in this "
            "envelope's requires_human_approval_for; the envelope may have "
            "changed since the proposal was evaluated"
        )
    if not operator_approval_ref.strip():
        raise RecoveryActionPromotionError(
            "applying a promotion requires a non-empty operator_approval_ref"
        )

    new_preapproved = (
        *envelope_model.preapproved_recovery_actions,
        proposal_model.action,
    )
    new_requires_approval = tuple(
        action
        for action in envelope_model.requires_human_approval_for
        if action != proposal_model.action
    )
    approved_at = _utc(now)
    application_id = _application_id(
        {
            "proposal_id": proposal_model.proposal_id,
            "action": proposal_model.action,
            "operator_approval_ref": operator_approval_ref,
            "approval_actor": approval_actor,
            "previous_envelope_id": envelope_model.envelope_id,
            "approved_at": approved_at.isoformat(),
        }
    )
    new_envelope = build_mission_autonomy_envelope(
        mission_ref=envelope_model.mission_ref,
        operator_approved=True,
        operator_approval_ref=envelope_model.operator_approval_ref,
        battery_policy=envelope_model.battery_policy,
        emergency_harness=envelope_model.emergency_harness,
        preapproved_recovery_actions=new_preapproved,
        requires_human_approval_for=new_requires_approval,
        blocked_actions=envelope_model.blocked_actions,
        conservative_recovery_actions=envelope_model.conservative_recovery_actions,
        applied_recovery_promotions=(
            *envelope_model.applied_recovery_promotions,
            application_id,
        ),
    )
    application = RecoveryActionPromotionApplication(
        application_id=application_id,
        proposal_ref=proposal_model.proposal_id,
        action=proposal_model.action,
        consecutive_agreement_count=proposal_model.consecutive_agreement_count,
        operator_approval_ref=operator_approval_ref,
        approval_actor=approval_actor,
        approved_at=approved_at,
        previous_envelope_ref=envelope_model.envelope_id,
        new_envelope_ref=new_envelope.envelope_id,
        metadata=dict(metadata or {}),
    )
    return new_envelope, application


__all__ = [
    "DEFAULT_MIN_CONSECUTIVE_AGREEMENTS",
    "RECOVERY_ACTION_PROMOTION_APPLICATION_SCHEMA_VERSION",
    "RECOVERY_ACTION_PROMOTION_PROPOSAL_SCHEMA_VERSION",
    "RecoveryActionPromotionApplication",
    "RecoveryActionPromotionError",
    "RecoveryActionPromotionProposal",
    "apply_recovery_action_promotion",
    "evaluate_recovery_action_promotion_candidates",
]
