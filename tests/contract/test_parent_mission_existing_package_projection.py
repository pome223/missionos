from __future__ import annotations

from missionos_core import (
    FrozenParentMissionContract,
    QuantificationScope,
    QuantificationScopeKind,
    build_parent_mission_approval_binding,
    build_parent_mission_stage_binding,
    canonical_sha256,
    validate_frozen_parent_mission_contract,
    validate_parent_mission_approval_binding,
)
from src.runtime.nav2_turtlebot3_predicate_package import (
    build_nav2_turtlebot3_replay_contract,
    nav2_turtlebot3_predicate_package_binding,
)
from src.runtime.px4_gazebo_delivery_predicate_package import (
    build_px4_gazebo_delivery_replay_contract,
    px4_gazebo_delivery_predicate_package_binding,
)


def test_existing_px4_and_nav2_contracts_project_without_shared_child_input() -> None:
    first = build_px4_gazebo_delivery_replay_contract(
        contract_id="parent-fixture:stage_1",
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
    second = build_nav2_turtlebot3_replay_contract(
        contract_id="parent-fixture:stage_2",
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
    first_before = first.to_material()
    second_before = second.to_material()
    parent = FrozenParentMissionContract(
        parent_mission_id="parent-fixture",
        parent_mission_version="v1",
        shared_target_descriptor_sha256=canonical_sha256(
            {
                "descriptor_id": "approved-cross-executor-target:v1",
                "physical_identity_asserted": False,
                "shared_world_asserted": False,
            }
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason=(
                "This composition proves authority and evidence lineage "
                "across two separate simulator worlds."
            ),
        ),
        stages=(
            build_parent_mission_stage_binding(
                stage_index=1,
                stage_ref="stage_1",
                executor_ref="sim-executor:first",
                child_contract=first,
            ),
            build_parent_mission_stage_binding(
                stage_index=2,
                stage_ref="stage_2",
                executor_ref="sim-executor:second",
                child_contract=second,
            ),
        ),
    )
    approval = build_parent_mission_approval_binding(
        contract=parent,
        operator_approval_ref="approval:parent-fixture",
        authority_bundle_ref="catalog:parent-fixture:v1",
    )

    assert first.to_material() == first_before
    assert second.to_material() == second_before
    assert first.predicate_package == (
        px4_gazebo_delivery_predicate_package_binding()
    )
    assert second.predicate_package == (
        nav2_turtlebot3_predicate_package_binding()
    )
    assert [item.input_id for item in first.reference_inputs] == [
        "approved_drop_zone",
        "approved_payload_release_rule",
        "approved_simulator_session_rule",
    ]
    assert [item.input_id for item in second.reference_inputs] == [
        "approved_goal_pose",
        "approved_goal_frame",
    ]
    assert validate_frozen_parent_mission_contract(parent) == ()
    assert (
        validate_parent_mission_approval_binding(
            contract=parent,
            approval=approval,
        )
        == ()
    )
