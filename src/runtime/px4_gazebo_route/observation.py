"""Read-only observation normalization for the PX4/Gazebo route runtime.

Functions in this module transform already-observed text, poses, and samples.
They do not invoke PX4, Gazebo, Docker, or any command/dispatch boundary.
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping


def listener_field(output: str, field: str) -> float | None:
    match = re.search(rf"\b{re.escape(field)}:\s*(-?\d+(?:\.\d+)?)", output)
    return float(match.group(1)) if match else None


def listener_bool(output: str, field: str) -> bool | None:
    match = re.search(rf"\b{re.escape(field)}:\s*(True|False)", output)
    if not match:
        return None
    return match.group(1) == "True"


def battery_status_from_listener_output(
    output: str,
    *,
    returncode: int,
) -> dict[str, Any]:
    remaining = listener_field(output, "remaining")
    warning = listener_field(output, "warning")
    observed = returncode == 0 and bool(output) and remaining is not None
    return {
        "battery_status_observed": observed,
        "battery_state_source": "px4-listener:battery_status",
        "battery_remaining_percent": (
            round(float(remaining) * 100.0, 3) if remaining is not None else None
        ),
        "battery_warning": int(warning) if warning is not None else None,
        "battery_voltage_v": (
            round(float(voltage), 3)
            if (voltage := listener_field(output, "voltage_v")) is not None
            else None
        ),
        "battery_current_a": (
            round(float(current), 3)
            if (current := listener_field(output, "current_a")) is not None
            else None
        ),
        "battery_connected": listener_bool(output, "connected"),
    }


def pose_rows(
    *,
    pickup_pose: dict[str, float],
    climb_samples: list[dict[str, float]],
    route_pose: dict[str, float],
    completed_pose: dict[str, float],
    landing_samples: list[dict[str, float]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [{"phase": "pickup", "sample": pickup_pose}]
    rows.extend(
        {"phase": "climb", "sample_index": index, "sample": sample}
        for index, sample in enumerate(climb_samples)
    )
    rows.append({"phase": "route", "sample": route_pose})
    rows.extend(
        {"phase": "landing", "sample_index": index, "sample": sample}
        for index, sample in enumerate(landing_samples)
    )
    rows.append({"phase": "completed", "sample": completed_pose})
    return rows


def pose_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def terminal_pose_record(
    *,
    phase: str,
    pose: Mapping[str, Any] | None,
    sample_index: int | None = None,
    progress_m: float | None = None,
    source: str = "gazebo_pose_sample",
) -> dict[str, Any]:
    observed = bool(pose)
    pose_data = pose or {}
    return {
        "schema_version": "missionos_terminal_pose.v1",
        "phase": phase,
        "observed": observed,
        "source": source if observed else "",
        "sample_index": sample_index,
        "x_m": pose_float(
            pose_data.get("x", pose_data.get("x_m", pose_data.get("local_x_m")))
        ),
        "y_m": pose_float(
            pose_data.get("y", pose_data.get("y_m", pose_data.get("local_y_m")))
        ),
        "z_m": pose_float(
            pose_data.get("z", pose_data.get("z_m", pose_data.get("local_z_m")))
        ),
        "progress_m": progress_m,
    }


def terminal_pose_summary_fields(
    *,
    route_pose: Mapping[str, Any] | None,
    completed_pose: Mapping[str, Any] | None,
    landing_samples: list[dict[str, float]],
    route_terminal_progress_m: float | None,
    route_terminal_local_ned_pose: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    landing_pose = landing_samples[-1] if landing_samples else None
    return {
        "route_terminal_pose": terminal_pose_record(
            phase="route",
            pose=route_pose,
            progress_m=route_terminal_progress_m,
        ),
        "route_terminal_local_ned_pose": terminal_pose_record(
            phase="route",
            pose=route_terminal_local_ned_pose,
            progress_m=route_terminal_progress_m,
            source="px4_local_position_ned",
        ),
        "route_terminal_progress_m": route_terminal_progress_m,
        "landing_terminal_pose": terminal_pose_record(
            phase="landing",
            pose=landing_pose,
            sample_index=(len(landing_samples) - 1 if landing_samples else None),
            progress_m=route_terminal_progress_m,
        ),
        "completed_terminal_pose": terminal_pose_record(
            phase="completed",
            pose=completed_pose,
            progress_m=route_terminal_progress_m,
        ),
    }


def contact_topic_candidates(
    topic_list: str,
    *,
    configured_topic: str,
) -> list[str]:
    topic_lines = [line.strip() for line in topic_list.splitlines() if line.strip()]
    return [
        line
        for line in topic_lines
        if line == configured_topic
        or ("mission_designer_collision_obstacle" in line and "contact" in line.lower())
        or "collision_obstacle_contact_sensor" in line
    ]


def select_contact_topic(topic_list: str, *, configured_topic: str) -> str:
    candidates = contact_topic_candidates(
        topic_list,
        configured_topic=configured_topic,
    )
    return (
        configured_topic
        if configured_topic in candidates
        else (candidates[0] if candidates else configured_topic)
    )


def contact_topic_observation(
    *,
    topic_list: str,
    sample_text: str,
    sample_returncode: int,
    configured_topic: str,
) -> dict[str, Any]:
    candidates = contact_topic_candidates(
        topic_list,
        configured_topic=configured_topic,
    )
    selected_topic = select_contact_topic(
        topic_list,
        configured_topic=configured_topic,
    )
    topic_advertised = bool(candidates)
    contact_event_observed = bool(sample_text)
    return {
        "source": "gz_topic_contact_sensor_read_only",
        "topic": selected_topic,
        "configured_topic": configured_topic,
        "candidate_topics": candidates,
        "topic_advertised": topic_advertised,
        "contact_topic_observed": topic_advertised,
        "contact_event_observed": contact_event_observed,
        "contact_sample_observed": contact_event_observed,
        "contact_sample_stdout_tail": sample_text[-500:],
        "contact_sample_returncode": sample_returncode,
        "read_only_observer": True,
        "route_blocking_observed": False,
        "incident_observed": False,
        "traffic_conflict_verified": False,
        "task_status_mutated": False,
        "delivery_completion_claimed": False,
    }


def xy_pairs_match(
    first: list[float] | None,
    second: list[float] | None,
    *,
    tolerance: float = 1e-6,
) -> bool:
    if first is None or second is None or len(first) != 2 or len(second) != 2:
        return False
    return all(
        abs(float(first[index]) - float(second[index])) <= tolerance
        for index in range(2)
    )


def distance_to_segment_xy(
    *,
    point_xy: tuple[float, float],
    start_xy: tuple[float, float],
    end_xy: tuple[float, float],
) -> float:
    px, py = point_xy
    sx, sy = start_xy
    ex, ey = end_xy
    dx = ex - sx
    dy = ey - sy
    length_squared = dx * dx + dy * dy
    if length_squared <= 0.0:
        return math.hypot(px - sx, py - sy)
    t = max(0.0, min(1.0, ((px - sx) * dx + (py - sy) * dy) / length_squared))
    nearest_x = sx + t * dx
    nearest_y = sy + t * dy
    return math.hypot(px - nearest_x, py - nearest_y)


def point_to_segment_distance_m(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    return distance_to_segment_xy(point_xy=point, start_xy=start, end_xy=end)


__all__ = [
    "battery_status_from_listener_output",
    "contact_topic_candidates",
    "contact_topic_observation",
    "distance_to_segment_xy",
    "listener_bool",
    "listener_field",
    "point_to_segment_distance_m",
    "pose_float",
    "pose_rows",
    "select_contact_topic",
    "terminal_pose_record",
    "terminal_pose_summary_fields",
    "xy_pairs_match",
]
