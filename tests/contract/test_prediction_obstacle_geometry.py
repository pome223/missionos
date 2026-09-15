"""The rendered obstacle and collision volume must describe the same probe."""

import xml.etree.ElementTree as ET

import pytest

from scripts.tb3_prediction.obstacle_geometry import SHAPES, add_geometry


@pytest.mark.parametrize("width,height", [(0.4, 0.6), (0.6, 5)])
@pytest.mark.parametrize("shape", SHAPES)
def test_visual_and_collision_have_matching_dimensions(width, height, shape):
    nodes = [ET.Element(kind) for kind in ("visual", "collision")]
    for node in nodes:
        add_geometry(node, shape, width, height)
    assert ET.tostring(nodes[0][0]) == ET.tostring(nodes[1][0])
    geometry = nodes[0][0]
    if shape == "cylinder":
        assert float(geometry.findtext("cylinder/radius")) * 2 == width
        assert float(geometry.findtext("cylinder/length")) == height
    else:
        x, y, z = map(float, geometry.findtext("box/size").split())
        assert (x, z) == (width, height)
        assert y == width * (1.5 if shape == "wide_box" else 1)


def test_unrecognized_shape_cannot_silently_fall_back_to_box():
    node = ET.Element("visual")
    with pytest.raises(ValueError, match="unsupported"):
        add_geometry(node, "unknown", 0.4, 0.6)
    assert len(node) == 0
