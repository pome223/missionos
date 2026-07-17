"""Command-line configuration for the opt-in PX4/Gazebo route runtime.

Argument parsing selects fixture/runtime scenarios only. It does not satisfy
the external-execution opt-in, create approval, or dispatch a command.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from src.runtime.px4_gazebo_route_dispatcher import (
    ROUTE_SETPOINT_STREAM_MAX_DURATION_SECONDS,
    ROUTE_SETPOINT_STREAM_MAX_FRAMES,
)
from src.runtime.px4_gazebo_route_plan import ROUTE_ON_DEVIATION_ACTIONS


PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX = (
    "payload_feasibility_advisory:mission_designer_payload_mass"
)


def parse_route_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--inject-target-offset-m",
        type=float,
        default=0.0,
        help="Offset the sent route target to exercise pose-deviation aborts.",
    )
    parser.add_argument(
        "--on-deviation-action",
        choices=ROUTE_ON_DEVIATION_ACTIONS,
        default="abort_only",
        help="Action to take after route pose-deviation detection.",
    )
    parser.add_argument(
        "--max-pose-deviation-xy-m",
        type=float,
        default=2.0,
        help=(
            "Horizontal route-deviation threshold for the planned route. "
            "Used by scoped runtime audits such as wind-drift recovery."
        ),
    )
    parser.add_argument(
        "--payload-advisory-recovery-action",
        choices=("none", "land", "rtl", "hold"),
        default="none",
        help=(
            "Operator-approved bounded recovery action to dispatch after the "
            "payload Form 2b advisory is consumed. This is scoped to payload "
            "advisory recovery audits."
        ),
    )
    parser.add_argument(
        "--post-recovery-action",
        choices=("none", "land"),
        default="none",
        help=(
            "Operator-approved bounded action to dispatch after an initial "
            "route-deviation recovery has been observed. This is scoped to "
            "strict Form 3 audits that need a second action-outcome observation."
        ),
    )
    parser.add_argument(
        "--mission-os-supervisor-recovery-loop",
        action="store_true",
        help=(
            "Route wind-drift RTL -> LAND recovery through a scoped Mission OS "
            "supervisor decision loop artifact. This is SITL-only and keeps "
            "hardware/physical authority false."
        ),
    )
    parser.add_argument(
        "--mission-os-supervisor-multi-condition-loop",
        action="store_true",
        help=(
            "Route wind-drift RTL -> LAND recovery through a multi-condition "
            "Mission OS supervisor runtime scope that checks wind, obstacle, "
            "payload, battery, telemetry, recovery state, and authority "
            "dimensions. This is SITL-only, not a full Gateway runtime, and "
            "keeps hardware/physical authority false."
        ),
    )
    parser.add_argument(
        "--mission-os-supervisor-obstacle-loop",
        action="store_true",
        help=(
            "Route obstacle alternate-route -> LAND recovery through a scoped "
            "Mission OS supervisor decision loop artifact. This is SITL-only "
            "and keeps hardware/physical authority false."
        ),
    )
    parser.add_argument(
        "--mission-os-supervisor-payload-loop",
        action="store_true",
        help=(
            "Route payload advisory RTL -> LAND recovery through a scoped "
            "Mission OS supervisor decision loop artifact. This is SITL-only "
            "and keeps hardware/physical authority false."
        ),
    )
    parser.add_argument(
        "--payload-feasibility-advisory-ref",
        default="",
        help=(
            "Source payload_feasibility_advisory.v1 ref consumed by the "
            "payload recovery action."
        ),
    )
    return parser.parse_args(argv)


def payload_advisory_recovery_requested(args: argparse.Namespace) -> bool:
    return args.payload_advisory_recovery_action != "none"


def validate_payload_advisory_recovery_args(args: argparse.Namespace) -> None:
    if not payload_advisory_recovery_requested(args):
        if args.mission_os_supervisor_payload_loop:
            raise RuntimeError(
                "payload supervisor loop requires "
                "--payload-advisory-recovery-action rtl and "
                "--post-recovery-action land"
            )
        return
    if not args.payload_feasibility_advisory_ref.startswith(
        PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX
    ):
        raise RuntimeError(
            "payload advisory recovery requires a source-bound "
            "--payload-feasibility-advisory-ref starting with "
            f"{PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX}"
        )
    if args.mission_os_supervisor_payload_loop and (
        args.payload_advisory_recovery_action != "rtl"
        or args.post_recovery_action != "land"
    ):
        raise RuntimeError(
            "payload supervisor Form 3 requires "
            "--payload-advisory-recovery-action rtl and "
            "--post-recovery-action land"
        )


def validate_planned_route_stream_budget(*, duration_seconds: float) -> None:
    max_planned_frames = 40 + int(duration_seconds / 0.05) + 2
    if duration_seconds > ROUTE_SETPOINT_STREAM_MAX_DURATION_SECONDS:
        raise RuntimeError("planned route stream duration exceeds allowlist")
    if max_planned_frames > ROUTE_SETPOINT_STREAM_MAX_FRAMES:
        raise RuntimeError("planned route stream frames exceed allowlist")


__all__ = [
    "PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX",
    "parse_route_args",
    "payload_advisory_recovery_requested",
    "validate_payload_advisory_recovery_args",
    "validate_planned_route_stream_budget",
]
