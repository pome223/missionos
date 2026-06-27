#!/usr/bin/env python3
"""Opt-in full Gazebo delivery simulation smoke.

This smoke starts the actual `gz sim` delivery world profile, observes bounded
stdout/log telemetry, and pushes the resulting artifacts through the simulated
delivery Mission OS chain. It does not expose Gazebo mutation, ROS/MAVLink,
actuator, live, or physical execution surfaces.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT_DIR = Path(__file__).resolve().parents[1]
SERVICE_NAME = "boiled-claw-gz-sim-delivery-world"
PROFILE = "gz-sim-delivery-world"
OPT_IN_ENV = "RUN_GAZEBO_DELIVERY_SIMULATION_SMOKE"
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the Gazebo delivery simulation smoke."
        )


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


def _wait_for_delivery_world_logs(*, timeout_seconds: float = 120.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_logs = ""
    while time.monotonic() < deadline:
        logs = _logs()
        clean = _strip_ansi(logs)
        if (
            "Gazebo Sim Server v" in clean
            and "Loading SDF world file" in clean
            and "Loaded level [default]" in clean
            and "/worlds/delivery_minimal.sdf" in clean
        ):
            return logs
        last_logs = logs
        time.sleep(2)
    raise RuntimeError(f"Gazebo delivery simulation logs did not appear: {last_logs[-500:]}")


def _inspect_service() -> dict:
    result = _run_command(
        ["docker", "inspect", SERVICE_NAME, "--format", "{{json .}}"],
        capture=True,
    )
    return json.loads(result.stdout)


def _provenance(inspect_data: dict, *, started_at: str, finished_at: str) -> dict:
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
        "world_name": "delivery_minimal",
        "world_sdf_path": "/worlds/delivery_minimal.sdf",
        "delivery_world_ref": "simulators/gazebo/worlds/delivery_minimal.sdf",
        "container_id": inspect_data["Id"][:12],
        "container_started_at": state["StartedAt"],
        "collector_started_at": started_at,
        "collector_finished_at": finished_at,
        "source_image": config["Image"],
        "image_tag": config["Image"].split(":")[-1] if ":" in config["Image"] else "",
        "network_mode": host_config["NetworkMode"],
        "port_bindings": host_config["PortBindings"],
        "read_only_rootfs": host_config["ReadonlyRootfs"],
        "privileged": host_config["Privileged"],
        "cap_drop": host_config.get("CapDrop") or [],
        "tmpfs": host_config.get("Tmpfs") or {},
        "home": env.get("HOME", ""),
        "xdg_cache_home": env.get("XDG_CACHE_HOME", ""),
        "xdg_config_home": env.get("XDG_CONFIG_HOME", ""),
        "gz_log_path": env.get("GZ_LOG_PATH", ""),
        "gazebo_invocation_args": list(config.get("Cmd") or []),
    }


def _contract(now: datetime):
    from src.runtime.delivery_mission_contract import build_delivery_mission_contract

    return build_delivery_mission_contract(
        mission_id="gazebo-delivery-simulation-smoke-001",
        pickup_location={
            "location_id": "pickup-pad-a",
            "latitude": 35.681236,
            "longitude": 139.767125,
        },
        dropoff_location={
            "location_id": "dropoff-pad-b",
            "latitude": 35.689487,
            "longitude": 139.691706,
        },
        delivery_window={
            "earliest_pickup_at": "2026-01-01T12:00:00Z",
            "latest_dropoff_at": "2026-01-01T12:30:00Z",
        },
        package_constraints={"package_id": "pkg-gazebo-smoke", "max_weight_kg": 1.2},
        geofence_constraints={"allowed_regions": ["sim-delivery-corridor"]},
        weather_constraints={
            "max_wind_speed_mps": 6.0,
            "max_precipitation_mm_per_hour": 0.0,
            "min_visibility_m": 1500.0,
        },
        battery_policy={
            "minimum_takeoff_percent": 80,
            "return_to_home_percent": 35,
            "reserve_landing_percent": 25,
        },
        landing_zone_policy={
            "min_clear_radius_m": 3.0,
            "max_slope_degrees": 5.0,
            "accepted_surface_kinds": ["marked_pad"],
        },
        telemetry_requirements={
            "required_measurements": [
                "position",
                "battery_percent",
                "vehicle_health",
                "weather_snapshot",
            ],
            "max_freshness_seconds": 2.0,
        },
        now=now,
    )


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
        "has_autonomy_gate": "autonomy_gate_result.v1" in html,
        "has_live_false": "live_execution_allowed=false" in html,
        "has_physical_false": "physical_execution_invoked=false" in html,
        "has_command_action_surface": 'data-action="px4' in html
        or 'data-action="gazebo' in html
        or 'data-action="gz-sim' in html
        or 'data-action="dispatch' in html,
    }


def _exercise_delivery_simulation() -> dict:
    from src.runtime.delivery_mission_gate import build_delivery_mission_gate_artifacts
    from src.runtime.delivery_mission_policy_review import (
        build_delivery_mission_policy_review,
    )
    from src.runtime.delivery_progress_review import build_delivery_progress_review
    from src.runtime.delivery_recovery_decision import build_delivery_recovery_decision
    from src.runtime.gazebo_delivery_scenario import build_gazebo_delivery_scenario
    from src.runtime.gazebo_delivery_sidecar_contract import (
        build_gazebo_delivery_sidecar_contract,
        validate_gazebo_delivery_sidecar_contract,
    )
    from src.runtime.gazebo_delivery_telemetry_window import (
        build_gazebo_delivery_telemetry_window_hil_artifacts,
    )
    from src.runtime.gz_sim_log_collector import (
        collect_gz_sim_delivery_world_log_sanitized,
    )
    from src.runtime.simulated_delivery_episode import build_simulated_delivery_episode
    from src.runtime.task_store import TaskStore

    captured_at = datetime.now(timezone.utc)
    collector_started_at = captured_at.isoformat()
    logs = _wait_for_delivery_world_logs()
    collector_finished_at = datetime.now(timezone.utc).isoformat()
    inspect_data = _inspect_service()
    host_config = inspect_data["HostConfig"]
    state = inspect_data["State"]
    provenance = _provenance(
        inspect_data,
        started_at=collector_started_at,
        finished_at=collector_finished_at,
    )
    sanitized = collect_gz_sim_delivery_world_log_sanitized(
        logs,
        captured_at=captured_at,
        provenance=provenance,
    )
    contract = _contract(captured_at)
    scenario = build_gazebo_delivery_scenario(
        delivery_mission_contract=contract,
        now=captured_at,
    )
    sidecar_contract = build_gazebo_delivery_sidecar_contract(
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        now=captured_at,
    )
    validated_sidecar = validate_gazebo_delivery_sidecar_contract(sidecar_contract)
    telemetry_artifacts = build_gazebo_delivery_telemetry_window_hil_artifacts(
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        sanitized_telemetry=sanitized,
        now=captured_at,
    )
    hil_review = telemetry_artifacts["hil_telemetry_review"]
    policy_review = build_delivery_mission_policy_review(
        delivery_mission_contract=contract,
        sanitized_telemetry=sanitized,
        hil_telemetry_review=hil_review,
        now=captured_at,
    )
    delivery_gate = build_delivery_mission_gate_artifacts(
        delivery_mission_contract=contract,
        delivery_mission_policy_review=policy_review,
        now=captured_at,
    )
    episode = build_simulated_delivery_episode(
        delivery_mission_contract=contract,
        delivery_mission_policy_review=policy_review,
        delivery_mission_scorecard=delivery_gate["delivery_mission_scorecard"],
        delivery_mission_gate_result=delivery_gate["delivery_mission_gate_result"],
        telemetry_refs=(
            f"gazebo_delivery_telemetry_window:{telemetry_artifacts['gazebo_delivery_telemetry_window']['window_id']}",
        ),
        now=captured_at,
    )
    progress = build_delivery_progress_review(
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        simulated_delivery_episode=episode,
        sanitized_telemetry=sanitized,
        hil_telemetry_review=hil_review,
        now=captured_at,
    )
    recovery = build_delivery_recovery_decision(
        delivery_mission_contract=contract,
        simulated_delivery_episode=episode,
        delivery_progress_review=progress,
        now=captured_at,
    )

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="Gazebo delivery simulation smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        artifacts = {
            "delivery_mission_contract": contract.model_dump(mode="json"),
            "gazebo_delivery_scenario": scenario.model_dump(mode="json"),
            "gazebo_delivery_sidecar_contract": validated_sidecar.model_dump(mode="json"),
            **telemetry_artifacts,
            "delivery_mission_policy_review": policy_review.model_dump(mode="json"),
            **delivery_gate,
            "simulated_delivery_episode": episode.model_dump(mode="json"),
            "delivery_progress_review": progress.model_dump(mode="json"),
            "delivery_recovery_decision": recovery.model_dump(mode="json"),
        }
        store.update(task["task_id"], artifacts=artifacts)
        stored = store.get(task["task_id"])
        assert stored is not None
        assert stored["status"] == "running"
        assert stored["artifacts"]["existing"] == {"kept": True}
        assert "approval" not in stored["artifacts"]
        assert "promotion_package" not in stored["artifacts"]
        assert "reuse_plan" not in stored["artifacts"]
        assert "runtime_reuse" not in stored["artifacts"]
        ui = _render_control_ui_task(stored)

    return {
        "service": SERVICE_NAME,
        "profile": PROFILE,
        "gazebo_delivery_process_started": state["Running"],
        "delivery_world_loaded": sanitized.metadata["delivery_world_loaded"],
        "telemetry_window_created": "gazebo_delivery_telemetry_window" in artifacts,
        "telemetry_window_bounded": artifacts["gazebo_delivery_telemetry_window"][
            "window_bounded"
        ],
        "sanitized_telemetry_created": "px4_gazebo_sanitized_telemetry" in artifacts,
        "hil_evidence_created": "hil_telemetry_evidence" in artifacts,
        "hil_review_created": "hil_telemetry_review" in artifacts,
        "policy_review_created": "delivery_mission_policy_review" in artifacts,
        "scorecard_created": "delivery_mission_scorecard" in artifacts,
        "gate_created": "delivery_mission_gate_result" in artifacts,
        "simulated_delivery_episode_created": "simulated_delivery_episode" in artifacts,
        "progress_review_created": "delivery_progress_review" in artifacts,
        "recovery_decision_created": "delivery_recovery_decision" in artifacts,
        "task_status_preserved": stored["status"] == "running",
        "attached_task_status": stored["status"],
        "existing_artifact_kept": stored["artifacts"]["existing"]["kept"],
        "approval_promotion_reuse_created": any(
            key in stored["artifacts"]
            for key in ("approval", "promotion_package", "reuse_plan", "runtime_reuse")
        ),
        "delivery_gate_status": artifacts["delivery_mission_gate_result"]["status"],
        "delivery_gate_passed": artifacts["delivery_mission_gate_result"]["passed"],
        "delivery_blocked_reasons": artifacts["delivery_mission_gate_result"][
            "blocked_reasons"
        ],
        "episode_final_status": artifacts["simulated_delivery_episode"][
            "final_status"
        ],
        "progress_status": artifacts["delivery_progress_review"]["status"],
        "recovery_primary_action": artifacts["delivery_recovery_decision"][
            "primary_action"
        ],
        "network_mode": host_config["NetworkMode"],
        "port_bindings": host_config["PortBindings"],
        "read_only_rootfs": host_config["ReadonlyRootfs"],
        "privileged": host_config["Privileged"],
        "cap_drop": host_config.get("CapDrop") or [],
        "live_execution_allowed": artifacts["delivery_mission_gate_result"][
            "live_execution_allowed"
        ],
        "physical_execution_invoked": artifacts["delivery_mission_gate_result"][
            "physical_execution_invoked"
        ],
        "command_payload_allowed": artifacts["delivery_mission_gate_result"][
            "command_payload_allowed"
        ],
        "dispatch_implementation_present": artifacts["delivery_mission_gate_result"][
            "dispatch_implementation_present"
        ],
        "ros_dispatch_allowed": artifacts["delivery_mission_gate_result"][
            "ros_dispatch_allowed"
        ],
        "mavlink_dispatch_allowed": artifacts["delivery_mission_gate_result"][
            "mavlink_dispatch_allowed"
        ],
        "actuator_execution_allowed": artifacts["delivery_mission_gate_result"][
            "actuator_execution_allowed"
        ],
        "ui_rendered": ui["has_hil_evidence"]
        and ui["has_hil_review"]
        and ui["has_autonomy_gate"],
        "ui_hil_evidence_rendered": ui["has_hil_evidence"],
        "ui_hil_review_rendered": ui["has_hil_review"],
        "ui_gate_rendered": ui["has_autonomy_gate"],
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
    _require_opt_in()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Leave the Gazebo Sim container running after the smoke.",
    )
    args = parser.parse_args()

    try:
        _compose("up", "-d", SERVICE_NAME, timeout=300)
        summary = _exercise_delivery_simulation()
        print(json.dumps(summary, indent=2, sort_keys=True))
        print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    finally:
        if not args.keep_running:
            _stop_service()
    return 0


if __name__ == "__main__":
    sys.exit(main())
