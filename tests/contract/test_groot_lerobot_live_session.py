from __future__ import annotations

from collections import deque
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from missionos_core import canonical_sha256
from scripts import run_groot_lerobot_same_world_repair as live_runner
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.runtime.groot_lerobot_live_session import (
    LeRobotActionChunkExecutionError,
    LeRobotLiveSession,
    SelectedAction,
    batch_single_environment_observation,
    verify_huggingface_local_snapshot,
)
from src.runtime.groot_lerobot_same_world_repair import (
    build_lerobot_same_world_repair_proposal,
    run_lerobot_same_world_repair,
)
from src.runtime.groot_libero_same_world_repair import (
    approve_same_world_repair,
    build_same_world_repair_dispatch,
)
from src.runtime.libero_panda_predicate_package import LIBERO_PANDA_SCENE8_ENVIRONMENT


def test_live_candidate_profile_matches_pinned_baseline() -> None:
    assert live_runner.TASK_SUITE == "libero_10"
    assert live_runner.TASK_ID == 8
    assert live_runner.EPISODE_INIT_STATE_INDEX == 1
    assert live_runner.PROCESS_SEED == 0
    assert live_runner.ENVIRONMENT_SEED == 0
    assert live_runner.SOURCE_STEP_BUDGET == 520
    assert live_runner.REPAIR_CANDIDATE_VECTORS == (
        (False, True, True),
        (True, False, True),
    )


def test_git_revision_scopes_safe_directory_without_global_mutation(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="6adf51511b7625090eade8d82d9f61a1846ebe56\n")

    monkeypatch.setattr(live_runner.subprocess, "run", fake_run)
    repository = Path("/opt/lerobot")

    assert live_runner._git_revision(repository) == live_runner.LEROBOT_REVISION
    assert calls == [
        (
            [
                "git",
                "-c",
                "safe.directory=/opt/lerobot",
                "-C",
                "/opt/lerobot",
                "rev-parse",
                "HEAD",
            ],
            {"check": True, "capture_output": True, "text": True},
        )
    ]


