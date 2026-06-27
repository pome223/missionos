#!/usr/bin/env python3
"""Opt-in actual Gazebo Sim (`gz sim`) telemetry-only Docker smoke.

This smoke starts the official Open Robotics Gazebo Harmonic image, runs
`gz sim` server-only against a minimal empty SDF world, reads stdout logs only,
converts startup / readiness evidence into sanitized telemetry, builds HIL
review/gate artifacts, attaches them to a temporary task, and verifies the
read-only Control UI can render the result.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT_DIR = Path(__file__).resolve().parents[1]
SERVICE_NAME = "boiled-claw-gz-sim-headless-telemetry"
PROFILE = "gz-sim-headless-telemetry"
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


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
    result = _compose("logs", "--no-color", "--tail", "260", SERVICE_NAME, capture=True)
    return result.stdout


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wait_for_logs(*, timeout_seconds: float = 120.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_logs = ""
    while time.monotonic() < deadline:
        logs = _logs()
        clean_logs = _strip_ansi(logs)
        if (
            "Gazebo Sim Server v" in clean_logs
            and "Loading SDF world file" in clean_logs
            and "Loaded level [default]" in clean_logs
        ):
            return logs
        last_logs = logs
        time.sleep(2)
    raise RuntimeError(f"Gazebo Sim logs did not appear: {last_logs[-500:]}")


def _collect_logs() -> tuple[str, str, str]:
    collector_started_at = _utc_now_iso()
    logs = _wait_for_logs()
    collector_finished_at = _utc_now_iso()
    return logs, collector_started_at, collector_finished_at


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


def _provenance_from_inspect(
    inspect_data: dict,
    *,
    collector_started_at: str,
    collector_finished_at: str,
) -> dict:
    host_config = inspect_data["HostConfig"]
    config = inspect_data["Config"]
    state = inspect_data["State"]
    env = {
        item.split("=", 1)[0]: item.split("=", 1)[1]
        for item in config.get("Env") or []
        if "=" in item
    }
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
        "gazebo_command": list(config.get("Cmd") or []),
        "network_mode": host_config["NetworkMode"],
        "port_bindings": host_config["PortBindings"],
        "read_only_rootfs": host_config["ReadonlyRootfs"],
        "privileged": host_config["Privileged"],
        "cap_drop": host_config.get("CapDrop") or [],
        "security_opt": host_config.get("SecurityOpt") or [],
        "tmpfs": host_config.get("Tmpfs") or {},
        "writable_scratch_paths": [
            "/tmp",
            "/tmp/gazebo-home",
            "/tmp/gazebo-cache",
            "/tmp/gazebo-config",
            "/tmp/gazebo-logs",
        ],
        "home": env.get("HOME", ""),
        "xdg_cache_home": env.get("XDG_CACHE_HOME", ""),
        "xdg_config_home": env.get("XDG_CONFIG_HOME", ""),
        "gz_log_path": env.get("GZ_LOG_PATH", ""),
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
        "has_live_false": "live_execution_allowed=false" in html,
        "has_physical_false": "physical_execution_invoked=false" in html,
        "has_source": "gz-sim-harmonic-container" in html,
        "has_command_action_surface": 'data-action="px4' in html
        or 'data-action="gazebo' in html
        or 'data-action="gz-sim' in html
        or 'data-action="dispatch' in html,
    }


def _exercise_logs() -> dict:
    from src.runtime.gz_sim_log_collector import (
        attach_gz_sim_log_hil_review_gate_artifacts,
        collect_gz_sim_log_sanitized,
    )
    from src.runtime.task_store import TaskStore

    logs, collector_started_at, collector_finished_at = _collect_logs()
    inspect_data = _inspect_service()
    host_config = inspect_data["HostConfig"]
    state = inspect_data["State"]
    provenance = _provenance_from_inspect(
        inspect_data,
        collector_started_at=collector_started_at,
        collector_finished_at=collector_finished_at,
    )
    sanitized = collect_gz_sim_log_sanitized(logs, provenance=provenance)

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="Gazebo Sim telemetry-only smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        artifacts = attach_gz_sim_log_hil_review_gate_artifacts(
            task["task_id"],
            logs,
            captured_at=sanitized.captured_at,
            provenance=provenance,
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

    assert sanitized.source_kind == "gz_sim_harmonic_stdout_log"
    assert sanitized.source_id == "gz-sim-harmonic-container"
    assert sanitized.measurements["gazebo_process_started"] is True
    assert sanitized.measurements["gazebo_kind"] == "gz_sim_harmonic"
    assert sanitized.measurements["headless"] is True
    assert sanitized.measurements["world_loaded"] is True
    assert artifacts["autonomy_gate_result"]["live_execution_allowed"] is False
    assert artifacts["autonomy_gate_result"]["physical_execution_invoked"] is False
    assert state["Running"] is True
    assert host_config["NetworkMode"] == "none"
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["Privileged"] is False
    assert host_config["PortBindings"] == {}
    assert host_config.get("CapDrop") == ["ALL"]
    assert "/tmp/gazebo-home" in host_config.get("Tmpfs", {})
    assert "/tmp/gazebo-cache" in host_config.get("Tmpfs", {})
    assert "/tmp/gazebo-config" in host_config.get("Tmpfs", {})
    assert "/tmp/gazebo-logs" in host_config.get("Tmpfs", {})
    assert sanitized.metadata["home"] == "/tmp/gazebo-home"
    assert sanitized.metadata["xdg_cache_home"] == "/tmp/gazebo-cache"
    assert sanitized.metadata["xdg_config_home"] == "/tmp/gazebo-config"
    assert sanitized.metadata["gz_log_path"] == "/tmp/gazebo-logs"
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
        "image": "ghcr.io/openrobotics/gazebo:harmonic-full",
        "gazebo_kind": sanitized.measurements["gazebo_kind"],
        "gazebo_process_started": sanitized.measurements["gazebo_process_started"],
        "headless": sanitized.measurements["headless"],
        "world_loaded": sanitized.measurements["world_loaded"],
        "telemetry_or_readiness_collected": True,
        "sanitized_telemetry_created": True,
        "hil_evidence_created": "hil_telemetry_evidence" in artifacts,
        "hil_review_created": "hil_telemetry_review" in artifacts,
        "gate_created": "autonomy_gate_result" in artifacts,
        "source_kind": sanitized.source_kind,
        "source_id": sanitized.source_id,
        "log_line_count": sanitized.measurements["log_line_count"],
        "container_running": state["Running"],
        "container_exit_code": state["ExitCode"],
        "container_id": sanitized.metadata["container_id"],
        "container_started_at": sanitized.metadata["container_started_at"],
        "collector_started_at": sanitized.metadata["collector_started_at"],
        "collector_finished_at": sanitized.metadata["collector_finished_at"],
        "network_mode": host_config["NetworkMode"],
        "read_only_rootfs": host_config["ReadonlyRootfs"],
        "privileged": host_config["Privileged"],
        "cap_drop": host_config.get("CapDrop") or [],
        "port_bindings": host_config["PortBindings"],
        "tmpfs": host_config.get("Tmpfs") or {},
        "home": sanitized.metadata["home"],
        "xdg_cache_home": sanitized.metadata["xdg_cache_home"],
        "xdg_config_home": sanitized.metadata["xdg_config_home"],
        "gz_log_path": sanitized.metadata["gz_log_path"],
        "task_status_preserved": stored["status"] == "running",
        "attached_task_status": stored["status"],
        "existing_artifact_kept": stored["artifacts"]["existing"]["kept"],
        "gate_passed": artifacts["autonomy_gate_result"]["passed"],
        "live_execution_allowed": artifacts["autonomy_gate_result"][
            "live_execution_allowed"
        ],
        "physical_execution_invoked": artifacts["autonomy_gate_result"][
            "physical_execution_invoked"
        ],
        "ui_rendered": ui["has_hil_evidence"]
        and ui["has_hil_review"]
        and ui["has_gate"],
        "ui_hil_evidence_rendered": ui["has_hil_evidence"],
        "ui_hil_review_rendered": ui["has_hil_review"],
        "ui_gate_rendered": ui["has_gate"],
        "command_action_surface": ui["has_command_action_surface"],
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
        help="Leave the Gazebo Sim container running after the smoke.",
    )
    args = parser.parse_args()

    try:
        _compose("up", "-d", SERVICE_NAME, timeout=300)
        summary = _exercise_logs()
        print(json.dumps(summary, indent=2, sort_keys=True))
        print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    finally:
        if not args.keep_running:
            _stop_service()
    return 0


if __name__ == "__main__":
    sys.exit(main())
