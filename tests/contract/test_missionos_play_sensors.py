"""Contract tests for mission-affecting sensor degradation (GPS denial)."""

import pytest

from src.runtime.missionos_play_sensors import (
    GPS_DENIED_PARAM,
    GpsStatus,
    _scan_bool,
    gps_health_snapshot,
)

pytestmark = pytest.mark.contract


def test_gps_denied_param_disables_ekf_gps_fusion():
    assert GPS_DENIED_PARAM == ("EKF2_GPS_CTRL", "0")


def test_scan_bool_reads_validity_fields():
    text = "xy_valid: False\n z_valid: True\n cs_gnss_pos: False\n"
    assert _scan_bool(text, "xy_valid") is False
    assert _scan_bool(text, "z_valid") is True
    assert _scan_bool(text, "cs_gnss_pos") is False
    assert _scan_bool(text, "missing") is None


def test_gps_denied_state_is_flagged_and_position_untrustworthy():
    snap = gps_health_snapshot(GpsStatus(xy_position_valid=False, gnss_fused=False))
    assert snap["gps_denied"] is True
    assert snap["position_trustworthy"] is False
    assert snap["real_hardware_gnss_evidence"] is False


def test_nominal_gps_is_trustworthy():
    snap = gps_health_snapshot(GpsStatus(xy_position_valid=True, gnss_fused=True))
    assert snap["gps_denied"] is False
    assert snap["position_trustworthy"] is True
