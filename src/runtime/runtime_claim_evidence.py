"""Fail-closed helpers for runtime claim evidence.

Artifact materialization is not runtime execution. These helpers keep that
boundary explicit by splitting authority/runtime booleans into artifact and
runtime phases and by requiring invocation evidence for any runtime phase.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Mapping, Sequence


RUNTIME_INVOCATION_EVIDENCE_SCHEMA_VERSION = "runtime_invocation_evidence.v1"
RUNTIME_CLAIM_VALIDATION_SCHEMA_VERSION = "runtime_claim_two_phase_validation.v1"

ALLOWED_RUNTIME_INVOCATION_KINDS = frozenset(
    {
        "subprocess",
        "docker_exec",
        "mavlink",
        "gz_topic",
        "http_loopback",
    }
)
DISPATCH_RUNTIME_INVOCATION_KINDS = frozenset(
    {
        "subprocess",
        "docker_exec",
        "mavlink",
        "gz_topic",
    }
)
AUTHORITY_RUNTIME_CLAIM_KEYS: tuple[str, ...] = (
    "dispatch_executed",
    "outcome_observed",
    "verified_dispatch_execution",
    "agent_execution_started",
    "knowledge_index_updated",
    "policy_update_applied",
    "automatic_recovery_rule_created",
    "dispatch_authority_created",
    "operator_approved",
    "deterministic_gate_passed",
    "supervisor_running",
    "process_materialized",
    "route_invocation_observed",
    "delivery_completion_claimed",
    "llm_judgment_used_in_gate",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RuntimeClaimValidationError(ValueError):
    """Raised when an artifact tries to promote artifact facts to runtime facts."""


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_iso8601(value: Any, *, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise RuntimeClaimValidationError(f"{field}_required")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeClaimValidationError(f"{field}_invalid_iso8601") from exc


def _validate_sha256(value: Any, *, field: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise RuntimeClaimValidationError(f"{field}_invalid_sha256")


def validate_runtime_invocation_evidence(evidence: Any) -> dict[str, Any]:
    """Return normalized runtime invocation evidence or raise fail-closed."""

    payload = dict(_as_mapping(evidence))
    if not payload:
        raise RuntimeClaimValidationError("runtime_invocation_evidence_required")
    if payload.get("schema_version") != RUNTIME_INVOCATION_EVIDENCE_SCHEMA_VERSION:
        raise RuntimeClaimValidationError("runtime_invocation_evidence_schema_version_invalid")
    invocation_kind = payload.get("invocation_kind")
    if invocation_kind not in ALLOWED_RUNTIME_INVOCATION_KINDS:
        raise RuntimeClaimValidationError("runtime_invocation_evidence_invocation_kind_invalid")
    if not isinstance(payload.get("invocation_target"), str) or not payload["invocation_target"]:
        raise RuntimeClaimValidationError("runtime_invocation_evidence_invocation_target_required")
    _parse_iso8601(payload.get("invocation_started_at"), field="invocation_started_at")
    _parse_iso8601(payload.get("invocation_completed_at"), field="invocation_completed_at")
    _validate_sha256(
        payload.get("invocation_stdout_sha256"),
        field="invocation_stdout_sha256",
    )
    _validate_sha256(
        payload.get("invocation_stderr_sha256"),
        field="invocation_stderr_sha256",
    )
    exit_code = payload.get("invocation_exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise RuntimeClaimValidationError("runtime_invocation_evidence_invocation_exit_code_invalid")
    return payload


def normalize_runtime_claims(
    payload: Mapping[str, Any],
    *,
    claim_keys: Sequence[str] = AUTHORITY_RUNTIME_CLAIM_KEYS,
    progress_key: str = "progress_counted",
    validate_progress: bool = True,
) -> dict[str, Any]:
    """Split legacy booleans into `*_in_artifact` and `*_in_runtime`.

    Legacy `foo=true` is preserved as `foo_in_artifact=true`. Runtime promotion
    only happens when `foo_in_runtime=true` is already present and valid runtime
    invocation evidence is attached.
    """

    normalized = dict(payload)
    evidence_present = "runtime_invocation_evidence" in normalized
    evidence: dict[str, Any] | None = None
    if evidence_present:
        evidence = validate_runtime_invocation_evidence(
            normalized.get("runtime_invocation_evidence")
        )
        normalized["runtime_invocation_evidence"] = evidence

    artifact_claims: list[str] = []
    runtime_claims: list[str] = []
    for key in claim_keys:
        artifact_key = f"{key}_in_artifact"
        runtime_key = f"{key}_in_runtime"
        artifact_value = normalized.get(artifact_key) is True or normalized.get(key) is True
        runtime_value = normalized.get(runtime_key) is True
        if runtime_value and evidence is None:
            raise RuntimeClaimValidationError(f"{runtime_key}_requires_runtime_invocation_evidence")
        if runtime_value and key == "dispatch_executed":
            invocation_kind = str(evidence.get("invocation_kind") if evidence else "")
            if invocation_kind not in DISPATCH_RUNTIME_INVOCATION_KINDS:
                raise RuntimeClaimValidationError(
                    "dispatch_executed_in_runtime_requires_subprocess_docker_mavlink_or_gz_evidence"
                )
        normalized[artifact_key] = artifact_value
        normalized[runtime_key] = runtime_value if evidence is not None else False
        if normalized[artifact_key]:
            artifact_claims.append(key)
        if normalized[runtime_key]:
            runtime_claims.append(key)

    if (
        validate_progress
        and normalized.get(progress_key) is True
        and artifact_claims
        and not runtime_claims
    ):
        raise RuntimeClaimValidationError("artifact_only_runtime_claim_cannot_count_progress")

    normalized["runtime_claim_validation"] = {
        "schema_version": RUNTIME_CLAIM_VALIDATION_SCHEMA_VERSION,
        "runtime_invocation_evidence_present": evidence_present,
        "runtime_invocation_evidence_valid": evidence is not None,
        "artifact_claims": artifact_claims,
        "runtime_claims": runtime_claims,
        "progress_key": progress_key,
        "progress_counted": normalized.get(progress_key) is True,
    }
    return normalized


def runtime_claim_validation_summary(
    payload: Mapping[str, Any],
    *,
    claim_keys: Sequence[str] = AUTHORITY_RUNTIME_CLAIM_KEYS,
) -> dict[str, Any]:
    """Build a validation summary suitable for UI/API authority boundaries."""

    normalized = normalize_runtime_claims(
        payload,
        claim_keys=claim_keys,
        validate_progress=False,
    )
    validation = _as_mapping(normalized.get("runtime_claim_validation"))
    return {
        "schema_version": RUNTIME_CLAIM_VALIDATION_SCHEMA_VERSION,
        "runtime_invocation_evidence_present": validation.get(
            "runtime_invocation_evidence_present"
        )
        is True,
        "runtime_invocation_evidence_valid": validation.get(
            "runtime_invocation_evidence_valid"
        )
        is True,
        "artifact_claims": list(validation.get("artifact_claims") or []),
        "runtime_claims": list(validation.get("runtime_claims") or []),
        "artifact_only": bool(validation.get("artifact_claims")) and not bool(
            validation.get("runtime_claims")
        ),
    }


__all__ = [
    "ALLOWED_RUNTIME_INVOCATION_KINDS",
    "AUTHORITY_RUNTIME_CLAIM_KEYS",
    "DISPATCH_RUNTIME_INVOCATION_KINDS",
    "RUNTIME_CLAIM_VALIDATION_SCHEMA_VERSION",
    "RUNTIME_INVOCATION_EVIDENCE_SCHEMA_VERSION",
    "RuntimeClaimValidationError",
    "normalize_runtime_claims",
    "runtime_claim_validation_summary",
    "validate_runtime_invocation_evidence",
]
