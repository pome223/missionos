"""Independent physical/authority edge cases for bounded urban feasibility."""

import copy

import pytest

from scripts.px4_aerial_flight_session import sign_command
from scripts.px4_urban_wam_trial import building_sdf, validate_trigger
from scripts.urban_navigation_contract import (
    route_check,
    scene_spec,
    segment_box_clearance,
)


def test_segment_crosses_thin_wall_even_when_both_endpoints_are_clear():
    assert (
        segment_box_clearance([0, 0, 3], [10, 0, 3], [4.999, -2, 0], [5.001, 2, 5])
        == -0.6
    )


def test_swept_volume_rejects_gap_that_only_fits_camera_center():
    assert segment_box_clearance(
        [0, 0.5, 3], [10, 0.5, 3], [4, 1, 0], [6, 4, 6]
    ) == pytest.approx(-0.1)


def test_segment_minimum_can_occur_between_endpoints_at_corner():
    assert segment_box_clearance(
        [-2, 1, 0], [1, -2, 0], [0, 0, -1], [2, 2, 1], radius=0
    ) == pytest.approx(2**-0.5)


@pytest.mark.parametrize("family", ["gap", "climb", "detour"])
def test_declared_feasibility_routes_have_envelope_margin(family):
    scene = scene_spec(family)
    assert route_check(scene, scene["routes"][scene["feasibility_route"]])["admissible"]


@pytest.mark.parametrize("family", ["climb", "detour"])
def test_forward_is_rejected_where_actual_building_blocks_it(family):
    scene = scene_spec(family)
    assert not route_check(scene, scene["routes"]["forward"])["admissible"]


def test_visual_and_collision_use_identical_mesh_scale():
    import xml.etree.ElementTree as ET

    b = scene_spec("gap")["buildings"][0]
    root = ET.fromstring(building_sdf(b))
    assert ET.tostring(root.find("model/link/visual/geometry")) == ET.tostring(
        root.find("model/link/collision/geometry")
    )


def test_urban_trigger_requires_unchanged_route_and_recent_signature():
    config = dict(
        session_id="session",
        scene_sha256="scene",
        route_sha256="route",
        approved_instruction_ref="user",
    )
    command = {**config, "issued_at_unix_s": 100.0, "expires_at_unix_s": 104.0}
    envelope = sign_command(command, b"key")
    assert validate_trigger(envelope, config, b"key", 101.0) == command
    with pytest.raises(ValueError, match="expired"):
        validate_trigger(envelope, config, b"key", 105.0)
    mutated = copy.deepcopy(envelope)
    mutated["command"]["route_sha256"] = "another"
    with pytest.raises(ValueError, match="signature"):
        validate_trigger(mutated, config, b"key", 101.0)
    with pytest.raises(ValueError, match="scope"):
        validate_trigger(
            sign_command(mutated["command"], b"key"), config, b"key", 101.0
        )
