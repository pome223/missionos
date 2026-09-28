#!/usr/bin/env python3
"""Verify the portable pad-advisory bundle and recompute RGB/mission receipts."""

from __future__ import annotations
import argparse
import gzip
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.verify_yokohama_pad_advisory import verify  # noqa: E402
from scripts.verify_yokohama_payload import verify as verify_payload  # noqa: E402
from scripts.yokohama_pad_state import sha  # noqa: E402


def check(bundle):
    manifest = json.loads((bundle / "evidence-manifest.json").read_text())
    listed = set()
    for item in manifest["files"]:
        path = bundle / item["path"]
        if (
            not path.resolve().is_relative_to(bundle.resolve())
            or path.is_symlink()
            or item["path"] in listed
            or path.stat().st_size != item["bytes"]
            or sha(path) != item["sha256"]
        ):
            raise ValueError("Public evidence changed")
        listed.add(item["path"])
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()}
    if actual - {"evidence-manifest.json"} != listed:
        raise ValueError("Unlisted or missing evidence")
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "run"
        shutil.copytree(bundle / "run", root)
        for path in root.glob("*.jsonl.gz"):
            output = path.with_suffix("")
            output.write_bytes(gzip.decompress(path.read_bytes()))
        for name, expected in manifest["raw_source_sha256"].items():
            if Path(name).name != name or sha(root / name) != expected:
                raise ValueError("Decompressed source identity changed")
        result = json.loads((root / "result.json").read_text())
        for name, expected in result["source_sha256"].items():
            if Path(name).name != name or sha(root / "sources" / name) != expected:
                raise ValueError("Flight-time source snapshot changed")
        advisory = verify(root)
        payload = verify_payload(root)
    expected = json.loads((bundle / "advisory-verification.json").read_text())
    if advisory != expected or payload != json.loads(
        (bundle / "payload-verification.json").read_text()
    ):
        raise ValueError("Recomputed evidence differs")
    if advisory["status"] != "passed" or payload["status"] != "passed":
        raise ValueError("Mission verification failed")
    return dict(
        status="passed",
        files_verified=len(listed),
        learned_forecasts_recomputed=advisory["inference_calls"],
        supported=advisory["supported"],
        action_changes=advisory["action_changes_against_same_observations"],
        scope="Recomputed RGB forecasts, queue and payload receipts; original geometry/CPU-isolation verifier result is preserved separately.",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    print(json.dumps(check(parser.parse_args().bundle)))
