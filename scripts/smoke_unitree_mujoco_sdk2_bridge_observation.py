#!/usr/bin/env python3
"""Opt-in headless Unitree SDK2 bridge state observation smoke."""

from __future__ import annotations

import json
import os

from src.runtime.unitree_mujoco_environment import (
    UNITREE_MUJOCO_PYTHON_EXECUTABLE_ENV,
    UNITREE_MUJOCO_ROOT_ENV,
    UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SMOKE_ENV,
    UNITREE_MUJOCO_SDK2_CHANNEL_INTERFACE_ENV,
    observe_unitree_mujoco_sdk2_bridge_state,
)

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def main() -> int:
    if not _truthy_env(UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SMOKE_ENV):
        print(
            json.dumps(
                {
                    "smoke": "unitree_mujoco_sdk2_bridge_observation",
                    "ran": False,
                    "reason": (
                        f"{UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SMOKE_ENV} "
                        "is not set to 1; Unitree SDK2 bridge was not started "
                        "and no dispatch was sent."
                    ),
                    "sdk2_bridge_started": False,
                    "lowstate_observed": False,
                    "dispatch_request_sent": False,
                    "physical_execution_invoked": False,
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    result = observe_unitree_mujoco_sdk2_bridge_state(
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
        "smoke": "unitree_mujoco_sdk2_bridge_observation",
        "ran": True,
        **result.model_dump(mode="json"),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result.bridge_observation_status == "observed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
