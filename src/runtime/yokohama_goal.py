"""Conservative source-mesh planning for pre-departure CPU fixture delivery.

No map click is flight authority. Coordinates are source EPSG:6677 local metres
and EPSG:6697 heights; the frozen D1 origin defines the existing ENU transform.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from shapely.geometry import LineString, Point, shape
from shapely.ops import unary_union

from src.runtime.yokohama_payload import digest
from src.runtime.yokohama_scene import build_frame, sha256, to_world

BUNDLE = Path(__file__).resolve().parents[2] / "docs/examples/yokohama-urban-scene"
ASSETS = ("scene.json", "route.json", "collision-footprints.geojson", "collision-prisms.obj")
RADIUS = 1.0
RESIDUAL = 3.0
LANDING_RADIUS = 3.0
ALTITUDE = 18.0


class GoalPlanner:
    def __init__(self, bundle=BUNDLE):
        self.bundle = Path(bundle)
        self.hashes = {n: sha256(self.bundle / n) for n in ASSETS}
        expected = json.loads((self.bundle / "files.sha256.json").read_text())
        if any(self.hashes[n] != expected[n] for n in ASSETS):
            raise ValueError("地図の原典hashが一致しません")
        self.version = digest(self.hashes)
        self.scene = json.loads((self.bundle / "scene.json").read_text())
        self.legacy = json.loads((self.bundle / "route.json").read_text())
        self.frame = build_frame(self.scene, self.legacy)
        self.start = self.legacy["waypoints"][0]["xyz_m"]
        features = json.loads((self.bundle / "collision-footprints.geojson").read_text())[
            "features"
        ]
        # All footprints, including low buildings and bridges, block a corridor.
        # This is stricter than flying over them at cruise altitude.
        self.obstacles = unary_union([shape(f["geometry"]) for f in features])
        terrain = next(o for o in self.scene["objects"] if o["kind"] == "terrain")
        tri = np.asarray(terrain["vertices"]).reshape(-1, 3)[
            np.asarray(terrain["triangles"]).reshape(-1, 3)
        ]
        self.a = tri[:, 0]
        self.b = tri[:, 1] - self.a
        self.c = tri[:, 2] - self.a
        self.den = self.b[:, 0] * self.c[:, 1] - self.b[:, 1] * self.c[:, 0]
        self.lo = tri[:, :, :2].min(axis=1)
        self.hi = tri[:, :, :2].max(axis=1)

    def current(self):
        if {n: sha256(self.bundle / n) for n in ASSETS} != self.hashes:
            raise ValueError("scene versionが変化しました。Gatewayを再起動して再計画してください")

    def height(self, xy):
        p = np.asarray(xy)
        indices = np.where(
            ((self.lo <= p + 1e-8) & (self.hi >= p - 1e-8)).all(axis=1) & (np.abs(self.den) > 1e-10)
        )[0]
        d = p - self.a[indices, :2]
        u = (d[:, 0] * self.c[indices, 1] - d[:, 1] * self.c[indices, 0]) / self.den[indices]
        v = (self.b[indices, 0] * d[:, 1] - self.b[indices, 1] * d[:, 0]) / self.den[indices]
        hit = np.where((u >= -1e-8) & (v >= -1e-8) & (u + v <= 1 + 1e-8))[0]
        if not len(hit):
            raise ValueError("原典地形のない地点です")
        i = hit[0]
        j = indices[i]
        return float(self.a[j, 2] + u[i] * self.b[j, 2] + v[i] * self.c[j, 2])

    def ground_goal(self, xy):
        if (
            not isinstance(xy, list)
            or len(xy) != 2
            or any(
                isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                for x in xy
            )
        ):
            raise ValueError("有限の地図座標 [x,y] が必要です")
        x, y = map(float, xy)
        if not (-297 <= x <= 297 and -297 <= y <= 297):
            raise ValueError("600m四方の範囲外、または境界に近すぎます")
        if self.obstacles.distance(Point(x, y)) < LANDING_RADIUS + RADIUS + RESIDUAL:
            raise ValueError("建物・橋梁内、または着陸域の衝突余裕不足です")
        z = self.height([x, y])
        heights = [
            self.height([x + dx, y + dy])
            for dx, dy in [
                (3, 0),
                (-3, 0),
                (0, 3),
                (0, -3),
                (2.12, 2.12),
                (-2.12, 2.12),
                (2.12, -2.12),
                (-2.12, -2.12),
            ]
        ]
        if max(abs(h - z) for h in heights) > 0.3 or ALTITUDE - max(heights + [z]) < 5:
            raise ValueError("地表の傾斜または高度余裕が着陸候補の制約を満たしません")
        return [x, y, z]

    def clear(self, a, b):
        if self.obstacles.distance(LineString([a[:2], b[:2]])) < RADIUS + RESIDUAL:
            return False
        # Terrain is checked every <=2m; fixture qualification only.
        count = max(1, math.ceil(math.dist(a[:2], b[:2]) / 2))
        try:
            return all(
                ALTITUDE - self.height([a[j] + (b[j] - a[j]) * i / count for j in (0, 1)]) >= 5
                for i in range(count + 1)
            )
        except ValueError:
            return False

    def plan(self, xy):
        self.current()
        from src.intelligence.yokohama_pad_jev import configuration

        judge = configuration()
        goal = self.ground_goal(xy)
        # Preserve the legacy urban route for the original pad, with fresh checks.
        original = self.legacy["delivery_pad"]["center_xyz_m"]
        legacy = math.dist(goal[:2], original[:2]) < 0.001
        if math.dist(goal[:2], original[:2]) > 30:
            raise ValueError("配送候補域は既存padから30m以内です")
        path = [p["xyz_m"] for p in self.legacy["waypoints"][:3]] + [[*goal[:2], ALTITUDE]]
        if not all(self.clear(a, b) for a, b in zip(path, path[1:])):
            raise ValueError("経路の衝突余裕不足です")
        ship = [
            self.start[0] + 1400 * math.cos(math.pi / 12),
            self.start[1] + 1400 * math.sin(math.pi / 12),
            ALTITUDE,
        ]
        home = [*ship[:2], self.frame["source_origin_xyz_m"][2]]
        inbound = [home, ship, *path, [*goal[:2], goal[2] + 3]]
        returning = [[*goal[:2], ALTITUDE], *list(reversed(path[:-1])), ship, home]
        return dict(
            schema="missionos.yokohama-map-plan.v1",
            mission_judge=judge,
            scene_version=self.version,
            source_sha256=self.hashes,
            runtime_source_sha256=runtime_hashes(),
            goal_source_xyz_m=goal,
            goal_world_enu_m=to_world(goal, self.frame).tolist(),
            frame=self.frame,
            urban_route_source_xyz_m=path,
            outbound_source_xyz_m=inbound,
            return_source_xyz_m=returning,
            home_source_xyz_m=home,
            legacy_route=legacy,
            constraints=dict(
                vehicle_radius_m=RADIUS,
                residual_horizontal_clearance_m=RESIDUAL,
                landing_radius_m=LANDING_RADIUS,
                terrain_sampling_m=2,
                cruise_source_height_m=ALTITUDE,
                minimum_ground_clearance_m=5,
                max_landing_height_variation_m=0.3,
            ),
            execution_target="px4_gazebo_fixture",
            physical_execution_invoked=False,
            qualification="bounded D1/D2/D3 corridor, stationary ship and zero wind; no real landing certification",
        )


def runtime_hashes():
    root = BUNDLE.parents[2]
    paths = sorted(
        {
            *root.glob("src/runtime/yokohama*.py"),
            *root.glob("scripts/yokohama*.py"),
            *root.glob("scripts/verify_yokohama*.py"),
            root / "src/gateway/yokohama_map.py",
            root / "src/gateway/yokohama_dispatch.py",
            root / "src/intelligence/yokohama_delivery_agents.py",
            root / "src/gateway/yokohama_delivery_chat.py",
            root / "src/intelligence/yokohama_pad_jev.py",
            root / "src/intelligence/yokohama_jev_live.py",
            root / "src/intelligence/jev_assurance.py",
            root / "src/runtime/task_store.py",
            root / "scripts/smoke_px4_gazebo_sitl_mission_upload.py",
        }
    )
    return {str(p.relative_to(root)): sha256(p) for p in paths}


def approved_map_plan(path):
    """Validate exact approved geometry and runtime before creating a simulator."""
    value = json.loads(Path(path).read_text())
    plan, approval = value["plan"], value["approval"]
    if (
        approval.get("plan_sha256") != digest(plan)
        or approval.get("scene_version") != plan.get("scene_version")
        or not approval.get("approval_ref")
    ):
        raise ValueError("目的地計画の人間承認bindingが一致しません")
    planner = GoalPlanner()
    expected = planner.plan(plan["goal_source_xyz_m"][:2])
    expected["execution_target"] = plan.get("execution_target")
    if expected["execution_target"] not in {
        "px4_gazebo_fixture",
        "cpu_kinematic_fixture",
    } or digest(expected) != digest(plan):
        raise ValueError("目的地計画・経路・scene・runtimeが変化しました")
    return plan


def source_route(plan, legacy):
    """Keep the four-point worker contract; only DELIVERY and its pad may move."""
    import copy

    route = copy.deepcopy(legacy)
    route["waypoints"][3]["xyz_m"] = plan["urban_route_source_xyz_m"][-1]
    # The simulated 4m pad top sits 0.12m above source terrain, as before.
    route["delivery_pad"]["center_xyz_m"] = [
        *plan["goal_source_xyz_m"][:2],
        plan["goal_source_xyz_m"][2] + 0.12,
    ]
    return route
