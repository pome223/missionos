import json
import pathlib
import struct
import sys
import numpy as np
from scipy.spatial import cKDTree

R = pathlib.Path(sys.argv[1]).resolve() / "preview"
s = json.loads((R / "scene.json").read_text())
r = json.loads((R / "route.json").read_text())
tv = np.array(next(o for o in s["objects"] if o["kind"] == "terrain")["vertices"]).reshape(-1, 3)
tree = cKDTree(tv[:, :2])
for o in s["objects"]:
    if o["kind"] == "road":
        v = np.array(o["vertices"]).reshape(-1, 3)
        _, j = tree.query(v[:, :2])
        o["display_z_m"] = np.round(tv[j, 2] + 0.08, 3).tolist()
html = (
    (R / "template.html")
    .read_text()
    .replace("/*__THREE__*/", (R / "vendor/three.min.js").read_text())
    .replace("/*__SCENE__*/", json.dumps(s, separators=(",", ":")))
    .replace("/*__ROUTE__*/", json.dumps(r, ensure_ascii=False, separators=(",", ":")))
)
(R / "index.html").write_text(html)
# Reusable, source-only glTF binary. Y-up; X east; Z south. Roads keep source z=0.
g = {
    "asset": {
        "version": "2.0",
        "generator": "MissionOS PLATEAU scene crop",
        "copyright": "Yokohama City / Project PLATEAU, CC BY 4.0; extracted and converted by MissionOS",
    },
    "scene": 0,
    "scenes": [{"nodes": []}],
    "nodes": [],
    "meshes": [],
    "bufferViews": [],
    "accessors": [],
    "materials": [],
}
bin = bytearray()
colors = {
    "building": [0.62, 0.69, 0.67, 1],
    "bridge": [0.48, 0.55, 0.53, 1],
    "road": [0.88, 0.89, 0.86, 1],
    "terrain": [0.65, 0.72, 0.64, 1],
}
for kind, col in colors.items():
    g["materials"].append(
        {
            "name": kind,
            "doubleSided": True,
            "pbrMetallicRoughness": {
                "baseColorFactor": col,
                "metallicFactor": 0,
                "roughnessFactor": 1,
            },
        }
    )


def append(a, typ, component):
    while len(bin) % 4:
        bin.extend(b"\0")
    start = len(bin)
    bin.extend(a.tobytes())
    view = len(g["bufferViews"])
    g["bufferViews"].append(
        {
            "buffer": 0,
            "byteOffset": start,
            "byteLength": a.nbytes,
            "target": 34962 if typ == "VEC3" else 34963,
        }
    )
    ac = {"bufferView": view, "componentType": component, "count": len(a), "type": typ}
    if typ == "VEC3":
        ac.update(min=a.min(axis=0).tolist(), max=a.max(axis=0).tolist())
    g["accessors"].append(ac)
    return len(g["accessors"]) - 1


for o in s["objects"]:
    v = np.array(o["vertices"], dtype="<f4").reshape(-1, 3)
    v = v[:, [0, 2, 1]].copy()
    v[:, 2] *= -1
    t = np.array(o["triangles"], dtype="<u4")
    pa = append(v, "VEC3", 5126)
    ia = append(t, "SCALAR", 5125)
    mi = len(g["meshes"])
    g["meshes"].append(
        {
            "name": o["id"],
            "primitives": [
                {
                    "attributes": {"POSITION": pa},
                    "indices": ia,
                    "material": list(colors).index(o["kind"]),
                }
            ],
        }
    )
    g["nodes"].append({"mesh": mi, "extras": {"source": o["source"], "lod": o["lod"]}})
    g["scenes"][0]["nodes"].append(mi)
g["buffers"] = [{"byteLength": len(bin)}]
jb = json.dumps(g, separators=(",", ":")).encode()
jb += b" " * ((-len(jb)) % 4)
bin += b"\0" * ((-len(bin)) % 4)
data = (
    struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(jb) + 8 + len(bin))
    + struct.pack("<I4s", len(jb), b"JSON")
    + jb
    + struct.pack("<I4s", len(bin), b"BIN\0")
    + bin
)
(R / "urban-scene.glb").write_bytes(data)
# Exact source triangles (Z-up), not watertightness-certified collision geometry.
with (R / "buildings-bridges.obj").open("w") as f:
    f.write(
        "# Yokohama / PLATEAU CC BY 4.0; MissionOS crop. X east Y north Z source height; metres.\n"
    )
    offset = 1
    for o in s["objects"]:
        if o["kind"] not in ["building", "bridge"]:
            continue
        f.write("o " + o["id"] + "\n")
        v = np.array(o["vertices"]).reshape(-1, 3)
        t = np.array(o["triangles"]).reshape(-1, 3)
        for x in v:
            f.write("v " + " ".join(map(str, x)) + "\n")
        for x in t:
            f.write("f " + " ".join(str(int(n) + offset) for n in x) + "\n")
        offset += len(v)
print("HTML bytes", len(html.encode()), "GLB bytes", len(data))
