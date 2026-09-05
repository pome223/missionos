from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.runtime.px4_gazebo_route import live_mission_assurance as live_assurance
from src.intelligence.mission_assurance_agent import (
    MissionAssuranceAgent,
    ModelJudgment,
)
from src.runtime.px4_gazebo_route.live_mission_assurance import (
    configured_mission_assurance_context,
    evaluate_live_route_deviation,
)


class _Judge:
    def __init__(
        self,
        output: dict[str, Any] | None = None,
        error: Exception | None = None,
        events: list[str] | None = None,
    ):
        self.output = output
        self.error = error
        self.events = events

    def judge(self, _prompt: dict[str, Any]) -> ModelJudgment:
        if self.events is not None:
            self.events.append("mission_assurance_agent")
        if self.error is not None:
            raise self.error
        return ModelJudgment(
            output=dict(self.output or {}),
            invocation_evidence={
                "invocation_kind": "fixture_llm",
                "model_id": "fixture_mission_assurance",
            },
        )


def _judgment(kind: str = "return") -> dict[str, Any]:
    return {
        "proposed_response_kind": kind,
        "parameters": {},
        "rationale": "The active route has deviated under observed wind.",
        "expected_outcome": "Return within the declared simulator constraints.",
        "uncertainty": "Execution remains subject to fresh feasibility.",
        "operator_question": "Proceed with the preapproved return envelope?",
    }


def _recovery_result(action: str = "return_to_launch") -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": "missionos_runtime_recovery_agent_result.v1",
        "runtime_status": "proposal_guardrail_passed",
        "blocking_reasons": [],
        "assessment": {
            "selected_bounded_action": action,
            "proposed_parameters": {},
            "operator_approval_required": True,
            "backend_action_request_allowed": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
            "action_feasibility": {
                "action": action,
                "feasibility_status": "verified_feasible",
                "blocking_reasons": [],
                "unverified_reasons": [],
                "approval_created": False,
                "dispatch_authority_created": False,
                "physical_execution_invoked": False,
            },
        },
        "agent_output": {
            "rationale": "RTL is the bounded recovery candidate.",
            "expected_outcome": "Return toward the launch point.",
            "operator_instruction": "Review the proposal before dispatch.",
        },
        "agent_invocations": [
            {
                "agent_name": "missionos_runtime_recovery_agent",
                "agent_role": "runtime_recovery_specialist",
                "provider": "fixture",
                "invocation_kind": "fixture_llm",
                "model_id": "fixture_runtime_recovery",
                "prompt_sha256": "fixture-prompt",
                "response_sha256": "fixture-response",
                "invocation_started_at": now,
                "invocation_completed_at": now,
            }
        ],
    }


def _runtime_evidence(label: str, telemetry: dict[str, Any]) -> dict[str, Any]:
    stdout = json.dumps(telemetry, sort_keys=True, separators=(",", ":"))
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": "runtime_invocation_evidence.v1",
        "invocation_kind": "docker_exec",
        "invocation_target": f"fixture_px4_snapshot:{label}",
        "invocation_started_at": now,
        "invocation_completed_at": now,
        "invocation_exit_code": 0,
        "invocation_stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "invocation_stderr_sha256": hashlib.sha256(b"").hexdigest(),
        "invocation_stdout_preimage": stdout,
        "invocation_stderr_preimage": "",
    }


def _telemetry(sample_index: int) -> dict[str, Any]:
    telemetry = {
        "source": "fixture_same_runtime_px4",
        "observed_at": datetime.now(UTC).isoformat(),
        "sample_index": sample_index,
        "elapsed_seconds": float(sample_index),
        "telemetry": {"stale": False, "dropout": False},
        "position": {
            "local_x_m": 1.5 - (sample_index - 10) * 0.1,
            "local_y_m": 0.2,
            "altitude_above_home_m": 2.0,
            "distance_to_home_m": 1.6 - (sample_index - 10) * 0.1,
            "frame_id": "gazebo_world_xy_altitude_up",
            "source_refs": ["fixture.gz_pose"],
        },
        "battery": {
            "remaining_percent": 80.0,
            "source_refs": ["fixture.px4_battery"],
        },
        "wind": {
            "speed_mps": 2.0,
            "gust_mps": 2.0,
            "source_refs": ["fixture.gz_wind_readback"],
        },
        "terrain": {
            "terrain_clearance_m": 2.0,
            "frame_id": "gazebo_world_xy_altitude_up",
            "source_refs": ["fixture.ground_plane"],
        },
    }
    return telemetry


