from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shlex
import sys

import pytest

from src.runtime.ros2_nav2_dispatch_bridge import (
    ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV,
    ROS2_NAV2_BRIDGE_COMMAND_ENV,
)
from src.runtime.turtlebot3_home_mission import (
    approve_turtlebot3_home_mission_plan,
    build_turtlebot3_home_mission_plan,
    run_turtlebot3_home_mission_dispatch,
)
from src.runtime.turtlebot3_route_transition_authority import (
    build_turtlebot3_route_authority_binding,
    evaluate_turtlebot3_segment_transition_authority,
    validate_turtlebot3_route_authority_binding,
)


def _plan_and_approval() -> tuple[dict, dict]:
    plan = build_turtlebot3_home_mission_plan(
        operator_instruction="TurtleBot3で障害物を避けながら屋内配送して",
    )
    proposal = plan["scenario_proposal"]
    approval = approve_turtlebot3_home_mission_plan(
        proposal=proposal,
        validation=plan["validation_result"],
    )["turtlebot3_home_mission_approval"]
    return proposal, approval


def _previous_predicate(*, proposal_id: str, segment_index: int) -> dict:
    return {
        "contract_id": f"{proposal_id}:segment_{segment_index}",
        "status": "satisfied",
        "completion_claimed": True,
        "predicate_package_evaluated": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "runtime_effect_requested": False,
        "physical_execution_invoked": False,
    }


def test_approval_binds_exact_ordered_route_and_envelope() -> None:
    proposal, approval = _plan_and_approval()
    binding = approval["route_authority"]

    assert binding["planned_segment_count"] == len(
        proposal["planned_segments"]
    )
    assert [item["segment_ref"] for item in binding["ordered_segments"]] == [
        f"segment_{index}"
        for index in range(1, len(proposal["planned_segments"]) + 1)
    ]
    assert (
        validate_turtlebot3_route_authority_binding(
            binding=binding,
            proposal_id=proposal["proposal_id"],
            operator_approval_ref=approval["operator_approval_ref"],
            approved_scope=approval["approved_scope"],
            planned_segments=proposal["planned_segments"],
            autonomy_envelope=approval["autonomy_envelope"],
        )
        == ()
    )
    assert binding["approval_created"] is False
    assert binding["dispatch_authority_created"] is False
    assert binding["runtime_effect_requested"] is False
    assert binding["physical_execution_invoked"] is False


@pytest.mark.parametrize(
    "mutation,expected_reason",
    [
        ("route", "turtlebot3_route_authority_planned_segments_sha256_mismatch"),
        ("order", "turtlebot3_route_authority_planned_segments_sha256_mismatch"),
        ("approval", "turtlebot3_route_authority_operator_approval_ref_mismatch"),
        (
            "envelope",
            "turtlebot3_route_authority_autonomy_envelope_sha256_mismatch",
        ),
    ],
)
def test_route_authority_rejects_mutated_material(
    mutation: str,
    expected_reason: str,
) -> None:
    proposal, approval = _plan_and_approval()
    planned_segments = deepcopy(proposal["planned_segments"])
    approval_ref = approval["operator_approval_ref"]
    autonomy_envelope = deepcopy(approval["autonomy_envelope"])
    if mutation == "route":
        planned_segments[1]["x_m"] += 0.2
    elif mutation == "order":
        planned_segments[0], planned_segments[1] = (
            planned_segments[1],
            planned_segments[0],
        )
    elif mutation == "approval":
        approval_ref = "approval_for_different_route"
    elif mutation == "envelope":
        autonomy_envelope["preapproved_recovery_actions"] = []

    reasons = validate_turtlebot3_route_authority_binding(
        binding=approval["route_authority"],
        proposal_id=proposal["proposal_id"],
        operator_approval_ref=approval_ref,
        approved_scope=approval["approved_scope"],
        planned_segments=planned_segments,
        autonomy_envelope=autonomy_envelope,
    )

    assert expected_reason in reasons


