"""Guard an ADK v2 execution node with MissionOS authority and idempotency."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
from typing import Any

from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.gateway.missionos_knowledge_sharing import (
    ARTIFACT_ROOT,
    FORM2A_PAYLOAD_RECOVERY_RESPONSE_KIND,
    FORM2A_TRAJECTORY_REOBSERVATION_OPT_IN_ENV,
    build_form2a_canonical_approval_binding,
    run_form2a_action_consumption,
    validate_form2a_canonical_approval,
)


MISSIONOS_ADK_V2_GUARDED_EXECUTION_ENV = (
    "MISSIONOS_ADK_V2_GUARDED_EXECUTION_ENABLED"
)
MISSIONOS_ADK_V2_GUARDED_EXECUTION_FIXTURE_ENV = (
    "MISSIONOS_ADK_V2_GUARDED_EXECUTION_FIXTURE"
)
MISSIONOS_ADK_V2_DISPATCH_SNAPSHOT_MAX_AGE_SECONDS_ENV = (
    "MISSIONOS_ADK_V2_DISPATCH_SNAPSHOT_MAX_AGE_SECONDS"
)
MISSIONOS_ADK_V2_GUARDED_EXECUTION_SCHEMA_VERSION = (
    "missionos_adk_v2_guarded_execution_result.v1"
)
MISSIONOS_ADK_V2_GUARDED_EXECUTION_STATE_PATH = (
    Path(ARTIFACT_ROOT)
    / "missionos_adk_v2_guarded_execution"
    / "dispatch_idempotency_state.json"
)

FreshPreflightProvider = Callable[
    [Mapping[str, Any]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]
ExecutionBoundary = Callable[
    [Mapping[str, Any]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_json(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _resolve_artifact_path(root: Path, path_value: Any) -> Path:
    path = Path(str(path_value or ""))
    if path.is_absolute() or path.exists():
        return path
    marker = "output/mission_designer_behavior_delta_audits/"
    path_text = path.as_posix()
    if marker in path_text:
        return root / path_text.split(marker, 1)[-1]
    return root / path


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _snapshot_max_age_seconds() -> float:
    raw = os.environ.get(
        MISSIONOS_ADK_V2_DISPATCH_SNAPSHOT_MAX_AGE_SECONDS_ENV,
        "300",
    )
    try:
        value = float(raw)
    except ValueError:
        return -1.0
    return value if value > 0 else -1.0


def _blocked_result(
    reasons: list[str],
    *,
    dispatch_ref: str = "",
    preflight: Mapping[str, Any] | None = None,
    idempotency: Mapping[str, Any] | None = None,
    canonical_approval_validated: bool = False,
    dispatch_authority_created: bool = False,
    dispatch_claimed: bool = False,
    send_started: bool = False,
    executor_invoked: bool = False,
    send_outcome_known: bool = True,
) -> dict[str, Any]:
    return {
        "schema_version": MISSIONOS_ADK_V2_GUARDED_EXECUTION_SCHEMA_VERSION,
        "guarded_execution_status": "blocked",
        "blocking_reasons": reasons,
        "dispatch_ref": dispatch_ref,
        "fresh_dispatch_preflight": dict(preflight or {}),
        "dispatch_idempotency": dict(idempotency or {}),
        "canonical_approval_validated": canonical_approval_validated,
        "dispatch_authority_created": dispatch_authority_created,
        "dispatch_claimed": dispatch_claimed,
        "send_started": send_started,
        "executor_invoked": executor_invoked,
        "external_sender_invoked": False,
        "external_sender_invocation_status": (
            "unknown" if executor_invoked and not send_outcome_known else "not_invoked"
        ),
        "send_outcome_known": send_outcome_known,
        "ack_observed": False,
        "outcome_observed": False,
        "verifier_passed": False,
        "completion_claimed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
        "automatic_redispatch_performed": False,
    }


async def _resolve(
    callback: Callable[[Mapping[str, Any]], Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    result = callback(payload)
    if inspect.isawaitable(result):
        result = await result
    return dict(result) if isinstance(result, Mapping) else {}


def build_form2a_fresh_dispatch_preflight(
    expected_binding: Mapping[str, Any],
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Reload canonical Form 2A state and its telemetry-derived source."""

    root = Path(artifact_root)
    approval_ref = str(expected_binding.get("approval_ref") or "")
    canonical = validate_form2a_canonical_approval(
        approval_ref,
        expected_binding,
        artifact_root=root,
    )
    current = build_form2a_canonical_approval_binding(artifact_root=root)
    selection_path = _resolve_artifact_path(root, current.get("proposal_artifact_path"))
    selection = _read_json(selection_path)
    snapshot_path = _resolve_artifact_path(
        root,
        selection.get("input_form1_artifact_path"),
    )
    snapshot = _read_json(snapshot_path)
    expected_snapshot_sha256 = str(
        selection.get("input_form1_artifact_sha256") or ""
    )
    observed_snapshot_sha256 = (
        _sha256_file(snapshot_path) if snapshot_path.is_file() else ""
    )
    observed_at = _timestamp(snapshot.get("generated_at"))
    if observed_at is None and snapshot_path.is_file():
        observed_at = datetime.fromtimestamp(
            snapshot_path.stat().st_mtime,
            tz=timezone.utc,
        )
    snapshot_age_seconds = (
        max(0.0, (datetime.now(timezone.utc) - observed_at).total_seconds())
        if observed_at is not None
        else None
    )
    maximum_age = _snapshot_max_age_seconds()
    snapshot_fresh = bool(
        snapshot
        and expected_snapshot_sha256
        and observed_snapshot_sha256 == expected_snapshot_sha256
        and snapshot_age_seconds is not None
        and maximum_age > 0
        and snapshot_age_seconds <= maximum_age
    )
    fixture_backend = (
        os.environ.get(MISSIONOS_ADK_V2_GUARDED_EXECUTION_FIXTURE_ENV, "").strip()
        == "1"
    )
    selected_response_kind = str(selection.get("selected_response_kind") or "")
    payload_recovery = (
        selected_response_kind == FORM2A_PAYLOAD_RECOVERY_RESPONSE_KIND
    )
    sitl_opt_in = os.environ.get("RUN_MISSIONOS_SITL_DISPATCH_RUNTIME") == "1"
    reobservation_opt_in = bool(
        payload_recovery
        or os.environ.get(FORM2A_TRAJECTORY_REOBSERVATION_OPT_IN_ENV) == "1"
    )
    backend_ready = fixture_backend or (sitl_opt_in and reobservation_opt_in)
    reasons = list(canonical.get("blocking_reasons") or [])
    if canonical.get("canonical_approval_validated") is not True:
        reasons.append("canonical_approval_not_fresh_at_dispatch")
    if current.get("binding_status") != "ready":
        reasons.append("canonical_binding_not_fresh_at_dispatch")
    if not selection_path.is_file():
        reasons.append("dispatch_proposal_artifact_missing")
    if not snapshot_path.is_file():
        reasons.append("dispatch_telemetry_snapshot_missing")
    elif observed_snapshot_sha256 != expected_snapshot_sha256:
        reasons.append("dispatch_telemetry_snapshot_hash_mismatch")
    if not snapshot_fresh:
        reasons.append("dispatch_telemetry_snapshot_stale")
    if not backend_ready:
        reasons.append("dispatch_backend_not_opted_in")
    source_check = selection.get("source_check")
    source_check = source_check if isinstance(source_check, Mapping) else {}
    policy_passed = source_check.get("source_supported") is True
    if not policy_passed:
        reasons.append("dispatch_policy_source_check_not_passed")
    bounded_action_ref = str(current.get("bounded_action_ref") or "")
    if not bounded_action_ref:
        reasons.append("dispatch_bounded_action_ref_missing")
    preflight_payload = {
        "task_id": current.get("task_id"),
        "approval_ref": approval_ref,
        "mission_response_candidate_ref": current.get(
            "mission_response_candidate_ref"
        ),
        "proposal_sha256": current.get("proposal_sha256"),
        "bounded_action_ref": bounded_action_ref,
        "dispatch_ref": current.get("dispatch_ref"),
        "telemetry_snapshot_ref": snapshot_path.as_posix(),
        "telemetry_snapshot_sha256": observed_snapshot_sha256,
        "telemetry_fresh": snapshot_fresh,
        "policy_ref": f"form2a_source_check:{current.get('proposal_sha256')}",
        "policy_passed": policy_passed,
        "envelope_ref": bounded_action_ref,
        "envelope_passed": bool(bounded_action_ref),
        "backend_ref": (
            "missionos_adk_v2_fixture"
            if fixture_backend
            else "missionos_form2a_sitl"
        ),
        "backend_ready": backend_ready,
        "fixture_backend": fixture_backend,
        "canonical_approval_validated": (
            canonical.get("canonical_approval_validated") is True
        ),
    }
    return {
        "schema_version": "missionos_form2a_fresh_dispatch_preflight.v1",
        "preflight_status": "passed" if not reasons else "blocked",
        "blocking_reasons": list(dict.fromkeys(reasons)),
        "fresh_state": preflight_payload,
        "fresh_state_sha256": _sha256_json(preflight_payload),
        "telemetry_snapshot_age_seconds": snapshot_age_seconds,
        "telemetry_snapshot_max_age_seconds": maximum_age,
    }


