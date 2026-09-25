#!/usr/bin/env python3
"""Opt-in installed CLI -> Gateway -> Go2 simulator and operator-view verification."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.parse import urlparse

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cancel-during-yield", action="store_true")
    args = parser.parse_args()
    if os.getenv("RUN_MISSIONOS_GO2_DELIVERY_SIM") != "1":
        parser.error("requires RUN_MISSIONOS_GO2_DELIVERY_SIM=1")
    if urlparse(args.gateway_url).hostname not in ("localhost", "127.0.0.1", "::1"):
        parser.error("requires a loopback Gateway")
    executable = shutil.which("missionos")
    if not executable:
        parser.error("install missionos-cli and put its environment on PATH")
    args.output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, NO_COLOR="1", COLUMNS="150", PYTHONDONTWRITEBYTECODE="1")
    base = [
        executable,
        "--gateway-url",
        args.gateway_url,
        "--state-path",
        str(args.output / "cli-state.json"),
    ]
    chat = [
        "chat",
        "--session-id",
        "go2-cli-" + args.output.name,
        "--history-path",
        str(args.output / "history"),
        "--go2-scenario",
        "moving_obstacle",
        "--go2-supervision-mode",
        "rules",
        "--no-companion-terminals",
    ]
    commands = []
    workers = []
    identity = None
    terminal = False

    def call(arguments, name):
        commands.append(base + arguments)
        run = subprocess.run(
            base + arguments,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=45,
        )
        output = run.stdout + run.stderr
        (args.output / name).write_text(output)
        if run.returncode:
            raise RuntimeError(f"{name}: CLI exit {run.returncode}: {output[-2000:]}")
        return output

    headers = {"X-API-Key": os.environ["GATEWAY_API_KEY"]} if os.getenv("GATEWAY_API_KEY") else {}
    with httpx.Client(base_url=args.gateway_url, headers=headers, trust_env=False) as client:

        def task():
            response = client.get("/tasks/" + identity)
            response.raise_for_status()
            payload = response.json()
            return payload.get("task", payload)

        try:
            call(chat + ["Go2で会議室Aへ届けて"], "chat-proposal.log")
            state = json.loads((args.output / "cli-state.json").read_text())
            identity = state["sitl_execution_task_id"]
            assert task()["status"] == "proposed"
            call(chat + ["/run"], "chat-unapproved-run.log")
            assert task()["status"] == "proposed", "unapproved dispatch"
            call(chat + ["/approve"], "chat-approve.log")
            assert task()["status"] == "approved"
            call(chat + ["/run"], "chat-run.log")
            for view in ("operate", "watch"):
                arguments = [view, "--task-id", identity, "--poll-interval", "0.5"]
                if view == "operate":
                    arguments += ["--history-path", str(args.output / "operate-history")]
                handle = (args.output / (view + "-live.log")).open("w")
                commands.append(base + arguments)
                process = subprocess.Popen(
                    base + arguments,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                )
                workers.append((process, handle))
            call(["job-status", "--task-id", identity], "job-status-running.log")
            deadline = time.monotonic() + 300
            canceled = False
            phases = set()
            while time.monotonic() < deadline:
                current = task()
                artifacts = current.get("artifacts", {})
                snapshot = artifacts.get("go2_delivery_snapshot", {})
                phase = (current["status"], snapshot.get("phase"))
                if phase not in phases:
                    print(identity, phase, flush=True)
                    phases.add(phase)
                if current["status"] not in ("starting", "running", "cancel_requested"):
                    terminal = True
                    break
                if (
                    args.cancel_during_yield
                    and not canceled
                    and snapshot.get("local_avoidance_state") == "yielding"
                ):
                    call(chat + ["/cancel"], "chat-cancel.log")
                    canceled = True
                time.sleep(0.25)
            assert terminal, "delivery exceeded wall timeout"
            expected = "canceled" if args.cancel_during_yield else "completed"
            assert current["status"] == expected, current["status"]
            for process, handle in workers:
                process.wait(timeout=20)
                handle.close()
                assert process.returncode == 0
            call(chat + ["/status"], "chat-final.log")
            for view in ("job-status", "operate", "watch"):
                arguments = [view, "--task-id", identity]
                if view == "operate":
                    arguments += ["--history-path", str(args.output / "operate-history")]
                output = call(arguments, view + "-final.log")
                assert identity in output and expected in output
                assert "physical_execution=False" in output
            call(
                [
                    "map",
                    "--task-id",
                    identity,
                    "--snapshot",
                    "--no-open",
                    "--output",
                    str(args.output / "map.html"),
                ],
                "map.log",
            )
            page = (args.output / "map.html").read_text()
            assert identity in page
            model = json.loads(
                page.split('<script id="go2-map-data" type="application/json">')[1].split(
                    "</script>"
                )[0]
            )
            result = current["artifacts"]["go2_delivery_result"]
            assert result["physical_execution_invoked"] is False
            assert result["dynamic_avoidance"]["contact_physics_steps"] == 0
            assert result["dynamic_avoidance"]["yield_count"] >= 1
            events = [event["event"] for event in result["events"]]
            if args.cancel_during_yield:
                assert canceled and "operator_cancel_stopped" in events
                assert not result["completion_claimed"]
                assert not model["delivery_and_return_verified"]
                assert result["terminal_state"]["measured_speed_mps"] < 0.06
            else:
                assert result["completion_claimed"] and model["delivery_and_return_verified"]
                assert result["receipt"]["source"] == "simulation_recipient"
                assert result["receipt"]["received"]
                assert events.index("terminal_hold_verified") < events.index("mission_completed")
            (args.output / "task-final.json").write_text(json.dumps(current, indent=2) + "\n")
            report = dict(
                status=expected,
                checks_passed=True,
                task_id=identity,
                commands=commands,
                unapproved_run_blocked=True,
                physical_execution_invoked=False,
                sim_time_s=result["terminal_state"]["sim_time_s"],
                dynamic_avoidance=result["dynamic_avoidance"],
                observed_points=len(model["trail"]),
                surfaces=["chat", "job-status", "operate", "watch", "map"],
                browser_visual_check=False,
            )
            (args.output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps({k: report[k] for k in ("status", "checks_passed", "sim_time_s")}))
        finally:
            if identity and not terminal:
                try:
                    call(chat + ["/cancel"], "cleanup-cancel.log")
                except Exception:
                    pass
            for process, handle in workers:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                handle.close()


if __name__ == "__main__":
    main()
