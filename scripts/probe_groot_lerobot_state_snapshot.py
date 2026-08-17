#!/usr/bin/env python3
"""Probe bounded LIBERO simulator-state snapshot and restore without loading GR00T."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from missionos_core import canonical_sha256  # noqa: E402
from src.runtime.libero_panda_official_runner_instrumentation import (  # noqa: E402
    _observe_libero_goal_predicates,
)
from src.runtime.libero_panda_predicate_package import (  # noqa: E402
    LIBERO_PANDA_SCENE8_ENVIRONMENT,
)


OPT_IN_ENV = "RUN_MISSIONOS_GROOT_LEROBOT_STATE_SNAPSHOT_PROBE"
TASK_SUITE = "libero_10"
TASK_ID = 8


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode-init-state-index", type=int, default=9)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--snapshot-out", type=Path)
    return parser


def _predicates(underlying: Any) -> list[dict[str, Any]]:
    return [
        item.to_material()
        for item in _observe_libero_goal_predicates(
            underlying,
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        )
    ]


def _write_output(path: Path | None, result: dict[str, Any]) -> None:
    encoded = json.dumps(result, sort_keys=True, indent=2) + "\n"
    print(encoded, end="")
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


def _write_snapshot(path: Path, snapshot: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("wb") as stream:
        import numpy as np

        np.save(stream, snapshot, allow_pickle=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = _parser().parse_args()
    if os.environ.get(OPT_IN_ENV) != "1":
        raise SystemExit(f"set {OPT_IN_ENV}=1 to run this simulator probe")

    import numpy as np
    from lerobot.envs.configs import LiberoEnv as LiberoEnvConfig
    from lerobot.envs.libero import LiberoEnv as LiberoEnvironment
    from lerobot.envs.libero import _get_suite

    camera_mapping = {
        "agentview_image": "image",
        "robot0_eye_in_hand_image": "wrist_image",
    }
    env_config = LiberoEnvConfig(
        task=TASK_SUITE,
        task_ids=[TASK_ID],
        episode_length=64,
        observation_height=256,
        observation_width=256,
        camera_name_mapping=camera_mapping,
    )
    environment = LiberoEnvironment(
        task_suite=_get_suite(TASK_SUITE),
        task_id=TASK_ID,
        task_suite_name=TASK_SUITE,
        episode_length=64,
        obs_type=env_config.obs_type,
        observation_height=256,
        observation_width=256,
        camera_name_mapping=camera_mapping,
        episode_index=args.episode_init_state_index,
        n_envs=1,
    )

    try:
        environment.reset(seed=0)
        underlying = environment._env
        simulator = underlying.env.sim
        snapshot = np.asarray(underlying.get_sim_state(), dtype=np.float64).copy()
        initial_predicates = _predicates(underlying)
        snapshot_file_sha256 = None
        restore_source = snapshot.copy()
        if args.snapshot_out is not None:
            snapshot_file_sha256 = _write_snapshot(args.snapshot_out, snapshot)
            restore_source = np.load(args.snapshot_out, allow_pickle=False)

        simulator.data.qpos[0] += 0.05
        simulator.data.qvel[0] += 0.01
        simulator.forward()
        underlying._post_process()
        underlying._update_observables(force=True)
        mutated = np.asarray(underlying.get_sim_state(), dtype=np.float64).copy()

        underlying.regenerate_obs_from_state(restore_source)
        restored = np.asarray(underlying.get_sim_state(), dtype=np.float64).copy()
        restored_predicates = _predicates(underlying)

        result = {
            "schema_version": "missionos.groot_lerobot_state_snapshot_probe.v1",
            "task_suite": TASK_SUITE,
            "task_id": TASK_ID,
            "episode_init_state_index": args.episode_init_state_index,
            "model_runtime_invoked": False,
            "physical_execution_invoked": False,
            "snapshot_value_count": int(snapshot.size),
            "snapshot_sha256": canonical_sha256({"state": snapshot.tolist()}),
            "snapshot_file_sha256": snapshot_file_sha256,
            "snapshot_disk_round_trip_tested": args.snapshot_out is not None,
            "snapshot_disk_round_trip_exactly_equal": bool(
                np.array_equal(snapshot, restore_source)
            ),
            "mutation_observed": not np.array_equal(snapshot, mutated),
            "restored_state_exactly_equal": bool(np.array_equal(snapshot, restored)),
            "restored_state_maximum_absolute_error": float(np.max(np.abs(snapshot - restored))),
            "initial_goal_predicates": initial_predicates,
            "restored_goal_predicates": restored_predicates,
            "restored_goal_predicates_equal": initial_predicates == restored_predicates,
            "claim_boundary": {
                "simulator_physics_state_snapshot_restore_tested": True,
                "policy_action_queue_restored": False,
                "python_numpy_torch_rng_restored": False,
                "environment_episode_counters_restored": False,
                "controller_internal_state_restored": False,
                "bit_for_bit_trajectory_replay_established": False,
                "semantic_repair_established": False,
            },
        }
        result["probe_passed"] = bool(
            result["mutation_observed"]
            and result["restored_state_exactly_equal"]
            and result["restored_goal_predicates_equal"]
            and (
                not result["snapshot_disk_round_trip_tested"]
                or result["snapshot_disk_round_trip_exactly_equal"]
            )
        )
        result["result_sha256"] = canonical_sha256(result)
        _write_output(args.output, result)
        return 0 if result["probe_passed"] else 2
    finally:
        environment.close()


if __name__ == "__main__":
    raise SystemExit(main())
