"""Mission autonomy proposal envelope.

The envelope keeps AI recovery judgment separate from execution authority.
Mission-level LLM proposals are always recordable; deterministic policy only
classifies whether a recorded proposal may execute automatically, needs a fresh
human approval, or is blocked but still reportable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MISSION_AUTONOMY_ENVELOPE_SCHEMA = "missionos_mission_autonomy_envelope.v1"
MISSION_AUTONOMY_RECOVERY_PROPOSAL_SCHEMA = (
    "missionos_mission_autonomy_recovery_proposal.v1"
)
MISSION_AUTONOMY_PROPOSAL_CLASSIFICATION_SCHEMA = (
    "missionos_mission_autonomy_proposal_classification.v1"
)

MissionAutonomyRecoveryAction = Literal[
    "continue",
    "return_home",
    "hold",
    "avoid_obstacle",
    "reroute",
    "safe_stop",
    "ask_human",
    "raw_velocity",
    "unbounded_move",
    "physical_execution",
    "payload_delivery_completion",
]
MissionAutonomyProposalSource = Literal[
    "llm",
    "deterministic_fallback",
    "operator",
    "emergency_harness",
]
MissionAutonomyExecutionClass = Literal[
    "auto_executable",
    "requires_human_approval",
    "blocked_but_reportable",
    "emergency_harness",
]

# Actions that fail toward safety: a wrong one costs mission time, not the
# robot. Authority widening (issue #31) treats these asymmetrically — they can
# be delegated on weaker evidence than progressive actions.
CONSERVATIVE_RECOVERY_ACTIONS: frozenset[str] = frozenset(
    {"hold", "safe_stop", "return_home", "ask_human"}
)
_DEFAULT_CONSERVATIVE_ACTIONS: tuple[MissionAutonomyRecoveryAction, ...] = (
    "ask_human",
    "hold",
    "return_home",
    "safe_stop",
)


class MissionAutonomyEnvelope(BaseModel):
    """Execution-authority envelope for mission-level recovery proposals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["missionos_mission_autonomy_envelope.v1"] = (
        MISSION_AUTONOMY_ENVELOPE_SCHEMA
    )
    envelope_id: str
    mission_ref: str
    operator_approved: bool
    operator_approval_ref: str | None = None
    llm_recovery_proposals_allowed: Literal[True] = True
    proposal_first_classification: Literal[True] = True
    preapproved_recovery_actions: tuple[MissionAutonomyRecoveryAction, ...] = (
        "return_home",
        "hold",
    )
    requires_human_approval_for: tuple[MissionAutonomyRecoveryAction, ...] = (
        "avoid_obstacle",
        "reroute",
        "safe_stop",
        "ask_human",
    )
    blocked_actions: tuple[MissionAutonomyRecoveryAction, ...] = (
        "raw_velocity",
        "unbounded_move",
        "physical_execution",
        "payload_delivery_completion",
    )
    conservative_recovery_actions: tuple[MissionAutonomyRecoveryAction, ...] = (
        _DEFAULT_CONSERVATIVE_ACTIONS
    )
    # Application ids of operator-approved recovery-action promotions
    # (recovery_action_promotion.py) that shaped this envelope's action sets;
    # audit refs only, never authority by themselves.
    applied_recovery_promotions: tuple[str, ...] = ()
    battery_policy: dict[str, Any] = Field(default_factory=dict)
    emergency_harness: dict[str, Any] = Field(default_factory=dict)
    claim_boundary: str = (
        "Policy classifies execution authority for recorded proposals; it must "
        "not prevent LLM recovery proposals from being recorded."
    )
    dispatch_authority_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    progress_counted: Literal[False] = False

    @model_validator(mode="after")
    def _validate_operator_ref(self) -> "MissionAutonomyEnvelope":
        if self.operator_approved and not (self.operator_approval_ref or "").strip():
            raise ValueError("approved autonomy envelope requires operator_approval_ref")
        return self