def _form2a_execution_boundary(
    request_payload: Mapping[str, Any],
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    dispatch_ref = str(request_payload.get("dispatch_ref") or "")
    fixture_backend = (
        os.environ.get(MISSIONOS_ADK_V2_GUARDED_EXECUTION_FIXTURE_ENV, "").strip()
        == "1"
    )
    if fixture_backend:
        return {
            "schema_version": "missionos_adk_v2_fixture_execution_receipt.v1",
            "dispatch_ref": dispatch_ref,
            "fixture_execution_boundary_invoked": True,
            "external_sender_invoked": False,
            "ack_observed": False,
            "effect_observed": False,
            "verifier_passed": False,
            "completion_claimed": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        }
    summary = run_form2a_action_consumption(artifact_root=artifact_root)
    authority = summary.get("authority_boundary")
    authority = authority if isinstance(authority, Mapping) else {}
    return {
        "schema_version": "missionos_form2a_execution_boundary_receipt.v1",
        "dispatch_ref": dispatch_ref,
        "form2a_action_consumption_summary": summary,
        "external_sender_invoked": (
            authority.get("dispatch_executed_in_runtime") is True
        ),
        "ack_observed": authority.get("command_ack_observed") is True,
        "effect_observed": authority.get("outcome_observed_in_runtime") is True,
        "verifier_passed": (
            authority.get("verified_dispatch_execution_in_runtime") is True
        ),
        "completion_claimed": False,
        "physical_execution_invoked": False,
        "progress_counted": summary.get("progress_counted") is True,
    }


async def execute_guarded_dispatch_once(
    validated_approval: Mapping[str, Any],
    *,
    dispatch_table: DispatchAuthorityTable,
    fresh_preflight_provider: FreshPreflightProvider,
    execution_boundary: ExecutionBoundary,
) -> dict[str, Any]:
    """Invoke one sender only after fresh checks and a durable send claim."""

    dispatch_ref = str(validated_approval.get("dispatch_ref") or "").strip()
    if validated_approval.get("canonical_approval_validated") is not True:
        return _blocked_result(
            ["guarded_execution_requires_canonical_approval"],
            dispatch_ref=dispatch_ref,
        )
    if not dispatch_ref:
        return _blocked_result(["guarded_execution_dispatch_ref_missing"])
    first_preflight = await _resolve(fresh_preflight_provider, validated_approval)
    if first_preflight.get("preflight_status") != "passed":
        return _blocked_result(
            list(first_preflight.get("blocking_reasons") or ["fresh_preflight_blocked"]),
            dispatch_ref=dispatch_ref,
            preflight=first_preflight,
        )
    fresh_state = first_preflight.get("fresh_state")
    fresh_state = dict(fresh_state) if isinstance(fresh_state, Mapping) else {}
    request_payload = {
        "schema_version": "missionos_adk_v2_guarded_dispatch_request.v1",
        **fresh_state,
    }
    if request_payload.get("dispatch_ref") != dispatch_ref:
        return _blocked_result(
            ["fresh_preflight_dispatch_ref_mismatch"],
            dispatch_ref=dispatch_ref,
            preflight=first_preflight,
        )
    claim = dispatch_table.claim_dispatch_ref(
        dispatch_ref=dispatch_ref,
        request_payload=request_payload,
        correlation={
            "approval_ref": validated_approval.get("approval_ref"),
            "mission_response_candidate_ref": validated_approval.get(
                "mission_response_candidate_ref"
            ),
            "proposal_sha256": validated_approval.get("proposal_sha256"),
            "bounded_action_ref": validated_approval.get("bounded_action_ref"),
        },
    )
    if claim.get("idempotency_status") == "existing_receipt":
        existing = claim.get("existing_receipt")
        existing = dict(existing) if isinstance(existing, Mapping) else {}
        receipt = existing.get("receipt")
        receipt = dict(receipt) if isinstance(receipt, Mapping) else {}
        return {
            "schema_version": MISSIONOS_ADK_V2_GUARDED_EXECUTION_SCHEMA_VERSION,
            "guarded_execution_status": "receipt_replayed",
            "blocking_reasons": [],
            "dispatch_ref": dispatch_ref,
            "fresh_dispatch_preflight": first_preflight,
            "dispatch_idempotency": claim,
            "canonical_approval_validated": True,
            "dispatch_authority_created": False,
            "dispatch_claimed": False,
            "send_started": False,
            "executor_invoked": False,
            "external_sender_invoked": False,
            "prior_external_sender_invoked": (
                receipt.get("external_sender_invoked") is True
            ),
            "ack_observed": existing.get("ack_observed") is True,
            "outcome_observed": existing.get("effect_observed") is True,
            "verifier_passed": existing.get("verifier_passed") is True,
            "completion_claimed": existing.get("completion_claimed") is True,
            "physical_execution_invoked": (
                existing.get("physical_execution_invoked") is True
            ),
            "progress_counted": receipt.get("progress_counted") is True,
            "automatic_redispatch_performed": False,
        }
    if claim.get("send_permitted") is not True:
        return _blocked_result(
            list(claim.get("blocking_reasons") or ["dispatch_claim_not_permitted"]),
            dispatch_ref=dispatch_ref,
            preflight=first_preflight,
            idempotency=claim,
        )
    claim_id = str(claim.get("claim_id") or "")
    second_preflight = await _resolve(fresh_preflight_provider, validated_approval)
    second_state = second_preflight.get("fresh_state")
    second_state = dict(second_state) if isinstance(second_state, Mapping) else {}
    second_payload = {
        "schema_version": "missionos_adk_v2_guarded_dispatch_request.v1",
        **second_state,
    }
    if (
        second_preflight.get("preflight_status") != "passed"
        or second_payload != request_payload
    ):
        cancelled = dispatch_table.cancel_dispatch_before_send(
            dispatch_ref=dispatch_ref,
            claim_id=claim_id,
            reason="fresh_preflight_changed_before_send",
        )
        return _blocked_result(
            ["fresh_preflight_changed_before_send"],
            dispatch_ref=dispatch_ref,
            preflight=second_preflight,
            idempotency=cancelled,
        )
    authority_id = (
        "adk_v2_guarded_dispatch_authority:"
        f"{hashlib.sha256(dispatch_ref.encode('utf-8')).hexdigest()[:16]}"
    )
    dispatch_table.register_authority(
        {
            "dispatch_authority_id": authority_id,
            "dispatch_ref": dispatch_ref,
            "approval_ref": validated_approval.get("approval_ref"),
            "bounded_action_ref": validated_approval.get("bounded_action_ref"),
            "operator_approval_required": True,
            "automatic_dispatch_suppressed": True,
        },
        artifact_path=str(validated_approval.get("proposal_artifact_path") or ""),
        backend_target=str(second_state.get("backend_ref") or ""),
    )
    authority_validation = dispatch_table.validate_dispatch_request(
        authority_id=authority_id,
        operator_approval={
            "approval_id": validated_approval.get("approval_ref"),
            "operator_approved_in_artifact": True,
            "automatic_dispatch_executed": False,
        },
        deterministic_gate={
            "gate_result_id": second_preflight.get("fresh_state_sha256"),
            "deterministic_gate_passed_in_artifact": True,
            "automatic_dispatch_executed": False,
        },
    )
    if authority_validation.get("validation_status") != "valid":
        cancelled = dispatch_table.cancel_dispatch_before_send(
            dispatch_ref=dispatch_ref,
            claim_id=claim_id,
            reason="dispatch_authority_validation_blocked",
        )
        return _blocked_result(
            ["dispatch_authority_validation_blocked"],
            dispatch_ref=dispatch_ref,
            preflight=second_preflight,
            idempotency=cancelled,
        )
    send_start = dispatch_table.mark_dispatch_send_started(
        dispatch_ref=dispatch_ref,
        claim_id=claim_id,
    )
    if send_start.get("send_permitted") is not True:
        return _blocked_result(
            list(send_start.get("blocking_reasons") or ["dispatch_send_not_permitted"]),
            dispatch_ref=dispatch_ref,
            preflight=second_preflight,
            idempotency=send_start,
            canonical_approval_validated=True,
            dispatch_authority_created=True,
        )
    try:
        receipt = await _resolve(execution_boundary, request_payload)
        receipt.setdefault("dispatch_ref", dispatch_ref)
        recorded = dispatch_table.record_dispatch_receipt(
            dispatch_ref=dispatch_ref,
            claim_id=claim_id,
            receipt=receipt,
        )
    except Exception as exc:
        try:
            unknown = dispatch_table.record_unknown_dispatch_outcome(
                dispatch_ref=dispatch_ref,
                claim_id=claim_id,
                error_type=type(exc).__name__,
            )
        except Exception as ledger_exc:  # pragma: no cover - storage failures vary.
            unknown = {
                "idempotency_status": "unknown_outcome_record_failed",
                "blocking_reasons": [
                    "dispatch_outcome_unknown_do_not_retry",
                    f"dispatch_ledger_update_failed:{type(ledger_exc).__name__}",
                ],
                "safe_to_retry": False,
            }
        return _blocked_result(
            ["dispatch_outcome_unknown_do_not_retry"],
            dispatch_ref=dispatch_ref,
            preflight=second_preflight,
            idempotency=unknown,
            canonical_approval_validated=True,
            dispatch_authority_created=True,
            dispatch_claimed=True,
            send_started=True,
            executor_invoked=True,
            send_outcome_known=False,
        )
    return {
        "schema_version": MISSIONOS_ADK_V2_GUARDED_EXECUTION_SCHEMA_VERSION,
        "guarded_execution_status": "execution_boundary_returned",
        "blocking_reasons": [],
        "dispatch_ref": dispatch_ref,
        "fresh_dispatch_preflight": second_preflight,
        "dispatch_authority_validation": authority_validation,
        "dispatch_idempotency": recorded,
        "execution_receipt": receipt,
        "canonical_approval_validated": True,
        "dispatch_authority_created": True,
        "dispatch_claimed": True,
        "send_started": True,
        "executor_invoked": True,
        "external_sender_invoked": receipt.get("external_sender_invoked") is True,
        "external_sender_invocation_status": (
            "invoked"
            if receipt.get("external_sender_invoked") is True
            else "not_invoked"
        ),
        "send_outcome_known": True,
        "ack_observed": receipt.get("ack_observed") is True,
        "outcome_observed": receipt.get("effect_observed") is True,
        "verifier_passed": receipt.get("verifier_passed") is True,
        "completion_claimed": receipt.get("completion_claimed") is True,
        "physical_execution_invoked": (
            receipt.get("physical_execution_invoked") is True
        ),
        "progress_counted": receipt.get("progress_counted") is True,
        "automatic_redispatch_performed": False,
    }


def build_form2a_guarded_execution_handler(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
    dispatch_state_path: Path | str = MISSIONOS_ADK_V2_GUARDED_EXECUTION_STATE_PATH,
    fresh_preflight_provider: FreshPreflightProvider | None = None,
    execution_boundary: ExecutionBoundary | None = None,
) -> ExecutionBoundary:
    root = Path(artifact_root)
    table = DispatchAuthorityTable(Path(dispatch_state_path))

    def preflight(payload: Mapping[str, Any]) -> dict[str, Any]:
        return build_form2a_fresh_dispatch_preflight(payload, artifact_root=root)

    def execute(payload: Mapping[str, Any]) -> dict[str, Any]:
        return _form2a_execution_boundary(payload, artifact_root=root)

    async def handler(validated_approval: Mapping[str, Any]) -> dict[str, Any]:
        return await execute_guarded_dispatch_once(
            validated_approval,
            dispatch_table=table,
            fresh_preflight_provider=fresh_preflight_provider or preflight,
            execution_boundary=execution_boundary or execute,
        )

    return handler


__all__ = [
    "MISSIONOS_ADK_V2_DISPATCH_SNAPSHOT_MAX_AGE_SECONDS_ENV",
    "MISSIONOS_ADK_V2_GUARDED_EXECUTION_ENV",
    "MISSIONOS_ADK_V2_GUARDED_EXECUTION_FIXTURE_ENV",
    "MISSIONOS_ADK_V2_GUARDED_EXECUTION_SCHEMA_VERSION",
    "build_form2a_fresh_dispatch_preflight",
    "build_form2a_guarded_execution_handler",
    "execute_guarded_dispatch_once",
]
