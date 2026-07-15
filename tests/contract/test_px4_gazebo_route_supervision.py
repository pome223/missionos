from __future__ import annotations

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import environment
from src.runtime.px4_gazebo_route import supervision


def _nominal_assessment() -> dict[str, object]:
    return supervision.wind_supervisor_assessment_inputs(
        selected_bounded_action="rtl",
        deviation_samples=[{"deviation_xy_m": 1.25}],
        wind_requested_profile={"requested": {"wind_mean_mps": 6.0, "wind_direction_deg": 90.0}},
        route_blocking_verification_summary={},
        vehicle_realism_summary={},
        battery_realism_summary={},
        telemetry_realism_summary={},
    )


def _cycle(
    *,
    cycle_index: int,
    assessment: dict[str, object],
    approval_ref: str | None,
    dispatch_ref: str | None,
    outcome_observed: bool,
) -> dict[str, object]:
    return supervision.build_wind_supervisor_cycle(
        cycle_index=cycle_index,
        observation_ref=f"observation:{cycle_index}",
        response_ref=f"response:{cycle_index}",
        selected_bounded_action="rtl" if cycle_index == 1 else "land",
        assessment_inputs=assessment,
        dispatch_ref=dispatch_ref,
        dispatch_status="accepted" if dispatch_ref else None,
        approval_ref=approval_ref,
        outcome_ref=f"outcome:{cycle_index}" if outcome_observed else None,
        outcome_observed=outcome_observed,
    )