def _observer(*, current_index: int = 11):
    def observe(phase: str) -> dict[str, Any]:
        telemetry = _telemetry(10 if phase == "original" else current_index)
        return {
            "telemetry_snapshot": telemetry,
            "runtime_invocation_evidence": _runtime_evidence(phase, telemetry),
        }

    return observe


def _route() -> dict[str, Any]:
    return {
        "route_plan_id": "fixture_route",
        "altitude_min_m": 1.0,
        "altitude_max_m": 2.5,
        "min_battery_margin_pct": 25.0,
    }


def _deviation() -> dict[str, Any]:
    return {
        "phase": "route",
        "sample": {"x": 1.5, "y": 0.2, "z": 2.0},
        "sample_index": 10,
        "elapsed_seconds": 10.0,
        "observed_at": datetime.now(UTC).isoformat(),
        "deviation_xy_m": 1.2,
        "deviation_z_m": 0.0,
        "threshold_xy_m": 0.85,
        "threshold_z_m": 1.5,
    }


def test_live_agent_requests_fresh_operator_approval_before_dispatch(
    tmp_path: Path,
) -> None:
    result = evaluate_live_route_deviation(
        task_id="task_fixture",
        artifact_dir=tmp_path,
        route=_route(),
        deviation=_deviation(),
        available_recovery_executor_action="rtl",
        operator_preapproval_observed=True,
        telemetry_observer=_observer(),
        agent=MissionAssuranceAgent(_Judge(_judgment())),
        recovery_agent_runner=lambda **_: _recovery_result(),
    )

    assert result["guard_status"] == "awaiting_operator_approval"
    assert result["selected_recovery_action"] is None
    assert result["proposed_recovery_action"] == "rtl"
    assert result["operator_recovery_approval_request"]["requires_new_human_approval"] is True
    assert result["operator_recovery_approval_request"][
        "route_execution_approval_is_not_recovery_approval"
    ] is True
    assert result["original_action_feasibility"]["feasibility_status"] == "verified_feasible"
    assert result["current_action_feasibility"] == {}
    assert result["action_revalidation"] == {}
    assert result["blocking_reasons"] == []
    assert result["runtime_recovery_agent_invoked"] is True
    assert result["recovery_agent_invoked_before_mission_assurance"] is True
    assert result["runtime_recovery_agent_proposal"]["selected_bounded_action"] == (
        "return_to_launch"
    )
    assert result["approval_recorded"] is False
    assert result["dispatch_request_sent"] is False
    assert result["decision_sequence"] == [
        "missionos_runtime_recovery_agent",
        "source_action_feasibility",
        "mission_assurance_agent",
        "fresh_operator_recovery_approval_boundary",
    ]


def test_continue_sequence_does_not_claim_action_feasibility_or_revalidation(
    tmp_path: Path,
) -> None:
    result = evaluate_live_route_deviation(
        task_id="task_fixture",
        artifact_dir=tmp_path,
        route=_route(),
        deviation=_deviation(),
        available_recovery_executor_action="rtl",
        operator_preapproval_observed=True,
        telemetry_observer=_observer(),
        agent=MissionAssuranceAgent(_Judge(_judgment("continue"))),
        recovery_agent_runner=lambda **_: _recovery_result("continue"),
        mission_context={
            "mission_contract": {"safe_continuation_permitted": True},
            "observations": {"vehicle_state_stable": True},
            "constraints": {"continued_route_allowed": True},
            "source_refs": ["fixture:continue_context"],
        },
    )

    assert result["guard_status"] == "no_dispatch"
    assert result["selected_recovery_action"] is None
    assert result["original_action_feasibility"] == {}
    assert result["current_action_feasibility"] == {}
    assert result["action_revalidation"] == {}
    assert result["decision_sequence"] == [
        "missionos_runtime_recovery_agent",
        "mission_assurance_agent",
        "existing_operator_approval_continue_boundary",
    ]
    assert result["physical_execution_invoked"] is False
    assert (tmp_path / result["artifact_path"]).is_file()