def test_next_segment_requires_both_predicate_and_existing_route_approval() -> None:
    proposal, approval = _plan_and_approval()
    transition = evaluate_turtlebot3_segment_transition_authority(
        binding=approval["route_authority"],
        proposal_id=proposal["proposal_id"],
        operator_approval_ref=approval["operator_approval_ref"],
        approved_scope=approval["approved_scope"],
        planned_segments=proposal["planned_segments"],
        autonomy_envelope=approval["autonomy_envelope"],
        segment_index=2,
        segment_ref="segment_2",
        goal=proposal["planned_segments"][1],
        previous_predicate_evaluation=_previous_predicate(
            proposal_id=proposal["proposal_id"],
            segment_index=1,
        ),
    )

    assert transition["transition_status"] == "authorized"
    assert transition["previous_predicate_satisfied"] is True
    assert transition["dispatch_authority_present"] is True
    assert transition["dispatch_authority_source"] == (
        "preexisting_route_approval"
    )
    assert transition["approval_created"] is False
    assert transition["dispatch_authority_created"] is False
    assert transition["runtime_effect_requested"] is False


@pytest.mark.parametrize(
    "segment_index,segment_ref,goal_index,previous,expected_reason",
    [
        (
            2,
            "segment_2",
            1,
            None,
            "turtlebot3_transition_previous_predicate_missing",
        ),
        (
            2,
            "segment_2",
            1,
            {
                "contract_id": "wrong:segment_1",
                "status": "satisfied",
                "completion_claimed": True,
                "predicate_package_evaluated": True,
            },
            "turtlebot3_transition_previous_predicate_not_satisfied",
        ),
        (
            2,
            "segment_3",
            1,
            "valid",
            "turtlebot3_transition_segment_ref_mismatch",
        ),
        (
            2,
            "segment_2",
            2,
            "valid",
            "turtlebot3_transition_goal_mismatch",
        ),
        (
            99,
            "segment_99",
            0,
            "valid",
            "turtlebot3_transition_segment_index_invalid",
        ),
    ],
)
def test_transition_rejects_missing_reordered_or_out_of_range_authority(
    segment_index: int,
    segment_ref: str,
    goal_index: int,
    previous: dict | str | None,
    expected_reason: str,
) -> None:
    proposal, approval = _plan_and_approval()
    previous_evaluation = (
        _previous_predicate(
            proposal_id=proposal["proposal_id"],
            segment_index=1,
        )
        if previous == "valid"
        else previous
    )

    transition = evaluate_turtlebot3_segment_transition_authority(
        binding=approval["route_authority"],
        proposal_id=proposal["proposal_id"],
        operator_approval_ref=approval["operator_approval_ref"],
        approved_scope=approval["approved_scope"],
        planned_segments=proposal["planned_segments"],
        autonomy_envelope=approval["autonomy_envelope"],
        segment_index=segment_index,
        segment_ref=segment_ref,
        goal=proposal["planned_segments"][goal_index],
        previous_predicate_evaluation=previous_evaluation,
    )

    assert transition["transition_status"] == "blocked"
    assert transition["dispatch_authority_present"] is False
    assert expected_reason in transition["blocking_reasons"]


def test_mutated_route_is_blocked_before_bridge_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proposal, approval = _plan_and_approval()
    mutated_proposal = deepcopy(proposal)
    mutated_proposal["planned_segments"][1]["x_m"] += 0.2
    marker = tmp_path / "bridge-called"
    bridge = tmp_path / "bridge.py"
    bridge.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('called', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(
        ROS2_NAV2_BRIDGE_COMMAND_ENV,
        shlex.join([sys.executable, str(bridge), str(marker)]),
    )

    result = run_turtlebot3_home_mission_dispatch(
        proposal=mutated_proposal,
        approval=approval,
    )

    assert marker.exists() is False
    assert result["summary"]["dispatch_request_sent"] is False
    assert result["summary"]["completion_claimed"] is False
    assert (
        "turtlebot3_route_authority_planned_segments_sha256_mismatch"
        in result["summary"]["blocking_reasons"]
    )


def test_binding_digest_cannot_be_reused_for_different_route() -> None:
    proposal, approval = _plan_and_approval()
    different_segments = deepcopy(proposal["planned_segments"])
    different_segments[-1]["label"] = "unapproved_extra_meaning"
    rebuilt = build_turtlebot3_route_authority_binding(
        proposal_id=proposal["proposal_id"],
        operator_approval_ref=approval["operator_approval_ref"],
        approved_scope=approval["approved_scope"],
        planned_segments=different_segments,
        autonomy_envelope=approval["autonomy_envelope"],
    )

    assert rebuilt["route_authority_sha256"] != approval["route_authority"][
        "route_authority_sha256"
    ]
