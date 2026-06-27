"""MissionOS operation registry and guarded Gateway execution surface.

This module exposes selected script-backed MissionOS workflows to the GUI
without turning Mission Designer into a physical execution console. Operations
are synchronous and evidence-oriented; only artifact replay operations can run
from this first GUI surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping
import uuid

from scripts.consume_wind_form3_operational_envelope_for_physical_plan import (
    build_wind_form3_physical_consumption_plan,
)
from src.gateway.live_runtime_boundary import gateway_route_invocation_boundary_supported


ARTIFACT_ROOT = Path("output/mission_designer_behavior_delta_audits")
OUTPUT_ROOT = ARTIFACT_ROOT
RUN_SCHEMA_VERSION = "missionos_operation_run.v1"
REGISTRY_SCHEMA_VERSION = "missionos_operation_registry.v1"

RISK_ARTIFACT_REPLAY_SAFE = "artifact_replay_safe"
RISK_GATEWAY_PROBE_SAFE = "gateway_probe_safe"
RISK_LIVE_SITL_HEAVY = "live_sitl_heavy"

AUTHORITY_FALSE_FIELDS = {
    "physical_execution_invoked": False,
    "physical_form1_claimed": False,
    "hardware_target_allowed": False,
    "dispatch_authority_created": False,
    "delivery_completion_claimed": False,
    "llm_gate_judge_used": False,
    "approval_free_stronger_execution": False,
    "public_sync_performed": False,
}
PHYSICAL_FORM1_REQUIRED = {"physical_form1_required": True}

LIVE_SITL_FALSE_FIELDS = {
    "live_sitl_invoked": False,
    "docker_container_started": False,
    "container_cleanup_checked": False,
    "stale_container_detected": False,
    "run_duration_seconds": 0,
    "batch_run_count": 0,
    "cooldown_required": False,
}

NEGATIVE_EVIDENCE_PATH_MARKERS = (
    "/forged",
    "forged_",
    "/tamper",
    "tamper_",
    "/negative",
    "negative_",
)


@dataclass(frozen=True)
class OperationSpec:
    operation_id: str
    label: str
    description: str
    risk_level: str
    script_path: str
    latest_filename: str
    status_field: str
    runner: str = ""
    allowed_from_gui: bool = True
    live_sitl_required: bool = False
    docker_required: bool = False
    requires_confirmation: bool = False
    disabled_by_default: bool = False


OPERATIONS: tuple[OperationSpec, ...] = (
    OperationSpec(
        operation_id="verify_parameter_normalized_wind_range",
        label="Verify wind range envelope",
        description="Index the latest parameter-normalized wind range verification artifact.",
        risk_level=RISK_ARTIFACT_REPLAY_SAFE,
        script_path="scripts/verify_parameter_normalized_wind_range_envelope.py",
        latest_filename="parameter_normalized_wind_range_envelope_verification.json",
        status_field="verification_status",
    ),
    OperationSpec(
        operation_id="consume_wind_range_physical_seed",
        label="Consume wind range as physical plan seed",
        description="Run the range-envelope consumption script as parameter knowledge only.",
        risk_level=RISK_ARTIFACT_REPLAY_SAFE,
        script_path="scripts/consume_wind_form3_operational_envelope_for_physical_plan.py",
        latest_filename="wind_form3_physical_envelope_consumption_plan.json",
        status_field="plan_status",
        runner="consume_wind_range_physical_seed",
    ),
    OperationSpec(
        operation_id="build_missionos_supervisor_scope_cohort",
        label="Build supervisor scope cohort",
        description="Index the wind/obstacle/payload supervisor scope cohort artifact.",
        risk_level=RISK_ARTIFACT_REPLAY_SAFE,
        script_path="scripts/build_mission_os_supervisor_scope_cohort.py",
        latest_filename="mission_os_supervisor_scope_cohort.json",
        status_field="scope_status",
    ),
    OperationSpec(
        operation_id="build_gateway_full_runtime_readiness",
        label="Build Gateway runtime readiness",
        description="Index the Gateway full-runtime readiness gate artifact.",
        risk_level=RISK_ARTIFACT_REPLAY_SAFE,
        script_path="scripts/build_gateway_full_runtime_readiness.py",
        latest_filename="gateway_full_runtime_readiness.json",
        status_field="readiness_status",
    ),
    OperationSpec(
        operation_id="probe_gateway_route_invocation_boundary",
        label="Probe Gateway route boundary",
        description="Loopback Gateway route process-boundary probe; no PX4/Gazebo.",
        risk_level=RISK_GATEWAY_PROBE_SAFE,
        script_path="scripts/probe_gateway_route_invocation_boundary.py",
        latest_filename="gateway_route_invocation_boundary.json",
        status_field="boundary_status",
        runner="probe_gateway_route_invocation_boundary",
        requires_confirmation=True,
    ),
    OperationSpec(
        operation_id="probe_gateway_supervisor_process_boundary",
        label="Probe Gateway supervisor boundary",
        description="Loopback Gateway supervisor-process probe; no PX4/Gazebo.",
        risk_level=RISK_GATEWAY_PROBE_SAFE,
        script_path="scripts/probe_gateway_supervisor_process_boundary.py",
        latest_filename="gateway_supervisor_process_probe_boundary.json",
        status_field="boundary_status",
        requires_confirmation=True,
    ),
    OperationSpec(
        operation_id="audit_multi_condition_supervisor_runtime",
        label="Run multi-condition SITL supervisor audit",
        description="Live PX4/Gazebo SITL audit. Disabled by default in GUI.",
        risk_level=RISK_LIVE_SITL_HEAVY,
        script_path="scripts/audit_mission_os_multi_condition_supervisor_runtime.py",
        latest_filename="mission_os_multi_condition_supervisor_runtime.json",
        status_field="audit_status",
        live_sitl_required=True,
        docker_required=True,
        requires_confirmation=True,
        disabled_by_default=True,
    ),
)


def _artifact_root() -> Path:
    return Path(os.getenv("MISSIONOS_OPERATIONS_ARTIFACT_ROOT") or ARTIFACT_ROOT)


def _output_root() -> Path:
    return Path(os.getenv("MISSIONOS_OPERATIONS_OUTPUT_ROOT") or OUTPUT_ROOT)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _positive_evidence_path(path: Path) -> bool:
    normalized = f"/{path.as_posix()}"
    return not any(marker in normalized for marker in NEGATIVE_EVIDENCE_PATH_MARKERS)


def _latest_artifact(root: Path, filename: str) -> tuple[Path, dict[str, Any]] | None:
    if not root.exists():
        return None
    candidates = sorted(
        (path for path in root.rglob(filename) if _positive_evidence_path(path)),
        key=lambda path: (path.stat().st_mtime, path.as_posix()),
        reverse=True,
    )
    for path in candidates:
        payload = _read_json(path)
        if payload is not None:
            return path, payload
    return None


def _blocked_reasons(payload: Mapping[str, Any]) -> list[str]:
    reasons = payload.get("blocked_reasons")
    return [str(item) for item in reasons] if isinstance(reasons, list) else []


def _authority_field_violations(payload: Mapping[str, Any]) -> list[str]:
    violations: list[str] = []
    for key in AUTHORITY_FALSE_FIELDS:
        if key in payload and payload.get(key) is not False:
            violations.append(f"{key}_not_false")
    if payload.get("full_gateway_runtime_loop") is True:
        violations.append("full_gateway_runtime_loop_not_false")
    return violations


def _source_artifact_hash_matches(payload: Mapping[str, Any]) -> bool:
    source_path = payload.get("source_runtime_artifact_path")
    source_sha = payload.get("source_runtime_artifact_sha256")
    if not isinstance(source_path, str) or not source_path:
        return False
    if not isinstance(source_sha, str) or not source_sha:
        return False
    try:
        digest = hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
    except OSError:
        return False
    return digest == source_sha


def _gateway_route_invocation_artifact_supported(payload: Mapping[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        gateway_route_invocation_boundary_supported(
            payload,
            gateway_mission_session_ref=str(
                payload.get("gateway_mission_session_ref") or ""
            ),
            supervisor_session_ref=str(payload.get("supervisor_session_ref") or ""),
            source_runtime_artifact_path=str(
                payload.get("source_runtime_artifact_path") or ""
            ),
            source_runtime_artifact_sha256=str(
                payload.get("source_runtime_artifact_sha256") or ""
            ),
        )
        and _source_artifact_hash_matches(payload)
    )


def _unsupported_artifact_reasons(
    spec: OperationSpec,
    payload: Mapping[str, Any],
) -> list[str]:
    reasons = _authority_field_violations(payload)
    if spec.operation_id == "probe_gateway_route_invocation_boundary":
        if not _gateway_route_invocation_artifact_supported(payload):
            reasons.append("gateway_route_invocation_boundary_not_source_bound")
    return reasons


def _status_from_payload(spec: OperationSpec, payload: Mapping[str, Any]) -> str:
    value = payload.get(spec.status_field)
    if isinstance(value, str) and value:
        return value
    for key in (
        "plan_status",
        "verification_status",
        "readiness_status",
        "scope_status",
        "audit_status",
        "gateway_runtime_probe_status",
        "boundary_status",
        "status",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _operation_map() -> dict[str, OperationSpec]:
    return {operation.operation_id: operation for operation in OPERATIONS}


def _spec_payload(spec: OperationSpec) -> dict[str, Any]:
    authority_boundary = _authority_boundary_payload()
    return {
        "operation_id": spec.operation_id,
        "label": spec.label,
        "description": spec.description,
        "risk_level": spec.risk_level,
        "script_path": spec.script_path,
        "latest_filename": spec.latest_filename,
        "status_field": spec.status_field,
        "allowed_from_gui": spec.allowed_from_gui,
        "live_sitl_required": spec.live_sitl_required,
        "docker_required": spec.docker_required,
        "requires_confirmation": spec.requires_confirmation,
        "disabled_by_default": spec.disabled_by_default,
        "authority_boundary": authority_boundary,
        **AUTHORITY_FALSE_FIELDS,
    }


def _authority_boundary_payload(
    *,
    status: str = "safe",
    unknown_fields: list[str] | None = None,
) -> dict[str, Any]:
    unknown = unknown_fields or []
    return {
        "authority_boundary_supported": True,
        "authority_boundary_status": status,
        "authority_true_paths": [],
        "authority_unknown_fields": unknown,
        **AUTHORITY_FALSE_FIELDS,
    }


def _latest_summary(spec: OperationSpec, *, artifact_root: Path) -> dict[str, Any]:
    authority_boundary = _authority_boundary_payload()
    hit = _latest_artifact(artifact_root, spec.latest_filename)
    if hit is None:
        return {
            "operation_id": spec.operation_id,
            "last_status": "missing",
            "latest_artifact_path": "",
            "blocked_reasons": ["latest_artifact_missing"],
            "artifact_summary": {},
            "authority_boundary": authority_boundary,
            **AUTHORITY_FALSE_FIELDS,
        }
    path, payload = hit
    unsupported_reasons = _unsupported_artifact_reasons(spec, payload)
    blocked_reasons = [*_blocked_reasons(payload), *unsupported_reasons]
    return {
        "operation_id": spec.operation_id,
        "last_status": (
            "unsupported" if unsupported_reasons else _status_from_payload(spec, payload)
        ),
        "latest_artifact_path": path.as_posix(),
        "blocked_reasons": blocked_reasons,
        "artifact_summary": {
            **_summarize_artifact(payload),
            "artifact_supported": not unsupported_reasons,
        },
        "authority_boundary": authority_boundary,
        **AUTHORITY_FALSE_FIELDS,
    }


def _summarize_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
    summary_keys = (
        "schema_version",
        "verification_status",
        "plan_status",
        "scope_status",
        "readiness_status",
        "audit_status",
        "gateway_runtime_probe_status",
        "causal_form",
        "progress_counted",
        "range_envelope_consumed",
        "parameter_knowledge_consumed",
        "full_gateway_runtime_loop",
        "cycle_count",
        "blocked_reasons",
    )
    return {key: payload.get(key) for key in summary_keys if key in payload}


def get_missionos_operations_registry(
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    root = artifact_root or _artifact_root()
    operations = []
    operation_evidence_warnings: list[str] = []
    for spec in OPERATIONS:
        payload = _spec_payload(spec)
        payload["last"] = _latest_summary(spec, artifact_root=root)
        last_status = payload["last"].get("last_status")
        if last_status in {"missing", "unsupported"}:
            operation_evidence_warnings.append(f"{spec.operation_id}.last={last_status}")
        operations.append(payload)
    authority_boundary = _authority_boundary_payload(
        status="warning" if operation_evidence_warnings else "safe",
        unknown_fields=operation_evidence_warnings,
    )
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "registry_status": "available",
        "operation_count": len(operations),
        "operations": operations,
        "boundary_notice": (
            "SITL/artifact evidence only. No physical execution, physical Form 1, "
            "hardware authority, dispatch authority creation, delivery completion, "
            "or public sync."
        ),
        "classification": "Form 0b / GUI + Gateway operation surface",
        "progress_counted": False,
        "authority_boundary": authority_boundary,
        **AUTHORITY_FALSE_FIELDS,
    }


def get_missionos_operation_last(operation_id: str) -> dict[str, Any]:
    spec = _resolve_operation(operation_id)
    return {
        "schema_version": "missionos_operation_last.v1",
        **_spec_payload(spec),
        "last": _latest_summary(spec, artifact_root=_artifact_root()),
    }


def _resolve_operation(operation_id: str) -> OperationSpec:
    spec = _operation_map().get(operation_id)
    if spec is None:
        raise KeyError(operation_id)
    return spec


def _new_run_dir(spec: OperationSpec, *, output_root: Path) -> tuple[str, Path]:
    now = datetime.now(timezone.utc)
    run_id = f"missionos_operation_run_{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    return run_id, output_root / "missionos_operations" / spec.operation_id / run_id


def _base_run_record(
    spec: OperationSpec,
    *,
    run_id: str,
    status: str,
    blocked_reasons: list[str],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    authority_boundary = _authority_boundary_payload()
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "operation_id": spec.operation_id,
        "operation_label": spec.label,
        "risk_level": spec.risk_level,
        "script_path": spec.script_path,
        "run_status": status,
        "blocked_reasons": blocked_reasons,
        "created_at": now,
        "completed_at": now,
        "classification": "Form 0b / GUI + Gateway operation surface",
        "progress_counted": False,
        "artifact_path": "",
        "artifact_summary": {},
        "authority_boundary": authority_boundary,
        **AUTHORITY_FALSE_FIELDS,
        **PHYSICAL_FORM1_REQUIRED,
        **LIVE_SITL_FALSE_FIELDS,
    }


def _finalize_record(
    record: dict[str, Any],
    *,
    run_dir: Path,
    output_artifact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if output_artifact is not None:
        record["artifact_summary"] = _summarize_artifact(output_artifact)
        if isinstance(output_artifact.get("output_path"), str):
            record["artifact_path"] = output_artifact["output_path"]
    run_path = run_dir / "missionos_operation_run.json"
    record["operation_run_artifact_path"] = run_path.as_posix()
    _write_json(run_path, record)
    return record


def _float_payload_value(payload: Mapping[str, Any], key: str, default: float) -> float:
    value = payload.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _run_consume_wind_range_physical_seed(
    *,
    spec: OperationSpec,
    payload: Mapping[str, Any],
    run_id: str,
    run_dir: Path,
    artifact_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    latest = _latest_artifact(
        artifact_root,
        "parameter_normalized_wind_range_envelope_verification.json",
    )
    if latest is None:
        record = _base_run_record(
            spec,
            run_id=run_id,
            status="blocked",
            blocked_reasons=["parameter_normalized_wind_range_artifact_missing"],
        )
        return _finalize_record(record, run_dir=run_dir)

    artifact_path, _ = latest
    output = build_wind_form3_physical_consumption_plan(
        cohort_artifact=artifact_path,
        physical_run_ref=str(
            payload.get("physical_run_ref") or "physical_run:gui_range_wind_seed"
        ),
        output_dir=output_root,
        wind_mps=_float_payload_value(payload, "wind_mps", 4.5),
        wind_direction_deg=_float_payload_value(payload, "wind_direction_deg", 90.0),
        drift_threshold_m=_float_payload_value(payload, "drift_threshold_m", 0.85),
    )
    status = "completed" if output.get("plan_status") != "blocked" else "blocked"
    record = _base_run_record(
        spec,
        run_id=run_id,
        status=status,
        blocked_reasons=_blocked_reasons(output),
    )
    record.update(
        {
            "operation_status": output.get("plan_status", ""),
            "input_artifact_path": artifact_path.as_posix(),
            "artifact_path": str(output.get("output_path") or ""),
            "range_envelope_consumed": output.get("range_envelope_consumed") is True,
            "parameter_knowledge_consumed": (
                output.get("parameter_knowledge_consumed") is True
            ),
            "all_parameters_within_envelope": (
                output.get("all_parameters_within_envelope") is True
            ),
        }
    )
    return _finalize_record(record, run_dir=run_dir, output_artifact=output)


def _run_gateway_route_invocation_boundary(
    *,
    spec: OperationSpec,
    run_id: str,
    run_dir: Path,
    output_root: Path,
) -> dict[str, Any]:
    probe_dir = output_root / f"gateway_route_invocation_boundary_gui_{run_id}"
    command = [
        sys.executable,
        "scripts/probe_gateway_route_invocation_boundary.py",
        "--output-dir",
        str(probe_dir),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        returncode = completed.returncode
        stdout_tail = completed.stdout[-4000:]
        stderr_tail = completed.stderr[-4000:]
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout_tail = str(exc.stdout or "")[-4000:]
        stderr_tail = (str(exc.stderr or "") + "\ngateway_probe_timeout")[-4000:]
    artifact_path = probe_dir / "gateway_route_invocation_boundary.json"
    artifact = _read_json(artifact_path) if artifact_path.exists() else None
    blocked_reasons: list[str] = []
    if returncode != 0:
        blocked_reasons.append("gateway_route_invocation_probe_failed")
    if artifact is None:
        blocked_reasons.append("gateway_route_invocation_boundary_artifact_missing")
    if artifact is not None and artifact.get("gateway_process_boundary_observed") is not True:
        blocked_reasons.append("gateway_process_boundary_not_observed")
    if artifact is not None:
        blocked_reasons.extend(_unsupported_artifact_reasons(spec, artifact))

    record = _base_run_record(
        spec,
        run_id=run_id,
        status="completed" if not blocked_reasons else "blocked",
        blocked_reasons=blocked_reasons,
    )
    record.update(
        {
            "command": command,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "artifact_path": artifact_path.as_posix() if artifact is not None else "",
            "gateway_probe_invoked": True,
            "live_sitl_invoked": False,
            "docker_container_started": False,
        }
    )
    return _finalize_record(record, run_dir=run_dir, output_artifact=artifact)


def run_missionos_operation(
    operation_id: str,
    *,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    spec = _resolve_operation(operation_id)
    request_payload = payload or {}
    artifact_root = _artifact_root()
    output_root = _output_root()
    run_id, run_dir = _new_run_dir(spec, output_root=output_root)

    if spec.risk_level == RISK_LIVE_SITL_HEAVY:
        reasons = ["live_sitl_heavy_disabled_by_default"]
        if request_payload.get("allow_live_sitl") is not True:
            reasons.append("explicit_live_sitl_opt_in_required")
        else:
            reasons.append("live_sitl_execution_not_enabled_from_gui_in_this_pr")
        record = _base_run_record(
            spec,
            run_id=run_id,
            status="blocked",
            blocked_reasons=reasons,
        )
        return _finalize_record(record, run_dir=run_dir)

    if spec.risk_level == RISK_GATEWAY_PROBE_SAFE:
        if request_payload.get("confirm_gateway_probe") is not True:
            record = _base_run_record(
                spec,
                run_id=run_id,
                status="blocked",
                blocked_reasons=["gateway_probe_confirmation_required"],
            )
            return _finalize_record(record, run_dir=run_dir)
        if spec.runner == "probe_gateway_route_invocation_boundary":
            return _run_gateway_route_invocation_boundary(
                spec=spec,
                run_id=run_id,
                run_dir=run_dir,
                output_root=output_root,
            )
        record = _base_run_record(
            spec,
            run_id=run_id,
            status="blocked",
            blocked_reasons=["gateway_probe_execution_not_enabled_in_this_pr"],
        )
        return _finalize_record(record, run_dir=run_dir)

    if spec.runner == "consume_wind_range_physical_seed":
        return _run_consume_wind_range_physical_seed(
            spec=spec,
            payload=request_payload,
            run_id=run_id,
            run_dir=run_dir,
            artifact_root=artifact_root,
            output_root=output_root,
        )

    record = _base_run_record(
        spec,
        run_id=run_id,
        status="blocked",
        blocked_reasons=["operation_runner_not_enabled_in_this_pr"],
    )
    return _finalize_record(record, run_dir=run_dir)


def _operation_run_candidates(output_root: Path) -> list[Path]:
    run_root = output_root / "missionos_operations"
    if not run_root.exists():
        return []
    return list(run_root.rglob("missionos_operation_run.json"))


def get_missionos_operation_run(run_id: str) -> dict[str, Any]:
    for path in _operation_run_candidates(_output_root()):
        payload = _read_json(path)
        if payload and payload.get("run_id") == run_id:
            return payload
    raise KeyError(run_id)


def get_missionos_operation_run_artifact(run_id: str) -> dict[str, Any]:
    record = get_missionos_operation_run(run_id)
    artifact_path = record.get("artifact_path")
    if isinstance(artifact_path, str) and artifact_path:
        payload = _read_json(Path(artifact_path))
        if payload is not None:
            return payload
    return record