@pytest.mark.parametrize(
    ("steps", "budget", "vector", "expected"),
    (
        (479, 480, [False, True, True], False),
        (480, 480, [False, True, True], True),
        (480, 480, [True, True, True], False),
    ),
)
def test_source_budget_exhaustion_requires_full_budget_and_incomplete_vector(
    steps: int,
    budget: int,
    vector: list[bool],
    expected: bool,
) -> None:
    assert (
        live_runner._source_budget_exhausted(
            source_steps_executed=steps,
            source_step_budget=budget,
            source_goal_predicate_vector=vector,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("vector", "expected"),
    (
        ([False, True, True], True),
        ([True, False, True], True),
        ([False, False, True], False),
        ([True, True, True], False),
    ),
)
def test_repair_candidate_accepts_runtime_list_vectors(
    vector: list[bool],
    expected: bool,
) -> None:
    assert live_runner._is_repair_candidate(vector) is expected


def test_post_hoc_budget_repair_cannot_create_unqualified_semantic_repair_claim() -> None:
    claims = live_runner._repair_claims(
        repair_completion_established=True,
        source_budget_exhausted=True,
        source_failure_basis="post_hoc_reference_success_truncation",
    )

    assert claims == {
        "semantic_repair_established": False,
        "budget_truncated_source_semantic_repair_established": True,
    }


@pytest.mark.parametrize("source_failure_basis", sorted(live_runner.SOURCE_FAILURE_BASES))
def test_live_harness_cannot_create_unqualified_semantic_repair_claim(
    source_failure_basis: str,
) -> None:
    claims = live_runner._repair_claims(
        repair_completion_established=True,
        source_budget_exhausted=True,
        source_failure_basis=source_failure_basis,
    )

    assert claims["semantic_repair_established"] is False


def test_natural_full_budget_screen_can_establish_semantic_repair() -> None:
    claims = live_runner._repair_claims(
        repair_completion_established=True,
        source_budget_exhausted=True,
        source_failure_basis=live_runner.NATURAL_SCREEN_FAILURE_BASIS,
        natural_task_failure_established=True,
    )

    assert claims == {
        "semantic_repair_established": True,
        "budget_truncated_source_semantic_repair_established": False,
    }


def test_screen_reuses_one_process_and_stops_on_first_natural_candidate(
    monkeypatch, tmp_path
) -> None:
    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(live_runner, "_LIVE_POLICY_LOAD_COUNT", 0)

    def fake_execute_live(**kwargs):
        index = kwargs["episode_init_state_index"]
        calls.append((index, kwargs["natural_screen_mode"]))
        if len(calls) == 1:
            live_runner._LIVE_POLICY_LOAD_COUNT += 1
            live_runner._LIVE_POLICY_CACHE = object()
        candidate = index == 1
        vector = [False, True, True] if candidate else [True, True, True]
        return {
            "result": "satisfied" if candidate else "source_satisfied_no_repair_needed",
            "source": {"source_steps_executed": 520},
            "source_goal_predicate_vector": vector,
            "natural_task_failure_established": candidate,
            "repair_executed": candidate,
            "semantic_repair_established": candidate,
        }

    monkeypatch.setattr(live_runner, "execute_live", fake_execute_live)

    report = live_runner.execute_live_screen(
        checkpoint_path=tmp_path,
        operator_approval_ref="operator:test",
        dispatch_state_path=tmp_path / "dispatch.json",
        maximum_repair_chunks=45,
        episode_init_state_indices=(0, 1, 2),
    )

    assert calls == [(0, True), (1, True)]
    assert report["semantic_repair_established"] is True
    assert report["screen"]["selected_init_state_index"] == 1
    assert report["screen"]["model_initialization_count_during_screen"] == 1
    assert report["screen"]["same_loaded_policy_reused_across_episodes"] is True


def test_screen_atomically_retains_completed_episode_progress(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(live_runner, "_LIVE_POLICY_LOAD_COUNT", 0)
    monkeypatch.setattr(live_runner, "_LIVE_POLICY_CACHE", object())

    def fake_execute_live(**kwargs):
        index = kwargs["episode_init_state_index"]
        return {
            "result": "source_satisfied_no_repair_needed",
            "source": {"source_steps_executed": 362 + index},
            "source_goal_predicate_vector": [True, True, True],
            "natural_task_failure_established": False,
            "repair_executed": False,
            "semantic_repair_established": False,
        }

    monkeypatch.setattr(live_runner, "execute_live", fake_execute_live)
    progress = tmp_path / "nested" / "screen-progress.json"

    report = live_runner.execute_live_screen(
        checkpoint_path=tmp_path,
        operator_approval_ref="operator:test",
        dispatch_state_path=tmp_path / "dispatch.json",
        maximum_repair_chunks=45,
        episode_init_state_indices=(2, 3),
        progress_output=progress,
    )

    stored = __import__("json").loads(progress.read_text())
    assert report["result"] == "no_natural_asymmetric_failure_observed"
    assert stored["completed_episode_count"] == 2
    assert [item["episode_init_state_index"] for item in stored["episodes_screened"]] == [
        2,
        3,
    ]
    assert stored["repair_authority_created_by_progress_artifact"] is False
    assert stored["semantic_repair_established"] is False


def test_screen_assigns_a_distinct_snapshot_path_to_every_episode(monkeypatch, tmp_path) -> None:
    observed_paths = []
    monkeypatch.setattr(live_runner, "_LIVE_POLICY_LOAD_COUNT", 0)
    monkeypatch.setattr(live_runner, "_LIVE_POLICY_CACHE", object())

    def fake_execute_live(**kwargs):
        observed_paths.append(kwargs["failure_snapshot_path"])
        return {
            "result": "source_vector_not_repair_candidate",
            "source": {"source_steps_executed": 520},
            "source_goal_predicate_vector": [False, False, True],
            "natural_task_failure_established": False,
            "repair_executed": False,
            "semantic_repair_established": False,
        }

    monkeypatch.setattr(live_runner, "execute_live", fake_execute_live)
    snapshot_dir = tmp_path / "snapshots"

    live_runner.execute_live_screen(
        checkpoint_path=tmp_path,
        operator_approval_ref="operator:test",
        dispatch_state_path=tmp_path / "dispatch.json",
        maximum_repair_chunks=45,
        episode_init_state_indices=(2, 3),
        failure_snapshot_dir=snapshot_dir,
    )

    assert observed_paths == [
        snapshot_dir / "screen-000-init-2.npz",
        snapshot_dir / "screen-001-init-3.npz",
    ]


def test_screen_reports_policy_reinitialization_instead_of_claiming_reuse(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(live_runner, "_LIVE_POLICY_LOAD_COUNT", 0)
    monkeypatch.setattr(live_runner, "_LIVE_POLICY_CACHE", object())

    def fake_execute_live(**kwargs):
        live_runner._LIVE_POLICY_LOAD_COUNT += 1
        return {
            "result": "source_vector_not_repair_candidate",
            "source": {"source_steps_executed": 520},
            "source_goal_predicate_vector": [False, False, True],
            "natural_task_failure_established": False,
            "repair_executed": False,
            "semantic_repair_established": False,
        }

    monkeypatch.setattr(live_runner, "execute_live", fake_execute_live)

    report = live_runner.execute_live_screen(
        checkpoint_path=tmp_path,
        operator_approval_ref="operator:test",
        dispatch_state_path=tmp_path / "dispatch.json",
        maximum_repair_chunks=45,
        episode_init_state_indices=(0, 1),
    )

    assert report["model_initialization_count_during_screen"] == 2
    assert report["same_loaded_policy_reused_across_episodes"] is False


def test_screen_rejects_more_than_cost_bounded_init_states(tmp_path) -> None:
    with pytest.raises(ValueError, match="screen_init_state_limit_exceeded"):
        live_runner.execute_live_screen(
            checkpoint_path=tmp_path,
            operator_approval_ref="operator:test",
            dispatch_state_path=tmp_path / "dispatch.json",
            maximum_repair_chunks=45,
            episode_init_state_indices=tuple(range(live_runner.MAX_SCREEN_INIT_STATES + 1)),
        )


def test_screen_accepts_the_full_cost_bounded_diagnostic_budget(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(live_runner, "_LIVE_POLICY_LOAD_COUNT", 0)
    monkeypatch.setattr(live_runner, "_LIVE_POLICY_CACHE", object())

    def fake_execute_live(**kwargs):
        return {
            "result": "source_satisfied_no_repair_needed",
            "source": {"source_steps_executed": 320},
            "source_goal_predicate_vector": [True, True, True],
            "natural_task_failure_established": False,
            "repair_executed": False,
            "semantic_repair_established": False,
        }

    monkeypatch.setattr(live_runner, "execute_live", fake_execute_live)
    indices = tuple(range(live_runner.MAX_SCREEN_INIT_STATES))

    report = live_runner.execute_live_screen(
        checkpoint_path=tmp_path,
        operator_approval_ref="operator:test",
        dispatch_state_path=tmp_path / "dispatch.json",
        maximum_repair_chunks=45,
        episode_init_state_indices=indices,
    )

    assert report["requested_init_state_indices"] == list(indices)
    assert len(report["episodes_screened"]) == live_runner.MAX_SCREEN_INIT_STATES
    assert report["repair_executed"] is False


def test_reset_stabilization_steps_are_observed_and_fail_closed() -> None:
    class Environment:
        num_steps_wait = live_runner.SIMULATOR_RESET_STABILIZATION_STEPS

    assert (
        live_runner._observed_reset_stabilization_steps(Environment())
        == live_runner.SIMULATOR_RESET_STABILIZATION_STEPS
    )

    Environment.num_steps_wait += 1
    with pytest.raises(RuntimeError, match="reset_stabilization_steps_mismatch"):
        live_runner._observed_reset_stabilization_steps(Environment())


def test_screen_rejects_operator_supplied_failure_basis(monkeypatch, tmp_path) -> None:
    output = tmp_path / "result.json"
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    monkeypatch.setenv(live_runner.OPT_IN_ENV, "1")
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_groot_lerobot_same_world_repair.py",
            "--checkpoint-path",
            str(checkpoint),
            "--operator-approval-ref",
            "operator:test",
            "--dispatch-state-path",
            str(tmp_path / "dispatch.json"),
            "--output",
            str(output),
            "--screen-init-state-index",
            "0",
            "--source-failure-basis",
            "post_hoc_reference_success_truncation",
        ],
    )

    assert live_runner.main() == 2
    report = __import__("json").loads(output.read_text())
    assert report["result"] == "execution_failed"
    assert report["semantic_repair_established"] is False


def test_natural_screen_rejects_post_hoc_truncated_source_budget(tmp_path) -> None:
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()

    with pytest.raises(ValueError, match="requires_frozen_full_source_budget"):
        live_runner.execute_live(
            checkpoint_path=checkpoint,
            operator_approval_ref="operator:test",
            dispatch_state_path=tmp_path / "dispatch.json",
            maximum_repair_chunks=45,
            episode_init_state_index=0,
            source_step_budget=496,
            natural_screen_mode=True,
        )


def test_live_dispatch_supplies_a_nonempty_unique_dispatch_reference(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_build_dispatch(**kwargs):
        captured.update(kwargs)
        return {"dispatch_ref": kwargs["dispatch_ref"]}

    monkeypatch.setattr(live_runner, "build_same_world_repair_dispatch", fake_build_dispatch)

    dispatch = live_runner._build_live_dispatch(proposal={}, approval={})

    assert dispatch["dispatch_ref"].startswith("groot-lerobot-repair-dispatch:")
    assert captured["dispatch_ref"] == dispatch["dispatch_ref"]


def test_init_state_selection_observer_verifies_argument_and_index() -> None:
    class _Backend:
        def __init__(self) -> None:
            self.selected = None

        def set_init_state(self, init_state):
            self.selected = init_state
            return {"selected": init_state.tolist()}

    class _Environment:
        def __init__(self) -> None:
            self._init_states = np.asarray([[1.0, 2.0], [3.0, 4.0]])
            self._reset_stride = 1
            self.init_state_id = 1
            self._env = _Backend()

        def _ensure_env(self) -> None:
            return None

        def reset(self):
            selected = self._init_states[self.init_state_id % len(self._init_states)]
            self.init_state_id += self._reset_stride
            return self._env.set_init_state(selected)

    environment = _Environment()
    observer = live_runner._InitStateSelectionObserver(environment, expected_index=1)

    environment.reset()
    evidence = observer.verify_after_reset(environment)

    assert evidence["requested_index"] == 1
    assert evidence["selected_index"] == 1
    assert evidence["set_init_state_call_count"] == 1
    assert evidence["observed_init_state_sha256"] == evidence["expected_init_state_sha256"]
    assert evidence["selection_verified"] is True


def test_init_state_selection_observer_rejects_wrong_selected_state() -> None:
    class _Backend:
        def set_init_state(self, init_state):
            return init_state

    class _Environment:
        def __init__(self) -> None:
            self._init_states = np.asarray([[1.0], [2.0]])
            self._reset_stride = 1
            self.init_state_id = 1
            self._env = _Backend()

        def _ensure_env(self) -> None:
            return None

    environment = _Environment()
    observer = live_runner._InitStateSelectionObserver(environment, expected_index=1)
    environment._env.set_init_state(environment._init_states[0])
    environment.init_state_id += 1

    with pytest.raises(RuntimeError, match="lerobot_init_state_selection_mismatch"):
        observer.verify_after_reset(environment)


def test_init_state_selection_observer_rejects_out_of_range_requested_index() -> None:
    class _Environment:
        def __init__(self) -> None:
            self._init_states = np.asarray([[1.0], [2.0]])

    with pytest.raises(RuntimeError, match="lerobot_init_state_index_out_of_range"):
        live_runner._InitStateSelectionObserver(_Environment(), expected_index=5)


def test_single_environment_observation_matches_vector_batch_shape() -> None:
    observation = {
        "pixels": {"image": np.zeros((8, 8, 3), dtype=np.uint8)},
        "robot_state": {
            "eef": {
                "pos": np.zeros(3, dtype=np.float64),
                "quat": np.zeros(4, dtype=np.float64),
            }
        },
    }

    batched = batch_single_environment_observation(observation)

    assert batched["pixels"]["image"].shape == (1, 8, 8, 3)
    assert batched["robot_state"]["eef"]["pos"].shape == (1, 3)
    assert batched["robot_state"]["eef"]["quat"].shape == (1, 4)


def _vector(*, first: bool = False, second: bool = True, stove: bool = True) -> list[dict]:
    predicates = [
        ("on", ["moka_pot_1", "flat_stove_1_cook_region"], first),
        ("on", ["moka_pot_2", "flat_stove_1_cook_region"], second),
        ("turnon", ["flat_stove_1"], stove),
    ]
    return [
        {
            "predicate_index": index,
            "predicate_id": canonical_sha256(
                {
                    "predicate_index": index,
                    "predicate_name": name,
                    "arguments": arguments,
                }
            ),
            "predicate_name": name,
            "arguments": arguments,
            "satisfied": satisfied,
        }
        for index, (name, arguments, satisfied) in enumerate(predicates)
    ]


def _witnesses(vector: list[dict]) -> dict[str, dict]:
    result = {}
    for predicate_index, object_name in enumerate(("moka_pot_1", "moka_pot_2")):
        on_stove = vector[predicate_index]["satisfied"]
        result[object_name] = {
            "object_name": object_name,
            "position_metres": [0.1 + predicate_index, 0.2, 0.3],
            "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
            "linear_velocity_metres_per_second": [0.0, 0.0, 0.0],
            "angular_velocity_radians_per_second": [0.0, 0.0, 0.0],
            "step_translation_distance_metres": 0.001,
            "end_effector_distance_metres": 0.2,
            "gripper_contact_observed": False,
            "stove_region_witness": {
                "region_name": "flat_stove_1_cook_region",
                "local_delta_metres": [0.0 if on_stove else 0.085, 0.0, 0.02],
                "half_extent_metres": [0.075, 0.075, 0.0025],
                "axis_margins_metres": {
                    "x": 0.075 if on_stove else -0.01,
                    "y": 0.075,
                    "z_lower": 0.0225,
                    "z_upper": 0.0825,
                },
                "inside_under_region": on_stove,
                "stove_parent_contact_observed": on_stove,
                "on_predicate_witness": on_stove,
            },
        }
    return result


class _FixtureRuntime:
    def __init__(self) -> None:
        self.queue: deque[list[float]] = deque()
        self.reset_count = 1
        self.version = 0
        self.vector = _vector()
        self.instructions: list[str] = []
        self.observed_versions: list[int] = []
        self.forward_count = 0

    def select(self, observation, instruction) -> SelectedAction:
        self.instructions.append(instruction)
        self.observed_versions.append(observation["version"])
        forwarded = not self.queue
        if forwarded:
            self.forward_count += 1
            self.queue.extend([[float(self.forward_count), float(index)] for index in range(16)])
        action = self.queue.popleft()
        return SelectedAction(
            action=action,
            model_forward_observed=forwarded,
            policy_request_sha256=(
                canonical_sha256({"version": observation["version"], "instruction": instruction})
                if forwarded
                else None
            ),
            policy_response_sha256=(
                canonical_sha256({"forward_count": self.forward_count, "action": action})
                if forwarded
                else None
            ),
            instruction_payload=[instruction] if forwarded else None,
        )

    def apply(self, action):
        self.version += 1
        if self.version >= 24 + 32:
            self.vector = _vector(first=True)
        return {"version": self.version}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "official_predicate_result": all(item["satisfied"] for item in self.vector),
            "done": False,
            "truncated": False,
            "object_witnesses": _witnesses(self.vector),
        }

    def reset_policy(self) -> None:
        self.queue.clear()


def _authorization():
    proposal = build_lerobot_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="lerobot-live-session:fixture",
        source_contract_sha256="a" * 64,
        source_goal_predicates=_vector(),
        reset_count=1,
        maximum_repair_chunks=2,
        proposal_id="proposal:fixture",
    )
    approval = approve_same_world_repair(
        proposal=proposal,
        operator_approval_ref="operator:fixture",
        approval_id="approval:fixture",
    )
    dispatch = build_same_world_repair_dispatch(
        proposal=proposal,
        approval=approval,
        dispatch_ref="dispatch:fixture",
    )
    return proposal, approval, dispatch


def test_source_queue_is_discarded_without_resetting_world() -> None:
    fixture = _FixtureRuntime()
    session = LeRobotLiveSession(
        initial_observation={"version": 0},
        select_action=fixture.select,
        apply_action=fixture.apply,
        observe_goal_predicates=lambda: deepcopy(fixture.vector),
        policy_reset=fixture.reset_policy,
        action_queue_depth=lambda: len(fixture.queue),
        observed_reset_count=lambda: fixture.reset_count,
    )

    source = session.run_source_steps(
        instruction="put both moka pots on the stove", maximum_steps=24
    )
    boundary = session.begin_repair()

    assert source["source_model_forward_count"] == 2
    assert source["queued_source_actions_remaining"] == 8
    assert boundary == {
        "policy_queue_reset_observed": True,
        "discarded_source_action_count": 8,
        "environment_reset_invoked": False,
        "same_world_reset_count": 1,
    }
    assert fixture.reset_count == 1
    assert len(fixture.queue) == 0


def test_live_session_connects_updated_observations_to_bounded_authority(tmp_path) -> None:
    fixture = _FixtureRuntime()
    session = LeRobotLiveSession(
        initial_observation={"version": 0},
        select_action=fixture.select,
        apply_action=fixture.apply,
        observe_goal_predicates=lambda: deepcopy(fixture.vector),
        policy_reset=fixture.reset_policy,
        action_queue_depth=lambda: len(fixture.queue),
        observed_reset_count=lambda: fixture.reset_count,
    )
    session.run_source_steps(instruction="put both moka pots on the stove", maximum_steps=24)
    session.begin_repair()
    proposal, approval, dispatch = _authorization()

    result = run_lerobot_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
        initial_observation=session.observation,
        invoke_model=session.invoke_model,
        apply_action_chunk=session.apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(fixture.vector),
        observed_reset_count=lambda: fixture.reset_count,
    )

    assert result["status"] == "satisfied"
    assert result["chunks_executed"] == 2
    assert fixture.forward_count == 4
    assert fixture.observed_versions[24] == 24
    assert fixture.observed_versions[40] == 40
    assert len(fixture.queue) == 0
    assert fixture.reset_count == 1


