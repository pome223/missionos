"""MissionOS runtime dispatch authority table."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any, Mapping
import uuid


DISPATCH_AUTHORITY_TABLE_SCHEMA_VERSION = "missionos_dispatch_authority_table_runtime_state.v1"
_DISPATCH_AUTHORITY_TABLE_THREAD_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(dict(payload), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _sha256_json(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(payload), sort_keys=True).encode("utf-8")).hexdigest()


class DispatchAuthorityTable:
    """File-backed authority table for bounded MissionOS dispatch requests."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def _lock_path(self) -> Path:
        return self.state_path.with_name(f"{self.state_path.name}.lock")

    def _state(self) -> dict[str, Any]:
        state = _read_json(self.state_path)
        if not state:
            state = {
                "schema_version": DISPATCH_AUTHORITY_TABLE_SCHEMA_VERSION,
                "table_status": "initialized",
                "authorities": {},
                "consumed_dispatch_tokens": {},
            }
        state.setdefault("schema_version", DISPATCH_AUTHORITY_TABLE_SCHEMA_VERSION)
        state.setdefault("authorities", {})
        state.setdefault("consumed_dispatch_tokens", {})
        return state

    def register_authority(
        self,
        authority: Mapping[str, Any],
        *,
        artifact_path: str,
        backend_target: str = "px4_gazebo_sitl",
    ) -> dict[str, Any]:
        authority_id = str(authority.get("dispatch_authority_id") or "")
        if not authority_id:
            raise ValueError("dispatch_authority_id_required")
        state = self._state()
        authorities = state.setdefault("authorities", {})
        authorities[authority_id] = {
            "registered_at": _utc_now(),
            "dispatch_authority_id": authority_id,
            "dispatch_ref": authority.get("dispatch_ref"),
            "authority_artifact_path": artifact_path,
            "bounded_action_ref": authority.get("bounded_action_ref"),
            "approval_ref": authority.get("approval_ref"),
            "automatic_dispatch_suppressed": authority.get("automatic_dispatch_suppressed") is True,
            "operator_approval_required": authority.get("operator_approval_required") is True,
            "backend_target": backend_target,
            "dispatch_authority_source_projection_sha256": authority.get(
                "runtime_source_projection_sha256"
            )
            or _sha256_json(
                {
                    "schema_version": authority.get("schema_version"),
                    "dispatch_authority_id": authority_id,
                    "dispatch_ref": authority.get("dispatch_ref"),
                    "active_policy_ref": authority.get("active_policy_ref"),
                    "automatic_recovery_rule_ref": authority.get(
                        "automatic_recovery_rule_ref"
                    ),
                    "approval_ref": authority.get("approval_ref"),
                    "bounded_action_ref": authority.get("bounded_action_ref"),
                    "operator_approval_required": authority.get(
                        "operator_approval_required"
                    )
                    is True,
                    "automatic_dispatch_suppressed": authority.get(
                        "automatic_dispatch_suppressed"
                    )
                    is True,
                }
            ),
        }
        state["table_status"] = "authority_registered"
        state["updated_at"] = _utc_now()
        _write_json(self.state_path, state)
        return dict(authorities[authority_id])

    def lookup(self, authority_id: str) -> dict[str, Any]:
        return dict(self._state().get("authorities", {}).get(authority_id, {}))

    def validate_dispatch_request(
        self,
        *,
        authority_id: str,
        operator_approval: Mapping[str, Any],
        deterministic_gate: Mapping[str, Any],
    ) -> dict[str, Any]:
        lock_path = self._lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            _DISPATCH_AUTHORITY_TABLE_THREAD_LOCK,
            lock_path.open("a+", encoding="utf-8") as lock_file,
        ):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            state = self._state()
            authority = dict(state.get("authorities", {}).get(authority_id, {}))
            approval_id = str(operator_approval.get("approval_id") or "")
            gate_result_id = str(deterministic_gate.get("gate_result_id") or "")
            dispatch_ref = str(authority.get("dispatch_ref") or "")
            dispatch_token = f"{authority_id}:{approval_id}:{gate_result_id}"
            consumed_tokens = state.setdefault("consumed_dispatch_tokens", {})
            replay_detected = bool(dispatch_token in consumed_tokens)
            approval_present = bool(approval_id)
            gate_present = bool(gate_result_id)
            operator_approved = (
                operator_approval.get("operator_approved_in_artifact") is True
                or operator_approval.get("operator_approved") is True
            )
            deterministic_gate_passed = (
                deterministic_gate.get("deterministic_gate_passed_in_artifact") is True
                or deterministic_gate.get("deterministic_gate_passed") is True
            )
            valid = bool(
                authority
                and authority.get("operator_approval_required") is True
                and authority.get("automatic_dispatch_suppressed") is True
                and approval_present
                and gate_present
                and operator_approved
                and deterministic_gate_passed
                and operator_approval.get("automatic_dispatch_executed") is not True
                and deterministic_gate.get("automatic_dispatch_executed") is not True
                and not replay_detected
            )
            consumed_at = _utc_now()
            if valid:
                consumed_tokens[dispatch_token] = {
                    "consumed_at": consumed_at,
                    "authority_id": authority_id,
                    "approval_id": approval_id,
                    "gate_result_id": gate_result_id,
                    "dispatch_ref": dispatch_ref,
                    "session_id": operator_approval.get("session_id")
                    or deterministic_gate.get("session_id"),
                }
                state["table_status"] = "authority_consumed_for_dispatch"
                state["updated_at"] = consumed_at
                _write_json(self.state_path, state)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return {
            "schema_version": "missionos_dispatch_authority_validation.v1",
            "validated_at": consumed_at,
            "authority_id": authority_id,
            "dispatch_ref": dispatch_ref,
            "validation_status": "valid" if valid else "blocked",
            "authority_registered": bool(authority),
            "backend_target": authority.get("backend_target"),
            "operator_approval_consumed": valid,
            "operator_approval_id": approval_id,
            "operator_approval_present": approval_present,
            "operator_approval_token_consumed": valid,
            "gate_result_id": gate_result_id,
            "gate_result_present": gate_present,
            "gate_result_consumed": valid,
            "dispatch_replay_detected": replay_detected,
            "operator_approved_in_artifact": operator_approved,
            "deterministic_gate_passed_in_artifact": deterministic_gate_passed,
            "automatic_dispatch_suppressed": authority.get("automatic_dispatch_suppressed") is True,
        }


__all__ = ["DISPATCH_AUTHORITY_TABLE_SCHEMA_VERSION", "DispatchAuthorityTable"]
