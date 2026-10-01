"""Wind must affect physics, not only a config label."""

import math
import xml.etree.ElementTree as ET

import pytest

from src.runtime.yokohama_wind import add_wind, verify_wind


def build(tmp_path):
    p = tmp_path / "models/worlds/default.sdf"
    p.parent.mkdir(parents=True)
    p.write_text(
        '<sdf><world name="default"><model name="delivery_payload"><link name="link"/></model></world></sdf>'
    )
    p = tmp_path / "models/x500_base/model.sdf"
    p.parent.mkdir(parents=True)
    p.write_text('<sdf><model name="x500_base"><link name="base_link"/></model></sdf>')
    world = add_wind(tmp_path, {"payload_delivery": {"enabled": True}}, 2)
    rows = []
    for t in [8, 9, 10]:
        rows.append(
            dict(
                sim_s=t,
                wind_probe=dict(
                    wind_witness=dict(
                        sensor_sim_s=t,
                        xyz=[2 * (t - 2 + (t + 2) * math.exp(-t)), 3000, 100],
                        age_s=0,
                    ),
                    wind_control=dict(sensor_sim_s=t, xyz=[0, 3010, 100], age_s=0),
                ),
            )
        )
    return {"world": world}, rows


@pytest.mark.parametrize(
    "fault", [None, "no_force", "stale", "disabled_cargo", "missing_plugin", "nonfinite"]
)
def test_wind_requires_force_and_vehicle_and_cargo_flags(tmp_path, fault):
    config, rows = build(tmp_path)
    if fault == "no_force":
        for r in rows:
            r["wind_probe"]["wind_witness"]["xyz"][0] = 0
    if fault == "nonfinite":
        rows[0]["wind_probe"]["wind_witness"]["xyz"][0] = math.nan
    if fault == "stale":
        rows[0]["wind_probe"]["wind_witness"]["age_s"] = 10
    if fault in ("disabled_cargo", "missing_plugin", "nonfinite"):
        path = tmp_path / "models/worlds/default.sdf"
        tree = ET.parse(path)
        world = tree.getroot().find("world")
        if fault == "disabled_cargo":
            world.find("model[@name='delivery_payload']/link/enable_wind").text = "false"
        else:
            world.remove(world.find("plugin"))
        tree.write(path)
    assert (verify_wind(tmp_path, config, rows)["status"] == "passed") == (fault is None)


@pytest.mark.parametrize("speed", [-1, 0, 9, math.nan, math.inf])
def test_invalid_wind_rejected_before_files(tmp_path, speed):
    with pytest.raises(ValueError):
        add_wind(tmp_path, {}, speed)
