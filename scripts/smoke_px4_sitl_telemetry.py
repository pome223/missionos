#!/usr/bin/env python3
"""Opt-in PX4 SIH telemetry-only Docker smoke.

This smoke starts the upstream `px4io/px4-sitl:latest` container with the SIH
quadrotor model, reads stdout logs only, converts the observed log output into a
telemetry-only Mission OS artifact chain, attaches the artifacts to a temporary
task, and renders the task with the read-only Control UI.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT_DIR = Path(__file__).resolve().parents[1]
SERVICE_NAME = "boiled-claw-px4-sitl-telemetry"
PROFILE = "px4-sitl-telemetry"


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
    result = _compose("logs", "--no-color", "--tail", "200", SERVICE_NAME, capture=True)
    return result.stdout


def _wait_for_logs(*, timeout_seconds: float = 90.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_logs = ""
    while time.monotonic() < deadline:
        logs = _logs()
        if (
            "INFO  [init] SIH simulator" in logs
            and "INFO  [simulator_sih] Simulation loop" in logs
            and "INFO  [px4] Startup script returned successfully" in logs
        ):
            return logs
        last_logs = logs
        time.sleep(2)
    raise RuntimeError(f"PX4 SIH logs did not appear: {last_logs[-500:]}")


def _collect_bounded_logs(
    *,
    max_duration_seconds: float,
    max_window_lines: int,
) -> tuple[str, str, str]:
    collector_started_at = _utc_now_iso()
    start = time.monotonic()
    logs = _wait_for_logs(timeout_seconds=max(90.0, max_duration_seconds))
    remaining = max_duration_seconds - (time.monotonic() - start)
    if remaining > 0:
        time.sleep(remaining)
        logs = _logs()
    collector_finished_at = _utc_now_iso()
    lines = [line for line in logs.splitlines() if line.strip()]
    return "\n".join(lines[:max_window_lines]), collector_started_at, collector_finished_at


def _inspect_service() -> dict:
    result = _run_command(
        [
            "docker",
            "inspect",
            SERVICE_NAME,
            "--format",
            "{{json .}}",
        ],
        capture=True,
    )
    return json.loads(result.stdout)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _provenance_from_inspect(
    inspect_data: dict,
    *,
    collector_started_at: str,
    collector_finished_at: str,
) -> dict:
    host_config = inspect_data["HostConfig"]
    config = inspect_data["Config"]
    state = inspect_data["State"]
    return {
        "compose_profile": PROFILE,
        "compose_service": SERVICE_NAME,
        "container_id": inspect_data["Id"][:12],
        "container_created_at": inspect_data["Created"],
        "container_started_at": state["StartedAt"],
        "collector_started_at": collector_started_at,
        "collector_finished_at": collector_finished_at,
        "source_image": config["Image"],
        "image_tag": config["Image"].split(":")[-1] if ":" in config["Image"] else "",
        "px4_daemon_args": list(config.get("Cmd") or []),
        "px4_sim_model": "sihsim_quadx",
        "network_mode": host_config["NetworkMode"],
        "port_bindings": host_config["PortBindings"],
        "read_only_rootfs": host_config["ReadonlyRootfs"],
        "privileged": host_config["Privileged"],
        "cap_drop": host_config.get("CapDrop") or [],
        "security_opt": host_config.get("SecurityOpt") or [],
    }


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
        "has_px4_sih_source_window": "PX4/SIH Source Window" in html,
        "has_window_bounded": "window_bounded=true" in html,
        "has_max_duration_seconds": "max_duration_seconds=3" in html,
        "has_max_window_lines": "max_window_lines=80" in html,
        "has_network_mode": "network_mode=none" in html,
        "has_read_only_rootfs": "read_only_rootfs=true" in html,
        "has_cap_drop": "cap_drop=ALL" in html,
        "has_live_false": "live_execution_allowed=false" in html,
        "has_physical_false": "physical_execution_invoked=false" in html,
        "has_source": "px4-sitl-sih-container" in html,
        "has_command_action_surface": 'data-action="px4' in html
        or 'data-action="gazebo' in html
        or 'data-action="dispatch' in html,
    }


def _exercise_logs(*, max_duration_seconds: float, max_window_lines: int) -> dict:
    from src.runtime.px4_sitl_log_collector import (
        attach_px4_sitl_log_hil_review_gate_artifacts,
        collect_px4_sitl_bounded_log_window_sanitized,
    )
    from src.runtime.task_store import TaskStore

    logs, collector_started_at, collector_finished_at = _collect_bounded_logs(
        max_duration_seconds=max_duration_seconds,
        max_window_lines=max_window_lines,
    )
    inspect_data = _inspect_service()
    host_config = inspect_data["HostConfig"]
    state = inspect_data["State"]
    provenance = _provenance_from_inspect(
        inspect_data,
        collector_started_at=collector_started_at,
        collector_finished_at=collector_finished_at,
    )
    sanitized = collect_px4_sitl_bounded_log_window_sanitized(
        logs,
        provenance=provenance,
        max_duration_seconds=max_duration_seconds,
        max_window_lines=max_window_lines,
    )

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="PX4 SIH telemetry-only smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        artifacts = attach_px4_sitl_log_hil_review_gate_artifacts(
            task["task_id"],
            logs,
            captured_at=sanitized.captured_at,
            provenance=provenance,
            max_duration_seconds=max_duration_seconds,
            max_window_lines=max_window_lines,
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

    assert sanitized.source_kind == "px4_sitl_sih_stdout_log"
    assert sanitized.source_id == "px4-sitl-sih-container"
    assert artifacts["autonomy_gate_result"]["live_execution_allowed"] is False
    assert artifacts["autonomy_gate_result"]["physical_execution_invoked"] is False
    assert state["Running"] is True
    assert state["ExitCode"] == 0
    assert sanitized.metadata["container_started_at"] == state["StartedAt"]
    assert sanitized.metadata["collector_started_at"] == collector_started_at
    assert sanitized.metadata["collector_finished_at"] == collector_finished_at
    assert sanitized.metadata["window_bounded"] is True
    assert sanitized.metadata["max_duration_seconds"] == max_duration_seconds
    assert sanitized.metadata["max_window_lines"] == max_window_lines
    assert sanitized.measurements["window_line_count"] <= max_window_lines
    assert sanitized.metadata["network_mode"] == "none"
    assert sanitized.metadata["port_bindings"] == {}
    assert sanitized.metadata["read_only_rootfs"] is True
    assert sanitized.metadata["privileged"] is False
    assert artifacts["hil_telemetry_envelope"]["metadata"]["source_metadata"][
        "container_started_at"
    ] == state["StartedAt"]
    assert artifacts["hil_telemetry_evidence"]["metadata"]["source_metadata"][
        "container_started_at"
    ] == state["StartedAt"]
    assert host_config["NetworkMode"] == "none"
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["Privileged"] is False
    assert host_config["PortBindings"] == {}
    assert ui["has_hil_evidence"] is True
    assert ui["has_hil_review"] is True
    assert ui["has_gate"] is True
    assert ui["has_px4_sih_source_window"] is True
    assert ui["has_window_bounded"] is True
    assert ui["has_max_duration_seconds"] is True
    assert ui["has_max_window_lines"] is True
    assert ui["has_network_mode"] is True
    assert ui["has_read_only_rootfs"] is True
    assert ui["has_cap_drop"] is True
    assert ui["has_live_false"] is True
    assert ui["has_physical_false"] is True
    assert ui["has_source"] is True
    assert ui["has_command_action_surface"] is False

    return {
        "service": SERVICE_NAME,
        "profile": PROFILE,
        "image": "px4io/px4-sitl:latest",
        "source_kind": sanitized.source_kind,
        "source_id": sanitized.source_id,
        "log_line_count": sanitized.measurements["log_line_count"],
        "window_line_count": sanitized.measurements["window_line_count"],
        "original_log_line_count": sanitized.measurements["original_log_line_count"],
        "window_truncated": sanitized.measurements["window_truncated"],
        "window_bounded": sanitized.metadata["window_bounded"],
        "max_window_lines": sanitized.metadata["max_window_lines"],
        "max_duration_seconds": sanitized.metadata["max_duration_seconds"],
        "container_running": state["Running"],
        "container_exit_code": state["ExitCode"],
        "container_id": sanitized.metadata["container_id"],
        "container_started_at": sanitized.metadata["container_started_at"],
        "collector_started_at": sanitized.metadata["collector_started_at"],
        "collector_finished_at": sanitized.metadata["collector_finished_at"],
        "px4_daemon_args": sanitized.metadata["px4_daemon_args"],
        "px4_sim_model": sanitized.metadata["px4_sim_model"],
        "cap_drop": sanitized.metadata["cap_drop"],
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
        "ui_px4_sih_source_window_rendered": ui["has_px4_sih_source_window"],
        "ui_window_bounded_rendered": ui["has_window_bounded"],
        "ui_max_duration_seconds_rendered": ui["has_max_duration_seconds"],
        "ui_max_window_lines_rendered": ui["has_max_window_lines"],
        "ui_network_mode_rendered": ui["has_network_mode"],
        "ui_read_only_rootfs_rendered": ui["has_read_only_rootfs"],
        "ui_cap_drop_rendered": ui["has_cap_drop"],
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
        "--keep-running",
        action="store_true",
        help="Leave the PX4 SIH container running after the smoke.",
    )
    parser.add_argument(
        "--max-duration-seconds",
        type=float,
        default=3.0,
        help="Bound the stdout collection window duration.",
    )
    parser.add_argument(
        "--max-window-lines",
        type=int,
        default=80,
        help="Bound the number of stdout lines converted into telemetry.",
    )
    args = parser.parse_args()

    try:
        _compose("up", "-d", SERVICE_NAME, timeout=240)
        summary = _exercise_logs(
            max_duration_seconds=args.max_duration_seconds,
            max_window_lines=args.max_window_lines,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    finally:
        if not args.keep_running:
            _stop_service()
    return 0


if __name__ == "__main__":
    sys.exit(main())
