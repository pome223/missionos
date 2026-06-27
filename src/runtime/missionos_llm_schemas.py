"""Schema guards for future MissionOS LLM intelligence artifacts.

These helpers define artifact contracts only. They do not call an LLM, do not
produce MissionOS intelligence, and do not make artifact existence count as
runtime or AI-agent progress.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


LLM_INVOCATION_EVIDENCE_SCHEMA_VERSION = "missionos_llm_invocation_evidence.v1"
LLM_SITUATION_ASSESSMENT_SCHEMA_VERSION = "missionos_llm_situation_assessment.v1"
LLM_RESPONSE_PROPOSAL_SCHEMA_VERSION = "missionos_llm_response_proposal.v1"
LLM_DIAGNOSTIC_NARRATIVE_SCHEMA_VERSION = "missionos_llm_diagnostic_narrative.v1"
LLM_REPAIR_PROPOSAL_SCHEMA_VERSION = "missionos_llm_repair_proposal.v1"
LLM_META_LESSON_SCHEMA_VERSION = "missionos_llm_meta_lesson.v1"
LLM_EXPERIMENT_PROPOSAL_SCHEMA_VERSION = "missionos_llm_experiment_proposal.v1"

MISSIONOS_LLM_ARTIFACT_SCHEMA_VERSIONS: tuple[str, ...] = (
    LLM_SITUATION_ASSESSMENT_SCHEMA_VERSION,
    LLM_RESPONSE_PROPOSAL_SCHEMA_VERSION,
    LLM_DIAGNOSTIC_NARRATIVE_SCHEMA_VERSION,
    LLM_REPAIR_PROPOSAL_SCHEMA_VERSION,
    LLM_META_LESSON_SCHEMA_VERSION,
    LLM_EXPERIMENT_PROPOSAL_SCHEMA_VERSION,
)

LLM_RESPONSE_KIND_WHITELIST = frozenset(
    {
        "operator_gated_wind_replan_with_compensation",
        "operator_gated_wind_compensated_reroute",
        "operator_gated_continue_with_wind_warning",
        "operator_gated_hold_and_reobserve",
        "operator_gated_abort_and_replan",
        "operator_gated_payload_recovery_land",
        "operator_gated_real_hardware_arm_disarm",
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MissionOSLLMSchemaValidationError(ValueError):
    """Raised when an LLM artifact violates the Phase 0c contract."""


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _require_bool_false(payload: Mapping[str, Any], key: str) -> None:
    if payload.get(key) is not False:
        raise MissionOSLLMSchemaValidationError(f"{key}_must_be_false")


def _require_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise MissionOSLLMSchemaValidationError(f"{key}_required")
    return value


def _require_sha256(payload: Mapping[str, Any], key: str) -> str:
    value = _require_string(payload, key)
    if not _SHA256_RE.fullmatch(value):
        raise MissionOSLLMSchemaValidationError(f"{key}_invalid_sha256")
    return value


def _require_number_between(
    payload: Mapping[str, Any],
    key: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MissionOSLLMSchemaValidationError(f"{key}_must_be_number")
    value = float(value)
    if value < minimum or value > maximum:
        raise MissionOSLLMSchemaValidationError(f"{key}_out_of_range")
    return value


def validate_llm_invocation_evidence(evidence: Any) -> dict[str, Any]:
    """Validate LLM invocation metadata without treating it as gate authority."""

    payload = dict(_as_mapping(evidence))
    if payload.get("schema_version") != LLM_INVOCATION_EVIDENCE_SCHEMA_VERSION:
        raise MissionOSLLMSchemaValidationError("llm_invocation_evidence_schema_version_invalid")
    _require_string(payload, "model_id")
    _require_sha256(payload, "prompt_sha256")
    _require_sha256(payload, "response_sha256")
    _require_number_between(payload, "temperature", minimum=0.0, maximum=2.0)
    replay_n_runs = payload.get("replay_n_runs")
    if isinstance(replay_n_runs, bool) or not isinstance(replay_n_runs, int) or replay_n_runs < 1:
        raise MissionOSLLMSchemaValidationError("replay_n_runs_must_be_positive_int")
    _require_number_between(payload, "replay_agreement_ratio", minimum=0.0, maximum=1.0)
    _require_bool_false(payload, "llm_judgment_in_gate")
    return payload


def validate_common_llm_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate Phase 0c common fields for every future LLM artifact schema."""

    artifact = dict(payload)
    schema_version = artifact.get("schema_version")
    if schema_version not in MISSIONOS_LLM_ARTIFACT_SCHEMA_VERSIONS:
        raise MissionOSLLMSchemaValidationError("missionos_llm_artifact_schema_version_invalid")
    _require_bool_false(artifact, "llm_judgment_in_gate")
    _require_bool_false(artifact, "progress_counted")
    _require_bool_false(artifact, "goal_640_progress_counted")
    _require_bool_false(artifact, "ai_agent_progress_counted")
    _require_bool_false(artifact, "drone_physics_affected")
    _require_bool_false(artifact, "dispatch_authority_created")
    _require_bool_false(artifact, "operator_approved")
    invocation = validate_llm_invocation_evidence(
        artifact.get("llm_invocation_evidence")
    )
    artifact["llm_invocation_evidence"] = invocation
    return artifact


