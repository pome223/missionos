#!/usr/bin/env python3
"""Opt-in actual Gazebo Sim bounded runner smoke for Mission Designer requests."""

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
PROMPT = "標高3000mの山頂に5kgの水を届ける"
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def _collect_logs() -> tuple[str, datetime, datetime]:
    collector_started_at = _utc_now()
    logs = _wait_for_logs()
    collector_finished_at = _utc_now()
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
    collector_started_at: datetime,
    collector_finished_at: datetime,
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
        "container_exit_code": state["ExitCode"],
        "container_created_at": inspect_data["Created"],
        "container_started_at": state["StartedAt"],
        "collector_started_at": collector_started_at.isoformat(),
        "collector_finished_at": collector_finished_at.isoformat(),
        "source_image": config["Image"],
        "image_tag": config["Image"].split(":")[-1] if ":" in config["Image"] else "",
        "world_name": "empty",
        "world_ref": "/tmp/empty.sdf",
        "world_sdf_path": "/tmp/empty.sdf",
        "network_mode": host_config["NetworkMode"],
        "port_bindings": host_config["PortBindings"],
        "read_only_rootfs": host_config["ReadonlyRootfs"],
        "privileged": host_config["Privileged"],
        "cap_drop": host_config.get("CapDrop") or [],
        "security_opt": host_config.get("SecurityOpt") or [],
        "tmpfs": host_config.get("Tmpfs") or {},
        "home": env.get("HOME", ""),
        "xdg_cache_home": env.get("XDG_CACHE_HOME", ""),
        "xdg_config_home": env.get("XDG_CONFIG_HOME", ""),
        "gz_log_path": env.get("GZ_LOG_PATH", ""),
    }


def _bounded_request() -> dict:
    from src.runtime.px4_gazebo_mission_scenario_designer import (
        approve_px4_gazebo_mission_scenario_for_bounded_simulation,
        run_px4_gazebo_mission_scenario_designer,
    )

    designed = run_px4_gazebo_mission_scenario_designer(
        prompt=PROMPT,
        now=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )
    approved = approve_px4_gazebo_mission_scenario_for_bounded_simulation(
        proposal=designed["scenario_proposal"],
        validation=designed["validation_result"],
        now=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )
    return approved["bounded_simulation_request"]


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
        "bounded_run_panel": "Bounded Gazebo Simulation Run" in html,
        "gazebo_true": "gazebo_execution_invoked=true" in html,
        "physical_false": "physical_execution_invoked=false" in html,
        "mavlink_false": "mavlink_dispatch_allowed=false" in html,
        "ros_false": "ros_dispatch_allowed=false" in html,
        "world_path": "world_sdf_path=/tmp/empty.sdf" in html,
        "window_bounded": "window_bounded=true" in html,
        "network_mode": "network_mode=none" in html,
        "command_action_surface": 'data-action="bounded-gazebo' in html
        or 'data-action="dispatch-gazebo' in html
        or 'data-action="mavlink-dispatch' in html
        or 'data-action="ros-dispatch' in html,
    }


