#!/usr/bin/env python3
"""Rebuild the frozen, source-derived Yokohama scene without a simulator or GPU."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import urllib.request
import zlib

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument(
        "--allow-download",
        action="store_true",
        help="Opt in to at most 50 MB of pinned source downloads",
    )
    parser.add_argument(
        "--replace-preview",
        action="store_true",
        help="Rebuild an existing generated preview directory",
    )
    args = parser.parse_args()
    root = args.work_dir.resolve()
    output = root / "preview"
    if output.exists() and not args.replace_preview:
        parser.error("preview already exists; use a new work directory or --replace-preview")
    cache = root / "source-cache"
    cache.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((HERE / "sources.json").read_text())
    if sum(m["compressed"] for m in manifest["members"]) > 50_000_000:
        raise ValueError("Source byte budget exceeded")

    def get_range(start, end):
        req = urllib.request.Request(
            manifest["url"], headers={"Range": f"bytes={start}-{end}", "If-Match": manifest["etag"]}
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            expected = f"bytes {start}-{end}/{manifest['archive_bytes']}"
            if response.status != 206 or response.headers.get("Content-Range") != expected:
                raise ValueError("Server did not honor the bounded range request")
            if response.headers.get("ETag") != manifest["etag"]:
                raise ValueError("Source archive changed")
            data = response.read(end - start + 2)
        if len(data) != end - start + 1:
            raise ValueError("Range response has the wrong length")
        return data

    for m in manifest["members"]:
        path = cache / (Path(m["name"]).name + ".deflate")
        if path.exists():
            if path.stat().st_size != m["compressed"]:
                raise ValueError(f"Cache length mismatch: {path.name}")
            data = path.read_bytes()
        else:
            if not args.allow_download:
                parser.error(
                    "Source cache missing; use --allow-download to fetch the pinned public inputs"
                )
            header = get_range(m["offset"], m["offset"] + 29)
            if header[:4] != b"PK\x03\x04":
                raise ValueError("Invalid ZIP local header")
            filename_length, extra_length = struct.unpack_from("<HH", header, 26)
            start = m["offset"] + 30 + filename_length + extra_length
            data = get_range(start, start + m["compressed"] - 1)
        if hashlib.sha256(data).hexdigest() != m["compressed_sha256"]:
            raise ValueError(f"Compressed SHA256 mismatch: {path.name}")
        if m["method"] != 8:
            raise ValueError("Only the frozen DEFLATE source members are supported")
        raw = zlib.decompress(data, -15)
        if len(raw) != m["size"] or zlib.crc32(raw) != m["crc"]:
            raise ValueError(f"Uncompressed ZIP receipt mismatch: {path.name}")
        if hashlib.sha256(raw).hexdigest() != m["uncompressed_sha256"]:
            raise ValueError(f"Uncompressed SHA256 mismatch: {path.name}")
        if not path.exists():
            path.write_bytes(data)
        del data, raw
        print("Verified", m["name"], flush=True)

    output.mkdir(exist_ok=True)
    shutil.copy2(HERE / "template.html", output / "template.html")
    shutil.copytree(HERE.parent / "vendor", output / "vendor", dirs_exist_ok=True)
    for name in [
        "extract_scene.py",
        "make_route.py",
        "collision_proxy.py",
        "package_preview.py",
        "verify_geometry.py",
    ]:
        subprocess.run([sys.executable, str(HERE / name), str(root)], check=True)
    for name in [
        "REPORT-ja.md",
        "ATTRIBUTION.md",
        "source-manifest.json",
        "overview.png",
        "view-D2.png",
        "top-view.png",
    ]:
        if (HERE.parent / name).exists():
            shutil.copy2(HERE.parent / name, output / name)
    # Retain the published validator receipt only for the identical generated GLB.
    expected = json.loads((HERE.parent / "files.sha256.json").read_text())
    actual = hashlib.sha256((output / "urban-scene.glb").read_bytes()).hexdigest()
    if actual == expected["urban-scene.glb"]:
        shutil.copy2(HERE.parent / "gltf-validation.json", output / "gltf-validation.json")
    else:
        raise ValueError("Generated GLB differs from the frozen validated asset")
    print("Scene preview:", output / "index.html")


if __name__ == "__main__":
    main()
