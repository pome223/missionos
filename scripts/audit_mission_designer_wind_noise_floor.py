#!/usr/bin/env python3
"""Characterize repeated-measurement noise for the wind trajectory audit.

This is a measurement audit only. It may execute the existing wind trajectory
delta audit under explicit opt-in, but it does not add a response mechanism,
does not change a gate threshold, and does not count MissionOS progress.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from typing import Any, Sequence

from scripts.audit_mission_designer_wind_trajectory_delta import (
    DEFAULT_THRESHOLD_CONFIG_PATH,
    load_form1_wind_threshold_config,
)


SCHEMA_VERSION = "sitl_wind_audit_noise_floor.v1"
LIVE_OPT_IN_ENV = "RUN_MISSIONOS_WIND_NOISE_CHARACTERIZATION"
DEFAULT_OUTPUT_ROOT = Path("output/missionos_sitl_noise_characterization")
DEFAULT_SAMPLE_COUNT = 5
DEFAULT_SAMPLE_RETRIES = 0
DEFAULT_SAMPLE_TIMEOUT_SECONDS = 900


def _utc_now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_json(payload: Any) -> str:
    return _sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _find_delta_artifact(search_root: Path) -> Path:
    candidates = sorted(
        search_root.rglob("drone_behavior_delta_under_wind.json"),
        key=lambda path: (path.stat().st_mtime, path.as_posix()),
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"drone_behavior_delta_under_wind.json not found under {search_root}"
        )
    return candidates[0]


def _metric_from_artifact(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    source_binding = (
        payload.get("source_binding")
        if isinstance(payload.get("source_binding"), dict)
        else {}
    )
    try:
        max_delta = float(metrics.get("max_observed_delta_m"))
    except (TypeError, ValueError):
        max_delta = 0.0
    supported = bool(
        payload.get("schema_version") == "drone_behavior_delta_under_wind.v1"
        and max_delta > 0.0
        and source_binding.get("runtime_invocation_evidence_complete") is True
        and source_binding.get("runtime_pairing_complete") is True
        and source_binding.get("source_boundary_flags_safe") is True
    )
    return {
        "artifact_path": path.as_posix(),
        "artifact_sha256": _file_sha256(path),
        "schema_version": payload.get("schema_version"),
        "audit_status": payload.get("audit_status"),
        "causal_form": payload.get("causal_form"),
        "run_mode": payload.get("run_mode"),
        "progress_counted": payload.get("progress_counted") is True,
        "drone_physics_affected": payload.get("drone_physics_affected") is True,
        "max_observed_delta_m": max_delta,
        "delta_threshold_m": metrics.get("delta_threshold_m"),
        "runtime_invocation_evidence_complete": source_binding.get(
            "runtime_invocation_evidence_complete"
        )
        is True,
        "runtime_pairing_complete": source_binding.get("runtime_pairing_complete") is True,
        "source_boundary_flags_safe": source_binding.get("source_boundary_flags_safe") is True,
        "sample_supported": supported,
    }


def _percentile_nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil((percentile / 100.0) * len(ordered)))
    return ordered[min(rank - 1, len(ordered) - 1)]


def build_noise_floor_artifact(
    samples: Sequence[dict[str, Any]],
    *,
    threshold_config: dict[str, Any],
    generated_at: str | None = None,
    run_mode: str = "existing_artifacts",
    sample_count_requested: int | None = None,
    live_opt_in_env: bool = False,
) -> dict[str, Any]:
    """Build a Form 0b noise-floor artifact from repeated wind audit samples."""

    generated_at = generated_at or _utc_now_iso8601()
    values = [
        float(sample["max_observed_delta_m"])
        for sample in samples
        if sample.get("sample_supported") is True
        and float(sample.get("max_observed_delta_m") or 0.0) > 0.0
    ]
    unsupported_reasons: list[str] = []
    if len(values) < 2:
        unsupported_reasons.append("at_least_two_supported_samples_required")
    if any(sample.get("sample_supported") is not True for sample in samples):
        unsupported_reasons.append("unsupported_sample_present")
    mean = statistics.fmean(values) if values else 0.0
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    minimum = min(values) if values else 0.0
    maximum = max(values) if values else 0.0
    absolute_deviations = [abs(value - mean) for value in values]
    p95_abs_deviation = _percentile_nearest_rank(absolute_deviations, 95.0)
    noise_fraction_95 = p95_abs_deviation / mean if mean > 0.0 else 0.0
    repeatability_ratio_95 = 1.0 + noise_fraction_95 if mean > 0.0 else 0.0
    recommended_improvement_gate_ratio = (
        max(0.0, 1.0 - noise_fraction_95) if mean > 0.0 else 0.0
    )
    supported = not unsupported_reasons
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "audit_status": "noise_floor_observed" if supported else "unsupported",
        "causal_form": "Form 0b",
        "progress_counted": False,
        "drone_physics_affected": False,
        "goal_640_progress_counted": False,
        "run_mode": run_mode,
        "live_opt_in_env": live_opt_in_env,
        "sample_count_requested": sample_count_requested or len(samples),
        "sample_count_observed": len(samples),
        "supported_sample_count": len(values),
        "threshold_config": threshold_config,
        "samples": list(samples),
        "statistics": {
            "metric": "max_observed_delta_m",
            "mean_m": mean,
            "sample_std_m": stdev,
            "min_m": minimum,
            "max_m": maximum,
            "range_m": maximum - minimum if values else 0.0,
            "absolute_deviation_from_mean_m": absolute_deviations,
            "p95_absolute_deviation_from_mean_m": p95_abs_deviation,
            "noise_fraction_95": noise_fraction_95,
            "repeatability_ratio_95": repeatability_ratio_95,
            "recommended_improvement_gate_ratio": recommended_improvement_gate_ratio,
        },
        "boundaries": {
            "automatic_dispatch_executed": False,
            "physical_execution_invoked": False,
            "hardware_target_allowed": False,
            "delivery_completion_claimed": False,
            "public_sync_performed": False,
        },
        "unsupported_reasons": unsupported_reasons,
        "operator_note": (
            "This characterizes repeated-measurement variation for the wind "
            "trajectory audit. It is not a response, dispatch, Form 2a, Form 3, "
            "or #640 progress claim."
        ),
    }


def _run_wind_delta_audit_sample(
    *,
    sample_index: int,
    attempt_index: int,
    output_root: Path,
    threshold_config_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    if attempt_index == 1:
        sample_dir = output_root / f"sample_{sample_index:02d}"
    else:
        sample_dir = output_root / f"sample_{sample_index:02d}_retry_{attempt_index:02d}"
    command = [
        sys.executable,
        "scripts/audit_mission_designer_wind_trajectory_delta.py",
        "--threshold-config",
        str(threshold_config_path),
        "--output-dir",
        str(sample_dir),
    ]
    started_at = _utc_now_iso8601()
    process = subprocess.Popen(
        command,
        cwd=Path.cwd(),
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=30)
        stderr = f"{stderr}\nwind noise characterization sample timeout"
    completed_at = _utc_now_iso8601()
    stdout_path = sample_dir / "wind_noise_sample_stdout.txt"
    stderr_path = sample_dir / "wind_noise_sample_stderr.txt"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    evidence = {
        "schema_version": "runtime_invocation_evidence.v1",
        "invocation_kind": "subprocess",
        "invocation_target": "scripts/audit_mission_designer_wind_trajectory_delta.py",
        "invocation_started_at": started_at,
        "invocation_completed_at": completed_at,
        "invocation_stdout_sha256": _sha256_text(stdout),
        "invocation_stderr_sha256": _sha256_text(stderr),
        "invocation_exit_code": int(process.returncode if process.returncode is not None else -1),
        "command_argv": command,
        "command_argv_sha256": _sha256_json(command),
        "process_pid": int(process.pid),
        "artifact_dir": sample_dir.as_posix(),
        "opt_in_env": True,
        "backend_target": "px4_gazebo_sitl",
    }
    if process.returncode != 0:
        return {
            "sample_index": sample_index,
            "attempt_index": attempt_index,
            "attempt_count": attempt_index,
            "sample_supported": False,
            "sample_error": "wind_delta_audit_subprocess_failed",
            "runtime_invocation_evidence": evidence,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
        }
    artifact_path = _find_delta_artifact(sample_dir)
    sample = _metric_from_artifact(artifact_path)
    sample.update(
        {
            "sample_index": sample_index,
            "attempt_index": attempt_index,
            "attempt_count": attempt_index,
            "runtime_invocation_evidence": evidence,
        }
    )
    return sample


def _attempt_record(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt_index": sample.get("attempt_index"),
        "runtime_invocation_evidence": sample.get("runtime_invocation_evidence"),
        "sample_error": sample.get("sample_error"),
        "sample_supported": sample.get("sample_supported") is True,
        "stderr_tail": sample.get("stderr_tail"),
        "stdout_tail": sample.get("stdout_tail"),
    }


def run_live_samples(
    *,
    output_root: Path,
    sample_count: int,
    sample_retries: int,
    threshold_config_path: Path,
    pause_seconds: float,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    if os.getenv(LIVE_OPT_IN_ENV) != "1":
        raise RuntimeError(f"Set {LIVE_OPT_IN_ENV}=1 to run live noise characterization")
    samples: list[dict[str, Any]] = []
    for index in range(1, sample_count + 1):
        prior_attempts: list[dict[str, Any]] = []
        final_sample: dict[str, Any] | None = None
        for attempt_index in range(1, sample_retries + 2):
            sample = _run_wind_delta_audit_sample(
                sample_index=index,
                attempt_index=attempt_index,
                output_root=output_root,
                threshold_config_path=threshold_config_path,
                timeout_seconds=timeout_seconds,
            )
            if sample.get("sample_supported") is True:
                if prior_attempts:
                    sample["previous_attempts"] = prior_attempts
                    sample["retried_after_failure"] = True
                final_sample = sample
                break
            prior_attempts.append(_attempt_record(sample))
            final_sample = sample
        if final_sample is not None and final_sample.get("sample_supported") is not True:
            final_sample["previous_attempts"] = prior_attempts[:-1]
            final_sample["retried_after_failure"] = bool(final_sample["previous_attempts"])
        samples.append(final_sample or {"sample_index": index, "sample_supported": False})
        if index != sample_count and pause_seconds > 0:
            time.sleep(pause_seconds)
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Characterize repeated-measurement noise for the wind trajectory audit."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--sample-count", type=int, default=DEFAULT_SAMPLE_COUNT)
    parser.add_argument("--sample-retries", type=int, default=DEFAULT_SAMPLE_RETRIES)
    parser.add_argument("--pause-seconds", type=float, default=0.0)
    parser.add_argument("--sample-timeout-seconds", type=int, default=DEFAULT_SAMPLE_TIMEOUT_SECONDS)
    parser.add_argument("--threshold-config", type=Path, default=DEFAULT_THRESHOLD_CONFIG_PATH)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--existing-artifacts", nargs="*", type=Path, default=[])
    args = parser.parse_args()
    if args.sample_count < 1:
        raise SystemExit("--sample-count must be >= 1")
    if args.sample_retries < 0:
        raise SystemExit("--sample-retries must be >= 0")

    threshold_config = load_form1_wind_threshold_config(args.threshold_config)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir / f"wind_audit_noise_floor_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=False)

    if args.run_live:
        samples = run_live_samples(
            output_root=output_dir / "samples",
            sample_count=args.sample_count,
            sample_retries=args.sample_retries,
            threshold_config_path=args.threshold_config,
            pause_seconds=args.pause_seconds,
            timeout_seconds=args.sample_timeout_seconds,
        )
        run_mode = "live_executed"
    else:
        if not args.existing_artifacts:
            raise SystemExit("--existing-artifacts is required unless --run-live is set")
        samples = [
            {
                **_metric_from_artifact(path),
                "sample_index": index,
            }
            for index, path in enumerate(args.existing_artifacts, start=1)
        ]
        run_mode = "existing_artifacts"

    artifact = build_noise_floor_artifact(
        samples,
        threshold_config=threshold_config,
        run_mode=run_mode,
        sample_count_requested=args.sample_count if args.run_live else len(samples),
        live_opt_in_env=args.run_live and os.getenv(LIVE_OPT_IN_ENV) == "1",
    )
    output_path = output_dir / "sitl_wind_audit_noise_floor.json"
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
