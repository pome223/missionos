from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping

from google.adk.sessions import InMemorySessionService
from fastapi.testclient import TestClient

from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.gateway import server as gateway_server
from src.intelligence import missionos_adk_v2_guarded_execution as guarded
from src.intelligence import missionos_adk_v2_hitl as hitl


def _validated_approval() -> dict[str, Any]:
    return {
        "task_id": "missionos_form2a_task:guarded-fixture",
        "canonical_approval_validated": True,
        "approval_ref": "approval:guarded-fixture",
        "mission_response_candidate_ref": "candidate:guarded-fixture",
        "proposal_sha256": "a" * 64,
        "bounded_action_ref": "bounded:guarded-fixture",
        "dispatch_ref": "dispatch:guarded-fixture",
    }


def _preflight(*, telemetry_sha256: str = "b" * 64) -> dict[str, Any]:
    state = {
        **_validated_approval(),
        "telemetry_snapshot_ref": "telemetry:guarded-fixture",
        "telemetry_snapshot_sha256": telemetry_sha256,
        "telemetry_fresh": True,
        "policy_ref": "policy:guarded-fixture",
        "policy_passed": True,
        "envelope_ref": "bounded:guarded-fixture",
        "envelope_passed": True,
        "backend_ref": "fixture:guarded-executor",
        "backend_ready": True,
    }
    return {
        "preflight_status": "passed",
        "blocking_reasons": [],
        "fresh_state": state,
        "fresh_state_sha256": telemetry_sha256,
    }


def _approved_validator(
    approval_ref: str,
    expected_binding: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "validation_status": "approved",
        "blocking_reasons": [],
        "approval_ref": approval_ref,
        "canonical_approval_validated": True,
        "mission_response_candidate_ref": expected_binding[
            "mission_response_candidate_ref"
        ],
        "proposal_sha256": expected_binding["proposal_sha256"],
        "bounded_action_ref": expected_binding["bounded_action_ref"],
        "dispatch_ref": expected_binding["dispatch_ref"],
    }


def test_guarded_dispatch_records_receipt_and_replays_without_second_send(
    tmp_path: Path,
) -> None:
    table = DispatchAuthorityTable(tmp_path / "dispatch-state.json")
    send_calls: list[str] = []

    def sender(request: Mapping[str, Any]) -> dict[str, Any]:
        send_calls.append(str(request["dispatch_ref"]))
        return {
            "dispatch_ref": request["dispatch_ref"],
            "external_sender_invoked": False,
            "ack_observed": False,
            "effect_observed": False,
            "verifier_passed": False,
            "completion_claimed": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        }

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        first = await guarded.execute_guarded_dispatch_once(
            _validated_approval(),
            dispatch_table=table,
            fresh_preflight_provider=lambda _payload: _preflight(),
            execution_boundary=sender,
        )
        replay = await guarded.execute_guarded_dispatch_once(
            _validated_approval(),
            dispatch_table=table,
            fresh_preflight_provider=lambda _payload: _preflight(),
            execution_boundary=sender,
        )
        return first, replay

    first, replay = asyncio.run(scenario())

    assert first["guarded_execution_status"] == "execution_boundary_returned"
    assert first["dispatch_authority_created"] is True
    assert first["dispatch_claimed"] is True
    assert first["send_started"] is True
    assert first["executor_invoked"] is True
    assert first["external_sender_invoked"] is False
    assert replay["guarded_execution_status"] == "receipt_replayed"
    assert replay["executor_invoked"] is False
    assert replay["automatic_redispatch_performed"] is False
    assert send_calls == ["dispatch:guarded-fixture"]


