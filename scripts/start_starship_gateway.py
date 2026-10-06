#!/usr/bin/env python3
"""Start an isolated loopback Gateway; secrets stay in its process environment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _read_secret(project: str, secret: str) -> str:
    try:
        result = subprocess.run(
            [
                "gcloud",
                "secrets",
                "versions",
                "access",
                "latest",
                "--project",
                project,
                "--secret",
                secret,
                "--quiet",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode or not result.stdout.strip():
            raise RuntimeError("starship_gateway_secret_unavailable")
        return result.stdout.decode("utf-8").strip()
    except Exception:
        raise RuntimeError("starship_gateway_secret_unavailable") from None


def build_environment(args: argparse.Namespace) -> dict[str, str]:
    if not 1 <= args.port <= 65535:
        raise ValueError("starship_gateway_invalid_port")
    live_shadow = getattr(args, "enable_live_jev_shadow", False)
    live_supervisor = getattr(args, "enable_live_flight_supervisor", False)
    if (args.enable_live_models or live_shadow or live_supervisor) and not args.project:
        raise ValueError("starship_gateway_secret_project_required")
    for value in (args.project, args.deepseek_secret, args.jev_secret):
        if value and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError("starship_gateway_invalid_secret_locator")
    state = args.state_dir.expanduser().absolute()
    if state.is_symlink() or (state / ".env").exists():
        raise ValueError("starship_gateway_isolated_state_required")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = state.resolve()
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SYSTEMROOT"}
    }
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(
                str(ROOT / part)
                for part in (
                    ".",
                    "packages/missionos-core/src",
                    "packages/missionos-gateway/src",
                    "packages/missionos-cli/src",
                )
            ),
            "MISSIONOS_GATEWAY_BACKEND": "production",
            "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_PORT": str(args.port),
            "TASK_STORE_DB_PATH": str(state / "tasks.db"),
            "AUDIT_LOG_PATH": str(state / "audit.log"),
            "MEMORY_DB_PATH": str(state / "memory.db"),
            "MISSIONOS_STARSHIP_STATE_DIR": str(state / "runs"),
            "MISSIONOS_LLM_BACKEND": "off",
            "MISSIONOS_AGENT_RUNTIME_ADK_ENABLED": "0",
            "MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED": "0",
            "MISSIONOS_LLM_DIALOGUE_ROUTER_ADK_ENABLED": "0",
            "MISSIONOS_STARSHIP_PLANNER_MODE": "fixture" if args.fixture_planner else "off",
            "MISSIONOS_STARSHIP_LIVE_MODELS_ENABLED": "0",
            "MISSIONOS_STARSHIP_DEEPSEEK_MAX_CALLS": "2",
            "MISSIONOS_STARSHIP_JEV_MAX_CALLS": "1",
            "MISSIONOS_STARSHIP_JEV_MODE": "off",
            "MISSIONOS_STARSHIP_JEV_SHADOW_MODE": "fixture" if args.fixture_planner else "off",
            "MISSIONOS_STARSHIP_FLIGHT_SUPERVISOR_MODE": "fixture" if args.fixture_planner else "off",
            "LITELLM_LOG": "ERROR",
        }
    )
    if args.enable_live_models:
        env["DEEPSEEK_API_KEY"] = _read_secret(args.project, args.deepseek_secret)
        env["TYPESAFE_API_KEY"] = _read_secret(args.project, args.jev_secret)
        env.update(
            {
                "MISSIONOS_STARSHIP_PLANNER_MODE": "deepseek",
                "MISSIONOS_STARSHIP_LIVE_MODELS_ENABLED": "1",
                "MISSIONOS_STARSHIP_JEV_SHADOW_MODE": "live",
                "MISSIONOS_AGENT_MISSIONOS_STARSHIP_PLANNER_AGENT_LLM_BACKEND": "deepseek",
                "MISSIONOS_AGENT_MISSIONOS_STARSHIP_PLANNER_AGENT_DEEPSEEK_API_BASE": "https://api.deepseek.com",
            }
        )
    elif live_shadow:
        env["TYPESAFE_API_KEY"] = _read_secret(args.project, args.jev_secret)
        env["MISSIONOS_STARSHIP_JEV_SHADOW_MODE"] = "live"
    if live_supervisor:
        if "DEEPSEEK_API_KEY" not in env:
            env["DEEPSEEK_API_KEY"] = _read_secret(args.project, args.deepseek_secret)
        if "TYPESAFE_API_KEY" not in env:
            env["TYPESAFE_API_KEY"] = _read_secret(args.project, args.jev_secret)
        env["MISSIONOS_STARSHIP_FLIGHT_SUPERVISOR_MODE"] = "live"
        env["MISSIONOS_AGENT_MISSIONOS_STARSHIP_PLANNER_AGENT_LLM_BACKEND"] = "deepseek"
        env["MISSIONOS_AGENT_MISSIONOS_STARSHIP_PLANNER_AGENT_DEEPSEEK_API_BASE"] = "https://api.deepseek.com"
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18794)
    parser.add_argument("--project")
    parser.add_argument("--deepseek-secret", default="deepseek-api-key")
    parser.add_argument("--jev-secret", default="jev-api-key")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--enable-live-models", action="store_true")
    modes.add_argument("--fixture-planner", action="store_true")
    parser.add_argument("--enable-live-jev-shadow", action="store_true",
                        help="Enable separately approved Jev shadow with at most 22 calls per plan")
    parser.add_argument("--enable-live-flight-supervisor", action="store_true",
                        help="Enable separately approved in-flight Jev (one) and conditional DeepSeek (one) proposals")
    args = parser.parse_args()
    try:
        env = build_environment(args)
    except (ValueError, RuntimeError):
        parser.error(
            "Starship Gateway configuration or credential unavailable; Gateway not started"
        )
    os.chdir(args.state_dir.expanduser().resolve())
    os.execve(
        sys.executable,
        [
            sys.executable,
            "-P",
            "-m",
            "missionos_gateway",
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
        ],
        env,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
