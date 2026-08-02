"""Read-only runtime support for MissionOS terminal and browser maps."""

from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote
import json
import secrets
import threading
import time

import click
from rich.console import Console
from rich.live import Live
from rich.panel import Panel

from .flight_map_html import _mission_map_html
from .gateway_client import MissionOSGatewayClient
from .job_status import _as_float, _task_artifacts, _task_status
from .map_model import (
    TERMINAL_TASK_STATUSES,
    _FLIGHT_MAP_TRAIL_LIMIT,
    _mission_map_model,
    _overlay_turtlebot3_live_telemetry,
    _turtlebot3_indoor_map_model_from_artifacts,
)
from .map_terminal import _render_flight_map, _render_turtlebot3_indoor_map
from .route_evidence_image import write_mission_route_evidence_artifacts
from .vla_operator import _is_vla_operator_task, _render_vla_operator_panel


FLIGHT_MAP_POLL_INTERVAL = 1.0
MISSION_MAP_OUTPUT_DIR = Path("output/missionos_maps")
console = Console()


def _task_and_timeline(
    client: MissionOSGatewayClient,
    task_id: str,
    *,
    timeline_limit: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    encoded_task_id = quote(task_id, safe="")
    task_payload = client.get(f"/tasks/{encoded_task_id}")
    timeline_payload = (
        client.get(f"/tasks/{encoded_task_id}/timeline?limit={timeline_limit}")
        if timeline_limit
        else {"events": []}
    )
    return task_payload, timeline_payload


def _write_mission_map_html(
    *,
    model: dict[str, Any],
    output_path: Path | None,
) -> Path:
    task_id = str(model.get("task_id") or "task").replace("/", "_")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_path or MISSION_MAP_OUTPUT_DIR / f"{task_id}_{timestamp}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_mission_map_html(model), encoding="utf-8")
    return path


def _write_terminal_route_evidence(
    *,
    model: dict[str, Any],
    output_dir: Path = MISSION_MAP_OUTPUT_DIR,
    stem: str | None = None,
) -> dict[str, Any] | None:
    """Write source-backed terminal route evidence for supported flight maps."""

    if model.get("map_kind") in {"indoor_local_xy", "vla_evidence_timeline"}:
        return None
    if str(model.get("task_status") or "").strip().lower() not in (TERMINAL_TASK_STATUSES):
        return None
    return write_mission_route_evidence_artifacts(
        model=model,
        output_dir=output_dir,
        stem=stem,
    )


def _watch_flight_map(
    client: MissionOSGatewayClient,
    task_id: str,
    *,
    poll_interval: float,
) -> None:
    trail: list[tuple[float, float]] = []
    turtlebot3_live_trail: list[dict[str, Any]] = []
    turtlebot3_alignment_state: dict[str, Any] = {}
    with Live(console=console, refresh_per_second=8, screen=False) as live:
        while True:
            try:
                task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
            except click.ClickException as exc:
                live.update(Panel(f"[red]{exc.message}[/red]", title="MissionOS Live Map"))
                time.sleep(max(0.05, poll_interval))
                continue
            artifacts = _task_artifacts(task_payload)
            if _is_vla_operator_task(task_payload):
                live.update(
                    _render_vla_operator_panel(
                        task_payload,
                        title="MissionOS Watch · governed VLA evidence",
                    )
                )
                status = _task_status(task_payload)
                if status in TERMINAL_TASK_STATUSES:
                    break
                time.sleep(max(0.05, poll_interval))
                continue
            indoor_map = _turtlebot3_indoor_map_model_from_artifacts(artifacts)
            status = _task_status(task_payload)
            if indoor_map:
                indoor_map = _overlay_turtlebot3_live_telemetry(
                    indoor_map,
                    artifacts=artifacts,
                    trail=turtlebot3_live_trail,
                    alignment_state=turtlebot3_alignment_state,
                    freeze_live_preview=status in TERMINAL_TASK_STATUSES,
                )
                live.update(
                    _render_turtlebot3_indoor_map(
                        indoor_map=indoor_map,
                        status=status,
                        task_id=task_id,
                    )
                )
                if status in TERMINAL_TASK_STATUSES:
                    break
                time.sleep(max(0.05, poll_interval))
                continue
            snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            north = _as_float(snapshot.get("local_x_m"))
            east = _as_float(snapshot.get("local_y_m"))
            if north is not None and east is not None:
                if not trail or trail[-1] != (north, east):
                    trail.append((north, east))
                    if len(trail) > _FLIGHT_MAP_TRAIL_LIMIT:
                        del trail[: len(trail) - _FLIGHT_MAP_TRAIL_LIMIT]
            if trail:
                live.update(
                    _render_flight_map(
                        trail=trail,
                        snapshot=snapshot,
                        artifacts=artifacts,
                        status=status,
                        task_id=task_id,
                    )
                )
            else:
                live.update(
                    Panel(
                        f"[dim]task={task_id} status={status} — waiting for telemetry...[/dim]",
                        title="MissionOS Live Map",
                        border_style="cyan",
                    )
                )
            if status in TERMINAL_TASK_STATUSES:
                break
            time.sleep(max(0.05, poll_interval))


def _serve_authenticated_live_mission_map(
    *,
    client: MissionOSGatewayClient,
    task_id: str,
    model: dict[str, Any],
    no_open: bool,
) -> None:
    """Serve live map HTML and an authenticated task proxy on loopback."""

    token = secrets.token_urlsafe(18)
    page_path = f"/{token}/"
    task_path = f"/{token}/task"
    evidence_path = f"/{token}/evidence.svg"
    live_model = dict(model)
    live_model["live"] = {
        **dict(model.get("live") or {}),
        "enabled": True,
        "task_url": task_path,
        "evidence_image_url": evidence_path,
    }
    html_bytes = _mission_map_html(live_model).encode("utf-8")
    terminal_seen = threading.Event()
    browser_live_trail: list[dict[str, Any]] = []
    browser_alignment_state: dict[str, Any] = {}
    overlay_lock = threading.Lock()
    evidence_lock = threading.Lock()
    evidence_state: dict[str, Any] = {}

    def ensure_terminal_evidence(
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with evidence_lock:
            if evidence_state:
                return evidence_state
            try:
                latest = payload or client.get(f"/tasks/{quote(task_id, safe='')}")
                terminal_model = _mission_map_model(
                    task_payload=latest,
                    provider=str((model.get("provider") or {}).get("key") or "osm"),
                    live_task_url=task_path,
                    poll_interval=float((model.get("live") or {}).get("poll_interval_ms") or 1000)
                    / 1000.0,
                )
                terminal_model["live"] = {
                    **dict(terminal_model.get("live") or {}),
                    "evidence_image_url": evidence_path,
                }
                generated = _write_terminal_route_evidence(model=terminal_model)
            except (click.ClickException, ValueError):
                return None
            if generated is None:
                return None
            evidence_state.update(generated)
            console.print(
                Panel(
                    "\n".join(
                        (
                            f"task_id={task_id}",
                            f"image={generated['svg_path']}",
                            f"manifest={generated['manifest_path']}",
                            "boundary=source-backed display evidence; source task "
                            "artifacts remain authoritative",
                        )
                    ),
                    title="MissionOS E2E Route Evidence",
                    border_style="green",
                )
            )
            return evidence_state

    class LiveMapHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _send(self, status: int, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == page_path:
                self._send(200, "text/html; charset=utf-8", html_bytes)
                return
            if self.path.split("?", 1)[0] == evidence_path:
                generated = ensure_terminal_evidence()
                if generated is None:
                    self._send(
                        409,
                        "text/plain; charset=utf-8",
                        b"terminal route evidence is not available",
                    )
                    return
                self._send(
                    200,
                    "image/svg+xml; charset=utf-8",
                    bytes(generated["svg_bytes"]),
                )
                return
            if self.path == task_path:
                try:
                    payload = client.get(f"/tasks/{quote(task_id, safe='')}")
                    task = payload.get("task")
                    task = task if isinstance(task, dict) else {}
                    artifacts = _task_artifacts(payload)
                    task_status = str(task.get("status") or "")
                    indoor_map = _turtlebot3_indoor_map_model_from_artifacts(artifacts)
                    if indoor_map:
                        with overlay_lock:
                            overlaid = _overlay_turtlebot3_live_telemetry(
                                indoor_map,
                                artifacts=artifacts,
                                trail=browser_live_trail,
                                alignment_state=browser_alignment_state,
                                freeze_live_preview=(task_status in TERMINAL_TASK_STATUSES),
                            )
                        next_task = dict(task)
                        next_artifacts = dict(artifacts)
                        next_artifacts["turtlebot3_indoor_map_model"] = overlaid
                        next_task["artifacts"] = next_artifacts
                        payload = {**payload, "task": next_task}
                        task = next_task
                    else:
                        provider_key = str((model.get("provider") or {}).get("key") or "osm")
                        fresh_model = _mission_map_model(
                            task_payload=payload,
                            provider=provider_key,
                            live_task_url=task_path,
                            poll_interval=float(
                                (model.get("live") or {}).get("poll_interval_ms") or 1000
                            )
                            / 1000.0,
                        )
                        fresh_model["live"] = {
                            **dict(fresh_model.get("live") or {}),
                            "evidence_image_url": evidence_path,
                        }
                        payload = {
                            "missionos_map_model": fresh_model,
                            "task": task,
                        }
                    terminal_response = str(task.get("status") or "") in TERMINAL_TASK_STATUSES
                    if terminal_response:
                        terminal_seen.set()
                        ensure_terminal_evidence(payload)
                    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    if terminal_response:
                        # The terminal preview is a one-response operator aid.
                        # A browser reload must reconstruct from persisted
                        # blue/purple evidence and therefore starts without it.
                        with overlay_lock:
                            browser_live_trail.clear()
                except click.ClickException as exc:
                    encoded = json.dumps({"detail": exc.message}, ensure_ascii=False).encode(
                        "utf-8"
                    )
                    self._send(502, "application/json; charset=utf-8", encoded)
                    return
                self._send(200, "application/json; charset=utf-8", encoded)
                return
            self._send(404, "text/plain; charset=utf-8", b"not found")

    server = ThreadingHTTPServer(("127.0.0.1", 0), LiveMapHandler)
    server.timeout = 0.5
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}{page_path}"
    opened = False if no_open else click.launch(url) == 0
    console.print(
        Panel(
            "\n".join(
                (
                    f"task_id={task_id}",
                    f"url={url}",
                    f"opened={str(opened).lower()}",
                    "live=true; authenticated_gateway_proxy=loopback",
                    "boundary=read-only display proxy; no approval, dispatch, "
                    "completion, or physical claim",
                )
            ),
            title="MissionOS Live 2D Map",
            border_style="cyan",
        )
    )
    try:
        next_status_poll = 0.0
        while not terminal_seen.is_set():
            server.handle_request()
            if time.monotonic() >= next_status_poll:
                try:
                    latest = client.get(f"/tasks/{quote(task_id, safe='')}")
                    latest_task = latest.get("task")
                    latest_task = latest_task if isinstance(latest_task, dict) else {}
                    if str(latest_task.get("status") or "") in TERMINAL_TASK_STATUSES:
                        ensure_terminal_evidence(latest)
                        terminal_seen.set()
                except click.ClickException:
                    pass
                next_status_poll = time.monotonic() + 1.0
        # Keep the authenticated read-only snapshot available after terminal
        # state. The companion lifecycle or Ctrl-C owns shutdown, so a browser
        # reload can reconstruct only persisted blue/purple evidence.
        while True:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
