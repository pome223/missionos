"""Real CLI subprocess smoke; all model/executor input is explicitly synthetic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT / "packages/missionos-cli/src"), str(ROOT)]),
    }
    count = 0

    def cli(*args, expected=0):
        nonlocal count
        command = [sys.executable, "-m", "missionos_cli", "assurance-policy", *map(str, args)]
        result = subprocess.run(
            command, cwd=ROOT, env=env, text=True, capture_output=True, check=False, timeout=60
        )
        assert result.returncode == expected, (command, result.stdout, result.stderr)
        count += 1
        return json.loads(result.stdout) if result.stdout.strip() else {}

    with tempfile.TemporaryDirectory(prefix="assurance-policy-smoke-") as directory:
        root = Path(directory)
        db = root / "policy.db"
        source = ROOT / "examples/assurance-policy/fixture.yaml"
        reviewed = cli("validate", source)
        assert reviewed["approved"] is False
        sha = reviewed["sha256"]
        cli("fixture", "--db", db, "--sha256", sha, "--proposal-id", "unapproved", expected=1)
        cli("approve", source, "--db", db, "--operator", "fixture-operator", "--sha256", sha)

        def run(proposal, expected=0, *extra):
            return cli(
                "fixture",
                "--db",
                db,
                "--sha256",
                sha,
                "--proposal-id",
                proposal,
                *extra,
                expected=expected,
            )["continuation"]

        first = run("one")
        assert first["executor_invoked"] and not first["human_approval_observed"]
        assert first["authorization_source"] == "human_approved_policy"
        assert first["physical_execution_invoked"] is False
        assert "policy_proposal_already_reserved" in run("one", 2)["blocking_reasons"]
        assert (
            "policy_parameter_out_of_range:target_x_m"
            in run("outside", 2, "--target-x", "50")["blocking_reasons"]
        )
        assert run("two")["executor_invoked"]
        assert run("three")["executor_invoked"]
        assert "policy_budget_exhausted" in run("four", 2)["blocking_reasons"]
        cli("revoke", "--db", db, "--sha256", sha)
        cli("fixture", "--db", db, "--sha256", sha, "--proposal-id", "revoked", expected=1)

        policy = reviewed["policy"]
        policy["mode"] = "shadow"
        policy["mission_id"] = "shadow-fixture"
        shadow = root / "shadow.yaml"
        shadow.write_text(yaml.safe_dump(policy))
        sha = cli("validate", shadow)["sha256"]
        cli("approve", shadow, "--db", db, "--operator", "fixture-operator", "--sha256", sha)
        assert not run("shadow-one", 2)["executor_invoked"]
        passed = run("shadow-one", 0, "--approve-action")
        assert passed["human_approval_observed"] and passed["executor_invoked"]
        assert not passed["policy_authorization"]["policy_authorized"]
    print(
        json.dumps(
            {
                "status": "passed",
                "cli_subprocesses": count,
                "fixture_only": True,
                "live_model_invoked": False,
                "physical_execution_invoked": False,
            }
        )
    )


if __name__ == "__main__":
    main()
