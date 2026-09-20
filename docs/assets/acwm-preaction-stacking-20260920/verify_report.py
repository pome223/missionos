#!/usr/bin/env python3
"""Verify the public ACWM report bundle without model weights or simulator."""

from pathlib import Path
import json
import re


ROOT = Path(__file__).resolve().parent
REPORT = ROOT.parent.parent / "agents" / "acwm-preaction-stacking-forecast-20260920.md"

readout = json.loads((ROOT / "future-video-readout.json").read_text())
game = json.loads((ROOT / "heldout-sequential-replay-59000.json").read_text())
frozen = json.loads((ROOT / "frozen.json").read_text())
verification = json.loads((ROOT / "verification.json").read_text())
report = REPORT.read_text()

assert frozen["step"] == 720
assert frozen["skipped_updates"] == 0
assert len(frozen["exposures"]) == 36
assert set(frozen["exposures"].values()) == {20}
assert frozen["sha256"] == verification["model_sha256"]
assert readout["wam_generated_seed195"]["accuracy"] == 13 / 16
assert readout["wam_generated_seed195"]["tp"] == 4
assert readout["wam_generated_seed195"]["fn"] == 0
assert readout["wam_generated_seed196"]["accuracy"] == 12 / 16
assert game["score"] == 8
assert [row["decision"] for row in game["decisions"]] == ["place"] * 8 + ["bank"]
assert verification["status"] == "PASS"

for stem in [
    "validation-59000-01",
    "validation-59000-07",
    "validation-59003-08",
    "validation-59006-10",
]:
    assert (ROOT / f"{stem}.mp4").stat().st_size > 50_000
    assert (ROOT / f"{stem}.png").stat().st_size > 50_000

assert not re.search(r"/Users/|file://|\.codex/", report)
assert "13/16" in report and "0.669" in report and "counterfactual replay" in report
print("PASS: ACWM pre-action stacking report bundle verified")
