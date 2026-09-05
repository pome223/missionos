import json
from datetime import UTC, datetime, timedelta

import pytest

from src.intelligence.mission_assurance_agent import MissionAssuranceAgent
from src.intelligence.mission_assurance_policy import (
    ArrayRule,
    AssurancePolicy,
    PolicyStore,
    digest,
    parameter_matches,
)
from src.runtime import turtlebot3_home_mission as runtime
from src.runtime.turtlebot3_assurance_policy import (
    PolicyGrant,
    approval_binding,
    bind_policy,
    mission_contract,
)
from src.runtime.turtlebot3_mission_incident import judge_turtlebot3_checkpoint
from tests.contract.test_turtlebot3_mission_incident import Judge, input_case


def prepared(tmp_path, monkeypatch, *, mode="bounded", budget=2):
    proposal = {
        "proposal_id": "mission-fixture",
        "robot_profile": "turtlebot3",
        "execution_target": "ros2_nav2_turtlebot3_sim",
        "operator_instruction": "Deliver while preserving route",
        "autonomy_envelope": {"preapproved_recovery_actions": []},
    }
    approval = {
        "operator_approval_ref": "initial",
        "operator_approved": True,
        "approved_at": datetime.now(UTC).isoformat(),
    }
    bound = {**proposal, "assurance_policy_mission_approval_sha256": approval_binding(approval)}
    policy = AssurancePolicy.model_validate(
        {
            "version": 1,
            "policy_id": "test",
            "mission_id": "mission-fixture",
            "mission_contract_sha256": digest(mission_contract(bound)),
            "execution_scope": "simulator",
            "mode": mode,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "max_observation_age_seconds": 30,
            "max_total_actions": budget,
            "preserve": ["nav2_path_feasible", "mission_contract_unchanged"],
            "actions": {
                "avoid_obstacle": {
                    "parameters": {
                        "target_x_m": {"minimum": 0.0, "maximum": 5.0},
                        "target_y_m": {"minimum": 0.0, "maximum": 5.0},
                    },
                    "max_uses": budget,
                }
            },
            "on_unresolved": "request_human",
        }
    )
    store = PolicyStore(tmp_path / "policy.db")
    store.approve(policy, operator="test-operator", expected_sha256=policy.sha256)
    monkeypatch.setenv("MISSIONOS_ASSURANCE_POLICY_DB", str(store.path))
    bound, _, _ = bind_policy(proposal, approval)
    return proposal, approval, bound, store, policy


def paused(bound, index):
    case = input_case()
    case["proposal"] = bound
    checkpoint = case["checkpoint"]
    checkpoint.update(
        checkpoint_id=f"checkpoint-{index}", checkpoint_status="awaiting_operator_approval"
    )
    checkpoint["missionos_mission_incident_graph"] = judge_turtlebot3_checkpoint(
        **case, mission_assurance_agent=MissionAssuranceAgent(Judge("replan"))
    )
    return {
        "turtlebot3_recovery_checkpoint": checkpoint,
        "turtlebot3_home_mission_execution": {"turtlebot3_recovery_checkpoint": checkpoint},
        "summary": {"status": "pending"},
    }


