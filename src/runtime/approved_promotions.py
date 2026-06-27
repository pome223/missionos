"""Approval-gated promotion artifacts for Mission OS packages.

This module is intentionally artifact-only. It turns a benchmark/eval-gated
``promotion_package.v1`` into an approved typed artifact after explicit operator
approval, but it does not register skills, install capabilities, enforce policy,
or wire anything into mission reuse.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.runtime.mission_promotion import (
    MissionPromotionCandidateType,
    MissionPromotionPackage,
    is_security_sensitive_candidate_type,
)

APPROVED_IMPROVEMENT_MEMORY_SCHEMA_VERSION = "approved_improvement_memory.v1"
APPROVED_SKILL_SCHEMA_VERSION = "approved_skill.v1"
CAPABILITY_PATCH_SCHEMA_VERSION = "capability_patch.v1"
POLICY_PATCH_SCHEMA_VERSION = "policy_patch.v1"


class ApprovedPromotionError(ValueError):
    """Raised when a promotion package cannot become an approved artifact."""


class ApprovedPromotionStatus(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovedPromotionTarget(str, Enum):
    APPROVED_IMPROVEMENT_MEMORY = "approved_improvement_memory"
    APPROVED_SKILL = "approved_skill"
    CAPABILITY_PATCH = "capability_patch"
    POLICY_PATCH = "policy_patch"


_TARGET_SCHEMA_VERSION = {
    ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY: (
        APPROVED_IMPROVEMENT_MEMORY_SCHEMA_VERSION
    ),
    ApprovedPromotionTarget.APPROVED_SKILL: APPROVED_SKILL_SCHEMA_VERSION,
    ApprovedPromotionTarget.CAPABILITY_PATCH: CAPABILITY_PATCH_SCHEMA_VERSION,
    ApprovedPromotionTarget.POLICY_PATCH: POLICY_PATCH_SCHEMA_VERSION,
}

_CANDIDATE_ALLOWED_TARGETS: dict[
    MissionPromotionCandidateType, tuple[ApprovedPromotionTarget, ...]
] = {
    MissionPromotionCandidateType.VERIFIER_IMPROVEMENT: (
        ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY,
        ApprovedPromotionTarget.POLICY_PATCH,
    ),
    MissionPromotionCandidateType.RECOVERY_STRATEGY: (
        ApprovedPromotionTarget.APPROVED_SKILL,
        ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY,
    ),
    MissionPromotionCandidateType.BENCHMARK_CASE: (
        ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY,
    ),
    MissionPromotionCandidateType.MEMORY_RULE: (
        ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY,
        ApprovedPromotionTarget.POLICY_PATCH,
    ),
    MissionPromotionCandidateType.SKILL_RECIPE: (
        ApprovedPromotionTarget.APPROVED_SKILL,
    ),
    MissionPromotionCandidateType.CAPABILITY_PATCH: (
        ApprovedPromotionTarget.CAPABILITY_PATCH,
    ),
    MissionPromotionCandidateType.POLICY_PATCH: (
        ApprovedPromotionTarget.POLICY_PATCH,
    ),
    MissionPromotionCandidateType.CODE_PATCH: (),
}


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


def _target(value: ApprovedPromotionTarget | str) -> ApprovedPromotionTarget:
    if isinstance(value, ApprovedPromotionTarget):
        return value
    normalized = str(value or "").strip()
    try:
        return ApprovedPromotionTarget(normalized)
    except ValueError as exc:
        raise ApprovedPromotionError(
            f"unknown approved promotion target: {normalized}"
        ) from exc


def approved_promotion_targets_for_candidate_type(
    candidate_type: MissionPromotionCandidateType | str,
) -> list[ApprovedPromotionTarget]:
    """Return allowed approved-artifact targets for a package candidate type."""

    if isinstance(candidate_type, MissionPromotionCandidateType):
        normalized = candidate_type
    else:
        raw_value = str(candidate_type or "").strip()
        try:
            normalized = MissionPromotionCandidateType(raw_value)
        except ValueError as exc:
            raise ApprovedPromotionError(
                f"unknown promotion candidate type: {raw_value}"
            ) from exc
    return list(_CANDIDATE_ALLOWED_TARGETS[normalized])


class _ApprovedPromotionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    artifact_id: str
    promotion_target: ApprovedPromotionTarget
    source_package_id: str
    candidate_id: str
    candidate_type: MissionPromotionCandidateType
    source_mission_task_id: str = ""
    source_refs: list[str] = Field(default_factory=list)
    approval_status: ApprovedPromotionStatus = ApprovedPromotionStatus.APPROVED
    approved_by: str
    approved_at: datetime
    approval_ref: str
    expires_at: datetime | None = None
    invalidation_rule: str = ""
    approval_requirements: dict[str, Any] = Field(default_factory=dict)
    benchmark_refs: list[str] = Field(default_factory=list)
    security_refs: list[str] = Field(default_factory=list)
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_refs", "benchmark_refs", "security_refs", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


class ApprovedImprovementMemoryArtifact(_ApprovedPromotionBase):
    schema_version: Literal[APPROVED_IMPROVEMENT_MEMORY_SCHEMA_VERSION] = (
        APPROVED_IMPROVEMENT_MEMORY_SCHEMA_VERSION
    )
    promotion_target: Literal[ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY] = (
        ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY
    )
    memory_kind: str = "approved_improvement"
    failure_type: str = ""


class ApprovedSkillArtifact(_ApprovedPromotionBase):
    schema_version: Literal[APPROVED_SKILL_SCHEMA_VERSION] = APPROVED_SKILL_SCHEMA_VERSION
    promotion_target: Literal[ApprovedPromotionTarget.APPROVED_SKILL] = (
        ApprovedPromotionTarget.APPROVED_SKILL
    )
    skill_name: str
    procedure: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)

    @field_validator("procedure", "inputs", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


class CapabilityPatchArtifact(_ApprovedPromotionBase):
    schema_version: Literal[CAPABILITY_PATCH_SCHEMA_VERSION] = CAPABILITY_PATCH_SCHEMA_VERSION
    promotion_target: Literal[ApprovedPromotionTarget.CAPABILITY_PATCH] = (
        ApprovedPromotionTarget.CAPABILITY_PATCH
    )
    capability_name: str
    registration_required: bool = True
    action_schema: dict[str, Any] = Field(default_factory=dict)


class PolicyPatchArtifact(_ApprovedPromotionBase):
    schema_version: Literal[POLICY_PATCH_SCHEMA_VERSION] = POLICY_PATCH_SCHEMA_VERSION
    promotion_target: Literal[ApprovedPromotionTarget.POLICY_PATCH] = (
        ApprovedPromotionTarget.POLICY_PATCH
    )
    policy_id: str
    enforcement_scope: str = "mission_promotion"
    rules: list[str] = Field(default_factory=list)

    @field_validator("rules", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


ApprovedPromotionArtifact: TypeAlias = (
    ApprovedImprovementMemoryArtifact
    | ApprovedSkillArtifact
    | CapabilityPatchArtifact
    | PolicyPatchArtifact
)


def _normalize_package(
    package: MissionPromotionPackage | dict[str, Any],
) -> MissionPromotionPackage:
    if isinstance(package, MissionPromotionPackage):
        return package
    return MissionPromotionPackage.model_validate(package)


def _source_candidate(package: MissionPromotionPackage) -> dict[str, Any]:
    return _as_dict(package.metadata.get("source_candidate"))


def _summary(package: MissionPromotionPackage) -> str:
    source_candidate = _source_candidate(package)
    summary = str(source_candidate.get("summary") or "").strip()
    return summary or f"Approved promotion for {package.candidate_id}"


def _failure_type(package: MissionPromotionPackage) -> str:
    return str(_source_candidate(package).get("failure_type") or "").strip()


def _stable_name(value: str) -> str:
    normalized = "".join(
        char.lower() if char.isalnum() else "_" for char in str(value or "")
    )
    normalized = "_".join(part for part in normalized.split("_") if part)
    return normalized[:80] or "approved_promotion"


def _benchmark_refs(package: MissionPromotionPackage) -> list[str]:
    refs = [f"mission_regression_gate:{package.package_id}"]
    for key in ("baseline_eval_result", "candidate_eval_result"):
        result = _as_dict(getattr(package, key))
        suite_id = str(result.get("suite_id") or "").strip()
        subject_id = str(result.get("subject_id") or "").strip()
        if suite_id:
            refs.append(f"mission_eval_result:{suite_id}:{subject_id or key}")
    return list(dict.fromkeys(refs))


def _security_refs(package: MissionPromotionPackage) -> list[str]:
    result = _as_dict(package.security_eval_result)
    suite_id = str(result.get("suite_id") or "").strip()
    subject_id = str(result.get("subject_id") or "").strip()
    if not suite_id:
        return []
    return [f"mission_eval_result:{suite_id}:{subject_id or 'security'}"]


def _approval_requirements(
    package: MissionPromotionPackage,
    *,
    target: ApprovedPromotionTarget,
    approval_ref: str,
) -> dict[str, Any]:
    return {
        "requires_operator_approval": True,
        "requires_benchmark_gate": True,
        "requires_security_eval": (
            target
            in {
                ApprovedPromotionTarget.CAPABILITY_PATCH,
                ApprovedPromotionTarget.POLICY_PATCH,
            }
            or is_security_sensitive_candidate_type(package.candidate_type.value)
        ),
        "approval_ref": approval_ref,
    }


def _security_eval_required_for_target(
    package: MissionPromotionPackage,
    *,
    target: ApprovedPromotionTarget,
) -> bool:
    return (
        target
        in {
            ApprovedPromotionTarget.CAPABILITY_PATCH,
            ApprovedPromotionTarget.POLICY_PATCH,
        }
        or is_security_sensitive_candidate_type(package.candidate_type.value)
    )


def is_promotion_package_approval_ready(
    package: MissionPromotionPackage | dict[str, Any],
    *,
    promotion_target: ApprovedPromotionTarget | str | None = None,
) -> bool:
    """Return whether a promotion package may become a target approved artifact."""

    normalized = _normalize_package(package)
    target = (
        _target(promotion_target)
        if promotion_target is not None
        else default_approved_promotion_target(normalized.candidate_type)
    )
    gate = _as_dict(normalized.regression_gate_result)
    security = _as_dict(normalized.security_eval_result)
    security_required = _security_eval_required_for_target(
        normalized,
        target=target,
    )
    if normalized.recommendation != "pending_operator_approval":
        return False
    if normalized.approval_status != "pending":
        return False
    if not normalized.requires_operator_approval:
        return False
    if normalized.unevaluated_required_suites:
        return False
    if not bool(gate.get("passed")):
        return False
    if security_required and not bool(security.get("passed")):
        return False
    return True


def default_approved_promotion_target(
    candidate_type: MissionPromotionCandidateType | str,
) -> ApprovedPromotionTarget:
    """Return the default approved artifact target for a candidate type."""

    targets = approved_promotion_targets_for_candidate_type(candidate_type)
    if not targets:
        raise ApprovedPromotionError(
            f"{candidate_type} has no approved promotion target in this layer"
        )
    return targets[0]


def build_approved_promotion_artifact(
    package: MissionPromotionPackage | dict[str, Any],
    *,
    promotion_target: ApprovedPromotionTarget | str | None = None,
    approved_by: str,
    approval_ref: str,
    approved_at: datetime | None = None,
    expires_at: datetime | None = None,
    invalidation_rule: str = "",
    metadata: dict[str, Any] | None = None,
) -> ApprovedPromotionArtifact:
    """Create a typed approved artifact from an approval-ready package."""

    normalized = _normalize_package(package)
    approver = str(approved_by or "").strip()
    approval_reference = str(approval_ref or "").strip()
    if not approver:
        raise ApprovedPromotionError("approved_by is required")
    if not approval_reference:
        raise ApprovedPromotionError("approval_ref is required")
    target = (
        _target(promotion_target)
        if promotion_target is not None
        else default_approved_promotion_target(normalized.candidate_type)
    )
    allowed_targets = approved_promotion_targets_for_candidate_type(
        normalized.candidate_type
    )
    if target not in allowed_targets:
        allowed = ", ".join(item.value for item in allowed_targets) or "none"
        raise ApprovedPromotionError(
            f"{target.value} is not allowed for "
            f"{normalized.candidate_type.value}; allowed targets: {allowed}"
        )
    if not is_promotion_package_approval_ready(
        normalized,
        promotion_target=target,
    ):
        if _security_eval_required_for_target(normalized, target=target) and not bool(
            _as_dict(normalized.security_eval_result).get("passed")
        ):
            raise ApprovedPromotionError(
                f"{target.value} requires a passing security eval"
            )
        raise ApprovedPromotionError("promotion package is not approval-ready")

    now = approved_at or _utc_now()
    base_payload = {
        "artifact_id": f"{normalized.package_id}:{target.value}",
        "source_package_id": normalized.package_id,
        "candidate_id": normalized.candidate_id,
        "candidate_type": normalized.candidate_type,
        "source_mission_task_id": normalized.source_mission_task_id,
        "source_refs": list(dict.fromkeys(normalized.source_refs)),
        "approval_status": ApprovedPromotionStatus.APPROVED,
        "approved_by": approver,
        "approved_at": now,
        "approval_ref": approval_reference,
        "expires_at": expires_at,
        "invalidation_rule": str(invalidation_rule or "").strip(),
        "approval_requirements": _approval_requirements(
            normalized,
            target=target,
            approval_ref=approval_reference,
        ),
        "benchmark_refs": _benchmark_refs(normalized),
        "security_refs": _security_refs(normalized),
        "content": _summary(normalized),
        "metadata": {
            **(metadata or {}),
            "source_package": normalized.model_dump(mode="json"),
        },
    }
    handle = _stable_name(normalized.candidate_id)

    if target == ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY:
        return ApprovedImprovementMemoryArtifact(
            **base_payload,
            failure_type=_failure_type(normalized),
        )
    if target == ApprovedPromotionTarget.APPROVED_SKILL:
        return ApprovedSkillArtifact(
            **base_payload,
            skill_name=f"approved/{handle}",
            procedure=[
                "Use the benchmarked recovery path from the promotion package.",
                "Capture destination-bound evidence before declaring success.",
                "Fall back to verifier-first recovery if the target has drifted.",
            ],
            inputs=["mission_contract", "current_evidence", "verifier_verdict"],
        )
    if target == ApprovedPromotionTarget.CAPABILITY_PATCH:
        return CapabilityPatchArtifact(
            **base_payload,
            capability_name=f"approved.{handle}",
            action_schema={
                "type": "object",
                "description": _summary(normalized),
                "requires_destination_bound_verification": True,
            },
        )
    if target == ApprovedPromotionTarget.POLICY_PATCH:
        return PolicyPatchArtifact(
            **base_payload,
            policy_id=f"policy-{handle.replace('_', '-')}",
            rules=[
                _summary(normalized),
                "benchmark regressions block promotion",
                "operator approval is required before reuse",
            ],
        )

    raise ApprovedPromotionError(f"unsupported approved promotion target: {target}")


def normalize_approved_promotion_artifact(
    artifact: ApprovedPromotionArtifact | dict[str, Any],
) -> ApprovedPromotionArtifact:
    """Normalize an approved promotion artifact by its target/schema."""

    if isinstance(
        artifact,
        (
            ApprovedImprovementMemoryArtifact,
            ApprovedSkillArtifact,
            CapabilityPatchArtifact,
            PolicyPatchArtifact,
        ),
    ):
        return artifact
    payload = _as_dict(artifact)
    target_value = payload.get("promotion_target")
    if not target_value:
        schema_version = str(payload.get("schema_version") or "").strip()
        for target, version in _TARGET_SCHEMA_VERSION.items():
            if schema_version == version:
                target_value = target.value
                break
    target = _target(target_value)
    if target == ApprovedPromotionTarget.APPROVED_IMPROVEMENT_MEMORY:
        return ApprovedImprovementMemoryArtifact.model_validate(payload)
    if target == ApprovedPromotionTarget.APPROVED_SKILL:
        return ApprovedSkillArtifact.model_validate(payload)
    if target == ApprovedPromotionTarget.CAPABILITY_PATCH:
        return CapabilityPatchArtifact.model_validate(payload)
    if target == ApprovedPromotionTarget.POLICY_PATCH:
        return PolicyPatchArtifact.model_validate(payload)
    raise ApprovedPromotionError(f"unsupported approved promotion target: {target}")


def reject_approved_promotion_artifact(
    artifact: ApprovedPromotionArtifact | dict[str, Any],
    *,
    rejected_reason: str,
) -> ApprovedPromotionArtifact:
    """Mark an approved promotion artifact as rejected without deleting history."""

    normalized = normalize_approved_promotion_artifact(artifact)
    reason = str(rejected_reason or "").strip()
    if not reason:
        raise ApprovedPromotionError("rejected_reason is required")
    return normalized.model_copy(
        update={
            "approval_status": ApprovedPromotionStatus.REJECTED,
            "metadata": {**normalized.metadata, "rejected_reason": reason},
        }
    )


def is_approved_promotion_artifact_usable(
    artifact: ApprovedPromotionArtifact | dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether an approved artifact is currently usable by future layers."""

    normalized = normalize_approved_promotion_artifact(artifact)
    current_time = now or _utc_now()
    return (
        normalized.approval_status == ApprovedPromotionStatus.APPROVED
        and (normalized.expires_at is None or normalized.expires_at > current_time)
    )


