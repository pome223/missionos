from xml.etree import ElementTree as ET

import pytest

from scripts.inspect_groot_n15_action_semantics import (
    EXPECTED_LEFT_ARM_JOINTS,
    EXPECTED_RIGHT_ARM_JOINTS,
    _derive_arm_joint_order,
    _dynamic_limit_source_audit,
)


def _robot_xml(joint_names: list[str]) -> ET.Element:
    joints = "".join(f'<joint name="{name}" range="-1 1" />' for name in joint_names)
    return ET.fromstring(f"<mujoco><worldbody>{joints}</worldbody></mujoco>")


def test_arm_joint_order_is_derived_right_then_left() -> None:
    root = _robot_xml(EXPECTED_RIGHT_ARM_JOINTS + EXPECTED_LEFT_ARM_JOINTS)

    right, left = _derive_arm_joint_order(root)

    assert right == EXPECTED_RIGHT_ARM_JOINTS
    assert left == EXPECTED_LEFT_ARM_JOINTS


def test_arm_joint_order_drift_fails_closed() -> None:
    drifted = EXPECTED_RIGHT_ARM_JOINTS.copy()
    drifted[1], drifted[2] = drifted[2], drifted[1]
    root = _robot_xml(drifted + EXPECTED_LEFT_ARM_JOINTS)

    with pytest.raises(ValueError, match="GR1 arm joint order drifted"):
        _derive_arm_joint_order(root)


def test_declared_but_unconsumed_velocity_limit_is_unverified() -> None:
    names = EXPECTED_RIGHT_ARM_JOINTS + EXPECTED_LEFT_ARM_JOINTS
    root = _robot_xml(names)
    controller_config = {
        "body_parts": {
            "arms": {
                "left": {"velocity_limits": [-1, 1]},
                "right": {"velocity_limits": [-1, 1]},
            }
        }
    }

    audit = _dynamic_limit_source_audit(
        root,
        names,
        controller_config,
        "def __init__(self, **kwargs): pass",
    )

    assert audit["arm_velocity"]["status"] == "unverified"
    assert (
        audit["arm_velocity"]["controller_config_declares_value"]
        is True
    )
    assert (
        audit["arm_velocity"][
            "controller_implementation_consumes_value"
        ]
        is False
    )
    assert audit["missionos_admission_dynamic_bounds_available"] is False
