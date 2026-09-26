import json
import pathlib
import struct
import collections
import sys
import numpy as np
from pyproj import Transformer

R = pathlib.Path(sys.argv[1]).resolve() / "preview"
s = json.loads((R / "scene.json").read_text())
r = json.loads((R / "route.json").read_text())
tris = []
owners = []
for o in s["objects"]:
    v = np.array(o["vertices"]).reshape(-1, 3)
    t = np.array(o["triangles"]).reshape(-1, 3)
    assert np.isfinite(v).all() and t.min() >= 0 and t.max() < len(v)
    if o["kind"] in ["building", "bridge"]:
        tris.append(v[t])
        owners.extend([o["id"]] * len(t))
a = np.concatenate(tris)
owners = np.array(owners)
e1 = a[:, 1] - a[:, 0]
e2 = a[:, 2] - a[:, 0]


def hits(p, q):
    p = np.array(p)
    d = np.array(q) - p
    h = np.cross(d, e2)
    det = np.sum(e1 * h, axis=1)
    valid = np.abs(det) > 1e-9
    inv = np.divide(1.0, det, out=np.zeros_like(det), where=valid)
    sv = p - a[:, 0]
    u = inv * np.sum(sv * h, axis=1)
    qv = np.cross(sv, e1)
    v = inv * np.sum(qv * d, axis=1)
    t = inv * np.sum(qv * e2, axis=1)
    sel = valid & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t > 1e-5) & (t < 1 - 1e-5)
    return sorted(set(owners[sel]))


los = []
for before, after, target in [(0, 1, 2), (1, 2, 3)]:
    p = [x["xyz_m"] for x in r["waypoints"]]
    bh = hits(p[before], p[target])
    ah = hits(p[after], p[target])
    assert bh and not ah
    los.append(
        {
            "from_before": r["waypoints"][before]["id"],
            "from_after": r["waypoints"][after]["id"],
            "target": r["waypoints"][target]["id"],
            "before_triangle_hit_buildings": bh,
            "after_triangle_hit_buildings": ah,
        }
    )
for wp_a, wp_b in zip(r["waypoints"], r["waypoints"][1:]):
    assert not hits(wp_a["xyz_m"], wp_b["xyz_m"])
for x in r["short_legs"]:
    assert np.linalg.norm(np.array(x["to"]) - x["from"]) <= 20.001
tr = Transformer.from_crs(6677, 6668, always_xy=True)
ll = tr.transform(*s["origin_projected_xy"])
assert np.max(np.abs(np.array(ll) - s["origin_lon_lat"])) < 1e-9
b = (R / "urban-scene.glb").read_bytes()
magic, version, length = struct.unpack_from("<4sII", b)
assert (magic, version, length) == (b"glTF", 2, len(b))
n, typ = struct.unpack_from("<I4s", b, 12)
assert typ == b"JSON"
g = json.loads(b[20 : 20 + n])
blen, btype = struct.unpack_from("<I4s", b, 20 + n)
assert btype == b"BIN\0"
binary = b[28 + n :]
assert len(binary) == blen
for ac in g["accessors"]:
    view = g["bufferViews"][ac["bufferView"]]
    count = ac["count"] * (3 if ac["type"] == "VEC3" else 1)
    arr = np.frombuffer(
        binary[view.get("byteOffset", 0) : view.get("byteOffset", 0) + view["byteLength"]],
        dtype="<f4" if ac["componentType"] == 5126 else "<u4",
    )
    assert len(arr) == count and np.isfinite(arr).all()
edges = collections.Counter()
directed = collections.Counter()
for line in (R / "collision-prisms.obj").read_text().splitlines():
    if line.startswith("f "):
        face = list(map(int, line.split()[1:]))
        pairs = [(face[i], face[(i + 1) % 3]) for i in range(3)]
        edges.update(tuple(sorted(x)) for x in pairs)
        directed.update(pairs)
assert set(edges.values()) == {2} and set(directed.values()) == {1}
summary = {
    "schema": "missionos.urban-scene-geometry-check.v1",
    "status": "passed",
    "mesh_objects": len(s["objects"]),
    "source_triangle_count": s["summary"]["total_triangles"],
    "line_of_sight_method": "two-sided segment / original 3D triangle intersection, Moller-Trumbore",
    "line_of_sight_checks": los,
    "route_centerline_triangle_intersections": 0,
    "short_legs_count": len(r["short_legs"]),
    "max_short_leg_m": max(
        round(float(np.linalg.norm(np.array(x["to"]) - x["from"])), 4) for x in r["short_legs"]
    ),
    "coordinate_origin_roundtrip_max_deg": float(
        np.max(np.abs(np.array(ll) - s["origin_lon_lat"]))
    ),
    "glb_structure_checked": True,
    "collision_proxy_edge_incidence_counts": dict(collections.Counter(edges.values())),
    "collision_proxy_consistent_winding": True,
    "collision_proxy_horizontal_expansion_m": 0.02,
    "collision_proxy_topology_simplification_m": 0.005,
    "native_flight": False,
    "gpu_spend_usd": 0,
    "limitations": [
        "No simulator flight or renderer-based perception test",
        "No wind, trees, wires, traffic, people or source-data accuracy margin",
        "Source-triangle meshes not certified watertight; conservative footprint prisms are the separate collision proxy",
        "Altitude is source vertical datum, not PX4 home-relative altitude",
    ],
}
(R / "geometry-checks.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