def _artifact_collections(artifacts: dict[str, Any] | list[Any]) -> list[Any]:
    if isinstance(artifacts, list):
        return artifacts
    payload = _as_dict(artifacts)
    collected: list[Any] = []
    for key in (
        "approved_promotions",
        "approved_improvement_memory",
        "approved_improvement_memories",
        "approved_skills",
        "capability_patches",
        "policy_patches",
    ):
        collected.extend(_as_list(payload.get(key)))
    return collected


def list_approved_promotion_artifacts(
    artifacts: dict[str, Any] | list[Any],
    *,
    promotion_target: ApprovedPromotionTarget | str | None = None,
    now: datetime | None = None,
) -> list[ApprovedPromotionArtifact]:
    """List usable approved artifacts, optionally filtered by promotion target."""

    target_filter = _target(promotion_target) if promotion_target is not None else None
    normalized: list[ApprovedPromotionArtifact] = []
    for item in _artifact_collections(artifacts):
        artifact = normalize_approved_promotion_artifact(_as_dict(item))
        if target_filter is not None and artifact.promotion_target != target_filter:
            continue
        if is_approved_promotion_artifact_usable(artifact, now=now):
            normalized.append(artifact)
    return normalized


def group_approved_promotion_artifacts_by_type(
    artifacts: dict[str, Any] | list[Any],
    *,
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return usable approved artifacts grouped by target for operator display."""

    grouped = {target.value: [] for target in ApprovedPromotionTarget}
    for artifact in list_approved_promotion_artifacts(artifacts, now=now):
        grouped[artifact.promotion_target.value].append(artifact.model_dump(mode="json"))
    return grouped


__all__ = [
    "APPROVED_IMPROVEMENT_MEMORY_SCHEMA_VERSION",
    "APPROVED_SKILL_SCHEMA_VERSION",
    "CAPABILITY_PATCH_SCHEMA_VERSION",
    "POLICY_PATCH_SCHEMA_VERSION",
    "ApprovedImprovementMemoryArtifact",
    "ApprovedPromotionArtifact",
    "ApprovedPromotionError",
    "ApprovedPromotionStatus",
    "ApprovedPromotionTarget",
    "ApprovedSkillArtifact",
    "CapabilityPatchArtifact",
    "PolicyPatchArtifact",
    "approved_promotion_targets_for_candidate_type",
    "build_approved_promotion_artifact",
    "default_approved_promotion_target",
    "group_approved_promotion_artifacts_by_type",
    "is_approved_promotion_artifact_usable",
    "is_promotion_package_approval_ready",
    "list_approved_promotion_artifacts",
    "normalize_approved_promotion_artifact",
    "reject_approved_promotion_artifact",
]
