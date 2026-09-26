import json
import math
import pathlib
import sys
import numpy as np
from shapely.geometry import shape, LineString, Point
from shapely.ops import unary_union

R = pathlib.Path(sys.argv[1]).resolve() / "preview"
f = json.loads((R / "collision-footprints.geojson").read_text())["features"]
scene = json.loads((R / "scene.json").read_text())
# Authored scene route chosen on the source-derived street layout. No dispatch permit.
xy = [
    [55.0, -50.0],
    [20.0, -110.62177826491069],
    [162.8941916244323, -193.12177826491074],
    [207.89419162443232, -115.1794919243113],
]
z = 18.0
ids = ["D1", "D2", "D3", "DELIVERY"]
labels = ["市街地入口", "角1：再観測", "角2：配送前判断", "仮想配送スペース"]
waypoints = [
    {
        "id": id,
        "label": label,
        "xyz_m": [round(p[0], 3), round(p[1], 3), z],
        "mandatory_observation": id != "DELIVERY",
    }
    for id, label, p in zip(ids, labels, xy)
]
# Conservative prisms: all source building/bridge projections through their full height.
obstacles = [x for x in f if x["properties"]["zmin"] <= z + 1 and x["properties"]["zmax"] >= z - 1]
obs = unary_union([shape(x["geometry"]) for x in obstacles])
path = LineString(xy)
clearance = path.distance(obs)
probes = []
for before, after, target in [(0, 1, 2), (1, 2, 3)]:
    hits = [
        x["properties"]["id"]
        for x in obstacles
        if LineString([xy[before], xy[target]]).intersects(shape(x["geometry"]))
    ]
    visible = not LineString([xy[after], xy[target]]).intersects(obs)
    probes.append(
        {
            "observer_before": ids[before],
            "observer_after": ids[after],
            "target": ids[target],
            "before_blocker_ids": hits,
            "after_line_clear": visible,
            "method": "line segment against conservative 2.5D footprint prisms at z=18 m; not RGB or WAM perception",
        }
    )
chunks = []
for a, b in zip(waypoints, waypoints[1:]):
    p = np.array(a["xyz_m"])
    q = np.array(b["xyz_m"])
    n = math.ceil(np.linalg.norm(q - p) / 20)
    for i in range(n):
        chunks.append(
            {
                "from": np.round(p + (q - p) * i / n, 3).tolist(),
                "to": np.round(p + (q - p) * (i + 1) / n, 3).tolist(),
            }
        )
terrain = next(o for o in scene["objects"] if o["kind"] == "terrain")
tv = np.array(terrain["vertices"]).reshape(-1, 3)
tt = np.array(terrain["triangles"]).reshape(-1, 3)


def elevation(p):
    for inds in tt:
        a, b, c = tv[inds]
        M = np.column_stack([b[:2] - a[:2], c[:2] - a[:2]])
        if abs(np.linalg.det(M)) < 1e-8:
            continue
        w = np.linalg.solve(M, np.array(p) - a[:2])
        if min(w) >= -1e-8 and w.sum() <= 1 + 1e-8:
            return float(a[2] + w[0] * (b[2] - a[2]) + w[1] * (c[2] - a[2]))
    raise ValueError("No terrain under route point")


samples = [path.interpolate(i * path.length / 100) for i in range(101)]
ground = [elevation((p.x, p.y)) for p in samples]
pad_ground = elevation(xy[-1])
pad_clearance = Point(xy[-1]).distance(unary_union([shape(x["geometry"]) for x in f]))
route = {
    "schema": "missionos.urban-route-design.v1",
    "status": "design_only_not_dispatched",
    "waypoints": waypoints,
    "return_policy": "reverse proposed urban route; AP handoff after verified urban model shutdown",
    "max_proposed_leg_m": 20,
    "short_legs": chunks,
    "delivery_pad": {
        "center_xyz_m": [round(v, 3) for v in xy[-1]] + [round(pad_ground + 0.15, 3)],
        "radius_m": 3,
        "synthetic": True,
        "landing_tested": False,
    },
    "sea_connection": {
        "status": "interface_only_no_offshore_trajectory_verified",
        "autopilot_only": True,
        "models_off_until_verified_urban_hold": True,
    },
    "checks": {
        "urban_route_length_m": round(path.length, 3),
        "geometry_min_horizontal_clearance_m": round(clearance, 3),
        "assumed_vehicle_radius_m": 1,
        "residual_horizontal_clearance_m": round(clearance - 1, 3),
        "altitude_source_datum_m": 18,
        "sampled_ground_clearance_min_m": round(18 - max(ground), 3),
        "ground_samples": 101,
        "pad_center_all_buildings_clearance_m": round(pad_clearance, 3),
        "turn_angles_deg": [90, 90],
        "visibility_probes": probes,
        "rules_or_flight_certification": False,
    },
}
assert clearance >= 4 and all(p["before_blocker_ids"] and p["after_line_clear"] for p in probes)
assert pad_clearance > 4 and min(18 - x for x in ground) > 5
(R / "route.json").write_text(json.dumps(route, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(route["checks"], ensure_ascii=False, indent=2))
