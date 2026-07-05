"""Opt-in real Unitree Go2 SportClient readiness checks.

This module is the real-Go2 branch of the Unitree work. It does not start
MuJoCo and it does not send movement commands. The first boundary is a
read-only service probe: initialize SDK2 on an operator-provided robot network
interface, then ask the onboard sport and robot-state services for metadata.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


UNITREE_GO2_REAL_HARDWARE_READINESS_SCHEMA_VERSION = (
    "missionos_unitree_go2_real_hardware_readiness.v1"
)
UNITREE_GO2_REAL_HARDWARE_READINESS_SMOKE_ENV = (
    "RUN_MISSIONOS_UNITREE_GO2_REAL_HARDWARE_READINESS"
)
UNITREE_GO2_NETWORK_INTERFACE_ENV = "UNITREE_GO2_NETWORK_INTERFACE"
UNITREE_GO2_DOMAIN_ID_ENV = "UNITREE_GO2_DOMAIN_ID"
UNITREE_GO2_PYTHON_EXECUTABLE_ENV = "UNITREE_GO2_PYTHON_EXECUTABLE"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_LOOPBACK_INTERFACES = {"lo", "lo0", "localhost"}
_REAL_GO2_SERVICE_PROBE_CODE = r"""
from __future__ import annotations

import json
import sys

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.go2.robot_state.robot_state_client import RobotStateClient
from unitree_sdk2py.go2.sport.sport_client import SportClient

request = json.loads(sys.stdin.read())
domain_id = int(request["domain_id"])
network_interface = str(request["network_interface"])
client_timeout_s = float(request.get("client_timeout_s", 1.0))

ChannelFactoryInitialize(domain_id, network_interface)

sport_client = SportClient()
sport_client.SetTimeout(client_timeout_s)
sport_client.Init()
sport_client_api_version = sport_client.GetApiVersion()
sport_server_api_code, sport_server_api_version = sport_client.GetServerApiVersion()

robot_state_client = RobotStateClient()
robot_state_client.SetTimeout(client_timeout_s)
robot_state_client.Init()
robot_state_server_api_code, robot_state_server_api_version = (
    robot_state_client.GetServerApiVersion()
)
robot_state_service_list_code, service_list = robot_state_client.ServiceList()
robot_state_services = []
if service_list:
    robot_state_services = [
        {
            "name": str(item.name),
            "status": int(item.status),
            "protect": bool(item.protect),
        }
        for item in service_list
    ]