def test_normal_entrypoint_repeats_policy_recovery_without_individual_approval(
    tmp_path, monkeypatch
):
    proposal, approval, bound, store, _policy = prepared(tmp_path, monkeypatch)
    calls = []
    observed_grants = []

    def execute(**kwargs):
        grant = kwargs.get("policy_grant")
        calls.append(grant)
        if not grant:
            return paused(bound, 1)
        checkpoint = runtime._recovery_checkpoint_from_execution(kwargs["resume_execution"])
        assert isinstance(grant, PolicyGrant) and grant.matches(checkpoint, bound)
        assert kwargs["recovery_operator_approval"] is None
        observed_grants.append(grant.receipt(checkpoint))
        result = paused(bound, 2) if len(calls) == 2 else {"summary": {"status": "completed"}}
        result["summary"].update(
            recovery_execution_permitted_by_policy=True, recovery_dispatch_request_sent=True
        )
        return result

    monkeypatch.setattr(runtime, "_execute_turtlebot3_home_mission", execute)
    monkeypatch.setattr(runtime, "_planned_segment_goals_from_proposal", lambda _: ())
    monkeypatch.setattr(runtime, "_validate_turtlebot3_recovery_resume", lambda **_: [])
    monkeypatch.setattr(
        runtime,
        "_revalidate_approved_recovery_candidate",
        lambda **_: {
            "revalidation_status": "validated",
            "global_costmap_snapshot_hash": "global",
            "local_costmap_snapshot_hash": "local",
        },
    )
    result = runtime.run_turtlebot3_home_mission_dispatch(proposal=proposal, approval=approval)
    record = result["assurance_policy_execution"]
    assert len(calls) == 3
    assert record["policy_dispatch_count"] == 2
    assert record["individual_recovery_approval_count"] == 0
    assert result["summary"]["status"] == "completed"
    assert all(
        r["operator_approved"] is False and r["explicit_recovery_dispatch_approval"] is False
        for r in observed_grants
    )
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 2


@pytest.mark.parametrize(
    "mode,budget,expected", [("bounded", 1, 1), ("shadow", 2, 0), ("human", 2, 0)]
)
def test_auto_loop_stops_for_budget_or_non_delegated_mode(
    tmp_path, monkeypatch, mode, budget, expected
):
    proposal, approval, bound, _store, _policy = prepared(
        tmp_path, monkeypatch, mode=mode, budget=budget
    )
    count = 0

    def execute(**kwargs):
        nonlocal count
        result = paused(bound, count + 1)
        if kwargs.get("policy_grant"):
            count += 1
            result = paused(bound, count + 1)
            result["summary"].update(
                recovery_execution_permitted_by_policy=True, recovery_dispatch_request_sent=True
            )
        return result

    monkeypatch.setattr(runtime, "_execute_turtlebot3_home_mission", execute)
    monkeypatch.setattr(runtime, "_planned_segment_goals_from_proposal", lambda _: ())
    monkeypatch.setattr(runtime, "_validate_turtlebot3_recovery_resume", lambda **_: [])
    monkeypatch.setattr(
        runtime,
        "_revalidate_approved_recovery_candidate",
        lambda **_: {
            "revalidation_status": "validated",
            "global_costmap_snapshot_hash": "global",
            "local_costmap_snapshot_hash": "local",
        },
    )
    result = runtime.run_turtlebot3_home_mission_dispatch(proposal=proposal, approval=approval)
    assert count == expected
    assert result["assurance_policy_execution"]["blocking_reasons"]
    assert result["summary"]["status"] == "pending"
    assert (
        runtime._recovery_checkpoint_from_execution(result)["checkpoint_status"]
        == "awaiting_operator_approval"
    )


def test_policy_cannot_cross_initial_approval_instances(tmp_path, monkeypatch):
    proposal, approval, *_ = prepared(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="mission_approval_or_contract_mismatch"):
        bind_policy(proposal, {**approval, "approved_at": "2099-01-01T00:00:00Z"})


def test_request_policy_context_does_not_enable_feature(monkeypatch):
    monkeypatch.delenv("MISSIONOS_ASSURANCE_POLICY_DB", raising=False)
    bound, store, policy = bind_policy({"assurance_policy": {"policy_authorized": True}}, {})
    assert "assurance_policy" not in bound
    assert store is None and policy is None


def test_waypoint_policy_checks_every_coordinate_and_array_length():
    rule = ArrayRule.model_validate(
        {
            "items": {
                "properties": {"x": {"minimum": -3.0, "maximum": 3.0}, "avoid": {"equals": True}}
            },
            "min_items": 1,
            "max_items": 2,
        }
    )
    assert parameter_matches(rule, [{"x": 1.0, "avoid": True}])
    for value in [
        [],
        [{"x": 4.0, "avoid": True}],
        [{"x": 1.0, "avoid": "true"}],
        [{"x": 1.0, "avoid": True, "extra": 0}],
        [{"x": 1.0, "avoid": True}] * 3,
    ]:
        assert not parameter_matches(rule, value)