def _exercise_bounded_runner() -> dict:
    from src.runtime.px4_gazebo_bounded_simulation_runner import (
        run_px4_gazebo_bounded_simulation_request,
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
    request = _bounded_request()

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="Bounded Gazebo simulation runner smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        artifacts = run_px4_gazebo_bounded_simulation_request(
            task_id=task["task_id"],
            request=request,
            log_text=logs,
            started_at=collector_started_at,
            finished_at=collector_finished_at,
            max_duration_seconds=120,
            max_log_lines=260,
            provenance=provenance,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])
        assert stored is not None
        ui = _render_control_ui_task(stored)

    run = artifacts["px4_gazebo_bounded_simulation_run"]
    sanitized = artifacts["px4_gazebo_sanitized_telemetry"]
    gate = artifacts["autonomy_gate_result"]

    assert run["status"] == "completed"
    assert run["bounded_simulation_invoked"] is True
    assert run["gazebo_execution_invoked"] is True
    assert run["physical_execution_invoked"] is False
    assert run["hardware_target_allowed"] is False
    assert run["px4_mission_upload_allowed"] is False
    assert run["ros_dispatch_allowed"] is False
    assert run["mavlink_dispatch_allowed"] is False
    assert run["actuator_execution_allowed"] is False
    assert run["arbitrary_gazebo_mutation_allowed"] is False
    assert gate["passed"] is True
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert "px4_gazebo_bounded_simulation_run" in stored["artifacts"]
    assert state["Running"] is True
    assert host_config["NetworkMode"] == "none"
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["Privileged"] is False
    assert host_config["PortBindings"] == {}
    assert host_config.get("CapDrop") == ["ALL"]
    assert ui["bounded_run_panel"] is True
    assert ui["gazebo_true"] is True
    assert ui["physical_false"] is True
    assert ui["mavlink_false"] is True
    assert ui["ros_false"] is True
    assert ui["world_path"] is True
    assert ui["window_bounded"] is True
    assert ui["network_mode"] is True
    assert ui["command_action_surface"] is False

    return {
        "service": SERVICE_NAME,
        "profile": PROFILE,
        "image": "ghcr.io/openrobotics/gazebo:harmonic-full",
        "prompt": PROMPT,
        "request_status": request["request_status"],
        "scenario_profile": request["scenario_profile"],
        "route_profile": request["route_profile"],
        "risk_profile": request["risk_profile"],
        "run_schema_version": run["schema_version"],
        "run_status": run["status"],
        "scenario_run_mapping": run["scenario_run_mapping"],
        "scenario_mapping_status": run["scenario_mapping_status"],
        "scenario_specific_episode_invoked": run["scenario_specific_episode_invoked"],
        "bounded_simulation_invoked": run["bounded_simulation_invoked"],
        "bounded_gazebo_runner_opt_in": run["bounded_gazebo_runner_opt_in"],
        "gazebo_execution_invoked": run["gazebo_execution_invoked"],
        "deterministic_bounded_runner_invoked": run[
            "deterministic_bounded_runner_invoked"
        ],
        "general_gazebo_execution_authority_granted": run[
            "general_gazebo_execution_authority_granted"
        ],
        "physical_execution_invoked": run["physical_execution_invoked"],
        "hardware_target_allowed": run["hardware_target_allowed"],
        "px4_mission_upload_allowed": run["px4_mission_upload_allowed"],
        "ros_dispatch_allowed": run["ros_dispatch_allowed"],
        "mavlink_dispatch_allowed": run["mavlink_dispatch_allowed"],
        "actuator_execution_allowed": run["actuator_execution_allowed"],
        "unbounded_setpoint_stream_allowed": run["unbounded_setpoint_stream_allowed"],
        "arbitrary_gazebo_mutation_allowed": run["arbitrary_gazebo_mutation_allowed"],
        "approval_free_dispatch_allowed": run["approval_free_dispatch_allowed"],
        "approval_free_stronger_execution_allowed": run[
            "approval_free_stronger_execution_allowed"
        ],
        "memory_direct_command_authority_allowed": run[
            "memory_direct_command_authority_allowed"
        ],
        "telemetry_refs": run["telemetry_refs"],
        "gate_ref": run["gate_ref"],
        "hil_review_ref": run["hil_review_ref"],
        "source_kind": sanitized["source_kind"],
        "source_id": sanitized["source_id"],
        "gazebo_kind": sanitized["measurements"]["gazebo_kind"],
        "gazebo_process_started": sanitized["measurements"]["gazebo_process_started"],
        "world_loaded": sanitized["measurements"]["world_loaded"],
        "observed_log_line_count": run["observed_log_line_count"],
        "max_duration_seconds": run["max_duration_seconds"],
        "max_log_lines": run["max_log_lines"],
        "window_bounded": run["window_bounded"],
        "telemetry_age_seconds": run["telemetry_age_seconds"],
        "world_name": run["world_name"],
        "world_ref": run["world_ref"],
        "world_sdf_path": run["world_sdf_path"],
        "server_marker_observed": run["server_marker_observed"],
        "world_load_marker_observed": run["world_load_marker_observed"],
        "loaded_level_marker_observed": run["loaded_level_marker_observed"],
        "container_running": state["Running"],
        "container_exit_code": run["container_exit_code"],
        "container_id": sanitized["metadata"]["container_id"],
        "network_mode": host_config["NetworkMode"],
        "read_only_rootfs": host_config["ReadonlyRootfs"],
        "privileged": host_config["Privileged"],
        "cap_drop": host_config.get("CapDrop") or [],
        "port_bindings": host_config["PortBindings"],
        "task_status_preserved": stored["status"] == "running",
        "existing_artifact_kept": stored["artifacts"]["existing"]["kept"],
        "hil_evidence_created": "hil_telemetry_evidence" in artifacts,
        "hil_review_created": "hil_telemetry_review" in artifacts,
        "gate_created": "autonomy_gate_result" in artifacts,
        "gate_passed": gate["passed"],
        "ui_bounded_run_panel_rendered": ui["bounded_run_panel"],
        "ui_gazebo_execution_invoked_rendered": ui["gazebo_true"],
        "ui_physical_execution_false_rendered": ui["physical_false"],
        "ui_world_path_rendered": ui["world_path"],
        "ui_window_bounded_rendered": ui["window_bounded"],
        "ui_command_action_surface": ui["command_action_surface"],
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
        summary = _exercise_bounded_runner()
        print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
        print(
            "SMOKE_SUMMARY_JSON "
            + json.dumps(summary, sort_keys=True, ensure_ascii=False)
        )
    finally:
        if not args.keep_running:
            _stop_service()
    return 0


if __name__ == "__main__":
    sys.exit(main())
