"""Published replay checks reject altered measurements and misleading time order."""

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from scripts.check_urban_reobserve_report import check

REPORT = Path(__file__).resolve().parents[2] / "docs/assets/px4-stop-reobserve-20260925/pair02"


def test_report_rejects_modified_measurements(tmp_path):
    report = tmp_path / "report"
    shutil.copytree(REPORT, report)
    assert check(report)["cases"] == 2
    path = report / "summary.json"
    data = json.loads(path.read_text())
    data["runs"][0]["destination_reached"] = True
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="artifact differs"):
        check(report)


def test_matching_hashes_cannot_hide_duplicate_replay_time(tmp_path):
    report = tmp_path / "report"
    shutil.copytree(REPORT, report)
    path = report / "replay-data.json"
    data = json.loads(path.read_text())
    data["cases"][0]["trace"][1]["t"] = data["cases"][0]["trace"][0]["t"]
    path.write_text(json.dumps(data))
    html = report / "index.html"
    before, rest = html.read_text().split("const D=", 1)
    _, after = rest.split(", slider=", 1)
    html.write_text(before + "const D=" + json.dumps(data) + ", slider=" + after)
    manifest = json.loads((report / "manifest.json").read_text())
    for changed in (path, html):
        manifest["artifact_sha256"][changed.name] = hashlib.sha256(changed.read_bytes()).hexdigest()
    (report / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="duplicate display sample"):
        check(report)
