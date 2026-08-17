from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "probe_groot_lerobot_state_snapshot.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "probe_groot_lerobot_state_snapshot",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_file_round_trip_is_exact_and_digest_bound(tmp_path: Path) -> None:
    module = _load_script()
    snapshot = np.asarray([0.0, -1.25, 3.5, 9.0], dtype=np.float64)
    destination = tmp_path / "nested" / "state.npy"

    observed_sha256 = module._write_snapshot(destination, snapshot)

    assert destination.is_file()
    assert observed_sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert np.array_equal(
        snapshot,
        np.load(destination, allow_pickle=False),
    )


def test_snapshot_probe_requires_explicit_opt_in(monkeypatch) -> None:
    module = _load_script()
    monkeypatch.delenv(module.OPT_IN_ENV, raising=False)
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH)])

    try:
        module.main()
    except SystemExit as exc:
        assert str(exc) == f"set {module.OPT_IN_ENV}=1 to run this simulator probe"
    else:
        raise AssertionError("snapshot probe ran without explicit opt-in")
