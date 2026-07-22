"""Local Docker and Gateway lifecycle for TurtleBot3 chat."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import os
import shlex
import subprocess

import click
from rich.console import Console
from rich.panel import Panel

from .gateway_client import MissionOSGatewayClient, _gateway_host_port
from .gateway_runtime import _gateway_reachable, make_client


DEFAULT_GATEWAY_URL = "http://127.0.0.1:18791"
DEFAULT_TURTLEBOT3_CHAT_INSTRUCTION = (
    "TurtleBot3で屋内配送ルートを走って。障害物を避けて、目的地まで届けて。"
)
TURTLEBOT3_CHAT_TIMEOUT = 600.0

console = Console()


def _find_repo_root_for_turtlebot3_smoke() -> Path:
    script_rel = Path("scripts/smoke_ros2_nav2_turtlebot3_obstacle_delivery_docker.sh")
    candidates: list[Path] = []
    cwd = Path.cwd().resolve()
    candidates.extend([cwd, *cwd.parents])
    module_path = Path(__file__).resolve()
    candidates.extend([module_path.parent, *module_path.parents])
    for candidate in candidates:
        if (candidate / script_rel).is_file():
            return candidate
    return cwd


def _run_turtlebot3_chat_smoke(
    *,
    instruction: str,
    build_image: bool,
    mid_recovery: bool,
    dry_run: bool,
) -> int:
    repo_root = _find_repo_root_for_turtlebot3_smoke()
    script = repo_root / "scripts" / "smoke_ros2_nav2_turtlebot3_obstacle_delivery_docker.sh"
    if not script.is_file():
        console.print(
            "[red]TurtleBot3 Docker smoke script was not found. "
            "Run this command from the MissionOS repository root.[/red]"
        )
        return 2
    image = os.environ.get(
        "MISSIONOS_TB3_DOCKER_IMAGE",
        "missionos-ros2-nav2-turtlebot3:local",
    )
    env = os.environ.copy()
    env["MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_INSTRUCTION"] = (
        instruction.strip() or DEFAULT_TURTLEBOT3_CHAT_INSTRUCTION
    )
    if mid_recovery:
        env["MISSIONOS_CHAT_TURTLEBOT3_MID_RECOVERY_SMOKE"] = "1"

    build_cmd = (
        "docker",
        "build",
        "-f",
        "docker/ros2_nav2_turtlebot3/Dockerfile",
        "-t",
        image,
        ".",
    )
    run_cmd = (str(script),)
    console.print(
        Panel(
            "\n".join(
                (
                    f"instruction={env['MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_INSTRUCTION']}",
                    f"repo_root={repo_root}",
                    f"image={image}",
                    "boundary=MissionOS chat -> Gateway -> TurtleBot3/Nav2/Gazebo sim",
                    "claim_scope=sim_action; physical_execution_invoked=false",
                )
            ),
            title="TurtleBot3 MissionOS Chat",
            border_style="green",
        )
    )
    if dry_run:
        if build_image:
            console.print("[cyan]build:[/cyan] " + shlex.join(build_cmd))
        console.print(
            "[cyan]run:[/cyan] "
            + "MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_INSTRUCTION="
            + shlex.quote(env["MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_INSTRUCTION"])
            + (
                " MISSIONOS_CHAT_TURTLEBOT3_MID_RECOVERY_SMOKE=1"
                if mid_recovery
                else ""
            )
            + " "
            + shlex.join(run_cmd)
        )
        return 0
    if build_image:
        build_result = subprocess.run(build_cmd, cwd=str(repo_root), env=env, check=False)
        if build_result.returncode != 0:
            return int(build_result.returncode)
    run_result = subprocess.run(run_cmd, cwd=str(repo_root), env=env, check=False)
    return int(run_result.returncode)


def _turtlebot3_gateway_container_name() -> str:
    return os.environ.get(
        "MISSIONOS_TB3_GATEWAY_CONTAINER",
        "missionos-turtlebot3-gateway",
    ).strip() or "missionos-turtlebot3-gateway"


def _turtlebot3_gateway_start_script(repo_root: Path) -> Path:
    return repo_root / "scripts" / "start_ros2_nav2_turtlebot3_gateway_docker.sh"


def _start_turtlebot3_gateway_container(
    *,
    gateway_url: str,
    instruction: str,
    build_image: bool,
    dry_run: bool,
    gateway_api_key: str = "",
) -> bool:
    repo_root = _find_repo_root_for_turtlebot3_smoke()
    script = _turtlebot3_gateway_start_script(repo_root)
    if not script.is_file():
        raise click.ClickException(
            "TurtleBot3 Gateway Docker launcher was not found. "
            "Run this command from the MissionOS repository root."
        )
    host, port = _gateway_host_port(gateway_url)
    if host not in {"127.0.0.1", "localhost"}:
        raise click.ClickException(
            "--robot turtlebot3 can autostart the Docker Gateway only on localhost. "
            f"Current gateway host is {host!r}."
        )
    image = os.environ.get(
        "MISSIONOS_TB3_DOCKER_IMAGE",
        "missionos-ros2-nav2-turtlebot3:local",
    )
    env = os.environ.copy()
    if gateway_api_key:
        env["GATEWAY_API_KEY"] = gateway_api_key
    env["MISSIONOS_TB3_DOCKER_IMAGE"] = image
    env["MISSIONOS_TB3_GATEWAY_CONTAINER"] = _turtlebot3_gateway_container_name()
    env["MISSIONOS_TB3_GATEWAY_PORT"] = str(port)
    world_profile = os.environ.get("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "house").strip()
    world_profile = world_profile if world_profile in {"arena", "house"} else "house"
    env["MISSIONOS_TURTLEBOT3_WORLD_PROFILE"] = world_profile
    build_cmd = (
        "docker",
        "build",
        "-f",
        "docker/ros2_nav2_turtlebot3/Dockerfile",
        "-t",
        image,
        ".",
    )
    console.print(
        Panel(
            "\n".join(
                (
                    f"instruction={instruction.strip() or DEFAULT_TURTLEBOT3_CHAT_INSTRUCTION}",
                    f"gateway_url={gateway_url}",
                    f"repo_root={repo_root}",
                    f"image={image}",
                    f"world_profile={world_profile}",
                    "boundary=MissionOS chat -> Gateway -> TurtleBot3/Nav2/Gazebo sim",
                    "surfaces=chat + operate + watch + map",
                    "claim_scope=sim_action; physical_execution_invoked=false",
                )
            ),
            title="TurtleBot3 MissionOS Gateway",
            border_style="green",
        )
    )
    if dry_run:
        if build_image:
            console.print("[cyan]build:[/cyan] " + shlex.join(build_cmd))
        console.print(
            "[cyan]start gateway/sim:[/cyan] "
            + f"MISSIONOS_TB3_GATEWAY_PORT={port} "
            + "MISSIONOS_TB3_GATEWAY_CONTAINER="
            + shlex.quote(env["MISSIONOS_TB3_GATEWAY_CONTAINER"])
            + " "
            + f"MISSIONOS_TURTLEBOT3_WORLD_PROFILE={world_profile} "
            + shlex.join((str(script),))
        )
        return False
    if build_image:
        build_result = subprocess.run(build_cmd, cwd=str(repo_root), env=env, check=False)
        if build_result.returncode != 0:
            raise click.ClickException(
                f"TurtleBot3 Docker image build failed with exit code {build_result.returncode}."
            )
    start_result = subprocess.run((str(script),), cwd=str(repo_root), env=env, check=False)
    if start_result.returncode != 0:
        raise click.ClickException(
            f"TurtleBot3 Docker Gateway startup failed with exit code {start_result.returncode}."
        )
    return True


def _stop_turtlebot3_gateway_container() -> None:
    subprocess.run(
        ("docker", "rm", "-f", _turtlebot3_gateway_container_name()),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _maybe_retarget_turtlebot3_gateway_url(
    ctx: click.Context,
    *,
    gateway_reachable: Callable[[MissionOSGatewayClient], bool] = _gateway_reachable,
) -> None:
    gateway_url = str(ctx.obj.get("missionos_gateway_url") or DEFAULT_GATEWAY_URL)
    if gateway_url.rstrip("/") != DEFAULT_GATEWAY_URL:
        return
    client = ctx.obj.get("missionos_client")
    if not isinstance(client, MissionOSGatewayClient) or not gateway_reachable(client):
        return
    alternate_url = os.environ.get(
        "MISSIONOS_TB3_GATEWAY_URL",
        "http://127.0.0.1:18792",
    ).strip()
    if not alternate_url:
        return
    ctx.obj["missionos_gateway_url"] = alternate_url
    ctx.obj["missionos_client"] = make_client(alternate_url, client.timeout)
    console.print(
        "[yellow]Default Gateway is already reachable at "
        f"{DEFAULT_GATEWAY_URL}; using TurtleBot3 Gateway URL {alternate_url}. "
        "Pass --gateway-url explicitly to override.[/yellow]"
    )


def _floor_turtlebot3_chat_timeout(ctx: click.Context) -> None:
    client = ctx.obj.get("missionos_client")
    if not isinstance(client, MissionOSGatewayClient):
        return
    if client.timeout >= TURTLEBOT3_CHAT_TIMEOUT:
        return
    gateway_url = str(ctx.obj.get("missionos_gateway_url") or DEFAULT_GATEWAY_URL)
    ctx.obj["missionos_client"] = make_client(gateway_url, TURTLEBOT3_CHAT_TIMEOUT)
