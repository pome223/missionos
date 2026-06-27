#!/usr/bin/env python3
"""Run a C5b live Gateway-owned runtime probe.

C5b consumes the C5a readiness chain, invokes or consumes a live multi-condition
Mission OS supervisor runtime artifact, and records whether Gateway owns the
mission session, lifecycle, observation stream, and recovery decision loop in one
same-session SITL run. It remains PX4/Gazebo SITL-only and does not create
physical, hardware, dispatch, or delivery authority.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from scripts.audit_mission_os_multi_condition_supervisor_runtime import (
    DEFAULT_DRIFT_THRESHOLD_M,
    DEFAULT_WIND_DIRECTION_DEG,
    DEFAULT_WIND_MPS,
    TARGET_SUPERVISOR_SCOPE,
)
from scripts.build_gateway_full_runtime_readiness import (
    READINESS_STATUS_READY,
    build_gateway_full_runtime_readiness,
)
from scripts.build_gateway_mission_session import (
    GATEWAY_SESSION_STATUS_READY,
    build_gateway_mission_session,
)
from scripts.build_gateway_owned_observation_stream import (
    STREAM_STATUS_READY,
    build_gateway_owned_observation_stream,
)
from scripts.build_gateway_owned_recovery_decision_loop import (
    LOOP_STATUS_READY,
    build_gateway_owned_recovery_decision_loop,
)
from scripts.build_gateway_supervisor_lifecycle import (
    LIFECYCLE_STATUS_READY,
    build_gateway_supervisor_lifecycle,
)
from src.gateway.live_runtime_boundary import (
    GATEWAY_PROCESS_BOUNDARY_KIND_ROUTE_INVOCATION,
    GATEWAY_PROCESS_BOUNDARY_KIND_SUPERVISOR_PROCESS_PROBE,
    gateway_route_invocation_boundary_supported,
    gateway_supervisor_process_probe_boundary_supported,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "gateway_live_runtime_probe.v1"
PROBE_STATUS_OBSERVED = "full_gateway_runtime_loop_observed"
PROBE_STATUS_BLOCKED = "blocked"
LIVE_OBSERVATION_STREAM_KIND = "source_bound_gateway_live_observation_stream"
LIVE_RECOVERY_DECISION_LOOP_KIND = "source_bound_gateway_live_recovery_decision_loop"
LIVE_OBSERVATION_PROCESS_EVIDENCE_SCHEMA = (
    "gateway_live_observation_process_evidence.v1"
)
LIVE_RECOVERY_DECISION_PROCESS_EVIDENCE_SCHEMA = (
    "gateway_live_recovery_decision_process_evidence.v1"
)
LIVE_GATEWAY_PROCESS_MATERIALIZER_SCHEMA = "gateway_live_process_materializer.v1"
LIVE_GATEWAY_PROCESS_MATERIALIZER_KIND = "source_bound_gateway_live_process_materializer"
ALLOWED_GATEWAY_PROCESS_BOUNDARY_KINDS = {
    GATEWAY_PROCESS_BOUNDARY_KIND_SUPERVISOR_PROCESS_PROBE,
}
AUTHORITY_KEYS_REQUIRED_FALSE = {
    "ai_judgment_is_gate_verdict",
    "ai_judgment_created_dispatch_authority",
    "llm_gate_judge_used",
    "dispatch_authority_created",
    "created_dispatch_authority",
    "automatic_dispatch_allowed",
    "delivery_completion_claimed",
    "hardware_target_allowed",
    "physical_execution_invoked",
    "physical_form1_claimed",
    "full_gateway_runtime_loop",
    "gateway_autonomous_runtime_claimed",
}


class GatewayRuntimeProbeError(RuntimeError):
    """Raised when the live source runtime could not be produced."""


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_evidence_artifact(path_value: Any, *, source_artifact_path: Path) -> dict[str, Any]:
    if not isinstance(path_value, str) or not path_value:
        return {}
    path = Path(path_value)
    if not path.is_absolute():
        if path.parts and path.parts[0] == "output":
            path = REPO_ROOT / path
        else:
            path = source_artifact_path.parent / path
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{prefix}_{digest[:12]}"


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _source_ref(value: Any, *, prefix: str) -> bool:
    return isinstance(value, str) and value.startswith(prefix) and len(value) > len(prefix)


def _nested_authority_reasons(payload: Any, *, path: str) -> list[str]:
    reasons: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            current_path = f"{path}.{key}"
            if key in AUTHORITY_KEYS_REQUIRED_FALSE and value is not False:
                reasons.append(f"nested_authority_{current_path}_not_false")
            if isinstance(value, (dict, list)):
                reasons.extend(_nested_authority_reasons(value, path=current_path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            reasons.extend(_nested_authority_reasons(value, path=f"{path}[{index}]"))
    return reasons


def _source_loop(runtime: dict[str, Any]) -> dict[str, Any]:
    loop = runtime.get("mission_os_supervisor_recovery_loop")
    return loop if isinstance(loop, dict) else {}


def _checks(payload: dict[str, Any]) -> dict[str, Any]:
    checks = payload.get("checks")
    return checks if isinstance(checks, dict) else {}


def _observation_stream_live_kind_allowlisted(observation_stream: dict[str, Any]) -> bool:
    return observation_stream.get("observation_stream_kind") == LIVE_OBSERVATION_STREAM_KIND


def _observation_stream_live_provenance_source_bound(
    observation_stream: dict[str, Any],
    *,
    gateway_mission_session_ref: str,
    supervisor_session_ref: str,
    observation_stream_artifact_path: Path,
) -> bool:
    process_ref = observation_stream.get("gateway_live_observation_process_ref")
    evidence_ref = observation_stream.get("gateway_live_observation_process_evidence_ref")
    evidence = _read_evidence_artifact(
        observation_stream.get("gateway_live_observation_process_evidence_artifact_path"),
        source_artifact_path=observation_stream_artifact_path,
    )
    return (
        observation_stream.get("gateway_live_observation_process_source_bound") is True
        and _source_ref(process_ref, prefix="gateway_live_observation_process:")
        and _source_ref(evidence_ref, prefix="gateway_live_observation_evidence:")
        and evidence.get("schema_version") == LIVE_OBSERVATION_PROCESS_EVIDENCE_SCHEMA
        and evidence.get("process_kind") == LIVE_OBSERVATION_STREAM_KIND
        and evidence.get("process_ref") == process_ref
        and evidence.get("evidence_ref") == evidence_ref
        and evidence.get("gateway_mission_session_ref") == gateway_mission_session_ref
        and evidence.get("supervisor_session_ref") == supervisor_session_ref
        and evidence.get("source_bound") is True
        and evidence.get("process_started") is True
    )


def _recovery_loop_live_kind_allowlisted(recovery_loop: dict[str, Any]) -> bool:
    return (
        recovery_loop.get("gateway_recovery_decision_loop_kind")
        == LIVE_RECOVERY_DECISION_LOOP_KIND
    )


def _recovery_loop_live_provenance_source_bound(
    recovery_loop: dict[str, Any],
    *,
    gateway_mission_session_ref: str,
    supervisor_session_ref: str,
    recovery_loop_artifact_path: Path,
) -> bool:
    process_ref = recovery_loop.get("gateway_live_recovery_decision_process_ref")
    evidence_ref = recovery_loop.get(
        "gateway_live_recovery_decision_process_evidence_ref"
    )
    evidence = _read_evidence_artifact(
        recovery_loop.get("gateway_live_recovery_decision_process_evidence_artifact_path"),
        source_artifact_path=recovery_loop_artifact_path,
    )
    return (
        recovery_loop.get("gateway_live_recovery_decision_process_source_bound") is True
        and _source_ref(process_ref, prefix="gateway_live_recovery_decision_process:")
        and _source_ref(evidence_ref, prefix="gateway_live_recovery_decision_evidence:")
        and evidence.get("schema_version")
        == LIVE_RECOVERY_DECISION_PROCESS_EVIDENCE_SCHEMA
        and evidence.get("process_kind") == LIVE_RECOVERY_DECISION_LOOP_KIND
        and evidence.get("process_ref") == process_ref
        and evidence.get("evidence_ref") == evidence_ref
        and evidence.get("gateway_mission_session_ref") == gateway_mission_session_ref
        and evidence.get("supervisor_session_ref") == supervisor_session_ref
        and evidence.get("source_bound") is True
        and evidence.get("process_started") is True
    )


def _process_evidence_matches_materializer(
    evidence: dict[str, Any],
    *,
    expected_schema: str,
    expected_process_kind: str,
    expected_process_ref: Any,
    expected_evidence_ref: Any,
    materializer: dict[str, Any],
    gateway_mission_session_ref: str,
    supervisor_session_ref: str,
    source_runtime_artifact_ref: str,
    source_runtime_artifact_path: Path,
) -> bool:
    materializer_ref = materializer.get("gateway_live_process_materializer_ref")
    boundary_kind = materializer.get("gateway_process_boundary_kind")
    boundary_ref = materializer.get("gateway_process_boundary_ref")
    return (
        evidence.get("schema_version") == expected_schema
        and evidence.get("process_kind") == expected_process_kind
        and evidence.get("process_ref") == expected_process_ref
        and evidence.get("evidence_ref") == expected_evidence_ref
        and evidence.get("materializer_ref") == materializer_ref
        and evidence.get("gateway_mission_session_ref") == gateway_mission_session_ref
        and evidence.get("supervisor_session_ref") == supervisor_session_ref
        and evidence.get("source_runtime_artifact_ref") == source_runtime_artifact_ref
        and evidence.get("source_runtime_artifact_path") == str(source_runtime_artifact_path)
        and evidence.get("source_runtime_artifact_sha256")
        == _file_sha256(source_runtime_artifact_path)
        and evidence.get("source_bound") is True
        and evidence.get("process_started") is True
        and evidence.get("gateway_process_boundary_observed") is True
        and evidence.get("gateway_process_boundary_kind") == boundary_kind
        and evidence.get("gateway_process_boundary_ref") == boundary_ref
        and _source_ref(boundary_ref, prefix="gateway_process_boundary:")
        and evidence.get("physical_execution_invoked") is False
        and evidence.get("hardware_target_allowed") is False
        and evidence.get("physical_form1_claimed") is False
        and evidence.get("dispatch_authority_created") is False
        and evidence.get("delivery_completion_claimed") is False
        and not _nested_authority_reasons(evidence, path="process_evidence")
    )


def _live_process_evidence_authority_reasons(
    *,
    observation_stream: dict[str, Any],
    observation_stream_artifact_path: Path,
    recovery_loop: dict[str, Any],
    recovery_loop_artifact_path: Path,
) -> list[str]:
    observation_evidence = _read_evidence_artifact(
        observation_stream.get("gateway_live_observation_process_evidence_artifact_path"),
        source_artifact_path=observation_stream_artifact_path,
    )
    recovery_evidence = _read_evidence_artifact(
        recovery_loop.get("gateway_live_recovery_decision_process_evidence_artifact_path"),
        source_artifact_path=recovery_loop_artifact_path,
    )
    return [
        *_nested_authority_reasons(
            observation_evidence,
            path="observation_process_evidence",
        ),
        *_nested_authority_reasons(
            recovery_evidence,
            path="recovery_decision_process_evidence",
        ),
    ]


def _live_gateway_process_materializer_supported(
    materializer: dict[str, Any],
    *,
    materializer_artifact_path: Path | None,
    gateway_mission_session_ref: str,
    supervisor_session_ref: str,
    gateway_supervisor_lifecycle_ref: str,
    source_runtime_artifact_ref: str,
    source_runtime_artifact_path: Path,
    observation_stream_artifact_path: Path,
    recovery_loop_artifact_path: Path,
    observation_stream: dict[str, Any],
    recovery_loop: dict[str, Any],
) -> bool:
    if materializer_artifact_path is None:
        return False
    materializer_from_disk = _read_json(materializer_artifact_path)
    if materializer_from_disk != materializer:
        return False
    boundary_ref = materializer.get("gateway_process_boundary_ref")
    supervisor_boundary_artifact_path = materializer.get(
        "gateway_supervisor_process_probe_boundary_artifact_path"
    )
    supervisor_boundary = _read_evidence_artifact(
        supervisor_boundary_artifact_path,
        source_artifact_path=materializer_artifact_path,
    )
    supervisor_boundary_supported = gateway_supervisor_process_probe_boundary_supported(
        supervisor_boundary,
        gateway_mission_session_ref=gateway_mission_session_ref,
        supervisor_session_ref=supervisor_session_ref,
        gateway_supervisor_lifecycle_ref=gateway_supervisor_lifecycle_ref,
        source_runtime_artifact_ref=source_runtime_artifact_ref,
        source_runtime_artifact_path=str(source_runtime_artifact_path),
        source_runtime_artifact_sha256=_file_sha256(source_runtime_artifact_path),
    )
    observation_evidence = _read_evidence_artifact(
        observation_stream.get("gateway_live_observation_process_evidence_artifact_path"),
        source_artifact_path=observation_stream_artifact_path,
    )
    recovery_evidence = _read_evidence_artifact(
        recovery_loop.get("gateway_live_recovery_decision_process_evidence_artifact_path"),
        source_artifact_path=recovery_loop_artifact_path,
    )
    return (
        materializer.get("schema_version") == LIVE_GATEWAY_PROCESS_MATERIALIZER_SCHEMA
        and materializer.get("materializer_status") == "gateway_live_processes_materialized"
        and materializer.get("materializer_kind") == LIVE_GATEWAY_PROCESS_MATERIALIZER_KIND
        and materializer.get("materializer_invoked") is True
        and materializer.get("gateway_process_boundary_observed") is True
        and materializer.get("gateway_process_boundary_kind")
        in ALLOWED_GATEWAY_PROCESS_BOUNDARY_KINDS
        and _source_ref(boundary_ref, prefix="gateway_process_boundary:")
        and supervisor_boundary_supported
        and supervisor_boundary.get("gateway_process_boundary_ref") == boundary_ref
        and supervisor_boundary.get("gateway_process_boundary_kind")
        == materializer.get("gateway_process_boundary_kind")
        and supervisor_boundary.get("gateway_supervisor_process_probe_ref")
        == materializer.get("gateway_supervisor_process_probe_ref")
        and materializer.get("gateway_supervisor_process_probe_boundary_artifact_path")
        == str(supervisor_boundary_artifact_path)
        and materializer.get("source_bound") is True
        and materializer.get("gateway_mission_session_ref") == gateway_mission_session_ref
        and materializer.get("supervisor_session_ref") == supervisor_session_ref
        and materializer.get("gateway_supervisor_lifecycle_ref")
        == gateway_supervisor_lifecycle_ref
        and materializer.get("source_runtime_artifact_ref") == source_runtime_artifact_ref
        and materializer.get("source_runtime_artifact_path")
        == str(source_runtime_artifact_path)
        and materializer.get("source_runtime_artifact_sha256")
        == _file_sha256(source_runtime_artifact_path)
        and materializer.get("gateway_live_observation_stream_artifact_path")
        == str(observation_stream_artifact_path)
        and materializer.get("gateway_live_recovery_decision_loop_artifact_path")
        == str(recovery_loop_artifact_path)
        and materializer.get("gateway_live_observation_process_ref")
        == observation_stream.get("gateway_live_observation_process_ref")
        and materializer.get("gateway_live_observation_process_evidence_ref")
        == observation_stream.get("gateway_live_observation_process_evidence_ref")
        and materializer.get("gateway_live_observation_process_evidence_artifact_path")
        == observation_stream.get("gateway_live_observation_process_evidence_artifact_path")
        and materializer.get("gateway_live_recovery_decision_process_ref")
        == recovery_loop.get("gateway_live_recovery_decision_process_ref")
        and materializer.get("gateway_live_recovery_decision_process_evidence_ref")
        == recovery_loop.get("gateway_live_recovery_decision_process_evidence_ref")
        and materializer.get("gateway_live_recovery_decision_process_evidence_artifact_path")
        == recovery_loop.get("gateway_live_recovery_decision_process_evidence_artifact_path")
        and _process_evidence_matches_materializer(
            observation_evidence,
            expected_schema=LIVE_OBSERVATION_PROCESS_EVIDENCE_SCHEMA,
            expected_process_kind=LIVE_OBSERVATION_STREAM_KIND,
            expected_process_ref=observation_stream.get(
                "gateway_live_observation_process_ref"
            ),
            expected_evidence_ref=observation_stream.get(
                "gateway_live_observation_process_evidence_ref"
            ),
            materializer=materializer,
            gateway_mission_session_ref=gateway_mission_session_ref,
            supervisor_session_ref=supervisor_session_ref,
            source_runtime_artifact_ref=source_runtime_artifact_ref,
            source_runtime_artifact_path=source_runtime_artifact_path,
        )
        and _process_evidence_matches_materializer(
            recovery_evidence,
            expected_schema=LIVE_RECOVERY_DECISION_PROCESS_EVIDENCE_SCHEMA,
            expected_process_kind=LIVE_RECOVERY_DECISION_LOOP_KIND,
            expected_process_ref=recovery_loop.get(
                "gateway_live_recovery_decision_process_ref"
            ),
            expected_evidence_ref=recovery_loop.get(
                "gateway_live_recovery_decision_process_evidence_ref"
            ),
            materializer=materializer,
            gateway_mission_session_ref=gateway_mission_session_ref,
            supervisor_session_ref=supervisor_session_ref,
            source_runtime_artifact_ref=source_runtime_artifact_ref,
            source_runtime_artifact_path=source_runtime_artifact_path,
        )
        and materializer.get("physical_execution_invoked") is False
        and materializer.get("hardware_target_allowed") is False
        and materializer.get("physical_form1_claimed") is False
        and materializer.get("dispatch_authority_created") is False
        and materializer.get("delivery_completion_claimed") is False
    )


def _write_stage_artifact(probe_dir: Path, filename: str, artifact: dict[str, Any]) -> Path:
    path = probe_dir / filename
    artifact["artifact_dir"] = str(probe_dir)
    _write_json(path, artifact)
    return path


def _latest_runtime_artifact(output_dir: Path) -> Path | None:
    candidates = sorted(
        output_dir.glob(
            "multi_condition_supervisor_runtime_*/mission_os_multi_condition_supervisor_runtime.json"
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _run_live_supervisor_runtime(
    *,
    output_dir: Path,
    wind_mps: float,
    wind_direction_deg: float,
    drift_threshold_m: float,
    timeout_seconds: int,
) -> tuple[Path | None, str | None]:
    command = [
        sys.executable,
        "scripts/audit_mission_os_multi_condition_supervisor_runtime.py",
        "--wind-mps",
        str(wind_mps),
        "--wind-direction-deg",
        str(wind_direction_deg),
        "--drift-threshold-m",
        str(drift_threshold_m),
        "--output-dir",
        str(output_dir),
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return _latest_runtime_artifact(output_dir), f"live_runtime_timeout:{exc}"
    runtime_path = _latest_runtime_artifact(output_dir)
    if result.returncode != 0:
        digest = (
            f"live_runtime_rc={result.returncode}\n"
            f"stdout_tail={result.stdout[-2000:]}\n"
            f"stderr_tail={result.stderr[-2000:]}"
        )
        return runtime_path, digest
    if runtime_path is None:
        return None, "live_runtime_artifact_missing_after_success"
    return runtime_path, None


def materialize_gateway_live_processes(
    *,
    probe_dir: Path,
    source_runtime: dict[str, Any],
    source_runtime_artifact_path: Path,
    gateway_session: dict[str, Any],
    lifecycle: dict[str, Any],
    observation_stream: dict[str, Any],
    recovery_loop: dict[str, Any],
    gateway_route_invocation_boundary: dict[str, Any] | None = None,
    gateway_route_invocation_boundary_artifact_path: Path | None = None,
    gateway_supervisor_process_probe_boundary: dict[str, Any] | None = None,
    gateway_supervisor_process_probe_boundary_artifact_path: Path | None = None,
) -> tuple[dict[str, Any], Path, dict[str, Any], Path, dict[str, Any], Path]:
    """Materialize source-bound live Gateway process evidence for the C5b probe."""

    gateway_mission_session_ref = str(
        gateway_session.get("gateway_mission_session_ref") or ""
    )
    supervisor_session_ref = str(gateway_session.get("supervisor_session_ref") or "")
    route_boundary = (
        gateway_route_invocation_boundary
        if isinstance(gateway_route_invocation_boundary, dict)
        else {}
    )
    supervisor_boundary = (
        gateway_supervisor_process_probe_boundary
        if isinstance(gateway_supervisor_process_probe_boundary, dict)
        else {}
    )
    route_boundary_supported = gateway_route_invocation_boundary_supported(
        route_boundary,
        gateway_mission_session_ref=gateway_mission_session_ref,
        supervisor_session_ref=supervisor_session_ref,
        source_runtime_artifact_path=str(source_runtime_artifact_path),
        source_runtime_artifact_sha256=_file_sha256(source_runtime_artifact_path),
    )
    supervisor_boundary_supported = gateway_supervisor_process_probe_boundary_supported(
        supervisor_boundary,
        gateway_mission_session_ref=gateway_mission_session_ref,
        supervisor_session_ref=supervisor_session_ref,
        gateway_supervisor_lifecycle_ref=str(
            lifecycle.get("gateway_supervisor_lifecycle_ref") or ""
        ),
        source_runtime_artifact_ref=str(source_runtime.get("audit_id") or ""),
        source_runtime_artifact_path=str(source_runtime_artifact_path),
        source_runtime_artifact_sha256=_file_sha256(source_runtime_artifact_path),
    )
    boundary_observed = supervisor_boundary_supported or route_boundary_supported
    boundary_kind = (
        GATEWAY_PROCESS_BOUNDARY_KIND_SUPERVISOR_PROCESS_PROBE
        if supervisor_boundary_supported
        else (
            GATEWAY_PROCESS_BOUNDARY_KIND_ROUTE_INVOCATION
            if route_boundary_supported
            else "materializer_scaffold_only"
        )
    )
    process_started = supervisor_boundary_supported
    boundary_ref = (
        supervisor_boundary.get("gateway_process_boundary_ref")
        if supervisor_boundary_supported
        else (
            route_boundary.get("gateway_process_boundary_ref")
            if route_boundary_supported
            else None
        )
    )
    route_boundary_artifact_path = (
        str(gateway_route_invocation_boundary_artifact_path)
        if gateway_route_invocation_boundary_artifact_path is not None
        else None
    )
    supervisor_boundary_artifact_path = (
        str(gateway_supervisor_process_probe_boundary_artifact_path)
        if gateway_supervisor_process_probe_boundary_artifact_path is not None
        else None
    )
    materializer_id = _stable_id(
        "gateway_live_process_materializer",
        {
            "schema_version": LIVE_GATEWAY_PROCESS_MATERIALIZER_SCHEMA,
            "gateway_mission_session_ref": gateway_mission_session_ref,
            "supervisor_session_ref": supervisor_session_ref,
            "source_runtime_artifact_path": str(source_runtime_artifact_path),
            "source_runtime_artifact_sha256": _file_sha256(source_runtime_artifact_path),
            "gateway_process_boundary_ref": boundary_ref,
        },
    )
    materializer_ref = f"gateway_live_process_materializer:{materializer_id}"
    observation_process_ref = (
        "gateway_live_observation_process:" f"{materializer_id}:observation"
    )
    observation_evidence_ref = (
        "gateway_live_observation_evidence:" f"{materializer_id}:observation"
    )
    recovery_process_ref = (
        "gateway_live_recovery_decision_process:" f"{materializer_id}:recovery"
    )
    recovery_evidence_ref = (
        "gateway_live_recovery_decision_evidence:" f"{materializer_id}:recovery"
    )
    observation_evidence_path = probe_dir / "gateway_live_observation_process_evidence.json"
    recovery_evidence_path = (
        probe_dir / "gateway_live_recovery_decision_process_evidence.json"
    )
    live_observation_stream_path = probe_dir / "gateway_live_observation_stream.json"
    live_recovery_loop_path = probe_dir / "gateway_live_recovery_decision_loop.json"
    materializer_path = probe_dir / "gateway_live_process_materializer.json"

    observation_evidence = {
        "schema_version": LIVE_OBSERVATION_PROCESS_EVIDENCE_SCHEMA,
        "process_kind": LIVE_OBSERVATION_STREAM_KIND,
        "process_ref": observation_process_ref,
        "evidence_ref": observation_evidence_ref,
        "materializer_ref": materializer_ref,
        "gateway_mission_session_ref": gateway_mission_session_ref,
        "supervisor_session_ref": supervisor_session_ref,
        "source_runtime_artifact_ref": source_runtime.get("audit_id"),
        "source_runtime_artifact_path": str(source_runtime_artifact_path),
        "source_runtime_artifact_sha256": _file_sha256(source_runtime_artifact_path),
        "source_runtime_run_mode": source_runtime.get("run_mode"),
        "source_bound": True,
        "process_started": process_started,
        "gateway_process_boundary_observed": boundary_observed,
        "gateway_process_boundary_kind": boundary_kind,
        "gateway_process_boundary_ref": boundary_ref,
        "gateway_route_invocation_boundary_artifact_path": route_boundary_artifact_path,
        "gateway_supervisor_process_probe_boundary_artifact_path": (
            supervisor_boundary_artifact_path
        ),
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_form1_claimed": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
    }
    recovery_evidence = {
        "schema_version": LIVE_RECOVERY_DECISION_PROCESS_EVIDENCE_SCHEMA,
        "process_kind": LIVE_RECOVERY_DECISION_LOOP_KIND,
        "process_ref": recovery_process_ref,
        "evidence_ref": recovery_evidence_ref,
        "materializer_ref": materializer_ref,
        "gateway_mission_session_ref": gateway_mission_session_ref,
        "supervisor_session_ref": supervisor_session_ref,
        "source_runtime_artifact_ref": source_runtime.get("audit_id"),
        "source_runtime_artifact_path": str(source_runtime_artifact_path),
        "source_runtime_artifact_sha256": _file_sha256(source_runtime_artifact_path),
        "source_runtime_run_mode": source_runtime.get("run_mode"),
        "source_bound": True,
        "process_started": process_started,
        "gateway_process_boundary_observed": boundary_observed,
        "gateway_process_boundary_kind": boundary_kind,
        "gateway_process_boundary_ref": boundary_ref,
        "gateway_route_invocation_boundary_artifact_path": route_boundary_artifact_path,
        "gateway_supervisor_process_probe_boundary_artifact_path": (
            supervisor_boundary_artifact_path
        ),
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_form1_claimed": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
    }
    _write_json(observation_evidence_path, observation_evidence)
    _write_json(recovery_evidence_path, recovery_evidence)

    live_observation_stream = dict(observation_stream)
    live_observation_stream.update(
        {
            "observation_stream_kind": LIVE_OBSERVATION_STREAM_KIND,
            "gateway_live_observation_stream": True,
            "gateway_observation_process_started": True,
            "gateway_live_observation_process_source_bound": True,
            "gateway_live_observation_process_ref": observation_process_ref,
            "gateway_live_observation_process_evidence_ref": observation_evidence_ref,
            "gateway_live_observation_process_evidence_artifact_path": str(
                observation_evidence_path
            ),
            "gateway_live_process_materializer_ref": materializer_ref,
        }
    )
    live_recovery_loop = dict(recovery_loop)
    live_recovery_loop.update(
        {
            "gateway_recovery_decision_loop_kind": LIVE_RECOVERY_DECISION_LOOP_KIND,
            "gateway_live_recovery_decision_loop": True,
            "gateway_recovery_decision_process_started": True,
            "gateway_live_recovery_decision_process_source_bound": True,
            "gateway_live_recovery_decision_process_ref": recovery_process_ref,
            "gateway_live_recovery_decision_process_evidence_ref": recovery_evidence_ref,
            "gateway_live_recovery_decision_process_evidence_artifact_path": str(
                recovery_evidence_path
            ),
            "gateway_live_process_materializer_ref": materializer_ref,
        }
    )
    _write_json(live_observation_stream_path, live_observation_stream)
    _write_json(live_recovery_loop_path, live_recovery_loop)

    materializer = {
        "schema_version": LIVE_GATEWAY_PROCESS_MATERIALIZER_SCHEMA,
        "gateway_live_process_materializer_id": materializer_id,
        "gateway_live_process_materializer_ref": materializer_ref,
        "materializer_status": (
            "gateway_live_processes_materialized"
            if supervisor_boundary_supported
            else (
                "gateway_route_invocation_boundary_recorded"
                if route_boundary_supported
                else "gateway_live_process_materializer_scaffold_ready"
            )
        ),
        "materializer_kind": LIVE_GATEWAY_PROCESS_MATERIALIZER_KIND,
        "materializer_invoked": True,
        "gateway_process_boundary_observed": boundary_observed,
        "gateway_process_boundary_kind": boundary_kind,
        "gateway_process_boundary_ref": boundary_ref,
        "gateway_route_invocation_boundary_schema": (
            route_boundary.get("schema_version") if route_boundary else None
        ),
        "gateway_route_invocation_boundary_artifact_path": route_boundary_artifact_path,
        "gateway_route_invocation_ref": (
            route_boundary.get("gateway_route_invocation_ref")
            if route_boundary_supported
            else None
        ),
        "gateway_supervisor_process_probe_boundary_schema": (
            supervisor_boundary.get("schema_version") if supervisor_boundary else None
        ),
        "gateway_supervisor_process_probe_boundary_artifact_path": (
            supervisor_boundary_artifact_path
        ),
        "gateway_supervisor_process_probe_ref": (
            supervisor_boundary.get("gateway_supervisor_process_probe_ref")
            if supervisor_boundary_supported
            else None
        ),
        "source_bound": True,
        "gateway_mission_session_ref": gateway_mission_session_ref,
        "supervisor_session_ref": supervisor_session_ref,
        "gateway_supervisor_lifecycle_ref": lifecycle.get(
            "gateway_supervisor_lifecycle_ref"
        ),
        "source_runtime_artifact_ref": source_runtime.get("audit_id"),
        "source_runtime_artifact_path": str(source_runtime_artifact_path),
        "source_runtime_artifact_sha256": _file_sha256(source_runtime_artifact_path),
        "source_runtime_run_mode": source_runtime.get("run_mode"),
        "gateway_live_observation_stream_artifact_path": str(
            live_observation_stream_path
        ),
        "gateway_live_observation_process_ref": observation_process_ref,
        "gateway_live_observation_process_evidence_ref": observation_evidence_ref,
        "gateway_live_observation_process_evidence_artifact_path": str(
            observation_evidence_path
        ),
        "gateway_live_recovery_decision_loop_artifact_path": str(
            live_recovery_loop_path
        ),
        "gateway_live_recovery_decision_process_ref": recovery_process_ref,
        "gateway_live_recovery_decision_process_evidence_ref": recovery_evidence_ref,
        "gateway_live_recovery_decision_process_evidence_artifact_path": str(
            recovery_evidence_path
        ),
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_form1_claimed": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "scope_boundary_notes": [
            (
                "gateway_supervisor_process_probe_boundary_observed"
                if supervisor_boundary_supported
                else (
                    "gateway_route_invocation_boundary_observed"
                    if route_boundary_supported
                    else "materializer_scaffold_only"
                )
            ),
            (
                "supervisor_process_probe_materializes_live_gateway_process_evidence"
                if supervisor_boundary_supported
                else (
                    "route_boundary_is_one_required_c5b_input_not_standalone_runtime"
                    if route_boundary_supported
                    else "does_not_observe_gateway_process_boundary"
                )
            ),
            (
                "gateway_live_observation_and_recovery_processes_started"
                if supervisor_boundary_supported
                else (
                    "does_not_materialize_gateway_live_observation_or_recovery_processes"
                    if route_boundary_supported
                    else "live_process_materialization_not_attempted"
                )
            ),
            "does_not_invoke_physical_execution_or_dispatch_authority",
            "c5b_probe_still_validates_full_gateway_runtime_loop",
        ],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(materializer_path, materializer)
    return (
        live_observation_stream,
        live_observation_stream_path,
        live_recovery_loop,
        live_recovery_loop_path,
        materializer,
        materializer_path,
    )


def build_gateway_live_runtime_probe(
    *,
    source_runtime: dict[str, Any],
    source_runtime_artifact_path: Path,
    gateway_session: dict[str, Any],
    gateway_session_artifact_path: Path,
    lifecycle: dict[str, Any],
    lifecycle_artifact_path: Path,
    observation_stream: dict[str, Any],
    observation_stream_artifact_path: Path,
    recovery_loop: dict[str, Any],
    recovery_loop_artifact_path: Path,
    readiness: dict[str, Any],
    readiness_artifact_path: Path,
    live_probe_invoked: bool,
    materializer: dict[str, Any] | None = None,
    materializer_artifact_path: Path | None = None,
    live_runtime_error: str | None = None,
) -> dict[str, Any]:
    """Build a fail-closed C5b Gateway live runtime probe artifact."""

    source_loop = _source_loop(source_runtime)
    c4_checks = _checks(recovery_loop)
    c5a_checks = _checks(readiness)
    source_authority_reasons = _nested_authority_reasons(source_runtime, path="runtime")
    c1_authority_reasons = _nested_authority_reasons(gateway_session, path="gateway_session")
    c2_authority_reasons = _nested_authority_reasons(lifecycle, path="lifecycle")
    c3_authority_reasons = _nested_authority_reasons(
        observation_stream, path="observation_stream"
    )
    c4_authority_reasons = _nested_authority_reasons(recovery_loop, path="recovery_loop")
    c5a_authority_reasons = _nested_authority_reasons(readiness, path="readiness")
    materializer = materializer if isinstance(materializer, dict) else {}
    materializer_authority_reasons = _nested_authority_reasons(
        materializer, path="materializer"
    )
    process_evidence_authority_reasons = _live_process_evidence_authority_reasons(
        observation_stream=observation_stream,
        observation_stream_artifact_path=observation_stream_artifact_path,
        recovery_loop=recovery_loop,
        recovery_loop_artifact_path=recovery_loop_artifact_path,
    )
    gateway_mission_session_ref = str(
        readiness.get("gateway_mission_session_ref")
        or recovery_loop.get("gateway_mission_session_ref")
        or ""
    )
    supervisor_session_ref = str(
        readiness.get("supervisor_session_ref")
        or recovery_loop.get("supervisor_session_ref")
        or ""
    )
    runtime_run_mode = str(source_runtime.get("run_mode") or "")
    lifecycle_states = lifecycle.get("observed_lifecycle_states")
    lifecycle_states = lifecycle_states if isinstance(lifecycle_states, list) else []
    observation_records = observation_stream.get("observations")
    observation_records = observation_records if isinstance(observation_records, list) else []
    decision_steps = recovery_loop.get("decision_steps")
    decision_steps = decision_steps if isinstance(decision_steps, list) else []
    observation_stream_live_kind_allowlisted = _observation_stream_live_kind_allowlisted(
        observation_stream
    )
    observation_stream_live_provenance_source_bound = (
        _observation_stream_live_provenance_source_bound(
            observation_stream,
            gateway_mission_session_ref=gateway_mission_session_ref,
            supervisor_session_ref=supervisor_session_ref,
            observation_stream_artifact_path=observation_stream_artifact_path,
        )
    )
    recovery_loop_live_kind_allowlisted = _recovery_loop_live_kind_allowlisted(
        recovery_loop
    )
    recovery_loop_live_provenance_source_bound = (
        _recovery_loop_live_provenance_source_bound(
            recovery_loop,
            gateway_mission_session_ref=gateway_mission_session_ref,
            supervisor_session_ref=supervisor_session_ref,
            recovery_loop_artifact_path=recovery_loop_artifact_path,
        )
    )
    observation_stream_live_process = (
        observation_stream.get("gateway_live_observation_stream") is True
        and observation_stream.get("gateway_observation_process_started") is True
        and observation_stream_live_kind_allowlisted
        and observation_stream_live_provenance_source_bound
    )
    recovery_loop_live_process = (
        recovery_loop.get("gateway_live_recovery_decision_loop") is True
        and recovery_loop.get("gateway_recovery_decision_process_started") is True
        and recovery_loop_live_kind_allowlisted
        and recovery_loop_live_provenance_source_bound
    )
    live_gateway_process_materializer_supported = (
        _live_gateway_process_materializer_supported(
            materializer,
            materializer_artifact_path=materializer_artifact_path,
            gateway_mission_session_ref=gateway_mission_session_ref,
            supervisor_session_ref=supervisor_session_ref,
            gateway_supervisor_lifecycle_ref=str(
                lifecycle.get("gateway_supervisor_lifecycle_ref") or ""
            ),
            source_runtime_artifact_ref=str(source_runtime.get("audit_id") or ""),
            source_runtime_artifact_path=source_runtime_artifact_path,
            observation_stream_artifact_path=observation_stream_artifact_path,
            recovery_loop_artifact_path=recovery_loop_artifact_path,
            observation_stream=observation_stream,
            recovery_loop=recovery_loop,
        )
    )
    checks = {
        "live_gateway_runtime_probe_invoked": live_probe_invoked is True,
        "source_runtime_executed_run": runtime_run_mode == "executed_run",
        "source_runtime_schema_observed": source_runtime.get("schema_version")
        == "mission_os_multi_condition_supervisor_runtime_audit.v1",
        "source_runtime_form3_observed": (
            source_runtime.get("audit_status")
            == "multi_condition_supervisor_runtime_observed"
            and source_runtime.get("causal_form") == "Form 3"
            and source_runtime.get("form3_claim_supported") is True
            and source_runtime.get("supervisor_runtime_claim_supported") is True
        ),
        "source_runtime_progress_counted": source_runtime.get("progress_counted") is True,
        "source_runtime_scope_matches": (
            source_runtime.get("decision_loop_driver") == "mission_os_supervisor"
            and source_runtime.get("supervisor_scope") == TARGET_SUPERVISOR_SCOPE
            and source_loop.get("supervisor_scope") == TARGET_SUPERVISOR_SCOPE
        ),
        "source_runtime_full_gateway_false_before_probe": (
            source_runtime.get("full_gateway_runtime_loop") is False
            and source_loop.get("full_gateway_runtime_loop") is False
        ),
        "source_runtime_cycles_two": (
            source_runtime.get("cycle_count") == 2
            and source_loop.get("cycle_count") == 2
        ),
        "gateway_mission_session_ready": gateway_session.get("gateway_session_status")
        == GATEWAY_SESSION_STATUS_READY,
        "gateway_supervisor_lifecycle_ready": lifecycle.get("lifecycle_status")
        == LIFECYCLE_STATUS_READY,
        "gateway_observation_stream_ready": observation_stream.get(
            "observation_stream_status"
        )
        == STREAM_STATUS_READY,
        "gateway_recovery_decision_loop_ready": recovery_loop.get(
            "recovery_decision_loop_status"
        )
        == LOOP_STATUS_READY,
        "gateway_full_runtime_readiness_ready": readiness.get("readiness_status")
        == READINESS_STATUS_READY
        and readiness.get("ready_for_live_gateway_runtime_probe") is True,
        "gateway_live_observation_stream_kind_allowlisted": (
            observation_stream_live_kind_allowlisted
        ),
        "gateway_live_observation_stream_provenance_source_bound": (
            observation_stream_live_provenance_source_bound
        ),
        "gateway_live_observation_stream_source_observed": observation_stream_live_process,
        "gateway_live_recovery_decision_loop_kind_allowlisted": (
            recovery_loop_live_kind_allowlisted
        ),
        "gateway_live_recovery_decision_loop_provenance_source_bound": (
            recovery_loop_live_provenance_source_bound
        ),
        "gateway_live_recovery_decision_loop_source_observed": recovery_loop_live_process,
        "gateway_live_gateway_process_materializer_implemented": (
            live_gateway_process_materializer_supported
        ),
        "gateway_starts_mission_session_live": live_probe_invoked
        and gateway_session.get("gateway_mission_session_ref") == gateway_mission_session_ref,
        "gateway_starts_supervisor_lifecycle_live": live_probe_invoked
        and lifecycle.get("gateway_mission_session_ref") == gateway_mission_session_ref
        and lifecycle.get("supervisor_session_ref") == supervisor_session_ref,
        "gateway_owns_live_observation_stream": live_probe_invoked
        and observation_stream_live_process
        and observation_stream.get("gateway_mission_session_ref") == gateway_mission_session_ref
        and observation_stream.get("supervisor_session_ref") == supervisor_session_ref
        and len(observation_records) == 2,
        "gateway_observes_mission_state_live": live_probe_invoked
        and all(
            isinstance(observation, dict)
            and observation.get("same_session_evidence") is True
            and observation.get("stale_telemetry_detected") is False
            and observation.get("source_outcome_observed") is True
            for observation in observation_records
        ),
        "gateway_owned_recovery_decision_loop_emits_decision_live": live_probe_invoked
        and recovery_loop_live_process
        and recovery_loop.get("gateway_mission_session_ref") == gateway_mission_session_ref
        and recovery_loop.get("supervisor_session_ref") == supervisor_session_ref
        and len(decision_steps) == 2
        and all(
            isinstance(step, dict)
            and step.get("same_session_evidence") is True
            and step.get("dispatch_observed") is True
            and step.get("outcome_observed") is True
            for step in decision_steps
        ),
        "backend_action_request_receipt_outcome_same_session_live": live_probe_invoked
        and c4_checks.get("cycle1_gateway_ref_chain_consistent") is True
        and c4_checks.get("cycle2_gateway_ref_chain_consistent") is True
        and c4_checks.get("cycle_dispatch_chains_distinct") is True
        and c5a_checks.get("c4_decision_steps_supported") is True,
        "gateway_records_lifecycle_result_live": live_probe_invoked
        and {"created", "spawned", "running", "heartbeat_observed", "completed"}.issubset(
            set(lifecycle_states)
        ),
        "c5a_same_session_scaffold_chain_ready": (
            c5a_checks.get("refs_same_session") is True
            and c5a_checks.get("c4_observation_stream_ref_matches_artifact") is True
            and c5a_checks.get("c4_lifecycle_ref_matches_artifact") is True
            and c5a_checks.get("c4_runtime_ref_matches_artifact") is True
            and c5a_checks.get("c4_mission_contract_ref_matches_sources") is True
            and c5a_checks.get("c4_task_graph_ref_matches_sources") is True
        ),
        "physical_hardware_dispatch_delivery_authority_remains_false": (
            source_runtime.get("physical_execution_invoked") is not True
            and source_runtime.get("hardware_target_allowed") is not True
            and source_runtime.get("physical_form1_claimed") is not True
            and source_runtime.get("dispatch_authority_created") is not True
            and source_runtime.get("delivery_completion_claimed") is not True
            and readiness.get("physical_execution_invoked") is False
            and readiness.get("hardware_target_allowed") is False
            and readiness.get("physical_form1_claimed") is False
            and readiness.get("dispatch_authority_created") is False
            and readiness.get("delivery_completion_claimed") is False
        ),
        "nested_authority_boundary_false": not (
            source_authority_reasons
            or c1_authority_reasons
            or c2_authority_reasons
            or c3_authority_reasons
            or c4_authority_reasons
            or c5a_authority_reasons
            or materializer_authority_reasons
            or process_evidence_authority_reasons
        ),
        "gateway_live_process_sidecar_authority_boundary_false": not (
            process_evidence_authority_reasons
        ),
    }
    blocked_reasons = [
        f"{name}_not_observed" for name, passed in checks.items() if not passed
    ]
    if live_runtime_error:
        blocked_reasons.append("live_supervisor_runtime_error:" + live_runtime_error[-2000:])
    blocked_reasons.extend(source_authority_reasons)
    blocked_reasons.extend(c1_authority_reasons)
    blocked_reasons.extend(c2_authority_reasons)
    blocked_reasons.extend(c3_authority_reasons)
    blocked_reasons.extend(c4_authority_reasons)
    blocked_reasons.extend(c5a_authority_reasons)
    blocked_reasons.extend(materializer_authority_reasons)
    blocked_reasons.extend(process_evidence_authority_reasons)
    observed = not blocked_reasons
    probe_id = _stable_id(
        "gateway_live_runtime_probe",
        {
            "schema_version": SCHEMA_VERSION,
            "gateway_mission_session_ref": gateway_mission_session_ref,
            "supervisor_session_ref": supervisor_session_ref,
            "source_runtime_path": str(source_runtime_artifact_path),
            "readiness_path": str(readiness_artifact_path),
            "live_probe_invoked": live_probe_invoked,
        },
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "gateway_live_runtime_probe_id": probe_id,
        "gateway_live_runtime_probe_ref": f"gateway_live_runtime_probe:{probe_id}",
        "gateway_runtime_probe_status": (
            PROBE_STATUS_OBSERVED if observed else PROBE_STATUS_BLOCKED
        ),
        "causal_form": "Form 3" if observed else "Form 0b",
        "form3_claim_supported": observed,
        "progress_counted": observed,
        "gateway_capability_progress_counted": observed,
        "decision_loop_driver": "mission_os_supervisor",
        "supervisor_scope": TARGET_SUPERVISOR_SCOPE,
        "gateway_mission_session_ref": gateway_mission_session_ref,
        "supervisor_session_ref": supervisor_session_ref,
        "source_runtime_artifact_ref": source_runtime.get("audit_id"),
        "source_runtime_artifact_path": str(source_runtime_artifact_path),
        "source_runtime_run_mode": runtime_run_mode,
        "gateway_mission_session_artifact_path": str(gateway_session_artifact_path),
        "gateway_supervisor_lifecycle_artifact_path": str(lifecycle_artifact_path),
        "gateway_owned_observation_stream_artifact_path": str(
            observation_stream_artifact_path
        ),
        "gateway_owned_recovery_decision_loop_artifact_path": str(
            recovery_loop_artifact_path
        ),
        "gateway_full_runtime_readiness_artifact_path": str(readiness_artifact_path),
        "gateway_live_process_materializer_ref": materializer.get(
            "gateway_live_process_materializer_ref"
        ),
        "gateway_live_process_materializer_artifact_path": (
            str(materializer_artifact_path) if materializer_artifact_path else None
        ),
        "live_gateway_runtime_probe_invoked": live_probe_invoked,
        "gateway_observation_stream_kind": observation_stream.get(
            "observation_stream_kind"
        ),
        "gateway_recovery_decision_loop_kind": recovery_loop.get(
            "gateway_recovery_decision_loop_kind"
        ),
        "gateway_live_observation_process_ref": observation_stream.get(
            "gateway_live_observation_process_ref"
        ),
        "gateway_live_observation_process_evidence_ref": observation_stream.get(
            "gateway_live_observation_process_evidence_ref"
        ),
        "gateway_live_observation_process_evidence_artifact_path": (
            observation_stream.get("gateway_live_observation_process_evidence_artifact_path")
        ),
        "gateway_live_recovery_decision_process_ref": recovery_loop.get(
            "gateway_live_recovery_decision_process_ref"
        ),
        "gateway_live_recovery_decision_process_evidence_ref": recovery_loop.get(
            "gateway_live_recovery_decision_process_evidence_ref"
        ),
        "gateway_live_recovery_decision_process_evidence_artifact_path": (
            recovery_loop.get("gateway_live_recovery_decision_process_evidence_artifact_path")
        ),
        "gateway_started_mission_session_live": observed,
        "gateway_supervisor_process_spawned": observed,
        "gateway_live_observation_stream": observed,
        "gateway_observation_process_started": observed,
        "gateway_live_recovery_decision_loop": observed,
        "gateway_recovery_decision_process_started": observed,
        "gateway_autonomous_runtime_claimed": observed,
        "full_gateway_runtime_loop": observed,
        "gateway_loop_same_session_evidence": observed,
        "cycle_count": 2 if observed else 0,
        "cycle1_gateway_ref_chain_consistent": observed
        and c4_checks.get("cycle1_gateway_ref_chain_consistent") is True,
        "cycle2_gateway_ref_chain_consistent": observed
        and c4_checks.get("cycle2_gateway_ref_chain_consistent") is True,
        "cycle_dispatch_chains_distinct": observed
        and c4_checks.get("cycle_dispatch_chains_distinct") is True,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_form1_claimed": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "checks": checks,
        "blocked_reasons": blocked_reasons,
        "scope_boundary_notes": [
            "c5b_live_gateway_owned_runtime_probe_sitl_only",
            "full_gateway_runtime_loop_true_only_when_live_probe_observed",
            "gateway_owns_session_lifecycle_observation_and_decision_loop_records",
            "physical_execution_and_dispatch_authority_are_not_created",
            "delivery_completion_is_not_claimed",
        ],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def build_gateway_live_runtime_probe_chain(
    *,
    source_runtime_artifact_path: Path,
    probe_dir: Path,
    live_probe_invoked: bool,
    materialize_live_gateway_processes: bool = False,
    gateway_route_invocation_boundary_artifact_path: Path | None = None,
    gateway_supervisor_process_probe_boundary_artifact_path: Path | None = None,
    live_runtime_error: str | None = None,
) -> dict[str, Any]:
    source_runtime = _read_json(source_runtime_artifact_path)
    gateway_session = build_gateway_mission_session(
        source_runtime,
        source_artifact_path=source_runtime_artifact_path,
    )
    gateway_session_path = _write_stage_artifact(
        probe_dir,
        "gateway_mission_session.json",
        gateway_session,
    )
    lifecycle = build_gateway_supervisor_lifecycle(
        gateway_session,
        source_artifact_path=gateway_session_path,
    )
    lifecycle_path = _write_stage_artifact(
        probe_dir,
        "gateway_supervisor_lifecycle.json",
        lifecycle,
    )
    observation_stream = build_gateway_owned_observation_stream(
        lifecycle,
        lifecycle_artifact_path=lifecycle_path,
    )
    observation_stream_path = _write_stage_artifact(
        probe_dir,
        "gateway_owned_observation_stream.json",
        observation_stream,
    )
    recovery_loop = build_gateway_owned_recovery_decision_loop(
        observation_stream,
        observation_stream_artifact_path=observation_stream_path,
    )
    recovery_loop_path = _write_stage_artifact(
        probe_dir,
        "gateway_owned_recovery_decision_loop.json",
        recovery_loop,
    )
    readiness = build_gateway_full_runtime_readiness(
        recovery_loop,
        c4_artifact_path=recovery_loop_path,
    )
    readiness_path = _write_stage_artifact(
        probe_dir,
        "gateway_full_runtime_readiness.json",
        readiness,
    )
    materializer: dict[str, Any] | None = None
    materializer_path: Path | None = None
    if materialize_live_gateway_processes and live_probe_invoked:
        gateway_route_invocation_boundary = (
            _read_json(gateway_route_invocation_boundary_artifact_path)
            if gateway_route_invocation_boundary_artifact_path is not None
            else None
        )
        gateway_supervisor_process_probe_boundary = (
            _read_json(gateway_supervisor_process_probe_boundary_artifact_path)
            if gateway_supervisor_process_probe_boundary_artifact_path is not None
            else None
        )
        (
            observation_stream,
            observation_stream_path,
            recovery_loop,
            recovery_loop_path,
            materializer,
            materializer_path,
        ) = materialize_gateway_live_processes(
            probe_dir=probe_dir,
            source_runtime=source_runtime,
            source_runtime_artifact_path=source_runtime_artifact_path,
            gateway_session=gateway_session,
            lifecycle=lifecycle,
            observation_stream=observation_stream,
            recovery_loop=recovery_loop,
            gateway_route_invocation_boundary=gateway_route_invocation_boundary,
            gateway_route_invocation_boundary_artifact_path=(
                gateway_route_invocation_boundary_artifact_path
            ),
            gateway_supervisor_process_probe_boundary=(
                gateway_supervisor_process_probe_boundary
            ),
            gateway_supervisor_process_probe_boundary_artifact_path=(
                gateway_supervisor_process_probe_boundary_artifact_path
            ),
        )
    probe = build_gateway_live_runtime_probe(
        source_runtime=source_runtime,
        source_runtime_artifact_path=source_runtime_artifact_path,
        gateway_session=gateway_session,
        gateway_session_artifact_path=gateway_session_path,
        lifecycle=lifecycle,
        lifecycle_artifact_path=lifecycle_path,
        observation_stream=observation_stream,
        observation_stream_artifact_path=observation_stream_path,
        recovery_loop=recovery_loop,
        recovery_loop_artifact_path=recovery_loop_path,
        readiness=readiness,
        readiness_artifact_path=readiness_path,
        live_probe_invoked=live_probe_invoked,
        materializer=materializer,
        materializer_artifact_path=materializer_path,
        live_runtime_error=live_runtime_error,
    )
    probe_path = _write_stage_artifact(probe_dir, "gateway_live_runtime_probe.json", probe)
    probe["gateway_live_runtime_probe_artifact_path"] = str(probe_path)
    _write_json(probe_path, probe)
    return probe


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a C5b Gateway live runtime probe.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-live-sitl", action="store_true")
    source.add_argument("--supervisor-runtime-artifact", type=Path)
    parser.add_argument("--wind-mps", type=float, default=DEFAULT_WIND_MPS)
    parser.add_argument("--wind-direction-deg", type=float, default=DEFAULT_WIND_DIRECTION_DEG)
    parser.add_argument("--drift-threshold-m", type=float, default=DEFAULT_DRIFT_THRESHOLD_M)
    parser.add_argument("--live-timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--materialize-live-gateway-processes",
        action="store_true",
        help=(
            "Write source-bound live Gateway observation/recovery process evidence. "
            "Only takes effect with --run-live-sitl."
        ),
    )
    parser.add_argument(
        "--gateway-route-invocation-boundary-artifact",
        type=Path,
        help=(
            "Optional gateway_route_invocation_boundary.v1 artifact. Only takes "
            "effect with --run-live-sitl --materialize-live-gateway-processes."
        ),
    )
    parser.add_argument(
        "--gateway-supervisor-process-probe-boundary-artifact",
        type=Path,
        help=(
            "Optional gateway_supervisor_process_probe_boundary.v1 artifact. "
            "Only takes effect with --run-live-sitl "
            "--materialize-live-gateway-processes."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()

    stamp = _utc_stamp()
    probe_dir = args.output_dir / f"gateway_live_runtime_probe_{stamp}"
    probe_dir.mkdir(parents=True, exist_ok=False)
    live_error: str | None = None
    if args.run_live_sitl:
        runtime_path, live_error = _run_live_supervisor_runtime(
            output_dir=probe_dir / "source_runtime",
            wind_mps=args.wind_mps,
            wind_direction_deg=args.wind_direction_deg,
            drift_threshold_m=args.drift_threshold_m,
            timeout_seconds=args.live_timeout_seconds,
        )
        if runtime_path is None:
            raise GatewayRuntimeProbeError(live_error or "source runtime missing")
        live_probe_invoked = True
    else:
        runtime_path = args.supervisor_runtime_artifact
        live_probe_invoked = False
    probe = build_gateway_live_runtime_probe_chain(
        source_runtime_artifact_path=runtime_path,
        probe_dir=probe_dir,
        live_probe_invoked=live_probe_invoked,
        materialize_live_gateway_processes=(
            args.materialize_live_gateway_processes and live_probe_invoked
        ),
        gateway_route_invocation_boundary_artifact_path=(
            args.gateway_route_invocation_boundary_artifact
        ),
        gateway_supervisor_process_probe_boundary_artifact_path=(
            args.gateway_supervisor_process_probe_boundary_artifact
        ),
        live_runtime_error=live_error,
    )
    print(json.dumps(probe, indent=2, sort_keys=True))
    return 0 if probe.get("gateway_runtime_probe_status") == PROBE_STATUS_OBSERVED else 1


if __name__ == "__main__":
    raise SystemExit(main())
