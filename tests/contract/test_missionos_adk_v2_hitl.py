from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from src.gateway import missionos_knowledge_sharing as knowledge
from src.gateway import server as gateway_server
from src.intelligence import missionos_adk_v2_hitl as hitl


def _binding() -> dict[str, Any]:
    return {
        "task_id": "missionos_form2a_task:fixture",
        "binding_status": "ready",
        "blocking_reasons": [],
        "approval_ref": "approval:fixture",
        "mission_response_candidate_ref": "candidate:fixture",
        "proposal_sha256": "a" * 64,
        "bounded_action_ref": "bounded:fixture",
        "dispatch_ref": "dispatch:fixture",
        "expires_at": "2026-08-08T00:00:00+00:00",
    }


def _approved_validation(
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
        "approval_created": False,
        "dispatch_authority_created": False,
        "executor_invoked": False,
        "progress_counted": False,
    }


def test_adk_v2_hitl_pauses_and_resumes_only_with_canonical_approval_ref() -> None:
    async def scenario() -> tuple[dict[str, Any], dict[str, Any], Any]:
        service = InMemorySessionService()
        paused = await hitl.start_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id="operator-fixture",
            approval_binding=_binding(),
            approval_validator=_approved_validation,
        )
        resumed = await hitl.resume_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id="operator-fixture",
            adk_session_id=paused["adk_session_id"],
            human_response={"approval_ref": "approval:fixture"},
            approval_validator=_approved_validation,
        )
        session = await service.get_session(
            app_name=hitl.MISSIONOS_ADK_V2_HITL_APP_NAME,
            user_id="operator-fixture",
            session_id=paused["adk_session_id"],
        )
        return paused, resumed, session

    paused, resumed, session = asyncio.run(scenario())

    assert paused["hitl_status"] == "awaiting_canonical_approval"
    assert paused["approval_ref"] == "approval:fixture"
    assert paused["canonical_approval_validated"] is False
    assert paused["human_input_received"] is False
    assert paused["approval_created"] is False
    assert paused["dispatch_authority_created"] is False
    assert paused["executor_invoked"] is False
    assert resumed["hitl_status"] == "canonical_approval_validated"
    assert resumed["canonical_approval_validated"] is True
    assert resumed["approval_ref"] == "approval:fixture"
    assert resumed["resume_attempted"] is True
    assert resumed["checkpoint_restored"] is True
    assert resumed["human_input_created_authority"] is False
    assert resumed["approval_created"] is False
    assert resumed["dispatch_authority_created"] is False
    assert resumed["executor_invoked"] is False
    assert resumed["physical_execution_invoked"] is False
    assert resumed["outcome_observed"] is False
    assert resumed["progress_counted"] is False
    assert session is not None
    paths = [event.node_info.path for event in session.events if event.node_info.path]
    assert any("bind_canonical_approval_request" in path for path in paths)
    assert any("await_canonical_missionos_approval" in path for path in paths)
    assert any("validate_canonical_missionos_approval" in path for path in paths)
    assert any("finalize_canonical_approval_resume" in path for path in paths)


def test_adk_v2_hitl_yes_response_does_not_resume_or_create_authority() -> None:
    calls: list[str] = []

    def validator(
        approval_ref: str,
        expected_binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        calls.append(approval_ref)
        return _approved_validation(approval_ref, expected_binding)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], int, int]:
        service = InMemorySessionService()
        paused = await hitl.start_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id="operator-yes",
            approval_binding=_binding(),
            approval_validator=validator,
        )
        before = await service.get_session(
            app_name=hitl.MISSIONOS_ADK_V2_HITL_APP_NAME,
            user_id="operator-yes",
            session_id=paused["adk_session_id"],
        )
        blocked = await hitl.resume_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id="operator-yes",
            adk_session_id=paused["adk_session_id"],
            human_response={"response": "yes"},
            approval_validator=validator,
        )
        after = await service.get_session(
            app_name=hitl.MISSIONOS_ADK_V2_HITL_APP_NAME,
            user_id="operator-yes",
            session_id=paused["adk_session_id"],
        )
        return paused, blocked, len(before.events), len(after.events)

    paused, blocked, before_count, after_count = asyncio.run(scenario())

    assert paused["hitl_status"] == "awaiting_canonical_approval"
    assert blocked["hitl_status"] == "approval_blocked"
    assert blocked["blocking_reasons"] == ["adk_request_input_approval_ref_required"]
    assert blocked["resume_attempted"] is False
    assert blocked["checkpoint_restored"] is False
    assert blocked["canonical_approval_validated"] is False
    assert blocked["approval_created"] is False
    assert blocked["dispatch_authority_created"] is False
    assert before_count == after_count
    assert calls == []