def test_repair_rejects_source_queue_without_policy_boundary_reset(tmp_path) -> None:
    fixture = _FixtureRuntime()
    session = LeRobotLiveSession(
        initial_observation={"version": 0},
        select_action=fixture.select,
        apply_action=fixture.apply,
        observe_goal_predicates=lambda: deepcopy(fixture.vector),
        policy_reset=fixture.reset_policy,
        action_queue_depth=lambda: len(fixture.queue),
        observed_reset_count=lambda: fixture.reset_count,
    )
    session.run_source_steps(instruction="put both moka pots on the stove", maximum_steps=24)
    proposal, approval, dispatch = _authorization()

    with pytest.raises(RuntimeError, match="policy_boundary_not_reset"):
        run_lerobot_same_world_repair(
            proposal=proposal,
            approval=approval,
            dispatch=dispatch,
            dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
            initial_observation=session.observation,
            invoke_model=session.invoke_model,
            apply_action_chunk=session.apply_action_chunk,
            observe_goal_predicates=lambda: deepcopy(fixture.vector),
            observed_reset_count=lambda: fixture.reset_count,
        )


def test_repair_rejects_truncated_observed_language_payload() -> None:
    fixture = _FixtureRuntime()

    def truncated_select(observation, instruction):
        selected = fixture.select(observation, instruction)
        return SelectedAction(
            action=selected.action,
            model_forward_observed=selected.model_forward_observed,
            policy_request_sha256=selected.policy_request_sha256,
            policy_response_sha256=selected.policy_response_sha256,
            instruction_payload=[instruction[:31]],
        )

    session = LeRobotLiveSession(
        initial_observation={"version": 0},
        select_action=truncated_select,
        apply_action=fixture.apply,
        observe_goal_predicates=lambda: deepcopy(fixture.vector),
        policy_reset=fixture.reset_policy,
        action_queue_depth=lambda: len(fixture.queue),
        observed_reset_count=lambda: fixture.reset_count,
    )
    session.run_source_steps(instruction="source", maximum_steps=24)
    session.begin_repair()
    instruction = "place the first moka pot on the stove without changing the other objects"

    with pytest.raises(ValueError, match="payload_exact_match_failed"):
        session.invoke_model(session.observation, instruction, 0)