def test_live_agent_failure_escalates_without_observing_dispatch_snapshot(
    tmp_path: Path,
) -> None:
    observed: list[str] = []
    base = _observer()

    def observe(phase: str) -> dict[str, Any]:
        observed.append(phase)
        return base(phase)

    result = evaluate_live_route_deviation(
        task_id="task_fixture",
        artifact_dir=tmp_path,
        route=_route(),
        deviation=_deviation(),
        available_recovery_executor_action="rtl",
        operator_preapproval_observed=True,
        telemetry_observer=observe,
        agent=MissionAssuranceAgent(_Judge(error=TimeoutError())),
        recovery_agent_runner=lambda **_: _recovery_result(),
    )

    assert result["guard_status"] == "blocked"
    assert result["selected_recovery_action"] is None
    assert "mission_assurance_agent_judgment_not_accepted" in result["blocking_reasons"]
    assert observed == ["original"]
    assert result["dispatch_authority_created"] is False


def test_live_revalidation_rejects_regressed_px4_cursor(tmp_path: Path) -> None:
    result = evaluate_live_route_deviation(
        task_id="task_fixture",
        artifact_dir=tmp_path,
        route=_route(),
        deviation=_deviation(),
        available_recovery_executor_action="rtl",
        operator_preapproval_observed=True,
        telemetry_observer=_observer(current_index=9),
        agent=MissionAssuranceAgent(_Judge(_judgment())),
        recovery_agent_runner=lambda **_: _recovery_result(),
        operator_recovery_approval={
            "operator_approval_performed": True,
            "approved_recovery_action": "rtl",
            "explicit_recovery_dispatch_approval": True,
        },
    )

    assert result["guard_status"] == "blocked"
    assert any(
        reason.startswith("mission_assurance_current_cursor_not_advanced")
        for reason in result["blocking_reasons"]
    )
    assert result["action_revalidation"]["revalidation_status"] == "blocked"
    assert result["dispatch_request_sent"] is False


def test_recovery_agent_proposes_before_mission_assurance_judges(tmp_path: Path) -> None:
    events: list[str] = []

    def recover(**_: Any) -> dict[str, Any]:
        events.append("missionos_runtime_recovery_agent")
        return _recovery_result()

    result = evaluate_live_route_deviation(
        task_id="task_fixture",
        artifact_dir=tmp_path,
        route=_route(),
        deviation=_deviation(),
        available_recovery_executor_action="rtl",
        operator_preapproval_observed=True,
        telemetry_observer=_observer(),
        agent=MissionAssuranceAgent(_Judge(_judgment(), events=events)),
        recovery_agent_runner=recover,
    )

    assert events == [
        "missionos_runtime_recovery_agent",
        "mission_assurance_agent",
    ]
    assert result["decision_sequence"][:3] == [
        "missionos_runtime_recovery_agent",
        "source_action_feasibility",
        "mission_assurance_agent",
    ]


