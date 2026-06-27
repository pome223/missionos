"""Artifact-only Mission OS reuse planning.

The reuse planner selects already-approved promotion artifacts for a new mission
and records why each one was or was not selected. It does not apply policy
patches, register capabilities, register skills, or alter live runtime behavior.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.runtime.approved_promotions import (
    ApprovedPromotionArtifact,
    ApprovedPromotionStatus,
    ApprovedPromotionTarget,
    normalize_approved_promotion_artifact,
)
from src.runtime.mission_contract import MissionContract, normalize_mission_contract

MISSION_REUSE_PLAN_SCHEMA_VERSION = "reuse_plan.v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _text_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        texts: list[str] = []
        for item in value.values():
            texts.extend(_text_values(item))
        return texts
    if isinstance(value, list | tuple | set):
        texts: list[str] = []
        for item in value:
            texts.extend(_text_values(item))
        return texts
    if isinstance(value, bool):
        return []
    return [str(value)]


def _tokens(value: Any) -> set[str]:
    text = " ".join(_text_values(value)).lower()
    tokens: set[str] = set()
    stop_words = {
        "the",
        "and",
        "for",
        "with",
        "schema_version",
        "metadata",
        "source",
        "artifact",
        "approved",
        "approval",
        "candidate",
        "package",
        "evidence",
        "when",
    }
    for token in re.findall(r"[a-z0-9_]+", text):
        if len(token) < 3 or token in stop_words:
            continue
        tokens.add(token)
        if len(token) > 3 and token.endswith("s"):
            tokens.add(token[:-1])
    return tokens


class MissionReuseSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    promotion_target: ApprovedPromotionTarget
    source_package_id: str = ""
    candidate_id: str = ""
    source_refs: list[str] = Field(default_factory=list)
    reason: str
    matched_terms: list[str] = Field(default_factory=list)
    relevance_score: float = 0.0
    approval_ref: str = ""
    expires_at: datetime | None = None
    invalidation_rule: str = ""
    application_mode: str = "operator_visible_plan_only"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_refs", "matched_terms", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


class MissionReuseExcludedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = ""
    candidate_id: str = ""
    promotion_target: str = ""
    reason: str
    details: str = ""
    source_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_refs", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


class MissionReuseCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    check: str
    passed: bool
    reason: str = ""
    checked_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class MissionReusePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = MISSION_REUSE_PLAN_SCHEMA_VERSION
    mission_task_id: str = ""
    mission_contract_id: str = ""
    selected_memories: list[MissionReuseSelection] = Field(default_factory=list)
    selected_skills: list[MissionReuseSelection] = Field(default_factory=list)
    selected_policies: list[MissionReuseSelection] = Field(default_factory=list)
    selected_capabilities: list[MissionReuseSelection] = Field(default_factory=list)
    excluded_candidates: list[MissionReuseExcludedCandidate] = Field(default_factory=list)
    selection_reasons: list[dict[str, Any]] = Field(default_factory=list)
    expiry_checks: list[MissionReuseCheck] = Field(default_factory=list)
    policy_checks: list[MissionReuseCheck] = Field(default_factory=list)
    operator_visible: bool = True
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _mission_contract_payload(
    mission_contract: MissionContract | dict[str, Any],
) -> dict[str, Any]:
    if isinstance(mission_contract, MissionContract):
        return mission_contract.model_dump(mode="json")
    return normalize_mission_contract(mission_contract).model_dump(mode="json")


def _approved_artifact_items(artifacts: dict[str, Any] | list[Any]) -> list[Any]:
    if isinstance(artifacts, list):
        return artifacts
    payload = _as_dict(artifacts)
    items: list[Any] = []
    for key in (
        "approved_promotions",
        "approved_improvement_memory",
        "approved_improvement_memories",
        "approved_skills",
        "capability_patches",
        "policy_patches",
    ):
        items.extend(_as_list(payload.get(key)))
    return items


def _non_reuse_candidate_items(artifacts: dict[str, Any] | list[Any]) -> list[Any]:
    if isinstance(artifacts, list):
        return []
    payload = _as_dict(artifacts)
    items: list[Any] = []
    for key in ("memory_promotion_candidates", "promotion_packages"):
        for item in _as_list(payload.get(key)):
            item_payload = _as_dict(item)
            item_payload.setdefault("_source_collection", key)
            items.append(item_payload)
    review = _as_dict(payload.get("mission_review"))
    for item in _as_list(review.get("memory_promotion_candidates")):
        item_payload = _as_dict(item)
        item_payload.setdefault(
            "_source_collection",
            "mission_review.memory_promotion_candidates",
        )
        items.append(item_payload)
    for item in _as_list(review.get("improvement_candidates")):
        item_payload = _as_dict(item)
        item_payload.setdefault(
            "_source_collection",
            "mission_review.improvement_candidates",
        )
        items.append(item_payload)
    return items


def _artifact_terms(artifact: ApprovedPromotionArtifact) -> set[str]:
    payload = artifact.model_dump(mode="json")
    focused_payload = {
        "content": payload.get("content"),
        "failure_type": payload.get("failure_type"),
        "invalidation_rule": payload.get("invalidation_rule"),
        "skill_name": payload.get("skill_name"),
        "procedure": payload.get("procedure"),
        "inputs": payload.get("inputs"),
        "capability_name": payload.get("capability_name"),
        "action_schema": payload.get("action_schema"),
        "policy_id": payload.get("policy_id"),
        "rules": payload.get("rules"),
        "metadata": _as_dict(payload.get("metadata")).get("reuse_keywords"),
    }
    return _tokens(focused_payload)


def _invalidation_triggered(artifact: ApprovedPromotionArtifact) -> bool:
    metadata = _as_dict(artifact.metadata)
    invalidation = _as_dict(metadata.get("invalidation"))
    return bool(
        metadata.get("invalidated")
        or metadata.get("invalidation_triggered")
        or invalidation.get("triggered")
    )


def _expiry_check(
    artifact: ApprovedPromotionArtifact,
    *,
    now: datetime,
) -> MissionReuseCheck:
    if artifact.expires_at is None:
        reason = "no_expiry"
        passed = True
    elif artifact.expires_at > now:
        reason = "not_expired"
        passed = True
    else:
        reason = "expired"
        passed = False
    return MissionReuseCheck(
        artifact_id=artifact.artifact_id,
        check="expiry",
        passed=passed,
        reason=reason,
        checked_at=now,
        metadata={
            "expires_at": artifact.expires_at.isoformat()
            if artifact.expires_at
            else None
        },
    )


def _policy_checks(
    artifact: ApprovedPromotionArtifact,
    *,
    now: datetime,
) -> list[MissionReuseCheck]:
    invalidated = _invalidation_triggered(artifact)
    checks = [
        MissionReuseCheck(
            artifact_id=artifact.artifact_id,
            check="approval_status",
            passed=artifact.approval_status == ApprovedPromotionStatus.APPROVED,
            reason=artifact.approval_status.value,
            checked_at=now,
        ),
        MissionReuseCheck(
            artifact_id=artifact.artifact_id,
            check="invalidation_rule",
            passed=not invalidated,
            reason=(
                "invalidation_triggered"
                if invalidated
                else (
                    "declared_not_triggered"
                    if artifact.invalidation_rule
                    else "not_declared"
                )
            ),
            checked_at=now,
            metadata={"invalidation_rule": artifact.invalidation_rule},
        ),
    ]
    if artifact.promotion_target in {
        ApprovedPromotionTarget.CAPABILITY_PATCH,
        ApprovedPromotionTarget.POLICY_PATCH,
    }:
        checks.append(
            MissionReuseCheck(
                artifact_id=artifact.artifact_id,
                check="application_mode",
                passed=True,
                reason="selected_for_operator_visible_plan_only",
                checked_at=now,
                metadata={"automatic_application": False},
            )
        )
    return checks


def _selection_bucket(target: ApprovedPromotionTarget) -> str:
    return {
        ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY: "selected_memories",
        ApprovedPromotionTarget.APPROVED_SKILL: "selected_skills",
        ApprovedPromotionTarget.POLICY_PATCH: "selected_policies",
        ApprovedPromotionTarget.CAPABILITY_PATCH: "selected_capabilities",
    }[target]


def _selection_reason(
    artifact: ApprovedPromotionArtifact,
    matched_terms: list[str],
) -> str:
    if matched_terms:
        return (
            f"selected {artifact.promotion_target.value} because it matches "
            f"mission terms: {', '.join(matched_terms[:5])}"
        )
    return f"selected {artifact.promotion_target.value} by explicit always_applicable metadata"


def _selection(
    artifact: ApprovedPromotionArtifact,
    *,
    matched_terms: list[str],
    relevance_score: float,
) -> MissionReuseSelection:
    return MissionReuseSelection(
        artifact_id=artifact.artifact_id,
        promotion_target=artifact.promotion_target,
        source_package_id=artifact.source_package_id,
        candidate_id=artifact.candidate_id,
        source_refs=artifact.source_refs,
        reason=_selection_reason(artifact, matched_terms),
        matched_terms=matched_terms,
        relevance_score=relevance_score,
        approval_ref=artifact.approval_ref,
        expires_at=artifact.expires_at,
        invalidation_rule=artifact.invalidation_rule,
        metadata={
            "schema_version": artifact.schema_version,
            "content": artifact.content,
        },
    )


def _excluded_from_artifact(
    artifact: ApprovedPromotionArtifact,
    *,
    reason: str,
    details: str = "",
) -> MissionReuseExcludedCandidate:
    return MissionReuseExcludedCandidate(
        artifact_id=artifact.artifact_id,
        candidate_id=artifact.candidate_id,
        promotion_target=artifact.promotion_target.value,
        reason=reason,
        details=details,
        source_refs=artifact.source_refs,
    )


def _excluded_from_candidate(candidate: dict[str, Any]) -> MissionReuseExcludedCandidate:
    source_collection = str(candidate.get("_source_collection") or "candidate").strip()
    return MissionReuseExcludedCandidate(
        artifact_id=str(
            candidate.get("artifact_id")
            or candidate.get("package_id")
            or candidate.get("candidate_id")
            or ""
        ).strip(),
        candidate_id=str(candidate.get("candidate_id") or "").strip(),
        promotion_target=str(
            candidate.get("promotion_target") or candidate.get("type") or ""
        ).strip(),
        reason="not_approved_promotion_artifact",
        details=f"{source_collection} is not consumed by the reuse planner",
        source_refs=_str_list(candidate.get("source_refs")),
        metadata={
            key: value
            for key, value in candidate.items()
            if key != "_source_collection"
        },
    )


def build_mission_reuse_plan(
    mission_contract: MissionContract | dict[str, Any],
    approved_artifacts: dict[str, Any] | list[Any],
    *,
    mission_task_id: str = "",
    now: datetime | None = None,
    max_per_type: int = 3,
) -> MissionReusePlan:
    """Build an operator-visible reuse plan from approved promotion artifacts."""

    current_time = now or _utc_now()
    try:
        per_type_limit = max(0, int(max_per_type))
    except (TypeError, ValueError):
        per_type_limit = 0
    contract_payload = _mission_contract_payload(mission_contract)
    contract_tokens = _tokens(contract_payload)
    selected_by_bucket: dict[str, list[MissionReuseSelection]] = {
        "selected_memories": [],
        "selected_skills": [],
        "selected_policies": [],
        "selected_capabilities": [],
    }
    excluded: list[MissionReuseExcludedCandidate] = []
    expiry_checks: list[MissionReuseCheck] = []
    policy_checks: list[MissionReuseCheck] = []
    selection_reasons: list[dict[str, Any]] = []

    for raw_item in _approved_artifact_items(approved_artifacts):
        try:
            artifact = normalize_approved_promotion_artifact(_as_dict(raw_item))
        except Exception as exc:  # noqa: BLE001 - malformed artifacts must be reported.
            excluded.append(
                MissionReuseExcludedCandidate(
                    reason="invalid_approved_artifact",
                    details=str(exc),
                    metadata=_as_dict(raw_item),
                )
            )
            continue

        expiry = _expiry_check(artifact, now=current_time)
        checks = _policy_checks(artifact, now=current_time)
        expiry_checks.append(expiry)
        policy_checks.extend(checks)

        approval_status = artifact.approval_status
        if approval_status == ApprovedPromotionStatus.REJECTED:
            excluded.append(
                _excluded_from_artifact(
                    artifact,
                    reason="rejected",
                    details="approved artifact was later rejected",
                )
            )
            continue
        if approval_status == ApprovedPromotionStatus.EXPIRED:
            excluded.append(
                _excluded_from_artifact(
                    artifact,
                    reason="expired",
                    details="approval status is expired",
                )
            )
            continue
        if approval_status != ApprovedPromotionStatus.APPROVED:
            excluded.append(
                _excluded_from_artifact(
                    artifact,
                    reason="not_approved",
                    details=approval_status.value,
                )
            )
            continue

        if not expiry.passed:
            excluded.append(
                _excluded_from_artifact(
                    artifact,
                    reason="expired",
                    details=expiry.reason,
                )
            )
            continue
        failed_policy_checks = [check for check in checks if not check.passed]
        if failed_policy_checks:
            excluded.append(
                _excluded_from_artifact(
                    artifact,
                    reason=failed_policy_checks[0].reason,
                    details=failed_policy_checks[0].check,
                )
            )
            continue

        artifact_tokens = _artifact_terms(artifact)
        matched_terms = sorted(contract_tokens & artifact_tokens)
        always_applicable = bool(_as_dict(artifact.metadata).get("always_applicable"))
        if not matched_terms and not always_applicable:
            excluded.append(
                _excluded_from_artifact(
                    artifact,
                    reason="no_contract_match",
                    details="approved artifact did not match mission contract terms",
                )
            )
            continue

        score = len(matched_terms) / max(len(contract_tokens), 1)
        selection = _selection(
            artifact,
            matched_terms=matched_terms,
            relevance_score=score,
        )
        bucket = _selection_bucket(artifact.promotion_target)
        selected_by_bucket[bucket].append(selection)
        selection_reasons.append(
            {
                "artifact_id": artifact.artifact_id,
                "promotion_target": artifact.promotion_target.value,
                "reason": selection.reason,
                "matched_terms": matched_terms,
                "relevance_score": score,
            }
        )

    for candidate in _non_reuse_candidate_items(approved_artifacts):
        excluded.append(_excluded_from_candidate(_as_dict(candidate)))

    for bucket, selections in selected_by_bucket.items():
        selected_by_bucket[bucket] = sorted(
            selections,
            key=lambda item: (-item.relevance_score, item.artifact_id),
        )[:per_type_limit]

    return MissionReusePlan(
        mission_task_id=str(mission_task_id or "").strip(),
        mission_contract_id=str(contract_payload.get("contract_id") or "").strip(),
        selected_memories=selected_by_bucket["selected_memories"],
        selected_skills=selected_by_bucket["selected_skills"],
        selected_policies=selected_by_bucket["selected_policies"],
        selected_capabilities=selected_by_bucket["selected_capabilities"],
        excluded_candidates=excluded,
        selection_reasons=selection_reasons,
        expiry_checks=expiry_checks,
        policy_checks=policy_checks,
        operator_visible=True,
        created_at=current_time,
        metadata={
            "contract_objective": contract_payload.get("objective"),
            "contract_terms_count": len(contract_tokens),
            "automatic_runtime_application": False,
        },
    )


__all__ = [
    "MISSION_REUSE_PLAN_SCHEMA_VERSION",
    "MissionReuseCheck",
    "MissionReuseExcludedCandidate",
    "MissionReusePlan",
    "MissionReuseSelection",
    "build_mission_reuse_plan",
]