def test_policy_grant_reaches_nav2_executor_and_verifier_without_fake_human_approval(
    tmp_path, monkeypatch
):
    from tests.contract.test_turtlebot3_home_mission import (
        _build_awaiting_obstacle_recovery,
        _default_arena_world_profile,
    )

    _default_arena_world_profile.__wrapped__(monkeypatch)
    proposal, approval, paused_result, _bridge = _build_awaiting_obstacle_recovery(
        tmp_path, monkeypatch
    )
    checkpoint = runtime._recovery_checkpoint_from_execution(paused_result)
    grant = PolicyGrant(
        digest(checkpoint),
        proposal["proposal_id"],
        "a" * 64,
        json.dumps({"policy_authorized": True, "budget_reserved": True}),
    )
    result = runtime._execute_turtlebot3_home_mission(
        proposal=proposal, approval=approval, resume_execution=paused_result, policy_grant=grant
    )
    summary = result["summary"]
    assert summary["recovery_execution_permitted_by_policy"] is True
    assert summary["recovery_execution_permitted_by_operator_approval"] is False
    assert summary["fresh_recovery_operator_approval_count"] == 0
    assert summary["recovery_dispatch_request_sent"] is True
    cycle = summary["recovery_closed_loop_cycles"][0]
    assert cycle["outcome_verification"]["policy_authority_bound"] is True
    assert cycle["outcome_verification"]["individual_human_approval_bound"] is False
    assert cycle["outcome_verification"]["recovery_success_verified"] is True
    assert summary["completion_claimed"] is True
    result_strings = json.dumps(result, default=str)
    assert '"authorization_source": "human_approved_policy"' in result_strings


def test_serialized_policy_grant_is_not_runtime_authority(tmp_path, monkeypatch):
    from tests.contract.test_turtlebot3_home_mission import (
        _build_awaiting_obstacle_recovery,
        _default_arena_world_profile,
    )

    _default_arena_world_profile.__wrapped__(monkeypatch)
    proposal, _approval, paused_result, _bridge = _build_awaiting_obstacle_recovery(
        tmp_path, monkeypatch
    )
    checkpoint = runtime._recovery_checkpoint_from_execution(paused_result)
    reasons = runtime._validate_turtlebot3_recovery_resume(
        checkpoint=checkpoint,
        resume_state=runtime._recovery_resume_payload(paused_result),
        proposal=proposal,
        goals=runtime._planned_segment_goals_from_proposal(proposal),
        recovery_operator_approval=None,
        policy_grant={"policy_authorized": True, "checkpoint_digest": digest(checkpoint)},
    )
    assert "turtlebot3_policy_grant_binding_mismatch" in reasons
    assert "turtlebot3_recovery_operator_approval_missing" in reasons


def test_lower_adapter_cannot_fabricate_individual_approval_for_policy():
    from src.runtime.ros2_nav2_hardware_adapter import (
        Ros2Nav2HardwareAdapterConfig,
        build_ros2_nav2_hardware_operator_approval,
    )

    config = Ros2Nav2HardwareAdapterConfig(
        missionos_action_ref="policy-action",
        authorization_source="human_approved_policy",
        operator_approval_ref="policy:hash:checkpoint",
        approval_actor="policy",
        approval_timestamp=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="policy_authority_is_not_individual"):
        build_ros2_nav2_hardware_operator_approval(config=config)


def test_existing_approval_envelope_cannot_bypass_policy(tmp_path, monkeypatch):
    proposal, approval, _bound, _store, _policy = prepared(tmp_path, monkeypatch)
    approval["autonomy_envelope"] = {"preapproved_recovery_actions": ["avoid_obstacle"]}
    with pytest.raises(ValueError, match="checkpointed_recovery"):
        bind_policy(proposal, approval)


