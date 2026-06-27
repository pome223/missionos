#!/usr/bin/env python3
"""Docker smoke for the PX4/Gazebo-style telemetry-only sidecar.

The smoke starts an external sidecar process via Docker Compose, reads telemetry
over HTTP, sanitizes it, builds HIL evidence/review/gate artifacts, attaches
them to a temporary TaskStore task, and verifies the Control UI renderer can
render the resulting task. It never sends commands to the sidecar.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import error, request


ROOT_DIR = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:18889"
SERVICE_NAME = "boiled-claw-px4-gazebo-telemetry-sidecar"


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


def _get_json(path: str) -> dict:
    with request.urlopen(BASE_URL + path, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(path: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = request.Request(
        BASE_URL + path,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _wait_for_health(*, timeout_seconds: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            payload = _get_json("/health")
        except Exception as exc:  # pragma: no cover - diagnostic path
            last_error = exc
            time.sleep(1)
            continue
        if payload.get("status") == "ok":
            return payload
        time.sleep(1)
    raise RuntimeError(
        f"PX4/Gazebo telemetry sidecar did not become healthy: {last_error}"
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
        "has_gate": "autonomy_gate_result.v1" in html,
        "has_live_false": "live_execution_allowed=false" in html,
        "has_physical_false": "physical_execution_invoked=false" in html,
        "has_px4_source": "px4-gazebo-telemetry-sidecar" in html,
        "has_command_action_surface": 'data-action="px4' in html
        or 'data-action="gazebo' in html
        or 'data-action="dispatch' in html,
    }


def _exercise_service() -> dict:
    from src.runtime.px4_gazebo_telemetry_sidecar_client import (
        attach_px4_gazebo_telemetry_sidecar_smoke_artifacts,
        collect_px4_gazebo_telemetry_sidecar_sanitized,
        fetch_px4_gazebo_telemetry_sidecar_sample,
    )
    from src.runtime.task_store import TaskStore

    health = _get_json("/health")
    sample = fetch_px4_gazebo_telemetry_sidecar_sample(base_url=BASE_URL)
    sanitized = collect_px4_gazebo_telemetry_sidecar_sanitized(base_url=BASE_URL)
    post_status, post_payload = _post_json("/telemetry", {"command": "takeoff"})
    command_like_sample = fetch_px4_gazebo_telemetry_sidecar_sample(
        base_url=BASE_URL,
        telemetry_case="command_like",
    )

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="PX4/Gazebo telemetry sidecar smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        artifacts = attach_px4_gazebo_telemetry_sidecar_smoke_artifacts(
            task["task_id"],
            base_url=BASE_URL,
            now=sanitized.captured_at,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])
        assert stored is not None
        assert stored["status"] == "running"
        assert stored["artifacts"]["existing"] == {"kept": True}
        assert "approval" not in stored["artifacts"]
        assert "promotion_package" not in stored["artifacts"]
        assert "reuse_plan" not in stored["artifacts"]

        try:
            attach_px4_gazebo_telemetry_sidecar_smoke_artifacts(
                task["task_id"],
                base_url=BASE_URL,
                telemetry_case="command_like",
                now=sanitized.captured_at,
                task_store_factory=lambda: store,
            )
        except Exception as exc:
            command_like_reject = str(exc)
        else:  # pragma: no cover - fail path
            raise AssertionError("command-like telemetry should reject before persistence")
        after_reject = store.get(task["task_id"])
        assert after_reject is not None
        assert after_reject["artifacts"] == stored["artifacts"]

        ui = _render_control_ui_task(stored)

    assert health["telemetry_sidecar_started"] is True
    assert health["live_execution_allowed"] is False
    assert health["physical_execution_invoked"] is False
    assert health["command_payload_allowed"] is False
    assert sample["source"]["source_kind"] == "px4_gazebo_telemetry_sidecar"
    assert sanitized.source_id == "px4-gazebo-telemetry-sidecar"
    assert post_status == 405
    assert "not exposed" in post_payload["error"]
    assert "RosTopic" in json.dumps(command_like_sample)
    assert "RosTopic" in command_like_reject
    assert artifacts["px4_gazebo_sanitized_telemetry"]["source_id"] == (
        "px4-gazebo-telemetry-sidecar"
    )
    assert artifacts["hil_telemetry_evidence"]["live_execution_allowed"] is False
    assert artifacts["hil_telemetry_evidence"]["physical_execution_invoked"] is False
    assert artifacts["autonomy_gate_result"]["live_execution_allowed"] is False
    assert artifacts["autonomy_gate_result"]["physical_execution_invoked"] is False
    assert ui["has_hil_evidence"] is True
    assert ui["has_hil_review"] is True
    assert ui["has_gate"] is True
    assert ui["has_live_false"] is True
    assert ui["has_physical_false"] is True
    assert ui["has_px4_source"] is True
    assert ui["has_command_action_surface"] is False

    return {
        "service": SERVICE_NAME,
        "health_status": health["status"],
        "telemetry_sidecar_started": health["telemetry_sidecar_started"],
        "source_id": sanitized.source_id,
        "post_telemetry_status": post_status,
        "command_like_rejected": "RosTopic" in command_like_reject,
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
    compose = ["docker", "compose", "--profile", "px4-gazebo-telemetry"]
    subprocess.run(
        [*compose, "stop", SERVICE_NAME],
        cwd=ROOT_DIR,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [*compose, "rm", "-f", SERVICE_NAME],
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
        help="Start the sidecar without rebuilding the image.",
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Leave the sidecar container running after the smoke.",
    )
    args = parser.parse_args()

    compose = ["docker", "compose", "--profile", "px4-gazebo-telemetry"]
    up_command = [*compose, "up", "-d"]
    if not args.skip_build:
        up_command.append("--build")
    up_command.append(SERVICE_NAME)

    try:
        _run_command(up_command, timeout=180)
        _wait_for_health()
        summary = _exercise_service()
        print(json.dumps(summary, indent=2, sort_keys=True))
    finally:
        if not args.keep_running:
            _stop_service()
    return 0


if __name__ == "__main__":
    sys.exit(main())