def test_mid_chunk_failure_retains_already_executed_step_evidence() -> None:
    fixture = _FixtureRuntime()
    repair_mode = False

    def fail_second_repair_step(action):
        observation, evidence = fixture.apply(action)
        if repair_mode and observation["version"] == 26:
            evidence = {**evidence, "official_predicate_result": True}
        return observation, evidence

    session = LeRobotLiveSession(
        initial_observation={"version": 0},
        select_action=fixture.select,
        apply_action=fail_second_repair_step,
        observe_goal_predicates=lambda: deepcopy(fixture.vector),
        policy_reset=fixture.reset_policy,
        action_queue_depth=lambda: len(fixture.queue),
        observed_reset_count=lambda: fixture.reset_count,
    )
    session.run_source_steps(instruction="source", maximum_steps=24)
    session.begin_repair()
    repair_mode = True
    action_token, _ = session.invoke_model(session.observation, "repair instruction", 0)

    with pytest.raises(LeRobotActionChunkExecutionError) as raised:
        session.apply_action_chunk(action_token, 0)

    partial = raised.value.partial_application
    assert partial["actions_applied"] == 2
    assert len(partial["executed_step_trace"]) == 2
    assert partial["executed_step_trace"][0]["verification_completed"] is True
    assert partial["executed_step_trace"][1]["verification_completed"] is False
    assert partial["verification_passed"] is False
    assert partial["completion_claimed"] is False
    assert raised.value.failure_code == "lerobot_repair_predicate_conjunction_mismatch"


