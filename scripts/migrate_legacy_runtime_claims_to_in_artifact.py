#!/usr/bin/env python3
"""Migrate legacy runtime-like booleans into two-phase claim fields."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


_RUNTIME_CLAIM_EVIDENCE_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "runtime" / "runtime_claim_evidence.py"
)
_RUNTIME_CLAIM_EVIDENCE_SPEC = importlib.util.spec_from_file_location(
    "runtime_claim_evidence_for_migration",
    _RUNTIME_CLAIM_EVIDENCE_PATH,
)
if _RUNTIME_CLAIM_EVIDENCE_SPEC is None or _RUNTIME_CLAIM_EVIDENCE_SPEC.loader is None:
    raise RuntimeError("runtime_claim_evidence_module_unavailable")
_RUNTIME_CLAIM_EVIDENCE = importlib.util.module_from_spec(_RUNTIME_CLAIM_EVIDENCE_SPEC)
_RUNTIME_CLAIM_EVIDENCE_SPEC.loader.exec_module(_RUNTIME_CLAIM_EVIDENCE)

AUTHORITY_RUNTIME_CLAIM_KEYS = _RUNTIME_CLAIM_EVIDENCE.AUTHORITY_RUNTIME_CLAIM_KEYS
RuntimeClaimValidationError = _RUNTIME_CLAIM_EVIDENCE.RuntimeClaimValidationError
normalize_runtime_claims = _RUNTIME_CLAIM_EVIDENCE.normalize_runtime_claims


MIGRATION_SCHEMA_VERSION = "legacy_runtime_claim_migration.v1"
DRY_RUN_SCHEMA_VERSION = "legacy_runtime_claim_migration_dry_run.v1"
CHECK_SCHEMA_VERSION = "legacy_runtime_claim_check.v1"
DEFAULT_ROOT = Path("output/mission_designer_behavior_delta_audits")
MIGRATION_LOG_PREFIX = "artifacts/migrations/"


def _utc_now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _script_content_sha256() -> str:
    return _file_sha256(Path(__file__))


def _relative_path(path: Path, *, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _iter_json_paths(roots: Sequence[Path]) -> Iterable[Path]:
    for root in roots:
        if root.is_file():
            if root.suffix == ".json":
                yield root
            continue
        if not root.exists():
            continue
        yield from root.rglob("*.json")


def _legacy_truthy_keys(payload: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        key for key in AUTHORITY_RUNTIME_CLAIM_KEYS if payload.get(key) is True
    )


def _has_suffixed_runtime_claim(payload: Mapping[str, Any]) -> bool:
    return any(
        payload.get(f"{key}_in_artifact") is True
        or payload.get(f"{key}_in_runtime") is True
        for key in AUTHORITY_RUNTIME_CLAIM_KEYS
    )


def _legacy_truthy_claims(payload: Any, *, prefix: str = "") -> tuple[str, ...]:
    claims: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key in AUTHORITY_RUNTIME_CLAIM_KEYS and value is True:
                claims.append(path)
            claims.extend(_legacy_truthy_claims(value, prefix=path))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            claims.extend(_legacy_truthy_claims(item, prefix=f"{prefix}[{index}]"))
    return tuple(claims)


def _remove_legacy_truthy_keys(
    payload: dict[str, Any],
    legacy_keys: Sequence[str],
) -> dict[str, Any]:
    cleaned = dict(payload)
    for key in legacy_keys:
        if cleaned.get(key) is True:
            cleaned.pop(key)
    return cleaned


def migrate_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return a migrated payload and whether it changed."""

    original = dict(payload)
    legacy_keys = _legacy_truthy_keys(payload)
    try:
        migrated = normalize_runtime_claims(payload)
    except RuntimeClaimValidationError as exc:
        if str(exc) != "artifact_only_runtime_claim_cannot_count_progress":
            raise
        payload = dict(payload)
        payload["progress_counted"] = False
        migrated = normalize_runtime_claims(payload)
    migrated = _remove_legacy_truthy_keys(migrated, legacy_keys)
    return migrated, migrated != original