def test_guarded_dispatch_cancels_before_send_when_fresh_state_changes(
    tmp_path: Path,
) -> None:
    table = DispatchAuthorityTable(tmp_path / "dispatch-state.json")
    preflight_calls = 0
    send_calls = 0

    def provider(_payload: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal preflight_calls
        preflight_calls += 1
        return _preflight(telemetry_sha256=("b" if preflight_calls == 1 else "c") * 64)

    def sender(_request: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal send_calls
        send_calls += 1
        return {}

    result = asyncio.run(
        guarded.execute_guarded_dispatch_once(
            _validated_approval(),
            dispatch_table=table,
            fresh_preflight_provider=provider,
            execution_boundary=sender,
        )
    )

    assert result["guarded_execution_status"] == "blocked"
    assert result["blocking_reasons"] == ["fresh_preflight_changed_before_send"]
    assert result["dispatch_idempotency"]["idempotency_status"] == (
        "cancelled_before_send"
    )
    assert result["send_started"] is False
    assert result["executor_invoked"] is False
    assert send_calls == 0


def test_guarded_dispatch_sender_error_is_unknown_and_never_retried(
    tmp_path: Path,
) -> None:
    table = DispatchAuthorityTable(tmp_path / "dispatch-state.json")
    send_calls = 0

    def sender(_request: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal send_calls
        send_calls += 1
        raise ConnectionError("fixture ambiguous send failure")

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        first = await guarded.execute_guarded_dispatch_once(
            _validated_approval(),
            dispatch_table=table,
            fresh_preflight_provider=lambda _payload: _preflight(),
            execution_boundary=sender,
        )
        retry = await guarded.execute_guarded_dispatch_once(
            _validated_approval(),
            dispatch_table=table,
            fresh_preflight_provider=lambda _payload: _preflight(),
            execution_boundary=sender,
        )
        return first, retry

    first, retry = asyncio.run(scenario())

    assert first["blocking_reasons"] == ["dispatch_outcome_unknown_do_not_retry"]
    assert retry["blocking_reasons"] == ["dispatch_outcome_unknown_do_not_retry"]
    assert first["canonical_approval_validated"] is True
    assert first["dispatch_authority_created"] is True
    assert first["executor_invoked"] is True
    assert first["send_started"] is True
    assert first["send_outcome_known"] is False
    assert first["external_sender_invocation_status"] == "unknown"
    assert retry["executor_invoked"] is False
    assert retry["automatic_redispatch_performed"] is False
    assert send_calls == 1


def test_invalid_sender_receipt_preserves_invocation_and_blocks_retry(
    tmp_path: Path,
) -> None:
    table = DispatchAuthorityTable(tmp_path / "dispatch-state.json")
    send_calls = 0

    def sender(_request: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal send_calls
        send_calls += 1
        return {"dispatch_ref": "dispatch:wrong-receipt"}

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        first = await guarded.execute_guarded_dispatch_once(
            _validated_approval(),
            dispatch_table=table,
            fresh_preflight_provider=lambda _payload: _preflight(),
            execution_boundary=sender,
        )
        retry = await guarded.execute_guarded_dispatch_once(
            _validated_approval(),
            dispatch_table=table,
            fresh_preflight_provider=lambda _payload: _preflight(),
            execution_boundary=sender,
        )
        return first, retry

    first, retry = asyncio.run(scenario())

    assert first["blocking_reasons"] == ["dispatch_outcome_unknown_do_not_retry"]
    assert first["canonical_approval_validated"] is True
    assert first["dispatch_authority_created"] is True
    assert first["executor_invoked"] is True
    assert first["send_started"] is True
    assert first["send_outcome_known"] is False
    assert first["external_sender_invocation_status"] == "unknown"
    assert retry["blocking_reasons"] == ["dispatch_outcome_unknown_do_not_retry"]
    assert retry["executor_invoked"] is False
    assert retry["automatic_redispatch_performed"] is False
    assert send_calls == 1


def test_duplicate_adk_resume_invokes_guarded_execution_boundary_once(
    tmp_path: Path,
) -> None:
    service = InMemorySessionService()
    table = DispatchAuthorityTable(tmp_path / "dispatch-state.json")
    send_calls: list[str] = []

    def sender(request: Mapping[str, Any]) -> dict[str, Any]:
        send_calls.append(str(request["dispatch_ref"]))
        return {
            "dispatch_ref": request["dispatch_ref"],
            "external_sender_invoked": False,
            "ack_observed": False,
            "effect_observed": False,
            "verifier_passed": False,
            "completion_claimed": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        }

    async def execution_handler(
        validated: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await guarded.execute_guarded_dispatch_once(
            validated,
            dispatch_table=table,
            fresh_preflight_provider=lambda _payload: _preflight(),
            execution_boundary=sender,
        )

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], Any]:
        paused = await hitl.start_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id="operator-guarded",
            approval_binding={
                "binding_status": "ready",
                "blocking_reasons": [],
                **_validated_approval(),
            },
            approval_validator=_approved_validator,
        )
        resumed = await hitl.resume_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id="operator-guarded",
            adk_session_id=paused["adk_session_id"],
            human_response={"approval_ref": "approval:guarded-fixture"},
            approval_validator=_approved_validator,
            guarded_execution_handler=execution_handler,
        )
        duplicate = await hitl.resume_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id="operator-guarded",
            adk_session_id=paused["adk_session_id"],
            human_response={"approval_ref": "approval:guarded-fixture"},
            approval_validator=_approved_validator,
            guarded_execution_handler=execution_handler,
        )
        session = await service.get_session(
            app_name=hitl.MISSIONOS_ADK_V2_HITL_APP_NAME,
            user_id="operator-guarded",
            session_id=paused["adk_session_id"],
        )
        return resumed, duplicate, session

    resumed, duplicate, session = asyncio.run(scenario())

    assert resumed["hitl_status"] == "guarded_execution_completed"
    assert resumed["canonical_approval_validated"] is True
    assert resumed["dispatch_authority_created"] is True
    assert resumed["executor_invoked"] is True
    assert resumed["physical_execution_invoked"] is False
    assert resumed["outcome_observed"] is False
    assert resumed["progress_counted"] is False
    trace = resumed["same_task_audit_trace"]
    assert trace["task_id"] == "missionos_form2a_task:guarded-fixture"
    assert trace["artifact_refs"]["approval_ref"] == (
        "approval:guarded-fixture"
    )
    assert trace["artifact_refs"]["dispatch_ref"] == "dispatch:guarded-fixture"
    assert trace["node_completion_is_external_execution"] is False
    assert trace["node_completion_counts_progress"] is False
    assert duplicate["hitl_status"] == "approval_blocked"
    assert duplicate["blocking_reasons"] == [
        "adk_v2_hitl_pending_interrupt_not_found"
    ]
    assert duplicate["resume_attempted"] is False
    assert duplicate["executor_invoked"] is False
    assert send_calls == ["dispatch:guarded-fixture"]
    assert session is not None
    node_paths = [event.node_info.path or "" for event in session.events]
    assert any("invoke_guarded_missionos_execution_boundary" in path for path in node_paths)
    assert any(
        "invoke_guarded_missionos_execution_boundary" in node["node_path"]
        for node in trace["nodes"]
    )


