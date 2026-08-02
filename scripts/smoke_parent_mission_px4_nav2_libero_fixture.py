"""GPU-free three-stage parent-mission runtime-contract smoke.

This smoke projects the existing PX4/Gazebo, Nav2/TurtleBot3, and
GR00T/LIBERO Panda child contracts into the generic parent coordinator. It
does not start a simulator, invoke a model, or claim parent-mission
completion.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from missionos_core import (
    FrozenParentMissionContract,
    QuantificationScope,
    QuantificationScopeKind,
    build_parent_mission_approval_binding,
    build_parent_mission_stage_binding,
    canonical_sha256,
)
from src.runtime.libero_panda_predicate_package import (
    GROOT_CHECKPOINT_REPOSITORY,
    GROOT_CHECKPOINT_REVISION,
    ISAAC_GROOT_REVISION,
    LIBERO_PANDA_EMBODIMENT_TAG,
    LIBERO_PANDA_ENVIRONMENT,
    LIBERO_POLICY_ACTION_HORIZON,
    LIBERO_REVISION,
    LIBEROPandaRunnerConfiguration,
    build_libero_panda_replay_contract,
)
from src.runtime.nav2_turtlebot3_predicate_package import (
    build_nav2_turtlebot3_replay_contract,
)
from src.runtime.parent_mission_coordinator import (
    run_parent_mission_coordinator,
)
from src.runtime.px4_gazebo_delivery_predicate_package import (
    build_px4_gazebo_delivery_replay_contract,
)


PX4_STAGE_REF = "px4_gazebo_delivery"
NAV2_STAGE_REF = "nav2_bounded_goal"
LIBERO_STAGE_REF = "groot_libero_panda"


def build_three_stage_parent():
    px4 = build_px4_gazebo_delivery_replay_contract(
        contract_id="issue171-smoke:stage-1",
        contract_version="v1",
        approved_drop_zone={
            "frame": "map",
            "center": {"x_m": 4.0, "y_m": 2.0},
            "radius_m": 1.0,
        },
        approved_payload_release_rule={
            "requires_landing_before_release": True,
        },
        approved_same_session_rule={
            "mission_upload_and_release_share_session": True,
        },
        maximum_observation_age_seconds=30.0,
    )
    nav2 = build_nav2_turtlebot3_replay_contract(
        contract_id="issue171-smoke:stage-2",
        contract_version="v1",
        approved_goal_pose={
            "frame_id": "map",
            "x_m": 1.0,
            "y_m": 0.0,
            "yaw_rad": 0.0,
        },
        approved_goal_frame={"frame_id": "map"},
        maximum_observation_age_seconds=30.0,
    )
    runner_configuration = LIBEROPandaRunnerConfiguration(
        model_repository=GROOT_CHECKPOINT_REPOSITORY,
        checkpoint_revision=GROOT_CHECKPOINT_REVISION,
        isaac_groot_revision=ISAAC_GROOT_REVISION,
        libero_revision=LIBERO_REVISION,
        embodiment_tag=LIBERO_PANDA_EMBODIMENT_TAG,
        environment=LIBERO_PANDA_ENVIRONMENT,
        maximum_episode_steps=720,
        policy_action_horizon=LIBERO_POLICY_ACTION_HORIZON,
        n_action_steps=8,
        n_envs=1,
        controller_configuration_sha256=canonical_sha256(
            {"controller": "OSC_POSE", "action_dim": 7}
        ),
        action_dim=7,
        terminate_on_success=True,
    )
    libero = build_libero_panda_replay_contract(
        contract_id="issue171-smoke:stage-3",
        contract_version="v1",
        runner_configuration=runner_configuration,
        run_identity="issue171-smoke:libero-run",
        episode_identity="issue171-smoke:libero-run:episode-1",
        maximum_observation_age_seconds=30.0,
    )
    children = (px4, nav2, libero)
    parent = FrozenParentMissionContract(
        parent_mission_id="issue171-smoke:px4-nav2-libero",
        parent_mission_version="v1",
        shared_target_descriptor_sha256=canonical_sha256(
            {
                "descriptor_id": "issue171-three-stage-smoke:v1",
                "simulation_world_count": 3,
                "physical_identity_asserted": False,
                "shared_world_asserted": False,
            }
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason=(
                "This smoke quantifies no shared object. It exercises "
                "authority and result lineage across three simulator-scoped "
                "child contracts."
            ),
        ),
        stages=tuple(
            build_parent_mission_stage_binding(
                stage_index=index,
                stage_ref=stage_ref,
                executor_ref=executor_ref,
                child_contract=child,
            )
            for index, (stage_ref, executor_ref, child) in enumerate(
                (
                    (PX4_STAGE_REF, "sim:px4-gazebo", px4),
                    (NAV2_STAGE_REF, "sim:nav2-turtlebot3", nav2),
                    (LIBERO_STAGE_REF, "vla:groot-libero-panda", libero),
                ),
                start=1,
            )
        ),
    )
    approval = build_parent_mission_approval_binding(
        contract=parent,
        operator_approval_ref="approval:issue171-smoke:three-stage",
        authority_bundle_ref="catalog:issue171-smoke:three-stage:v1",
    )
    return parent, approval, children


def evaluation(
    parent: FrozenParentMissionContract,
    stage_index: int,
    *,
    satisfied: bool = True,
) -> dict:
    stage = parent.stages[stage_index - 1]
    return {
        "contract_id": stage.child_contract_id,
        "contract_sha256": stage.child_contract_sha256,
        "predicate_package_id": stage.predicate_package.package_id,
        "predicate_package_version": stage.predicate_package.package_version,
        "predicate_package_sha256": stage.predicate_package.content_sha256,
        "status": "satisfied" if satisfied else "not_satisfied",
        "evaluated_outcome_claim": satisfied,
        "actual_verification_basis": "deterministic",
        "predicate_package_evaluated": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "runtime_effect_requested": False,
        "operational_closure_created": False,
        "physical_execution_invoked": False,
    }


def runner(
    *,
    result: dict,
    calls: list[str],
    stage_ref: str,
) -> Callable[[], dict]:
    def run() -> dict:
        calls.append(stage_ref)
        return result

    return run


def run_scenario(
    parent: FrozenParentMissionContract,
    approval,
    *,
    unsatisfied_stage: int | None = None,
    reuse_stage_two_for_three: bool = False,
) -> tuple[dict, list[str]]:
    calls: list[str] = []
    stage_two = evaluation(
        parent,
        2,
        satisfied=unsatisfied_stage != 2,
    )
    stage_three = (
        stage_two
        if reuse_stage_two_for_three
        else evaluation(
            parent,
            3,
            satisfied=unsatisfied_stage != 3,
        )
    )
    record = run_parent_mission_coordinator(
        contract=parent,
        approval=approval,
        stage_runners={
            PX4_STAGE_REF: runner(
                result=evaluation(
                    parent,
                    1,
                    satisfied=unsatisfied_stage != 1,
                ),
                calls=calls,
                stage_ref=PX4_STAGE_REF,
            ),
            NAV2_STAGE_REF: runner(
                result=stage_two,
                calls=calls,
                stage_ref=NAV2_STAGE_REF,
            ),
            LIBERO_STAGE_REF: runner(
                result=stage_three,
                calls=calls,
                stage_ref=LIBERO_STAGE_REF,
            ),
        },
    )
    return record, calls


def main() -> None:
    parent, approval, children = build_three_stage_parent()
    child_material = tuple(child.to_material() for child in children)

    positive, positive_calls = run_scenario(parent, approval)
    stage_two_blocked, stage_two_calls = run_scenario(
        parent,
        approval,
        unsatisfied_stage=2,
    )
    stage_three_blocked, stage_three_calls = run_scenario(
        parent,
        approval,
        unsatisfied_stage=3,
    )
    reused, reused_calls = run_scenario(
        parent,
        approval,
        reuse_stage_two_for_three=True,
    )

    report = {
        "schema_version": "missionos_issue171_three_stage_fixture_smoke.v1",
        "child_package_count": 3,
        "existing_child_material_unchanged": (
            tuple(child.to_material() for child in children) == child_material
        ),
        "all_stages_satisfied": {
            "calls": positive_calls,
            "coordinator_status": positive["coordinator_status"],
            "stages_satisfied": positive["stages_satisfied"],
            "mission_completion_claimed": positive[
                "mission_completion_claimed"
            ],
            "mission_completion_status": positive[
                "mission_completion_status"
            ],
        },
        "stage_two_not_satisfied": {
            "calls": stage_two_calls,
            "libero_runner_invoked": LIBERO_STAGE_REF in stage_two_calls,
            "coordinator_status": stage_two_blocked["coordinator_status"],
        },
        "stage_three_not_satisfied": {
            "calls": stage_three_calls,
            "coordinator_status": stage_three_blocked[
                "coordinator_status"
            ],
            "stages_satisfied": stage_three_blocked["stages_satisfied"],
        },
        "stage_two_result_reused_for_stage_three": {
            "calls": reused_calls,
            "coordinator_status": reused["coordinator_status"],
            "stage_three_reasons": reused["stage_records"][2]["stage_result"][
                "reasons"
            ],
        },
        "shared_world_claimed": positive["shared_world_claimed"],
        "identity_continuity_claimed": positive[
            "identity_continuity_claimed"
        ],
        "physical_execution_invoked": positive[
            "physical_execution_invoked"
        ],
        "gpu_invoked": False,
        "simulator_invoked": False,
        "model_invoked": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))

    if (
        positive_calls
        != [PX4_STAGE_REF, NAV2_STAGE_REF, LIBERO_STAGE_REF]
        or positive["coordinator_status"] != "stages_satisfied"
        or positive["stages_satisfied"] != 3
        or positive["mission_completion_claimed"] is not False
        or positive["mission_completion_status"] != "unverified"
        or stage_two_calls != [PX4_STAGE_REF, NAV2_STAGE_REF]
        or stage_two_blocked["coordinator_status"] != "blocked"
        or stage_three_calls
        != [PX4_STAGE_REF, NAV2_STAGE_REF, LIBERO_STAGE_REF]
        or stage_three_blocked["coordinator_status"] != "blocked"
        or stage_three_blocked["stages_satisfied"] != 2
        or reused["coordinator_status"] != "blocked"
        or (
            "parent_mission_stage_result_contract_id_mismatch"
            not in reused["stage_records"][2]["stage_result"]["reasons"]
        )
        or (
            "parent_mission_stage_result_predicate_package_id_mismatch"
            not in reused["stage_records"][2]["stage_result"]["reasons"]
        )
        or tuple(child.to_material() for child in children) != child_material
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
