from missionos_cli import cli as missionos_cli


def _stage_record(
    index: int,
    *,
    satisfied: bool = True,
) -> dict:
    stage_refs = {
        1: "px4_gazebo_delivery",
        2: "nav2_bounded_goal",
        3: "groot_libero_panda",
    }
    stage_ref = stage_refs[index]
    prerequisite_refs = {
        1: None,
        2: "px4_gazebo_delivery",
        3: "nav2_bounded_goal",
    }
    return {
        "stage_index": index,
        "stage_ref": stage_ref,
        "executor_ref": f"sim:{stage_ref}",
        "transition_authority": {
            "transition_status": "authorized",
            "dispatch_authority_present": True,
            "dispatch_authority_source": "preexisting_mission_approval",
            "prerequisite_stage_ref": prerequisite_refs[index],
            "prerequisite_predicate_satisfied": None if index == 1 else True,
        },
        "runner_invoked": True,
        "predicate_evaluation": {
            "predicate_package_id": f"missionos.fixture.stage-{index}",
            "predicate_package_version": "1",
            "evidence_readiness": "ready",
            "outcome_claim_scope": f"bounded_stage_{index}",
            "observation_content_sha256": str(index + 2) * 64,
            "evidence_origins": ["stored_artifact"],
        },
        "stage_result": {
            "child_contract_sha256": str(index) * 64,
            "predicate_status": "satisfied" if satisfied else "not_satisfied",
            "predicate_satisfied": satisfied,
            "actual_verification_basis": ("deterministic" if satisfied else "unverified"),
        },
    }


def _parent_mission_task() -> dict:
    coordinator = {
        "schema_version": "missionos_parent_mission_run_record.v1",
        "parent_mission_id": "issue164:px4-then-nav2",
        "parent_mission_sha256": "a" * 64,
        "approval_binding_sha256": "b" * 64,
        "stage_count": 2,
        "stage_records": [_stage_record(1), _stage_record(2)],
        "stages_satisfied": 2,
        "coordinator_status": "stages_satisfied",
        "blocking_reasons": [],
        "mission_completion_claimed": False,
        "mission_completion_status": "unverified",
        "identity_continuity_claimed": False,
        "shared_world_claimed": False,
        "operational_closure_created": False,
        "physical_execution_invoked": False,
    }
    return {
        "task_id": "issue164:px4-then-nav2",
        "kind": "parent_mission_execution",
        "status": "completed",
        "artifacts": {
            "missionos_parent_mission_run_record": {
                "schema_version": "missionos_issue164_live_parent_mission.v1",
                "parent_contract_frozen_at": "2026-07-30T00:00:00+00:00",
                "parent_mission_sha256": "a" * 64,
                "approval_binding_sha256": "b" * 64,
                "shared_target_descriptor_sha256": "c" * 64,
                "execution_mode": "live",
                "coordinator_record": coordinator,
                "mission_completion_claimed": False,
                "identity_continuity_claimed": False,
                "shared_world_claimed": False,
                "physical_execution_invoked": False,
            }
        },
    }


def _add_promotion_artifacts(payload: dict) -> None:
    artifacts = payload["artifacts"]
    artifacts["virtual_to_real_promotion_receipt"] = {
        "schema_version": "missionos_core_v2r_promotion_receipt.v1",
        "source_scope": "sim",
        "target_scope": "bench",
        "target_executor_profile_sha256": "d" * 64,
        "target_controller_profile_sha256": "e" * 64,
        "approval_artifact_ref": "approval:v2r:bench",
        "approved_by": "human:operator",
        "expires_at": "2026-08-01T01:00:00+00:00",
        "gaps": [
            {"gap_id": "safe_stop", "status": "resolved"},
            {"gap_id": "hardware_attestation", "status": "unresolved"},
        ],
        "rollback_condition_ids": ["rollback:disable-profile"],
        "disable_condition_ids": ["disable:on-evidence-drift"],
    }
    artifacts["virtual_to_real_promotion_validation"] = {
        "status": "unverified",
        "verification_basis": "unverified",
        "reasons": ["v2r_promotion_gap_unresolved:hardware_attestation"],
        "promotion_prerequisite_satisfied": False,
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_safety_claimed": False,
        "physical_execution_invoked": False,
    }
    artifacts["safe_stop_receipt_validation"] = {
        "status": "unverified",
        "verification_basis": "unverified",
        "reasons": ["safe_stop_effect_evidence_missing"],
        "stop_capability_evidenced": False,
        "physical_execution_invoked": False,
    }


