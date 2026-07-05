#!/usr/bin/env python3
"""Opt-in Unitree MuJoCo SportClient bounded-move capability probe."""

from __future__ import annotations

import json
import os

from src.runtime.unitree_mujoco_environment import (
    UNITREE_MUJOCO_PYTHON_EXECUTABLE_ENV,
    UNITREE_MUJOCO_ROOT_ENV,
    UNITREE_MUJOCO_SDK2_CHANNEL_INTERFACE_ENV,
    UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SMOKE_ENV,
    probe_unitree_mujoco_sport_client_bounded_move,
)

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def main() -> int:
    if not _truthy_env(UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SMOKE_ENV):
        print(
            json.dumps(
                {
                    "smoke": "unitree_mujoco_sport_client_probe",
                    "ran": False,
                    "reason": (
                        f"{UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SMOKE_ENV} is "
                        "not set to 1; SportClient was not initialized and no "
                        "bounded move was attempted."
                    ),
                    "sport_client_initialized": False,
                    "sport_move_call_attempted": False,
                    "dispatch_request_sent": False,
                    "command_ack_observed": False,
                    "physical_execution_invoked": False,
                    "raw_lowcmd_published": False,
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    result = probe_unitree_mujoco_sport_client_bounded_move(
        checkout_root=os.environ.get(UNITREE_MUJOCO_ROOT_ENV),
        opt_in=True,
        python_executable=(
            os.environ.get(UNITREE_MUJOCO_PYTHON_EXECUTABLE_ENV) or None
        ),
        channel_interface=(
            os.environ.get(UNITREE_MUJOCO_SDK2_CHANNEL_INTERFACE_ENV) or "config"
        ),
    )
    summary = {
        "smoke": "unitree_mujoco_sport_client_probe",
        "ran": True,
        **result.model_dump(mode="json"),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result.probe_status in {"observed", "blocked"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
