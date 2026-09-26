#!/usr/bin/env python3
"""Run a prepared urban preview on an explicitly configured, already rented VM.

Provisioning, budget enforcement and cleanup remain explicit operator actions.
Only the fresh request and NPZ arrays cross this transport; approval records,
Jev credentials and other repository files are never included in the upload.
"""

from __future__ import annotations

import json
import io
import re
import shlex
import subprocess
import tarfile
import time


def call(args, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("urban GPU transport failed: " + result.stderr[-1500:])
    return result.stdout


def input_archive(prepared):
    """Only the two authorized, freshly prepared model-input files may leave."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in ("request.json", "assets.npz"):
            path = prepared / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024**2:
                raise ValueError("invalid bounded model input")
            info = tarfile.TarInfo(name)
            content = path.read_bytes()
            info.size, info.mode = len(content), 0o600
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def receive_forecasts(payload, destination, candidate_ids):
    """Reject paths, links, extras and duplicates before writing remote bytes."""
    expected = {"result.json"}
    for name in candidate_ids:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
            raise ValueError("invalid candidate asset identifier")
        expected.update((name + ".png", name + "-projection.png"))
    if len(payload) > 32 * 1024**2 or destination.exists():
        raise ValueError("oversized result or existing forecast destination")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        for member in archive:
            if (
                not member.isfile()
                or member.name not in expected
                or member.name in files
                or member.size > 8 * 1024**2
            ):
                raise ValueError("unexpected forecast archive member")
            files[member.name] = archive.extractfile(member).read()
    if set(files) != expected:
        raise ValueError("forecast archive missing bound assets")
    destination.mkdir()
    for name, content in files.items():
        (destination / name).write_bytes(content)


def stream_command(remote_root, identity, runtime_hash, candidate_ids):
    """One SSH session: input tar, pinned runtime, output tar; no extra code upload."""
    if (
        not re.fullmatch(r"/home/[A-Za-z0-9_-]+/aerial-wam", remote_root)
        or not re.fullmatch(r"[a-f0-9]{32}", identity)
        or not re.fullmatch(r"[a-f0-9]{64}", runtime_hash)
        or not 2 <= len(candidate_ids) <= 4
        or len(set(candidate_ids)) != len(candidate_ids)
        or any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", n) for n in candidate_ids)
    ):
        raise ValueError("invalid bounded streaming command")
    q = shlex.quote
    runtime = remote_root + "/aerial_anwm_runtime.py"
    remote_input, remote_output = (
        remote_root + part + identity for part in ("/incoming/", "/output/")
    )
    files = ["result.json"] + [
        filename for name in candidate_ids for filename in (name + ".png", name + "-projection.png")
    ]
    return "\n".join(
        [
            "set -eu",
            "umask 077",
            'test "$(sha256sum ' + q(runtime) + " | cut -d ' ' -f 1)\" = " + q(runtime_hash),
            "mkdir -p " + q(remote_root + "/incoming") + " " + q(remote_root + "/output"),
            "mkdir " + q(remote_input) + " " + q(remote_output),
            "tar -xf - -C " + q(remote_input),
            "HF_HUB_OFFLINE=1 "
            + " ".join(
                map(
                    q,
                    [
                        remote_root + "/venv/bin/python",
                        runtime,
                        "--request",
                        remote_input + "/request.json",
                        "--output-dir",
                        remote_output,
                    ],
                )
            )
            + " 1>&2",
            "COPYFILE_DISABLE=1 tar -cf - -C " + q(remote_output) + " " + " ".join(map(q, files)),
        ]
    )


def stream_forecasts(ssh, cfg, identity, prepared, destination, candidate_ids):
    command = stream_command(
        cfg["remote_root"], identity, cfg["published_runtime_sha256"], candidate_ids
    )
    result = subprocess.run(
        ssh + ["--ssh-flag=-T", "--command", command],
        input=input_archive(prepared),
        capture_output=True,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(
            "streamed urban GPU transport failed: " + result.stderr.decode(errors="replace")[-1500:]
        )
    receive_forecasts(result.stdout, destination, candidate_ids)


def infer(root, gpu_config_path):
    from scripts.prepare_px4_anwm_input import prepare
    from scripts.select_urban_wam_route import select_route
    from scripts.aerial_anwm_runtime import digest_file
    from src.prediction.px4_camera import register_capture

    infer_started = time.perf_counter()
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
    transport = cfg.get("transport", "scp_v1")
    if transport not in ("scp_v1", "single_ssh_tar_v1"):
        raise ValueError("unsupported urban GPU transport")
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
    manifest = prepare(
        history,
        registered,
        session / "goal/reference.json",
        prepared,
        upstream_root="../../upstream",
        checkpoint_path="../../assets/0200000.pth.tar",
        num_timesteps=32,
        urban_routes=True,
    )
    preparation_seconds = time.perf_counter() - infer_started
    common = ["--project", cfg["project"], "--zone", cfg["zone"], "--quiet"]
    ssh = ["gcloud", "compute", "ssh", cfg["instance"], *common]
    remote_input = remote_root + "/incoming/" + identity
    remote_output = remote_root + "/output/" + identity
    runtime = remote_root + "/aerial_anwm_runtime.py"
    transport_started = time.perf_counter()
    if transport == "single_ssh_tar_v1":
        stream_forecasts(
            ssh,
            cfg,
            identity,
            prepared,
            root / "forecast",
            [c["candidate_id"] for c in manifest["candidates"]],
        )
    else:
        legacy_transfer(ssh, common, cfg, remote_input, remote_output, runtime, prepared, root)
    transport_seconds = time.perf_counter() - transport_started
    result_path = root / "forecast/result.json"
    status = json.loads((session / "status.json").read_text())
    scene = json.loads((session / "scene.json").read_text())
    selection_started = time.perf_counter()
    decision = select_route(prepared, result_path, scene, status, time.time())
    decision["remote_call_wall_seconds"] = transport_seconds
    decision["transport"] = transport
    decision["transport_timing"] = {
        "preparation_wall_seconds": preparation_seconds,
        "transport_round_trip_wall_seconds": transport_seconds,
        "selection_validation_wall_seconds": time.perf_counter() - selection_started,
        "host_infer_wall_seconds": time.perf_counter() - infer_started,
        "remote_transport_invocations": 1 if transport == "single_ssh_tar_v1" else 4,
        "scope": "infer entry through selection; input age also includes preceding capture wait",
    }
    decision["runtime_script_sha256"] = cfg["published_runtime_sha256"]
    decision["uploaded_files"] = {
        name: digest_file(prepared / name) for name in ("request.json", "assets.npz")
    }
    return decision


def legacy_transfer(ssh, common, cfg, remote_input, remote_output, runtime, prepared, root):
    """Retained opt-in transport for existing configurations."""
    q = shlex.quote
    remote_root = cfg["remote_root"]
    # This check prevents silently running a different script on a reused VM.
    observed_sha = call(
        ssh + ["--command", "sha256sum " + q(runtime) + " && mkdir -p " + q(remote_input)]
    ).split()[0]
    if observed_sha != cfg["published_runtime_sha256"]:
        raise ValueError("remote runtime differs from explicitly published script")
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
    # Three candidates need one more sampling call than the two-way passage.
    # The independent 180-second observation limit remains unchanged.
    call(ssh + ["--command", "HF_HUB_OFFLINE=1 " + command], timeout=150)
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
