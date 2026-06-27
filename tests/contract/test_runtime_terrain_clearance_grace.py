"""The risk side must respect the same terrain-clearance grace as the projection.

Regression for the mismatch where the terrain projection reports
below_minimum=false (margin within grace) but _telemetry_risk_reasons() still
flagged terrain_clearance_below_minimum on any clearance < target.
"""

import pytest

from src.intelligence.missionos_agent_runtime import _telemetry_risk_reasons

pytestmark = pytest.mark.contract


def _snap(clearance, target, margin, grace, below_minimum=None):
    terrain = {
        "terrain_clearance_m": clearance,
        "terrain_clearance_target_m": target,
        "terrain_clearance_margin_m": margin,
        "terrain_clearance_grace_m": grace,
    }
    if below_minimum is not None:
        terrain["terrain_clearance_below_minimum"] = below_minimum
    return {"terrain": terrain}


def test_within_grace_does_not_flag_below_minimum():
    # 29.228 m AGL vs 30 m target, margin -0.772, grace 1.0 -> within grace.
    reasons = _telemetry_risk_reasons(_snap(29.228, 30.0, -0.772, 1.0), {})
    assert "terrain_clearance_below_minimum" not in reasons


def test_beyond_grace_flags_below_minimum():
    # 28.969 m AGL, margin -1.031, grace 1.0 -> beyond grace.
    reasons = _telemetry_risk_reasons(_snap(28.969, 30.0, -1.031, 1.0), {})
    assert "terrain_clearance_below_minimum" in reasons


def test_explicit_below_minimum_flag_is_honoured():
    reasons = _telemetry_risk_reasons(
        _snap(29.5, 30.0, -0.5, 1.0, below_minimum=True), {}
    )
    assert "terrain_clearance_below_minimum" in reasons


def test_no_grace_defaults_to_strict_target():
    # No grace key -> grace 0 -> any sub-target clearance flags (back-compat).
    snap = {"terrain": {"terrain_clearance_m": 29.5, "terrain_clearance_target_m": 30.0}}
    reasons = _telemetry_risk_reasons(snap, {})
    assert "terrain_clearance_below_minimum" in reasons
