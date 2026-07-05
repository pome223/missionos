#!/usr/bin/env python3
"""Bounded JSON bridge from MissionOS to Unitree Go2 SportClient.

This command is intentionally narrow: it only tries the official high-level
SportClient surface and never publishes ``rt/lowcmd`` or raw motor commands.
"""

from __future__ import annotations

import json
import math
import os
import sys
from typing import Any

from src.runtime.unitree_hardware_adapter import UnitreeBoundedLocalMove
from src.runtime.unitree_mujoco_dispatch_bridge import (
    UNITREE_MUJOCO_SPORT_CLIENT_BRIDGE_ENV,
)
from src.runtime.unitree_mujoco_environment import (
    UNITREE_MUJOCO_ROOT_ENV,
    UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SMOKE_ENV,
    UNITREE_MUJOCO_SDK2_CHANNEL_INTERFACE_ENV,
    UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SMOKE_ENV,
    observe_unitree_mujoco_sdk2_bridge_state,
    probe_unitree_mujoco_sport_client_bounded_move,
)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_ALLOWED_ACTIONS = {"bounded_local_move", "read_state", "read_progress", "hold", "safe_stop"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _base_response() -> dict[str, Any]:
    return {
        "schema_version": "missionos_unitree_mujoco_bridge_response.v1",
        "ack_source": "unitree_mujoco_sport_client_bridge",
        "physical_execution_invoked": False,
        "raw_lowcmd_published": False,
        "raw_motor_invoked": False,
        "raw_velocity_invoked": False,
        "special_motion_invoked": False,
    }


def _blocked(*reasons: str, ack_status: str = "rejected") -> dict[str, Any]:
    return {
        **_base_response(),
        "ack_status": ack_status,
        "runtime_progress_observed": False,
        "completion_observed": False,
        "unitree_status": "blocked",
        "blocking_reasons": tuple(reason for reason in reasons if reason),
    }


def _read_request() -> dict[str, Any]:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _validate_request(request: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if request.get("schema_version") != "missionos_unitree_mujoco_bridge_request.v1":
        reasons.append("bridge_request_schema_version_invalid")
    action = request.get("action")
    if action not in _ALLOWED_ACTIONS:
        reasons.append("bridge_request_action_not_supported")
    for field in (
        "physical_execution_invoked",
        "raw_motor_allowed",
        "raw_velocity_allowed",
        "special_motion_allowed",
    ):
        if request.get(field) is not False:
            reasons.append(f"{field}_must_be_false")
    return reasons


def _bounded_velocity(payload: dict[str, Any]) -> tuple[float, float, float]:
    move = UnitreeBoundedLocalMove.model_validate(payload)
    duration = move.duration_s if move.duration_s > 0 else 1.0
    max_speed = min(move.max_speed_mps, 0.3)
    vx = max(-max_speed, min(max_speed, move.forward_m / duration))
    vy = max(-max_speed, min(max_speed, move.lateral_m / duration))
    yaw_limit = 0.5
    vyaw = max(-yaw_limit, min(yaw_limit, move.yaw_rad / duration))
    if not all(math.isfinite(value) for value in (vx, vy, vyaw)):
        raise ValueError("bounded velocity is not finite")
    return vx, vy, vyaw


def _probe_move(request: dict[str, Any]) -> dict[str, Any]:
    if not _truthy_env(UNITREE_MUJOCO_SPORT_CLIENT_BRIDGE_ENV):
        return _blocked("unitree_mujoco_sport_client_bridge_opt_in_not_enabled")
    payload = request.get("payload")
    if not isinstance(payload, dict):
        return _blocked("bridge_request_payload_missing")
    try:
        vx_mps, vy_mps, vyaw_rps = _bounded_velocity(payload)
    except Exception as exc:
        return _blocked("bridge_request_payload_invalid", str(exc))

    os.environ[UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SMOKE_ENV] = "1"
    result = probe_unitree_mujoco_sport_client_bounded_move(
        checkout_root=os.environ.get(UNITREE_MUJOCO_ROOT_ENV),
        opt_in=True,
        channel_interface=(
            os.environ.get(UNITREE_MUJOCO_SDK2_CHANNEL_INTERFACE_ENV) or "config"
        ),
        vx_mps=vx_mps,
        vy_mps=vy_mps,
        vyaw_rps=vyaw_rps,
    )
    if result.probe_status == "observed" and result.sport_move_return_code == 0:
        return {
            **_base_response(),
            "ack_status": "accepted",
            "unitree_status": "move_request_accepted",
            "runtime_progress_observed": False,
            "completion_observed": False,
            "sport_move_return_code": result.sport_move_return_code,
            "sport_server_api_code": result.sport_server_api_code,
            "sport_service_available": result.sport_service_available,
            "robot_state_service_list_code": result.robot_state_service_list_code,
            "robot_state_services": result.robot_state_services,
            "bounded_command": result.bounded_command,
            "scene_loaded_observed": result.scene_loaded_observed,
            "lowstate_observed": result.lowstate_observed,
            "robot_motion_observed": result.robot_motion_observed,
            "blocking_reasons": (),
            "raw_logs_ref": "unitree_mujoco_sport_client_probe",
        }
    return {
        **_blocked(*result.blocking_reasons),
        "sport_move_return_code": result.sport_move_return_code,
        "sport_server_api_code": result.sport_server_api_code,
        "sport_service_available": result.sport_service_available,
        "robot_state_service_list_code": result.robot_state_service_list_code,
        "robot_state_services": result.robot_state_services,
        "bounded_command": result.bounded_command,
        "scene_loaded_observed": result.scene_loaded_observed,
        "lowstate_observed": result.lowstate_observed,
        "robot_motion_observed": result.robot_motion_observed,
        "unitree_status": "move_request_rejected",
        "raw_logs_ref": "unitree_mujoco_sport_client_probe",
    }


def _observe_state() -> dict[str, Any]:
    if not _truthy_env(UNITREE_MUJOCO_SPORT_CLIENT_BRIDGE_ENV):
        return _blocked("unitree_mujoco_sport_client_bridge_opt_in_not_enabled")
    os.environ[UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SMOKE_ENV] = "1"
    result = observe_unitree_mujoco_sdk2_bridge_state(
        checkout_root=os.environ.get(UNITREE_MUJOCO_ROOT_ENV),
        opt_in=True,
        channel_interface=(
            os.environ.get(UNITREE_MUJOCO_SDK2_CHANNEL_INTERFACE_ENV) or "config"
        ),
    )
    return {
        **_base_response(),
        "ack_status": "accepted" if result.lowstate_observed else "rejected",
        "pose_observed": result.lowstate_observed,
        "base_stable": result.lowstate_observed,
        "lowstate_observed": result.lowstate_observed,
        "lowstate_sample": result.lowstate_sample,
        "scene_loaded_observed": result.scene_loaded_observed,
        "robot_motion_observed": result.robot_motion_observed,
        "blocking_reasons": result.blocking_reasons,
    }


def _observe_progress() -> dict[str, Any]:
    return {
        **_base_response(),
        "ack_status": "accepted",
        "runtime_progress_observed": False,
        "completion_observed": False,
        "unitree_status": "progress_not_verified",
        "blocking_reasons": ("unitree_mujoco_progress_not_verified",),
    }


def main() -> int:
    request = _read_request()
    blocking_reasons = _validate_request(request)
    if blocking_reasons:
        print(json.dumps(_blocked(*blocking_reasons), ensure_ascii=True, sort_keys=True))
        return 0

    action = str(request.get("action"))
    if action == "bounded_local_move":
        response = _probe_move(request)
    elif action == "read_state":
        response = _observe_state()
    elif action == "read_progress":
        response = _observe_progress()
    elif action in {"hold", "safe_stop"}:
        response = _blocked("unitree_mujoco_sport_client_stop_not_verified")
    else:
        response = _blocked("bridge_request_action_not_supported")

    print(json.dumps(response, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
