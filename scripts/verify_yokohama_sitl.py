#!/usr/bin/env python3
"""Read-only verification of CPU Gazebo observations against frozen scene geometry."""

from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import sys
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.runtime.yokohama_scene import to_world, to_source, sha256  # noqa: E402
from scripts.yokohama_flight_worker import hold_metrics, hold_passes  # noqa: E402


def read(path):
    return json.loads(path.read_text())


def depth_check(root, config, scene):
    frame = config["world"]["frame"]
    triangles = np.concatenate(
        [
            to_world(np.array(o["vertices"]).reshape(-1, 3), frame)[
                np.array(o["triangles"]).reshape(-1, 3)
            ]
            for o in scene["objects"]
        ]
    )
    a = triangles[:, 0]
    e1 = triangles[:, 1] - a
    e2 = triangles[:, 2] - a
    origin = np.array(config["world"]["points"][1]["world_xyz_m"])
    direction = np.array(config["world"]["points"][2]["world_xyz_m"]) - origin
    yaw = math.atan2(direction[1], direction[0])
    rot = np.array(
        [[math.cos(yaw), -math.sin(yaw), 0], [math.sin(yaw), math.cos(yaw), 0], [0, 0, 1]]
    )
    depth = np.fromfile(root / "images/scene-scene_depth.f32", dtype="<f4").reshape(360, 640)
    info = read(root / "camera-info.json")
    k = info["k"]
    rows = []
    # Fixed pixel grid. Do not cherry-pick pixels based on their measured error.
    for u in [60, 160, 260, 380, 480, 580]:
        for v in [90, 180, 270, 330]:
            ray = rot @ np.array([1, -(u + 0.5 - k[2]) / k[0], -(v + 0.5 - k[5]) / k[4]])
            h = np.cross(ray, e2)
            det = np.sum(e1 * h, axis=1)
            valid = np.abs(det) > 1e-9
            inv = np.divide(1, det, out=np.zeros_like(det), where=valid)
            s = origin - a
            U = inv * np.sum(s * h, axis=1)
            q = np.cross(s, e1)
            V = inv * np.sum(ray * q, axis=1)
            t = inv * np.sum(e2 * q, axis=1)
            t = np.where(valid & (U >= 0) & (V >= 0) & (U + V <= 1) & (t > 0.1), t, np.inf)
            expected = float(np.min(t))
            observed = float(depth[v, u])
            rows.append(
                dict(
                    pixel=[u, v],
                    source_ray_depth_m=expected,
                    gazebo_depth_m=observed,
                    absolute_error_m=abs(expected - observed),
                )
            )
    return dict(
        samples=rows,
        max_error_m=max(r["absolute_error_m"] for r in rows),
        tolerance_m=0.05,
        passed=all(
            math.isfinite(r["absolute_error_m"]) and r["absolute_error_m"] < 0.05 for r in rows
        ),
    )


def recorded_hold_rows(rows, events, hold):
    # World stats may repeat between the final settling query and hold start.
    # Use the explicit boundary event and recorded count, not every row sharing
    # the same simulator timestamp. Never collapse samples within the hold.
    starts = [e for e in events if e["event"] == "hold_started" and e["phase"] == hold["point"]]
    if len(starts) != 1:
        raise ValueError("Missing or ambiguous hold start event")
    start = starts[0]
    candidates = [
        i
        for i, r in enumerate(rows)
        if r["phase"] == hold["point"]
        and r["sim_s"] == hold["start_sim_s"]
        and r["wall_s"] <= start["wall_s"]
    ]
    if not candidates:
        raise ValueError("Hold start observation is missing")
    index = candidates[-1]
    selected = rows[index : index + hold["samples"]]
    if (
        len(selected) != hold["samples"]
        or selected[-1]["sim_s"] != hold["end_sim_s"]
        or any(r["phase"] != hold["point"] for r in selected)
    ):
        raise ValueError("Hold interval binding mismatch")
    return selected


