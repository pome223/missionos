#!/usr/bin/env python3
"""Run the standard LIBERO Panda stage from server-owned configuration.

The parent coordinator supplies run, episode, output, and contract identities.
The server supplies model and policy-service locations. Operator text controls
neither side of this boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import re
import subprocess
import sys


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name.lower()}_missing")
    return value


def _policy_port(environment: Mapping[str, str]) -> int:
    raw = environment.get("MISSIONOS_LIBERO_POLICY_CLIENT_PORT", "5555")
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise RuntimeError("missionos_libero_policy_client_port_invalid") from error
    if isinstance(raw, bool) or value < 1 or value > 65535:
        raise RuntimeError("missionos_libero_policy_client_port_invalid")
    return value


def build_stage_command(
    environment: Mapping[str, str],
    *,
    python_executable: str = sys.executable,
) -> tuple[str, ...]:
    """Build the content-bound official-runner invocation."""

    run_identity = _required(environment, "MISSIONOS_PARENT_RUN_IDENTITY")
    episode_identity = _required(
        environment,
        "MISSIONOS_LIBERO_EPISODE_IDENTITY",
    )
    result_path = _required(environment, "MISSIONOS_LIBERO_RESULT_PATH")
    expected_contract = _required(
        environment,
        "MISSIONOS_EXPECTED_LIBERO_CONTRACT_SHA256",
    )
    if _SHA256.fullmatch(expected_contract) is None:
        raise RuntimeError("missionos_expected_libero_contract_sha256_invalid")

    model_path = _required(environment, "MISSIONOS_LIBERO_MODEL_PATH")
    reference_model_path = environment.get(
        "MISSIONOS_LIBERO_REFERENCE_MODEL_PATH",
        model_path,
    ).strip()
    if not reference_model_path:
        reference_model_path = model_path
    policy_host = environment.get(
        "MISSIONOS_LIBERO_POLICY_CLIENT_HOST",
        "127.0.0.1",
    ).strip()
    if not policy_host:
        raise RuntimeError("missionos_libero_policy_client_host_invalid")

    return (
        python_executable,
        "scripts/run_libero_panda_instrumented_live.py",
        "--model-path",
        model_path,
        "--reference-model-path",
        reference_model_path,
        "--policy-client-host",
        policy_host,
        "--policy-client-port",
        str(_policy_port(environment)),
        "--run-identity",
        run_identity,
        "--episode-identity",
        episode_identity,
        "--expected-contract-sha256",
        expected_contract,
        "--maximum-observation-age-seconds",
        "30",
        "--maximum-rollout-elapsed-seconds",
        "600",
        "--output",
        str(Path(result_path)),
    )


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        raise SystemExit("this command accepts server configuration only")
    command = build_stage_command(os.environ)
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
