#!/usr/bin/env python3
"""Compatibility entrypoint for the maintained TurtleBot3 chat E2E runner."""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.runtime import turtlebot3_chat_e2e_runner as _runner  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(_runner.main())


sys.modules[__name__] = _runner
