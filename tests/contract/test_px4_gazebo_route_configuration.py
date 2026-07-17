from __future__ import annotations

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import configuration


def test_legacy_entrypoint_uses_packaged_argument_parser() -> None:
    assert route_entrypoint._parse_args is configuration.parse_route_args
    assert (
        route_entrypoint._payload_advisory_recovery_requested
        is configuration.payload_advisory_recovery_requested
    )
    assert (
        route_entrypoint._validate_payload_advisory_recovery_args
        is configuration.validate_payload_advisory_recovery_args
    )
    assert (
        route_entrypoint._assert_planned_route_stream_budget
        is configuration.validate_planned_route_stream_budget
    )
    assert (
        route_entrypoint.PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX
        == configuration.PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX
    )


def test_route_argument_defaults_do_not_request_recovery_loops() -> None:
    args = configuration.parse_route_args([])

    assert args.inject_target_offset_m == 0.0
    assert args.on_deviation_action == "abort_only"
    assert args.max_pose_deviation_xy_m == 2.0
    assert args.payload_advisory_recovery_action == "none"
    assert args.post_recovery_action == "none"
    assert args.mission_os_supervisor_recovery_loop is False
    assert args.mission_os_supervisor_multi_condition_loop is False
    assert args.mission_os_supervisor_obstacle_loop is False
    assert args.mission_os_supervisor_payload_loop is False
    assert args.payload_feasibility_advisory_ref == ""


def test_route_argument_parser_preserves_explicit_scenario_selection() -> None:
    args = configuration.parse_route_args(
        [
            "--inject-target-offset-m",
            "1.5",
            "--on-deviation-action",
            "rtl",
            "--max-pose-deviation-xy-m",
            "3.25",
            "--payload-advisory-recovery-action",
            "rtl",
            "--post-recovery-action",
            "land",
            "--mission-os-supervisor-recovery-loop",
            "--mission-os-supervisor-multi-condition-loop",
            "--mission-os-supervisor-obstacle-loop",
            "--mission-os-supervisor-payload-loop",
            "--payload-feasibility-advisory-ref",
            "payload-advisory:fixture",
        ]
    )

    assert args.inject_target_offset_m == 1.5
    assert args.on_deviation_action == "rtl"
    assert args.max_pose_deviation_xy_m == 3.25
    assert args.payload_advisory_recovery_action == "rtl"
    assert args.post_recovery_action == "land"
    assert args.mission_os_supervisor_recovery_loop is True
    assert args.mission_os_supervisor_multi_condition_loop is True
    assert args.mission_os_supervisor_obstacle_loop is True
    assert args.mission_os_supervisor_payload_loop is True
    assert args.payload_feasibility_advisory_ref == "payload-advisory:fixture"


def test_route_argument_parser_rejects_unlisted_actions() -> None:
    with pytest.raises(SystemExit):
        configuration.parse_route_args(["--on-deviation-action", "unbounded"])
    with pytest.raises(SystemExit):
        configuration.parse_route_args(
            ["--payload-advisory-recovery-action", "execute_anything"]
        )


def test_payload_recovery_validation_accepts_no_recovery_request() -> None:
    args = configuration.parse_route_args([])

    assert configuration.payload_advisory_recovery_requested(args) is False
    configuration.validate_payload_advisory_recovery_args(args)


def test_payload_supervisor_loop_requires_bounded_recovery_actions() -> None:
    args = configuration.parse_route_args(["--mission-os-supervisor-payload-loop"])

    with pytest.raises(RuntimeError, match="requires.*rtl.*land"):
        configuration.validate_payload_advisory_recovery_args(args)


def test_payload_recovery_requires_source_bound_advisory_reference() -> None:
    args = configuration.parse_route_args(
        [
            "--payload-advisory-recovery-action",
            "rtl",
            "--payload-feasibility-advisory-ref",
            "unrelated-artifact:fixture",
        ]
    )

    with pytest.raises(RuntimeError, match="source-bound"):
        configuration.validate_payload_advisory_recovery_args(args)


def test_payload_supervisor_loop_accepts_source_bound_rtl_then_land() -> None:
    args = configuration.parse_route_args(
        [
            "--payload-advisory-recovery-action",
            "rtl",
            "--post-recovery-action",
            "land",
            "--mission-os-supervisor-payload-loop",
            "--payload-feasibility-advisory-ref",
            configuration.PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX + ":fixture",
        ]
    )

    assert configuration.payload_advisory_recovery_requested(args) is True
    configuration.validate_payload_advisory_recovery_args(args)


def test_route_stream_budget_accepts_limit_and_rejects_excess() -> None:
    configuration.validate_planned_route_stream_budget(duration_seconds=30.0)

    with pytest.raises(RuntimeError, match="duration exceeds allowlist"):
        configuration.validate_planned_route_stream_budget(duration_seconds=30.01)
