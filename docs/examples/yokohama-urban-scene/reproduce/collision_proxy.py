import json
import pathlib
import sys
import numpy as np
import mapbox_earcut as earcut
from shapely.geometry import shape
from shapely.geometry.polygon import orient

R = pathlib.Path(sys.argv[1]).resolve() / "preview"
features = json.loads((R / "collision-footprints.geojson").read_text())["features"]
count = 0
faces = 0
with (R / "collision-prisms.obj").open("w") as out:
    out.write(
        "# CC BY 4.0 Yokohama / PLATEAU, modified by MissionOS. Conservative whole-height prisms.\n# X east Y north Z source vertical datum; metres. Preview collision proxy, no flight qualification.\n"
    )
    offset = 1
    for feature in features:
        geo = (
            shape(feature["geometry"])
            .buffer(0.02, join_style="mitre")
            .simplify(0.005, preserve_topology=True)
        )
        assert geo.covers(shape(feature["geometry"]))
        parts = list(geo.geoms) if geo.geom_type == "MultiPolygon" else [geo]
        for part in parts:
            # Zero-distance topology-preserving simplification removes exactly collinear edges.
            part = orient(part.simplify(0, preserve_topology=True), sign=1.0)
            rs = [np.array(x.coords)[:-1] for x in [part.exterior, *part.interiors]]
            xy = np.concatenate(rs)
            n = len(xy)
            idx = earcut.triangulate_float64(
                xy, np.cumsum([len(a) for a in rs], dtype=np.uint32)
            ).reshape(-1, 3)
            zmin = feature["properties"]["zmin"]
            zmax = feature["properties"]["zmax"]
            v = np.r_[
                np.column_stack([xy, np.full(n, zmin)]), np.column_stack([xy, np.full(n, zmax)])
            ]
            ts = []
            for t in idx:
                # Earcut output is CCW. Bottom reverses, top preserves.
                ts.extend([t[::-1].tolist(), (t + n).tolist()])
            start = 0
            for ring in rs:
                for i in range(len(ring)):
                    a = start + i
                    b = start + (i + 1) % len(ring)
                    ts.extend([[a, b, b + n], [a, b + n, a + n]])
                start += len(ring)
            out.write("o " + feature["properties"]["id"] + "_" + str(count) + "\n")
            for q in v:
                out.write("v " + " ".join(f"{x:.6f}" for x in q) + "\n")
            for t in ts:
                out.write("f " + " ".join(str(i + offset) for i in t) + "\n")
            offset += len(v)
            count += 1
            faces += len(ts)
print("Collision proxy components", count, "triangles", faces)
