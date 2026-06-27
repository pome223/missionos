"""Runtime subprocess bridge for MissionOS claim backfills."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from src.runtime.runtime_claim_evidence import RUNTIME_INVOCATION_EVIDENCE_SCHEMA_VERSION


DEFAULT_MISSIONOS_RUNTIME_TIMEOUT_SECONDS = 120


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_last_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value
    decoder = json.JSONDecoder()
    latest: dict[str, Any] = {}
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            latest = value
    return latest


def invoke_missionos_subprocess(
    command: Sequence[str],
    *,
    invocation_target: str,
    artifact_dir: Path,
    backend_target: str,
    timeout_seconds: int = DEFAULT_MISSIONOS_RUNTIME_TIMEOUT_SECONDS,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Invoke a bounded MissionOS worker and return runtime evidence."""

    artifact_dir.mkdir(parents=True, exist_ok=True)
    runtime_env = os.environ.copy()
    if env:
        runtime_env.update(dict(env))
    command_list = [str(part) for part in command]
    command_argv_json = json.dumps(command_list, sort_keys=True)
    started_at = _utc_now()
    process = subprocess.Popen(
        command_list,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=runtime_env,
        cwd=str(cwd) if cwd else None,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate(timeout=30)
        stderr = f"{stderr}\nmissionos subprocess timeout"
    completed_at = _utc_now()
    exit_code = int(process.returncode if process.returncode is not None else -1)
    runtime_completed = bool(exit_code == 0 and not timed_out)
    stdout_path = artifact_dir / "runtime_stdout.txt"
    stderr_path = artifact_dir / "runtime_stderr.txt"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    evidence = {
        "schema_version": RUNTIME_INVOCATION_EVIDENCE_SCHEMA_VERSION,
        "missionos_schema_version": "missionos_runtime_invocation_evidence.v1",
        "invocation_kind": "subprocess",
        "invocation_target": invocation_target,
        "invocation_started_at": started_at,
        "invocation_completed_at": completed_at,
        "invocation_stdout_sha256": _sha256_text(stdout),
        "invocation_stderr_sha256": _sha256_text(stderr),
        "invocation_exit_code": exit_code,
        "process_pid": process.pid,
        "docker_container_id": "",
        "command_argv": command_list,
        "command_argv_sha256": _sha256_text(command_argv_json),
        "artifact_dir": str(artifact_dir),
        "backend_target": backend_target,
        "opt_in_env": True,
        # Aliases used by MissionOS-specific artifacts. The validator keeps
        # using the stable invocation_* fields above.
        "started_at": started_at,
        "completed_at": completed_at,
        "stdout_sha256": _sha256_text(stdout),
        "stderr_sha256": _sha256_text(stderr),
        "exit_code": exit_code,
        "stdout_artifact_path": str(stdout_path),
        "stderr_artifact_path": str(stderr_path),
    }
    evidence_path = artifact_dir / "runtime_invocation_evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "runtime_invoked": runtime_completed,
        "runtime_process_started": True,
        "runtime_timed_out": timed_out,
        "runtime_invocation_evidence": evidence,
        "runtime_stdout_tail": stdout[-4000:],
        "runtime_stderr_tail": stderr[-4000:],
        "runtime_stdout_json": _parse_last_json_object(stdout),
        "runtime_artifact_dir": str(artifact_dir),
        "runtime_evidence_artifact_path": str(evidence_path),
    }


__all__ = ["DEFAULT_MISSIONOS_RUNTIME_TIMEOUT_SECONDS", "invoke_missionos_subprocess"]