def test_source_fails_closed_on_implicit_environment_end() -> None:
    fixture = _FixtureRuntime()

    def ended(action):
        observation, evidence = fixture.apply(action)
        return observation, {**evidence, "done": True}

    session = LeRobotLiveSession(
        initial_observation={"version": 0},
        select_action=fixture.select,
        apply_action=ended,
        observe_goal_predicates=lambda: deepcopy(fixture.vector),
        policy_reset=fixture.reset_policy,
        action_queue_depth=lambda: len(fixture.queue),
        observed_reset_count=lambda: fixture.reset_count,
    )

    with pytest.raises(RuntimeError, match="environment_ended_without_success"):
        session.run_source_steps(instruction="put both moka pots on the stove", maximum_steps=24)


def test_live_runner_requires_explicit_opt_in(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.delenv(live_runner.OPT_IN_ENV, raising=False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_groot_lerobot_same_world_repair.py",
            "--checkpoint-path",
            str(tmp_path / "checkpoint"),
            "--operator-approval-ref",
            "operator:test",
            "--dispatch-state-path",
            str(tmp_path / "dispatch.json"),
            "--output",
            str(tmp_path / "result.json"),
        ],
    )

    assert live_runner.main() == 3
    assert live_runner.OPT_IN_ENV in capsys.readouterr().out


def test_local_snapshot_verification_binds_revision_and_file_digest(tmp_path) -> None:
    revision = "a" * 40
    snapshot = tmp_path / "snapshot"
    source = snapshot / "config.json"
    metadata = snapshot / ".cache/huggingface/download/config.json.metadata"
    source.parent.mkdir(parents=True)
    metadata.parent.mkdir(parents=True)
    source.write_text('{"model":"fixture"}\n', encoding="utf-8")
    metadata.write_text(f"{revision}\nfixture-etag\n0\n", encoding="utf-8")

    result = verify_huggingface_local_snapshot(
        snapshot_path=snapshot,
        expected_revision=revision,
        required_files=("config.json",),
    )

    assert result["snapshot_verified"] is True
    assert result["revision"] == revision
    assert len(result["required_file_sha256"]["config.json"]) == 64


