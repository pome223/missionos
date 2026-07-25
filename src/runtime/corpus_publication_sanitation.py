"""Shared publication-sanitation rules for conformance corpora.

Every Action Feasibility conformance corpus is a publication artifact. A case
must never carry a credential, a private task identifier, a local absolute path,
an operator identity, a prompt or model response, a serial device path, or a
hardware individual identifier.

The rules live here so each backend corpus enforces the same boundary. A backend
that needs a stricter rule may add one; it may not relax a rule defined here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


CASE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,95}$")

PRIVATE_TASK_ID_PATTERN = re.compile(r"\btask_[0-9a-f]{8,}\b", re.IGNORECASE)

SECRET_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|"
    r"(?:api[_-]?key|authorization|credential|secret|token)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)

ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?:file://|/(?:Users|private|tmp|home|var/folders)/|[A-Za-z]:\\\\)",
    re.IGNORECASE,
)

# Serial and device endpoints. A bench corpus records the link *class*
# ("serial" or "loopback"), never the endpoint that identifies one workstation
# or one board. POSIX device nodes are matched anywhere in the value; a Windows
# COM endpoint is matched as a whole value or in its `\\.\COMn` device form, so
# ordinary prose is not flagged.
DEVICE_PATH_PATTERN = re.compile(
    r"(?:/dev/[A-Za-z0-9._-]+|\\\\\.\\COM\d+|^COM\d+$)",
    re.IGNORECASE,
)

FORBIDDEN_KEYS = frozenset(
    {
        # credentials and private storage
        "api_key",
        "artifact_dir",
        "artifact_path",
        "authorization",
        "credential",
        "database_path",
        "db_path",
        "secret",
        "task_id",
        "token",
        # operator and session identity
        "approval_actor",
        "attesting_operator_id",
        "operator_name",
        "owner_session_id",
        "owner_user_id",
        # bench session evidence that depicts one physical workstation
        "bench_photo_evidence_ref",
        # model interaction text
        "prompt",
        "prompt_text",
        "response_text",
        # hardware individual identity and serial endpoints
        "autopilot_uid",
        "board_id",
        "board_serial",
        "device_path",
        "hardware_uid",
        "serial_device",
        "serial_number",
        "serial_port",
        "usb_serial",
    }
)


def publication_findings(value: Any, *, path: str = "$") -> list[str]:
    """Return JSON paths that violate the publication boundary."""

    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            next_path = f"{path}.{key}"
            if str(key).lower() in FORBIDDEN_KEYS:
                findings.append(next_path)
            findings.extend(publication_findings(item, path=next_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(publication_findings(item, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        if (
            PRIVATE_TASK_ID_PATTERN.search(value)
            or SECRET_PATTERN.search(value)
            or ABSOLUTE_PATH_PATTERN.search(value)
            or DEVICE_PATH_PATTERN.search(value)
        ):
            findings.append(path)
    return findings
