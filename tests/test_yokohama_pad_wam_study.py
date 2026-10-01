"""The lead-forecast study must read real motion, keep targets local and gate honestly."""

import json

import numpy as np
from PIL import Image
import pytest

from scripts.evaluate_yokohama_pad_wam_study import evaluate
from scripts.prepare_yokohama_pad_wam_study import phase
from scripts.stage_yokohama_pad_wam_study import select
from scripts.train_yokohama_pad_wam_study import validate
from scripts.yokohama_pad_wam_study import CROPS, detect_lead, sha, to_original, write

ORANGE = (240, 128, 20)


def frame(x, y=100, w=30, h=12, stray=None):
    im = np.full((224, 224, 3), 150, np.uint8)
    im[y : y + h, x : x + w] = ORANGE
    if stray:
        im[stray[1], stray[0]] = ORANGE
    return im


def test_readout_finds_the_body_in_original_pixels_and_ignores_sparse_texture():
    found = detect_lead(frame(60, stray=(200, 10)), "tight")
    assert found["present"] and found["stray_pixels"] == 1
    assert np.allclose(found["center_px"], to_original([74.5, 105.5], "tight"), atol=0.01)
    assert detect_lead(np.full((224, 224, 3), 150, np.uint8), "tight")["present"] is False


def test_phase_labels_follow_the_scripted_knots():
    k = [[0, "start"], [4, "start"], [8, "up7"], [20, "end7_0"], [26, "end7_0"]]
    assert [phase(k, t) for t in (2, 6, 10, 23)] == ["on_pad", "ascending", "departing", "away"]
    r = [[0, "end5_45"], [3, "end5_45"], [11, "up5"], [13, "up5"], [22, "start"], [28, "start"]]
    assert [phase(r, t) for t in (1, 5, 12, 15, 25)] == [
        "away",
        "returning",
        "hover",
        "landing",
        "on_pad",
    ]


def truth_row(split, seq, last, offset, now, future, phase_now="departing"):
    def readout(x):
        return dict(present=x is not None, **({"center_px": [x, 150.0]} if x is not None else {}))

    return dict(
        id=f"wam-{split}-{seq:02d}-depart-{last:03d}-t{offset:02d}-tight",
        split=split,
        phase_now=phase_now,
        readout_now=readout(now),
        readout_future=readout(future),
    )


def test_selection_uses_only_train_motion_and_val_holdout():
    truth = []
    for seq in range(6):
        for last in range(20, 60, 4):
            truth.append(truth_row("train", seq, last, 12, 300.0, 330.0))
            truth.append(truth_row("train", seq, last, 4, 300.0, 310.0))
            truth.append(truth_row("train", seq, last + 1, 12, 300.0, 300.5, "on_pad"))
        truth.append(truth_row("val", seq, 30, 12, 300.0, 320.0))
        truth.append(truth_row("test", seq, 30, 12, 300.0, 320.0))
    moving, train_ids, eval_ids = select(truth)
    assert len(moving) == 16 and len(train_ids) == 24 and len(eval_ids) == 6
    assert all("-train-" in i for i in train_ids) and all("-val-" in i for i in eval_ids)


def study(tmp_path, crop="tight"):
    payload = tmp_path / "study/payload"
    for d in ("frames", "train-targets", "histories"):
        (payload / d).mkdir(parents=True)
    rgb = np.stack([frame(20 + 4 * i) for i in range(40)])
    stamps = np.arange(40) * 250_000_000
    np.savez_compressed(payload / "frames/seq.npz", rgb=rgb, stamps_ns=stamps)
    # Held-out input: only the 16 frames up to its cutoff (sequence index 24).
    np.savez_compressed(payload / "histories/val.npz", rgb=rgb[9:25], stamps_ns=stamps[9:25])
    samples = [
        dict(
            id=f"s-train-{crop}",
            split="train",
            crop=crop,
            frames="frames/seq.npz",
            last_index=20,
            offset=12,
            target="train-targets/a.png",
        ),
        dict(
            id=f"s-val-{crop}",
            split="val",
            crop=crop,
            frames="histories/val.npz",
            last_index=15,
            offset=12,
            cutoff_stamp_ns=int(stamps[24]),
        ),
    ]
    Image.fromarray(rgb[32]).save(payload / "train-targets/a.png")
    assets = {
        rel: sha(payload / rel)
        for rel in ("frames/seq.npz", "histories/val.npz", "train-targets/a.png")
    }
    data = dict(
        crops=CROPS, history=16, evaluation_targets_uploaded=False, samples=samples, assets=assets
    )
    write(payload / "dataset.json", data)
    protocol = dict(
        dataset_sha256=sha(payload / "dataset.json"),
        source_sha256={},
        configs=[dict(name="c", crop=crop)],
        train_ids=["s-train"],
        moving_ids=["s-train"],
        eval_ids=["s-val"],
        memorization_gate=dict(
            offset=12, presence_fraction=0.8, median_error_px=6.0, fraction_of_persistence=0.5
        ),
    )
    write(tmp_path / "study/study-protocol.json", protocol)
    return tmp_path / "study", rgb


