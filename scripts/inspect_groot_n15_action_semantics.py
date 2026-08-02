#!/usr/bin/env python3
"""Inspect pinned public GR00T N1.5 action-semantics sources without a GPU.

The script downloads source files from immutable GitHub revisions, verifies
their SHA-256 digests, and emits only derived facts. It does not download a
checkpoint, dataset trajectories, images, or model-produced action values.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any


GROOT_REVISION = "4af2b622892f7dcb5aae5a3fb70bcb02dc217b96"
ROBOCASA_REVISION = "4840e671596f93ca03651524b9f72ffb1aadfeff"
ROBOSUITE_REVISION = "75a4c9f4d242c1b7fe7c7fc247b564ec5d8550a2"


@dataclass(frozen=True)
class Source:
    repository: str
    revision: str
    path: str
    sha256: str

    @property
    def url(self) -> str:
        return f"https://raw.githubusercontent.com/{self.repository}/{self.revision}/{self.path}"


SOURCES = {
    "modality": Source(
        "NVIDIA/Isaac-GR00T",
        GROOT_REVISION,
        "demo_data/robot_sim.PickNPlace/meta/modality.json",
        "6c738eb139d07690bd38ae3852d084b03bfb9f543324b46453776977c90e2c40",
    ),
    "dataset_info": Source(
        "NVIDIA/Isaac-GR00T",
        GROOT_REVISION,
        "demo_data/robot_sim.PickNPlace/meta/info.json",
        "39a1167b65b5224474af5b8494ef27d454086ad629b03ce10fe717e40af8a5b4",
    ),
    "data_config": Source(
        "NVIDIA/Isaac-GR00T",
        GROOT_REVISION,
        "gr00t/experiment/data_config.py",
        "71a3e145041a29c6648f5467043b902e2851eb88a0903f6dc0277712c31f5863",
    ),
    "dataset_loader": Source(
        "NVIDIA/Isaac-GR00T",
        GROOT_REVISION,
        "gr00t/data/dataset.py",
        "cb4a8b016dd9d56c61c041b20b179e761c3b6f060e07b4427e4973c1fc719381",
    ),
    "state_action_transform": Source(
        "NVIDIA/Isaac-GR00T",
        GROOT_REVISION,
        "gr00t/data/transform/state_action.py",
        "375bdddd4ec9077b66b4105f2fed4d001c137b1121525936d51dfa008227f1b0",
    ),
    "policy": Source(
        "NVIDIA/Isaac-GR00T",
        GROOT_REVISION,
        "gr00t/model/policy.py",
        "458eef19a9da229190c730b9d1c0d2e0c2fa851b949f661ed9c1e5e6bdbe2c1f",
    ),
    "multistep": Source(
        "NVIDIA/Isaac-GR00T",
        GROOT_REVISION,
        "gr00t/eval/wrappers/multistep_wrapper.py",
        "18d583355cc3580930b0436987469767863d10b0d95903454de1be3baa6d02d1",
    ),
    "simulation": Source(
        "NVIDIA/Isaac-GR00T",
        GROOT_REVISION,
        "gr00t/eval/simulation.py",
        "adff18b484c2483fad3678093d195174cc2e6a94076093807222b10d41cf4af5",
    ),
    "robocasa_robot_mapping": Source(
        "robocasa/robocasa-gr1-tabletop-tasks",
        ROBOCASA_REVISION,
        "robocasa/models/robots/__init__.py",
        "5d6a03dbec5d6de10a6cf947ac0693e16d01c0c6507a77164eeb39712f1e0c60",
    ),
    "robocasa_environment": Source(
        "robocasa/robocasa-gr1-tabletop-tasks",
        ROBOCASA_REVISION,
        "robocasa/utils/gym_utils/gymnasium_basic.py",
        "637707002866e3432555ab13db744970780be757c1f26f09454ad5899d2f0bea",
    ),
    "robocasa_groot_wrapper": Source(
        "robocasa/robocasa-gr1-tabletop-tasks",
        ROBOCASA_REVISION,
        "robocasa/utils/gym_utils/gymnasium_groot.py",
        "b5b0dedaf051dff21e85e72635150cc8e5c9fcd6c203d8260a1bc9f76a687e5f",
    ),
    "robot_xml": Source(
        "ARISE-Initiative/robosuite",
        ROBOSUITE_REVISION,
        "robosuite/models/assets/robots/gr1/robot.xml",
        "c2806641a8e41f10d1daba7b56e2409da37496720deb5a27420b6195bc8fc60d",
    ),
    "mjcf_model_base": Source(
        "ARISE-Initiative/robosuite",
        ROBOSUITE_REVISION,
        "robosuite/models/base.py",
        "5bc7f3d8ca37ad1ff747942885981748d57717f269808cd194164f7b41c31a07",
    ),
    "manipulator_model": Source(
        "ARISE-Initiative/robosuite",
        ROBOSUITE_REVISION,
        "robosuite/models/robots/manipulators/manipulator_model.py",
        "96707f46c1869353a17a9e79f4f235bba4f55283bc7e8ce53d8696f0aba21c36",
    ),
    "gr1_robot_model": Source(
        "ARISE-Initiative/robosuite",
        ROBOSUITE_REVISION,
        "robosuite/models/robots/manipulators/gr1_robot.py",
        "6a029108f3083c522192dcbcfa49735e306030d14117347aa12b591dd8ffa8c7",
    ),
    "robot_runtime": Source(
        "ARISE-Initiative/robosuite",
        ROBOSUITE_REVISION,
        "robosuite/robots/robot.py",
        "d2496ddf3460725841956298417ccb67ed2758f40df4c27351d043e48447dff4",
    ),
    "mobile_robot_runtime": Source(
        "ARISE-Initiative/robosuite",
        ROBOSUITE_REVISION,
        "robosuite/robots/mobile_robot.py",
        "8299df62a8a942cc206906c11dacac444a5d914058044203d394bf75198d779b",
    ),
    "left_hand_xml": Source(
        "ARISE-Initiative/robosuite",
        ROBOSUITE_REVISION,
        "robosuite/models/assets/grippers/fourier_left_hand.xml",
        "01cf4dc513054358707f189b3eb4c12ef1bbedb2aae179622084a9875c91d32b",
    ),
    "right_hand_xml": Source(
        "ARISE-Initiative/robosuite",
        ROBOSUITE_REVISION,
        "robosuite/models/assets/grippers/fourier_right_hand.xml",
        "0ac8b3a858a9f35a1347c20b880d8b21fee1e18bd69204e4dcf9728b8a03fc83",
    ),
    "fourier_hand_model": Source(
        "ARISE-Initiative/robosuite",
        ROBOSUITE_REVISION,
        "robosuite/models/grippers/fourier_hands.py",
        "cbf2740714a93907f348a414f67907f0780b5a54deb5d7cab91fa5932afae9f4",
    ),
    "controller_config": Source(
        "ARISE-Initiative/robosuite",
        ROBOSUITE_REVISION,
        "robosuite/controllers/config/robots/default_gr1.json",
        "8fc42fda888bf6d2d0dd23b9443c822d9e20b51082234ea187f760abffa1e840",
    ),
    "joint_position_controller": Source(
        "ARISE-Initiative/robosuite",
        ROBOSUITE_REVISION,
        "robosuite/controllers/parts/generic/joint_pos.py",
        "744ee0d0a793a0bb3acb1822a20c8960b25689001fddb1f343a1ea7d4f2788aa",
    ),
    "simple_grip_controller": Source(
        "ARISE-Initiative/robosuite",
        ROBOSUITE_REVISION,
        "robosuite/controllers/parts/gripper/simple_grip.py",
        "2207911877d3416ae7dd247c948eeae9c684c511e92cb9dc5410668b40a90c4c",
    ),
}


def _fetch(source: Source, timeout_seconds: float) -> bytes:
    request = urllib.request.Request(
        source.url,
        headers={"User-Agent": "missionos-groot-action-semantics-inspector/1"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        content = response.read()
    observed = hashlib.sha256(content).hexdigest()
    if observed != source.sha256:
        raise ValueError(
            f"source digest mismatch for {source.repository}/{source.path}: "
            f"expected {source.sha256}, observed {observed}"
        )
    return content


def _class_assignments(source: str, class_name: str) -> dict[str, Any]:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            result: dict[str, Any] = {}
            for child in node.body:
                if isinstance(child, ast.Assign) and len(child.targets) == 1:
                    target = child.targets[0]
                    if isinstance(target, ast.Name):
                        try:
                            result[target.id] = ast.literal_eval(child.value)
                        except ValueError:
                            if (
                                isinstance(child.value, ast.Call)
                                and isinstance(child.value.func, ast.Name)
                                and child.value.func.id == "list"
                                and child.value.args
                            ):
                                range_call = child.value.args[0]
                                if (
                                    isinstance(range_call, ast.Call)
                                    and isinstance(range_call.func, ast.Name)
                                    and range_call.func.id == "range"
                                ):
                                    result[target.id] = list(
                                        range(*[ast.literal_eval(arg) for arg in range_call.args])
                                    )
            return result
    raise ValueError(f"class {class_name} not found")


def _joint_rows(
    key: str,
    joint_names: list[str],
    joint_ranges: dict[str, list[float]],
) -> list[dict[str, Any]]:
    return [
        {
            "service_key": key,
            "dimension": index,
            "quantity": name,
            "command_kind": "absolute_joint_position",
            "unit": "radian",
            "frame": f"joint_local_axis:{name}",
            "source_limit": joint_ranges[name],
            "source_limit_status": "robot_description_limit_not_missionos_policy",
        }
        for index, name in enumerate(joint_names)
    ]


EXPECTED_RIGHT_ARM_JOINTS = [
    "r_shoulder_pitch",
    "r_shoulder_roll",
    "r_shoulder_yaw",
    "r_elbow_pitch",
    "r_wrist_yaw",
    "r_wrist_roll",
    "r_wrist_pitch",
]
EXPECTED_LEFT_ARM_JOINTS = [name.replace("r_", "l_", 1) for name in EXPECTED_RIGHT_ARM_JOINTS]


def _derive_arm_joint_order(robot_root: ET.Element) -> tuple[list[str], list[str]]:
    """Derive the controller arm order from the pinned robot XML and fail on drift."""
    arm_joint_names = []
    for joint in robot_root.findall(".//joint"):
        name = joint.attrib.get("name", "")
        if name.startswith(("r_shoulder_", "r_elbow_", "r_wrist_")):
            arm_joint_names.append(name)
        elif name.startswith(("l_shoulder_", "l_elbow_", "l_wrist_")):
            arm_joint_names.append(name)

    expected = EXPECTED_RIGHT_ARM_JOINTS + EXPECTED_LEFT_ARM_JOINTS
    if arm_joint_names != expected:
        raise ValueError(
            "GR1 arm joint order drifted: "
            f"expected right-then-left controller order {expected}, observed {arm_joint_names}"
        )
    split = len(EXPECTED_RIGHT_ARM_JOINTS)
    return arm_joint_names[:split], arm_joint_names[split:]


def _hand_rows(
    key: str, side: str, actuator_ranges: dict[str, list[float]]
) -> list[dict[str, Any]]:
    prefix = side.upper()
    groups = [
        (
            "pinky_flexion",
            [f"{prefix}_pinky_intermediate_joint", f"{prefix}_pinky_proximal_joint"],
        ),
        (
            "ring_flexion",
            [f"{prefix}_ring_intermediate_joint", f"{prefix}_ring_proximal_joint"],
        ),
        (
            "middle_flexion",
            [f"{prefix}_middle_intermediate_joint", f"{prefix}_middle_proximal_joint"],
        ),
        (
            "index_flexion",
            [f"{prefix}_index_intermediate_joint", f"{prefix}_index_proximal_joint"],
        ),
        (
            "thumb_flexion",
            [f"{prefix}_thumb_distal_joint", f"{prefix}_thumb_proximal_pitch_joint"],
        ),
        ("thumb_yaw", [f"{prefix}_thumb_proximal_yaw_joint"]),
    ]
    rows = []
    for index, (quantity, actuators) in enumerate(groups):
        ranges = [actuator_ranges[name] for name in actuators]
        rows.append(
            {
                "service_key": key,
                "dimension": index,
                "quantity": quantity,
                "command_kind": "reduced_hand_command_expanded_to_position_actuators",
                "effective_simulator_unit": "radian",
                "controller_api_unit": "unverified",
                "frame": "joint_local_axes",
                "actuator_targets": actuators,
                "actuator_ctrlranges": ranges,
                "source_limit_status": "actuator_limit_not_missionos_policy",
            }
        )
    return rows


def _dynamic_limit_source_audit(
    robot_root: ET.Element,
    arm_joint_names: list[str],
    controller_config: dict[str, Any],
    joint_position_controller_source: str,
) -> dict[str, Any]:
    arm_joints = {
        joint.attrib["name"]: joint
        for joint in robot_root.findall(".//joint")
        if joint.attrib.get("name") in arm_joint_names
    }
    robot_declares_velocity = all(
        "velocity" in arm_joints[name].attrib for name in arm_joint_names
    )
    robot_declares_effort = all(
        "effort" in arm_joints[name].attrib for name in arm_joint_names
    )
    arm_configs = controller_config["body_parts"]["arms"]
    controller_declares_velocity_limits = all(
        "velocity_limits" in config for config in arm_configs.values()
    )
    controller_implementation_consumes_velocity_limits = (
        "velocity_limits" in joint_position_controller_source
    )
    return {
        "arm_position_range": {
            "status": "source_available",
            "source": "pinned_robot_description",
        },
        "arm_velocity": {
            "status": "unverified",
            "robot_description_declares_limit": robot_declares_velocity,
            "controller_config_declares_value": (
                controller_declares_velocity_limits
            ),
            "controller_implementation_consumes_value": (
                controller_implementation_consumes_velocity_limits
            ),
            "reason": (
                "pinned robot description has no arm velocity attributes and "
                "JointPositionController ignores velocity_limits via unused kwargs"
            ),
        },
        "arm_acceleration": {
            "status": "unverified",
            "reason": "not declared by pinned robot or controller sources",
        },
        "arm_jerk": {
            "status": "unverified",
            "reason": "not declared by pinned robot or controller sources",
        },
        "arm_effort": {
            "status": (
                "source_available" if robot_declares_effort else "unverified"
            ),
            "reason": (
                "pinned robot description has no arm effort attributes"
                if not robot_declares_effort
                else "declared by pinned robot description"
            ),
        },
        "missionos_admission_dynamic_bounds_available": False,
    }


def inspect(timeout_seconds: float) -> dict[str, Any]:
    fetched = {name: _fetch(source, timeout_seconds) for name, source in SOURCES.items()}
    modality = json.loads(fetched["modality"])
    dataset_info = json.loads(fetched["dataset_info"])
    data_config = _class_assignments(
        fetched["data_config"].decode(), "FourierGr1ArmsOnlyDataConfig"
    )

    expected_action_keys = [
        "action.left_arm",
        "action.right_arm",
        "action.left_hand",
        "action.right_hand",
    ]
    if data_config["action_keys"] != expected_action_keys:
        raise ValueError(f"unexpected action key order: {data_config['action_keys']}")
    if data_config["action_indices"] != list(range(16)):
        raise ValueError(f"unexpected action indices: {data_config['action_indices']}")

    robot_root = ET.fromstring(fetched["robot_xml"])
    if robot_root.find("compiler").attrib.get("angle") != "radian":
        raise ValueError("GR1 robot description no longer declares radians")
    joint_ranges = {
        joint.attrib["name"]: [float(value) for value in joint.attrib["range"].split()]
        for joint in robot_root.findall(".//joint")
        if "name" in joint.attrib and "range" in joint.attrib
    }
    right_arm, left_arm = _derive_arm_joint_order(robot_root)
    for name in left_arm + right_arm:
        if name not in joint_ranges:
            raise ValueError(f"missing arm joint {name}")

    hand_ranges: dict[str, list[float]] = {}
    for source_name in ("left_hand_xml", "right_hand_xml"):
        root = ET.fromstring(fetched[source_name])
        if root.find("compiler").attrib.get("angle") != "radian":
            raise ValueError(f"{source_name} no longer declares radians")
        actuators = root.findall(".//actuator/position")
        side = "L" if source_name == "left_hand_xml" else "R"
        expected_actuator_order = [
            f"{side}_pinky_intermediate_joint",
            f"{side}_pinky_proximal_joint",
            f"{side}_ring_intermediate_joint",
            f"{side}_ring_proximal_joint",
            f"{side}_middle_intermediate_joint",
            f"{side}_middle_proximal_joint",
            f"{side}_index_intermediate_joint",
            f"{side}_index_proximal_joint",
            f"{side}_thumb_distal_joint",
            f"{side}_thumb_proximal_pitch_joint",
            f"{side}_thumb_proximal_yaw_joint",
        ]
        observed_actuator_order = [actuator.attrib["joint"] for actuator in actuators]
        if observed_actuator_order != expected_actuator_order:
            raise ValueError(f"unexpected {source_name} actuator order: {observed_actuator_order}")
        for actuator in actuators:
            hand_ranges[actuator.attrib["joint"]] = [
                float(value) for value in actuator.attrib["ctrlrange"].split()
            ]

    controller_config = json.loads(fetched["controller_config"])
    arm_configs = controller_config["body_parts"]["arms"]
    if any(config["input_type"] != "absolute" for config in arm_configs.values()):
        raise ValueError("GR1 arm controller is no longer absolute")
    if any(config["gripper"]["use_action_scaling"] for config in arm_configs.values()):
        raise ValueError("GR1 hand controller unexpectedly applies action scaling")

    source_text = {
        name: content.decode()
        for name, content in fetched.items()
        if name
        not in {
            "modality",
            "dataset_info",
            "controller_config",
            "robot_xml",
            "left_hand_xml",
            "right_hand_xml",
        }
    }
    required_markers = {
        "state_action_transform": [
            'elif self.mode == "min_max":',
            "return (x + 1) / 2 * (max - min) + min",
        ],
        "policy": ["unnormalized_action = self._get_unnormalized_action(normalized_action)"],
        "dataset_loader": [
            "step_indices = self.delta_indices[key] + base_index",
            'assert key.startswith(modality + ".")',
        ],
        "multistep": [
            "for step in range(self.n_action_steps):",
            "act[key] = value[step, :]",
        ],
        "simulation": ["n_action_steps: int = 16"],
        "robocasa_robot_mapping": [
            'assert controller.input_type == "absolute"',
            "assert not controller.use_action_scaling",
        ],
        "fourier_hand_model": [
            "indices = np.array([0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5])",
        ],
        "robocasa_environment": [
            'controller_configs["control_delta"] = False',
            "raw_obs.update(gather_robot_observations(self.env))",
        ],
        "robocasa_groot_wrapper": [
            "action = self.key_converter.unmap_action(action)",
            "raw_obs, reward, terminated, truncated, info = super().step(action)",
        ],
        "joint_position_controller": [
            'elif self.input_type == "absolute":',
            "self.goal_qpos = action",
            "**kwargs,  # does nothing",
        ],
        "simple_grip_controller": [
            "self.goal_qvel = scaled_delta",
            'return "JOINT_VELOCITY"',
        ],
        "mjcf_model_base": [
            'self._joints = [e.get("name") for e in self._elements.get("joints", [])]',
        ],
        "manipulator_model": [
            "for joint in self.all_joints:",
            "self._arms_joints.append(joint)",
            "return self._arms_joints",
        ],
        "gr1_robot_model": ['arms = ["right", "left"]'],
        "robot_runtime": [
            "return int(len(self.robot_arm_joints) / len(self.arms))",
        ],
        "mobile_robot_runtime": [
            '(start, end) = (None, self._joint_split_idx) if arm == "right" else',
            "self.robot_model.arm_joints[start:end]",
        ],
    }
    for source_name, markers in required_markers.items():
        for marker in markers:
            if marker not in source_text[source_name]:
                raise ValueError(f"required marker missing from {source_name}: {marker}")

    dimension_rows = (
        _joint_rows("action.left_arm", left_arm, joint_ranges)
        + _joint_rows("action.right_arm", right_arm, joint_ranges)
        + _hand_rows("action.left_hand", "l", hand_ranges)
        + _hand_rows("action.right_hand", "r", hand_ranges)
    )
    modality_groups = {
        kind: {
            name: list(range(spec["start"], spec["end"])) for name, spec in modality[kind].items()
        }
        for kind in ("state", "action")
    }
    source_manifest = {
        name: {
            "repository": source.repository,
            "revision": source.revision,
            "path": source.path,
            "sha256": source.sha256,
            "url": source.url,
        }
        for name, source in SOURCES.items()
    }
    dynamic_limit_source_audit = _dynamic_limit_source_audit(
        robot_root,
        left_arm + right_arm,
        controller_config,
        source_text["joint_position_controller"],
    )
    return {
        "schema": "missionos_groot_n15_action_semantics_inspection.v1",
        "claim_boundary": {
            "metadata_and_source_inspection": True,
            "model_inference_this_run": False,
            "simulator_started": False,
            "robot_dispatch": False,
            "observed_motion": False,
            "physical_execution": False,
        },
        "target": {
            "model_family": "GR00T N1.5",
            "dataset": "robot_sim.PickNPlace",
            "dataset_robot_type": dataset_info["robot_type"],
            "simulator_environment": (
                "robocasa_gr1_arms_only_fourier_hands/"
                "TwoArmPnPCarPartBrakepedal_GR1ArmsOnlyFourierHands_Env"
            ),
            "source_revision_note": (
                "RoboCasa does not pin robosuite. This inspection selects the latest "
                "robosuite commit preceding the pinned RoboCasa revision; runtime "
                "compatibility remains unverified until #135."
            ),
        },
        "dataset": {
            "fps": dataset_info["fps"],
            "modality_groups": modality_groups,
            "selected_action_keys_in_concat_order": expected_action_keys,
            "selected_action_dimensions": 26,
            "modality_declares_units": False,
            "modality_declares_per_dimension_joint_names": False,
        },
        "policy_output": {
            "action_horizon_steps": len(data_config["action_indices"]),
            "action_horizon_milliseconds_at_dataset_rate": (
                len(data_config["action_indices"]) / dataset_info["fps"] * 1000
            ),
            "action_delta_indices": data_config["action_indices"],
            "first_action_training_index_relation": (
                "same_base_index_as_current_observation"
            ),
            "first_action_reference_runtime_use": (
                "first_command_applied_by_next_environment_step"
            ),
            "first_action_equals_current_joint_state": "not_established",
            "service_output_is_unnormalized": True,
            "training_transform": "per-key dataset min/max to [-1, 1]",
            "dataset_statistics_are_safety_policy_bounds": False,
        },
        "controller": {
            "rate_hz": 20.0,
            "chunk_consumption": "all_16_samples_sequentially",
            "receding_horizon_or_temporal_ensemble_in_reference_path": False,
            "arm_controller": "absolute JointPositionController goals",
            "hand_controller_conflict": (
                "Six reduced values are expanded and sent unchanged to MuJoCo "
                "position actuators, while SimpleGripController names the quantity "
                "goal_qvel and reports JOINT_VELOCITY."
            ),
            "readback": (
                "next state.* observations are simulator joint qpos; no separate "
                "dispatch ACK is defined"
            ),
        },
        "dynamic_limit_source_audit": dynamic_limit_source_audit,
        "dimensions": dimension_rows,
        "envelope_readiness": {
            "arm_joint_bounds": "source_available_policy_binding_still_required",
            "hand_bounds": "blocked_by_logical_command_semantics_conflict",
            "controller_runtime_compatibility": "unverified",
            "overall": "partially_unblocked",
        },
        "sources": source_manifest,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    try:
        report = inspect(args.timeout_seconds)
    except Exception as exc:
        print(f"inspection failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=None if args.compact else 2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
