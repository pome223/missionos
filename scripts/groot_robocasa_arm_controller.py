#!/usr/bin/env python3
"""Opt-in GR1 ArmsOnly RoboCasa controller subprocess.

This process owns the 20 Hz loop. It disables both hand part controllers and
verifies that their actuator controls did not change. It reports simulator
facts only and never claims physical execution or semantic task completion.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from time import monotonic
from typing import Any

import numpy as np

from missionos_core import canonical_sha256


GROOT_ARM_CONTROLLER_RECEIPT_SCHEMA = (
    "missionos_groot_arm_controller_receipt.v1"
)
GROOT_ARM_CONTROLLER_REQUEST_SCHEMA = (
    "missionos_groot_arm_controller_request.v1"
)
GROOT_ROBOCASA_REVISION = "4840e671596f93ca03651524b9f72ffb1aadfeff"
GROOT_ROBOSUITE_REVISION = "75a4c9f4d242c1b7fe7c7fc247b564ec5d8550a2"
GROOT_ROBOCASA_ENVIRONMENT_ID = (
    "robocasa_gr1_arms_only_fourier_hands/"
    "Tabletop_GR1ArmsOnlyFourierHands_Env"
)
GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD = 0.25
GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS = 0.05
GROOT_FIXED_BASE_ARM_ONLY_PROFILE = "fixed_base_arm_only"


def _array_sha256(left: np.ndarray, right: np.ndarray) -> str:
    import hashlib

    return canonical_sha256(
        {
            "dtype": "float64",
            "left_shape": list(left.shape),
            "left_sha256": hashlib.sha256(
                left.astype(np.float64, copy=False).tobytes(order="C")
            ).hexdigest(),
            "right_shape": list(right.shape),
            "right_sha256": hashlib.sha256(
                right.astype(np.float64, copy=False).tobytes(order="C")
            ).hexdigest(),
        }
    )


def _controller_configuration_material() -> dict[str, Any]:
    return {
        "environment_id": GROOT_ROBOCASA_ENVIRONMENT_ID,
        "robocasa_revision": GROOT_ROBOCASA_REVISION,
        "robosuite_revision": GROOT_ROBOSUITE_REVISION,
        "mujoco_version": "3.2.6",
        "numpy_version": "1.26.4",
        "robot": "GR1ArmsOnlyFourierHands",
        "execution_profile": GROOT_FIXED_BASE_ARM_ONLY_PROFILE,
        "base_mobility": "fixed",
        "governed_body_parts": ["left_arm", "right_arm"],
        "balance_coupling_governed": False,
        "whole_body_safety_claimed": False,
        "controller_config": "default_gr1.json",
        "controller_override": {
            "left_gripper_enabled": False,
            "right_gripper_enabled": False,
            "require_gripper_actuator_ctrl_unchanged": True,
        },
        "arm_controller_input_type": "absolute",
        "arm_unit": "rad",
        "controller_dynamic_limits": {
            "ownership": "controller",
            "maximum_handoff_delta_rad": (
                GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD
            ),
            "maximum_handoff_state_age_seconds": (
                GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS
            ),
            "source_backed_arm_velocity_limit": None,
            "source_backed_arm_acceleration_limit": None,
            "source_backed_arm_jerk_limit": None,
            "source_status": (
                "not_declared_or_enforced_by_pinned_robot_controller_sources"
            ),
        },
        "sample_rate_hz": 20.0,
        "chunk_steps": 16,
        "hand_actuation_allowed": False,
        "execution_scope": "sim",
    }


def _controller_configuration_sha256() -> str:
    return canonical_sha256(_controller_configuration_material())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git_revision(module_file: str) -> str:
    path = Path(module_file).resolve()
    for parent in path.parents:
        if (parent / ".git").exists():
            return subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=parent,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
    raise RuntimeError("groot_controller_editable_revision_unavailable")


@dataclass
class _Runtime:
    env: Any
    robot: Any
    gripper_indexes: np.ndarray
    observation: dict[str, Any]


def _load_environment() -> _Runtime:
    with redirect_stdout(sys.stderr):
        import gymnasium as gym
        import mujoco
        import robocasa
        import robocasa.utils.gym_utils.gymnasium_groot  # noqa: F401
        import robosuite

    if _git_revision(robocasa.__file__) != GROOT_ROBOCASA_REVISION:
        raise RuntimeError("groot_controller_robocasa_revision_mismatch")
    if _git_revision(robosuite.__file__) != GROOT_ROBOSUITE_REVISION:
        raise RuntimeError("groot_controller_robosuite_revision_mismatch")
    if mujoco.__version__ != "3.2.6" or np.__version__ != "1.26.4":
        raise RuntimeError("groot_controller_runtime_version_mismatch")
    with redirect_stdout(sys.stderr):
        env = gym.make(
            GROOT_ROBOCASA_ENVIRONMENT_ID,
            enable_render=False,
            seed=7,
            disable_env_checker=True,
        )
        observation, _ = env.reset(seed=7)
    robot = env.unwrapped.env.robots[0]
    robot._enabled_parts["left_gripper"] = False
    robot._enabled_parts["right_gripper"] = False
    gripper_indexes = np.concatenate(
        (
            robot._ref_actuators_indexes_dict["left_gripper"],
            robot._ref_actuators_indexes_dict["right_gripper"],
        )
    )
    return _Runtime(env, robot, gripper_indexes, observation)


def _observe_state(runtime: _Runtime | None = None) -> dict[str, Any]:
    owned = runtime is None
    runtime = runtime or _load_environment()
    try:
        return {
            "schema_version": "missionos_groot_robocasa_handoff_state.v1",
            "environment_id": GROOT_ROBOCASA_ENVIRONMENT_ID,
            "left_arm_rad": np.asarray(
                runtime.observation["state.left_arm"],
                dtype=np.float64,
            ).tolist(),
            "right_arm_rad": np.asarray(
                runtime.observation["state.right_arm"],
                dtype=np.float64,
            ).tolist(),
            "observed_at": _utc_now(),
            "execution_scope": "sim",
            "physical_execution_invoked": False,
        }
    finally:
        if owned:
            runtime.env.close()


def _observe_policy_input(runtime: _Runtime | None = None) -> dict[str, Any]:
    """Return the live simulator observation used at the GR00T boundary."""

    owned = runtime is None
    runtime = runtime or _load_environment()
    try:
        return {
            "schema_version": "missionos_groot_policy_observation.v1",
            "environment_id": GROOT_ROBOCASA_ENVIRONMENT_ID,
            "state.left_arm": np.asarray(
                runtime.observation["state.left_arm"],
                dtype=np.float32,
            ).tolist(),
            "state.left_hand": np.asarray(
                runtime.observation["state.left_hand"],
                dtype=np.float32,
            ).tolist(),
            "state.right_arm": np.asarray(
                runtime.observation["state.right_arm"],
                dtype=np.float32,
            ).tolist(),
            "state.right_hand": np.asarray(
                runtime.observation["state.right_hand"],
                dtype=np.float32,
            ).tolist(),
            "video.ego_view": np.asarray(
                runtime.observation[
                    "video.ego_view_bg_crop_pad_res256_freq20"
                ],
                dtype=np.uint8,
            ).tolist(),
            "observed_at": _utc_now(),
            "execution_scope": "sim",
            "physical_execution_invoked": False,
        }
    finally:
        if owned:
            runtime.env.close()


def _exercise_safe_stop(runtime: _Runtime | None = None) -> dict[str, Any]:
    owned = runtime is None
    runtime = runtime or _load_environment()
    try:
        left = np.asarray(
            runtime.observation["state.left_arm"],
            dtype=np.float64,
        )
        right = np.asarray(
            runtime.observation["state.right_arm"],
            dtype=np.float64,
        )
        pre_at = _utc_now()
        pre_digest = _array_sha256(left.reshape(1, 7), right.reshape(1, 7))
        request_at = _utc_now()
        request_digest = canonical_sha256(
            {
                "mechanism": "controller_position_hold",
                "left_target_sha256": _array_sha256(
                    left.reshape(1, 7),
                    right.reshape(1, 7),
                ),
            }
        )
        ack_at = _utc_now()
        ack_digest = canonical_sha256(
            {
                "request_sha256": request_digest,
                "left_gripper_enabled": False,
                "right_gripper_enabled": False,
            }
        )
        gripper_ctrl_before = runtime.robot.sim.data.ctrl[
            runtime.gripper_indexes
        ].copy()
        observed_states: list[np.ndarray] = []
        for _ in range(8):
            with redirect_stdout(sys.stderr):
                runtime.observation, _, _, _, _ = runtime.env.step(
                    {
                        "action.left_arm": left,
                        "action.right_arm": right,
                        "action.left_hand": np.zeros(6, dtype=np.float64),
                        "action.right_hand": np.zeros(6, dtype=np.float64),
                    }
                )
            observed_states.append(
                np.concatenate(
                    (
                        np.asarray(
                            runtime.observation["state.left_arm"],
                            dtype=np.float64,
                        ),
                        np.asarray(
                            runtime.observation["state.right_arm"],
                            dtype=np.float64,
                        ),
                    )
                )
            )
        post_left = np.asarray(
            runtime.observation["state.left_arm"],
            dtype=np.float64,
        )
        post_right = np.asarray(
            runtime.observation["state.right_arm"],
            dtype=np.float64,
        )
        effect_at = _utc_now()
        maximum_step_delta = float(
            np.max(np.abs(np.diff(np.stack(observed_states), axis=0)))
        )
        hand_ctrl_unchanged = bool(
            np.array_equal(
                gripper_ctrl_before,
                runtime.robot.sim.data.ctrl[runtime.gripper_indexes],
            )
        )
        effect_digest = canonical_sha256(
            {
                "maximum_step_delta_rad": maximum_step_delta,
                "hand_ctrl_unchanged": hand_ctrl_unchanged,
            }
        )
        post_at = _utc_now()
        post_digest = _array_sha256(
            post_left.reshape(1, 7),
            post_right.reshape(1, 7),
        )
        return {
            "schema_version": "missionos_groot_robocasa_safe_stop_exercise.v1",
            "pre_state": {
                "observed_at": pre_at,
                "sha256": pre_digest,
            },
            "request": {
                "observed_at": request_at,
                "sha256": request_digest,
            },
            "ack": {
                "observed_at": ack_at,
                "sha256": ack_digest,
            },
            "effect": {
                "observed_at": effect_at,
                "sha256": effect_digest,
                "maximum_step_delta_rad": maximum_step_delta,
                "hand_ctrl_unchanged": hand_ctrl_unchanged,
            },
            "post_state": {
                "observed_at": post_at,
                "sha256": post_digest,
            },
            "bounds_observed": bool(
                maximum_step_delta <= 0.01 and hand_ctrl_unchanged
            ),
            "execution_scope": "sim",
            "physical_execution_invoked": False,
            "task_completion_claimed": False,
        }
    finally:
        if owned:
            runtime.env.close()


def _validate_request(request: dict[str, Any]) -> None:
    if request.get("schema_version") != GROOT_ARM_CONTROLLER_REQUEST_SCHEMA:
        raise RuntimeError("groot_controller_request_schema_not_supported")
    if request.get("execution_scope") != "sim":
        raise RuntimeError("groot_controller_request_scope_invalid")
    if request.get("physical_execution_invoked") is not False:
        raise RuntimeError("groot_controller_physical_execution_forbidden")
    if request.get("hand_actuation_allowed") is not False:
        raise RuntimeError("groot_controller_hand_actuation_forbidden")
    if (
        request.get("execution_profile")
        != GROOT_FIXED_BASE_ARM_ONLY_PROFILE
        or request.get("balance_coupling_governed") is not False
        or request.get("whole_body_safety_claimed") is not False
    ):
        raise RuntimeError("groot_controller_execution_profile_invalid")
    if (
        request.get("controller_configuration_sha256")
        != _controller_configuration_sha256()
    ):
        raise RuntimeError("groot_controller_configuration_mismatch")
    if canonical_sha256(request["controller_policy_material"]) != request.get(
        "controller_policy_sha256"
    ):
        raise RuntimeError("groot_controller_policy_digest_mismatch")
    if canonical_sha256(request["transformation_material"]) != request.get(
        "transformation_sha256"
    ):
        raise RuntimeError("groot_controller_transformation_digest_mismatch")


def _apply_chunk(
    request: dict[str, Any],
    runtime: _Runtime | None = None,
) -> dict[str, Any]:
    _validate_request(request)
    left = np.asarray(request["left_arm_rad"], dtype=np.float64)
    right = np.asarray(request["right_arm_rad"], dtype=np.float64)
    if (
        left.shape != (16, 7)
        or right.shape != (16, 7)
        or not np.isfinite(left).all()
        or not np.isfinite(right).all()
    ):
        raise RuntimeError("groot_controller_arm_chunk_invalid")

    owned = runtime is None
    runtime = runtime or _load_environment()
    started = monotonic()
    handoff_at = _utc_now()
    handoff_datetime = datetime.fromisoformat(
        handoff_at.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    handoff_left = np.asarray(
        runtime.observation["state.left_arm"],
        dtype=np.float64,
    )
    handoff_right = np.asarray(
        runtime.observation["state.right_arm"],
        dtype=np.float64,
    )
    handoff = np.concatenate((handoff_left, handoff_right))
    first = np.concatenate((left[0], right[0]))
    handoff_state_age_seconds = max(monotonic() - started, 0.0)
    observed_handoff_delta = float(np.max(np.abs(first - handoff)))
    dynamic_limits_passed = bool(
        observed_handoff_delta
        <= GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD
        and handoff_state_age_seconds
        <= GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS
    )
    dynamic_limits_observation_sha256 = canonical_sha256(
        {
            "controller_configuration_sha256": (
                _controller_configuration_sha256()
            ),
            "maximum_handoff_delta_rad": (
                GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD
            ),
            "maximum_handoff_state_age_seconds": (
                GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS
            ),
            "observed_handoff_delta_rad": observed_handoff_delta,
            "observed_handoff_state_age_seconds": (
                handoff_state_age_seconds
            ),
            "dynamic_limits_passed": dynamic_limits_passed,
        }
    )
    continuity_ok = dynamic_limits_passed
    handoff_deadline = datetime.fromisoformat(
        request["handoff_deadline"].replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    if datetime.now(timezone.utc) > handoff_deadline:
        continuity_ok = False
    gripper_ctrl_before = runtime.robot.sim.data.ctrl[
        runtime.gripper_indexes
    ].copy()
    progress: list[dict[str, Any]] = []
    try:
        if continuity_ok:
            for index, (left_sample, right_sample) in enumerate(
                zip(left, right, strict=True)
            ):
                with redirect_stdout(sys.stderr):
                    runtime.observation, _, _, _, _ = runtime.env.step(
                        {
                            "action.left_arm": left_sample,
                            "action.right_arm": right_sample,
                            # The wrapper requires these keys, but both hand
                            # part controllers are disabled above.
                            "action.left_hand": np.zeros(6, dtype=np.float64),
                            "action.right_hand": np.zeros(6, dtype=np.float64),
                        }
                    )
                progress.append(
                    {
                        "sample_index": index,
                        "sim_time": float(runtime.robot.sim.data.time),
                    }
                )
        gripper_ctrl_after = runtime.robot.sim.data.ctrl[
            runtime.gripper_indexes
        ].copy()
        hand_command_applied = not np.array_equal(
            gripper_ctrl_before,
            gripper_ctrl_after,
        )
        effect_left = np.asarray(
            runtime.observation["state.left_arm"],
            dtype=np.float64,
        )
        effect_right = np.asarray(
            runtime.observation["state.right_arm"],
            dtype=np.float64,
        )
        effect_at = _utc_now()
        applied_digest = _array_sha256(left, right) if continuity_ok else None
        progress_digest = canonical_sha256({"samples": progress})
        effect_digest = _array_sha256(
            effect_left.reshape(1, 7),
            effect_right.reshape(1, 7),
        )
        return {
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
            "remaining_valid_horizon_seconds_at_handoff": max(
                (handoff_deadline - handoff_datetime).total_seconds(),
                0.0,
            ),
            "handoff_observed_at": handoff_at,
            "handoff_state_age_seconds": handoff_state_age_seconds,
            "handoff_left_arm_rad": handoff_left.tolist(),
            "handoff_right_arm_rad": handoff_right.tolist(),
            "controller_ack_observed": continuity_ok,
            "progress_samples_observed": len(progress),
            "progress_samples": progress,
            "progress_observed_at": effect_at,
            "progress_source_sha256": progress_digest,
            "applied_left_arm_rad": left.tolist() if continuity_ok else None,
            "applied_right_arm_rad": right.tolist() if continuity_ok else None,
            "applied_command_sha256": applied_digest,
            "effect_observed_at": effect_at,
            "effect_left_arm_rad": effect_left.tolist(),
            "effect_right_arm_rad": effect_right.tolist(),
            "effect_source_id": (
                f"robocasa-qpos:{request['request_id']}:{len(progress)}"
            ),
            "effect_source_sha256": effect_digest,
            "hand_command_applied": hand_command_applied,
            "dynamic_limits_configuration_sha256": (
                _controller_configuration_sha256()
            ),
            "dynamic_limits_observation_sha256": (
                dynamic_limits_observation_sha256
            ),
            "dynamic_limits_evidence_origin": "machine_observed",
            "dynamic_limits_enforced": True,
            "execution_profile": GROOT_FIXED_BASE_ARM_ONLY_PROFILE,
            "balance_coupling_governed": False,
            "whole_body_safety_claimed": False,
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
    finally:
        if owned:
            runtime.env.close()


def _handle(request: dict[str, Any], runtime: _Runtime | None = None) -> dict[str, Any]:
    if request == {"action": "observe_handoff_state"}:
        return _observe_state(runtime)
    if request == {"action": "observe_policy_input"}:
        return _observe_policy_input(runtime)
    if request == {"action": "exercise_safe_stop"}:
        return _exercise_safe_stop(runtime)
    return _apply_chunk(request, runtime)


def _serve() -> int:
    runtime = _load_environment()
    print(
        json.dumps(
            {
                "status": "ready",
                "execution_scope": "sim",
                "physical_execution_invoked": False,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if request == {"action": "shutdown"}:
                    return 0
                response = _handle(request, runtime)
            except Exception as exc:
                response = {
                    "error": str(exc),
                    "physical_execution_invoked": False,
                }
            print(
                json.dumps(response, ensure_ascii=True, sort_keys=True),
                flush=True,
            )
    finally:
        runtime.env.close()
    return 0


def main() -> int:
    if sys.argv[1:] == ["--server"]:
        return _serve()
    try:
        request = json.load(sys.stdin)
        response = _handle(request)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "physical_execution_invoked": False,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(response, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