print(json.dumps({
    "channel_initialized": True,
    "sport_client_initialized": True,
    "sport_client_api_version": sport_client_api_version,
    "sport_server_api_code": sport_server_api_code,
    "sport_server_api_version": sport_server_api_version,
    "sport_service_available": sport_server_api_code == 0,
    "robot_state_server_api_code": robot_state_server_api_code,
    "robot_state_server_api_version": robot_state_server_api_version,
    "robot_state_service_list_code": robot_state_service_list_code,
    "robot_state_services": robot_state_services,
}, ensure_ascii=True, sort_keys=True))
"""


class UnitreeGo2RealHardwareReadiness(BaseModel):
    """Read-only real Go2 service readiness artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[UNITREE_GO2_REAL_HARDWARE_READINESS_SCHEMA_VERSION] = (
        UNITREE_GO2_REAL_HARDWARE_READINESS_SCHEMA_VERSION
    )
    readiness_status: Literal["ready", "blocked", "failed"]
    python_executable: str | None = None
    domain_id: int | None = None
    network_interface: str | None = None
    channel_initialized: bool = False
    sport_client_initialized: bool = False
    sport_client_api_version: str | None = None
    sport_server_api_code: int | None = None
    sport_server_api_version: str | None = None
    sport_service_available: bool = False
    robot_state_server_api_code: int | None = None
    robot_state_server_api_version: str | None = None
    robot_state_service_list_code: int | None = None
    robot_state_services: tuple[dict[str, Any], ...] = ()
    dispatch_request_sent: Literal[False] = False
    sport_move_call_attempted: Literal[False] = False
    command_ack_observed: Literal[False] = False
    completion_claimed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    raw_lowcmd_published: Literal[False] = False
    raw_motor_invoked: Literal[False] = False
    raw_velocity_invoked: Literal[False] = False
    special_motion_invoked: Literal[False] = False
    blocking_reasons: tuple[str, ...] = ()
    subprocess_returncode: int | None = None
    stderr_excerpt: str | None = None


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _dedupe(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _excerpt(value: str, *, limit: int = 800) -> str:
    text = value.strip()
    return text if len(text) <= limit else f"{text[:limit]}..."


def _loads_json_or_last_json_line(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        for line in reversed(value.splitlines()):
            text = line.strip()
            if not text.startswith("{"):
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            break
        else:
            raise
    if not isinstance(payload, dict):
        raise ValueError("expected JSON object")
    return payload


def build_unitree_go2_real_hardware_readiness(
    *,
    opt_in: bool,
    network_interface: str | None,
    domain_id: int = 0,
    python_executable: str | None = None,
    client_timeout_s: float = 1.0,
    subprocess_timeout_s: float = 10.0,
) -> UnitreeGo2RealHardwareReadiness:
    """Probe read-only Go2 onboard services after explicit real-hardware opt-in."""

    normalized_interface = (network_interface or "").strip()
    executable = python_executable or sys.executable
    blocking_reasons: list[str] = []
    if not opt_in or not _truthy_env(UNITREE_GO2_REAL_HARDWARE_READINESS_SMOKE_ENV):
        blocking_reasons.append("unitree_go2_real_hardware_readiness_opt_in_not_enabled")
    if not normalized_interface:
        blocking_reasons.append("unitree_go2_network_interface_required")
    if normalized_interface in _LOOPBACK_INTERFACES:
        blocking_reasons.append("unitree_go2_network_interface_must_not_be_loopback")
    if domain_id != 0:
        blocking_reasons.append("unitree_go2_real_hardware_domain_id_must_be_0")
    if blocking_reasons:
        return UnitreeGo2RealHardwareReadiness(
            readiness_status="blocked",
            python_executable=executable,
            domain_id=domain_id,
            network_interface=normalized_interface or None,
            blocking_reasons=_dedupe(blocking_reasons),
        )

    try:
        completed = subprocess.run(
            (executable, "-c", _REAL_GO2_SERVICE_PROBE_CODE),
            input=json.dumps(
                {
                    "domain_id": domain_id,
                    "network_interface": normalized_interface,
                    "client_timeout_s": client_timeout_s,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            capture_output=True,
            text=True,
            timeout=subprocess_timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return UnitreeGo2RealHardwareReadiness(
            readiness_status="failed",
            python_executable=executable,
            domain_id=domain_id,
            network_interface=normalized_interface,
            blocking_reasons=("unitree_go2_real_hardware_readiness_timeout",),
            stderr_excerpt=_excerpt(exc.stderr or ""),
        )
    except OSError as exc:
        return UnitreeGo2RealHardwareReadiness(
            readiness_status="failed",
            python_executable=executable,
            domain_id=domain_id,
            network_interface=normalized_interface,
            blocking_reasons=("unitree_go2_real_hardware_readiness_subprocess_failed",),
            stderr_excerpt=f"{type(exc).__name__}: {exc}",
        )

    if completed.returncode != 0:
        return UnitreeGo2RealHardwareReadiness(
            readiness_status="failed",
            python_executable=executable,
            domain_id=domain_id,
            network_interface=normalized_interface,
            subprocess_returncode=completed.returncode,
            blocking_reasons=("unitree_go2_real_hardware_readiness_failed",),
            stderr_excerpt=_excerpt(completed.stderr or completed.stdout),
        )

    try:
        payload = _loads_json_or_last_json_line(completed.stdout)
    except Exception as exc:
        return UnitreeGo2RealHardwareReadiness(
            readiness_status="failed",
            python_executable=executable,
            domain_id=domain_id,
            network_interface=normalized_interface,
            subprocess_returncode=completed.returncode,
            blocking_reasons=("unitree_go2_real_hardware_readiness_non_json",),
            stderr_excerpt=f"{type(exc).__name__}: {exc}",
        )

    sport_code = payload.get("sport_server_api_code")
    robot_state_code = payload.get("robot_state_service_list_code")
    blocking = []
    if sport_code != 0:
        blocking.append("unitree_go2_sport_service_unavailable")
    if robot_state_code != 0:
        blocking.append("unitree_go2_robot_state_service_list_unavailable")
    robot_state_services = payload.get("robot_state_services")
    return UnitreeGo2RealHardwareReadiness(
        readiness_status="ready" if not blocking else "blocked",
        python_executable=executable,
        domain_id=domain_id,
        network_interface=normalized_interface,
        channel_initialized=payload.get("channel_initialized") is True,
        sport_client_initialized=payload.get("sport_client_initialized") is True,
        sport_client_api_version=(
            str(payload["sport_client_api_version"])
            if payload.get("sport_client_api_version") is not None
            else None
        ),
        sport_server_api_code=int(sport_code) if isinstance(sport_code, int) else None,
        sport_server_api_version=(
            str(payload["sport_server_api_version"])
            if payload.get("sport_server_api_version") is not None
            else None
        ),
        sport_service_available=payload.get("sport_service_available") is True,
        robot_state_server_api_code=(
            int(payload["robot_state_server_api_code"])
            if isinstance(payload.get("robot_state_server_api_code"), int)
            else None
        ),
        robot_state_server_api_version=(
            str(payload["robot_state_server_api_version"])
            if payload.get("robot_state_server_api_version") is not None
            else None
        ),
        robot_state_service_list_code=(
            int(robot_state_code) if isinstance(robot_state_code, int) else None
        ),
        robot_state_services=(
            tuple(
                {str(key): value for key, value in item.items()}
                for item in robot_state_services
                if isinstance(item, dict)
            )
            if isinstance(robot_state_services, list)
            else ()
        ),
        subprocess_returncode=completed.returncode,
        blocking_reasons=_dedupe(blocking),
    )


__all__ = [
    "UNITREE_GO2_DOMAIN_ID_ENV",
    "UNITREE_GO2_NETWORK_INTERFACE_ENV",
    "UNITREE_GO2_PYTHON_EXECUTABLE_ENV",
    "UNITREE_GO2_REAL_HARDWARE_READINESS_SCHEMA_VERSION",
    "UNITREE_GO2_REAL_HARDWARE_READINESS_SMOKE_ENV",
    "UnitreeGo2RealHardwareReadiness",
    "build_unitree_go2_real_hardware_readiness",
]
