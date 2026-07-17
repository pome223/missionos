"""Bounded route-blocking observation and branch selection.

One observation cycle is supplied by the caller because it owns the live
Gazebo/PX4 state and the evidence projection order.  This module only repeats
that cycle, inspects the resulting evidence, and reports which already-defined
terminal branch is requested.  It does not create approval, dispatch authority,
completion evidence, or physical-execution claims.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import time
from typing import Any


ObservationCycle = Callable[[int], Mapping[str, Mapping[str, Any]]]
WaitObservation = Callable[[int], None]


@dataclass(frozen=True)
class RouteBlockingDecision:
    alternate_landing_requested: bool
    rth_behavior_requested: bool
    decision_summaries: dict[str, dict[str, Any]]
    observation_attempts_performed: int


def _alternate_landing_requested(
    summaries: Mapping[str, Mapping[str, Any]],
) -> bool:
    candidate = summaries.get("alternate_landing_candidate", {})
    observed = candidate.get("alternate_landing_candidate_evidence", {}).get("observed") or {}
    return bool(observed.get("alternate_landing_candidate"))


def _route_blocking_verified(
    summaries: Mapping[str, Mapping[str, Any]],
) -> bool:
    verification = summaries.get("route_blocking_verification", {})
    observed = verification.get("route_blocking_verification", {}).get("observed") or {}
    return bool(observed.get("route_blocking_verified"))


def observe_route_blocking_decision(
    *,
    observation_attempts: int,
    rth_requested: bool,
    observe_once: ObservationCycle,
    record_wait_observation: WaitObservation,
    sleep: Callable[[float], None] = time.sleep,
    retry_interval_seconds: float = 1.0,
) -> RouteBlockingDecision:
    """Repeat evidence observation until a bounded terminal branch is justified."""

    if observation_attempts < 1:
        raise ValueError("observation_attempts must be at least 1")
    if retry_interval_seconds < 0:
        raise ValueError("retry_interval_seconds must be non-negative")

    for attempt in range(1, observation_attempts + 1):
        summaries = {name: dict(summary) for name, summary in observe_once(attempt).items()}
        alternate_requested = _alternate_landing_requested(summaries)
        rth_behavior_requested = bool(rth_requested and _route_blocking_verified(summaries))
        if alternate_requested or rth_behavior_requested:
            return RouteBlockingDecision(
                alternate_landing_requested=alternate_requested,
                rth_behavior_requested=rth_behavior_requested,
                decision_summaries=summaries,
                observation_attempts_performed=attempt,
            )
        if attempt < observation_attempts:
            record_wait_observation(attempt)
            sleep(retry_interval_seconds)

    return RouteBlockingDecision(
        alternate_landing_requested=False,
        rth_behavior_requested=False,
        decision_summaries={},
        observation_attempts_performed=observation_attempts,
    )


__all__ = [
    "ObservationCycle",
    "RouteBlockingDecision",
    "WaitObservation",
    "observe_route_blocking_decision",
]
