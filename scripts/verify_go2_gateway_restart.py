#!/usr/bin/env python3
"""Opt-in real Go2 worker: kill/restart an isolated Gateway and verify its fence.

SIGSTOP is injected into this probe's simulator only to hold the crash window
open deterministically. A new approved task must remain undispatched until that
worker exits. No hardware or external model service is invoked.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import httpx

from src.runtime import go2_worker_lease


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--port", type=int, default=18845)
    args = p.parse_args()
    if os.getenv("RUN_MISSIONOS_GO2_DELIVERY_SIM") != "1":
        p.error("requires RUN_MISSIONOS_GO2_DELIVERY_SIM=1")
    cli = shutil.which("missionos")
    if not cli:
        p.error("install the CLI and activate its environment")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    url = f"http://127.0.0.1:{args.port}"
    env = dict(
        os.environ,
        TASK_STORE_DB_PATH=str(args.output / "tasks.db"),
        MISSIONOS_GO2_OUTPUT_ROOT=str(args.output / "delivery"),
        MISSIONOS_GO2_SUPERVISION_MODE="rules",
        MISSIONOS_JEV_MODE="off",
        NO_COLOR="1",
        COLUMNS="150",
        PYTHONDONTWRITEBYTECODE="1",
    )
    # Prevent inherited service credentials/model mode from altering this probe.
    for key in ("GATEWAY_API_KEY", "DEEPSEEK_API_KEY", "RUN_MISSIONOS_GO2_SUPERVISOR"):
        env.pop(key, None)
    gateway = None
    worker = None
    paused = False
    logs = []
    commands = []
    old_lease = None

    def start_gateway(name):
        handle = (args.output / name).open("w")
        logs.append(handle)
        code = (
            "from src.gateway.server import create_missionos_gateway; "
            f'create_missionos_gateway().run(host="127.0.0.1", port={args.port})'
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code], env=env, stdout=handle, stderr=subprocess.STDOUT
        )
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Gateway exited during startup")
            try:
                if httpx.get(url + "/health", timeout=1, trust_env=False).status_code == 200:
                    return process
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        process.terminate()
        process.wait(timeout=10)
        raise RuntimeError("Gateway startup timeout")

    def chat(owner, text, label):
        command = [
            cli,
            "--gateway-url",
            url,
            "--state-path",
            str(args.output / (owner + "-state.json")),
            "chat",
            "--session-id",
            "restart-probe-" + owner,
            "--history-path",
            str(args.output / (owner + "-history")),
            "--go2-scenario",
            "moving_obstacle",
            "--go2-supervision-mode",
            "rules",
            "--no-companion-terminals",
            text,
        ]
        commands.append(command)
        run = subprocess.run(
            command, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30
        )
        output = run.stdout + run.stderr
        (args.output / label).write_text(output)
        assert run.returncode == 0, output
        return output

    def identity(owner):
        return json.loads((args.output / (owner + "-state.json")).read_text())[
            "sitl_execution_task_id"
        ]

    def task(task_id):
        response = httpx.get(url + "/tasks/" + task_id, trust_env=False)
        response.raise_for_status()
        payload = response.json()
        return payload.get("task", payload)

    try:
        gateway = start_gateway("gateway-before.log")
        chat("old", "Go2で会議室Aへ届けて", "old-proposal.log")
        chat("old", "/approve", "old-approved.log")
        chat("old", "/run", "old-dispatched.log")
        old_id = identity("old")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            old = task(old_id)
            worker = old["artifacts"].get("go2_worker_pid")
            snapshot = old["artifacts"].get("go2_delivery_snapshot", {})
            if worker and snapshot.get("sim_time_s", 0) >= 2:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Go2 worker did not produce physics observations")
        old_lease = old["artifacts"]["go2_worker_lease"]
        os.kill(worker, signal.SIGSTOP)
        paused = True
        assert not go2_worker_lease.stopped(old_lease)
        gateway.kill()
        gateway.wait(timeout=10)
        gateway = start_gateway("gateway-after.log")
        # Startup must request cancellation without waiting for a Go2 chat call.
        assert (args.output / "delivery" / f"{old_id}.cancel").exists()
        assert task(old_id)["status"] == "needs_attention"
        chat("new", "Go2で会議室Aへ届けて", "new-proposal.log")
        chat("new", "/approve", "new-approved.log")
        blocked = chat("new", "/run", "new-blocked.log")
        new_id = identity("new")
        assert "停止確認" in blocked
        assert task(new_id)["status"] == "approved"
        assert not (args.output / "delivery" / new_id).exists()
        assert (args.output / "delivery" / f"{old_id}.cancel").exists()
        assert not go2_worker_lease.stopped(old_lease)
        stale = chat("old", "/cancel", "old-context-cancel-rejected.log")
        assert "承認情報を確認できません" in stale  # Old context is not restored on restart.
        old = task(old_id)
        assert old["status"] == "needs_attention"
        assert not old["artifacts"]["go2_restart_recovery"]["stop_confirmed"]
        os.kill(worker, signal.SIGCONT)
        paused = False
        deadline = time.monotonic() + 40
        while not go2_worker_lease.stopped(old_lease) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert go2_worker_lease.stopped(old_lease), "old worker did not release its fence"
        worker = None
        chat("new", "/run", "new-dispatched-after-exit.log")
        assert task(new_id)["status"] in ("starting", "running")
        assert task(old_id)["artifacts"]["go2_restart_recovery"]["stop_confirmed"]
        assert (
            not task(old_id)["artifacts"].get("go2_delivery_result", {}).get("completion_claimed")
        )
        # The positive dispatch is sufficient here; normal delivery is a separate probe.
        chat("new", "/cancel", "new-cleanup-cancel.log")
        deadline = time.monotonic() + 60
        while task(new_id)["status"] in ("starting", "running", "cancel_requested"):
            if time.monotonic() > deadline:
                raise RuntimeError("new worker did not stop")
            time.sleep(0.2)
        report = dict(
            checks_passed=True,
            commands=commands,
            fault="SIGSTOP simulator and SIGKILL its Gateway",
            real_simulator_observed=True,
            dispatch_blocked_while_old_worker_alive=True,
            cancel_requested_on_restart=True,
            new_dispatch_after_worker_exit=True,
            old_mission_success_claimed=False,
            physical_execution_invoked=False,
            old_task=task(old_id),
            new_task=task(new_id),
        )
        (args.output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
        print("PASS: orphan remained fenced; new dispatch permitted only after worker exit")
    finally:
        if worker is not None and old_lease and not go2_worker_lease.stopped(old_lease):
            if paused:
                os.kill(worker, signal.SIGCONT)
            os.kill(worker, signal.SIGTERM)
        if gateway and gateway.poll() is None:
            gateway.terminate()
            try:
                gateway.wait(timeout=20)
            except subprocess.TimeoutExpired:
                gateway.kill()
                gateway.wait(timeout=5)
        for handle in logs:
            handle.close()


if __name__ == "__main__":
    main()
