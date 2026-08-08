from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

from google.adk.sessions import InMemorySessionService

from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.intelligence import missionos_adk_v2_guarded_execution as guarded
from src.intelligence import missionos_adk_v2_hitl as hitl
from src.intelligence import missionos_adk_v2_recovery as recovery


def _binding() -> dict[str, Any]:
    return {
        "binding_status": "ready",
        "blocking_reasons": [],
        "task_id": "missionos_form2a_task:recovery-fixture",
        "approval_ref": "approval:recovery-fixture",
        "mission_response_candidate_ref": "candidate:recovery-fixture",
        "proposal_sha256": "a" * 64,
        "bounded_action_ref": "bounded:recovery-fixture",
        "dispatch_ref": "dispatch:recovery-fixture",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }


def _validator(
    approval_ref: str,
    expected_binding: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "validation_status": "approved",
        "blocking_reasons": [],
        "approval_ref": approval_ref,
        "canonical_approval_validated": True,
        "task_id": expected_binding["task_id"],
        "mission_response_candidate_ref": expected_binding[
            "mission_response_candidate_ref"
        ],
        "proposal_sha256": expected_binding["proposal_sha256"],
        "bounded_action_ref": expected_binding["bounded_action_ref"],
        "dispatch_ref": expected_binding["dispatch_ref"],
    }


def _preflight(_payload: Mapping[str, Any]) -> dict[str, Any]:
    fresh_state = {
        **_binding(),
        "telemetry_snapshot_ref": "telemetry:recovery-fixture",
        "telemetry_snapshot_sha256": "b" * 64,
        "telemetry_fresh": True,
        "policy_ref": "policy:recovery-fixture",
        "policy_passed": True,
        "envelope_ref": "bounded:recovery-fixture",
        "envelope_passed": True,
        "backend_ref": "fixture:recovery",
        "backend_ready": True,
    }
    return {
        "preflight_status": "passed",
        "blocking_reasons": [],
        "fresh_state": fresh_state,
        "fresh_state_sha256": "c" * 64,
    }


def _failed_verifier_result() -> dict[str, Any]:
    return {
        **_binding(),
        "canonical_approval_validated": True,
        "guarded_execution": {
            "guarded_execution_status": "execution_boundary_returned",
            "verifier_status": "failed",
            "verifier_reasons": ["fixture_expected_state_not_observed"],
            "execution_receipt": {
                "dispatch_ref": "dispatch:recovery-fixture",
                "effect_observed": False,
                "verifier_status": "failed",
            },
        },
    }


def test_verifier_failure_creates_changed_approval_pending_recovery(
    tmp_path: Path,
) -> None:
    proposal = recovery.create_bounded_recovery_proposal(
        _failed_verifier_result(),
        artifact_root=tmp_path,
    )

    assert proposal["recovery_status"] == "approval_pending"
    assert proposal["task_id"] == "missionos_form2a_task:recovery-fixture"
    assert proposal["approval_request_created"] is True
    assert proposal["new_human_approval_required"] is True
    assert proposal["approval_created"] is False
    assert proposal["prior_approval_reusable"] is False
    assert proposal["bounded_action_ref"] != "bounded:recovery-fixture"
    assert proposal["dispatch_ref"] != "dispatch:recovery-fixture"
    assert proposal["dispatch_authority_created"] is False
    assert proposal["executor_invoked"] is False
    assert proposal["automatic_recovery_executed"] is False
    assert proposal["physical_execution_invoked"] is False
    assert proposal["progress_counted"] is False
    artifact = Path(proposal["recovery_proposal_artifact_path"])
    assert artifact.is_file()
    assert json.loads(artifact.read_text(encoding="utf-8"))["recovery_proposal_ref"] == (
        proposal["recovery_proposal_ref"]
    )


def test_non_failure_verifier_does_not_create_recovery_artifact(
    tmp_path: Path,
) -> None:
    result = _failed_verifier_result()
    result["guarded_execution"]["verifier_status"] = "unverified"

    proposal = recovery.create_bounded_recovery_proposal(
        result,
        artifact_root=tmp_path,
    )

    assert proposal["recovery_status"] == "not_required"
    assert proposal["approval_request_created"] is False
    assert not list(tmp_path.rglob("missionos_adk_v2_recovery_proposal.json"))


def test_graph_routes_failed_verifier_to_new_approval_without_auto_execution(
    tmp_path: Path,
) -> None:
    service = InMemorySessionService()
    table = DispatchAuthorityTable(tmp_path / "dispatch-state.json")
    sender_calls = 0

    def sender(request: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal sender_calls
        sender_calls += 1
        return {
            "dispatch_ref": request["dispatch_ref"],
            "external_sender_invoked": False,
            "ack_observed": False,
            "effect_observed": False,
            "verifier_status": "failed",
            "verifier_reasons": ["fixture_expected_state_not_observed"],
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
            fresh_preflight_provider=_preflight,
            execution_boundary=sender,
        )

    def recovery_handler(result: Mapping[str, Any]) -> dict[str, Any]:
        return recovery.create_bounded_recovery_proposal(
            result,
            artifact_root=tmp_path,
        )

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        paused = await hitl.start_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id="operator-recovery",
            approval_binding=_binding(),
            approval_validator=_validator,
        )
        resumed = await hitl.resume_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id="operator-recovery",
            adk_session_id=paused["adk_session_id"],
            human_response={"approval_ref": "approval:recovery-fixture"},
            approval_validator=_validator,
            guarded_execution_handler=execution_handler,
            recovery_proposal_handler=recovery_handler,
        )
        duplicate = await hitl.resume_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id="operator-recovery",
            adk_session_id=paused["adk_session_id"],
            human_response={"approval_ref": "approval:recovery-fixture"},
            approval_validator=_validator,
            guarded_execution_handler=execution_handler,
            recovery_proposal_handler=recovery_handler,
        )
        return resumed, duplicate

    resumed, duplicate = asyncio.run(scenario())

    assert resumed["hitl_status"] == "recovery_approval_pending"
    assert resumed["dispatch_authority_created"] is True
    assert resumed["executor_invoked"] is True
    assert resumed["verifier_passed"] is False
    assert resumed["recovery_proposal_created"] is True
    assert resumed["recovery_approval_request_created"] is True
    assert resumed["recovery_human_approval_created"] is False
    assert resumed["recovery_dispatch_authority_created"] is False
    assert resumed["recovery_executor_invoked"] is False
    assert resumed["automatic_recovery_executed"] is False
    proposal = resumed["recovery_proposal"]
    assert proposal["bounded_action_ref"] != resumed["bounded_action_ref"]
    assert proposal["dispatch_ref"] != resumed["dispatch_ref"]
    assert proposal["new_human_approval_required"] is True
    assert duplicate["resume_attempted"] is False
    assert duplicate["executor_invoked"] is False
    assert sender_calls == 1
    trace = resumed["same_task_audit_trace"]
    assert trace["task_id"] == "missionos_form2a_task:recovery-fixture"
    assert any("route_verifier_failure_to_recovery" in node["node_path"] for node in trace["nodes"])
