#!/usr/bin/env python3
"""Opt-in CPU reentry capture and post-training; no vehicle dispatch or GPU."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.run_yokohama_pad_state import load_data, prepare  # noqa: E402
from scripts.yokohama_pad_state import Model, SCHEMA, fit, sha, write  # noqa: E402


def cases():
    return [
        dict(
            id="train-return",
            split="train",
            knots=[[0, "end"], [10, "end"], [25, "up"], [39, "up"], [53, "end"], [60, "end"]],
            cutoffs=[],
        ),
        dict(
            id="development-return",
            split="development",
            knots=[[0, "end"], [8, "end"], [25, "up"], [39, "up"], [54, "end"], [62, "end"]],
            cutoffs=[],
        ),
        dict(
            id="test-return-fast",
            split="unseen_test",
            knots=[[0, "end"], [12, "end"], [25, "up"], [39, "up"], [53, "end"], [61, "end"]],
            cutoffs=[],
        ),
        dict(
            id="test-return-slow",
            split="unseen_test",
            knots=[[0, "end"], [7, "end"], [26, "up"], [40, "up"], [56, "end"], [64, "end"]],
            cutoffs=[],
        ),
        dict(
            id="test-depart-control",
            split="unseen_test",
            knots=[[0, "start"], [8, "start"], [16, "up"], [32, "end"], [54, "end"]],
            cutoffs=[],
        ),
    ]


def followup_cases():
    return [
        dict(
            id="test-risk-return-medium",
            split="unseen_test",
            knots=[[0, "end"], [9, "end"], [23.5, "up"], [37.5, "up"], [52, "end"], [61, "end"]],
            cutoffs=[],
        ),
        dict(
            id="test-risk-return-slow",
            split="unseen_test",
            knots=[[0, "end"], [12, "end"], [30.5, "up"], [44.5, "up"], [58, "end"], [66, "end"]],
            cutoffs=[],
        ),
        dict(
            id="test-risk-depart",
            split="unseen_test",
            knots=[[0, "start"], [7, "start"], [15, "up"], [30, "end"], [54, "end"]],
            cutoffs=[],
        ),
    ]


def freeze_followup(root, parent):
    root.mkdir(parents=True, exist_ok=False)
    protocol = json.loads((parent / "protocol.json").read_text())
    protocol.update(
        schema="missionos.pad-reentry-risk-protocol.v1",
        frozen_at=datetime.now(timezone.utc).isoformat(),
        cases=followup_cases(),
        risk_followup=True,
        risk_source_sha256={
            name: sha(REPO / name)
            for name in [
                "src/runtime/yokohama_pad_reentry_risk.py",
                "src/runtime/yokohama_pad_advisory_contract.py",
                "scripts/yokohama_pad_advisory_host.py",
            ]
        },
        training_protocol_sha256=sha(parent / "protocol.json"),
        training="No further training; reuse the post-trained model frozen before the first evaluation",
        evaluation="Fresh timings not read when adding the risk policy; old evaluations remain diagnosis/regression",
        risk_policy=dict(
            inward_trend_m_per_s=0.25,
            forecast_center_enters_current_pad_radius=True,
            supported_history_required=True,
            occupancy_labels_unchanged=True,
            unknown_only_is_not_a_warning=True,
            current_rules_unchanged=True,
            retained_after_horizon=False,
            max_recheck_interval_sim_s=2,
        ),
    )
    write(root / "protocol.json", protocol)
    shutil.copy2(parent / "protocol.json", root / "training-protocol.json")
    shutil.copy2(parent / "admission.json", root / "admission.json")
    timeline = json.loads((parent / "replay-protocol.json").read_text())
    timeline["frozen_at"] = datetime.now(timezone.utc).isoformat()
    write(root / "replay-protocol.json", timeline)
    shutil.copytree(parent / "model", root / "model")
    print(
        json.dumps(dict(status="risk_followup_frozen", protocol_sha256=sha(root / "protocol.json")))
    )


def freeze_replay(root):
    write(
        root / "replay-protocol.json",
        dict(
            schema="missionos.pad-reentry-replay.v1",
            frozen_at=datetime.now(timezone.utc).isoformat(),
            arrival_times_sim_s=[8, 10, 12, 14, 16],
            episode_decision_stride_s=2,
            first_decision_after_arrival_s=5,
            ready_window_s=5,
            horizon_s=4,
            selection="all predeclared arrivals for every new case, not selected after forecasts",
            boundary="synthetic held own aircraft with measured lead trajectory and RGB; stop replay episode at first permitted entry; no dispatch or physical outcome",
            endpoint="next four seconds sampled pad or approach reoccupation after first permission; not collision or mission completion",
            no_arrival_phase_warmup=True,
        ),
    )
    from scripts.evaluate_yokohama_pad_reentry import observation, clear, WAIT, ENTER
    from src.runtime.yokohama_pad_queue import make_request, propose

    previous = REPO / "docs/examples/yokohama-pad-state"
    config = json.loads((previous / "test-capture/config.json").read_text())
    config["operator_approval"] = "offline fixture only"
    episodes = []
    for sequence in load_data(previous / "test-data", {"unseen_test"}):
        if sequence["id"] != "state-reenter":
            continue
        rows = [observation(config, sequence, i) for i in range(len(sequence["rgb"]))]
        flags = [clear(config, row) for row in rows]
        records = []
        for i in range(23, len(rows) - 16, 4):
            request = make_request(
                config, i, rows[i - 20 : i + 1] if all(flags[i - 20 : i + 1]) else [rows[i]]
            )
            action = propose(config, request)["proposed_action"]
            future = not all(flags[i + 1 : i + 17])
            records.append(
                dict(
                    t=float(sequence["elapsed_s"][i]),
                    current=action,
                    oracle=WAIT if future else action,
                    reoccupied=future,
                )
            )
        for arrival in [8, 10, 12, 14, 16]:
            candidates = [r for r in records if r["t"] >= arrival + 5][::2]
            first = {
                m: next((r for r in candidates if r[m] == ENTER), None)
                for m in ["current", "oracle"]
            }
            episodes.append(dict(arrival=arrival, first=first))
    write(
        root / "admission.json",
        dict(
            source="previous state-reenter diagnostic only, not new heldout result",
            episodes=episodes,
            oracle_avoids=sum(
                r["first"]["current"]["reoccupied"] and not r["first"]["oracle"]["reoccupied"]
                for r in episodes
            ),
        ),
    )


def freeze(root):
    root.mkdir(parents=True, exist_ok=False)
    protocol = dict(
        schema="missionos.pad-reentry-protocol.v1",
        frozen_at=datetime.now(timezone.utc).isoformat(),
        cases=cases(),
        existing_weights_sha256=sha(REPO / "docs/examples/yokohama-pad-state/model/model.npz"),
        endpoints=[
            "advisory changes current-Rules entry to wait before measured reoccupation",
            "false additional waits",
            "first permitted entry and next 4 seconds measured occupancy",
        ],
        comparison=[
            "current Rules",
            "existing learned model",
            "post-trained model",
            "constant velocity from the same image localizations",
            "measured-future oracle for scoring only",
        ],
        candidate_actions=["wait_at_current_hold", "enter_delivery_approach"],
        decision_stride_s=1,
        current_clear_window_s=5,
        future_horizon_s=4,
        training="Existing train split plus train-return; development selection uses existing development plus development-return only",
        evaluation="Three new timings withheld from fitting and selection; older state-eval data are regression only",
        adoption="No fixed 90% threshold and no requirement to beat ideal Rules; report practical changed decisions and regressions, not flight benefit from replay",
        headroom="Reentry can interrupt an entry despite 5 seconds of past clearance. Independent current Rules remain mandatory. Individual decision opportunities and first-entry timelines are both scored; correlated windows are not independent missions.",
        limits=[
            "fixed camera and known visible lead/corridor",
            "scripted lead; no second autopilot",
            "fixture own-aircraft hold for decision replay",
            "no native ANWM, VLA, dispatch, cargo flight or battery measurement",
        ],
        budget=dict(new_gpu_usd=0, cumulative_estimate_usd=18.532818962573373, ceiling_usd=20),
    )
    write(root / "protocol.json", protocol)
    freeze_replay(root)
    print(json.dumps(dict(status="frozen", protocol_sha256=sha(root / "protocol.json"))))


def capture(root, source, approved):
    if not approved:
        raise ValueError("Explicit --approve-sitl required")
    protocol = json.loads((root / "protocol.json").read_text())
    if protocol["cases"] != (followup_cases() if protocol.get("risk_followup") else cases()):
        raise ValueError("Changed frozen cases")
    if json.loads((source / "capture-result.json").read_text())["status"] != "passed":
        raise ValueError("Unqualified source camera rig")
    target = root / "capture"
    target.mkdir(exist_ok=False)
    for name in ("assets", "models"):
        shutil.copytree(source / name, target / name, symlinks=True)
    for name in (
        "capture_yokohama_pad_motion.py",
        "yokohama_sitl_worker.py",
        "ship_urban_camera_worker.py",
    ):
        shutil.copy2(REPO / "scripts" / name, target / name)
    config = json.loads((source / "config.json").read_text())
    config.update(
        run_id="pad-reentry-" + uuid4().hex[:12],
        motion_cases=protocol["cases"],
        motion_case_set="reentry-v1",
    )
    config["decisions"] = dict(backend="camera-recording-only")
    if sha(target / "models/worlds/default.sdf") != config["world"]["world_sha256"]:
        raise ValueError("Source world changed")
    write(target / "config.json", config)
    name = "missionos-" + config["run_id"]
    image = subprocess.run(
        ["docker", "image", "inspect", "px4io/px4-sitl-gazebo:latest", "--format", "{{.Id}}"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    created = False
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                "none",
                "--cpus",
                "4",
                "--memory",
                "4g",
                "--entrypoint",
                "sh",
                "-v",
                str(target.resolve()) + ":/mission",
                "-e",
                "LIBGL_ALWAYS_SOFTWARE=1",
                "-e",
                "GZ_SIM_RESOURCE_PATH=/mission/models:/opt/px4-gazebo/share/gz/models",
                image,
                "-c",
                "gz sim -r -s --headless-rendering /mission/models/worlds/default.sdf",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        created = True
        inspected = json.loads(
            subprocess.run(
                ["docker", "inspect", name], check=True, capture_output=True, text=True
            ).stdout
        )[0]
        write(
            target / "runtime.json",
            dict(
                image=image,
                network=inspected["HostConfig"]["NetworkMode"],
                device_requests=inspected["HostConfig"].get("DeviceRequests"),
                cpu_only=True,
                software_rendering=True,
                aircraft_flown=False,
            ),
        )
        time.sleep(5)
        with (
            (target / "worker.stdout").open("w") as out,
            (target / "worker.stderr").open("w") as err,
        ):
            subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "python3",
                    "-u",
                    "/mission/capture_yokohama_pad_motion.py",
                    "--worker",
                ],
                check=True,
                stdout=out,
                stderr=err,
                timeout=1700,
            )
    finally:
        if created:
            log = subprocess.run(
                ["docker", "logs", name], text=True, capture_output=True, timeout=20
            )
            (target / "gazebo.log").write_text(log.stdout + log.stderr)
            subprocess.run(
                ["docker", "rm", "-f", name], check=True, capture_output=True, timeout=30
            )
            absent = subprocess.run(["docker", "inspect", name], capture_output=True, timeout=15)
            write(
                target / "cleanup.json", dict(owned_container=name, removed=absent.returncode != 0)
            )
    prepare(target, root / "data", "reentry-v1")
    path = root / "data/dataset.json"
    data = json.loads(path.read_text())
    split = {c["id"]: c["split"] for c in protocol["cases"]}
    for item in data["records"]:
        item["split"] = split[item["id"]]
    write(path, data)
    print(json.dumps(dict(status="captured", frames=sum(r["frames"] for r in data["records"]))))


def train(root):
    previous = REPO / "docs/examples/yokohama-pad-state"
    protocol = json.loads((root / "protocol.json").read_text())
    if sha(previous / "model/model.npz") != protocol["existing_weights_sha256"]:
        raise ValueError("Original weights changed")
    training = load_data(previous / "development-data", {"train"}) + load_data(
        root / "data", {"train"}
    )
    development = load_data(previous / "development-data", {"development"}) + load_data(
        root / "data", {"development"}
    )
    model, report = fit(training, development)
    target = root / "model"
    target.mkdir(exist_ok=False)
    np.savez_compressed(target / "model.npz", **model.values)
    write(
        target / "model.json",
        dict(
            schema=SCHEMA,
            weights_sha256=sha(target / "model.npz"),
            model_kind="CPU learned localization and state forecast; not ANWM or VLA",
            scope="fixed camera, known visible lead, known corridor",
            flight_admitted=False,
        ),
    )
    report.update(
        training_cases=[x["id"] for x in training],
        development_cases=[x["id"] for x in development],
        protocol_sha256=sha(root / "protocol.json"),
        weights_sha256=sha(target / "model.npz"),
        frozen_at=datetime.now(timezone.utc).isoformat(),
        evaluation_accessed=False,
    )
    # No new evaluation records are loaded before these weights are frozen.
    reloaded = Model.load(target)
    s = development[-1]
    args = (s["rgb"][24:40], s["stamps_ns"][24:40], s["camera_poses"][24:40])
    a, b = model.predict(*args), reloaded.predict(*args)
    a.pop("compute_seconds")
    b.pop("compute_seconds")
    if a != b:
        raise ValueError("Reload differs")
    report["saved_reloaded_identical"] = True
    write(target / "training.json", report)
    print(
        json.dumps(
            dict(
                status="post_trained",
                weights_sha256=report["weights_sha256"],
                training_seconds=report["training_seconds"],
            )
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["freeze", "capture", "train", "freeze-followup"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-rig", type=Path)
    parser.add_argument("--parent", type=Path)
    parser.add_argument("--approve-sitl", action="store_true")
    args = parser.parse_args()
    if args.phase == "freeze-followup":
        if args.parent is None:
            parser.error("--parent required")
        freeze_followup(args.root, args.parent)
    elif args.phase == "freeze":
        freeze(args.root)
    elif args.phase == "capture":
        if args.source_rig is None:
            parser.error("--source-rig required")
        capture(args.root, args.source_rig, args.approve_sitl)
    else:
        train(args.root)


if __name__ == "__main__":
    main()
