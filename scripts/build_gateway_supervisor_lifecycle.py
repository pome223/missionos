#!/usr/bin/env python3
"""Build a Gateway supervisor lifecycle scaffold from a Gateway session.

C2 records that Gateway can create and track a supervisor session lifecycle
record. It is still a scaffold: no supervisor process is started, no Gateway-
owned observation stream is created, and no Gateway-owned recovery loop runs.
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


SCHEMA_VERSION = "gateway_supervisor_lifecycle.v1"
LIFECYCLE_STATUS_READY = "gateway_supervisor_lifecycle_scaffold_ready"
LIFECYCLE_STATUS_BLOCKED = "blocked"
SUPPORTED_LIFECYCLE_STATES = [
    "created",
    "spawned",
    "running",
    "heartbeat_observed",
    "completed",
    "failed",
    "aborted",
]
READY_LIFECYCLE_PATH = [
    "created",
    "spawned",
    "running",
    "heartbeat_observed",
    "completed",
]
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


def _build_lifecycle_events(
    *,
    lifecycle_id: str,
    gateway_mission_session_ref: str,
    supervisor_session_ref: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    previous_event_ref = ""
    for index, state in enumerate(READY_LIFECYCLE_PATH):
        event_ref = f"gateway_supervisor_lifecycle_event:{lifecycle_id}:{state}"
        event = {
            "event_ref": event_ref,
            "event_index": index,
            "lifecycle_state": state,
            "event_observed": True,
            "gateway_mission_session_ref": gateway_mission_session_ref,
            "supervisor_session_ref": supervisor_session_ref,
            "previous_event_ref": previous_event_ref,
            "full_gateway_runtime_loop": False,
            "physical_execution_invoked": False,
            "hardware_target_allowed": False,
            "dispatch_authority_created": False,
            "delivery_completion_claimed": False,
        }
        events.append(event)
        previous_event_ref = event_ref
    return events


def build_gateway_supervisor_lifecycle(
    gateway_session: dict[str, Any],
    *,
    source_artifact_path: Path | None = None,
) -> dict[str, Any]:
    """Build a fail-closed C2 Gateway supervisor lifecycle scaffold."""

    authority_reasons = _nested_authority_reasons(gateway_session)
    required_source_checks = [
        "source_loop_cycles_exactly_two_structural",
        "source_cycle1_structure_supported",
        "source_cycle2_structure_supported",
        "source_cycle_dispatch_refs_distinct",
        "source_required_checks_true",
        "nested_authority_boundary_false",
        "source_full_gateway_runtime_loop_false",
    ]
    gateway_session_ref = str(gateway_session.get("gateway_mission_session_ref") or "")
    supervisor_session_ref = str(gateway_session.get("supervisor_session_ref") or "")
    checks = {
        "source_schema_observed": (
            gateway_session.get("schema_version")
            == GATEWAY_MISSION_SESSION_SCHEMA_VERSION
        ),
        "source_gateway_session_ready": (
            gateway_session.get("gateway_session_status") == GATEWAY_SESSION_STATUS_READY
        ),
        "source_form0b": gateway_session.get("causal_form") == "Form 0b",
        "source_progress_not_counted": gateway_session.get("progress_counted") is False,
        "source_gateway_capability_not_counted": gateway_session.get(
            "gateway_capability_progress_counted"
        )
        is False,
        "source_gateway_session_ref_present": _nonempty_string(gateway_session_ref),
        "source_supervisor_session_ref_present": _nonempty_string(supervisor_session_ref),
        "source_supervisor_runtime_ref_present": _nonempty_string(
            gateway_session.get("supervisor_runtime_artifact_ref")
        ),
        "source_supervisor_scope_matches": (
            gateway_session.get("supervisor_scope") == TARGET_SUPERVISOR_SCOPE
        ),
        "source_full_gateway_runtime_loop_false": (
            gateway_session.get("full_gateway_runtime_loop") is False
        ),
        "source_gateway_supervisor_not_spawned_yet": (
            gateway_session.get("gateway_supervisor_spawned") is False
        ),
        "source_gateway_supervisor_process_not_spawned": (
            gateway_session.get("gateway_supervisor_process_spawned") in (None, False)
        ),
        "source_gateway_supervisor_spawn_kind_session_record_only_or_absent": (
            gateway_session.get("gateway_supervisor_spawn_kind")
            in (None, "", "session_record_only")
        ),
        "source_gateway_observation_stream_not_owned": (
            gateway_session.get("gateway_owned_observation_stream") is False
        ),
        "source_gateway_recovery_loop_not_owned": (
            gateway_session.get("gateway_owned_recovery_decision_loop") is False
        ),
        "source_physical_hardware_dispatch_delivery_false": (
            gateway_session.get("physical_execution_invoked") is False
            and gateway_session.get("hardware_target_allowed") is False
            and gateway_session.get("physical_form1_claimed") is False
            and gateway_session.get("dispatch_authority_created") is False
            and gateway_session.get("delivery_completion_claimed") is False
        ),
        "source_required_checks_true": _checks_true(
            gateway_session, required_source_checks
        ),
        "nested_authority_boundary_false": not authority_reasons,
        "source_blocked_reasons_absent": gateway_session.get("blocked_reasons") == [],
    }
    blocked_reasons = [
        f"{name}_not_observed" for name, passed in checks.items() if not passed
    ]
    blocked_reasons.extend(authority_reasons)
    ready = not blocked_reasons
    lifecycle_id = _stable_id(
        "gateway_supervisor_lifecycle",
        {
            "schema_version": SCHEMA_VERSION,
            "gateway_mission_session_ref": gateway_session_ref,
            "supervisor_session_ref": supervisor_session_ref,
            "source_artifact_path": str(source_artifact_path or ""),
        },
    )
    events = (
        _build_lifecycle_events(
            lifecycle_id=lifecycle_id,
            gateway_mission_session_ref=gateway_session_ref,
            supervisor_session_ref=supervisor_session_ref,
        )
        if ready
        else []
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "gateway_supervisor_lifecycle_id": lifecycle_id,
        "gateway_supervisor_lifecycle_ref": (
            f"gateway_supervisor_lifecycle:{lifecycle_id}"
        ),
        "lifecycle_status": (
            LIFECYCLE_STATUS_READY if ready else LIFECYCLE_STATUS_BLOCKED
        ),
        "causal_form": "Form 0b",
        "progress_counted": False,
        "gateway_capability_progress_counted": False,
        "gateway_mission_session_ref": gateway_session_ref,
        "gateway_mission_session_artifact_path": str(source_artifact_path or ""),
        "mission_contract_ref": gateway_session.get("mission_contract_ref"),
        "task_graph_ref": gateway_session.get("task_graph_ref"),
        "supervisor_scope": TARGET_SUPERVISOR_SCOPE,
        "supervisor_session_ref": supervisor_session_ref,
        "supervisor_runtime_artifact_ref": gateway_session.get(
            "supervisor_runtime_artifact_ref"
        ),
        "supported_lifecycle_states": SUPPORTED_LIFECYCLE_STATES,
        "observed_lifecycle_states": READY_LIFECYCLE_PATH if ready else [],
        "gateway_lifecycle_state": "completed" if ready else "blocked",
        "lifecycle_events": events,
        "gateway_supervisor_spawn_kind": "session_record_only",
        "gateway_supervisor_session_spawned": ready,
        "gateway_supervisor_spawn_record_created": ready,
        "gateway_supervisor_process_spawned": False,
        "gateway_supervisor_stop_recorded": ready,
        "gateway_supervisor_failed_record_supported": True,
        "gateway_supervisor_aborted_record_supported": True,
        "gateway_owned_observation_stream": False,
        "gateway_owned_recovery_decision_loop": False,
        "gateway_autonomous_runtime_claimed": False,
        "full_gateway_runtime_loop": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_form1_claimed": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "checks": checks,
        "blocked_reasons": blocked_reasons,
        "scope_boundary_notes": [
            "c2_gateway_supervisor_lifecycle_scaffold_only",
            "gateway_spawns_session_record_not_runtime_process",
            "gateway_tracks_lifecycle_states_without_owning_observation_stream",
            "gateway_does_not_drive_recovery_decision_loop_in_c2",
            "full_gateway_runtime_loop_remains_false",
            "physical_execution_and_dispatch_authority_are_not_created",
        ],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a Gateway supervisor lifecycle scaffold from a Gateway session."
    )
    parser.add_argument("--gateway-mission-session-artifact", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()

    gateway_session = _read_json(args.gateway_mission_session_artifact)
    artifact = build_gateway_supervisor_lifecycle(
        gateway_session,
        source_artifact_path=args.gateway_mission_session_artifact,
    )
    stamp = _utc_stamp()
    lifecycle_dir = args.output_dir / f"gateway_supervisor_lifecycle_{stamp}"
    lifecycle_dir.mkdir(parents=True, exist_ok=False)
    artifact["artifact_dir"] = str(lifecycle_dir)
    output_path = lifecycle_dir / "gateway_supervisor_lifecycle.json"
    _write_json(output_path, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact.get("lifecycle_status") == LIFECYCLE_STATUS_READY else 1


if __name__ == "__main__":
    raise SystemExit(main())