def validate_llm_response_proposal(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a future response proposal as a proposal, not approval."""

    artifact = validate_common_llm_artifact(payload)
    if artifact.get("schema_version") != LLM_RESPONSE_PROPOSAL_SCHEMA_VERSION:
        raise MissionOSLLMSchemaValidationError("llm_response_proposal_schema_version_invalid")
    response_kind = _require_string(artifact, "response_kind")
    if response_kind not in LLM_RESPONSE_KIND_WHITELIST:
        raise MissionOSLLMSchemaValidationError("response_kind_not_allowed")
    parameters = artifact.get("parameters")
    if not isinstance(parameters, Mapping):
        raise MissionOSLLMSchemaValidationError("parameters_mapping_required")
    _require_string(artifact, "rationale")
    _require_string(artifact, "expected_outcome")
    _require_string(artifact, "uncertainty")
    _require_string(artifact, "approval_request")
    return artifact


def validate_llm_repair_proposal(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a repair proposal as planning intelligence, not authority."""

    artifact = validate_common_llm_artifact(payload)
    if artifact.get("schema_version") != LLM_REPAIR_PROPOSAL_SCHEMA_VERSION:
        raise MissionOSLLMSchemaValidationError("llm_repair_proposal_schema_version_invalid")
    _require_string(artifact, "repair_target")
    actions = artifact.get("repair_actions")
    if not isinstance(actions, list) or not actions:
        raise MissionOSLLMSchemaValidationError("repair_actions_nonempty_list_required")
    for index, action in enumerate(actions):
        if not isinstance(action, Mapping):
            raise MissionOSLLMSchemaValidationError(f"repair_action_{index}_mapping_required")
        _require_string(action, "action_type")
        _require_string(action, "description")
    _require_string(artifact, "rationale")
    _require_string(artifact, "expected_outcome")
    _require_string(artifact, "uncertainty")
    _require_string(artifact, "next_verification")
    _require_string(artifact, "proposed_operator_instruction")
    proposed_parameters = artifact.get("proposed_parameters")
    if not isinstance(proposed_parameters, Mapping):
        raise MissionOSLLMSchemaValidationError("proposed_parameters_mapping_required")
    return artifact


__all__ = [
    "LLM_DIAGNOSTIC_NARRATIVE_SCHEMA_VERSION",
    "LLM_EXPERIMENT_PROPOSAL_SCHEMA_VERSION",
    "LLM_INVOCATION_EVIDENCE_SCHEMA_VERSION",
    "LLM_META_LESSON_SCHEMA_VERSION",
    "LLM_REPAIR_PROPOSAL_SCHEMA_VERSION",
    "LLM_RESPONSE_KIND_WHITELIST",
    "LLM_RESPONSE_PROPOSAL_SCHEMA_VERSION",
    "LLM_SITUATION_ASSESSMENT_SCHEMA_VERSION",
    "MISSIONOS_LLM_ARTIFACT_SCHEMA_VERSIONS",
    "MissionOSLLMSchemaValidationError",
    "validate_common_llm_artifact",
    "validate_llm_invocation_evidence",
    "validate_llm_response_proposal",
    "validate_llm_repair_proposal",
]
