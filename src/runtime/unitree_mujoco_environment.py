"""Unitree MuJoCo external environment readiness and launch checks.

MissionOS does not vendor Unitree MuJoCo, import Unitree SDK2, or start MuJoCo
unless an explicit opt-in caller asks for that stronger boundary. Readiness
checks validate that an operator-provided local checkout looks like the
supported Go2 Python simulator environment before a future opt-in smoke crosses
that simulator boundary.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


UNITREE_MUJOCO_ENVIRONMENT_READINESS_SCHEMA_VERSION = (
    "missionos_unitree_mujoco_environment_readiness.v1"
)
UNITREE_SDK2_IMPORT_READINESS_SCHEMA_VERSION = (
    "missionos_unitree_sdk2_import_readiness.v1"
)
UNITREE_MUJOCO_PROCESS_LAUNCH_SCHEMA_VERSION = (
    "missionos_unitree_mujoco_process_launch.v1"
)
UNITREE_MUJOCO_SCENE_OBSERVATION_SCHEMA_VERSION = (
    "missionos_unitree_mujoco_scene_observation.v1"
)
UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SCHEMA_VERSION = (
    "missionos_unitree_mujoco_sdk2_bridge_observation.v1"
)
UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SCHEMA_VERSION = (
    "missionos_unitree_mujoco_sport_client_probe.v1"
)
UNITREE_MUJOCO_READINESS_SMOKE_ENV = "RUN_MISSIONOS_UNITREE_MUJOCO_READINESS_SMOKE"
UNITREE_SDK2_IMPORT_SMOKE_ENV = "RUN_MISSIONOS_UNITREE_SDK2_IMPORT_SMOKE"
UNITREE_MUJOCO_PROCESS_SMOKE_ENV = "RUN_MISSIONOS_UNITREE_MUJOCO_PROCESS_SMOKE"
UNITREE_MUJOCO_SCENE_OBSERVATION_SMOKE_ENV = (
    "RUN_MISSIONOS_UNITREE_MUJOCO_SCENE_OBSERVATION_SMOKE"
)
UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SMOKE_ENV = (
    "RUN_MISSIONOS_UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SMOKE"
)
UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SMOKE_ENV = (
    "RUN_MISSIONOS_UNITREE_MUJOCO_SPORT_CLIENT_PROBE"
)
UNITREE_MUJOCO_ROOT_ENV = "UNITREE_MUJOCO_ROOT"
UNITREE_MUJOCO_PYTHON_EXECUTABLE_ENV = "UNITREE_MUJOCO_PYTHON_EXECUTABLE"
UNITREE_MUJOCO_SDK2_CHANNEL_INTERFACE_ENV = "UNITREE_MUJOCO_SDK2_CHANNEL_INTERFACE"
UNITREE_SDK2_PYTHON_EXECUTABLE_ENV = "UNITREE_SDK2_PYTHON_EXECUTABLE"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_SDK2_IMPORT_MODULES = (
    "unitree_sdk2py",
    "unitree_sdk2py.core.channel",
    "unitree_sdk2py.go2.sport.sport_client",
)
_LOOPBACK_INTERFACES = ("lo", "lo0")
_SDK2_IMPORT_SUBPROCESS_CODE = r"""
import importlib
import json
import sys

module_names = json.loads(sys.stdin.read())
imported_modules = []
missing_modules = []
module_errors = {}
for module_name in module_names:
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        missing_modules.append(module_name)
        detail = str(exc).strip() or "<empty>"
        module_errors[module_name] = f"{type(exc).__name__}: {detail[:240]}"
    else:
        imported_modules.append(module_name)

print(json.dumps({
    "imported_modules": imported_modules,
    "missing_modules": missing_modules,
    "module_errors": module_errors,
}, ensure_ascii=True, sort_keys=True))
"""
_SCENE_OBSERVATION_SUBPROCESS_CODE = r"""
from __future__ import annotations

import json
import math
import sys

import mujoco
import numpy as np

import config

request = json.loads(sys.stdin.read())
step_count = int(request.get("step_count", 200))
motion_threshold = float(request.get("motion_threshold", 1e-6))

model = mujoco.MjModel.from_xml_path(config.ROBOT_SCENE)
data = mujoco.MjData(model)
qpos_before = np.array(data.qpos, copy=True)
for _ in range(max(step_count, 0)):
    mujoco.mj_step(model, data)
qpos_after = np.array(data.qpos, copy=True)
qpos_delta_norm = float(np.linalg.norm(qpos_after - qpos_before))

print(json.dumps({
    "scene_loaded_observed": True,
    "robot_motion_observed": bool(
        math.isfinite(qpos_delta_norm) and qpos_delta_norm > motion_threshold
    ),
    "qpos_delta_norm": qpos_delta_norm,
    "step_count": step_count,
    "motion_source": "uncommanded_physics_step",
    "model_nq": int(model.nq),
    "model_nv": int(model.nv),
    "model_nu": int(model.nu),
    "config_robot": str(config.ROBOT),
    "config_domain_id": int(config.DOMAIN_ID),
    "config_interface": str(config.INTERFACE),
}, ensure_ascii=True, sort_keys=True))
"""
_SDK2_BRIDGE_OBSERVATION_SUBPROCESS_CODE = r"""
from __future__ import annotations

import json
import math
import os
import sys
import time

import mujoco
import numpy as np

import config
from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_
from unitree_sdk2py_bridge import UnitreeSdk2Bridge

request = json.loads(sys.stdin.read())
step_count = int(request.get("step_count", 400))
motion_threshold = float(request.get("motion_threshold", 1e-6))
channel_interface = str(request.get("channel_interface") or "config")
network_interfaces = []
if os.path.isdir("/sys/class/net"):
    network_interfaces = sorted(
        name
        for name in os.listdir("/sys/class/net")
        if os.path.isdir(os.path.join("/sys/class/net", name))
    )
active_network_interfaces = []
for name in network_interfaces:
    operstate_path = f"/sys/class/net/{name}/operstate"
    try:
        operstate = open(operstate_path, encoding="utf-8").read().strip()
    except OSError:
        operstate = "unknown"
    if operstate in {"up", "unknown"}:
        active_network_interfaces.append(name)
active_non_loopback_interfaces = [
    name for name in active_network_interfaces if name not in {"lo", "lo0"}
]
network_isolated = bool(network_interfaces) and not active_non_loopback_interfaces
if channel_interface == "auto" and not network_isolated:
    print(json.dumps({
        "bridge_observation_status": "blocked",
        "blocking_reasons": ["unitree_sdk2_auto_interface_requires_isolated_network"],
        "network_interfaces": network_interfaces,
        "active_network_interfaces": active_network_interfaces,
        "network_isolated": False,
        "channel_interface_mode": "auto",
        "scene_loaded_observed": False,
        "robot_motion_observed": False,
        "sdk2_bridge_started": False,
        "lowstate_observed": False,
    }, ensure_ascii=True, sort_keys=True))
    raise SystemExit(0)

model = mujoco.MjModel.from_xml_path(config.ROBOT_SCENE)
data = mujoco.MjData(model)
qpos_before = np.array(data.qpos, copy=True)
observed = []

def lowstate_handler(msg: LowState_) -> None:
    if not observed:
        observed.append({
            "motor_count": len(msg.motor_state),
            "first_motor_q": float(msg.motor_state[0].q),
            "power_v": float(getattr(msg, "power_v", 0.0)),
        })

if channel_interface == "auto":
    ChannelFactoryInitialize(config.DOMAIN_ID)
    channel_interface_used = "auto"
