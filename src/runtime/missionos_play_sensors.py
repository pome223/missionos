"""Mission-affecting sensor degradation for the play live flight.

Honest finding (verified live in this px4io gz SITL): scaling the model SDF
sensor noise (baro/mag/IMU) does NOT degrade the mission, because the EKF's
horizontal position is GPS-dominated and rides through the noise; PX4's
``failure gps off`` injection also no-ops (the gz navsat never acks it). The one
degradation that genuinely affects the mission is **GPS denial** — disabling GPS
fusion at the EKF level (``EKF2_GPS_CTRL=0``) before arm makes the horizontal
position estimate invalid (``xy_valid=False``), so the vehicle cannot establish
position and a safe controller must not launch.

This module exposes that real GPS-denied state so the play flight + recovery
agent can react to "no trustworthy position" rather than flying blind.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

PX4_BIN = "/opt/px4-gazebo/bin"
# EKF GPS-control bitmask. 0 disables GPS fusion -> GPS-denied operation.
GPS_DENIED_PARAM = ("EKF2_GPS_CTRL", "0")


@dataclass(frozen=True)
class GpsStatus:
    xy_position_valid: bool | None
    gnss_fused: bool | None


def _scan_bool(text: str, field_name: str) -> bool | None:
    match = re.search(rf"(?:^|\s){field_name}:\s*(True|False)", text)
    if not match:
        return None
    return match.group(1) == "True"


def read_gps_status(container: str) -> GpsStatus:
    """Read whether the EKF has a valid GPS-fused horizontal position."""
    xy_valid = None
    gnss = None
    try:
        pos = subprocess.run(
            ["docker", "exec", container, f"{PX4_BIN}/px4-listener",
             "vehicle_local_position"],
            check=False, capture_output=True, text=True, timeout=15,
        )
        xy_valid = _scan_bool(pos.stdout or "", "xy_valid")
        flags = subprocess.run(
            ["docker", "exec", container, f"{PX4_BIN}/px4-listener",
             "estimator_status_flags"],
            check=False, capture_output=True, text=True, timeout=15,
        )
        gnss = _scan_bool(flags.stdout or "", "cs_gnss_pos")
    except (subprocess.SubprocessError, OSError):
        pass
    return GpsStatus(xy_position_valid=xy_valid, gnss_fused=gnss)


def set_gps_denied(container: str, runner) -> None:
    """Disable GPS fusion at the EKF level (call before arming)."""
    name, value = GPS_DENIED_PARAM
    runner(["docker", "exec", container, f"{PX4_BIN}/px4-param", "set", name, value])


def gps_health_snapshot(status: GpsStatus) -> dict:
    """Shape the GPS state for the recovery snapshot. Honest: a sim EKF signal."""
    denied = status.gnss_fused is False or status.xy_position_valid is False
    return {
        "gps_fused": status.gnss_fused,
        "xy_position_valid": status.xy_position_valid,
        "gps_denied": denied,
        "position_trustworthy": status.xy_position_valid is True,
        "real_hardware_gnss_evidence": False,
    }
