#!/usr/bin/env python3
"""Train or evaluate the CPU pad-state learner on measured Gazebo RGB records."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.ship_anwm import camera_pose, NED_FROM_ENU  # noqa: E402
from scripts.capture_yokohama_pad_motion import state_evaluation_cases  # noqa: E402
from scripts.yokohama_pad_state import Model, SCHEMA, fit, sha, write  # noqa: E402


def prepare(capture, output, expected):
    config = json.loads((capture / "config.json").read_text())
    receipt = json.loads((capture / "capture-result.json").read_text())
    if (
        receipt["status"] != "passed"
        or config["motion_case_set"] != expected
        or not json.loads((capture / "cleanup.json").read_text())["removed"]
    ):
        raise ValueError("Acquisition or cleanup did not pass")
    pad = np.array(config["world"]["pad_queue"]["pad_xyz_m"])
    output.mkdir(exist_ok=False, parents=True)
    records = []
    for entry in receipt["cases"]:
        name = entry["id"]
        if Path(name).name != name:
            raise ValueError("Invalid case identity")
        folder = capture / name
        if sha(folder / "capture.json") != entry["capture_sha256"]:
            raise ValueError("Capture changed")
        record = json.loads((folder / "capture.json").read_text())
        if record["world_sha256"] != config["world"]["world_sha256"]:
            raise ValueError("Foreign world")
        split = (
            ("train" if name.startswith("train-") else "development")
            if expected == "learning-v1"
            else "unseen_test"
        )
        arrays = {k: [] for k in ("rgb", "uv", "xyz", "camera_poses", "stamps_ns", "elapsed_s")}
        sources = []
        for frame in record["frames"]:
            asset = frame["assets"]["rgb"]
            if (
                Path(asset["file"]).name != asset["file"]
                or sha(folder / asset["file"]) != asset["sha256"]
            ):
                raise ValueError("Measured RGB changed")
            rgb = Image.open(folder / asset["file"]).convert("RGB")
            if rgb.size != (640, 360):
                raise ValueError("Wrong measured RGB geometry")
            arrays["rgb"].append(np.asarray(rgb.resize((320, 180), Image.Resampling.BILINEAR)))
            rig = frame["rig_pose"]
            pose = camera_pose(
                dict(vehicle_position_enu_m=rig["xyz"], vehicle_quaternion_wxyz=rig["quat_wxyz"])
            )
            xyz = np.array(frame["lead_pose"]["xyz"])
            projected = np.linalg.inv(pose) @ np.r_[NED_FROM_ENU @ xyz, 1]
            if projected[2] <= 0:
                raise ValueError("Actor behind camera")
            arrays["uv"].append(
                [
                    277.128129 * projected[0] / projected[2] + 160,
                    277.128129 * projected[1] / projected[2] + 90,
                ]
            )
            arrays["xyz"].append(xyz - pad)
            arrays["camera_poses"].append(pose)
            arrays["stamps_ns"].append(frame["stamp_ns"])
            arrays["elapsed_s"].append(frame["elapsed_sim_s"])
            sources.append(asset["sha256"])
        arrays = {k: np.array(v) for k, v in arrays.items()}
        if not np.all(np.abs(np.diff(arrays["stamps_ns"]) - 250_000_000) <= 4_000_001):
            raise ValueError("Noncontiguous observation stream")
        np.savez_compressed(output / (name + ".npz"), **arrays)
        records.append(
            dict(
                id=name,
                split=split,
                file=name + ".npz",
                sha256=sha(output / (name + ".npz")),
                frames=len(arrays["rgb"]),
                capture_sha256=entry["capture_sha256"],
                raw_rgb_sha256=sources,
            )
        )
    write(
        output / "dataset.json",
        dict(
            schema="missionos.pad-state-data.v1",
            records=records,
            world_sha256=config["world"]["world_sha256"],
            target_role="xyz/uv are training or scoring targets only; never forecast inputs",
            aircraft_flown=False,
            native_anwm_called=False,
            acquisition_passed=True,
            owned_container_removed=True,
        ),
    )


def load_data(root, splits):
    manifest = json.loads((root / "dataset.json").read_text())
    if manifest["schema"] != "missionos.pad-state-data.v1":
        raise ValueError("Dataset schema")
    data = []
    for row in manifest["records"]:
        if row["split"] not in splits:
            continue
        if Path(row["file"]).name != row["file"] or sha(root / row["file"]) != row["sha256"]:
            raise ValueError("Dataset identity")
        with np.load(root / row["file"], allow_pickle=False) as z:
            data.append(dict(id=row["id"], **{k: z[k] for k in z.files}))
    if not data:
        raise ValueError("Empty dataset split")
    return data


def train(dataset, output):
    training = load_data(dataset, {"train"})
    development = load_data(dataset, {"development"})
    model, report = fit(training, development)
    output.mkdir(parents=True, exist_ok=False)
    np.savez_compressed(output / "model.npz", **model.values)
    weights = sha(output / "model.npz")
    write(
        output / "model.json",
        dict(
            schema=SCHEMA,
            weights_sha256=weights,
            dataset_sha256=sha(dataset / "dataset.json"),
            inference_inputs=["past RGB", "own camera pose", "timestamps"],
            model_kind="CPU learned localization and state forecast; not ANWM or VLA",
            scope="fixed camera, known visible lead, known corridor",
            flight_admitted=False,
        ),
    )
    reloaded = Model.load(output)
    sequence = development[0]
    args = (sequence["rgb"][40:56], sequence["stamps_ns"][40:56], sequence["camera_poses"][40:56])
    before, after = model.predict(*args), reloaded.predict(*args)
    for result in (before, after):
        result.pop("compute_seconds")
    if before != after:
        raise ValueError("Save/reload parity failed")
    write(
        output / "training.json",
        dict(**report, saved_reloaded_identical=True, weights_sha256=weights),
    )
    protocol = dict(
        schema="missionos.pad-state-protocol.v1",
        frozen_at=datetime.now(timezone.utc).isoformat(),
        weights_sha256=weights,
        dataset_sha256=sha(dataset / "dataset.json"),
        source_sha256={
            p: sha(Path(__file__).parent / p)
            for p in [
                "yokohama_pad_state.py",
                "run_yokohama_pad_state.py",
                "capture_yokohama_pad_motion.py",
            ]
        },
        evaluation_cases=state_evaluation_cases(),
        input_shape=[16, 180, 320, 3],
        horizon_s=4,
        timestep_s=0.25,
        evaluation_stride_s=1,
        conditions=dict(
            min_known_match=0.80,
            max_false_clear=0,
            max_mean_xyz_error_m=1.0,
            max_unknown_fraction=0.20,
            min_support_fraction=0.90,
        ),
        advisory="Review entry only when all 17 sampled states are clear; otherwise wait/reobserve.",
        capability_only=True,
        interval_collision_verified=False,
        flight_admitted=False,
        comparable_inputs="same learned image localizations for persistence and constant velocity",
        budget=dict(ceiling_usd=20, cumulative_estimate_usd=18.532818962573373, new_gpu_usd=0),
        limitations=[
            "development reused previous heldout recordings",
            "three new timing sequences only",
            "shared camera/appearance/path; correlated windows and possible repeated pixels",
            "no absence detection, action conditioning or continuous-interval guarantee",
            "no real VLA, native ANWM, AP hold, dispatch or delivery in this experiment",
        ],
    )
    write(output / "protocol.json", protocol)
    print(
        json.dumps(
            dict(
                status="trained_and_frozen",
                weights_sha256=weights,
                development=report["trials"][report["selected_trial"]],
                localization=report["localization"],
            )
        )
    )


def truth_states(xyz):
    r = np.linalg.norm(xyz[..., :2], axis=-1)
    return np.where(r < 5.2, "occupied", np.where(r > 6.8, "clear", "unknown"))


def score(rows):
    actual = np.array([row["actual_states"] for row in rows])
    predicted = np.array([row["states"] for row in rows])
    known = actual != "unknown"
    error = [
        np.linalg.norm(np.array(row["xyz"]) - np.array(row["actual_xyz"]), axis=1).tolist()
        for row in rows
        if row["xyz"] is not None
    ]
    return dict(
        windows=len(rows),
        supported_windows=sum(r["supported"] for r in rows),
        known_targets=int(known.sum()),
        known_matches=int(((predicted == actual) & known).sum()),
        known_match_fraction=float((predicted[known] == actual[known]).mean()),
        unknown_fraction=float((predicted[known] == "unknown").mean()),
        false_clear=int(((predicted == "clear") & (actual == "occupied")).sum()),
        false_clear_windows=int(
            ((predicted == "clear") & (actual == "occupied")).any(axis=1).sum()
        ),
        occupied_targets=int((actual == "occupied").sum()),
        clear_targets=int((actual == "clear").sum()),
        mean_xyz_error_m=float(np.mean(error)) if error else None,
        mean_xyz_error_by_horizon_m=np.mean(error, axis=0).tolist() if error else None,
        current_known_match_fraction=float(
            (predicted[:, 0][known[:, 0]] == actual[:, 0][known[:, 0]]).mean()
        ),
        current_mean_xyz_error_m=float(np.array(error)[:, 0].mean()) if error else None,
        support_fraction=float(np.mean([r["supported"] for r in rows])),
        mean_compute_seconds=float(np.mean([r["compute_seconds"] for r in rows])),
        p95_compute_seconds=float(np.quantile([r["compute_seconds"] for r in rows], 0.95)),
    )


def evaluate(dataset, model_dir, output):
    model = Model.load(model_dir)
    protocol = json.loads((model_dir / "protocol.json").read_text())
    if protocol["weights_sha256"] != sha(model_dir / "model.npz"):
        raise ValueError("Frozen model changed")
    sequences = load_data(dataset, {"unseen_test"})
    if [s["id"] for s in sequences] != [s["id"] for s in protocol["evaluation_cases"]]:
        raise ValueError("Unexpected evaluation cases")
    output.mkdir(parents=True, exist_ok=False)
    methods = {k: [] for k in ("learned", "persistence", "constant_velocity")}
    records = []
    for sequence in sequences:
        for i in range(15, len(sequence["rgb"]) - 16, 4):
            # Target positions and future RGB remain outside the inference API.
            prediction = model.predict(
                sequence["rgb"][i - 15 : i + 1],
                sequence["stamps_ns"][i - 15 : i + 1],
                sequence["camera_poses"][i - 15 : i + 1],
            )
            actual = sequence["xyz"][i : i + 17]
            expected = truth_states(actual).tolist()
            base = dict(
                case=sequence["id"],
                frame_index=i,
                elapsed_s=float(sequence["elapsed_s"][i]),
                stamp_ns=int(sequence["stamps_ns"][i]),
                actual_xyz=actual.tolist(),
                actual_states=expected,
                supported=prediction["supported"],
                compute_seconds=prediction["compute_seconds"],
            )
            xyz = (
                [p["xyz_relative_to_pad_m"] for p in prediction["forecasts"]]
                if prediction["supported"]
                else None
            )
            row = dict(**base, xyz=xyz, states=[p["state"] for p in prediction["forecasts"]])
            methods["learned"].append(row)
            points = np.array([d["xyz_m"] for d in prediction["detections"]])
            for method in ("persistence", "constant_velocity"):
                v = np.zeros(3) if method == "persistence" else points[-1] - points[-5]
                comparator = points[-1] + np.arange(17)[:, None] / 4 * v
                labels = (
                    truth_states(comparator) if prediction["supported"] else np.full(17, "unknown")
                )
                methods[method].append(
                    dict(
                        **base,
                        xyz=comparator.tolist() if prediction["supported"] else None,
                        states=labels.tolist(),
                    )
                )
            advisory = (
                "review_entry" if all(p == "clear" for p in row["states"]) else "wait_reobserve"
            )
            records.append(
                dict(
                    case=sequence["id"],
                    frame_index=i,
                    elapsed_s=base["elapsed_s"],
                    forecast=prediction,
                    actual_xyz=actual.tolist(),
                    actual_states=expected,
                    advisory=advisory,
                    dispatched=False,
                )
            )
    totals = {k: score(v) for k, v in methods.items()}
    per_case = {
        s["id"]: {k: score([r for r in v if r["case"] == s["id"]]) for k, v in methods.items()}
        for s in sequences
    }
    c, result = protocol["conditions"], totals["learned"]
    checks = dict(
        known_match=result["known_match_fraction"] >= c["min_known_match"],
        false_clear=result["false_clear"] <= c["max_false_clear"],
        mean_xyz_error=result["mean_xyz_error_m"] is not None
        and result["mean_xyz_error_m"] <= c["max_mean_xyz_error_m"],
        unknown_fraction=result["unknown_fraction"] <= c["max_unknown_fraction"],
        support_fraction=result["support_fraction"] >= c["min_support_fraction"],
    )
    traces = []
    for s in sequences:
        rows = [r for r in records if r["case"] == s["id"]]
        transitions = [
            dict(elapsed_s=r["elapsed_s"], advisory=r["advisory"])
            for j, r in enumerate(rows)
            if j == 0 or r["advisory"] != rows[j - 1]["advisory"]
        ]
        safe = all(
            "occupied" not in r["actual_states"] for r in rows if r["advisory"] == "review_entry"
        )
        switch = any(
            a["advisory"] == "wait_reobserve" and b["advisory"] == "review_entry"
            for a, b in zip(transitions, transitions[1:])
        )
        traces.append(
            dict(
                case=s["id"],
                transitions=transitions,
                no_occupied_review=safe,
                wait_then_review=switch,
                dispatch_invoked=False,
            )
        )
    checks["advisory_transition_all_cases"] = all(
        t["wait_then_review"] and t["no_occupied_review"] for t in traces
    )
    report = dict(
        schema="missionos.pad-state-evaluation.v1",
        status="passed" if all(checks.values()) else "failed",
        checks=checks,
        totals=totals,
        per_case=per_case,
        traces=traces,
        weights_sha256=sha(model_dir / "model.npz"),
        protocol_sha256=sha(model_dir / "protocol.json"),
        dataset_sha256=sha(dataset / "dataset.json"),
        flight_admitted=False,
        native_anwm_improved=False,
        vla_used=False,
        gpu_used=False,
        comparison_role="diagnostic only; no requirement to beat ideal Rules",
    )
    write(output / "evaluation.json", report)
    write(output / "predictions.json", records)
    print(json.dumps(report))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("prepare")
    a.add_argument("--capture", type=Path, required=True)
    a.add_argument("--output", type=Path, required=True)
    a.add_argument("--case-set", choices=["learning-v1", "state-eval-v1"], required=True)
    a = sub.add_parser("train")
    a.add_argument("--dataset", type=Path, required=True)
    a.add_argument("--output", type=Path, required=True)
    a = sub.add_parser("evaluate")
    a.add_argument("--dataset", type=Path, required=True)
    a.add_argument("--model", type=Path, required=True)
    a.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.command == "prepare":
        prepare(a.capture, a.output, a.case_set)
    elif a.command == "train":
        train(a.dataset, a.output)
    else:
        evaluate(a.dataset, a.model, a.output)


if __name__ == "__main__":
    main()