elif channel_interface == "config":
    ChannelFactoryInitialize(config.DOMAIN_ID, config.INTERFACE)
    channel_interface_used = str(config.INTERFACE)
else:
    ChannelFactoryInitialize(config.DOMAIN_ID, channel_interface)
    channel_interface_used = channel_interface

UnitreeSdk2Bridge(model, data)
subscriber = ChannelSubscriber("rt/lowstate", LowState_)
subscriber.Init(lowstate_handler, 10)
for _ in range(max(step_count, 0)):
    mujoco.mj_step(model, data)
    if hasattr(model, "opt") and getattr(model.opt, "timestep", 0) > 0:
        time.sleep(float(model.opt.timestep))
qpos_after = np.array(data.qpos, copy=True)
qpos_delta_norm = float(np.linalg.norm(qpos_after - qpos_before))

print(json.dumps({
    "bridge_observation_status": "observed" if observed else "failed",
    "blocking_reasons": [] if observed else ["unitree_sdk2_lowstate_not_observed"],
    "network_interfaces": network_interfaces,
    "active_network_interfaces": active_network_interfaces,
    "network_isolated": network_isolated,
    "channel_interface_mode": channel_interface,
    "channel_interface_used": channel_interface_used,
    "scene_loaded_observed": True,
    "robot_motion_observed": bool(
        math.isfinite(qpos_delta_norm) and qpos_delta_norm > motion_threshold
    ),
    "sdk2_bridge_started": True,
    "lowstate_observed": bool(observed),
    "lowstate_sample": observed[0] if observed else None,
    "qpos_delta_norm": qpos_delta_norm,
    "step_count": step_count,
    "model_nq": int(model.nq),
    "model_nv": int(model.nv),
    "model_nu": int(model.nu),
    "config_robot": str(config.ROBOT),
    "config_domain_id": int(config.DOMAIN_ID),
    "config_interface": str(config.INTERFACE),
}, ensure_ascii=True, sort_keys=True))
"""
_SPORT_CLIENT_PROBE_SUBPROCESS_CODE = r"""
from __future__ import annotations

import json
import math
import os
import sys
import time

import mujoco
import numpy as np

import config
from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
from unitree_sdk2py.go2.robot_state.robot_state_client import RobotStateClient
from unitree_sdk2py.go2.sport.sport_client import SportClient
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_
from unitree_sdk2py_bridge import UnitreeSdk2Bridge

request = json.loads(sys.stdin.read())
step_count = int(request.get("step_count", 400))
warmup_steps = int(request.get("warmup_steps", 50))
motion_threshold = float(request.get("motion_threshold", 1e-6))
channel_interface = str(request.get("channel_interface") or "config")
vx_mps = float(request.get("vx_mps", 0.05))
vy_mps = float(request.get("vy_mps", 0.0))
vyaw_rps = float(request.get("vyaw_rps", 0.0))
client_timeout_s = float(request.get("client_timeout_s", 1.0))
network_interfaces = []
if os.path.isdir("/sys/class/net"):
    network_interfaces = sorted(
        name
        for name in os.listdir("/sys/class/net")
        if os.path.isdir(os.path.join("/sys/class/net", name))
    )
active_network_interfaces = []
for name in network_interfaces:
    operstate_path = f"/sys/class/net/{name}/operstate"
    try:
        operstate = open(operstate_path, encoding="utf-8").read().strip()
    except OSError:
        operstate = "unknown"
    if operstate in {"up", "unknown"}:
        active_network_interfaces.append(name)
active_non_loopback_interfaces = [
    name for name in active_network_interfaces if name not in {"lo", "lo0"}
]
network_isolated = bool(network_interfaces) and not active_non_loopback_interfaces
if channel_interface == "auto" and not network_isolated:
    print(json.dumps({
        "probe_status": "blocked",
        "blocking_reasons": ["unitree_sdk2_auto_interface_requires_isolated_network"],
        "network_interfaces": network_interfaces,
        "active_network_interfaces": active_network_interfaces,
        "network_isolated": False,
        "channel_interface_mode": "auto",
        "scene_loaded_observed": False,
        "robot_motion_observed": False,
        "sdk2_bridge_started": False,
        "lowstate_observed": False,
        "sport_client_initialized": False,
        "sport_move_call_attempted": False,
    }, ensure_ascii=True, sort_keys=True))
    raise SystemExit(0)

model = mujoco.MjModel.from_xml_path(config.ROBOT_SCENE)
data = mujoco.MjData(model)
observed = []

def lowstate_handler(msg: LowState_) -> None:
    if not observed:
        observed.append({
            "motor_count": len(msg.motor_state),
            "first_motor_q": float(msg.motor_state[0].q),
            "power_v": float(getattr(msg, "power_v", 0.0)),
        })

if channel_interface == "auto":
    ChannelFactoryInitialize(config.DOMAIN_ID)
    channel_interface_used = "auto"
elif channel_interface == "config":
    ChannelFactoryInitialize(config.DOMAIN_ID, config.INTERFACE)
    channel_interface_used = str(config.INTERFACE)
else:
    ChannelFactoryInitialize(config.DOMAIN_ID, channel_interface)
    channel_interface_used = channel_interface

UnitreeSdk2Bridge(model, data)
subscriber = ChannelSubscriber("rt/lowstate", LowState_)
subscriber.Init(lowstate_handler, 10)
for _ in range(max(warmup_steps, 0)):
    mujoco.mj_step(model, data)
    if hasattr(model, "opt") and getattr(model.opt, "timestep", 0) > 0:
        time.sleep(float(model.opt.timestep))

qpos_before_probe = np.array(data.qpos, copy=True)
sport_client_initialized = False
sport_move_call_attempted = False
sport_move_return_code = None
stop_move_return_code = None
sport_client_error = None
sport_server_api_code = None
sport_server_api_version = None
sport_client_api_version = None
robot_state_server_api_code = None
robot_state_server_api_version = None
robot_state_service_list_code = None
robot_state_services = []
robot_state_client_error = None
try:
    sport_client = SportClient()
    sport_client.SetTimeout(client_timeout_s)
    sport_client.Init()
    sport_client_initialized = True
    sport_client_api_version = sport_client.GetApiVersion()
    sport_server_api_code, sport_server_api_version = sport_client.GetServerApiVersion()
    sport_move_call_attempted = True
    sport_move_return_code = int(sport_client.Move(vx_mps, vy_mps, vyaw_rps))
    if sport_move_return_code == 0:
        stop_move_return_code = int(sport_client.StopMove())
except Exception as exc:
    sport_client_error = f"{type(exc).__name__}: {exc}"

try:
    robot_state_client = RobotStateClient()
    robot_state_client.SetTimeout(client_timeout_s)
    robot_state_client.Init()
    robot_state_server_api_code, robot_state_server_api_version = (
        robot_state_client.GetServerApiVersion()
    )
    robot_state_service_list_code, service_list = robot_state_client.ServiceList()
    if service_list:
        robot_state_services = [
            {
                "name": str(item.name),
                "status": int(item.status),
                "protect": bool(item.protect),
            }
            for item in service_list
        ]
except Exception as exc:
    robot_state_client_error = f"{type(exc).__name__}: {exc}"

for _ in range(max(step_count, 0)):
    mujoco.mj_step(model, data)
    if hasattr(model, "opt") and getattr(model.opt, "timestep", 0) > 0:
        time.sleep(float(model.opt.timestep))
