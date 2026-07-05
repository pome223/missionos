#!/usr/bin/env python3
"""Extract the TurtleBot3 house floor plan and Nav2 map from its Gazebo SDF.

The turtlebot3_house model builds its walls from box collisions, so the
floor plan can be derived deterministically instead of driving a SLAM run.
This tool reads ``model.sdf`` and writes:

- ``map.pgm`` / ``map.yaml``: an occupancy grid of every collision that
  intersects the robot lidar height band, for Nav2/AMCL localization.
- ``floor_plan.json``: vector wall rectangles, cylinder footprints, and mesh
  furniture markers for the MissionOS read-only indoor map display.

It is an offline asset generator; it never talks to a robot or simulator.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

LIDAR_BAND_Z_M = (0.10, 0.25)
DEFAULT_RESOLUTION_M = 0.05
MARGIN_M = 0.5


def _floats(text: str | None, count: int) -> list[float]:
    values = [float(part) for part in (text or "").split()]
    while len(values) < count:
        values.append(0.0)
    return values[:count]


def _compose(link_pose: list[float], local_pose: list[float]) -> tuple[float, float, float, float]:
    """Compose link and collision poses (planar: x, y, z, yaw)."""

    lx, ly, lz, _, _, lyaw = link_pose
    cx, cy, cz, _, _, cyaw = local_pose
    cos_yaw = math.cos(lyaw)
    sin_yaw = math.sin(lyaw)
    x = lx + cx * cos_yaw - cy * sin_yaw
    y = ly + cx * sin_yaw + cy * cos_yaw
    return x, y, lz + cz, lyaw + cyaw


def _iter_links_with_model_pose(model: ET.Element, base_pose: list[float]):
    model_pose = _compose_pose(base_pose, _floats(model.findtext("pose"), 6))
    for link in model.findall("link"):
        yield model, link, model_pose
    for nested in model.findall("model"):
        yield from _iter_links_with_model_pose(nested, model_pose)


def _compose_pose(outer: list[float], inner: list[float]) -> list[float]:
    """Compose two planar poses (x, y, z, roll, pitch, yaw); yaw-only rotation."""

    ox, oy, oz, _, _, oyaw = outer
    ix, iy, iz, _, _, iyaw = inner
    cos_yaw = math.cos(oyaw)
    sin_yaw = math.sin(oyaw)
    return [
        ox + ix * cos_yaw - iy * sin_yaw,
        oy + ix * sin_yaw + iy * cos_yaw,
        oz + iz,
        0.0,
        0.0,
        oyaw + iyaw,
    ]


def extract_house_geometry(sdf_path: Path) -> dict:
    tree = ET.parse(sdf_path)
    root = tree.getroot()
    walls: list[dict] = []
    cylinders: list[dict] = []
    mesh_markers: list[dict] = []
    z_lo, z_hi = LIDAR_BAND_Z_M
    top = root.find("model")
    if top is None:
        raise SystemExit("SDF has no top-level model element")
    for model, link, model_pose in _iter_links_with_model_pose(
        top, [0.0] * 6
    ):
        model_name = str(model.get("name") or "model")
        link_name = str(link.get("name") or "link")
        if link_name in {"link", "cabinet_bottom_plate"}:
            link_name = model_name
        link_pose = _compose_pose(model_pose, _floats(link.findtext("pose"), 6))
        for collision in link.findall("collision"):
            local_pose = _floats(collision.findtext("pose"), 6)
            x, y, z, yaw = _compose(link_pose, local_pose)
            geometry = collision.find("geometry")
            if geometry is None:
                continue
            box = geometry.find("box")
            cylinder = geometry.find("cylinder")
            mesh = geometry.find("mesh")
            if box is not None:
                sx, sy, sz = _floats(box.findtext("size"), 3)
                if z + sz / 2.0 < z_lo or z - sz / 2.0 > z_hi:
                    continue
                walls.append(
                    {
                        "link": link_name,
                        "x_m": round(x, 4),
                        "y_m": round(y, 4),
                        "yaw_rad": round(yaw, 6),
                        "size_x_m": round(sx, 4),
                        "size_y_m": round(sy, 4),
                    }
                )
            elif cylinder is not None:
                radius = float(cylinder.findtext("radius") or 0.0)
                length = float(cylinder.findtext("length") or 0.0)
                if z + length / 2.0 < z_lo or z - length / 2.0 > z_hi:
                    continue
                cylinders.append(
                    {
                        "link": link_name,
                        "x_m": round(x, 4),
                        "y_m": round(y, 4),
                        "radius_m": round(radius, 4),
                    }
                )
            elif mesh is not None:
                mesh_markers.append(
                    {
                        "link": link_name,
                        "x_m": round(x, 4),
                        "y_m": round(y, 4),
                        "yaw_rad": round(yaw, 6),
                        "mesh_uri": str(mesh.findtext("uri") or ""),
                    }
                )
    return {"walls": walls, "cylinders": cylinders, "mesh_markers": mesh_markers}


def _bounds(geometry: dict) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for wall in geometry["walls"]:
        half = math.hypot(wall["size_x_m"], wall["size_y_m"]) / 2.0
        xs.extend((wall["x_m"] - half, wall["x_m"] + half))
        ys.extend((wall["y_m"] - half, wall["y_m"] + half))
    for cylinder in geometry["cylinders"]:
        xs.extend((cylinder["x_m"] - cylinder["radius_m"], cylinder["x_m"] + cylinder["radius_m"]))
        ys.extend((cylinder["y_m"] - cylinder["radius_m"], cylinder["y_m"] + cylinder["radius_m"]))
    return (
        min(xs) - MARGIN_M,
        max(xs) + MARGIN_M,
        min(ys) - MARGIN_M,
        max(ys) + MARGIN_M,
    )


def rasterize(geometry: dict, resolution_m: float) -> tuple[bytes, dict]:
    min_x, max_x, min_y, max_y = _bounds(geometry)
    width = int(math.ceil((max_x - min_x) / resolution_m))
    height = int(math.ceil((max_y - min_y) / resolution_m))
    grid = bytearray([254]) * 0
    grid = bytearray([254] * (width * height))

    def mark(x_m: float, y_m: float) -> None:
        col = int((x_m - min_x) / resolution_m)
        row = int((max_y - y_m) / resolution_m)
        if 0 <= col < width and 0 <= row < height:
            grid[row * width + col] = 0

    step = resolution_m / 2.0
    for wall in geometry["walls"]:
        cos_yaw = math.cos(wall["yaw_rad"])
        sin_yaw = math.sin(wall["yaw_rad"])
        half_x = wall["size_x_m"] / 2.0
        half_y = wall["size_y_m"] / 2.0
        u = -half_x
        while u <= half_x:
            v = -half_y
            while v <= half_y:
                mark(
                    wall["x_m"] + u * cos_yaw - v * sin_yaw,
                    wall["y_m"] + u * sin_yaw + v * cos_yaw,
                )
                v += step
            u += step
    for cylinder in geometry["cylinders"]:
        radius = cylinder["radius_m"]
        u = -radius
        while u <= radius:
            v = -radius
            while v <= radius:
                if u * u + v * v <= radius * radius:
                    mark(cylinder["x_m"] + u, cylinder["y_m"] + v)
                v += step
            u += step

    header = f"P5\n{width} {height}\n255\n".encode("ascii")
    meta = {
        "resolution": resolution_m,
        "origin": [round(min_x, 4), round(min_y, 4), 0.0],
        "width": width,
        "height": height,
    }
    return header + bytes(grid), meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sdf", type=Path, help="Path to turtlebot3_house model.sdf")
    parser.add_argument("out_dir", type=Path, help="Output directory for map + floor plan")
    parser.add_argument("--resolution", type=float, default=DEFAULT_RESOLUTION_M)
    args = parser.parse_args()

    geometry = extract_house_geometry(args.sdf)
    pgm, meta = rasterize(geometry, args.resolution)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "map.pgm").write_bytes(pgm)
    (args.out_dir / "map.yaml").write_text(
        "image: map.pgm\n"
        f"resolution: {meta['resolution']}\n"
        f"origin: [{meta['origin'][0]}, {meta['origin'][1]}, 0.0]\n"
        "negate: 0\n"
        "occupied_thresh: 0.65\n"
        "free_thresh: 0.196\n",
        encoding="utf-8",
    )
    floor_plan = {
        "schema_version": "missionos_turtlebot3_house_floor_plan_source.v1",
        "source": "turtlebot3_house_model_sdf_collision",
        "lidar_band_z_m": list(LIDAR_BAND_Z_M),
        "bounds": {
            "min_x_m": meta["origin"][0],
            "max_x_m": round(meta["origin"][0] + meta["width"] * meta["resolution"], 4),
            "min_y_m": meta["origin"][1],
            "max_y_m": round(meta["origin"][1] + meta["height"] * meta["resolution"], 4),
        },
        **geometry,
    }
    (args.out_dir / "floor_plan.json").write_text(
        json.dumps(floor_plan, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "walls": len(geometry["walls"]),
                "cylinders": len(geometry["cylinders"]),
                "mesh_markers": len(geometry["mesh_markers"]),
                "map_size": [meta["width"], meta["height"]],
                "out_dir": str(args.out_dir),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
