"""Read-only MissionOS milestone summary for the Control UI.

This module indexes already persisted artifacts. It must not start SITL,
materialize Gateway runtime evidence, create dispatch authority, or mutate
mission state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping


ARTIFACT_ROOT = Path("output/mission_designer_behavior_delta_audits")
SCHEMA_VERSION = "missionos_current_milestone_gui_summary.v1"

AUTHORITY_FALSE_KEYS = {
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
NEGATIVE_EVIDENCE_PATH_MARKERS = (
    "/forged",
    "forged_",
    "/tamper",
    "tamper_",
    "/negative",
    "negative_",
)
C5B_REQUIRED_TRUE_KEYS = (
    "form3_claim_supported",
    "full_gateway_runtime_loop",
    "gateway_live_observation_stream",
    "gateway_live_recovery_decision_loop",
    "gateway_started_mission_session_live",
    "gateway_supervisor_process_spawned",
    "gateway_observation_process_started",
    "gateway_recovery_decision_process_started",
    "gateway_loop_same_session_evidence",
    "cycle1_gateway_ref_chain_consistent",
    "cycle2_gateway_ref_chain_consistent",
    "cycle_dispatch_chains_distinct",
    "live_gateway_runtime_probe_invoked",
)
C5B_REQUIRED_CHECKS = (
    "backend_action_request_receipt_outcome_same_session_live",
    "c5a_same_session_scaffold_chain_ready",
    "gateway_full_runtime_readiness_ready",
    "gateway_live_gateway_process_materializer_implemented",
    "gateway_live_observation_stream_kind_allowlisted",
    "gateway_live_observation_stream_provenance_source_bound",
    "gateway_live_observation_stream_source_observed",
    "gateway_live_process_sidecar_authority_boundary_false",
    "gateway_live_recovery_decision_loop_kind_allowlisted",
    "gateway_live_recovery_decision_loop_provenance_source_bound",
    "gateway_live_recovery_decision_loop_source_observed",
    "gateway_mission_session_ready",
    "gateway_observation_stream_ready",
    "gateway_observes_mission_state_live",
    "gateway_owned_recovery_decision_loop_emits_decision_live",
    "gateway_owns_live_observation_stream",
    "gateway_records_lifecycle_result_live",
    "gateway_recovery_decision_loop_ready",
    "gateway_starts_mission_session_live",
    "gateway_starts_supervisor_lifecycle_live",
    "gateway_supervisor_lifecycle_ready",
    "live_gateway_runtime_probe_invoked",
    "nested_authority_boundary_false",
    "physical_hardware_dispatch_delivery_authority_remains_false",
    "source_runtime_cycles_two",
    "source_runtime_executed_run",
    "source_runtime_form3_observed",
    "source_runtime_full_gateway_false_before_probe",
    "source_runtime_progress_counted",
    "source_runtime_schema_observed",
    "source_runtime_scope_matches",
)
C5B_REQUIRED_REF_KEYS = (
    "gateway_live_process_materializer_ref",
    "gateway_live_process_materializer_artifact_path",
    "gateway_live_observation_process_evidence_ref",
    "gateway_live_observation_process_evidence_artifact_path",
    "gateway_live_recovery_decision_process_evidence_ref",
    "gateway_live_recovery_decision_process_evidence_artifact_path",
    "gateway_mission_session_ref",
    "gateway_supervisor_lifecycle_artifact_path",
    "source_runtime_artifact_ref",
    "source_runtime_artifact_path",
)


@dataclass(frozen=True)
class ArtifactHit:
    path: Path
    payload: dict[str, Any]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _latest_artifact(
    root: Path,
    filename: str,
    *,
    predicate: Callable[[Mapping[str, Any]], bool] | None = None,
    path_predicate: Callable[[Path], bool] | None = None,
) -> ArtifactHit | None:
    if not root.exists():
        return None
    candidates = sorted(
        root.rglob(filename),
        key=lambda path: (path.stat().st_mtime, path.as_posix()),
        reverse=True,
    )
    for path in candidates:
        if path_predicate is not None and not path_predicate(path):
            continue
        payload = _read_json(path)
        if payload is None:
            continue
        if predicate is not None and not predicate(payload):
            continue
        return ArtifactHit(path=path, payload=payload)
    return None


def _positive_evidence_path(path: Path) -> bool:
    normalized = f"/{path.as_posix()}"
    return not any(marker in normalized for marker in NEGATIVE_EVIDENCE_PATH_MARKERS)


def _relative(path: Path) -> str:
    return path.as_posix()


def _nested_truthy_paths(value: Any, keys: set[str], prefix: str = "") -> list[str]:
    if isinstance(value, Mapping):
        paths: list[str] = []
        for key, nested in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            if key in keys and nested is True:
                paths.append(next_prefix)
            paths.extend(_nested_truthy_paths(nested, keys, next_prefix))
        return paths
    if isinstance(value, list):
        paths = []
        for index, nested in enumerate(value):
            next_prefix = f"{prefix}[{index}]"
            paths.extend(_nested_truthy_paths(nested, keys, next_prefix))
        return paths
    return []


def _authority_false_summary(payloads: list[Mapping[str, Any]]) -> dict[str, Any]:
    true_paths: list[str] = []
    key_values = dict.fromkeys(AUTHORITY_FALSE_KEYS, False)
    for index, payload in enumerate(payloads):
        for path in _nested_truthy_paths(payload, AUTHORITY_FALSE_KEYS):
            true_paths.append(f"artifact[{index}].{path}")
            key = path.rsplit(".", maxsplit=1)[-1]
            key = key.split("[", maxsplit=1)[0]
            if key in key_values:
                key_values[key] = True
    return {
        "physical_execution_invoked": key_values["physical_execution_invoked"],
        "physical_form1_claimed": key_values["physical_form1_claimed"],
        "physical_success_claimed": key_values["physical_success_claimed"],
        "hardware_target_allowed": key_values["hardware_target_allowed"],
        "dispatch_authority_created": key_values["dispatch_authority_created"],
        "delivery_completion_claimed": key_values["delivery_completion_claimed"],
        "llm_gate_judge_used": key_values["llm_gate_judge_used"],
        "approval_free_stronger_execution": key_values[
            "approval_free_stronger_execution"
        ],
        "public_sync_performed": key_values["public_sync_performed"],
        "sitl_causal_verification_transferred_to_physical": False,
        "authority_boundary_supported": not true_paths,
        "authority_true_paths": true_paths,
    }


def _c5b_supported(payload: Mapping[str, Any]) -> bool:
    checks = payload.get("checks")
    return (
        payload.get("gateway_runtime_probe_status")
        == "full_gateway_runtime_loop_observed"
        and payload.get("causal_form") == "Form 3"
        and payload.get("progress_counted") is True
        and payload.get("cycle_count") == 2
        and payload.get("source_runtime_run_mode") == "executed_run"
        and payload.get("blocked_reasons") == []
        and payload.get("gateway_observation_stream_kind")
        == "source_bound_gateway_live_observation_stream"
        and payload.get("gateway_recovery_decision_loop_kind")
        == "source_bound_gateway_live_recovery_decision_loop"
        and all(payload.get(key) is True for key in C5B_REQUIRED_TRUE_KEYS)
        and isinstance(checks, Mapping)
        and all(checks.get(key) is True for key in C5B_REQUIRED_CHECKS)
        and all(
            isinstance(payload.get(key), str) and payload.get(key)
            for key in C5B_REQUIRED_REF_KEYS
        )
    )


def _path_payload(hit: ArtifactHit | None) -> tuple[str, dict[str, Any]]:
    if hit is None:
        return "", {}
    return _relative(hit.path), hit.payload


def _status_from(condition: bool, *, missing: bool = False) -> str:
    if missing:
        return "missing"
    return "observed" if condition else "blocked"


def _step(
    *,
    step_id: str,
    title: str,
    description: str,
    status: str,
    artifact_path: str,
    artifact: Mapping[str, Any],
    fields: Mapping[str, Any],
    boundary_note: str,
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "title": title,
        "description": description,
        "status": status,
        "artifact_path": artifact_path,
        "schema_version": artifact.get("schema_version", ""),
        "observed_at": artifact.get("observed_at", ""),
        "fields": dict(fields),
        "boundary_note": boundary_note,
    }


def build_current_missionos_milestone_summary(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Return the current read-only GUI milestone summary.

    The summary is intentionally evidence-indexing only. It reads persisted JSON
    artifacts under ``artifact_root`` and never starts any runtime process.
    """

    root = Path(artifact_root)
    b2_hit = _latest_artifact(
        root,
        "parameter_normalized_wind_range_envelope_verification.json",
        predicate=lambda item: item.get("verification_status") == "verified",
        path_predicate=_positive_evidence_path,
    )
    b2_path, b2 = _path_payload(b2_hit)
    b2_cohort = b2.get("cohort") if isinstance(b2.get("cohort"), Mapping) else {}

    b2_ready_hit = _latest_artifact(
        root,
        "wind_form3_physical_envelope_consumption_plan.json",
        predicate=lambda item: item.get("plan_status") == "physical_test_plan_seed_ready",
        path_predicate=_positive_evidence_path,
    )
    b2_ready_path, b2_ready = _path_payload(b2_ready_hit)
    b2_blocked_hit = _latest_artifact(
        root,
        "wind_form3_physical_envelope_consumption_plan.json",
        predicate=lambda item: item.get("plan_status") == "blocked",
    )
    b2_blocked_path, b2_blocked = _path_payload(b2_blocked_hit)

    b1_prep_hit = _latest_artifact(
        root,
        "mission_os_supervisor_scope_cohort.json",
        predicate=lambda item: item.get("scope_status") == "supervisor_scope_observed",
        path_predicate=_positive_evidence_path,
    )
    b1_prep_path, b1_prep = _path_payload(b1_prep_hit)

    b1_runtime_hit = _latest_artifact(
        root,
        "mission_os_multi_condition_supervisor_runtime.json",
        predicate=lambda item: (
            item.get("audit_status") == "multi_condition_supervisor_runtime_observed"
            and item.get("decision_loop_driver") == "mission_os_supervisor"
        ),
        path_predicate=_positive_evidence_path,
    )
    b1_runtime_path, b1_runtime = _path_payload(b1_runtime_hit)

    c5b_hit = _latest_artifact(
        root,
        "gateway_live_runtime_probe.json",
        path_predicate=_positive_evidence_path,
    )
    c5b_path, c5b = _path_payload(c5b_hit)

    selected_payloads = [
        payload
        for payload in [b2, b2_ready, b2_blocked, b1_prep, b1_runtime, c5b]
        if payload
    ]
    authority = _authority_false_summary(selected_payloads)

    b2_observed = (
        b2.get("verification_status") == "verified"
        and b2.get("wind_speed_mps_min") is not None
        and b2.get("wind_speed_mps_max") is not None
    )
    b2_prime_observed = (
        b2_ready.get("plan_status") == "physical_test_plan_seed_ready"
        and b2_ready.get("range_envelope_consumed") is True
        and b2_blocked.get("plan_status") == "blocked"
        and "wind_speed_mps"
        in list(b2_blocked.get("planned_parameters_outside_envelope") or [])
    )
    b1_prep_observed = b1_prep.get("scope_status") == "supervisor_scope_observed"
    b1_runtime_observed = (
        b1_runtime.get("audit_status") == "multi_condition_supervisor_runtime_observed"
        and b1_runtime.get("supervisor_runtime_claim_supported") is True
    )
    c5b_observed = _c5b_supported(c5b)

    steps = [
        _step(
            step_id="B2",
            title="Wind range envelope verified",
            description="Parameter-normalized SITL wind range evidence is active.",
            status=_status_from(b2_observed, missing=not b2),
            artifact_path=b2_path,
            artifact=b2,
            fields={
                "verification_status": b2.get("verification_status"),
                "cohort_status": b2_cohort.get("cohort_status"),
                "wind_speed_mps_min": b2.get("wind_speed_mps_min"),
                "wind_speed_mps_max": b2.get("wind_speed_mps_max"),
                "accepted_wind_mps_values": b2.get("accepted_wind_mps_values"),
                "source_artifact_count": b2_cohort.get("source_artifact_count"),
                "progress_counted": b2.get("progress_counted"),
            },
            boundary_note="SITL-derived parameter knowledge only.",
        ),
        _step(
            step_id="B2'",
            title="Range envelope consumed for physical planning",
            description=(
                "In-range planned wind can seed a physical plan; out-of-range "
                "wind is blocked."
            ),
            status=_status_from(b2_prime_observed, missing=not b2_ready and not b2_blocked),
            artifact_path=b2_ready_path or b2_blocked_path,
            artifact=b2_ready or b2_blocked,
            fields={
                "ready_plan_status": b2_ready.get("plan_status"),
                "ready_range_envelope_consumed": b2_ready.get("range_envelope_consumed"),
                "blocked_plan_status": b2_blocked.get("plan_status"),
                "blocked_parameters": b2_blocked.get(
                    "planned_parameters_outside_envelope"
                ),
                "causal_verification_transferred": b2_ready.get(
                    "causal_verification_transferred"
                ),
                "physical_form1_required": b2_ready.get("physical_form1_required"),
            },
            boundary_note="Physical seed only; no physical success or Form 1 claim.",
        ),
        _step(
            step_id="B1-prep",
            title="Supervisor scope cohort observed",
            description="Wind / obstacle / payload scoped supervisor evidence is bundled.",
            status=_status_from(b1_prep_observed, missing=not b1_prep),
            artifact_path=b1_prep_path,
            artifact=b1_prep,
            fields={
                "scope_status": b1_prep.get("scope_status"),
                "supervisor_scope": b1_prep.get("supervisor_scope"),
                "accepted_condition_count": b1_prep.get("accepted_condition_count"),
                "progress_counted": b1_prep.get("progress_counted"),
                "form3_capability_progress_counted": b1_prep.get(
                    "form3_capability_progress_counted"
                ),
            },
            boundary_note="Cohort only; not a new runtime capability by itself.",
        ),
        _step(
            step_id="B1-runtime",
            title="Multi-condition supervisor runtime observed",
            description="Mission OS supervisor assesses wind / obstacle / payload dimensions.",
            status=_status_from(b1_runtime_observed, missing=not b1_runtime),
            artifact_path=b1_runtime_path,
            artifact=b1_runtime,
            fields={
                "audit_status": b1_runtime.get("audit_status"),
                "causal_form": b1_runtime.get("causal_form"),
                "progress_counted": b1_runtime.get("progress_counted"),
                "supervisor_scope": b1_runtime.get("supervisor_scope"),
                "primary_trigger": b1_runtime.get("primary_trigger"),
                "conflicting_risks": b1_runtime.get("conflicting_risks"),
                "cycle_count": b1_runtime.get("cycle_count"),
            },
            boundary_note="SITL supervisor runtime; full Gateway loop remains separate.",
        ),
        _step(
            step_id="C5b",
            title="Gateway-owned runtime probe observed",
            description="Gateway process-boundary evidence supports the runtime probe.",
            status=_status_from(c5b_observed, missing=not c5b),
            artifact_path=c5b_path,
            artifact=c5b,
            fields={
                "gateway_runtime_probe_status": c5b.get("gateway_runtime_probe_status"),
                "causal_form": c5b.get("causal_form"),
                "progress_counted": c5b.get("progress_counted"),
                "full_gateway_runtime_loop": c5b.get("full_gateway_runtime_loop"),
                "gateway_started_mission_session_live": c5b.get(
                    "gateway_started_mission_session_live"
                ),
                "gateway_supervisor_process_spawned": c5b.get(
                    "gateway_supervisor_process_spawned"
                ),
                "gateway_live_observation_stream": c5b.get(
                    "gateway_live_observation_stream"
                ),
                "gateway_observation_process_started": c5b.get(
                    "gateway_observation_process_started"
                ),
                "gateway_live_recovery_decision_loop": c5b.get(
                    "gateway_live_recovery_decision_loop"
                ),
                "gateway_recovery_decision_process_started": c5b.get(
                    "gateway_recovery_decision_process_started"
                ),
                "gateway_loop_same_session_evidence": c5b.get(
                    "gateway_loop_same_session_evidence"
                ),
                "cycle1_gateway_ref_chain_consistent": c5b.get(
                    "cycle1_gateway_ref_chain_consistent"
                ),
                "cycle2_gateway_ref_chain_consistent": c5b.get(
                    "cycle2_gateway_ref_chain_consistent"
                ),
                "cycle_dispatch_chains_distinct": c5b.get(
                    "cycle_dispatch_chains_distinct"
                ),
                "source_runtime_run_mode": c5b.get("source_runtime_run_mode"),
                "cycle_count": c5b.get("cycle_count"),
            },
            boundary_note=(
                "Gateway-owned SITL runtime probe; not physical execution or "
                "delivery completion."
            ),
        ),
    ]

    all_observed = all(step["status"] == "observed" for step in steps)
    summary_status = (
        "observed" if all_observed and authority["authority_boundary_supported"] else "partial"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_root": _relative(root),
        "milestone_label": "SITL-only Gateway-owned runtime probe milestone",
        "summary_status": summary_status,
        "steps": steps,
        "authority_boundary": authority,
        "not_claimed": [
            "physical_execution",
            "physical_form1",
            "physical_success",
            "hardware_target_authority",
            "dispatch_authority_creation",
            "delivery_completion",
            "public_sync",
        ],
        "next_step": (
            "Use this panel as read-only evidence orientation before entering "
            "physical backend design."
        ),
    }