def rewrite(root, change):
    data = json.loads((root / "payload/dataset.json").read_text())
    change(data)
    write(root / "payload/dataset.json", data)
    protocol = json.loads((root / "study-protocol.json").read_text())
    protocol["dataset_sha256"] = sha(root / "payload/dataset.json")
    write(root / "study-protocol.json", protocol)


def test_gpu_payload_rejects_test_samples_futures_and_changed_assets(tmp_path):
    root, _ = study(tmp_path)
    assert validate(root)[0]["train_ids"] == ["s-train"]
    data = json.loads((root / "payload/dataset.json").read_text())
    data["samples"][1]["split"] = "test"
    write(root / "payload/dataset.json", data)
    with pytest.raises(ValueError, match="Dataset changed"):
        validate(root)
    rewrite(root, lambda d: None)
    with pytest.raises(ValueError, match="reserved"):
        validate(root)
    whole, _ = study(tmp_path / "whole")
    rewrite(whole, lambda d: d["samples"][1].update(frames="frames/seq.npz", last_index=24))
    with pytest.raises(ValueError, match="cutoff-bounded"):
        validate(whole)
    late, _ = study(tmp_path / "late")
    rewrite(late, lambda d: d["samples"][1].update(cutoff_stamp_ns=0))
    with pytest.raises(ValueError, match="past its cutoff"):
        validate(late)
    changed, _ = study(tmp_path / "changed")
    (changed / "payload/train-targets/a.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="Asset changed"):
        validate(changed)


def scored(tmp_path, forecasts, calls=None):
    root, rgb = study(tmp_path)
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    data = json.loads((root / "payload/dataset.json").read_text())
    truth = []
    for s in data["samples"]:
        last = 20 if s["split"] == "train" else 24
        truth.append(
            dict(
                id=s["id"],
                phase_now="departing",
                readout_now=detect_lead(rgb[last], "tight"),
                readout_future=detect_lead(rgb[last + s["offset"]], "tight"),
            )
        )
    write(prepared / "truth.json", truth)
    frames = {s["id"]: data["assets"][s["frames"]] for s in data["samples"]}
    results = tmp_path / "results"
    records = []
    for sid, index in forecasts:
        dest = results / "c/after" / sid
        dest.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgb[index]).save(dest / "prediction.png")
        receipt = dict(
            sample_id=sid,
            config="c",
            stage="after",
            frames_sha256=frames[sid],
            prediction_sha256=sha(dest / "prediction.png"),
        )
        write(dest / "result.json", receipt)
        records.append(dict(file=f"c/after/{sid}/result.json", sha256=sha(dest / "result.json")))
    write(results / "forecast-manifest.json", dict(records=records))
    write(
        results / "summary.json",
        dict(status="completed", inference_calls=len(records) if calls is None else calls),
    )
    return evaluate(root, prepared, results)


def test_host_scoring_separates_forecast_persistence_and_gate(tmp_path):
    # A perfect training forecast, and a held-out "forecast" that copies the present.
    value = scored(tmp_path, [("s-train-tight", 32), ("s-val-tight", 24)])
    assert value["complete"] is True
    moving = value["table"]["c/after/moving"]
    assert moving["model"]["median_error_px"] == 0 and moving["persistence"]["median_error_px"] > 20
    assert value["memorization_passed"] == ["c"]
    assert value["table"]["c/after/val"]["model"]["median_error_px"] > 20


def test_missing_or_duplicated_forecasts_cannot_pass_the_gate(tmp_path):
    value = scored(tmp_path / "missing", [("s-train-tight", 32)])
    assert value["completeness"]["missing"] == [["c", "after", "s-val-tight"]]
    assert value["complete"] is False and value["memorization_passed"] == []
    value = scored(
        tmp_path / "duplicate", [("s-train-tight", 32), ("s-train-tight", 32), ("s-val-tight", 36)]
    )
    assert value["completeness"]["duplicates"] == 1 and value["memorization_passed"] == []
    value = scored(tmp_path / "unfinished", [("s-train-tight", 32), ("s-val-tight", 36)], calls=3)
    assert value["completeness"]["run_completed"] is False and value["memorization_passed"] == []
