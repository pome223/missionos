#!/usr/bin/env python3
"""Start an isolated loopback Gateway; secrets stay in its process environment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
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


def read_operator_token(path: Path) -> str:
    """Read a host-only token; never pass it to the model or flight subprocess."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 256):
                raise ValueError
            token = handle.read(257).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
            raise ValueError
        return token
    except (OSError, ValueError, UnicodeError):
        raise ValueError("starship_inflight_operator_token_file_invalid") from None


def build_environment(args: argparse.Namespace) -> dict[str, str]:
    if not 1 <= args.port <= 65535:
        raise ValueError("starship_gateway_invalid_port")
    live_shadow = getattr(args, "enable_live_jev_shadow", False)
    live_supervisor = getattr(args, "enable_live_flight_supervisor", False)
    live_director = getattr(args, "enable_live_mission_director", False)
    if (args.enable_live_models or live_shadow or live_supervisor or live_director) and not args.project:
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
            "MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE": "fixture" if args.fixture_planner else "off",
            "LITELLM_LOG": "ERROR",
        }
    )
    operator_file = getattr(args, "inflight_operator_token_file", None)
    operator_id = getattr(args, "inflight_operator_id", None)
    operator_source = getattr(args, "inflight_operator_source", "human_ui")
    if (operator_file is None) != (operator_id is None):
        raise ValueError("starship_inflight_operator_configuration_incomplete")
    if operator_file is not None:
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", operator_id)
                or operator_source not in {"human_ui", "scripted_test_driver"}):
            raise ValueError("starship_inflight_operator_identity_invalid")
        env.update(MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_TOKEN=read_operator_token(Path(operator_file)),
                   MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_ID=operator_id,
                   MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_SOURCE=operator_source)
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
    if live_supervisor or live_director:
        if "DEEPSEEK_API_KEY" not in env:
            env["DEEPSEEK_API_KEY"] = _read_secret(args.project, args.deepseek_secret)
        if "TYPESAFE_API_KEY" not in env:
            env["TYPESAFE_API_KEY"] = _read_secret(args.project, args.jev_secret)
        if live_supervisor:
            env["MISSIONOS_STARSHIP_FLIGHT_SUPERVISOR_MODE"] = "live"
        if live_director:
            env["MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE"] = "live"
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
    parser.add_argument("--enable-live-mission-director", action="store_true",
                        help="Enable separately approved mission decisions: at most five Jev and five conditional DeepSeek calls per plan")
    parser.add_argument("--inflight-operator-token-file", type=Path,
                        help="Private mode-600 bearer file, read only by the Gateway host")
    parser.add_argument("--inflight-operator-id", help="Configured local operator principal; not SSO identity")
    parser.add_argument("--inflight-operator-source", choices=("human_ui", "scripted_test_driver"),
                        default="human_ui")
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
