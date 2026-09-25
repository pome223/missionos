#!/usr/bin/env python3
"""Start an explicitly opted-in local Go2 Gateway with host-only Agent credentials."""

import argparse
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument("--state-dir", type=Path, default=Path("output/go2-agent"))
    parser.add_argument("--secret-project")
    parser.add_argument("--secret-id")
    args = parser.parse_args()
    if any(
        os.getenv(k) != "1"
        for k in ("RUN_MISSIONOS_GO2_DELIVERY_SIM", "RUN_MISSIONOS_GO2_SUPERVISOR")
    ):
        parser.error("requires RUN_MISSIONOS_GO2_DELIVERY_SIM=1 and RUN_MISSIONOS_GO2_SUPERVISOR=1")
    if bool(args.secret_project) != bool(args.secret_id):
        parser.error("provide both --secret-project and --secret-id")
    if args.secret_project:
        result = subprocess.run(
            [
                "gcloud",
                "secrets",
                "versions",
                "access",
                "latest",
                f"--project={args.secret_project}",
                f"--secret={args.secret_id}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode or not result.stdout.strip():
            parser.error("could not access the configured host credential")
        os.environ["DEEPSEEK_API_KEY"] = result.stdout.strip()
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        parser.error("requires a host DEEPSEEK_API_KEY or explicit Secret Manager locator")
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        MISSIONOS_GO2_SUPERVISION_MODE="agent",
        MISSIONOS_AGENT_MISSIONOS_GO2_SUPERVISOR_AGENT_LLM_BACKEND="deepseek",
        MISSIONOS_AGENT_MISSIONOS_GO2_SUPERVISOR_AGENT_MODEL_ID="deepseek-flash",
        MISSIONOS_GO2_OUTPUT_ROOT=str(state / "runs"),
        TASK_STORE_DB_PATH=str(state / "gateway-tasks.db"),
        AUDIT_LOG_PATH=str(state / "audit.log"),
    )
    from src.gateway.server import create_missionos_gateway

    create_missionos_gateway().run(host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
