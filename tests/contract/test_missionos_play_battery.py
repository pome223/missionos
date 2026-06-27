"""Contract tests for the physics-coupled battery (SDF patch + state parse)."""

import pytest

from src.runtime.missionos_play_battery import (
    BatteryState,
    inject_coupler_plugin,
    _scan,
)

pytestmark = pytest.mark.contract


def test_inject_coupler_plugin_adds_block_before_model_close():
    sdf = "<model name='x500'>\n  <link name='base_link'/>\n</model>\n"
    patched = inject_coupler_plugin(sdf)
    assert "MotorLoadBatteryCoupler" in patched
    # injected before the closing tag, model still well-formed-ish
    assert patched.index("MotorLoadBatteryCoupler") < patched.index("</model>")
    assert patched.count("</model>") == 1


def test_inject_coupler_plugin_requires_model_close_tag():
    with pytest.raises(ValueError):
        inject_coupler_plugin("<model name='x500'>no close")


def test_battery_state_scan_parses_gz_battery_message():
    dump = "header {...}\nvoltage: 25.16\ncurrent: 8.557\ncharge: 5.1\npercentage: 0.991\n"
    assert _scan(dump, "voltage") == pytest.approx(25.16)
    assert _scan(dump, "current") == pytest.approx(8.557)
    assert _scan(dump, "percentage") == pytest.approx(0.991)
    assert _scan(dump, "missing") is None


def test_battery_state_dataclass_round_trip():
    state = BatteryState(voltage_v=25.16, current_a=8.557, percentage=0.991)
    assert state.current_a > state.voltage_v * 0  # sanity
    assert 0.0 <= state.percentage <= 1.0
