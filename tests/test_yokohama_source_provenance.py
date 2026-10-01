"""The immutable public snapshot stays mapped to retained flight evidence."""
import hashlib
import json
from pathlib import Path
import subprocess


PUBLIC_SNAPSHOT = "5431fe1a2508ab2c79662e1f5a6cc8d6afbef65f"


def snapshot_blob(root: Path, path: str) -> bytes:
    # Missing history is a hard failure; never substitute the current worktree.
    return subprocess.run(
        ["git", "cat-file", "blob", f"{PUBLIC_SNAPSHOT}:{path}"],
        cwd=root, capture_output=True, check=True, timeout=5,
    ).stdout


def test_public_snapshot_matches_historical_correspondence_receipt():
    root = Path(__file__).resolve().parents[1]
    receipt_path = "docs/examples/yokohama-map-delivery/source-provenance.json"
    retained = (root / receipt_path).read_bytes()
    assert retained == snapshot_blob(root, receipt_path)
    receipt = json.loads(retained)
    assert receipt["approved_runtime_files"] == 55
    assert len(receipt["files"]) == 56
    changes = set()
    for row in receipt["files"]:
        assert hashlib.sha256(snapshot_blob(root, row["path"])).hexdigest() == row["public_sha256"]
        if row["role"] == "unchanged_runtime":
            assert row["public_sha256"] == row["recorded_sha256"]
        else:
            changes.add(row["path"])
    assert changes == {"src/intelligence/yokohama_jev_live.py", "src/intelligence/yokohama_pad_jev.py",
                       "scripts/run_yokohama_jev_live_gateway.py"}
    uploader = next(r for r in receipt["files"] if r["path"] == "scripts/smoke_px4_gazebo_sitl_mission_upload.py")
    assert uploader["approved_manifest_bound"] and uploader["role"] == "unchanged_runtime"
