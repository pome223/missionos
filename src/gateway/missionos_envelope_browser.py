"""Read-only MissionOS envelope browser projection for the Control UI.

The browser indexes persisted operational-envelope artifacts and physical seed
consumption plans. It must not start SITL, invoke Gateway probes, transfer
causal verification to physical execution, create dispatch authority, or mutate
mission state.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from src.gateway.missionos_milestone import (
    ARTIFACT_ROOT,
    AUTHORITY_FALSE_KEYS,
    _authority_false_summary,
    _positive_evidence_path,
    _relative,
)


SCHEMA_VERSION = "missionos_envelope_browser_gui_summary.v1"
ENVELOPE_CLASSIFICATION = "Form 0b / GUI envelope visualization"
ENVELOPE_REQUIRED_AUTHORITY_KEYS = {
    "physical_execution_invoked",
    "physical_form1_claimed",
    "physical_success_claimed",
    "hardware_target_allowed",
    "dispatch_authority_created",
    "delivery_completion_claimed",
    "llm_gate_judge_used",
    "approval_free_stronger_execution",
    "public_sync_performed",
}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _latest_json(root: Path, filename: str) -> tuple[str, dict[str, Any]]:
    if not root.exists():
        return "", {}
    candidates = sorted(
        root.rglob(filename),
        key=lambda path: (path.stat().st_mtime, path.as_posix()),
        reverse=True,
    )
    for path in candidates:
        if not _positive_evidence_path(path):
            continue
        payload = _read_json(path)
        if payload is not None:
            return _relative(path), payload
    return "", {}


def _plain_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _field_values(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in fields.items()
        if value is not None and value != "" and value != []
    }


def _source_count(payload: Mapping[str, Any]) -> int:
    for key in (
        "source_artifact_count",
        "accepted_source_artifact_count",
        "accepted_sim_run_count",
    ):
        value = payload.get(key)
        if isinstance(value, int):
            return value
    for key in (
        "discovered_source_artifact_paths",
        "enriched_source_artifact_paths",
        "input_artifact_paths",
        "accepted_source_runs",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def _safe_range(envelope: Mapping[str, Any], parameter: str) -> Mapping[str, Any]:
    observed = _plain_mapping(envelope.get("observed_envelope"))
    safe_range = _plain_mapping(observed.get("safe_range"))
    return _plain_mapping(safe_range.get(parameter))


def _bounds_from_cohort(cohort: Mapping[str, Any]) -> dict[str, Any]:
    envelope = _plain_mapping(cohort.get("operational_envelope"))
    wind_range = _safe_range(envelope, "wind_speed_mps")
    accepted_bounds = _plain_mapping(envelope.get("accepted_parameter_bounds"))
    wind_bounds = _plain_mapping(accepted_bounds.get("wind_speed_mps"))
    return _field_values(
        {
            "wind_speed_mps_min": wind_range.get("min")
            if "min" in wind_range
            else wind_bounds.get("min_value"),
            "wind_speed_mps_max": wind_range.get("max")
            if "max" in wind_range
            else wind_bounds.get("max_value"),
            "wind_speed_unit": wind_range.get("unit") or "m/s",
            "wind_speed_sample_count": wind_bounds.get("sample_count")
            or envelope.get("accepted_sim_run_count"),
        }
    )


def _range_kind(min_value: Any, max_value: Any) -> str:
    if min_value is None or max_value is None:
        return "unknown"
    return "point envelope" if min_value == max_value else "range envelope"


def _card(
    *,
    card_id: str,
    title: str,
    status: str,
    envelope_type: str,
    artifact_path: str,
    fields: Mapping[str, Any],
    boundary_note: str,
) -> dict[str, Any]:
    return {
        "card_id": card_id,
        "title": title,
        "status": status,
        "envelope_type": envelope_type,
        "artifact_path": artifact_path,
        "fields": _field_values(fields),
        "boundary_note": boundary_note,
    }


def _range_verification_card(
    path: str,
    verification: Mapping[str, Any],
) -> dict[str, Any]:
    min_value = verification.get("wind_speed_mps_min")
    max_value = verification.get("wind_speed_mps_max")
    status = (
        "active"
        if verification.get("verification_status") == "verified"
        and verification.get("blocked_reasons") == []
        else "blocked"
        if verification
        else "missing"
    )
    return _card(
        card_id="range_envelope_verification",
        title="Wind Range Envelope Verification",
        status=status,
        envelope_type=_range_kind(min_value, max_value),
        artifact_path=path,
        fields={
            "verification_status": verification.get("verification_status"),
            "accepted_wind_mps_values": verification.get("accepted_wind_mps_values"),
            "wind_speed_mps_min": min_value,
            "wind_speed_mps_max": max_value,
            "source_artifact_count": _source_count(verification),
            "causal_form": verification.get("causal_form"),
            "progress_counted": verification.get("progress_counted"),
            "blocked_reasons": verification.get("blocked_reasons"),
        },
        boundary_note=(
            "SITL-derived parameter knowledge only; this does not transfer "
            "causal verification to physical execution."
        ),
    )


def _cohort_card(path: str, cohort: Mapping[str, Any]) -> dict[str, Any]:
    envelope = _plain_mapping(cohort.get("operational_envelope"))
    bounds = _bounds_from_cohort(cohort)
    status = (
        str(envelope.get("envelope_status") or "active")
        if cohort.get("cohort_status") == "operational_envelope_active"
        else "blocked"
        if cohort
        else "missing"
    )
    return _card(
        card_id="operational_envelope",
        title="Operational Envelope",
        status=status,
        envelope_type=_range_kind(
            bounds.get("wind_speed_mps_min"),
            bounds.get("wind_speed_mps_max"),
        ),
        artifact_path=path,
        fields={
            "cohort_status": cohort.get("cohort_status"),
            "envelope_status": envelope.get("envelope_status"),
            "accepted_sim_run_count": envelope.get("accepted_sim_run_count"),
            "min_sim_run_count": envelope.get("min_sim_run_count"),
            "range_envelope_observed": cohort.get("range_envelope_observed"),
            "parameterized_contexts_complete": envelope.get(
                "parameterized_condition_contexts_complete"
            ),
            "all_runs_same_backend_context": envelope.get(
                "all_runs_same_backend_context"
            ),
            "causal_verification_transferred": envelope.get(
                "causal_verification_transferred"
            ),
            "physical_form1_required": envelope.get("physical_form1_required"),
            **bounds,
            "expiration_triggers": sorted(
                key
                for key, value in _plain_mapping(
                    envelope.get("expiration_triggers")
                ).items()
                if value is True
            ),
            "blocked_reasons": envelope.get("blocked_reasons")
            or cohort.get("blocked_reasons"),
        },
        boundary_note=(
            "Envelope is reusable parameter knowledge, not physical success, "
            "dispatch authority, or delivery completion."
        ),
    )


def _physical_seed_card(path: str, plan: Mapping[str, Any]) -> dict[str, Any]:
    parameter_match = _plain_mapping(plan.get("parameter_match"))
    return _card(
        card_id="physical_seed_consumption",
        title="Physical Plan Seed Consumption",
        status=str(plan.get("plan_status") or "missing")
        if plan
        else "missing",
        envelope_type="physical seed boundary",
        artifact_path=path,
        fields={
            "plan_status": plan.get("plan_status"),
            "range_envelope_consumed": plan.get("range_envelope_consumed"),
            "parameter_knowledge_consumed": plan.get("parameter_knowledge_consumed"),
            "all_parameters_within_envelope": plan.get(
                "all_parameters_within_envelope"
            ),
            "blocked_parameters": plan.get("blocked_parameters")
            or plan.get("planned_parameters_outside_envelope"),
            "causal_verification_transferred": plan.get(
                "causal_verification_transferred"
            ),
            "physical_form1_required": plan.get("physical_form1_required"),
            "transfer_scope": plan.get("transfer_scope"),
            "physical_execution_invoked": plan.get("physical_execution_invoked"),
            "hardware_target_allowed": plan.get("hardware_target_allowed"),
            "dispatch_authority_created": plan.get("dispatch_authority_created"),
            "delivery_completion_claimed": plan.get("delivery_completion_claimed"),
            "parameter_match": parameter_match,
            "blocked_reasons": plan.get("blocked_reasons"),
        },
        boundary_note=(
            "Ready means a physical test plan seed can be prepared. It is not "
            "a physical Form 1 or physical success claim."
        ),
    )


def _boundary_warnings(
    *,
    authority: Mapping[str, Any],
    cohort: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if authority.get("authority_boundary_supported") is not True:
        warnings.append("authority boundary contains true forbidden flags")
    if authority.get("authority_boundary_explicit") is not True:
        warnings.append("authority boundary has missing forbidden-flag evidence")

    envelope = _plain_mapping(cohort.get("operational_envelope"))
    transfer_values = [
        cohort.get("causal_verification_transferred"),
        envelope.get("causal_verification_transferred"),
        plan.get("causal_verification_transferred"),
    ]
    if any(value is True for value in transfer_values):
        warnings.append("causal verification transfer attempted")
    if not any(value is False for value in transfer_values):
        warnings.append("causal verification transfer boundary missing")

    form1_values = [
        cohort.get("physical_form1_required"),
        envelope.get("physical_form1_required"),
        plan.get("physical_form1_required"),
    ]
    if any(value is False for value in form1_values):
        warnings.append("physical Form 1 requirement missing")
    if not any(value is True for value in form1_values):
        warnings.append("physical Form 1 requirement not evidenced")

    return warnings


def _physical_form1_required_summary(*values: Any) -> bool:
    if any(value is False for value in values):
        return False
    return any(value is True for value in values)


def build_missionos_envelope_browser_summary(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Return a read-only MissionOS envelope browser summary."""

    root = Path(artifact_root)
    verification_path, verification = _latest_json(
        root,
        "parameter_normalized_wind_range_envelope_verification.json",
    )
    cohort_path, cohort = _latest_json(
        root,
        "wind_form3_operational_envelope_cohort.json",
    )
    plan_path, plan = _latest_json(
        root,
        "wind_form3_physical_envelope_consumption_plan.json",
    )
    selected_payloads = [payload for payload in [verification, cohort, plan] if payload]
    authority = _authority_false_summary(selected_payloads)
    authority_missing_keys = sorted(
        key
        for key in ENVELOPE_REQUIRED_AUTHORITY_KEYS
        if not any(_explicit_key_value(payload, key) is False for payload in selected_payloads)
    )
    authority["authority_boundary_explicit"] = not authority_missing_keys
    authority["authority_missing_false_keys"] = authority_missing_keys
    warnings = _boundary_warnings(authority=authority, cohort=cohort, plan=plan)
    envelope = _plain_mapping(cohort.get("operational_envelope"))
    cards = [
        _range_verification_card(verification_path, verification),
        _cohort_card(cohort_path, cohort),
        _physical_seed_card(plan_path, plan),
    ]
    observed = (
        verification.get("verification_status") == "verified"
        and cohort.get("cohort_status") == "operational_envelope_active"
        and plan.get("plan_status") == "physical_test_plan_seed_ready"
        and not warnings
    )
    browser_status = "observed" if observed else "partial" if selected_payloads else "missing"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_root": _relative(root),
        "browser_label": "MissionOS envelope browser",
        "browser_status": browser_status,
        "classification": {
            "causal_form": "Form 0b",
            "surface": "GUI envelope visualization",
            "progress_counted": False,
            "runtime_capability_added": False,
        },
        "authority_boundary": authority,
        "boundary_warnings": warnings,
        "summary": {
            "active_envelope_count": sum(
                1 for card in cards if card.get("status") == "active"
            ),
            "blocked_envelope_count": sum(
                1 for card in cards if card.get("status") == "blocked"
            ),
            "physical_seed_ready": plan.get("plan_status")
            == "physical_test_plan_seed_ready",
            "causal_verification_transferred": plan.get(
                "causal_verification_transferred"
            )
            is True
            or _plain_mapping(cohort.get("operational_envelope")).get(
                "causal_verification_transferred"
            )
            is True,
            "physical_form1_required": _physical_form1_required_summary(
                cohort.get("physical_form1_required"),
                envelope.get("physical_form1_required"),
                plan.get("physical_form1_required"),
            ),
            "parameter": "wind_speed_mps",
            "wind_speed_mps_min": verification.get("wind_speed_mps_min")
            or _bounds_from_cohort(cohort).get("wind_speed_mps_min"),
            "wind_speed_mps_max": verification.get("wind_speed_mps_max")
            or _bounds_from_cohort(cohort).get("wind_speed_mps_max"),
            "accepted_wind_mps_values": verification.get(
                "accepted_wind_mps_values"
            ),
        },
        "cards": cards,
        "not_claimed": [
            "physical_execution",
            "physical_form1",
            "physical_success",
            "hardware_target_authority",
            "dispatch_authority_creation",
            "delivery_completion",
            "public_sync",
        ],
        "operator_note": (
            "This view summarizes persisted envelope artifacts for readability. "
            "It does not run SITL, consume live telemetry, transfer causal "
            "verification, authorize dispatch, or claim delivery completion."
        ),
        "authority_false_keys": sorted(AUTHORITY_FALSE_KEYS),
        "envelope_classification": ENVELOPE_CLASSIFICATION,
    }


def _explicit_key_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        if key in value:
            return value.get(key)
        for nested in value.values():
            found = _explicit_key_value(nested, key)
            if found is not None:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _explicit_key_value(nested, key)
            if found is not None:
                return found
    return None
