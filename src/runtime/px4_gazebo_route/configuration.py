"""Command-line configuration for the opt-in PX4/Gazebo route runtime.

Argument parsing selects fixture/runtime scenarios only. It does not satisfy
the external-execution opt-in, create approval, or dispatch a command.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from src.runtime.px4_gazebo_route_plan import ROUTE_ON_DEVIATION_ACTIONS


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


__all__ = ["parse_route_args"]
