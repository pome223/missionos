#!/usr/bin/env python3
"""Recompute the public ACWM report claims from individual records."""

from pathlib import Path
import hashlib
import json
import re
import struct

ROOT = Path(__file__).resolve().parent
REPORT = ROOT.parent.parent / "agents" / "acwm-preaction-stacking-forecast-20260920.md"
readout = json.loads((ROOT / "future-video-readout.json").read_text())
game = json.loads((ROOT / "heldout-sequential-replay-59000.json").read_text())
frozen = json.loads((ROOT / "frozen.json").read_text())
verification = json.loads((ROOT / "verification.json").read_text())
before = json.loads((ROOT / "forecast-before-seed195.json").read_text())
after195 = json.loads((ROOT / "forecast-after-seed195.json").read_text())
after196 = json.loads((ROOT / "forecast-after-seed196.json").read_text())
media_hashes = json.loads((ROOT / "media-sha256.json").read_text())
forty = json.loads((ROOT / "v7-40-game-acwm-comparison.json").read_text())
report = REPORT.read_text()


def confusion(section, expected_count):
    rows = section["rows"]
    assert len(rows) == expected_count
    assert len({row["name"] for row in rows}) == expected_count
    for row in rows:
        assert row["predicted_collapse"] == (row["probability"] >= readout["threshold"])
    tp = sum(row["predicted_collapse"] and row["collapsed"] for row in rows)
    fp = sum(row["predicted_collapse"] and not row["collapsed"] for row in rows)
    tn = sum(not row["predicted_collapse"] and not row["collapsed"] for row in rows)
    fn = sum(not row["predicted_collapse"] and row["collapsed"] for row in rows)
    assert (tp, fp, tn, fn) == (section["tp"], section["fp"], section["tn"], section["fn"])
    assert section["accuracy"] == (tp + tn) / expected_count
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn}


def averages(rows):
    return {
        "iou": sum(row["block_mask_iou"] for row in rows) / len(rows),
        "late3": sum(row["late3_iou"] for row in rows) / len(rows),
        "centroid": sum(row["block_centroid_error_pixels"] for row in rows) / len(rows),
    }


assert frozen["step"] == 720 and frozen["skipped_updates"] == 0
assert len(frozen["exposures"]) == 36 and set(frozen["exposures"].values()) == {20}
assert frozen["sha256"] == verification["model_sha256"]
assert readout["threshold"] == 0.5
assert confusion(readout["wam_generated_train_seed195"], 36) == {"tp": 11, "fp": 1, "tn": 23, "fn": 1}
assert confusion(readout["wam_generated_seed195"], 16) == {"tp": 4, "fp": 3, "tn": 9, "fn": 0}
assert confusion(readout["wam_generated_seed196"], 16) == {"tp": 4, "fp": 4, "tn": 8, "fn": 0}

primary = {row["name"]: row for row in readout["wam_generated_seed195"]["rows"]}
assert sum(name.startswith("validation-59000-") for name in primary) == 10
assert len({name.split("-")[1] for name in primary}) == 6
for count, expected in {8: (2, 0, 2, 0), 9: (0, 1, 0, 0), 10: (2, 2, 0, 0)}.items():
    rows = [row for row in primary.values() if int(row["name"].rsplit("-", 1)[1]) == count]
    actual = (
        sum(row["predicted_collapse"] and row["collapsed"] for row in rows),
        sum(row["predicted_collapse"] and not row["collapsed"] for row in rows),
        sum(not row["predicted_collapse"] and not row["collapsed"] for row in rows),
        sum(not row["predicted_collapse"] and row["collapsed"] for row in rows),
    )
    assert actual == expected

assert len(before) == len(after195) == len(after196) == 16
assert {row["name"] for row in before} == {row["name"] for row in after195} == {row["name"] for row in after196}
before_mean, after195_mean, after196_mean = averages(before), averages(after195), averages(after196)
for actual, expected in [
    (before_mean["iou"], 0.4389954651978979),
    (before_mean["late3"], 0.4935931194369911),
    (before_mean["centroid"], 19.085214084042793),
    (after195_mean["iou"], 0.6685057619155949),
    (after195_mean["late3"], 0.5181834209659646),
    (after195_mean["centroid"], 9.431788117528965),
    (after196_mean["iou"], 0.6532884403403874),
    (after196_mean["late3"], 0.5794366018799789),
    (after196_mean["centroid"], 11.324893844782329),
]:
    assert abs(actual - expected) < 1e-12

