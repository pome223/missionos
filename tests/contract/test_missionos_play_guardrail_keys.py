"""The deterministic safety net must read play telemetry snapshots.

Regression guard for the key mismatch Codex flagged: the runtime guardrail's
_telemetry_risk_reasons() reads wind.speed_mps / route.deviation_xy_m (or the
top-level forms), so the play snapshots must populate those, not only the
display-only wind_mps / recovery.route_deviation_xy_m.
"""

import pytest

from src.intelligence.missionos_agent_runtime import _telemetry_risk_reasons
from src.runtime.missionos_play_live_sitl import _recovery_snapshot as live_snapshot
from src.runtime.missionos_play_delivery import _recovery_snapshot as delivery_snapshot
from src.runtime.missionos_play_wind_driver import WindDriverStep

pytestmark = pytest.mark.contract

_POLICY = {
    "max_wind_speed_mps": 10.0,
    "max_route_deviation_xy_m": 8.0,
    "emergency_landing_route_deviation_xy_m": 25.0,
}


class _Vehicle:
    max_wind_speed_mps = 10.0


class _Scenario:
    key = "test"
    ambient_wind_mps = 6.0
    vehicle = _Vehicle()


def test_live_snapshot_exposes_wind_and_deviation_to_guardrail():
    step = WindDriverStep(
        elapsed_s=1.0, altitude_agl_m=30.0, wind_mps=16.0,
        bearing_from_deg=270.0, force_east_n=40.0, force_north_n=0.0,
    )
    snap = live_snapshot(
        scenario=_Scenario(), wind_steps=(step,),
        takeoff_observed=True, route_deviation_xy_m=12.0,
    )
    reasons = _telemetry_risk_reasons(snap, _POLICY)
    assert "wind_above_recovery_limit" in reasons  # 16 > 10
    assert "route_deviation_above_limit" in reasons  # 12 > 8


def test_delivery_snapshot_exposes_wind_and_deviation_to_guardrail():
    snap = delivery_snapshot(
        scenario=_Scenario(), drift_m=12.0, wind_mps=16.0,
        phase="outbound", deviation_limit_m=8.0,
    )
    reasons = _telemetry_risk_reasons(snap, _POLICY)
    assert "wind_above_recovery_limit" in reasons
    assert "route_deviation_above_limit" in reasons


def test_calm_on_track_snapshot_has_no_wind_or_deviation_risk():
    step = WindDriverStep(
        elapsed_s=1.0, altitude_agl_m=30.0, wind_mps=4.0,
        bearing_from_deg=270.0, force_east_n=5.0, force_north_n=0.0,
    )
    snap = live_snapshot(
        scenario=_Scenario(), wind_steps=(step,),
        takeoff_observed=True, route_deviation_xy_m=1.0,
    )
    reasons = _telemetry_risk_reasons(snap, _POLICY)
    assert "wind_above_recovery_limit" not in reasons
    assert "route_deviation_above_limit" not in reasons
