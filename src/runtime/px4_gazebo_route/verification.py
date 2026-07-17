"""Pure readback verification for the PX4/Gazebo route runtime.

These functions evaluate existing observations. They do not dispatch commands,
mint authority, mutate task state, or infer mission completion.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Mapping


MATERIALIZED_APPLICATION_STATUSES = {"applied", "applied_with_approximations"}


def application_status_is_materialized(status: Any) -> bool:
    return status in MATERIALIZED_APPLICATION_STATUSES


def wind_readback_status(
    output: str,
    *,
    expected_x: float,
    expected_y: float,
) -> dict[str, Any]:
    publish_status_match = re.search(r"__BC_WIND_PUBLISH_STATUS=(\d+)", output)
    readback_status_match = re.search(r"__BC_WIND_READBACK_STATUS=(\d+)", output)
    wind_message = output.split("__BC_WIND_PUBLISH_STATUS=", 1)[0].strip()
    vector_match = re.search(
        r"linear_velocity\s*\{(?P<body>.*?)\}",
        wind_message,
        flags=re.DOTALL,
    )
    parsed_x = None
    parsed_y = None
    if vector_match:
        body = vector_match.group("body")
        x_match = re.search(r"\bx:\s*([-+0-9.eE]+)", body)
        y_match = re.search(r"\by:\s*([-+0-9.eE]+)", body)
        if x_match:
            parsed_x = float(x_match.group(1))
        if y_match:
            parsed_y = float(y_match.group(1))
        if parsed_x is None:
            parsed_x = 0.0
        if parsed_y is None:
            parsed_y = 0.0
    vector_matches = (
        parsed_x is not None
        and parsed_y is not None
        and math.isclose(parsed_x, expected_x, rel_tol=1e-6, abs_tol=1e-6)
        and math.isclose(parsed_y, expected_y, rel_tol=1e-6, abs_tol=1e-6)
    )
    return {
        "readback_observed": vector_matches,
        "readback_source": "gz_topic_echo",
        "readback_publish_status": (
            int(publish_status_match.group(1)) if publish_status_match else None
        ),
        "readback_status": (
            int(readback_status_match.group(1)) if readback_status_match else None
        ),
        "readback_wind_vector_x_mps": parsed_x,
        "readback_wind_vector_y_mps": parsed_y,
        "readback_message_sha256": (
            hashlib.sha256(wind_message.encode("utf-8")).hexdigest()
            if wind_message
            else None
        ),
    }


def px4_param_set_applied(result: Mapping[str, Any]) -> bool:
    output = (
        f"{result.get('stdout_tail') or ''}\n{result.get('stderr_tail') or ''}".lower()
    )
    return result.get("returncode") == 0 and "not found" not in output


def px4_param_value_matches(
    snapshot: Mapping[str, Any],
    expected_value: float,
    *,
    abs_tol: float = 1e-4,
) -> bool:
    if snapshot.get("returncode") != 0:
        return False
    value = snapshot.get("value")
    return value is not None and math.isclose(
        float(value),
        float(expected_value),
        abs_tol=abs_tol,
    )


def route_corridor_obstacle_application_source_check(
    spawn_application: Mapping[str, Any],
) -> tuple[bool, list[str]]:
    observed = spawn_application.get("observed") or {}
    reasons: list[str] = []
    if (
        spawn_application.get("schema_version")
        != "gazebo_route_corridor_obstacle_spawn_application.v1"
    ):
        reasons.append("gazebo_route_corridor_obstacle_spawn_schema_missing")
    if (
        spawn_application.get("application_id")
        != "gazebo_route_corridor_obstacle_spawn_application:mission_designer_collision_obstacle"
    ):
        reasons.append("gazebo_route_corridor_obstacle_spawn_ref_missing")
    if spawn_application.get("application_status") != "applied":
        reasons.append("gazebo_route_corridor_obstacle_spawn_not_applied")
    if observed.get("observed") is not True:
        reasons.append("gazebo_route_corridor_obstacle_spawn_not_observed")
    if observed.get("world_sdf_hash_match") is not True:
        reasons.append("gazebo_route_corridor_obstacle_world_sdf_hash_not_verified")
    if observed.get("model_materialized") is not True:
        reasons.append("gazebo_route_corridor_obstacle_model_not_materialized")
    if observed.get("collision_geometry_materialized") is not True:
        reasons.append("gazebo_route_corridor_obstacle_collision_not_materialized")
    if observed.get("trajectory_follower_materialized") is not True:
        reasons.append("gazebo_route_corridor_obstacle_motion_not_materialized")
    return not reasons, reasons


__all__ = [
    "MATERIALIZED_APPLICATION_STATUSES",
    "application_status_is_materialized",
    "px4_param_set_applied",
    "px4_param_value_matches",
    "route_corridor_obstacle_application_source_check",
    "wind_readback_status",
]