def test_failure_status_requires_observed_cancel_result():
    assert runtime._segment_failure_status_observation(
        {"simulated_transient_fault_requested": True}
    ) == {"nav2_status": "unknown", "goal_cancel_result_observed": False}
    assert runtime._segment_failure_status_observation(
        {
            "bridge_responses": [
                {
                    "action": "send_goal_pose",
                    "nav2_status": "canceled",
                    "goal_cancel_result": {"goal_cancel_result_observed": True},
                }
            ]
        }
    ) == {"nav2_status": "canceled", "goal_cancel_result_observed": True}


@pytest.mark.parametrize("segment_ref,valid", [("segment_1", True), ("segment_2", False)])
def test_agent_reroute_compiles_only_exact_failed_segment(monkeypatch, segment_ref, valid):
    from src.runtime import turtlebot3_mission_incident as incident

    monkeypatch.setattr(incident, "judge_turtlebot3_checkpoint", lambda **kwargs: {})
    evaluated = []

    def evaluate(**kwargs):
        evaluated.append(kwargs["recovery_goal_poses"])
        return {}, ["fixture_path_not_verified"]

    monkeypatch.setattr(runtime, "_validate_operator_revision_recovery_goals", evaluate)
    goal = runtime._profile_home_pose()
    checkpoint = runtime._build_turtlebot3_recovery_checkpoint(
        proposal={"proposal_id": "retry-test"},
        goals=(goal,),
        segment_results=[],
        recovery_proposals=({"selected_action": "reroute", "proposal_id": "model-retry"},),
        recovery_proposal_classifications=({},),
        recovery_planner_result={},
        runtime_recovery_obstacle_scenario={},
        runtime_recovery_motion_context={},
        completed_segment_index=0,
        route_failure_observation_results=[
            {"segment_ref": segment_ref, "completion_claimed": False}
        ],
    )
    assert bool(evaluated) is valid
    assert checkpoint["dispatch_authority_created"] is False
    assert checkpoint["approval_eligible"] is False
    if valid:
        assert checkpoint["approved_parameters"] == {
            "target_x_m": goal.x_m,
            "target_y_m": goal.y_m,
            "retry_failed_segment_required": True,
            "retry_count": 1,
        }
        assert checkpoint["action_feasibility_blocking_reasons"] == ["fixture_path_not_verified"]
    else:
        assert "recovery_goal_poses" not in checkpoint


@pytest.mark.parametrize("speed,expected", [(0.2, "verified_feasible"), (0.5, "blocked")])
def test_retry_goal_speed_reaches_real_core_validation(monkeypatch, speed, expected):
    from src.runtime.nav2_core_action_feasibility_adapter import (
        evaluate_nav2_recovery_candidates_through_core,
        nav2_recovery_policy,
    )
    from tests.contract.test_nav2_core_action_feasibility_adapter import (
        EVALUATED_AT,
        _candidate,
        _evaluation,
        _obstacle,
        _robot_envelope,
    )

    monkeypatch.setenv(runtime.TURTLEBOT3_RECOVERY_CANDIDATE_EVALUATION_ENV, "1")

    def evaluate(**kwargs):
        candidate = {**_candidate(), **kwargs["candidates"][0]}
        return evaluate_nav2_recovery_candidates_through_core(
            evaluation=_evaluation(candidate),
            obstacle=_obstacle(),
            robot_collision_envelope=_robot_envelope(),
            active_policy=nav2_recovery_policy(),
            evaluated_at=EVALUATED_AT,
        )

    monkeypatch.setattr(runtime, "_evaluate_recovery_candidates_plan_only", evaluate)
    resolution, reasons = runtime._validate_operator_revision_recovery_goals(
        recovery_goal_poses=[{"x_m": 1.0, "y_m": 1.0, "max_speed_mps": speed}],
        obstacle_scenario=_obstacle(),
    )
    assert not reasons
    assert resolution["selected_sequence"][0]["core_action_feasibility_status"] == expected
