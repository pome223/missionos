"""Ingest historical Mission Designer artifacts into operational envelopes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from src.runtime.operational_envelope import (
    REQUIRED_BACKEND_CONTEXT_KEYS,
    build_operational_envelope,
)


OPERATIONAL_ENVELOPE_SOURCE_INGESTION_SCHEMA_VERSION = (
    "operational_envelope_source_ingestion.v1"
)
_CANDIDATE_CAUSAL_FORMS = ("Form 1a", "Form 1", "Form 1b", "Form 3", "Form 4")
_WIND_FORM3_CONDITION_KIND = "source_bound_wind_drift_form3_closed_loop"
_WIND_PARAMETER_NAMES = (
    "wind_speed_mps",
    "wind_direction_deg",
    "wind_drift_threshold_m",
)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def discover_json_artifacts(paths: Sequence[Path]) -> list[Path]:
    discovered: list[Path] = []
    for path in paths:
        if path.is_dir():
            discovered.extend(sorted(item for item in path.rglob("*.json") if item.is_file()))
        elif path.is_file() and path.suffix == ".json":
            discovered.append(path)
    return sorted(dict.fromkeys(discovered))


def _source_bound_from_checks(artifact: Mapping[str, Any]) -> bool:
    if artifact.get("source_bound") is True:
        return True
    if artifact.get("form3_claim_supported") is True and _as_mapping(
        artifact.get("source_refs")
    ):
        return True
    checks = _as_mapping(artifact.get("checks"))
    if checks:
        failing_source_checks = [
            key
            for key, value in checks.items()
            if ("source_bound" in str(key) or "source_refs" in str(key))
            and value is not True
        ]
        passing_source_checks = [
            key
            for key, value in checks.items()
            if ("source_bound" in str(key) or "source_refs" in str(key))
            and value is True
        ]
        return bool(passing_source_checks) and not failing_source_checks
    return False


def _default_backend_context(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    context = dict(_as_mapping(overrides))
    return {key: context.get(key) for key in REQUIRED_BACKEND_CONTEXT_KEYS}


def _stable_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _parameter_value_map(observations: Any) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    if not isinstance(observations, list):
        return values
    for item in observations:
        mapping = _as_mapping(item)
        parameter = str(mapping.get("parameter") or "").strip()
        if parameter in _WIND_PARAMETER_NAMES and mapping.get("value") is not None:
            values[parameter] = {
                "value": mapping.get("value"),
                "unit": str(mapping.get("unit") or ""),
                "source_ref": str(mapping.get("source_ref") or ""),
            }
    return values


def _parameterized_sdf_delta_proof_supported(
    artifact: Mapping[str, Any],
    *,
    backend_context: Mapping[str, Any],
    parameter_values: Mapping[str, Mapping[str, Any]],
) -> bool:
    proof = _as_mapping(artifact.get("parameterized_sdf_delta_proof"))
    if proof.get("schema_version") != "parameterized_sdf_delta_proof.v1":
        return False
    if proof.get("proof_status") != "source_bound" or proof.get("proof_supported") is not True:
        return False
    if str(proof.get("raw_sdf_hash") or "").strip() != str(
        backend_context.get("sdf_hash") or ""
    ).strip():
        return False
    if str(proof.get("applied_file_sha256") or "").strip() != str(
        backend_context.get("sdf_hash") or ""
    ).strip():
        return False
    proof_values = _as_mapping(proof.get("parameter_values"))
    for name, value in parameter_values.items():
        proof_value = _as_mapping(proof_values.get(name))
        if proof_value.get("value") != value.get("value"):
            return False
        if str(proof_value.get("unit") or "") != str(value.get("unit") or ""):
            return False
    checks = _as_mapping(proof.get("checks"))
    required_checks = (
        "source_condition_application_ref_matches",
        "application_status_applied",
        "applied_file_hash_matches_backend_context",
        "applied_fields_present",
        "applied_fields_are_wind_only",
        "wind_speed_matches_parameter",
        "wind_direction_matches_parameter",
        "wind_world_linear_velocity_matches_requested",
        "wind_effects_plugin_materialized",
        "wind_enabled_on_vehicle_links",
        "hardware_target_not_allowed",
        "physical_execution_not_invoked",
        "delivery_completion_not_claimed",
    )
    return all(checks.get(key) is True for key in required_checks)


def _parameter_normalized_backend_context(
    artifact: Mapping[str, Any],
    backend_context: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Split wind Form 3 SDF variation into static and parameterized context.

    This is intentionally narrow. Raw SDF hash differences remain authoritative
    unless the source is a wind Form 3 artifact with the complete declared wind
    parameter set needed to explain the SDF delta.
    """

    if artifact.get("condition_kind") != _WIND_FORM3_CONDITION_KIND:
        return None
    raw_sdf_hash = str(backend_context.get("sdf_hash") or "").strip()
    if not raw_sdf_hash:
        return None
    parameter_values = _parameter_value_map(artifact.get("parameter_observations"))
    if any(name not in parameter_values for name in _WIND_PARAMETER_NAMES):
        return None
    if not _parameterized_sdf_delta_proof_supported(
        artifact,
        backend_context=backend_context,
        parameter_values=parameter_values,
    ):
        return None

    static_payload = {
        "backend_type": backend_context.get("backend_type"),
        "image_version": backend_context.get("image_version"),
        "sim_version": backend_context.get("sim_version"),
        "applicator_chain_refs": backend_context.get("applicator_chain_refs"),
        "verifier_version": backend_context.get("verifier_version"),
        "audit_script_version": backend_context.get("audit_script_version"),
        "parameterized_condition_family": _WIND_FORM3_CONDITION_KIND,
    }
    static_sdf_hash = f"parameterized_sdf_family:{_stable_hash(static_payload)[:16]}"
    backend_static_context = dict(backend_context)
    backend_static_context["sdf_hash"] = static_sdf_hash
    parameter_context = {
        "condition_kind": _WIND_FORM3_CONDITION_KIND,
        "raw_sdf_hash": raw_sdf_hash,
        "parameter_values": parameter_values,
        "parameterized_sdf_patch_hash": "parameterized_sdf_patch:"
        + _stable_hash(
            {
                "condition_kind": _WIND_FORM3_CONDITION_KIND,
                "applied_message_sha256": _as_mapping(
                    artifact.get("parameterized_sdf_delta_proof")
                ).get("applied_message_sha256"),
                "parameter_values": parameter_values,
                "raw_sdf_hash": raw_sdf_hash,
            }
        )[:16],
    }
    return {
        "backend_context": backend_static_context,
        "raw_backend_context": dict(backend_context),
        "backend_static_context": backend_static_context,
        "parameterized_condition_context": parameter_context,
        "backend_context_normalization": {
            "normalization_status": "parameterized_condition_context_declared",
            "normalization_mode": "wind_form3_parameter_normalized_backend_context",
            "raw_sdf_hash_authoritative": True,
            "raw_sdf_hash_not_ignored": True,
            "cohort_static_context_uses_parameterized_sdf_family_hash": True,
            "explained_by_parameters": list(_WIND_PARAMETER_NAMES),
        },
    }


