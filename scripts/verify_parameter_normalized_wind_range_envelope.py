#!/usr/bin/env python3
"""Verify a parameter-normalized wind Form 3 range envelope.

This smoke intentionally consumes source-bound SITL Form 3 artifacts and checks
whether they produce an active operational envelope in parameter-normalized
backend-context mode. It transfers parameter knowledge only; it does not create
physical authority or claim physical Form 1.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.build_wind_form3_operational_envelope_cohort import (
    build_wind_form3_operational_envelope_cohort,
)
from scripts.mission_designer_form3_envelope_source import (
    build_wind_parameterized_sdf_delta_proof,
)
from src.runtime.operational_envelope import DEFAULT_MIN_SIM_RUN_COUNT


SCHEMA_VERSION = "parameter_normalized_wind_range_envelope_verification.v1"
DEFAULT_MIN_DISTINCT_WIND_VALUES = 2


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _wind_bounds(envelope: dict[str, Any]) -> dict[str, Any]:
    bounds = envelope.get("accepted_parameter_bounds")
    bounds = bounds if isinstance(bounds, dict) else {}
    wind = bounds.get("wind_speed_mps")
    return wind if isinstance(wind, dict) else {}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _verifier_generated_source_copy(path: Path) -> bool:
    return "enriched_sources" in path.parts


def _parameter_value(payload: dict[str, Any], name: str) -> float | None:
    for item in payload.get("parameter_observations") or []:
        if not isinstance(item, dict) or item.get("parameter") != name:
            continue
        return _as_float(item.get("value"))
    return None


def _enrich_wind_form3_source_artifacts(
    *,
    artifact_paths: list[Path],
    output_dir: Path,
) -> tuple[list[Path], list[dict[str, Any]]]:
    """Copy artifacts and add source-bound SDF delta proof when summary exists."""

    enriched_dir = output_dir / "enriched_sources"
    enriched_dir.mkdir(parents=True, exist_ok=True)
    enriched_paths: list[Path] = []
    enrichment_records: list[dict[str, Any]] = []
    for source_path in artifact_paths:
        payload = _read_json(source_path)
        existing_proof = payload.get("parameterized_sdf_delta_proof")
        existing_proof = existing_proof if isinstance(existing_proof, dict) else {}
        target_path = (
            enriched_dir
            / hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()[:12]
            / source_path.name
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "source_path": str(source_path),
            "enriched_path": str(target_path),
            "proof_added": False,
            "proof_status": str(existing_proof.get("proof_status") or ""),
            "enrichment_status": "copied_without_proof",
        }
        if (
            payload.get("condition_kind")
            == "source_bound_wind_drift_form3_closed_loop"
            and "parameterized_sdf_delta_proof" not in payload
        ):
            run_dir_text = str(payload.get("artifact_dir") or "").strip()
            summary_path = Path(run_dir_text) / "summary.json" if run_dir_text else None
            summary = _read_json(summary_path) if summary_path else {}
            wind_mps = _parameter_value(payload, "wind_speed_mps")
            wind_direction = _parameter_value(payload, "wind_direction_deg")
            drift_threshold = _parameter_value(payload, "wind_drift_threshold_m")
            wind_source_ref = str(
                (payload.get("source_refs") or {}).get("wind_application")
                or "simulator_condition_application:mission_designer_wind_gust"
            )
            if summary and wind_mps is not None and wind_direction is not None and drift_threshold is not None:
                proof = build_wind_parameterized_sdf_delta_proof(
                    summary,
                    wind_speed_mps=wind_mps,
                    wind_direction_deg=wind_direction,
                    drift_threshold_m=drift_threshold,
                    source_ref=wind_source_ref,
                )
                payload["parameterized_sdf_delta_proof"] = proof
                record["proof_added"] = True
                record["proof_status"] = str(proof.get("proof_status") or "")
                record["enrichment_status"] = (
                    "source_bound_sdf_delta_proof_added"
                    if proof.get("proof_supported") is True
                    else "sdf_delta_proof_added_but_unsupported"
                )
        target_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        enriched_paths.append(target_path)
        enrichment_records.append(record)
    return enriched_paths, enrichment_records


def verify_parameter_normalized_wind_range_envelope(
    *,
    artifact_paths: list[Path],
    min_sim_run_count: int = DEFAULT_MIN_SIM_RUN_COUNT,
    min_distinct_wind_values: int = DEFAULT_MIN_DISTINCT_WIND_VALUES,
    expected_min_wind_mps: float | None = None,
    expected_max_wind_mps: float | None = None,
    output_dir: Path = Path("output/mission_designer_behavior_delta_audits"),
) -> dict[str, Any]:
    stamp = _utc_stamp()
    verification_dir = output_dir / f"parameter_normalized_wind_range_verification_{stamp}"
    verification_dir.mkdir(parents=True, exist_ok=False)
    discovered_source_paths = []
    for path in artifact_paths:
        if (
            path.is_file()
            and path.suffix == ".json"
            and not _verifier_generated_source_copy(path)
        ):
            discovered_source_paths.append(path)
        elif path.is_dir():
            discovered_source_paths.extend(
                sorted(
                    item
                    for item in path.rglob("mission_designer_wind_drift_form3_closed_loop.json")
                    if item.is_file()
                    and not _verifier_generated_source_copy(item)
                )
            )
    discovered_source_paths = sorted(dict.fromkeys(discovered_source_paths))
    enriched_paths, enrichment_records = _enrich_wind_form3_source_artifacts(
        artifact_paths=discovered_source_paths,
        output_dir=verification_dir,
    )

    cohort = build_wind_form3_operational_envelope_cohort(
        artifact_paths=enriched_paths,
        run_count=min_sim_run_count,
        min_sim_run_count=min_sim_run_count,
        output_dir=verification_dir,
        parameter_normalized_backend_context=True,
    )
    envelope = cohort.get("operational_envelope")
    envelope = envelope if isinstance(envelope, dict) else {}
    wind = _wind_bounds(envelope)
    wind_min = _as_float(wind.get("min_value"))
    wind_max = _as_float(wind.get("max_value"))
    accepted_wind_values = cohort.get("accepted_wind_mps_values")
    accepted_wind_values = (
        accepted_wind_values if isinstance(accepted_wind_values, list) else []
    )

    checks = {
        "cohort_status_active": cohort.get("cohort_status")
        == "operational_envelope_active",
        "range_envelope_observed": cohort.get("range_envelope_observed") is True,
        "min_sim_run_count_satisfied": int(
            envelope.get("accepted_sim_run_count") or 0
        )
        >= min_sim_run_count,
        "distinct_wind_value_count_satisfied": len(accepted_wind_values)
        >= min_distinct_wind_values,
        "backend_context_comparison_parameter_normalized": envelope.get(
            "backend_context_comparison_mode"
        )
        == "parameter_normalized",
        "all_runs_same_backend_context": envelope.get("all_runs_same_backend_context")
        is True,
        "raw_backend_context_not_collapsed": envelope.get(
            "all_runs_same_raw_backend_context"
        )
        is False,
        "parameterized_condition_contexts_complete": envelope.get(
            "parameterized_condition_contexts_complete"
        )
        is True,
        "parameterized_sdf_patch_mapping_valid": envelope.get(
            "parameterized_sdf_patch_mapping_valid"
        )
        is True,
        "transfer_scope_parameter_knowledge_only": envelope.get("transfer_scope")
        == "parameter_knowledge_only",
        "causal_verification_not_transferred": envelope.get(
            "causal_verification_transferred"
        )
        is False,
        "physical_form1_required": envelope.get("physical_form1_required") is True,
        "physical_execution_not_invoked": envelope.get("physical_execution_invoked")
        is False,
        "hardware_target_not_allowed": envelope.get("hardware_target_allowed")
        is False,
        "delivery_completion_not_claimed": envelope.get("delivery_completion_claimed")
        is False,
        "physical_form1_not_claimed": envelope.get("physical_form1_claimed")
        is False,
        "dispatch_authority_not_created": envelope.get("safety_boundary", {}).get(
            "dispatch_authority_created"
        )
        is False,
        "source_bound_sdf_delta_proof_observed": any(
            record.get("proof_status") == "source_bound"
            for record in enrichment_records
        ),
    }
    if expected_min_wind_mps is not None:
        checks["expected_min_wind_observed"] = wind_min == expected_min_wind_mps
    if expected_max_wind_mps is not None:
        checks["expected_max_wind_observed"] = wind_max == expected_max_wind_mps

    blocked_reasons = sorted(
        reason for reason, passed in checks.items() if passed is not True
    )
    status = "verified" if not blocked_reasons else "blocked"
    artifact_id = _stable_id(
        "parameter_normalized_wind_range_envelope_verification",
        {
            "source_artifact_paths": [str(path) for path in artifact_paths],
            "min_sim_run_count": min_sim_run_count,
            "min_distinct_wind_values": min_distinct_wind_values,
            "wind_min": wind_min,
            "wind_max": wind_max,
            "status": status,
        },
    )
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "verification_id": artifact_id,
        "verification_ref": f"parameter_normalized_wind_range_envelope_verification:{artifact_id}",
        "verification_status": status,
        "causal_form": "Form 0b",
        "progress_counted": False,
        "scenario": "parameter_normalized_wind_form3_range_envelope",
        "input_artifact_paths": [str(path) for path in artifact_paths],
        "source_evidence_mode": "source_bound_sitl_form3_artifacts_with_sdf_delta_proof",
        "discovered_source_artifact_paths": [str(path) for path in discovered_source_paths],
        "enriched_source_artifact_paths": [str(path) for path in enriched_paths],
        "source_enrichment_records": enrichment_records,
        "min_sim_run_count": min_sim_run_count,
        "min_distinct_wind_values": min_distinct_wind_values,
        "expected_min_wind_mps": expected_min_wind_mps,
        "expected_max_wind_mps": expected_max_wind_mps,
        "accepted_wind_mps_values": accepted_wind_values,
        "wind_speed_mps_min": wind_min,
        "wind_speed_mps_max": wind_max,
        "operational_envelope_ref": envelope.get("envelope_ref"),
        "cohort_output_path": cohort.get("output_path"),
        "cohort": cohort,
        "checks": checks,
        "blocked_reasons": blocked_reasons,
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
        "verification_dir": str(verification_dir),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    output_path = verification_dir / "parameter_normalized_wind_range_envelope_verification.json"
    artifact["output_path"] = str(output_path)
    output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a parameter-normalized wind Form 3 range envelope."
    )
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    parser.add_argument("--artifact-dir", action="append", type=Path, default=[])
    parser.add_argument("--min-sim-run-count", type=int, default=DEFAULT_MIN_SIM_RUN_COUNT)
    parser.add_argument(
        "--min-distinct-wind-values",
        type=int,
        default=DEFAULT_MIN_DISTINCT_WIND_VALUES,
    )
    parser.add_argument("--expected-min-wind-mps", type=float)
    parser.add_argument("--expected-max-wind-mps", type=float)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()

    artifact = verify_parameter_normalized_wind_range_envelope(
        artifact_paths=[*args.artifact, *args.artifact_dir],
        min_sim_run_count=args.min_sim_run_count,
        min_distinct_wind_values=args.min_distinct_wind_values,
        expected_min_wind_mps=args.expected_min_wind_mps,
        expected_max_wind_mps=args.expected_max_wind_mps,
        output_dir=args.output_dir,
    )
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact["verification_status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
