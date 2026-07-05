#!/usr/bin/env python3
"""Opt-in readiness smoke for a local Unitree MuJoCo checkout.

This script does not import Unitree SDK2, does not start MuJoCo, and does not
send any command. When explicitly gated, it validates that ``UNITREE_MUJOCO_ROOT``
points at a Go2 Python simulator checkout with loopback simulation settings.
"""

from __future__ import annotations

import json
import os

from src.runtime.unitree_mujoco_environment import (
    UNITREE_MUJOCO_READINESS_SMOKE_ENV,
    UNITREE_MUJOCO_ROOT_ENV,
    build_unitree_mujoco_environment_readiness,
)

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _skip() -> int:
    print(
        json.dumps(
            {
                "smoke": "unitree_mujoco_environment_readiness",
                "ran": False,
                "reason": (
                    f"{UNITREE_MUJOCO_READINESS_SMOKE_ENV} is not set to 1; "
                    "no Unitree MuJoCo checkout was inspected, no simulator was "
                    "started, and no hardware was touched."
                ),
                "unitree_sdk2_imported": False,
                "mujoco_started": False,
                "dispatch_request_sent": False,
                "physical_execution_invoked": False,
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    if not _truthy_env(UNITREE_MUJOCO_READINESS_SMOKE_ENV):
        return _skip()

    readiness = build_unitree_mujoco_environment_readiness(
        checkout_root=os.environ.get(UNITREE_MUJOCO_ROOT_ENV),
    )
    summary = {
        "smoke": "unitree_mujoco_environment_readiness",
        "ran": True,
        **readiness.model_dump(mode="json"),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))

    if readiness.unitree_sdk2_imported:
        raise SystemExit("readiness smoke unexpectedly imported Unitree SDK2")
    if readiness.mujoco_started:
        raise SystemExit("readiness smoke unexpectedly started MuJoCo")
    if readiness.dispatch_request_sent:
        raise SystemExit("readiness smoke unexpectedly sent a dispatch request")
    if readiness.physical_execution_invoked:
        raise SystemExit("readiness smoke unexpectedly claimed physical execution")
    if readiness.readiness_status != "ready":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
