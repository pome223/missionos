#!/usr/bin/env python3
"""Build an operational envelope cohort from wind Form 3 SITL evidence.

The default mode ingests existing wind Form 3 artifacts. Live PX4/Gazebo SITL
execution is opt-in via --execute-live because a real N>=10 cohort is expensive.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from subprocess import TimeoutExpired
from typing import Any

from scripts.audit_mission_designer_wind_drift_recovery_closed_loop import (
    DEFAULT_DRIFT_THRESHOLD_M,
    DEFAULT_WIND_DIRECTION_DEG,
    DEFAULT_WIND_MPS,
)
from src.runtime.operational_envelope import DEFAULT_MIN_SIM_RUN_COUNT
from src.runtime.operational_envelope_source_ingestion import (
    build_operational_envelope_from_artifacts,
)


SCHEMA_VERSION = "wind_form3_operational_envelope_cohort.v1"
WIND_FORM3_ARTIFACT_NAME = "mission_designer_wind_drift_form3_closed_loop.json"
DEFAULT_MAX_LIVE_RUNS_PER_BATCH = 3
DEFAULT_MAX_LIVE_SESSION_SECONDS = 3600
DEFAULT_REQUIRED_COOLDOWN_MINUTES = 10
_SITL_CONTAINER_MARKERS = ("px4", "gazebo", "gz-sim", "gz_sim")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{prefix}_{digest[:12]}"


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else {}


def _discover_artifacts(paths: list[Path]) -> list[Path]:
    discovered: list[Path] = []
    for path in paths:
        if path.is_file() and path.suffix == ".json":
            discovered.append(path)
        elif path.is_dir():
            discovered.extend(
                sorted(
                    item
                    for item in path.rglob(WIND_FORM3_ARTIFACT_NAME)
                    if item.is_file()
                )
            )
    return sorted(dict.fromkeys(discovered))


def _artifact_path_from_stdout(stdout: str) -> Path | None:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    audit_dir = payload.get("audit_dir")
    if not audit_dir:
        return None
    path = Path(str(audit_dir)) / WIND_FORM3_ARTIFACT_NAME
    return path if path.exists() else None


def _active_px4_gazebo_container_refs() -> dict[str, Any]:
    command = [
        "docker",
        "ps",
        "--format",
        "{{.ID}} {{.Image}} {{.Names}} {{.Status}}",
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, TimeoutExpired) as exc:
        return {
            "cleanup_check_status": "docker_cleanup_check_unavailable",
            "command": command,
            "active_container_refs": [],
            "error": str(exc),
        }
    if result.returncode != 0:
        return {
            "cleanup_check_status": "docker_cleanup_check_failed",
            "command": command,
            "returncode": result.returncode,
            "active_container_refs": [],
            "stderr_tail": result.stderr[-2000:],
        }
    rows = [row.strip() for row in result.stdout.splitlines() if row.strip()]
    active_refs = [
        row
        for row in rows
        if any(marker in row.lower() for marker in _SITL_CONTAINER_MARKERS)
    ]
    return {
        "cleanup_check_status": (
            "active_containers_observed" if active_refs else "clean"
        ),
        "command": command,
        "returncode": result.returncode,
        "active_container_refs": active_refs,
        "stderr_tail": result.stderr[-2000:],
    }


def _run_live_wind_form3_audit(
    *,
    run_index: int,
    output_dir: Path,
    wind_mps: float,
    wind_direction_deg: float,
    drift_threshold_m: float,
    timeout_seconds: int,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/audit_mission_designer_wind_drift_form3_closed_loop.py",
        "--wind-mps",
        str(wind_mps),
        "--wind-direction-deg",
        str(wind_direction_deg),
        "--drift-threshold-m",
        str(drift_threshold_m),
        "--output-dir",
        str(output_dir / "source_runs"),
    ]
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except TimeoutExpired as exc:
        return {
            "run_index": run_index,
            "command": command,
            "started_at": started_at,
            "returncode": 124,
            "artifact_path": "",
            "stdout_tail": (exc.stdout or "")[-2000:]
            if isinstance(exc.stdout, str)
            else "",
            "stderr_tail": (exc.stderr or "")[-2000:]
            if isinstance(exc.stderr, str)
            else "",
            "failure_category": "wind_form3_live_audit_timeout",
        }
    artifact_path = _artifact_path_from_stdout(result.stdout)
    return {
        "run_index": run_index,
        "wind_mps": wind_mps,
        "command": command,
        "started_at": started_at,
        "returncode": result.returncode,
        "artifact_path": str(artifact_path) if artifact_path else "",
        "stdout_tail": result.stdout[-2000:],
        "stderr_tail": result.stderr[-2000:],
        "failure_category": (
            "" if result.returncode == 0 else "wind_form3_live_audit_failed"
        ),
    }


def build_wind_form3_operational_envelope_cohort(
    *,
    artifact_paths: list[Path],
    execute_live: bool = False,
    run_count: int = DEFAULT_MIN_SIM_RUN_COUNT,
    min_sim_run_count: int = DEFAULT_MIN_SIM_RUN_COUNT,
    wind_mps: float = DEFAULT_WIND_MPS,
    wind_mps_values: list[float] | None = None,
    wind_direction_deg: float = DEFAULT_WIND_DIRECTION_DEG,
    drift_threshold_m: float = DEFAULT_DRIFT_THRESHOLD_M,
    output_dir: Path = Path("output/mission_designer_behavior_delta_audits"),
    timeout_seconds: int = 600,
    target_live_success_count: int | None = None,
    target_success_per_wind: int | None = None,
    parameter_normalized_backend_context: bool = False,
    max_live_runs_per_batch: int = DEFAULT_MAX_LIVE_RUNS_PER_BATCH,
    max_live_session_seconds: int = DEFAULT_MAX_LIVE_SESSION_SECONDS,
    required_cooldown_minutes: int = DEFAULT_REQUIRED_COOLDOWN_MINUTES,
) -> dict[str, Any]:
    """Collect wind Form 3 evidence and build an operational envelope cohort."""

    stamp = _utc_stamp()
    cohort_dir = output_dir / f"wind_form3_operational_envelope_cohort_{stamp}"
    cohort_dir.mkdir(parents=True, exist_ok=False)

    live_runs: list[dict[str, Any]] = []
    live_container_cleanup_checks: list[dict[str, Any]] = []
    live_batch_stop_reason = ""
    session_started_at = datetime.now(timezone.utc)
    source_paths = _discover_artifacts(artifact_paths)
    requested_wind_values = wind_mps_values or [wind_mps]
    successful_by_wind: dict[float, int] = {value: 0 for value in requested_wind_values}
    if execute_live:
        successful_live_runs = 0
        for index in range(run_count):
            if max_live_runs_per_batch > 0 and len(live_runs) >= max_live_runs_per_batch:
                live_batch_stop_reason = "live_batch_limit_reached"
                break
            elapsed_seconds = (
                datetime.now(timezone.utc) - session_started_at
            ).total_seconds()
            if (
                max_live_session_seconds > 0
                and elapsed_seconds >= max_live_session_seconds
            ):
                live_batch_stop_reason = "live_session_runtime_limit_reached"
                break
            current_wind_mps = requested_wind_values[index % len(requested_wind_values)]
            live_run = _run_live_wind_form3_audit(
                run_index=index,
                output_dir=cohort_dir,
                wind_mps=current_wind_mps,
                wind_direction_deg=wind_direction_deg,
                drift_threshold_m=drift_threshold_m,
                timeout_seconds=timeout_seconds,
            )
            live_runs.append(live_run)
            artifact_path = live_run.get("artifact_path")
            if artifact_path:
                source_paths.append(Path(str(artifact_path)))
            cleanup_check = _active_px4_gazebo_container_refs()
            cleanup_check["run_index"] = index
            cleanup_check["observed_at"] = datetime.now(timezone.utc).isoformat()
            live_container_cleanup_checks.append(cleanup_check)
            if cleanup_check["cleanup_check_status"] != "clean":
                live_batch_stop_reason = "live_sitl_container_cleanup_blocked"
                break
            if live_run["returncode"] == 0:
                successful_live_runs += 1
                successful_by_wind[current_wind_mps] = (
                    successful_by_wind.get(current_wind_mps, 0) + 1
                )
                if target_success_per_wind is not None and all(
                    successful_by_wind.get(value, 0) >= target_success_per_wind
                    for value in requested_wind_values
                ):
                    break
                if (
                    target_live_success_count is not None
                    and successful_live_runs >= target_live_success_count
                ):
                    break
                continue
            if target_live_success_count is None:
                break

    source_paths = sorted(dict.fromkeys(source_paths))
    if not source_paths:
        ingestion = {
            "schema_version": "operational_envelope_source_ingestion.v1",
            "ingestion_status": "envelope_not_ready",
            "causal_form": "Form 0b",
            "progress_counted": False,
            "artifact_path_count": 0,
            "source_candidate_count": 0,
            "skipped_artifact_count": 0,
            "source_records": [],
            "skipped_artifacts": [],
            "operational_envelope": {
                "schema_version": "operational_envelope.v1",
                "envelope_status": "inactive_insufficient_sim_evidence",
                "accepted_sim_run_count": 0,
                "rejected_sim_run_count": 0,
                "blocked_reasons": ["wind_form3_source_artifacts_missing"],
                "progress_counted": False,
                "causal_verification_transferred": False,
                "physical_form1_required": True,
            },
            "ready_blockers": ["wind_form3_source_artifacts_missing"],
            "safety_boundary": {
                "parameter_knowledge_transfer_only": True,
                "causal_verification_transferred": False,
                "physical_form1_required": True,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
                "delivery_completion_claimed": False,
                "dispatch_authority_created": False,
            },
        }
    else:
        ingestion = build_operational_envelope_from_artifacts(
            artifact_paths=source_paths,
            min_sim_run_count=min_sim_run_count,
            parameter_normalized_backend_context=parameter_normalized_backend_context,
        )

    envelope = ingestion.get("operational_envelope", {})
    envelope_active = ingestion.get("ingestion_status") == "envelope_ready"
    source_summaries: list[dict[str, Any]] = []
    for path in source_paths:
        payload = _read_json(path)
        source_summaries.append(
            {
                "path": str(path),
                "audit_id": payload.get("audit_id"),
                "schema_version": payload.get("schema_version"),
                "causal_form": payload.get("causal_form"),
                "form3_claim_supported": payload.get("form3_claim_supported"),
                "backend_context": payload.get("backend_context"),
                "unsupported_reasons": payload.get("unsupported_reasons", []),
                "ready_blocker": payload.get("ready_blocker"),
            }
        )
    accepted_wind_values = sorted(
        {
            float(item.get("value"))
            for record in ingestion.get("source_records", [])
            for item in record.get("parameter_observations", [])
            if item.get("parameter") == "wind_speed_mps"
            and item.get("value") is not None
        }
    )
    range_envelope_observed = len(accepted_wind_values) >= 2
    checks = {
        "accepted_sim_run_count_gte_min": (
            int(envelope.get("accepted_sim_run_count") or 0) >= min_sim_run_count
        ),
        "all_runs_same_mission_contract": envelope.get("all_runs_same_mission_contract")
        is True,
        "all_runs_same_task_graph": envelope.get("all_runs_same_task_graph") is True,
        "all_runs_same_backend_context": envelope.get("all_runs_same_backend_context")
        is True,
        "envelope_status_active": envelope.get("envelope_status") == "active",
        "transfer_scope_parameter_knowledge_only": envelope.get("transfer_scope")
        == "parameter_knowledge_only",
        "causal_verification_not_transferred": envelope.get(
            "causal_verification_transferred"
        )
        is False,
        "physical_form1_required": envelope.get("physical_form1_required") is True,
        "physical_execution_not_invoked": envelope.get("physical_execution_invoked")
        is False,
        "hardware_target_not_allowed": envelope.get("hardware_target_allowed") is False,
        "delivery_completion_not_claimed": envelope.get("delivery_completion_claimed")
        is False,
        "dispatch_authority_not_created": envelope.get("safety_boundary", {}).get(
            "dispatch_authority_created"
        )
        is False,
        "range_envelope_observed": range_envelope_observed,
    }
    cohort_id = _stable_id(
        "wind_form3_operational_envelope_cohort",
        {
            "source_paths": [str(path) for path in source_paths],
            "run_count": run_count,
            "min_sim_run_count": min_sim_run_count,
            "envelope_ref": envelope.get("envelope_ref"),
            "requested_wind_values": requested_wind_values,
        },
    )
    live_failure_blockers = [
        f"live_run_{run['run_index']}_{run['failure_category']}"
        for run in live_runs
        if run.get("failure_category")
    ]
    skipped_blockers = [
        f"skipped_{item.get('skipped_reason')}"
        for item in ingestion.get("skipped_artifacts", [])
        if item.get("skipped_reason")
    ]
    warnings = sorted(dict.fromkeys([*live_failure_blockers, *skipped_blockers]))
    if live_batch_stop_reason:
        warnings = sorted(dict.fromkeys([*warnings, live_batch_stop_reason]))
    live_batch_blocking_risks = []
    if execute_live:
        live_batch_blocking_risks = [
            *live_failure_blockers,
            *(
                [live_batch_stop_reason]
                if live_batch_stop_reason
                in (
                    "live_sitl_container_cleanup_blocked",
                    "live_session_runtime_limit_reached",
                )
                else []
            ),
        ]
    cohort_ready = envelope_active and not live_batch_blocking_risks
    ready_blockers = [] if cohort_ready else sorted(
        dict.fromkeys(
            [
                *ingestion.get("ready_blockers", []),
                *warnings,
            ]
        )
    )
    next_recommended_wind_mps_values = [
        value
        for value in requested_wind_values
        if target_success_per_wind is None
        or successful_by_wind.get(value, 0) < target_success_per_wind
    ]
    if not next_recommended_wind_mps_values:
        next_recommended_wind_mps_values = requested_wind_values
    failed_or_partial_run_count = sum(
        1 for run in live_runs if run.get("failure_category")
    ) + int(ingestion.get("skipped_artifact_count") or 0)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "cohort_id": cohort_id,
        "cohort_ref": f"wind_form3_operational_envelope_cohort:{cohort_id}",
        "cohort_status": (
            "operational_envelope_active"
            if cohort_ready
            else "operational_envelope_not_ready"
        ),
        "causal_form": "Form 0b",
        "progress_counted": False,
        "source_condition": "wind_form3_closed_loop",
        "run_mode": "live_sitl" if execute_live else "historical_artifacts",
        "live_sitl_execution_requested": execute_live,
        "live_sitl_runs_attempted": len(live_runs),
        "live_batch_stop_reason": live_batch_stop_reason,
        "live_batch_blocking_risks": sorted(dict.fromkeys(live_batch_blocking_risks)),
        "target_live_success_count": target_live_success_count,
        "target_success_per_wind": target_success_per_wind,
        "live_batch_safety": {
            "max_live_runs_per_batch": max_live_runs_per_batch,
            "max_live_session_seconds": max_live_session_seconds,
            "required_cooldown_minutes": required_cooldown_minutes,
            "parallel_sitl_allowed": False,
            "single_px4_gazebo_session_required": True,
            "container_cleanup_required_after_each_run": True,
            "resumable_artifact_accumulation": True,
            "cooldown_required_after_batch": bool(live_batch_stop_reason),
            "continue_automatically_after_batch_limit": False,
            "session_started_at": session_started_at.isoformat(),
        },
        "parameter_normalized_backend_context": parameter_normalized_backend_context,
        "run_count_requested": run_count,
        "min_sim_run_count": min_sim_run_count,
        "wind_mps": wind_mps,
        "wind_mps_values": requested_wind_values,
        "accepted_wind_mps_values": accepted_wind_values,
        "range_envelope_observed": range_envelope_observed,
        "wind_direction_deg": wind_direction_deg,
        "drift_threshold_m": drift_threshold_m,
        "source_artifact_count": len(source_paths),
        "source_artifact_paths": [str(path) for path in source_paths],
        "source_artifact_summaries": source_summaries,
        "live_run_results": live_runs,
        "live_container_cleanup_checks": live_container_cleanup_checks,
        "operational_envelope_source_ingestion": ingestion,
        "operational_envelope": envelope,
        "checks": checks,
        "ready_blockers": ready_blockers,
        "warnings": warnings,
        "safety_boundary": {
            "parameter_knowledge_transfer_only": True,
            "causal_verification_transferred": False,
            "physical_form1_required": True,
            "physical_form1_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "dispatch_authority_created": False,
            "llm_gate_judge_used": False,
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
        },
        "output_dir": str(cohort_dir),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    output_path = cohort_dir / "wind_form3_operational_envelope_cohort.json"
    artifact["output_path"] = str(output_path)
    artifact["live_batch_checkpoint"] = {
        "accepted_sim_run_count": int(envelope.get("accepted_sim_run_count") or 0),
        "accepted_wind_mps_values": accepted_wind_values,
        "failed_or_partial_run_count": failed_or_partial_run_count,
        "skipped_artifact_count": int(ingestion.get("skipped_artifact_count") or 0),
        "current_output_artifact_path": str(output_path),
        "next_recommended_wind_mps_values": next_recommended_wind_mps_values,
        "resume_artifact_dirs": [
            str(cohort_dir / "source_runs"),
            *[str(path) for path in artifact_paths if path.is_dir()],
        ],
    }
    output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a wind Form 3 operational envelope cohort."
    )
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    parser.add_argument("--artifact-dir", action="append", type=Path, default=[])
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--run-count", type=int, default=DEFAULT_MIN_SIM_RUN_COUNT)
    parser.add_argument("--min-sim-run-count", type=int, default=DEFAULT_MIN_SIM_RUN_COUNT)
    parser.add_argument("--wind-mps", type=float, default=DEFAULT_WIND_MPS)
    parser.add_argument("--wind-mps-values", nargs="+", type=float)
    parser.add_argument(
        "--wind-direction-deg", type=float, default=DEFAULT_WIND_DIRECTION_DEG
    )
    parser.add_argument(
        "--drift-threshold-m", type=float, default=DEFAULT_DRIFT_THRESHOLD_M
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--target-live-success-count", type=int)
    parser.add_argument("--target-success-per-wind", type=int)
    parser.add_argument("--parameter-normalized-backend-context", action="store_true")
    parser.add_argument(
        "--max-live-runs-per-batch",
        type=int,
        default=DEFAULT_MAX_LIVE_RUNS_PER_BATCH,
    )
    parser.add_argument(
        "--max-live-session-seconds",
        type=int,
        default=DEFAULT_MAX_LIVE_SESSION_SECONDS,
    )
    parser.add_argument(
        "--required-cooldown-minutes",
        type=int,
        default=DEFAULT_REQUIRED_COOLDOWN_MINUTES,
    )
    args = parser.parse_args()

    artifact = build_wind_form3_operational_envelope_cohort(
        artifact_paths=[*args.artifact, *args.artifact_dir],
        execute_live=args.execute_live,
        run_count=args.run_count,
        min_sim_run_count=args.min_sim_run_count,
        wind_mps=args.wind_mps,
        wind_mps_values=args.wind_mps_values,
        wind_direction_deg=args.wind_direction_deg,
        drift_threshold_m=args.drift_threshold_m,
        output_dir=args.output_dir,
        timeout_seconds=args.timeout_seconds,
        target_live_success_count=args.target_live_success_count,
        target_success_per_wind=args.target_success_per_wind,
        parameter_normalized_backend_context=args.parameter_normalized_backend_context,
        max_live_runs_per_batch=args.max_live_runs_per_batch,
        max_live_session_seconds=args.max_live_session_seconds,
        required_cooldown_minutes=args.required_cooldown_minutes,
    )
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact["cohort_status"] == "operational_envelope_active" else 1


if __name__ == "__main__":
    raise SystemExit(main())
