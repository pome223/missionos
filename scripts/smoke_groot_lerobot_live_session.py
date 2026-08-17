#!/usr/bin/env python3
"""Run the production LeRobot Repair CLI through its CPU fixture backend."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts import run_groot_lerobot_same_world_repair as production_cli  # noqa: E402


def main() -> int:
    with TemporaryDirectory(prefix="missionos-lerobot-production-cli-smoke-") as directory:
        root = Path(directory)
        checkpoint = root / "unused-fixture-checkpoint"
        checkpoint.mkdir()
        output = root / "result.json"
        os.environ[production_cli.OPT_IN_ENV] = "1"
        os.environ[production_cli.FIXTURE_OPT_IN_ENV] = "1"
        previous_argv = sys.argv
        try:
            sys.argv = [
                "run_groot_lerobot_same_world_repair.py",
                "--runtime",
                "fixture",
                "--checkpoint-path",
                str(checkpoint),
                "--operator-approval-ref",
                "operator:production-cli-smoke",
                "--dispatch-state-path",
                str(root / "dispatch.json"),
                "--output",
                str(output),
                "--maximum-repair-chunks",
                "2",
            ]
            exit_code = production_cli.main()
        finally:
            sys.argv = previous_argv
        report = json.loads(output.read_text(encoding="utf-8"))
    if exit_code != 0 or report.get("fixture_runtime_verified") is not True:
        raise RuntimeError("LeRobot production CLI fixture boundary did not close")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
