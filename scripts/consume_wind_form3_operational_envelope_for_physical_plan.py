#!/usr/bin/env python3
"""Consume a wind Form 3 operational envelope as a physical-test plan seed.

This script does not invoke physical hardware. It records that a candidate
physical test plan consumed SITL-derived parameter knowledge while preserving
the boundary that physical Form 1 must be independently proven.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from src.runtime.operational_envelope import (
    build_physical_run_operational_envelope_consumption,
)


SCHEMA_VERSION = "wind_form3_physical_envelope_consumption_plan.v1"
RANGE_VERIFICATION_SCHEMA_VERSION = (
    "parameter_normalized_wind_range_envelope_verification.v1"
)
RANGE_VERIFICATION_REQUIRED_TRUE_CHECKS = (
    "cohort_status_active",
    "range_envelope_observed",
    "min_sim_run_count_satisfied",
    "distinct_wind_value_count_satisfied",
    "backend_context_comparison_parameter_normalized",
    "all_runs_same_backend_context",
    "raw_backend_context_not_collapsed",
    "parameterized_condition_contexts_complete",
    "parameterized_sdf_patch_mapping_valid",
    "transfer_scope_parameter_knowledge_only",
    "causal_verification_not_transferred",
    "physical_form1_required",
    "physical_execution_not_invoked",
    "hardware_target_not_allowed",
    "delivery_completion_not_claimed",
    "physical_form1_not_claimed",
    "dispatch_authority_not_created",
    "source_bound_sdf_delta_proof_observed",
)
AUTHORITY_FALSE_KEYS = (
    "hardware_target_allowed",
    "physical_execution_invoked",
    "delivery_completion_claimed",
    "causal_verification_transferred",
    "physical_form1_claimed",
    "dispatch_authority_created",
)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else {}


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{prefix}_{digest[:12]}"


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parameter_bounds(envelope: dict[str, Any], parameter: str) -> dict[str, Any]:
    bounds = envelope.get("accepted_parameter_bounds", {})
    return bounds.get(parameter, {}) if isinstance(bounds, dict) else {}


def _input_cohort(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a cohort-like payload from a direct cohort or verification artifact."""

    if payload.get("schema_version") == RANGE_VERIFICATION_SCHEMA_VERSION:
        cohort = payload.get("cohort")
        return cohort if isinstance(cohort, dict) else {}
    return payload


def _input_artifact_ref(payload: dict[str, Any], cohort: dict[str, Any]) -> str:
    for key in ("verification_ref", "cohort_ref", "envelope_ref"):
        value = str(payload.get(key) or cohort.get(key) or "").strip()
        if value:
            return value
    return ""


def _range_envelope_observed(envelope: dict[str, Any]) -> bool:
    wind = _parameter_bounds(envelope, "wind_speed_mps")
    min_value = _as_float(wind.get("min_value"))
    max_value = _as_float(wind.get("max_value"))
    return min_value is not None and max_value is not None and min_value < max_value


