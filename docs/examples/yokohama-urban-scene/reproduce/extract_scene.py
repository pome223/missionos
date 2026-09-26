"""Extract source CityGML geometry; no inference, dispatch or flight execution."""

import io
import json
import pathlib
import zlib
import collections
import sys
import numpy as np
from lxml import etree as E
from pyproj import Transformer
import mapbox_earcut as earcut
from shapely.geometry import Polygon, box, mapping
from shapely.ops import unary_union

ROOT = pathlib.Path(sys.argv[1]).resolve()
OUT = ROOT / "preview"
OUT.mkdir(exist_ok=True)
NS = {
    "g": "http://www.opengis.net/gml",
    "b": "http://www.opengis.net/citygml/building/2.0",
    "t": "http://www.opengis.net/citygml/transportation/2.0",
    "bridge": "http://www.opengis.net/citygml/bridge/2.0",
}
LL = [139.6442, 35.4481]
tr = Transformer.from_crs(6668, 6677, always_xy=True)
origin = np.array(tr.transform(*LL))
aoi = box(-300, -300, 300, 300)
summary = collections.Counter()
objects = []
footprints = []


def pos(text):
    a = np.fromstring(text, sep=" ").reshape(-1, 3)
    x, y = tr.transform(a[:, 1], a[:, 0])
    a[:, 0] = x - origin[0]
    a[:, 1] = y - origin[1]
    return a


def rings(poly):
    result = []
    for role in ["exterior", "interior"]:
        for n in poly.findall("g:" + role + "/g:LinearRing/g:posList", NS):
            a = pos(n.text)
            if np.allclose(a[0], a[-1], atol=1e-6, rtol=0):
                a = a[:-1]
            keep = np.r_[True, np.linalg.norm(np.diff(a, axis=0), axis=1) > 1e-7]
            a = a[keep]
            if len(a) >= 3:
                result.append(a)
    return result


def triangles(rs):
    if not rs:
        return np.empty((0, 3)), np.empty((0, 3), dtype=int)
    v = np.concatenate(rs)
    normal = np.sum(np.cross(rs[0], np.roll(rs[0], -1, axis=0)), axis=0)
    if np.linalg.norm(normal) < 1e-8:
        summary["degenerate_polygons"] += 1
        return v, np.empty((0, 3), dtype=int)
    drop = int(np.argmax(np.abs(normal)))
    xy = np.ascontiguousarray(np.delete(v, drop, axis=1))
    idx = earcut.triangulate_float64(xy, np.cumsum([len(r) for r in rs], dtype=np.uint32)).reshape(
        -1, 3
    )
    # Keep ring winding in the original 3D frame.
    for t in idx:
        if np.dot(np.cross(v[t[1]] - v[t[0]], v[t[2]] - v[t[0]]), normal) < 0:
            t[1], t[2] = t[2], t[1]
    summary["holes_triangulated"] += len(rs) - 1
    return v, idx