def test_local_snapshot_verification_rejects_revision_mismatch(tmp_path) -> None:
    snapshot = tmp_path / "snapshot"
    source = snapshot / "config.json"
    metadata = snapshot / ".cache/huggingface/download/config.json.metadata"
    source.parent.mkdir(parents=True)
    metadata.parent.mkdir(parents=True)
    source.write_text("{}\n", encoding="utf-8")
    metadata.write_text(f"{'b' * 40}\netag\n0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="revision_mismatch"):
        verify_huggingface_local_snapshot(
            snapshot_path=snapshot,
            expected_revision="a" * 40,
            required_files=("config.json",),
        )


def test_live_runner_writes_sanitized_failure_without_exception_text(monkeypatch, tmp_path) -> None:
    output = tmp_path / "nested" / "result.json"
    monkeypatch.setenv(live_runner.OPT_IN_ENV, "1")
    monkeypatch.setattr(
        live_runner,
        "execute_live",
        lambda **_: (_ for _ in ()).throw(RuntimeError("/private/checkpoint/path")),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_groot_lerobot_same_world_repair.py",
            "--checkpoint-path",
            str(tmp_path / "checkpoint"),
            "--operator-approval-ref",
            "operator:test",
            "--dispatch-state-path",
            str(tmp_path / "dispatch.json"),
            "--output",
            str(output),
        ],
    )

    assert live_runner.main() == 2
    report = __import__("json").loads(output.read_text())
    assert report["cause_type"] == "RuntimeError"
    assert "/private/checkpoint/path" not in output.read_text()
    assert report["semantic_repair_established"] is False


def test_live_runner_writes_partial_steps_and_safe_failure_code(monkeypatch, tmp_path) -> None:
    output = tmp_path / "partial" / "result.json"
    partial_application = {
        "actions_applied": 2,
        "executed_step_trace": [
            {
                "action_step_number": 1,
                "action_step_sha256": "a" * 64,
                "verification_completed": True,
            },
            {
                "action_step_number": 2,
                "action_step_sha256": "b" * 64,
                "verification_completed": False,
            },
        ],
        "verification_passed": False,
        "completion_claimed": False,
    }
    error = LeRobotActionChunkExecutionError(
        cause=RuntimeError("lerobot_repair_predicate_conjunction_mismatch"),
        partial_application=partial_application,
    )
    monkeypatch.setenv(live_runner.OPT_IN_ENV, "1")
    monkeypatch.setattr(
        live_runner,
        "execute_live",
        lambda **_: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_groot_lerobot_same_world_repair.py",
            "--checkpoint-path",
            str(tmp_path / "checkpoint"),
            "--operator-approval-ref",
            "operator:test",
            "--dispatch-state-path",
            str(tmp_path / "dispatch.json"),
            "--output",
            str(output),
        ],
    )

    assert live_runner.main() == 2
    report = __import__("json").loads(output.read_text())
    assert report["failure_code"] == "lerobot_repair_predicate_conjunction_mismatch"
    assert report["repair_executed"] is True
    assert report["partial_application"] == partial_application
    assert report["semantic_repair_established"] is False


def _failure_snapshot_metadata() -> dict:
    predicates = _vector()
    return {
        "task_suite": live_runner.TASK_SUITE,
        "task_id": live_runner.TASK_ID,
        "episode_init_state_index": 9,
        "checkpoint_repository": live_runner.CHECKPOINT_REPOSITORY,
        "checkpoint_revision": live_runner.CHECKPOINT_REVISION,
        "lerobot_revision": live_runner.LEROBOT_REVISION,
        "source_contract_sha256": "a" * 64,
        "source_steps_executed": live_runner.SOURCE_STEP_BUDGET,
        "source_goal_predicate_observations": predicates,
        "source_goal_predicate_vector": [False, True, True],
        "source_goal_predicate_vector_sha256": canonical_sha256(
            {"goal_predicate_observations": predicates}
        ),
        "source_object_poses": {
            "moka_pot_1": [0.0, 0.0, 1.0],
            "moka_pot_2": [1.0, 0.0, 1.0],
        },
        "source_failure_is_repair_candidate": True,
        "model_runtime_invoked_for_snapshot_restore": False,
        "physical_execution_invoked": False,
    }


def test_failure_snapshot_round_trip_is_atomic_and_diagnostic_only(tmp_path) -> None:
    path = tmp_path / "candidate.npz"
    state = np.asarray([0.1, 0.2, 0.3], dtype=np.float64)

    written = live_runner._write_failure_snapshot(
        path=path,
        simulator_state=state,
        metadata=_failure_snapshot_metadata(),
    )
    restored_state, restored = live_runner._read_failure_snapshot(path)

    assert np.array_equal(restored_state, state)
    assert restored["snapshot_artifact_sha256"] == written["snapshot_artifact_sha256"]
    assert restored["authority"] == "diagnostic_only"
    assert restored["semantic_repair_claim_eligible"] is False
    assert restored["local_path_recorded"] is False
    assert list(tmp_path.glob(".*.tmp")) == []


def test_failure_snapshot_rejects_state_digest_mismatch(tmp_path) -> None:
    path = tmp_path / "candidate.npz"
    live_runner._write_failure_snapshot(
        path=path,
        simulator_state=[0.1, 0.2, 0.3],
        metadata=_failure_snapshot_metadata(),
    )
    with np.load(path, allow_pickle=False) as archive:
        metadata_json = archive["metadata_json"].copy()
    with path.open("wb") as stream:
        np.savez_compressed(
            stream,
            simulator_state=np.asarray([9.0, 9.0, 9.0]),
            metadata_json=metadata_json,
        )

    with pytest.raises(ValueError, match="state_digest_mismatch"):
        live_runner._read_failure_snapshot(path)


def test_replay_schedule_is_alternating_seed_paired_and_honest_about_odd_balance() -> None:
    schedule = live_runner._counterbalanced_replay_schedule(
        trials_per_variant=5,
        seed_base=700,
    )

    assert [item["repair_instruction_variant"] for item in schedule[:4]] == [
        "short_target",
        "original_task",
        "original_task",
        "short_target",
    ]
    assert [item["repair_sampling_seed"] for item in schedule[:4]] == [700, 700, 701, 701]
    assert sum(item["repair_instruction_variant"] == "short_target" for item in schedule) == 5
    assert sum(item["repair_instruction_variant"] == "original_task" for item in schedule) == 5
    assert all(item["pair_order_alternated"] is True for item in schedule)
    assert all(item["fully_order_balanced"] is False for item in schedule)


