#!/usr/bin/env python3
"""Build a Gateway-owned observation stream scaffold from C2 lifecycle evidence.

C3 lets Gateway own an artifact-bound observation stream record over the
existing same-session supervisor runtime evidence. It is still not a live
Gateway observation process and it does not drive a recovery decision loop.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.build_gateway_mission_session import (
    GATEWAY_SESSION_STATUS_READY,
    SCHEMA_VERSION as GATEWAY_MISSION_SESSION_SCHEMA_VERSION,
    SOURCE_SCHEMA_VERSION as SUPERVISOR_RUNTIME_SCHEMA_VERSION,
    TARGET_SUPERVISOR_SCOPE,
)
from scripts.build_gateway_supervisor_lifecycle import (
    LIFECYCLE_STATUS_READY,
    SCHEMA_VERSION as GATEWAY_SUPERVISOR_LIFECYCLE_SCHEMA_VERSION,
)


SCHEMA_VERSION = "gateway_owned_observation_stream.v1"
STREAM_STATUS_READY = "gateway_observation_stream_scaffold_ready"
STREAM_STATUS_BLOCKED = "blocked"
OBSERVATION_STREAM_KIND = "artifact_bound_replay"
SOURCE_OBSERVATION_REF_PREFIXES = (
    "route_deviation_observation:",
    "px4_gazebo_route_recovery_completion:",
)
OUTCOME_OBSERVATION_REF_PREFIXES = ("px4_gazebo_route_recovery_completion:",)
EXPECTED_SECONDARY_RISK_SOURCE_PREFIXES = {
    "route_blocking": "route_blocking_verification:",
    "payload_feasibility": "simulator_condition_application:",
    "battery_warning": "observed_vehicle_condition_evidence:",
    "telemetry_continuity": "telemetry_freshness_report:",
}
AUTHORITY_KEYS_REQUIRED_FALSE = {
    "ai_judgment_is_gate_verdict",
    "ai_judgment_created_dispatch_authority",
    "llm_gate_judge_used",
    "dispatch_authority_created",
    "created_dispatch_authority",
    "automatic_dispatch_allowed",
    "delivery_completion_claimed",
    "hardware_target_allowed",
    "physical_execution_invoked",
    "physical_form1_claimed",
    "full_gateway_runtime_loop",
    "gateway_autonomous_runtime_claimed",
    "gateway_owned_observation_stream",
    "gateway_owned_recovery_decision_loop",
    "gateway_supervisor_process_spawned",
}


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{prefix}_{digest[:12]}"


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _ref_has_prefix(value: Any, prefixes: tuple[str, ...] | str) -> bool:
    if isinstance(prefixes, str):
        prefixes = (prefixes,)
    return isinstance(value, str) and any(
        value.startswith(prefix) and len(value.split(":", 1)[1]) > 0
        for prefix in prefixes
    )


def _nested_authority_reasons(payload: Any, *, path: str) -> list[str]:
    reasons: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            current_path = f"{path}.{key}"
            if key in AUTHORITY_KEYS_REQUIRED_FALSE and value is not False:
                reasons.append(f"nested_authority_{current_path}_not_false")
            if isinstance(value, (dict, list)):
                reasons.extend(_nested_authority_reasons(value, path=current_path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            reasons.extend(_nested_authority_reasons(value, path=f"{path}[{index}]"))
    return reasons


def _checks_true(payload: dict[str, Any], names: list[str]) -> bool:
    checks = payload.get("checks")
    checks = checks if isinstance(checks, dict) else {}
    return all(checks.get(name) is True for name in names)


def _path_from_payload(
    payload: dict[str, Any],
    key: str,
    *,
    source_artifact_path: Path,
) -> Path | None:
    raw_value = payload.get(key)
    if not isinstance(raw_value, str) or not raw_value:
        return None
    path = Path(raw_value)
    if path.is_absolute():
        return path
    return (source_artifact_path.parent / path).resolve() if not path.exists() else path


def _source_loop(runtime: dict[str, Any]) -> dict[str, Any]:
    loop = runtime.get("mission_os_supervisor_recovery_loop")
    return loop if isinstance(loop, dict) else {}


def _assessment(cycle: dict[str, Any]) -> dict[str, Any]:
    decision = cycle.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    assessment = decision.get("assessment_inputs")
    return assessment if isinstance(assessment, dict) else {}


def _telemetry(assessment: dict[str, Any]) -> dict[str, Any]:
    telemetry = assessment.get("telemetry")
    return telemetry if isinstance(telemetry, dict) else {}


def _source_bound_refs(cycle: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    decision = cycle.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    source_observation_ref = decision.get("source_observation_ref")
    if _nonempty_string(source_observation_ref):
        refs.append(source_observation_ref)
    assessment = _assessment(cycle)
    telemetry = _telemetry(assessment)
    telemetry_ref = telemetry.get("telemetry_freshness_ref")
    if _nonempty_string(telemetry_ref):
        refs.append(telemetry_ref)
    for risk in assessment.get("secondary_risks", []):
        if isinstance(risk, dict) and _nonempty_string(risk.get("source_ref")):
            refs.append(risk["source_ref"])
    outcome = cycle.get("outcome_observation")
    outcome = outcome if isinstance(outcome, dict) else {}
    outcome_ref = outcome.get("outcome_observation_ref")
    if _nonempty_string(outcome_ref):
        refs.append(outcome_ref)
    return sorted(set(refs))


def _secondary_risk_refs_supported(assessment: dict[str, Any]) -> bool:
    secondary_risks = assessment.get("secondary_risks")
    if not isinstance(secondary_risks, list):
        return False
    by_condition = {
        risk.get("condition"): risk for risk in secondary_risks if isinstance(risk, dict)
    }
    for condition, prefix in EXPECTED_SECONDARY_RISK_SOURCE_PREFIXES.items():
        risk = by_condition.get(condition)
        if not isinstance(risk, dict):
            return False
        if not _ref_has_prefix(risk.get("source_ref"), prefix):
            return False
        if risk.get("silent_continuation_allowed") is not True:
            return False
    return True


def _cycle_observation_supported(cycle: dict[str, Any], *, cycle_index: int) -> bool:
    if not isinstance(cycle, dict) or cycle.get("cycle_index") != cycle_index:
        return False
    decision = cycle.get("decision")
    outcome = cycle.get("outcome_observation")
    decision = decision if isinstance(decision, dict) else {}
    outcome = outcome if isinstance(outcome, dict) else {}
    assessment = _assessment(cycle)
    telemetry = _telemetry(assessment)
    source_observation_ref = decision.get("source_observation_ref")
    telemetry_freshness_ref = telemetry.get("telemetry_freshness_ref")
    outcome_observation_ref = outcome.get("outcome_observation_ref")
    return (
        _ref_has_prefix(source_observation_ref, SOURCE_OBSERVATION_REF_PREFIXES)
        and assessment.get("assessment_mode") == "compound_mission_state_assessment"
        and assessment.get("supervisor_scope") == TARGET_SUPERVISOR_SCOPE
        and _secondary_risk_refs_supported(assessment)
        and telemetry.get("telemetry_continuity")
        == "sufficient_for_recovery_audit"
        and telemetry.get("observer_dropout_active") is False
        and _ref_has_prefix(telemetry_freshness_ref, "telemetry_freshness_report:")
        and outcome.get("outcome_observed") is True
        and _ref_has_prefix(outcome_observation_ref, OUTCOME_OBSERVATION_REF_PREFIXES)
        and outcome.get("delivery_completion_claimed") is False
        and len(_source_bound_refs(cycle)) >= 4
    )


def _build_observations(
    *,
    stream_id: str,
    gateway_mission_session_ref: str,
    supervisor_session_ref: str,
    cycles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for cycle in cycles:
        cycle_index = cycle["cycle_index"]
        decision = cycle.get("decision", {})
        outcome = cycle.get("outcome_observation", {})
        assessment = _assessment(cycle)
        telemetry = _telemetry(assessment)
        observation_ref = (
            f"gateway_observation_stream:{stream_id}:cycle_{cycle_index}"
        )
        observations.append(
            {
                "gateway_observation_ref": observation_ref,
                "cycle_index": cycle_index,
                "gateway_mission_session_ref": gateway_mission_session_ref,
                "supervisor_session_ref": supervisor_session_ref,
                "source_decision_ref": cycle.get("decision_ref"),
                "source_observation_ref": decision.get("source_observation_ref"),
                "source_outcome_observation_ref": cycle.get(
                    "outcome_observation_ref"
                ),
                "source_outcome_observed": outcome.get("outcome_observed") is True,
                "telemetry_freshness_ref": telemetry.get("telemetry_freshness_ref"),
                "telemetry_continuity": telemetry.get("telemetry_continuity"),
                "stale_telemetry_detected": False,
                "stale_telemetry_rejected": True,
                "source_bound_artifact_refs": _source_bound_refs(cycle),
                "same_session_evidence": True,
                "full_gateway_runtime_loop": False,
                "physical_execution_invoked": False,
                "hardware_target_allowed": False,
                "dispatch_authority_created": False,
                "delivery_completion_claimed": False,
            }
        )
    return observations


def build_gateway_owned_observation_stream(
    lifecycle: dict[str, Any],
    *,
    lifecycle_artifact_path: Path,
) -> dict[str, Any]:
    """Build a fail-closed C3 Gateway observation stream scaffold."""

    lifecycle_authority_reasons = _nested_authority_reasons(
        lifecycle, path="lifecycle"
    )
    gateway_session_path = _path_from_payload(
        lifecycle,
        "gateway_mission_session_artifact_path",
        source_artifact_path=lifecycle_artifact_path,
    )
    gateway_session = _read_json(gateway_session_path) if gateway_session_path else {}
    session_authority_reasons = _nested_authority_reasons(
        gateway_session, path="gateway_session"
    )
    runtime_path = _path_from_payload(
        gateway_session,
        "supervisor_runtime_artifact_path",
        source_artifact_path=gateway_session_path or lifecycle_artifact_path,
    )
    runtime = _read_json(runtime_path) if runtime_path else {}
    runtime_authority_reasons = _nested_authority_reasons(runtime, path="runtime")
    loop = _source_loop(runtime)
    cycles = loop.get("cycles")
    cycles = cycles if isinstance(cycles, list) else []
    cycle1 = cycles[0] if len(cycles) > 0 and isinstance(cycles[0], dict) else {}
    cycle2 = cycles[1] if len(cycles) > 1 and isinstance(cycles[1], dict) else {}
    required_lifecycle_checks = [
        "source_gateway_session_ready",
        "source_gateway_supervisor_process_not_spawned",
        "source_gateway_supervisor_spawn_kind_session_record_only_or_absent",
        "source_gateway_observation_stream_not_owned",
        "source_gateway_recovery_loop_not_owned",
        "nested_authority_boundary_false",
    ]
    required_runtime_checks = [
        "compound_assessment_dimensions_observed",
        "conflicting_risks_absent",
        "cycle1_ref_chain_consistent",
        "cycle2_ref_chain_consistent",
        "cycle_dispatch_chains_distinct",
        "cycle_list_exactly_two",
        "nested_authority_boundary_false",
        "top_level_hardware_physical_false",
        "dropoff_not_claimed",
    ]
    gateway_mission_session_ref = str(
        lifecycle.get("gateway_mission_session_ref") or ""
    )
    supervisor_session_ref = str(lifecycle.get("supervisor_session_ref") or "")
    checks = {
        "lifecycle_schema_observed": (
            lifecycle.get("schema_version")
            == GATEWAY_SUPERVISOR_LIFECYCLE_SCHEMA_VERSION
        ),
        "lifecycle_ready": lifecycle.get("lifecycle_status")
        == LIFECYCLE_STATUS_READY,
        "lifecycle_form0b": lifecycle.get("causal_form") == "Form 0b",
        "lifecycle_progress_not_counted": lifecycle.get("progress_counted") is False,
        "lifecycle_gateway_capability_not_counted": lifecycle.get(
            "gateway_capability_progress_counted"
        )
        is False,
        "lifecycle_gateway_session_ref_present": _nonempty_string(
            gateway_mission_session_ref
        ),
        "lifecycle_supervisor_session_ref_present": _nonempty_string(
            supervisor_session_ref
        ),
        "lifecycle_session_record_spawn_only": (
            lifecycle.get("gateway_supervisor_spawn_kind") == "session_record_only"
            and lifecycle.get("gateway_supervisor_session_spawned") is True
            and lifecycle.get("gateway_supervisor_process_spawned") is False
        ),
        "lifecycle_full_gateway_runtime_loop_false": lifecycle.get(
            "full_gateway_runtime_loop"
        )
        is False,
        "lifecycle_gateway_observation_stream_not_owned": lifecycle.get(
            "gateway_owned_observation_stream"
        )
        is False,
        "lifecycle_gateway_recovery_loop_not_owned": lifecycle.get(
            "gateway_owned_recovery_decision_loop"
        )
        is False,
        "lifecycle_physical_hardware_dispatch_delivery_false": (
            lifecycle.get("physical_execution_invoked") is False
            and lifecycle.get("hardware_target_allowed") is False
            and lifecycle.get("physical_form1_claimed") is False
            and lifecycle.get("dispatch_authority_created") is False
            and lifecycle.get("delivery_completion_claimed") is False
        ),
        "lifecycle_required_checks_true": _checks_true(
            lifecycle, required_lifecycle_checks
        ),
        "gateway_session_artifact_path_present": gateway_session_path is not None,
        "gateway_session_artifact_readable": bool(gateway_session),
        "gateway_session_schema_observed": gateway_session.get("schema_version")
        == GATEWAY_MISSION_SESSION_SCHEMA_VERSION,
        "gateway_session_ready": gateway_session.get("gateway_session_status")
        == GATEWAY_SESSION_STATUS_READY,
        "gateway_session_ref_matches_lifecycle": gateway_session.get(
            "gateway_mission_session_ref"
        )
        == gateway_mission_session_ref,
        "gateway_session_supervisor_ref_matches_lifecycle": gateway_session.get(
            "supervisor_session_ref"
        )
        == supervisor_session_ref,
        "runtime_artifact_path_present": runtime_path is not None,
        "runtime_artifact_readable": bool(runtime),
        "runtime_schema_observed": runtime.get("schema_version")
        == SUPERVISOR_RUNTIME_SCHEMA_VERSION,
        "runtime_observed": runtime.get("audit_status")
        == "multi_condition_supervisor_runtime_observed",
        "runtime_supervisor_scope_matches": runtime.get("supervisor_scope")
        == TARGET_SUPERVISOR_SCOPE
        and loop.get("supervisor_scope") == TARGET_SUPERVISOR_SCOPE,
        "runtime_full_gateway_runtime_loop_false": runtime.get(
            "full_gateway_runtime_loop"
        )
        is False
        and loop.get("full_gateway_runtime_loop") is False,
        "runtime_required_checks_true": _checks_true(runtime, required_runtime_checks),
        "runtime_cycles_exactly_two": len(cycles) == 2 and loop.get("cycle_count") == 2,
        "cycle1_observation_supported": _cycle_observation_supported(
            cycle1, cycle_index=1
        ),
        "cycle2_observation_supported": _cycle_observation_supported(
            cycle2, cycle_index=2
        ),
        "telemetry_freshness_supported": (
            _telemetry(_assessment(cycle1)).get("telemetry_continuity")
            == "sufficient_for_recovery_audit"
            and _telemetry(_assessment(cycle2)).get("telemetry_continuity")
            == "sufficient_for_recovery_audit"
        ),
        "conflicting_risks_absent": runtime.get("conflicting_risks") == []
        and loop.get("conflicting_risks") == [],
        "nested_authority_boundary_false": not (
            lifecycle_authority_reasons
            or session_authority_reasons
            or runtime_authority_reasons
        ),
    }
    blocked_reasons = [
        f"{name}_not_observed" for name, passed in checks.items() if not passed
    ]
    blocked_reasons.extend(lifecycle_authority_reasons)
    blocked_reasons.extend(session_authority_reasons)
    blocked_reasons.extend(runtime_authority_reasons)
    ready = not blocked_reasons
    stream_id = _stable_id(
        "gateway_owned_observation_stream",
        {
            "schema_version": SCHEMA_VERSION,
            "gateway_mission_session_ref": gateway_mission_session_ref,
            "supervisor_session_ref": supervisor_session_ref,
            "lifecycle_artifact_path": str(lifecycle_artifact_path),
        },
    )
    observations = (
        _build_observations(
            stream_id=stream_id,
            gateway_mission_session_ref=gateway_mission_session_ref,
            supervisor_session_ref=supervisor_session_ref,
            cycles=[cycle1, cycle2],
        )
        if ready
        else []
    )
    observation_refs = [
        ref
        for observation in observations
        for ref in observation["source_bound_artifact_refs"]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "gateway_owned_observation_stream_id": stream_id,
        "gateway_owned_observation_stream_ref": (
            f"gateway_owned_observation_stream:{stream_id}"
        ),
        "observation_stream_status": (
            STREAM_STATUS_READY if ready else STREAM_STATUS_BLOCKED
        ),
        "causal_form": "Form 0b",
        "progress_counted": False,
        "gateway_capability_progress_counted": False,
        "gateway_mission_session_ref": gateway_mission_session_ref,
        "gateway_supervisor_lifecycle_ref": lifecycle.get(
            "gateway_supervisor_lifecycle_ref"
        ),
        "gateway_supervisor_lifecycle_artifact_path": str(lifecycle_artifact_path),
        "gateway_mission_session_artifact_path": str(gateway_session_path or ""),
        "supervisor_runtime_artifact_ref": lifecycle.get(
            "supervisor_runtime_artifact_ref"
        ),
        "supervisor_runtime_artifact_path": str(runtime_path or ""),
        "mission_contract_ref": lifecycle.get("mission_contract_ref"),
        "task_graph_ref": lifecycle.get("task_graph_ref"),
        "supervisor_scope": TARGET_SUPERVISOR_SCOPE,
        "supervisor_session_ref": supervisor_session_ref,
        "observation_stream_kind": OBSERVATION_STREAM_KIND,
        "gateway_owned_observation_stream": ready,
        "gateway_live_observation_stream": False,
        "gateway_observation_process_started": False,
        "gateway_owned_recovery_decision_loop": False,
        "gateway_autonomous_runtime_claimed": False,
        "full_gateway_runtime_loop": False,
        "observation_window": {
            "cycle_index_start": 1 if ready else None,
            "cycle_index_end": 2 if ready else None,
            "source_runtime_observed_at": runtime.get("observed_at"),
            "same_session_evidence": ready,
        },
        "observations": observations,
        "source_bound_artifact_refs": sorted(set(observation_refs)),
        "stale_telemetry_rejected": ready,
        "stale_telemetry_detected": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_form1_claimed": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "checks": checks,
        "blocked_reasons": blocked_reasons,
        "scope_boundary_notes": [
            "c3_gateway_observation_stream_scaffold_only",
            "gateway_indexes_existing_same_session_observation_refs",
            "gateway_live_observation_process_not_started",
            "gateway_does_not_drive_recovery_decision_loop_in_c3",
            "full_gateway_runtime_loop_remains_false",
            "physical_execution_and_dispatch_authority_are_not_created",
        ],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a Gateway-owned observation stream scaffold."
    )
    parser.add_argument(
        "--gateway-supervisor-lifecycle-artifact",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()

    lifecycle = _read_json(args.gateway_supervisor_lifecycle_artifact)
    artifact = build_gateway_owned_observation_stream(
        lifecycle,
        lifecycle_artifact_path=args.gateway_supervisor_lifecycle_artifact,
    )
    stamp = _utc_stamp()
    stream_dir = args.output_dir / f"gateway_owned_observation_stream_{stamp}"
    stream_dir.mkdir(parents=True, exist_ok=False)
    artifact["artifact_dir"] = str(stream_dir)
    output_path = stream_dir / "gateway_owned_observation_stream.json"
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact.get("observation_stream_status") == STREAM_STATUS_READY else 1


if __name__ == "__main__":
    raise SystemExit(main())