qpos_after_probe = np.array(data.qpos, copy=True)
qpos_delta_norm = float(np.linalg.norm(qpos_after_probe - qpos_before_probe))
dispatch_request_sent = sport_move_return_code == 0
blocking_reasons = []
if not observed:
    blocking_reasons.append("unitree_sdk2_lowstate_not_observed")
if sport_client_error is not None:
    blocking_reasons.append("unitree_sport_client_probe_failed")
elif sport_move_return_code is None:
    blocking_reasons.append("unitree_sport_client_move_not_attempted")
elif sport_move_return_code != 0:
    blocking_reasons.append("unitree_sport_client_move_not_accepted")
    blocking_reasons.append("unitree_mujoco_sport_service_unavailable")
if sport_server_api_code is not None and sport_server_api_code != 0:
    blocking_reasons.append("unitree_sport_service_version_unavailable")
if robot_state_service_list_code is not None and robot_state_service_list_code != 0:
    blocking_reasons.append("unitree_robot_state_service_list_unavailable")
if robot_state_client_error is not None:
    blocking_reasons.append("unitree_robot_state_client_probe_failed")

print(json.dumps({
    "probe_status": (
        "observed" if dispatch_request_sent else "blocked" if not sport_client_error else "failed"
    ),
    "blocking_reasons": blocking_reasons,
    "network_interfaces": network_interfaces,
    "active_network_interfaces": active_network_interfaces,
    "network_isolated": network_isolated,
    "channel_interface_mode": channel_interface,
    "channel_interface_used": channel_interface_used,
    "scene_loaded_observed": True,
    "robot_motion_observed": bool(
        math.isfinite(qpos_delta_norm) and qpos_delta_norm > motion_threshold
    ),
    "sdk2_bridge_started": True,
    "lowstate_observed": bool(observed),
    "lowstate_sample": observed[0] if observed else None,
    "sport_client_initialized": sport_client_initialized,
    "sport_move_call_attempted": sport_move_call_attempted,
    "sport_move_return_code": sport_move_return_code,
    "stop_move_return_code": stop_move_return_code,
    "sport_client_error": sport_client_error,
    "sport_server_api_code": sport_server_api_code,
    "sport_server_api_version": sport_server_api_version,
    "sport_client_api_version": sport_client_api_version,
    "sport_service_available": sport_server_api_code == 0,
    "robot_state_server_api_code": robot_state_server_api_code,
    "robot_state_server_api_version": robot_state_server_api_version,
    "robot_state_service_list_code": robot_state_service_list_code,
    "robot_state_services": robot_state_services,
    "robot_state_client_error": robot_state_client_error,
    "bounded_command": {
        "vx_mps": vx_mps,
        "vy_mps": vy_mps,
        "vyaw_rps": vyaw_rps,
    },
    "qpos_delta_norm": qpos_delta_norm,
    "step_count": step_count,
    "warmup_steps": warmup_steps,
    "model_nq": int(model.nq),
    "model_nv": int(model.nv),
    "model_nu": int(model.nu),
    "config_robot": str(config.ROBOT),
    "config_domain_id": int(config.DOMAIN_ID),
    "config_interface": str(config.INTERFACE),
}, ensure_ascii=True, sort_keys=True))
"""

_REQUIRED_RELATIVE_FILES = (
    "readme.md",
    "simulate_python/unitree_mujoco.py",
    "simulate_python/config.py",
    "simulate_python/unitree_sdk2py_bridge.py",
    "unitree_robots/go2/scene.xml",
    "example/python/stand_go2.py",
)


class UnitreeMujocoEnvironmentReadiness(BaseModel):
    """Read-only readiness artifact for an external Unitree MuJoCo checkout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[UNITREE_MUJOCO_ENVIRONMENT_READINESS_SCHEMA_VERSION] = (
        UNITREE_MUJOCO_ENVIRONMENT_READINESS_SCHEMA_VERSION
    )
    readiness_status: Literal["ready", "blocked"]
    checkout_root_provided: bool
    checkout_root_name: str | None = None
    checked_relative_files: tuple[str, ...] = _REQUIRED_RELATIVE_FILES
    missing_relative_files: tuple[str, ...] = ()
    expected_robot: str = "go2"
    config_robot: str | None = None
    config_robot_scene: str | None = None
    config_domain_id: int | None = None
    config_interface: str | None = None
    config_use_joystick: int | bool | None = None
    unitree_sdk2_imported: Literal[False] = False
    mujoco_started: Literal[False] = False
    dispatch_request_sent: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    blocking_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = Field(
        default=(
            "https://github.com/unitreerobotics/unitree_mujoco",
            "https://github.com/unitreerobotics/unitree_sdk2_python",
        )
    )


class UnitreeSdk2ImportReadiness(BaseModel):
    """Opt-in import readiness artifact for Unitree SDK2 Python."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[UNITREE_SDK2_IMPORT_READINESS_SCHEMA_VERSION] = (
        UNITREE_SDK2_IMPORT_READINESS_SCHEMA_VERSION
    )
    readiness_status: Literal["ready", "blocked"]
    import_attempted: bool
    checked_modules: tuple[str, ...] = _SDK2_IMPORT_MODULES
    imported_modules: tuple[str, ...] = ()
    missing_modules: tuple[str, ...] = ()
    module_errors: dict[str, str] = Field(default_factory=dict)
    python_executable: str | None = None
    import_subprocess_invoked: bool = False
    import_subprocess_returncode: int | None = None
    blocking_reasons: tuple[str, ...] = ()
    mujoco_started: Literal[False] = False
    dispatch_request_sent: Literal[False] = False
    physical_execution_invoked: Literal[False] = False


class UnitreeMujocoProcessLaunchResult(BaseModel):
    """Opt-in process launch artifact for an external Unitree MuJoCo simulator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[UNITREE_MUJOCO_PROCESS_LAUNCH_SCHEMA_VERSION] = (
        UNITREE_MUJOCO_PROCESS_LAUNCH_SCHEMA_VERSION
    )
    launch_status: Literal["started", "blocked", "exited_early", "timeout"]
    checkout_root_name: str | None = None
    command: tuple[str, ...] = ()
    cwd_name: str | None = None
    process_started: bool = False
    process_pid: int | None = None
    mujoco_started: bool = False
    scene_loaded_observed: Literal[False] = False
    robot_motion_observed: Literal[False] = False
    unitree_sdk2_imported: Literal[False] = False
    dispatch_request_sent: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    terminated_by_smoke: bool = False
    returncode: int | None = None
    blocking_reasons: tuple[str, ...] = ()
    stderr_excerpt: str | None = None


