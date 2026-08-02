from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.smoke_parent_mission_px4_nav2_libero_fixture import (
    LIBERO_STAGE_REF,
    NAV2_STAGE_REF,
    PX4_STAGE_REF,
    build_three_stage_parent,
    evaluation,
)
from src.runtime.libero_panda_predicate_package import (
    libero_panda_predicate_package_binding,
)
from src.runtime.nav2_turtlebot3_predicate_package import (
    nav2_turtlebot3_predicate_package_binding,
)
from src.runtime.parent_mission_coordinator import (
    run_parent_mission_coordinator,
)
from src.runtime.px4_gazebo_delivery_predicate_package import (
    px4_gazebo_delivery_predicate_package_binding,
)


def _setup():
    parent, approval, children = build_three_stage_parent()
    before = tuple(child.to_material() for child in children)
    return parent, approval, children, before


def _runner(
    *,
    result: dict,
    calls: list[str],
    stage_ref: str,
    marker: Path | None = None,
) -> Callable[[], dict]:
    def run() -> dict:
        calls.append(stage_ref)
        if marker is not None:
            marker.write_text("invoked", encoding="utf-8")
        return result

    return run


def test_three_concrete_packages_project_without_core_or_child_mutation() -> None:
    parent, approval, children, before = _setup()
    calls: list[str] = []

    record = run_parent_mission_coordinator(
        contract=parent,
        approval=approval,
        stage_runners={
            stage.stage_ref: _runner(
                result=evaluation(parent, stage.stage_index),
                calls=calls,
                stage_ref=stage.stage_ref,
            )
            for stage in parent.stages
        },
    )

    assert calls == [PX4_STAGE_REF, NAV2_STAGE_REF, LIBERO_STAGE_REF]
    assert record["stages_satisfied"] == 3
    assert record["coordinator_status"] == "stages_satisfied"
    assert record["blocking_reasons"] == []
    assert record["mission_completion_claimed"] is False
    assert record["mission_completion_status"] == "unverified"
    assert record["identity_continuity_claimed"] is False
    assert record["shared_world_claimed"] is False
    assert record["physical_execution_invoked"] is False
    assert tuple(child.to_material() for child in children) == before
    assert children[0].predicate_package == (
        px4_gazebo_delivery_predicate_package_binding()
    )
    assert children[1].predicate_package == (
        nav2_turtlebot3_predicate_package_binding()
    )
    assert children[2].predicate_package == (
        libero_panda_predicate_package_binding()
    )
    assert [
        item["transition_authority"]["dispatch_authority_source"]
        for item in record["stage_records"]
    ] == ["preexisting_mission_approval"] * 3


@pytest.mark.parametrize("blocked_stage_index", (1, 2))
def test_unsatisfied_earlier_stage_never_invokes_libero_runner(
    blocked_stage_index: int,
    tmp_path: Path,
) -> None:
    parent, approval, _, _ = _setup()
    calls: list[str] = []
    marker = tmp_path / "libero-runner-invoked"

    record = run_parent_mission_coordinator(
        contract=parent,
        approval=approval,
        stage_runners={
            PX4_STAGE_REF: _runner(
                result=evaluation(
                    parent,
                    1,
                    satisfied=blocked_stage_index != 1,
                ),
                calls=calls,
                stage_ref=PX4_STAGE_REF,
            ),
            NAV2_STAGE_REF: _runner(
                result=evaluation(
                    parent,
                    2,
                    satisfied=blocked_stage_index != 2,
                ),
                calls=calls,
                stage_ref=NAV2_STAGE_REF,
            ),
            LIBERO_STAGE_REF: _runner(
                result=evaluation(parent, 3),
                calls=calls,
                stage_ref=LIBERO_STAGE_REF,
                marker=marker,
            ),
        },
    )

    assert marker.exists() is False
    assert LIBERO_STAGE_REF not in calls
    assert record["coordinator_status"] == "blocked"
    assert record["mission_completion_claimed"] is False
    assert record["unreached_stage_refs"][-1] == LIBERO_STAGE_REF


def test_unsatisfied_libero_stage_blocks_parent_without_promotion() -> None:
    parent, approval, _, _ = _setup()
    calls: list[str] = []
    runners = {
        stage.stage_ref: _runner(
            result=evaluation(
                parent,
                stage.stage_index,
                satisfied=stage.stage_index != 3,
            ),
            calls=calls,
            stage_ref=stage.stage_ref,
        )
        for stage in parent.stages
    }

    record = run_parent_mission_coordinator(
        contract=parent,
        approval=approval,
        stage_runners=runners,
    )

    assert calls == [PX4_STAGE_REF, NAV2_STAGE_REF, LIBERO_STAGE_REF]
    assert record["stages_satisfied"] == 2
    assert record["coordinator_status"] == "blocked"
    assert record["mission_completion_claimed"] is False
    assert record["mission_completion_status"] == "unverified"


def test_nav2_evaluation_cannot_be_reused_as_libero_stage_result() -> None:
    parent, approval, _, _ = _setup()
    calls: list[str] = []
    nav2_evaluation = evaluation(parent, 2)

    record = run_parent_mission_coordinator(
        contract=parent,
        approval=approval,
        stage_runners={
            PX4_STAGE_REF: _runner(
                result=evaluation(parent, 1),
                calls=calls,
                stage_ref=PX4_STAGE_REF,
            ),
            NAV2_STAGE_REF: _runner(
                result=nav2_evaluation,
                calls=calls,
                stage_ref=NAV2_STAGE_REF,
            ),
            LIBERO_STAGE_REF: _runner(
                result=nav2_evaluation,
                calls=calls,
                stage_ref=LIBERO_STAGE_REF,
            ),
        },
    )

    assert calls == [PX4_STAGE_REF, NAV2_STAGE_REF, LIBERO_STAGE_REF]
    assert record["stages_satisfied"] == 2
    assert record["coordinator_status"] == "blocked"
    stage_three_reasons = record["stage_records"][2]["stage_result"][
        "reasons"
    ]
    assert "parent_mission_stage_result_contract_id_mismatch" in (
        stage_three_reasons
    )
    assert "parent_mission_stage_result_predicate_package_id_mismatch" in (
        stage_three_reasons
    )
    assert record["mission_completion_claimed"] is False
