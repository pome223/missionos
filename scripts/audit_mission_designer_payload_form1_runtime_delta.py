#!/usr/bin/env python3
"""Audit payload-mass Form 1 runtime behavior delta.

This is a payload-only wrapper around the existing obstacle/payload behavior
delta audit. It runs (or reads) light/heavy payload PX4/Gazebo SITL route
summaries and emits a source-bound `drone_behavior_delta_under_payload_mass.v1`
artifact under `artifacts/form1_runtime_delta/`.

The artifact counts progress only when the payload delta is supported and both
light/heavy runs were invoked by this script with runtime invocation evidence.
Existing run dirs can be used for diagnosis, but they remain non-progress unless
fresh invocation evidence is present.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import uuid

from scripts.audit_mission_designer_obstacle_payload_behavior_delta import (
    DEFAULT_DELTA_THRESHOLD_M,
    DEFAULT_PAYLOAD_CLIMB_TIME_DELTA_THRESHOLD_SECONDS,
    DEFAULT_PAYLOAD_HEAVY_KG,
    DEFAULT_PAYLOAD_LIGHT_KG,
    build_payload_behavior_delta,
    build_payload_feasibility_advisory,
    payload_feasibility_advisory_requested,
)


WRAPPER_SCHEMA_VERSION = "missionos_payload_form1_runtime_delta_audit.v1"
FORM1_SCHEMA_VERSION = "drone_behavior_delta_under_payload_mass.v1"
DEFAULT_OUTPUT_DIR = Path("artifacts/form1_runtime_delta")
SMOKE_SCRIPT = Path("scripts/smoke_px4_gazebo_horizontal_route_delivery.py")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _runtime_evidence_complete(evidence: dict[str, Any]) -> bool:
    return bool(
        evidence.get("schema_version") == "runtime_invocation_evidence.v1"
        and evidence.get("invocation_kind") == "subprocess"
        and evidence.get("invocation_exit_code") == 0
        and evidence.get("process_pid")
        and evidence.get("invocation_stdout_sha256")
        and evidence.get("invocation_stderr_sha256")
        and evidence.get("runtime_summary_path")
    )


def _invoke_payload_smoke(
    *,
    run_root: Path,
    label: str,
    payload_mass_kg: float,
    timeout_seconds: int,
) -> tuple[Path, dict[str, Any]]:
    artifact_root = run_root / label
    stdout_path = artifact_root / f"{label}_stdout.txt"
    stderr_path = artifact_root / f"{label}_stderr.txt"
    command_argv = [sys.executable, str(SMOKE_SCRIPT)]
    env = os.environ.copy()
    env.update(
        {
            "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE": "1",
            "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT": str(artifact_root),
            "MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG": str(payload_mass_kg),
        }
    )
    for key in (
        "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE",
        "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_CONTACT_TOPIC",
        "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS",
        "MISSION_DESIGNER_REALISM_WIND_GUST_MPS",
        "MISSION_DESIGNER_REALISM_WIND_VARIANCE",
    ):
        env.pop(key, None)
    started_at = datetime.now(timezone.utc).isoformat()
    artifact_root.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        command_argv,
        cwd=Path.cwd(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=30)
        stderr = f"{stderr}\npayload Form 1 smoke timeout"
    completed_at = datetime.now(timezone.utc).isoformat()
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    reported_run_dir = ""
    runtime_summary_path = ""
    try:
        summary = json.loads(stdout)
        if isinstance(summary, dict):
            reported_run_dir = str(summary.get("artifact_dir") or "")
            runtime_summary_path = str(Path(reported_run_dir) / "summary.json") if reported_run_dir else ""
    except json.JSONDecodeError:
        pass
    evidence = {
        "schema_version": "runtime_invocation_evidence.v1",
        "invocation_kind": "subprocess",
        "invocation_target": str(SMOKE_SCRIPT),
        "invocation_started_at": started_at,
        "invocation_completed_at": completed_at,
        "invocation_stdout_sha256": _sha256_text(stdout),
        "invocation_stderr_sha256": _sha256_text(stderr),
        "invocation_exit_code": int(process.returncode if process.returncode is not None else -1),
        "process_pid": int(process.pid),
        "command_argv": command_argv,
        "command_argv_sha256": _sha256_json(command_argv),
        "runtime_summary_path": runtime_summary_path,
        "stdout_artifact_path": str(stdout_path),
        "stderr_artifact_path": str(stderr_path),
        "backend_target": "px4_gazebo_sitl",
        "requested_payload_mass_kg": payload_mass_kg,
        "artifact_root": str(artifact_root),
    }
    if process.returncode != 0:
        raise RuntimeError(
            "payload Form 1 horizontal route smoke failed "
            f"for {label}: rc={process.returncode}\n"
            f"stdout_tail={stdout[-2000:]}\n"
            f"stderr_tail={stderr[-2000:]}"
        )
    run_dir = Path(reported_run_dir)
    if not run_dir.exists():
        raise FileNotFoundError(f"reported artifact_dir does not exist: {run_dir}")
    return run_dir, evidence


def build_payload_form1_runtime_delta_artifact(
    *,
    light_dir: Path,
    heavy_dir: Path,
    light_payload_kg: float,
    heavy_payload_kg: float,
    delta_threshold_m: float,
    climb_time_delta_threshold_seconds: float,
    light_runtime_invocation_evidence: dict[str, Any] | None = None,
    heavy_runtime_invocation_evidence: dict[str, Any] | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    payload_delta = build_payload_behavior_delta(
        light_dir,
        heavy_dir,
        light_payload_kg=light_payload_kg,
        heavy_payload_kg=heavy_payload_kg,
        delta_threshold_m=delta_threshold_m,
        climb_time_delta_threshold_seconds=climb_time_delta_threshold_seconds,
    )
    light_evidence = light_runtime_invocation_evidence or {}
    heavy_evidence = heavy_runtime_invocation_evidence or {}
    runtime_invocation_evidence_complete = bool(
        _runtime_evidence_complete(light_evidence)
        and _runtime_evidence_complete(heavy_evidence)
    )
    supported = bool(
        payload_delta.get("form1_claim_supported") is True
        and runtime_invocation_evidence_complete
    )
    source_binding = dict(payload_delta.get("source_binding") or {})
    source_binding["runtime_invocation_evidence_complete"] = runtime_invocation_evidence_complete
    source_binding["runtime_invocation_evidence_required_for_progress"] = True
    payload_delta.update(
        {
            "causal_form": "Form 1a" if supported else "Form 0b",
            "progress_counted": supported,
            "drone_physics_affected": supported,
            "form1_scope": "drone_physics_or_mission_behavior",
            "source_binding": source_binding,
            "light_runtime_invocation_evidence": light_evidence,
            "heavy_runtime_invocation_evidence": heavy_evidence,
            "runtime_pairing": {
                "pairing_kind": "payload_light_heavy",
                "condition_only_env_delta": True,
                "condition_env_diff_keys": ["MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG"],
                "light_payload_kg": light_payload_kg,
                "heavy_payload_kg": heavy_payload_kg,
            },
        }
    )
    if not runtime_invocation_evidence_complete and "runtime_invocation_evidence_missing_or_incomplete" not in payload_delta.get("unsupported_reasons", []):
        payload_delta.setdefault("unsupported_reasons", []).append(
            "runtime_invocation_evidence_missing_or_incomplete"
        )
    advisory = build_payload_feasibility_advisory(payload_delta)
    wrapper = {
        "schema_version": WRAPPER_SCHEMA_VERSION,
        "audit_id": f"missionos_payload_form1_runtime_delta:{uuid.uuid4().hex[:12]}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "payload_behavior_delta": payload_delta,
        "payload_feasibility_advisory": advisory
        if payload_feasibility_advisory_requested(advisory)
        else {},
        "progress_counted": supported,
        "drone_physics_affected": supported,
        "goal_640_progress_counted": False,
        "ai_agent_progress_counted": False,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "delivery_completion_claimed": False,
        "public_sync_performed": False,
        "output_artifact_path": str(output_path) if output_path else "",
    }
    return wrapper


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit payload Form 1 runtime behavior delta."
    )
    parser.add_argument("--payload-light-kg", type=float, default=DEFAULT_PAYLOAD_LIGHT_KG)
    parser.add_argument("--payload-heavy-kg", type=float, default=DEFAULT_PAYLOAD_HEAVY_KG)
    parser.add_argument("--delta-threshold-m", type=float, default=DEFAULT_DELTA_THRESHOLD_M)
    parser.add_argument(
        "--payload-climb-time-delta-threshold-seconds",
        type=float,
        default=DEFAULT_PAYLOAD_CLIMB_TIME_DELTA_THRESHOLD_SECONDS,
    )
    parser.add_argument("--payload-light-dir", type=Path)
    parser.add_argument("--payload-heavy-dir", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = Path("output/mission_designer_behavior_delta_audits") / (
        f"payload_form1_runtime_delta_{stamp}"
    )
    light_evidence: dict[str, Any] = {}
    heavy_evidence: dict[str, Any] = {}
    light_dir = args.payload_light_dir
    heavy_dir = args.payload_heavy_dir
    if light_dir is None:
        light_dir, light_evidence = _invoke_payload_smoke(
            run_root=run_root / "runs",
            label="payload_light",
            payload_mass_kg=args.payload_light_kg,
            timeout_seconds=args.timeout_seconds,
        )
    if heavy_dir is None:
        heavy_dir, heavy_evidence = _invoke_payload_smoke(
            run_root=run_root / "runs",
            label="payload_heavy",
            payload_mass_kg=args.payload_heavy_kg,
            timeout_seconds=args.timeout_seconds,
        )
    if light_dir is None or heavy_dir is None:
        raise RuntimeError("payload light/heavy run dirs were not resolved")
    output_path = args.output_dir / f"payload_form1_runtime_delta_{stamp}.v1.json"
    artifact = build_payload_form1_runtime_delta_artifact(
        light_dir=light_dir,
        heavy_dir=heavy_dir,
        light_payload_kg=args.payload_light_kg,
        heavy_payload_kg=args.payload_heavy_kg,
        delta_threshold_m=args.delta_threshold_m,
        climb_time_delta_threshold_seconds=args.payload_climb_time_delta_threshold_seconds,
        light_runtime_invocation_evidence=light_evidence,
        heavy_runtime_invocation_evidence=heavy_evidence,
        output_path=output_path,
    )
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
