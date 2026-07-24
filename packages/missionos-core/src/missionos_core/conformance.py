"""Backend-neutral conformance-corpus runner."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


CONFORMANCE_RUN_SCHEMA_VERSION = "missionos_core_conformance_run.v1"
_AUTHORITY_OUTPUTS = (
    "llm_invoked",
    "approval_created",
    "dispatch_authority_created",
    "dispatch_request_sent",
    "physical_execution_invoked",
    "progress_claimed",
    "completion_claimed",
    "delivery_completion_claimed",
)


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def run_conformance_corpus(
    manifest_path: Path,
    *,
    execute_case: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Run sealed cases through an adapter and enforce Core-wide invariants."""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reasons: list[str] = []
    verdicts: list[dict[str, Any]] = []
    manifest_material = {
        key: value
        for key, value in manifest.items()
        if key != "manifest_sha256"
    }
    if manifest.get("manifest_sha256") != _sha256(manifest_material):
        reasons.append("manifest_hash_mismatch")
    raw_cases = manifest.get("cases")
    raw_cases = (
        raw_cases
        if isinstance(raw_cases, Sequence)
        and not isinstance(raw_cases, (str, bytes, bytearray))
        else []
    )
    root = manifest_path.parent.resolve()
    for entry in raw_cases:
        entry = dict(entry) if isinstance(entry, Mapping) else {}
        relative_path = str(entry.get("path") or "")
        relative = Path(relative_path)
        if (
            not relative.name
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            reasons.append("case_path_invalid")
            continue
        case_path = (root / relative).resolve()
        if root not in case_path.parents:
            reasons.append("case_path_escaped")
            continue
        case = json.loads(case_path.read_text(encoding="utf-8"))
        material = {key: value for key, value in case.items() if key != "case_sha256"}
        if case.get("case_sha256") != _sha256(material):
            reasons.append("case_hash_mismatch")
            continue
        if entry.get("sha256") != _sha256(case):
            reasons.append("manifest_case_hash_mismatch")
            continue
        result = dict(execute_case(case))
        output_flags = result.get("output_flags")
        output_flags = (
            dict(output_flags) if isinstance(output_flags, Mapping) else {}
        )
        authority_safe = all(output_flags.get(name) is False for name in _AUTHORITY_OUTPUTS)
        case_passed = bool(result.get("passed")) and authority_safe
        if not authority_safe:
            reasons.append("adapter_created_authority")
        if not case_passed:
            reasons.append("case_failed")
        verdicts.append(
            {
                "case_id": case.get("case_id"),
                "passed": case_passed,
                "output_flags": output_flags,
                "adapter_result": result,
            }
        )
    if len(verdicts) != len(raw_cases):
        reasons.append("case_count_mismatch")
    reasons = list(dict.fromkeys(reasons))
    return {
        "schema_version": CONFORMANCE_RUN_SCHEMA_VERSION,
        "status": "verified" if not reasons else "failed",
        "case_count": len(verdicts),
        "reasons": reasons,
        "case_verdicts": verdicts,
        "approval_created": False,
        "dispatch_authority_created": False,
        "execution_invoked": False,
        "progress_claimed": False,
        "completion_claimed": False,
    }
