"""The rule-learning gate passes only on complete, motion-using, better-than-persistence forecasts."""


import numpy as np
from PIL import Image

from scripts.evaluate_yokohama_pad_wam_rule import evaluate
from scripts.yokohama_pad_wam_study import detect_lead, sha, write


def frame(x):
    im = np.full((224, 224, 3), 150, np.uint8)
    im[100:112, x : x + 30] = (240, 128, 20)
    return im


def run(tmp_path, moving_forecast, still_forecast, drop=False):
    staged, prepared, results = tmp_path / "staged", tmp_path / "prepared", tmp_path / "results"
    (staged / "payload/histories").mkdir(parents=True)
    prepared.mkdir()
    ids, samples, truth, frames = ["v1", "v2"], [], [], {}
    for n, ident in enumerate(ids):
        xs = [10 + 3 * i + n for i in range(28)]
        rgb = np.stack([frame(x) for x in xs])
        rel = f"histories/{ident}.npz"
        np.savez_compressed(staged / "payload" / rel, rgb=rgb[:16], stamps_ns=np.arange(16))
        samples.append(
            dict(
                id=ident,
                split="val",
                crop="tight",
                frames=rel,
                last_index=15,
                offset=12,
                cutoff_stamp_ns=15,
            )
        )
        truth.append(
            dict(
                id=ident,
                readout_now=detect_lead(rgb[15], "tight"),
                readout_future=detect_lead(rgb[27], "tight"),
            )
        )
        frames[ident] = rgb
    data = dict(
        samples=samples,
        assets={s["frames"]: sha(staged / "payload" / s["frames"]) for s in samples},
    )
    write(staged / "payload/dataset.json", data)
    protocol = dict(
        moving_ids=ids,
        static_ids=[],
        checkpoints=[10],
        seed=42,
        second_seed=43,
        gate=dict(readable_fraction=0.5, fraction_of_persistence=0.5),
    )
    write(staged / "rule-protocol.json", protocol)
    write(prepared / "truth.json", truth)
    records = []
    plan = [("moving", 42), ("moving", 43), ("still", 42)]
    for hist, seed in plan:
        for ident in ids:
            if drop and hist == "moving" and seed == 43 and ident == "v2":
                continue
            image = frames[ident][27 if hist == "moving" and moving_forecast == "future" else 15]
            if hist == "still":
                image = frames[ident][27 if still_forecast == "future" else 15]
            dest = results / "ckpt-00010" / f"{hist}-seed-{seed}" / ident
            dest.mkdir(parents=True)
            Image.fromarray(image).save(dest / "prediction.png")
            write(
                dest / "result.json",
                dict(
                    sample_id=ident,
                    stage="ckpt-00010",
                    seed=seed,
                    history=hist,
                    frames_sha256=data["assets"][f"histories/{ident}.npz"],
                    prediction_sha256=sha(dest / "prediction.png"),
                ),
            )
            records.append(
                dict(
                    file=str((dest / "result.json").relative_to(results)),
                    sha256=sha(dest / "result.json"),
                )
            )
    write(results / "forecast-manifest.json", dict(records=records))
    write(results / "summary.json", dict(status="completed", inference_calls=len(records)))
    return evaluate(staged, prepared, results)


def test_motion_using_forecast_passes(tmp_path):
    value = run(tmp_path, "future", "now")
    assert value["complete"] and value["passed"], value["checks"]


def test_persistence_copy_and_still_equivalence_fail(tmp_path):
    value = run(tmp_path, "now", "now")
    assert value["passed"] is False and value["checks"]["seed_42_beats_persistence"] is False
    assert value["checks"]["history_used"] is False


def test_missing_forecast_blocks_the_gate(tmp_path):
    value = run(tmp_path, "future", "now", drop=True)
    assert value["complete"] is False and value["passed"] is False