def test_job_status_shows_parent_stage_chain_without_promoting_completion() -> None:
    rendered = "\n".join(missionos_cli._job_operator_summary(_parent_mission_task()))

    assert "Stages Satisfied: 2/2; parent mission completion remains unverified" in rendered
    assert "Frozen: id=issue164:px4-then-nav2" in rendered
    assert "contract=aaaaaaaaaaaa..." in rendered
    assert "mode=live; stages=2" in rendered
    assert "approval=bbbbbbbbbbbb..." in rendered
    assert "target=cccccccccccc..." in rendered
    assert "Stage 1/2: ref=px4_gazebo_delivery" in rendered
    assert "executor=sim:px4_gazebo_delivery; controller=unknown" in rendered
    assert "Observed: readiness=ready; content_bound=True" in rendered
    assert (
        "Completion predicate: package=missionos.fixture.stage-1@1; "
        "status=satisfied; scope=bounded_stage_1; basis=deterministic"
    ) in rendered
    assert (
        "Transition authority: status=authorized; "
        "present=True; source=preexisting_mission_approval; "
        "prerequisite=not_applicable"
    ) in rendered
    assert "Stage 2/2: ref=nav2_bounded_goal" in rendered
    assert "prerequisite=True" in rendered
    assert ("Parent outcome: stages_satisfied=2/2; claimed=False; status=unverified") in rendered
    assert (
        "Unconfirmed: claims=identity_continuity,shared_world,physical_execution; reasons=-"
    ) in rendered
    assert "Physical AI Control Tower:" in rendered
    assert "Current: stage=unknown" in rendered
    assert "Promotion: receipt=absent; status=unknown" in rendered
    assert "Safe stop: request=unknown; ack=unknown; effect=unknown" in rendered
    assert "Physical: deployment_authority=unknown; execution=False; safety=unknown" in rendered


def test_job_status_shows_vla_stage_without_promoting_ack_or_physical_execution() -> None:
    task = {
        "task_id": "task_vla",
        "kind": "vla_mission_execution",
        "status": "completed",
        "artifacts": {
            "physical_ai_mission_proposal": {
                "mission_kind": "groot_libero_panda",
                "parent_run_identity": "run:vla",
                "episode_identity": "run:vla:episode-1",
            },
            "physical_ai_mission_approval": {
                "approval_sha256": "a" * 64,
            },
            "missionos_vla_mission_run_record": {
                "run_identity": "run:vla",
                "episode_identity": "run:vla:episode-1",
                "contract_sha256": "b" * 64,
                "execution_mode": "live",
                "bounded_outcome_claimed": True,
                "controller_ack_observed": False,
                "mission_completion_claimed": False,
                "physical_execution_invoked": False,
                "predicate_evaluation": {
                    "predicate_package_id": "groot_n17_libero_panda",
                    "predicate_package_version": "2",
                    "status": "satisfied",
                    "evidence_readiness": "ready",
                    "actual_verification_basis": "deterministic",
                    "outcome_claim_scope": "exact_libero_episode",
                    "observation_content_sha256": "c" * 64,
                },
            },
        },
    }

    rendered = "\n".join(missionos_cli._job_operator_summary(task))

    assert "VLA Mission: task=task_vla; status=completed" in rendered
    assert "Bounded outcome: claimed=True; scope=exact_libero_episode" in rendered
    assert "controller_ack=False" in rendered
    assert "parent_completion=False" in rendered
    assert "physical_execution=False" in rendered


def test_job_status_labels_vla_fixture_before_any_success_wording() -> None:
    task = {
        "task_id": "task_vla_fixture",
        "kind": "vla_mission_execution",
        "status": "completed",
        "artifacts": {
            "missionos_vla_mission_run_record": {
                "execution_mode": "fixture",
                "bounded_outcome_claimed": True,
                "predicate_evaluation": {
                    "status": "satisfied",
                    "fixture_execution": True,
                },
            }
        },
    }

    rendered = "\n".join(missionos_cli._job_operator_summary(task))

    assert rendered.startswith(
        "VLA Fixture: task=task_vla_fixture; status=completed; "
        "live execution not observed"
    )
    assert "VLA Mission:" not in rendered


def test_control_tower_separates_promotion_stop_repair_and_operator_facts() -> None:
    payload = _parent_mission_task()
    _add_promotion_artifacts(payload)

    rendered = "\n".join(missionos_cli._job_operator_summary(payload))

    assert (
        "Promotion: receipt=present; status=unverified; prerequisite=False; "
        "source=sim; target=bench"
    ) in rendered
    assert "Promotion approval: source=approval:v2r:bench; approver=human:operator" in rendered
    assert "Promotion gaps: resolved=1/2; unresolved=hardware_attestation" in rendered
    assert "deployment_authority=unknown" in rendered
    assert "Safe stop: request=unknown; ack=unknown; effect=unknown; status=unverified" in rendered
    assert "Repair: requested=unknown; approved=unknown; result=unknown" in rendered
    assert "Operator: intervention=unknown; operational_closure=False; reason=unknown" in rendered
    assert "Physical: deployment_authority=unknown; execution=False; safety=False" in rendered
    assert "parent_completion=False" in rendered
    assert "v2r_promotion_gap_unresolved:hardware_attestation" in rendered


def test_control_tower_keeps_partial_promotion_fields_unknown() -> None:
    payload = _parent_mission_task()
    payload["artifacts"]["virtual_to_real_promotion_receipt"] = {
        "source_scope": "sim",
        "target_scope": "bench",
    }

    rendered = "\n".join(missionos_cli._job_operator_summary(payload))

    assert "Promotion gaps: resolved=unknown/unknown" in rendered
    assert "rollback=unknown; disable=unknown" in rendered
    assert "promotion_reasons=unknown" in rendered


