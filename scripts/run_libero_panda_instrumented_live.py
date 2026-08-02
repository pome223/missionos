"""Run one opt-in, instrumented GR00T N1.7 LIBERO Panda episode.

This command is intentionally outside the default test and smoke paths. It
uses the pinned NVIDIA official rollout runner and MissionOS's thin
instrumentation wrapper. It does not replace policy inference, the LIBERO
controller, the simulator step loop, termination, or the official predicate.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
from math import isfinite
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sysconfig
import time
from typing import Any, Iterator

from missionos_core import canonical_sha256
from src.runtime.libero_panda_official_runner_instrumentation import (
    LIBEROPandaControllerRuntimeBinding,
    LIBEROPandaInstrumentationError,
    LIBEROPandaInstrumentationEvent,
    LIBEROPandaInstrumentationRejectionReason,
    prepare_libero_panda_instrumented_episode,
    run_instrumented_official_libero_rollout,
)
from src.runtime.libero_panda_predicate_package import (
    GROOT_CHECKPOINT_REPOSITORY,
    GROOT_CHECKPOINT_REVISION,
    GROOT_MODEL_ACTION_HORIZON_CAPACITY,
    ISAAC_GROOT_REVISION,
    LIBERO_PANDA_EMBODIMENT_TAG,
    LIBERO_PANDA_ENVIRONMENT,
    LIBERO_POLICY_ACTION_HORIZON,
    LIBERO_REVISION,
    LIBEROPandaRunnerConfiguration,
)


COSMOS_REPOSITORY = "nvidia/Cosmos-Reason2-2B"
COSMOS_REVISION = "9ce19a195e423419c349abfc86fd07178b230561"
EXPECTED_CHECKPOINT_CONFIG_SHA256 = (
    "ce763f74ad1cda2ca9e67491229cbc5810d83cae675cb4dad839a69a9731fd97"
)
EXPECTED_CHECKPOINT_PROCESSOR_CONFIG_SHA256 = (
    "545bb44c7d8caaf5ac82df55b90831271a1469a4e48adcc9a5f469f064f4e121"
)
EXPECTED_COSMOS_PROCESSOR_SHA256 = {
    "config.json": (
        "bec4b3d446efa05807365c9e1cec03ac590836879d02f3a6da879971154bdd3b"
    ),
    "preprocessor_config.json": (
        "27225450ac9c6529872ee1924fcb0962ff5634834f817040f444118116f4e516"
    ),
    "tokenizer_config.json": (
        "c2da771801886ad9ae98181793ffd3dfb7f1af30f6f7c6a4e15d7dbba52e2399"
    ),
}
MAXIMUM_EPISODE_STEPS = 720
N_ACTION_STEPS = 8
N_ENVS = 1
MISSIONOS_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_RUNTIME_DEPENDENCIES = {
    "robosuite": "1.4.0",
    "mujoco": "2.3.7",
}
_SAFE_PYTHON_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

EXPECTED_CONTROLLER_RUNTIME_MATERIAL = {
    "controller_name": "OSC_POSE",
    "controller_class": "OperationalSpaceController",
    "action_dim": 7,
    "arm_control_dim": 6,
    "gripper_dof": 1,
    "control_freq_hz": 20,
    "use_delta": True,
    "use_orientation": True,
    "impedance_mode": "fixed",
}

EXPECTED_CHECKPOINT_SHA256 = {
    "model-00001-of-00002.safetensors": (
        "39b7beaadd9a06c87502f2b741a36f939b84941f37fef757c6177e54e34a5eff"
    ),
    "model-00002-of-00002.safetensors": (
        "8c4fa56f1b4f25a1a842c811e8a12d2bf6d77d73942f9a04f5b876a11bd6d703"
    ),
    "model.safetensors.index.json": (
        "407804ea5a62f4f8823f48811ae0edbb82fac101e9cf4d7273e6e2f692bb4d59"
    ),
}


class LiveEpisodeExecutionError(RuntimeError):
    """Carry publication-safe failure evidence out of a live episode."""

    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__("instrumented LIBERO Panda live episode failed")
        self.report = report


def _require_positive_finite_seconds(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(
            "elapsed seconds must be a positive finite number"
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "elapsed seconds must be a positive finite number"
        ) from error
    if not isfinite(parsed) or parsed <= 0:
        raise ValueError(
            "elapsed seconds must be a positive finite number"
        )
    return parsed


def _positive_finite_seconds(value: Any) -> float:
    try:
        return _require_positive_finite_seconds(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


@contextmanager
def _rollout_deadline(
    maximum_elapsed_seconds: float,
    *,
    signal_module: Any = signal,
) -> Iterator[None]:
    maximum_elapsed_seconds = _require_positive_finite_seconds(
        maximum_elapsed_seconds
    )
    previous_timer = signal_module.getitimer(signal_module.ITIMER_REAL)
    if previous_timer != (0.0, 0.0):
        raise RuntimeError("refusing to replace an active process timer")
    previous_handler = signal_module.getsignal(signal_module.SIGALRM)

    def _deadline_reached(_signum: int, _frame: Any) -> None:
        raise LIBEROPandaInstrumentationError(
            "bounded diagnostic rollout exceeded its elapsed-time limit",
            rejection_reason=(
                LIBEROPandaInstrumentationRejectionReason
                .ROLLOUT_DEADLINE_EXCEEDED
            ),
        )

    signal_module.signal(signal_module.SIGALRM, _deadline_reached)
    signal_module.setitimer(
        signal_module.ITIMER_REAL,
        float(maximum_elapsed_seconds),
    )
    try:
        yield
    finally:
        signal_module.setitimer(signal_module.ITIMER_REAL, 0.0)
        signal_module.signal(signal_module.SIGALRM, previous_handler)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_revision(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _verify_processor_locator(
    *,
    model_path: Path,
    reference_model_path: Path,
) -> dict[str, Any]:
    reference_config_path = reference_model_path / "config.json"
    runtime_config_path = model_path / "config.json"
    reference_processor_path = reference_model_path / "processor_config.json"
    runtime_processor_path = model_path / "processor_config.json"
    if not all(
        candidate.is_file()
        for candidate in (
            reference_config_path,
            runtime_config_path,
            reference_processor_path,
            runtime_processor_path,
        )
    ):
        raise RuntimeError("checkpoint config missing")
    reference_config_sha256 = _sha256_path(reference_config_path)
    if reference_config_sha256 != EXPECTED_CHECKPOINT_CONFIG_SHA256:
        raise RuntimeError("reference checkpoint config digest mismatch")
    reference_processor_sha256 = _sha256_path(reference_processor_path)
    if (
        reference_processor_sha256
        != EXPECTED_CHECKPOINT_PROCESSOR_CONFIG_SHA256
    ):
        raise RuntimeError("reference processor config digest mismatch")

    reference_config = json.loads(
        reference_config_path.read_text(encoding="utf-8")
    )
    runtime_config = json.loads(runtime_config_path.read_text(encoding="utf-8"))
    if reference_config.get("model_name") != COSMOS_REPOSITORY:
        raise RuntimeError("reference checkpoint processor locator mismatch")

    runtime_locator = runtime_config.get("model_name")
    reference_material = deepcopy(reference_config)
    runtime_material = deepcopy(runtime_config)
    reference_material.pop("model_name", None)
    runtime_material.pop("model_name", None)
    if runtime_material != reference_material:
        raise RuntimeError(
            "runtime checkpoint config changed beyond the processor locator"
        )

    override_applied = runtime_locator != COSMOS_REPOSITORY
    reference_processor = json.loads(
        reference_processor_path.read_text(encoding="utf-8")
    )
    runtime_processor = json.loads(
        runtime_processor_path.read_text(encoding="utf-8")
    )
    reference_processor_kwargs = deepcopy(
        reference_processor.get("processor_kwargs", {})
    )
    runtime_processor_kwargs = deepcopy(
        runtime_processor.get("processor_kwargs", {})
    )
    reference_action_config = (
        reference_processor_kwargs.get("modality_configs", {})
        .get("libero_sim", {})
        .get("action", {})
    )
    reference_action_delta_indices = reference_action_config.get(
        "delta_indices"
    )
    if (
        reference_config.get("action_horizon")
        != GROOT_MODEL_ACTION_HORIZON_CAPACITY
        or reference_processor_kwargs.get("max_action_horizon")
        != GROOT_MODEL_ACTION_HORIZON_CAPACITY
        or reference_action_delta_indices
        != list(range(LIBERO_POLICY_ACTION_HORIZON))
    ):
        raise RuntimeError("pinned LIBERO action horizon material mismatch")
    reference_processor_locator = reference_processor_kwargs.pop(
        "model_name",
        COSMOS_REPOSITORY,
    )
    runtime_processor_locator = runtime_processor_kwargs.pop(
        "model_name",
        COSMOS_REPOSITORY,
    )
    if reference_processor_locator != COSMOS_REPOSITORY:
        raise RuntimeError("reference processor config locator mismatch")
    reference_processor["processor_kwargs"] = reference_processor_kwargs
    runtime_processor["processor_kwargs"] = runtime_processor_kwargs
    if runtime_processor != reference_processor:
        raise RuntimeError(
            "runtime processor config changed beyond the processor locator"
        )
    if runtime_processor_locator != runtime_locator:
        raise RuntimeError("runtime model and processor locators disagree")

    if override_applied:
        if not isinstance(runtime_locator, str):
            raise RuntimeError("runtime processor locator invalid")
        local_snapshot = Path(runtime_locator).resolve()
        if not local_snapshot.is_dir() or local_snapshot.name != COSMOS_REVISION:
            raise RuntimeError("runtime processor snapshot revision mismatch")
        for filename, expected in EXPECTED_COSMOS_PROCESSOR_SHA256.items():
            candidate = local_snapshot / filename
            if not candidate.is_file() or _sha256_path(candidate) != expected:
                raise RuntimeError(
                    f"runtime processor artifact mismatch: {filename}"
                )

    return {
        "repository": COSMOS_REPOSITORY,
        "revision": COSMOS_REVISION,
        "offline_local_snapshot_override_applied": override_applied,
        "reference_checkpoint_config_sha256": reference_config_sha256,
        "reference_processor_config_sha256": reference_processor_sha256,
        "runtime_checkpoint_config_sha256": _sha256_path(
            runtime_config_path
        ),
        "runtime_processor_config_sha256": _sha256_path(
            runtime_processor_path
        ),
        "only_model_name_locator_changed": (
            runtime_material == reference_material
            and runtime_processor == reference_processor
        ),
        "local_path_recorded": False,
        "processor_artifact_sha256": EXPECTED_COSMOS_PROCESSOR_SHA256,
        "model_action_horizon_capacity": (
            GROOT_MODEL_ACTION_HORIZON_CAPACITY
        ),
        "policy_action_horizon": LIBERO_POLICY_ACTION_HORIZON,
        "execution_horizon": N_ACTION_STEPS,
    }


def verify_source_and_checkpoint_revisions(
    *,
    isaac_groot_root: Path,
    model_path: Path,
    reference_model_path: Path,
) -> dict[str, Any]:
    isaac_revision = _git_revision(isaac_groot_root)
    libero_root = isaac_groot_root / "external_dependencies" / "LIBERO"
    libero_revision = _git_revision(libero_root)
    if isaac_revision != ISAAC_GROOT_REVISION:
        raise RuntimeError("Isaac-GR00T revision does not match the contract")
    if libero_revision != LIBERO_REVISION:
        raise RuntimeError("LIBERO revision does not match the contract")

    checkpoint_digests: dict[str, str] = {}
    for filename, expected in EXPECTED_CHECKPOINT_SHA256.items():
        candidate = model_path / filename
        if not candidate.is_file():
            raise RuntimeError(f"checkpoint file missing: {filename}")
        observed = _sha256_path(candidate)
        if observed != expected:
            raise RuntimeError(f"checkpoint digest mismatch: {filename}")
        checkpoint_digests[filename] = observed

    processor_locator = _verify_processor_locator(
        model_path=model_path,
        reference_model_path=reference_model_path,
    )
    return {
        "isaac_groot_revision": isaac_revision,
        "libero_revision": libero_revision,
        "checkpoint_repository": GROOT_CHECKPOINT_REPOSITORY,
        "checkpoint_revision": GROOT_CHECKPOINT_REVISION,
        "checkpoint_file_sha256": checkpoint_digests,
        "cosmos_repository": COSMOS_REPOSITORY,
        "cosmos_revision": COSMOS_REVISION,
        "processor_locator": processor_locator,
    }


def observe_controller_runtime_material(env: Any) -> dict[str, Any]:
    offscreen_env = getattr(env, "_env", None)
    task_env = getattr(offscreen_env, "env", None)
    robots = getattr(task_env, "robots", None)
    if not isinstance(robots, (list, tuple)) or len(robots) != 1:
        raise RuntimeError("expected exactly one LIBERO robot")
    robot = robots[0]
    controller = getattr(robot, "controller", None)
    gripper = getattr(robot, "gripper", None)
    if controller is None or gripper is None:
        raise RuntimeError("LIBERO controller or gripper is unavailable")

    return {
        "controller_name": str(getattr(controller, "name", "")),
        "controller_class": type(controller).__name__,
        "action_dim": int(getattr(task_env, "action_dim")),
        "arm_control_dim": int(getattr(controller, "control_dim")),
        "gripper_dof": int(getattr(gripper, "dof")),
        "control_freq_hz": int(getattr(task_env, "control_freq")),
        "use_delta": bool(getattr(controller, "use_delta")),
        "use_orientation": bool(getattr(controller, "use_ori")),
        "impedance_mode": str(getattr(controller, "impedance_mode", "")),
    }


def live_controller_probe(env: Any) -> LIBEROPandaControllerRuntimeBinding:
    material = observe_controller_runtime_material(env)
    if material != EXPECTED_CONTROLLER_RUNTIME_MATERIAL:
        raise RuntimeError(
            "live LIBERO controller material does not match the frozen profile"
        )
    return LIBEROPandaControllerRuntimeBinding(
        controller_configuration_sha256=canonical_sha256(material),
        action_dim=material["action_dim"],
    )


def build_runner_configuration() -> LIBEROPandaRunnerConfiguration:
    return LIBEROPandaRunnerConfiguration(
        model_repository=GROOT_CHECKPOINT_REPOSITORY,
        checkpoint_revision=GROOT_CHECKPOINT_REVISION,
        isaac_groot_revision=ISAAC_GROOT_REVISION,
        libero_revision=LIBERO_REVISION,
        embodiment_tag=LIBERO_PANDA_EMBODIMENT_TAG,
        environment=LIBERO_PANDA_ENVIRONMENT,
        maximum_episode_steps=MAXIMUM_EPISODE_STEPS,
        policy_action_horizon=LIBERO_POLICY_ACTION_HORIZON,
        n_action_steps=N_ACTION_STEPS,
        n_envs=N_ENVS,
        controller_configuration_sha256=canonical_sha256(
            EXPECTED_CONTROLLER_RUNTIME_MATERIAL
        ),
        action_dim=EXPECTED_CONTROLLER_RUNTIME_MATERIAL["action_dim"],
        terminate_on_success=True,
    )


def pre_reset_ordering(result: Any, *, prepared: Any) -> dict[str, Any]:
    events = [event.value for event in result.event_sequence]
    prepared_index = events.index(
        LIBEROPandaInstrumentationEvent.PREPARED_BEFORE_RESET.value
    )
    controller_index = events.index(
        LIBEROPandaInstrumentationEvent.CONTROLLER_RUNTIME_BOUND.value
    )
    reset_index = events.index(
        LIBEROPandaInstrumentationEvent.RESET_OBSERVED.value
    )
    order_observed = prepared_index < controller_index < reset_index
    if not order_observed:
        raise RuntimeError("pre-reset contract ordering was not observed")
    return {
        "run_identity_created_before_reset": True,
        "episode_identity_created_before_first_policy_request": True,
        "contract_sha256_available_before_reset": True,
        "prepared_at": prepared.prepared_at,
        "prepared_event_index": prepared_index,
        "controller_bound_event_index": controller_index,
        "reset_observed_event_index": reset_index,
        "order_observed": order_observed,
    }


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not_installed"


def runtime_environment(torch_module: Any) -> dict[str, Any]:
    return {
        "process_role": "instrumented_libero_client",
        "platform": platform.system().lower(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "gpu": torch_module.cuda.get_device_name(0),
        "gpu_memory_mib": int(
            torch_module.cuda.get_device_properties(0).total_memory
            / (1024 * 1024)
        ),
        "torch": str(torch_module.__version__),
        "torch_cuda": str(torch_module.version.cuda),
        "numpy": _package_version("numpy"),
        "robosuite": _package_version("robosuite"),
        "mujoco": _package_version("mujoco"),
    }


def verify_runtime_dependency_profile(
    runtime: dict[str, Any],
) -> None:
    """Refuse a live episode before reset when known-good pins drift."""

    observed = {
        package_name: runtime.get(package_name)
        for package_name in REQUIRED_RUNTIME_DEPENDENCIES
    }
    if observed != REQUIRED_RUNTIME_DEPENDENCIES:
        raise LIBEROPandaInstrumentationError(
            "live LIBERO dependency profile does not match the pinned "
            "reset-verified profile",
            rejection_reason=(
                LIBEROPandaInstrumentationRejectionReason
                .RUNTIME_DEPENDENCY_PROFILE_MISMATCH
            ),
        )


def verify_expected_contract_binding(
    *,
    prepared_contract_sha256: str,
    expected_contract_sha256: str | None,
) -> None:
    """Refuse before reset when the prepared child is not the approved child."""

    if (
        expected_contract_sha256 is not None
        and prepared_contract_sha256 != expected_contract_sha256
    ):
        raise LIBEROPandaInstrumentationError(
            "prepared LIBERO contract does not match the parent-bound "
            "contract digest",
            rejection_reason=(
                LIBEROPandaInstrumentationRejectionReason
                .CONTRACT_BINDING_MISMATCH
            ),
        )


def _negative_claim_boundary() -> dict[str, bool]:
    return {
        "missionos_contract_frozen_before_episode": False,
        "missionos_instrumentation_in_loop": False,
        "missionos_parent_coordinator_in_live_loop": False,
        "model_inference_invoked": False,
        "simulator_step_return_observed": False,
        "controller_ack_observed": False,
        "readback_satisfies_controller_ack": False,
        "safe_stop_effect_observed": False,
        "parent_mission_completion_claimed": False,
        "shared_world_claimed": False,
        "identity_continuity_claimed": False,
        "physical_execution_invoked": False,
        "real_robot_execution": False,
        "real_world_safety_claimed": False,
    }


def _publication_boundary() -> dict[str, bool]:
    return {
        "raw_logs_included": False,
        "video_included": False,
        "rendered_frames_included": False,
        "raw_action_values_included": False,
        "raw_observations_included": False,
        "credentials_included": False,
        "local_paths_included": False,
    }


def _innermost_exception(error: BaseException) -> BaseException:
    current = error
    visited = {id(current)}
    while True:
        nested = current.__cause__
        if nested is None and not current.__suppress_context__:
            nested = current.__context__
        if nested is None or id(nested) in visited:
            return current
        visited.add(id(nested))
        current = nested


def _wraps_lower_layer_exception(error: BaseException) -> bool:
    """Return whether a typed refusal explicitly wraps a lower-layer failure."""

    return error.__cause__ is not None


def _relative_frame_path(
    path: Path,
    *,
    repository_root: Path,
) -> str | None:
    try:
        return path.resolve(strict=False).relative_to(
            repository_root.resolve(strict=False)
        ).as_posix()
    except ValueError:
        return None


def _exception_fingerprint(
    error: BaseException,
    *,
    isaac_groot_root: Path | None = None,
) -> dict[str, Any]:
    """Return a publication-safe source location without exception text."""

    innermost = _innermost_exception(error)
    traceback = innermost.__traceback__
    if traceback is None:
        return {
            "innermost_frame_repo": "unknown",
            "innermost_frame_path": None,
            "innermost_frame_lineno": None,
        }
    while traceback.tb_next is not None:
        traceback = traceback.tb_next

    frame_path = Path(traceback.tb_frame.f_code.co_filename)
    repository_roots: list[tuple[str, Path]] = []
    if isaac_groot_root is not None:
        repository_roots.extend(
            (
                (
                    "libero",
                    isaac_groot_root / "external_dependencies" / "LIBERO",
                ),
                ("isaac_groot", isaac_groot_root),
            )
        )
    repository_roots.append(("missionos", MISSIONOS_REPOSITORY_ROOT))
    for repository, repository_root in repository_roots:
        relative_path = _relative_frame_path(
            frame_path,
            repository_root=repository_root,
        )
        if relative_path is not None:
            return {
                "innermost_frame_repo": repository,
                "innermost_frame_path": relative_path,
                "innermost_frame_lineno": traceback.tb_lineno,
            }

    parts = frame_path.parts
    for marker in ("site-packages", "dist-packages"):
        if marker not in parts:
            continue
        relative_parts = parts[parts.index(marker) + 1 :]
        if (
            not relative_parts
            or any(part in {".", ".."} for part in relative_parts)
            or not _SAFE_PYTHON_PACKAGE_NAME.fullmatch(relative_parts[0])
        ):
            break
        return {
            "innermost_frame_repo": "python_dependency",
            "innermost_frame_package": relative_parts[0],
            "innermost_frame_path": Path(*relative_parts).as_posix(),
            "innermost_frame_lineno": traceback.tb_lineno,
        }

    standard_library = Path(sysconfig.get_paths()["stdlib"])
    relative_path = _relative_frame_path(
        frame_path,
        repository_root=standard_library,
    )
    if relative_path is not None:
        return {
            "innermost_frame_repo": "python_stdlib",
            "innermost_frame_path": relative_path,
            "innermost_frame_lineno": traceback.tb_lineno,
        }
    return {
        "innermost_frame_repo": "unknown",
        "innermost_frame_path": None,
        "innermost_frame_lineno": traceback.tb_lineno,
    }


def _failure_report(
    *,
    phase: str,
    error: BaseException,
    prepared: Any | None = None,
    revisions: dict[str, Any] | None = None,
    configuration: LIBEROPandaRunnerConfiguration | None = None,
    runtime: dict[str, Any] | None = None,
    isaac_groot_root: Path | None = None,
    maximum_rollout_elapsed_seconds: float | None = None,
    rollout_elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    event_sequence = []
    if prepared is not None:
        event_sequence = [
            event.value for event in prepared.recorder.event_sequence
        ]
    boundary = _negative_claim_boundary()
    boundary["missionos_contract_frozen_before_episode"] = (
        prepared is not None
    )
    boundary["missionos_instrumentation_in_loop"] = (
        LIBEROPandaInstrumentationEvent.RESET_OBSERVED.value
        in event_sequence
    )
    boundary["model_inference_invoked"] = (
        LIBEROPandaInstrumentationEvent.POLICY_REQUEST_INVOKED.value
        in event_sequence
    )
    boundary["simulator_step_return_observed"] = (
        LIBEROPandaInstrumentationEvent.SIMULATOR_STEP_RETURN_OBSERVED.value
        in event_sequence
    )
    report: dict[str, Any] = {
        "schema_version": (
            "missionos_groot_n17_libero_panda_live_instrumented_failure.v3"
        ),
        "recorded_at": _utc_now(),
        "result": "failed",
        "failure": {
            "phase": phase,
            "error_type": type(error).__name__,
            "rejection_reason": (
                error.rejection_reason.value
                if isinstance(error, LIBEROPandaInstrumentationError)
                else "unclassified_runtime_failure"
            ),
            "error_message_included": False,
        },
        "event_sequence": event_sequence,
        "claim_boundary": boundary,
        "publication": _publication_boundary(),
    }
    if (
        not isinstance(error, LIBEROPandaInstrumentationError)
        or _wraps_lower_layer_exception(error)
    ):
        report["failure"]["exception_fingerprint"] = (
            _exception_fingerprint(
                error,
                isaac_groot_root=isaac_groot_root,
            )
        )
    if revisions is not None:
        report["source_revisions"] = revisions
    if configuration is not None:
        report["frozen_runner_configuration"] = configuration.to_material()
    if runtime is not None:
        report["runtime"] = runtime
    if maximum_rollout_elapsed_seconds is not None:
        report["diagnostic_run_limit"] = {
            "maximum_rollout_elapsed_seconds": (
                maximum_rollout_elapsed_seconds
            ),
            "limit_source": "operator_invocation",
            "outcome_claim_effect": "none",
        }
        if rollout_elapsed_seconds is not None:
            report["diagnostic_run_limit"].update(
                {
                    "elapsed_before_failure_seconds": (
                        rollout_elapsed_seconds
                    ),
                    "elapsed_clock_basis": "process_monotonic",
                }
            )
    if prepared is not None:
        report["prepared_episode"] = {
            "run_identity": prepared.run_identity,
            "episode_identity": prepared.episode_identity,
            "contract_sha256": canonical_sha256(
                prepared.contract.to_material()
            ),
            "prepared_at": prepared.prepared_at,
        }
    return report


def execute_live_episode(
    *,
    model_path: Path,
    reference_model_path: Path,
    maximum_observation_age_seconds: float,
    maximum_rollout_elapsed_seconds: float,
    policy_client_host: str,
    policy_client_port: int,
    run_identity: str | None = None,
    episode_identity: str | None = None,
    expected_contract_sha256: str | None = None,
) -> dict[str, Any]:
    maximum_rollout_elapsed_seconds = _require_positive_finite_seconds(
        maximum_rollout_elapsed_seconds
    )
    os.environ.setdefault("MUJOCO_GL", "egl")
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")

    import torch
    from gr00t.data.embodiment_tags import EmbodimentTag
    from gr00t.eval import rollout_policy as rollout_module

    isaac_root = Path(rollout_module.__file__).resolve().parents[2]
    revisions: dict[str, Any] | None = None
    configuration: LIBEROPandaRunnerConfiguration | None = None
    runtime: dict[str, Any] | None = None
    prepared: Any | None = None
    rollout_started: float | None = None
    phase = "source_and_checkpoint_verification"
    try:
        revisions = verify_source_and_checkpoint_revisions(
            isaac_groot_root=isaac_root,
            model_path=model_path,
            reference_model_path=reference_model_path,
        )
        phase = "runtime_dependency_verification"
        runtime = runtime_environment(torch)
        verify_runtime_dependency_profile(runtime)
        configuration = build_runner_configuration()

        # Identities, contract, contract digest, and the first recorder event
        # exist before policy creation and before the official runner can
        # create/reset an environment.
        phase = "contract_preparation"
        prepared = prepare_libero_panda_instrumented_episode(
            runner_configuration=configuration,
            maximum_observation_age_seconds=(
                maximum_observation_age_seconds
            ),
            run_identity=run_identity,
            episode_identity=episode_identity,
        )
        prepared_contract_sha256 = canonical_sha256(
            prepared.contract.to_material()
        )
        verify_expected_contract_binding(
            prepared_contract_sha256=prepared_contract_sha256,
            expected_contract_sha256=expected_contract_sha256,
        )

        phase = "policy_client_creation"
        policy_client_started = time.monotonic()
        policy = rollout_module.create_gr00t_sim_policy(
            "",
            EmbodimentTag.LIBERO_PANDA,
            policy_client_host,
            policy_client_port,
        )
        policy_client_creation_elapsed_seconds = (
            time.monotonic() - policy_client_started
        )

        wrapper_configs = rollout_module.WrapperConfigs(
            video=rollout_module.VideoConfig(
                video_dir=None,
                max_episode_steps=MAXIMUM_EPISODE_STEPS,
                n_action_steps=N_ACTION_STEPS,
            ),
            multistep=rollout_module.MultiStepConfig(
                n_action_steps=N_ACTION_STEPS,
                max_episode_steps=MAXIMUM_EPISODE_STEPS,
                terminate_on_success=True,
            ),
        )

        phase = "official_rollout"
        rollout_started_at = _utc_now()
        rollout_started = time.monotonic()
        with _rollout_deadline(maximum_rollout_elapsed_seconds):
            result = run_instrumented_official_libero_rollout(
                rollout_module=rollout_module,
                policy=policy,
                wrapper_configs=wrapper_configs,
                prepared=prepared,
                controller_probe=live_controller_probe,
            )
        rollout_elapsed_seconds = time.monotonic() - rollout_started
        phase = "evidence_assembly"
        serialized_result = result.to_dict()
        predicate = serialized_result["predicate_evaluation"]
        ordering = pre_reset_ordering(result, prepared=prepared)
    except Exception as error:
        raise LiveEpisodeExecutionError(
            _failure_report(
                phase=phase,
                error=error,
                prepared=prepared,
                revisions=revisions,
                configuration=configuration,
                runtime=runtime,
                isaac_groot_root=isaac_root,
                maximum_rollout_elapsed_seconds=(
                    maximum_rollout_elapsed_seconds
                ),
                rollout_elapsed_seconds=(
                    time.monotonic() - rollout_started
                    if rollout_started is not None
                    else None
                ),
            )
        ) from error

    return {
        "schema_version": (
            "missionos_groot_n17_libero_panda_live_instrumented_run.v2"
        ),
        "recorded_at": _utc_now(),
        "source_revisions": revisions,
        "frozen_runner_configuration": configuration.to_material(),
        "pre_reset_binding": ordering,
        "execution": {
            "scope": "sim",
            "episode_count": 1,
            "official_episode_success": (
                result.content.official_runner_episode_success
            ),
            "success_rate_claimed": False,
            "benchmark_result_claimed": False,
            "policy_transport": "zmq_policy_client",
            "policy_client_creation_elapsed_seconds": (
                policy_client_creation_elapsed_seconds
            ),
            "rollout_started_at": rollout_started_at,
            "rollout_elapsed_seconds": rollout_elapsed_seconds,
            "maximum_rollout_elapsed_seconds": (
                maximum_rollout_elapsed_seconds
            ),
        },
        "runtime": runtime,
        "instrumented_episode": serialized_result,
        "claim_boundary": {
            "missionos_contract_frozen_before_episode": True,
            "missionos_instrumentation_in_loop": True,
            "missionos_parent_coordinator_in_live_loop": False,
            "model_inference_invoked": (
                "policy_request_invoked"
                in serialized_result["event_sequence"]
            ),
            "simulator_step_return_observed": (
                "simulator_step_return_observed"
                in serialized_result["event_sequence"]
            ),
            "predicate_status": predicate["status"],
            "evaluated_outcome_claim": predicate[
                "evaluated_outcome_claim"
            ],
            "controller_ack_observed": False,
            "readback_satisfies_controller_ack": False,
            "safe_stop_effect_observed": False,
            "parent_mission_completion_claimed": False,
            "shared_world_claimed": False,
            "identity_continuity_claimed": False,
            "physical_execution_invoked": False,
            "real_robot_execution": False,
            "real_world_safety_claimed": False,
        },
        "publication": _publication_boundary(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument(
        "--reference-model-path",
        type=Path,
        help=(
            "Original pinned checkpoint used to verify an offline-only "
            "processor-locator override. Defaults to --model-path."
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy-client-host", default="127.0.0.1")
    parser.add_argument("--policy-client-port", type=int, default=5555)
    parser.add_argument("--run-identity")
    parser.add_argument("--episode-identity")
    parser.add_argument("--expected-contract-sha256")
    parser.add_argument(
        "--maximum-observation-age-seconds",
        type=float,
        default=30.0,
    )
    parser.add_argument(
        "--maximum-rollout-elapsed-seconds",
        type=_positive_finite_seconds,
        default=600.0,
        help=(
            "Operator-supplied diagnostic cap. Exceeding it records a "
            "failure and never implies task completion."
        ),
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if (args.run_identity is None) != (args.episode_identity is None):
        raise SystemExit(
            "--run-identity and --episode-identity must be supplied together"
        )
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("refusing to overwrite an existing run artifact")
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        report = execute_live_episode(
            model_path=args.model_path.resolve(),
            reference_model_path=(
                args.reference_model_path.resolve()
                if args.reference_model_path is not None
                else args.model_path.resolve()
            ),
            maximum_observation_age_seconds=(
                args.maximum_observation_age_seconds
            ),
            maximum_rollout_elapsed_seconds=(
                args.maximum_rollout_elapsed_seconds
            ),
            policy_client_host=args.policy_client_host,
            policy_client_port=args.policy_client_port,
            run_identity=args.run_identity,
            episode_identity=args.episode_identity,
            expected_contract_sha256=args.expected_contract_sha256,
        )
    except LiveEpisodeExecutionError as error:
        report = error.report
        output.write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "schema_version": report["schema_version"],
                    "recorded_at": report["recorded_at"],
                    "result": "failed",
                    "failure_phase": report["failure"]["phase"],
                    "parent_mission_completion_claimed": False,
                    "physical_execution_invoked": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        raise SystemExit(1) from error
    except Exception as error:
        report = _failure_report(
            phase="client_initialization",
            error=error,
        )
        output.write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "schema_version": report["schema_version"],
                    "recorded_at": report["recorded_at"],
                    "result": "failed",
                    "failure_phase": report["failure"]["phase"],
                    "parent_mission_completion_claimed": False,
                    "physical_execution_invoked": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        raise SystemExit(1) from error
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "schema_version": report["schema_version"],
                "recorded_at": report["recorded_at"],
                "predicate_status": report["claim_boundary"][
                    "predicate_status"
                ],
                "evaluated_outcome_claim": report["claim_boundary"][
                    "evaluated_outcome_claim"
                ],
                "controller_ack_observed": False,
                "parent_mission_completion_claimed": False,
                "physical_execution_invoked": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
