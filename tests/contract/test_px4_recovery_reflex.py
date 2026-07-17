"""Contract tests for the PX4 budgeted-loiter reflex (issue #31).

Unlike TB3's stop-first reflex, an air vehicle cannot safely halt — the
reflex here bounds how long LLM deliberation may run before the
deterministic floor forces return-to-home, and it never claims to send a
command itself.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.runtime.px4_recovery_reflex import (
    DEFAULT_DISCHARGE_PCT_PER_MINUTE,
    MAX_DELIBERATION_BUDGET_SECONDS,
    PX4_RECOVERY_REFLEX_SCHEMA_VERSION,
    build_px4_recovery_reflex,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def test_reflex_grants_a_bounded_deliberation_budget_above_reserve() -> None:
    reflex = build_px4_recovery_reflex(
        trigger="battery_below_return_to_home_threshold",
        battery_percent=30.0,
        reserve_landing_percent=25.0,
        now=NOW,
    )
    assert reflex.schema_version == PX4_RECOVERY_REFLEX_SCHEMA_VERSION
    assert reflex.available_pct_before_reserve == 5.0
    expected_seconds = round((5.0 / DEFAULT_DISCHARGE_PCT_PER_MINUTE) * 60.0, 3)
    assert reflex.deliberation_budget_seconds == expected_seconds
    assert reflex.budget_exhausted is False
    assert reflex.reflex_action == "loiter"
    assert reflex.entered_deliberation is True


def test_reflex_forces_return_to_home_when_at_or_below_reserve() -> None:
    reflex = build_px4_recovery_reflex(
        trigger="battery_below_reserve_landing_threshold",
        battery_percent=25.0,
        reserve_landing_percent=25.0,
        now=NOW,
    )
    assert reflex.available_pct_before_reserve == 0.0
    assert reflex.deliberation_budget_seconds == 0.0
    assert reflex.budget_exhausted is True
    assert reflex.reflex_action == "return_to_home_recommended"
    assert reflex.entered_deliberation is False


def test_reflex_caps_budget_even_with_large_margin() -> None:
    reflex = build_px4_recovery_reflex(
        trigger="battery_below_return_to_home_threshold",
        battery_percent=95.0,
        reserve_landing_percent=25.0,
        discharge_pct_per_minute=0.01,
        now=NOW,
    )
    assert reflex.deliberation_budget_seconds == MAX_DELIBERATION_BUDGET_SECONDS


def test_reflex_rejects_nonpositive_discharge_rate() -> None:
    with pytest.raises(ValueError):
        build_px4_recovery_reflex(
            trigger="battery_below_return_to_home_threshold",
            battery_percent=30.0,
            reserve_landing_percent=25.0,
            discharge_pct_per_minute=0.0,
            now=NOW,
        )


def test_reflex_never_claims_command_authority() -> None:
    reflex = build_px4_recovery_reflex(
        trigger="battery_below_return_to_home_threshold",
        battery_percent=30.0,
        reserve_landing_percent=25.0,
        now=NOW,
    )
    assert reflex.approval_created is False
    assert reflex.dispatch_authority_created is False
    assert reflex.physical_execution_invoked is False
    assert reflex.command_payload_allowed is False
    assert "MAVLink" in reflex.claim_boundary