def _authority_true_paths(value: Any, *, prefix: str = "") -> list[str]:
    observed: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key in AUTHORITY_FALSE_KEYS and item is True:
                observed.append(path)
            observed.extend(_authority_true_paths(item, prefix=path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            observed.extend(_authority_true_paths(item, prefix=path))
    return observed


def _range_verification_wrapper_support(
    payload: dict[str, Any],
) -> tuple[dict[str, bool], list[str]]:
    """Validate the range-verification wrapper before consuming its cohort.

    The nested cohort is only trusted when the wrapper itself is verified and
    preserves the SOT 9.1 / 9.2 safety boundary.
    """

    if payload.get("schema_version") != RANGE_VERIFICATION_SCHEMA_VERSION:
        return {}, []

    checks = payload.get("checks")
    checks = checks if isinstance(checks, dict) else {}
    safety_boundary = payload.get("safety_boundary")
    safety_boundary = safety_boundary if isinstance(safety_boundary, dict) else {}
    blocked_reasons = payload.get("blocked_reasons")
    blocked_reasons = blocked_reasons if isinstance(blocked_reasons, list) else []

    observed_checks: dict[str, bool] = {
        "verification_status_verified": payload.get("verification_status")
        == "verified",
        "verification_blocked_reasons_empty": len(blocked_reasons) == 0,
        "cohort_present": isinstance(payload.get("cohort"), dict)
        and bool(payload.get("cohort")),
    }
    for key in RANGE_VERIFICATION_REQUIRED_TRUE_CHECKS:
        observed_checks[key] = checks.get(key) is True

    observed_checks.update(
        {
            "safety_parameter_knowledge_transfer_only": safety_boundary.get(
                "parameter_knowledge_transfer_only"
            )
            is True,
            "safety_causal_verification_not_transferred": safety_boundary.get(
                "causal_verification_transferred"
            )
            is False,
            "safety_physical_form1_required": safety_boundary.get(
                "physical_form1_required"
            )
            is True,
            "safety_physical_form1_not_claimed": safety_boundary.get(
                "physical_form1_claimed"
            )
            is False,
            "safety_hardware_target_not_allowed": safety_boundary.get(
                "hardware_target_allowed"
            )
            is False,
            "safety_physical_execution_not_invoked": safety_boundary.get(
                "physical_execution_invoked"
            )
            is False,
            "safety_delivery_completion_not_claimed": safety_boundary.get(
                "delivery_completion_claimed"
            )
            is False,
            "safety_dispatch_authority_not_created": safety_boundary.get(
                "dispatch_authority_created"
            )
            is False,
        }
    )
    return observed_checks, sorted(
        key for key, passed in observed_checks.items() if passed is not True
    )


def _default_point_value(envelope: dict[str, Any], parameter: str) -> float | None:
    bounds = _parameter_bounds(envelope, parameter)
    min_value = _as_float(bounds.get("min_value"))
    max_value = _as_float(bounds.get("max_value"))
    if min_value is None or max_value is None or min_value != max_value:
        return None
    return min_value


def _build_parameter_match(
    envelope: dict[str, Any],
    *,
    wind_mps: float | None,
    wind_direction_deg: float | None,
    drift_threshold_m: float | None,
) -> list[dict[str, Any]]:
    requested = {
        "wind_speed_mps": wind_mps,
        "wind_direction_deg": wind_direction_deg,
        "wind_drift_threshold_m": drift_threshold_m,
    }
    matches: list[dict[str, Any]] = []
    for parameter, candidate_value in requested.items():
        bounds = _parameter_bounds(envelope, parameter)
        min_value = _as_float(bounds.get("min_value"))
        max_value = _as_float(bounds.get("max_value"))
        unit = str(bounds.get("unit") or "")
        if candidate_value is None:
            candidate_value = _default_point_value(envelope, parameter)
        within_range = (
            candidate_value is not None
            and min_value is not None
            and max_value is not None
            and min_value <= candidate_value <= max_value
        )
        delta = None
        if candidate_value is not None and min_value is not None and max_value is not None:
            if candidate_value < min_value:
                delta = round(candidate_value - min_value, 6)
            elif candidate_value > max_value:
                delta = round(candidate_value - max_value, 6)
            else:
                delta = 0.0
        matches.append(
            {
                "condition_kind": parameter,
                "sim_min_value": min_value,
                "sim_max_value": max_value,
                "physical_planned_value": candidate_value,
                "unit": unit,
                "within_safe_range": within_range,
                "delta_from_envelope": delta,
                "deviation_approval_ref": "",
            }
        )
    return matches


def build_wind_form3_physical_consumption_plan(
    *,
    cohort_artifact: Path,
    physical_run_ref: str,
    output_dir: Path,
    wind_mps: float | None = None,
    wind_direction_deg: float | None = None,
    drift_threshold_m: float | None = None,
) -> dict[str, Any]:
    input_artifact = _read_json(cohort_artifact)
    cohort = _input_cohort(input_artifact)
    envelope = cohort.get("operational_envelope", {})
    if not isinstance(envelope, dict):
        envelope = {}
    backend_context = envelope.get("backend_context", {})
    if not isinstance(backend_context, dict):
        backend_context = {}
    now = datetime.now(timezone.utc)
    consumption = build_physical_run_operational_envelope_consumption(
        envelope=envelope,
        physical_run_ref=physical_run_ref,
        backend_context=backend_context,
        now=now,
    )
    parameter_match = _build_parameter_match(
        envelope,
        wind_mps=wind_mps,
        wind_direction_deg=wind_direction_deg,
        drift_threshold_m=drift_threshold_m,
    )
    all_parameters_within_envelope = all(
        item["within_safe_range"] is True for item in parameter_match
    )
    planned_parameters_outside_envelope = [
        item["condition_kind"]
        for item in parameter_match
        if item["within_safe_range"] is not True
    ]
    consumed = consumption.get("consumption_status") == "parameter_knowledge_consumed"
    input_checks = input_artifact.get("checks")
    input_checks = input_checks if isinstance(input_checks, dict) else {}
    wrapper_checks, wrapper_blocked_reasons = _range_verification_wrapper_support(
        input_artifact
    )
    range_verification_wrapper_supported = not wrapper_blocked_reasons
    input_cohort_authority_true_paths = _authority_true_paths(cohort, prefix="cohort")
    input_cohort_authority_boundary_supported = not input_cohort_authority_true_paths
    range_envelope_observed = (
        cohort.get("range_envelope_observed") is True
        or input_checks.get("range_envelope_observed") is True
        or _range_envelope_observed(envelope)
    )
    parameter_knowledge_consumed = (
        consumed
        and range_verification_wrapper_supported
        and input_cohort_authority_boundary_supported
    )
    range_envelope_consumed = (
        parameter_knowledge_consumed
        and range_envelope_observed
        and all_parameters_within_envelope
    )
    blocked_reasons = [
        *([] if consumed else ["operational_envelope_not_consumed"]),
        *(
            []
            if all_parameters_within_envelope
            else ["planned_parameters_outside_envelope"]
        ),
        *(
            []
            if range_verification_wrapper_supported
            else ["range_verification_wrapper_not_supported"]
        ),
        *(
            []
            if input_cohort_authority_boundary_supported
            else ["input_cohort_authority_boundary_not_supported"]
        ),
        *[
            f"range_verification_wrapper_{reason}"
            for reason in wrapper_blocked_reasons
        ],
        *[
            f"input_cohort_authority_true:{path}"
            for path in input_cohort_authority_true_paths
        ],
    ]
    plan_id = _stable_id(
        "wind_form3_physical_envelope_consumption_plan",
        {
            "cohort_artifact": str(cohort_artifact),
            "physical_run_ref": physical_run_ref,
            "operational_envelope_ref": consumption.get("operational_envelope_ref"),
            "parameter_match": parameter_match,
        },
    )
    plan = {
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan_id,
        "plan_ref": f"wind_form3_physical_envelope_consumption_plan:{plan_id}",
        "causal_form": "Form 0b",
        "progress_counted": False,
        "cohort_artifact_path": str(cohort_artifact),
        "input_artifact_schema_version": input_artifact.get("schema_version"),
        "input_artifact_ref": _input_artifact_ref(input_artifact, cohort),
        "range_envelope_verification_ref": (
            input_artifact.get("verification_ref")
            if input_artifact.get("schema_version") == RANGE_VERIFICATION_SCHEMA_VERSION
            else ""
        ),
        "cohort_ref": cohort.get("cohort_ref"),
        "physical_run_ref": physical_run_ref,
        "operational_envelope_ref": consumption.get("operational_envelope_ref"),
        "physical_run_operational_envelope_consumption": consumption,
        "parameter_match": parameter_match,
        "all_parameters_within_envelope": all_parameters_within_envelope,
        "planned_parameters_outside_envelope": planned_parameters_outside_envelope,
        "range_envelope_observed": range_envelope_observed,
        "range_envelope_consumed": range_envelope_consumed,
        "range_verification_wrapper_supported": range_verification_wrapper_supported,
        "range_verification_wrapper_checks": wrapper_checks,
        "range_verification_wrapper_blocked_reasons": wrapper_blocked_reasons,
        "input_cohort_authority_boundary_supported": (
            input_cohort_authority_boundary_supported
        ),
        "input_cohort_authority_true_paths": input_cohort_authority_true_paths,
        "parameter_knowledge_consumed": parameter_knowledge_consumed,
        "plan_status": (
            "physical_test_plan_seed_ready"
            if parameter_knowledge_consumed and all_parameters_within_envelope
            else "blocked"
        ),
        "blocked_reasons": blocked_reasons,
        "transfer_scope": "parameter_knowledge_only",
        "envelope_consumed_but_not_authoritative": parameter_knowledge_consumed,
        "causal_verification_transferred": False,
        "physical_form1_required": True,
        "physical_form1_claimed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "delivery_completion_claimed": False,
        "dispatch_authority_created": False,
        "safety_boundary": {
            "parameter_knowledge_transfer_only": True,
            "causal_verification_transferred": False,
            "physical_form1_required": True,
            "physical_form1_claimed": False,
            "physical_backend_execution_allowed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "dispatch_authority_created": False,
            "llm_gate_judge_used": False,
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
        },
        "observed_at": now.isoformat(),
    }
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    plan_suffix = plan_id.rsplit("_", 1)[-1]
    plan_dir = output_dir / f"wind_form3_physical_envelope_consumption_plan_{stamp}_{plan_suffix}"
    plan_dir.mkdir(parents=True, exist_ok=False)
    output_path = plan_dir / "wind_form3_physical_envelope_consumption_plan.json"
    plan["output_path"] = str(output_path)
    output_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Consume a wind Form 3 envelope as a physical-test plan seed."
    )
    parser.add_argument("--cohort-artifact", required=True, type=Path)
    parser.add_argument(
        "--physical-run-ref",
        default="physical_run:wind_form3_point_envelope_seed",
    )
    parser.add_argument("--wind-mps", type=float)
    parser.add_argument("--wind-direction-deg", type=float)
    parser.add_argument("--drift-threshold-m", type=float)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()
    plan = build_wind_form3_physical_consumption_plan(
        cohort_artifact=args.cohort_artifact,
        physical_run_ref=args.physical_run_ref,
        output_dir=args.output_dir,
        wind_mps=args.wind_mps,
        wind_direction_deg=args.wind_direction_deg,
        drift_threshold_m=args.drift_threshold_m,
    )
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0 if plan["plan_status"] == "physical_test_plan_seed_ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
