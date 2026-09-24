#!/usr/bin/env python3
"""Run a prepared urban preview on an explicitly configured, already rented VM.

Provisioning, budget enforcement and cleanup remain explicit operator actions.
Only the fresh request and NPZ arrays cross this transport; approval records,
Jev credentials and other repository files are never included in the upload.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time


def call(args, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("urban GPU transport failed: " + result.stderr[-1500:])
    return result.stdout


def infer(root, gpu_config_path):
    from scripts.prepare_px4_anwm_input import prepare
    from scripts.select_urban_wam_route import select_route
    from scripts.aerial_anwm_runtime import digest_file
    from src.prediction.px4_camera import register_capture

    cfg = json.loads(gpu_config_path.read_text())
    if cfg.get("schema_version") != "missionos_urban_gpu_transport.v1":
        raise ValueError("explicit urban GPU transport configuration required")
    for key in ("project", "zone", "instance"):
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", cfg.get(key, "")):
            raise ValueError("invalid GPU transport identifier")
    remote_root = cfg.get("remote_root", "")
    if not re.fullmatch(r"/home/[A-Za-z0-9_-]+/aerial-wam", remote_root):
        raise ValueError("bounded remote ANWM directory required")
    if not re.fullmatch(r"[a-f0-9]{64}", cfg.get("published_runtime_sha256", "")):
        raise ValueError("published runtime digest required")
    session = root / "session"
    status = json.loads((session / "status.json").read_text())
    identity = status["session_id"]
    if not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise ValueError("invalid flight session ID")
    history, registered, prepared = (
        session / "history",
        root / "registered",
        root / "input",
    )
    register_capture(
        history / "capture.json",
        airframe_sdf=history / "airframe.sdf",
        camera_sdf=history / "camera.sdf",
        output_dir=registered,
    )
    prepare(
        history,
        registered,
        session / "goal/reference.json",
        prepared,
        upstream_root="../../upstream",
        checkpoint_path="../../assets/0200000.pth.tar",
        num_timesteps=32,
        urban_routes=True,
    )
    common = ["--project", cfg["project"], "--zone", cfg["zone"], "--quiet"]
    ssh = ["gcloud", "compute", "ssh", cfg["instance"], *common]
    remote_input = remote_root + "/incoming/" + identity
    remote_output = remote_root + "/output/" + identity
    runtime = remote_root + "/aerial_anwm_runtime.py"
    q = shlex.quote
    # This check prevents silently running a different script on a reused VM.
    observed_sha = call(ssh + ["--command", "sha256sum " + q(runtime)]).split()[0]
    if observed_sha != cfg["published_runtime_sha256"]:
        raise ValueError("remote runtime differs from explicitly published script")
    call(ssh + ["--command", "mkdir -p " + q(remote_input)])
    call(
        [
            "gcloud",
            "compute",
            "scp",
            str(prepared / "request.json"),
            str(prepared / "assets.npz"),
            cfg["instance"] + ":" + remote_input + "/",
            *common,
        ]
    )
    started = time.time()
    command = " ".join(
        q(value)
        for value in (
            remote_root + "/venv/bin/python",
            runtime,
            "--request",
            remote_input + "/request.json",
            "--output-dir",
            remote_output,
        )
    )
    call(ssh + ["--command", "HF_HUB_OFFLINE=1 " + command], timeout=135)
    call(
        [
            "gcloud",
            "compute",
            "scp",
            "--recurse",
            cfg["instance"] + ":" + remote_output,
            str(root / "forecast"),
            *common,
        ]
    )
    # The runtime writes result.json beside its output images.
    result_path = root / "forecast/result.json"
    status = json.loads((session / "status.json").read_text())
    scene = json.loads((session / "scene.json").read_text())
    decision = select_route(prepared, result_path, scene, status, time.time())
    decision["remote_call_wall_seconds"] = time.time() - started
    decision["runtime_script_sha256"] = cfg["published_runtime_sha256"]
    decision["uploaded_files"] = {
        name: digest_file(prepared / name) for name in ("request.json", "assets.npz")
    }
    return decision