def test_control_tower_does_not_hide_empty_or_malformed_receipt() -> None:
    payload = _parent_mission_task()
    payload["artifacts"]["virtual_to_real_promotion_receipt"] = {}

    rendered = "\n".join(missionos_cli._job_operator_summary(payload))

    assert "Promotion: receipt=present; status=unknown" in rendered
    assert "Promotion gaps: resolved=unknown/unknown; unresolved=unknown" in rendered

    payload["artifacts"]["virtual_to_real_promotion_receipt"] = {
        "gaps": ["not-a-gap"],
    }
    rendered = "\n".join(missionos_cli._job_operator_summary(payload))

    assert "Promotion: receipt=present; status=unknown" in rendered
    assert "Promotion gaps: resolved=unknown/unknown; unresolved=unknown" in rendered


def test_job_status_keeps_missing_stage_and_claims_visible_as_unknown() -> None:
    payload = _parent_mission_task()
    record = payload["artifacts"]["missionos_parent_mission_run_record"]
    coordinator = record["coordinator_record"]
    coordinator["stage_records"] = coordinator["stage_records"][:1]
    coordinator["stages_satisfied"] = 1
    coordinator["coordinator_status"] = "blocked"
    coordinator["blocking_reasons"] = ["second_stage_record_missing"]
    for field in (
        "identity_continuity_claimed",
        "shared_world_claimed",
        "physical_execution_invoked",
    ):
        coordinator.pop(field)
        record.pop(field)

    rendered = "\n".join(missionos_cli._job_operator_summary(payload))

    assert "Blocked: parent mission stopped after 1/2 satisfied stages" in rendered
    assert "Stage 2/2: ref=unknown; executor=unknown; record=missing" in rendered
    assert "Parent outcome: stages_satisfied=1/2" in rendered
    assert "identity_continuity:unknown" in rendered
    assert "shared_world:unknown" in rendered
    assert "physical_execution:unknown" in rendered
    assert "reasons=second_stage_record_missing" in rendered


def test_job_status_renders_three_stage_parent_with_planned_denominator() -> None:
    payload = _parent_mission_task()
    record = payload["artifacts"]["missionos_parent_mission_run_record"]
    coordinator = record["coordinator_record"]
    coordinator["stage_count"] = 3
    coordinator["stage_records"].append(_stage_record(3))
    coordinator["stages_satisfied"] = 3

    rendered = "\n".join(missionos_cli._job_operator_summary(payload))

    assert ("Stages Satisfied: 3/3; parent mission completion remains unverified") in rendered
    assert "Stage 3/3: ref=groot_libero_panda" in rendered
    assert "prerequisite=True" in rendered
    assert ("Parent outcome: stages_satisfied=3/3; claimed=False; status=unverified") in rendered


def test_job_status_labels_fixture_parent_in_the_headline() -> None:
    payload = _parent_mission_task()
    record = payload["artifacts"]["missionos_parent_mission_run_record"]
    record["execution_mode"] = "fixture"

    rendered = "\n".join(missionos_cli._job_operator_summary(payload))

    assert rendered.startswith(
        "Fixture Stages Satisfied: 2/2; live execution not observed; "
        "parent mission completion remains unverified"
    )
    assert "\nStages Satisfied:" not in rendered


def test_stages_satisfied_label_requires_all_planned_stage_records() -> None:
    payload = _parent_mission_task()
    coordinator = payload["artifacts"]["missionos_parent_mission_run_record"]["coordinator_record"]
    coordinator["stage_records"] = coordinator["stage_records"][:1]
    coordinator["stages_satisfied"] = 2

    rendered = "\n".join(missionos_cli._job_operator_summary(payload))

    assert "Stages Satisfied:" not in rendered
    assert "Parent Mission: coordinator=stages_satisfied; stages=1/2" in rendered
    assert "Stage 2/2: ref=unknown; executor=unknown; record=missing" in rendered


def test_parent_kind_without_record_does_not_invent_stage_facts() -> None:
    rendered = "\n".join(
        missionos_cli._job_operator_summary(
            {
                "task_id": "parent-without-record",
                "kind": "parent_mission_execution",
                "status": "blocked",
                "artifacts": {},
            }
        )
    )

    assert "Parent Mission:" in rendered
    assert "contract=-" in rendered
    assert "stages=unknown" in rendered
    assert "claimed=unknown; status=unknown" in rendered
    assert "identity_continuity:unknown" in rendered
    assert "physical_execution:unknown" in rendered
    assert "Physical AI Control Tower:" not in rendered


def test_unrelated_task_does_not_invent_parent_mission_section() -> None:
    rendered = "\n".join(
        missionos_cli._job_operator_summary(
            {
                "task_id": "unrelated",
                "kind": "other",
                "status": "completed",
                "artifacts": {},
            }
        )
    )

    assert "Parent Mission:" not in rendered
