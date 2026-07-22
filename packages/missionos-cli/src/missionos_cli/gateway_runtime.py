"""Gateway client construction and local process lifecycle for the MissionOS CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os
import subprocess
import time

import click
import httpx
from rich.console import Console

from .gateway_client import (
    MissionOSGatewayClient,
    _gateway_unreachable_message,
)
from .gateway_process import (
    _build_gateway_pid_record,
    _dotenv_process_values,
    _gateway_argv,
    _gateway_process_env,
    _stop_gateway_pid,
)


console = Console()


def make_client(base_url: str, timeout: float) -> MissionOSGatewayClient:
    dotenv_values = _dotenv_process_values()
    api_key = os.getenv("GATEWAY_API_KEY") or dotenv_values.get("GATEWAY_API_KEY")
    return MissionOSGatewayClient(
        base_url=base_url,
        timeout=timeout,
        api_key=api_key or None,
    )


def _gateway_reachable(client: MissionOSGatewayClient) -> bool:
    """Return True when the Gateway answers a health probe."""
    try:
        client.health()
    except (click.ClickException, httpx.HTTPError):
        return False
    return True


def _gateway_health_payload(client: MissionOSGatewayClient) -> dict[str, Any]:
    try:
        payload = client.health()
    except (click.ClickException, httpx.HTTPError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _gateway_is_fixture_backend(client: MissionOSGatewayClient) -> bool:
    payload = _gateway_health_payload(client)
    backend = str(payload.get("session_backend") or payload.get("backend") or "").lower()
    version = str(payload.get("version") or "").lower()
    return backend == "fixture" or "fixture" in version


def _spawn_gateway(
    base_url: str,
    *,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    detached: bool = False,
    enable_live_sitl: bool = False,
) -> "subprocess.Popen[bytes]":
    return subprocess.Popen(
        _gateway_argv(base_url),
        stdout=stdout,
        stderr=stderr,
        env=_gateway_process_env(enable_live_sitl=enable_live_sitl),
        start_new_session=detached,
    )


def _terminate_gateway(proc: "subprocess.Popen[bytes]") -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _ensure_gateway(
    client: MissionOSGatewayClient,
    base_url: str,
    *,
    autostart: bool,
    enable_live_sitl: bool = False,
) -> "subprocess.Popen[bytes] | None":
    """Ensure the Gateway is reachable before the chat loop starts."""
    if _gateway_reachable(client):
        if enable_live_sitl and _gateway_is_fixture_backend(client):
            raise click.ClickException(
                "A fixture Gateway is already running at this URL. Live SITL "
                "requires the production backend. Run "
                "`missionos gateway restart --enable-live-sitl` and then retry."
            )
        if autostart:
            console.print(
                "[yellow]Gateway is already running. --autostart will reuse the "
                f"existing Gateway: {base_url}[/yellow]"
            )
            if enable_live_sitl:
                console.print(
                    "[yellow]The existing Gateway live SITL environment will not "
                    "be changed. To pick up code or env changes, run "
                    "`missionos gateway restart --enable-live-sitl`."
                    "[/yellow]"
                )
        return None
    if not autostart:
        raise click.ClickException(_gateway_unreachable_message(base_url))
    console.print(f"[blue]Autostarting Gateway ({base_url})...[/blue]")
    if enable_live_sitl:
        console.print(
            "[yellow]Live SITL opt-in: "
            "sitl_dispatch_runtime_enabled=true; "
            "live_hardware_target_allowed=false; "
            "physical_execution_invoked=false; "
            "operator_approval_required=true[/yellow]"
        )
    proc = _spawn_gateway(base_url, enable_live_sitl=enable_live_sitl)
    for _ in range(40):
        if proc.poll() is not None:
            raise click.ClickException("Gateway autostart failed; the process exited.")
        if _gateway_reachable(client):
            console.print("[green]Gateway is ready.[/green]")
            return proc
        time.sleep(0.5)
    _terminate_gateway(proc)
    raise click.ClickException("Timed out waiting for the Gateway to start.")


def _start_managed_gateway(
    *,
    client: MissionOSGatewayClient,
    base_url: str,
    pid_path: Path,
    log_path: Path,
    wait: bool,
    enable_live_sitl: bool,
) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    try:
        proc = _spawn_gateway(
            base_url,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            detached=True,
            enable_live_sitl=enable_live_sitl,
        )
    finally:
        log_file.close()
    record = _build_gateway_pid_record(
        pid=proc.pid,
        base_url=base_url,
        enable_live_sitl=enable_live_sitl,
    )
    pid_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    console.print(f"[blue]Started Gateway:[/blue] pid={proc.pid} url={base_url}")
    console.print(f"[blue]Log:[/blue] {log_path}")
    if enable_live_sitl:
        console.print(
            "[yellow]Live SITL opt-in: "
            "sitl_dispatch_runtime_enabled=true; "
            "live_hardware_target_allowed=false; "
            "physical_execution_invoked=false; "
            "operator_approval_required=true[/yellow]"
        )
    else:
        console.print(
            "[blue]Gateway mode:[/blue] planning-only (live SITL/dispatch env is not set)"
        )
    if not wait:
        return
    for _ in range(40):
        if proc.poll() is not None:
            pid_path.unlink(missing_ok=True)
            raise click.ClickException(f"Gateway failed to start. Check the log: {log_path}")
        if _gateway_reachable(client):
            console.print("[green]Gateway health: healthy[/green]")
            return
        time.sleep(0.5)
    _stop_gateway_pid(proc.pid)
    pid_path.unlink(missing_ok=True)
    raise click.ClickException(f"Gateway health check timed out. Check the log: {log_path}")