def normalize_source_artifact(
    artifact: Mapping[str, Any],
    *,
    path: Path | None = None,
    context_overrides: Mapping[str, Any] | None = None,
    parameter_normalized_backend_context: bool = False,
) -> dict[str, Any] | None:
    """Normalize one historical artifact into a builder source-run record."""

    causal_form = artifact.get("causal_form")
    if causal_form not in _CANDIDATE_CAUSAL_FORMS:
        return None
    source_ref_base = str(
        artifact.get("audit_id")
        or artifact.get("artifact_ref")
        or artifact.get("run_ref")
        or ""
    ).strip()
    path_ref = path.as_posix() if path else ""
    source_ref = (
        f"{source_ref_base}|path={path_ref}"
        if source_ref_base and path_ref
        else source_ref_base or path_ref
    )
    backend_context = artifact.get("backend_context") or _default_backend_context(
        _as_mapping((context_overrides or {}).get("backend_context"))
    )
    normalized = {
        "schema_version": artifact.get("schema_version"),
        "artifact_ref": source_ref,
        "audit_id": source_ref,
        "causal_form": causal_form,
        "condition_kind": artifact.get("condition_kind"),
        "form1_claim_supported": artifact.get("form1_claim_supported"),
        "form3_claim_supported": artifact.get("form3_claim_supported"),
        "progress_counted": artifact.get("progress_counted"),
        "source_bound": _source_bound_from_checks(artifact),
        "parameter_observations": artifact.get("parameter_observations") or [],
        "mission_contract_ref": artifact.get("mission_contract_ref")
        or (context_overrides or {}).get("mission_contract_ref"),
        "task_graph_ref": artifact.get("task_graph_ref")
        or (context_overrides or {}).get("task_graph_ref"),
        "source_backend_type": artifact.get("source_backend_type")
        or (context_overrides or {}).get("source_backend_type"),
        "backend_context": backend_context,
        "hardware_target_allowed": artifact.get("hardware_target_allowed", False),
        "physical_execution_invoked": artifact.get("physical_execution_invoked", False),
        "delivery_completion_claimed": artifact.get("delivery_completion_claimed", False),
        "physical_form1_claimed": artifact.get("physical_form1_claimed", False),
        "safety_boundary": {
            "dispatch_authority_created": _as_mapping(
                artifact.get("safety_boundary")
            ).get(
                "dispatch_authority_created",
                artifact.get("dispatch_authority_created", False),
            )
        },
    }
    if "parameterized_sdf_delta_proof" in artifact:
        normalized["parameterized_sdf_delta_proof"] = artifact.get(
            "parameterized_sdf_delta_proof"
        )
    if parameter_normalized_backend_context:
        normalized_context = _parameter_normalized_backend_context(
            artifact,
            _as_mapping(backend_context),
        )
        if normalized_context:
            normalized.update(normalized_context)
    return normalized


