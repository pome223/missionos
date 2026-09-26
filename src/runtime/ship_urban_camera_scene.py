"""Scripted test geometry kept outside the image-only selector's inputs."""

from __future__ import annotations

import math

from src.runtime.ship_delivery_world import _box


CASES = ("short_clear", "long_block", "brake_stop")
SAMPLE_TIMES = (0, 0.5, 1, 1.5, 2, 4, 8)


def capture_config(run_id):
    return {
        "run_id": run_id,
        "cases": [
            {
                "case": case,
                "samples": [{"scripted_at_s": t, "x_m": actor_x(case, t)} for t in SAMPLE_TIMES],
            }
            for case in CASES
        ],
    }


def actor_x(case, t):
    if case not in CASES or not math.isfinite(t) or t < 0:
        raise ValueError("Invalid scripted case or time")
    if case == "short_clear":
        x = 2 * t
    elif case == "long_block":
        x = 0.15 * t
    elif t <= 4:
        x = 4 * t - 0.5 * t * t
    else:
        x = 8 + 2 * max(0, t - 40)
    return min(16, x)


def analytic_clearance_time(case):
    return {"short_clear": 6.5, "long_block": 13 / 0.15, "brake_stop": 42.5}[case]


def camera_world():
    """Fixed camera at the compact route's entry; no aircraft is spawned."""
    geometry = [
        _box("coastal_land", (0, 2100, -2), (1000, 4000, 4), "0.4 0.45 0.3 1"),
        _box("sea", (0, 50, -1), (1000, 300, 0.05), "0.03 0.2 0.5 1"),
        _box("urban_obstacle", (0, 170, 30), (16, 8, 60), "0.9 0.25 0.1 1"),
    ]
    for row, north in enumerate((145, 265, 345)):
        for side in (-1, 1):
            geometry.append(
                _box(
                    f"building_{row}_{side + 1}",
                    (side * 45, north, 22.5),
                    (35, 30, 45),
                    "0.6 0.6 0.65 1",
                )
            )
    return (
        """<sdf version="1.9"><world name="default">
      <physics name="1ms" type="ignored"><max_step_size>0.001</max_step_size>
      <real_time_factor>0</real_time_factor></physics>
      <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
      <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
      <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
      <plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors">
      <render_engine>ogre2</render_engine></plugin>
      <scene><ambient>0.8 0.8 0.8 1</ambient><background>0.6 0.7 0.8 1</background></scene>
      <light name="sun" type="directional"><pose>0 0 500 0 0 0</pose>
      <diffuse>0.8 0.8 0.8 1</diffuse><specular>0.2 0.2 0.2 1</specular>
      <direction>-0.5 0.1 -0.9</direction><cast_shadows>false</cast_shadows></light>
      """
        + "\n".join(geometry)
        + """
      <model name="entry_camera"><static>true</static><pose>0 70 30 0 0 1.5707963267948966</pose>
      <link name="link"><sensor name="rgb" type="camera"><always_on>true</always_on>
      <update_rate>10</update_rate><topic>/ship/urban/rgb</topic>
      <camera><horizontal_fov>1.0471975512</horizontal_fov>
      <image><width>640</width><height>360</height><format>R8G8B8</format></image>
      <clip><near>0.1</near><far>500</far></clip></camera></sensor></link></model>
      </world></sdf>"""
    )
