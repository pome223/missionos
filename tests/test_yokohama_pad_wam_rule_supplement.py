"""Host-only strata must not replace the predeclared diagnostic gate."""

import json
from pathlib import Path
import subprocess
import sys

import numpy as np
from PIL import Image
import pytest

from scripts.analyze_yokohama_pad_wam_rule import audit, freeze, score
from scripts.evaluate_yokohama_pad_wam_rule import evaluate
from scripts.yokohama_pad_wam_study import detect_lead, sha, write


def frame(x):
    im = np.full((224, 224, 3), 150, np.uint8)
    im[100:112, x : x + 30] = (240, 128, 20)
    return im


@pytest.fixture
def bundle(tmp_path):
    staged, prepared, results = (tmp_path / n for n in ("staged", "prepared", "results"))
    prepared.mkdir()
    samples, assets, truth, histories, futures = [], {}, [], {}, {}
    # Identical training inputs can have different scripted future departure times.
    for n, target_x in enumerate((70, 100)):
        rel = f"frames/train-{n}.npz"
        path = staged / "payload" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        rgb = np.stack([frame(40)] * 16 + [frame(target_x)] * 12)
        np.savez_compressed(path, rgb=rgb, stamps_ns=np.arange(28))
        samples.append(
            dict(
                id=f"train-{n}",
                sequence=f"train-{n}",
                frames=rel,
                split="train",
                last_index=15,
                offset=12,
                cutoff_stamp_ns=15,
            )
        )
        assets[rel] = sha(path)
    for ident, xs, future_x in (
        ("constant", [40] * 16, 70),
        ("changing", [10 + 3 * i for i in range(16)], 91),
        ("static", [120] * 16, 120),
    ):
        rel = f"histories/{ident}.npz"
        path = staged / "payload" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        history = np.stack([frame(x) for x in xs])
        future = frame(future_x)
        np.savez_compressed(path, rgb=history, stamps_ns=np.arange(16))
        samples.append(
            dict(
                id=ident,
                sequence=f"val-{ident}",
                frames=rel,
                split="val",
                last_index=15,
                offset=12,
                cutoff_stamp_ns=15,
            )
        )
        assets[rel] = sha(path)
        truth.append(
            dict(
                id=ident,
                readout_now=detect_lead(history[-1], "tight"),
                readout_future=detect_lead(future, "tight"),
            )
        )
        histories[ident], futures[ident] = history, future
    write(staged / "payload/dataset.json", dict(samples=samples, assets=assets))
    protocol = dict(
        dataset_sha256=sha(staged / "payload/dataset.json"),
        moving_ids=["constant", "changing"],
        static_ids=["static"],
        checkpoints=[10],
        seed=42,
        second_seed=43,
        gate=dict(readable_fraction=0.5, fraction_of_persistence=0.5),
    )
    write(staged / "rule-protocol.json", protocol)
    write(prepared / "truth.json", truth)
    records = []
    for hist, seed, ids in (
        ("moving", 42, ["constant", "changing", "static"]),
        ("still", 42, ["constant", "changing"]),
        ("moving", 43, ["constant", "changing"]),
    ):
        for ident in ids:
            dest = results / "ckpt-00010" / f"{hist}-{seed}" / ident
            dest.mkdir(parents=True)
            prediction = futures[ident] if hist == "moving" else histories[ident][-1]
            Image.fromarray(prediction).save(dest / "prediction.png")
            write(
                dest / "result.json",
                dict(
                    stage="ckpt-00010",
                    history=hist,
                    seed=seed,
                    sample_id=ident,
                    frames_sha256=assets[f"histories/{ident}.npz"],
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
    write(
        results / "summary.json",
        dict(
            status="completed",
            inference_calls=len(records),
            protocol_sha256=sha(staged / "rule-protocol.json"),
        ),
    )
    return staged, prepared, results, tmp_path / "plan.json"


def test_grouping_uses_input_content_and_records_overlap(bundle):
    staged, _, _, plan = bundle
    value = freeze(staged, plan)["audit"]
    assert value["groups"]["constant_history"] == ["constant"]
    assert value["groups"]["changing_history"] == ["changing"]
    assert value["training"]["unique_history_content"] == 1
    assert value["training"]["identical_input_different_target_groups"] == 1
    assert value["observations"]["constant"]["matching_training_input_rows"] == 2
    assert value["observations"]["changing"]["matching_training_input_rows"] == 0
    with pytest.raises(FileExistsError):
        freeze(staged, plan)


def test_supplement_preserves_entire_original_result(bundle):
    staged, prepared, results, plan = bundle
    freeze(staged, plan)
    original = evaluate(staged, prepared, results)
    value = score(staged, prepared, results, plan)
    assert value["original_evaluation"] == original
    assert original["passed"]
    assert value["supplementary"]["gate_defined"] is False
    groups = value["supplementary"]["groups"]
    assert groups["constant_history"]["learning_curve"]["ckpt-00010"]["expected"] == 1
    assert groups["changing_history"]["history_swap_pairs"]["moving_better"] == 1
    assert "passed" not in groups["changing_history"]


@pytest.mark.parametrize("fault", ["missing", "duplicate", "unfinished"])
def test_incomplete_runs_cannot_gain_a_supplementary_success(bundle, fault):
    staged, prepared, results, plan = bundle
    freeze(staged, plan)
    path = results / "forecast-manifest.json"
    records = json.loads(path.read_text())["records"]
    if fault == "missing":
        records.pop()
    elif fault == "duplicate":
        records.append(records[0])
    write(path, dict(records=records))
    write(
        results / "summary.json",
        dict(
            status="failed" if fault == "unfinished" else "completed",
            inference_calls=len(records),
            protocol_sha256=sha(staged / "rule-protocol.json"),
        ),
    )
    value = score(staged, prepared, results, plan)
    assert value["original_evaluation"]["passed"] is False
    assert value["supplementary"]["status"] == "not_scored_incomplete_run"
    assert "groups" not in value["supplementary"]


def test_results_from_a_different_protocol_are_rejected(bundle):
    staged, prepared, results, plan = bundle
    freeze(staged, plan)
    path = results / "summary.json"
    value = json.loads(path.read_text())
    value["protocol_sha256"] = "another-run"
    write(path, value)
    with pytest.raises(ValueError, match="different frozen protocol"):
        score(staged, prepared, results, plan)


def test_unreadable_forecasts_remain_in_the_denominator(bundle):
    staged, prepared, results, plan = bundle
    freeze(staged, plan)
    manifest_path = results / "forecast-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for record in manifest["records"]:
        path = results / record["file"]
        receipt = json.loads(path.read_text())
        if receipt["sample_id"] == "changing" and receipt["history"] == "moving":
            image = path.with_name("prediction.png")
            Image.fromarray(np.full((224, 224, 3), 150, np.uint8)).save(image)
            receipt["prediction_sha256"] = sha(image)
            write(path, receipt)
            record["sha256"] = sha(path)
    write(manifest_path, manifest)
    value = score(staged, prepared, results, plan)
    group = value["supplementary"]["groups"]["changing_history"]
    metrics = group["learning_curve"]["ckpt-00010"]
    assert metrics["expected"] == metrics["observed"] == metrics["unreadable"] == 1
    assert metrics["median_error_including_misses_px"] == "missed"
    assert value["original_evaluation"] == evaluate(staged, prepared, results)


def test_regrouping_and_changed_inputs_are_rejected(bundle):
    staged, prepared, results, plan = bundle
    frozen = freeze(staged, plan)
    frozen["audit"]["groups"]["changing_history"] = []
    write(plan, frozen)
    with pytest.raises(ValueError, match="regrouping"):
        score(staged, prepared, results, plan)
    (staged / "payload/histories/constant.npz").write_bytes(b"changed")
    with pytest.raises(ValueError, match="Changed history"):
        audit(staged)


def test_cli_freeze_and_score_without_gpu(bundle):
    staged, prepared, results, plan = bundle
    script = Path(__file__).resolve().parents[1] / "scripts/analyze_yokohama_pad_wam_rule.py"
    common = [sys.executable, str(script)]
    frozen = subprocess.run(
        common + ["freeze", "--staged", str(staged), "--output", str(plan)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(frozen.stdout)["gpu_requested"] is False
    output = plan.with_name("scored.json")
    result = subprocess.run(
        common
        + [
            "score",
            "--staged",
            str(staged),
            "--prepared",
            str(prepared),
            "--results",
            str(results),
            "--plan",
            str(plan),
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout)["supplementary_status"] == "completed"
    assert json.loads(output.read_text())["original_evaluation"]["passed"]
