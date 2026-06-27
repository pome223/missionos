"""Contract tests for real payload separation (detachable joint patch + verify)."""

import pytest

from src.runtime.missionos_play_payload import (
    inject_detachable_joint,
    inject_payload_model,
    verify_separation,
)

pytestmark = pytest.mark.contract


def test_inject_detachable_joint_into_model():
    sdf = "<model name='x500'>\n  <link name='base_link'/>\n</model>\n"
    patched = inject_detachable_joint(sdf)
    assert "DetachableJoint" in patched
    assert "delivery_payload" in patched
    assert patched.index("DetachableJoint") < patched.index("</model>")


def test_inject_payload_model_into_world():
    world = "<world name='default'>\n  <model name='ground_plane'/>\n</world>\n"
    patched = inject_payload_model(world, mass_kg=0.05)
    assert "<model name=\"delivery_payload\">" in patched
    assert patched.index("delivery_payload") < patched.index("</world>")


def test_inject_requires_close_tags():
    with pytest.raises(ValueError):
        inject_detachable_joint("<model>no close")
    with pytest.raises(ValueError):
        inject_payload_model("<world>no close", mass_kg=0.05)


def test_verify_separation_requires_a_real_drop():
    # carried at 2.5 m, fell to 0.02 m -> separated
    assert verify_separation(2.52, 0.02) is True
    # barely moved -> not separated (still attached / noise)
    assert verify_separation(2.50, 2.48) is False
    # missing reads -> cannot claim separation
    assert verify_separation(None, 0.02) is False
    assert verify_separation(2.5, None) is False