def migrate_paths(
    roots: tuple[Path, ...],
    *,
    write: bool = False,
    generated_at: str | None = None,
    repo_root: Path | None = None,
    migration_script_commit_sha: str | None = None,
    migration_script_sha256: str | None = None,
) -> dict[str, Any]:
    """Scan roots, optionally rewrite legacy truthy claim fields, and report."""

    repo_root = repo_root or Path.cwd()
    generated_at = generated_at or _utc_now_iso8601()
    migration_script_commit_sha = migration_script_commit_sha or "unknown"
    migration_script_sha256 = migration_script_sha256 or _script_content_sha256()
    rewritten_files: list[dict[str, Any]] = []
    skipped_files: list[dict[str, Any]] = []
    key_counts: Counter[str] = Counter()
    total_candidates = 0

    for path in _iter_json_paths(roots):
        payload = _read_json(path)
        relative = _relative_path(path, repo_root=repo_root)
        if payload is None:
            skipped_files.append({"file_path": relative, "reason": "malformed"})
            continue
        if not isinstance(payload, dict):
            skipped_files.append({"file_path": relative, "reason": "out_of_scope"})
            continue
        legacy_keys = _legacy_truthy_keys(payload)
        if not legacy_keys:
            if _has_suffixed_runtime_claim(payload):
                skipped_files.append(
                    {"file_path": relative, "reason": "already_migrated"}
                )
            continue
        total_candidates += 1
        key_counts.update(legacy_keys)
        before_sha256 = _file_sha256(path)
        migrated, did_change = migrate_payload(payload)
        after_sha256 = _sha256_bytes(_canonical_json_bytes(migrated))
        if not did_change:
            skipped_files.append({"file_path": relative, "reason": "already_migrated"})
            continue
        rewritten_files.append(
            {
                "file_path": relative,
                "before_sha256": before_sha256,
                "after_sha256": after_sha256,
                "touched_keys": {key: f"{key}_in_artifact" for key in legacy_keys},
                "migration_timestamp_utc": generated_at,
                "migration_script_commit_sha": migration_script_commit_sha,
            }
        )
        if write:
            _write_json(path, migrated)

    dry_run = not write
    return {
        "schema_version": DRY_RUN_SCHEMA_VERSION if dry_run else MIGRATION_SCHEMA_VERSION,
        "generated_at": generated_at,
        "migration_script_commit_sha": migration_script_commit_sha,
        "migration_script_sha256": migration_script_sha256,
        "dry_run": dry_run,
        "total_candidates": total_candidates,
        "total_rewritten": 0 if dry_run else len(rewritten_files),
        "expected_rewrite_count": len(rewritten_files),
        "total_skipped": len(skipped_files),
        "legacy_truthy_key_counts": dict(sorted(key_counts.items())),
        "rewritten_files": rewritten_files,
        "skipped_files": skipped_files,
        "drone_behavior_delta": {
            "status": "not_observed",
            "drone_physics_affected": False,
        },
        "progress_counted": False,
    }


def _read_changed_paths(path: Path) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _is_migration_log_path(changed_path: str) -> bool:
    normalized = changed_path.replace("\\", "/")
    return normalized.startswith(MIGRATION_LOG_PREFIX)


def check_changed_paths_for_legacy_truthy_claims(
    changed_paths: Sequence[str],
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Return a fail-closed report for changed JSON files with legacy truthy claims."""

    repo_root = repo_root or Path.cwd()
    violations: list[dict[str, Any]] = []
    checked_paths: list[str] = []
    skipped_paths: list[dict[str, str]] = []
    for changed_path in changed_paths:
        normalized = changed_path.replace("\\", "/")
        if not normalized.endswith(".json"):
            continue
        if _is_migration_log_path(normalized):
            skipped_paths.append({"file_path": normalized, "reason": "migration_log"})
            continue
        path = repo_root / normalized
        if not path.exists():
            skipped_paths.append({"file_path": normalized, "reason": "missing"})
            continue
        payload = _read_json(path)
        checked_paths.append(normalized)
        claims = _legacy_truthy_claims(payload)
        if claims:
            violations.append(
                {
                    "file_path": normalized,
                    "legacy_truthy_claims": list(claims),
                }
            )
    return {
        "schema_version": CHECK_SCHEMA_VERSION,
        "checked_path_count": len(checked_paths),
        "checked_paths": checked_paths,
        "skipped_paths": skipped_paths,
        "violation_count": len(violations),
        "violations": violations,
        "passed": not violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[DEFAULT_ROOT],
    )
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--log-path", type=Path)
    parser.add_argument("--generated-at", type=str)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--migration-script-commit-sha", type=str)
    parser.add_argument("--migration-script-sha256", type=str)
    parser.add_argument(
        "--check-changed",
        type=Path,
        help="Scan newline-delimited changed paths for new legacy truthy claim fields.",
    )
    args = parser.parse_args()

    if args.check_changed is not None:
        report = check_changed_paths_for_legacy_truthy_claims(
            _read_changed_paths(args.check_changed),
            repo_root=args.repo_root,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["passed"] else 1

    report = migrate_paths(
        tuple(args.paths),
        write=args.write,
        generated_at=args.generated_at,
        repo_root=args.repo_root,
        migration_script_commit_sha=args.migration_script_commit_sha,
        migration_script_sha256=args.migration_script_sha256,
    )
    if args.log_path is not None:
        _write_json(args.log_path, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
