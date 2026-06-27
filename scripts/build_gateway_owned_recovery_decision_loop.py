#!/usr/bin/env python3
"""Build a Gateway-owned recovery decision loop scaffold from C3 evidence.

C4 lets Gateway own an artifact-bound recovery decision loop record over the
existing same-session supervisor runtime chain. It is not a live Gateway
runtime process and it does not claim full Gateway autonomous execution.
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
    TARGET_SUPERVISOR_SCOPE,
)
from scripts.build_gateway_owned_observation_stream import (
    OBSERVATION_STREAM_KIND,
    SCHEMA_VERSION as GATEWAY_OBSERVATION_STREAM_SCHEMA_VERSION,
    STREAM_STATUS_READY,
)
from scripts.build_gateway_supervisor_lifecycle import (
    LIFECYCLE_STATUS_READY,
    SCHEMA_VERSION as GATEWAY_SUPERVISOR_LIFECYCLE_SCHEMA_VERSION,
)


SCHEMA_VERSION = "gateway_owned_recovery_decision_loop.v1"
LOOP_STATUS_READY = "gateway_recovery_decision_loop_scaffold_ready"
LOOP_STATUS_BLOCKED = "blocked"
RECOVERY_LOOP_KIND = "artifact_bound_replay"
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
    "gateway_live_observation_stream",
    "gateway_observation_process_started",
    "gateway_owned_recovery_decision_loop",
    "gateway_live_recovery_decision_loop",
    "gateway_recovery_decision_process_started",
    "gateway_supervisor_process_spawned",
}


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_read_json(path: Path | None, *, label: str) -> tuple[dict[str, Any], list[str]]:
    if path is None:
        return {}, []
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return {}, [f"{label}_artifact_missing"]
    except json.JSONDecodeError:
        return {}, [f"{label}_artifact_malformed_json"]
    except OSError:
        return {}, [f"{label}_artifact_read_error"]
    if not isinstance(payload, dict):
        return {}, [f"{label}_artifact_not_object"]
    return payload, []


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


def _decision(cycle: dict[str, Any]) -> dict[str, Any]:
    decision = cycle.get("decision")
    return decision if isinstance(decision, dict) else {}


def _request(cycle: dict[str, Any]) -> dict[str, Any]:
    request = cycle.get("action_request")
    return request if isinstance(request, dict) else {}


def _receipt(cycle: dict[str, Any]) -> dict[str, Any]:
    receipt = cycle.get("action_receipt")
    return receipt if isinstance(receipt, dict) else {}


def _outcome(cycle: dict[str, Any]) -> dict[str, Any]:
    outcome = cycle.get("outcome_observation")
    return outcome if isinstance(outcome, dict) else {}


def _observation_by_cycle(stream: dict[str, Any]) -> dict[int, dict[str, Any]]:
    observations = stream.get("observations")
    observations = observations if isinstance(observations, list) else []
    result: dict[int, dict[str, Any]] = {}
    for observation in observations:
        if isinstance(observation, dict) and isinstance(
            observation.get("cycle_index"), int
        ):
            result[observation["cycle_index"]] = observation
    return result


def _cycle_ref_chain_supported(
    cycle: dict[str, Any],
    observation: dict[str, Any],
    *,
    cycle_index: int,
) -> bool:
    if not isinstance(cycle, dict) or cycle.get("cycle_index") != cycle_index:
        return False
    if not isinstance(observation, dict) or observation.get("cycle_index") != cycle_index:
        return False
    decision = _decision(cycle)
    request = _request(cycle)
    receipt = _receipt(cycle)
    outcome = _outcome(cycle)
    decision_ref = cycle.get("decision_ref")
    request_ref = cycle.get("action_request_ref")
    receipt_ref = cycle.get("action_receipt_ref")
    outcome_ref = cycle.get("outcome_observation_ref")
    expected_dispatch_ref = request.get("expected_dispatch_ref")
    source_bound_refs = observation.get("source_bound_artifact_refs")
    source_bound_refs = source_bound_refs if isinstance(source_bound_refs, list) else []
    outcome_observation_ref = outcome.get("outcome_observation_ref")
    source_observation_ref = decision.get("source_observation_ref")
    return (
        _nonempty_string(decision_ref)
        and _nonempty_string(request_ref)
        and _nonempty_string(receipt_ref)
        and _nonempty_string(outcome_ref)
        and _nonempty_string(expected_dispatch_ref)
        and decision.get("schema_version") == "mission_os_recovery_decision.v1"
        and request.get("schema_version") == "mission_os_backend_action_request.v1"
        and receipt.get("schema_version") == "mission_os_backend_action_receipt.v1"
        and outcome.get("schema_version")
        == "mission_os_recovery_outcome_observation.v1"
        and decision.get("decision_id") == decision_ref
        and request.get("request_id") == request_ref
        and receipt.get("receipt_id") == receipt_ref
        and outcome.get("observation_id") == outcome_ref
        and request.get("decision_ref") == decision_ref
        and receipt.get("action_request_ref") == request_ref
        and outcome.get("action_receipt_ref") == receipt_ref
        and receipt.get("dispatch_ref") == expected_dispatch_ref
        and receipt.get("dispatch_observed") is True
        and outcome.get("outcome_observed") is True
        and observation.get("source_decision_ref") == decision_ref
        and observation.get("source_observation_ref") == source_observation_ref
        and observation.get("source_outcome_observation_ref") == outcome_ref
        and observation.get("source_outcome_observed") is True
        and observation.get("same_session_evidence") is True
        and observation.get("stale_telemetry_detected") is False
        and observation.get("stale_telemetry_rejected") is True
        and _nonempty_string(source_observation_ref)
        and source_observation_ref in source_bound_refs
        and _nonempty_string(outcome_observation_ref)
        and outcome_observation_ref in source_bound_refs
        and decision.get("full_gateway_runtime_loop") is False
        and request.get("dispatch_authority_created") is False
        and receipt.get("physical_execution_invoked") is False
        and outcome.get("delivery_completion_claimed") is False
    )


def _cycle_dispatch_ref(cycle: dict[str, Any]) -> str:
    dispatch_ref = _request(cycle).get("expected_dispatch_ref")
    return dispatch_ref if isinstance(dispatch_ref, str) else ""


def _build_decision_steps(
    *,
    loop_id: str,
    gateway_mission_session_ref: str,
    supervisor_session_ref: str,
    observations_by_cycle: dict[int, dict[str, Any]],
    cycles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for cycle in cycles:
        cycle_index = cycle["cycle_index"]
        observation = observations_by_cycle[cycle_index]
        decision = _decision(cycle)
        request = _request(cycle)
        receipt = _receipt(cycle)
        outcome = _outcome(cycle)
        step_ref = f"gateway_recovery_decision_step:{loop_id}:cycle_{cycle_index}"
        steps.append(
            {
                "gateway_recovery_decision_step_ref": step_ref,
                "cycle_index": cycle_index,
                "gateway_mission_session_ref": gateway_mission_session_ref,
                "supervisor_session_ref": supervisor_session_ref,
                "gateway_observation_ref": observation.get("gateway_observation_ref"),
                "source_decision_ref": cycle.get("decision_ref"),
                "source_action_request_ref": cycle.get("action_request_ref"),
                "source_action_receipt_ref": cycle.get("action_receipt_ref"),
                "source_outcome_observation_ref": cycle.get("outcome_observation_ref"),
                "source_runtime_observation_ref": decision.get("source_observation_ref"),
                "selected_bounded_action": decision.get("selected_bounded_action"),
                "backend_target": request.get("backend_target"),
                "expected_dispatch_ref": request.get("expected_dispatch_ref"),
                "dispatch_observed": receipt.get("dispatch_observed") is True,
                "outcome_observed": outcome.get("outcome_observed") is True,
                "same_session_evidence": True,
                "gateway_decision_loop_kind": RECOVERY_LOOP_KIND,
                "gateway_live_recovery_decision_loop": False,
                "gateway_recovery_decision_process_started": False,
                "full_gateway_runtime_loop": False,
                "physical_execution_invoked": False,
                "hardware_target_allowed": False,
                "dispatch_authority_created": False,
                "delivery_completion_claimed": False,
            }
        )
    return steps


def build_gateway_owned_recovery_decision_loop(
    stream: dict[str, Any],
    *,
    observation_stream_artifact_path: Path,
) -> dict[str, Any]:
    """Build a fail-closed C4 Gateway recovery decision loop scaffold."""

    stream_authority_reasons = _nested_authority_reasons(stream, path="stream")
    lifecycle_path = _path_from_payload(
        stream,
        "gateway_supervisor_lifecycle_artifact_path",
        source_artifact_path=observation_stream_artifact_path,
    )
    lifecycle, lifecycle_read_reasons = _safe_read_json(
        lifecycle_path,
        label="lifecycle",
    )
    lifecycle_authority_reasons = _nested_authority_reasons(
        lifecycle, path="lifecycle"
    )
    gateway_session_path = _path_from_payload(
        stream,
        "gateway_mission_session_artifact_path",
        source_artifact_path=observation_stream_artifact_path,
    )
    gateway_session, gateway_session_read_reasons = _safe_read_json(
        gateway_session_path,
        label="gateway_session",
    )
    session_authority_reasons = _nested_authority_reasons(
        gateway_session, path="gateway_session"
    )
    runtime_path = _path_from_payload(
        stream,
        "supervisor_runtime_artifact_path",
        source_artifact_path=observation_stream_artifact_path,
    )
    runtime, runtime_read_reasons = _safe_read_json(
        runtime_path,
        label="runtime",
    )
    runtime_authority_reasons = _nested_authority_reasons(runtime, path="runtime")
    loop = _source_loop(runtime)
    cycles = loop.get("cycles")
    cycles = cycles if isinstance(cycles, list) else []
    cycle1 = cycles[0] if len(cycles) > 0 and isinstance(cycles[0], dict) else {}
    cycle2 = cycles[1] if len(cycles) > 1 and isinstance(cycles[1], dict) else {}
    observations_by_cycle = _observation_by_cycle(stream)
    gateway_mission_session_ref = str(
        stream.get("gateway_mission_session_ref") or ""
    )
    supervisor_session_ref = str(stream.get("supervisor_session_ref") or "")
    required_stream_checks = [
        "lifecycle_ready",
        "gateway_session_ready",
        "runtime_observed",
        "runtime_required_checks_true",
        "runtime_cycles_exactly_two",
        "cycle1_observation_supported",
        "cycle2_observation_supported",
        "telemetry_freshness_supported",
        "conflicting_risks_absent",
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
    checks = {
        "stream_schema_observed": (
            stream.get("schema_version") == GATEWAY_OBSERVATION_STREAM_SCHEMA_VERSION
        ),
        "stream_ready": stream.get("observation_stream_status") == STREAM_STATUS_READY,
        "stream_form0b": stream.get("causal_form") == "Form 0b",
        "stream_progress_not_counted": stream.get("progress_counted") is False,
        "stream_gateway_capability_not_counted": stream.get(
            "gateway_capability_progress_counted"
        )
        is False,
        "stream_gateway_owned_observation_stream": stream.get(
            "gateway_owned_observation_stream"
        )
        is True,
        "stream_artifact_bound_replay": (
            stream.get("observation_stream_kind") == OBSERVATION_STREAM_KIND
        ),
        "stream_live_process_not_started": (
            stream.get("gateway_live_observation_stream") is False
            and stream.get("gateway_observation_process_started") is False
        ),
        "stream_recovery_loop_not_owned_yet": (
            stream.get("gateway_owned_recovery_decision_loop") is False
        ),
        "stream_full_gateway_runtime_loop_false": (
            stream.get("full_gateway_runtime_loop") is False
        ),
        "stream_physical_hardware_dispatch_delivery_false": (
            stream.get("physical_execution_invoked") is False
            and stream.get("hardware_target_allowed") is False
            and stream.get("physical_form1_claimed") is False
            and stream.get("dispatch_authority_created") is False
            and stream.get("delivery_completion_claimed") is False
        ),
        "stream_required_checks_true": _checks_true(stream, required_stream_checks),
        "stream_observations_exactly_two": len(observations_by_cycle) == 2,
        "lifecycle_artifact_path_present": lifecycle_path is not None,
        "lifecycle_artifact_readable": bool(lifecycle),
        "lifecycle_schema_observed": lifecycle.get("schema_version")
        == GATEWAY_SUPERVISOR_LIFECYCLE_SCHEMA_VERSION,
        "lifecycle_ready": lifecycle.get("lifecycle_status") == LIFECYCLE_STATUS_READY,
        "lifecycle_ref_matches_stream": lifecycle.get("gateway_mission_session_ref")
        == gateway_mission_session_ref,
        "gateway_session_artifact_path_present": gateway_session_path is not None,
        "gateway_session_artifact_readable": bool(gateway_session),
        "gateway_session_schema_observed": gateway_session.get("schema_version")
        == GATEWAY_MISSION_SESSION_SCHEMA_VERSION,
        "gateway_session_ready": gateway_session.get("gateway_session_status")
        == GATEWAY_SESSION_STATUS_READY,
        "gateway_session_ref_matches_stream": gateway_session.get(
            "gateway_mission_session_ref"
        )
        == gateway_mission_session_ref,
        "runtime_artifact_path_present": runtime_path is not None,
        "runtime_artifact_readable": bool(runtime),
        "runtime_schema_observed": runtime.get("schema_version")
        == "mission_os_multi_condition_supervisor_runtime_audit.v1",
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
        "cycle1_gateway_ref_chain_consistent": _cycle_ref_chain_supported(
            cycle1,
            observations_by_cycle.get(1, {}),
            cycle_index=1,
        ),
        "cycle2_gateway_ref_chain_consistent": _cycle_ref_chain_supported(
            cycle2,
            observations_by_cycle.get(2, {}),
            cycle_index=2,
        ),
        "cycle_dispatch_chains_distinct": bool(
            _cycle_dispatch_ref(cycle1)
            and _cycle_dispatch_ref(cycle2)
            and _cycle_dispatch_ref(cycle1) != _cycle_dispatch_ref(cycle2)
        ),
        "conflicting_risks_absent": runtime.get("conflicting_risks") == []
        and loop.get("conflicting_risks") == [],
        "nested_authority_boundary_false": not (
            stream_authority_reasons
            or lifecycle_authority_reasons
            or session_authority_reasons
            or runtime_authority_reasons
        ),
    }
    blocked_reasons = [
        f"{name}_not_observed" for name, passed in checks.items() if not passed
    ]
    blocked_reasons.extend(lifecycle_read_reasons)
    blocked_reasons.extend(gateway_session_read_reasons)
    blocked_reasons.extend(runtime_read_reasons)
    blocked_reasons.extend(stream_authority_reasons)
    blocked_reasons.extend(lifecycle_authority_reasons)
    blocked_reasons.extend(session_authority_reasons)
    blocked_reasons.extend(runtime_authority_reasons)
    ready = not blocked_reasons
    loop_id = _stable_id(
        "gateway_owned_recovery_decision_loop",
        {
            "schema_version": SCHEMA_VERSION,
            "gateway_mission_session_ref": gateway_mission_session_ref,
            "supervisor_session_ref": supervisor_session_ref,
            "observation_stream_artifact_path": str(observation_stream_artifact_path),
        },
    )
    decision_steps = (
        _build_decision_steps(
            loop_id=loop_id,
            gateway_mission_session_ref=gateway_mission_session_ref,
            supervisor_session_ref=supervisor_session_ref,
            observations_by_cycle=observations_by_cycle,
            cycles=[cycle1, cycle2],
        )
        if ready
        else []
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "gateway_owned_recovery_decision_loop_id": loop_id,
        "gateway_owned_recovery_decision_loop_ref": (
            f"gateway_owned_recovery_decision_loop:{loop_id}"
        ),
        "recovery_decision_loop_status": (
            LOOP_STATUS_READY if ready else LOOP_STATUS_BLOCKED
        ),
        "causal_form": "Form 0b",
        "progress_counted": False,
        "gateway_capability_progress_counted": False,
        "gateway_mission_session_ref": gateway_mission_session_ref,
        "gateway_owned_observation_stream_ref": stream.get(
            "gateway_owned_observation_stream_ref"
        ),
        "gateway_owned_observation_stream_artifact_path": str(
            observation_stream_artifact_path
        ),
        "gateway_supervisor_lifecycle_ref": stream.get(
            "gateway_supervisor_lifecycle_ref"
        ),
        "gateway_supervisor_lifecycle_artifact_path": str(lifecycle_path or ""),
        "gateway_mission_session_artifact_path": str(gateway_session_path or ""),
        "supervisor_runtime_artifact_ref": stream.get("supervisor_runtime_artifact_ref"),
        "supervisor_runtime_artifact_path": str(runtime_path or ""),
        "mission_contract_ref": stream.get("mission_contract_ref"),
        "task_graph_ref": stream.get("task_graph_ref"),
        "supervisor_scope": TARGET_SUPERVISOR_SCOPE,
        "supervisor_session_ref": supervisor_session_ref,
        "gateway_recovery_decision_loop_kind": RECOVERY_LOOP_KIND,
        "gateway_owned_recovery_decision_loop": ready,
        "gateway_live_recovery_decision_loop": False,
        "gateway_recovery_decision_process_started": False,
        "gateway_owned_observation_stream": stream.get(
            "gateway_owned_observation_stream"
        )
        is True
        and ready,
        "gateway_live_observation_stream": False,
        "gateway_observation_process_started": False,
        "gateway_autonomous_runtime_claimed": False,
        "full_gateway_runtime_loop": False,
        "cycle_count": 2 if ready else 0,
        "decision_steps": decision_steps,
        "gateway_loop_same_session_evidence": ready,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_form1_claimed": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "checks": checks,
        "blocked_reasons": blocked_reasons,
        "scope_boundary_notes": [
            "c4_gateway_recovery_decision_loop_scaffold_only",
            "gateway_replays_existing_same_session_decision_action_outcome_chain",
            "gateway_recovery_decision_process_not_started",
            "gateway_does_not_claim_full_autonomous_runtime_in_c4",
            "full_gateway_runtime_loop_remains_false",
            "physical_execution_and_dispatch_authority_are_not_created",
        ],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a Gateway-owned recovery decision loop scaffold from C3 evidence."
        )
    )
    parser.add_argument(
        "--gateway-owned-observation-stream-artifact",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()

    stream = _read_json(args.gateway_owned_observation_stream_artifact)
    artifact = build_gateway_owned_recovery_decision_loop(
        stream,
        observation_stream_artifact_path=args.gateway_owned_observation_stream_artifact,
    )
    stamp = _utc_stamp()
    loop_dir = args.output_dir / f"gateway_owned_recovery_decision_loop_{stamp}"
    loop_dir.mkdir(parents=True, exist_ok=False)
    artifact["artifact_dir"] = str(loop_dir)
    output_path = loop_dir / "gateway_owned_recovery_decision_loop.json"
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return (
        0
        if artifact.get("recovery_decision_loop_status") == LOOP_STATUS_READY
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
