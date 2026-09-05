"""Exercise production incident graphs with explicitly synthetic model/robot IO."""

from datetime import UTC, datetime

from src.intelligence.mission_assurance_agent import MissionAssuranceAgent, ModelJudgment
from src.intelligence.mission_assurance_policy import PolicyStore, digest
from src.intelligence.missionos_mission_incident_continuation_graph import (
    run_missionos_mission_incident_continuation_graph,
)
from src.intelligence.missionos_mission_incident_graph import run_missionos_mission_incident_graph

FIXTURE_CONTRACT = {"objective": "deliver fixture payload", "destination": "point_b"}


class FixtureJudge:
    def judge(self, prompt):
        return ModelJudgment(
            output={
                "proposed_response_kind": "replan",
                "parameters": {},
                "rationale": "Synthetic judgment accepts the bounded candidate.",
                "expected_outcome": "Reach the fixture target while preserving payload.",
                "uncertainty": "Fixture IO; no live model or robot.",
                "operator_question": "Review changes outside the approved policy.",
            },
            invocation_evidence={"invocation_kind": "fixture", "model_id": "fixture-assurance"},
        )


def run_fixture(
    store: PolicyStore,
    sha: str,
    proposal_id: str,
    *,
    target_x: float = 2.0,
    explicit_approval: bool = False,
):
    policy = store.approved(sha)
    if policy.execution_scope != "fixture":
        raise ValueError("fixture_policy_required")
    parameters = {"target_x_m": target_x, "target_y_m": 1.0}
    recovery = {
        "schema_version": "missionos_fixture_recovery.v1",
        "runtime_status": "proposal_guardrail_passed",
        "blocking_reasons": [],
        "assessment": {
            "selected_bounded_action": "avoid_obstacle",
            "proposed_parameters": parameters,
            "action_feasibility": {
                "action": "avoid_obstacle",
                "feasibility_status": "verified_feasible",
            },
        },
        "agent_invocations": [
            {
                "agent_name": "missionos_runtime_recovery_agent",
                "agent_role": "recovery",
                "invocation_kind": "fixture",
            }
        ],
    }
    graph = run_missionos_mission_incident_graph(
        telemetry_snapshot={"observed_at": datetime.now(UTC).isoformat()},
        mission_context={
            "task_id": policy.mission_id,
            "execution_scope": "fixture",
            "mission_contract": FIXTURE_CONTRACT,
            "constraints": {"assurance_policy": store.context(sha)},
        },
        recovery_policy={},
        recovery_runner=lambda **_: recovery,
        mission_assurance_agent=MissionAssuranceAgent(FixtureJudge()),
    )
    effects = []

    def execute(state):
        effects.append(parameters)
        return {
            "executor_invoked": True,
            "dispatch_request_sent": True,
            "dispatch_authority_created": True,
            "physical_execution_invoked": False,
            "execution_scope": "fixture",
        }

    result = run_missionos_mission_incident_continuation_graph(
        frozen_mission_incident_graph=graph,
        continuation_request={
            "task_id": policy.mission_id,
            "proposal_id": proposal_id,
            "recovery_action": "avoid_obstacle",
            "recovery_parameters": parameters,
            "explicit_recovery_dispatch_approval": explicit_approval,
        },
        action_revalidation_handler=lambda _: {
            "validation_status": "valid",
            "proposal_id": proposal_id,
        },
        policy_authorization_handler=store.handler(
            sha,
            lambda: {
                "mission_id": policy.mission_id,
                "execution_scope": "fixture",
                "mission_contract_sha256": digest(FIXTURE_CONTRACT),
                "observed_at": datetime.now(UTC).isoformat(),
                "source_ref": "fixture-world",
                "predicates": {"payload_integrity": True, "return_energy_reserve": True},
            },
        ),
        executor_handler=execute,
        verifier_handler=lambda _: {
            "verifier_status": "fixture_effect_observed",
            "effect_observed": bool(effects),
            "progress_counted": False,
            "delivery_completion_claimed": False,
        },
        observation_handler=lambda _: {
            "next_mission_situation_created": bool(effects),
            "execution_scope": "fixture",
        },
    )
    return {
        "fixture_only": True,
        "live_model_invoked": False,
        "physical_execution_invoked": False,
        "mission_incident_graph": graph,
        "continuation": result,
    }
