"""Narrow host-to-container request writer for the active PX4 SITL runner."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any


def queue_px4_active_runner_recovery_request(
    *,
    container_path: str,
    request_payload: Mapping[str, Any],
    container_name: str | None = None,
) -> dict[str, Any]:
    """Atomically queue one bounded request in the opted-in SITL container."""

    import scripts.smoke_px4_gazebo_sitl_mission_upload as upload_smoke

    path = str(container_path or "").strip()
    if not path.startswith("/tmp/missionos_auto_operator_recovery_request_"):
        raise ValueError("operator recovery request path must be the AUTO /tmp path")
    if "\x00" in path or "\n" in path or "\r" in path:
        raise ValueError("operator recovery request path contains invalid characters")
    target_container = str(container_name or upload_smoke.CONTAINER_NAME)
    payload_text = json.dumps(dict(request_payload), sort_keys=True)
    upload_smoke._run(
        [
            "docker",
            "exec",
            "-i",
            target_container,
            "sh",
            "-c",
            'cat > "$1.tmp" && mv "$1.tmp" "$1"',
            "sh",
            path,
        ],
        input_text=payload_text,
        timeout=10,
    )
    return {
        "request_status": "queued",
        "container_name": target_container,
        "container_path": path,
        "bytes_written": len(payload_text.encode("utf-8")),
    }


__all__ = ["queue_px4_active_runner_recovery_request"]
