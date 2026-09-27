"""Recompute published adaptation errors/hashes without GPU or flight access."""

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
    c = read("comparison.json")
    r = read("training-receipt.json")
    p = read("protocol.json")
    b = read("budget.json")
    data = read("gpu-dataset-manifest.json")
    steps = read("training.json")
    assert r["protocol_sha256"] == sha(R / "protocol.json")
    assert p["dataset_manifest_sha256"] == sha(R / "gpu-dataset-manifest.json")
    assert p["collection_manifest_sha256"] == sha(R / "dataset-manifest.json")
    for name in ["train_head.py", "paired_appearance.py", "ship_anwm.py", "ship_anwm_server.py"]:
        assert sha(R / name) == p["payload_sha256"][name]
    assert r["steps"] == p["steps"] == len(steps) == 2048
    assert [s["step"] for s in steps] == list(range(1, 2049))
    train = {s["id"] for s in data["samples"] if s["split"] == "train"}
    test = {s["id"] for s in data["samples"] if s["split"] == "test"}
    assert all(
        s["sample"] in train and np.isfinite([s["loss"], s["gradient_norm"]]).all() for s in steps
    )
    assert r["frozen_base_unchanged"] and r["checkpoint_reloaded"] and r["head_change_l2"] > 0
    audit = read("saved-head-audit.json")
    assert audit["head_sha256"] == r["final_head_sha256"] and audit["all_tensors_finite"]
    assert (
        audit["adapter_sha256"] == r["adapter_sha256"]
        and audit["parameters"] == r["trainable_parameters"]
    )
    assert audit["protocol_sha256"] == r["protocol_sha256"]
    previous = json.loads((R.parent / "yokohama-wam-attention/training-receipt.json").read_text())
    assert (
        r["initial_adapter_sha256"]
        == previous["adapter_sha256"]
        == p["payload_sha256"]["initial-adapter.pt"]
    )
    assert r["learning_kind"] == "block-v3" and r["trainable_parameters"] == 47851808
    old_manifest = R.parent / "yokohama-wam-attention/dataset-manifest.json"
    assert sha(old_manifest) == p["previous_manifest_sha256"]
    earlier = R.parent / "yokohama-wam-adaptation/dataset-manifest.json"
    assert sha(earlier) == p["earlier_manifest_sha256"]
    assert data["previous_inspected_sites"] == (
        json.loads(earlier.read_text())["sites"] + json.loads(old_manifest.read_text())["sites"]
    )
    new_positions = np.asarray(
        [s[k] for s in data["sites"] if s["split"] == "test" for k in ("xyz", "endpoint_xyz")]
    )
    old_positions = np.asarray(
        [s[k] for s in data["previous_inspected_sites"] for k in ("xyz", "endpoint_xyz")]
    )
    separation = float(np.linalg.norm(new_positions[:, None] - old_positions[None], axis=2).min())
    assert separation >= 15 and abs(separation - data["previous_site_separation_m"]) < 1e-8
    identity = read("identity.json")
    assert all(r[key] == value for key, value in identity.items())
    assert r["initial_head_sha256"] != r["final_head_sha256"]
    changes = read("parameter-change-audit.json")
    assert changes["status"] == "verified" and not changes["cuda_initialized"]
    assert changes["adapter_sha256"] == r["adapter_sha256"]
    assert changes["protocol_sha256"] == r["protocol_sha256"]
    assert {x["name"] for x in changes["parameters"]} == set(r["trainable_names"])
    assert sum(x["parameters"] for x in changes["parameters"]) == r["trainable_parameters"]
    assert all(
        x["all_finite"] and x["change_l2"] > 0 and x["initial_sha256"] != x["final_sha256"]
        for x in changes["parameters"]
    )
    assert (
        not r["test_targets_uploaded"]
        and r["training_targets_uploaded"]
        and not r["flight_invoked"]
        and not r["vla_invoked"]
    )
    assert r["inference_calls"] == len(r["before"]) + len(r["after"]) == 16
    assert set(x["id"] for x in c["cases"]) == test and len(test) == 8
    for case in c["cases"]:
        raw = R / case["raw_actual"]
        assert sha(raw) == case["actual_raw_sha256"]
        sample = next(s for s in read("dataset-manifest.json")["samples"] if s["id"] == case["id"])
        assert (
            sample["target"]["sha256"] == case["actual_raw_sha256"]
            and sample["site"] == case["site"]
            and sample["action"] == case["action"]
        )
        actual = (
            Image.open(raw)
            .convert("RGB")
            .crop((80, 0, 560, 360))
            .resize((224, 224), Image.Resampling.BILINEAR)
        )
        np.testing.assert_array_equal(
            np.asarray(actual), np.asarray(Image.open(R / case["actual"]))
        )
        for stage in ["before", "after"]:
            q = case[stage]
            assert sha(R / q["image"]) == q["sha256"]
            receipt = next(v for v in r[stage] if v["sample"] == case["id"])
            assert q["sha256"] == receipt["sha256"] and q["elapsed_s"] == receipt["elapsed_s"]
            diff = np.abs(
                np.asarray(Image.open(R / q["image"]), dtype=float)
                - np.asarray(actual, dtype=float)
            )
            mae = float(diff.mean())
            bad = float((diff.max(2) > 40).mean())
            assert (
                abs(mae - q["metrics"]["rgb_mae"]) < 1e-10
                and abs(bad - q["metrics"]["fraction_pixels_max_channel_error_over_40"]) < 1e-10
            )
            assert q["numeric_pass"] == (mae <= 15 and bad <= 0.1 and q["elapsed_s"] <= 75)
    sites = {x["site"] for x in c["cases"]}
    assert len(sites) == 4 and all(
        {x["action"] for x in c["cases"] if x["site"] == s} == {"hold", "forward"} for s in sites
    )
    passed = [
        s
        for s in sites
        if all(
            x["after"]["numeric_pass"] and x["manual_after_pass"]
            for x in c["cases"]
            if x["site"] == s
        )
    ]
    assert c["manual_inspection_complete"] and c["minimum_feasibility_passed"] == bool(passed)
    assert sorted(c["passed_sites"]) == sorted(passed)
    for stage in ["before", "after"]:
        mean = float(np.mean([x[stage]["metrics"]["rgb_mae"] for x in c["cases"]]))
        assert abs(mean - c["aggregate"]["mean_rgb_mae_" + stage]) < 1e-10
    assert (
        abs(
            c["aggregate"]["relative_mae_reduction"]
            - (1 - c["aggregate"]["mean_rgb_mae_after"] / c["aggregate"]["mean_rgb_mae_before"])
        )
        < 1e-10
    )
    assert c["native_flight_qualified"] is False
    assert (
        b["cleanup_confirmed"]
        and b["cost_estimate_closed"]
        and b["cumulative_estimated_usd"] <= 15
        and not b["error"]
        and not b["cleanup_errors"]
    )
    manifest = read("manifest.json")["sha256"]
    inventory = {
        str(x.relative_to(R))
        for x in R.rglob("*")
        if x.is_file()
        and x.name not in {"manifest.json", "verification.json"}
        and "__pycache__" not in x.parts
    }
    assert inventory == set(manifest)
    for path, digest in manifest.items():
        assert sha(R / path) == digest, path
    print(
        json.dumps(
            dict(
                status="publication_record_verified",
                updates=2048,
                test_pairs=8,
                before_passes=sum(x["before"]["numeric_pass"] for x in c["cases"]),
                after_passes=sum(x["after"]["numeric_pass"] for x in c["cases"]),
                limited_feasibility_sites=sorted(passed),
                native_flight_qualified=False,
                raw_training_rerun=False,
            )
        )
    )


if __name__ == "__main__":
    main()
