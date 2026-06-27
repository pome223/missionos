"""Verifier invariance helpers for advisory mission lessons.

Lessons may be read by scenario proposal code, but verifier outputs must remain
independent of the lesson registry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from src.runtime.advisory_mission_memory import (
    DEFAULT_VERIFIER_PREDICATE_MODULE_PATHS,
    AdvisoryMissionMemoryError,
    current_verifier_contract,
)

_VOLATILE_KEYS = frozenset(
    {
        "created_at",
        "updated_at",
        "started_at",
        "ended_at",
        "evaluated_at",
        "verified_at",
        "observed_at",
        "captured_at",
        "finished_at",
        "task_id",
        "run_id",
        "owner_session_id",
        "owner_user_id",
    }
)
_UUID_OR_TASK_FRAGMENT = re.compile(
    r"(task|run)_[0-9a-f]{8,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_ACTIVE_LESSON_REGISTRY: ContextVar[tuple[Mapping[str, Any], ...]] = ContextVar(
    "active_advisory_lesson_registry",
    default=(),
)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _canonicalize(value: Any) -> Any:
    value = _jsonable(value)
    if isinstance(value, Mapping):
        return {
            key: _canonicalize(item)
            for key, item in sorted(value.items())
            if key not in _VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, str):
        return _UUID_OR_TASK_FRAGMENT.sub("<volatile-id>", value)
    return value


def canonical_verifier_json(value: Any) -> str:
    """Return deterministic verifier output JSON after removing volatile fields."""

    return json.dumps(
        _canonicalize(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_verifier_digest(value: Any) -> str:
    return sha256(canonical_verifier_json(value).encode("utf-8")).hexdigest()


def validate_verifier_contract_ref_is_current(
    verifier_contract_ref: str,
    *,
    root: Path | None = None,
) -> None:
    if not verifier_contract_ref.startswith("verifier_contract:"):
        raise AdvisoryMissionMemoryError("verifier_contract_ref_prefix_invalid")
    expected = current_verifier_contract(root=root)
    expected_ref = f"verifier_contract:{expected.contract_id}"
    if verifier_contract_ref != expected_ref:
        raise AdvisoryMissionMemoryError("verifier_contract_ref_not_current")


@contextmanager
def lesson_registry(registry: Sequence[Mapping[str, Any]] | None = None):
    """Context helper documenting the registry supplied to verifier tests."""

    token = _ACTIVE_LESSON_REGISTRY.set(tuple(registry or ()))
    try:
        yield _ACTIVE_LESSON_REGISTRY.get()
    finally:
        _ACTIVE_LESSON_REGISTRY.reset(token)


def current_lesson_registry() -> tuple[Mapping[str, Any], ...]:
    return _ACTIVE_LESSON_REGISTRY.get()


def assert_verifier_ignores_lessons(
    *,
    corpus: Sequence[Mapping[str, Any]],
    verifier_runner: Callable[[Mapping[str, Any], Sequence[Mapping[str, Any]]], Any],
    empty_lesson_registry: Sequence[Mapping[str, Any]] = (),
    full_lesson_registry: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    if not full_lesson_registry:
        raise AdvisoryMissionMemoryError("lesson_invariance_full_registry_empty")
    evidence: list[dict[str, str]] = []
    for case in corpus:
        case_id = str(case.get("id") or "")
        with lesson_registry(empty_lesson_registry) as empty:
            output_empty = verifier_runner(case, empty)
        with lesson_registry(full_lesson_registry) as full:
            output_full = verifier_runner(case, full)
        digest_empty = canonical_verifier_digest(output_empty)
        digest_full = canonical_verifier_digest(output_full)
        if digest_empty != digest_full:
            raise AdvisoryMissionMemoryError(
                f"verifier_output_diverged_with_lesson_registry:{case_id}"
            )
        evidence.append(
            {
                "case_id": case_id,
                "canonical_digest": digest_empty,
                "case_kind": str(case.get("kind") or ""),
            }
        )
    return evidence


def lesson_invariance_watch_paths() -> tuple[str, ...]:
    return DEFAULT_VERIFIER_PREDICATE_MODULE_PATHS


__all__ = [
    "assert_verifier_ignores_lessons",
    "canonical_verifier_digest",
    "canonical_verifier_json",
    "current_lesson_registry",
    "lesson_invariance_watch_paths",
    "lesson_registry",
    "validate_verifier_contract_ref_is_current",
]
