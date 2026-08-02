from missionos_cli import cli as missionos_cli


def _mission_contract_task() -> dict:
    route_digest = "a" * 64
    approval_ref = "approval:route-7"
    segments = []
    transitions = []
    for index in (1, 2):
        segments.append(
            {
                "result_observed_at": f"2026-07-30T00:00:0{index}+00:00",
                "bridge_responses": [{"status": "succeeded"}],
                "adapter_evidence": {"completion_claimed": True},
                "mission_contract": {
                    "contract_id": f"proposal:segment_{index}",
                },
                "mission_contract_sha256": str(index) * 64,
                "mission_contract_predicate_evaluation": {
                    "predicate_package_id": (
                        "missionos.nav2_turtlebot3.succeeded_with_motion"
                    ),
                    "predicate_package_version": "1",
                    "observation_content_sha256": str(index + 2) * 64,
                    "evidence_origins": ["stored_artifact"],
                    "predicate_package_evaluated": True,
                    "evaluated_outcome_claim": True,
                    "actual_verification_basis": "deterministic",
                    "satisfied_alternative": "nav2_goal_succeeded",
                },
            }
        )
        transitions.append(
            {
                "transition_status": "authorized",
                "dispatch_authority_source": "preexisting_route_approval",
                "operator_approval_ref": approval_ref,
                "route_authority_sha256": route_digest,
            }
        )
    return {
        "task_id": "task_contract_status",
        "kind": "turtlebot3_home_mission_execution",
        "status": "completed",
        "artifacts": {
            "summary": {
                "completion_claimed": True,
                "completion_scope": "sim_action",
                "physical_execution_invoked": False,
                "mission_delivery_completion_claimed": False,
                "blocking_reasons": [],
                "unproven_claims": ["sim_action_completion_not_physical"],
            },
            "turtlebot3_home_mission_execution": {
                "completion_claimed": True,
                "completion_scope": "sim_action",
                "route_authority": {
                    "planned_segment_count": 2,
                    "route_authority_sha256": route_digest,
                },
                "segment_results": segments,
                "segment_transition_authority_records": transitions,
            },
        },
    }


def test_job_status_explains_mission_contract_boundary() -> None:
    rendered = "\n".join(
        missionos_cli._job_operator_summary(_mission_contract_task())
    )

    assert "Mission Contract:" in rendered
    assert "Frozen: contracts=2" in rendered
    assert "contract_ids=proposal:segment_1,proposal:segment_2" in rendered
    assert "route_segments=2" in rendered
    assert (
        "Observed: runtime_results=2/2; "
        "sources=nav2_bridge_response,adapter_evidence; "
        "content_bound=2/2"
    ) in rendered
    assert "origins=stored_artifact" in rendered
    assert "Completion predicate:" in rendered
    assert "evaluated=2/2; satisfied=2/2" in rendered
    assert "alternatives=nav2_goal_succeeded" in rendered
    assert (
        "Bounded outcome: claimed=True; scope=sim_action; "
        "basis=deterministic"
    ) in rendered
    assert (
        "Transition authority: authorized=2/2; "
        "source=preexisting_route_approval"
    ) in rendered
    assert "approval=approval:route-7" in rendered
    assert "route_authority=aaaaaaaaaaaa..." in rendered
    assert "physical_execution" in rendered
    assert "mission_delivery_completion" in rendered
    assert "reasons=-" in rendered


def test_job_status_does_not_invent_mission_contract_section() -> None:
    rendered = "\n".join(
        missionos_cli._job_operator_summary(
            {
                "task_id": "task_without_contract",
                "status": "completed",
                "artifacts": {},
            }
        )
    )

    assert "Mission Contract:" not in rendered


def test_malformed_contract_artifacts_remain_unconfirmed() -> None:
    payload = _mission_contract_task()
    execution = payload["artifacts"]["turtlebot3_home_mission_execution"]
    execution["segment_results"] = [
        {
            "mission_contract": "not-a-contract",
            "mission_contract_predicate_evaluation": {
                "predicate_package_id": "package",
                "predicate_package_version": "1",
                "predicate_package_evaluated": False,
                "evaluated_outcome_claim": False,
                "actual_verification_basis": "unverified",
                "reasons": ["observation_missing"],
            },
        }
    ]
    execution["segment_transition_authority_records"] = [
        {
            "transition_status": "blocked",
            "dispatch_authority_source": None,
            "operator_approval_ref": None,
        }
    ]
    payload["artifacts"]["summary"].update(
        {
            "completion_claimed": False,
            "completion_scope": "none",
            "blocking_reasons": ["observation_missing"],
        }
    )

    rendered = "\n".join(missionos_cli._job_operator_summary(payload))

    assert "Frozen: contracts=0" in rendered
    assert "Observed: runtime_results=0/1; sources=-; content_bound=0/1" in rendered
    assert "evaluated=0/1; satisfied=0/1" in rendered
    assert "Bounded outcome: claimed=False; scope=none; basis=unverified" in rendered
    assert "Transition authority: authorized=0/2; source=-; approval=-" in rendered
    assert "reasons=observation_missing" in rendered


def test_job_status_surfaces_missing_transition_and_claim_records() -> None:
    payload = _mission_contract_task()
    artifacts = payload["artifacts"]
    summary = artifacts["summary"]
    execution = artifacts["turtlebot3_home_mission_execution"]
    execution["segment_transition_authority_records"] = execution[
        "segment_transition_authority_records"
    ][:1]
    summary.pop("physical_execution_invoked")
    summary.pop("mission_delivery_completion_claimed")

    rendered = "\n".join(missionos_cli._job_operator_summary(payload))

    assert "Transition authority: authorized=1/2" in rendered
    assert "physical_execution:unknown" in rendered
    assert "mission_delivery_completion:unknown" in rendered


def test_job_status_uses_segment_count_for_legacy_missing_authority() -> None:
    payload = _mission_contract_task()
    execution = payload["artifacts"]["turtlebot3_home_mission_execution"]
    execution["route_authority"] = {}
    execution["segment_transition_authority_records"] = []

    rendered = "\n".join(missionos_cli._job_operator_summary(payload))

    assert "Transition authority: authorized=0/2" in rendered
