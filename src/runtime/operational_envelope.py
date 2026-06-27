"""Operational envelope artifacts for sim-derived parameter knowledge.

An operational envelope transfers parameter knowledge only. It never transfers
causal verification from SITL to a physical backend and never grants execution
authority.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any


OPERATIONAL_ENVELOPE_SCHEMA_VERSION = "operational_envelope.v1"
PHYSICAL_RUN_ENVELOPE_CONSUMPTION_SCHEMA_VERSION = (
    "physical_run_operational_envelope_consumption.v1"
)
OPERATIONAL_ENVELOPE_REF_PREFIX = "operational_envelope"
DEFAULT_MIN_SIM_RUN_COUNT = 10
ACCEPTED_CAUSAL_FORMS = ("Form 1a", "Form 3")
REQUIRED_BACKEND_CONTEXT_KEYS = (
    "backend_type",
    "image_version",
    "sim_version",
    "sdf_hash",
    "applicator_chain_refs",
    "verifier_version",
    "audit_script_version",
)

_AUTHORITY_KEYS = (
    "hardware_target_allowed",
    "physical_execution_invoked",
    "delivery_completion_claimed",
    "causal_verification_transferred",
    "physical_form1_claimed",
    "dispatch_authority_created",
)


def _utc(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{prefix}_{digest[:12]}"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _run_ref(run: Mapping[str, Any], index: int) -> str:
    for key in ("artifact_ref", "audit_id", "run_ref", "source_ref"):
        raw = str(run.get(key) or "").strip()
        if raw:
            return raw
    schema = str(run.get("schema_version") or "sim_run")
    return f"{schema}:source_run_{index}"


def _authority_true_keys(value: Any) -> list[str]:
    observed: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _AUTHORITY_KEYS and item is True:
                observed.append(str(key))
            observed.extend(_authority_true_keys(item))
    elif isinstance(value, list):
        for item in value:
            observed.extend(_authority_true_keys(item))
    return observed


def _run_source_bound(run: Mapping[str, Any]) -> bool:
    if run.get("source_bound") is True:
        return True
    checks = _as_mapping(run.get("checks"))
    source_keys = [
        key
        for key in checks
        if "source_bound" in str(key) or str(key).endswith("_observed")
    ]
    return bool(source_keys) and all(checks.get(key) is True for key in source_keys)


def _run_supported(run: Mapping[str, Any]) -> bool:
    return (
        run.get("causal_form") in ACCEPTED_CAUSAL_FORMS
        and run.get("progress_counted") is True
        and (
            run.get("form3_claim_supported") is True
            or (
                run.get("causal_form") == "Form 1a"
                and run.get("form1_claim_supported") is True
            )
        )
        and _run_source_bound(run)
        and not _authority_true_keys(run)
    )


def _run_context(run: Mapping[str, Any]) -> dict[str, Any]:
    context = dict(_as_mapping(run.get("backend_context")))
    for key in REQUIRED_BACKEND_CONTEXT_KEYS:
        if key in run and key not in context:
            context[key] = run[key]
    return context


def _backend_context_complete(context: Mapping[str, Any]) -> bool:
    for key in REQUIRED_BACKEND_CONTEXT_KEYS:
        value = context.get(key)
        if value in (None, ""):
            return False
        if key == "applicator_chain_refs" and not isinstance(value, list):
            return False
    return True


def _all_same(values: list[str]) -> bool:
    return bool(values) and len(set(values)) == 1


def _parameter_observations(run: Mapping[str, Any], run_ref: str) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    raw = run.get("parameter_observations")
    if isinstance(raw, list):
        for item in raw:
            mapping = _as_mapping(item)
            parameter = str(mapping.get("parameter") or "").strip()
            value = _as_float(mapping.get("value"))
            if parameter and value is not None:
                observations.append(
                    {
                        "parameter": parameter,
                        "value": value,
                        "unit": str(mapping.get("unit") or ""),
                        "source_ref": run_ref,
                    }
                )

    requested = _as_mapping(run.get("requested"))
    observed = _as_mapping(run.get("observed"))
    for parameter, unit, keys in (
        (
            "wind_speed_mps",
            "m/s",
            ("wind_speed_mps", "requested_wind_mps", "expected_wind_mps"),
        ),
        ("payload_kg", "kg", ("payload_kg", "payload_weight_kg")),
        ("altitude_m", "m", ("altitude_m", "target_altitude_m")),
    ):
        for source in (requested, observed, run):
            for key in keys:
                value = _as_float(source.get(key))
                if value is not None:
                    observations.append(
                        {
                            "parameter": parameter,
                            "value": value,
                            "unit": unit,
                            "source_ref": run_ref,
                        }
                    )
                    break
            else:
                continue
            break
    return observations


def _build_bounds(observations: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for observation in observations:
        grouped.setdefault(str(observation["parameter"]), []).append(observation)

    bounds: dict[str, dict[str, Any]] = {}
    for parameter, items in sorted(grouped.items()):
        values = [float(item["value"]) for item in items]
        source_refs = sorted({str(item["source_ref"]) for item in items})
        unit = str(items[0].get("unit") or "")
        bounds[parameter] = {
            "min_value": min(values),
            "max_value": max(values),
            "unit": unit,
            "source_run_count": len(source_refs),
            "sample_count": len(values),
            "source_refs": source_refs,
        }
    return bounds


def _confidence_indicators(bounds: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    indicators: dict[str, dict[str, Any]] = {}
    for parameter, bound in sorted(bounds.items()):
        min_value = _as_float(bound.get("min_value"))
        max_value = _as_float(bound.get("max_value"))
        sample_count = int(bound.get("sample_count") or 0)
        if min_value is None or max_value is None:
            continue
        mean = (min_value + max_value) / 2.0
        spread = max_value - min_value
        indicators[parameter] = {
            "sample_count": sample_count,
            "margin_min": min_value,
            "margin_max": max_value,
            "margin_mean": round(mean, 6),
            "dispersion": round(spread, 6),
            "stddev": None,
            "stddev_basis": "not_computed_from_scaffold_min_max_only",
        }
    return indicators


def _observed_envelope(bounds: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "safe_range": {
            parameter: {
                "min": bound.get("min_value"),
                "max": bound.get("max_value"),
                "unit": bound.get("unit"),
            }
            for parameter, bound in sorted(bounds.items())
        },
        "outliers": [],
        "failure_modes_observed": [],
    }


def _parameter_context_key(
    context: Mapping[str, Any],
) -> tuple[tuple[tuple[str, str, str], ...], str, str] | None:
    raw_sdf_hash = str(context.get("raw_sdf_hash") or "").strip()
    patch_hash = str(context.get("parameterized_sdf_patch_hash") or "").strip()
    values = _as_mapping(context.get("parameter_values"))
    if not raw_sdf_hash or not patch_hash or not values:
        return None
    parameter_tuple: list[tuple[str, str, str]] = []
    for parameter, item in sorted(values.items()):
        item_mapping = _as_mapping(item)
        if item_mapping.get("value") is None:
            return None
        parameter_tuple.append(
            (
                str(parameter),
                json.dumps(
                    item_mapping.get("value"),
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ),
                str(item_mapping.get("unit") or ""),
            )
        )
    return tuple(parameter_tuple), raw_sdf_hash, patch_hash


def _parameterized_sdf_patch_mapping_status(
    contexts: Iterable[Mapping[str, Any]],
    *,
    expected_count: int,
) -> tuple[bool, bool, list[dict[str, Any]]]:
    raw_hashes_by_tuple: dict[tuple[tuple[str, str, str], ...], set[str]] = {}
    patch_hashes_by_tuple: dict[tuple[tuple[str, str, str], ...], set[str]] = {}
    complete = True
    context_count = 0
    for context_like in contexts:
        context = _as_mapping(context_like)
        context_count += 1
        key = _parameter_context_key(context)
        if key is None:
            complete = False
            continue
        parameter_tuple, raw_sdf_hash, patch_hash = key
        raw_hashes_by_tuple.setdefault(parameter_tuple, set()).add(raw_sdf_hash)
        patch_hashes_by_tuple.setdefault(parameter_tuple, set()).add(patch_hash)

    if context_count != expected_count:
        complete = False

    conflicts: list[dict[str, Any]] = []
    for parameter_tuple in sorted(raw_hashes_by_tuple):
        raw_hashes = sorted(raw_hashes_by_tuple[parameter_tuple])
        patch_hashes = sorted(patch_hashes_by_tuple.get(parameter_tuple, set()))
        if len(raw_hashes) > 1 or len(patch_hashes) > 1:
            conflicts.append(
                {
                    "parameter_tuple": [
                        {
                            "parameter": parameter,
                            "value": json.loads(value),
                            "unit": unit,
                        }
                        for parameter, value, unit in parameter_tuple
                    ],
                    "raw_sdf_hashes": raw_hashes,
                    "parameterized_sdf_patch_hashes": patch_hashes,
                }
            )
    return complete, not conflicts, conflicts


def build_operational_envelope(
    *,
    source_runs: Iterable[Mapping[str, Any]],
    now: datetime | None = None,
    min_sim_run_count: int = DEFAULT_MIN_SIM_RUN_COUNT,
) -> dict[str, Any]:
    """Build a Form 0b operational envelope from source-bound SITL run evidence."""

    observed_at = _utc(now).isoformat()
    accepted_runs: list[dict[str, Any]] = []
    rejected_runs: list[dict[str, Any]] = []
    accepted_observations: list[dict[str, Any]] = []
    source_run_list = list(source_runs)
    accepted_contract_refs: list[str] = []
    accepted_task_graph_refs: list[str] = []
    accepted_backend_types: list[str] = []
    accepted_backend_contexts: list[dict[str, Any]] = []
    accepted_raw_backend_contexts: list[dict[str, Any]] = []
    accepted_parameterized_contexts: list[dict[str, Any]] = []
    backend_context_normalization_modes: list[str] = []

    for index, run_like in enumerate(source_run_list):
        run = _as_mapping(run_like)
        run_ref = _run_ref(run, index)
        authority_flags = sorted(set(_authority_true_keys(run)))
        supported = _run_supported(run)
        mission_contract_ref = str(run.get("mission_contract_ref") or "").strip()
        task_graph_ref = str(run.get("task_graph_ref") or "").strip()
        source_backend_type = str(run.get("source_backend_type") or "").strip()
        backend_context = _run_context(run)
        raw_backend_context = dict(_as_mapping(run.get("raw_backend_context")) or backend_context)
        parameterized_context = dict(_as_mapping(run.get("parameterized_condition_context")))
        normalization = dict(_as_mapping(run.get("backend_context_normalization")))
        context_complete = _backend_context_complete(backend_context)
        observations = _parameter_observations(run, run_ref) if supported else []
        required_context_present = bool(
            mission_contract_ref and task_graph_ref and source_backend_type and context_complete
        )
        if supported and observations and required_context_present:
            accepted_runs.append(
                {
                    "source_ref": run_ref,
                    "causal_form": run.get("causal_form"),
                    "condition_kind": run.get("condition_kind"),
                    "mission_contract_ref": mission_contract_ref,
                    "task_graph_ref": task_graph_ref,
                    "source_backend_type": source_backend_type,
                    "parameter_observation_count": len(observations),
                    "backend_context": backend_context,
                    "raw_backend_context": raw_backend_context,
                    "parameterized_condition_context": parameterized_context,
                    "backend_context_normalization": normalization,
                }
            )
            accepted_observations.extend(observations)
            accepted_contract_refs.append(mission_contract_ref)
            accepted_task_graph_refs.append(task_graph_ref)
            accepted_backend_types.append(source_backend_type)
            accepted_backend_contexts.append(backend_context)
            accepted_raw_backend_contexts.append(raw_backend_context)
            if parameterized_context:
                accepted_parameterized_contexts.append(parameterized_context)
            mode = str(normalization.get("normalization_status") or "").strip()
            if mode:
                backend_context_normalization_modes.append(mode)
            continue
        rejected_reasons = [
            *([] if supported else ["source_run_not_supported_for_envelope"]),
            *([] if observations else ["parameter_observation_missing"]),
            *([] if mission_contract_ref else ["mission_contract_ref_missing"]),
            *([] if task_graph_ref else ["task_graph_ref_missing"]),
            *([] if source_backend_type else ["source_backend_type_missing"]),
            *([] if context_complete else ["backend_context_incomplete"]),
            *(
                ["source_run_forbidden_authority_flags_observed"]
                if authority_flags
                else []
            ),
        ]
        rejected_runs.append(
            {
                "source_ref": run_ref,
                "causal_form": run.get("causal_form"),
                "condition_kind": run.get("condition_kind"),
                "rejected_reasons": rejected_reasons,
                "authority_flags_observed": authority_flags,
            }
        )

    bounds = _build_bounds(accepted_observations)
    accepted_run_count = len(accepted_runs)
    all_runs_same_mission_contract = _all_same(accepted_contract_refs)
    all_runs_same_task_graph = _all_same(accepted_task_graph_refs)
    all_runs_same_backend_type = _all_same(accepted_backend_types)
    backend_context = accepted_backend_contexts[0] if accepted_backend_contexts else {}
    raw_backend_context = (
        accepted_raw_backend_contexts[0] if accepted_raw_backend_contexts else {}
    )
    all_runs_same_backend_context = bool(accepted_backend_contexts) and all(
        context == backend_context for context in accepted_backend_contexts
    )
    all_runs_same_raw_backend_context = bool(accepted_raw_backend_contexts) and all(
        context == raw_backend_context for context in accepted_raw_backend_contexts
    )
    backend_context_comparison_mode = (
        "parameter_normalized"
        if "parameterized_condition_context_declared"
        in backend_context_normalization_modes
        else "raw_backend_context"
    )
    (
        parameterized_condition_contexts_complete,
        parameterized_sdf_patch_mapping_valid,
        parameterized_sdf_patch_conflicts,
    ) = (
        _parameterized_sdf_patch_mapping_status(
            accepted_parameterized_contexts,
            expected_count=accepted_run_count,
        )
        if backend_context_comparison_mode == "parameter_normalized"
        else (True, True, [])
    )
    ready = (
        accepted_run_count >= min_sim_run_count
        and bool(bounds)
        and all_runs_same_mission_contract
        and all_runs_same_task_graph
        and all_runs_same_backend_type
        and all_runs_same_backend_context
        and parameterized_condition_contexts_complete
        and parameterized_sdf_patch_mapping_valid
    )
    blocked_reasons: list[str] = []
    if accepted_run_count < min_sim_run_count:
        blocked_reasons.append("insufficient_sim_run_count")
    if not bounds:
        blocked_reasons.append("accepted_parameter_bounds_missing")
    if accepted_runs and not all_runs_same_mission_contract:
        blocked_reasons.append("mission_contract_ref_mismatch")
    if accepted_runs and not all_runs_same_task_graph:
        blocked_reasons.append("task_graph_ref_mismatch")
    if accepted_runs and not all_runs_same_backend_type:
        blocked_reasons.append("source_backend_type_mismatch")
    if accepted_runs and not all_runs_same_backend_context:
        blocked_reasons.append("backend_context_mismatch")
    if (
        backend_context_comparison_mode == "parameter_normalized"
        and not parameterized_condition_contexts_complete
    ):
        blocked_reasons.append("parameterized_condition_context_incomplete")
    if (
        backend_context_comparison_mode == "parameter_normalized"
        and not parameterized_sdf_patch_mapping_valid
    ):
        blocked_reasons.append("parameterized_sdf_patch_mismatch")

    envelope_id = _stable_id(
        OPERATIONAL_ENVELOPE_REF_PREFIX,
        {
            "accepted_refs": [run["source_ref"] for run in accepted_runs],
            "bounds": bounds,
            "min_sim_run_count": min_sim_run_count,
            "observed_at": observed_at,
        },
    )
    return {
        "schema_version": OPERATIONAL_ENVELOPE_SCHEMA_VERSION,
        "envelope_id": envelope_id,
        "envelope_ref": f"{OPERATIONAL_ENVELOPE_REF_PREFIX}:{envelope_id}",
        "causal_form": "Form 0b",
        "audit_status": (
            "parameter_knowledge_ready" if ready else "insufficient_sim_evidence"
        ),
        "envelope_status": "active" if ready else "inactive_insufficient_sim_evidence",
        "progress_counted": False,
        "transfer_scope": "parameter_knowledge_only",
        "causal_verification_transferred": False,
        "physical_form1_required": True,
        "all_runs_required_form": "Form 1a_or_true_Form_3",
        "mission_contract_ref": accepted_contract_refs[0] if all_runs_same_mission_contract else "",
        "task_graph_ref": accepted_task_graph_refs[0] if all_runs_same_task_graph else "",
        "source_backend_type": accepted_backend_types[0] if all_runs_same_backend_type else "",
        "all_runs_same_mission_contract": all_runs_same_mission_contract,
        "all_runs_same_task_graph": all_runs_same_task_graph,
        "all_runs_same_backend_type": all_runs_same_backend_type,
        "backend_context": backend_context if all_runs_same_backend_context else {},
        "backend_context_comparison_mode": backend_context_comparison_mode,
        "all_runs_same_raw_backend_context": all_runs_same_raw_backend_context,
        "raw_backend_context": (
            raw_backend_context if all_runs_same_raw_backend_context else {}
        ),
        "parameterized_condition_contexts": accepted_parameterized_contexts,
        "parameterized_condition_contexts_complete": (
            parameterized_condition_contexts_complete
        ),
        "parameterized_sdf_patch_mapping_valid": parameterized_sdf_patch_mapping_valid,
        "parameterized_sdf_patch_conflicts": parameterized_sdf_patch_conflicts,
        "all_runs_same_backend_context": all_runs_same_backend_context,
        "expiration_triggers": {
            "on_image_version_change": True,
            "on_sim_version_change": True,
            "on_sdf_hash_change": True,
            "on_applicator_chain_change": True,
            "on_verifier_version_change": True,
            "on_audit_script_version_change": True,
        },
        "physical_backend_execution_allowed": False,
        "physical_execution_invoked": False,
        "physical_form1_claimed": False,
        "hardware_target_allowed": False,
        "delivery_completion_claimed": False,
        "sim_run_count": len(source_run_list),
        "accepted_sim_run_count": accepted_run_count,
        "rejected_sim_run_count": len(rejected_runs),
        "min_sim_run_count": min_sim_run_count,
        "accepted_parameter_bounds": bounds,
        "observed_envelope": _observed_envelope(bounds),
        "confidence_indicators": _confidence_indicators(bounds),
        "accepted_source_runs": accepted_runs,
        "rejected_source_runs": rejected_runs,
        "rejected_outliers": rejected_runs,
        "blocked_reasons": blocked_reasons,
        "safety_boundary": {
            "parameter_knowledge_transfer_only": True,
            "causal_verification_transferred": False,
            "physical_form1_required": True,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "dispatch_authority_created": False,
            "llm_gate_judge_used": False,
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
        },
        "observed_at": observed_at,
    }


def operational_envelope_ready(envelope: Mapping[str, Any]) -> bool:
    return (
        envelope.get("schema_version") == OPERATIONAL_ENVELOPE_SCHEMA_VERSION
        and envelope.get("audit_status") == "parameter_knowledge_ready"
        and envelope.get("envelope_status") == "active"
        and envelope.get("transfer_scope") == "parameter_knowledge_only"
        and envelope.get("causal_verification_transferred") is False
        and envelope.get("physical_form1_required") is True
        and envelope.get("all_runs_same_mission_contract") is True
        and envelope.get("all_runs_same_task_graph") is True
        and envelope.get("all_runs_same_backend_context") is True
    )


def operational_envelope_status_for_context(
    envelope: Mapping[str, Any],
    *,
    backend_context: Mapping[str, Any],
) -> str:
    """Return the envelope status for a candidate physical-run backend context."""

    if not operational_envelope_ready(envelope):
        return str(envelope.get("envelope_status") or "inactive_insufficient_sim_evidence")
    envelope_context = _as_mapping(envelope.get("backend_context"))
    candidate_context = _as_mapping(backend_context)
    status_by_key = {
        "image_version": "expired_due_to_image_version_change",
        "sim_version": "expired_due_to_sim_version_change",
        "sdf_hash": "expired_due_to_sdf_hash_change",
        "applicator_chain_refs": "expired_due_to_applicator_chain_change",
        "verifier_version": "expired_due_to_verifier_version_change",
        "audit_script_version": "expired_due_to_audit_script_version_change",
    }
    for key in REQUIRED_BACKEND_CONTEXT_KEYS:
        if envelope_context.get(key) != candidate_context.get(key):
            return status_by_key.get(key, "expired_due_to_backend_context_change")
    return "active"


def build_physical_run_operational_envelope_consumption(
    *,
    envelope: Mapping[str, Any],
    physical_run_ref: str,
    backend_context: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record a physical run consuming envelope parameter knowledge only.

    This is a Section 9.2 scaffold: it never transfers SITL causal verification
    and never grants physical execution or dispatch authority.
    """

    observed_at = _utc(now).isoformat()
    envelope_status_at_run = operational_envelope_status_for_context(
        envelope,
        backend_context=backend_context,
    )
    envelope_ready = operational_envelope_ready(envelope)
    parameter_knowledge_consumed = envelope_ready and envelope_status_at_run == "active"
    blocked_reasons: list[str] = []
    if not envelope_ready:
        blocked_reasons.append("operational_envelope_not_ready")
    if envelope_status_at_run != "active":
        blocked_reasons.append(envelope_status_at_run)

    envelope_ref = str(envelope.get("envelope_ref") or "")
    consumption_id = _stable_id(
        "physical_run_operational_envelope_consumption",
        {
            "envelope_ref": envelope_ref,
            "physical_run_ref": physical_run_ref,
            "backend_context": dict(_as_mapping(backend_context)),
            "observed_at": observed_at,
        },
    )
    return {
        "schema_version": PHYSICAL_RUN_ENVELOPE_CONSUMPTION_SCHEMA_VERSION,
        "consumption_id": consumption_id,
        "consumption_ref": f"physical_run_operational_envelope_consumption:{consumption_id}",
        "causal_form": "Form 0b",
        "progress_counted": False,
        "physical_run_ref": physical_run_ref,
        "operational_envelope_ref": envelope_ref,
        "envelope_status_at_run": envelope_status_at_run,
        "consumption_status": (
            "parameter_knowledge_consumed"
            if parameter_knowledge_consumed
            else "blocked"
        ),
        "blocked_reasons": blocked_reasons,
        "transfer_scope": "parameter_knowledge_only",
        "parameter_knowledge_consumed": parameter_knowledge_consumed,
        "parameter_bounds_ref": envelope_ref if parameter_knowledge_consumed else "",
        "accepted_parameter_bounds": (
            envelope.get("accepted_parameter_bounds", {})
            if parameter_knowledge_consumed
            else {}
        ),
        "causal_verification_transferred": False,
        "physical_form1_required": True,
        "physical_form1_claimed": False,
        "physical_backend_execution_allowed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "delivery_completion_claimed": False,
        "dispatch_authority_created": False,
        "backend_context_at_run": dict(_as_mapping(backend_context)),
        "envelope_backend_context": dict(_as_mapping(envelope.get("backend_context"))),
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
        "observed_at": observed_at,
    }
