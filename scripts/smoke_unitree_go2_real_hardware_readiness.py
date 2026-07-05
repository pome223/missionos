#!/usr/bin/env python3
"""Opt-in read-only readiness smoke for real Unitree Go2 SportClient services."""

from __future__ import annotations

import json
import os

from src.runtime.unitree_go2_real_hardware import (
    UNITREE_GO2_DOMAIN_ID_ENV,
    UNITREE_GO2_NETWORK_INTERFACE_ENV,
    UNITREE_GO2_PYTHON_EXECUTABLE_ENV,
    UNITREE_GO2_REAL_HARDWARE_READINESS_SMOKE_ENV,
    build_unitree_go2_real_hardware_readiness,
)

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def main() -> int:
    if not _truthy_env(UNITREE_GO2_REAL_HARDWARE_READINESS_SMOKE_ENV):
        print(
            json.dumps(
                {
                    "smoke": "unitree_go2_real_hardware_readiness",
                    "ran": False,
                    "reason": (
                        f"{UNITREE_GO2_REAL_HARDWARE_READINESS_SMOKE_ENV} is "
                        "not set to 1; Unitree SDK2 was not initialized and no "
                        "robot service request was sent."
                    ),
                    "channel_initialized": False,
                    "sport_client_initialized": False,
                    "dispatch_request_sent": False,
                    "sport_move_call_attempted": False,
                    "physical_execution_invoked": False,
                    "raw_lowcmd_published": False,
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    readiness = build_unitree_go2_real_hardware_readiness(
        opt_in=True,
        network_interface=os.environ.get(UNITREE_GO2_NETWORK_INTERFACE_ENV),
        domain_id=_int_env(UNITREE_GO2_DOMAIN_ID_ENV, 0),
        python_executable=os.environ.get(UNITREE_GO2_PYTHON_EXECUTABLE_ENV) or None,
    )
    summary = {
        "smoke": "unitree_go2_real_hardware_readiness",
        "ran": True,
        **readiness.model_dump(mode="json"),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if readiness.readiness_status == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
