#!/usr/bin/env python3
"""Build a C5a Gateway full-runtime readiness gate from C4 evidence.

C5a is a no-SITL, no-live-process gate. It verifies that C1-C4 artifacts form a
same-session scaffold chain that is ready to be used as input for a later live
Gateway runtime probe. It does not claim full Gateway runtime.
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
    SCHEMA_VERSION as GATEWAY_OBSERVATION_STREAM_SCHEMA_VERSION,
    STREAM_STATUS_READY,
)
from scripts.build_gateway_owned_recovery_decision_loop import (
    LOOP_STATUS_READY,
    RECOVERY_LOOP_KIND,
    SCHEMA_VERSION as GATEWAY_RECOVERY_LOOP_SCHEMA_VERSION,
)
from scripts.build_gateway_supervisor_lifecycle import (
    LIFECYCLE_STATUS_READY,
    SCHEMA_VERSION as GATEWAY_SUPERVISOR_LIFECYCLE_SCHEMA_VERSION,
)


SCHEMA_VERSION = "gateway_full_runtime_readiness.v1"
READINESS_STATUS_READY = "ready_for_live_gateway_runtime_probe"
READINESS_STATUS_BLOCKED = "blocked"
REPO_ROOT = Path(__file__).resolve().parents[1]
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
    "gateway_live_recovery_decision_loop",
    "gateway_recovery_decision_process_started",
    "gateway_supervisor_process_spawned",
    "live_gateway_runtime_probe_invoked",
}
C5B_REQUIRED_CHECKS = [
    "gateway_starts_mission_session_live",
    "gateway_starts_supervisor_lifecycle_live",
    "gateway_owns_live_observation_stream",
    "gateway_observes_mission_state_live",
    "gateway_owned_recovery_decision_loop_emits_decision_live",
    "backend_action_request_receipt_outcome_same_session_live",
    "gateway_records_lifecycle_result_live",
    "physical_hardware_dispatch_delivery_authority_remains_false",
]


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
    if path.parts and path.parts[0] == "output":
        return (REPO_ROOT / path).resolve()
    return (source_artifact_path.parent / path).resolve()


def _checks_true(payload: dict[str, Any], names: list[str]) -> bool:
    checks = payload.get("checks")
    checks = checks if isinstance(checks, dict) else {}
    return all(checks.get(name) is True for name in names)


def _source_loop(runtime: dict[str, Any]) -> dict[str, Any]:
    loop = runtime.get("mission_os_supervisor_recovery_loop")
    return loop if isinstance(loop, dict) else {}


def _decision_steps_supported(c4_loop: dict[str, Any]) -> bool:
    steps = c4_loop.get("decision_steps")
    steps = steps if isinstance(steps, list) else []
    if len(steps) != 2:
        return False
    dispatch_refs: list[str] = []
    for expected_index, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or step.get("cycle_index") != expected_index:
            return False
        dispatch_ref = step.get("expected_dispatch_ref")
        dispatch_refs.append(dispatch_ref if isinstance(dispatch_ref, str) else "")
        if not (
            _nonempty_string(step.get("gateway_observation_ref"))
            and _nonempty_string(step.get("source_decision_ref"))
            and _nonempty_string(step.get("source_action_request_ref"))
            and _nonempty_string(step.get("source_action_receipt_ref"))
            and _nonempty_string(step.get("source_outcome_observation_ref"))
            and _nonempty_string(step.get("source_runtime_observation_ref"))
            and step.get("same_session_evidence") is True
            and step.get("gateway_decision_loop_kind") == RECOVERY_LOOP_KIND
            and step.get("gateway_live_recovery_decision_loop") is False
            and step.get("gateway_recovery_decision_process_started") is False
            and step.get("full_gateway_runtime_loop") is False
            and step.get("dispatch_observed") is True
            and step.get("outcome_observed") is True
            and step.get("physical_execution_invoked") is False
            and step.get("hardware_target_allowed") is False
            and step.get("dispatch_authority_created") is False
            and step.get("delivery_completion_claimed") is False
        ):
            return False
    return bool(dispatch_refs[0] and dispatch_refs[1] and dispatch_refs[0] != dispatch_refs[1])


def build_gateway_full_runtime_readiness(
    c4_loop: dict[str, Any],
    *,
    c4_artifact_path: Path,
) -> dict[str, Any]:
    """Build a fail-closed C5a readiness artifact from C4 evidence."""

    c4_authority_reasons = _nested_authority_reasons(c4_loop, path="c4")
    stream_path = _path_from_payload(
        c4_loop,
        "gateway_owned_observation_stream_artifact_path",
        source_artifact_path=c4_artifact_path,
    )
    stream, stream_read_reasons = _safe_read_json(stream_path, label="stream")
    stream_authority_reasons = _nested_authority_reasons(stream, path="stream")
    lifecycle_path = _path_from_payload(
        c4_loop,
        "gateway_supervisor_lifecycle_artifact_path",
        source_artifact_path=c4_artifact_path,
    )
    lifecycle, lifecycle_read_reasons = _safe_read_json(
        lifecycle_path,
        label="lifecycle",
    )
    lifecycle_authority_reasons = _nested_authority_reasons(
        lifecycle, path="lifecycle"
    )
    gateway_session_path = _path_from_payload(
        c4_loop,
        "gateway_mission_session_artifact_path",
        source_artifact_path=c4_artifact_path,
    )
    gateway_session, gateway_session_read_reasons = _safe_read_json(
        gateway_session_path,
        label="gateway_session",
    )
    session_authority_reasons = _nested_authority_reasons(
        gateway_session, path="gateway_session"
    )
    runtime_path = _path_from_payload(
        c4_loop,
        "supervisor_runtime_artifact_path",
        source_artifact_path=c4_artifact_path,
    )
    runtime, runtime_read_reasons = _safe_read_json(runtime_path, label="runtime")
    runtime_authority_reasons = _nested_authority_reasons(runtime, path="runtime")
    loop = _source_loop(runtime)
    gateway_mission_session_ref = str(
        c4_loop.get("gateway_mission_session_ref") or ""
    )
    supervisor_session_ref = str(c4_loop.get("supervisor_session_ref") or "")
    mission_contract_ref = str(c4_loop.get("mission_contract_ref") or "")
    task_graph_ref = str(c4_loop.get("task_graph_ref") or "")
    required_c4_checks = [
        "stream_ready",
        "lifecycle_ready",
        "gateway_session_ready",
        "runtime_observed",
        "runtime_required_checks_true",
        "runtime_cycles_exactly_two",
        "cycle1_gateway_ref_chain_consistent",
        "cycle2_gateway_ref_chain_consistent",
        "cycle_dispatch_chains_distinct",
        "conflicting_risks_absent",
        "nested_authority_boundary_false",
    ]
    checks = {
        "c4_schema_observed": (
            c4_loop.get("schema_version") == GATEWAY_RECOVERY_LOOP_SCHEMA_VERSION
        ),
        "c4_ready": c4_loop.get("recovery_decision_loop_status")
        == LOOP_STATUS_READY,
        "c4_form0b": c4_loop.get("causal_form") == "Form 0b",
        "c4_progress_not_counted": c4_loop.get("progress_counted") is False,
        "c4_gateway_capability_not_counted": c4_loop.get(
            "gateway_capability_progress_counted"
        )
        is False,
        "c4_artifact_bound_replay": (
            c4_loop.get("gateway_recovery_decision_loop_kind") == RECOVERY_LOOP_KIND
        ),
        "c4_owned_recovery_loop_record": c4_loop.get(
            "gateway_owned_recovery_decision_loop"
        )
        is True,
        "c4_live_recovery_loop_not_started": (
            c4_loop.get("gateway_live_recovery_decision_loop") is False
            and c4_loop.get("gateway_recovery_decision_process_started") is False
        ),
        "c4_observation_stream_owned_record": c4_loop.get(
            "gateway_owned_observation_stream"
        )
        is True,
        "c4_live_observation_stream_not_started": (
            c4_loop.get("gateway_live_observation_stream") is False
            and c4_loop.get("gateway_observation_process_started") is False
        ),
        "c4_full_gateway_runtime_loop_false": (
            c4_loop.get("full_gateway_runtime_loop") is False
            and c4_loop.get("gateway_autonomous_runtime_claimed") is False
        ),
        "c4_physical_hardware_dispatch_delivery_false": (
            c4_loop.get("physical_execution_invoked") is False
            and c4_loop.get("hardware_target_allowed") is False
            and c4_loop.get("physical_form1_claimed") is False
            and c4_loop.get("dispatch_authority_created") is False
            and c4_loop.get("delivery_completion_claimed") is False
        ),
        "c4_loop_same_session_evidence": c4_loop.get(
            "gateway_loop_same_session_evidence"
        )
        is True,
        "c4_cycle_count_two": c4_loop.get("cycle_count") == 2,
        "c4_decision_steps_supported": _decision_steps_supported(c4_loop),
        "c4_required_checks_true": _checks_true(c4_loop, required_c4_checks),
        "stream_artifact_path_present": stream_path is not None,
        "stream_artifact_readable": bool(stream),
        "stream_schema_observed": stream.get("schema_version")
        == GATEWAY_OBSERVATION_STREAM_SCHEMA_VERSION,
        "stream_ready": stream.get("observation_stream_status") == STREAM_STATUS_READY,
        "c4_observation_stream_ref_matches_artifact": c4_loop.get(
            "gateway_owned_observation_stream_ref"
        )
        == stream.get("gateway_owned_observation_stream_ref"),
        "stream_ref_matches_c4": stream.get("gateway_mission_session_ref")
        == gateway_mission_session_ref,
        "lifecycle_artifact_path_present": lifecycle_path is not None,
        "lifecycle_artifact_readable": bool(lifecycle),
        "lifecycle_schema_observed": lifecycle.get("schema_version")
        == GATEWAY_SUPERVISOR_LIFECYCLE_SCHEMA_VERSION,
        "lifecycle_ready": lifecycle.get("lifecycle_status") == LIFECYCLE_STATUS_READY,
        "c4_lifecycle_ref_matches_artifact": c4_loop.get(
            "gateway_supervisor_lifecycle_ref"
        )
        == lifecycle.get("gateway_supervisor_lifecycle_ref"),
        "lifecycle_ref_matches_c4": lifecycle.get("gateway_mission_session_ref")
        == gateway_mission_session_ref,
        "gateway_session_artifact_path_present": gateway_session_path is not None,
        "gateway_session_artifact_readable": bool(gateway_session),
        "gateway_session_schema_observed": gateway_session.get("schema_version")
        == GATEWAY_MISSION_SESSION_SCHEMA_VERSION,
        "gateway_session_ready": gateway_session.get("gateway_session_status")
        == GATEWAY_SESSION_STATUS_READY,
        "gateway_session_ref_matches_c4": gateway_session.get(
            "gateway_mission_session_ref"
        )
        == gateway_mission_session_ref,
        "runtime_artifact_path_present": runtime_path is not None,
        "runtime_artifact_readable": bool(runtime),
        "runtime_schema_observed": runtime.get("schema_version")
        == "mission_os_multi_condition_supervisor_runtime_audit.v1",
        "runtime_observed": runtime.get("audit_status")
        == "multi_condition_supervisor_runtime_observed",
        "c4_runtime_ref_matches_artifact": c4_loop.get(
            "supervisor_runtime_artifact_ref"
        )
        == runtime.get("audit_id"),
        "runtime_supervisor_scope_matches": runtime.get("supervisor_scope")
        == TARGET_SUPERVISOR_SCOPE
        and loop.get("supervisor_scope") == TARGET_SUPERVISOR_SCOPE,
        "runtime_full_gateway_runtime_loop_false": runtime.get(
            "full_gateway_runtime_loop"
        )
        is False
        and loop.get("full_gateway_runtime_loop") is False,
        "refs_same_session": (
            _nonempty_string(gateway_mission_session_ref)
            and _nonempty_string(supervisor_session_ref)
            and stream.get("supervisor_session_ref") == supervisor_session_ref
            and lifecycle.get("supervisor_session_ref") == supervisor_session_ref
            and gateway_session.get("supervisor_session_ref") == supervisor_session_ref
        ),
        "c4_mission_contract_ref_matches_sources": (
            _nonempty_string(mission_contract_ref)
            and stream.get("mission_contract_ref") == mission_contract_ref
            and lifecycle.get("mission_contract_ref") == mission_contract_ref
            and gateway_session.get("mission_contract_ref") == mission_contract_ref
        ),
        "c4_task_graph_ref_matches_sources": (
            _nonempty_string(task_graph_ref)
            and stream.get("task_graph_ref") == task_graph_ref
            and lifecycle.get("task_graph_ref") == task_graph_ref
            and gateway_session.get("task_graph_ref") == task_graph_ref
        ),
        "nested_authority_boundary_false": not (
            c4_authority_reasons
            or stream_authority_reasons
            or lifecycle_authority_reasons
            or session_authority_reasons
            or runtime_authority_reasons
        ),
    }
    blocked_reasons = [
        f"{name}_not_observed" for name, passed in checks.items() if not passed
    ]
    blocked_reasons.extend(stream_read_reasons)
    blocked_reasons.extend(lifecycle_read_reasons)
    blocked_reasons.extend(gateway_session_read_reasons)
    blocked_reasons.extend(runtime_read_reasons)
    blocked_reasons.extend(c4_authority_reasons)
    blocked_reasons.extend(stream_authority_reasons)
    blocked_reasons.extend(lifecycle_authority_reasons)
    blocked_reasons.extend(session_authority_reasons)
    blocked_reasons.extend(runtime_authority_reasons)
    ready = not blocked_reasons
    readiness_id = _stable_id(
        "gateway_full_runtime_readiness",
        {
            "schema_version": SCHEMA_VERSION,
            "gateway_mission_session_ref": gateway_mission_session_ref,
            "supervisor_session_ref": supervisor_session_ref,
            "c4_artifact_path": str(c4_artifact_path),
        },
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "gateway_full_runtime_readiness_id": readiness_id,
        "gateway_full_runtime_readiness_ref": (
            f"gateway_full_runtime_readiness:{readiness_id}"
        ),
        "readiness_status": (
            READINESS_STATUS_READY if ready else READINESS_STATUS_BLOCKED
        ),
        "causal_form": "Form 0b",
        "progress_counted": False,
        "gateway_capability_progress_counted": False,
        "gateway_mission_session_ref": gateway_mission_session_ref,
        "supervisor_session_ref": supervisor_session_ref,
        "supervisor_scope": TARGET_SUPERVISOR_SCOPE,
        "gateway_owned_recovery_decision_loop_ref": c4_loop.get(
            "gateway_owned_recovery_decision_loop_ref"
        ),
        "gateway_owned_recovery_decision_loop_artifact_path": str(c4_artifact_path),
        "gateway_owned_observation_stream_ref": c4_loop.get(
            "gateway_owned_observation_stream_ref"
        ),
        "gateway_owned_observation_stream_artifact_path": str(stream_path or ""),
        "gateway_supervisor_lifecycle_ref": c4_loop.get(
            "gateway_supervisor_lifecycle_ref"
        ),
        "gateway_supervisor_lifecycle_artifact_path": str(lifecycle_path or ""),
        "gateway_mission_session_artifact_path": str(gateway_session_path or ""),
        "supervisor_runtime_artifact_ref": c4_loop.get("supervisor_runtime_artifact_ref"),
        "supervisor_runtime_artifact_path": str(runtime_path or ""),
        "mission_contract_ref": c4_loop.get("mission_contract_ref"),
        "task_graph_ref": c4_loop.get("task_graph_ref"),
        "ready_for_live_gateway_runtime_probe": ready,
        "live_gateway_runtime_probe_invoked": False,
        "gateway_owned_recovery_decision_loop": c4_loop.get(
            "gateway_owned_recovery_decision_loop"
        )
        is True
        and ready,
        "gateway_recovery_decision_loop_kind": RECOVERY_LOOP_KIND,
        "gateway_live_recovery_decision_loop": False,
        "gateway_recovery_decision_process_started": False,
        "gateway_owned_observation_stream": c4_loop.get(
            "gateway_owned_observation_stream"
        )
        is True
        and ready,
        "gateway_live_observation_stream": False,
        "gateway_observation_process_started": False,
        "gateway_autonomous_runtime_claimed": False,
        "full_gateway_runtime_loop": False,
        "cycle_count": c4_loop.get("cycle_count") if ready else 0,
        "c5b_required_live_checks": C5B_REQUIRED_CHECKS,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_form1_claimed": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "checks": checks,
        "blocked_reasons": blocked_reasons,
        "scope_boundary_notes": [
            "c5a_readiness_gate_only",
            "ready_for_live_gateway_runtime_probe_is_not_full_gateway_runtime",
            "live_gateway_runtime_probe_not_invoked",
            "full_gateway_runtime_loop_remains_false",
            "physical_execution_and_dispatch_authority_are_not_created",
        ],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a Gateway full-runtime readiness gate from C4 evidence."
    )
    parser.add_argument(
        "--gateway-owned-recovery-decision-loop-artifact",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()

    c4_loop = _read_json(args.gateway_owned_recovery_decision_loop_artifact)
    artifact = build_gateway_full_runtime_readiness(
        c4_loop,
        c4_artifact_path=args.gateway_owned_recovery_decision_loop_artifact,
    )
    stamp = _utc_stamp()
    readiness_dir = args.output_dir / f"gateway_full_runtime_readiness_{stamp}"
    readiness_dir.mkdir(parents=True, exist_ok=False)
    artifact["artifact_dir"] = str(readiness_dir)
    output_path = readiness_dir / "gateway_full_runtime_readiness.json"
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact.get("readiness_status") == READINESS_STATUS_READY else 1


if __name__ == "__main__":
    raise SystemExit(main())
