#!/usr/bin/env python3
"""Run the governed GR00T boundary through a live RoboCasa controller.

By default, the policy is an explicit hold-position fixture. Supplying a ZMQ
endpoint opts into one real GR00T policy request. In both modes the controller,
safe-stop exercise, application, and qpos readback use the pinned RoboCasa
runtime.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import uuid

import numpy as np
from missionos_core import HardwareExecutionMode, canonical_sha256

from scripts.smoke_groot_robocasa_arm_controller import (
    LIVE_SMOKE_ENV,
    PYTHON_ENV,
    build_groot_robocasa_arm_policy,
    build_validated_groot_robocasa_envelope,
)
from src.runtime.groot_arm_controller_bridge import (
    GROOT_ARM_CONTROLLER_SMOKE_ENV,
    GrootArmControllerProcessClient,
    GrootArmExecutionContext,
    groot_robocasa_controller_configuration_sha256,
)
from src.runtime.groot_governed_e2e import (
    GROOT_ARM_HOLD_INSTRUCTION,
    GROOT_ARM_HOLD_INSTRUCTION_ID,
    GrootGovernedApproval,
    GrootSafeStopEvidenceSummary,
    build_groot_governed_preparation,
    run_groot_governed_e2e,
)
from src.runtime.groot_policy_client import (
    GrootPolicyBinding,
    GrootZmqPolicyTransport,
    build_groot_sim_freshness_policy,
)


class _HoldPositionPolicyFixture:
    """Return the measured arm state as a 16-sample proposal."""

    def get_action(self, payload):
        left = np.repeat(
            np.asarray(payload["state.left_arm"], dtype=np.float32),
            16,
            axis=0,
        )
        right = np.repeat(
            np.asarray(payload["state.right_arm"], dtype=np.float32),
            16,
            axis=0,
        )
        hand = np.zeros((16, 6), dtype=np.float32)
        return {
            "action.left_arm": left,
            "action.left_hand": hand.copy(),
            "action.right_arm": right,
            "action.right_hand": hand.copy(),
        }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        help="optional path for the sanitized MissionOS-owned JSON report",
    )
    parser.add_argument(
        "--approval-ref",
        required=True,
        help="explicit operator approval ref for this single run",
    )
    parser.add_argument(
        "--policy-endpoint",
        help="opt-in GR00T ZMQ endpoint, for example tcp://127.0.0.1:5567",
    )
    parser.add_argument(
        "--policy-timeout-ms",
        type=int,
        default=5000,
        help="real policy request deadline; ignored by the fixture",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if os.environ.get(LIVE_SMOKE_ENV, "").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise SystemExit(f"{LIVE_SMOKE_ENV}=1 is required")
    python = os.environ.get(PYTHON_ENV, "").strip()
    if not python:
        raise SystemExit(
            f"{PYTHON_ENV} must point to the pinned Python 3.11 env"
        )
    if not args.approval_ref.startswith("groot-e2e-approval:"):
        raise SystemExit(
            "--approval-ref must start with groot-e2e-approval:"
        )

    controller_script = (
        Path(__file__).resolve().parent / "groot_robocasa_arm_controller.py"
    )
    controller = GrootArmControllerProcessClient(
        command=(python, str(controller_script)),
        timeout_seconds=30,
    )
    os.environ[GROOT_ARM_CONTROLLER_SMOKE_ENV] = "1"
    try:
        exercise = dict(controller.exercise_safe_stop())
        exercise_invocation = (
            controller.collect_runtime_invocation_evidence()[-1]
        )
        controller_digest = (
            groot_robocasa_controller_configuration_sha256()
        )
        safety_digest = canonical_sha256(
            {
                "mechanism": "controller_position_hold",
                "hold_steps": 8,
                "maximum_step_delta_rad": 0.01,
                "hand_part_controllers_enabled": False,
            }
        )
        (
            envelope_policy,
            envelope_validation,
            stop_receipt,
            stop_validation,
        ) = build_validated_groot_robocasa_envelope(
            exercise,
            invocation_digest=str(
                exercise_invocation["response_sha256"]
            ),
            controller_digest=controller_digest,
            safety_digest=safety_digest,
        )
        policy = build_groot_robocasa_arm_policy(envelope_policy)

        now = datetime.now(timezone.utc)
        freshness_policy = build_groot_sim_freshness_policy()
        suffix = f"sim-{uuid.uuid4().hex[:12]}"
        preparation = build_groot_governed_preparation(
            run_ref=f"groot-e2e-run:{suffix}",
            instruction_allowlist_id=GROOT_ARM_HOLD_INSTRUCTION_ID,
            controller_configuration_sha256=controller_digest,
            safety_configuration_sha256=safety_digest,
            envelope_policy_sha256=(
                envelope_policy.binding.policy_sha256
            ),
            freshness_policy_sha256=freshness_policy.policy_sha256,
            transformation_id="arm-only-projection-identity-v1",
            prepared_at=now,
            expires_at=now + timedelta(seconds=30),
        )
        approval = GrootGovernedApproval(
            run_ref=preparation.run_ref,
            instruction_ref=preparation.instruction_ref,
            preparation_ref=preparation.preparation_ref,
            preparation_sha256=preparation.preparation_sha256,
            operator_approval_ref=args.approval_ref,
            approved_at=now.isoformat().replace("+00:00", "Z"),
            expires_at=(now + timedelta(seconds=30))
            .isoformat()
            .replace("+00:00", "Z"),
        )
        policy_observation = dict(controller.observe_policy_input())
        observed_at = datetime.fromisoformat(
            str(policy_observation["observed_at"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        policy_payload = {
            "annotation.human.action.task_description": [
                GROOT_ARM_HOLD_INSTRUCTION
            ],
            "state.left_arm": np.asarray(
                policy_observation["state.left_arm"],
                dtype=np.float64,
            )[None, :],
            "state.left_hand": np.asarray(
                policy_observation["state.left_hand"],
                dtype=np.float64,
            )[None, :],
            "state.right_arm": np.asarray(
                policy_observation["state.right_arm"],
                dtype=np.float64,
            )[None, :],
            "state.right_hand": np.asarray(
                policy_observation["state.right_hand"],
                dtype=np.float64,
            )[None, :],
            "video.ego_view": np.asarray(
                policy_observation["video.ego_view"],
                dtype=np.uint8,
            )[None, ...],
        }
        policy_transport = (
            GrootZmqPolicyTransport(
                endpoint=args.policy_endpoint,
                timeout_ms=args.policy_timeout_ms,
            )
            if args.policy_endpoint
            else _HoldPositionPolicyFixture()
        )
        binding = GrootPolicyBinding(
            instruction_ref=preparation.instruction_ref,
            preparation_sha256=preparation.preparation_sha256,
            observed_at=observed_at.isoformat().replace("+00:00", "Z"),
            freshness_deadline=(
                observed_at
                + timedelta(
                    seconds=(
                        freshness_policy.maximum_observation_age_seconds
                    )
                )
            )
            .isoformat()
            .replace("+00:00", "Z"),
            freshness_policy=freshness_policy,
        )
        context = GrootArmExecutionContext(
            instruction_ref=preparation.instruction_ref,
            approval_ref=approval.operator_approval_ref,
            expected_preparation_sha256=preparation.preparation_sha256,
            controller_configuration_sha256=controller_digest,
            safety_configuration_sha256=safety_digest,
            policy=policy,
            envelope_validation=envelope_validation,
        )
        with tempfile.TemporaryDirectory(
            prefix="missionos-groot-e2e-authority-"
        ) as authority_root:
            report = run_groot_governed_e2e(
                preparation=preparation,
                approval=approval,
                policy_payload=policy_payload,
                policy_binding=binding,
                policy_transport=policy_transport,
                policy_clock=lambda: datetime.now(timezone.utc),
                controller=controller,
                controller_context=context,
                safe_stop_summary=GrootSafeStopEvidenceSummary(
                    receipt_ref=stop_receipt.receipt_id,
                    receipt_sha256=canonical_sha256(
                        stop_receipt.to_dict()
                    ),
                    request_observed=True,
                    ack_observed=True,
                    effect_observed=True,
                    capability_evidenced=(
                        stop_validation.stop_capability_evidenced
                    ),
                    execution_scope=HardwareExecutionMode.SIM,
                ),
                authority_state_path=(
                    Path(authority_root) / "authority.json"
                ),
                evaluated_at=None,
                limitations=tuple(
                    [
                        "sim scope only",
                        "fixed-base arm-only execution profile",
                        "no balance coupling or whole-body safety claim",
                        "hand part controllers disabled",
                        "shared host process clock and MuJoCo state",
                        "no semantic task completion",
                        (
                            "handoff continuity reports a maximum absolute "
                            "joint delta; it does not identify the semantic "
                            "root cause of a rejection"
                        ),
                        (
                            "request observation, dispatch state, and first "
                            "action vectors are not published in this summary"
                        ),
                    ]
                    + (
                        [
                            (
                                "one live GR00T policy request over the "
                                "configured ZMQ endpoint"
                            ),
                            "hold-current-pose instruction is not a semantic "
                            "task-success test",
                        ]
                        if args.policy_endpoint
                        else [
                            "policy service is a deterministic fixture",
                            "no live GR00T inference",
                        ]
                    )
                ),
            )
    finally:
        controller.close()

    encoded = json.dumps(
        report,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{encoded}\n", encoding="utf-8")
    print(encoded)
    return (
        0
        if report["status"]
        == (
            "verified_execution_evidence"
            if args.policy_endpoint
            else "verified_fixture_execution_evidence"
        )
        and report["semantic_completion"]["claimed"] is False
        and report["physical_execution_invoked"] is False
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
