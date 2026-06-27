"""Artifact-only promotion packages for Mission OS improvement candidates."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.runtime.mission_evals import (
    MissionEvalResult,
    compare_mission_eval_results,
    run_mission_eval_suite,
)

PROMOTION_CANDIDATE_SCHEMA_VERSION = "mission_promotion_candidate.v1"
PROMOTION_PACKAGE_SCHEMA_VERSION = "promotion_package.v1"
PROMOTION_APPROVAL_STATUS_PENDING = "pending"


class MissionPromotionError(ValueError):
    """Raised when a promotion candidate/package cannot be built safely."""


class MissionPromotionCandidateType(str, Enum):
    VERIFIER_IMPROVEMENT = "verifier_improvement"
    RECOVERY_STRATEGY = "recovery_strategy"
    BENCHMARK_CASE = "benchmark_case"
    MEMORY_RULE = "memory_rule"
    SKILL_RECIPE = "skill_recipe"
    CAPABILITY_PATCH = "capability_patch"
    POLICY_PATCH = "policy_patch"
    CODE_PATCH = "code_patch"


_CANDIDATE_TYPE_EVAL_SUITES: dict[MissionPromotionCandidateType, list[str]] = {
    MissionPromotionCandidateType.VERIFIER_IMPROVEMENT: [
        "weak_evidence_probe",
        "mission_review_artifact_shape",
    ],
    MissionPromotionCandidateType.RECOVERY_STRATEGY: [
        "blocked_state_correctness",
        "budget_exhaustion_probe",
    ],
    MissionPromotionCandidateType.BENCHMARK_CASE: [
        "template_contract_generation",
        "control_ui_mission_panel_smoke",
    ],
    MissionPromotionCandidateType.MEMORY_RULE: [
        "memory_candidate_approval_boundary",
    ],
    MissionPromotionCandidateType.SKILL_RECIPE: [
        "template_contract_generation",
        "mission_review_artifact_shape",
    ],
    MissionPromotionCandidateType.CAPABILITY_PATCH: [
        "approval_required_action",
        "mission_review_artifact_shape",
    ],
    MissionPromotionCandidateType.POLICY_PATCH: [
        "approval_required_action",
        "mission_review_artifact_shape",
    ],
    MissionPromotionCandidateType.CODE_PATCH: [
        "approval_required_action",
        "mission_review_artifact_shape",
    ],
}

_SECURITY_SENSITIVE_CANDIDATE_TYPES = {
    MissionPromotionCandidateType.CAPABILITY_PATCH,
    MissionPromotionCandidateType.POLICY_PATCH,
    MissionPromotionCandidateType.CODE_PATCH,
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


def _candidate_type(value: Any) -> MissionPromotionCandidateType:
    normalized = str(value or "").strip()
    try:
        return MissionPromotionCandidateType(normalized)
    except ValueError as exc:
        raise MissionPromotionError(f"unknown promotion candidate type: {normalized}") from exc


def required_eval_suites_for_candidate_type(candidate_type: str) -> list[str]:
    """Return required deterministic eval suites for a promotion candidate type."""

    normalized = _candidate_type(candidate_type)
    return list(_CANDIDATE_TYPE_EVAL_SUITES[normalized])


def is_security_sensitive_candidate_type(candidate_type: str) -> bool:
    """Return whether a candidate type must pass explicit security evaluation."""

    return _candidate_type(candidate_type) in _SECURITY_SENSITIVE_CANDIDATE_TYPES


class MissionPromotionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = PROMOTION_CANDIDATE_SCHEMA_VERSION
    candidate_id: str
    candidate_type: MissionPromotionCandidateType
    summary: str
    failure_type: str | None = None
    source_mission_task_id: str = ""
    source_artifact_ref: str
    source_refs: list[str] = Field(default_factory=list)
    required_eval_suites: list[str] = Field(default_factory=list)
    security_sensitive: bool = False
    requires_benchmark: bool = True
    requires_operator_approval: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_refs", "required_eval_suites", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


class MissionPromotionPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = PROMOTION_PACKAGE_SCHEMA_VERSION
    package_id: str
    candidate_id: str
    candidate_type: MissionPromotionCandidateType
    source_mission_task_id: str = ""
    source_refs: list[str] = Field(default_factory=list)
    required_eval_suites: list[str] = Field(default_factory=list)
    evaluated_suite_ids: list[str] = Field(default_factory=list)
    unevaluated_required_suites: list[str] = Field(default_factory=list)
    eval_suite_id: str
    baseline_eval_result: dict[str, Any]
    candidate_eval_result: dict[str, Any]
    regression_gate_result: dict[str, Any]
    security_eval_result: dict[str, Any] | None = None
    recommendation: str
    approval_status: str = PROMOTION_APPROVAL_STATUS_PENDING
    requires_operator_approval: bool = True
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "source_refs",
        "required_eval_suites",
        "evaluated_suite_ids",
        "unevaluated_required_suites",
        mode="before",
    )
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


def normalize_promotion_candidate(
    candidate: MissionPromotionCandidate | dict[str, Any],
    *,
    mission_review: dict[str, Any] | None = None,
) -> MissionPromotionCandidate:
    """Normalize a mission review improvement candidate into a typed promotion candidate."""

    if isinstance(candidate, MissionPromotionCandidate):
        return candidate
    payload = _as_dict(candidate)
    candidate_type = _candidate_type(payload.get("candidate_type"))
    required_suites = required_eval_suites_for_candidate_type(candidate_type.value)
    review = _as_dict(mission_review)
    mission_task_id = str(
        payload.get("source_mission_task_id") or review.get("mission_task_id") or ""
    )
    source_artifact_ref = str(
        payload.get("source_artifact_ref")
        or "mission_review.improvement_candidates"
    ).strip()
    source_refs = _str_list(payload.get("source_refs"))
    if not source_refs:
        source_refs = _str_list(review.get("source_refs"))
    if mission_task_id:
        source_refs.append(f"mission_review:{mission_task_id}")
    if source_artifact_ref:
        source_refs.append(source_artifact_ref)
    unique_source_refs = list(dict.fromkeys(source_refs))
    return MissionPromotionCandidate(
        candidate_id=str(payload.get("candidate_id") or "").strip(),
        candidate_type=candidate_type,
        summary=str(payload.get("summary") or "").strip(),
        failure_type=(
            str(payload.get("failure_type")).strip()
            if payload.get("failure_type") is not None
            else None
        ),
        source_mission_task_id=mission_task_id,
        source_artifact_ref=source_artifact_ref,
        source_refs=unique_source_refs,
        required_eval_suites=required_suites,
        security_sensitive=candidate_type in _SECURITY_SENSITIVE_CANDIDATE_TYPES,
        requires_benchmark=True,
        requires_operator_approval=True,
        metadata={
            **_as_dict(payload.get("metadata")),
            "source_requires_benchmark": bool(payload.get("requires_benchmark", True)),
            "source_requires_approval": bool(
                payload.get(
                    "requires_operator_approval",
                    payload.get("requires_approval", True),
                )
            ),
        },
    )


def promotion_candidates_from_mission_review(
    mission_review: dict[str, Any],
) -> list[MissionPromotionCandidate]:
    """Read and normalize improvement candidates from a mission_review artifact."""

    review = _as_dict(mission_review)
    return [
        normalize_promotion_candidate(_as_dict(candidate), mission_review=review)
        for candidate in _as_list(review.get("improvement_candidates"))
    ]


def _normalize_eval_result(result: MissionEvalResult | dict[str, Any]) -> MissionEvalResult:
    if isinstance(result, MissionEvalResult):
        return result
    return MissionEvalResult.model_validate(result)


def _eval_result_or_run(
    *,
    suite_id: str,
    artifacts: dict[str, Any] | None,
    result: MissionEvalResult | dict[str, Any] | None,
    subject_id: str,
) -> MissionEvalResult:
    if result is not None:
        normalized = _normalize_eval_result(result)
        if normalized.suite_id != suite_id:
            raise MissionPromotionError(
                f"eval result suite mismatch: expected {suite_id}, got {normalized.suite_id}"
            )
        return normalized
    if artifacts is None:
        raise MissionPromotionError(
            f"{suite_id} eval requires either artifacts or a precomputed result"
        )
    return run_mission_eval_suite(suite_id, artifacts, subject_id=subject_id)


def _recommendation(
    gate_payload: dict[str, Any],
    *,
    security_eval_result: MissionEvalResult | None,
    unevaluated_required_suites: list[str],
) -> str:
    if unevaluated_required_suites:
        return "blocked_by_missing_required_eval"
    if security_eval_result is not None and not security_eval_result.passed:
        return "blocked_by_security_eval"
    if not bool(gate_payload.get("passed")):
        return "blocked_by_regression_gate"
    return "pending_operator_approval"


def build_promotion_package(
    candidate: MissionPromotionCandidate | dict[str, Any],
    *,
    baseline_artifacts: dict[str, Any] | None = None,
    candidate_artifacts: dict[str, Any] | None = None,
    baseline_eval_result: MissionEvalResult | dict[str, Any] | None = None,
    candidate_eval_result: MissionEvalResult | dict[str, Any] | None = None,
    security_eval_result: MissionEvalResult | dict[str, Any] | None = None,
    eval_suite_id: str | None = None,
    created_at: datetime | None = None,
) -> MissionPromotionPackage:
    """Build one approval-pending promotion package without promoting anything."""

    normalized_candidate = normalize_promotion_candidate(candidate)
    required_suites = list(normalized_candidate.required_eval_suites)
    selected_suite_id = str(eval_suite_id or required_suites[0]).strip()
    if selected_suite_id not in required_suites:
        raise MissionPromotionError(
            f"eval suite {selected_suite_id} is not required for "
            f"{normalized_candidate.candidate_type.value}"
        )
    baseline = _eval_result_or_run(
        suite_id=selected_suite_id,
        artifacts=baseline_artifacts,
        result=baseline_eval_result,
        subject_id=f"{normalized_candidate.candidate_id}:baseline",
    )
    candidate_result = _eval_result_or_run(
        suite_id=selected_suite_id,
        artifacts=candidate_artifacts,
        result=candidate_eval_result,
        subject_id=f"{normalized_candidate.candidate_id}:candidate",
    )
    gate = compare_mission_eval_results(
        baseline,
        candidate_result,
        requires_operator_approval=True,
    )

    security_result: MissionEvalResult | None = None
    if normalized_candidate.security_sensitive:
        security_suite_id = "approval_required_action"
        security_result = _eval_result_or_run(
            suite_id=security_suite_id,
            artifacts=candidate_artifacts,
            result=security_eval_result,
            subject_id=f"{normalized_candidate.candidate_id}:security",
        )

    evaluated_suite_ids = [selected_suite_id]
    if security_result is not None:
        evaluated_suite_ids.append(security_result.suite_id)
    evaluated_suite_ids = list(dict.fromkeys(evaluated_suite_ids))
    unevaluated_required_suites = [
        suite_id for suite_id in required_suites if suite_id not in evaluated_suite_ids
    ]

    gate_payload = gate.model_dump(mode="json")
    if unevaluated_required_suites:
        gate_payload["passed"] = False
        gate_payload["blocked_reasons"] = list(
            dict.fromkeys(
                list(gate_payload.get("blocked_reasons") or [])
                + [
                    f"missing_required_eval:{suite_id}"
                    for suite_id in unevaluated_required_suites
                ]
            )
        )
    if security_result is not None and not security_result.passed:
        gate_payload["passed"] = False
        gate_payload["blocked_reasons"] = list(
            dict.fromkeys(
                list(gate_payload.get("blocked_reasons") or [])
                + ["security_eval_failed"]
            )
        )
    recommendation = _recommendation(
        gate_payload,
        security_eval_result=security_result,
        unevaluated_required_suites=unevaluated_required_suites,
    )

    return MissionPromotionPackage(
        package_id=f"{normalized_candidate.candidate_id}:promotion_package",
        candidate_id=normalized_candidate.candidate_id,
        candidate_type=normalized_candidate.candidate_type,
        source_mission_task_id=normalized_candidate.source_mission_task_id,
        source_refs=normalized_candidate.source_refs,
        required_eval_suites=required_suites,
        evaluated_suite_ids=evaluated_suite_ids,
        unevaluated_required_suites=unevaluated_required_suites,
        eval_suite_id=selected_suite_id,
        baseline_eval_result=baseline.model_dump(mode="json"),
        candidate_eval_result=candidate_result.model_dump(mode="json"),
        regression_gate_result=gate_payload,
        security_eval_result=(
            security_result.model_dump(mode="json") if security_result else None
        ),
        recommendation=recommendation,
        approval_status=PROMOTION_APPROVAL_STATUS_PENDING,
        requires_operator_approval=True,
        created_at=created_at or _utc_now(),
        metadata={
            "security_sensitive": normalized_candidate.security_sensitive,
            "requires_benchmark": normalized_candidate.requires_benchmark,
            "evaluated_suite_ids": evaluated_suite_ids,
            "unevaluated_required_suites": unevaluated_required_suites,
            "source_candidate": normalized_candidate.model_dump(mode="json"),
        },
    )


def build_promotion_packages_from_mission_review(
    mission_review: dict[str, Any],
    *,
    baseline_artifacts: dict[str, Any] | None = None,
    candidate_artifacts: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> list[MissionPromotionPackage]:
    """Build promotion packages for every improvement candidate in a mission review."""

    return [
        build_promotion_package(
            candidate,
            baseline_artifacts=baseline_artifacts,
            candidate_artifacts=candidate_artifacts,
            created_at=created_at,
        )
        for candidate in promotion_candidates_from_mission_review(mission_review)
    ]