def verify(root, bundle):
    result, config = read(root / "result.json"), read(root / "config.json")
    world = config["world"]
    checks = {}
    checks["run_finished_successfully"] = (
        result["status"] == "passed" and result.get("cleanup") is True
    )
    checks["world_binding"] = (
        sha256(root / "models/worlds/default.sdf")
        == world["world_sha256"]
        == result["world"]["world_sha256"]
    )
    checks["mesh_binding"] = all(
        sha256(root / "assets" / n) == v for n, v in world["asset_sha256"].items()
    )
    checks["model_binding"] = bool(world.get("model_sha256")) and all(
        sha256(root / n) == v for n, v in world.get("model_sha256", {}).items()
    )
    checks["source_binding"] = all(
        sha256(bundle / n) == v for n, v in world["source_sha256"].items()
    )
    checks["worker_source_binding"] = all(
        sha256(root / n) == v for n, v in result["source_sha256"].items()
    )
    inspect = read(root / "container-inspect.json")[0]
    checks["cpu_isolated"] = (
        inspect["HostConfig"]["NetworkMode"] == "none"
        and not inspect["HostConfig"].get("DeviceRequests")
        and not inspect["HostConfig"].get("Devices")
        and "LIBGL_ALWAYS_SOFTWARE=1" in inspect["Config"]["Env"]
        and result.get("nvidia_device_absent") is True
    )
    checks["no_models_invoked"] = all(
        result[k] is False
        for k in ["vla_invoked", "wam_invoked", "physical_execution_invoked", "gpu_requested"]
    )
    output = dict(run_id=config["run_id"], phase=config["phase"], checks=checks)
    observed = read(root / "worker-result.json")
    checks["worker_binding"] = (
        observed["run_id"] == config["run_id"] and observed["world_sha256"] == world["world_sha256"]
    )
    if config["phase"] == "contacts":
        checks["contact_cases"] = len(observed["cases"]) == 3 and all(
            v["passed"] for v in observed["cases"].values()
        )
        events = [
            json.loads(line) for line in (root / "sensor-events.jsonl").read_text().splitlines()
        ]
        checks["positive_negative_contacts"] = all(
            any(
                name in e["collision1"] + e["collision2"]
                and target in e["collision1"] + e["collision2"]
                for e in events
            )
            for name, target in [("wall_probe", "collision-prisms"), ("ground_probe", "terrain")]
        ) and not any("free_probe" in e["collision1"] + e["collision2"] for e in events)
    else:
        rows = [
            json.loads(line) for line in (root / "flight-trajectory.jsonl").read_text().splitlines()
        ]
        checks["trajectory_binding"] = (
            all(
                r["run_id"] == config["run_id"] and r["world_sha256"] == world["world_sha256"]
                for r in rows
            )
            and len({r["vehicle"]["id"] for r in rows}) == 1
        )
        checks["trajectory_finite_ordered"] = all(
            math.isfinite(x) for r in rows for x in [r["sim_s"], *r["vehicle"]["xyz"]]
        ) and all(b["sim_s"] >= a["sim_s"] for a, b in zip(rows, rows[1:]))
        flight_events = [
            json.loads(line) for line in (root / "flight-events.jsonl").read_text().splitlines()
        ]
        holds = []
        for hold in observed["holds"]:
            measured = recorded_hold_rows(rows, flight_events, hold)
            metrics = hold_metrics(measured, hold["target_world_xyz_m"])
            metrics["passed"] = hold_passes(metrics, config)
            metrics["point"] = hold["point"]
            holds.append(metrics)
        output["holds_recomputed"] = holds
        checks["seven_measured_holds"] = len(holds) == 7 and all(r["passed"] for r in holds)
        final = rows[-1]
        checks["returned_landed_disarmed"] = (
            final["landed"] is True
            and final["arming_state"] == 1
            and math.hypot(*final["vehicle"]["xyz"][:2]) < 1.5
        )
        events = [
            json.loads(line) for line in (root / "sensor-events.jsonl").read_text().splitlines()
        ]
        checks["contact_sensor_outcome"] = not any(
            e["topic"] == "city" and "x500" in e["collision1"] + e["collision2"] for e in events
        ) and any(
            e["topic"] == "launch_pad" and "x500" in e["collision1"] + e["collision2"]
            for e in events
        )
        output["depth_check"] = depth_check(root, config, read(bundle / "scene.json"))
        checks["depth_matches_source"] = output["depth_check"]["passed"]
        # Conservative 1 m-radius horizontal swept envelope against all height-overlapping prisms.
        from shapely.geometry import LineString, shape

        points = to_source(np.array([r["vehicle"]["xyz"] for r in rows]), world["frame"])
        footprints = read(bundle / "collision-footprints.geojson")["features"]
        clearances = []
        for a, b in zip(points, points[1:]):
            line = LineString([a[:2], b[:2]])
            relevant = [
                shape(f["geometry"])
                for f in footprints
                if f["properties"]["zmax"] >= min(a[2], b[2]) - 1
                and f["properties"]["zmin"] <= max(a[2], b[2]) + 1
            ]
            if relevant:
                clearances.append(min(line.distance(g) for g in relevant))
        output["sampled_swept_centerline_clearance_m"] = min(clearances)
        output["assumed_radius_m"] = 1
        checks["sampled_swept_envelope_clear"] = min(clearances) > 1
        output["measured_path_length_m"] = sum(
            math.dist(a["vehicle"]["xyz"], b["vehicle"]["xyz"]) for a, b in zip(rows, rows[1:])
        )
        output["sim_duration_s"] = rows[-1]["sim_s"] - rows[0]["sim_s"]
        output["wall_duration_s"] = rows[-1]["wall_s"] - rows[0]["wall_s"]
    output["status"] = "passed" if all(checks.values()) else "failed"
    output["evidence_hashes"] = {
        n: sha256(root / n)
        for n in [
            "config.json",
            "result.json",
            "worker-result.json",
            "sensor-events.jsonl",
            "container-inspect.json",
        ]
        + (
            [
                "flight-trajectory.jsonl",
                "flight-events.jsonl",
                "hold-results.json",
                "video-frames.json",
            ]
            if config["phase"] == "flight"
            else ["probe-trajectory.jsonl"]
        )
    }
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.run_dir, REPO / "docs/examples/yokohama-urban-scene")
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"status": result["status"], "checks": result["checks"]}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
