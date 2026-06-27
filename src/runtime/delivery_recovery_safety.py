"""Shared safety checks for delivery recovery artifacts."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "action",
        "actuator",
        "actuator_command",
        "actuator_execution_allowed",
        "attitude_setpoint",
        "cmd_vel",
        "command",
        "command_long",
        "command_payload",
        "command_payload_allowed",
        "dispatch",
        "entity_mutation",
        "execute",
        "gazebo_entity_mutation",
        "gazebo_mutation",
        "hardware_target_allowed",
        "landing_command",
        "live_execution_allowed",
        "mav_cmd",
        "mavlink",
        "mavlink_command",
        "mission_item",
        "mission_upload",
        "physical_execution_invoked",
        "position_setpoint",
        "raw_payload",
        "real_hardware_target",
        "return_to_home_command",
        "ros_action",
        "ros_topic",
        "setpoint",
        "setpoint_stream",
        "thrust",
        "torque",
        "velocity_command",
    }
)

_FORBIDDEN_TEXT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bCOMMAND_LONG\b",
        r"\bMAV_CMD\b",
        r"\bMAVLink\b",
        r"\bMISSION_ITEM(?:_INT)?\b",
        r"\b/cmd_vel\b",
        r"\b/fmu/",
        r"\bsetpoint\b",
        r"\bactuator\b",
        r"\bros\s+action\b",
        r"\bmission\s+upload\b",
        r"\bport\s*[:=]?\s*\d{2,5}\b",
        r"\budp:",
        r"\btcp:",
    )
)


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


_FORBIDDEN_COMMAND_KEYS_NORMALIZED = frozenset(
    _normalize_key(key) for key in _FORBIDDEN_COMMAND_KEYS
)


def command_like_paths(value: Any, *, root: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, sub in value.items():
            key_text = str(key)
            path = f"{root}.{key_text}" if root else key_text
            if _normalize_key(key_text) in _FORBIDDEN_COMMAND_KEYS_NORMALIZED:
                findings.append(path)
            findings.extend(command_like_paths(sub, root=path))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            path = f"{root}.{index}" if root else str(index)
            findings.extend(command_like_paths(item, root=path))
    elif isinstance(value, str):
        for pattern in _FORBIDDEN_TEXT_PATTERNS:
            if pattern.search(value):
                findings.append(root or "<value>")
                break
    return findings


def raise_for_command_like_payload(
    value: Any,
    *,
    root: str,
    error_type: type[Exception] = ValueError,
    prefix: str = "delivery recovery artifact refused command-like payload",
) -> None:
    findings = command_like_paths(value, root=root)
    if findings:
        raise error_type(f"{prefix}: " + ", ".join(sorted(set(findings))))
