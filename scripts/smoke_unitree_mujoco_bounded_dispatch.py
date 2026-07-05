#!/usr/bin/env python3
"""Opt-in bounded Unitree MuJoCo dispatch smoke.

This smoke sends only ``bounded_local_move`` through an operator-provided bridge
command. It never implements raw Unitree motor control in MissionOS.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os

from src.runtime.unitree_hardware_adapter import (
    UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV,
    UnitreeBoundedLocalMove,
    UnitreeHardwareAdapter,
    UnitreeHardwareAdapterConfig,
)
from src.runtime.unitree_mujoco_dispatch_bridge import (
    UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE_ENV,
    UNITREE_MUJOCO_BRIDGE_COMMAND_ENV,
    UnitreeMujocoBridgeCommandClient,
)

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def main() -> int:
    if not _truthy_env(UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE_ENV):
        print(
            json.dumps(
                {
                    "smoke": "unitree_mujoco_bounded_dispatch",
                    "ran": False,
                    "reason": (
                        f"{UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE_ENV} is not "
                        "set to 1; no bounded dispatch was sent."
                    ),
                    "bridge_command_present": bool(
                        os.environ.get(UNITREE_MUJOCO_BRIDGE_COMMAND_ENV, "").strip()
                    ),
                    "dispatch_request_sent": False,
                    "physical_execution_invoked": False,
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    client = UnitreeMujocoBridgeCommandClient()
    adapter = UnitreeHardwareAdapter(
        config=UnitreeHardwareAdapterConfig(
            missionos_action_ref="missionos_plan_unitree_bounded_local_move",
            local_move=UnitreeBoundedLocalMove(forward_m=0.25, lateral_m=0.0),
            operator_approval_ref="smoke_operator_approval_unitree_dispatch_001",
            approval_actor="smoke-operator",
            approval_timestamp=datetime.now(timezone.utc),
            opt_in=True,
        ),
        client=client,
    )
    evidence = adapter.dispatch_approved_action()
    summary = {
        "smoke": "unitree_mujoco_bounded_dispatch",
        "ran": True,
        "adapter_runtime_gate_enabled": _truthy_env(
            UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV
        ),
        "bridge_command_present": bool(
            os.environ.get(UNITREE_MUJOCO_BRIDGE_COMMAND_ENV, "").strip()
        ),
        "bridge_responses": client.collect_responses(),
        "evidence": evidence.model_dump(mode="json"),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))

    if evidence.physical_execution_invoked:
        raise SystemExit("bounded dispatch smoke claimed physical execution")
    if "special_motion_not_invoked" not in evidence.unproven_claims:
        raise SystemExit("bounded dispatch smoke did not preserve special-motion block")
    return 0 if evidence.dispatch_request_sent else 2


if __name__ == "__main__":
    raise SystemExit(main())
