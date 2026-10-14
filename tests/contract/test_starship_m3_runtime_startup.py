"""Readiness checks never create a physics worker, forecast or hosted call."""

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "m3_startup_smoke",
    Path(__file__).resolve().parents[2] / "scripts/smoke_starship_m3_http_flight.py",
)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_bootstrap_imports_core_without_existing_pythonpath(tmp_path):
    tree = tmp_path / "checkout"
    (tree / "scripts").mkdir(parents=True)
    (tree / "scripts/driver.py").write_text(Path(SPEC.origin).read_text())
    core = tree / "packages/missionos-core/src/missionos_core"
    core.mkdir(parents=True)
    (core / "__init__.py").write_text("LOCAL_CHECKOUT = True\n")
    code = (
        "import runpy; runpy.run_path("
        + repr(str(tree / "scripts/driver.py"))
        + ", run_name='bootstrap_only'); "
        "import missionos_core; assert missionos_core.LOCAL_CHECKOUT; print('core_from_checkout')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "core_from_checkout"
    assert not (tree / "output").exists()


def test_worker_environment_has_all_local_packages_without_provider_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-secret-must-not-be-forwarded")
    monkeypatch.setenv("PATH", "fixture-path")
    values = smoke.clean_environment(tmp_path)
    assert values["PYTHONPATH"].split(os.pathsep) == smoke.package_paths(tmp_path)
    assert values["PATH"] == "fixture-path" and "TYPESAFE_API_KEY" not in values


@pytest.mark.parametrize(
    "name", ["missionos_core", "package.submodule", "/private/secret", "bad secret"]
)
def test_initialization_diagnostics_never_persist_exception_text(tmp_path, monkeypatch, name):
    error = ModuleNotFoundError("Authorization: Bearer example-private-token", name=name)

    @contextmanager
    def failed(_):
        raise error
        yield

    monkeypatch.setattr(smoke, "gateway_runtime", failed)
    monkeypatch.setattr(
        smoke.subprocess, "Popen", lambda *_a, **_k: pytest.fail("worker must not start")
    )
    args = SimpleNamespace(output_dir=tmp_path / "attempt")
    assert smoke.host(args) == 2
    value = json.loads((args.output_dir / "host-failure.json").read_text())
    assert value["reason"] == "host_runtime_initialization_failed"
    assert value.get("missing_module") == (
        name if name in {"missionos_core", "package.submodule"} else None
    )
    assert "example-private-token" not in json.dumps(value)
    assert not (args.output_dir / "preflight.json").exists()


def test_runtime_failure_cleanup_restores_environment_and_closes_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_PARENT_VALUE", "fixture-only")
    before, cwd = dict(os.environ), Path.cwd()

    class Socket:
        closed = False

        def bind(self, _):
            raise ModuleNotFoundError("arbitrary secret", name="missionos_core")

        def close(self):
            self.closed = True

    sock = Socket()
    monkeypatch.setattr(smoke.socket, "socket", lambda: sock)
    with pytest.raises(ModuleNotFoundError):
        with smoke.gateway_runtime(tmp_path):
            pytest.fail("must not become ready")
    assert sock.closed and dict(os.environ) == before and Path.cwd() == cwd


def test_runtime_preflight_time_is_included_in_original_flight_clock(tmp_path, monkeypatch):
    clock, order = [10.0], []

    @contextmanager
    def ready(_):
        order.append("http_ready")
        clock[0] = 13.0
        yield {"checks": {"health_ready": True}}
        order.append("http_closed")

    def flight(args, runtime, started):
        assert started == 10.0 and clock[0] == 13.0
        assert (args.output_dir / "runtime-check.json").is_file()
        order.append("worker_may_start")
        return 0

    monkeypatch.setattr(smoke, "gateway_runtime", ready)
    monkeypatch.setattr(smoke, "flight_host", flight)
    monkeypatch.setattr(smoke.time, "monotonic", lambda: clock[0])
    assert smoke.host(SimpleNamespace(output_dir=tmp_path / "attempt")) == 0
    assert order == ["http_ready", "worker_may_start", "http_closed"]


def test_runtime_cli_route_cannot_enter_flight_or_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "host", lambda _: pytest.fail("flight entered"))
    monkeypatch.setattr(smoke, "worker", lambda _: pytest.fail("worker entered"))
    monkeypatch.setattr(smoke, "check_runtime", lambda _: 0)
    assert smoke.main(["--check-runtime", "--output-dir", str(tmp_path / "ready")]) == 0
    with pytest.raises(SystemExit):
        smoke.main(
            ["--check-runtime", "--approve-simulation", "--output-dir", str(tmp_path / "bad")]
        )


