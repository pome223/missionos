#!/usr/bin/env python3
"""Opt-in PX4/Gazebo-compatible fake telemetry log-source smoke.

This smoke starts a Dockerized external process that behaves like a
PX4/Gazebo-style telemetry log source. Mission OS reads stdout logs only,
sanitizes the latest telemetry sample, builds HIL review/gate artifacts,
attaches them to a temporary task, and verifies the read-only Control UI can
render the result.

This is a fake-source compatibility smoke. It is not actual PX4/Gazebo SITL
process evidence; use `smoke_px4_gazebo_sitl_telemetry_run.py` for #408.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT_DIR = Path(__file__).resolve().parents[1]
SERVICE_NAME = "boiled-claw-px4-gazebo-compatible-log-source"
PROFILE = "px4-gazebo-sitl-telemetry"


def _run_command(
    command: list[str],
    *,
    capture: bool = False,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=ROOT_DIR,
        check=True,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def _compose(*args: str, capture: bool = False, timeout: int | None = None):
    return _run_command(
        ["docker", "compose", "--profile", PROFILE, *args],
        capture=capture,
        timeout=timeout,
    )


def _logs() -> str:
    result = _compose("logs", "--no-color", "--tail", "100", SERVICE_NAME, capture=True)
    return result.stdout


def _wait_for_telemetry(*, timeout_seconds: float = 60.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        logs = _logs()
        if "PX4_GAZEBO_TELEMETRY " in logs:
            return logs
        time.sleep(1)
    raise RuntimeError("PX4/Gazebo-compatible telemetry log did not appear")


def _inspect_service() -> dict:
    result = _run_command(
        [
            "docker",
            "inspect",
            SERVICE_NAME,
            "--format",
            "{{json .HostConfig}}",
        ],
        capture=True,
    )
    return json.loads(result.stdout)


def _render_control_ui_task(stored_task: dict) -> dict:
    from playwright.sync_api import sync_playwright

    index_html = (ROOT_DIR / "src/gateway/static/index.html").read_text(
        encoding="utf-8"
    )
    app_js = (ROOT_DIR / "src/gateway/static/app.js").read_text(encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def handle(route):
            url = route.request.url
            if url.endswith("/chat"):
                route.fulfill(status=200, content_type="text/html", body=index_html)
            elif url.endswith("/chat-static/app.js"):
                route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body=app_js,
                )
            elif url.endswith("/chat-static/styles.css"):
                route.fulfill(status=200, content_type="text/css", body="")
            else:
                route.fulfill(status=200, content_type="application/json", body="{}")

        page.route("**/*", handle)
        page.goto("http://127.0.0.1:18999/chat")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_function('typeof renderTaskDetail === "function"')
        html = page.evaluate("(task) => renderTaskDetail(task)", stored_task)
        browser.close()

    return {
        "has_hil_evidence": "HIL Telemetry Evidence" in html,
        "has_hil_review": "HIL Telemetry Review" in html,
        "has_gate": "autonomy_gate_result.v1" in html,
        "has_live_false": "live_execution_allowed=false" in html,
        "has_physical_false": "physical_execution_invoked=false" in html,
        "has_source": "px4-gazebo-compatible-log-source" in html,
        "has_command_action_surface": 'data-action="px4' in html
        or 'data-action="gazebo' in html
        or 'data-action="dispatch' in html,
    }


def _exercise_logs() -> dict:
    from src.runtime.px4_gazebo_log_collector import collect_px4_gazebo_log_sanitized
    from src.runtime.px4_gazebo_sitl_telemetry_spike import (
        attach_px4_gazebo_sitl_telemetry_only_spike,
    )
    from src.runtime.px4_gazebo_telemetry import (
        attach_px4_gazebo_hil_review_gate_artifacts,
    )
    from src.runtime.task_store import TaskStore

    logs = _wait_for_telemetry()
    sanitized = collect_px4_gazebo_log_sanitized(logs)
    host_config = _inspect_service()

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="PX4/Gazebo compatible telemetry log smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        artifacts = attach_px4_gazebo_hil_review_gate_artifacts(
            task["task_id"],
            sanitized,
            now=sanitized.captured_at,
            task_store_factory=lambda: store,
        )
        artifacts = attach_px4_gazebo_sitl_telemetry_only_spike(
            task_id=task["task_id"],
            artifacts=artifacts,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])
        assert stored is not None
        assert stored["status"] == "running"
        assert stored["artifacts"]["existing"] == {"kept": True}
        assert "approval" not in stored["artifacts"]
        assert "promotion_package" not in stored["artifacts"]
        assert "reuse_plan" not in stored["artifacts"]
        ui = _render_control_ui_task(stored)

    assert sanitized.source_kind == "px4_gazebo_compatible_log_source"
    assert sanitized.source_id == "px4-gazebo-compatible-log-source"
    assert artifacts["autonomy_gate_result"]["live_execution_allowed"] is False
    assert artifacts["autonomy_gate_result"]["physical_execution_invoked"] is False
    assert (
        artifacts["px4_gazebo_sitl_telemetry_only_spike"][
            "px4_gazebo_sitl_telemetry_only_spike"
        ]
        is True
    )
    assert (
        artifacts["px4_gazebo_sitl_telemetry_only_spike"][
            "coupled_px4_gazebo_execution_invoked"
        ]
        is False
    )
    assert (
        artifacts["px4_gazebo_sitl_telemetry_only_spike"]["mavlink_dispatch_allowed"]
        is False
    )
    assert host_config["NetworkMode"] == "none"
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["Privileged"] is False
    assert host_config["PortBindings"] == {}
    assert ui["has_hil_evidence"] is True
    assert ui["has_hil_review"] is True
    assert ui["has_gate"] is True
    assert ui["has_live_false"] is True
    assert ui["has_physical_false"] is True
    assert ui["has_source"] is True
    assert ui["has_command_action_surface"] is False

    return {
        "service": SERVICE_NAME,
        "profile": PROFILE,
        "source_kind": sanitized.source_kind,
        "source_id": sanitized.source_id,
        "spike_schema_version": artifacts["px4_gazebo_sitl_telemetry_only_spike"][
            "schema_version"
        ],
        "spike_scope": artifacts["px4_gazebo_sitl_telemetry_only_spike"]["spike_scope"],
        "px4_gazebo_sitl_telemetry_only_spike": artifacts[
            "px4_gazebo_sitl_telemetry_only_spike"
        ]["px4_gazebo_sitl_telemetry_only_spike"],
        "coupled_px4_gazebo_execution_invoked": artifacts[
            "px4_gazebo_sitl_telemetry_only_spike"
        ]["coupled_px4_gazebo_execution_invoked"],
        "actual_px4_gazebo_flight_control_invoked": artifacts[
            "px4_gazebo_sitl_telemetry_only_spike"
        ]["actual_px4_gazebo_flight_control_invoked"],
        "network_mode": host_config["NetworkMode"],
        "read_only_rootfs": host_config["ReadonlyRootfs"],
        "privileged": host_config["Privileged"],
        "port_bindings": host_config["PortBindings"],
        "attached_task_status": stored["status"],
        "existing_artifact_kept": stored["artifacts"]["existing"]["kept"],
        "gate_passed": artifacts["autonomy_gate_result"]["passed"],
        "live_execution_allowed": artifacts["autonomy_gate_result"][
            "live_execution_allowed"
        ],
        "physical_execution_invoked": artifacts["autonomy_gate_result"][
            "physical_execution_invoked"
        ],
        "ui_hil_evidence_rendered": ui["has_hil_evidence"],
        "ui_hil_review_rendered": ui["has_hil_review"],
        "ui_gate_rendered": ui["has_gate"],
        "ui_command_action_surface": ui["has_command_action_surface"],
    }


def _stop_service() -> None:
    subprocess.run(
        ["docker", "compose", "--profile", PROFILE, "stop", SERVICE_NAME],
        cwd=ROOT_DIR,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["docker", "compose", "--profile", PROFILE, "rm", "-f", SERVICE_NAME],
        cwd=ROOT_DIR,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Start the source without rebuilding the image.",
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Leave the telemetry source container running after the smoke.",
    )
    args = parser.parse_args()

    up_command = ["up", "-d"]
    if not args.skip_build:
        up_command.append("--build")
    up_command.append(SERVICE_NAME)

    try:
        _compose(*up_command, timeout=180)
        summary = _exercise_logs()
        print(json.dumps(summary, indent=2, sort_keys=True))
        print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    finally:
        if not args.keep_running:
            _stop_service()
    return 0


if __name__ == "__main__":
    sys.exit(main())