def test_fixture_cli_exercises_snapshot_and_atomic_replay_outputs(monkeypatch, tmp_path) -> None:
    output = tmp_path / "result.json"
    snapshot = tmp_path / "candidate.npz"
    progress = tmp_path / "replay-progress.json"
    trials = tmp_path / "trials"
    monkeypatch.setenv(live_runner.OPT_IN_ENV, "1")
    monkeypatch.setenv(live_runner.FIXTURE_OPT_IN_ENV, "1")
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_groot_lerobot_same_world_repair.py",
            "--runtime",
            "fixture",
            "--checkpoint-path",
            str(tmp_path / "unused-checkpoint"),
            "--operator-approval-ref",
            "operator:test",
            "--dispatch-state-path",
            str(tmp_path / "dispatch.json"),
            "--output",
            str(output),
            "--failure-snapshot-out",
            str(snapshot),
            "--replay-trials-per-variant",
            "2",
            "--replay-progress-output",
            str(progress),
            "--replay-trial-output-dir",
            str(trials),
        ],
    )

    assert live_runner.main() == 0
    report = __import__("json").loads(output.read_text())
    assert report["fixture_runtime_verified"] is True
    assert report["fixture_replay"]["completed_trial_count"] == 4
    assert report["fixture_replay"]["semantic_repair_established"] is False
    assert len(list(trials.glob("trial-*.json"))) == 4
    assert __import__("json").loads(progress.read_text())["completed_trial_count"] == 4


def test_live_language_probe_cli_passes_no_dispatch_state(monkeypatch, tmp_path) -> None:
    output = tmp_path / "probe-result.json"
    checkpoint = tmp_path / "checkpoint"
    snapshot = tmp_path / "candidate.npz"
    checkpoint.mkdir()
    snapshot.write_bytes(b"diagnostic-snapshot")
    calls = []

    def fake_execute_live(**kwargs):
        calls.append(kwargs)
        return {
            "status": "no_local_prediction_difference_observed",
            "local_instruction_conditioning_observed": False,
            "semantic_repair_established": False,
            "physical_execution_invoked": False,
        }

    monkeypatch.setattr(live_runner, "execute_live", fake_execute_live)
    monkeypatch.setenv(live_runner.OPT_IN_ENV, "1")
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_groot_lerobot_same_world_repair.py",
            "--runtime",
            "live",
            "--checkpoint-path",
            str(checkpoint),
            "--restore-snapshot",
            str(snapshot),
            "--diagnostic-authorization-ref",
            "diagnostic:test",
            "--output",
            str(output),
            "--language-conditioning-probe",
            "--repair-sampling-seed",
            "1000",
        ],
    )

    assert live_runner.main() == 0
    assert len(calls) == 1
    assert calls[0]["operator_approval_ref"] is None
    assert calls[0]["dispatch_state_path"] is None
    assert calls[0]["language_conditioning_probe"] is True
    assert calls[0]["diagnostic_authorization_ref"] == "diagnostic:test"
    assert __import__("json").loads(output.read_text())["status"] == (
        "no_local_prediction_difference_observed"
    )


def test_live_semantic_direction_probe_cli_passes_no_dispatch_state(monkeypatch, tmp_path) -> None:
    output = tmp_path / "probe-result.json"
    checkpoint = tmp_path / "checkpoint"
    snapshot = tmp_path / "candidate.npz"
    checkpoint.mkdir()
    snapshot.write_bytes(b"diagnostic-snapshot")
    calls = []

    def fake_execute_live(**kwargs):
        calls.append(kwargs)
        return {
            "status": "local_direction_alignment_inconclusive",
            "local_failed_target_direction_alignment_observed": False,
            "semantic_repair_established": False,
            "physical_execution_invoked": False,
        }

    monkeypatch.setattr(live_runner, "execute_live", fake_execute_live)
    monkeypatch.setenv(live_runner.OPT_IN_ENV, "1")
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_groot_lerobot_same_world_repair.py",
            "--runtime",
            "live",
            "--checkpoint-path",
            str(checkpoint),
            "--restore-snapshot",
            str(snapshot),
            "--diagnostic-authorization-ref",
            "diagnostic:semantic-direction",
            "--output",
            str(output),
            "--semantic-direction-probe",
            "--repair-sampling-seed",
            "1000",
        ],
    )

    assert live_runner.main() == 0
    assert len(calls) == 1
    assert calls[0]["operator_approval_ref"] is None
    assert calls[0]["dispatch_state_path"] is None
    assert calls[0]["language_conditioning_probe"] is False
    assert calls[0]["semantic_direction_probe"] is True
    assert calls[0]["diagnostic_authorization_ref"] == "diagnostic:semantic-direction"


def test_live_replay_trials_alternate_order_are_atomic_and_never_semantic(
    monkeypatch, tmp_path
) -> None:
    snapshot_path = tmp_path / "candidate.npz"
    live_runner._write_failure_snapshot(
        path=snapshot_path,
        simulator_state=[0.1, 0.2, 0.3],
        metadata=_failure_snapshot_metadata(),
    )
    calls = []

    def fake_execute_live(**kwargs):
        calls.append(kwargs)
        variant = kwargs["repair_instruction_variant"]
        improved = variant == "short_target"
        return {
            "result": (
                "satisfied_diagnostic_observation"
                if improved
                else "budget_exhausted_without_improvement"
            ),
            "source_goal_predicate_vector": [False, True, True],
            "diagnostic_clone_identity_verified": True,
            "semantic_repair_established": False,
            "repair_result": {
                "status": (
                    "satisfied_diagnostic_observation"
                    if improved
                    else "budget_exhausted_without_improvement"
                ),
                "predicate_improvement_observed": improved,
                "chunks_executed": 2,
            },
        }

    monkeypatch.setattr(live_runner, "execute_live", fake_execute_live)
    result = live_runner.execute_live_replay_trials(
        checkpoint_path=tmp_path / "checkpoint",
        snapshot_path=snapshot_path,
        operator_approval_ref="operator:test",
        dispatch_state_path=tmp_path / "dispatch.json",
        maximum_repair_chunks=45,
        trials_per_variant=2,
        seed_base=42,
        progress_output=tmp_path / "progress.json",
        trial_output_dir=tmp_path / "trials",
    )

    assert [call["repair_instruction_variant"] for call in calls] == [
        "short_target",
        "original_task",
        "original_task",
        "short_target",
    ]
    assert [call["repair_sampling_seed"] for call in calls] == [42, 42, 43, 43]
    assert all(call["restore_snapshot_path"] == snapshot_path for call in calls)
    assert result["success_counts"] == {"short_target": 2, "original_task": 0}
    assert result["conclusion_key"] == "variant_difference"
    assert result["general_instruction_superiority_claimed"] is False
    assert result["semantic_repair_established"] is False
    assert len(list((tmp_path / "trials").glob("trial-*.json"))) == 4
    progress = __import__("json").loads((tmp_path / "progress.json").read_text())
    assert progress["status"] == "replay_complete"
    assert progress["completed_trial_count"] == 4


