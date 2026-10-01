"""Public control files remain explicitly mapped to retained flight evidence."""
import hashlib
import json
from pathlib import Path


def test_public_runtime_matches_correspondence_receipt():
    root = Path(__file__).resolve().parents[1]
    receipt = json.loads((root / "docs/examples/yokohama-map-delivery/source-provenance.json").read_text())
    assert receipt["approved_runtime_files"] == 55
    assert len(receipt["files"]) == 56
    changes = set()
    for row in receipt["files"]:
        assert hashlib.sha256((root / row["path"]).read_bytes()).hexdigest() == row["public_sha256"]
        if row["role"] == "unchanged_runtime":
            assert row["public_sha256"] == row["recorded_sha256"]
        else:
            changes.add(row["path"])
    assert changes == {"src/intelligence/yokohama_jev_live.py", "src/intelligence/yokohama_pad_jev.py",
                       "scripts/run_yokohama_jev_live_gateway.py"}
    uploader = next(r for r in receipt["files"] if r["path"] == "scripts/smoke_px4_gazebo_sitl_mission_upload.py")
    assert uploader["approved_manifest_bound"] and uploader["role"] == "unchanged_runtime"
