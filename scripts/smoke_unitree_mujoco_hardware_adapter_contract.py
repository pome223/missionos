#!/usr/bin/env python3
"""Blocked-path smoke for the bounded Unitree MuJoCo adapter contract.

This smoke deliberately does not import Unitree SDK2 and does not start MuJoCo.
It exercises the production adapter preflight with the runtime gate off and
proves that a Unitree dispatch blocks before any command can be sent.
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


def main() -> int:
    os.environ.pop(UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV, None)
    adapter = UnitreeHardwareAdapter(
        config=UnitreeHardwareAdapterConfig(
            missionos_action_ref="missionos_plan_unitree_bounded_local_move",
            local_move=UnitreeBoundedLocalMove(forward_m=0.25, lateral_m=0.0),
            operator_approval_ref="smoke_operator_approval_unitree_001",
            approval_actor="smoke-operator",
            approval_timestamp=datetime.now(timezone.utc),
            opt_in=False,
        ),
        client=None,
    )
    capabilities = adapter.capabilities()
    preflight = adapter.preflight_check()
    evidence = adapter.dispatch_approved_action()
    summary = {
        "smoke": "unitree_mujoco_hardware_adapter_contract",
        "ran": True,
        "unitree_sdk2_imported": False,
        "mujoco_started": False,
        "unitree_client_present": False,
        "execution_mode": evidence.execution_mode.value,
        "adapter_kind": evidence.adapter_kind.value,
        "allowed_actions": [action.value for action in capabilities.allowed_actions],
        "blocked_actions": [action.value for action in capabilities.blocked_actions],
        "preflight_status": preflight.preflight_status.value,
        "dispatch_status": evidence.dispatch_status.value,
        "dispatch_request_sent": evidence.dispatch_request_sent,
        "physical_execution_invoked": evidence.physical_execution_invoked,
        "completion_claimed": evidence.completion_claimed,
        "completion_scope": evidence.completion_scope,
        "blocking_reasons": list(evidence.blocking_reasons),
        "unproven_claims": list(evidence.unproven_claims),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))

    if evidence.dispatch_request_sent:
        raise SystemExit("Unitree smoke unexpectedly sent a dispatch request")
    if evidence.physical_execution_invoked:
        raise SystemExit("Unitree smoke unexpectedly claimed physical execution")
    if evidence.completion_claimed:
        raise SystemExit("Unitree smoke unexpectedly claimed completion")
    if "special_motion" not in summary["blocked_actions"]:
        raise SystemExit("Unitree smoke did not block special_motion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