class MissionAutonomyRecoveryProposal(BaseModel):
    """Recorded AI/operator recovery proposal; not approval or dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["missionos_mission_autonomy_recovery_proposal.v1"] = (
        MISSION_AUTONOMY_RECOVERY_PROPOSAL_SCHEMA
    )
    proposal_id: str
    mission_ref: str
    proposal_source: MissionAutonomyProposalSource
    selected_action: MissionAutonomyRecoveryAction
    reason: str
    input_observations: dict[str, Any] = Field(default_factory=dict)
    llm_invocation_evidence: dict[str, Any] = Field(default_factory=dict)
    llm_judgment_recorded: bool
    proposal_allowed_to_be_recorded: Literal[True] = True
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    progress_counted: Literal[False] = False

    @model_validator(mode="after")
    def _validate_llm_flag(self) -> "MissionAutonomyRecoveryProposal":
        expected = self.proposal_source == "llm"
        if self.llm_judgment_recorded is not expected:
            raise ValueError("llm_judgment_recorded must match proposal_source=llm")
        return self


class MissionAutonomyProposalClassification(BaseModel):
    """Policy classification for a recorded recovery proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["missionos_mission_autonomy_proposal_classification.v1"] = (
        MISSION_AUTONOMY_PROPOSAL_CLASSIFICATION_SCHEMA
    )
    classification_id: str
    envelope_ref: str
    proposal_ref: str
    selected_action: MissionAutonomyRecoveryAction
    proposal_recorded: Literal[True] = True
    proposal_allowed: Literal[True] = True
    execution_class: MissionAutonomyExecutionClass
    execution_permitted_by_envelope: bool
    requires_new_human_approval: bool
    selected_action_is_conservative: bool = False
    blocked_reasons: tuple[str, ...] = ()
    classification_reason: str
    proposal_first_classification: Literal[True] = True
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    progress_counted: Literal[False] = False

    @model_validator(mode="after")
    def _validate_classification_bounds(self) -> "MissionAutonomyProposalClassification":
        if self.execution_class == "auto_executable":
            if not self.execution_permitted_by_envelope:
                raise ValueError("auto_executable classification must permit execution")
            if self.requires_new_human_approval:
                raise ValueError("auto_executable must not require new human approval")
            if self.blocked_reasons:
                raise ValueError("auto_executable must not carry blocked_reasons")
        if self.execution_class == "blocked_but_reportable":
            if self.execution_permitted_by_envelope:
                raise ValueError("blocked_but_reportable cannot permit execution")
            if not self.blocked_reasons:
                raise ValueError("blocked_but_reportable requires blocked_reasons")
        if self.execution_class == "requires_human_approval":
            if self.execution_permitted_by_envelope:
                raise ValueError("requires_human_approval cannot permit execution")
            if not self.requires_new_human_approval:
                raise ValueError("requires_human_approval must require approval")
        return self


def _stable_id(prefix: str, payload: Sequence[Any]) -> str:
    raw = "\n".join(str(item) for item in payload)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:12]}"


def build_mission_autonomy_envelope(
    *,
    mission_ref: str,
    operator_approved: bool = False,
    operator_approval_ref: str | None = None,
    battery_policy: Mapping[str, Any] | None = None,
    emergency_harness: Mapping[str, Any] | None = None,
    preapproved_recovery_actions: Sequence[MissionAutonomyRecoveryAction] = (
        "return_home",
        "hold",
    ),
    requires_human_approval_for: Sequence[MissionAutonomyRecoveryAction] = (
        "avoid_obstacle",
        "reroute",
        "safe_stop",
        "ask_human",
    ),
    blocked_actions: Sequence[MissionAutonomyRecoveryAction] = (
        "raw_velocity",
        "unbounded_move",
        "physical_execution",
        "payload_delivery_completion",
    ),
    conservative_recovery_actions: Sequence[MissionAutonomyRecoveryAction] = (
        _DEFAULT_CONSERVATIVE_ACTIONS
    ),
    applied_recovery_promotions: Sequence[str] = (),
) -> MissionAutonomyEnvelope:
    """Build an autonomy envelope that classifies execution, not proposals."""

    envelope_id = _stable_id(
        "mission_autonomy_envelope",
        (
            mission_ref,
            operator_approved,
            operator_approval_ref or "",
            tuple(preapproved_recovery_actions),
            tuple(requires_human_approval_for),
            tuple(blocked_actions),
            tuple(conservative_recovery_actions),
            tuple(applied_recovery_promotions),
        ),
    )
    return MissionAutonomyEnvelope(
        envelope_id=envelope_id,
        mission_ref=mission_ref,
        operator_approved=operator_approved,
        operator_approval_ref=operator_approval_ref,
        preapproved_recovery_actions=tuple(dict.fromkeys(preapproved_recovery_actions)),
        requires_human_approval_for=tuple(dict.fromkeys(requires_human_approval_for)),
        blocked_actions=tuple(dict.fromkeys(blocked_actions)),
        conservative_recovery_actions=tuple(
            dict.fromkeys(conservative_recovery_actions)
        ),
        applied_recovery_promotions=tuple(
            dict.fromkeys(applied_recovery_promotions)
        ),
        battery_policy=dict(battery_policy or {}),
        emergency_harness=dict(emergency_harness or {}),
    )


def approve_mission_autonomy_envelope(
    envelope: Mapping[str, Any],
    *,
    operator_approval_ref: str,
) -> MissionAutonomyEnvelope:
    """Return an approved copy of an existing autonomy envelope."""

    return build_mission_autonomy_envelope(
        mission_ref=str(envelope.get("mission_ref") or ""),
        operator_approved=True,
        operator_approval_ref=operator_approval_ref,
        battery_policy=dict(envelope.get("battery_policy") or {}),
        emergency_harness=dict(envelope.get("emergency_harness") or {}),
        preapproved_recovery_actions=tuple(
            envelope.get("preapproved_recovery_actions") or ()
        ),
        requires_human_approval_for=tuple(
            envelope.get("requires_human_approval_for") or ()
        ),
        blocked_actions=tuple(envelope.get("blocked_actions") or ()),
        conservative_recovery_actions=tuple(
            envelope.get("conservative_recovery_actions")
            or _DEFAULT_CONSERVATIVE_ACTIONS
        ),
        applied_recovery_promotions=tuple(
            envelope.get("applied_recovery_promotions") or ()
        ),
    )


