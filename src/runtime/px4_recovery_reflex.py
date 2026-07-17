"""PX4 reflex phase: budgeted loiter instead of TB3's stop-first (issue #31).

An air vehicle cannot safely halt the way a ground robot can — holding
position still burns battery. The reflex phase here computes how long LLM
deliberation may run before the deterministic floor must act: the battery
margin above the landing reserve, divided by an assumed discharge rate. If
deliberation would exceed that budget, a deterministic return-to-home fires
without waiting for the LLM.

This module only records the reflex assessment. MissionOS's delivery
recovery layer (see delivery_recovery_decision.py) explicitly sends no
MAVLink, setpoint, or actuator commands; actual RTL/loiter dispatch is a
firmware failsafe or an operator action outside this boundary. Widening this
to an actual dispatch mirrors the TB3 harness-authority step and is a
separate, deliberate change.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

PX4_RECOVERY_REFLEX_SCHEMA_VERSION = "missionos_px4_recovery_reflex.v1"

# Conservative multirotor hover discharge estimate used when no
# vehicle-specific rate is supplied. Erring toward a faster assumed drain
# shortens the deliberation budget, which fails toward an earlier RTL rather
# than a later one.
DEFAULT_DISCHARGE_PCT_PER_MINUTE = 2.0

# Deliberation is capped even when the battery margin is large, so a
# misconfigured discharge rate cannot produce an unbounded budget.
MAX_DELIBERATION_BUDGET_SECONDS = 180.0

PX4RecoveryReflexAction = Literal["loiter", "return_to_home_recommended"]


class PX4RecoveryReflex(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_RECOVERY_REFLEX_SCHEMA_VERSION] = (
        PX4_RECOVERY_REFLEX_SCHEMA_VERSION
    )
    trigger: str
    battery_percent: float
    reserve_landing_percent: float
    available_pct_before_reserve: float
    discharge_pct_per_minute: float
    deliberation_budget_seconds: float
    budget_exhausted: bool
    reflex_action: PX4RecoveryReflexAction
    recorded_at: datetime
    entered_deliberation: bool
    claim_boundary: str = (
        "The reflex record computes a deliberation time budget; it never "
        "sends MAVLink, setpoint, or actuator commands. Any RTL/loiter "
        "dispatch is a firmware failsafe or operator action outside this "
        "boundary."
    )
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_px4_recovery_reflex(
    *,
    trigger: str,
    battery_percent: float,
    reserve_landing_percent: float,
    discharge_pct_per_minute: float = DEFAULT_DISCHARGE_PCT_PER_MINUTE,
    now: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> PX4RecoveryReflex:
    """Compute the reflex budget: how long deliberation may run before RTL."""

    if discharge_pct_per_minute <= 0:
        raise ValueError("discharge_pct_per_minute must be positive")
    available = max(0.0, battery_percent - reserve_landing_percent)
    budget_seconds = min(
        MAX_DELIBERATION_BUDGET_SECONDS,
        (available / discharge_pct_per_minute) * 60.0,
    )
    exhausted = available <= 0.0
    return PX4RecoveryReflex(
        trigger=trigger,
        battery_percent=battery_percent,
        reserve_landing_percent=reserve_landing_percent,
        available_pct_before_reserve=round(available, 3),
        discharge_pct_per_minute=discharge_pct_per_minute,
        deliberation_budget_seconds=round(budget_seconds, 3),
        budget_exhausted=exhausted,
        reflex_action="return_to_home_recommended" if exhausted else "loiter",
        recorded_at=_utc(now),
        entered_deliberation=not exhausted,
        metadata=dict(metadata or {}),
    )


__all__ = [
    "DEFAULT_DISCHARGE_PCT_PER_MINUTE",
    "MAX_DELIBERATION_BUDGET_SECONDS",
    "PX4_RECOVERY_REFLEX_SCHEMA_VERSION",
    "PX4RecoveryReflex",
    "PX4RecoveryReflexAction",
    "build_px4_recovery_reflex",
]