class UnitreeMujocoSceneObservationResult(BaseModel):
    """Opt-in headless MuJoCo scene load and passive motion observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[UNITREE_MUJOCO_SCENE_OBSERVATION_SCHEMA_VERSION] = (
        UNITREE_MUJOCO_SCENE_OBSERVATION_SCHEMA_VERSION
    )
    observation_status: Literal["observed", "blocked", "failed"]
    checkout_root_name: str | None = None
    command: tuple[str, ...] = ()
    cwd_name: str | None = None
    scene_loaded_observed: bool = False
    robot_motion_observed: bool = False
    motion_source: Literal["none", "uncommanded_physics_step"] = "none"
    qpos_delta_norm: float | None = None
    step_count: int = 0
    model_nq: int | None = None
    model_nv: int | None = None
    model_nu: int | None = None
    config_robot: str | None = None
    config_domain_id: int | None = None
    config_interface: str | None = None
    mujoco_process_started: Literal[False] = False
    dispatch_request_sent: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    subprocess_returncode: int | None = None
    blocking_reasons: tuple[str, ...] = ()
    stderr_excerpt: str | None = None


class UnitreeMujocoSdk2BridgeObservationResult(BaseModel):
    """Opt-in headless Unitree SDK2 bridge state observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SCHEMA_VERSION] = (
        UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SCHEMA_VERSION
    )
    bridge_observation_status: Literal["observed", "blocked", "failed"]
    checkout_root_name: str | None = None
    command: tuple[str, ...] = ()
    cwd_name: str | None = None
    scene_loaded_observed: bool = False
    robot_motion_observed: bool = False
    sdk2_bridge_started: bool = False
    lowstate_observed: bool = False
    lowstate_sample: dict[str, Any] | None = None
    qpos_delta_norm: float | None = None
    step_count: int = 0
    model_nq: int | None = None
    model_nv: int | None = None
    model_nu: int | None = None
    config_robot: str | None = None
    config_domain_id: int | None = None
    config_interface: str | None = None
    network_interfaces: tuple[str, ...] = ()
    active_network_interfaces: tuple[str, ...] = ()
    network_isolated: bool = False
    channel_interface_mode: Literal["config", "auto", "explicit"] = "config"
    channel_interface_used: str | None = None
    dispatch_request_sent: Literal[False] = False
    command_ack_observed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    subprocess_returncode: int | None = None
    blocking_reasons: tuple[str, ...] = ()
    stderr_excerpt: str | None = None