def test_recovery_agent_judges_without_preselected_rtl_request(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def recover(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _recovery_result("hold")

    result = evaluate_live_route_deviation(
        task_id="task_fixture",
        artifact_dir=tmp_path,
        route=_route(),
        deviation=_deviation(),
        available_recovery_executor_action="rtl",
        operator_preapproval_observed=True,
        telemetry_observer=_observer(),
        agent=MissionAssuranceAgent(_Judge(_judgment("hold"))),
        recovery_agent_runner=recover,
        mission_context={
            "mission_contract": {"safe_continuation_permitted": True},
            "observations": {"vehicle_state_stable": True},
            "constraints": {"continued_route_allowed": True},
            "uncertainty": {"material_uncertainty_present": False},
            "source_refs": ["fixture:shared_mission_context"],
        },
    )

    recovery_context = captured["mission_context"]
    assert "operator_recovery_request" not in recovery_context
    assert recovery_context["recovery_trigger"] == {
        "trigger_kind": "route_deviation",
        "decision_scope": "vehicle_recovery_candidate_only",
        "mission_alignment_deferred_to": "mission_assurance_agent",
        "source_action_feasibility_materialized_after_proposal": True,
        "absence_of_preselected_candidate_is_not_rejection_evidence": True,
        "available_executor_action": "return_to_launch",
        "selection_instruction": (
            "independently judge whether a vehicle-level recovery candidate is "
            "needed; do not decide final mission-level alignment, and do not "
            "treat the available executor action as a requested action"
        ),
        "approval_created": False,
        "dispatch_authority_created": False,
    }
    assert recovery_context["mission_contract"] == {
        "safe_continuation_permitted": True
    }
    assert recovery_context["observations"] == {"vehicle_state_stable": True}
    assert recovery_context["constraints"] == {"continued_route_allowed": True}
    assert recovery_context["uncertainty"] == {
        "material_uncertainty_present": False
    }
    assert recovery_context["source_refs"] == [
        "fixture:shared_mission_context"
    ]
    assert result["guard_status"] == "no_dispatch"
    assert result["runtime_recovery_agent_proposal"]["selected_bounded_action"] == (
        "hold"
    )
    assert result["mission_assurance_response_kind"] == "hold"
    assert result["recovery_no_dispatch_response_accepted"] is True
    assert result["recovery_proposal_accepted"] is True
    assert result["dispatch_prevented_by_mission_assurance"] is False
    assert result["selected_recovery_action"] is None
    assert result["blocking_reasons"] == []
    assert result["dispatch_request_sent"] is False
    assert result["decision_sequence"] == [
        "missionos_runtime_recovery_agent",
        "mission_assurance_agent",
        "mission_assurance_no_dispatch_boundary",
    ]


def test_mission_assurance_suppresses_feasible_recovery_agent_action(
    tmp_path: Path,
) -> None:
    result = evaluate_live_route_deviation(
        task_id="task_fixture",
        artifact_dir=tmp_path,
        route=_route(),
        deviation=_deviation(),
        available_recovery_executor_action="rtl",
        operator_preapproval_observed=True,
        telemetry_observer=_observer(),
        agent=MissionAssuranceAgent(_Judge(_judgment("hold"))),
        recovery_agent_runner=lambda **_: _recovery_result(),
        mission_context={
            "mission_contract": {
                "required_observation_active": True,
                "return_would_abandon_required_observation": True,
            },
            "observations": {
                "deviation_trend": "stable_not_increasing",
                "immediate_safety_hazard_observed": False,
            },
            "constraints": {"position_hold_supported": True},
            "source_refs": ["fixture:mission_context"],
        },
    )

    assert result["guard_status"] == "no_dispatch"
    assert result["runtime_recovery_agent_proposal"]["selected_bounded_action"] == (
        "return_to_launch"
    )
    assert result["original_action_feasibility"]["feasibility_status"] == (
        "verified_feasible"
    )
    assert result["mission_assurance_evaluation"]["proposal"][
        "proposed_response_kind"
    ] == "hold"
    assert result["selected_recovery_action"] is None
    assert result["blocking_reasons"] == []
    assert result["response_compilation"]["compile_status"] == "no_action_required"
    assert result["dispatch_prevented_by_mission_assurance"] is True
    assert result["mission_assurance_suppression_accepted"] is True
    assert result["recovery_no_dispatch_response_accepted"] is False
    assert result["suppression_source"] == "mission_assurance_agent"
    assert result["suppression_reason"] == (
        "mission_assurance_hold_suppressed_feasible_recovery_proposal"
    )
    assert result["suppressed_recovery_action"] == "return_to_launch"
    assert result["recovery_proposal_accepted"] is False
    assert result["current_action_feasibility"] == {}
    assert result["action_revalidation"] == {}
    assert result["post_suppression_observation"]["observation_kind"] == (
        "mission_assurance_post_suppression_reobservation"
    )
    assert result["decision_sequence"] == [
        "missionos_runtime_recovery_agent",
        "source_action_feasibility",
        "mission_assurance_agent",
        "mission_assurance_no_dispatch_boundary",
        "post_suppression_reobservation",
    ]
    assert result["dispatch_request_sent"] is False
    assert result["approval_recorded"] is False
    assert result["dispatch_authority_created"] is False


def test_ab_changes_only_mission_assurance_response_for_same_feasible_rtl(
    tmp_path: Path,
) -> None:
    deviation = _deviation()
    recovery_result = _recovery_result()
    bundles = {}
    for phase, sample_index in (
        ("original", 10),
        ("current", 11),
        ("post_suppression", 11),
    ):
        telemetry = _telemetry(sample_index)
        bundles[phase] = {
            "telemetry_snapshot": telemetry,
            "runtime_invocation_evidence": _runtime_evidence(phase, telemetry),
        }

    def observer(phase: str) -> dict[str, Any]:
        return deepcopy(bundles[phase])

    common = {
        "task_id": "task_fixture_ab",
        "route": _route(),
        "deviation": deviation,
        "available_recovery_executor_action": "rtl",
        "operator_preapproval_observed": True,
        "telemetry_observer": observer,
        "recovery_agent_runner": lambda **_: deepcopy(recovery_result),
        "mission_context": {
            "mission_contract": {
                "required_observation_active": True,
                "return_would_abandon_required_observation": True,
            },
            "observations": {
                "deviation_trend": "stable_not_increasing",
                "immediate_safety_hazard_observed": False,
            },
            "constraints": {"position_hold_supported": True},
            "source_refs": ["fixture:mission_context_ab"],
        },
    }
    case_a = evaluate_live_route_deviation(
        artifact_dir=tmp_path / "case_a",
        agent=MissionAssuranceAgent(_Judge(_judgment("return"))),
        **common,
    )
    case_b = evaluate_live_route_deviation(
        artifact_dir=tmp_path / "case_b",
        agent=MissionAssuranceAgent(_Judge(_judgment("hold"))),
        **common,
    )

    assert case_a["guard_status"] == "awaiting_operator_approval"
    assert case_b["guard_status"] == "no_dispatch"
    assert case_a["mission_assurance_response_kind"] == "return"
    assert case_b["mission_assurance_response_kind"] == "hold"
    assert case_a["decision_input_bindings"] == case_b["decision_input_bindings"]
    assert case_a["mission_assurance_evaluation"]["situation"]["input_digest"] == (
        case_b["mission_assurance_evaluation"]["situation"]["input_digest"]
    )
    assert case_a["runtime_recovery_agent_proposal"]["proposal_ref"] == (
        case_b["runtime_recovery_agent_proposal"]["proposal_ref"]
    )
    assert case_a["original_action_feasibility"]["action_feasibility_sha256"] == (
        case_b["original_action_feasibility"]["action_feasibility_sha256"]
    )
    assert case_a["original_action_feasibility"]["telemetry_cursor"] == (
        case_b["original_action_feasibility"]["telemetry_cursor"]
    )
    assert case_a["recovery_policy_sha256"] == case_b["recovery_policy_sha256"]
    assert case_a["available_recovery_executor_action"] == (
        case_b["available_recovery_executor_action"]
    )
    assert case_a["route_execution_approval_observed"] == (
        case_b["route_execution_approval_observed"]
    )
    assert case_b["dispatch_prevented_by_mission_assurance"] is True
    assert case_a["action_revalidation"] == {}
    assert case_a["operator_recovery_approval_request"][
        "request_status"
    ] == "awaiting_operator_approval"
    assert case_b["action_revalidation"] == {}
    assert case_b["post_suppression_observation"]
    assert case_b["dispatch_request_sent"] is False
    assert case_b["command_ack_observed"] is False
    assert case_b["runtime_progress_observed"] is False


def test_explicit_scoped_recovery_approval_enables_fresh_revalidation(
    tmp_path: Path,
) -> None:
    result = evaluate_live_route_deviation(
        task_id="task_fixture_approved",
        artifact_dir=tmp_path,
        route=_route(),
        deviation=_deviation(),
        available_recovery_executor_action="rtl",
        operator_preapproval_observed=True,
        telemetry_observer=_observer(),
        agent=MissionAssuranceAgent(_Judge(_judgment("return"))),
        recovery_agent_runner=lambda **_: _recovery_result(),
        operator_recovery_approval={
            "operator_approval_performed": True,
            "approved_recovery_action": "rtl",
            "explicit_recovery_dispatch_approval": True,
        },
    )

    assert result["guard_status"] == "dispatch_eligible"
    assert result["selected_recovery_action"] == "rtl"
    assert result["operator_recovery_approval_observed"] is True
    assert result["action_revalidation"]["revalidation_status"] == "valid"
    assert result["decision_sequence"][-2:] == [
        "dispatch_time_action_feasibility_revalidation",
        "operator_approved_recovery_dispatch_boundary",
    ]


def test_assurance_cannot_invent_rtl_when_recovery_proposes_continue(
    tmp_path: Path,
) -> None:
    result = evaluate_live_route_deviation(
        task_id="task_fixture_reverse_disagreement",
        artifact_dir=tmp_path,
        route=_route(),
        deviation=_deviation(),
        available_recovery_executor_action="rtl",
        operator_preapproval_observed=True,
        telemetry_observer=_observer(),
        agent=MissionAssuranceAgent(_Judge(_judgment("return"))),
        recovery_agent_runner=lambda **_: _recovery_result("continue"),
    )

    assert result["runtime_recovery_agent_proposal"][
        "selected_bounded_action"
    ] == "continue"
    assert result["mission_assurance_response_kind"] == "return"
    assert result["guard_status"] == "operator_escalation"
    assert result["agent_disagreement_observed"] is True
    assert result["agent_disagreement_kind"] == (
        "assurance_action_without_recovery_action_candidate"
    )
    assert result["agent_disagreement_resolution"] == "operator_escalation"
    assert result["assurance_requested_action"] == "return_to_launch"
    assert result["recovery_no_action_response"] == "continue"
    assert result["selected_recovery_action"] is None
    assert result["operator_recovery_approval_request"] == {}
    assert result["original_action_feasibility"] == {}
    assert result["action_revalidation"] == {}
    assert result["dispatch_request_sent"] is False
    assert result["decision_sequence"] == [
        "missionos_runtime_recovery_agent",
        "mission_assurance_agent",
        "agent_disagreement_operator_escalation_boundary",
    ]


@pytest.mark.parametrize("response_kind", ["hold", "operator_escalation"])
def test_assurance_records_prevented_continuation_without_dispatch_suppression(
    tmp_path: Path,
    response_kind: str,
) -> None:
    result = evaluate_live_route_deviation(
        task_id=f"task_fixture_continue_{response_kind}",
        artifact_dir=tmp_path,
        route=_route(),
        deviation=_deviation(),
        available_recovery_executor_action="rtl",
        operator_preapproval_observed=True,
        telemetry_observer=_observer(),
        agent=MissionAssuranceAgent(_Judge(_judgment(response_kind))),
        recovery_agent_runner=lambda **_: _recovery_result("continue"),
    )

    assert result["runtime_recovery_agent_proposal"][
        "selected_bounded_action"
    ] == "continue"
    assert result["mission_assurance_response_kind"] == response_kind
    assert result["guard_status"] == "no_dispatch"
    assert result["mission_continuation_prevented_by_mission_assurance"] is True
    assert result["dispatch_prevented_by_mission_assurance"] is False
    assert result["mission_assurance_suppression_accepted"] is False
    assert result["suppression_source"] == "mission_assurance_agent"
    assert result["suppression_reason"] == (
        f"mission_assurance_{response_kind}_prevented_mission_continuation"
    )
    assert result["suppressed_recovery_action"] is None
    assert result["suppressed_recovery_response"] == "continue"
    assert result["selected_recovery_action"] is None
    assert result["original_action_feasibility"] == {}
    assert result["action_revalidation"] == {}
    assert result["post_suppression_observation"]["observation_kind"] == (
        "mission_assurance_post_suppression_reobservation"
    )
    assert result["dispatch_request_sent"] is False
    assert result["decision_sequence"] == [
        "missionos_runtime_recovery_agent",
        "mission_assurance_agent",
        "mission_assurance_continuation_suppression_boundary",
        "post_suppression_reobservation",
    ]


def test_recovery_hold_and_assurance_continue_remains_operator_disagreement(
    tmp_path: Path,
) -> None:
    result = evaluate_live_route_deviation(
        task_id="task_fixture_hold_continue",
        artifact_dir=tmp_path,
        route=_route(),
        deviation=_deviation(),
        available_recovery_executor_action="rtl",
        operator_preapproval_observed=True,
        telemetry_observer=_observer(),
        agent=MissionAssuranceAgent(_Judge(_judgment("continue"))),
        recovery_agent_runner=lambda **_: _recovery_result("hold"),
    )

    assert result["guard_status"] == "operator_escalation"
    assert result["mission_continuation_prevented_by_mission_assurance"] is False
    assert result["dispatch_prevented_by_mission_assurance"] is False
    assert result["suppression_source"] is None
    assert result["decision_sequence"] == [
        "missionos_runtime_recovery_agent",
        "mission_assurance_agent",
        "agent_disagreement_operator_escalation_boundary",
    ]


def test_rules_block_is_not_attributed_to_mission_assurance_hold(
    tmp_path: Path,
) -> None:
    base = _observer()

    def low_battery_observer(phase: str) -> dict[str, Any]:
        bundle = base(phase)
        bundle["telemetry_snapshot"]["battery"]["remaining_percent"] = 10.0
        bundle["runtime_invocation_evidence"] = _runtime_evidence(
            phase, bundle["telemetry_snapshot"]
        )
        return bundle

    result = evaluate_live_route_deviation(
        task_id="task_fixture_rules_block",
        artifact_dir=tmp_path,
        route=_route(),
        deviation=_deviation(),
        available_recovery_executor_action="rtl",
        operator_preapproval_observed=True,
        telemetry_observer=low_battery_observer,
        agent=MissionAssuranceAgent(_Judge(_judgment("hold"))),
        recovery_agent_runner=lambda **_: _recovery_result(),
    )

    assert result["guard_status"] == "blocked"
    assert result["original_action_feasibility"]["feasibility_status"] == "blocked"
    assert result["dispatch_prevented_by_mission_assurance"] is False
    assert result["mission_assurance_suppression_accepted"] is False
    assert result["suppression_source"] is None
    assert result["post_suppression_observation"] == {}


def test_mission_context_cannot_claim_execution_authority(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError,
        match="mission_assurance_context_authority_keys_forbidden",
    ):
        evaluate_live_route_deviation(
            task_id="task_fixture",
            artifact_dir=tmp_path,
            route=_route(),
            deviation=_deviation(),
            available_recovery_executor_action="rtl",
            operator_preapproval_observed=True,
            telemetry_observer=_observer(),
            agent=MissionAssuranceAgent(_Judge(_judgment("hold"))),
            recovery_agent_runner=lambda **_: _recovery_result(),
            mission_context={
                "observations": {"dispatch_authority_created": True},
            },
        )


def test_runtime_mission_context_is_loaded_from_json_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MISSIONOS_MISSION_ASSURANCE_CONTEXT_JSON",
        json.dumps(
            {
                "mission_contract": {"required_observation_active": True},
                "source_refs": ["fixture:runtime_context"],
            }
        ),
    )

    assert configured_mission_assurance_context() == {
        "mission_contract": {"required_observation_active": True},
        "source_refs": ["fixture:runtime_context"],
    }


def test_recovery_agent_failure_cannot_be_turned_into_rtl(tmp_path: Path) -> None:
    failed = _recovery_result()
    failed["runtime_status"] = "guardrail_blocked"
    failed["blocking_reasons"] = ["fixture_recovery_invalid"]
    failed["assessment"] = {}

    result = evaluate_live_route_deviation(
        task_id="task_fixture",
        artifact_dir=tmp_path,
        route=_route(),
        deviation=_deviation(),
        available_recovery_executor_action="rtl",
        operator_preapproval_observed=True,
        telemetry_observer=_observer(),
        agent=MissionAssuranceAgent(_Judge(_judgment("operator_escalation"))),
        recovery_agent_runner=lambda **_: failed,
    )

    assert result["guard_status"] == "blocked"
    assert result["selected_recovery_action"] is None
    assert "runtime_recovery_agent_proposal_not_accepted" in result["blocking_reasons"]
    assert result["dispatch_request_sent"] is False


def test_missing_graph_projection_does_not_rerun_legacy_recovery_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recovery_calls: list[dict[str, Any]] = []

    def recovery_runner(**kwargs: Any) -> dict[str, Any]:
        recovery_calls.append(kwargs)
        return _recovery_result()

    monkeypatch.setattr(
        live_assurance,
        "run_missionos_mission_incident_graph",
        lambda **_kwargs: {
            "schema_version": (
                "missionos_adk_v2_mission_incident_graph_result.v1"
            ),
            "graph_runtime_status": "proposal_guardrail_passed",
            "decision_status": "awaiting_operator_approval",
            "blocking_reasons": [],
        },
    )

    result = evaluate_live_route_deviation(
        task_id="task_missing_graph_projection",
        artifact_dir=tmp_path,
        route=_route(),
        deviation=_deviation(),
        available_recovery_executor_action="rtl",
        operator_preapproval_observed=True,
        telemetry_observer=_observer(),
        agent=MissionAssuranceAgent(_Judge(_judgment("return"))),
        recovery_agent_runner=recovery_runner,
    )

    assert recovery_calls == []
    assert result["guard_status"] == "blocked"
    assert result["selected_recovery_action"] is None
    assert "mission_incident_graph_recovery_projection_missing" in result[
        "blocking_reasons"
    ]
    assert "mission_incident_graph_situation_missing" in result[
        "blocking_reasons"
    ]
    assert result["dispatch_request_sent"] is False