def test_nominal_wind_assessment_has_no_implicit_authority() -> None:
    assessment = _nominal_assessment()

    assert assessment["conflicting_risks"] == []
    assert assessment["secondary_risks"] == []
    assert assessment["wind"]["wind_drift_deviation_xy_m"] == 1.25
    assert assessment["authority"] == {
        "operator_review_required": True,
        "automatic_dispatch_allowed": False,
        "bounded_action_dispatch_allowed": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    assert assessment["route"]["delivery_completion_claimed"] is False


def test_multi_condition_assessment_reports_every_active_conflict() -> None:
    assessment = supervision.wind_supervisor_assessment_inputs(
        selected_bounded_action="rtl",
        deviation_samples=[],
        wind_requested_profile={"requested": {}},
        route_blocking_verification_summary={
            "route_blocking_verification": {
                "verification_id": "route-blocking:1",
                "verification_status": "blocked",
                "observed": {"route_blocked": True},
            }
        },
        vehicle_realism_summary={
            "payload_simulator_condition_application": {"application_id": "payload-application:1"},
            "payload_feasibility_advisory": {"advisory_id": "payload:1"},
        },
        battery_realism_summary={
            "observed_battery_condition_evidence": {
                "evidence_id": "battery:1",
                "observed": {"observed_warning": 1},
            }
        },
        telemetry_realism_summary={
            "telemetry_freshness_report": {
                "report_id": "telemetry:1",
                "freshness_status": "gap_observed",
                "gap_count": 2,
            }
        },
        supervisor_scope=supervision.MULTI_CONDITION_SUPERVISOR_SCOPE,
    )

    assert assessment["conflicting_risks"] == [
        "route_blocking_active",
        "payload_feasibility_advisory_active",
        "battery_warning_active",
        "telemetry_observer_dropout_active",
    ]
    assert [risk["condition"] for risk in assessment["secondary_risks"]] == [
        "route_blocking",
        "payload_feasibility",
        "battery_warning",
        "telemetry_continuity",
    ]
    assert all(
        risk["silent_continuation_allowed"] is False for risk in assessment["secondary_risks"]
    )


def test_cycle_does_not_claim_operator_approval_without_approval_ref() -> None:
    cycle = _cycle(
        cycle_index=1,
        assessment=_nominal_assessment(),
        approval_ref=None,
        dispatch_ref=None,
        outcome_observed=False,
    )

    assert cycle["decision"]["operator_approved_dispatch_allowed"] is False
    assert cycle["action_request"]["operator_approved"] is False
    assert cycle["action_request"]["dispatch_authority_created"] is False
    assert cycle["action_receipt"]["dispatch_observed"] is False
    assert cycle["outcome_observation"]["outcome_observed"] is False


def test_cycle_preserves_approval_reference_without_minting_authority() -> None:
    cycle = _cycle(
        cycle_index=1,
        assessment=_nominal_assessment(),
        approval_ref="operator-approval:1",
        dispatch_ref="px4_gazebo_emergency_command_dispatch_result:1",
        outcome_observed=True,
    )

    assert cycle["decision"]["operator_approved_dispatch_allowed"] is True
    assert cycle["action_request"]["operator_approved"] is True
    assert cycle["action_request"]["approval_ref"] == "operator-approval:1"
    assert cycle["action_request"]["dispatch_authority_created"] is False
    assert cycle["action_receipt"]["dispatch_observed"] is True
    assert cycle["outcome_observation"]["physical_execution_invoked"] is False


def test_loop_support_requires_two_outcomes_and_no_conflicting_risks() -> None:
    nominal = _nominal_assessment()
    cycle1 = _cycle(
        cycle_index=1,
        assessment=nominal,
        approval_ref="approval:1",
        dispatch_ref="px4_gazebo_emergency_command_dispatch_result:1",
        outcome_observed=True,
    )
    cycle2 = _cycle(
        cycle_index=2,
        assessment=nominal,
        approval_ref="approval:2",
        dispatch_ref="px4_gazebo_emergency_command_dispatch_result:2",
        outcome_observed=True,
    )
    loop = supervision.build_wind_supervisor_loop(
        cycle1=cycle1,
        cycle2=cycle2,
        cycle1_outcome_observed=True,
        cycle2_outcome_observed=True,
    )

    assert loop["supervisor_loop_claim_supported"] is True
    assert loop["cycle_count"] == 2
    assert loop["observed_cycle_count"] == 2
    assert loop["authority_boundary"]["dispatch_authority_created"] is False
    assert loop["authority_boundary"]["delivery_completion_claimed"] is False

    conflict = dict(nominal)
    conflict["conflicting_risks"] = ["battery_warning_active"]
    conflicting_cycle = _cycle(
        cycle_index=2,
        assessment=conflict,
        approval_ref="approval:2",
        dispatch_ref="px4_gazebo_emergency_command_dispatch_result:2",
        outcome_observed=True,
    )
    blocked_loop = supervision.build_wind_supervisor_loop(
        cycle1=cycle1,
        cycle2=conflicting_cycle,
        cycle1_outcome_observed=True,
        cycle2_outcome_observed=True,
    )
    assert blocked_loop["supervisor_loop_claim_supported"] is False
    assert blocked_loop["cycle_count"] == 1
    assert blocked_loop["conflicting_risks"] == ["battery_warning_active"]


def test_entrypoint_wrapper_only_collects_current_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(environment.WIND_MEAN_MPS_ENV, "5.5")
    monkeypatch.setattr(
        route_entrypoint,
        "ROUTE_BLOCKING_VERIFICATION_SUMMARY",
        {
            "route_blocking_verification": {
                "verification_id": "route:live",
                "verification_status": "blocked",
                "observed": {"route_blocked": True},
            }
        },
    )
    monkeypatch.setattr(route_entrypoint, "VEHICLE_REALISM_SUMMARY", None)
    monkeypatch.setattr(route_entrypoint, "BATTERY_REALISM_SUMMARY", None)
    monkeypatch.setattr(route_entrypoint, "TELEMETRY_REALISM_SUMMARY", None)

    assessment = route_entrypoint._wind_supervisor_assessment_inputs(
        selected_bounded_action="rtl",
        deviation_samples=[{"deviation_xy_m": 2.0}],
        supervisor_scope=route_entrypoint.MULTI_CONDITION_SUPERVISOR_SCOPE,
    )
    assert assessment["wind"]["wind_speed_mps"] == 5.5
    assert assessment["obstacle"]["route_blocking_observed"] is True
    assert assessment["conflicting_risks"] == ["route_blocking_active"]


def _obstacle_assessment(*, conflicts: bool = False) -> dict[str, object]:
    return supervision.obstacle_supervisor_assessment_inputs(
        selected_bounded_action="alternate_route",
        route_blocking_verification_summary={
            "route_blocking_verification": {"observed": {"route_blocking_verified": True}}
        },
        alternate_mission_upload_summary={
            "alternate_route_execution_evidence": {
                "alternate_route_execution_observed": True,
                "alternate_waypoint_reached_observed": True,
                "observed": {"final_distance_to_alternate_waypoint_m": 0.2},
            }
        },
        battery_realism_summary=(
            {"observed_battery_condition_evidence": {"observed": {"observed_warning": 1}}}
            if conflicts
            else {}
        ),
        telemetry_realism_summary=(
            {
                "telemetry_freshness_report": {
                    "freshness_status": "gap_observed",
                    "gap_count": 1,
                }
            }
            if conflicts
            else {}
        ),
    )


def _obstacle_cycle(
    *,
    cycle_index: int,
    assessment: dict[str, object],
    approval_ref: str | None,
    outcome_observed: bool,
) -> dict[str, object]:
    dispatch_ref = (
        "alternate_route_dispatch:1"
        if cycle_index == 1
        else "px4_gazebo_emergency_command_dispatch_result:2"
    )
    return supervision.build_obstacle_supervisor_cycle(
        cycle_index=cycle_index,
        observation_ref=f"obstacle-observation:{cycle_index}",
        response_ref=f"obstacle-response:{cycle_index}",
        selected_bounded_action=("alternate_route" if cycle_index == 1 else "land"),
        assessment_inputs=assessment,
        dispatch_ref=dispatch_ref if approval_ref else None,
        dispatch_status="accepted" if approval_ref else None,
        approval_ref=approval_ref,
        outcome_ref=f"obstacle-outcome:{cycle_index}" if outcome_observed else None,
        outcome_observed=outcome_observed,
    )


def test_obstacle_assessment_reports_battery_and_telemetry_conflicts() -> None:
    assessment = _obstacle_assessment(conflicts=True)

    assert assessment["obstacle"]["route_blocked"] is True
    assert assessment["alternate_route"]["final_distance_to_alternate_waypoint_m"] == 0.2
    assert assessment["conflicting_risks"] == [
        "battery_warning_active",
        "telemetry_observer_dropout_active",
    ]
    assert assessment["route"]["delivery_completion_claimed"] is False
    assert assessment["authority"]["automatic_dispatch_allowed"] is False


def test_obstacle_cycle_requires_real_approval_reference() -> None:
    cycle = _obstacle_cycle(
        cycle_index=1,
        assessment=_obstacle_assessment(),
        approval_ref=None,
        outcome_observed=False,
    )

    assert cycle["decision"]["operator_approved_dispatch_allowed"] is False
    assert cycle["action_request"]["operator_approved"] is False
    assert cycle["action_request"]["dispatch_authority_created"] is False
    assert cycle["action_receipt"]["dispatch_observed"] is False


def test_obstacle_loop_fails_closed_on_compound_risk() -> None:
    nominal = _obstacle_assessment()
    cycle1 = _obstacle_cycle(
        cycle_index=1,
        assessment=nominal,
        approval_ref="approval:1",
        outcome_observed=True,
    )
    cycle2 = _obstacle_cycle(
        cycle_index=2,
        assessment=nominal,
        approval_ref="approval:2",
        outcome_observed=True,
    )
    loop = supervision.build_obstacle_supervisor_loop(
        cycle1=cycle1,
        cycle2=cycle2,
        cycle1_outcome_observed=True,
        cycle2_outcome_observed=True,
    )
    assert loop["supervisor_loop_claim_supported"] is True
    assert loop["cycle_count"] == 2
    assert loop["authority_boundary"]["dispatch_authority_created"] is False

    risky_cycle = _obstacle_cycle(
        cycle_index=2,
        assessment=_obstacle_assessment(conflicts=True),
        approval_ref="approval:2",
        outcome_observed=True,
    )
    risky_loop = supervision.build_obstacle_supervisor_loop(
        cycle1=cycle1,
        cycle2=risky_cycle,
        cycle1_outcome_observed=True,
        cycle2_outcome_observed=True,
    )
    assert risky_loop["supervisor_loop_claim_supported"] is False
    assert risky_loop["cycle_count"] == 1
    assert risky_loop["conflicting_risks"] == [
        "battery_warning_active",
        "telemetry_observer_dropout_active",
    ]


def test_entrypoint_obstacle_wrapper_collects_current_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        route_entrypoint,
        "ROUTE_BLOCKING_VERIFICATION_SUMMARY",
        {"route_blocking_verification": {"observed": {"route_blocking_verified": True}}},
    )
    monkeypatch.setattr(route_entrypoint, "ALTERNATE_MISSION_UPLOAD_SUMMARY", None)
    monkeypatch.setattr(route_entrypoint, "BATTERY_REALISM_SUMMARY", None)
    monkeypatch.setattr(route_entrypoint, "TELEMETRY_REALISM_SUMMARY", None)

    assessment = route_entrypoint._obstacle_supervisor_assessment_inputs(
        selected_bounded_action="alternate_route"
    )
    assert assessment["obstacle"]["route_blocked"] is True
    assert assessment["conflicting_risks"] == []


