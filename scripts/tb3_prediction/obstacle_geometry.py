"""Matched visual/collision geometry for opt-in simulator coverage probes."""

import xml.etree.ElementTree as ET

SHAPES = ("box", "cylinder", "wide_box")


def add_geometry(parent, shape, width, height):
    if shape not in SHAPES:
        raise ValueError("unsupported obstacle shape")
    geometry = ET.SubElement(parent, "geometry")
    if shape == "cylinder":
        cylinder = ET.SubElement(geometry, "cylinder")
        ET.SubElement(cylinder, "radius").text = str(width / 2)
        ET.SubElement(cylinder, "length").text = str(height)
    else:
        transverse = width * (1.5 if shape == "wide_box" else 1)
        ET.SubElement(
            ET.SubElement(geometry, "box"), "size"
        ).text = f"{width} {transverse} {height}"
