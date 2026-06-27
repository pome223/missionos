#!/usr/bin/env python3
"""Build a Gateway mission session scaffold from supervisor runtime evidence.

C1 only creates the Gateway-owned session record that can reference the scoped
Mission OS supervisor runtime evidence. It does not spawn a supervisor, own an
observation stream, drive recovery decisions, or create physical authority.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.mission_designer_form3_envelope_source import (
    MISSION_DESIGNER_FORM3_MISSION_CONTRACT_REF,
    MISSION_DESIGNER_FORM3_TASK_GRAPH_REF,
)


SCHEMA_VERSION = "gateway_mission_session.v1"
SOURCE_SCHEMA_VERSION = "mission_os_multi_condition_supervisor_runtime_audit.v1"
TARGET_SUPERVISOR_SCOPE = "wind_obstacle_payload_form3_sitl"
GATEWAY_SESSION_STATUS_READY = "gateway_mission_session_scaffold_ready"
GATEWAY_SESSION_STATUS_BLOCKED = "blocked"
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


def _nested_authority_reasons(payload: Any, *, path: str = "source") -> list[str]:
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


def _checks_true(source: dict[str, Any], names: list[str]) -> bool:
    checks = source.get("checks")
    checks = checks if isinstance(checks, dict) else {}
    return all(checks.get(name) is True for name in names)


def _source_loop(source: dict[str, Any]) -> dict[str, Any]:
    loop = source.get("mission_os_supervisor_recovery_loop")
    return loop if isinstance(loop, dict) else {}


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _cycle_structure_supported(cycle: dict[str, Any], *, cycle_index: int) -> bool:
    if not isinstance(cycle, dict) or cycle.get("cycle_index") != cycle_index:
        return False
    decision = cycle.get("decision")
    request = cycle.get("action_request")
    receipt = cycle.get("action_receipt")
    outcome = cycle.get("outcome_observation")
    if not all(
        isinstance(artifact, dict) for artifact in (decision, request, receipt, outcome)
    ):
        return False
    decision_ref = cycle.get("decision_ref")
    request_ref = cycle.get("action_request_ref")
    receipt_ref = cycle.get("action_receipt_ref")
    outcome_ref = cycle.get("outcome_observation_ref")
    expected_dispatch_ref = request.get("expected_dispatch_ref")
    return (
        _nonempty_string(decision_ref)
        and _nonempty_string(request_ref)
        and _nonempty_string(receipt_ref)
        and _nonempty_string(outcome_ref)
        and decision.get("schema_version") == "mission_os_recovery_decision.v1"
        and request.get("schema_version") == "mission_os_backend_action_request.v1"
        and receipt.get("schema_version") == "mission_os_backend_action_receipt.v1"
        and outcome.get("schema_version")
        == "mission_os_recovery_outcome_observation.v1"
        and decision.get("decision_id") == decision_ref
        and request.get("request_id") == request_ref
        and receipt.get("receipt_id") == receipt_ref
        and outcome.get("observation_id") == outcome_ref
        and decision.get("cycle_index") == cycle_index
        and request.get("cycle_index") == cycle_index
        and receipt.get("cycle_index") == cycle_index
        and outcome.get("cycle_index") == cycle_index
        and decision.get("decision_loop_driver") == "mission_os_supervisor"
        and decision.get("supervisor_scope") == TARGET_SUPERVISOR_SCOPE
        and decision.get("full_gateway_runtime_loop") is False
        and request.get("decision_ref") == decision_ref
        and receipt.get("action_request_ref") == request_ref
        and outcome.get("action_receipt_ref") == receipt_ref
        and _nonempty_string(expected_dispatch_ref)
        and receipt.get("dispatch_ref") == expected_dispatch_ref
        and receipt.get("dispatch_observed") is True
        and outcome.get("outcome_observed") is True
    )


def _cycle_dispatch_ref(cycle: dict[str, Any]) -> str:
    request = cycle.get("action_request") if isinstance(cycle, dict) else {}
    request = request if isinstance(request, dict) else {}
    dispatch_ref = request.get("expected_dispatch_ref")
    return dispatch_ref if isinstance(dispatch_ref, str) else ""


def _source_ref(source: dict[str, Any]) -> str:
    audit_id = source.get("audit_id")
    if isinstance(audit_id, str) and audit_id:
        return audit_id
    return "mission_os_multi_condition_supervisor_runtime:unknown"


def build_gateway_mission_session(
    source: dict[str, Any],
    *,
    source_artifact_path: Path | None = None,
) -> dict[str, Any]:
    """Build a fail-closed C1 Gateway session scaffold."""

    loop = _source_loop(source)
    cycles = loop.get("cycles")
    cycles = cycles if isinstance(cycles, list) else []
    cycle1 = cycles[0] if len(cycles) > 0 and isinstance(cycles[0], dict) else {}
    cycle2 = cycles[1] if len(cycles) > 1 and isinstance(cycles[1], dict) else {}
    authority_reasons = _nested_authority_reasons(source)
    required_check_names = [
        "compound_assessment_dimensions_observed",
        "secondary_source_artifacts_inactive",
        "conflicting_risks_absent",
        "cycle1_ref_chain_consistent",
        "cycle2_ref_chain_consistent",
        "cycle_dispatch_chains_distinct",
        "cycle_list_exactly_two",
        "nested_authority_boundary_false",
        "top_level_hardware_physical_false",
        "dropoff_not_claimed",
    ]
    checks = {
        "source_schema_observed": source.get("schema_version") == SOURCE_SCHEMA_VERSION,
        "source_runtime_observed": (
            source.get("audit_status") == "multi_condition_supervisor_runtime_observed"
        ),
        "source_form3_supported": source.get("form3_claim_supported") is True,
        "source_progress_counted": source.get("progress_counted") is True,
        "source_supervisor_runtime_claim_supported": (
            source.get("supervisor_runtime_claim_supported") is True
        ),
        "source_decision_loop_driver_supervisor": (
            source.get("decision_loop_driver") == "mission_os_supervisor"
            and loop.get("decision_loop_driver") == "mission_os_supervisor"
        ),
        "source_supervisor_scope_matches": (
            source.get("supervisor_scope") == TARGET_SUPERVISOR_SCOPE
            and loop.get("supervisor_scope") == TARGET_SUPERVISOR_SCOPE
        ),
        "source_full_gateway_runtime_loop_false": (
            source.get("full_gateway_runtime_loop") is False
            and loop.get("full_gateway_runtime_loop") is False
        ),
        "source_loop_present": bool(loop),
        "source_loop_cycle_count_two": loop.get("cycle_count") == 2,
        "source_loop_cycles_exactly_two_structural": len(cycles) == 2,
        "source_cycle1_structure_supported": _cycle_structure_supported(
            cycle1, cycle_index=1
        ),
        "source_cycle2_structure_supported": _cycle_structure_supported(
            cycle2, cycle_index=2
        ),
        "source_cycle_dispatch_refs_distinct": bool(
            _cycle_dispatch_ref(cycle1)
            and _cycle_dispatch_ref(cycle2)
            and _cycle_dispatch_ref(cycle1) != _cycle_dispatch_ref(cycle2)
        ),
        "source_required_checks_true": _checks_true(source, required_check_names),
        "nested_authority_boundary_false": not authority_reasons,
        "source_conflicting_risks_absent": loop.get("conflicting_risks") == []
        and source.get("conflicting_risks") == [],
    }
    blocked_reasons = [
        f"{name}_not_observed" for name, passed in checks.items() if not passed
    ]
    blocked_reasons.extend(authority_reasons)
    ready = not blocked_reasons
    source_runtime_ref = _source_ref(source)
    stable_payload = {
        "schema_version": SCHEMA_VERSION,
        "source_runtime_ref": source_runtime_ref,
        "source_artifact_path": str(source_artifact_path or ""),
        "supervisor_scope": TARGET_SUPERVISOR_SCOPE,
    }
    session_id = _stable_id("gateway_mission_session", stable_payload)
    supervisor_session_id = _stable_id(
        "mission_os_supervisor_session",
        {
            "source_runtime_ref": source_runtime_ref,
            "supervisor_scope": TARGET_SUPERVISOR_SCOPE,
        },
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "gateway_mission_session_id": session_id,
        "gateway_mission_session_ref": f"gateway_mission_session:{session_id}",
        "gateway_session_status": (
            GATEWAY_SESSION_STATUS_READY if ready else GATEWAY_SESSION_STATUS_BLOCKED
        ),
        "gateway_lifecycle_state": "session_record_created" if ready else "blocked",
        "causal_form": "Form 0b",
        "progress_counted": False,
        "gateway_capability_progress_counted": False,
        "mission_contract_ref": MISSION_DESIGNER_FORM3_MISSION_CONTRACT_REF,
        "task_graph_ref": MISSION_DESIGNER_FORM3_TASK_GRAPH_REF,
        "supervisor_scope": TARGET_SUPERVISOR_SCOPE,
        "supervisor_session_id": supervisor_session_id,
        "supervisor_session_ref": (
            f"mission_os_supervisor_session:{supervisor_session_id}"
        ),
        "supervisor_runtime_artifact_ref": source_runtime_ref,
        "supervisor_runtime_artifact_path": str(source_artifact_path or ""),
        "decision_loop_driver": "mission_os_supervisor",
        "full_gateway_runtime_loop": False,
        "gateway_supervisor_spawned": False,
        "gateway_owned_observation_stream": False,
        "gateway_owned_recovery_decision_loop": False,
        "gateway_autonomous_runtime_claimed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_form1_claimed": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "checks": checks,
        "blocked_reasons": blocked_reasons,
        "source_boundary": {
            "source_schema_version": source.get("schema_version"),
            "source_audit_status": source.get("audit_status"),
            "source_causal_form": source.get("causal_form"),
            "source_progress_counted": source.get("progress_counted"),
            "source_full_gateway_runtime_loop": source.get("full_gateway_runtime_loop"),
            "source_supervisor_scope": source.get("supervisor_scope"),
        },
        "scope_boundary_notes": [
            "c1_gateway_session_scaffold_only",
            "gateway_session_references_existing_scoped_supervisor_runtime_evidence",
            "gateway_does_not_spawn_supervisor_in_c1",
            "gateway_does_not_own_observation_stream_in_c1",
            "gateway_does_not_drive_recovery_decision_loop_in_c1",
            "full_gateway_runtime_loop_remains_false",
            "physical_execution_and_dispatch_authority_are_not_created",
        ],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a Gateway mission session scaffold from supervisor evidence."
    )
    parser.add_argument("--supervisor-runtime-artifact", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()

    source = _read_json(args.supervisor_runtime_artifact)
    artifact = build_gateway_mission_session(
        source,
        source_artifact_path=args.supervisor_runtime_artifact,
    )
    stamp = _utc_stamp()
    session_dir = args.output_dir / f"gateway_mission_session_{stamp}"
    session_dir.mkdir(parents=True, exist_ok=False)
    artifact["artifact_dir"] = str(session_dir)
    output_path = session_dir / "gateway_mission_session.json"
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return (
        0
        if artifact.get("gateway_session_status") == GATEWAY_SESSION_STATUS_READY
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
