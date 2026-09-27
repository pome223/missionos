"""Verify public image metrics and receipts; no GPU, simulator or cloud access."""

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

R = Path(__file__).resolve().parent


def read(name):
    return json.loads((R / name).read_text())


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    c, r, p, data = [
        read(n)
        for n in [
            "comparison.json",
            "training-receipt.json",
            "protocol.json",
            "gpu-dataset-manifest.json",
        ]
    ]
    assert r["protocol_sha256"] == sha(R / "protocol.json")
    assert p["dataset_manifest_sha256"] == sha(R / "gpu-dataset-manifest.json")
    assert p["motion_verification_sha256"] == sha(R / "motion-verification.json")
    assert p["sitl_verification_sha256"] == sha(R / "sitl-verification.json")
    for name in [
        "train_head.py",
        "yokohama_motion_data.py",
        "ship_anwm.py",
        "ship_anwm_server.py",
        "paired_appearance.py",
    ]:
        assert sha(R / name) == p["payload_sha256"][name]
    old = json.loads((R.parent / "yokohama-wam-block/training-receipt.json").read_text())
    assert (
        r["initial_adapter_sha256"]
        == old["adapter_sha256"]
        == p["payload_sha256"]["initial-adapter.pt"]
    )
    assert r["learning_kind"] == p["learning_kind"] == "motion-v4"
    assert r["trainable_parameters"] == 47851808 and len(r["trainable_names"]) == 24
    assert r["frozen_base_unchanged"] and r["checkpoint_reloaded"] and r["head_change_l2"] > 0
    assert not r["flight_invoked"] and not r["vla_invoked"] and not r["test_targets_uploaded"]
    assert r["training_targets_uploaded"] and data["test_targets_uploaded"] is False
    identity, audit, changes = [
        read(n) for n in ["identity.json", "saved-head-audit.json", "parameter-change-audit.json"]
    ]
    assert all(r[k] == v for k, v in identity.items())
    assert audit["adapter_sha256"] == changes["adapter_sha256"] == r["adapter_sha256"]
    assert audit["head_sha256"] == r["final_head_sha256"] != r["initial_head_sha256"]
    assert (
        audit["all_tensors_finite"]
        and changes["status"] == "verified"
        and not changes["cuda_initialized"]
    )
    assert changes["protocol_sha256"] == audit["protocol_sha256"] == r["protocol_sha256"]
    assert {x["name"] for x in changes["parameters"]} == set(r["trainable_names"])
    assert all(
        x["all_finite"] and x["change_l2"] > 0 and x["initial_sha256"] != x["final_sha256"]
        for x in changes["parameters"]
    )
    train = {s["id"] for s in data["samples"] if s["split"] == "train"}
    test = {s["id"] for s in data["samples"] if s["split"] == "test"}
    assert len(train) == 12 and len(test) == 5 and train.isdisjoint(test)
    steps = read("training.json")
    assert r["steps"] == p["steps"] == len(steps) == 2048
    assert [x["step"] for x in steps] == list(range(1, 2049))
    assert all(
        x["sample"] in train and np.isfinite([x["loss"], x["gradient_norm"]]).all() for x in steps
    )
    points = {"train": [], "test": []}
    inputs, future = set(), set()
    for s in data["samples"]:
        path = R / "histories" / Path(s["input"]["file"]).name
        assert sha(path) == s["input"]["sha256"] == data["assets"][s["input"]["file"]]
        history = json.loads(path.read_text())["frames"]
        assert len(history) == 16 and [x["index"] for x in history] == s["input_indices"]
        assert "actual_target_camera_pose" not in s
        inputs.update(s["input_indices"])
        points[s["split"]].extend(np.asarray(x["camera_pose"])[:3, 3] for x in history)
        points[s["split"]].append(np.asarray(s["requested_camera_pose"])[:3, 3])
        if s["split"] == "test":
            assert "training_target" not in s
            future.add(s["target_index"])
    assert not inputs & future
    a, b = np.asarray(points["train"]), np.asarray(points["test"])
    gap = float(np.linalg.norm(a[:, None] - b[None], axis=2).min())
    assert gap >= 15.6 and abs(gap - data["spatial_separation_m"]) < 1e-8
    assert r["inference_calls"] == len(r["before"]) + len(r["after"]) == 10
    assert len(c["cases"]) == 5 and {x["id"] for x in c["cases"]} == test
    for case in c["cases"]:
        s = next(s for s in data["samples"] if s["id"] == case["id"])
        assert sha(R / case["raw_actual"]) == s["target_sha256"] == case["actual_raw_sha256"]
        truth = (
            Image.open(R / case["raw_actual"])
            .convert("RGB")
            .crop((80, 0, 560, 360))
            .resize((224, 224), Image.Resampling.BILINEAR)
        )
        np.testing.assert_array_equal(np.asarray(truth), np.asarray(Image.open(R / case["actual"])))
        assert (
            case["action"] == s["action"]
            and abs(case["target_sim_s"] - case["input_cutoff_sim_s"] - 1) <= 0.0080001
        )
        for stage in ["before", "after"]:
            row = case[stage]
            record = next(x for x in r[stage] if x["sample"] == case["id"])
            assert sha(R / row["image"]) == row["sha256"] == record["sha256"]
            assert row["elapsed_s"] == record["elapsed_s"]
            diff = np.abs(
                np.asarray(Image.open(R / row["image"]), dtype=float)
                - np.asarray(truth, dtype=float)
            )
            mae, bad = float(diff.mean()), float((diff.max(2) > 40).mean())
            assert abs(mae - row["metrics"]["rgb_mae"]) < 1e-10
            assert abs(bad - row["metrics"]["fraction_pixels_max_channel_error_over_40"]) < 1e-10
            assert row["numeric_pass"] == (mae <= 15 and bad <= 0.1 and row["elapsed_s"] <= 75)
    passed = [x["id"] for x in c["cases"] if x["after"]["numeric_pass"] and x["manual_after_pass"]]
    assert c["manual_inspection_complete"] and c["minimum_feasibility_passed"] == (len(passed) == 5)
    assert c["qualified_cases"] == passed and c["native_flight_qualified"] is False
    for stage in ["before", "after"]:
        assert (
            abs(
                np.mean([x[stage]["metrics"]["rgb_mae"] for x in c["cases"]])
                - c["aggregate"]["mean_rgb_mae_" + stage]
            )
            < 1e-10
        )
    assert (
        abs(
            c["aggregate"]["relative_mae_reduction"]
            - (1 - c["aggregate"]["mean_rgb_mae_after"] / c["aggregate"]["mean_rgb_mae_before"])
        )
        < 1e-10
    )
    cpu, motion = read("sitl-verification.json"), read("motion-verification.json")
    assert cpu["status"] == motion["status"] == "passed" and all(cpu["checks"].values())
    assert len(cpu["holds_recomputed"]) == 7 and all(x["passed"] for x in cpu["holds_recomputed"])
    route = read("route.json")
    assert route["source_sha256"] == cpu["evidence_hashes"]["flight-trajectory.jsonl"]
    assert route["run_id"] == cpu["run_id"] == motion["run_id"]
    assert all(np.isfinite(x["xyz"]).all() for x in route["samples"])
    assert np.all(np.diff([x["sim_s"] for x in route["samples"]]) >= 0)
    assert len(read("collection-attempts.json")) == 2
    assert [a["status"] for a in read("collection-attempts.json")] == ["failed", "passed"]
    for clip in read("clips.json"):
        assert sha(R / clip["file"]) == clip["sha256"] and len(clip["frames"]) == 20
    budget = read("budget.json")
    assert (
        budget["cleanup_confirmed"]
        and budget["cost_estimate_closed"]
        and budget["cumulative_estimated_usd"] <= 15
    )
    assert not budget["error"] and not budget["cleanup_errors"]
    assert budget["cumulative_estimated_usd"] == c["cumulative_estimated_usd"]
    manifest = read("manifest.json")["sha256"]
    inventory = {
        str(x.relative_to(R))
        for x in R.rglob("*")
        if x.is_file()
        and x.name not in {"manifest.json", "verification.json"}
        and "__pycache__" not in x.parts
    }
    assert inventory == set(manifest)
    for name, value in manifest.items():
        assert sha(R / name) == value, name
    print(
        json.dumps(
            dict(
                status="publication_record_verified",
                updates=2048,
                test_cases=5,
                before_passes=sum(x["before"]["numeric_pass"] for x in c["cases"]),
                after_passes=sum(x["after"]["numeric_pass"] for x in c["cases"]),
                qualified_cases=passed,
                native_flight_qualified=False,
                raw_training_rerun=False,
            )
        )
    )


if __name__ == "__main__":
    main()
