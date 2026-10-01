"""The input ablation must swap exactly one input and keep futures on the host."""

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ablate_yokohama_pad_inputs import inputs, validate  # noqa: E402
from scripts.evaluate_yokohama_pad_inputs import evaluate  # noqa: E402
from scripts.yokohama_pad_wam_study import sha, write  # noqa: E402

ORANGE = (240, 128, 20)


def frame(x):
    im = np.full((224, 224, 3), 150, np.uint8)
    im[100:112, x : x + 30] = ORANGE
    return im


def test_conditions_swap_only_history_or_conditioning():
    history = np.stack([frame(10 + 4 * i) for i in range(16)])
    background = np.full((224, 224, 3), 150, np.uint8)
    a_ctx, a_proj = inputs(history, background, "A")
    b_ctx, b_proj = inputs(history, background, "B")
    c_ctx, c_proj = inputs(history, background, "C")
    d_ctx, d_proj = inputs(history, background, "D")
    assert np.array_equal(a_ctx, history) and np.array_equal(a_proj, history[-1:])
    assert all(np.array_equal(f, history[-1]) for f in b_ctx) and np.array_equal(b_proj, a_proj)
    assert np.array_equal(c_ctx, history) and np.array_equal(c_proj, background[None])
    assert np.array_equal(d_ctx, b_ctx) and np.array_equal(d_proj, c_proj)


def staged(tmp_path):
    root = tmp_path / "staged"
    root.mkdir()
    history = np.stack([frame(10 + 4 * i) for i in range(16)])
    stamps = np.arange(16) * 250_000_000
    np.savez_compressed(root / "h.npz", rgb=history, stamps_ns=stamps)
    Image.fromarray(np.full((224, 224, 3), 150, np.uint8)).save(root / "bg.png")
    protocol = dict(
        schema="pad_anwm_input_ablation.v1",
        training_allowed=False,
        conditions={k: k for k in "ABCD"},
        offset=12,
        weights=["initial"],
        seeds=[42],
        samples=[
            dict(
                id="s",
                history="h.npz",
                background="bg.png",
                cutoff_stamp_ns=int(stamps[-1]),
                trained_in_after_1024=True,
            )
        ],
        files={"h.npz": sha(root / "h.npz"), "bg.png": sha(root / "bg.png")},
        interpretation=dict(scope="test"),
    )
    write(root / "input-ablation-protocol.json", protocol)
    return root, history


def test_validation_rejects_future_files_and_late_histories(tmp_path):
    root, _ = staged(tmp_path)
    assert validate(root)["offset"] == 12
    (root / "training-target.png").write_bytes(b"future")
    with pytest.raises(ValueError, match="host"):
        validate(root)
    (root / "training-target.png").unlink()
    protocol = json.loads((root / "input-ablation-protocol.json").read_text())
    protocol["samples"][0]["cutoff_stamp_ns"] = 0
    write(root / "input-ablation-protocol.json", protocol)
    with pytest.raises(ValueError, match="cutoff"):
        validate(root)


def test_sensitivity_separates_history_and_conditioning_effects(tmp_path):
    root, history = staged(tmp_path)
    results = tmp_path / "results"
    future = frame(10 + 4 * 27)
    Image.fromarray(future).save(tmp_path / "future.png")
    # A and B identical (history ignored); C and D drop the lead (conditioning matters).
    outputs = {
        "A": history[-1],
        "B": history[-1],
        "C": np.full_like(future, 150),
        "D": np.full_like(future, 150),
    }
    records = []
    for c, image in outputs.items():
        path = results / f"initial/s/{c}/seed-42/final.png"
        path.parent.mkdir(parents=True)
        Image.fromarray(image).save(path)
        records.append(
            dict(
                weights="initial",
                sample="s",
                condition=c,
                seed=42,
                final=dict(file=str(path.relative_to(results)), sha256=sha(path)),
            )
        )
    write(results / "forecasts.json", records)
    write(results / "summary.json", dict(status="completed"))
    value = evaluate(root, results, {"s": tmp_path / "future.png"})
    assert value["complete"] is True
    s = value["sensitivity"]["initial/trained"]
    assert s["history_with_latest"]["now"] == 0 and s["conditioning_with_moving"]["now"] > 20
    row = value["rows"][0]
    assert row["A"]["error_to_now"] == 0 and row["C"]["present"] is False
