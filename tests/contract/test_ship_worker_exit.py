"""Success finalization cannot turn a runtime/cleanup exception into success."""

from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("failure", [False, True])
def test_worker_entrypoint_preserves_error_and_flushes_success(failure):
    path = Path(__file__).resolve().parents[2] / "scripts/ship_delivery_sitl_worker.py"
    program = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('isolated_worker', sys.argv[1])
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
def explicit_double():
    print('evidence closed', end='')
    if sys.argv[2] == 'fail':
        raise RuntimeError('runtime or cleanup failed')
worker.main = explicit_double
worker.run_worker()
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(path), "fail" if failure else "pass"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout == "evidence closed"
    assert (result.returncode != 0) is failure
    assert ("runtime or cleanup failed" in result.stderr) is failure