def test_adk_v2_hitl_rejects_mismatched_or_stale_canonical_approval() -> None:
    calls: list[str] = []

    def stale_validator(
        approval_ref: str,
        _expected_binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        calls.append(approval_ref)
        return {
            "validation_status": "blocked",
            "blocking_reasons": ["canonical_approval_binding_changed:proposal_sha256"],
            "canonical_approval_validated": False,
        }

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        service = InMemorySessionService()
        first = await hitl.start_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id="operator-mismatch",
            approval_binding=_binding(),
            approval_validator=stale_validator,
        )
        mismatch = await hitl.resume_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id="operator-mismatch",
            adk_session_id=first["adk_session_id"],
            human_response={"approval_ref": "approval:other"},
            approval_validator=stale_validator,
        )
        second = await hitl.start_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id="operator-stale",
            approval_binding=_binding(),
            approval_validator=stale_validator,
        )
        stale = await hitl.resume_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id="operator-stale",
            adk_session_id=second["adk_session_id"],
            human_response={"approval_ref": "approval:fixture"},
            approval_validator=stale_validator,
        )
        return mismatch, stale

    mismatch, stale = asyncio.run(scenario())

    assert mismatch["blocking_reasons"] == ["adk_request_input_approval_ref_mismatch"]
    assert mismatch["canonical_approval_validated"] is False
    assert mismatch["resume_attempted"] is False
    assert mismatch["checkpoint_restored"] is True
    assert stale["blocking_reasons"] == [
        "canonical_approval_binding_changed:proposal_sha256"
    ]
    assert stale["canonical_approval_validated"] is False
    assert stale["dispatch_authority_created"] is False
    assert calls == ["approval:fixture"]


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), sort_keys=True), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _form2a_fixture(root: Path, *, approved: bool = True) -> dict[str, Path]:
    source_path = root / "source" / "form1.json"
    selection_path = root / "selection" / "missionos_form2a_response_selection.json"
    token_path = root / "token" / "missionos_form2a_operator_approval_token.json"
    review_path = root / "review" / "missionos_form2a_human_operator_review.json"
    _write_json(source_path, {"schema_version": "form1_fixture.v1", "value": 1})
    approval_ref = "missionos_form2a_operator_approval_token:approval_fixture"
    candidate_ref = "missionos_form2a_response_selection:selection_fixture"
    bounded_action_ref = "missionos_bounded_action:fixture"
    dispatch_ref = "missionos_pending_dispatch:fixture"
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    selection = {
        "schema_version": knowledge.FORM2A_RESPONSE_SELECTION_SCHEMA_VERSION,
        "response_selection_id": "selection_fixture",
        "selection_status": "selected",
        "input_form1_artifact_path": str(source_path),
        "input_form1_artifact_sha256": _sha256(source_path),
        "source_check": {"source_supported": True},
        "form2a_response_selected_in_artifact": True,
        "form2a_response_selected_in_runtime": False,
        "mission_response_kind": "action",
        "proposed_response_kind": "replan",
        "selected_response_kind": "operator_gated_wind_compensated_reroute",
        "bounded_action_kind": "reroute",
        "action_feasibility_status": "verified_feasible",
        "action_feasibility_ref": "action_feasibility:fixture",
        "operator_approval_required": True,
        "operator_approval_token_issued_in_artifact": True,
        "operator_approval_token_consumed_in_runtime": False,
        "approval_ref": approval_ref,
        "approval_artifact_path": str(token_path),
        "bounded_action_ref": bounded_action_ref,
        "dispatch_ref": dispatch_ref,
        "dispatch_executed_in_runtime": False,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    _write_json(selection_path, selection)
    token = {
        "schema_version": knowledge.FORM2A_OPERATOR_APPROVAL_TOKEN_SCHEMA_VERSION,
        "approval_token_id": "approval_fixture",
        "approval_token_status": "issued_unconsumed",
        "expires_at": expires_at,
        "response_selection_ref": candidate_ref,
        "response_selection_artifact_path": str(selection_path),
        "operator_approval_token_issued_in_artifact": True,
        "operator_approval_token_consumed_in_runtime": False,
        "approval_ref": approval_ref,
        "bounded_action_ref": bounded_action_ref,
        "dispatch_ref": dispatch_ref,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    _write_json(token_path, token)
    if approved:
        review = {
            "schema_version": knowledge.FORM2A_HUMAN_OPERATOR_REVIEW_SCHEMA_VERSION,
            "review_id": "review_fixture",
            "review_status": "approved",
            "human_operator_review_recorded_in_artifact": True,
            "human_operator_approval_granted_in_artifact": True,
            "human_operator_approval_granted_in_runtime": False,
            "response_selection_ref": candidate_ref,
            "response_selection_artifact_path": str(selection_path),
            "response_selection_artifact_sha256": _sha256(selection_path),
            "operator_approval_token_ref": approval_ref,
            "operator_approval_token_artifact_path": str(token_path),
            "operator_approval_token_artifact_sha256": _sha256(token_path),
            "llm_judgment_in_gate": False,
            "operator_approved": False,
            "dispatch_authority_created": False,
            "operator_approval_token_consumed_in_runtime": False,
            "dispatch_executed_in_runtime": False,
            "automatic_dispatch_executed": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        }
        _write_json(review_path, review)
    return {
        "source": source_path,
        "selection": selection_path,
        "token": token_path,
        "review": review_path,
    }


def test_form2a_canonical_approval_reloads_hash_binding_and_human_review(
    tmp_path: Path,
) -> None:
    paths = _form2a_fixture(tmp_path)
    binding = knowledge.build_form2a_canonical_approval_binding(
        artifact_root=tmp_path,
    )

    assert binding["binding_status"] == "ready"
    validation = knowledge.validate_form2a_canonical_approval(
        binding["approval_ref"],
        binding,
        artifact_root=tmp_path,
    )
    assert validation["validation_status"] == "approved"
    assert validation["canonical_approval_validated"] is True
    assert validation["approval_created"] is False
    assert validation["approval_consumed"] is False
    assert validation["dispatch_authority_created"] is False
    assert validation["executor_invoked"] is False
    assert validation["progress_counted"] is False

    changed = json.loads(paths["selection"].read_text(encoding="utf-8"))
    changed["selected_response_kind"] = "operator_gated_wind_replan_with_compensation"
    _write_json(paths["selection"], changed)
    stale = knowledge.validate_form2a_canonical_approval(
        binding["approval_ref"],
        binding,
        artifact_root=tmp_path,
    )
    assert stale["validation_status"] == "blocked"
    assert "canonical_approval_binding_changed:proposal_sha256" in stale[
        "blocking_reasons"
    ]
    assert stale["canonical_approval_validated"] is False
    assert stale["dispatch_authority_created"] is False


def test_form2a_canonical_approval_rejects_expired_token(tmp_path: Path) -> None:
    paths = _form2a_fixture(tmp_path)
    token = json.loads(paths["token"].read_text(encoding="utf-8"))
    token["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    _write_json(paths["token"], token)

    binding = knowledge.build_form2a_canonical_approval_binding(
        artifact_root=tmp_path,
    )

    assert binding["binding_status"] == "blocked"
    assert "form2a_approval_token_expired" in binding["blocking_reasons"]
    assert binding["approval_created"] is False
    assert binding["dispatch_authority_created"] is False


@pytest.fixture
def missionos_gateway(monkeypatch, tmp_path: Path):
    from src.config.settings import reset_settings
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    reset_settings()
    reset_task_store()
    audit._audit_logger = None
    gateway = gateway_server.create_missionos_gateway()
    yield gateway
    reset_task_store()
    reset_settings()
    audit._audit_logger = None


def test_gateway_hitl_route_is_opt_in_and_requires_redis(
    monkeypatch,
    missionos_gateway,
) -> None:
    client = TestClient(missionos_gateway.app)
    monkeypatch.delenv(hitl.MISSIONOS_ADK_V2_HITL_ENV, raising=False)

    disabled = client.post(
        "/missionos/adk-v2/hitl/form2a-approval/start",
        json={"operator_session_id": "operator-disabled"},
    )
    assert disabled.status_code == 409
    assert disabled.json()["hitl_status"] == "not_configured"
    assert disabled.json()["dispatch_authority_created"] is False

    monkeypatch.setenv(hitl.MISSIONOS_ADK_V2_HITL_ENV, "1")
    monkeypatch.setattr(
        gateway_server,
        "build_form2a_canonical_approval_binding",
        _binding,
    )
    no_redis = client.post(
        "/missionos/adk-v2/hitl/form2a-approval/start",
        json={"operator_session_id": "operator-no-redis"},
    )
    assert no_redis.status_code == 503
    assert no_redis.json()["blocking_reasons"] == [
        "redis_session_backend_required:REDIS_URL_not_configured"
    ]
    assert no_redis.json()["approval_created"] is False
    assert no_redis.json()["executor_invoked"] is False


def test_gateway_hitl_yes_is_not_approval_but_canonical_ref_resumes(
    monkeypatch,
    missionos_gateway,
) -> None:
    client = TestClient(missionos_gateway.app)
    service = InMemorySessionService()
    monkeypatch.setenv(hitl.MISSIONOS_ADK_V2_HITL_ENV, "1")
    monkeypatch.setattr(
        gateway_server,
        "create_runtime_session_service",
        lambda *_args, **_kwargs: service,
    )
    monkeypatch.setattr(
        gateway_server,
        "build_form2a_canonical_approval_binding",
        _binding,
    )
    monkeypatch.setattr(
        gateway_server,
        "validate_form2a_canonical_approval",
        _approved_validation,
    )

    yes_pause = client.post(
        "/missionos/adk-v2/hitl/form2a-approval/start",
        json={"operator_session_id": "operator-gateway-yes"},
    )
    assert yes_pause.status_code == 200
    yes_result = client.post(
        "/missionos/adk-v2/hitl/form2a-approval/resume",
        json={
            "operator_session_id": "operator-gateway-yes",
            "adk_session_id": yes_pause.json()["adk_session_id"],
            "approval_ref": "yes",
        },
    )
    assert yes_result.status_code == 409
    assert yes_result.json()["blocking_reasons"] == [
        "adk_request_input_approval_ref_mismatch"
    ]
    assert yes_result.json()["canonical_approval_validated"] is False
    assert yes_result.json()["dispatch_authority_created"] is False
    assert yes_result.json()["resume_attempted"] is False
    approved = client.post(
        "/missionos/adk-v2/hitl/form2a-approval/resume",
        json={
            "operator_session_id": "operator-gateway-yes",
            "adk_session_id": yes_pause.json()["adk_session_id"],
            "approval_ref": "approval:fixture",
        },
    )
    assert approved.status_code == 200
    assert approved.json()["hitl_status"] == "canonical_approval_validated"
    assert approved.json()["canonical_approval_validated"] is True
    assert approved.json()["human_input_created_authority"] is False
    assert approved.json()["approval_created"] is False
    assert approved.json()["dispatch_authority_created"] is False
    assert approved.json()["executor_invoked"] is False
    assert approved.json()["progress_counted"] is False
