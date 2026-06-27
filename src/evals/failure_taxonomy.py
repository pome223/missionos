"""Phase 0 failure taxonomy for browser-first trajectory evals.

Classification follows a small lifecycle that stays stable across report,
promotion, and reuse surfaces:

- verifier emits a preliminary bucket from the live execution result
- replay analysis normalizes that bucket against the persisted trajectory
- operator override can replace the normalized bucket before promotion or reuse
"""

from __future__ import annotations

from typing import Any


PHASE0_FAILURE_BUCKETS = (
    "weak_evidence",
    "focus_mismatch",
    "wrong_surface",
    "target_context_mismatch",
    "unknown",
)


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _failed_checks(verification: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(verification, dict):
        return {}
    failed: dict[str, dict[str, Any]] = {}
    for item in verification.get("checks") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        if not item.get("passed"):
            failed[name] = item
    return failed


def _classify_from_verification_checks(trajectory: dict[str, Any]) -> str | None:
    verification = trajectory.get("verification")
    if not isinstance(verification, dict):
        return None

    failed = _failed_checks(verification)
    if "frontmost_app" in failed:
        return "focus_mismatch"
    if "url_contains" in failed or "window_title_contains" in failed:
        return "target_context_mismatch"
    if "surface" in failed or "final_surface" in failed:
        return "wrong_surface"
    return None


def _classify_from_surface(trajectory: dict[str, Any]) -> str | None:
    observation = trajectory.get("observation")
    if not isinstance(observation, dict):
        observation = {}
    preferred_surface = _normalized_text(observation.get("preferred_surface"))
    final_surface = _normalized_text(trajectory.get("final_surface"))
    if preferred_surface and final_surface and preferred_surface != final_surface:
        return "wrong_surface"
    return None


def _classify_from_attempts(trajectory: dict[str, Any]) -> str | None:
    attempts = trajectory.get("attempts") or []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        result = attempt.get("result")
        error = _normalized_text(result.get("error")) if isinstance(result, dict) else ""
        if not error:
            continue
        if any(token in error for token in ("frontmost", "focus", "inactive")):
            return "focus_mismatch"
        if any(
            token in error
            for token in (
                "wrong surface",
                "surface mismatch",
                "preferred surface",
                "final surface",
            )
        ):
            return "wrong_surface"
        if any(token in error for token in ("tab", "window", "url")):
            return "target_context_mismatch"
    return None


def _classify_from_verification_status(trajectory: dict[str, Any]) -> str | None:
    verification = trajectory.get("verification")
    if not isinstance(verification, dict):
        return None

    failed = _failed_checks(verification)
    if "text_contains" in failed or "text_not_contains" in failed:
        return "weak_evidence"

    status = _normalized_text(verification.get("status"))
    if status in {"fail", "partial_pass"}:
        return "weak_evidence"
    return None


def _classify_failure(trajectory: dict[str, Any]) -> str | None:
    if str(trajectory.get("status") or "") in {"success", "recovered"}:
        return None

    return (
        _classify_from_verification_checks(trajectory)
        or _classify_from_surface(trajectory)
        or _classify_from_attempts(trajectory)
        or _classify_from_verification_status(trajectory)
        or "unknown"
    )


def normalize_trajectory_failure(
    trajectory: dict[str, Any],
    *,
    classified_by: str | None = None,
) -> dict[str, Any]:
    """Return Phase 0 failure metadata with preliminary/normalized provenance."""

    status = str(trajectory.get("status") or "")
    operator_override = str(trajectory.get("operator_override") or "").strip() or None
    existing_preliminary = str(trajectory.get("preliminary_failure_type") or "").strip() or None
    existing_normalized = str(trajectory.get("normalized_failure_type") or "").strip() or None
    existing_sources = [
        str(item).strip()
        for item in (trajectory.get("classified_by") or [])
        if str(item).strip()
    ]

    if status in {"success", "recovered"}:
        return {
            "preliminary_failure_type": None,
            "normalized_failure_type": None,
            "failure_type": None,
            "classified_by": existing_sources,
            "operator_override": operator_override,
        }

    inferred = _classify_failure(trajectory)
    preliminary = existing_preliminary or inferred
    normalized = operator_override or existing_normalized or preliminary

    classified_sources = list(existing_sources)
    if classified_by and classified_by not in classified_sources:
        classified_sources.append(classified_by)
    if operator_override and "operator" not in classified_sources:
        classified_sources.append("operator")

    return {
        "preliminary_failure_type": preliminary,
        "normalized_failure_type": normalized,
        "failure_type": normalized,
        "classified_by": classified_sources,
        "operator_override": operator_override,
    }
