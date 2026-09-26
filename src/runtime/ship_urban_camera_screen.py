"""Opt-in RGB perception screen and read-only archived-evidence verification."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from src.runtime.runtime_claim_evidence import validate_runtime_invocation_evidence
from src.runtime.ship_delivery_sitl import IMAGE, _run
from src.runtime.ship_urban_camera import (
    FOCAL_PX,
    POLICIES,
    EXTRA_DETOUR_S,
    choose_camera_action,
    obstacle_pixels,
)
from src.runtime.ship_urban_camera_scene import (
    CASES,
    SAMPLE_TIMES,
    actor_x,
    analytic_clearance_time,
    camera_world,
    capture_config,
)


ANALYSIS_SOURCES = (
    "ship_urban_camera.py",
    "ship_urban_camera_scene.py",
    "ship_urban_camera_screen.py",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_camera_records(root):
    """Recompute tracks from pixels. Future validation frames never enter policy."""
    config = json.loads((root / "capture-config.json").read_text())
    if config != capture_config(config["run_id"]):
        raise ValueError("Script or camera capture configuration mismatch")
    rows = [json.loads(line) for line in (root / "frames.jsonl").read_text().splitlines()]
    if len(rows) != len(CASES) * len(SAMPLE_TIMES):
        raise ValueError("Incomplete camera sequence")
    results = []
    for case_index, case in enumerate(CASES):
        frames = rows[case_index * 7 : (case_index + 1) * 7]
        tracks = []
        epoch = frames[0]["sensor_stamp_s"]
        for index, (row, t) in enumerate(zip(frames, SAMPLE_TIMES)):
            expected_file = f"{case}-{index:02}.png"
            if (
                row["case"] != case
                or row["index"] != index
                or row["file"] != expected_file
                or row["run_id"] != config["run_id"]
                or row["scripted_at_s"] != t
                or row["source"] != "gz.msgs.Image"
                or row["clock"] != "gazebo_simulation"
                or row["frame_role"] != ("decision" if index < 5 else "validation_only")
            ):
                raise ValueError("Camera frame identity, role or source mismatch")
            if not all(
                type(row[k]) in (int, float) and math.isfinite(row[k])
                for k in ("sensor_stamp_s", "observed_at_s")
            ):
                raise ValueError("Invalid camera clock")
            if abs(row["sensor_stamp_s"] - epoch - row["observed_at_s"]) > 1e-6:
                raise ValueError("Camera clock not bound to sensor timestamp")
            if abs(row["observed_at_s"] - t) > 0.15:
                raise ValueError("Camera frame does not match staged observation time")
            path = root / expected_file
            if digest(path) != row["sha256"]:
                raise ValueError("Camera image hash mismatch")
            detection = obstacle_pixels(path)
            # Evaluation only: staged geometry never enters the policy history.
            # Box silhouette can differ slightly from its near-face center.
            reference_px = 320 + FOCAL_PX * actor_x(case, t) / 96
            if abs(detection["center_x_px"] - reference_px) > 3:
                raise ValueError("Rendered obstacle disagrees with calibrated staged position")
            tracks.append(
                {
                    "observed_at_s": row["observed_at_s"],
                    **detection,
                    "image_sha256": row["sha256"],
                    "file": expected_file,
                }
            )
        # The only policy inputs, identically shared by all policies. Images at
        # 4s and 8s are deliberately retained separately as post-decision checks.
        history = [{k: row[k] for k in ("observed_at_s", "center_x_px")} for row in tracks[:5]]
        proposals = {p: choose_camera_action(history, p) for p in POLICIES}
        decision_t = SAMPLE_TIMES[4]
        costs = {
            "wait": max(0, analytic_clearance_time(case) - decision_t),
            "detour": EXTRA_DETOUR_S,
        }
        best = min(costs, key=costs.get)
        results.append(
            {
                "case": case,
                "history": history,
                "decision_frames": tracks[:5],
                "validation_frames": tracks[5:],
                "max_calibration_error_px": max(
                    abs(r["center_x_px"] - (320 + FOCAL_PX * actor_x(case, t) / 96))
                    for r, t in zip(tracks, SAMPLE_TIMES)
                ),
                "proposals": proposals,
                "analytic_extra_seconds": costs,
                "offline_oracle_action": best,
                "analytic_regret_s": {
                    p: costs[v["action_proposal"]] - costs[best] for p, v in proposals.items()
                },
            }
        )
    regrets = {p: sum(r["analytic_regret_s"][p] for r in results) / len(results) for p in POLICIES}
    return {
        "schema_version": "ship_urban_camera_screen.v1",
        "status": "screened",
        "scope": "stepped_fixed_camera_perception_with_analytic_costs",
        "run_id": config["run_id"],
        "captured_frames": len(rows),
        "cases": results,
        "mean_analytic_regret_s": regrets,
        "strongest_simple_baseline": min(regrets, key=regrets.get),
        "oracle_headroom_over_best_simple_s": min(regrets.values()),
        "learned_model_comparison_admitted": False,
        "gazebo_runtime_invoked": True,
        "px4_runtime_invoked": False,
        "physical_execution_invoked": False,
        "vla_invoked": False,
        "wam_invoked": False,
        "simulation_mission_completed": False,
        "dispatch_authorized": False,
        "limitations": [
            "Actual Gazebo RGB images from one fixed entry camera, not an onboard flight camera.",
            "Actor poses are staged with a stepped simulation clock; this is not continuous flight video.",
            "Color segmentation and a known planar-depth calibration are synthetic-scene assumptions.",
            "Candidate costs use hidden scripts only in offline evaluation; no new flight duration is measured.",
            "Five pre-decision frames per case; later validation images are excluded from every policy.",
            "Three development conditions, no held-out evaluation or learned-model efficacy evidence.",
        ],
    }


def verify_camera_screen(directory):
    root = Path(directory).resolve()
    report = json.loads((root / "result.json").read_text())
    evidence = validate_runtime_invocation_evidence(report["runtime_invocation_evidence"])
    if (
        evidence["invocation_exit_code"] != 0
        or evidence["run_id"] != report["run_id"]
        or evidence.get("execution_scope") != "sim"
    ):
        raise ValueError("Unsuccessful or mismatched camera invocation")
    required = {"world.sdf", "capture-config.json", "worker.py", "frames.jsonl"}
    required.update(ANALYSIS_SOURCES)
    required.update(f"{case}-{i:02}.png" for case in CASES for i in range(7))
    hashes = report["artifact_sha256"]
    if set(hashes) != required or any(digest(root / name) != h for name, h in hashes.items()):
        raise ValueError("Camera source artifacts are missing or modified")
    if evidence["worker_sha256"] != hashes["worker.py"]:
        raise ValueError("Worker hash is not bound to invocation")
    if any(digest(Path(__file__).with_name(name)) != hashes[name] for name in ANALYSIS_SOURCES):
        raise ValueError("Reverify using the recorded analysis code revision")
    if (root / "world.sdf").read_text() != camera_world():
        raise ValueError("World differs from the declared camera calibration")
    recalculated = inspect_camera_records(root)
    if any(report.get(k) != value for k, value in recalculated.items()):
        raise ValueError("Camera report does not match recomputed pixels and decisions")
    return {**recalculated, "archived_evidence_reverified": True}


def run_camera_screen(*, output_dir, operator_approved=False, timeout_s=180):
    if operator_approved is not True:
        raise PermissionError("Explicit --approve-gazebo is required")
    if not math.isfinite(timeout_s) or not 30 <= timeout_s <= 600:
        raise ValueError("Camera timeout must be between 30 and 600 seconds")
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    run_id = uuid4().hex
    container = "missionos-camera-" + run_id[:12]
    report = {
        "schema_version": "ship_urban_camera_screen.v1",
        "run_id": run_id,
        "status": "blocked",
        "blocking_reasons": [],
        "container": container,
        "gazebo_runtime_invoked": False,
        "px4_runtime_invoked": False,
        "physical_execution_invoked": False,
        "vla_invoked": False,
        "wam_invoked": False,
        "simulation_mission_completed": False,
        "dispatch_authorized": False,
        "learned_model_comparison_admitted": False,
    }
    try:
        if shutil.disk_usage(root).free < 300 * 1024**2:
            raise ValueError("Camera capture needs 300 MiB free disk space")
        report["image_id"] = _run(
            ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"]
        ).stdout.strip()
        (root / "world.sdf").write_text(camera_world())
        config = capture_config(run_id)
        (root / "capture-config.json").write_text(json.dumps(config, indent=2))
        shutil.copyfile(
            Path(__file__).resolve().parents[2] / "scripts/ship_urban_camera_worker.py",
            root / "worker.py",
        )
        for name in ANALYSIS_SOURCES:
            shutil.copyfile(Path(__file__).with_name(name), root / name)
        started = datetime.now(timezone.utc).isoformat()
        code = None
        try:
            with (
                (root / "worker.stdout").open("w") as out,
                (root / "worker.stderr").open("w") as err,
            ):
                result = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--name",
                        container,
                        "--network",
                        "none",
                        "--memory",
                        "2g",
                        "--cpus",
                        "2",
                        "--entrypoint",
                        "python3",
                        "-e",
                        "LIBGL_ALWAYS_SOFTWARE=1",
                        "-v",
                        f"{root}:/mission",
                        report["image_id"],
                        "-u",
                        "/mission/worker.py",
                    ],
                    stdout=out,
                    stderr=err,
                    timeout=timeout_s,
                )
                code = result.returncode
        finally:
            invocation = {
                "schema_version": "runtime_invocation_evidence.v1",
                "invocation_kind": "subprocess",
                "invocation_target": f"{container}:python3 /mission/worker.py",
                "run_id": run_id,
                "execution_scope": "sim",
                "invocation_started_at": started,
                "invocation_completed_at": datetime.now(timezone.utc).isoformat(),
                "invocation_exit_code": code if code is not None else -1,
                "worker_sha256": digest(root / "worker.py"),
            }
            for stream in ("stdout", "stderr"):
                invocation[f"{stream}_artifact_path"] = str(root / f"worker.{stream}")
                invocation[f"invocation_{stream}_sha256"] = digest(root / f"worker.{stream}")
            report["runtime_invocation_evidence"] = validate_runtime_invocation_evidence(invocation)
        if code != 0:
            raise RuntimeError("Camera worker failed; see worker.stderr and simulator.log")
        report.update(inspect_camera_records(root))
        names = ["world.sdf", "capture-config.json", "worker.py", "frames.jsonl"]
        names.extend(ANALYSIS_SOURCES)
        names.extend(f"{case}-{i:02}.png" for case in CASES for i in range(7))
        report["artifact_sha256"] = {name: digest(root / name) for name in names}
    except Exception as exc:
        report["blocking_reasons"].append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            cleanup = _run(["docker", "rm", "-f", container], check=False, timeout=20)
            report["container_removed"] = cleanup.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            report["container_removed"] = False
            report["cleanup_error"] = str(exc)
        (root / "result.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report