def build_operational_envelope_from_artifacts(
    *,
    artifact_paths: Sequence[Path],
    now: datetime | None = None,
    min_sim_run_count: int = 10,
    context_overrides: Mapping[str, Any] | None = None,
    parameter_normalized_backend_context: bool = False,
) -> dict[str, Any]:
    source_records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for path in discover_json_artifacts(artifact_paths):
        try:
            payload = _read_json(path)
        except json.JSONDecodeError as exc:
            skipped.append(
                {
                    "path": str(path),
                    "skipped_reason": "json_decode_error",
                    "error": str(exc),
                }
            )
            continue
        artifact = _as_mapping(payload)
        normalized = normalize_source_artifact(
            artifact,
            path=path,
            context_overrides=context_overrides,
            parameter_normalized_backend_context=parameter_normalized_backend_context,
        )
        if normalized is None:
            skipped.append(
                {
                    "path": str(path),
                    "skipped_reason": "not_operational_envelope_source_candidate",
                    "schema_version": artifact.get("schema_version"),
                    "causal_form": artifact.get("causal_form"),
                }
            )
            continue
        source_records.append(normalized)

    envelope = build_operational_envelope(
        source_runs=source_records,
        now=now,
        min_sim_run_count=min_sim_run_count,
    )
    return {
        "schema_version": OPERATIONAL_ENVELOPE_SOURCE_INGESTION_SCHEMA_VERSION,
        "ingestion_status": (
            "envelope_ready"
            if envelope.get("envelope_status") == "active"
            else "envelope_not_ready"
        ),
        "causal_form": "Form 0b",
        "progress_counted": False,
        "artifact_path_count": len(discover_json_artifacts(artifact_paths)),
        "source_candidate_count": len(source_records),
        "skipped_artifact_count": len(skipped),
        "source_records": source_records,
        "skipped_artifacts": skipped,
        "operational_envelope": envelope,
        "ready_blockers": envelope.get("blocked_reasons", []),
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