def test_real_gateway_runtime_check_without_a_worker(tmp_path, monkeypatch):
    # Locate production modules from the test process; the staged CLI itself is
    # loaded above. No fake app, provider or numerical actor is used.
    import src

    root = Path(src.__file__).resolve().parents[1]
    monkeypatch.setattr(smoke, "ROOT", root)
    for path in reversed(smoke.package_paths(root)):
        monkeypatch.syspath_prepend(path)
    monkeypatch.setattr(
        smoke.subprocess, "Popen", lambda *_a, **_k: pytest.fail("no worker allowed")
    )
    original_environment, cwd = dict(os.environ), Path.cwd()
    output = tmp_path / "runtime-only"
    assert smoke.main(["--check-runtime", "--output-dir", str(output)]) == 0
    result = json.loads((output / "runtime-check.json").read_text())
    assert result["passed"] and all(result["checks"].values())
    assert result["worker_spawned"] is False
    assert (
        result["model_calls"]
        == result["simulator_runs"]
        == result["forecasts"]
        == result["operator_approvals"]
        == 0
    )
    assert not (output / "flight").exists() and not (output / "preflight.json").exists()
    assert dict(os.environ) == original_environment and Path.cwd() == cwd


def test_relative_host_paths_survive_gateway_cwd_before_worker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reference.json").write_text('{"fixture": "saved-prefix"}')
    order = []

    @contextmanager
    def ready(output):
        previous = Path.cwd()
        os.chdir(output)
        try:
            order.append("gateway_ready")
            yield {"checks": {"health_ready": True}}
        finally:
            os.chdir(previous)
            order.append("gateway_closed")

    def flight(args, runtime, started):
        assert args.reference_prefix == tmp_path / "reference.json"
        assert args.output_dir == tmp_path / "attempt"
        assert json.loads(args.reference_prefix.read_text()) == {"fixture": "saved-prefix"}
        order.append("reference_read")
        assert (args.output_dir / "runtime-check.json").exists()
        order.append("worker_may_start")
        return 0

    monkeypatch.setattr(smoke, "gateway_runtime", ready)
    monkeypatch.setattr(smoke, "flight_host", flight)
    assert (
        smoke.host(
            SimpleNamespace(output_dir=Path("attempt"), reference_prefix=Path("reference.json"))
        )
        == 0
    )
    assert order == ["gateway_ready", "reference_read", "worker_may_start", "gateway_closed"]
    assert Path.cwd() == tmp_path


@pytest.mark.parametrize("free,size,accepted", [(2000, 100, True), (999, 100, False), (2000, 1000, False)])
def test_storage_budget_preserves_partial_evidence(tmp_path, monkeypatch, free, size, accepted):
    monkeypatch.setitem(smoke.BUDGET, "maximum_output_bytes", 1000)
    monkeypatch.setitem(smoke.BUDGET, "minimum_free_bytes", 1000)
    monkeypatch.setattr(smoke.shutil, "disk_usage", lambda _: SimpleNamespace(free=free))
    evidence = tmp_path / "partial.json"
    evidence.write_bytes(b"x" * size)
    if accepted:
        assert all(smoke.check_storage_budget(tmp_path)["checks"].values())
    else:
        with pytest.raises(OSError, match="fixed_flight_storage_budget_exhausted"):
            smoke.check_storage_budget(tmp_path)
    assert evidence.read_bytes() == b"x" * size
    record = json.loads((tmp_path / "storage-budget.json").read_text())
    assert record["output_bytes"] == size and record["free_bytes"] == free
    assert record["files_deleted"] == 0