def test_gateway_enables_guarded_handler_only_with_explicit_flag(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.config.settings import reset_settings
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    service = InMemorySessionService()
    table = DispatchAuthorityTable(tmp_path / "dispatch-state.json")
    send_calls: list[str] = []

    def sender(request: Mapping[str, Any]) -> dict[str, Any]:
        send_calls.append(str(request["dispatch_ref"]))
        return {
            "dispatch_ref": request["dispatch_ref"],
            "external_sender_invoked": False,
            "ack_observed": False,
            "effect_observed": False,
            "verifier_passed": False,
            "completion_claimed": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        }

    async def handler(validated: Mapping[str, Any]) -> dict[str, Any]:
        return await guarded.execute_guarded_dispatch_once(
            validated,
            dispatch_table=table,
            fresh_preflight_provider=lambda _payload: _preflight(),
            execution_boundary=sender,
        )

    monkeypatch.setenv(hitl.MISSIONOS_ADK_V2_HITL_ENV, "1")
    monkeypatch.setenv(guarded.MISSIONOS_ADK_V2_GUARDED_EXECUTION_ENV, "1")
    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(
        gateway_server,
        "create_runtime_session_service",
        lambda *_args, **_kwargs: service,
    )
    monkeypatch.setattr(
        gateway_server,
        "build_form2a_canonical_approval_binding",
        lambda: {"binding_status": "ready", "blocking_reasons": [], **_validated_approval()},
    )
    monkeypatch.setattr(
        gateway_server,
        "validate_form2a_canonical_approval",
        _approved_validator,
    )
    monkeypatch.setattr(
        gateway_server,
        "build_form2a_guarded_execution_handler",
        lambda: handler,
    )
    reset_settings()
    reset_task_store()
    audit._audit_logger = None
    try:
        client = TestClient(gateway_server.create_missionos_gateway().app)
        paused = client.post(
            "/missionos/adk-v2/hitl/form2a-approval/start",
            json={"operator_session_id": "operator-gateway-guarded"},
        )
        assert paused.status_code == 200
        resumed = client.post(
            "/missionos/adk-v2/hitl/form2a-approval/resume",
            json={
                "operator_session_id": "operator-gateway-guarded",
                "adk_session_id": paused.json()["adk_session_id"],
                "approval_ref": "approval:guarded-fixture",
            },
        )
        assert resumed.status_code == 200
        assert resumed.json()["hitl_status"] == "guarded_execution_completed"
        assert resumed.json()["dispatch_authority_created"] is True
        assert resumed.json()["executor_invoked"] is True
        duplicate = client.post(
            "/missionos/adk-v2/hitl/form2a-approval/resume",
            json={
                "operator_session_id": "operator-gateway-guarded",
                "adk_session_id": paused.json()["adk_session_id"],
                "approval_ref": "approval:guarded-fixture",
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["resume_attempted"] is False
        assert duplicate.json()["executor_invoked"] is False
        assert send_calls == ["dispatch:guarded-fixture"]
    finally:
        reset_task_store()
        reset_settings()
        audit._audit_logger = None
