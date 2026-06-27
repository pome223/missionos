"""Failure classification for persisted control-loop task results.

This maps live control-loop outcomes onto the Phase 0 recovery buckets used by
the durable scheduler worker. Unlike trajectory replay classification, the
inputs here are the task result payloads emitted by the production control loop.
"""

from __future__ import annotations

from typing import Any


_INSUFFICIENT_EVIDENCE_FAILURES = {
    "insufficient_evidence",
    "weak_evidence",
}


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _from_verification_result(
    *,
    verification_status: str | None,
    verification_report: dict[str, Any] | None,
) -> str | None:
    status = _normalized_text(verification_status)
    report = verification_report if isinstance(verification_report, dict) else {}
    report_failure_type = _normalized_text(report.get("failure_type"))
    if report_failure_type in _INSUFFICIENT_EVIDENCE_FAILURES:
        return "weak_evidence"
    if status == "partial_pass":
        return "weak_evidence"
    return None


def _from_text(*, final_text: str | None, error: str | None) -> str | None:
    text = " ".join(
        part for part in (str(final_text or "").strip(), str(error or "").strip()) if part
    ).lower()
    if not text:
        return None
    if "timeout" in text:
        return "tool_timeout"
    if any(token in text for token in ("frontmost", "focus", "inactive")):
        return "focus_mismatch"
    if any(
        token in text
        for token in (
            "wrong surface",
            "surface mismatch",
            "preferred surface",
            "final surface",
        )
    ):
        return "wrong_surface"
    if any(token in text for token in ("current tab", "current_tab", "window", "url", "tab")):
        return "target_context_mismatch"
    return None


def classify_control_loop_failure(
    *,
    success: bool,
    needs_human: bool,
    final_text: str | None,
    verification_status: str | None = None,
    verification_report: dict[str, Any] | None = None,
    error: str | None = None,
    existing_failure_type: str | None = None,
) -> dict[str, Any]:
    normalized_existing = str(existing_failure_type or "").strip() or None
    verification_failure = _from_verification_result(
        verification_status=verification_status,
        verification_report=verification_report,
    )
    if success and not normalized_existing and not needs_human and not verification_failure:
        return {
            "preliminary_failure_type": None,
            "normalized_failure_type": None,
            "failure_type": None,
            "classified_by": ["control_loop_runtime"],
            "operator_override": None,
        }

    inferred = (
        normalized_existing
        or ("policy_blocked" if needs_human else None)
        or verification_failure
        or (None if success else _from_text(final_text=final_text, error=error))
        or "unknown"
    )
    return {
        "preliminary_failure_type": inferred,
        "normalized_failure_type": inferred,
        "failure_type": inferred,
        "classified_by": ["control_loop_runtime"],
        "operator_override": None,
    }