def test_live_replay_rejects_clone_identity_mismatch_before_counting_trial(
    monkeypatch, tmp_path
) -> None:
    snapshot_path = tmp_path / "candidate.npz"
    live_runner._write_failure_snapshot(
        path=snapshot_path,
        simulator_state=[0.1, 0.2, 0.3],
        metadata=_failure_snapshot_metadata(),
    )
    monkeypatch.setattr(
        live_runner,
        "execute_live",
        lambda **_: {
            "result": "budget_exhausted_without_improvement",
            "source_goal_predicate_vector": [False, False, True],
            "diagnostic_clone_identity_verified": True,
            "semantic_repair_established": False,
            "repair_result": {},
        },
    )

    with pytest.raises(RuntimeError, match="clone_identity_mismatch"):
        live_runner.execute_live_replay_trials(
            checkpoint_path=tmp_path / "checkpoint",
            snapshot_path=snapshot_path,
            operator_approval_ref="operator:test",
            dispatch_state_path=tmp_path / "dispatch.json",
            maximum_repair_chunks=45,
            trials_per_variant=1,
            seed_base=42,
            progress_output=tmp_path / "progress.json",
            trial_output_dir=tmp_path / "trials",
        )

    assert not (tmp_path / "progress.json").exists()
    assert list((tmp_path / "trials").glob("trial-*.json")) == []


def test_live_restore_snapshot_replay_cli_runs_matrix_without_screening(
    monkeypatch, tmp_path
) -> None:
    output = tmp_path / "result.json"
    checkpoint = tmp_path / "checkpoint"
    snapshot = tmp_path / "candidate.npz"
    checkpoint.mkdir()
    snapshot.write_bytes(b"diagnostic-snapshot")
    calls = []

    def fake_execute_live_replay_trials(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": live_runner.REPLAY_RESULT_SCHEMA_VERSION,
            "status": "replay_complete",
            "completed_trial_count": 10,
            "success_counts": {"short_target": 0, "original_task": 0},
            "semantic_repair_established": False,
            "physical_execution_invoked": False,
        }

    monkeypatch.setattr(
        live_runner, "execute_live_replay_trials", fake_execute_live_replay_trials
    )
    monkeypatch.setenv(live_runner.OPT_IN_ENV, "1")
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_groot_lerobot_same_world_repair.py",
            "--runtime",
            "live",
            "--checkpoint-path",
            str(checkpoint),
            "--restore-snapshot",
            str(snapshot),
            "--operator-approval-ref",
            "operator:original-task-control",
            "--dispatch-state-path",
            str(tmp_path / "dispatch.json"),
            "--output",
            str(output),
            "--maximum-repair-chunks",
            "45",
            "--replay-trials-per-variant",
            "5",
            "--replay-seed-base",
            "2000",
            "--replay-progress-output",
            str(tmp_path / "progress.json"),
            "--replay-trial-output-dir",
            str(tmp_path / "trials"),
        ],
    )

    assert live_runner.main() == 2
    assert len(calls) == 1
    assert calls[0]["snapshot_path"] == snapshot
    assert calls[0]["trials_per_variant"] == 5
    assert calls[0]["maximum_repair_chunks"] == 45
    assert calls[0]["operator_approval_ref"] == "operator:original-task-control"
    assert __import__("json").loads(output.read_text())["completed_trial_count"] == 10


def test_live_replay_stops_matrix_on_first_preservation_violation(
    monkeypatch, tmp_path
) -> None:
    snapshot_path = tmp_path / "candidate.npz"
    live_runner._write_failure_snapshot(
        path=snapshot_path,
        simulator_state=[0.1, 0.2, 0.3],
        metadata=_failure_snapshot_metadata(),
    )
    calls = []

    def fake_execute_live(**kwargs):
        calls.append(kwargs)
        return {
            "result": "stopped_on_preservation_violation",
            "source_goal_predicate_vector": [False, True, True],
            "diagnostic_clone_identity_verified": True,
            "semantic_repair_established": False,
            "repair_result": {
                "status": "stopped_on_preservation_violation",
                "predicate_improvement_observed": False,
                "chunks_executed": 1,
            },
        }

    monkeypatch.setattr(live_runner, "execute_live", fake_execute_live)
    result = live_runner.execute_live_replay_trials(
        checkpoint_path=tmp_path / "checkpoint",
        snapshot_path=snapshot_path,
        operator_approval_ref="operator:test",
        dispatch_state_path=tmp_path / "dispatch.json",
        maximum_repair_chunks=45,
        trials_per_variant=5,
        seed_base=42,
        progress_output=tmp_path / "progress.json",
        trial_output_dir=tmp_path / "trials",
    )

    assert len(calls) == 1
    assert result["status"] == "stopped_on_preservation_violation"
    assert result["preservation_violation_observed"] is True
    assert result["completed_trial_count"] == 1
    assert result["semantic_repair_established"] is False
