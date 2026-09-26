"""CPU contract checks; actual simulator evidence comes from the opt-in CLI."""

import json
from pathlib import Path
import numpy as np
import pytest
from src.runtime.yokohama_scene import add_face_normals, build_frame, to_world, to_source
from scripts.yokohama_flight_worker import hold_metrics, hold_passes

BUNDLE = Path(__file__).resolve().parents[1] / "docs/examples/yokohama-urban-scene"


def test_source_altitude_is_rebased_and_frame_is_invertible():
    pytest.importorskip("pyproj")
    scene = json.loads((BUNDLE / "scene.json").read_text())
    route = json.loads((BUNDLE / "route.json").read_text())
    frame = build_frame(scene, route)
    points = np.array([w["xyz_m"] for w in route["waypoints"]])
    world = to_world(points, frame)
    np.testing.assert_allclose(world[0, :2], 0, atol=1e-8)
    assert 12 < world[0, 2] < 18
    np.testing.assert_allclose(to_source(world, frame), points, atol=1e-9)
    assert abs(np.linalg.det(frame["source_to_world_matrix"]) - 1) < 0.001
    with pytest.raises(ValueError):
        to_world([np.nan, 0, 0], frame)


def test_normals_keep_faces_and_remove_only_zero_area(tmp_path):
    path = tmp_path / "triangles.obj"
    path.write_text("o part\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\nf 1 1 2\n")
    receipt = add_face_normals(path)
    assert receipt == dict(triangles_with_normals=1, zero_area_faces_omitted=1)
    assert "vn 0.000000000 0.000000000 1.000000000" in path.read_text()
    assert "f 1//1 2//1 3//1" in path.read_text()


def measured_hold():
    return [
        dict(
            sim_s=t,
            vehicle=dict(xyz=[1, 2, 15], age_s=0.02),
            velocity_ned=[0.01, 0, 0],
            nav_state=4,
            landed=False,
            arming_state=2,
        )
        for t in range(31)
    ]


CONFIG = dict(
    hold_duration_sim_s=30,
    hold_horizontal_tolerance_m=1,
    hold_vertical_tolerance_m=0.6,
    hold_max_speed_mps=0.5,
)


def test_hold_requires_measured_duration_and_stability():
    rows = measured_hold()
    assert hold_passes(hold_metrics(rows, [1, 2, 15]), CONFIG)
    assert not hold_passes(hold_metrics(rows[:20], [1, 2, 15]), CONFIG)
    rows[12]["vehicle"]["xyz"][0] = 3
    assert not hold_passes(hold_metrics(rows, [1, 2, 15]), CONFIG)


@pytest.mark.parametrize("mutation", ["stale", "mode", "ground", "speed", "gap"])
def test_hold_rejects_invalid_observation(mutation):
    rows = measured_hold()
    if mutation == "stale":
        rows[10]["vehicle"]["age_s"] = 3
    if mutation == "mode":
        rows[10]["nav_state"] = 3
    if mutation == "ground":
        rows[10]["landed"] = True
    if mutation == "speed":
        rows[10]["velocity_ned"] = [2, 0, 0]
    if mutation == "gap":
        rows = rows[:10] + rows[13:]
    assert not hold_passes(hold_metrics(rows, [1, 2, 15]), CONFIG)


@pytest.mark.parametrize("fault", ["nan", "reversed", "duplicate"])
def test_hold_rejects_bad_numeric_or_clock_records(fault):
    rows = measured_hold()
    if fault == "nan":
        rows[12]["vehicle"]["xyz"][2] = float("nan")
    elif fault == "reversed":
        rows.reverse()
    else:
        rows[12]["sim_s"] = rows[11]["sim_s"]
    with pytest.raises(ValueError):
        hold_metrics(rows, [1, 2, 15])


def test_replay_hold_boundary_excludes_settling_tick_without_dropping_hold_samples():
    from scripts.verify_yokohama_sitl import recorded_hold_rows

    rows = [dict(sim_s=t, wall_s=i, phase="D1") for i, t in enumerate([10, 10, 11, 12])]
    events = [dict(event="hold_started", phase="D1", wall_s=1.1)]
    hold = dict(point="D1", start_sim_s=10, end_sim_s=12, samples=3)
    assert recorded_hold_rows(rows, events, hold) == rows[1:]
    with pytest.raises(ValueError):
        recorded_hold_rows(rows, [], hold)
