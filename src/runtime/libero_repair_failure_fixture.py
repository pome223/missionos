"""Deterministic, authority-free failure fixtures for LIBERO Repair tests.

The fixture injector changes simulator state before a governed Repair proposal
is created.  It does not propose, approve, dispatch, or execute Repair.  Its
only purpose is to create an explicit and visually meaningful failed state and
to prove that the state is stable enough to hand to the normal Repair runtime.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import math
from typing import Any, Callable, Mapping, Sequence


SCRIPTED_FAILURE_FIXTURE_BASIS = "scripted_failure_fixture"
FIXTURE_SCHEMA_VERSION = "missionos.libero_repair_failure_fixture.v1"
TARGET_OBJECT = "moka_pot_2"
PROTECTED_OBJECT = "moka_pot_1"
STOVE_REGION = "flat_stove_1_cook_region"


@dataclass(frozen=True)
class FailureFixtureSpec:
    scenario: str
    local_x_clearance_metres: float
    local_y_clearance_metres: float
    release_height_metres: float
    tip_angle_degrees: float
    settle_steps: int
    minimum_translation_metres: float
    minimum_outside_clearance_metres: float
    maximum_linear_speed_metres_per_second: float = 0.05
    maximum_angular_speed_radians_per_second: float = 0.25


FAILURE_FIXTURE_SPECS: dict[str, FailureFixtureSpec] = {
    "displaced_from_stove": FailureFixtureSpec(
        scenario="displaced_from_stove",
        local_x_clearance_metres=0.07,
        local_y_clearance_metres=0.0,
        release_height_metres=0.0,
        tip_angle_degrees=0.0,
        settle_steps=60,
        minimum_translation_metres=0.05,
        minimum_outside_clearance_metres=0.05,
    ),
    "wrong_table_location": FailureFixtureSpec(
        scenario="wrong_table_location",
        local_x_clearance_metres=-0.12,
        local_y_clearance_metres=0.10,
        release_height_metres=0.0,
        tip_angle_degrees=0.0,
        settle_steps=80,
        minimum_translation_metres=0.10,
        minimum_outside_clearance_metres=0.08,
    ),
    "tipped_over": FailureFixtureSpec(
        scenario="tipped_over",
        local_x_clearance_metres=0.08,
        local_y_clearance_metres=0.02,
        release_height_metres=0.02,
        tip_angle_degrees=90.0,
        settle_steps=100,
        minimum_translation_metres=0.05,
        minimum_outside_clearance_metres=0.05,
    ),
    "dropped_during_scripted_transfer": FailureFixtureSpec(
        scenario="dropped_during_scripted_transfer",
        local_x_clearance_metres=-0.09,
        local_y_clearance_metres=-0.08,
        release_height_metres=0.14,
        tip_angle_degrees=0.0,
        settle_steps=120,
        minimum_translation_metres=0.07,
        minimum_outside_clearance_metres=0.06,
    ),
}


def failure_fixture_spec(scenario: str) -> FailureFixtureSpec:
    try:
        return FAILURE_FIXTURE_SPECS[scenario]
    except KeyError as exc:
        raise ValueError("libero_repair_failure_fixture_scenario_invalid") from exc


def failure_fixture_contract(scenario: str) -> dict[str, Any]:
    """Return immutable preregistration material for a fixture scenario."""

    spec = failure_fixture_spec(scenario)
    return {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "source_failure_basis": SCRIPTED_FAILURE_FIXTURE_BASIS,
        "scenario": scenario,
        "target_object": TARGET_OBJECT,
        "protected_object": PROTECTED_OBJECT,
        "stove_region": STOVE_REGION,
        "specification": asdict(spec),
        "authority": "test_fixture_only",
        "human_approval_created": False,
        "repair_proposal_created": False,
        "governed_dispatch_created": False,
        "model_inference_invoked": False,
        "physical_execution_invoked": False,
    }


def _quaternion_multiply_wxyz(left: Sequence[float], right: Sequence[float]) -> list[float]:
    lw, lx, ly, lz = (float(value) for value in left)
    rw, rx, ry, rz = (float(value) for value in right)
    return [
        lw * rw - lx * rx - ly * ry - lz * rz,
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
    ]


def _predicate_vector(material: Sequence[Mapping[str, Any]]) -> list[bool]:
    vector: list[bool] = []
    for item in material:
        satisfied = item.get("satisfied")
        if not isinstance(satisfied, bool):
            raise RuntimeError("libero_repair_failure_fixture_predicate_not_boolean")
        vector.append(satisfied)
    if len(vector) != 3:
        raise RuntimeError("libero_repair_failure_fixture_predicate_count_invalid")
    return vector


def inject_failure_fixture(
    *,
    environment: Any,
    scenario: str,
    observe_goal_predicates: Callable[[], Sequence[Mapping[str, Any]]],
) -> tuple[Any, dict[str, Any]]:
    """Inject and validate one stable failed state in a live LIBERO world.

    ``environment`` is the LeRobot LIBERO environment returned by reset.  The
    function uses MuJoCo's free-joint state only for fixture creation, then
    advances the normal 7D environment interface with zero actions while the
    object settles.  Those settling steps are fixture setup, not Repair steps.
    """

    import numpy as np

    spec = failure_fixture_spec(scenario)
    before_predicates = [deepcopy(dict(item)) for item in observe_goal_predicates()]
    before_vector = _predicate_vector(before_predicates)
    if before_vector[0] is not True or before_vector[2] is not True:
        raise RuntimeError("libero_repair_failure_fixture_preserve_precondition_missing")

    underlying = environment._env
    simulator = underlying.env
    target_model = simulator.get_object(TARGET_OBJECT)
    target_joint = target_model.joints[0]
    body_id = int(simulator.obj_body_id[TARGET_OBJECT])
    body_name = simulator.sim.model.body_id2name(body_id)
    if not body_name:
        raise RuntimeError("libero_repair_failure_fixture_body_name_unavailable")

    initial_position = np.asarray(simulator.sim.data.body_xpos[body_id], dtype=np.float64).copy()
    initial_quaternion = np.asarray(
        simulator.sim.data.body_xquat[body_id], dtype=np.float64
    ).copy()
    protected_body_id = int(simulator.obj_body_id[PROTECTED_OBJECT])
    protected_initial_position = np.asarray(
        simulator.sim.data.body_xpos[protected_body_id], dtype=np.float64
    ).copy()

    region = simulator.object_sites_dict[STOVE_REGION]
    region_position = np.asarray(
        simulator.sim.data.get_site_xpos(STOVE_REGION), dtype=np.float64
    )
    region_matrix = np.asarray(
        simulator.sim.data.get_site_xmat(STOVE_REGION), dtype=np.float64
    ).reshape(3, 3)
    half_extent = np.asarray(region.size, dtype=np.float64)

    local_position = np.array(
        [
            math.copysign(half_extent[0] + abs(spec.local_x_clearance_metres), spec.local_x_clearance_metres),
            (
                math.copysign(
                    half_extent[1] + abs(spec.local_y_clearance_metres),
                    spec.local_y_clearance_metres,
                )
                if spec.local_y_clearance_metres
                else 0.0
            ),
            0.0,
        ],
        dtype=np.float64,
    )
    injected_position = region_position + region_matrix.T @ local_position
    injected_position[2] = initial_position[2] + spec.release_height_metres
    injected_quaternion = initial_quaternion.copy()
    if spec.tip_angle_degrees:
        half_angle = math.radians(spec.tip_angle_degrees) / 2.0
        tip_quaternion = [math.cos(half_angle), math.sin(half_angle), 0.0, 0.0]
        injected_quaternion = np.asarray(
            _quaternion_multiply_wxyz(tip_quaternion, initial_quaternion),
            dtype=np.float64,
        )
        injected_quaternion /= np.linalg.norm(injected_quaternion)

    simulator.sim.data.set_joint_qpos(
        target_joint,
        np.concatenate([injected_position, injected_quaternion]),
    )
    try:
        simulator.sim.data.set_joint_qvel(target_joint, np.zeros(6, dtype=np.float64))
    except (AttributeError, ValueError):
        # Some mujoco-py builds do not expose set_joint_qvel for free joints.
        # Settlement and the terminal speed gate remain mandatory below.
        pass
    simulator.sim.forward()
    underlying._post_process()
    underlying._update_observables(force=True)

    observation: Any = None
    preservation_violation = False
    settle_trace: list[dict[str, Any]] = []
    for step_index in range(spec.settle_steps):
        observation, _, terminated, truncated, info = environment.step(
            np.zeros(7, dtype=np.float64)
        )
        vector = _predicate_vector(observe_goal_predicates())
        settle_trace.append(
            {
                "fixture_step_index": step_index,
                "predicate_vector": vector,
            }
        )
        if vector[0] is not True or vector[2] is not True:
            preservation_violation = True
            break
        if truncated or bool(info.get("done", terminated)):
            raise RuntimeError("libero_repair_failure_fixture_environment_terminated")
    if preservation_violation:
        raise RuntimeError("libero_repair_failure_fixture_preservation_violation")

    terminal_predicates = [deepcopy(dict(item)) for item in observe_goal_predicates()]
    terminal_vector = _predicate_vector(terminal_predicates)
    terminal_position = np.asarray(
        simulator.sim.data.body_xpos[body_id], dtype=np.float64
    ).copy()
    terminal_rotation = np.asarray(
        simulator.sim.data.body_xmat[body_id], dtype=np.float64
    ).reshape(3, 3)
    terminal_local = region_matrix @ (terminal_position - region_position)
    outside_clearance = float(
        max(
            abs(terminal_local[0]) - half_extent[0],
            abs(terminal_local[1]) - half_extent[1],
        )
    )
    translation = float(np.linalg.norm(terminal_position - initial_position))
    linear_speed = float(
        np.linalg.norm(simulator.sim.data.get_body_xvelp(body_name))
    )
    angular_speed = float(
        np.linalg.norm(simulator.sim.data.get_body_xvelr(body_name))
    )
    protected_terminal_position = np.asarray(
        simulator.sim.data.body_xpos[protected_body_id], dtype=np.float64
    ).copy()
    protected_displacement = float(
        np.linalg.norm(protected_terminal_position - protected_initial_position)
    )
    world_up_alignment = float(terminal_rotation[2, 2])
    release_drop = float(injected_position[2] - terminal_position[2])

    if terminal_vector != [True, False, True]:
        raise RuntimeError("libero_repair_failure_fixture_terminal_vector_invalid")
    if translation < spec.minimum_translation_metres:
        raise RuntimeError("libero_repair_failure_fixture_translation_too_small")
    if outside_clearance < spec.minimum_outside_clearance_metres:
        raise RuntimeError("libero_repair_failure_fixture_clearance_too_small")
    if linear_speed > spec.maximum_linear_speed_metres_per_second:
        raise RuntimeError("libero_repair_failure_fixture_linear_speed_too_high")
    if angular_speed > spec.maximum_angular_speed_radians_per_second:
        raise RuntimeError("libero_repair_failure_fixture_angular_speed_too_high")
    if protected_displacement > 0.005:
        raise RuntimeError("libero_repair_failure_fixture_protected_object_moved")
    if scenario == "tipped_over" and abs(world_up_alignment) > 0.50:
        raise RuntimeError("libero_repair_failure_fixture_not_tipped")
    if scenario == "dropped_during_scripted_transfer" and release_drop < 0.08:
        raise RuntimeError("libero_repair_failure_fixture_drop_not_observed")

    material = {
        **failure_fixture_contract(scenario),
        "before_goal_predicate_observations": before_predicates,
        "before_goal_predicate_vector": before_vector,
        "terminal_goal_predicate_observations": terminal_predicates,
        "terminal_goal_predicate_vector": terminal_vector,
        "initial_target_position_metres": initial_position.tolist(),
        "injected_target_position_metres": injected_position.tolist(),
        "terminal_target_position_metres": terminal_position.tolist(),
        "terminal_target_translation_metres": translation,
        "terminal_outside_stove_clearance_metres": outside_clearance,
        "terminal_world_up_alignment": world_up_alignment,
        "release_to_terminal_drop_metres": release_drop,
        "terminal_linear_speed_metres_per_second": linear_speed,
        "terminal_angular_speed_radians_per_second": angular_speed,
        "protected_object_displacement_metres": protected_displacement,
        "fixture_settle_steps_applied": len(settle_trace),
        "fixture_settle_trace": settle_trace,
        "stable_failure_fixture_observed": True,
        "scripted_release_is_not_observed_policy_grasp_failure": (
            scenario == "dropped_during_scripted_transfer"
        ),
        "repair_steps_applied": 0,
    }
    return observation, material


__all__ = [
    "FAILURE_FIXTURE_SPECS",
    "FIXTURE_SCHEMA_VERSION",
    "SCRIPTED_FAILURE_FIXTURE_BASIS",
    "FailureFixtureSpec",
    "failure_fixture_contract",
    "failure_fixture_spec",
    "inject_failure_fixture",
]