def _payload_assessment(*, conflicts: bool = False) -> dict[str, object]:
    return supervision.payload_supervisor_assessment_inputs(
        selected_bounded_action="rtl",
        payload_feasibility_advisory_ref="payload-advisory:1",
        vehicle_realism_summary={
            "vehicle_condition_profile": {"requested": {"payload_mass_kg": 2.5}}
        },
        battery_realism_summary=(
            {"observed_battery_condition_evidence": {"observed": {"observed_warning": 2}}}
            if conflicts
            else {}
        ),
        telemetry_realism_summary=(
            {
                "telemetry_freshness_report": {
                    "freshness_status": "gap_observed",
                    "gap_count": 3,
                }
            }
            if conflicts
            else {}
        ),
    )


def _payload_cycle(
    *,
    cycle_index: int,
    assessment: dict[str, object],
    approval_ref: str | None,
    outcome_observed: bool,
) -> dict[str, object]:
    return supervision.build_payload_supervisor_cycle(
        cycle_index=cycle_index,
        observation_ref=f"payload-observation:{cycle_index}",
        response_ref=f"payload-response:{cycle_index}",
        selected_bounded_action="rtl" if cycle_index == 1 else "land",
        assessment_inputs=assessment,
        dispatch_ref=(
            f"px4_gazebo_emergency_command_dispatch_result:{cycle_index}" if approval_ref else None
        ),
        dispatch_status="accepted" if approval_ref else None,
        approval_ref=approval_ref,
        outcome_ref=f"payload-outcome:{cycle_index}" if outcome_observed else None,
        outcome_observed=outcome_observed,
    )


