"""Recompute published case aggregates and verify artifact integrity (no model run)."""

from pathlib import Path
import hashlib
import json
import math
import struct
import zlib

BASE = Path(__file__).resolve().parent


def png(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path.name
    i, image_data, header, ended = 8, b"", None, False
    while i < len(data):
        n = struct.unpack(">I", data[i : i + 4])[0]
        tag, body = data[i + 4 : i + 8], data[i + 8 : i + 8 + n]
        crc = struct.unpack(">I", data[i + 8 + n : i + 12 + n])[0]
        assert zlib.crc32(tag + body) & 0xFFFFFFFF == crc, path.name
        if tag == b"IHDR":
            assert header is None and i == 8
            header = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            image_data += body
        elif tag == b"IEND":
            assert n == 0 and i + 12 == len(data)
            ended = True
        i += n + 12
    assert ended and header
    w, h, depth, color, compression, filtering, interlace = header
    assert 0 < w <= 10000 and 0 < h <= 10000
    assert depth == 8 and color in (2, 6) and compression == filtering == interlace == 0
    stride = w * (3 if color == 2 else 4) + 1
    raw = zlib.decompress(image_data)
    assert len(raw) == h * stride
    assert all(raw[k * stride] <= 4 for k in range(h))


def quantile(values, p):
    v = sorted(values)
    index = (len(v) - 1) * p
    i = int(index)
    return v[i] if i == len(v) - 1 else v[i] + (v[i + 1] - v[i]) * (index - i)


def verify(root=BASE):
    def load(name):
        return json.loads((root / name).read_text())

    manifest = load("manifest.json")
    actual = {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.name != "manifest.json"
    }
    assert actual == set(manifest), "unexpected/missing artifact"
    for name, digest in manifest.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
        if name.endswith(".png"):
            png(root / name)
    budget = load("budget-and-cleanup.json")
    assert budget["gpu_instance_deleted"] and budget["dedicated_disk_deleted"]
    assert (
        budget["remaining_matching_instances"]
        == budget["remaining_matching_disks"]
        == 0
    )
    assert (
        budget["cumulative_conservative_estimate_usd"] <= budget["authorized_cap_usd"]
    )
    experiment = load("experiment.json")
    protocol = load("final-protocol.json")
    frozen = load("final-freeze.json")
    assert (
        hashlib.sha256((root / "input_contract.py").read_bytes()).hexdigest()
        == frozen["files"]["input_contract.py"]
    )
    assert (
        protocol["starting_sha256"]
        == frozen["files"]["checkpoints/start.pt"]
        == load("initial-training.json")["sha256"]
    )
    assert protocol["updates"] == 720 and protocol["training_cases"] == 78
    assert protocol["validation_cases"] == 32
    assert protocol["forecast_options"] == {"continue_hold": 28.4, "bank": 14.2}
    assert experiment["new_games_executed"] == 0 and experiment["threshold"] == 0.5
    assert (
        experiment["final_readout_sha256"]
        == frozen["files"]["checkpoints/readout-new.npz"]
    )
    targets = {r["name"]: r for r in load("validation-cases.json")}
    assert len(targets) == 32
    train = load("final-training-cases.json")
    assert (
        len(train) == len({(r["seed"], r["count"], r["option"]) for r in train}) == 78
    )
    assert all(r["split"] == "train" for r in train)
    assert frozen["no_final_results_seen"] is True
    expected_files = {r["name"] + ".npz" for r in targets.values()} | {
        f"train-{r['seed']}-{r['count']:02}-{r['option']}.npz" for r in train
    }
    assert set(frozen["data"]) == expected_files
    assert all(len(value) == 64 for value in frozen["data"].values())

    scan = load("source-training-scan.json")
    assert len(scan) == len({(r["seed"], r["next_count"]) for r in scan}) == 417
    failures = {(r["seed"], r["next_count"]) for r in scan if r["bank_collapsed"]}
    assert len(failures) == 9
    assert failures == {
        (r["seed"], r["count"])
        for r in train
        if r["option"] == "bank" and r["collapsed"]
    }
    expected = {"bank": (42, 9), "continue_hold": (36, 15)}
    for option, (n, positive) in expected.items():
        rr = [r for r in train if r["option"] == option]
        assert len(rr) == n and sum(r["collapsed"] for r in rr) == positive
    initial_drops = load("initial-collapse-audit.json")
    assert len(initial_drops) == len({r["name"] for r in initial_drops}) == 11
    assert all(0 <= r["initial_max_drop_m"] < 0.03 for r in initial_drops)
    datasets = {
        name: load(name)
        for name in ("initial-evaluation.json", "final-evaluation.json")
    }
    case_sets = []
    for filename, stages in datasets.items():
        assert set(stages) == {"before-seed195", "after-seed195", "after-seed196"}
        for stage, data in stages.items():
            rows = data["rows"]
            assert len(rows) == len({r["name"] for r in rows}) == 32
            case_sets.append({r["name"] for r in rows})
            for row in rows:
                assert type(row["collapsed"]) is bool and type(row["predicted"]) is bool
                assert math.isfinite(row["risk"]) and 0 <= row["risk"] <= 1
                assert row["predicted"] == (row["risk"] >= 0.5)
                target = targets[row["name"]]
                assert (
                    row["collapsed"] == target["collapsed"]
                    and row["count"] == target["count"]
                    and row["option"] == target["option"]
                )
                assert (
                    math.isfinite(row["generation_seconds"])
                    and row["generation_seconds"] > 0
                )
                seed = int(row["name"].split("-")[1])
                assert seed not in {r["seed"] for r in train}
                for key in ("block_mask_iou", "late3_iou"):
                    assert math.isfinite(row[key]) and 0 <= row[key] <= 1
            for option, positives in [("bank", 2), ("continue_hold", 7)]:
                rr = [r for r in rows if r["option"] == option]
                assert len(rr) == 16 and sum(r["collapsed"] for r in rr) == positives
                cm = dict(tp=0, fp=0, tn=0, fn=0)
                for r in rr:
                    cm[
                        ("t" if r["predicted"] == r["collapsed"] else "f")
                        + ("p" if r["predicted"] else "n")
                    ] += 1
                saved = data["summary"][option]
                assert saved["n"] == 16 and all(saved[k] == v for k, v in cm.items())
                for summary, field in [
                    ("mean_iou", "block_mask_iou"),
                    ("late3_iou", "late3_iou"),
                ]:
                    assert abs(saved[summary] - sum(r[field] for r in rr) / 16) < 1e-12
                assert (
                    abs(
                        saved["generation_p95_seconds"]
                        - quantile([r["generation_seconds"] for r in rr], 0.95)
                    )
                    < 1e-10
                )
    assert all(s == case_sets[0] for s in case_sets)
    original = {
        r["name"]: r
        for r in datasets["initial-evaluation.json"]["after-seed195"]["rows"]
    }
    for row in datasets["final-evaluation.json"]["before-seed195"]["rows"]:
        assert all(
            row[k] == original[row["name"]][k]
            for k in ("risk", "block_mask_iou", "late3_iou")
        )

    reader = load("readout-parameters.json")
    measured = load("measured-readout.json")
    assert len(measured) == len({r["name"] for r in measured}) == 32
    assert reader["threshold"] == 0.5
    for row in measured:
        assert len(row["features"]) == len(reader["weights"]) == 28
        z = (
            sum(
                (v - m) / sd * w
                for v, m, sd, w in zip(
                    row["features"], reader["mean"], reader["std"], reader["weights"]
                )
            )
            + reader["bias"]
        )
        risk = 1 / (1 + math.exp(-max(-30, min(30, z))))
        assert abs(risk - row["risk"]) < 1e-10
        assert row["collapsed"] == targets[row["name"]]["collapsed"]
    for option, expected_tp in [("bank", 1), ("continue_hold", 7)]:
        rr = [r for r in measured if r["option"] == option]
        assert sum(r["collapsed"] and r["risk"] >= 0.5 for r in rr) == expected_tp
        assert not any(not r["collapsed"] and r["risk"] >= 0.5 for r in rr)
    final = load("final-training.json")
    assert (
        final["step"] == 720
        and final["successful_updates"] + final["skipped_updates"] == 720
    )
    assert final["sha256"] == experiment["final_model_sha256"]
    assert sum(final["exposures"].values()) == 720
    exposures = {
        o: 0
        for o in [
            ("bank", False),
            ("bank", True),
            ("continue_hold", False),
            ("continue_hold", True),
        ]
    }
    for r in train:
        key = f"train-{r['seed']}-{r['count']:02}-{r['option']}"
        exposures[(r["option"], r["collapsed"])] += final["exposures"][key]
    assert set(exposures.values()) == {180}
    x = datasets["final-evaluation.json"]["after-seed195"]["summary"]
    gate = (
        all(
            x[o]["mean_iou"] >= 0.5 and x[o]["generation_p95_seconds"] <= 120 for o in x
        )
        and x["continue_hold"]["tp"] >= 5
        and x["continue_hold"]["fp"] <= 3
        and x["bank"]["tp"] >= 1
        and x["bank"]["fp"] <= 4
    )
    assert experiment["final_gate_pass"] == gate
    for file in ["initial-diagnostic.json", "final-diagnostic.json"]:
        rows = load(file)
        assert len(rows) == len({(r["case"], r["option"]) for r in rows}) == 12
        assert {r["case"] for r in rows} == {
            f"{s}-{c}" for s in [68006, 68022, 68034] for c in [9, 10]
        }
        for r in rows:
            assert (
                r["future_action_tape_access"] is False
                and r["known_case_diagnostic"] is True
            )
            assert r["predicted_collapse"] == (r["risk"] >= 0.5)
            assert r["horizon_seconds"] == (
                28.4 if r["option"] == "continue_hold" else 14.2
            )
    return {
        "validation_starts": 16,
        "validation_branches_per_noise": 32,
        "final_training_movies": 78,
        "optimizer_updates": final["successful_updates"],
        "final_gate_pass": gate,
        "new_games_executed": 0,
    }


if __name__ == "__main__":
    print("PASS", json.dumps(verify()))