assert game["score"] == 8 and len(game["decisions"]) == 9
for decision in game["decisions"]:
    source = primary[decision["name"]]
    assert decision["collapsed"] == source["collapsed"]
    assert decision["predicted_collapse"] == source["predicted_collapse"]
    assert decision["probability"] == source["probability"]
    assert decision["decision"] == ("bank" if source["predicted_collapse"] else "place")
assert game["bank_outcome"] == {
    "source_case": "physics-wam-v6/validation/59000/step-09",
    "horizon_steps": 284,
    "control_frequency_hz": 20,
    "observation_seconds": 14.2,
    "count_after": 8,
    "collapsed": False,
    "technical_failure": None,
    "max_drop_m": 0.011251332137516723,
    "score": 8,
}
assert verification["status"] == "PASS"

assert len(forty["games"]) == len(forty["rows"]) == 40
assert {game["seed"] for game in forty["games"]} == set(range(67000, 67040))
assert {row["seed"] for row in forty["rows"]} == set(range(67000, 67040))
game_lookup = {game["seed"]: game for game in forty["games"]}
row_lookup = {row["seed"]: row for row in forty["rows"]}
reached = []
for seed, current in game_lookup.items():
    decisions = current["decisions"]
    assert decisions and all(row["seed"] == seed for row in decisions)
    assert [row["count"] for row in decisions] == list(range(1, len(decisions) + 1))
    last = decisions[-1]
    expected_score = last["bank_score"] if last["decision"] == "bank" else (0 if last["continue_collapsed"] else last["continue_score"])
    assert current["score"] == expected_score
    assert current["stop_count"] == len(decisions) - int(last["decision"] == "bank")
    assert row_lookup[seed]["scores"]["acwm"] == current["score"]
    reached.extend(decisions)

truth = [row["continue_collapsed"] for row in reached]
prediction = [row["predicted_collapse"] for row in reached]
reached_confusion = {
    "n": len(reached),
    "tp": sum(pred and actual for pred, actual in zip(prediction, truth)),
    "fp": sum(pred and not actual for pred, actual in zip(prediction, truth)),
    "tn": sum(not pred and not actual for pred, actual in zip(prediction, truth)),
    "fn": sum(not pred and actual for pred, actual in zip(prediction, truth)),
}
assert reached_confusion == forty["event_confusion_reached"] == {"n": 327, "tp": 9, "fp": 23, "tn": 290, "fn": 5}
for method, expected in {"acwm": 243, "wam": 279, "width_rule": 280, "old_wam": 225, "rule": 198, "vla": 30, "fixed6": 240}.items():
    assert sum(row["scores"][method] for row in forty["rows"]) == forty["totals"][method] == expected
for method, expected in {"wam": (7, 15, 18), "width_rule": (10, 16, 14), "old_wam": (10, 13, 17), "rule": (14, 15, 11), "vla": (30, 9, 1)}.items():
    differences = [row["scores"]["acwm"] - row["scores"][method] for row in forty["rows"]]
    comparison = forty["comparisons"][method]
    assert (sum(value > 0 for value in differences), sum(value == 0 for value in differences), sum(value < 0 for value in differences)) == expected
    assert (comparison["wins"], comparison["ties"], comparison["losses"]) == expected
    assert comparison["total_difference"] == sum(differences)
    assert comparison["mean_difference"] == sum(differences) / 40

published_media = {path.name for path in ROOT.glob("*.mp4")} | {path.name for path in ROOT.glob("*.png")}
assert set(media_hashes) == published_media
for name, expected_sha in media_hashes.items():
    path = ROOT / name
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == expected_sha
    if path.suffix == ".png":
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", data[16:24]) == (1680, 528)
    else:
        assert data[4:8] == b"ftyp" and b"moov" in data and b"mdat" in data

assert not re.search(r"/Users/|file://|\.codex/", report)
assert "13/16" in report and "0.669" in report and "counterfactual replay" in report and "243" in report
print("PASS: ACWM per-case metrics, decisions, bank outcome, and media hashes verified")