def extract_file(p, kind):
    raw = zlib.decompress(p.read_bytes(), -15)
    root = E.fromstring(raw, parser=E.XMLParser(resolve_entities=False, no_network=True))
    assert root.find("g:boundedBy/g:Envelope", NS).get("srsName").endswith("/6697")
    tags = {"building": "b:Building", "road": "t:Road", "bridge": "bridge:Bridge"}
    for e in root.findall(".//" + tags[kind], NS):
        allpos = e.findall(".//g:posList", NS)
        if not allpos:
            continue
        # Broad-phase envelope includes every LOD; geometry below selects one LOD.
        first = np.concatenate(
            [np.fromstring(n.text, sep=" ").reshape(-1, 3)[:, :2] for n in allpos]
        )
        latmin, lonmin = first.min(axis=0)
        latmax, lonmax = first.max(axis=0)
        if latmax < 35.4452 or latmin > 35.451 or lonmax < 139.6408 or lonmin > 139.6476:
            continue
        if kind == "building":
            polys = e.findall(".//b:lod2MultiSurface//g:Polygon", NS)
            lod = 2
            if not polys:
                polys = e.findall(".//b:lod1Solid//g:Polygon", NS)
                lod = 1
        elif kind == "bridge":
            polys = e.findall(".//bridge:lod2MultiSurface//g:Polygon", NS)
            lod = 2
            if not polys:
                polys = e.findall(".//bridge:lod1Solid//g:Polygon", NS)
                lod = 1
        else:
            polys = e.findall(".//t:lod1MultiSurface//g:Polygon", NS)
            lod = 1
        vv = []
        tt = []
        offset = 0
        projections = []
        for poly in polys:
            rs = rings(poly)
            v, t = triangles(rs)
            if not len(t):
                continue
            if kind == "road" and not Polygon(rs[0][:, :2]).intersects(aoi):
                continue
            vv.append(v)
            tt.append(t + offset)
            offset += len(v)
            for tri in t:
                xy = Polygon(v[tri, :2])
                if xy.area > 1e-8:
                    projections.append(xy)
        if not vv:
            continue
        v = np.concatenate(vv)
        t = np.concatenate(tt)
        fp = unary_union(projections)
        if not fp.intersects(aoi):
            continue
        ident = e.get("{" + NS["g"] + "}id")
        summary[kind + "_lod" + str(lod)] += 1
        objects.append(
            {
                "id": ident,
                "kind": kind,
                "lod": lod,
                "vertices": np.round(v, 3).ravel().tolist(),
                "triangles": t.ravel().tolist(),
                "source": p.name.removesuffix(".deflate"),
            }
        )
        if kind in ["building", "bridge"]:
            footprints.append(
                {
                    "type": "Feature",
                    "properties": {
                        "id": ident,
                        "kind": kind,
                        "zmin": round(float(v[:, 2].min()), 3),
                        "zmax": round(float(v[:, 2].max()), 3),
                    },
                    "geometry": mapping(fp),
                }
            )
    print(kind, len(objects), flush=True)


for k, kind in [("bldg", "building"), ("tran", "road"), ("brid", "bridge")]:
    for p in sorted((ROOT / "source-cache").glob("*_" + k + "_*.deflate")):
        extract_file(p, kind)

# Stream through the TIN: retain triangles intersecting the selected square.
p = ROOT / "source-cache/533915_dem_6697_00_op.gml.deflate"
raw = zlib.decompress(p.read_bytes(), -15)
terrain = []
for _, e in E.iterparse(
    io.BytesIO(raw), tag="{" + NS["g"] + "}Triangle", resolve_entities=False, no_network=True
):
    q = np.fromstring(e.find(".//g:posList", NS).text, sep=" ").reshape(-1, 3)[:3]
    if (
        q[:, 0].max() >= 35.4452
        and q[:, 0].min() <= 35.451
        and q[:, 1].max() >= 139.6408
        and q[:, 1].min() <= 139.6476
    ):
        x, y = tr.transform(q[:, 1], q[:, 0])
        q[:, 0] = x - origin[0]
        q[:, 1] = y - origin[1]
        if Polygon(q[:, :2]).intersects(aoi):
            terrain.append(q)
    e.clear()
    while e.getprevious() is not None:
        del e.getparent()[0]
v = np.concatenate(terrain)
t = np.arange(len(v)).reshape(-1, 3)
objects.append(
    {
        "id": "terrain",
        "kind": "terrain",
        "lod": 1,
        "vertices": np.round(v, 3).ravel().tolist(),
        "triangles": t.ravel().tolist(),
        "source": p.name.removesuffix(".deflate"),
    }
)
summary["terrain_triangles"] = len(t)
summary["total_triangles"] = sum(len(o["triangles"]) // 3 for o in objects)
scene = {
    "schema": "missionos.urban-scene-preview.v1",
    "origin_lon_lat": LL,
    "horizontal_crs": "EPSG:6677 (JGD2011 Japan Plane Rectangular IX), local east/north metres",
    "origin_projected_xy": origin.tolist(),
    "vertical_datum": "Source EPSG:6697 JGD2011 (vertical) height; unchanged, not ellipsoid or AGL",
    "aoi_m": [-300, -300, 300, 300],
    "crop_policy": "Whole buildings/bridges intersecting AOI; intersecting road polygons and TIN triangles retained without boundary clipping",
    "summary": dict(summary),
    "objects": objects,
}
(OUT / "scene.json").write_text(json.dumps(scene, separators=(",", ":")) + "\n")
(OUT / "collision-footprints.geojson").write_text(
    json.dumps({"type": "FeatureCollection", "features": footprints}, separators=(",", ":")) + "\n"
)
print(json.dumps(scene["summary"], indent=2))
