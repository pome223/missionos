"""Recovery outcome invariance helpers for advisory recovery context.

Advisory context may shape recovery proposals, but recovery outcomes must stay
independent of advisory context and depend only on the recovery run plus
observed facts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from src.runtime.advisory_lesson_invariance import (
    canonical_verifier_digest,
    canonical_verifier_json,
)


class RecoveryAdvisoryOutcomeInvarianceError(RuntimeError):
    """Raised when advisory context changes recovery outcome output."""


_ACTIVE_RECOVERY_ADVISORY_CONTEXT: ContextVar[tuple[Mapping[str, Any], ...]] = (
    ContextVar("active_recovery_advisory_context", default=())
)


@contextmanager
def recovery_advisory_context_registry(
    registry: Sequence[Mapping[str, Any]] | None = None,
):
    """Context helper documenting advisory context available to outcome tests."""

    token = _ACTIVE_RECOVERY_ADVISORY_CONTEXT.set(tuple(registry or ()))
    try:
        yield _ACTIVE_RECOVERY_ADVISORY_CONTEXT.get()
    finally:
        _ACTIVE_RECOVERY_ADVISORY_CONTEXT.reset(token)


def current_recovery_advisory_context() -> tuple[Mapping[str, Any], ...]:
    return _ACTIVE_RECOVERY_ADVISORY_CONTEXT.get()


def canonical_recovery_outcome_json(value: Any) -> str:
    """Return deterministic recovery outcome JSON after volatile-field stripping."""

    return canonical_verifier_json(value)


def canonical_recovery_outcome_digest(value: Any) -> str:
    return canonical_verifier_digest(value)


def assert_recovery_outcome_ignores_advisory_context(
    *,
    corpus: Sequence[Mapping[str, Any]],
    outcome_runner: Callable[[Mapping[str, Any], Sequence[Mapping[str, Any]]], Any],
    empty_advisory_context: Sequence[Mapping[str, Any]] = (),
    full_advisory_context: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    if not full_advisory_context:
        raise RecoveryAdvisoryOutcomeInvarianceError(
            "recovery_advisory_invariance_full_context_empty"
        )
    evidence: list[dict[str, str]] = []
    for case in corpus:
        case_id = str(case.get("id") or "")
        with recovery_advisory_context_registry(empty_advisory_context) as empty:
            output_empty = outcome_runner(case, empty)
        with recovery_advisory_context_registry(full_advisory_context) as full:
            output_full = outcome_runner(case, full)
        digest_empty = canonical_recovery_outcome_digest(output_empty)
        digest_full = canonical_recovery_outcome_digest(output_full)
        if digest_empty != digest_full:
            raise RecoveryAdvisoryOutcomeInvarianceError(
                f"recovery_outcome_diverged_with_advisory_context:{case_id}"
            )
        evidence.append(
            {
                "case_id": case_id,
                "case_kind": str(case.get("kind") or ""),
                "canonical_digest": digest_empty,
            }
        )
    return evidence


__all__ = [
    "RecoveryAdvisoryOutcomeInvarianceError",
    "assert_recovery_outcome_ignores_advisory_context",
    "canonical_recovery_outcome_digest",
    "canonical_recovery_outcome_json",
    "current_recovery_advisory_context",
    "recovery_advisory_context_registry",
]
