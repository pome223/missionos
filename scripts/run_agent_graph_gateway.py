#!/usr/bin/env python3
"""Run the integrated Gateway against an explicit local state directory.

The DeepSeek key is read from Secret Manager into process memory. This launcher
does not approve missions, run simulations, or install a system service.
"""

import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--task-db", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument("--secret-project", required=True)
    parser.add_argument("--secret-name", default="deepseek-api-key")
    parser.add_argument("--enable-live-sitl", action="store_true")
    parser.add_argument(
        "--jev-mode",
        choices=("off", "shadow", "primary", "cascade", "cascade_shadow"),
        default=None,
        help="Override MISSIONOS_JEV_MODE from the process environment or state-root .env (default: off).",
    )
    parser.add_argument("--jev-secret-name", default="jev-api-key")
    parser.add_argument("--navigation-wam-mode", choices=("off", "shadow", "required"))
    parser.add_argument(
        "--navigation-wam-config", type=Path,
        help="Operator-owned navigation WAM manifest; relative paths use --state-root.",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    state_root = args.state_root.resolve(strict=True)
    task_db = args.task_db.resolve()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    # The state directory supplies operator-owned config, never source code.
    from dotenv import dotenv_values

    env = os.environ.copy()
    for key, value in dotenv_values(state_root / ".env").items():
        if value is not None:
            env.setdefault(key, value)
    jev_mode = args.jev_mode if args.jev_mode is not None else env.get("MISSIONOS_JEV_MODE", "off")
    if jev_mode not in {"off", "shadow", "primary", "cascade", "cascade_shadow"}:
        parser.error("MISSIONOS_JEV_MODE must be off, shadow, primary, cascade, or cascade_shadow")
    if jev_mode in {"cascade", "cascade_shadow"}:
        if env.get("MISSIONOS_JEV_CASCADE_FAST_PATH", "disabled") not in {
            "disabled", "fixture_verified_detour_v1"
        }:
            parser.error("invalid MISSIONOS_JEV_CASCADE_FAST_PATH")
    wam_mode = args.navigation_wam_mode or env.get("MISSIONOS_NAVIGATION_WAM_MODE", "off")
    if wam_mode not in {"off", "shadow", "required"}:
        parser.error("MISSIONOS_NAVIGATION_WAM_MODE must be off, shadow, or required")
    wam_config = args.navigation_wam_config or env.get("MISSIONOS_NAVIGATION_WAM_CONFIG", "")
    if wam_mode != "off":
        if not wam_config:
            parser.error("navigation WAM requires MISSIONOS_NAVIGATION_WAM_CONFIG")
        config_path = Path(wam_config)
        if not config_path.is_absolute():
            config_path = state_root / config_path
        if not config_path.is_file():
            parser.error("navigation WAM config file does not exist")
        env["MISSIONOS_NAVIGATION_WAM_CONFIG"] = str(config_path.resolve())
    env["MISSIONOS_NAVIGATION_WAM_MODE"] = wam_mode
    key_result = subprocess.run(
        [
            "gcloud",
            f"--project={args.secret_project}",
            "secrets",
            "versions",
            "access",
            "latest",
            f"--secret={args.secret_name}",
        ],
        capture_output=True,
        check=False,
    )
    if key_result.returncode:
        raise SystemExit("DeepSeek secret could not be read; Gateway was not started")
    env["DEEPSEEK_API_KEY"] = key_result.stdout.decode().strip()
    if jev_mode != "off":
        jev_secret = subprocess.run(
            [
                "gcloud",
                f"--project={args.secret_project}",
                "secrets",
                "versions",
                "access",
                "latest",
                f"--secret={args.jev_secret_name}",
            ],
            capture_output=True,
        )
        if jev_secret.returncode or not jev_secret.stdout.strip():
            raise SystemExit("Jev secret could not be read; Gateway was not started")
        env["TYPESAFE_API_KEY"] = jev_secret.stdout.decode().strip()
    env.update(
        {
            "MISSIONOS_LLM_BACKEND": "deepseek",
            "MISSIONOS_AGENT_RUNTIME_ADK_ENABLED": "1",
            "MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED": "1",
            "MISSIONOS_JEV_MODE": jev_mode,
            "MISSIONOS_ADK_V2_GRAPH_PRIMARY": "1",
            "MISSIONOS_ADK_V2_GRAPH_ROLLBACK": "0",
            "MISSIONOS_ADK_V2_GRAPH_SHADOW": "0",
            "MISSIONOS_LLM_DIALOGUE_ROUTER_ADK_ENABLED": "0",
            "MISSIONOS_GATEWAY_BACKEND": "production",
            "TASK_STORE_DB_PATH": str(task_db),
            "PYTHONPATH": os.pathsep.join(
                str(root / part)
                for part in (
                    ".",
                    "packages/missionos-core/src",
                    "packages/missionos-gateway/src",
                    "packages/missionos-cli/src",
                )
            ),
        }
    )
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    env["MISSIONOS_DEPLOYMENT_REVISION"] = revision
    if args.enable_live_sitl:
        sys.path[:0] = [str(root / "packages/missionos-cli/src")]
        from missionos_cli.gateway_process import _GATEWAY_LIVE_SITL_ENV

        env.update(_GATEWAY_LIVE_SITL_ENV)
    else:
        # Defaults must remain planning-only even if the parent's dotenv opted in.
        for key in list(env):
            if key.startswith("RUN_") and ("SITL" in key or "GUI_DISPATCH" in key):
                env[key] = "0"
    os.chdir(state_root)
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


if __name__ == "__main__":
    main()
