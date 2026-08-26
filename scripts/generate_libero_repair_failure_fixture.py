#!/usr/bin/env python3
"""Generate one explicit, diagnostic-only LIBERO Repair failure fixture."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from missionos_core import canonical_sha256  # noqa: E402
from scripts.run_groot_lerobot_same_world_repair import (  # noqa: E402
    CHECKPOINT_REPOSITORY,
    CHECKPOINT_REVISION,
    LEROBOT_REVISION,
    TASK_ID,
    TASK_SUITE,
    _git_revision,
    _predicate_material,
    _read_failure_snapshot,
    _sha256_path,
    _write_failure_snapshot,
)
from src.runtime.libero_repair_failure_fixture import (  # noqa: E402
    FAILURE_FIXTURE_SPECS,
    SCRIPTED_FAILURE_FIXTURE_BASIS,
    failure_fixture_contract,
    failure_fixture_spec,
    inject_failure_fixture,
)


OPT_IN_ENV = "RUN_MISSIONOS_LIBERO_REPAIR_FAILURE_FIXTURE"


class _PredicateEnvironment:
    def __init__(self, environment: Any) -> None:
        self.environment = environment


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _render(simulator: Any, path: Path) -> dict[str, Any]:
    import numpy as np
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    pixels = simulator.sim.render(camera_name="agentview", height=512, width=512)
    Image.fromarray(np.asarray(pixels, dtype=np.uint8)[::-1]).save(path)
    return {"file_name": path.name, "sha256": _sha256_path(path)}


def generate_fixture(
    *,
    base_snapshot_path: Path,
    scenario: str,
    output_snapshot_path: Path,
    render_dir: Path,
) -> dict[str, Any]:
    if output_snapshot_path.exists():
        raise ValueError("libero_repair_failure_fixture_output_exists")
    spec = failure_fixture_spec(scenario)

    os.environ.setdefault("MUJOCO_GL", "osmesa")
    os.environ.setdefault("PYOPENGL_PLATFORM", "osmesa")

    import numpy as np
    from lerobot.envs.configs import LiberoEnv as LiberoEnvConfig
    from lerobot.envs.libero import LiberoEnv as LiberoEnvironment
    from lerobot.envs.libero import _get_suite

    import lerobot

    simulator_state, base_metadata = _read_failure_snapshot(base_snapshot_path)
    if base_metadata.get("task_suite") != TASK_SUITE:
        raise ValueError("libero_repair_failure_fixture_task_suite_mismatch")
    if base_metadata.get("task_id") != TASK_ID:
        raise ValueError("libero_repair_failure_fixture_task_id_mismatch")
    episode_init_state_index = base_metadata.get("episode_init_state_index")
    if isinstance(episode_init_state_index, bool) or not isinstance(
        episode_init_state_index, int
    ):
        raise ValueError("libero_repair_failure_fixture_init_state_invalid")

    observed_lerobot_revision = _git_revision(Path(lerobot.__file__).resolve().parents[2])
    if observed_lerobot_revision != LEROBOT_REVISION:
        raise RuntimeError("lerobot_source_revision_mismatch")

    camera_mapping = {
        "agentview_image": "image",
        "robot0_eye_in_hand_image": "wrist_image",
    }
    env_config = LiberoEnvConfig(
        task=TASK_SUITE,
        task_ids=[TASK_ID],
        episode_length=spec.settle_steps + 40,
        observation_height=256,
        observation_width=256,
        camera_name_mapping=camera_mapping,
    )
    environment = LiberoEnvironment(
        task_suite=_get_suite(TASK_SUITE),
        task_id=TASK_ID,
        task_suite_name=TASK_SUITE,
        episode_length=spec.settle_steps + 40,
        obs_type=env_config.obs_type,
        observation_height=256,
        observation_width=256,
        camera_name_mapping=camera_mapping,
        episode_index=episode_init_state_index,
        n_envs=1,
    )
    predicate_environment = _PredicateEnvironment(environment)
    try:
        environment.reset(seed=0)
        raw = environment._env.regenerate_obs_from_state(simulator_state)
        environment._format_raw_obs(raw)
        restored_state = np.asarray(
            environment._env.get_sim_state(), dtype=np.float64
        ).reshape(-1)
        if not np.array_equal(restored_state, simulator_state):
            raise RuntimeError("libero_repair_failure_fixture_base_restore_not_exact")
        before_predicates = _predicate_material(predicate_environment)
        before_vector = [item["satisfied"] for item in before_predicates]
        if before_vector != [True, False, True]:
            raise RuntimeError("libero_repair_failure_fixture_base_vector_invalid")

        simulator = environment._env.env
        before_render = _render(simulator, render_dir / "before.png")
        _, fixture = inject_failure_fixture(
            environment=environment,
            scenario=scenario,
            observe_goal_predicates=lambda: _predicate_material(predicate_environment),
        )
        after_render = _render(simulator, render_dir / "after.png")
        output_state = np.asarray(
            environment._env.get_sim_state(), dtype=np.float64
        ).reshape(-1)
        source_contract = {
            "task_suite": TASK_SUITE,
            "task_id": TASK_ID,
            "episode_init_state_index": episode_init_state_index,
            "base_snapshot_sha256": base_metadata["snapshot_artifact_sha256"],
            "fixture_contract": failure_fixture_contract(scenario),
            "fixture_observation": fixture,
            "authority": "test_fixture_only",
        }
        source_contract_sha256 = canonical_sha256(source_contract)
        snapshot = _write_failure_snapshot(
            path=output_snapshot_path,
            simulator_state=output_state,
            metadata={
                "task_suite": TASK_SUITE,
                "task_id": TASK_ID,
                "episode_init_state_index": episode_init_state_index,
                "checkpoint_repository": CHECKPOINT_REPOSITORY,
                "checkpoint_revision": CHECKPOINT_REVISION,
                "lerobot_revision": observed_lerobot_revision,
                "source_contract_sha256": source_contract_sha256,
                "source_steps_executed": 0,
                "source_goal_predicate_observations": fixture[
                    "terminal_goal_predicate_observations"
                ],
                "source_goal_predicate_vector": fixture[
                    "terminal_goal_predicate_vector"
                ],
                "source_goal_predicate_vector_sha256": canonical_sha256(
                    {
                        "goal_predicate_observations": fixture[
                            "terminal_goal_predicate_observations"
                        ]
                    }
                ),
                "source_failure_basis": SCRIPTED_FAILURE_FIXTURE_BASIS,
                "scripted_failure_fixture": fixture,
                "source_failure_is_repair_candidate": True,
                "model_runtime_invoked_for_snapshot_restore": False,
                "physical_execution_invoked": False,
            },
        )
        result = {
            "schema_version": "missionos.libero_repair_failure_fixture_generation.v1",
            "status": "stable_scripted_failure_fixture_generated",
            "scenario": scenario,
            "source_failure_basis": SCRIPTED_FAILURE_FIXTURE_BASIS,
            "base_snapshot_sha256": base_metadata["snapshot_artifact_sha256"],
            "fixture": fixture,
            "output_snapshot": snapshot,
            "renders": {"before": before_render, "after": after_render},
            "source_contract_sha256": source_contract_sha256,
            "claim_boundary": {
                "test_fixture_created": True,
                "repair_proposal_created": False,
                "human_approval_created": False,
                "governed_dispatch_created": False,
                "model_inference_invoked": False,
                "repair_attempted": False,
                "semantic_repair_established": False,
                "physical_execution_invoked": False,
            },
        }
        result["result_sha256"] = canonical_sha256(result)
        return result
    finally:
        environment.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-snapshot", type=Path, required=True)
    parser.add_argument("--scenario", choices=sorted(FAILURE_FIXTURE_SPECS), required=True)
    parser.add_argument("--output-snapshot", type=Path, required=True)
    parser.add_argument("--render-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get(OPT_IN_ENV) != "1":
        print(json.dumps({"status": "not_run", "required_opt_in": OPT_IN_ENV}))
        return 3
    report = generate_fixture(
        base_snapshot_path=args.base_snapshot.resolve(),
        scenario=args.scenario,
        output_snapshot_path=args.output_snapshot.resolve(),
        render_dir=args.render_dir.resolve(),
    )
    _write_json(args.output.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