class UnitreeMujocoSportClientProbeResult(BaseModel):
    """Opt-in probe for the official Go2 SportClient bounded-move surface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SCHEMA_VERSION] = (
        UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SCHEMA_VERSION
    )
    probe_status: Literal["observed", "blocked", "failed"]
    checkout_root_name: str | None = None
    command: tuple[str, ...] = ()
    cwd_name: str | None = None
    scene_loaded_observed: bool = False
    robot_motion_observed: bool = False
    sdk2_bridge_started: bool = False
    lowstate_observed: bool = False
    lowstate_sample: dict[str, Any] | None = None
    sport_client_initialized: bool = False
    sport_move_call_attempted: bool = False
    sport_move_return_code: int | None = None
    stop_move_return_code: int | None = None
    sport_client_error: str | None = None
    sport_server_api_code: int | None = None
    sport_server_api_version: str | None = None
    sport_client_api_version: str | None = None
    sport_service_available: bool = False
    robot_state_server_api_code: int | None = None
    robot_state_server_api_version: str | None = None
    robot_state_service_list_code: int | None = None
    robot_state_services: tuple[dict[str, Any], ...] = ()
    robot_state_client_error: str | None = None
    bounded_command: dict[str, float] = Field(default_factory=dict)
    qpos_delta_norm: float | None = None
    step_count: int = 0
    warmup_steps: int = 0
    model_nq: int | None = None
    model_nv: int | None = None
    model_nu: int | None = None
    config_robot: str | None = None
    config_domain_id: int | None = None
    config_interface: str | None = None
    network_interfaces: tuple[str, ...] = ()
    active_network_interfaces: tuple[str, ...] = ()
    network_isolated: bool = False
    channel_interface_mode: Literal["config", "auto", "explicit"] = "config"
    channel_interface_used: str | None = None
    dispatch_request_sent: bool = False
    command_ack_observed: Literal[False] = False
    completion_claimed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    raw_lowcmd_published: Literal[False] = False
    raw_motor_invoked: Literal[False] = False
    raw_velocity_invoked: Literal[False] = False
    special_motion_invoked: Literal[False] = False
    subprocess_returncode: int | None = None
    blocking_reasons: tuple[str, ...] = ()
    stderr_excerpt: str | None = None


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _safe_config_value(node: ast.AST, values: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _safe_config_value(node.left, values)
        right = _safe_config_value(node.right, values)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
    raise ValueError("unsupported config expression")


def _literal_assignments(config_text: str) -> dict[str, Any]:
    parsed = ast.parse(config_text)
    values: dict[str, Any] = {}
    for node in parsed.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        try:
            values[name] = _safe_config_value(node.value, values)
        except Exception:
            continue
    return values


def _dedupe(values: list[str]) -> tuple[str, ...]:
    deduped: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in deduped:
            deduped.append(text)
    return tuple(deduped)


def _excerpt(value: str, *, limit: int = 800) -> str | None:
    text = value.strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _format_import_error(exc: Exception) -> str:
    detail = _excerpt(str(exc), limit=240) or "<empty>"
    return f"{type(exc).__name__}: {detail}"


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


def build_unitree_mujoco_environment_readiness(
    *,
    checkout_root: str | Path | None,
    expected_robot: str = "go2",
) -> UnitreeMujocoEnvironmentReadiness:
    """Validate a local Unitree MuJoCo checkout without starting it."""

    blocking_reasons: list[str] = []
    warning_reasons: list[str] = []
    config_values: dict[str, Any] = {}
    missing_relative_files: list[str] = []
    root_name: str | None = None

    if checkout_root is None or not str(checkout_root).strip():
        blocking_reasons.append("unitree_mujoco_root_not_provided")
        root: Path | None = None
    else:
        root = Path(checkout_root).expanduser()
        root_name = root.name
        if not root.exists():
            blocking_reasons.append("unitree_mujoco_root_not_found")
        elif not root.is_dir():
            blocking_reasons.append("unitree_mujoco_root_not_directory")

    if root is not None and root.exists() and root.is_dir():
        for relative_path in _REQUIRED_RELATIVE_FILES:
            if not (root / relative_path).is_file():
                missing_relative_files.append(relative_path)
        if missing_relative_files:
            blocking_reasons.append("unitree_mujoco_required_files_missing")

        config_path = root / "simulate_python/config.py"
        if config_path.is_file():
            config_values = _literal_assignments(
                config_path.read_text(encoding="utf-8")
            )
        else:
            blocking_reasons.append("unitree_mujoco_python_config_missing")

    config_robot = config_values.get("ROBOT")
    config_robot_scene = config_values.get("ROBOT_SCENE")
    config_domain_id = config_values.get("DOMAIN_ID")
    config_interface = config_values.get("INTERFACE")
    config_use_joystick = config_values.get("USE_JOYSTICK")

    if config_values:
        if config_robot != expected_robot:
            blocking_reasons.append("unitree_mujoco_robot_not_go2")
        if not isinstance(config_robot_scene, str) or "unitree_robots" not in config_robot_scene:
            blocking_reasons.append("unitree_mujoco_robot_scene_not_unitree_robot_ref")
        if config_domain_id != 1:
            blocking_reasons.append("unitree_mujoco_domain_id_not_sim_safe_1")
        if config_interface not in _LOOPBACK_INTERFACES:
            blocking_reasons.append("unitree_mujoco_interface_not_loopback")
        if config_use_joystick not in {0, False, None}:
            warning_reasons.append("unitree_mujoco_joystick_enabled")

    ready = not blocking_reasons
    return UnitreeMujocoEnvironmentReadiness(
        readiness_status="ready" if ready else "blocked",
        checkout_root_provided=root is not None,
        checkout_root_name=root_name,
        missing_relative_files=tuple(missing_relative_files),
        expected_robot=expected_robot,
        config_robot=str(config_robot) if config_robot is not None else None,
        config_robot_scene=(
            str(config_robot_scene) if config_robot_scene is not None else None
        ),
        config_domain_id=(
            int(config_domain_id) if isinstance(config_domain_id, int) else None
        ),
        config_interface=str(config_interface) if config_interface is not None else None,
        config_use_joystick=(
            config_use_joystick
            if isinstance(config_use_joystick, int | bool)
            else None
        ),
        blocking_reasons=_dedupe(blocking_reasons),
        warning_reasons=_dedupe(warning_reasons),
    )


def build_unitree_sdk2_import_readiness(
    *,
    opt_in: bool,
    module_names: tuple[str, ...] = _SDK2_IMPORT_MODULES,
    python_executable: str | None = None,
    import_timeout_s: float = 10.0,
) -> UnitreeSdk2ImportReadiness:
    """Attempt Unitree SDK2 Python imports only when explicitly opted in."""

    if not opt_in or not _truthy_env(UNITREE_SDK2_IMPORT_SMOKE_ENV):
        return UnitreeSdk2ImportReadiness(
            readiness_status="blocked",
            import_attempted=False,
            checked_modules=module_names,
            python_executable=python_executable,
            blocking_reasons=("unitree_sdk2_import_opt_in_not_enabled",),
        )

    if python_executable and python_executable.strip():
        try:
            completed = subprocess.run(
                (python_executable, "-c", _SDK2_IMPORT_SUBPROCESS_CODE),
                input=json.dumps(list(module_names), ensure_ascii=True),
                capture_output=True,
                text=True,
                timeout=import_timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return UnitreeSdk2ImportReadiness(
                readiness_status="blocked",
                import_attempted=True,
                checked_modules=module_names,
                python_executable=python_executable,
                import_subprocess_invoked=True,
                blocking_reasons=("unitree_sdk2_import_subprocess_timeout",),
            )
        except OSError as exc:
            return UnitreeSdk2ImportReadiness(
                readiness_status="blocked",
                import_attempted=True,
                checked_modules=module_names,
                module_errors={"__subprocess__": _format_import_error(exc)},
                python_executable=python_executable,
                import_subprocess_invoked=True,
                blocking_reasons=("unitree_sdk2_import_subprocess_failed",),
            )

        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            return UnitreeSdk2ImportReadiness(
                readiness_status="blocked",
                import_attempted=True,
                checked_modules=module_names,
                module_errors={"__subprocess__": _format_import_error(exc)},
                python_executable=python_executable,
                import_subprocess_invoked=True,
                import_subprocess_returncode=completed.returncode,
                blocking_reasons=("unitree_sdk2_import_subprocess_non_json",),
            )

        imported = tuple(str(value) for value in payload.get("imported_modules", ()))
        missing = tuple(str(value) for value in payload.get("missing_modules", ()))
        module_errors = {
            str(key): str(value)
            for key, value in payload.get("module_errors", {}).items()
        }
        blocking_reasons = []
        if completed.returncode != 0:
            blocking_reasons.append("unitree_sdk2_import_subprocess_failed")
        if missing:
            blocking_reasons.append("unitree_sdk2_python_modules_missing")
        return UnitreeSdk2ImportReadiness(
            readiness_status="ready" if not blocking_reasons else "blocked",
            import_attempted=True,
            checked_modules=module_names,
            imported_modules=imported,
            missing_modules=missing,
            module_errors=module_errors,
            python_executable=python_executable,
            import_subprocess_invoked=True,
            import_subprocess_returncode=completed.returncode,
            blocking_reasons=_dedupe(blocking_reasons),
        )

    imported: list[str] = []
    missing: list[str] = []
    module_errors: dict[str, str] = {}
    for module_name in module_names:
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            missing.append(module_name)
            module_errors[module_name] = _format_import_error(exc)
        else:
            imported.append(module_name)

    return UnitreeSdk2ImportReadiness(
        readiness_status="ready" if not missing else "blocked",
        import_attempted=True,
        checked_modules=module_names,
        imported_modules=tuple(imported),
        missing_modules=tuple(missing),
        module_errors=module_errors,
        python_executable=python_executable,
        blocking_reasons=(
            () if not missing else ("unitree_sdk2_python_modules_missing",)
        ),
    )


def build_unitree_mujoco_python_command(
    *,
    checkout_root: str | Path,
    python_executable: str | None = None,
) -> tuple[str, ...]:
    """Build the Unitree MuJoCo Python simulator command without running it."""

    root = Path(checkout_root).expanduser()
    return (
        python_executable or sys.executable,
        str(root / "simulate_python/unitree_mujoco.py"),
    )


def launch_unitree_mujoco_python_simulator(
    *,
    checkout_root: str | Path | None,
    opt_in: bool,
    wait_seconds: float = 5.0,
    python_executable: str | None = None,
) -> UnitreeMujocoProcessLaunchResult:
    """Start then terminate Unitree MuJoCo Python simulator when explicitly gated.

    This crosses the simulator process boundary only. It does not import Unitree
    SDK2 in this process, does not send commands, and never claims physical
    execution.
    """

    readiness = build_unitree_mujoco_environment_readiness(
        checkout_root=checkout_root,
    )
    root = Path(checkout_root).expanduser() if checkout_root else None
    command = (
        build_unitree_mujoco_python_command(
            checkout_root=root,
            python_executable=python_executable,
        )
        if root is not None
        else ()
    )
    blocking_reasons = list(readiness.blocking_reasons)
    if not opt_in or not _truthy_env(UNITREE_MUJOCO_PROCESS_SMOKE_ENV):
        blocking_reasons.append("unitree_mujoco_process_opt_in_not_enabled")
    if readiness.readiness_status != "ready":
        blocking_reasons.append("unitree_mujoco_environment_not_ready")
    if blocking_reasons:
        return UnitreeMujocoProcessLaunchResult(
            launch_status="blocked",
            checkout_root_name=readiness.checkout_root_name,
            command=command,
            cwd_name="simulate_python" if root is not None else None,
            blocking_reasons=_dedupe(blocking_reasons),
        )

    assert root is not None
    process: subprocess.Popen[str] | None = None
    cwd = root / "simulate_python"
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        time.sleep(max(wait_seconds, 0.0))
        returncode = process.poll()
        if returncode is not None:
            _, stderr = process.communicate(timeout=1)
            return UnitreeMujocoProcessLaunchResult(
                launch_status="exited_early",
                checkout_root_name=root.name,
                command=command,
                cwd_name=cwd.name,
                process_started=True,
                process_pid=process.pid,
                mujoco_started=False,
                terminated_by_smoke=False,
                returncode=returncode,
                stderr_excerpt=_excerpt(stderr or ""),
                blocking_reasons=("unitree_mujoco_process_exited_early",),
            )

        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        return UnitreeMujocoProcessLaunchResult(
            launch_status="started",
            checkout_root_name=root.name,
            command=command,
            cwd_name=cwd.name,
            process_started=True,
            process_pid=process.pid,
            mujoco_started=True,
            terminated_by_smoke=True,
            returncode=process.returncode,
        )
    except subprocess.TimeoutExpired:
        return UnitreeMujocoProcessLaunchResult(
            launch_status="timeout",
            checkout_root_name=root.name,
            command=command,
            cwd_name=cwd.name,
            process_started=process is not None,
            process_pid=process.pid if process is not None else None,
            mujoco_started=False,
            terminated_by_smoke=False,
            blocking_reasons=("unitree_mujoco_process_timeout",),
        )
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=1)
            except Exception:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except Exception:
                    pass


def observe_unitree_mujoco_scene_motion(
    *,
    checkout_root: str | Path | None,
    opt_in: bool,
    python_executable: str | None = None,
    step_count: int = 200,
    motion_threshold: float = 1e-6,
    timeout_s: float = 15.0,
) -> UnitreeMujocoSceneObservationResult:
    """Load and step the Unitree Go2 scene in headless MuJoCo when gated.

    This observes a MuJoCo scene load and passive physics motion only. It does
    not start Unitree's viewer process, does not send any Unitree command, and
    does not claim MissionOS dispatch control.
    """

    readiness = build_unitree_mujoco_environment_readiness(
        checkout_root=checkout_root,
    )
    root = Path(checkout_root).expanduser() if checkout_root else None
    command = (
        (python_executable or sys.executable, "-c", _SCENE_OBSERVATION_SUBPROCESS_CODE)
        if root is not None
        else ()
    )
    blocking_reasons = list(readiness.blocking_reasons)
    if not opt_in or not _truthy_env(UNITREE_MUJOCO_SCENE_OBSERVATION_SMOKE_ENV):
        blocking_reasons.append("unitree_mujoco_scene_observation_opt_in_not_enabled")
    if readiness.readiness_status != "ready":
        blocking_reasons.append("unitree_mujoco_environment_not_ready")
    if blocking_reasons:
        return UnitreeMujocoSceneObservationResult(
            observation_status="blocked",
            checkout_root_name=readiness.checkout_root_name,
            command=command,
            cwd_name="simulate_python" if root is not None else None,
            blocking_reasons=_dedupe(blocking_reasons),
        )

    assert root is not None
    cwd = root / "simulate_python"
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(
                {
                    "step_count": max(step_count, 0),
                    "motion_threshold": motion_threshold,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return UnitreeMujocoSceneObservationResult(
            observation_status="failed",
            checkout_root_name=root.name,
            command=command,
            cwd_name=cwd.name,
            blocking_reasons=("unitree_mujoco_scene_observation_timeout",),
            stderr_excerpt=_excerpt(exc.stderr or ""),
        )
    except OSError as exc:
        return UnitreeMujocoSceneObservationResult(
            observation_status="failed",
            checkout_root_name=root.name,
            command=command,
            cwd_name=cwd.name,
            blocking_reasons=("unitree_mujoco_scene_observation_subprocess_failed",),
            stderr_excerpt=_format_import_error(exc),
        )

    if completed.returncode != 0:
        return UnitreeMujocoSceneObservationResult(
            observation_status="failed",
            checkout_root_name=root.name,
            command=command,
            cwd_name=cwd.name,
            subprocess_returncode=completed.returncode,
            blocking_reasons=("unitree_mujoco_scene_observation_failed",),
            stderr_excerpt=_excerpt(completed.stderr or completed.stdout or ""),
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return UnitreeMujocoSceneObservationResult(
            observation_status="failed",
            checkout_root_name=root.name,
            command=command,
            cwd_name=cwd.name,
            subprocess_returncode=completed.returncode,
            blocking_reasons=("unitree_mujoco_scene_observation_non_json",),
            stderr_excerpt=_format_import_error(exc),
        )

    scene_loaded = payload.get("scene_loaded_observed") is True
    robot_motion = payload.get("robot_motion_observed") is True
    return UnitreeMujocoSceneObservationResult(
        observation_status=(
            "observed" if scene_loaded and robot_motion else "failed"
        ),
        checkout_root_name=root.name,
        command=command,
        cwd_name=cwd.name,
        scene_loaded_observed=scene_loaded,
        robot_motion_observed=robot_motion,
        motion_source=(
            "uncommanded_physics_step"
            if payload.get("motion_source") == "uncommanded_physics_step"
            else "none"
        ),
        qpos_delta_norm=(
            float(payload["qpos_delta_norm"])
            if isinstance(payload.get("qpos_delta_norm"), int | float)
            else None
        ),
        step_count=int(payload.get("step_count") or 0),
        model_nq=int(payload["model_nq"]) if isinstance(payload.get("model_nq"), int) else None,
        model_nv=int(payload["model_nv"]) if isinstance(payload.get("model_nv"), int) else None,
        model_nu=int(payload["model_nu"]) if isinstance(payload.get("model_nu"), int) else None,
        config_robot=(
            str(payload["config_robot"])
            if payload.get("config_robot") is not None
            else None
        ),
        config_domain_id=(
            int(payload["config_domain_id"])
            if isinstance(payload.get("config_domain_id"), int)
            else None
        ),
        config_interface=(
            str(payload["config_interface"])
            if payload.get("config_interface") is not None
            else None
        ),
        subprocess_returncode=completed.returncode,
        blocking_reasons=(
            ()
            if scene_loaded and robot_motion
            else ("unitree_mujoco_scene_or_motion_not_observed",)
        ),
    )


def observe_unitree_mujoco_sdk2_bridge_state(
    *,
    checkout_root: str | Path | None,
    opt_in: bool,
    python_executable: str | None = None,
    channel_interface: str = "config",
    step_count: int = 400,
    motion_threshold: float = 1e-6,
    timeout_s: float = 20.0,
) -> UnitreeMujocoSdk2BridgeObservationResult:
    """Start the headless Unitree SDK2 bridge and observe lowstate when gated.

    This crosses the SDK2/DDS state-observation boundary only. It never sends a
    command and it treats auto interface selection as safe only inside an
    isolated network namespace.
    """

    readiness = build_unitree_mujoco_environment_readiness(
        checkout_root=checkout_root,
    )
    root = Path(checkout_root).expanduser() if checkout_root else None
    command = (
        (
            python_executable or sys.executable,
            "-c",
            _SDK2_BRIDGE_OBSERVATION_SUBPROCESS_CODE,
        )
        if root is not None
        else ()
    )
    channel_mode: Literal["config", "auto", "explicit"]
    normalized_channel = channel_interface.strip() if channel_interface else "config"
    if normalized_channel in {"config", "auto"}:
        channel_mode = normalized_channel  # type: ignore[assignment]
    else:
        channel_mode = "explicit"

    blocking_reasons = list(readiness.blocking_reasons)
    if not opt_in or not _truthy_env(UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SMOKE_ENV):
        blocking_reasons.append("unitree_mujoco_sdk2_bridge_observation_opt_in_not_enabled")
    if readiness.readiness_status != "ready":
        blocking_reasons.append("unitree_mujoco_environment_not_ready")
    if blocking_reasons:
        return UnitreeMujocoSdk2BridgeObservationResult(
            bridge_observation_status="blocked",
            checkout_root_name=readiness.checkout_root_name,
            command=command,
            cwd_name="simulate_python" if root is not None else None,
            channel_interface_mode=channel_mode,
            channel_interface_used=(
                readiness.config_interface if channel_mode == "config" else None
            ),
            blocking_reasons=_dedupe(blocking_reasons),
        )

    assert root is not None
    cwd = root / "simulate_python"
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(
                {
                    "step_count": max(step_count, 0),
                    "motion_threshold": motion_threshold,
                    "channel_interface": normalized_channel,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return UnitreeMujocoSdk2BridgeObservationResult(
            bridge_observation_status="failed",
            checkout_root_name=root.name,
            command=command,
            cwd_name=cwd.name,
            channel_interface_mode=channel_mode,
            blocking_reasons=("unitree_mujoco_sdk2_bridge_observation_timeout",),
            stderr_excerpt=_excerpt(exc.stderr or ""),
        )
    except OSError as exc:
        return UnitreeMujocoSdk2BridgeObservationResult(
            bridge_observation_status="failed",
            checkout_root_name=root.name,
            command=command,
            cwd_name=cwd.name,
            channel_interface_mode=channel_mode,
            blocking_reasons=(
                "unitree_mujoco_sdk2_bridge_observation_subprocess_failed",
            ),
            stderr_excerpt=_format_import_error(exc),
        )

    if completed.returncode != 0:
        return UnitreeMujocoSdk2BridgeObservationResult(
            bridge_observation_status="failed",
            checkout_root_name=root.name,
            command=command,
            cwd_name=cwd.name,
            channel_interface_mode=channel_mode,
            subprocess_returncode=completed.returncode,
            blocking_reasons=("unitree_mujoco_sdk2_bridge_observation_failed",),
            stderr_excerpt=_excerpt(completed.stderr or completed.stdout or ""),
        )

    try:
        payload = _loads_json_or_last_json_line(completed.stdout)
    except Exception as exc:
        return UnitreeMujocoSdk2BridgeObservationResult(
            bridge_observation_status="failed",
            checkout_root_name=root.name,
            command=command,
            cwd_name=cwd.name,
            channel_interface_mode=channel_mode,
            subprocess_returncode=completed.returncode,
            blocking_reasons=("unitree_mujoco_sdk2_bridge_observation_non_json",),
            stderr_excerpt=_format_import_error(exc),
        )

    status = str(payload.get("bridge_observation_status") or "failed")
    bridge_status: Literal["observed", "blocked", "failed"] = (
        "observed" if status == "observed" else "blocked" if status == "blocked" else "failed"
    )
    network_interfaces = tuple(str(value) for value in payload.get("network_interfaces", ()))
    active_network_interfaces = tuple(
        str(value) for value in payload.get("active_network_interfaces", ())
    )
    blocking = tuple(str(value) for value in payload.get("blocking_reasons", ()))
    lowstate_sample = payload.get("lowstate_sample")
    return UnitreeMujocoSdk2BridgeObservationResult(
        bridge_observation_status=bridge_status,
        checkout_root_name=root.name,
        command=command,
        cwd_name=cwd.name,
        scene_loaded_observed=payload.get("scene_loaded_observed") is True,
        robot_motion_observed=payload.get("robot_motion_observed") is True,
        sdk2_bridge_started=payload.get("sdk2_bridge_started") is True,
        lowstate_observed=payload.get("lowstate_observed") is True,
        lowstate_sample=(
            {
                str(key): value
                for key, value in lowstate_sample.items()
            }
            if isinstance(lowstate_sample, dict)
            else None
        ),
        qpos_delta_norm=(
            float(payload["qpos_delta_norm"])
            if isinstance(payload.get("qpos_delta_norm"), int | float)
            else None
        ),
        step_count=int(payload.get("step_count") or 0),
        model_nq=int(payload["model_nq"]) if isinstance(payload.get("model_nq"), int) else None,
        model_nv=int(payload["model_nv"]) if isinstance(payload.get("model_nv"), int) else None,
        model_nu=int(payload["model_nu"]) if isinstance(payload.get("model_nu"), int) else None,
        config_robot=(
            str(payload["config_robot"])
            if payload.get("config_robot") is not None
            else None
        ),
        config_domain_id=(
            int(payload["config_domain_id"])
            if isinstance(payload.get("config_domain_id"), int)
            else None
        ),
        config_interface=(
            str(payload["config_interface"])
            if payload.get("config_interface") is not None
            else None
        ),
        network_interfaces=network_interfaces,
        active_network_interfaces=active_network_interfaces,
        network_isolated=payload.get("network_isolated") is True,
        channel_interface_mode=channel_mode,
        channel_interface_used=(
            str(payload["channel_interface_used"])
            if payload.get("channel_interface_used") is not None
            else None
        ),
        subprocess_returncode=completed.returncode,
        blocking_reasons=blocking,
    )


def probe_unitree_mujoco_sport_client_bounded_move(
    *,
    checkout_root: str | Path | None,
    opt_in: bool,
    python_executable: str | None = None,
    channel_interface: str = "config",
    vx_mps: float = 0.05,
    vy_mps: float = 0.0,
    vyaw_rps: float = 0.0,
    step_count: int = 400,
    warmup_steps: int = 50,
    motion_threshold: float = 1e-6,
    client_timeout_s: float = 1.0,
    timeout_s: float = 25.0,
) -> UnitreeMujocoSportClientProbeResult:
    """Probe whether the official Go2 SportClient can carry a bounded move.

    This is still a simulator-only capability probe. It starts the headless
    Unitree SDK2 bridge, attempts a tiny high-level SportClient move, and never
    publishes raw ``rt/lowcmd`` from MissionOS.
    """

    readiness = build_unitree_mujoco_environment_readiness(
        checkout_root=checkout_root,
    )
    root = Path(checkout_root).expanduser() if checkout_root else None
    command = (
        (
            python_executable or sys.executable,
            "-c",
            _SPORT_CLIENT_PROBE_SUBPROCESS_CODE,
        )
        if root is not None
        else ()
    )
    channel_mode: Literal["config", "auto", "explicit"]
    normalized_channel = channel_interface.strip() if channel_interface else "config"
    if normalized_channel in {"config", "auto"}:
        channel_mode = normalized_channel  # type: ignore[assignment]
    else:
        channel_mode = "explicit"

    blocking_reasons = list(readiness.blocking_reasons)
    if not opt_in or not _truthy_env(UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SMOKE_ENV):
        blocking_reasons.append("unitree_mujoco_sport_client_probe_opt_in_not_enabled")
    if readiness.readiness_status != "ready":
        blocking_reasons.append("unitree_mujoco_environment_not_ready")
    if blocking_reasons:
        return UnitreeMujocoSportClientProbeResult(
            probe_status="blocked",
            checkout_root_name=readiness.checkout_root_name,
            command=command,
            cwd_name="simulate_python" if root is not None else None,
            channel_interface_mode=channel_mode,
            channel_interface_used=(
                readiness.config_interface if channel_mode == "config" else None
            ),
            blocking_reasons=_dedupe(blocking_reasons),
        )

    assert root is not None
    cwd = root / "simulate_python"
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(
                {
                    "step_count": max(step_count, 0),
                    "warmup_steps": max(warmup_steps, 0),
                    "motion_threshold": motion_threshold,
                    "channel_interface": normalized_channel,
                    "vx_mps": vx_mps,
                    "vy_mps": vy_mps,
                    "vyaw_rps": vyaw_rps,
                    "client_timeout_s": client_timeout_s,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return UnitreeMujocoSportClientProbeResult(
            probe_status="failed",
            checkout_root_name=root.name,
            command=command,
            cwd_name=cwd.name,
            channel_interface_mode=channel_mode,
            blocking_reasons=("unitree_mujoco_sport_client_probe_timeout",),
            stderr_excerpt=_excerpt(exc.stderr or ""),
        )
    except OSError as exc:
        return UnitreeMujocoSportClientProbeResult(
            probe_status="failed",
            checkout_root_name=root.name,
            command=command,
            cwd_name=cwd.name,
            channel_interface_mode=channel_mode,
            blocking_reasons=("unitree_mujoco_sport_client_probe_subprocess_failed",),
            stderr_excerpt=_format_import_error(exc),
        )

    if completed.returncode != 0:
        return UnitreeMujocoSportClientProbeResult(
            probe_status="failed",
            checkout_root_name=root.name,
            command=command,
            cwd_name=cwd.name,
            channel_interface_mode=channel_mode,
            subprocess_returncode=completed.returncode,
            blocking_reasons=("unitree_mujoco_sport_client_probe_failed",),
            stderr_excerpt=_excerpt(completed.stderr or completed.stdout or ""),
        )

    try:
        payload = _loads_json_or_last_json_line(completed.stdout)
    except Exception as exc:
        return UnitreeMujocoSportClientProbeResult(
            probe_status="failed",
            checkout_root_name=root.name,
            command=command,
            cwd_name=cwd.name,
            channel_interface_mode=channel_mode,
            subprocess_returncode=completed.returncode,
            blocking_reasons=("unitree_mujoco_sport_client_probe_non_json",),
            stderr_excerpt=_format_import_error(exc),
        )

    status = str(payload.get("probe_status") or "failed")
    probe_status: Literal["observed", "blocked", "failed"] = (
        "observed" if status == "observed" else "blocked" if status == "blocked" else "failed"
    )
    network_interfaces = tuple(str(value) for value in payload.get("network_interfaces", ()))
    active_network_interfaces = tuple(
        str(value) for value in payload.get("active_network_interfaces", ())
    )
    blocking = tuple(str(value) for value in payload.get("blocking_reasons", ()))
    lowstate_sample = payload.get("lowstate_sample")
    bounded_command = payload.get("bounded_command")
    move_code = payload.get("sport_move_return_code")
    stop_code = payload.get("stop_move_return_code")
    sport_api_code = payload.get("sport_server_api_code")
    robot_state_api_code = payload.get("robot_state_server_api_code")
    robot_state_service_list_code = payload.get("robot_state_service_list_code")
    robot_state_services = payload.get("robot_state_services")
    return UnitreeMujocoSportClientProbeResult(
        probe_status=probe_status,
        checkout_root_name=root.name,
        command=command,
        cwd_name=cwd.name,
        scene_loaded_observed=payload.get("scene_loaded_observed") is True,
        robot_motion_observed=payload.get("robot_motion_observed") is True,
        sdk2_bridge_started=payload.get("sdk2_bridge_started") is True,
        lowstate_observed=payload.get("lowstate_observed") is True,
        lowstate_sample=(
            {str(key): value for key, value in lowstate_sample.items()}
            if isinstance(lowstate_sample, dict)
            else None
        ),
        sport_client_initialized=payload.get("sport_client_initialized") is True,
        sport_move_call_attempted=payload.get("sport_move_call_attempted") is True,
        sport_move_return_code=int(move_code) if isinstance(move_code, int) else None,
        stop_move_return_code=int(stop_code) if isinstance(stop_code, int) else None,
        sport_client_error=(
            str(payload["sport_client_error"])
            if payload.get("sport_client_error") is not None
            else None
        ),
        sport_server_api_code=(
            int(sport_api_code) if isinstance(sport_api_code, int) else None
        ),
        sport_server_api_version=(
            str(payload["sport_server_api_version"])
            if payload.get("sport_server_api_version") is not None
            else None
        ),
        sport_client_api_version=(
            str(payload["sport_client_api_version"])
            if payload.get("sport_client_api_version") is not None
            else None
        ),
        sport_service_available=payload.get("sport_service_available") is True,
        robot_state_server_api_code=(
            int(robot_state_api_code)
            if isinstance(robot_state_api_code, int)
            else None
        ),
        robot_state_server_api_version=(
            str(payload["robot_state_server_api_version"])
            if payload.get("robot_state_server_api_version") is not None
            else None
        ),
        robot_state_service_list_code=(
            int(robot_state_service_list_code)
            if isinstance(robot_state_service_list_code, int)
            else None
        ),
        robot_state_services=(
            tuple(
                {
                    str(key): value
                    for key, value in item.items()
                }
                for item in robot_state_services
                if isinstance(item, dict)
            )
            if isinstance(robot_state_services, list)
            else ()
        ),
        robot_state_client_error=(
            str(payload["robot_state_client_error"])
            if payload.get("robot_state_client_error") is not None
            else None
        ),
        bounded_command=(
            {str(key): float(value) for key, value in bounded_command.items()}
            if isinstance(bounded_command, dict)
            else {}
        ),
        qpos_delta_norm=(
            float(payload["qpos_delta_norm"])
            if isinstance(payload.get("qpos_delta_norm"), int | float)
            else None
        ),
        step_count=int(payload.get("step_count") or 0),
        warmup_steps=int(payload.get("warmup_steps") or 0),
        model_nq=int(payload["model_nq"]) if isinstance(payload.get("model_nq"), int) else None,
        model_nv=int(payload["model_nv"]) if isinstance(payload.get("model_nv"), int) else None,
        model_nu=int(payload["model_nu"]) if isinstance(payload.get("model_nu"), int) else None,
        config_robot=(
            str(payload["config_robot"])
            if payload.get("config_robot") is not None
            else None
        ),
        config_domain_id=(
            int(payload["config_domain_id"])
            if isinstance(payload.get("config_domain_id"), int)
            else None
        ),
        config_interface=(
            str(payload["config_interface"])
            if payload.get("config_interface") is not None
            else None
        ),
        network_interfaces=network_interfaces,
        active_network_interfaces=active_network_interfaces,
        network_isolated=payload.get("network_isolated") is True,
        channel_interface_mode=channel_mode,
        channel_interface_used=(
            str(payload["channel_interface_used"])
            if payload.get("channel_interface_used") is not None
            else None
        ),
        dispatch_request_sent=payload.get("sport_move_return_code") == 0,
        subprocess_returncode=completed.returncode,
        blocking_reasons=blocking,
    )


__all__ = [
    "UNITREE_MUJOCO_ENVIRONMENT_READINESS_SCHEMA_VERSION",
    "UNITREE_MUJOCO_PROCESS_LAUNCH_SCHEMA_VERSION",
    "UNITREE_MUJOCO_PROCESS_SMOKE_ENV",
    "UNITREE_MUJOCO_PYTHON_EXECUTABLE_ENV",
    "UNITREE_MUJOCO_READINESS_SMOKE_ENV",
    "UNITREE_MUJOCO_ROOT_ENV",
    "UNITREE_MUJOCO_SCENE_OBSERVATION_SCHEMA_VERSION",
    "UNITREE_MUJOCO_SCENE_OBSERVATION_SMOKE_ENV",
    "UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SCHEMA_VERSION",
    "UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SMOKE_ENV",
    "UNITREE_MUJOCO_SDK2_CHANNEL_INTERFACE_ENV",
    "UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SCHEMA_VERSION",
    "UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SMOKE_ENV",
    "UNITREE_SDK2_IMPORT_READINESS_SCHEMA_VERSION",
    "UNITREE_SDK2_IMPORT_SMOKE_ENV",
    "UNITREE_SDK2_PYTHON_EXECUTABLE_ENV",
    "UnitreeMujocoEnvironmentReadiness",
    "UnitreeMujocoProcessLaunchResult",
    "UnitreeMujocoSceneObservationResult",
    "UnitreeMujocoSdk2BridgeObservationResult",
    "UnitreeMujocoSportClientProbeResult",
    "UnitreeSdk2ImportReadiness",
    "build_unitree_mujoco_python_command",
    "build_unitree_mujoco_environment_readiness",
    "build_unitree_sdk2_import_readiness",
    "launch_unitree_mujoco_python_simulator",
    "observe_unitree_mujoco_sdk2_bridge_state",
    "observe_unitree_mujoco_scene_motion",
    "probe_unitree_mujoco_sport_client_bounded_move",
]
