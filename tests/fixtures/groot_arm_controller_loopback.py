"""Subprocess fixture for the GR00T arm controller command boundary."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sys

import numpy as np
from missionos_core import canonical_sha256

from src.runtime.groot_arm_controller_bridge import (
    GROOT_ARM_CONTROLLER_RECEIPT_SCHEMA,
    _array_sha256,
)


def main() -> int:
    request = json.load(sys.stdin)
    left = np.asarray(request["left_arm_rad"], dtype=np.float64)
    right = np.asarray(request["right_arm_rad"], dtype=np.float64)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    remaining_horizon = max(
        (
            datetime.fromisoformat(
                request["handoff_deadline"].replace("Z", "+00:00")
            )
            - datetime.fromisoformat(now.replace("Z", "+00:00"))
        ).total_seconds(),
        0.0,
    )
    response = {
        "schema_version": GROOT_ARM_CONTROLLER_RECEIPT_SCHEMA,
        "request_id": request["request_id"],
        "admitted_chunk_sha256": request["admitted_chunk_sha256"],
        "transformed_chunk_sha256": request["transformed_chunk_sha256"],
        "transformation_sha256": request["transformation_sha256"],
        "controller_policy_sha256": request["controller_policy_sha256"],
        "controller_configuration_sha256": request[
            "controller_configuration_sha256"
        ],
        "proposal_received_at": request["proposal_received_at"],
        "handoff_deadline": request["handoff_deadline"],
        "remaining_valid_horizon_seconds_at_handoff": (
            remaining_horizon
        ),
        "handoff_observed_at": now,
        "handoff_state_age_seconds": 0.001,
        "handoff_left_arm_rad": list(left[0]),
        "handoff_right_arm_rad": list(right[0]),
        "controller_ack_observed": True,
        "progress_samples_observed": int(left.shape[0]),
        "progress_samples": [
            {"sample_index": index, "sim_time": index * 0.05}
            for index in range(left.shape[0])
        ],
        "progress_observed_at": now,
        "progress_source_sha256": canonical_sha256(
            {
                "samples": [
                    {"sample_index": index, "sim_time": index * 0.05}
                    for index in range(left.shape[0])
                ]
            }
        ),
        "applied_left_arm_rad": left.tolist(),
        "applied_right_arm_rad": right.tolist(),
        "applied_command_sha256": _array_sha256(left, right),
        "effect_observed_at": now,
        "effect_left_arm_rad": list(left[-1]),
        "effect_right_arm_rad": list(right[-1]),
        "effect_source_id": "loopback-simulator-qpos:final",
        "effect_source_sha256": _array_sha256(left[-1:], right[-1:]),
        "hand_command_applied": False,
        "envelope_violation_observed": False,
        "safe_stop_requested": False,
        "safe_stop_ack_observed": False,
        "safe_stop_effect_observed": False,
        "stop_detection_latency_seconds": None,
        "stop_effect_latency_seconds": None,
        "remaining_chunk_horizon_seconds": None,
        "execution_scope": "sim",
        "physical_execution_invoked": False,
        "task_completion_claimed": False,
    }
    json.dump(response, sys.stdout, ensure_ascii=True, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