def test_payload_assessment_keeps_advisory_separate_from_completion() -> None:
    assessment = _payload_assessment(conflicts=True)

    assert assessment["payload"] == {
        "payload_feasibility_advisory_active": True,
        "payload_feasibility_advisory_ref": "payload-advisory:1",
        "payload_margin_risk": "payload_feasibility_advisory_active",
        "payload_kg": 2.5,
    }
    assert assessment["conflicting_risks"] == [
        "battery_warning_active",
        "telemetry_observer_dropout_active",
    ]
    assert assessment["route"]["delivery_completion_claimed"] is False
    assert assessment["authority"]["physical_execution_invoked"] is False


def test_payload_cycle_requires_real_approval_reference() -> None:
    cycle = _payload_cycle(
        cycle_index=1,
        assessment=_payload_assessment(),
        approval_ref=None,
        outcome_observed=False,
    )

    assert cycle["decision"]["operator_approved_dispatch_allowed"] is False
    assert cycle["action_request"]["operator_approved"] is False
    assert cycle["action_request"]["dispatch_authority_created"] is False
    assert cycle["action_receipt"]["dispatch_observed"] is False


def test_payload_loop_fails_closed_on_compound_risk() -> None:
    nominal = _payload_assessment()
    cycle1 = _payload_cycle(
        cycle_index=1,
        assessment=nominal,
        approval_ref="approval:1",
        outcome_observed=True,
    )
    cycle2 = _payload_cycle(
        cycle_index=2,
        assessment=nominal,
        approval_ref="approval:2",
        outcome_observed=True,
    )
    loop = supervision.build_payload_supervisor_loop(
        cycle1=cycle1,
        cycle2=cycle2,
        cycle1_outcome_observed=True,
        cycle2_outcome_observed=True,
    )
    assert loop["supervisor_loop_claim_supported"] is True
    assert loop["cycle_count"] == 2
    assert loop["authority_boundary"]["delivery_completion_claimed"] is False

    risky_cycle = _payload_cycle(
        cycle_index=2,
        assessment=_payload_assessment(conflicts=True),
        approval_ref="approval:2",
        outcome_observed=True,
    )
    risky_loop = supervision.build_payload_supervisor_loop(
        cycle1=cycle1,
        cycle2=risky_cycle,
        cycle1_outcome_observed=True,
        cycle2_outcome_observed=True,
    )
    assert risky_loop["supervisor_loop_claim_supported"] is False
    assert risky_loop["cycle_count"] == 1


def test_entrypoint_payload_wrapper_collects_current_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        route_entrypoint,
        "VEHICLE_REALISM_SUMMARY",
        {"vehicle_condition_profile": {"requested": {"payload_mass_kg": 3.0}}},
    )
    monkeypatch.setattr(route_entrypoint, "BATTERY_REALISM_SUMMARY", None)
    monkeypatch.setattr(route_entrypoint, "TELEMETRY_REALISM_SUMMARY", None)

    assessment = route_entrypoint._payload_supervisor_assessment_inputs(
        selected_bounded_action="rtl"
    )
    assert assessment["payload"]["payload_kg"] == 3.0
    assert assessment["conflicting_risks"] == []
