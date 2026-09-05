from __future__ import annotations

from typing import Any

from src.intelligence.mission_assurance_agent import MissionAssuranceAgent
from src.intelligence.missionos_mission_incident_continuation_graph import (
    MISSION_INCIDENT_CONTINUATION_NODE_SEQUENCE,
    run_missionos_mission_incident_continuation_graph,
)
from src.intelligence.missionos_mission_incident_graph import (
    run_missionos_mission_incident_graph,
)
from tests.contract.test_mission_incident_graph import _Judge, _recovery_result


def _frozen_graph() -> dict[str, Any]:
    return run_missionos_mission_incident_graph(
        telemetry_snapshot={"observed_at": "2026-09-04T00:00:00+00:00"},
        mission_context={"task_id": "task_continuation_fixture"},
        recovery_policy={"policy_ref": "fixture"},
        recovery_runner=lambda **_: _recovery_result(),
        mission_assurance_agent=MissionAssuranceAgent(_Judge("replan")),
    )


def _request() -> dict[str, Any]:
    return {
        "task_id": "task_continuation_fixture",
        "proposal_id": "runtime_recovery_proposal_fixture",
        "recovery_action": "avoid_obstacle",
        "recovery_parameters": {"target_x_m": 40.0, "target_y_m": 10.0},
        "explicit_recovery_dispatch_approval": True,
    }


def test_continuation_uses_frozen_judgment_without_rerunning_agents() -> None:
    graph = _frozen_graph()
    events: list[str] = []

    def revalidate(_state):
        events.append("revalidation")
        return {
            "validation_status": "valid",
            "proposal_id": "runtime_recovery_proposal_fixture",
        }

    def execute(_state):
        events.append("executor")
        return {
            "executor_invoked": True,
            "dispatch_authority_created": True,
            "dispatch_request_sent": True,
            "command_ack_observed": False,
            "physical_execution_invoked": False,
            "blocking_reasons": [],
        }

    def verify(_state):
        events.append("verifier")
        return {
            "verifier_status": "pending_effect_observation",
            "effect_observed": False,
            "progress_counted": False,
            "delivery_completion_claimed": False,
        }

    def observe(_state):
        events.append("observation")
        return {
            "observation_status": "observed",
            "sample_index": 26,
            "next_mission_situation_created": True,
        }

    result = run_missionos_mission_incident_continuation_graph(
        frozen_mission_incident_graph=graph,
        continuation_request=_request(),
        action_revalidation_handler=revalidate,
        executor_handler=execute,
        verifier_handler=verify,
        observation_handler=observe,
    )

    assert events == ["revalidation", "executor", "verifier", "observation"]
    assert result["continuation_runtime_status"] == "completed"
    assert result["continuation_node_sequence"] == list(
        MISSION_INCIDENT_CONTINUATION_NODE_SEQUENCE
    )
    assert result["frozen_mission_incident_graph_id"] == graph[
        "mission_incident_graph_id"
    ]
    assert result["recovery_agent_rerun"] is False
    assert result["mission_assurance_agent_rerun"] is False
    assert result["human_approval_observed"] is True
    assert result["dispatch_authority_created"] is True
    assert result["dispatch_request_sent"] is True
    assert result["executor_invoked"] is True
    assert result["command_ack_observed"] is False
    assert result["effect_observed"] is False
    assert result["verifier_status"] == "pending_effect_observation"
    assert result["next_mission_situation_created"] is True
    assert result["physical_execution_invoked"] is False
    assert result["progress_counted"] is False


def test_continuation_blocks_changed_incident_graph_before_side_effects() -> None:
    graph = _frozen_graph()
    graph["mission_assurance_response_kind"] = "hold"
    side_effect_calls: list[str] = []

    def diagnostic_revalidation(_state):
        return {}

    def must_not_run(_state):
        side_effect_calls.append("called")
        return {}

    result = run_missionos_mission_incident_continuation_graph(
        frozen_mission_incident_graph=graph,
        continuation_request=_request(),
        action_revalidation_handler=diagnostic_revalidation,
        executor_handler=must_not_run,
        verifier_handler=must_not_run,
        observation_handler=must_not_run,
    )

    assert side_effect_calls == []
    assert result["continuation_runtime_status"] == "blocked"
    assert "frozen_mission_incident_graph_hash_mismatch" in result[
        "blocking_reasons"
    ]
    assert result["dispatch_authority_created"] is False
    assert result["executor_invoked"] is False


def test_continuation_blocks_failed_revalidation_before_executor() -> None:
    calls: list[str] = []

    def revalidate(_state):
        return {
            "validation_status": "blocked",
            "proposal_id": "runtime_recovery_proposal_fixture",
            "reasons": ["fixture_revalidation_blocked"],
        }

    def must_not_run(_state):
        calls.append("called")
        return {}

    result = run_missionos_mission_incident_continuation_graph(
        frozen_mission_incident_graph=_frozen_graph(),
        continuation_request=_request(),
        action_revalidation_handler=revalidate,
        executor_handler=must_not_run,
        verifier_handler=must_not_run,
        observation_handler=must_not_run,
    )

    assert calls == []
    assert result["continuation_runtime_status"] == "blocked"
    assert result["blocking_reasons"] == ["fixture_revalidation_blocked"]
    assert result["human_approval_observed"] is True
    assert result["dispatch_authority_created"] is False
    assert result["executor_invoked"] is False


def test_continuation_records_executor_failure_without_dispatch_claim() -> None:
    events: list[str] = []

    def revalidate(_state):
        return {
            "validation_status": "valid",
            "proposal_id": "runtime_recovery_proposal_fixture",
        }

    def execute(_state):
        events.append("executor")
        return {
            "execution_status": "blocked",
            "executor_invoked": True,
            "dispatch_authority_created": True,
            "dispatch_request_sent": False,
            "command_ack_observed": False,
            "physical_execution_invoked": False,
            "blocking_reasons": ["fixture_executor_failed"],
        }

    def verify(_state):
        events.append("verifier")
        return {
            "verifier_status": "execution_boundary_returned_without_dispatch",
            "effect_observed": False,
            "progress_counted": False,
            "delivery_completion_claimed": False,
        }

    def observe(_state):
        events.append("observation")
        return {
            "observation_status": "awaiting_fresh_post_dispatch_observation",
            "next_mission_situation_created": False,
        }

    result = run_missionos_mission_incident_continuation_graph(
        frozen_mission_incident_graph=_frozen_graph(),
        continuation_request=_request(),
        action_revalidation_handler=revalidate,
        executor_handler=execute,
        verifier_handler=verify,
        observation_handler=observe,
    )

    assert events == ["executor", "verifier", "observation"]
    assert result["continuation_runtime_status"] == (
        "completed_without_dispatch"
    )
    assert result["blocking_reasons"] == ["fixture_executor_failed"]
    assert result["dispatch_authority_created"] is True
    assert result["dispatch_request_sent"] is False
    assert result["executor_invoked"] is True
    assert result["effect_observed"] is False
    assert result["next_mission_situation_created"] is False
