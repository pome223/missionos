#!/usr/bin/env python3
"""CPU-only input audit and supplementary scoring; never changes the frozen gate.

``freeze`` groups inputs without reading forecasts. ``score`` reruns the original
evaluator unchanged and reports the frozen subgroups alongside its full result.
Neither command changes training inputs, starts a GPU or permits flight.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.evaluate_yokohama_pad_wam_rule import evaluate  # noqa: E402
from scripts.yokohama_pad_wam_study import sha, write  # noqa: E402


def digest(array):
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def audit(staged):
    protocol_path = staged / "rule-protocol.json"
    dataset_path = staged / "payload/dataset.json"
    protocol = json.loads(protocol_path.read_text())
    data = json.loads(dataset_path.read_text())
    if sha(dataset_path) != protocol["dataset_sha256"]:
        raise ValueError("Dataset differs from the frozen GPU protocol")
    samples = data["samples"]
    ids = [s["id"] for s in samples]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate sample ids")
    moving, static = protocol["moving_ids"], protocol["static_ids"]
    if len(moving + static) != len(set(moving + static)):
        raise ValueError("Duplicate or overlapping evaluation ids")
    if not set(moving + static) <= set(ids):
        raise ValueError("Missing evaluation samples")
    by_file = defaultdict(list)
    for s in samples:
        by_file[s["frames"]].append(s)
    train_histories = Counter()
    train_pairs = Counter()
    target_groups = defaultdict(set)
    observations = {}
    for rel, group in sorted(by_file.items()):
        path = staged / "payload" / rel
        if not path.resolve().is_relative_to((staged / "payload").resolve()):
            raise ValueError("Unsafe history path")
        if sha(path) != data["assets"][rel]:
            raise ValueError("Changed history asset")
        with np.load(path, allow_pickle=False) as a:
            rgb, stamps = a["rgb"], a["stamps_ns"]
        if rgb.dtype != np.uint8 or rgb.shape[1:] != (224, 224, 3):
            raise ValueError("Unexpected image representation")
        for s in group:
            last = s["last_index"]
            if not 15 <= last < len(rgb) or len(stamps) != len(rgb):
                raise ValueError("Invalid history indices")
            if int(stamps[last]) != s["cutoff_stamp_ns"]:
                raise ValueError("History cutoff mismatch")
            history = rgb[last - 15 : last + 1]
            h = digest(history)
            key = (h, s["offset"])
            if s["split"] == "train":
                if not last < last + s["offset"] < len(rgb):
                    raise ValueError("Missing training target")
                train_histories[h] += 1
                train_pairs[key] += 1
                target_groups[key].add(digest(rgb[last + s["offset"]]))
            elif s["id"] in moving + static:
                if s["split"] != "val" or last != 15 or len(rgb) != 16:
                    raise ValueError("Evaluation must contain only 16 validation frames")
                if np.any(stamps > s["cutoff_stamp_ns"]):
                    raise ValueError("Evaluation includes a future observation")
                observations[s["id"]] = dict(
                    sequence=s["sequence"],
                    history_content_sha256=h,
                    offset=s["offset"],
                    history_all_frames_identical=bool(np.all(history == history[-1:])),
                )
            else:
                raise ValueError("Unexpected non-training sample")
    for o in observations.values():
        h = o["history_content_sha256"]
        o["matching_training_history_rows"] = train_histories[h]
        o["matching_training_input_rows"] = train_pairs[(h, o["offset"])]
    constant = [i for i in moving if observations[i]["history_all_frames_identical"]]
    changing = [i for i in moving if not observations[i]["history_all_frames_identical"]]
    return dict(
        schema="pad_wam_rule_input_audit.v1",
        protocol_sha256=sha(protocol_path),
        dataset_sha256=sha(dataset_path),
        assets_verified=True,
        training=dict(
            pairs=sum(s["split"] == "train" for s in samples),
            sequences=len({s["sequence"] for s in samples if s["split"] == "train"}),
            unique_history_content=len(train_histories),
            unique_history_and_horizon=len(train_pairs),
            identical_input_different_target_groups=sum(len(v) > 1 for v in target_groups.values()),
        ),
        groups=dict(
            all_moving=moving,
            constant_history=constant,
            changing_history=changing,
            static_reference=static,
        ),
        observations=observations,
    )


def freeze(staged, output):
    value = dict(
        schema="pad_wam_rule_supplement_plan.v1",
        created_at=datetime.now(timezone.utc).isoformat(),
        selection="Exact equality of the 16 input images; no forecast or future selects a group",
        grouping_uses_forecasts=False,
        original_gate_changed=False,
        supplementary_gate_defined=False,
        audit=audit(staged),
        limits=[
            "Changing pixels alone do not prove useful motion reasoning.",
            "Constant observed history does not specify a scripted future departure time.",
            "Separate sequence ids do not guarantee unseen image content.",
            "This run has no training-pair forecast evaluation; fitting and generalization failures remain distinct possibilities.",
            "Three-second offline forecasts are not live delivery authorization.",
        ],
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as f:
        json.dump(value, f, indent=2, allow_nan=False)
        f.write("\n")
    return value


def numeric(value):
    return math.inf if value == "missed" else float(value)


def median(values):
    if not values:
        return None
    value = statistics.median(values)
    return "missed" if math.isinf(value) else value


def summarize(rows, ids, stage, history, seed, observations):
    selected = [
        r
        for r in rows
        if r["id"] in ids and (r["stage"], r["history"], r["seed"]) == (stage, history, seed)
    ]
    observed = {r["id"] for r in selected}
    return dict(
        expected=len(ids),
        observed=len(selected),
        missing_ids=[i for i in ids if i not in observed],
        sequences=len({observations[i]["sequence"] for i in ids}),
        readable=sum(r["present"] for r in selected),
        unreadable=sum(not r["present"] for r in selected),
        median_error_including_misses_px=median([numeric(r["model"]) for r in selected]),
        median_persistence_px=median([numeric(r["persistence"]) for r in selected]),
        median_linear_extrapolation_px=median([numeric(r["extrapolation"]) for r in selected]),
        nearer_than_persistence=sum(
            numeric(r["model"]) < numeric(r["persistence"]) for r in selected
        ),
        nearer_than_linear=sum(numeric(r["model"]) < numeric(r["extrapolation"]) for r in selected),
    )


def score(staged, prepared, results, plan_path):
    plan = json.loads(plan_path.read_text())
    if plan.get("schema") != "pad_wam_rule_supplement_plan.v1" or plan["audit"] != audit(staged):
        raise ValueError("Supplement plan or inputs changed; refusing regrouping")
    summary = json.loads((results / "summary.json").read_text())
    if summary.get("protocol_sha256") != plan["audit"]["protocol_sha256"]:
        raise ValueError("Results belong to a different frozen protocol")
    # The production evaluator and its acceptance rule are deliberately untouched.
    original = evaluate(staged, prepared, results)
    value = dict(
        schema="pad_wam_rule_supplement_evaluation.v1",
        plan_sha256=sha(plan_path),
        truth_sha256=sha(prepared / "truth.json"),
        original_evaluator_sha256=sha(
            Path(__file__).with_name("evaluate_yokohama_pad_wam_rule.py")
        ),
        original_gate_changed=False,
        original_evaluation=original,
        supplementary=dict(status="not_scored_incomplete_run", gate_defined=False),
    )
    if not original["complete"]:
        return value
    protocol = json.loads((staged / "rule-protocol.json").read_text())
    rows = original["rows"]
    observations = plan["audit"]["observations"]
    final = f"ckpt-{protocol['checkpoints'][-1]:05d}"
    groups = {}
    for name, ids in plan["audit"]["groups"].items():
        stages = {
            f"ckpt-{c:05d}": summarize(
                rows, ids, f"ckpt-{c:05d}", "moving", protocol["seed"], observations
            )
            for c in protocol["checkpoints"]
        }
        entry = dict(ids=ids, learning_curve=stages)
        if name != "static_reference":
            entry["final_second_seed"] = summarize(
                rows, ids, final, "moving", protocol["second_seed"], observations
            )
            entry["final_still_history"] = summarize(
                rows, ids, final, "still", protocol["seed"], observations
            )
            paired = {}
            for r in rows:
                if r["id"] in ids and r["stage"] == final and r["seed"] == protocol["seed"]:
                    paired.setdefault(r["id"], {})[r["history"]] = numeric(r["model"])
            entry["history_swap_pairs"] = dict(
                expected=len(ids),
                moving_better=sum(v["moving"] < v["still"] for v in paired.values()),
                still_better=sum(v["still"] < v["moving"] for v in paired.values()),
                equal=sum(v["still"] == v["moving"] for v in paired.values()),
                both_unreadable=sum(
                    math.isinf(v["still"]) and math.isinf(v["moving"]) for v in paired.values()
                ),
            )
        entry["final_by_sequence"] = {
            sequence: summarize(
                rows,
                [i for i in ids if observations[i]["sequence"] == sequence],
                final,
                "moving",
                protocol["seed"],
                observations,
            )
            for sequence in sorted({observations[i]["sequence"] for i in ids})
        }
        groups[name] = entry
    value["supplementary"] = dict(status="completed", gate_defined=False, groups=groups)
    return value


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    for name in ("freeze", "score"):
        sub = commands.add_parser(name)
        sub.add_argument("--staged", type=Path, required=True)
        sub.add_argument("--output", type=Path, required=True)
        if name == "score":
            sub.add_argument("--prepared", type=Path, required=True)
            sub.add_argument("--results", type=Path, required=True)
            sub.add_argument("--plan", type=Path, required=True)
    a = p.parse_args()
    if a.command == "freeze":
        value = freeze(a.staged, a.output)
        print(
            json.dumps(
                dict(
                    training=value["audit"]["training"],
                    groups={k: len(v) for k, v in value["audit"]["groups"].items()},
                    gpu_requested=False,
                )
            )
        )
    else:
        value = score(a.staged, a.prepared, a.results, a.plan)
        a.output.parent.mkdir(parents=True, exist_ok=True)
        write(a.output, value)
        print(
            json.dumps(
                dict(
                    original_passed=value["original_evaluation"]["passed"],
                    supplementary_status=value["supplementary"]["status"],
                    gpu_requested=False,
                )
            )
        )


if __name__ == "__main__":
    main()
