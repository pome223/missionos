"""Stateful LeRobot ``select_action`` bridge for one same-world Repair run.

The bridge owns no approval or dispatch authority.  It adapts LeRobot's
stateful single-action API to the existing bounded-chunk Repair callbacks while
making the policy queue boundary explicit.  The simulator lifecycle remains
owned by the caller.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any

import numpy as np

from missionos_core import canonical_sha256

from src.runtime.groot_libero_same_world_repair import (
    FRAME_CAPTURE_AUTHORITY,
    FRAME_CAPTURE_SCHEMA_VERSION,
    PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
    verify_exact_repair_instruction_payload,
)
from src.runtime.libero_panda_official_runner_instrumentation import digest_runtime_material


LEROBOT_LIVE_ACTION_STEPS = 16
_SAFE_FAILURE_CODE = re.compile(r"[a-z0-9_]{1,96}")


def batch_single_environment_observation(value: Any) -> Any:
    """Match the official one-element vector-env observation boundary.

    The persistent same-world harness owns one non-vectorized environment so it
    can retain direct verifier access.  LeRobot's official evaluator wraps that
    environment in ``SyncVectorEnv`` and consequently supplies a leading batch
    dimension on every ndarray leaf.  Reproduce only that structural boundary;
    do not normalize, rename, or otherwise interpret observation values here.
    """

    if isinstance(value, Mapping):
        return {key: batch_single_environment_observation(item) for key, item in value.items()}
    if isinstance(value, np.ndarray):
        return np.expand_dims(value, axis=0)
    return deepcopy(value)


def verify_huggingface_local_snapshot(
    *,
    snapshot_path: Path,
    expected_revision: str,
    required_files: Sequence[str],
) -> dict[str, Any]:
    """Verify local-dir downloads against Hugging Face revision metadata."""

    if not re.fullmatch(r"[0-9a-f]{40}", expected_revision):
        raise ValueError("huggingface_snapshot_revision_invalid")
    if not snapshot_path.is_dir():
        raise ValueError("huggingface_snapshot_directory_required")
    file_sha256: dict[str, str] = {}
    for relative_name in required_files:
        if Path(relative_name).is_absolute() or ".." in Path(relative_name).parts:
            raise ValueError("huggingface_snapshot_relative_path_invalid")
        source = snapshot_path / relative_name
        metadata = (
            snapshot_path / ".cache" / "huggingface" / "download" / f"{relative_name}.metadata"
        )
        if not source.is_file() or not metadata.is_file():
            raise ValueError("huggingface_snapshot_required_file_missing")
        metadata_lines = metadata.read_text(encoding="utf-8").splitlines()
        if not metadata_lines or metadata_lines[0] != expected_revision:
            raise ValueError("huggingface_snapshot_revision_mismatch")
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        file_sha256[relative_name] = digest.hexdigest()
    return {
        "revision": expected_revision,
        "required_file_sha256": file_sha256,
        "snapshot_verified": True,
    }


@dataclass(frozen=True)
class SelectedAction:
    """One selected action and evidence about the policy call that produced it."""

    action: Any
    model_forward_observed: bool
    policy_request_sha256: str | None
    policy_response_sha256: str | None
    instruction_payload: Any


class LeRobotActionChunkExecutionError(RuntimeError):
    """Retain evidence for actions applied before a chunk failed closed."""

    def __init__(self, *, cause: Exception, partial_application: Mapping[str, Any]) -> None:
        super().__init__("lerobot_action_chunk_execution_failed")
        self.error_type = type(cause).__name__
        reason = str(cause)
        self.failure_code = reason if _SAFE_FAILURE_CODE.fullmatch(reason) else "unspecified"
        self.partial_application = deepcopy(dict(partial_application))


PolicySelect = Callable[[Any, str], SelectedAction]
ApplyAction = Callable[[Any], tuple[Any, Mapping[str, Any]]]
ObservePredicates = Callable[[], Sequence[Mapping[str, Any]]]


def _not_requested_frame_capture() -> dict[str, Any]:
    return {
        "schema_version": FRAME_CAPTURE_SCHEMA_VERSION,
        "authority": FRAME_CAPTURE_AUTHORITY,
        "status": "not_requested",
        "cameras": [],
    }


class LeRobotLiveSession:
    """Keep one environment alive while adapting a stateful LeRobot policy.

    ``select_action`` must expose whether the current call really invoked the
    model or merely consumed a queued action.  The first call of every admitted
    Repair chunk must invoke the model, and the following fifteen calls must
    consume exactly the remainder of that same queue.
    """

    def __init__(
        self,
        *,
        initial_observation: Any,
        select_action: PolicySelect,
        apply_action: ApplyAction,
        observe_goal_predicates: ObservePredicates,
        policy_reset: Callable[[], None],
        action_queue_depth: Callable[[], int],
        observed_reset_count: Callable[[], int],
        action_steps: int = LEROBOT_LIVE_ACTION_STEPS,
    ) -> None:
        if action_steps != LEROBOT_LIVE_ACTION_STEPS:
            raise ValueError("lerobot_live_action_steps_mismatch")
        self._observation = initial_observation
        self._select_action = select_action
        self._apply_action = apply_action
        self._observe_goal_predicates = observe_goal_predicates
        self._policy_reset = policy_reset
        self._action_queue_depth = action_queue_depth
        self._observed_reset_count = observed_reset_count
        self._action_steps = action_steps
        self._pending_first_action: SelectedAction | None = None
        self._pending_instruction: str | None = None
        self._repair_policy_reset_observed = False
        self._source_model_forward_count = 0
        self._source_steps_executed = 0

    @property
    def observation(self) -> Any:
        return self._observation

    @property
    def source_steps_executed(self) -> int:
        return self._source_steps_executed

    @property
    def source_model_forward_count(self) -> int:
        return self._source_model_forward_count

    def run_source_steps(self, *, instruction: str, maximum_steps: int) -> dict[str, Any]:
        """Run the frozen source task without resetting the environment."""

        if not instruction:
            raise ValueError("lerobot_source_instruction_required")
        if maximum_steps <= 0:
            raise ValueError("lerobot_source_step_budget_invalid")
        if self._observed_reset_count() != 1:
            raise RuntimeError("lerobot_source_reset_count_invalid")
        predicate_changes: list[dict[str, Any]] = []
        previous_vector: list[dict[str, Any]] | None = None
        for step_index in range(maximum_steps):
            queue_before = self._action_queue_depth()
            if queue_before < 0 or queue_before >= self._action_steps:
                raise RuntimeError("lerobot_source_action_queue_depth_invalid")
            selected = self._select_action(self._observation, instruction)
            if queue_before == 0:
                if selected.model_forward_observed is not True:
                    raise RuntimeError("lerobot_source_model_forward_not_observed")
                if not selected.policy_request_sha256:
                    raise RuntimeError("lerobot_source_policy_request_digest_missing")
                if not selected.policy_response_sha256:
                    raise RuntimeError("lerobot_source_policy_response_digest_missing")
                self._source_model_forward_count += 1
            else:
                if selected.model_forward_observed is True:
                    raise RuntimeError("lerobot_source_unexpected_model_forward")
                if selected.policy_request_sha256 is not None:
                    raise RuntimeError("lerobot_source_queued_request_digest_present")
                if selected.policy_response_sha256 is not None:
                    raise RuntimeError("lerobot_source_queued_response_digest_present")
            expected_queue_after = self._action_steps - 1 if queue_before == 0 else queue_before - 1
            if self._action_queue_depth() != expected_queue_after:
                raise RuntimeError("lerobot_source_action_queue_transition_mismatch")
            self._observation, application = self._apply_action(selected.action)
            self._source_steps_executed += 1
            predicates = [dict(item) for item in self._observe_goal_predicates()]
            if previous_vector != predicates:
                predicate_changes.append(
                    {
                        "step_index": step_index,
                        "goal_predicate_observations": deepcopy(predicates),
                        "goal_predicate_vector_sha256": canonical_sha256(
                            {"goal_predicate_observations": predicates}
                        ),
                    }
                )
                previous_vector = deepcopy(predicates)
            official_result = application.get("official_predicate_result")
            if not isinstance(official_result, bool):
                raise RuntimeError("lerobot_source_official_result_missing")
            conjunction = bool(predicates) and all(item["satisfied"] for item in predicates)
            if official_result is not conjunction:
                raise RuntimeError("lerobot_source_predicate_conjunction_mismatch")
            if official_result:
                break
            if application.get("truncated") is True or application.get("done") is True:
                raise RuntimeError("lerobot_source_environment_ended_without_success")
            if self._observed_reset_count() != 1:
                raise RuntimeError("lerobot_source_implicit_reset_observed")
        final_predicates = [dict(item) for item in self._observe_goal_predicates()]
        return {
            "source_steps_executed": self._source_steps_executed,
            "source_model_forward_count": self._source_model_forward_count,
            "source_goal_predicate_observations": final_predicates,
            "source_goal_predicate_vector_sha256": canonical_sha256(
                {"goal_predicate_observations": final_predicates}
            ),
            "source_predicate_changes": predicate_changes,
            "queued_source_actions_remaining": self._action_queue_depth(),
            "same_world_reset_count": self._observed_reset_count(),
        }

    def begin_repair(self) -> dict[str, Any]:
        """Discard source-command queue state without resetting the world."""

        if self._pending_first_action is not None:
            raise RuntimeError("lerobot_repair_chunk_already_pending")
        if self._repair_policy_reset_observed:
            raise RuntimeError("lerobot_repair_policy_boundary_already_reset")
        if self._observed_reset_count() != 1:
            raise RuntimeError("lerobot_repair_reset_count_invalid")
        discarded = self._action_queue_depth()
        self._policy_reset()
        if self._action_queue_depth() != 0:
            raise RuntimeError("lerobot_repair_policy_queue_not_cleared")
        if self._observed_reset_count() != 1:
            raise RuntimeError("lerobot_repair_policy_reset_changed_world")
        self._repair_policy_reset_observed = True
        return {
            "policy_queue_reset_observed": True,
            "discarded_source_action_count": discarded,
            "environment_reset_invoked": False,
            "same_world_reset_count": 1,
        }

    def invoke_model(
        self, observation: Any, instruction: str, chunk_index: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Start one admitted chunk and retain its first selected action."""

        if not self._repair_policy_reset_observed:
            raise RuntimeError("lerobot_repair_policy_boundary_not_reset")
        if self._pending_first_action is not None:
            raise RuntimeError("lerobot_repair_chunk_already_pending")
        if observation is not self._observation:
            raise RuntimeError("lerobot_repair_observation_identity_mismatch")
        if self._observed_reset_count() != 1:
            raise RuntimeError("lerobot_repair_reset_count_changed")
        if self._action_queue_depth() != 0:
            raise RuntimeError("lerobot_repair_chunk_started_with_queued_actions")
        selected = self._select_action(observation, instruction)
        if selected.model_forward_observed is not True:
            raise RuntimeError("lerobot_repair_model_forward_not_observed")
        if not selected.policy_request_sha256:
            raise RuntimeError("lerobot_repair_policy_request_digest_missing")
        if not selected.policy_response_sha256:
            raise RuntimeError("lerobot_repair_policy_response_digest_missing")
        if self._action_queue_depth() != self._action_steps - 1:
            raise RuntimeError("lerobot_repair_action_queue_depth_mismatch")
        instruction_sha256 = canonical_sha256({"repair_instruction": instruction})
        payload_evidence = verify_exact_repair_instruction_payload(
            payload=selected.instruction_payload,
            expected_instruction=instruction,
        )
        self._pending_first_action = selected
        self._pending_instruction = instruction
        return {"chunk_index": chunk_index}, {
            "model_runtime_invoked": True,
            "repair_instruction_sha256": instruction_sha256,
            **payload_evidence,
            "policy_request_sha256": selected.policy_request_sha256,
            "policy_response_sha256": selected.policy_response_sha256,
        }

    def apply_action_chunk(
        self, action_token: Mapping[str, Any], chunk_index: int
    ) -> tuple[Any, dict[str, Any]]:
        """Consume exactly one 16-action queue and observe every simulator step."""

        if action_token.get("chunk_index") != chunk_index:
            raise RuntimeError("lerobot_repair_action_token_mismatch")
        selected = self._pending_first_action
        if selected is None:
            raise RuntimeError("lerobot_repair_first_action_missing")
        actions: list[Any] = []
        trace: list[dict[str, Any]] = []
        executed_step_trace: list[dict[str, Any]] = []
        any_effect = False
        instruction = self._pending_instruction
        try:
            for action_step_index in range(self._action_steps):
                if action_step_index == 0:
                    current = selected
                else:
                    if instruction is None:
                        raise RuntimeError("lerobot_repair_instruction_not_retained")
                    current = self._select_action(self._observation, instruction)
                    if current.model_forward_observed is True:
                        raise RuntimeError("lerobot_repair_extra_model_forward_inside_chunk")
                    if current.policy_request_sha256 is not None:
                        raise RuntimeError("lerobot_repair_queued_request_digest_present")
                    if current.policy_response_sha256 is not None:
                        raise RuntimeError("lerobot_repair_queued_response_digest_present")
                    expected_queue_depth = self._action_steps - action_step_index - 1
                    if self._action_queue_depth() != expected_queue_depth:
                        raise RuntimeError("lerobot_repair_action_queue_transition_mismatch")
                actions.append(current.action)
                self._observation, application = self._apply_action(current.action)
                execution_entry = {
                    "chunk_index": chunk_index,
                    "action_step_index": action_step_index,
                    "action_step_number": action_step_index + 1,
                    "action_step_sha256": digest_runtime_material("action_step", current.action),
                    "simulator_step_return_observed": (
                        application.get("simulator_step_return_observed") is True
                    ),
                    "simulator_effect_observed": bool(application.get("simulator_effect_observed")),
                    "verification_completed": False,
                }
                executed_step_trace.append(execution_entry)
                if application.get("simulator_step_return_observed") is not True:
                    raise RuntimeError("lerobot_repair_simulator_step_not_observed")
                any_effect = bool(any_effect or application.get("simulator_effect_observed"))
                predicates = [dict(item) for item in self._observe_goal_predicates()]
                official_result = application.get("official_predicate_result")
                execution_entry["goal_predicate_observations"] = deepcopy(predicates)
                execution_entry["official_predicate_result"] = (
                    official_result if isinstance(official_result, bool) else None
                )
                if not isinstance(official_result, bool):
                    raise RuntimeError("lerobot_repair_official_result_missing")
                conjunction = bool(predicates) and all(item["satisfied"] for item in predicates)
                if official_result is not conjunction:
                    raise RuntimeError("lerobot_repair_predicate_conjunction_mismatch")
                if application.get("truncated") is True or (
                    application.get("done") is True and not official_result
                ):
                    raise RuntimeError("lerobot_repair_environment_ended_without_success")
                global_index = chunk_index * self._action_steps + action_step_index
                trace.append(
                    {
                        "schema_version": PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
                        "chunk_index": chunk_index,
                        "action_step_index": action_step_index,
                        "action_step_number": action_step_index + 1,
                        "global_repair_step_index": global_index,
                        "global_repair_step_number": global_index + 1,
                        "action_step_sha256": execution_entry["action_step_sha256"],
                        "goal_predicate_observations": predicates,
                        "goal_predicate_vector_sha256": canonical_sha256(
                            {"goal_predicate_observations": predicates}
                        ),
                        "official_predicate_conjunction": conjunction,
                        "official_predicate_result": official_result,
                        "conjunction_matches_official_result": True,
                        "object_witnesses": deepcopy(application.get("object_witnesses", {})),
                        "frame_capture": deepcopy(
                            application.get("frame_capture", _not_requested_frame_capture())
                        ),
                    }
                )
                execution_entry["verification_completed"] = True
                if self._observed_reset_count() != 1:
                    raise RuntimeError("lerobot_repair_implicit_reset_observed")
            if self._action_queue_depth() != 0:
                raise RuntimeError("lerobot_repair_action_queue_not_consumed")
            return self._observation, {
                "simulator_step_return_observed": True,
                "simulator_effect_observed": any_effect,
                "action_chunk_sha256": digest_runtime_material("action_chunk", actions),
                "official_predicate_result": trace[-1]["official_predicate_result"],
                "preservation_step_trace": trace,
            }
        except Exception as error:
            raise LeRobotActionChunkExecutionError(
                cause=error,
                partial_application={
                    "chunk_index": chunk_index,
                    "actions_applied": len(executed_step_trace),
                    "simulator_step_return_observed": bool(executed_step_trace),
                    "simulator_effect_observed": any(
                        item["simulator_effect_observed"] for item in executed_step_trace
                    ),
                    "executed_step_trace": executed_step_trace,
                    "same_world_reset_count": self._observed_reset_count(),
                    "verification_passed": False,
                    "completion_claimed": False,
                },
            ) from error
        finally:
            self._pending_first_action = None
            self._pending_instruction = None


__all__ = [
    "LEROBOT_LIVE_ACTION_STEPS",
    "LeRobotActionChunkExecutionError",
    "LeRobotLiveSession",
    "SelectedAction",
]