def build_mission_autonomy_recovery_proposal(
    *,
    mission_ref: str,
    proposal_source: MissionAutonomyProposalSource,
    selected_action: MissionAutonomyRecoveryAction,
    reason: str,
    input_observations: Mapping[str, Any] | None = None,
    llm_invocation_evidence: Mapping[str, Any] | None = None,
) -> MissionAutonomyRecoveryProposal:
    """Record a recovery proposal without creating execution authority."""

    proposal_id = _stable_id(
        "mission_autonomy_recovery_proposal",
        (mission_ref, proposal_source, selected_action, reason, dict(input_observations or {})),
    )
    return MissionAutonomyRecoveryProposal(
        proposal_id=proposal_id,
        mission_ref=mission_ref,
        proposal_source=proposal_source,
        selected_action=selected_action,
        reason=reason,
        input_observations=dict(input_observations or {}),
        llm_invocation_evidence=dict(llm_invocation_evidence or {}),
        llm_judgment_recorded=proposal_source == "llm",
    )


def classify_mission_autonomy_recovery_proposal(
    *,
    envelope: Mapping[str, Any] | MissionAutonomyEnvelope,
    proposal: Mapping[str, Any] | MissionAutonomyRecoveryProposal,
) -> MissionAutonomyProposalClassification:
    """Classify whether a recorded proposal may execute under the envelope."""

    envelope_model = (
        envelope
        if isinstance(envelope, MissionAutonomyEnvelope)
        else MissionAutonomyEnvelope.model_validate(dict(envelope))
    )
    proposal_model = (
        proposal
        if isinstance(proposal, MissionAutonomyRecoveryProposal)
        else MissionAutonomyRecoveryProposal.model_validate(dict(proposal))
    )
    action = proposal_model.selected_action
    blocked_reasons: list[str] = []
    if action in envelope_model.blocked_actions:
        blocked_reasons.append("action_blocked_by_autonomy_envelope")
        execution_class: MissionAutonomyExecutionClass = "blocked_but_reportable"
        permitted = False
        requires_approval = False
        reason = (
            "Proposal was recorded, but this action is outside the autonomy "
            "envelope and may only be reported."
        )
    elif not envelope_model.operator_approved:
        blocked_reasons.append("autonomy_envelope_not_operator_approved")
        execution_class = "requires_human_approval"
        permitted = False
        requires_approval = True
        reason = "Proposal was recorded, but the autonomy envelope is not approved."
    elif action in envelope_model.requires_human_approval_for:
        blocked_reasons.append("action_requires_new_human_approval")
        execution_class = "requires_human_approval"
        permitted = False
        requires_approval = True
        reason = "Proposal was recorded and requires a fresh human approval."
    elif action in envelope_model.preapproved_recovery_actions:
        execution_class = "auto_executable"
        permitted = True
        requires_approval = False
        reason = "Proposal is within the approved autonomy envelope."
    else:
        blocked_reasons.append("action_not_preapproved_by_autonomy_envelope")
        execution_class = "requires_human_approval"
        permitted = False
        requires_approval = True
        reason = "Proposal was recorded but is not preapproved by the envelope."

    classification_id = _stable_id(
        "mission_autonomy_proposal_classification",
        (
            envelope_model.envelope_id,
            proposal_model.proposal_id,
            action,
            execution_class,
            tuple(blocked_reasons),
        ),
    )
    return MissionAutonomyProposalClassification(
        classification_id=classification_id,
        envelope_ref=envelope_model.envelope_id,
        proposal_ref=proposal_model.proposal_id,
        selected_action=action,
        execution_class=execution_class,
        execution_permitted_by_envelope=permitted,
        requires_new_human_approval=requires_approval,
        selected_action_is_conservative=(
            action in envelope_model.conservative_recovery_actions
        ),
        blocked_reasons=tuple(blocked_reasons),
        classification_reason=reason,
    )


__all__ = [
    "CONSERVATIVE_RECOVERY_ACTIONS",
    "MISSION_AUTONOMY_ENVELOPE_SCHEMA",
    "MISSION_AUTONOMY_PROPOSAL_CLASSIFICATION_SCHEMA",
    "MISSION_AUTONOMY_RECOVERY_PROPOSAL_SCHEMA",
    "MissionAutonomyEnvelope",
    "MissionAutonomyExecutionClass",
    "MissionAutonomyProposalClassification",
    "MissionAutonomyProposalSource",
    "MissionAutonomyRecoveryAction",
    "MissionAutonomyRecoveryProposal",
    "approve_mission_autonomy_envelope",
    "build_mission_autonomy_envelope",
    "build_mission_autonomy_recovery_proposal",
    "classify_mission_autonomy_recovery_proposal",
]
