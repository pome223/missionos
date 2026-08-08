"""MissionOS runtime dispatch authority table."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any, Iterator, Mapping
import uuid


DISPATCH_AUTHORITY_TABLE_SCHEMA_VERSION = "missionos_dispatch_authority_table_runtime_state.v1"
DISPATCH_IDEMPOTENCY_RECORD_SCHEMA_VERSION = "missionos_dispatch_idempotency_record.v1"
DISPATCH_IDEMPOTENCY_RESULT_SCHEMA_VERSION = "missionos_dispatch_idempotency_result.v1"
DISPATCH_IDEMPOTENT_RECEIPT_SCHEMA_VERSION = "missionos_dispatch_idempotent_receipt.v1"
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
                "dispatch_idempotency_records": {},
            }
        state.setdefault("schema_version", DISPATCH_AUTHORITY_TABLE_SCHEMA_VERSION)
        state.setdefault("authorities", {})
        state.setdefault("consumed_dispatch_tokens", {})
        state.setdefault("dispatch_idempotency_records", {})
        return state

    @contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        lock_path = self._lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            _DISPATCH_AUTHORITY_TABLE_THREAD_LOCK,
            lock_path.open("a+", encoding="utf-8") as lock_file,
        ):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield self._state()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

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
        with self._locked_state() as state:
            authorities = state.setdefault("authorities", {})
            authorities[authority_id] = {
                "registered_at": _utc_now(),
                "dispatch_authority_id": authority_id,
                "dispatch_ref": authority.get("dispatch_ref"),
                "authority_artifact_path": artifact_path,
                "bounded_action_ref": authority.get("bounded_action_ref"),
                "approval_ref": authority.get("approval_ref"),
                "automatic_dispatch_suppressed": (
                    authority.get("automatic_dispatch_suppressed") is True
                ),
                "operator_approval_required": (
                    authority.get("operator_approval_required") is True
                ),
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
                        "operator_approval_required": (
                            authority.get("operator_approval_required") is True
                        ),
                        "automatic_dispatch_suppressed": (
                            authority.get("automatic_dispatch_suppressed") is True
                        ),
                    }
                ),
            }
            state["table_status"] = "authority_registered"
            state["updated_at"] = _utc_now()
            _write_json(self.state_path, state)
            return dict(authorities[authority_id])

    def lookup(self, authority_id: str) -> dict[str, Any]:
        return dict(self._state().get("authorities", {}).get(authority_id, {}))

    def lookup_dispatch_ref(self, dispatch_ref: str) -> dict[str, Any]:
        """Return the durable idempotency record for ``dispatch_ref``."""

        if not dispatch_ref:
            return {}
        record = self._state().get("dispatch_idempotency_records", {}).get(
            dispatch_ref,
            {},
        )
        return dict(record) if isinstance(record, Mapping) else {}

    @staticmethod
    def _idempotency_result(
        *,
        dispatch_ref: str,
        request_sha256: str,
        status: str,
        send_permitted: bool,
        record: Mapping[str, Any],
        blocking_reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        receipt = record.get("receipt")
        receipt = dict(receipt) if isinstance(receipt, Mapping) else {}
        current_attempt_id = str(record.get("current_attempt_id") or "")
        attempts = record.get("attempts")
        attempts = attempts if isinstance(attempts, Mapping) else {}
        current_attempt = attempts.get(current_attempt_id)
        current_attempt = (
            dict(current_attempt) if isinstance(current_attempt, Mapping) else {}
        )
        return {
            "schema_version": DISPATCH_IDEMPOTENCY_RESULT_SCHEMA_VERSION,
            "dispatch_ref": dispatch_ref,
            "request_sha256": request_sha256,
            "idempotency_status": status,
            "send_permitted": send_permitted,
            "claim_id": current_attempt_id,
            "attempt_ordinal": current_attempt.get("attempt_ordinal"),
            "attempt_status": current_attempt.get("attempt_status"),
            "send_started": current_attempt.get("send_started") is True,
            "send_outcome_known": current_attempt.get("send_outcome_known") is True,
            "safe_to_retry": current_attempt.get("safe_to_retry") is True,
            "existing_receipt": receipt,
            "receipt_present": bool(receipt),
            "idempotent_replay": status == "existing_receipt",
            "automatic_redispatch_performed": False,
            "blocking_reasons": list(blocking_reasons or []),
        }

    def claim_dispatch_ref(
        self,
        *,
        dispatch_ref: str,
        request_payload: Mapping[str, Any],
        correlation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically claim one external send identity before sender invocation.

        A duplicate claim never permits another send. A new attempt is allowed
        only after the previous claim was explicitly cancelled with proof that
        the sender was not invoked.
        """

        normalized_ref = str(dispatch_ref or "").strip()
        if not normalized_ref:
            raise ValueError("dispatch_ref_required")
        request_sha256 = _sha256_json(request_payload)
        now = _utc_now()
        with self._locked_state() as state:
            records = state.setdefault("dispatch_idempotency_records", {})
            existing = records.get(normalized_ref)
            record = dict(existing) if isinstance(existing, Mapping) else {}
            if record:
                existing_request_sha256 = str(record.get("request_sha256") or "")
                if existing_request_sha256 != request_sha256:
                    return self._idempotency_result(
                        dispatch_ref=normalized_ref,
                        request_sha256=request_sha256,
                        status="dispatch_ref_payload_mismatch",
                        send_permitted=False,
                        record=record,
                        blocking_reasons=["dispatch_ref_payload_mismatch"],
                    )
                receipt = record.get("receipt")
                if isinstance(receipt, Mapping) and receipt:
                    return self._idempotency_result(
                        dispatch_ref=normalized_ref,
                        request_sha256=request_sha256,
                        status="existing_receipt",
                        send_permitted=False,
                        record=record,
                    )
                attempts = record.get("attempts")
                attempts = dict(attempts) if isinstance(attempts, Mapping) else {}
                current_attempt_id = str(record.get("current_attempt_id") or "")
                current_attempt = attempts.get(current_attempt_id)
                current_attempt = (
                    dict(current_attempt)
                    if isinstance(current_attempt, Mapping)
                    else {}
                )
                if current_attempt.get("attempt_status") != "cancelled_before_send":
                    return self._idempotency_result(
                        dispatch_ref=normalized_ref,
                        request_sha256=request_sha256,
                        status="unknown_send_outcome",
                        send_permitted=False,
                        record=record,
                        blocking_reasons=["dispatch_outcome_unknown_do_not_retry"],
                    )
                attempt_ordinal = int(current_attempt.get("attempt_ordinal") or 0) + 1
            else:
                record = {
                    "schema_version": DISPATCH_IDEMPOTENCY_RECORD_SCHEMA_VERSION,
                    "dispatch_ref": normalized_ref,
                    "request_sha256": request_sha256,
                    "created_at": now,
                    "correlation": dict(correlation or {}),
                    "attempts": {},
                    "receipt": {},
                }
                attempts = {}
                attempt_ordinal = 1

            claim_digest = hashlib.sha256(
                f"{normalized_ref}:{request_sha256}:{attempt_ordinal}".encode()
            ).hexdigest()
            claim_id = f"dispatch_claim_{claim_digest[:16]}"
            attempts[claim_id] = {
                "claim_id": claim_id,
                "attempt_ordinal": attempt_ordinal,
                "attempt_status": "claimed_before_send",
                "claimed_at": now,
                "send_started": False,
                "send_outcome_known": False,
                "safe_to_retry": False,
                "automatic_redispatch_performed": False,
            }
            record["attempts"] = attempts
            record["current_attempt_id"] = claim_id
            record["updated_at"] = now
            records[normalized_ref] = record
            state["table_status"] = "dispatch_ref_claimed"
            state["updated_at"] = now
            _write_json(self.state_path, state)
            return self._idempotency_result(
                dispatch_ref=normalized_ref,
                request_sha256=request_sha256,
                status="claimed",
                send_permitted=True,
                record=record,
            )

    def mark_dispatch_send_started(
        self,
        *,
        dispatch_ref: str,
        claim_id: str,
    ) -> dict[str, Any]:
        """Persist the send boundary immediately before external invocation."""

        now = _utc_now()
        with self._locked_state() as state:
            records = state.setdefault("dispatch_idempotency_records", {})
            record = records.get(dispatch_ref)
            if not isinstance(record, dict):
                raise ValueError("dispatch_ref_not_claimed")
            attempts = record.get("attempts")
            if not isinstance(attempts, dict):
                raise ValueError("dispatch_attempts_missing")
            attempt = attempts.get(claim_id)
            if not isinstance(attempt, dict):
                raise ValueError("dispatch_claim_not_found")
            if record.get("current_attempt_id") != claim_id:
                raise ValueError("dispatch_claim_not_current")
            status = str(attempt.get("attempt_status") or "")
            if status not in {"claimed_before_send", "send_started"}:
                raise ValueError("dispatch_claim_not_sendable")
            newly_started = status == "claimed_before_send"
            if status == "claimed_before_send":
                attempt.update(
                    {
                        "attempt_status": "send_started",
                        "send_started": True,
                        "send_started_at": now,
                        "send_outcome_known": False,
                        "safe_to_retry": False,
                    }
                )
                record["updated_at"] = now
                state["table_status"] = "dispatch_send_started"
                state["updated_at"] = now
                _write_json(self.state_path, state)
            return self._idempotency_result(
                dispatch_ref=dispatch_ref,
                request_sha256=str(record.get("request_sha256") or ""),
                status=(
                    "send_started" if newly_started else "send_already_started"
                ),
                send_permitted=newly_started,
                record=record,
                blocking_reasons=(
                    []
                    if newly_started
                    else ["dispatch_outcome_unknown_do_not_retry"]
                ),
            )

    def cancel_dispatch_before_send(
        self,
        *,
        dispatch_ref: str,
        claim_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Permit retry only when the sender was provably not invoked."""

        if not str(reason or "").strip():
            raise ValueError("dispatch_cancel_reason_required")
        now = _utc_now()
        with self._locked_state() as state:
            records = state.setdefault("dispatch_idempotency_records", {})
            record = records.get(dispatch_ref)
            if not isinstance(record, dict):
                raise ValueError("dispatch_ref_not_claimed")
            attempts = record.get("attempts")
            if not isinstance(attempts, dict):
                raise ValueError("dispatch_attempts_missing")
            attempt = attempts.get(claim_id)
            if not isinstance(attempt, dict):
                raise ValueError("dispatch_claim_not_found")
            if record.get("current_attempt_id") != claim_id:
                raise ValueError("dispatch_claim_not_current")
            if (
                attempt.get("attempt_status") != "claimed_before_send"
                or attempt.get("send_started") is True
            ):
                raise ValueError("dispatch_send_may_have_started")
            attempt.update(
                {
                    "attempt_status": "cancelled_before_send",
                    "cancelled_at": now,
                    "cancel_reason": str(reason),
                    "send_outcome_known": True,
                    "send_outcome": "not_sent",
                    "safe_to_retry": True,
                }
            )
            record["updated_at"] = now
            state["table_status"] = "dispatch_cancelled_before_send"
            state["updated_at"] = now
            _write_json(self.state_path, state)
            return self._idempotency_result(
                dispatch_ref=dispatch_ref,
                request_sha256=str(record.get("request_sha256") or ""),
                status="cancelled_before_send",
                send_permitted=False,
                record=record,
            )

    def record_dispatch_receipt(
        self,
        *,
        dispatch_ref: str,
        claim_id: str,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Record sender return evidence without inferring ACK or outcome."""

        receipt_dispatch_ref = str(receipt.get("dispatch_ref") or dispatch_ref)
        if receipt_dispatch_ref != dispatch_ref:
            raise ValueError("dispatch_receipt_ref_mismatch")
        now = _utc_now()
        with self._locked_state() as state:
            records = state.setdefault("dispatch_idempotency_records", {})
            record = records.get(dispatch_ref)
            if not isinstance(record, dict):
                raise ValueError("dispatch_ref_not_claimed")
            attempts = record.get("attempts")
            if not isinstance(attempts, dict):
                raise ValueError("dispatch_attempts_missing")
            attempt = attempts.get(claim_id)
            if not isinstance(attempt, dict):
                raise ValueError("dispatch_claim_not_found")
            if record.get("current_attempt_id") != claim_id:
                raise ValueError("dispatch_claim_not_current")
            if attempt.get("attempt_status") not in {
                "send_started",
                "receipt_recorded",
            }:
                raise ValueError("dispatch_send_not_started")

            receipt_payload = dict(receipt)
            receipt_sha256 = _sha256_json(receipt_payload)
            existing_receipt = record.get("receipt")
            if isinstance(existing_receipt, Mapping) and existing_receipt:
                if existing_receipt.get("receipt_sha256") != receipt_sha256:
                    raise ValueError("dispatch_receipt_conflict")
            else:
                record["receipt"] = {
                    "schema_version": DISPATCH_IDEMPOTENT_RECEIPT_SCHEMA_VERSION,
                    "dispatch_ref": dispatch_ref,
                    "claim_id": claim_id,
                    "receipt_sha256": receipt_sha256,
                    "recorded_at": now,
                    "sender_returned": True,
                    "ack_observed": receipt_payload.get("ack_observed") is True,
                    "effect_observed": receipt_payload.get("effect_observed") is True,
                    "verifier_passed": receipt_payload.get("verifier_passed") is True,
                    "completion_claimed": receipt_payload.get("completion_claimed")
                    is True,
                    "physical_execution_invoked": receipt_payload.get(
                        "physical_execution_invoked"
                    )
                    is True,
                    "receipt": receipt_payload,
                }
                attempt.update(
                    {
                        "attempt_status": "receipt_recorded",
                        "receipt_recorded_at": now,
                        "send_outcome_known": True,
                        "send_outcome": "sender_returned_receipt",
                        "safe_to_retry": False,
                    }
                )
                record["updated_at"] = now
                state["table_status"] = "dispatch_receipt_recorded"
                state["updated_at"] = now
                _write_json(self.state_path, state)
            return self._idempotency_result(
                dispatch_ref=dispatch_ref,
                request_sha256=str(record.get("request_sha256") or ""),
                status="receipt_recorded",
                send_permitted=False,
                record=record,
            )

    def record_unknown_dispatch_outcome(
        self,
        *,
        dispatch_ref: str,
        claim_id: str,
        error_type: str,
    ) -> dict[str, Any]:
        """Fail closed after a sender error whose external outcome is unknown."""

        now = _utc_now()
        with self._locked_state() as state:
            records = state.setdefault("dispatch_idempotency_records", {})
            record = records.get(dispatch_ref)
            if not isinstance(record, dict):
                raise ValueError("dispatch_ref_not_claimed")
            attempts = record.get("attempts")
            if not isinstance(attempts, dict):
                raise ValueError("dispatch_attempts_missing")
            attempt = attempts.get(claim_id)
            if not isinstance(attempt, dict):
                raise ValueError("dispatch_claim_not_found")
            if record.get("current_attempt_id") != claim_id:
                raise ValueError("dispatch_claim_not_current")
            if attempt.get("send_started") is not True:
                raise ValueError("dispatch_send_not_started")
            attempt.update(
                {
                    "attempt_status": "unknown_send_outcome",
                    "unknown_outcome_recorded_at": now,
                    "send_error_type": str(error_type or "unknown"),
                    "send_outcome_known": False,
                    "send_outcome": "unknown",
                    "safe_to_retry": False,
                    "automatic_redispatch_performed": False,
                }
            )
            record["updated_at"] = now
            state["table_status"] = "dispatch_outcome_unknown"
            state["updated_at"] = now
            _write_json(self.state_path, state)
            return self._idempotency_result(
                dispatch_ref=dispatch_ref,
                request_sha256=str(record.get("request_sha256") or ""),
                status="unknown_send_outcome",
                send_permitted=False,
                record=record,
                blocking_reasons=["dispatch_outcome_unknown_do_not_retry"],
            )

    def validate_dispatch_request(
        self,
        *,
        authority_id: str,
        operator_approval: Mapping[str, Any],
        deterministic_gate: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._locked_state() as state:
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


__all__ = [
    "DISPATCH_AUTHORITY_TABLE_SCHEMA_VERSION",
    "DISPATCH_IDEMPOTENCY_RECORD_SCHEMA_VERSION",
    "DISPATCH_IDEMPOTENCY_RESULT_SCHEMA_VERSION",
    "DISPATCH_IDEMPOTENT_RECEIPT_SCHEMA_VERSION",
    "DispatchAuthorityTable",
]
