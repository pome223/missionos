#!/usr/bin/env python3
"""Audit whether the battery-threshold applicator changes PX4 battery behavior.

This audit deliberately separates three facts:

* the requested remaining percent is not spoofed into PX4 battery telemetry;
* the threshold applicator can materialize PX4 warning params and warning state;
* warning-state behavior is not the same as trajectory, failsafe, recovery, or
  delivery-completion authority.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any


BATTERY_SCHEMA_VERSION = "drone_behavior_delta_under_battery_threshold.v1"
AUDIT_SCHEMA_VERSION = "mission_designer_battery_threshold_behavior_delta_audit.v1"
BATTERY_APPLICATION_SCHEMA = "simulator_condition_application.v1"
BATTERY_APPLICATION_ID = "simulator_condition_application:mission_designer_battery_threshold"
BATTERY_EVIDENCE_SCHEMA = "observed_vehicle_condition_evidence.v1"
BATTERY_EVIDENCE_ID = "observed_vehicle_condition_evidence:mission_designer_battery_threshold"
BATTERY_CONDITION_KIND = "battery_threshold"
BATTERY_CONDITION_REF = "battery_condition_profile:mission_designer_battery_threshold"
DEFAULT_WARNING_LEVEL = 1
DEFAULT_DRAIN_RATE_DELTA_THRESHOLD_PERCENT_PER_MIN = 1.0
UNSAFE_SOURCE_AUTHORITY_KEYS = (
    "auto_gate",
    "task_status_mutated",
    "gate_status_mutated",
    "dropoff_verified",
    "delivery_completion_claimed",
    "payload_dropoff_success_claimed",
    "hardware_target_allowed",
    "physical_execution_invoked",
    "actuator_execution_performed",
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _summary_path(run_dir: Path) -> Path:
    path = run_dir / "summary.json"
    if not path.exists():
        raise FileNotFoundError(f"summary.json not found under {run_dir}")
    return path


def _observed_at_epoch_seconds(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _nested_true_keys(payload: Any, keys: set[str]) -> list[str]:
    observed: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in keys and value is True:
                observed.append(key)
            observed.extend(_nested_true_keys(value, keys))
    elif isinstance(payload, list):
        for value in payload:
            observed.extend(_nested_true_keys(value, keys))
    return observed


def _nested_delivery_false_observed(summary: dict[str, Any]) -> bool:
    if summary.get("delivery_completion_claimed") is False:
        return True
    candidate_keys = (
        "battery_simulator_condition_application",
        "observed_battery_condition_evidence",
        "observed_vehicle_condition_evidence",
    )
    return any(
        isinstance(summary.get(key), dict)
        and summary[key].get("delivery_completion_claimed") is False
        for key in candidate_keys
    )


def _nested_delivery_true_observed(summary: dict[str, Any]) -> bool:
    if summary.get("delivery_completion_claimed") is True:
        return True
    return any(
        isinstance(value, dict) and value.get("delivery_completion_claimed") is True
        for value in summary.values()
    )


def _load_pose_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        battery_status = payload.get("battery_status")
        if not isinstance(battery_status, dict):
            continue
        rows.append(
            {
                "phase": payload.get("phase"),
                "sample_index": payload.get("sample_index"),
                "observed_at": payload.get("observed_at"),
                "battery_status": battery_status,
            }
        )
    return rows


def _battery_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    for fallback_index, row in enumerate(rows):
        battery = row.get("battery_status") or {}
        if battery.get("battery_status_observed") is not True:
            continue
        warning = _as_int(battery.get("battery_warning"))
        remaining = _as_float(battery.get("battery_remaining_percent"))
        observed_at_seconds = _observed_at_epoch_seconds(row.get("observed_at"))
        series.append(
            {
                "sample_index": _as_int(row.get("sample_index")) or fallback_index,
                "observed_at": row.get("observed_at"),
                "observed_at_seconds": observed_at_seconds,
                "battery_warning": warning,
                "battery_remaining_percent": remaining,
                "battery_voltage_v": _as_float(battery.get("battery_voltage_v")),
                "battery_current_a": _as_float(battery.get("battery_current_a")),
            }
        )
    return series


def _max_warning(series: list[dict[str, Any]], fallback: Any = None) -> int | None:
    warnings = [
        int(item["battery_warning"])
        for item in series
        if item.get("battery_warning") is not None
    ]
    fallback_warning = _as_int(fallback)
    if fallback_warning is not None:
        warnings.append(fallback_warning)
    return max(warnings) if warnings else None


def _first_warning_elapsed_seconds(
    series: list[dict[str, Any]], *, warning_level: int
) -> float | None:
    timed = [
        item
        for item in series
        if item.get("observed_at_seconds") is not None
    ]
    if not timed:
        return None
    start = float(timed[0]["observed_at_seconds"])
    for item in timed:
        warning = item.get("battery_warning")
        if warning is not None and int(warning) >= int(warning_level):
            return float(item["observed_at_seconds"]) - start
    return None


def _drain_rate_percent_per_min(series: list[dict[str, Any]]) -> float | None:
    timed = [
        item
        for item in series
        if item.get("observed_at_seconds") is not None
        and item.get("battery_remaining_percent") is not None
    ]
    if len(timed) < 2:
        return None
    elapsed = float(timed[-1]["observed_at_seconds"]) - float(timed[0]["observed_at_seconds"])
    if elapsed <= 0:
        return None
    remaining_delta = float(timed[0]["battery_remaining_percent"]) - float(
        timed[-1]["battery_remaining_percent"]
    )
    return remaining_delta / (elapsed / 60.0)


def _summarize_run(run_dir: Path, *, label: str) -> dict[str, Any]:
    summary = _read_json(_summary_path(run_dir))
    pose_path = run_dir / "pose_samples.jsonl"
    if not pose_path.exists():
        raise FileNotFoundError(f"pose_samples.jsonl not found under {run_dir}")
    series = _battery_series(_load_pose_rows(pose_path))
    profile = summary.get("battery_condition_profile") or {}
    application = summary.get("battery_simulator_condition_application") or {}
    evidence = summary.get("observed_battery_condition_evidence") or summary.get(
        "observed_vehicle_condition_evidence"
    ) or {}
    observed = evidence.get("observed") or {}
    requested = profile.get("requested") or {}
    unsafe_authority_flags = sorted(
        set(_nested_true_keys(summary, set(UNSAFE_SOURCE_AUTHORITY_KEYS)))
    )
    return {
        "label": label,
        "artifact_dir": str(run_dir),
        "task_status": summary.get("task_status"),
        "final_status": summary.get("final_status"),
        "actual_px4_gazebo_horizontal_smoke_observed": summary.get(
            "actual_px4_gazebo_horizontal_smoke_observed"
        ),
        "dropoff_region_reached": summary.get("dropoff_region_reached"),
        "blocked_reasons": summary.get("blocked_reasons", []),
        "battery_requested_present": profile.get("requested_present"),
        "battery_requested_warning_level": requested.get("requested_warning_level"),
        "battery_requested_remaining_percent": requested.get(
            "requested_remaining_percent"
        ),
        "battery_application_schema_version": application.get("schema_version"),
        "battery_application_id": application.get("application_id"),
        "battery_application_status": application.get("application_status"),
        "battery_condition_kind": application.get("condition_kind"),
        "battery_requested_condition_ref": application.get("requested_condition_ref"),
        "battery_applied": application.get("applied") or {},
        "battery_unsupported_reasons": application.get("unsupported_reasons", []),
        "battery_evidence_schema_version": evidence.get("schema_version"),
        "battery_evidence_id": evidence.get("evidence_id"),
        "battery_evidence_condition_kind": evidence.get("condition_kind"),
        "battery_evidence_application_ref": evidence.get("application_ref"),
        "battery_evidence_requested_condition_ref": evidence.get(
            "requested_condition_ref"
        ),
        "battery_observation_status": evidence.get("observation_status"),
        "battery_observed": observed,
        "battery_series_sample_count": len(series),
        "battery_max_warning": _max_warning(
            series,
            observed.get("observed_warning")
            or (observed.get("battery_status") or {}).get("battery_warning"),
        ),
        "battery_first_warning_elapsed_seconds": None,
        "battery_drain_rate_percent_per_min": _drain_rate_percent_per_min(series),
        "pose_trace_path": str(pose_path),
        "hardware_target_allowed": summary.get("hardware_target_allowed"),
        "physical_execution_invoked": summary.get("physical_execution_invoked"),
        "delivery_completion_claimed": _nested_delivery_true_observed(summary),
        "delivery_completion_claimed_explicit_false": _nested_delivery_false_observed(
            summary
        ),
        "unsafe_source_authority_flags_observed": unsafe_authority_flags,
    }


def _run_has_interpretable_outcome(run: dict[str, Any]) -> bool:
    return (
        run.get("actual_px4_gazebo_horizontal_smoke_observed") is True
        and run.get("task_status") in {"completed", "blocked"}
        and run.get("final_status") in {"completed", "blocked"}
    )


def _source_boundary_flags_safe(run: dict[str, Any]) -> bool:
    return (
        run.get("hardware_target_allowed") is False
        and run.get("physical_execution_invoked") is False
        and run.get("delivery_completion_claimed") is False
        and run.get("delivery_completion_claimed_explicit_false") is True
        and not run.get("unsafe_source_authority_flags_observed")
    )


def _source_binding(
    nominal: dict[str, Any],
    battery: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    source_runs_interpretable = all(
        _run_has_interpretable_outcome(run) for run in (nominal, battery)
    )
    source_boundary_flags_safe = all(
        _source_boundary_flags_safe(run) for run in (nominal, battery)
    )
    reasons: list[str] = []
    if not source_runs_interpretable:
        reasons.append("source_runs_not_interpretable")
    if not source_boundary_flags_safe:
        reasons.append("source_run_forbidden_authority_flags_observed")
    return (
        {
            "source_runs_interpretable": source_runs_interpretable,
            "source_boundary_flags_safe": source_boundary_flags_safe,
            "source_unsafe_authority_flags_observed": sorted(
                set(nominal.get("unsafe_source_authority_flags_observed", []))
                | set(battery.get("unsafe_source_authority_flags_observed", []))
            ),
        },
        reasons,
    )


def _battery_threshold_not_requested(run: dict[str, Any]) -> bool:
    return run.get("battery_requested_present") in {None, False} and run.get(
        "battery_application_status"
    ) in {None, "not_requested"}


def _battery_application_observed(run: dict[str, Any]) -> bool:
    applied = run.get("battery_applied") or {}
    observed = run.get("battery_observed") or {}
    return (
        run.get("battery_application_schema_version") == BATTERY_APPLICATION_SCHEMA
        and run.get("battery_application_id") == BATTERY_APPLICATION_ID
        and run.get("battery_condition_kind") == BATTERY_CONDITION_KIND
        and run.get("battery_requested_condition_ref") == BATTERY_CONDITION_REF
        and run.get("battery_application_status") == "applied_with_approximations"
        and run.get("battery_evidence_schema_version") == BATTERY_EVIDENCE_SCHEMA
        and run.get("battery_evidence_id") == BATTERY_EVIDENCE_ID
        and run.get("battery_evidence_condition_kind") == BATTERY_CONDITION_KIND
        and run.get("battery_evidence_application_ref") == BATTERY_APPLICATION_ID
        and run.get("battery_evidence_requested_condition_ref") == BATTERY_CONDITION_REF
        and applied.get("battery_warning_threshold_materialized") is True
        and observed.get("battery_warning_threshold_materialized") is True
        and observed.get("requested_remaining_does_not_spoof_px4_battery_status")
        is True
        and observed.get("battery_remaining_target_materialized") is False
    )


def build_battery_threshold_behavior_delta(
    nominal_dir: Path,
    battery_dir: Path,
    *,
    expected_warning_level: int = DEFAULT_WARNING_LEVEL,
    drain_rate_delta_threshold_percent_per_min: float = (
        DEFAULT_DRAIN_RATE_DELTA_THRESHOLD_PERCENT_PER_MIN
    ),
) -> dict[str, Any]:
    nominal = _summarize_run(nominal_dir, label="battery_nominal")
    battery = _summarize_run(battery_dir, label="battery_threshold")
    expected_warning_level = int(
        battery.get("battery_requested_warning_level") or expected_warning_level
    )
    nominal_series = _battery_series(_load_pose_rows(Path(nominal["pose_trace_path"])))
    battery_series = _battery_series(_load_pose_rows(Path(battery["pose_trace_path"])))
    nominal["battery_first_warning_elapsed_seconds"] = _first_warning_elapsed_seconds(
        nominal_series,
        warning_level=expected_warning_level,
    )
    battery["battery_first_warning_elapsed_seconds"] = _first_warning_elapsed_seconds(
        battery_series,
        warning_level=expected_warning_level,
    )
    source_binding, unsupported_reasons = _source_binding(nominal, battery)
    if not _battery_threshold_not_requested(nominal):
        unsupported_reasons.append("nominal_battery_threshold_not_absent")
    if not _battery_application_observed(battery):
        unsupported_reasons.append("battery_threshold_application_not_source_bound_observed")

    nominal_max_warning = nominal.get("battery_max_warning")
    battery_max_warning = battery.get("battery_max_warning")
    warning_delta_observed = (
        battery_max_warning is not None
        and int(battery_max_warning) >= expected_warning_level
        and (
            nominal_max_warning is None
            or int(nominal_max_warning) < expected_warning_level
        )
    )
    battery_warning_time_observed = (
        battery.get("battery_first_warning_elapsed_seconds") is not None
    )
    drain_delta = None
    if (
        nominal.get("battery_drain_rate_percent_per_min") is not None
        and battery.get("battery_drain_rate_percent_per_min") is not None
    ):
        drain_delta = (
            float(battery["battery_drain_rate_percent_per_min"])
            - float(nominal["battery_drain_rate_percent_per_min"])
        )
    drain_rate_delta_observed = (
        drain_delta is not None
        and abs(drain_delta) >= drain_rate_delta_threshold_percent_per_min
    )
    mission_outcome_changed = (
        nominal.get("task_status") != battery.get("task_status")
        or nominal.get("final_status") != battery.get("final_status")
        or nominal.get("dropoff_region_reached") != battery.get("dropoff_region_reached")
    )
    px4_warning_behavior_affected = warning_delta_observed and battery_warning_time_observed
    drone_physics_affected = bool(mission_outcome_changed or drain_rate_delta_observed)
    raw_behavior_delta_observed = bool(
        px4_warning_behavior_affected or drone_physics_affected
    )
    behavior_effect_basis = []
    if px4_warning_behavior_affected:
        behavior_effect_basis.append("px4_battery_warning_state_delta_observed")
    if drain_rate_delta_observed:
        behavior_effect_basis.append("battery_remaining_drain_rate_delta_above_threshold")
    if mission_outcome_changed:
        behavior_effect_basis.append("mission_outcome_changed")
    supported = raw_behavior_delta_observed and not unsupported_reasons
    status = (
        "battery_threshold_behavior_delta_observed"
        if supported
        else "battery_threshold_behavior_delta_below_threshold"
        if not unsupported_reasons
        else "unsupported"
    )
    return {
        "schema_version": BATTERY_SCHEMA_VERSION,
        "audit_id": "drone_behavior_delta_under_battery_threshold:mission_designer_battery_threshold",
        "condition_kind": "battery_threshold_behavior_delta",
        "audit_status": status,
        "requested": {
            "nominal_battery_threshold_requested": False,
            "condition_battery_threshold_requested": True,
            "expected_warning_level": expected_warning_level,
            "drain_rate_delta_threshold_percent_per_min": (
                drain_rate_delta_threshold_percent_per_min
            ),
        },
        "runs": [nominal, battery],
        "source_binding": source_binding,
        "metrics": {
            "nominal_max_warning": nominal_max_warning,
            "battery_max_warning": battery_max_warning,
            "nominal_first_warning_elapsed_seconds": nominal.get(
                "battery_first_warning_elapsed_seconds"
            ),
            "battery_first_warning_elapsed_seconds": battery.get(
                "battery_first_warning_elapsed_seconds"
            ),
            "nominal_drain_rate_percent_per_min": nominal.get(
                "battery_drain_rate_percent_per_min"
            ),
            "battery_drain_rate_percent_per_min": battery.get(
                "battery_drain_rate_percent_per_min"
            ),
            "battery_drain_rate_delta_percent_per_min": drain_delta,
            "drain_rate_delta_threshold_percent_per_min": (
                drain_rate_delta_threshold_percent_per_min
            ),
            "warning_delta_observed": warning_delta_observed,
            "drain_rate_delta_observed": drain_rate_delta_observed,
            "mission_outcome_changed": mission_outcome_changed,
        },
        "behavior_effect_basis": behavior_effect_basis,
        "px4_battery_warning_state_affected": px4_warning_behavior_affected,
        "drone_physics_affected": drone_physics_affected,
        "drone_behavior_affected": supported,
        "battery_threshold_behavior_delta_observed": supported,
        "raw_behavior_delta_observed": raw_behavior_delta_observed,
        "form1_claim_supported": supported,
        "form1_scope": (
            "px4_battery_warning_state"
            if px4_warning_behavior_affected and not drone_physics_affected
            else "drone_physics_or_mission_behavior"
            if drone_physics_affected
            else "none"
        ),
        "form4_reclassification_candidate": (
            not supported and not unsupported_reasons
        ),
        "unsupported_reasons": unsupported_reasons,
        "verifier": False,
        "candidate": False,
        "approval_chain": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "delivery_completion_claimed": False,
        "payload_dropoff_success_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def build_battery_threshold_behavior_delta_audit(
    nominal_dir: Path,
    battery_dir: Path,
) -> dict[str, Any]:
    battery_delta = build_battery_threshold_behavior_delta(nominal_dir, battery_dir)
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "audit_id": "mission_designer_battery_threshold_behavior_delta_audit:mission_designer_battery_threshold",
        "battery_threshold_audit": battery_delta,
        "form1_claim_supported": battery_delta["form1_claim_supported"],
        "px4_battery_warning_state_affected": battery_delta[
            "px4_battery_warning_state_affected"
        ],
        "drone_physics_affected": battery_delta["drone_physics_affected"],
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nominal-run-dir", required=True, type=Path)
    parser.add_argument("--battery-run-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    audit = build_battery_threshold_behavior_delta_audit(
        args.nominal_run_dir,
        args.battery_run_dir,
    )
    _write_json(args.output, audit)
    print(json.dumps(audit, indent=2, sort_keys=True))
    print("BATTERY_THRESHOLD_AUDIT_JSON " + json.dumps(audit, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
